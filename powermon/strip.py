"""任务栏长条 —— 把关键读数摊成一条横条，贴在开始按钮左边。

托盘图标只有 16~20px（系统定的，改不了：给一个长条 HICON 进去也会被压成方的），
三个数字挤在那点地方必然看不清。所以这里另开一个**置顶、无边框、能穿透点击的
分层窗口**，直接盖在任务栏那片空白上，用接近两倍的字号显示同样的数据。

放在哪儿：
  * ``start`` 贴着开始按钮左边（默认）。实测开始按钮是独立 HWND（类名 ``"Start"``），
    位置查得到；Win11 居中任务栏时它左边是一大片空地。
  * ``tray``  贴着通知区域左边。开始按钮查不到、或者左边实在放不下时自动退回这里。

几个刻意的取舍：
  * **默认能拖、不穿透**：整块胶囊就是拖动面（见下面「拖动」一段）。想让长条彻底
    不碰鼠标，就在托盘菜单里勾上「锁定位置」——那时才加回 ``WS_EX_TRANSPARENT``，
    变成只显示、纯穿透。
  * **底色默认采样自任务栏**：直接读长条目标位置旁边那一个像素，再往白里调一点点
    当背景。任务栏是亚克力/跟随壁纸的，写死颜色一定不对。用户也可以在托盘菜单里
    把质感换成固定色调（深色 / 浅色 / 强调色卡片）、半透明玻璃、或者只留描边和
    文字的线框 —— 档位目录见 ``stripopts``。
  * 不抢焦点（``WS_EX_NOACTIVATE``）也不进 Alt+Tab（``WS_EX_TOOLWINDOW``）。

外观（质感 / 字号 / 尺寸 / 显示哪些字段）全部来自 ``cfg``，由 ``stripopts`` 统一
解释。改任何一项都会走 ``_style_key()`` → ``_content_key()`` 变化 → 强制重画，
不需要重建窗口。

两个后加的东西值得单独讲：

**拖动**。默认长条**自己就能拖**：整块胶囊就是拖动面，按住左右拖、双击复位，
位置存进 ``cfg.strip_offset_x``（设计基准像素，跟着 DPI / 任务栏大小缩放）。

  为什么不是「两端各挂一个把手」？试过，不好。把手得画小圆点才看得出能抓，
  可小圆点摆在一条已经排满数字的胶囊两端非常丑；而不画圆点又没人知道能抓。
  现在的做法是**把可拖动这件事交给长条本体**：悬停时胶囊的描边会变成一圈
  强调色的发丝线、光标变 ↔，鼠标一移开就恢复原样 —— 不留任何常驻装饰。
  代价是长条不再穿透点击，所以托盘菜单里给了一个「锁定位置（穿透点击）」的开关，
  锁上就恢复成完全看不见摸不着的纯显示窗口。

**改大小**。光标落进胶囊**上下边缘带**（各 4px）时光标变 ↕，按住竖拖就是
无级缩放字号（0.60~1.60，菜单里那六档只是快捷取值）：向上拖变大、向下拖
变小，拖动中实时重排但不落盘，松手才写 ``cfg.strip_font_scale``（连续值，
``stripopts`` 会原样接受）。想回 1.0：字号菜单底部有「复位标准字号」。

  ⚠️ 分层窗口的**命中测试是按像素 alpha 走的**：胶囊内部的 alpha 是 255，
  四个圆角外面和阴影那一圈是 0，于是「胶囊能抓、圆角外照样穿透」是天然成立的，
  不需要额外做什么。反过来，如果哪天为了好看把整块画布的 alpha 都垫高，
  长条就会变成一块会吃点击的方板。

**网格对齐**（``_grid``）。折行之后每一行各自从左往右排，同一个字段在两行里的
起始 x 完全由前面几项有多宽决定 —— 两排的分隔线、数值全都不在一条竖线上，
用户看一眼就说「没对齐、没质感」。改成表格：列数 = ceil(字段数 / 行数)，
每列宽度取该列所有格子的最大值，于是**每一列在每一行都从同一个 x 开始、
分隔线也在同一个 x**。单行时列宽就等于格子自身宽度，外观和以前逐像素一致。
"""

from __future__ import annotations

import ctypes

from . import debug, frost, stripopts, taskbar
from .roundwin import compose_shape_alpha, dib_section, present_layered
from .w32 import (
    CLEARTYPE_QUALITY,
    DEFAULT_CHARSET,
    DT_CENTER,
    DT_LEFT,
    DT_NOPREFIX,
    DT_RIGHT,
    DT_SINGLELINE,
    DT_VCENTER,
    FW_BOLD,
    FW_NORMAL,
    GWL_EXSTYLE,
    GWL_STYLE,
    HTCLIENT,
    HWND_TOPMOST,
    IDC_SIZENS,
    IDC_SIZEWE,
    NULL_BRUSH,
    PS_SOLID,
    SIZE,
    SW_SHOWNOACTIVATE,
    SWP_FRAMECHANGED,
    SWP_NOACTIVATE,
    SWP_NOMOVE,
    SWP_NOOWNERZORDER,
    SWP_NOSIZE,
    SWP_NOZORDER,
    SWP_SHOWWINDOW,
    TME_LEAVE,
    TRACKMOUSEEVENT,
    TRANSPARENT,
    WM_CAPTURECHANGED,
    WM_DESTROY,
    WM_LBUTTONDBLCLK,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_MOUSELEAVE,
    WM_MOUSEMOVE,
    WM_RBUTTONUP,
    WM_SETCURSOR,
    WNDCLASSEXW,
    WNDPROC,
    WS_CHILD,
    WS_EX_LAYERED,
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_EX_TOPMOST,
    WS_EX_TRANSPARENT,
    WS_MAXIMIZE,
    WS_POPUP,
    WS_VISIBLE,
    gdi32,
    int_resource,
    kernel32,
    user32,
    wintypes,
)

_FONT_FACE = "Microsoft YaHei UI"
_CLASS_NAME = "PowerMonitorTaskbarStrip"

# ---------------------------------------------------------------- 拖动（无把手）
# 悬停 / 按住时，胶囊的描边往这个强调色靠 —— 和详情面板、强调色质感、托盘图标
# 是同一个蓝，整机看上去是一套东西。0.72 是「一眼看得出变了、但不像故障闪烁」。
_ACCENT = (0x1E, 0x5C, 0xE0)
_HOVER_MIX = 0.72
_HOVER_BORDER_W = 2
# 悬停时底色也轻轻提一点（往白里 0.06），像卡片被指到的那种反馈
_HOVER_BG_MIX = 0.06
# 拖动量的绝对值上限见 stripopts.OFFSET_LIMIT（配置文件校验那边也要用同一个数）

# 窗口类样式：CS_DBLCLKS。没有它收不到 WM_LBUTTONDBLCLK，双击复位就不工作。
_CS_DBLCLKS = 0x0008
# 拖动 / 悬停需要鼠标消息，所以默认**不带** WS_EX_TRANSPARENT；
# 「锁定位置」时才加回去（见 set_interactive）。
_EX_BASE = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST

# ---- 拖边缘改大小（resize）----
# 胶囊上下各一条这么厚的「边缘带」：光标进去变 ↕，按住竖拖就是无级缩放字号。
# 取 4（设计基准像素）：再薄点不中，再厚会吃掉太多「横向拖」的区域。
_EDGE_PX = 4.0
# 竖拖一整个任务栏高度（设计基准 48px）≈ 字号变这么多。0.5 的手感是「拖一下
# 明显变大、但不会一碰就飞」。
_RESIZE_RATE = 0.5
# 字号变化小于这个就不重排：SDF 合成是逐像素的 Python 循环，每一帧都全量
# 重画会掉帧；0.02 的步长在视觉上是连续的（高度每 0.02 才跳一丝）。
_RESIZE_STEP = 0.02

# 设计基准：任务栏高度 48px 时的那套尺寸。真实尺寸按任务栏高度等比缩放，
# 这样用户改「任务栏大小」或换 DPI 时长条会跟着变，不会显得突兀。
_NOMINAL_TASKBAR_H = 48.0
# 胶囊高度 / 任务栏高度，以及左右内边距倍数 —— 都由「尺寸」档位决定（见 stripopts）。
# 默认档（normal + 字号 1.0）必须正好得出原来的 0.78，否则老用户升级后高度会变。
_PAD_X = 15.0
_GAP_LABEL = 6.0
_GAP_UNIT = 3.0
_DIV_MARGIN = 13.0
_RADIUS = 999.0           # 足够大就会被夹成胶囊（= 高的一半）
_LABEL_SZ = 13.0
_VALUE_SZ = 20.0
_UNIT_SZ = 12.0
# 长条宽度上限（设计基准 48px 任务栏下的像素）。
#
# ⚠️ 它**不是**「能显示几个字段」的主约束 —— 主约束是锚点左边的**实际可用宽度**
# （屏幕左边 8px → 开始按钮左边，见 _target_rect 里的 available，本机有 1000+ px）。
# 踩过的坑：这个常量原来取 560，被当成了真上限，于是用户勾了 6~8 个字段只显示出
# 4 个，左边明明一大片空地 —— 从后往前丢字段的逻辑（_layout）看起来"正常工作"，
# 所以从代码上完全看不出是这里卡住了。
#
# 现在它只是一条**雪崩线**：万一锚点算歪（比如开始按钮被挤到屏幕很右边），
# 也不至于让长条铺满整条任务栏。
_MAX_WIDTH = 2600.0
# 兜底锚点（找不到开始按钮、退化成贴通知区域左边）时仍用老上限 560：
# 那个位置往左就是中间那排任务图标，铺长了会盖住它们。
_MAX_WIDTH_TRAY = 560.0
# 少于这么多像素可用宽度就整条不显示：硬塞一两个字符进任务栏角落，比不显示更糟。
# 取 108 是为了让「只勾一个字段」的最窄情况（约 130px）仍然显示得出来。
_ABS_MIN_WIDTH = 108.0

# --------------------------------------------------------------- 折行
# 用户的原话是「勾了多少就该显示多少」。但单行装不下：本机任务栏 2560 宽、
# 图标簇居中，长条左边（开始按钮往左）只有 883px 可用，一个字段约 126px
# （scale 1.0）—— 单行物理上限就是 6~7 项。
#
# 所以一行放不下就**折行**：行高比单行矮一点，必要时字号自动降一档，
# 让所有勾选的字段都排进去。只在需要时才折 —— 字段少的时候外观和以前完全一样。
_MAX_ROWS = 3
_ROW_H = 26.0                 # 单行行高基准（48px 任务栏基准下的 px）
_MULTI_ROW_MAX_RATIO = 0.94   # 折行时胶囊最多占任务栏高度的比例
# 折行时圆角要收小一些：高度都接近任务栏了，再按 height/2 画就成了一个巨型胶囊
_MULTI_ROW_RADIUS_RATIO = 0.30
# 文本宽度缓存上限（条数）。每秒都在出新数字，超了就整批清掉。
_TEXT_WIDTH_CACHE = 4096

