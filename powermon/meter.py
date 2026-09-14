"""采样调度、能耗积分、分时计费与持久化。

**时间口径**：分两层，都不含糊。

* **开机时长** 由 ``poweron.session_start()`` 从 System 日志的
  Kernel-Boot 事件还原——这才是用户认知里的「这次开机」。
  不用 ``psutil.boot_time()`` / ``GetTickCount64``：睡眠与「快速启动」
  会让它们严重偏离（实测报 72.7 小时，实际只有 4.9 小时）。
* **能耗** 只能统计程序自己跑着的那段时间（``covered_seconds``），
  开机后到程序启动前的部分无从测量，因此额外给一个按均值外推的
  参考值，并明确标注是估算。

**计费**：按用户所在地区的居民电价分时段累计。时段与电价都来自配置
（``peak_hours`` / ``valley_hours`` 与三个单价），所以在托盘菜单
「电价设置…」里换成自己省份的预设即可，计价逻辑不用改。
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from .config import STATE_PATH, Config
from .poweron import session_start
from .sensors import Reading, SensorHub
from .tariffs import parse_hours

# 曲线按 10 秒一个点聚合，滚动保留 2 小时
CURVE_BUCKET = 10.0
CURVE_POINTS = int(2 * 3600 / CURVE_BUCKET)
# state.json 落盘间隔（秒）
PERSIST_INTERVAL = 60.0


def segment_of(cfg: Config, when: time.struct_time | None = None) -> str:
    """当前时段名称：峰段 / 平段 / 谷段。

    时段划分支持跨零点，写法见 ``tariffs.parse_hours``。峰段优先于谷段，
    都不匹配就是平段 —— 所以没配峰段的地区（如四川）非谷段全按平段计，
    而没配谷段的地区全天都是平段（即平价计费）。
    """
    tm = when or time.localtime()
    hour = tm.tm_hour
    if cfg.peak_hours and hour in parse_hours(cfg.peak_hours):
        return "峰段"
    if cfg.valley_hours and hour in parse_hours(cfg.valley_hours):
        return "谷段"
    return "平段"


def rate_at(cfg: Config, when: time.struct_time | None = None) -> tuple[str, float]:
    """当前时段及其电价。谷段按丰/枯水期取价（多数地区两者相同）。"""
    tm = when or time.localtime()
    segment = segment_of(cfg, tm)
    if segment == "峰段":
        return segment, cfg.price_peak
    if segment == "谷段":
        wet = tm.tm_mon in (cfg.valley_wet_months or ())
        return segment, (cfg.price_valley_wet if wet else cfg.price_valley_dry)
    return segment, cfg.price_flat


def is_valley(cfg: Config, when: time.struct_time | None = None) -> bool:
    """当前是否处于低谷时段。"""
    return segment_of(cfg, when) == "谷段"


def valley_price(cfg: Config, when: time.struct_time | None = None) -> float:
    """当前谷段电价（非谷段时返回该地区的谷段单价，供界面展示）。"""
    tm = when or time.localtime()
    wet = tm.tm_mon in (cfg.valley_wet_months or ())
    return cfg.price_valley_wet if wet else cfg.price_valley_dry


@dataclass
class Snapshot:
    """给界面用的只读快照。"""

    # 本次统计
    session_wh: float
    session_cost: float
    covered_seconds: float
    average_w: float
    peak_w: float
    samples: int
    peak_wh: float
    valley_wh: float
    # 当前
    current_w: float
    cpu_w: float
    gpu_w: float
    base_w: float
    cpu_util: float
    in_valley: bool
    # 今日
    today_wh: float
    today_cost: float
    # 全局
    total_wh: float
    total_sessions: int
    # 时间
    power_on_ts: float
    power_on_source: str
    first_seen_ts: float
    # 数据源
    cpu_source: str
    gpu_source: str
    cpu_estimated: bool
    gpu_measured: bool
    gpu_names: list[str] = field(default_factory=list)
    gpu_limits: list[float] = field(default_factory=list)
    # 当前所处时段与电价（来自配置的地区预设）
    segment: str = "平段"
    rate: float = 0.0

    @property
    def power_on_seconds(self) -> float:
        """本次开机到现在（真实口径）。"""
        return max(0.0, time.time() - self.power_on_ts)

    @property
    def observed_hours(self) -> float:
        """程序实际统计到的时长（小时）。"""
        return self.covered_seconds / 3600.0

    @property
    def estimated_wh(self) -> float:
        """按已统计均值外推到整段开机时长的参考能耗。

        只在统计满 15 分钟、且确实还有未覆盖的时段时才给出，
        否则短时间的抖动会被放大成一个离谱的数字。
        """
        hours = self.power_on_seconds / 3600.0
        if self.observed_hours < 0.25 or hours <= self.observed_hours:
            return self.session_wh
        return self.session_wh / self.observed_hours * hours


class EnergyMeter:
    """后台采样线程 + 能耗账本。"""

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._hub = SensorHub(cfg)
        self._lock = threading.RLock()

        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

        # 本次会话
        self._power_on_ts: float = 0.0
        self._power_on_source: str = "-"
        self._first_seen_ts: float = 0.0
        self._session_wh: float = 0.0
        self._session_cost: float = 0.0
        self._peak_wh: float = 0.0
        self._valley_wh: float = 0.0
        self._peak_w: float = 0.0
        self._samples: int = 0
        self._covered_seconds: float = 0.0

        # 今日
        self._today_date: str = ""
        self._today_wh: float = 0.0
        self._today_cost: float = 0.0

        # 全局
        self._total_wh: float = 0.0
        self._total_sessions: int = 0

        self._latest: Reading | None = None
        self._curve: deque[list[float]] = deque(maxlen=CURVE_POINTS)
        self._last_persist = 0.0

        self._restore()

    # ------------------------------------------------------------- 账本

    @staticmethod
    def _today_key() -> str:
        return time.strftime("%Y-%m-%d")

    def _restore(self) -> None:
        power_on_ts, source = session_start()
        self._power_on_ts = power_on_ts
        self._power_on_source = source

        state: dict = {}
        if STATE_PATH.exists():
            try:
                state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}

        self._total_wh = float(state.get("total_wh", 0.0))
        self._total_sessions = int(state.get("total_sessions", 0))

        # 以「本次开机时刻」作为会话标识：同一次开机内重开程序接着上次记账，
        # 重新开机（含早上快速启动恢复）就另起一段。
        last_power_on = float(state.get("power_on_ts", 0.0))
        same_session = abs(last_power_on - power_on_ts) < 30.0
        if same_session:
            self._first_seen_ts = float(state.get("first_seen_ts", time.time()))
            self._session_wh = float(state.get("session_wh", 0.0))
            self._session_cost = float(state.get("session_cost", 0.0))
            self._peak_wh = float(state.get("peak_wh", 0.0))
            self._valley_wh = float(state.get("valley_wh", 0.0))
            self._peak_w = float(state.get("peak_w", 0.0))
            self._samples = int(state.get("samples", 0))
            self._covered_seconds = float(state.get("covered_seconds", 0.0))
            for item in state.get("curve", []):
                try:
                    ts, total, count = float(item[0]), float(item[1]), float(item[2])
                except (TypeError, ValueError, IndexError):
                    continue
                if time.time() - ts <= CURVE_POINTS * CURVE_BUCKET:
                    self._curve.append([ts, total, count])
        else:
            self._first_seen_ts = time.time()
            self._total_sessions += 1

        # 今日数据只在同一天内继承
        if str(state.get("today_date", "")) == self._today_key():
            self._today_date = self._today_key()
            self._today_wh = float(state.get("today_wh", 0.0))
            self._today_cost = float(state.get("today_cost", 0.0))
        else:
            self._today_date = self._today_key()

    def reset_session(self) -> None:
        """清零本次统计（保留历史总量与今日累计）。"""
        with self._lock:
            self._first_seen_ts = time.time()
            self._session_wh = 0.0
            self._session_cost = 0.0
            self._peak_wh = 0.0
            self._valley_wh = 0.0
            self._peak_w = 0.0
            self._samples = 0
            self._covered_seconds = 0.0
            self._curve.clear()
        self._persist(force=True)

    def _persist(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_persist < PERSIST_INTERVAL:
            return
        self._last_persist = now
        with self._lock:
            payload = {
                "power_on_ts": self._power_on_ts,
                "first_seen_ts": self._first_seen_ts,
                "session_wh": round(self._session_wh, 4),
                "session_cost": round(self._session_cost, 4),
                "peak_wh": round(self._peak_wh, 4),
                "valley_wh": round(self._valley_wh, 4),
                "peak_w": round(self._peak_w, 2),
                "samples": self._samples,
                "covered_seconds": round(self._covered_seconds, 2),
                "today_date": self._today_date,
                "today_wh": round(self._today_wh, 4),
                "today_cost": round(self._today_cost, 4),
                "total_wh": round(self._total_wh, 4),
                "total_sessions": self._total_sessions,
                "curve": [
                    [round(ts, 1), round(total, 3), int(count)]
                    for ts, total, count in self._curve
                ],
                "saved_at": time.time(),
            }
        try:
            STATE_PATH.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    # ------------------------------------------------------------- 采样

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="power-sampler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        self._persist(force=True)
        self._hub.close()

    def _loop(self) -> None:
        interval = max(0.5, self._cfg.sample_interval)
        prev_ts = time.monotonic()
        prev_w: float | None = None

        while not self._stop.is_set():
            try:
                reading = self._hub.read()
            except Exception:  # noqa: BLE001 - 单次采样失败不能让线程退出
                self._stop.wait(interval)
                continue

            now = time.monotonic()
            dt = now - prev_ts
            prev_ts = now

            tm = time.localtime(reading.ts)
            today = time.strftime("%Y-%m-%d", tm)
            segment, rate = rate_at(self._cfg, tm)
            valley = segment == "谷段"

            with self._lock:
                self._latest = reading
                self._samples += 1
                self._peak_w = max(self._peak_w, reading.total_w)

                # 跨天就重新开始记"今日"
                if today != self._today_date:
                    self._today_date = today
                    self._today_wh = 0.0
                    self._today_cost = 0.0

                # 只有在合理的时间步长内才积分；睡眠/挂起留下的大空档直接跳过，
                # 免得把休眠时长乘上功率算成虚高的能耗
                if prev_w is not None and 0.0 < dt < interval * 5:
                    energy_wh = (prev_w + reading.total_w) / 2.0 * dt / 3600.0
                    money = energy_wh / 1000.0 * rate

                    self._session_wh += energy_wh
                    self._session_cost += money
                    self._total_wh += energy_wh
                    self._today_wh += energy_wh
                    self._today_cost += money
                    self._covered_seconds += dt

                    if valley:
                        self._valley_wh += energy_wh
                    else:
                        self._peak_wh += energy_wh

                prev_w = reading.total_w

                bucket_ts = int(reading.ts / CURVE_BUCKET) * CURVE_BUCKET
                if self._curve and self._curve[-1][0] == bucket_ts:
                    self._curve[-1][1] += reading.total_w
                    self._curve[-1][2] += 1
                else:
                    self._curve.append([bucket_ts, reading.total_w, 1])

            self._persist()
            self._stop.wait(interval)

    # ------------------------------------------------------------- 快照

    def snapshot(self) -> Snapshot:
        with self._lock:
            latest = self._latest
            hours = self._covered_seconds / 3600.0
            avg = self._session_wh / hours if hours > 0 else 0.0
            now_tm = time.localtime()
            return Snapshot(
                session_wh=self._session_wh,
                session_cost=self._session_cost,
                covered_seconds=self._covered_seconds,
                average_w=avg,
                peak_w=self._peak_w,
                samples=self._samples,
                peak_wh=self._peak_wh,
                valley_wh=self._valley_wh,
                current_w=latest.total_w if latest else 0.0,
                cpu_w=latest.cpu_w if latest else 0.0,
                gpu_w=latest.gpu_w if latest else 0.0,
                base_w=latest.base_w if latest else 0.0,
                cpu_util=latest.cpu_util if latest else 0.0,
                in_valley=is_valley(self._cfg, now_tm),
                segment=segment_of(self._cfg, now_tm),
                rate=rate_at(self._cfg, now_tm)[1],
                today_wh=self._today_wh,
                today_cost=self._today_cost,
                total_wh=self._total_wh,
                total_sessions=self._total_sessions,
                power_on_ts=self._power_on_ts,
                power_on_source=self._power_on_source,
                first_seen_ts=self._first_seen_ts,
                cpu_source=latest.cpu_source if latest else "-",
                gpu_source=latest.gpu_source if latest else "-",
                cpu_estimated=latest.cpu_estimated if latest else True,
                gpu_measured=latest.gpu_measured if latest else False,
                gpu_names=self._hub.gpu.names,
                gpu_limits=self._hub.gpu.limits,
            )

    def curve(self) -> list[tuple[float, float]]:
        with self._lock:
            return [
                (ts, total / count) for ts, total, count in self._curve if count > 0
            ]

    @property
    def config(self) -> Config:
        return self._cfg

    def apply_config(self, cfg: Config) -> None:
        with self._lock:
            self._cfg = cfg
            self._hub.reload(cfg)


__all__ = [
    "EnergyMeter",
    "Snapshot",
    "is_valley",
    "rate_at",
    "segment_of",
    "valley_price",
]
