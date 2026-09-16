"""配置与路径。

配置与账本写在**固定的用户数据目录** ``%LOCALAPPDATA%\\PowerMonitor\\``，
首次运行自动生成默认值；从旧版（配置放在 exe 同目录）升级时会自动接手。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


def app_dir() -> Path:
    """程序目录。打包成单文件 exe 后返回 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def user_data_dir() -> Path:
    """用户数据目录 —— 位置固定，**与 exe 所在目录无关**。

    为什么不能放 exe 同目录（旧版就是这么干的）：账本 ``state.json`` 一旦跟着
    exe 走，用户从「绿色版目录」换成「安装版」、或者重装到别的路径之后，新目录
    里没有 state.json，在他眼里就是「更新一次，历史全没了」。配置同理。
    所以账本和配置都落到一个固定的地方。
    """
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    directory = Path(base) / "PowerMonitor"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return app_dir()          # 实在建不出来就退回老行为，不能让程序起不来
    return directory


DATA_DIR = user_data_dir()
CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"
# 账本的「上一代」副本，每次启动时刷新一次（见 snapshot_state）。
STATE_BACKUP_PATH = DATA_DIR / "state.json.prev"


def _candidate_dirs() -> list[Path]:
    """旧版可能把文件放在哪儿（按可能性排序，去重、且排除当前数据目录）。"""
    out: list[Path] = []
    for directory in (
        app_dir(),
        app_dir() / "dist",
        app_dir().parent / "dist",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "PowerMonitor",
        Path.cwd(),
    ):
        try:
            resolved = directory.resolve()
        except OSError:
            continue
        if resolved == DATA_DIR.resolve():
            continue
        if resolved not in out and resolved.is_dir():
            out.append(resolved)
    return out