# --------------------------------------------------------------- 密度档
# 光靠折行还是不够塞：15 个字段连标签带数值一共要 ~1900px，两行只有 1690px。
# 于是再给三档「密度」，从松到紧依次启用，**只在需要时才启用** ——
# 字段少的时候必须走第 0 档，外观和加折行之前一模一样。
#
#   (短标签, 分隔间距倍数, 标签/单位间距倍数)
#
# 第 1 档换短标签（「本次电费」→「本次」，见 stripopts.SHORT_LABEL），
# 第 2 档再把分隔和间隙收紧：每个分隔在 scale 1.25 下要 33px，15 个字段就是
# 460px —— 这比一个字段还宽，挤的时候最该动它。
# 分隔线本身还在（只是靠得近些），不会变成一坨连在一起的字。
_DENSITY = (
    (False, 1.00, 1.00),
    (True, 1.00, 1.00),
    (True, 0.40, 0.70),
)

# --------------------------------------------------------------- 规划占位值
# 排「几行、哪档字号、哪档密度」时必须用**固定的**数值占位，不能用此刻的读数。
#
# 踩过的坑：一开始直接按真实数值算。结果 6 个字段时正好卡在 1 行 / 2 行的边界上 ——
# 空闲 9 W 时单行、一跑起来 132 W 就要折行，于是字号在 1.00 和 0.85 之间来回弹、
# 长条高度跟着在 47px 和 55px 之间跳。宽度有「数字按最宽算」的补偿撑着，
# 但**数字的位数**是补偿不了的，而位数一变总宽就能差出 100 多像素。
#
# 所以规划时把数值换成下面这套占位（只影响排版，不参与绘制）：
#   key -> (整数位下限, 小数位)
# 于是「9 W」「132 W」「999 W」在规划眼里是同一个宽度，
# 「1.00 kWh」「8.89 kWh」也是 —— 计划只由「勾了哪些字段 / 字号 / 尺寸 / 可用宽度」
# 决定，读数怎么变都不动。真正跨越数量级的格式变化（Wh 变 kWh）例外，那是真的
# 换了一种写法，重新规划是对的。
_PLAN_NUMBER = {
    "current": (3, 0), "cpu": (3, 0), "gpu": (3, 0), "base": (3, 0),
    "avg": (3, 0), "peak": (3, 0),
    "session": (3, 2), "today": (3, 2), "month": (3, 2), "total": (3, 2),
    # 电费按两位数留（"¥99.99"）：日常账单就是这个量级；留三位的话光是这三个
    # 字段白白多占 60px，够再挤下一个字段了（实测：改成两位之后，本机 863px
    # 的可用宽度能重新放下全部 15 项）。真过了 ¥100 才需要重新规划一次。
    "cost": (2, 2), "today_cost": (2, 2), "total_cost": (2, 2),
}
# 这几档的数值会在两种单位之间切换（Wh / kWh）。规划时一律按更长的那种（kWh）量，
# 这样「428 Wh → 1.02 kWh」这一步也不会改排版计划。
_PLAN_UNIT = {
    "session": "kWh", "today": "kWh", "month": "kWh", "total": "kWh",
}
# 超宽容差（设计基准像素）：排版是按占位值算的，真实读数偶尔会多出一位
# （「999 W」→「1000 W」）。只超这么一点点就**不藏**长条 —— 藏起来用户会以为
# 功能坏了，宁可让胶囊往右多盖住开始按钮十来个像素。
_PLAN_TOLERANCE = 14.0


def _plan_cell(key: str, value: str, unit: str) -> tuple[str, str]:
    """把一个字段折算成「规划用」的 (数值, 单位)。没有配占位的字段原样返回。"""
    digits = _PLAN_NUMBER.get(key)
    if digits is None:
        return value, unit
    # 数值可能带货币前缀（"¥0.32"）—— 前缀原样留着，只把数字部分换成占位
    cut = 0
    while cut < len(value) and not value[cut].isdigit():
        cut += 1
    prefix, number = value[:cut], value[cut:]
    whole, _dot, _frac = number.partition(".")
    if not whole.isdigit():
        return value, unit
    placeholder = "9" * max(digits[0], len(whole))
    if digits[1]:
        # 小数位**不看当前值有没有小数点**：电量字段在 Wh 段是整数（"428"）、
        # 进了 kWh 段才有小数（"1.02"）。跟着实际格式走的话，这俩的占位宽度就
        # 不一样，Wh→kWh 那一步照样会改排版计划。
        placeholder += "." + "9" * digits[1]
    return prefix + placeholder, _PLAN_UNIT.get(key, unit)

_strips: dict[int, "TaskbarStrip"] = {}
_strip_proc_ref: WNDPROC | None = None


def _colorref(r: int, g: int, b: int) -> int:
    return (b << 16) | (g << 8) | r


def _unpack(colorref: int) -> tuple[int, int, int]:
    return (colorref & 0xFF, (colorref >> 8) & 0xFF, (colorref >> 16) & 0xFF)


def _mix(color: tuple[int, int, int], target: tuple[int, int, int],
         t: float) -> tuple[int, int, int]:
    return tuple(
        min(255, max(0, int(round(color[i] + (target[i] - color[i]) * t))))
        for i in range(3)
    )


def _luma(color: tuple[int, int, int]) -> float:
    """感知亮度。判定「这个底色上该用深字还是浅字」。"""
    return 0.299 * color[0] + 0.587 * color[1] + 0.114 * color[2]


def _palette_for(theme: str, sample, light: bool) -> dict:
    """质感 → 这一帧的整套配色（全部是 COLORREF，直接用）。

    ``sample`` / ``light`` 只有「跟随任务栏」「玻璃」「线框」用得到 —— 那三种要贴
    着任务栏底色走。固定色调的档位（深色 / 浅色 / 强调色）根本不需要采样，
    所以传 None 进来也没问题。

    ``alpha`` 是整块的不透明度（255 = 不透）；``key`` 非空表示走「抠色」模式，
    这时 ``bg`` 只当哨兵色用，最后会被抠成全透明（线框主题）。
    ``highlight`` 非空时会在胶囊上半部铺一层高光渐变。

    ``frost`` 是**毛玻璃浓度**（0 = 不糊，走实色底；非 0 就把背后任务栏糊掉再叠
    这个浓度），``frost_tint`` 是糊的时候用的色调层。色调层单独存一份、不跟着
    ``_apply_hover`` 的底色提亮一起变 —— 悬停要是把色调也改了，缓存键就每次都不同，
    鼠标一划过就得重新抓屏（抓屏要藏窗口，会闪）。反正悬停本来就靠描边变色提示。
    """
    if theme == "dark":
        bg = (26, 28, 33)
        return {
            "bg": _colorref(*bg), "alpha": 236, "key": None,
            "frost": 196, "frost_tint": bg,
            "border": _colorref(*_mix(bg, (255, 255, 255), 0.16)), "border_w": 1,
            "ink": _colorref(0xF2, 0xF5, 0xFA),
            "dim": _colorref(0xA8, 0xB2, 0xC2),
            "unit": _colorref(0x8A, 0x95, 0xA6),
            "div": _colorref(*_mix(bg, (255, 255, 255), 0.16)),
            "highlight": None,
        }
    if theme == "light":
        bg = (250, 250, 252)
        return {
            "bg": _colorref(*bg), "alpha": 242, "key": None,
            "frost": 205, "frost_tint": bg,
            "border": _colorref(*_mix(bg, (0, 0, 0), 0.11)), "border_w": 1,
            "ink": _colorref(0x1A, 0x1E, 0x24),
            "dim": _colorref(0x5E, 0x66, 0x74),
            "unit": _colorref(0x80, 0x8A, 0x9C),
            "div": _colorref(*_mix(bg, (0, 0, 0), 0.12)),
            "highlight": None,
        }
    if theme == "accent":
        # 和详情面板的 ACCENT 同色，托盘图标 / 面板 / 长条才是同一个视觉体系
        bg = (0x1E, 0x5C, 0xE0)
        return {
            "bg": _colorref(*bg), "alpha": 244, "key": None,
            "frost": 216, "frost_tint": bg,
            "border": _colorref(*_mix(bg, (255, 255, 255), 0.30)), "border_w": 1,
            "ink": _colorref(0xFF, 0xFF, 0xFF),
            "dim": _colorref(0xD5, 0xE0, 0xFA),
            "unit": _colorref(0xB7, 0xCB, 0xF4),
            "div": _colorref(*_mix(bg, (255, 255, 255), 0.26)),
            "highlight": _colorref(*_mix(bg, (255, 255, 255), 0.22)),
        }
    if theme == "outline":
        # 底色 = 采样到的**真实任务栏色**，毛玻璃的色调层也用它。
        #
        # v1.0.13 起这一档也上毛玻璃了：原来靠「把纯底色像素抠成透明」来透出
        # 真实任务栏（见下面的历史注释），但那样背景是**清晰**的、只是没有底 ——
        # 和其余档位的毛玻璃不是一套质感。现在统一成「糊掉 + 叠一层同色薄纱」：
        # 色调层就是任务栏自己的颜色，所以色相不变，只是加了一层均匀的雾 + 模糊，
        # 观感和原来「几乎看不出有底色」最接近，同时又是真毛玻璃。
        #
        # 老做法踩过的坑（保留备查）：一开始用醒目的品红当哨兵色，结果每个字都镶
        # 一圈紫边 —— 文字是抗锯齿画的，字边那一圈是「文字色 × 底色」的混合像素，
        # 它们不等于品红，抠色抠不掉。后来改成「底色就是背后的真实颜色」才干净。
        if light:
            border = _mix(sample, (0, 0, 0), 0.42)
            div = _mix(sample, (0, 0, 0), 0.22)
        else:
            border = _mix(sample, (255, 255, 255), 0.60)
            div = _mix(sample, (255, 255, 255), 0.30)
        return {
            "bg": _colorref(*sample), "alpha": 205, "key": None,
            "frost": 150, "frost_tint": sample,
            "border": _colorref(*border), "border_w": 1,
            "ink": _colorref(0x14, 0x18, 0x1E) if light else _colorref(0xF6, 0xF8, 0xFB),
            "dim": _colorref(0x4E, 0x57, 0x66) if light else _colorref(0xBC, 0xC5, 0xD3),
            "unit": _colorref(0x6C, 0x76, 0x88) if light else _colorref(0x9C, 0xA7, 0xB8),
            "div": _colorref(*div),
            "highlight": None,
        }
    if theme == "glass":
        if light:
            bg = _mix(sample, (255, 255, 255), 0.42)
            border = _mix(sample, (0, 0, 0), 0.12)
            ink, dim, unit = (0x1A, 0x1E, 0x24), (0x50, 0x58, 0x6A), (0x6E, 0x78, 0x89)
        else:
            bg = _mix(sample, (255, 255, 255), 0.26)
            border = _mix(sample, (255, 255, 255), 0.46)
            ink, dim, unit = (0xF2, 0xF5, 0xF9), (0xB4, 0xBE, 0xCA), (0x99, 0xA3, 0xB4)
        return {
            "bg": _colorref(*bg), "alpha": 200, "key": None,
            "frost": 132, "frost_tint": bg,
            "border": _colorref(*border), "border_w": 1,
            "ink": _colorref(*ink), "dim": _colorref(*dim), "unit": _colorref(*unit),
            "div": _colorref(*_mix(bg, (0, 0, 0) if light else (255, 255, 255), 0.16)),
            "highlight": _colorref(*_mix(bg, (255, 255, 255), 0.58 if light else 0.26)),
        }

    # ---- auto：采样任务栏底色（原行为）----
    if light:
        bg = _mix(sample, (255, 255, 255), 0.18)
        border = _mix(sample, (0, 0, 0), 0.13)
        ink, dim, unit = (0x1A, 0x1E, 0x24), (0x5E, 0x66, 0x74), (0x77, 0x80, 0x8E)
        div = _mix(sample, (0, 0, 0), 0.16)
    else:
        bg = _mix(sample, (255, 255, 255), 0.20)
        border = _mix(sample, (255, 255, 255), 0.26)
        ink, dim, unit = (0xF2, 0xF5, 0xF9), (0xA9, 0xB2, 0xC0), (0x8E, 0x98, 0xA8)
        div = _mix(sample, (255, 255, 255), 0.22)
    return {
        "bg": _colorref(*bg), "alpha": 232, "key": None,
        "frost": 168, "frost_tint": bg,
        "border": _colorref(*border), "border_w": 1,
        "ink": _colorref(*ink), "dim": _colorref(*dim), "unit": _colorref(*unit),
        "div": _colorref(*div),
        "highlight": None,
    }


