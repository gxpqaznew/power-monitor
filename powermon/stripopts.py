"""任务栏长条的「可调项」目录 —— 菜单、配置校验、渲染共用的唯一真源。

为什么单开一个模块：同一份选项要被三处消费

  * ``config.py``  归一化非法值（旧配置、或者用户手改过 config.json）
  * ``app.py``     拼托盘二级菜单
  * ``strip.py``   真正画的时候取字段与样式

留在各自文件里迟早会出现「菜单里有、渲染不认识」这种不一致。

所有取值都走 ``getattr(cfg, ..., 默认)``，这样测试里用 ``SimpleNamespace``
临时造一个只有两三个字段的 cfg 也能跑。
"""

from __future__ import annotations

DEFAULT_FIELDS = ["current", "cost", "session", "today"]

# ------------------------------------------------------------------ 质感
# (key, 菜单文字, 一句话说明)。说明只用于 README / 悬停提示，菜单里不显示。
THEMES = [
    ("auto", "跟随任务栏", "采样任务栏底色，自动判断深浅"),
    ("glass", "玻璃", "半透明胶囊 + 顶部高光"),
    ("outline", "线框", "底色透明，只留描边和文字"),
    ("dark", "深色卡片", "固定深色底 + 浅色字"),
    ("light", "浅色卡片", "固定浅色底 + 深色字"),
    ("accent", "强调色", "程序主色蓝底 + 白字"),
]
THEME_KEYS = tuple(k for k, _, _ in THEMES)
THEME_LABEL = {k: t for k, t, _ in THEMES}
THEME_HINT = {k: h for k, _, h in THEMES}

# ------------------------------------------------------------------ 字号
FONT_SCALES = [
    (0.85, "小"),
    (1.00, "标准"),
    (1.15, "大"),
    (1.32, "特大"),
    (1.50, "巨大"),
]
FONT_SCALE_VALUES = tuple(v for v, _ in FONT_SCALES)
FONT_SCALE_LABEL = {v: t for v, t in FONT_SCALES}

# ------------------------------------------------------------------ 尺寸
# (key, 菜单文字, 高度占任务栏比例, 左右内边距倍数)
SIZES = [
    ("slim", "纤细", 0.62, 0.78),
    ("normal", "标准", 0.78, 1.00),
    ("large", "宽大", 0.92, 1.24),
]
SIZE_KEYS = tuple(k for k, _, _, _ in SIZES)
SIZE_LABEL = {k: t for k, t, _, _ in SIZES}
_SIZE_SPEC = {k: (r, p) for k, _t, r, p in SIZES}

# ------------------------------------------------------------------ 字段
# 顺序 = 长条从左到右的顺序，菜单里也按这个顺序列。
FIELDS = [
    ("current", "当前功率"),
    ("cpu", "处理器功率"),
    ("gpu", "显卡功率"),
    ("base", "其他功耗"),
    ("cost", "本次电费"),
    ("session", "本次电量"),
    ("today", "今日电量"),
    ("today_cost", "今日电费"),
    ("avg", "平均功率"),
    ("peak", "峰值功率"),
    ("uptime", "开机时长"),
    ("segment", "当前时段"),
]
FIELD_KEYS = tuple(k for k, _ in FIELDS)
FIELD_LABEL = dict(FIELDS)


# ------------------------------------------------------------------ 取值


def theme(cfg) -> str:
    value = getattr(cfg, "strip_theme", "auto")
    return value if value in THEME_KEYS else "auto"


def font_scale(cfg) -> float:
    try:
        value = float(getattr(cfg, "strip_font_scale", 1.0))
    except (TypeError, ValueError):
        return 1.0
    if value in FONT_SCALE_VALUES:
        return value
    # 手改 config.json 写了个菜单里没有的值：夹到最近的档位
    return min(FONT_SCALE_VALUES, key=lambda v: abs(v - value))


def size_key(cfg) -> str:
    value = getattr(cfg, "strip_size", "normal")
    return value if value in SIZE_KEYS else "normal"


def size_spec(cfg) -> tuple[float, float]:
    """(高度占任务栏比例, 内边距倍数)"""
    return _SIZE_SPEC[size_key(cfg)]


def height_ratio(cfg) -> float:
    """胶囊高度 / 任务栏高度。

    字号变大时字要占更多地方，所以高度也跟着长一点，否则大字号会被上下切掉。
    默认档（标准尺寸 + 标准字号）必须正好等于原来的 0.78，不然老用户升级后
    长条会莫名其妙变个高度。
    """
    ratio, _pad = size_spec(cfg)
    return min(0.98, ratio * (0.74 + 0.26 * font_scale(cfg)))