def _state_score(path: Path) -> tuple[float, float]:
    """账本的「分量」：(累计电量, 最后保存时间)。取最大的那一份接手。

    ⚠️ 顺序不能反。**累计电量 `total_wh` 是单调递增的**（程序里没有任何入口
    会把它清零），所以几份账本里累计值最大的那份，就是见过最多历史的那份。

    踩过的坑：一开始按「时间最新」挑，结果挑中了**正在运行的那个实例刚写的
    小文件** —— 它启动才十几分钟、累计只有 220 Wh，比被冷落的那份 3605 Wh
    少了 3.4 度电。时间戳新 ≠ 账本全：刚重启的程序永远写出一个「很新但很空」
    的文件，正好会把最全的那份比下去。
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return (0.0, 0.0)

    def number(key: str) -> float:
        try:
            return float(raw.get(key, 0.0))
        except (TypeError, ValueError):
            return 0.0

    saved = number("saved_at")
    if not saved:
        try:
            saved = path.stat().st_mtime
        except OSError:
            saved = 0.0
    return (number("total_wh"), saved)


def _backup(path: Path, reason: str) -> None:
    """把没被选中的那份账本另存一份，绝不原地丢掉。

    接手账本是个「可能有损」的动作（几份文件里只能选一份），万一挑错了，
    原文件还在就能救回来。备份只做一次，不覆盖已有的。
    """
    target = DATA_DIR / f"state.json.bak-{reason}"
    if target.exists():
        return
    _copy(path, target)


def migrate_user_data() -> list[str]:
    """把旧位置（exe 同目录 / dist / 安装目录）的配置与账本接手到数据目录。

    只在新位置**还没有**该文件时才搬，绝不覆盖 —— 升级不能把用户刚记的账覆盖掉。
    返回搬了哪些，方便日志与自检核对。
    """
    moved: list[str] = []

    target = DATA_DIR / "config.json"
    if not target.exists():
        best: Path | None = None
        for directory in _candidate_dirs():
            candidate = directory / "config.json"
            if candidate.is_file():
                if best is None or candidate.stat().st_mtime > best.stat().st_mtime:
                    best = candidate
        if best is not None and _copy(best, target):
            moved.append(f"{best.parent.name}/config.json")

    target = DATA_DIR / "state.json"
    if not target.exists():
        # 数据目录自己的「上一代」副本优先于任何旧位置的文件 —— 它是同一血脉
        # 里最近的一份，比 exe 同目录 / 安装目录里那些更忠实。走这条路恢复时
        # 用户看到的是「记录还在」，而不是「又从头开始了」。
        if STATE_BACKUP_PATH.is_file() and _state_score(STATE_BACKUP_PATH) > (0.0, 0.0):
            if _copy(STATE_BACKUP_PATH, target):
                moved.append("state.json.prev")
                return moved

        found: list[Path] = []
        for directory in _candidate_dirs():
            candidate = directory / "state.json"
            if candidate.is_file() and _state_score(candidate) > (0.0, 0.0):
                found.append(candidate)
        if found:
            best = max(found, key=_state_score)
            for other in found:
                if other != best:
                    _backup(other, other.parent.name)   # 没被选中的留个底
            if _copy(best, target):
                moved.append(f"{best.parent.name}/state.json")

    return moved


def _copy(src: Path, dst: Path) -> bool:
    try:
        shutil.copyfile(src, dst)
        return True
    except OSError:
        return False


def snapshot_state() -> bool:
    """把当前账本另存一份 ``state.json.prev``（覆盖式，只留上一代）。

    每次启动读账本之前刷一次 —— 这样任何「把 state.json 清掉 / 写坏」的意外
    （安装程序多手、磁盘故障、用户手改坏文件）都还有上一代完整的账本可救。
    账本只有几 KB，这点开销可以忽略；换来的是一条最后防线。

    注意是**覆盖式**：只保留上一代，不做无限堆叠，免得数据目录长出一堆快照。
    """
    if not STATE_PATH.exists():
        return False
    return _copy(STATE_PATH, STATE_BACKUP_PATH)


def atomic_write_text(path: Path, text: str) -> bool:
    """先写临时文件、再 ``os.replace`` 换上去。

    账本每隔十几秒就落一次盘，要是正好在写一半的时候断电 / 被任务管理器强杀，
    留下的是个截断的 JSON —— 下次启动整本账都读不出来，比丢几十秒严重得多。
    ``os.replace`` 在同一分区上是原子的：读到的要么是旧的整份，要么是新的整份。
    """
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False



@dataclass
class Config:
    """可调参数。默认值按 Ryzen 7 5800X + RTX 3080 台式机标定。"""

    # ---- 硬件功耗模型 ----
    # 5800X 的 PPT（Peak Package Power）为 142W，是封装功耗的实际上限
    cpu_ppt: float = 142.0
    # 桌面待机时的封装功耗（含 IO die），5800X 实测约 18~24W
    cpu_idle: float = 20.0
    # 负载指数：负载 → 功耗不是线性的，低负载时电压频率同步下降
    cpu_load_exponent: float = 0.85
    # 主板 + 内存 + 2×NVMe + 风扇/水泵 等固定开销
    baseline_watts: float = 35.0

    # ---- 计价：默认按四川省「一户一表」居民第一档 ----
    # 各地电价差别很大，同一个省内还分档位 —— 下面这些值都可以在托盘菜单
    # 「电价设置…」里改，也可以直接选内置的省份预设自动填。
    price_peak: float = 0.5224        # 峰段电价
    price_flat: float = 0.5224        # 平段电价（该省无独立峰段时与峰段相同）
    price_valley_dry: float = 0.2535  # 谷段电价（枯、平水期 / 常规）
    price_valley_wet: float = 0.175   # 谷段电价（丰水期；无季节差时与之相同）
    valley_wet_months: list = field(default_factory=lambda: [6, 7, 8, 9, 10])

    # 时段写法见 tariffs.parse_hours："23-7" = 23:00 到次日 06:59
    peak_hours: str = ""              # 空 = 没有独立峰段（非谷段全按平段计）
    valley_hours: str = "23-7"        # 空 = 没有低谷优惠

    # 预设来源，界面展示 + 自查用
    tariff_region: str = "四川"
    tariff_plan: str = "一户一表 第一档"
    tariff_source: str = "四川省电网居民生活电价表（川发改价格〔2012〕560号、〔2026〕255号）"
    tariff_effective: str = "2026-07-01"
    tariff_verify: bool = False       # True = 只有网络汇总口径，建议核对
    tariff_note: str = "7-9月第一档电量为260度及以下，其他月份180度及以下"

    currency: str = "\u00a5"

    # ---- 采样与显示 ----
    sample_interval: float = 2.0
    # 托盘图标显示内容：
    #   current  当前功率（W）
    #   cost     已用电费（元）—— 本次统计累计，按量级自动调小数位
    #   session  本次累计能耗（Wh / kWh）
    #   average  平均功率（W）
    # 不管选哪种，图标底色始终表示当前负载轻重（绿 / 琥珀 / 红）。
    tray_display: str = "current"

    # ---- 任务栏长条 ----
    # 托盘图标只有 16~20px（系统固定，塞不进长条 HICON，会被压成方的），三个
    # 数字挤在那点地方看不清。所以另开一个**置顶、可穿透点击**的长条窗口贴在
    # 任务栏上，用接近两倍的字号显示同样的读数。
    strip_enabled: bool = True
    # 长条贴哪边：
    #   start  开始按钮左边（默认）。实测开始按钮是独立 HWND，左边一大片空地。
    #   tray   通知区域左边。开始按钮查不到、或左边放不下时也自动退到这儿。
    strip_position: str = "start"

    # ---- 任务栏长条 · 外观与内容 ----
    # 下面这几项都能在托盘右键菜单的二级子菜单里改：
    # 「长条质感 ▸」「长条字号 ▸」「长条大小 ▸」「长条显示内容 ▸」
    # 每一项的取值清单见 powermon/stripopts.py —— 那是菜单、配置校验、渲染
    # 三处共用的唯一真源，加档位只改那一个文件。
    # 质感：auto 跟随任务栏 / glass 玻璃 / outline 线框 /
    #       dark 深色卡片 / light 浅色卡片 / accent 强调色
    strip_theme: str = "auto"
    # 字号倍数：0.85 小 / 1.0 标准 / 1.15 大 / 1.32 特大 / 1.5 巨大
    strip_font_scale: float = 1.0
    # 尺寸：slim 纤细 / normal 标准 / large 宽大（影响胶囊高度与左右内边距）
    strip_size: str = "normal"
    # 显示哪些字段、按什么顺序（从左到右）。字段清单见 stripopts.FIELDS
    strip_fields: list = field(
        default_factory=lambda: ["current", "cost", "session", "today"]
    )
    # 长条能不能拖：**默认不锁**，整块胶囊就是拖动面 —— 按住横着拖、双击回到
    # 默认位置，鼠标指上去描边会转成强调色（没有常驻的把手 / 小圆点之类的装饰）。
    # 勾上「锁定位置」后长条变回穿透点击的纯显示窗口，鼠标完全碰不到它。
    strip_locked: bool = False
    # 用户拖出来的横向偏移，单位是「设计基准像素」（48px 任务栏下的像素 ×
    # 缩放系数还原），所以换 DPI / 改任务栏高度之后位置依然合理。
    strip_offset_x: float = 0.0

    # ---- 整机口径 ----
    # 是否把显示器功耗计入（显示器一般独立计量，默认不计）
    include_monitor: bool = False
    monitor_watts: float = 30.0
    # 整机校准系数：把「软件估算（机箱内直流口径）」换算到「插座读数（交流口径）」。
    # CPU 模型偏差与电源转换损耗都是系统性偏小，一个系数一起吸收最省事。
    # 用智能插座/功率计实测后，按 实测值 ÷ 软件值 填。1.0 = 不校准。
    calibration: float = 1.0

    @classmethod
    def load(cls) -> "Config":
        # 先看看旧位置有没有配置/账本要接手（只搬一次，之后就直接读数据目录）
        migrate_user_data()
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
            known = {f.name for f in fields(cls)}
            cfg = cls(**{k: v for k, v in raw.items() if k in known})
            cfg._backfill(raw)
            # 长条的外观/内容项可能是旧配置里没有的，也可能被手改成了不认识的值
            # （比如 strip_font_scale 写成 3.0 会把长条撑爆）。统一在这儿夹回合法值。
            from . import stripopts

            if stripopts.sanitize(cfg):
                cfg.save()
            return cfg
        cfg = cls()
        cfg.save()
        return cfg

    def _backfill(self, raw: dict | None = None) -> None:
        """兼容旧版配置。

        旧版只有 price_peak / price_valley_wet / price_valley_dry，没有
        price_flat、时段字符串，也没有地区标签。老配置里用户可能已经自己
        改过价格，所以只补缺失项、标成「自定义」，不覆盖他的数字 —— 让他
        自己去界面里选预设。
        """
        raw = raw or {}
        if not self.price_flat:
            self.price_flat = self.price_peak
        if not self.valley_hours and not self.peak_hours:
            # 旧版就是 23 点起算的固定谷段
            self.valley_hours = "23-7"
        if "tariff_region" not in raw:
            self.tariff_region = "自定义"
            self.tariff_plan = "手动填写"
            self.tariff_source = "用户手动设置（旧版配置迁移）"
            self.tariff_note = ""

    def save(self) -> None:
        atomic_write_text(
            CONFIG_PATH,
            json.dumps(asdict(self), indent=2, ensure_ascii=False),
        )