def _apply_hover(pal: dict, active: bool) -> dict:
    """悬停 / 按住时的那套配色 —— 唯一的「这里可以拖」提示。

    只动两样东西：描边转向强调色、加粗一档；底色再轻轻提亮一点。**不做任何常驻
    装饰**（早先版本在两端画两列小圆点当抓手，用户的原话是「太丑了」）：
    鼠标不指着它的时候，长条就是一条干干净净的读数条。

    ``bg`` 也一起改是因为线框质感要靠 ``key`` 抠透明底 —— 那里 ``key`` 是真实
    任务栏色，不能跟着悬停变（变了就抠不干净、字会镶边），所以只调 ``bg`` 不动
    ``key``；线框档下 ``bg`` 本来就是哨兵，改了也无害。
    """
    if not active:
        return pal
    out = dict(pal)
    # 线框质感要保持「透出任务栏」的观感，底色不能动，只换描边
    if pal.get("key") is None:
        bg = _unpack(pal["bg"])
        out["bg"] = _colorref(*_mix(bg, (255, 255, 255), _HOVER_BG_MIX))
    out["border"] = _colorref(*_mix(_unpack(pal["border"]), _ACCENT, _HOVER_MIX))
    out["border_w"] = max(int(pal.get("border_w", 1)), _HOVER_BORDER_W)
    return out


@WNDPROC
def _strip_proc(hwnd, msg, wparam, lparam):
    strip = _strips.get(hwnd)
    if strip is not None:
        try:
            handled, result = strip._on_message(msg, wparam, lparam)
            if handled:
                return result
        except Exception:  # noqa: BLE001 - 回调里不能让异常逃逸
            pass
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


