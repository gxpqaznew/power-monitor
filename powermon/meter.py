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

import atexit
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from .config import STATE_PATH, Config, atomic_write_text, migrate_user_data
from .poweron import session_start
from .sensors import Reading, SensorHub
from .tariffs import parse_hours

# 曲线按 10 秒一个点聚合，滚动保留 2 小时
CURVE_BUCKET = 10.0
CURVE_POINTS = int(2 * 3600 / CURVE_BUCKET)
# state.json 落盘间隔（秒）。
# 原来是 60 秒，被任务管理器结束 / 断电时最多丢一分钟的账。账本本身很小
# （几 KB），15 秒一次的开销可以忽略，但丢数据的窗口缩小到 1/4。
PERSIST_INTERVAL = 15.0
# 每日账本最多保留多少天（约 13 个月）。再老的只留在 total_wh 里。
HISTORY_DAYS = 400


def _num_seq(source, index: int, default: float = 0.0) -> float:
    """从序列（``[电量, 电费, 秒数]`` 这种紧凑写法）里安全取一个数。"""
    try:
        value = source[index]
    except (IndexError, KeyError, TypeError):
        return default
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _num(source, key: str, default: float = 0.0) -> float:
    """从 dict 里安全取一个数。

    账本是用户可见、可手改的文件（托盘菜单里就有「打开配置文件」），里面出现
    ``null``／字符串／被改坏的数字都要能扛住 —— 不能因为一个字段读不出来就
    让整本账作废。
    """
    try:
        value = source.get(key, default)
    except AttributeError:
        return default
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


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

    # ---- 历史累计（在最后并带默认值：旧代码 / 测试里手工造的快照可以不填） ----
    # 本月：按日历月把每日账本加起来
    month_wh: float = 0.0
    month_cost: float = 0.0
    month_days: int = 0
    # 累计：开这个程序以来所有天
    total_cost: float = 0.0
    total_days: int = 0
    # 最近几天：(日期 "MM-DD", 电量 Wh, 电费)
    recent_days: list[tuple[str, float, float]] = field(default_factory=list)

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
        self._stopped = False

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

        # 每日账本：{"YYYY-MM-DD": {"wh":…, "cost":…, "seconds":…}}
        # 这是「关掉再打开还能看到以前每天用了多少」的唯一来源 —— 只存一个
        # total 是不够的，用户要看的是「哪天用了多少」。
        self._days: dict[str, dict[str, float]] = {}

        # 全局
        self._total_wh: float = 0.0
        self._total_cost: float = 0.0
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

        # 数据目录里还没有账本时，先看看旧位置（exe 同目录 / dist / 安装目录）
        # 有没有能接手的 —— 升级换了路径也不能把历史当没发生过。
        if not STATE_PATH.exists():
            try:
                migrate_user_data()
            except Exception:  # noqa: BLE001 - 迁移失败不该挡住启动
                pass

        state: dict = {}
        if STATE_PATH.exists():
            try:
                state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}

        self._total_wh = _num(state, "total_wh")
        self._total_cost = _num(state, "total_cost")
        self._total_sessions = int(_num(state, "total_sessions"))

        # 以「本次开机时刻」作为会话标识：同一次开机内重开程序接着上次记账，
        # 重新开机（含早上快速启动恢复）就另起一段。
        last_power_on = _num(state, "power_on_ts")
        same_session = abs(last_power_on - power_on_ts) < 30.0
        if same_session:
            self._first_seen_ts = float(state.get("first_seen_ts", time.time()))
            self._session_wh = _num(state, "session_wh")
            self._session_cost = _num(state, "session_cost")
            self._peak_wh = _num(state, "peak_wh")
            self._valley_wh = _num(state, "valley_wh")
            self._peak_w = _num(state, "peak_w")
            self._samples = int(_num(state, "samples"))
            self._covered_seconds = _num(state, "covered_seconds")
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

        self._restore_days(state)

        # 旧版没有 total_cost（只存了 session/today 的电费）。如果 days 里已经
        # 有历史电费，就把它补齐 —— 否则「累计电量」几千瓦时、「累计电费」却是
        # ¥0.00，看着像坏了。（days 有 400 天上限，所以这只是个下限，够用。）
        if self._total_cost <= 0.0:
            self._total_cost = sum(item["cost"] for item in self._days.values())

    def _restore_days(self, state: dict) -> None:
        """恢复每日账本，并把旧版「今日」字段折算进去。

        旧版只存 today_wh / today_cost 一个当天的桶，隔天启动就直接丢弃 ——
        用户看到的就是「电费重新统计了，没有之前的记录」。

        这里做两件事：
          1. 把旧字段按它自己的 today_date 折进 days（不丢最后那一天）；
          2. 把 days 里今天的条目接上，让程序重启后「今日」接着算。
        """
        raw = state.get("days")
        if isinstance(raw, dict):
            for day, item in raw.items():
                key = str(day)[:10]
                if len(key) != 10 or key[4] != "-":
                    continue
                # 自己写出去的是 [电量, 电费, 秒数]；手改过的可能是对象写法，
                # 两种都认，免得用户编辑一下文件整本账就哑了。
                if isinstance(item, dict):
                    wh, cost, seconds = (
                        _num(item, "wh"), _num(item, "cost"), _num(item, "seconds"),
                    )
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    wh, cost = _num_seq(item, 0), _num_seq(item, 1)
                    seconds = _num_seq(item, 2)
                else:
                    continue
                self._days[key] = {
                    "wh": max(0.0, wh),
                    "cost": max(0.0, cost),
                    "seconds": max(0.0, seconds),
                }

        # 旧版的单日桶 → days（只在 days 里还没有那天时补，不覆盖已有的）
        legacy_date = str(state.get("today_date", ""))[:10]
        legacy_wh = _num(state, "today_wh")
        if legacy_date and legacy_wh > 0 and legacy_date not in self._days:
            self._days[legacy_date] = {
                "wh": legacy_wh,
                "cost": _num(state, "today_cost"),
                "seconds": 0.0,
            }

        self._prune_days()

        today = self._today_key()
        self._today_date = today
        if today in self._days:
            self._today_wh = self._days[today]["wh"]
            self._today_cost = self._days[today]["cost"]
        else:
            self._today_wh = 0.0
            self._today_cost = 0.0

    def _prune_days(self) -> None:
        """只留最近 HISTORY_DAYS 天（按日期字符串排序，格式是 YYYY-MM-DD）。"""
        if len(self._days) <= HISTORY_DAYS:
            return
        keep = sorted(self._days)[-HISTORY_DAYS:]
        self._days = {k: self._days[k] for k in keep}

    def _bump_day(self, day: str, wh: float, cost: float, seconds: float) -> None:
        """把这一笔记到「那一天」头上。调用方必须已经持有 ``self._lock``。"""
        item = self._days.get(day)
        if item is None:
            item = {"wh": 0.0, "cost": 0.0, "seconds": 0.0}
            self._days[day] = item
        item["wh"] += wh
        item["cost"] += cost
        item["seconds"] += seconds

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
                # 每日账本写成 [电量Wh, 电费, 统计秒数] 的紧凑数组 —— 一天 3 个数，
                # 一年也就几 KB，比一天一个对象省一半体积。
                "days": {
                    day: [
                        round(item["wh"], 3),
                        round(item["cost"], 5),
                        round(item["seconds"], 1),
                    ]
                    for day, item in sorted(self._days.items())[-HISTORY_DAYS:]
                },
                "total_wh": round(self._total_wh, 4),
                "total_cost": round(self._total_cost, 5),
                "total_sessions": self._total_sessions,
                "curve": [
                    [round(ts, 1), round(total, 3), int(count)]
                    for ts, total, count in self._curve
                ],
                "saved_at": time.time(),
            }
        # 原子替换：中途断电/被强杀也不能留下半截 JSON，否则下次启动整本账
        # 都读不出来（比丢十几秒严重得多）
        atomic_write_text(
            STATE_PATH, json.dumps(payload, ensure_ascii=False)
        )

    def save_now(self) -> None:
        """立刻落盘（退出、关机、注销前调）。"""
        self._persist(force=True)

    # ------------------------------------------------------------- 采样

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="power-sampler", daemon=True
        )
        self._thread.start()
        # 兜底：正常退出路径由 app._shutdown 负责落盘，这里再挂一层，
        # 保证「解释器退出 / 未捕获异常」这类路径也不会把最后一段账丢掉。
        # save_now 只写内存快照，重复调用是安全的。
        atexit.register(self.save_now)

    def stop(self) -> None:
        """停止采样并**强制落盘**。

        幂等：重复调用（正常退出 + atexit 兜底都会调）不会再关一次传感器。
        """
        if self._stopped:
            return
        self._stopped = True
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3.0)
        self._persist(force=True)
        try:
            self._hub.close()
        except Exception:  # noqa: BLE001 - 退出路径上不该再抛
            pass

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

                # 跨天就换到新的「今日」桶。已经记进 days 的那天原样留着，
                # 新的一天如果早些时候跑过程序（days 里已有）就接着累加。
                if today != self._today_date:
                    self._today_date = today
                    existing = self._days.get(today)
                    self._today_wh = existing["wh"] if existing else 0.0
                    self._today_cost = existing["cost"] if existing else 0.0

                # 只有在合理的时间步长内才积分；睡眠/挂起留下的大空档直接跳过，
                # 免得把休眠时长乘上功率算成虚高的能耗
                if prev_w is not None and 0.0 < dt < interval * 5:
                    energy_wh = (prev_w + reading.total_w) / 2.0 * dt / 3600.0
                    money = energy_wh / 1000.0 * rate

                    self._session_wh += energy_wh
                    self._session_cost += money
                    self._total_wh += energy_wh
                    self._total_cost += money
                    self._today_wh += energy_wh
                    self._today_cost += money
                    self._covered_seconds += dt
                    self._bump_day(today, energy_wh, money, dt)

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
            month = time.strftime("%Y-%m", now_tm)
            month_wh = 0.0
            month_cost = 0.0
            month_days = 0
            for day, item in self._days.items():
                if day.startswith(month):
                    month_wh += item["wh"]
                    month_cost += item["cost"]
                    month_days += 1
            recent = [
                (day[5:], item["wh"], item["cost"])
                for day, item in sorted(self._days.items())[-7:]
            ]
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
                month_wh=month_wh,
                month_cost=month_cost,
                month_days=month_days,
                total_wh=self._total_wh,
                total_cost=self._total_cost,
                total_sessions=self._total_sessions,
                total_days=len(self._days),
                recent_days=recent,
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
