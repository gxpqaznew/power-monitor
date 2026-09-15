"""任务栏长条 —— 把关键读数摊成一条横条，贴在开始按钮左边。

托盘图标只有 16~20px（系统定的，改不了：给一个长条 HICON 进去也会被压成方的），
三个数字挤在那点地方必然看不清。所以这里另开一个**置顶、无边框、能穿透点击的
分层窗口**，直接盖在任务栏那片空白上，用接近两倍的字号显示同样的数据。

放在哪儿：
  * ``start`` 贴着开始按钮左边（默认）。实测开始按钮是独立 HWND（类名 ``"Start"``），
    位置查得到；Win11 居中任务栏时它左边是一大片空地。
  * ``tray``  贴着通知区域左边。开始按钮查不到、或者左边实在放不下时自动退回这里。

几个刻意的取舍：
  * **穿透点击**（``WS_EX_TRANSPARENT``）：长条只负责显示，绝不抢鼠标。否则会把
    任务栏那一片的右键菜单吃掉。
  * **底色默认采样自任务栏**：直接读长条目标位置旁边那一个像素，再往白里调一点点
    当背景。任务栏是亚克力/跟随壁纸的，写死颜色一定不对。用户也可以在托盘菜单里
    把质感换成固定色调（深色 / 浅色 / 强调色卡片）、半透明玻璃、或者只留描边和
    文字的线框 —— 档位目录见 ``stripopts``。
  * 不抢焦点（``WS_EX_NOACTIVATE``）也不进 Alt+Tab（``WS_EX_TOOLWINDOW``）。

外观（质感 / 字号 / 尺寸 / 显示哪些字段）全部来自 ``cfg``，由 ``stripopts`` 统一
解释。改任何一项都会走 ``_style_key()`` → ``_content_key()`` 变化 → 强制重画，
不需要重建窗口。
"""

from __future__ import annotations

import ctypes

from . import debug, stripopts, taskbar
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
    GWL_STYLE,
    HWND_TOPMOST,
    NULL_BRUSH,
    PS_SOLID,
    SIZE,
    SW_SHOWNOACTIVATE,
    SWP_NOACTIVATE,
    SWP_NOMOVE,
    SWP_NOOWNERZORDER,
    SWP_NOSIZE,
    SWP_NOZORDER,
    TRANSPARENT,
    WM_DESTROY,
    WNDCLASSEXW,
    WNDPROC,
    WS_EX_LAYERED,
    WS_MAXIMIZE,
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_EX_TOPMOST,
    WS_EX_TRANSPARENT,
    WS_POPUP,
    WS_CHILD,
    gdi32,
    kernel32,
    user32,
    wintypes,
)