class TaskbarStrip:
    """任务栏上的长条读数。所有窗口操作都在主线程做。"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._hwnd = None
        self._parent = None     # 嵌进任务栏后的父窗口（Shell_TrayWnd）
        self._fonts: dict[str, int] = {}
        self._digit_w: dict[int, dict[str, int]] = {}   # 每个字体下 0-9 的宽度（防抖动用）
        self._brushes: dict[int, int] = {}
        self._pens: dict[tuple[int, int], int] = {}
        self._mem_dc = None
        self._bmp = None
        self._old_bmp = None
        self._view = None
        self._w = 0
        self._h = 0
        self._rect: tuple[int, int, int, int] | None = None
        self._key: tuple | None = None
        self._topmost_at = 0.0
        self._hidden = False
        # 量宽和绘制必须用同一个缩放系数，否则会出现「按 6 段量出来、按 4 段画」
        # 的错位，长条右边就会露出一块没画到的底。位置是每次 tick 先算的，
        # 所以把那次算出的系数记下来给绘制用。
        self._scale = 1.0
        # 宽度上限（像素）。开始按钮被运行中的程序挤到左边时，能用的地方会变小，
        # 这时候按上限丢字段；量宽和绘制必须用同一个上限。
        self._limit = 0
        # 这一帧锚在哪儿（"start" 贴开始按钮 / "tray" 贴通知区域）。
        # 只影响 _width_cap 的雪崩线取哪一档，见那边的注释。
        self._anchor_kind = "start"
        # ---- 折行 ----
        # 高度预算：折行最多能把胶囊撑到多高（由 _target_rect 按任务栏高度算）。
        # 量宽和绘制都读它，保证「量的时候 1 行、画的时候 2 行」这种错位不会发生。
        self._max_height = 0
        self._plan_rows = 1        # 这一帧排了几行
        self._plan_height = 0      # 这一帧需要多高（_target_rect 拿它定窗口大小）
        self._radius = 0           # 这一帧用的圆角（多行时会收小）
        self._tw: dict[tuple, int] = {}   # 文本宽度缓存，见 _raw_text_width
        # ---- 拖动（没有把手：整块胶囊就是拖动面）----
        # 光标是不是停在长条上。悬停时描边转强调色、光标变 ↔ —— 这是唯一一处
        # 「这里能拖」的提示，不做任何常驻装饰（小圆点那种一眼就丑）。
        self._hover = False
        # 是否已经向系统登记过 TME_LEAVE。不登记就收不到 WM_MOUSELEAVE，
        # 鼠标移开后高亮会一直亮着不灭。
        self._leave_tracked = False
        # 右键时弹谁的菜单。由 app 注入 —— 长条不直接依赖托盘对象，方便单测。
        self.menu_callback = None
        # 拖动中的状态。非 None 就是「正被拖」：里面记着按下时的光标 x 与长条左缘，
        # 之后每个 WM_MOUSEMOVE 只算增量 —— 用增量而不是绝对位置，长条跟着光标走
        # 时不会因为「窗口移动 → 客户区坐标跟着变」而产生反馈自激。
        self._drag: dict | None = None
        # 缩放（resize）中的状态。按住胶囊**上下边缘**竖拖 = 无级改字号：
        # 里面记着按下时的光标 y 与当时的字号，之后每个 WM_MOUSEMOVE 只算增量。
        # 与横向拖动共用「按下即捕获」的套路，但改的是字号不是位置。
        self._resize: dict | None = None
        # resize 拖动中临时生效的字号（cfg 还没写，松手才落盘）。
        # None = 用 cfg 里的值。渲染 / 测宽 / 定高都走 ``_font_scale()``，
        # 它先查这个覆盖值 —— 拖的时候长条才会跟着手指实时变大变小。
        self._resize_scale: float | None = None
        # 用户拖出来的横向偏移（设计基准像素；None = 还没跟 cfg 同步过）
        self._offset_nominal: float | None = None
        # 默认落点（不含拖动偏移）的屏幕 x。拖动时把「想去的 x」换算成偏移量要用它。
        self._base_left = 0
        # 最近一帧的配色（把手要用同样的 ink/dim 画小圆点）
        self._pal: dict | None = None
        # 最近一份快照：拖动时在消息回调里也要能重算落点，找不到 meter 就靠它
        self._last_snap = None

    # ------------------------------------------------------------- 生命周期

    def create(self, snap=None) -> bool:
        """建窗口。``snap`` 用来算初始宽度（拿不到就只能失败，见下）。

        注意这里必须**带着一份数据**进来：长条是「内容驱动尺寸」的，窗口宽度
        由当前字段宽度决定，没有数据算不出宽度，就没法定窗口大小。
        """
        global _strip_proc_ref
        _strip_proc_ref = _strip_proc

        hinstance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        # CS_DBLCLKS：没有它收不到 WM_LBUTTONDBLCLK，双击复位就不工作
        wc.style = _CS_DBLCLKS
        wc.lpfnWndProc = _strip_proc
        wc.hInstance = hinstance
        wc.lpszClassName = _CLASS_NAME
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            if ctypes.get_last_error() != 1410:
                debug.log("strip", "RegisterClassEx 失败")
                return False

        target = self._target_rect(snap)
        if target is None:
            # 没有任务栏、或者左边完全放不下：静默失败（上层当作「功能不可用」）
            debug.log("strip", "算不出落点（没有任务栏 / 放不下）")
            return False
        left, top, right, bottom = target

        ex = _EX_BASE | (0 if self._interactive else WS_EX_TRANSPARENT)
        self._hwnd = user32.CreateWindowExW(
            ex, _CLASS_NAME, "PowerMonitorStrip", WS_POPUP,
            left, top, right - left, bottom - top, None, None, hinstance, None,
        )
        if not self._hwnd:
            debug.log("strip", f"CreateWindowEx 失败 err={ctypes.get_last_error()}")
            return False

        _strips[self._hwnd] = self
        self._rect = target
        self._parent = None
        self._topmost_at = 0.0
        self._hidden = True
        # 拖动偏移从配置里接手（用户上次拖到哪儿就是哪儿）
        self._offset_nominal = self._cfg_offset()
        self._last_snap = snap
        # 先嵌进任务栏再显示，位置立刻按父窗口客户区坐标重排一次
        self._embed_into_taskbar()
        cl, ct, cr, cb = self._client_rect(target)
        user32.SetWindowPos(
            self._hwnd, None, cl, ct, cr - cl, cb - ct,
            SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOOWNERZORDER,
        )
        user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
        self._hidden = False
        self._ensure_above_siblings(force=True)
        self._render_if_needed(snap)
        if self._parent:
            debug.log("strip", f"创建成功 rect={target} 已嵌入任务栏 parent={self._parent:#x}")
        else:
            debug.log("strip", f"创建成功 rect={target}（未嵌入，走置顶兜底）")
        return True

    def destroy(self) -> None:
        self._drag = None
        self._resize = None
        self._resize_scale = None
        self._leave_tracked = False
        self._hover = False
        if self._hwnd:
            _strips.pop(self._hwnd, None)
            user32.DestroyWindow(self._hwnd)
            self._hwnd = None
        for font in self._fonts.values():
            gdi32.DeleteObject(font)
        self._fonts.clear()
        self._digit_w.clear()
        for brush in self._brushes.values():
            gdi32.DeleteObject(brush)
        self._brushes.clear()
        for pen in self._pens.values():
            gdi32.DeleteObject(pen)
        self._pens.clear()
        self._release_buffer()

    @property
    def hwnd(self):
        return self._hwnd

    @property
    def visible(self) -> bool:
        return bool(self._hwnd) and bool(user32.IsWindowVisible(self._hwnd))

    def _on_message(self, msg, wparam, lparam):
        if msg == WM_DESTROY:
            _strips.pop(self._hwnd, None)
            self._hwnd = None
            return True, 0
        if msg == WM_SETCURSOR:
            # lparam 低 16 位是命中测试码：只有落在客户区才改光标，
            # 边框 / 标题栏上也改的话光标会自己乱闪。
            if (int(lparam) & 0xFFFF) == HTCLIENT and self._interactive:
                # 上下边缘带里给 ↕（竖拖 = 改大小），其余区域给 ↔（横拖 = 挪位置）
                cur = IDC_SIZENS if self._cursor_on_edge() else IDC_SIZEWE
                user32.SetCursor(user32.LoadCursorW(None, int_resource(cur)))
                return True, 1
            return False, 0
        if msg == WM_MOUSEMOVE:
            if self._resize is not None:
                self._resize_move()
            elif self._drag is not None:
                self._drag_move()
            elif self._interactive:
                self._set_hover(True)
            return False, 0
        if msg == WM_MOUSELEAVE:
            self._leave_tracked = False
            if self._drag is None and self._resize is None:
                self._set_hover(False)
            return False, 0
        if msg == WM_LBUTTONDOWN:
            # 按下的 y 落在上下边缘带里 = 改大小，否则 = 挪位置
            y = (int(lparam) >> 16) & 0xFFFF
            if y >= 0x8000:
                y -= 0x10000
            if self._on_edge(y):
                self._resize_start(self._hwnd)
            else:
                self._drag_start(self._hwnd)
            return True, 0
        if msg == WM_LBUTTONUP:
            if self._resize is not None:
                self._resize_end()
            elif self._drag is not None:
                self._drag_end()
            return True, 0
        if msg == WM_LBUTTONDBLCLK:
            # 双击 = 位置复位。拖歪了又不想翻菜单的人用得上。
            self.reset_offset()
            return True, 0
        if msg == WM_RBUTTONUP:
            # 长条默认不再穿透，会把任务栏那一片的右键菜单吃掉 —— 所以这里
            # 主动把菜单补上（app 注入的回调会弹托盘那整套菜单）。
            if self.menu_callback is not None:
                try:
                    self.menu_callback()
                except Exception:  # noqa: BLE001 - 弹菜单失败不该带崩消息循环
                    pass
            return True, 0
        if msg == WM_CAPTURECHANGED:
            # 捕获被系统抢走（Alt+Tab / 弹窗）：当成松手，别让长条继续黏着鼠标
            if self._resize is not None:
                self._resize_end()
            if self._drag is not None:
                self._drag_end()
            return False, 0
        return False, 0

    # ------------------------------------------------------------- 拖动

    @property
    def _interactive(self) -> bool:
        """能不能被鼠标碰到（= 能不能拖）。锁定位置后变回穿透窗口。"""
        return not stripopts.locked(self.cfg)

    def _set_hover(self, on: bool) -> None:
        """悬停状态变了就重画；顺手登记 WM_MOUSELEAVE。"""
        if on and not self._leave_tracked and self._hwnd:
            tme = TRACKMOUSEEVENT()
            tme.cbSize = ctypes.sizeof(TRACKMOUSEEVENT)
            tme.dwFlags = TME_LEAVE
            tme.hwndTrack = self._hwnd
            tme.dwHoverTime = 0
            if user32.TrackMouseEvent(ctypes.byref(tme)):
                self._leave_tracked = True
        if on == self._hover:
            return
        self._hover = on
        self.invalidate()          # 悬停在 _style_key 里，所以这一下必然重画

    def set_interactive(self, enabled: bool) -> None:
        """开 / 关「可拖动」。关掉 = 加回 WS_EX_TRANSPARENT，变回纯显示。

        托盘菜单里那个「锁定位置」就是调它。运行期换扩展样式必须带
        SWP_FRAMECHANGED 让系统重新算一遍窗口的非客户区，否则改动可能不生效。
        """
        self._drag = None
        self._resize = None
        self._resize_scale = None
        self._leave_tracked = False
        self._hover = False
        if not self._hwnd:
            return
        ex = user32.GetWindowLongPtrW(self._hwnd, GWL_EXSTYLE)
        if enabled:
            ex &= ~WS_EX_TRANSPARENT
        else:
            ex |= WS_EX_TRANSPARENT
        user32.SetWindowLongPtrW(self._hwnd, GWL_EXSTYLE, ex)
        user32.SetWindowPos(
            self._hwnd, None, 0, 0, 0, 0,
            SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
            | SWP_FRAMECHANGED,
        )
        self.invalidate()

    def _cfg_offset(self) -> float:
        """配置里记着的拖动偏移（设计基准像素）。"""
        return stripopts.offset_x(self.cfg)

    def _offset(self) -> float:
        if self._offset_nominal is None:
            return self._cfg_offset()
        return self._offset_nominal

    def reset_offset(self) -> None:
        """回到默认落点（开始按钮左边那个位置）。"""
        self._drag = None
        self._offset_nominal = 0.0
        self._persist_offset()
        self.invalidate()

    def _drag_start(self, hwnd) -> None:
        if not self._rect:
            return
        pt = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(pt)):
            return
        self._drag = {"cursor": pt.x, "left": self._rect[0]}
        user32.SetCapture(hwnd)
        # 拖动期间保持「激活」外观（光标可能被甩出窗口，别让高亮闪掉）
        self._set_hover(True)
        debug.log("strip", f"开始拖动 left={self._rect[0]} cursor={pt.x}")

    def _drag_move(self) -> None:
        drag = self._drag
        if drag is None or not self._hwnd:
            return
        pt = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(pt)):
            return
        want = drag["left"] + (pt.x - drag["cursor"])
        scale = self._scale or 1.0
        # 存的是**相对默认落点的偏移**（设计基准像素），不是绝对屏幕 x —— 换 DPI、
        # 改任务栏高度、改长条大小之后位置依然合理，不会跑到屏幕外面去。
        self._offset_nominal = max(
            -stripopts.OFFSET_LIMIT,
            min(stripopts.OFFSET_LIMIT, (want - self._base_left) / scale),
        )
        self._drag_apply()

    def _drag_apply(self) -> None:
        """按当前偏移立刻把窗口搬过去（不等下一 tick，手感才跟手）。"""
        target = self._target_rect(self._last_snap)
        if target is None or target == self._rect:
            return
        cl, ct, cr, cb = self._client_rect(target)
        user32.SetWindowPos(
            self._hwnd, None, cl, ct, cr - cl, cb - ct,
            SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOOWNERZORDER,
        )
        self._rect = target

    def _drag_end(self) -> None:
        self._drag = None
        user32.ReleaseCapture()
        self._persist_offset()
        # 换了位置就要按新位置重新采一次任务栏底色（auto / 玻璃 / 线框三种质感
        # 的底色是现场采样来的），顺手把悬停那圈强调描边收掉。
        self._key = None
        self._render_if_needed(self._last_snap)
        debug.log("strip", f"拖动结束 rect={self._rect} 偏移={self._offset():.1f}")

    def _persist_offset(self) -> None:
        try:
            old = float(getattr(self.cfg, "strip_offset_x", 0.0) or 0.0)
        except (TypeError, ValueError):
            old = 0.0
        if abs(old - self._offset()) < 0.5:
            return
        try:
            self.cfg.strip_offset_x = round(self._offset(), 1)
            self.cfg.save()
        except Exception:  # noqa: BLE001 - 存盘失败不该影响拖动本身
            pass

    # ---------------------------------------------------- 拖上下边缘改大小
    #
    # 与「横拖挪位置」互补：光标落在胶囊上下边缘带里时变 ↕，按住竖拖就是
    # 无级缩放字号（0.60 ~ 1.60，菜单里那六档只是它的快捷取值）。
    # 向上拖变大、向下拖缩小；拖动过程中**不落盘**，松手才写 cfg。

    def _edge_band(self) -> float:
        return _EDGE_PX * (self._scale or 1.0)

    def _on_edge(self, y: float) -> bool:
        """客户区 y 是不是落在上下边缘带里。"""
        if not self._rect:
            return False
        h = self._rect[3] - self._rect[1]
        band = self._edge_band()
        return y < band or y >= h - band

    def _cursor_on_edge(self) -> bool:
        """当前光标（屏幕坐标）换算到客户区后，是不是在边缘带里。"""
        pt = wintypes.POINT()
        if not self._hwnd or not user32.GetCursorPos(ctypes.byref(pt)):
            return False
        if not user32.ScreenToClient(self._hwnd, ctypes.byref(pt)):
            return False
        return self._on_edge(pt.y)

    def _font_scale(self) -> float:
        """当前生效的字号：resize 拖动中用手指拖出来的值，否则用配置。"""
        if self._resize_scale is not None:
            return self._resize_scale
        return stripopts.font_scale(self.cfg)

    def _height_ratio(self) -> float:
        """胶囊高度 / 任务栏高度。resize 拖动中跟着临时字号走，长条实时变高变矮。"""
        if self._resize_scale is None:
            return stripopts.height_ratio(self.cfg)
        ratio, _pad = stripopts.size_spec(self.cfg)
        return min(0.98, ratio * (0.74 + 0.26 * self._resize_scale))

    def _resize_start(self, hwnd) -> None:
        if not self._rect:
            return
        pt = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(pt)):
            return
        self._resize = {"cursor": pt.y, "scale0": stripopts.font_scale(self.cfg)}
        self._resize_scale = self._resize["scale0"]
        user32.SetCapture(hwnd)
        # 缩放期间保持「激活」外观（光标可能被甩出窗口，别让高亮闪掉）
        self._set_hover(True)
        debug.log("strip",
                  f"开始缩放 scale0={self._resize['scale0']:.2f} cursor_y={pt.y}")

    def _resize_move(self) -> None:
        rs = self._resize
        if rs is None or not self._hwnd:
            return
        pt = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(pt)):
            return
        scale = self._scale or 1.0
        # 向上拖变大：dy = 按下时的 y − 现在的 y，换成设计基准像素后按比例映射。
        dy = (rs["cursor"] - pt.y) / scale
        want = rs["scale0"] + dy / _NOMINAL_TASKBAR_H * _RESIZE_RATE
        want = max(stripopts.FONT_SCALE_MIN,
                   min(stripopts.FONT_SCALE_MAX, want))
        if self._resize_scale is not None and \
                abs(want - self._resize_scale) < _RESIZE_STEP:
            return                      # 变化太小：重排是逐像素合成，省一帧是一帧
        self._resize_scale = want
        # 字号变 → 内容高度变 → 窗口位置 / 尺寸都要重算，内容也要重画
        self._key = None
        self._relayout()

    def _relayout(self) -> None:
        """按当前（可能正被拖着的）字号重排一次：搬窗口 + 重画。"""
        target = self._target_rect(self._last_snap)
        if target is not None and target != self._rect:
            cl, ct, cr, cb = self._client_rect(target)
            user32.SetWindowPos(
                self._hwnd, None, cl, ct, cr - cl, cb - ct,
                SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOOWNERZORDER,
            )
            self._rect = target
        self._render_if_needed(self._last_snap)

    def _resize_end(self) -> None:
        rs = self._resize
        final = self._resize_scale
        self._resize = None
        self._resize_scale = None
        user32.ReleaseCapture()
        if rs is not None and final is not None:
            final = round(final, 3)
            if abs(stripopts.font_scale(self.cfg) - final) > 1e-6:
                try:
                    self.cfg.strip_font_scale = final
                    self.cfg.save()
                except Exception:  # noqa: BLE001 - 存盘失败不该影响松手后的重排
                    pass
        self._key = None
        self._render_if_needed(self._last_snap)
        debug.log("strip", f"缩放结束 scale={final}")

    # ------------------------------------------------------------- 资源

    def _release_buffer(self) -> None:
        if self._mem_dc:
            if self._old_bmp:
                gdi32.SelectObject(self._mem_dc, self._old_bmp)
            if self._bmp:
                gdi32.DeleteObject(self._bmp)
            gdi32.DeleteDC(self._mem_dc)
        self._mem_dc = None
        self._bmp = None
        self._old_bmp = None
        self._view = None
        self._w = self._h = 0

    def _font(self, key: str, size: float, bold: bool = False):
        cached = self._fonts.get(key)
        if cached:
            return cached
        font = gdi32.CreateFontW(
            -max(7, int(round(size))), 0, 0, 0, FW_BOLD if bold else FW_NORMAL,
            0, 0, 0, DEFAULT_CHARSET, 0, 0, CLEARTYPE_QUALITY, 0, _FONT_FACE,
        )
        self._fonts[key] = font
        return font

    def _brush(self, color: int):
        brush = self._brushes.get(color)
        if brush is None:
            brush = gdi32.CreateSolidBrush(color)
            self._brushes[color] = brush
        return brush

    def _pen(self, color: int, width: int = 1):
        key = (color, int(width))
        pen = self._pens.get(key)
        if pen is None:
            pen = gdi32.CreatePen(PS_SOLID, int(width), color)
            self._pens[key] = pen
        return pen

    def _fill(self, dc, x, y, w, h, color) -> None:
        rect = wintypes.RECT(int(x), int(y), int(x + w), int(y + h))
        user32.FillRect(dc, ctypes.byref(rect), self._brush(color))

    def _frost_bg(self, dc, x, y, w, h, pal) -> None:
        """胶囊底色：毛玻璃（糊掉背后的任务栏 + 叠本档色调）或实色兜底。

        先铺一层实色再往上盖毛玻璃 —— 这一层不是多余的：① 毛玻璃抓不到（窗口还
        没显示、屏幕被挡）时它就是最终配色，② 拖动中 ``hold`` 只会贴旧图，
        尺寸变大时多出来的那条就靠它兜着，不然是没画过的黑边。
        """
        self._fill(dc, x, y, w, h, pal["bg"])
        strength = int(pal.get("frost") or 0)
        if not strength or not self._rect:
            return
        tint = pal.get("frost_tint") or _unpack(pal["bg"])
        frost.blit(
            dc, "strip",
            self._rect[0], self._rect[1], x, y, w, h,
            tint, strength,
            # 抓屏前要把长条自己藏起来：不藏的话抓进来的就是它上一帧的样子，
            # 一帧帧叠着糊下去会越糊越黑。
            hide_hwnd=self._hwnd,
            # 拖动中锁死缓存（重抓要藏窗口，每秒几十次会闪成一片）
            hold=self._drag is not None,
        )

    def _raw_text_width(self, dc, text: str, font) -> int:
        """量文本宽度（带缓存）。

        缓存不是可有可无的优化：选「排几行」要对同一批字段在几个字号下各量一遍，
        而 GDI 的 GetTextExtentPoint32W 每次都要重新排版。没有缓存时每秒光量宽
        就要上千次调用。文本内容每秒都在变（功率数字），所以缓存设了上限，
        超了整批清掉，不会无限长。
        """
        key = (font, text)
        cached = self._tw.get(key)
        if cached is not None:
            return cached
        if len(self._tw) > _TEXT_WIDTH_CACHE:
            self._tw.clear()
        old = gdi32.SelectObject(dc, font)
        size = SIZE()
        gdi32.GetTextExtentPoint32W(dc, text, len(text), ctypes.byref(size))
        gdi32.SelectObject(dc, old)
        self._tw[key] = size.cx
        return size.cx

    def _digit_widths(self, dc, font) -> dict[str, int]:
        """量一遍 0-9 各自宽度并缓存（同一字体只量一次）。"""
        cached = self._digit_w.get(font)
        if cached is not None:
            return cached
        old = gdi32.SelectObject(dc, font)
        size = SIZE()
        ws: dict[str, int] = {}
        for d in "0123456789":
            gdi32.GetTextExtentPoint32W(dc, d, 1, ctypes.byref(size))
            ws[d] = size.cx
        gdi32.SelectObject(dc, old)
        self._digit_w[font] = ws
        return ws

    def _text_width(self, dc, text: str, font) -> int:
        """量文本宽度，但所有数字按「最宽数字」计。

        Microsoft YaHei UI 的数字是比例宽度（'1' 明显比 '0' 窄），长条又是按内容
        自适应宽度的，于是实时功率一变（101 W ↔ 115 W）整条就跟着改宽、左边缘左右
        跳 15px，看起来像在抖。把数字统一按最宽算，宽度就与具体数值无关了。
        """
        if not any("0" <= ch <= "9" for ch in text):
            return self._raw_text_width(dc, text, font)
        ws = self._digit_widths(dc, font)
        widest = max(ws.values())
        extra = sum(widest - ws[ch] for ch in text if "0" <= ch <= "9")
        return self._raw_text_width(dc, text, font) + extra

    def _text(self, dc, text, x, y, w, h, font, color, align=DT_LEFT) -> None:
        old = gdi32.SelectObject(dc, font)
        gdi32.SetTextColor(dc, color)
        gdi32.SetBkMode(dc, TRANSPARENT)
        rect = wintypes.RECT(int(x), int(y), int(x + w), int(y + h))
        user32.DrawTextW(
            dc, text, len(text), ctypes.byref(rect),
            align | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX,
        )
        gdi32.SelectObject(dc, old)

    # ------------------------------------------------------------- 内容

    def _sections(self, snap, compact: bool = False) -> list[tuple[str, str, str, str]]:
        """要显示的字段（key, 标签, 数值, 单位）。

        顺序完全由用户在「长条显示内容」里定的顺序决定，宽度不够时从后往前丢 ——
        所以靠后的字段是「有余量才显示」的那些。

        ``compact=True`` 换两字短标签，只在折行 + 挤不下时才由 ``_plan`` 启用。
        """
        return stripopts.sections(snap, self.cfg, compact=compact)

    def _style_key(self) -> tuple:
        """只跟「长条长什么样」有关的键。

        必须算进 ``_content_key``：改质感 / 字号 / 尺寸 / 显示项都不会改变数值本身，
        只比数值的话 ``_render_if_needed`` 会认为「没变化」直接返回，
        用户点了半天菜单长条纹丝不动。

        ``hover`` 也要算进来 —— 悬停是把描边换成强调色，属于「长什么样」，
        不算的话鼠标指上去不会重画，那圈提示色永远不出现。
        """
        return (
            stripopts.theme(self.cfg),
            round(self._font_scale(), 3),
            stripopts.size_key(self.cfg),
            tuple(stripopts.enabled_fields(self.cfg)),
            self._hover,
            stripopts.locked(self.cfg),
        )

    def _content_key(self, snap) -> tuple:
        return (
            self._style_key(),
            tuple((k, v, u) for k, _label, v, u in self._sections(snap)),
        )

    def _resolve_palette(self, total: int, height: int,
                         origin_x: int, origin_y: int) -> dict:
        """这一帧要用哪套配色。固定色调的档位不需要采样任务栏。"""
        theme = stripopts.theme(self.cfg)
        sample = None
        light = False
        if theme in ("auto", "glass", "outline"):
            # 采样要按长条**在屏幕上的真实位置**取（origin 是画布内偏移，不是屏幕坐标）
            base_x, base_y = (self._rect[0], self._rect[1]) if self._rect else (0, 0)
            sample = self._sample_taskbar(base_x + origin_x, base_y + origin_y,
                                          total, height)
            if sample is None:
                # 采不到（窗口还没显示、像素被遮挡）才退回注册表口径
                sample = (233, 237, 243) if taskbar.uses_light_theme() else (32, 32, 32)
            else:
                # 明暗直接看**实际像素**，不看注册表：本机实测 SystemUsesLightTheme=1
                # 时任务栏却因为「自动」主题 + 深色壁纸呈深色，读注册表会判断反，
                # 结果就是在深色任务栏上画一条浅灰底黑字，非常突兀。
                light = _luma(sample) >= 128
        pal = _palette_for(theme, sample, light)
        active = self._hover or self._drag is not None
        return _apply_hover(pal, active) if active else pal

    # ------------------------------------------------------------- 位置

    def _target_rect(self, snap) -> tuple[int, int, int, int] | None:
        """算出长条该在屏幕上的哪个位置。放不下就返回 None（应该藏起来）。"""
        info = taskbar.taskbar()
        if info is None:
            return None
        _bar, trect, _dpi = info
        if not taskbar.is_visible(trect):
            return None

        bar_top, bar_bottom = trect[1], trect[3]
        bar_h = bar_bottom - bar_top
        scale = bar_h / _NOMINAL_TASKBAR_H
        self._scale = scale
        # 单行高度由「尺寸」档位决定，字号大了再往上抬一点（大了要更多行高，
        # 否则字会被上下切掉）。默认档 = 0.78，和以前一样。
        base_height = max(18, int(round(bar_h * self._height_ratio())))
        # 折行时允许长高（最多到任务栏的 0.94），但不会比单行矮 ——
        # 「尺寸」档位依然是单行时的外观契约，不会被折行偷偷改掉。
        self._max_height = max(base_height,
                               int(round(bar_h * _MULTI_ROW_MAX_RATIO)))

        screen = taskbar.screen_rect()
        gap = int(round(16 * scale))

        anchor = None
        # 用的是哪个锚点，决定宽度上限走哪一档（见 _width_cap）
        kind = "start"
        if self.cfg.strip_position == "start":
            anchor = taskbar.cluster_left()      # 贴着开始按钮左边
        if anchor is None:
            tray = taskbar.notification_area()
            if tray is None:
                return None
            anchor = tray[0]                     # 兜底：贴通知区域左边
            kind = "tray"
        self._anchor_kind = kind

        right = anchor - gap
        # 能用的宽度 = 从屏幕左边留 8px 到锚点左边 —— **这才是真正的上限**。
        # 开始按钮在 Win11 是居中偏左那一簇的左端，它左边通常是整条空任务栏，
        # 所以这里往往有上千像素，够显示十几个字段。任务栏改左对齐、或者同时开了
        # 一堆程序把开始按钮挤到很左边时，这里会变小 —— 那种情况就按这个上限少显示
        # 几个字段，而不是整条消失（整条消失用户会以为功能坏了）。
        available = right - (screen[0] + 8)
        if available < int(round(_ABS_MIN_WIDTH * scale)):
            return None
        self._limit = min(int(round(self._width_cap(scale))), available)

        width = self._measure_width(scale, snap)
        if width <= 0:
            return None

        # 高度取「量宽那一步算出来的行数」需要的高度 —— 字段少就跟以前一样高，
        # 勾得多了才长高折行。上限就是上面那条 _max_height。
        #
        # 取整放在这里（而不是等到拼矩形时）是为了让窗口高度是个整数：
        # 绘制那边 `_layout` 拿 `height` 当画布高度，半个像素的零头会让
        # 描边和 alpha 遮罩差一行，边缘上就会出现一条毛边。
        height = int(round(max(base_height, min(self._plan_height or base_height,
                                                self._max_height))))
        # strip_position 的兜底锚点比 start 矮一点也无所谓，这里统一居中
        top = bar_top + (bar_h - height) // 2

        left = right - width
        # 默认落点记下来：拖动时把「光标想去的 x」换算成偏移量要用它（见 _drag_move）
        self._base_left = int(left)
        # ---- 用户拖出来的横向偏移 ----
        # 偏移量存的是设计基准像素（见 _drag_move 的注释），这里按 scale 还原成
        # 真实像素再夹回「屏幕左边 8px ~ 通知区域左边」，拖不出可见范围。
        offset = int(round(self._offset() * scale))
        if offset:
            left += offset
            right += offset
        min_left = screen[0] + 8
        max_right = screen[2] - 8
        tray = taskbar.notification_area()
        if tray is not None:
            # 往右最多到通知区域左边：那边是时钟和托盘图标，压上去就成了「挡住系统」
            max_right = min(max_right, tray[0] - 8)
        if max_right - width < min_left:
            max_right = min_left + width
        if left < min_left:
            left, right = min_left, min_left + width
        if right > max_right:
            right, left = max_right, max_right - width
        return (int(left), int(top), int(right), int(top + height))

    def _width_cap(self, scale: float) -> float:
        """雪崩线：长条最多能到多宽（像素）。

        真正的上限是 ``_target_rect`` 里按当前任务栏布局实测出来的 ``available``，
        这里只是防止锚点算歪时长条无限铺开。贴开始按钮时放得很宽（左边整条都是
        空的），退化成贴通知区域时保守一些（往左就是那排任务图标）。

        字号调大时内容本来就变宽，上限必须跟着放宽 —— 否则大字号下会有一两个
        字段被从后往前砍掉，用户会觉得「把字调大反而少显示了一项」。小字号不
        收缩上限：内容本来就窄、够不到上限，缩了只是白白少一份余量。
        """
        base = _MAX_WIDTH_TRAY if self._anchor_kind == "tray" else _MAX_WIDTH
        font_mult = max(1.0, self._font_scale())
        return base * scale * font_mult

    def _measure_width(self, scale: float, snap) -> int:
        """先算需要多宽。用一个临时 DC 量字宽即可。"""
        if snap is None:
            return 0
        screen = user32.GetDC(None)
        dc = gdi32.CreateCompatibleDC(screen)
        try:
            width = self._layout(dc, scale, snap, render=False)[0]
        finally:
            gdi32.DeleteDC(dc)
            user32.ReleaseDC(None, screen)
        # 连「当前功率 + 本次电费」两段都塞不进可用宽度，就别硬塞了。
        # 留一位数字的余量（见 _PLAN_TOLERANCE）：规划用的是占位值，实际读数
        # 偶尔会多一位，多出那十来像素不值得把整条藏掉。
        if self._limit:
            slack = int(round(_PLAN_TOLERANCE * (self._scale or 1.0)))
            if width > self._limit + slack:
                return 0
        return width

    def _layout(self, dc, scale: float, snap, render: bool,
                origin_x: int = 0, origin_y: int = 0, height: int = 0):
        """量宽 + 画。返回 ``(宽, 高, 配色)``；``render=False`` 时配色为 None。

        ``render=False`` 时只在临时 DC 上量字宽，不画；这样位置和内容用同一套
        布局代码算，不会出现「量的时候 3 段、画的时候 4 段」这种错位。
        顺带返回配色，是为了让调用方（``_render_if_needed``）拿到 ``alpha`` /
        ``key`` 再透传给 ``compose_shape_alpha`` —— 半透明与抠色两种质感都得靠它。

        **折行 + 表格**：一行排不下所有勾选的字段时就往上加行（最多
        ``_MAX_ROWS`` 行），必要时把字号降一档换高度；折行之后**不再各排各的**，
        而是排成一张「行 × 列」的表格（``_grid``），这样两行的分隔线、每一列的
        起点都在同一条竖线上。行数只由「字段、可用宽度、可用的最大高度」决定，
        跟传进来的 ``height`` 无关 —— 量宽和绘制两条路径因此必然得到同一个计划，
        画出来的东西和窗口大小严丝合缝。

        间距（标签间隙 / 单位间隙 / 分隔宽度）也由计划给出，不再在这里算：
        挤的时候 ``_plan`` 会收紧它们，量宽和绘制必须用同一套值。
        """
        limit = self._limit or int(round(self._width_cap(scale)))
        max_height = self._max_height or max(height, 0)

        plan = self._plan(dc, snap, scale, limit, max_height)
        rows = plan["rows"]
        grid = plan["grid"]
        fonts = plan["fonts"]
        gap_label = plan["gap_label"]
        gap_unit = plan["gap_unit"]
        div_margin = plan["div_margin"]

        pad_mult = stripopts.size_spec(self.cfg)[1]
        pad_x = _PAD_X * scale * pad_mult

        width = int(round(pad_x * 2 + grid["width"]))
        row_h = plan["row_h"]
        content_h = max(int(round(row_h * len(rows))), 1)
        # 画布高度 = **窗口高度**（`_target_rect` 按 base_height / 内容高度算好的），
        # 内容多条时再按 content_h 铺满。
        #
        # 这里踩过坑：折行改造时一度把画布高度写成 content_h（= 行高 × 行数）。
        # 单行时 content_h 只有 32.5px，而窗口是 47px 高的胶囊 —— 于是底色和描边
        # 只画了上面 32.5px，下面 14.5px 是没填过的黑底色，叠上半透明就成了一条
        # 黑带。窗口高度才是权威：画布永远占满它，几行内容在画布里**垂直居中**，
        # 这样单行时的观感和老版本逐像素一致。
        canvas_h = max(height, content_h) if height > 0 else content_h
        band_top = origin_y + (canvas_h - content_h) / 2.0
        self._plan_rows = len(rows)
        self._plan_height = content_h
        if not render:
            return width, canvas_h, None

        # ---- 配色：由「质感」档位决定，固定色调的档位不需要采样任务栏 ----
        pal = self._resolve_palette(width, canvas_h, origin_x, origin_y)
        if len(rows) == 1:
            radius = canvas_h / 2.0
        else:
            radius = min(canvas_h / 2.0, canvas_h * _MULTI_ROW_RADIUS_RATIO)
        self._radius = radius
        self._frost_bg(dc, origin_x, origin_y, width, canvas_h, pal)
        # 高光必须在画文字**之前**铺，否则会把刚画上去的字一起洗白。
        if pal["highlight"] is not None:
            self._highlight(width, canvas_h, origin_x, origin_y, pal["highlight"])
        # 描边用 RoundRect 的**空心**画笔勾。RoundRect 会用当前画刷填充内部，
        # 如果这里选实心刷会把刚铺好的底色整块盖掉，所以必须用 NULL_BRUSH。
        old_brush = gdi32.SelectObject(dc, gdi32.GetStockObject(NULL_BRUSH))
        old_pen = gdi32.SelectObject(dc, self._pen(pal["border"], pal["border_w"]))
        gdi32.RoundRect(dc, origin_x, origin_y, origin_x + width,
                        origin_y + canvas_h, int(radius * 2), int(radius * 2))
        gdi32.SelectObject(dc, old_pen)
        gdi32.SelectObject(dc, old_brush)

        # ---- 逐行逐列 ----
        # 每一列都从 ``origin_x + pad_x + 前面所有列宽`` 开始，和行号无关 ——
        # 这就是「两排对齐」的全部秘密：起点和分隔线的 x 只由列决定。
        label_w = grid["label_w"]
        col_w = grid["col_w"]
        div_px = max(1, scale)
        for index, row in enumerate(rows):
            top = band_top + row_h * index
            mid = top + row_h / 2.0
            x = origin_x + pad_x
            for j, (_key, label, value, unit) in enumerate(row):
                slot = label_w[j]
                w_value = self._text_width(dc, value, fonts["value"])
                self._text(dc, label, x, top, slot + 1, row_h,
                           fonts["label"], pal["dim"])
                vx = x + slot + gap_label
                self._text(dc, value, vx, top, w_value + 2, row_h,
                           fonts["value"], pal["ink"])
                if unit:
                    w_unit = self._text_width(dc, unit, fonts["unit"])
                    self._text(dc, unit, vx + w_value + gap_unit, top,
                               w_unit + 2, row_h, fonts["unit"], pal["unit"])
                x += col_w[j]
                if j < len(row) - 1:
                    x += div_margin
                    div_h = row_h * 0.46
                    self._fill(dc, x, mid - div_h / 2, div_px, div_h, pal["div"])
                    x += div_px + div_margin
        return width, canvas_h, pal

    # ------------------------------------------------------------- 网格排版

    def _split(self, sections, rows_wanted: int) -> list[list[tuple]]:
        """把字段摊成若干行：阅读顺序（从左到右、从上到下），每行尽量一样多。

        列数 = ceil(字段数 / 行数)，于是每行要么 ``列数`` 个、要么少一个
        （只有最后一行短）。行数取 ``min(rows_wanted, 字段数)`` —— 字段比行数
        还少时不要摊出一堆空行。
        """
        n = len(sections)
        if n <= 0:
            return []
        rows = max(1, min(int(rows_wanted), n))
        cols = -(-n // rows)
        return [list(sections[i * cols:(i + 1) * cols]) for i in range(rows)]

    def _grid(self, dc, rows, fonts, gap_label, gap_unit, div_margin) -> dict:
        """量出一张「行 × 列」表格：每列多宽、每个格子的标签槽多宽。

        列宽 = 该列所有格子里的最大值（标签槽同理），所以：

          * 每个格子在自己的列里**从同一个 x 开始**；
          * 分隔线也跟着列宽走 → 两行的竖线在一条线上（用户要的「对齐」）；

        数值一律按「占位值 / 真实值里更宽的那个」算：占位值（``_plan_cell``）
        保证宽度不随读数变化（长条不会因为 999 W 变 1000 W 就抽一下），真实值
        兜底保证再离谱的读数也不会被截掉。

        单行列数 = 字段数，列宽就是格子自身宽度 —— 所以只勾几项时算出来的
        尺寸和加折行之前**逐像素一致**。
        """
        cols = max((len(r) for r in rows), default=0)
        label_w = [0.0] * cols
        rest_w = [0.0] * cols
        for row in rows:
            for j, (key, label, value, unit) in enumerate(row):
                w_label = self._text_width(dc, label, fonts["label"])
                plan_value, plan_unit = _plan_cell(key, value, unit)
                w_value = max(self._text_width(dc, value, fonts["value"]),
                              self._text_width(dc, plan_value, fonts["value"]))
                rest = w_value
                if unit or plan_unit:
                    w_unit = 0.0
                    if unit:
                        w_unit = self._text_width(dc, unit, fonts["unit"])
                    if plan_unit:
                        w_unit = max(w_unit,
                                     self._text_width(dc, plan_unit, fonts["unit"]))
                    rest = w_value + gap_unit + w_unit
                label_w[j] = max(label_w[j], w_label)
                rest_w[j] = max(rest_w[j], rest)
        col_w = [label_w[j] + gap_label + rest_w[j] for j in range(cols)]
        total = sum(col_w)
        if cols > 1:
            total += (cols - 1) * (div_margin * 2 + 1)
        return {
            "cols": cols, "col_w": col_w, "label_w": label_w, "rest_w": rest_w,
            "width": total,
        }

    def _fit_sections(self, dc, sections, budget, rows_wanted, gap_label,
                      gap_unit, div_margin, fonts) -> list[tuple]:
        """在「最多 rows_wanted 行」的约束下，从后往前丢字段，返回排得下的前缀。

        只用于「怎么都塞不下」的兜底：先尽量多排，而不是一上来就退回单行。
        丢永远从**末尾**丢（末尾是「有余量才显示」的字段），长条左边的读数不会
        因为勾得多而改变位置 —— 用户的眼睛盯着的就是最左边那几个数。
        """
        shown = list(sections)
        while len(shown) > 1:
            rows = self._split(shown, rows_wanted)
            grid = self._grid(dc, rows, fonts, gap_label, gap_unit, div_margin)
            if grid["width"] <= budget:
                return shown
            shown.pop()
        return shown

    # ------------------------------------------------------------- 折行计划

    def _fonts_for(self, scale: float, font_scale: float) -> dict:
        # 字体缓存键必须带上字号倍率：同一个 scale 下换了字号就是另一套字体，
        # 只按 scale 缓存会拿回旧尺寸的字体，现象就是「调了字号没反应」。
        fkey = f"{scale:.3f}x{font_scale:.2f}"
        return {
            "label": self._font(f"lbl{fkey}", _LABEL_SZ * scale * font_scale),
            "value": self._font(f"val{fkey}", _VALUE_SZ * scale * font_scale,
                                bold=True),
            "unit": self._font(f"unt{fkey}", _UNIT_SZ * scale * font_scale),
        }

    def _row_height(self, scale: float, font_scale: float) -> float:
        """一行要多高。比字号略大一点，否则两行的字会贴在一起。"""
        return _ROW_H * scale * font_scale

    def _plan(self, dc, snap, scale: float, limit: int, max_height: int) -> dict:
        """决定「排几行、用哪档字号、要不要换短标签 / 收紧间距」。

        偏好顺序（先来的优先）：

        1. **保持用户选的字号、单行、原样标签** —— 字段少的时候外观跟以前一模一样；
        2. 保持字号、折成 2~3 行（高度够的话）；
        3. 逐档降字号（最大 → 小）再折行；
        4. 还是排不下就换短标签（第 1 档密度）重来一遍；
        5. 再收紧分隔间距（第 2 档密度）重来一遍；
        6. 全部试完仍塞不下，才从后往前丢字段，并且**尽量少丢**。

        单行时**绝不**自动降字号：用户点了「巨大」就该是巨大，哪怕因此少显示
        几项 —— 否则「调字号没反应」比「少显示一项」更让人费解。同理，第 0 档
        密度是外观契约，只有前面全部失败才会动到标签和间距。
        """
        base_font = self._font_scale()
        smaller = [v for v in reversed(stripopts.FONT_SCALE_VALUES) if v < base_font]
        pad_mult = stripopts.size_spec(self.cfg)[1]
        pad_x = _PAD_X * scale * pad_mult
        budget = max(1, limit - int(round(pad_x * 2)))

        def max_rows_for(font_scale: float) -> int:
            row_h = self._row_height(scale, font_scale)
            if max_height <= 0:
                return 1
            return max(1, min(_MAX_ROWS, int(max_height // row_h)))

        # (行数上限, 字号) 候选，按偏好排序
        candidates: list[tuple[int, float]] = [(1, base_font)]
        for rows in range(2, _MAX_ROWS + 1):
            for font_scale in [base_font] + smaller:
                candidates.append((rows, font_scale))
        for rows in range(1, _MAX_ROWS + 1):
            for font_scale in smaller:
                candidates.append((rows, font_scale))

        cache: dict[bool, list] = {}

        def sections_for(compact: bool):
            if compact not in cache:
                cache[compact] = self._sections(snap, compact=compact)
            return cache[compact]

        def attempt(compact, div_mult, gap_mult, rows_wanted, font_scale,
                    density_index, keep: int | None = None):
            """按这一组档位排一次，返回计划（含网格）。

            ``keep`` 非 None 时只用前 ``keep`` 个字段 —— 兜底丢字段时用，
            丢永远从末尾丢，所以剩下的必然是一个前缀。
            """
            sections = sections_for(compact)
            total = len(sections)
            if keep is not None:
                sections = sections[:max(1, keep)]
            gap_label = _GAP_LABEL * scale * gap_mult
            gap_unit = _GAP_UNIT * scale * gap_mult
            div_margin = _DIV_MARGIN * scale * div_mult
            fonts = self._fonts_for(scale, font_scale)
            rows = self._split(sections, rows_wanted)
            grid = self._grid(dc, rows, fonts, gap_label, gap_unit, div_margin)
            return {
                "rows": rows,
                "grid": grid,
                "font_scale": font_scale,
                "row_h": self._row_height(scale, font_scale),
                "fonts": fonts,
                "gap_label": gap_label,
                "gap_unit": gap_unit,
                "div_margin": div_margin,
                "placed": sum(len(r) for r in rows),
                "total": total,
                "density": density_index,
                "compact": compact,
            }

        # ---- 第一遍：按偏好顺序找「第一个能把所有字段排下」的方案 ----
        for index, (compact, div_mult, gap_mult) in enumerate(_DENSITY):
            for rows_wanted, font_scale in candidates:
                if rows_wanted > max_rows_for(font_scale):
                    continue
                # 第 0 档的「单行 + 用户字号」是外观契约，不许换标签 / 收紧间距
                if index and rows_wanted == 1 and font_scale == base_font:
                    continue
                plan = attempt(compact, div_mult, gap_mult, rows_wanted,
                               font_scale, index)
                if plan["grid"]["width"] <= budget:
                    return plan

        # ---- 第二遍：怎么都塞不下，选「丢得最少」的那个方案 ----
        # 遍历顺序仍是「从松到紧」，用严格大于比较，所以丢得一样多时先出现的
        # （也就是外观改动最小的那一档）胜出。
        best = None
        for index, (compact, div_mult, gap_mult) in enumerate(_DENSITY):
            sections = sections_for(compact)
            if not sections:
                continue
            for rows_wanted, font_scale in candidates:
                if rows_wanted > max_rows_for(font_scale):
                    continue
                if index and rows_wanted == 1 and font_scale == base_font:
                    continue
                shown = self._fit_sections(
                    dc, sections, budget, rows_wanted,
                    _GAP_LABEL * scale * gap_mult, _GAP_UNIT * scale * gap_mult,
                    _DIV_MARGIN * scale * div_mult,
                    self._fonts_for(scale, font_scale),
                )
                if best is not None and len(shown) <= best["placed"]:
                    continue
                best = attempt(compact, div_mult, gap_mult, rows_wanted,
                               font_scale, index, keep=len(shown))

        if best is None:
            # 理论上到不了这儿（字段至少 1 个，总能排下）；真到了就退回最保守的形态
            best = attempt(False, 1.0, 1.0, 1, base_font, 0, keep=1)
        return best

    def _highlight(self, total: int, height: int, origin_x: int, origin_y: int,
                   color: int) -> None:
        """给胶囊上半部叠一层自上而下渐隐的高光（玻璃质感的「反光」）。

        GDI 画渐变要么走 ``msimg32.GradientFill``、要么手工铺几百条矩形，都太绕；
        而长条只有两万来像素、又只在内容变化时重画，直接改 DIB 像素做软过渡最省事。
        这里只写 RGB、不碰 alpha —— alpha 随后由 ``compose_shape_alpha`` 统一补。
        """
        view = self._view
        if view is None or total <= 0 or height <= 0:
            return
        hr, hg, hb = _unpack(color)
        stride = self._w
        band = max(1.0, height * 0.55)
        for row in range(int(band)):
            y = origin_y + row
            if y < 0 or y >= self._h:
                continue
            t = (1.0 - row / band) ** 1.6 * 0.85     # 顶部最强，往下迅速变淡
            if t <= 0.004:
                continue
            inv = 1.0 - t
            base = (y * stride) * 4 + origin_x * 4
            for col in range(total):
                x = origin_x + col
                if x < 0 or x >= stride:
                    continue
                idx = base + col * 4
                # view 是 BGRA：idx=B, idx+1=G, idx+2=R
                view[idx] = int(hb * t + view[idx] * inv)
                view[idx + 1] = int(hg * t + view[idx + 1] * inv)
                view[idx + 2] = int(hr * t + view[idx + 2] * inv)

    def _sample_taskbar(self, x: int, y: int, w: int, h: int):
        """采任务栏底色。取长条左外侧一点，避开长条自身和图标。

        返回 ``(r, g, b)``；采不到返回 ``None``，由调用方决定用什么兜底。
        """
        try:
            screen = user32.GetDC(None)
            try:
                px = max(2, x - 6)
                py = y + h // 2
                value = gdi32.GetPixel(screen, px, py)
            finally:
                user32.ReleaseDC(None, screen)
            if value == 0xFFFFFFFF:  # CLR_INVALID
                return None
            return _unpack(value)
        except (ValueError, OSError):
            return None

    # ------------------------------------------------------------- 刷新

    def invalidate(self) -> None:
        """强制下一帧重画。

        改完质感 / 字号 / 大小 / 显示项之后由 app 调用。这些信息本来就在
        ``_content_key`` 里，理论上会自动失效；显式清一次是为稳妥 —— 免得日后
        有人把某项从 ``_style_key`` 里挪走，表现成「菜单点了长条纹丝不动」。
        """
        self._key = None

    def tick(self, snap) -> None:
        """每秒调一次：位置/尺寸变了就搬，内容变了就重画。"""
        if not self._hwnd:
            # explorer 重启会把任务栏连着我们的子窗口一起带走：环境回来了就重建
            if snap is not None and taskbar.taskbar() is not None:
                self.create(snap)
            if not self._hwnd:
                return
        elif not user32.IsWindow(self._hwnd):
            # 句柄还挂着但窗口已经死了（异常情况），清干净下个 tick 再建
            self._hwnd = None
            self._rect = None
            self._release_buffer()
            return

        if self._should_hide():
            if not self._hidden:
                user32.ShowWindow(self._hwnd, 0)
                self._hover = False
                self._hidden = True
            return

        # 正在拖：位置由鼠标说了算，这一刻不要按锚点重算（否则会和手抢，抖）
        if self._drag is not None:
            return

        target = self._target_rect(snap)
        if target is None:
            if not self._hidden:
                user32.ShowWindow(self._hwnd, 0)
                self._hover = False
                self._hidden = True
            return

        if self._hidden:
            user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
            self._hidden = False
            self._key = None
            self._leave_tracked = False
            # 刚从隐藏恢复，立刻把层级重新声明一次，避免被任务栏压到下面
            # （否则会出现「消失一下、过两秒才冒出来」的错觉）。
            self._ensure_above_siblings(force=True)

        if target != self._rect:
            cl, ct, cr, cb = self._client_rect(target)
            user32.SetWindowPos(
                self._hwnd, None, cl, ct, cr - cl, cb - ct,
                SWP_NOACTIVATE | SWP_NOZORDER | SWP_NOOWNERZORDER,
            )
            self._rect = target
            self._key = None
            debug.log("strip", f"移动到 {target}")

        self._ensure_above_siblings()
        self._render_if_needed(snap)

    def _embed_into_taskbar(self) -> None:
        """把自己 SetParent 成 Shell_TrayWnd 的**子窗口**。

        顶级 TOPMOST 窗口和任务栏抢 z 序必输：explorer 会频繁把任务栏重新抬到
        最前（Win11 的 XAML 任务栏尤其勤快，实测长条隔零点几秒就被压下去一次、
        每 2 秒才抢回来一次，看起来就是「一闪一闪」）。变成任务栏的子窗口后
        永远画在任务栏背景之上，这个争夺根本不存在（TrafficMonitor 同款做法）。
        """
        info = taskbar.taskbar()
        if info is None:
            return
        bar_hwnd, _trect, _dpi = info
        user32.ShowWindow(self._hwnd, 0)  # 改样式 / 换爹之前先藏起来
        style = user32.GetWindowLongPtrW(self._hwnd, GWL_STYLE)
        user32.SetWindowLongPtrW(
            self._hwnd, GWL_STYLE, (style & ~WS_POPUP) | WS_CHILD)
        user32.SetParent(self._hwnd, bar_hwnd)
        self._parent = bar_hwnd

    def _client_rect(self, rect: tuple[int, int, int, int]):
        """屏幕坐标 -> 父窗口客户区坐标（没嵌入时原样返回）。"""
        if not self._parent:
            return rect
        pt = wintypes.POINT(0, 0)
        if not user32.ClientToScreen(self._parent, ctypes.byref(pt)):
            return rect
        l, t, r, b = rect
        return (l - pt.x, t - pt.y, r - pt.x, b - pt.y)

    def _ensure_above_siblings(self, force: bool = False) -> None:
        """维持长条的层级。

        嵌入成功时：偶尔把自己抬到任务栏**子窗口**的最顶层（防 Win11 的
        XAML 内容桥这类兄弟子窗口盖住自己）；没嵌入成功（兜底还是顶级
        窗口）时：维持原来「定期重申 TOPMOST」的逻辑。
        """
        import time

        now = time.time()
        if not force and now - self._topmost_at < 2.0:
            return
        self._topmost_at = now
        if self._parent:
            user32.SetWindowPos(
                self._hwnd, 0, 0, 0, 0, 0,  # HWND_TOP：兄弟窗口里排最前
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
            )
            # 把手再压一层：它也必须是「兄弟里最前」，否则会被长条的胶囊底盖住，
            # 小圆点就看不见了（长条是透明的，所以即使把手在下也还能点，只是不好看）
        else:
            user32.SetWindowPos(
                self._hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER,
            )

    def _should_hide(self) -> bool:
        """只有「任务栏真的看不到了」时才藏：自动隐藏且滑走、或独占全屏把任务栏盖住。

        曾经的逻辑是「前台窗口盖住整屏就藏」，但**最大化**（非独占）的普通窗口
        （浏览器 / 资源管理器）也会盖住整屏，于是点一下别的窗口长条就消失 —— 这正是
        用户说的「不常驻、点别的地方就没了」。所以这里改成：

          * 桌面 / 任务栏自己 → 不藏；
          * 带 ``WS_MAXIMIZE`` 的窗口（系统最大化，任务栏依然可见）→ 不藏；
          * 真正的独占全屏（游戏 / 无边框视频）才盖住任务栏 → 藏。
        """
        info = taskbar.taskbar()
        if info is None:
            return True
        bar_hwnd, trect, _dpi = info
        if not taskbar.is_visible(trect):
            return True

        fg = user32.GetForegroundWindow()
        if not fg or fg == bar_hwnd:
            return False

        buf = ctypes.create_unicode_buffer(64)
        if user32.GetClassNameW(fg, buf, 64):
            cls = buf.value
            # 桌面和任务栏自己永远不算全屏
            if cls in ("Progman", "WorkerW", "Shell_TrayWnd"):
                return False

        # 系统最大化的普通窗口：任务栏照常可见，不该藏。靠 WS_MAXIMIZE 区分，
        # 因为 GetWindowRect 在最大化时返回的是整屏矩形，光看覆盖面积会误判。
        style = user32.GetWindowLongPtrW(fg, GWL_STYLE)
        if style & WS_MAXIMIZE:
            return False

        rect = wintypes.RECT()
        if not user32.GetWindowRect(fg, ctypes.byref(rect)):
            return False
        screen = taskbar.screen_rect()
        covers = (rect.left <= screen[0] and rect.top <= screen[1]
                  and rect.right >= screen[2] and rect.bottom >= screen[3])
        return covers

    def _render_if_needed(self, snap) -> None:
        if snap is not None:
            self._last_snap = snap
        key = self._content_key(snap)
        if key == self._key and self._view is not None:
            return
        if not self._rect:
            return
        left, top, right, bottom = self._rect
        w, h = right - left, bottom - top
        if w <= 0 or h <= 0:
            return

        need_new = (self._mem_dc is None or w != self._w or h != self._h)
        if need_new:
            self._release_buffer()
            screen = user32.GetDC(None)
            try:
                self._mem_dc = gdi32.CreateCompatibleDC(screen)
                self._bmp, self._view = dib_section(self._mem_dc, w, h)
            finally:
                user32.ReleaseDC(None, screen)
            if not self._bmp:
                self._release_buffer()
                return
            self._old_bmp = gdi32.SelectObject(self._mem_dc, self._bmp)
            self._w, self._h = w, h

        _total, _h, pal = self._layout(
            self._mem_dc, self._scale, snap, render=True,
            origin_x=0, origin_y=0, height=h,
        )
        if pal is None:
            return

        compose_shape_alpha(
            self._view, w, h, margin=0,
            # 圆角必须和 _layout 里画描边用的那个一致，否则描边和 alpha 边缘对不齐，
            # 半透明质感下会看到一圈毛边。多行时 _layout 会把它收小。
            radius=self._radius or min(_RADIUS, h / 2.0),
            shape_w=w, shape_h=h, shadow=0,
            # 半透明质感整块压 alpha；线框质感用哨兵色抠出透明底。
            shape_alpha=pal["alpha"], key_rgb=pal["key"],
        )
        # 子窗口的 UpdateLayeredWindow 位置是相对父窗口客户区的（和 SetWindowPos
        # 同一套约定）；没嵌入时 _client_rect 原样返回屏幕坐标。
        cl, ct, _cr, _cb = self._client_rect(self._rect)
        present_layered(self._hwnd, self._mem_dc, w, h, cl, ct)
        self._key = key
        # 把手要用这一帧的配色画小圆点（换质感 / 换深浅时跟着变），顺便对齐位置
        self._pal = pal
