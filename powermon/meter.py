"""采样调度、能耗积分、分时计费与持久化。

**时间口径**：分两层，都不含糊。

* **开机时长** 由 ``poweron.session_start()`` 从 System 日志的
  Kernel-Boot 事件还原——这才是用户认知里的「这次开机」。
  不用 ``psutil.boot_time()`` / ``GetTickCount64``：睡眠与「快速启动」
  会让它们严重偏离（实测报 72.7 小时，实际只有 4.9 小时）。
* **能耗** 只能统计程序自己跑着的那段时间（``covered_seconds``），
  开机后到程序启动前的部分无从测量，因此额外给一个按均值外推的
  参考值，并明确标注是估算。

**计费：账本只记电量，电费是电量的一个「视图」**（v1.1.0 起的核心约定）。

早期版本把电费当成独立累加器（``total_cost`` / ``session_cost`` 各加各的），
结果是这两条数会**互相矛盾**：实测用户账本里出现「累计电量 = 本次电量 = 3.70 kWh，
但累计电费 ¥1.16 < 本次电费 ¥1.64」——因为 ``total_cost`` 是从**残缺的每日账本**
回填的（旧版本只存了当天一个桶，更早的历史只活在 total_wh 里），而 ``session_cost``
继承的是老版本在**旧电价**下累加的值。两者来源不同，就会出现「累计比本次还低」
这种物理上不可能的数字。

现在的做法：每个维度的账目只存**电量**，并按「峰 / 平 / 谷」拆开
（谷段还要分丰枯，所以必须存拆分量才可能算对单价）：

* ``wh`` / ``peak`` / ``valley`` → 平段电量 = ``wh - peak - valley``（不会为负，读了会夹）
* 电费一律由 ``_blend()`` 现算：``峰×峰价 + 平×平价 + 谷×谷价``，
  谷价按**那一天所在的月份**决定丰/枯。

由此得到两个必然成立的性质，它们就是这套设计的意义：

1. **累计 ≥ 今年 ≥ 本月 ≥ 今日，且 累计 ≥ 本次**（同一套单价、电量单调，
   电费只是电量的单调映射）；「累计电费比本次电费低」在结构上不可能再发生。
2. **改了电价，所有历史数字一起跟着变**——因为电费从来不是存下来的。
   这也符合直觉：用户把电价改成账单上的数，当然希望历史电费按新价重算。

旧账（v1）里那些只有 ``[电量, 电费, 秒数]`` 没有峰谷拆分的记录，迁移时按
**整体观测到的峰谷比例**补齐（而不是一律算平段——那样会和总量对不上）。

时段与电价都来自配置（``peak_hours`` / ``valley_hours`` 与三个单价），
所以在托盘菜单「电价设置…」里换成自己省份的预设即可，计价逻辑不用改。
"""

from __future__ import annotations

import atexit
import json
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from .config import (
    STATE_PATH,
    Config,
    atomic_write_text,
    migrate_user_data,
    snapshot_state,
)
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
# 「每次开机」的明细最多保留多少条。一条几十字节，240 条也就十几 KB，
# 够翻大半年的开机关机记录。
HISTORY_SESSIONS = 240
# 时段分布的桶数（0~23 点）
HOUR_BUCKETS = 24
# 账本格式版本。
#   v1：每条 [电量, 电费, 秒数(, 最后采样)] —— 电费是独立累加器，会互相矛盾
#   v2：每条 [电量, 秒数, (最后采样), 峰电量, 谷电量] —— 只存电量，电费现算
# 有版本号才能无歧义地区分两者（只靠数组长度猜太脆）。
LEDGER_VERSION = 2


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


