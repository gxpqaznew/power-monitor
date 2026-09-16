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
# 「极小」这一档不是让人手工点的常规档位，而是**折行时的最后一根救命稻草**：
# 字段多到两行都塞不下时，只有把字再缩小 15% 才排得进去（文本宽度按比例缩，
# 分隔和间隙不缩）。0.85 档下 15 个字段的纯文本就有 1495px，而本机两行合起来
# 只有 1690px —— 除尽分隔线后正好贴边，实际排不下。所以必须留这一档。
FONT_SCALES = [
    (0.72, "极小"),
    (0.85, "小"),
    (1.00, "标准"),
    (1.15, "大"),
    (1.32, "特大"),
    (1.50, "巨大"),
]
FONT_SCALE_VALUES = tuple(v for v, _ in FONT_SCALES)
FONT_SCALE_LABEL = {v: t for v, t in FONT_SCALES}

# 连续字号的合法区间：菜单上只有六档，但**拖长条上下边缘可以无级缩放**
# （见 strip.py 的 resize），所以任何落在区间里的值都是合法的，
# 不要再把连续值夹回最近的档位 —— 否则用户刚拖好的大小下一秒就被「校正」回去。
FONT_SCALE_MIN = 0.60
FONT_SCALE_MAX = 1.60

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
    # 累计类放在最后：默认不勾，勾上就按 FIELDS 顺序排在长条最右边。
    ("month", "本月电量"),
    ("total", "累计电量"),
    ("total_cost", "累计电费"),
]
FIELD_KEYS = tuple(k for k, _ in FIELDS)
FIELD_LABEL = dict(FIELDS)

# 紧凑标签：字段多到排不下时用（长条的可用宽度是任务栏上「开始按钮左边」那一段，
# 本机只有 883px）。标签在每段里占的比例不小（「本次电费」四个字 ≈ 52px，
# 一段总共才 126px），换成两个字就能多塞进四成字段。
# 「本次电费」和「本次电量」都缩成「本次」会撞名 —— 无所谓，单位不同
# （¥ / Wh / kWh），一格里有单位在，不会看错。
SHORT_LABEL = {
    "current": "当前",
    "cpu": "CPU",
    "gpu": "显卡",
    "base": "其他",
    "cost": "本次",
    "session": "本次",
    "today": "今日",
    "today_cost": "今日",
    "avg": "平均",
    "peak": "峰值",
    "uptime": "开机",
    "segment": "时段",
    "month": "本月",
    "total": "累计",
    "total_cost": "累计",
}

# ------------------------------------------------------------------ 拖动
# 长条横向可以拖（两端有把手，见 strip.py）。拖动量存成「设计基准像素」——
# 48px 任务栏下的像素，跟 DPI 缩放走，换屏 / 改缩放后位置依然合理。
# 这是允许的绝对值上限：再大也只是「拖到屏幕最左/最右」，`strip._target_rect`
# 里还会按实际可用区域再夹一次。放这么宽是因为它只管「配置别被手改写坏」。
OFFSET_LIMIT = 4000.0

# ------------------------------------------------------------------ 排列
# 「一排还是两排」——用户要的是**他自己决定**，而不是程序觉得怎么合适就怎么排。
#
#   auto  字段排不下就往上折行（最多 3 排），是默认的「别让我操心」
#   1/2/3 固定排数：宁可少显示几项也不折行，横向长度完全可控
#
# 固定排数时如果任务栏太矮、连一排都放不下（高度不够），会自动退到放得下的
# 那个排数，而不是把长条整个藏掉（详见 strip._plan 的 rows_allowed）。
ROWS = [
    ("auto", "自动"),
    ("1", "一排"),
    ("2", "两排"),
    ("3", "三排"),
]
ROW_KEYS = tuple(k for k, _ in ROWS)
ROW_LABEL = {k: t for k, t in ROWS}

