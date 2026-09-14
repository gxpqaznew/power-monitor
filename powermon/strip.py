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
  * **底色采样自任务栏**：直接读长条目标位置旁边那一个像素，再往白里调一点点当
    背景。任务栏是亚克力/跟随壁纸的，写死颜色一定不对。
  * 不抢焦点（``WS_EX_NOACTIVATE``）也不进 Alt+Tab（``WS_EX_TOOLWINDOW``）。
"""

from __future__ import annotations

import ctypes

from . import debug, taskbar
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
_HEIGHT_RATIO = 0.78      # 长条高 / 任务栏高
_PAD_X = 15.0
_GAP_LABEL = 6.0
_GAP_UNIT = 3.0
_DIV_MARGIN = 13.0
_RADIUS = 999.0           # 足够大就会被夹成胶囊（= 高的一半）
_LABEL_SZ = 13.0
_VALUE_SZ = 20.0
_UNIT_SZ = 12.0
# 长条最宽多少（设计基准 48px 任务栏下的像素）。太宽会顶到任务栏中间的任务按钮，
# 所以超了就按重要性从后往前丢字段。
_MAX_WIDTH = 560.0
# 最少要留出多少地方才值得放（放不下就整条不显示，总比糊在开始按钮上强）
_MIN_WIDTH = 210.0

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

    def _text_width(self, dc, text: str, font) -> int:
        old = gdi32.SelectObject(dc, font)
        size = SIZE()
        gdi32.GetTextExtentPoint32W(dc, text, len(text), ctypes.byref(size))
        gdi32.SelectObject(dc, old)
        return size.cx

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

    def _sections(self, snap) -> list[tuple[str, str, str]]:
        """按重要性排序的字段。宽度不够时从后往前丢。"""
        wh = snap.session_wh
        energy = (f"{wh / 1000:.2f}", "kWh") if wh >= 1000 else (f"{wh:.0f}", "Wh")
        return [
            ("当前", f"{snap.current_w:.0f}", "W"),
            ("本次电费", f"{self.cfg.currency}{snap.session_cost:.2f}", ""),
            ("本次电量", energy[0], energy[1]),
            ("今日", f"{snap.today_wh / 1000:.2f}", "kWh"),
        ]

    def _content_key(self, snap) -> tuple:
        return (
            int(snap.current_w), f"{snap.session_cost:.2f}",
            int(snap.session_wh), round(snap.today_wh / 1000, 2),
        )

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
        height = max(18, int(round(bar_h * _HEIGHT_RATIO)))
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
        if available < int(round(_MIN_WIDTH * scale)):
            return None
        self._limit = min(int(round(_MAX_WIDTH * scale)), available)

        width = self._measure_width(scale, snap)
        if width <= 0:
            return None

        left = right - width
        # 左边放不下（上面已经按 available 缩过，走不到这儿；留个保险）
        if left < screen[0] + 8:
            left, right = screen[0] + 8, screen[0] + 8 + width
        return (int(left), int(top), int(right), int(top + height))

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
        """量宽 + 画。返回 (宽, 高)。

        ``render=False`` 时只在临时 DC 上量字宽，不画；这样位置和内容用同一套
        布局代码算，不会出现「量的时候 3 段、画的时候 4 段」这种错位。
        """
        pad_x = _PAD_X * scale
        gap_label = _GAP_LABEL * scale
        gap_unit = _GAP_UNIT * scale
        div_margin = _DIV_MARGIN * scale

        label_font = self._font(f"lbl{scale:.3f}", _LABEL_SZ * scale)
        value_font = self._font(f"val{scale:.3f}", _VALUE_SZ * scale, bold=True)
        unit_font = self._font(f"unt{scale:.3f}", _UNIT_SZ * scale)

        sections = self._sections(snap)
        limit = self._limit or int(round(_MAX_WIDTH * scale))
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
            return total, 0

        # ---- 底色：采任务栏那一个像素 + 主题色 ----
        # 采样要按长条**在屏幕上的真实位置**取（origin 是画布内偏移，不是屏幕坐标）
        base_x, base_y = (self._rect[0], self._rect[1]) if self._rect else (0, 0)
        sample = self._sample_taskbar(base_x + origin_x, base_y + origin_y,
                                      total, height)
        if sample is None:
            # 采不到（窗口还没显示、像素被遮挡）才退回注册表口径
            light = taskbar.uses_light_theme()
            sample = (233, 237, 243) if light else (32, 32, 32)
        else:
            # 明暗直接看**实际像素**，不看注册表：本机实测 SystemUsesLightTheme=1
            # 时任务栏却因为「自动」主题 + 深色壁纸呈深色，读注册表会判断反，
            # 结果就是在深色任务栏上画一条浅灰底黑字，非常突兀。
            light = _luma(sample) >= 128
        if light:
            bg = _mix(sample, (255, 255, 255), 0.18)
            border = _mix(sample, (0, 0, 0), 0.13)
            ink = _colorref(0x1A, 0x1E, 0x24)
            dim = _colorref(0x5E, 0x66, 0x74)
            unit_c = _colorref(0x77, 0x80, 0x8E)
            div = _mix(sample, (0, 0, 0), 0.16)
        else:
            bg = _mix(sample, (255, 255, 255), 0.20)
            border = _mix(sample, (255, 255, 255), 0.26)
            ink = _colorref(0xF2, 0xF5, 0xF9)
            dim = _colorref(0xA9, 0xB2, 0xC0)
            unit_c = _colorref(0x8E, 0x98, 0xA8)
            div = _mix(sample, (255, 255, 255), 0.22)

        radius = height / 2.0
        self._fill(dc, origin_x, origin_y, total, height, _colorref(*bg))
        # 描边用 RoundRect 的**空心**画笔勾。RoundRect 会用当前画刷填充内部，
        # 如果这里选实心刷会把刚铺好的底色整块盖掉，所以必须用 NULL_BRUSH。
        old_brush = gdi32.SelectObject(dc, gdi32.GetStockObject(NULL_BRUSH))
        old_pen = gdi32.SelectObject(dc, self._pen(_colorref(*border), 1))
        gdi32.RoundRect(dc, origin_x, origin_y, origin_x + total, origin_y + height,
                        int(radius * 2), int(radius * 2))
        gdi32.SelectObject(dc, old_pen)
        gdi32.SelectObject(dc, old_brush)

        # ---- 各段 ----
        x = origin_x + pad_x
        mid = origin_y + height / 2.0
        for i, (label, value, unit) in enumerate(sections):
            w_label = self._text_width(dc, label, label_font)
            w_value = self._text_width(dc, value, value_font)
            w_unit = self._text_width(dc, unit, unit_font) if unit else 0

            self._text(dc, label, x, origin_y, w_label + 2, height, label_font, dim)
            x += w_label + gap_label
            self._text(dc, value, x, origin_y, w_value + 2, height, value_font, ink)
            x += w_value
            if unit:
                x += gap_unit
                self._text(dc, unit, x, origin_y, w_unit + 2, height, unit_font,
                           unit_c)
                x += w_unit
            if i < len(sections) - 1:
                x += div_margin
                div_h = height * 0.46
                self._fill(dc, x, mid - div_h / 2, max(1, scale), div_h, _colorref(*div))
                x += max(1, scale) + div_margin
        return total, height

    def _sections_width(self, dc, sections, pad_x, gap_label, gap_unit, div_margin,
                        label_font, value_font, unit_font) -> int:
        """量出这一组字段排下来要多宽。和 ``_layout`` 里的绘制用同一套间距常量。

        dc 必须由调用方传进来：量位置（临时 DC）和真画（内存 DC）用的是两个
        不同的设备上下文，之前想从 self 上取一个「当前 DC」，但在量位置那一步
        还没建缓冲区，取到 None 会直接崩。
        """
        total = pad_x * 2
        for i, (label, value, unit) in enumerate(sections):
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

        self._layout(self._mem_dc, self._scale, snap, render=True,
                     origin_x=0, origin_y=0, height=h)

        compose_shape_alpha(
            self._view, w, h, margin=0, radius=min(_RADIUS, h / 2.0),
            shape_w=w, shape_h=h, shadow=0,
        )
        # 子窗口的 UpdateLayeredWindow 位置是相对父窗口客户区的（和 SetWindowPos
        # 同一套约定）；没嵌入时 _client_rect 原样返回屏幕坐标。
        cl, ct, _cr, _cb = self._client_rect(self._rect)
        present_layered(self._hwnd, self._mem_dc, w, h, cl, ct)
        self._key = key