# 周几（``time.struct_time.tm_wday`` 是 0=周一）
WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def _duration(seconds: float) -> str:
    """紧凑时长：45m / 2h13m / 3d4h。

    统计表里一列放得下，比「2 小时 13 分」省地方，也不会在窄窗口里折行。
    这里刻意不 import panel.fmt_duration —— panel 反过来要 import meter，
    会绕成循环。
    """
    minutes = int(max(0.0, seconds) // 60)
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{mins:02d}m"
    return f"{mins}m"


def day_tm(day: str) -> time.struct_time | None:
    """``"YYYY-MM-DD"`` → struct_time；读不出来返回 None（坏日期不能崩）。"""
    try:
        return time.strptime(day[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def valley_rate(cfg: Config, tm: time.struct_time | None = None) -> float:
    """谷段单价 —— 按 ``tm`` 所在月份决定丰水期还是枯平水期。"""
    month = (tm or time.localtime()).tm_mon
    wet = month in (cfg.valley_wet_months or ())
    return cfg.price_valley_wet if wet else cfg.price_valley_dry


def blend_cost(cfg: Config, tm: time.struct_time | None,
               wh: float, peak_wh: float, valley_wh: float) -> float:
    """由电量按**当前电价**算电费。这是全程序唯一的电费算法。

    峰 / 谷电量超出总量时（手改过账本）夹回，保证平段电量不为负 ——
    否则会出现「负电量」这种会让用户彻底不信账的数字。
    """
    if wh <= 0.0:
        return 0.0
    peak_wh = min(max(0.0, peak_wh), wh)
    valley_wh = min(max(0.0, valley_wh), wh - peak_wh)
    flat_wh = wh - peak_wh - valley_wh
    return (peak_wh * cfg.price_peak
            + flat_wh * cfg.price_flat
            + valley_wh * valley_rate(cfg, tm)) / 1000.0


def split_by_ratio(wh: float, peak_ratio: float,
                   valley_ratio: float) -> tuple[float, float]:
    """按给定比例把一段电量拆成 (峰, 谷)。

    只用于**旧账迁移**：老账目没有峰谷拆分，若一律当平段计，会和总量对不上
    （用户的账本里平段电量为 0，正是因为老版本的峰谷电量已经覆盖了全部电量），
    于是「每日账本电费之和」会大于「累计电费」。按整体观测到的比例补齐，
    每日合计恰好等于总量，两边的电费也就自动一致了。
    """
    if wh <= 0.0:
        return 0.0, 0.0
    peak = max(0.0, min(1.0, peak_ratio)) * wh
    valley = max(0.0, min(1.0 - peak_ratio, valley_ratio)) * wh
    return peak, valley


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

        # 本次会话。电费不在这里存 —— 它由 (wh, _peak_wh, _valley_wh) 现算，
        # 见 blend_cost()。
        self._power_on_ts: float = 0.0
        self._power_on_source: str = "-"
        self._first_seen_ts: float = 0.0
        self._session_wh: float = 0.0
        self._peak_wh: float = 0.0
        self._valley_wh: float = 0.0
        self._peak_w: float = 0.0
        self._samples: int = 0
        self._covered_seconds: float = 0.0

        # 今日（只有日期 + 电量；今天是哪天由 _days 里的条目表达）
        self._today_date: str = ""

        # 每日账本：{"YYYY-MM-DD": {"wh","peak","valley","seconds"}}
        # 这是「关掉再打开还能看到以前每天用了多少」的唯一来源 —— 只存一个
        # total 是不够的，用户要看的是「哪天用了多少」。
        self._days: dict[str, dict[str, float]] = {}

        # 每次开机的明细：{开机时刻(int): {"wh","peak","valley","seconds","last"}}
        # 「每次开机」不等于「当前这次」—— 用户要的是能把历史每一次翻出来看，
        # 所以按开机时刻（Kernel-Boot 事件）分条存，同一次开机内重开程序接上同一条。
        self._sessions: dict[int, dict[str, float]] = {}
        self._boot_key: int = 0

        # 时段分布：[0 点, 1 点, … 23 点] 各自累计了多少 Wh。
        # 用来回答「这台机器一天里什么时候最费电」，比总量有意思得多。
        # 谷段电量单独记一列，否则按时段表的电费只能按平段估算。
        self._hours: list[float] = [0.0] * HOUR_BUCKETS
        self._hours_valley: list[float] = [0.0] * HOUR_BUCKETS

        # 全局（开程序以来所有天，**不随 400 天裁剪而减少**）。
        # 峰 / 谷电量一起记，累计电费才能由它现算 —— 只存一个 total_wh 是
        # 「累计电费靠回填、和本次对不上」的根源。
        self._total_wh: float = 0.0
        self._total_peak_wh: float = 0.0
        self._total_valley_wh: float = 0.0
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
            # 开始往这本账上写之前先留一份「上一代」：任何把 state.json 清掉或写坏
            # 的意外，都能靠 state.json.prev 原样救回来（下次启动会自动接手它）。
            try:
                snapshot_state()
            except Exception:  # noqa: BLE001 - 备份失败不能挡住启动
                pass
            try:
                state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                state = {}

        self._ledger_version = int(_num(state, "ledger_version", 1.0))
        self._total_wh = _num(state, "total_wh")
        self._total_peak_wh = _num(state, "total_peak_wh")
        self._total_valley_wh = _num(state, "total_valley_wh")
        self._total_sessions = int(_num(state, "total_sessions"))

        # 以「本次开机时刻」作为会话标识：同一次开机内重开程序接着上次记账，
        # 重新开机（含早上快速启动恢复）就另起一段。
        last_power_on = _num(state, "power_on_ts")
        same_session = abs(last_power_on - power_on_ts) < 30.0
        if same_session:
            self._first_seen_ts = float(state.get("first_seen_ts", time.time()))
            self._session_wh = _num(state, "session_wh")
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

        # 旧账没有全局峰谷电量时，用本次会话的峰谷比例反推全局 —— 实测用户的旧账本里
        # 「峰 + 谷」正好等于全部电量（平段为 0），直接照搬比例能让「每日合计」和
        # 「累计」自动对上；比例未知（全是平段）时就是 0，也自洽。
        if self._total_peak_wh <= 0.0 and self._total_valley_wh <= 0.0:
            self._migrate_total_split(state)

        self._restore_days(state)
        self._restore_sessions(state)
        self._restore_hours(state)

    def _migrate_total_split(self, state: dict) -> None:
        """旧账补齐全局峰/谷电量（按会话的峰谷比例缩放）。"""
        peak = _num(state, "peak_wh")
        valley = _num(state, "valley_wh")
        span = peak + valley
        if span <= 0.0 or self._total_wh <= 0.0:
            return
        scale = self._total_wh / span
        self._total_peak_wh = peak * scale
        self._total_valley_wh = valley * scale

    def _legacy_split_ratio(self) -> tuple[float, float]:
        """旧账迁移用的 (峰比例, 谷比例)。没有拆分信息时返回 (0, 0)（即全按平段）。

        这是 v1 → v2 迁移的关键：老账目只有「电量 + 电费」，没有峰谷拆分。
        若一律按平段补，每日合计会比累计还大（实测用户账本平段电量为 0），
        补出来的账自己就打自己脸。按观测比例补齐，两边自动相等。
        """
        span = self._total_peak_wh + self._total_valley_wh
        if span <= 0.0 or self._total_wh <= 0.0:
            return 0.0, 0.0
        return self._total_peak_wh / self._total_wh, self._total_valley_wh / self._total_wh

    def _restore_days(self, state: dict) -> None:
        """恢复每日账本，并把旧版「今日」字段折算进去。

        旧版只存 today_wh / today_cost 一个当天的桶，隔天启动就直接丢弃 ——
        用户看到的就是「电费重新统计了，没有之前的记录」。

        这里做两件事：
          1. 把旧字段按它自己的 today_date 折进 days（不丢最后那一天）；
          2. 把 days 里今天的条目接上，让程序重启后「今日」接着算。

        每条要读两种写法：
          * v2（自己写出去的）``[电量, 秒数, 峰电量, 谷电量]``
          * v1（老版本）``[电量, 电费, 秒数]`` —— 电费丢弃（它已经不可信），
            峰谷按整体观测比例补齐
          * 用户手改成 ``{"wh":…}`` 对象的，也认
        """
        peak_ratio, valley_ratio = self._legacy_split_ratio()
        version = getattr(self, "_ledger_version", 1)
        raw = state.get("days")
        if isinstance(raw, dict):
            for day, item in raw.items():
                key = str(day)[:10]
                if len(key) != 10 or key[4] != "-":
                    continue
                if isinstance(item, dict):
                    wh = _num(item, "wh")
                    seconds = _num(item, "seconds")
                    peak = _num(item, "peak")
                    valley = _num(item, "valley")
                elif isinstance(item, (list, tuple)) and len(item) >= 2:
                    wh = _num_seq(item, 0)
                    if version >= 2:
                        # [电量, 秒数, 峰, 谷]
                        seconds = _num_seq(item, 1)
                        peak, valley = _num_seq(item, 2), _num_seq(item, 3)
                    else:
                        # [电量, 电费, 秒数] —— 电费没有拆分，按比例补
                        seconds = _num_seq(item, 2)
                        peak, valley = split_by_ratio(wh, peak_ratio, valley_ratio)
                else:
                    continue
                self._days[key] = {
                    "wh": max(0.0, wh),
                    "peak": max(0.0, peak),
                    "valley": max(0.0, valley),
                    "seconds": max(0.0, seconds),
                }

        # 旧版的单日桶 → days（只在 days 里还没有那天时补，不覆盖已有的）
        legacy_date = str(state.get("today_date", ""))[:10]
        legacy_wh = _num(state, "today_wh")
        if legacy_date and legacy_wh > 0 and legacy_date not in self._days:
            peak, valley = split_by_ratio(legacy_wh, peak_ratio, valley_ratio)
            self._days[legacy_date] = {
                "wh": legacy_wh,
                "peak": peak,
                "valley": valley,
                "seconds": 0.0,
            }

        self._prune_days()

        today = self._today_key()
        self._today_date = today
        # 今日电量没有单独的累加器 —— 它就是 days 里今天那一条（单一真源），
        # snapshot() 直接取，省掉一个必然会和账本对不上的副本。

    def _prune_days(self) -> None:
        """只留最近 HISTORY_DAYS 天（按日期字符串排序，格式是 YYYY-MM-DD）。"""
        if len(self._days) <= HISTORY_DAYS:
            return
        keep = sorted(self._days)[-HISTORY_DAYS:]
        self._days = {k: self._days[k] for k in keep}

    # ------------------------------------------------------ 每次开机 / 时段

    def _restore_sessions(self, state: dict) -> None:
        """恢复「每次开机用了多少」的明细。

        键用**开机时刻**（System 日志里的 Kernel-Boot 事件时间）：

        * 同一次开机内反复开关程序 → 落在同一条上接着累加，不会碎成一堆小条目；
        * 真正重启之后开机时刻变了 → 自然分出一条新记录。

        这正是用户说的「每次开机后分别用了多少（每次，不是当前次）」。
        """
        peak_ratio, valley_ratio = self._legacy_split_ratio()
        version = getattr(self, "_ledger_version", 1)
        raw = state.get("sessions")
        pairs: list[tuple[object, object]] = []
        if isinstance(raw, dict):
            pairs = list(raw.items())
        elif isinstance(raw, list):
            pairs = list(enumerate(raw))
        for key, item in pairs:
            try:
                boot = int(float(key))
            except (TypeError, ValueError):
                continue
            if boot <= 0:
                continue
            if isinstance(item, dict):
                wh = _num(item, "wh")
                seconds, last = _num(item, "seconds"), _num(item, "last")
                peak, valley = _num(item, "peak"), _num(item, "valley")
            elif isinstance(item, (list, tuple)) and len(item) >= 3:
                wh = _num_seq(item, 0)
                if version >= 2:
                    # [电量, 秒数, 最后采样, 峰, 谷]
                    seconds, last = _num_seq(item, 1), _num_seq(item, 2)
                    peak, valley = _num_seq(item, 3), _num_seq(item, 4)
                else:
                    # [电量, 电费, 秒数, 最后采样] —— 电费丢弃，按比例补峰谷
                    seconds, last = _num_seq(item, 2), _num_seq(item, 3)
                    peak, valley = split_by_ratio(wh, peak_ratio, valley_ratio)
            else:
                continue
            self._sessions[boot] = {
                "wh": max(0.0, wh),
                "peak": max(0.0, peak),
                "valley": max(0.0, valley),
                "seconds": max(0.0, seconds),
                "last": last or float(boot),
            }

        # 本次开机：有就接上，没有就新建 —— 保证「当前这一次」也在明细里看得见
        boot_key = int(self._power_on_ts or self._first_seen_ts or time.time())
        self._boot_key = boot_key
        entry = self._sessions.get(boot_key)
        if entry is None:
            self._sessions[boot_key] = {
                "wh": 0.0,
                "peak": 0.0,
                "valley": 0.0,
                "seconds": 0.0,
                "last": max(self._first_seen_ts, boot_key),
            }
        else:
            entry["last"] = max(entry["last"], self._first_seen_ts)
        self._prune_sessions()

    def _prune_sessions(self) -> None:
        """只留最近 HISTORY_SESSIONS 次开机（按开机时刻排序）。"""
        if len(self._sessions) <= HISTORY_SESSIONS:
            return
        keep = sorted(self._sessions)[-HISTORY_SESSIONS:]
        self._sessions = {k: self._sessions[k] for k in keep}

    def _bump_session(self, ts: float, wh: float, peak: float, valley: float,
                      seconds: float) -> None:
        """把这一笔记到**本次开机**头上。调用方必须已持有 ``self._lock``。"""
        item = self._sessions.get(self._boot_key)
        if item is None:
            item = {"wh": 0.0, "peak": 0.0, "valley": 0.0,
                    "seconds": 0.0, "last": ts}
            self._sessions[self._boot_key] = item
        item["wh"] += wh
        item["peak"] += peak
        item["valley"] += valley
        item["seconds"] += seconds
        item["last"] = ts

    def _restore_hours(self, state: dict) -> None:
        """恢复时段分布（0~23 点各累计多少 Wh）。长度对不上一律重置。"""
        self._hours = self._read_hour_buckets(state.get("hours"))
        self._hours_valley = self._read_hour_buckets(state.get("hours_valley"))

    @staticmethod
    def _read_hour_buckets(raw) -> list[float]:
        values: list[float] = []
        if isinstance(raw, list):
            values = [_num_seq(raw, i) for i in range(len(raw))]
        elif isinstance(raw, dict):
            values = [_num(raw, str(i)) for i in range(HOUR_BUCKETS)]
        if len(values) != HOUR_BUCKETS:
            # 长度不对（手改坏了 / 旧格式）就整段重置，别按位数错位对齐
            return [0.0] * HOUR_BUCKETS
        return [max(0.0, v) for v in values]

    def _bump_hour(self, hour: int, wh: float, valley_wh: float) -> None:
        """记到「几点钟」这个桶上。调用方必须已持有 ``self._lock``。"""
        if 0 <= hour < HOUR_BUCKETS:
            self._hours[hour] += wh
            self._hours_valley[hour] += valley_wh

    def _bump_day(self, day: str, wh: float, peak: float, valley: float,
                  seconds: float) -> None:
        """把这一笔记到「那一天」头上。调用方必须已经持有 ``self._lock``。"""
        item = self._days.get(day)
        if item is None:
            item = {"wh": 0.0, "peak": 0.0, "valley": 0.0, "seconds": 0.0}
            self._days[day] = item
        item["wh"] += wh
        item["peak"] += peak
        item["valley"] += valley
        item["seconds"] += seconds

    def reset_session(self) -> None:
        """清零本次统计（保留每日 / 每次开机 / 时段账本与累计电量）。"""
        with self._lock:
            self._first_seen_ts = time.time()
            self._session_wh = 0.0
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
                "ledger_version": LEDGER_VERSION,
                "power_on_ts": self._power_on_ts,
                "first_seen_ts": self._first_seen_ts,
                "session_wh": round(self._session_wh, 4),
                "peak_wh": round(self._peak_wh, 4),
                "valley_wh": round(self._valley_wh, 4),
                "peak_w": round(self._peak_w, 2),
                "samples": self._samples,
                "covered_seconds": round(self._covered_seconds, 2),
                "today_date": self._today_date,
                # 每日账本写成 [电量Wh, 秒数, 峰电量, 谷电量] 的紧凑数组 ——
                # 一天 4 个数、一年也就几 KB。**不存电费**：电费是电量的换算，
                # 存下来就会出现「改了电价历史还是旧价」和「累计比本次还低」。
                "days": {
                    day: [
                        round(item["wh"], 3),
                        round(item["seconds"], 1),
                        round(item["peak"], 3),
                        round(item["valley"], 3),
                    ]
                    for day, item in sorted(self._days.items())[-HISTORY_DAYS:]
                },
                # 每次开机一条：[电量Wh, 秒数, 最后采样时刻, 峰电量, 谷电量]。
                # 键是开机时刻，所以「同一次开机内重开程序」不会碎成多条。
                "sessions": {
                    str(boot): [
                        round(item["wh"], 3),
                        round(item["seconds"], 1),
                        round(item["last"], 1),
                        round(item["peak"], 3),
                        round(item["valley"], 3),
                    ]
                    for boot, item in sorted(self._sessions.items())[-HISTORY_SESSIONS:]
                },
                # 0~23 点各累计多少 Wh（跨所有天累加，用来找「几点最费电」）
                "hours": [round(v, 3) for v in self._hours],
                "hours_valley": [round(v, 3) for v in self._hours_valley],
                "total_wh": round(self._total_wh, 4),
                # 全局峰谷电量：累计电费由它现算，是「累计 ≥ 本次」的结构保证
                "total_peak_wh": round(self._total_peak_wh, 4),
                "total_valley_wh": round(self._total_valley_wh, 4),
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
            # 只取时段名 —— 电价不在这里乘，电费一律由 blend_cost() 按电量现算
            segment = segment_of(self._cfg, tm)
            valley = segment == "谷段"

            with self._lock:
                self._latest = reading
                self._samples += 1
                self._peak_w = max(self._peak_w, reading.total_w)

                # 跨天只更新「今天」是哪天 —— 电量本身记在 days 里，
                # 新的一天如果没有历史条目就从 0 开始（_bump_day 会建）。
                if today != self._today_date:
                    self._today_date = today

                # 只有在合理的时间步长内才积分；睡眠/挂起留下的大空档直接跳过，
                # 免得把休眠时长乘上功率算成虚高的能耗
                if prev_w is not None and 0.0 < dt < interval * 5:
                    energy_wh = (prev_w + reading.total_w) / 2.0 * dt / 3600.0
                    # 峰 / 谷电量分开记：电费是它们乘上电价现算的（见 blend_cost），
                    # 所以电量拆得越细，改电价后重算得越准。
                    valley_wh = energy_wh if valley else 0.0
                    peak_wh = energy_wh if (segment == "峰段") else 0.0

                    self._session_wh += energy_wh
                    self._total_wh += energy_wh
                    self._covered_seconds += dt
                    if valley:
                        self._valley_wh += energy_wh
                        self._total_valley_wh += energy_wh
                    elif segment == "峰段":
                        self._peak_wh += energy_wh
                        self._total_peak_wh += energy_wh
                    self._bump_day(today, energy_wh, peak_wh, valley_wh, dt)
                    # 同一条数据同时记到「本次开机」和「几点钟」两个维度上 ——
                    # 统计窗口的三个视图（每天 / 每次开机 / 按时段）都从这儿来。
                    self._bump_session(reading.ts, energy_wh, peak_wh,
                                       valley_wh, dt)
                    self._bump_hour(tm.tm_hour, energy_wh, valley_wh)

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

    def _day_cost(self, day: str, item: dict) -> float:
        """一天的电费 —— 由那天的电量按当前电价现算（单价按那天的月份取丰/枯）。"""
        return blend_cost(self._cfg, day_tm(day),
                          item["wh"], item["peak"], item["valley"])

    def _residual(self) -> tuple[float, float, float]:
        """累计账本里**没有明细**的那部分电量（峰, 谷, 总）。

        ``total_*`` 是全量累加器；每日账本有 400 天上限，更老的会被裁掉，
        差额就是「只在累计里、不在每日里」的电量。累计电费必须把它算进去，
        否则裁掉旧日期的那一天，累计电费会凭空掉一截。

        返回永远非负：手改坏的账本不能让这里变成负电量。
        """
        day_wh = sum(i["wh"] for i in self._days.values())
        day_peak = sum(i["peak"] for i in self._days.values())
        day_valley = sum(i["valley"] for i in self._days.values())
        return (
            max(0.0, self._total_peak_wh - day_peak),
            max(0.0, self._total_valley_wh - day_valley),
            max(0.0, self._total_wh - day_wh),
        )

    def _total_cost(self) -> float:
        """累计电费 = 每日账本的电费之和 + 无明细那部分的电费。

        **不要**把它写成独立累加器 —— 那正是「累计电费比本次电费还低」的成因：
        独立累加器可以只覆盖了历史的一部分（旧版本只存当天一个桶），
        回填时又不知道缺的那截电价是多少，于是凭空少算。
        """
        cost = sum(self._day_cost(day, item) for day, item in self._days.items())
        peak, valley, wh = self._residual()
        now_tm = time.localtime()
        cost += blend_cost(self._cfg, now_tm, wh, peak, valley)
        # 上限保护：任何情况下累计都不该低于本次 / 今日（电量单调、单价同一套）
        return max(cost, self._session_cost(), self._today_cost())

    def _today_item(self) -> dict:
        return self._days.get(self._today_key()) or {
            "wh": 0.0, "peak": 0.0, "valley": 0.0, "seconds": 0.0,
        }

    def _today_cost(self) -> float:
        day = self._today_key()
        return self._day_cost(day, self._today_item())

    def _session_cost(self) -> float:
        return blend_cost(self._cfg, time.localtime(),
                          self._session_wh, self._peak_wh, self._valley_wh)

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
                    month_cost += self._day_cost(day, item)
                    month_days += 1
            recent = [
                (day[5:], item["wh"], self._day_cost(day, item))
                for day, item in sorted(self._days.items())[-7:]
            ]
            today_item = self._today_item()
            return Snapshot(
                session_wh=self._session_wh,
                session_cost=self._session_cost(),
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
                today_wh=today_item["wh"],
                today_cost=self._today_cost(),
                month_wh=month_wh,
                month_cost=month_cost,
                month_days=month_days,
                total_wh=self._total_wh,
                total_cost=self._total_cost(),
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

    # ------------------------------------------------------------- 统计明细

    def stats_totals(self) -> dict:
        """统计窗口顶部那行汇总：今日 / 本月 / 今年 / 累计。

        本月与今年都从每日账本现算（而不是各存一份），只有一个真源 —— 存三份
        迟早会互相对不上。
        """
        with self._lock:
            today = time.strftime("%Y-%m-%d")
            month, year = today[:7], today[:4]

            def fold(prefix: str) -> tuple[float, float, int]:
                wh = cost = 0.0
                days = 0
                for day, item in self._days.items():
                    if day.startswith(prefix):
                        wh += item["wh"]
                        cost += self._day_cost(day, item)
                        days += 1
                return wh, cost, days

            month_wh, month_cost, month_days = fold(month)
            year_wh, year_cost, year_days = fold(year)
            return {
                "today": (self._today_item()["wh"], self._today_cost()),
                "month": (month_wh, month_cost),
                "year": (year_wh, year_cost),
                "total": (self._total_wh, self._total_cost()),
                "month_days": month_days,
                "year_days": year_days,
                "days": len(self._days),
                "sessions": len(self._sessions),
            }

    def stats_rows(self, kind: str, limit: int = 500) -> list[dict]:
        """按某个维度取明细行，**新的在前**。

        kind 取值：``day`` / ``month`` / ``year`` / ``session`` / ``hour``。
        每行统一是 ``{"when", "wh", "cost", "seconds", "note", "key"}`` —— 统计
        窗口只认这一种形状，加维度时不用改窗口代码。

        ``key`` 是该行的**稳定标识**：按天是 ``2026-09-16``，按月是 ``2026-09``，
        按每次开机是开机时刻的字符串。用户要「自己挑某一天/某一次开机去看」，
        窗口靠它定位到具体那一条。
        """
        with self._lock:
            if kind == "session":
                rows = []
                for boot, item in sorted(self._sessions.items(), reverse=True):
                    tm = time.localtime(boot)
                    span = max(0.0, item["last"] - boot)
                    rows.append({
                        "when": time.strftime("%m-%d %H:%M", tm),
                        "wh": item["wh"],
                        "cost": blend_cost(self._cfg, tm, item["wh"],
                                           item["peak"], item["valley"]),
                        "seconds": item["seconds"],
                        # 「这次开机一共开了多久」和「统计到多久」是两回事：
                        # 程序没跑的那段测不到，但用户想知道整次开机有多长。
                        "note": "开机 " + _duration(span),
                        "day": time.strftime("%Y-%m-%d", tm),
                        "key": str(boot),
                        "boot": boot,
                        "peak_wh": item["peak"],
                        "valley_wh": item["valley"],
                        "last": item["last"],
                    })
                return rows[:limit]

            if kind == "hour":
                total = sum(self._hours) or 0.0
                now_tm = time.localtime()
                v_rate = valley_rate(self._cfg, now_tm)
                rows = []
                for h in range(HOUR_BUCKETS):
                    wh = self._hours[h]
                    v_wh = self._hours_valley[h]
                    share = (wh / total * 100.0) if total > 0 else 0.0
                    rows.append({
                        "when": f"{h:02d}:00 – {h + 1:02d}:00",
                        "wh": wh,
                        # 时段桶只拆了峰谷（没有日期），谷价按**当前月份**取 ——
                        # 跨了丰枯的话这一列会有一点偏差，列头已注明是估算。
                        "cost": blend_cost(self._cfg, now_tm, wh, 0.0, v_wh),
                        "seconds": 0.0,
                        "note": f"占 {share:.1f}%" if wh > 0 else "—",
                        "key": f"{h:02d}",
                        "peak_wh": 0.0,
                        "valley_wh": v_wh,
                    })
                return rows[:limit]

            if kind == "day":
                launched = self._sessions_per_day()
                rows = []
                for day, item in sorted(self._days.items(), reverse=True):
                    tm = time.strptime(day, "%Y-%m-%d")
                    rows.append({
                        "when": f"{day} {WEEKDAYS[tm.tm_wday]}",
                        "wh": item["wh"],
                        "cost": self._day_cost(day, item),
                        "seconds": item["seconds"],
                        "note": f"开机 {launched.get(day, 0)} 次"
                                if launched.get(day) else "—",
                        "day": day,
                        "key": day,
                        "peak_wh": item["peak"],
                        "valley_wh": item["valley"],
                    })
                return rows[:limit]

            if kind in ("month", "year"):
                width = 7 if kind == "month" else 4
                groups: dict[str, dict[str, float]] = {}
                for day, item in self._days.items():
                    key = day[:width]
                    acc = groups.setdefault(key, {"wh": 0.0, "cost": 0.0,
                                                  "seconds": 0.0, "days": 0.0,
                                                  "peak": 0.0, "valley": 0.0})
                    acc["wh"] += item["wh"]
                    # 电费按**天**累加而不是把整月的电量按一个单价算 ——
                    # 跨丰枯水期时只有逐天算才对得上。
                    acc["cost"] += self._day_cost(day, item)
                    acc["seconds"] += item["seconds"]
                    acc["peak"] += item["peak"]
                    acc["valley"] += item["valley"]
                    acc["days"] += 1
                rows = []
                for key, acc in sorted(groups.items(), reverse=True):
                    days = int(acc["days"])
                    avg = acc["wh"] / days if days else 0.0
                    rows.append({
                        "when": key,
                        "wh": acc["wh"],
                        "cost": acc["cost"],
                        "seconds": acc["seconds"],
                        "note": f"{days} 天 · 日均 {avg / 1000:.2f} kWh",
                        "day": key,
                        "key": key,
                        "peak_wh": acc["peak"],
                        "valley_wh": acc["valley"],
                    })
                return rows[:limit]

            return []

    def _sessions_per_day(self) -> dict[str, int]:
        """每天开机几次（按开机时刻所在日期归组）。调用方必须已持有锁。"""
        out: dict[str, int] = {}
        for boot in self._sessions:
            day = time.strftime("%Y-%m-%d", time.localtime(boot))
            out[day] = out.get(day, 0) + 1
        return out

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
    "blend_cost",
    "day_tm",
    "is_valley",
    "rate_at",
    "segment_of",
    "split_by_ratio",
    "valley_price",
    "valley_rate",
]