# ------------------------------------------------------------------ 配色方案
# 一整套协调好的配色（不是让用户从 1677 万色里瞎挑）。每款给四个色：
#
#   bg     底色（同时也是毛玻璃的色调层 → 色相稳定、不随壁纸飘）
#   ink    标签色（次要信息，要压得住但不能糊）
#   value  数值色（主角，最亮/最饱和的那一个）
#   accent 悬停描边 / 强调线（拖长条时会亮起来的那圈）
#   light  浅色系（描边、悬停提亮的方向要反过来）
#
# 底色一律取低饱和深色或干净的高明度浅色 —— 长条是贴着任务栏的一条细带，
# 花哨的底色会把数字吃掉。对比度按 WCAG AA（正文 4.5:1）挑过。
PALETTES = [
    ("theme", "跟随质感", None, None, None, None, False),
    ("graphite", "石墨", (32, 35, 42), (156, 165, 181), (255, 255, 255),
     (96, 165, 250), False),
    ("midnight", "午夜", (13, 20, 36), (138, 160, 190), (125, 211, 252),
     (56, 189, 248), False),
    ("forest", "松林", (16, 36, 30), (150, 190, 172), (110, 231, 183),
     (52, 211, 153), False),
    ("sunset", "落日", (40, 24, 20), (200, 172, 152), (253, 186, 116),
     (251, 146, 60), False),
    ("wine", "酒红", (42, 18, 28), (205, 168, 182), (251, 113, 133),
     (244, 63, 94), False),
    ("ink", "墨玉", (17, 17, 19), (150, 152, 162), (245, 245, 250),
     (212, 212, 216), False),
    ("paper", "宣纸", (247, 245, 240), (118, 108, 94), (28, 25, 21),
     (180, 83, 9), True),
    ("rose", "樱花", (253, 242, 245), (122, 86, 102), (190, 24, 93),
     (236, 72, 153), True),
    ("mint", "薄荷", (240, 253, 248), (64, 110, 96), (15, 118, 110),
     (20, 184, 166), True),
    ("sky", "晴空", (240, 247, 255), (74, 100, 132), (29, 78, 216),
     (59, 130, 246), True),
]
PALETTE_KEYS = tuple(p[0] for p in PALETTES)
PALETTE_LABEL = {p[0]: p[1] for p in PALETTES}
_PALETTE_SPEC = {
    p[0]: {"bg": p[2], "ink": p[3], "value": p[4], "accent": p[5], "light": p[6]}
    for p in PALETTES
}

# ------------------------------------------------------------------ 细节开关
# 「显示方式」里最容易出效果的两个：标签要不要、分隔线要不要。
# 关掉标签只剩数值，一条能塞下的字段数立刻多出四成 —— 喜欢「只看数」的人会关。

# ------------------------------------------------------------------ 背景图
BG_FITS = [
    ("cover", "填满", "等比放大铺满，超出部分裁掉"),
    ("contain", "适应", "等比缩到完整可见，两侧留空"),
    ("tile", "平铺", "原尺寸重复铺满（适合小图案 / 纹理）"),
]
BG_FIT_KEYS = tuple(k for k, _, _ in BG_FITS)
BG_FIT_LABEL = {k: t for k, t, _ in BG_FITS}
BG_OPACITY_MIN = 0
BG_OPACITY_MAX = 100


# ------------------------------------------------------------------ 取值


def theme(cfg) -> str:
    value = getattr(cfg, "strip_theme", "auto")
    return value if value in THEME_KEYS else "auto"


def font_scale(cfg) -> float:
    try:
        value = float(getattr(cfg, "strip_font_scale", 1.0))
    except (TypeError, ValueError):
        return 1.0
    # 档位之外的连续值也是合法的：拖长条上下边缘就是在这个区间里无级缩放。
    # 只有越界（或手改成 9.9 这种）才夹回边界。
    return max(FONT_SCALE_MIN, min(FONT_SCALE_MAX, value))


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


def locked(cfg) -> bool:
    """长条要不要「锁定位置」。

    默认**不锁**：长条自己就能拖（整块胶囊是拖动面，悬停会出强调色描边）。
    勾上之后长条加回 ``WS_EX_TRANSPARENT``，变成纯显示、完全穿透点击 ——
    给「长条压着的地方我要点任务栏」的人用。
    """
    value = getattr(cfg, "strip_locked", False)
    return bool(value) if isinstance(value, bool) else False


