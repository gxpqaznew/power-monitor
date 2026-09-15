"""配置与路径。

配置写在程序目录的 ``config.json``，首次运行自动生成默认值。
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


def app_dir() -> Path:
    """程序目录。打包成单文件 exe 后返回 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH = app_dir() / "config.json"
STATE_PATH = app_dir() / "state.json"


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
        try:
            CONFIG_PATH.write_text(
                json.dumps(asdict(self), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError:
            pass