def enabled_fields(cfg) -> list[str]:
    raw = getattr(cfg, "strip_fields", None)
    keys: list[str] = []
    if isinstance(raw, (list, tuple)):
        for k in raw:
            if k in FIELD_KEYS and k not in keys:
                keys.append(k)
    return keys or list(DEFAULT_FIELDS)


def _energy(wh: float) -> tuple[str, str]:
    return (f"{wh / 1000:.2f}", "kWh") if wh >= 1000 else (f"{wh:.0f}", "Wh")


def _short_duration(seconds: float) -> str:
    """紧凑时长。长条位置紧，用 7h21 / 1d3h 这种写法，控制在 6 字符内。"""
    minutes = int(max(0.0, seconds) // 60)
    days, rem = divmod(minutes, 1440)
    hours, mins = divmod(rem, 60)
    if days:
        return f"{days}d{hours}h"
    if hours:
        return f"{hours}h{mins:02d}"
    return f"{mins}m"


def field_value(key: str, snap, cfg) -> tuple[str, str, str] | None:
    """取一个字段的 (标签, 数值, 单位)。数据不可用时返回 None（跳过这一段）。"""
    cur = getattr(cfg, "currency", "\u00a5")
    if key == "current":
        return ("当前", f"{snap.current_w:.0f}", "W")
    if key == "cpu":
        return ("处理器", f"{snap.cpu_w:.0f}", "W")
    if key == "gpu":
        # 没有能读功耗的独显时不要显示一个恒为 0 的假数字
        if not getattr(snap, "gpu_names", None):
            return None
        return ("显卡", f"{snap.gpu_w:.0f}", "W")
    if key == "base":
        return ("其他", f"{snap.base_w:.0f}", "W")
    if key == "cost":
        return ("本次电费", f"{cur}{snap.session_cost:.2f}", "")
    if key == "session":
        value, unit = _energy(snap.session_wh)
        return ("本次电量", value, unit)
    if key == "today":
        return ("今日", f"{snap.today_wh / 1000:.2f}", "kWh")
    if key == "today_cost":
        return ("今日电费", f"{cur}{snap.today_cost:.2f}", "")
    if key == "avg":
        return ("平均", f"{snap.average_w:.0f}", "W")
    if key == "peak":
        return ("峰值", f"{snap.peak_w:.0f}", "W")
    if key == "uptime":
        return ("开机", _short_duration(snap.power_on_seconds), "")
    if key == "segment":
        return ("时段", str(getattr(snap, "segment", "") or ""), "")
    return None


def sections(snap, cfg) -> list[tuple[str, str, str, str]]:
    """(key, 标签, 数值, 单位) 列表，按用户选的顺序。"""
    out: list[tuple[str, str, str, str]] = []
    for key in enabled_fields(cfg):
        try:
            got = field_value(key, snap, cfg)
        except AttributeError:
            # 快照是测试里造的 SimpleNamespace，缺字段就跳过这一段
            continue
        if got is None:
            continue
        label, value, unit = got
        if value == "" and not unit:
            continue
        out.append((key, label, value, unit))
    return out


# ------------------------------------------------------------------ 校验


def sanitize(cfg) -> bool:
    """把长条相关的配置归一化，返回是否改动过（调用方据此决定要不要 save）。"""
    changed = False

    if getattr(cfg, "strip_theme", None) not in THEME_KEYS:
        cfg.strip_theme = "auto"
        changed = True
    if getattr(cfg, "strip_size", None) not in SIZE_KEYS:
        cfg.strip_size = "normal"
        changed = True

    raw_scale = getattr(cfg, "strip_font_scale", 1.0)
    fixed_scale = font_scale(cfg)
    if raw_scale != fixed_scale:
        cfg.strip_font_scale = fixed_scale
        changed = True

    raw = getattr(cfg, "strip_fields", None)
    keys = enabled_fields(cfg)
    if not isinstance(raw, (list, tuple)) or list(raw) != keys:
        cfg.strip_fields = keys
        changed = True

    return changed


def toggle_field(cfg, key: str) -> bool:
    """勾选 / 取消一个字段。最后一个字段不允许取消（否则长条上什么都没有）。

    返回是否真的改动了。
    """
    if key not in FIELD_KEYS:
        return False
    keys = enabled_fields(cfg)
    if key in keys:
        if len(keys) <= 1:
            return False
        keys.remove(key)
    else:
        # 新勾的字段按 FIELDS 的定义顺序插回去，不然会出现「刚勾的跑最后」
        # 这种和菜单顺序对不上的排布
        order = {k: i for i, k in enumerate(FIELD_KEYS)}
        keys.append(key)
        keys.sort(key=lambda k: order.get(k, 999))
    cfg.strip_fields = keys
    return True