def offset_x(cfg) -> float:
    """用户拖出来的横向偏移（设计基准像素）。"""
    try:
        value = float(getattr(cfg, "strip_offset_x", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return max(-OFFSET_LIMIT, min(OFFSET_LIMIT, value))


def rows_mode(cfg) -> str:
    value = getattr(cfg, "strip_rows", "auto")
    return value if value in ROW_KEYS else "auto"


def fixed_rows(cfg) -> int | None:
    """固定排数；``auto`` 返回 None（交给渲染自己折行）。"""
    key = rows_mode(cfg)
    return None if key == "auto" else int(key)


def palette_key(cfg) -> str:
    value = getattr(cfg, "strip_palette", "theme")
    return value if value in PALETTE_KEYS else "theme"


def palette(cfg) -> dict | None:
    """选中的配色方案；``theme``（跟随质感档）返回 None。

    🔴 ``theme`` 这一档的四个色在目录里就是 None（表示「不覆盖，交给质感档」），
    所以**不能**直接 ``_PALETTE_SPEC.get(key)`` —— 那会返回一个「字段全是 None」
    的字典，绘制那边会拿去 ``_colorref(*None)`` 当场炸掉。
    """
    key = palette_key(cfg)
    if key == "theme":
        return None
    return _PALETTE_SPEC.get(key)


def hex_to_rgb(text) -> tuple[int, int, int] | None:
    if not isinstance(text, str):
        return None
    body = text.strip().lstrip("#")
    if len(body) == 3:
        body = "".join(ch * 2 for ch in body)
    if len(body) != 6:
        return None
    try:
        value = int(body, 16)
    except ValueError:
        return None
    return ((value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF)


def rgb_to_hex(rgb) -> str:
    return "#%02X%02X%02X" % tuple(
        int(max(0, min(255, c))) for c in rgb
    )


def color_override(cfg, name: str) -> tuple[int, int, int] | None:
    """自定义颜色（name ∈ label / value / bg）；没设或写坏了都返回 None。"""
    return hex_to_rgb(getattr(cfg, f"strip_{name}_color", ""))


def color_spec(cfg) -> tuple:
    """三个自定义色的规范化形式 —— 直接进 ``_style_key``，改完立刻重画。"""
    return tuple(color_override(cfg, n) for n in ("label", "value", "bg"))


def bg_image(cfg) -> str:
    value = getattr(cfg, "strip_bg_image", "")
    if not isinstance(value, str):
        return ""
    return value.strip()


def bg_fit(cfg) -> str:
    value = getattr(cfg, "strip_bg_fit", "cover")
    return value if value in BG_FIT_KEYS else "cover"


def bg_opacity(cfg) -> int:
    try:
        value = int(getattr(cfg, "strip_bg_opacity", 100))
    except (TypeError, ValueError):
        return 100
    return max(BG_OPACITY_MIN, min(BG_OPACITY_MAX, value))


def show_label(cfg) -> bool:
    value = getattr(cfg, "strip_show_label", True)
    return value if isinstance(value, bool) else True


def show_divider(cfg) -> bool:
    value = getattr(cfg, "strip_show_divider", True)
    return value if isinstance(value, bool) else True


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
    if key == "month":
        return ("本月", f"{snap.month_wh / 1000:.1f}", "kWh")
    if key == "total":
        value, unit = _energy(snap.total_wh)
        return ("累计", value, unit)
    if key == "total_cost":
        return ("累计电费", f"{cur}{snap.total_cost:.2f}", "")
    return None


def sections(snap, cfg, compact: bool = False) -> list[tuple[str, str, str, str]]:
    """(key, 标签, 数值, 单位) 列表，按用户选的顺序。

    ``compact=True`` 时标签换成两字短名（见 ``SHORT_LABEL``）—— 字段多到一行
    排不下、折行又不够高时，靠这个把字段全塞进去，而不是从后往前丢。
    """
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
        if compact:
            label = SHORT_LABEL.get(key, label)
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

    # 锁定开关 / 拖动量：手改 config.json 写成 "1e9" 之类会让长条飞到天边，
    # 统一在这儿夹回合法值（真正的边界在 strip._target_rect 里按屏幕再夹一次）。
    raw_locked = getattr(cfg, "strip_locked", False)
    if not isinstance(raw_locked, bool):
        cfg.strip_locked = bool(raw_locked)
        changed = True

    raw_offset = getattr(cfg, "strip_offset_x", 0.0)
    fixed_offset = offset_x(cfg)
    if raw_offset != fixed_offset:
        cfg.strip_offset_x = fixed_offset
        changed = True

    # ---- 排列 / 配色 / 背景图 / 细节（v1.0.15 加的外观扩展）----
    # 这些都是「手改 config.json 会出事」的类型：排数写成 "7" 会让长条怎么排都
    # 不对，颜色写成 "red" 会让绘制那边拿到 None，背景图路径写成数字会让
    # os.stat 抛异常。统一在这儿夹回合法值。
    if getattr(cfg, "strip_rows", None) not in ROW_KEYS:
        cfg.strip_rows = rows_mode(cfg)
        changed = True
    if getattr(cfg, "strip_palette", None) not in PALETTE_KEYS:
        cfg.strip_palette = palette_key(cfg)
        changed = True

    for name in ("label", "value", "bg"):
        attr = f"strip_{name}_color"
        raw_color = getattr(cfg, attr, "")
        rgb = hex_to_rgb(raw_color)
        fixed_color = rgb_to_hex(rgb) if rgb else ""
        if raw_color != fixed_color:
            setattr(cfg, attr, fixed_color)
            changed = True

    raw_image = getattr(cfg, "strip_bg_image", "")
    if not isinstance(raw_image, str):
        cfg.strip_bg_image = ""
        changed = True
    if getattr(cfg, "strip_bg_fit", None) not in BG_FIT_KEYS:
        cfg.strip_bg_fit = bg_fit(cfg)
        changed = True
    raw_opacity = getattr(cfg, "strip_bg_opacity", 100)
    fixed_opacity = bg_opacity(cfg)
    if raw_opacity != fixed_opacity:
        cfg.strip_bg_opacity = fixed_opacity
        changed = True

    for attr in ("strip_show_label", "strip_show_divider"):
        raw_flag = getattr(cfg, attr, True)
        if not isinstance(raw_flag, bool):
            # 🔴 不能写成 bool(raw_flag)：``show_label`` / ``show_divider`` 两个
            # 访问器对「不是 bool」一律当 **True**（只有显式 False 才算关），而
            # ``bool(None)`` / ``bool(0)`` 都是 False —— 两处口径不一致时，手改过
            # config.json 的用户会觉得「开着的开关每次启动都被悄悄关掉」。
            setattr(cfg, attr, True)
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