_FONT_FACE = "Microsoft YaHei UI"
_CLASS_NAME = "PowerMonitorTaskbarStrip"

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
# 长条最宽多少（设计基准 48px 任务栏下的像素）。太宽会顶到任务栏中间的任务按钮，
# 所以超了就按顺序从后往前丢字段。字号调大时按比例放宽 —— 字大了本来就需要更多地方。
_MAX_WIDTH = 560.0
# 少于这么多像素可用宽度就整条不显示：硬塞一两个字符进任务栏角落，比不显示更糟。
# 取 108 是为了让「只勾一个字段」的最窄情况（约 130px）仍然显示得出来。
_ABS_MIN_WIDTH = 108.0

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
    """
    if theme == "dark":
        bg = (26, 28, 33)
        return {
            "bg": _colorref(*bg), "alpha": 236, "key": None,
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
            "border": _colorref(*_mix(bg, (255, 255, 255), 0.30)), "border_w": 1,
            "ink": _colorref(0xFF, 0xFF, 0xFF),
            "dim": _colorref(0xD5, 0xE0, 0xFA),
            "unit": _colorref(0xB7, 0xCB, 0xF4),
            "div": _colorref(*_mix(bg, (255, 255, 255), 0.26)),
            "highlight": _colorref(*_mix(bg, (255, 255, 255), 0.22)),
        }
    if theme == "outline":
        # 底色 = 采样到的**真实任务栏色**，同时拿它当抠色键。
        #
        # 这里踩过坑：一开始用醒目的品红当哨兵色，结果每个字都镶一圈紫边 ——
        # 文字是抗锯齿画的，字边那一圈是「文字色 × 底色」的混合像素，它们不等于
        # 品红，抠色抠不掉，于是紫边就留在了画面上（实测混合像素是 AC4A66 /
        # E43181 这种脏紫）。想用「按离底色的距离软抠 + 反解底色」补救，数学上
        # 要求覆盖率估计得准，而覆盖率无法从颜色反推得足够准，仍有残留。
        #
        # 正确做法是让底色**就是背后的真实颜色**：这样字边混出来的像素恰好等于
        # 「文字直接画在任务栏上」应有的颜色，于是「纯底色的像素抠成透明、其余
        # 原样保留不透明」就够了 —— 边缘自动是对的，不需要任何反解。
        # 代价是底色得靠采样（采样不到才退回注册表口径的近似色）。
        if light:
            border = _mix(sample, (0, 0, 0), 0.42)
            div = _mix(sample, (0, 0, 0), 0.22)
        else:
            border = _mix(sample, (255, 255, 255), 0.60)
            div = _mix(sample, (255, 255, 255), 0.30)
        return {
            "bg": _colorref(*sample), "alpha": 255, "key": tuple(sample),
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
        "bg": _colorref(*bg), "alpha": 255, "key": None,
        "border": _colorref(*border), "border_w": 1,
        "ink": _colorref(*ink), "dim": _colorref(*dim), "unit": _colorref(*unit),
        "div": _colorref(*div),
        "highlight": None,
    }


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
        wc.style = 0
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

        ex = (WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
              | WS_EX_TOPMOST | WS_EX_TRANSPARENT)
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
        # 穿透点击，别的消息一概不处理
        return False, 0

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

    def _raw_text_width(self, dc, text: str, font) -> int:
        old = gdi32.SelectObject(dc, font)
        size = SIZE()
        gdi32.GetTextExtentPoint32W(dc, text, len(text), ctypes.byref(size))
        gdi32.SelectObject(dc, old)
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

    def _sections(self, snap) -> list[tuple[str, str, str, str]]:
        """要显示的字段（key, 标签, 数值, 单位）。

        顺序完全由用户在「长条显示内容」里定的顺序决定，宽度不够时从后往前丢 ——
        所以靠后的字段是「有余量才显示」的那些。
        """
        return stripopts.sections(snap, self.cfg)

    def _style_key(self) -> tuple:
        """只跟「长条长什么样」有关的键。

        必须算进 ``_content_key``：改质感 / 字号 / 尺寸 / 显示项都不会改变数值本身，
        只比数值的话 ``_render_if_needed`` 会认为「没变化」直接返回，
        用户点了半天菜单长条纹丝不动。
        """
        return (
            stripopts.theme(self.cfg),
            round(stripopts.font_scale(self.cfg), 3),
            stripopts.size_key(self.cfg),
            tuple(stripopts.enabled_fields(self.cfg)),
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
        return _palette_for(theme, sample, light)

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
        # 胶囊高度由「尺寸」档位决定，字号大了再往上抬一点（大了要更多行高，
        # 否则字会被上下切掉）。默认档 = 0.78，和以前一样。
        height = max(18, int(round(bar_h * stripopts.height_ratio(self.cfg))))
        top = bar_top + (bar_h - height) // 2

        screen = taskbar.screen_rect()
        gap = int(round(16 * scale))

        anchor = None
        if self.cfg.strip_position == "start":
            anchor = taskbar.cluster_left()      # 贴着开始按钮左边
        if anchor is None:
            tray = taskbar.notification_area()
            if tray is None:
                return None
            anchor = tray[0]                     # 兜底：贴通知区域左边

        right = anchor - gap
        # 能用的宽度 = 从屏幕左边留 8px 到锚点左边。任务栏左对齐、或者同时开了
        # 一堆程序把开始按钮挤到很左边时，这里会很小 —— 那种情况就按这个上限
        # 少显示几个字段，而不是整条消失（整条消失用户会以为功能坏了）。
        available = right - (screen[0] + 8)
        if available < int(round(_ABS_MIN_WIDTH * scale)):
            return None
        self._limit = min(int(round(self._width_cap(scale))), available)

        width = self._measure_width(scale, snap)
        if width <= 0:
            return None

        left = right - width
        # 左边放不下（上面已经按 available 缩过，走不到这儿；留个保险）
        if left < screen[0] + 8:
            left, right = screen[0] + 8, screen[0] + 8 + width
        return (int(left), int(top), int(right), int(top + height))

    def _width_cap(self, scale: float) -> float:
        """长条宽度上限（像素）。

        字号调大时内容本来就变宽，上限必须跟着放宽 —— 否则大字号下会有一两个
        字段被从后往前砍掉，用户会觉得「把字调大反而少显示了一项」。小字号不
        收缩上限：内容本来就窄、够不到上限，缩了只是白白少一份余量。
        """
        font_mult = max(1.0, stripopts.font_scale(self.cfg))
        return _MAX_WIDTH * scale * font_mult

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
        # 连「当前功率 + 本次电费」两段都塞不进可用宽度，就别硬塞了
        if self._limit and width > self._limit:
            return 0
        return width

    def _layout(self, dc, scale: float, snap, render: bool,
                origin_x: int = 0, origin_y: int = 0, height: int = 0):
        """量宽 + 画。返回 ``(宽, 高, 配色)``；``render=False`` 时配色为 None。

        ``render=False`` 时只在临时 DC 上量字宽，不画；这样位置和内容用同一套
        布局代码算，不会出现「量的时候 3 段、画的时候 4 段」这种错位。
        顺带返回配色，是为了让调用方（``_render_if_needed``）拿到 ``alpha`` /
        ``key`` 再透传给 ``compose_shape_alpha`` —— 半透明与抠色两种质感都得靠它。
        """
        font_scale = stripopts.font_scale(self.cfg)
        pad_mult = stripopts.size_spec(self.cfg)[1]
        pad_x = _PAD_X * scale * pad_mult
        gap_label = _GAP_LABEL * scale
        gap_unit = _GAP_UNIT * scale
        div_margin = _DIV_MARGIN * scale

        # 字体缓存键必须带上字号倍率：同一个 scale 下换了字号就是另一套字体，
        # 只按 scale 缓存会拿回旧尺寸的字体，现象就是「调了字号没反应」。
        fkey = f"{scale:.3f}x{font_scale:.2f}"
        label_font = self._font(f"lbl{fkey}", _LABEL_SZ * scale * font_scale)
        value_font = self._font(f"val{fkey}", _VALUE_SZ * scale * font_scale, bold=True)
        unit_font = self._font(f"unt{fkey}", _UNIT_SZ * scale * font_scale)

        sections = self._sections(snap)
        limit = self._limit or int(round(self._width_cap(scale)))
        while len(sections) > 2:
            total = self._sections_width(
                dc, sections, pad_x, gap_label, gap_unit, div_margin,
                label_font, value_font, unit_font,
            )
            if total <= limit:
                break
            sections.pop()

        total = self._sections_width(
            dc, sections, pad_x, gap_label, gap_unit, div_margin,
            label_font, value_font, unit_font,
        )
        if not render:
            return total, 0, None

        # ---- 配色：由「质感」档位决定，固定色调的档位不需要采样任务栏 ----
        pal = self._resolve_palette(total, height, origin_x, origin_y)
        radius = height / 2.0
        self._fill(dc, origin_x, origin_y, total, height, pal["bg"])
        # 高光必须在画文字**之前**铺，否则会把刚画上去的字一起洗白。
        if pal["highlight"] is not None:
            self._highlight(total, height, origin_x, origin_y, pal["highlight"])
        # 描边用 RoundRect 的**空心**画笔勾。RoundRect 会用当前画刷填充内部，
        # 如果这里选实心刷会把刚铺好的底色整块盖掉，所以必须用 NULL_BRUSH。
        old_brush = gdi32.SelectObject(dc, gdi32.GetStockObject(NULL_BRUSH))
        old_pen = gdi32.SelectObject(dc, self._pen(pal["border"], pal["border_w"]))
        gdi32.RoundRect(dc, origin_x, origin_y, origin_x + total, origin_y + height,
                        int(radius * 2), int(radius * 2))
        gdi32.SelectObject(dc, old_pen)
        gdi32.SelectObject(dc, old_brush)

        # ---- 各段 ----
        x = origin_x + pad_x
        mid = origin_y + height / 2.0
        for i, (_key, label, value, unit) in enumerate(sections):
            w_label = self._text_width(dc, label, label_font)
            w_value = self._text_width(dc, value, value_font)
            w_unit = self._text_width(dc, unit, unit_font) if unit else 0

            self._text(dc, label, x, origin_y, w_label + 2, height, label_font,
                       pal["dim"])
            x += w_label + gap_label
            self._text(dc, value, x, origin_y, w_value + 2, height, value_font,
                       pal["ink"])
            x += w_value
            if unit:
                x += gap_unit
                self._text(dc, unit, x, origin_y, w_unit + 2, height, unit_font,
                           pal["unit"])
                x += w_unit
            if i < len(sections) - 1:
                x += div_margin
                div_h = height * 0.46
                self._fill(dc, x, mid - div_h / 2, max(1, scale), div_h, pal["div"])
                x += max(1, scale) + div_margin
        return total, height, pal

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

    def _sections_width(self, dc, sections, pad_x, gap_label, gap_unit, div_margin,
                        label_font, value_font, unit_font) -> int:
        """量出这一组字段排下来要多宽。和 ``_layout`` 里的绘制用同一套间距常量。

        dc 必须由调用方传进来：量位置（临时 DC）和真画（内存 DC）用的是两个
        不同的设备上下文，之前想从 self 上取一个「当前 DC」，但在量位置那一步
        还没建缓冲区，取到 None 会直接崩。
        """
        total = pad_x * 2
        # sections 的元素是 (key, 标签, 数值, 单位) 四元组 —— key 只有菜单 /
        # 配置那边用得到，量宽和绘制都只看后三个。
        for i, (_key, label, value, unit) in enumerate(sections):
            total += self._text_width(dc, label, label_font)
            total += gap_label
            total += self._text_width(dc, value, value_font)
            if unit:
                total += gap_unit + self._text_width(dc, unit, unit_font)
            if i < len(sections) - 1:
                total += div_margin * 2 + 1
        return int(round(total))

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
                self._hidden = True
            return

        target = self._target_rect(snap)
        if target is None:
            if not self._hidden:
                user32.ShowWindow(self._hwnd, 0)
                self._hidden = True
            return

        if self._hidden:
            user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
            self._hidden = False
            self._key = None
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
            self._view, w, h, margin=0, radius=min(_RADIUS, h / 2.0),
            shape_w=w, shape_h=h, shadow=0,
            # 半透明质感整块压 alpha；线框质感用哨兵色抠出透明底。
            shape_alpha=pal["alpha"], key_rgb=pal["key"],
        )
        # 子窗口的 UpdateLayeredWindow 位置是相对父窗口客户区的（和 SetWindowPos
        # 同一套约定）；没嵌入时 _client_rect 原样返回屏幕坐标。
        cl, ct, _cr, _cb = self._client_rect(self._rect)
        present_layered(self._hwnd, self._mem_dc, w, h, cl, ct)
        self._key = key
