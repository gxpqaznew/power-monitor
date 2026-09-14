"""详情面板 —— 纯 GDI 自绘窗口。

用 CreateWindowEx + WM_PAINT 手工绘制，配一块内存位图做双缓冲。
比起引入 Qt，这样做常驻内存只有几十兆，代价是所有排版都得自己算。

视觉上刻意做了几件事，都是为了让「一眼看上去」不像个内部工具：
  * 一整张**圆角卡片**：窗口交给系统切圆角（Win11 原生 8px，带抗锯齿），
    再把标题栏/边框/标题字色全部对齐到面板自己的配色 —— 否则会出现
    「系统灰标题栏里嵌了一张蓝色卡片」的割裂感（见 _apply_dwm_style）；
  * 单一强调色（蓝）只用于「钱」，其余数字一律用近黑，层级靠字重和字号；
  * 卡片带 3 层偏移投影 —— 内存 DC 没有 alpha，只能用叠实色假装柔影；
  * 所有色块都用逐行实色插值做竖向渐变，纯 GDI 也能有光感；
  * 卡片间距按窗口富余空间自动摊开，底部不留一大块死白。

为什么不用自绘分层窗口做「更大的圆角 + 柔影」：实测把 565×845 的面板
过一遍逐像素 alpha 合成要 68ms（不带阴影）/ 374ms（带阴影），而面板每秒
都会重画一次，等于白白吃掉近一个核。系统原生圆角零成本、还自带抗锯齿。
"""

from __future__ import annotations

import ctypes
import time

from . import debug
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
    HWND_NOTOPMOST,
    HWND_TOPMOST,
    PAINTSTRUCT,
    PS_SOLID,
    SIZE,
    SM_CXSCREEN,
    SM_CYSCREEN,
    SWP_NOMOVE,
    SWP_NOSIZE,
    SWP_SHOWWINDOW,
    TRANSPARENT,
    WINDING,
    WM_CLOSE,
    WM_DESTROY,
    WM_ERASEBKGND,
    WM_PAINT,
    WNDCLASSEXW,
    WNDPROC,
    WS_CAPTION,
    WS_MINIMIZEBOX,
    WS_OVERLAPPED,
    WS_SYSMENU,
    gdi32,
    kernel32,
    user32,
    wintypes,
)

CURVE_WINDOW_SECONDS = 30 * 60
_FONT_FACE = "Microsoft YaHei UI"
_CLASS_NAME = "PowerMonitorPanelWnd"
_SRCCOPY = 0x00CC0020
_LOGPIXELSX = 88
_SPI_GETWORKAREA = 0x0030
_NULL_PEN = 8

# DWM 窗口属性（Windows 11）。旧系统上调用会返回错误码，忽略即可。
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWA_BORDER_COLOR = 34
_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36
_DWMWCP_ROUND = 2

CARD_GAP = 13      # 卡片最小间距；富余空间会在这个基础上再摊开
TOP_PAD = 16
BOTTOM_PAD = 14


def rgb(r: int, g: int, b: int) -> int:
    """COLORREF 是 0x00BBGGRR。"""
    return (b << 16) | (g << 8) | r


# --------------------------------------------------------------------- 配色
# 一个强调色 + 一套中性灰阶。中性色全部带一点点蓝，比纯灰显得干净。
BG = rgb(0xF4, 0xF6, 0xF9)
CARD = rgb(0xFF, 0xFF, 0xFF)
INK = rgb(0x12, 0x17, 0x22)
INK_SOFT = rgb(0x4B, 0x55, 0x66)
INK_DIM = rgb(0x76, 0x81, 0x95)
INK_FAINT = rgb(0x9E, 0xA8, 0xB8)
LINE = rgb(0xE7, 0xEB, 0xF1)
LINE_SOFT = rgb(0xF0, 0xF3, 0xF8)
TRACK = rgb(0xE9, 0xED, 0xF3)
CHIP_BG = rgb(0xF1, 0xF4, 0xF9)

# 强调色只给「电费」用：一屏里只有一个蓝色数字，视线自然会落上去
ACCENT = rgb(0x1E, 0x5C, 0xE0)
ACCENT_INK = rgb(0x1A, 0x4F, 0xC6)
ACCENT_DEEP = rgb(0x17, 0x45, 0xB0)
ACCENT_SOFT = rgb(0xE7, 0xEE, 0xFE)

# 顶部电费卡：极淡的蓝底，把它从其余白卡里托出来
HERO_TOP = rgb(0xF5, 0xF9, 0xFF)
HERO_BOT = rgb(0xE9, 0xF0, 0xFE)
HERO_LINE = rgb(0xD8, 0xE3, 0xFA)

# 曲线下方的渐变面积：上深下浅，看起来像有光源
AREA_TOP = rgb(0xC9, 0xDC, 0xFB)
AREA_BOTTOM = rgb(0xF2, 0xF6, 0xFE)

# 功率构成：琥珀 / 青 / 中性灰。刻意避开蓝色，免得和「钱」的强调色打架
CPU_COLOR = rgb(0xE8, 0x9B, 0x1C)
GPU_COLOR = rgb(0x0E, 0x9E, 0x92)
OTHER_COLOR = rgb(0xB3, 0xBD, 0xCB)
WARN = rgb(0xB4, 0x53, 0x09)

# 投影：由深到浅三层，越远越淡
SHADOW = (rgb(0xE3, 0xE7, 0xEE), rgb(0xEB, 0xEE, 0xF4), rgb(0xF2, 0xF4, 0xF9))
HERO_SHADOW = (rgb(0xDD, 0xE4, 0xF6), rgb(0xE8, 0xEE, 0xFA), rgb(0xF1, 0xF5, 0xFD))


def fmt_duration(seconds: float) -> str:
    seconds = max(0.0, seconds)
    total_minutes = int(seconds // 60)
    days, rem = divmod(total_minutes, 1440)
    hours, minutes = divmod(rem, 60)
    if days:
        return f"{days} 天 {hours} 小时 {minutes} 分"
    if hours:
        return f"{hours} 小时 {minutes} 分"
    return f"{minutes} 分"


def fmt_energy(wh: float) -> tuple[str, str]:
    if wh >= 1000:
        return f"{wh / 1000:.2f}", "kWh"
    if wh < 10:
        # 刚启动时累计只有零点几瓦时，取整会一直显示 0
        return f"{wh:.1f}", "Wh"
    return f"{wh:.0f}", "Wh"


def _clock(ts: float) -> str:
    try:
        return time.strftime("%H:%M", time.localtime(ts))
    except (ValueError, OSError, OverflowError):
        return "--:--"


_panels: dict[int, "Panel"] = {}
_panel_proc_ref: WNDPROC | None = None


@WNDPROC
def _panel_proc(hwnd, msg, wparam, lparam):
    panel = _panels.get(hwnd)
    if panel is not None:
        try:
            handled, result = panel._on_message(msg, wparam, lparam)
            if handled:
                return result
        except Exception:  # noqa: BLE001 - 回调里不能让异常逃逸
            pass
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


class Panel:
    """常驻单例窗口，关闭即隐藏。"""

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self._hwnd = None
        self._fonts: dict[str, int] = {}
        self._brushes: dict[int, int] = {}
        self._pens: dict[tuple[int, int], int] = {}
        self._snap = None
        self._curve: list[tuple[float, float]] = []
        self._buffer_dc = None
        self._buffer_bmp = None
        self._buffer_old = None
        self._buffer_w = 0
        self._buffer_h = 0
        self._clip_region = None
        # 没有通知区域时（精简系统 / 部分远程会话）退化成普通窗口，关窗即退出
        self.quit_on_close = False
        self.scale = 1.0
        self.client_w = 452
        # 高度按最坏情况留：电价卡可能多出「来源待核对」一行。卡片间距会把
        # 富余空间吃掉，所以这里宽一点不会在底部留死白。
        self.client_h = 676

    # ------------------------------------------------------------- 尺寸

    def s(self, px: float) -> int:
        return int(round(px * self.scale))

    def _detect_dpi(self) -> None:
        screen = user32.GetDC(None)
        dpi = gdi32.GetDeviceCaps(screen, _LOGPIXELSX) or 96
        user32.ReleaseDC(None, screen)
        self.scale = max(1.0, dpi / 96.0)

    # ------------------------------------------------------------- 创建

    def create(self) -> bool:
        global _panel_proc_ref
        _panel_proc_ref = _panel_proc

        self._detect_dpi()

        hinstance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = _panel_proc
        wc.hInstance = hinstance
        wc.lpszClassName = _CLASS_NAME
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            if ctypes.get_last_error() != 1410:  # 已注册过无所谓
                return False

        style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX
        rect = wintypes.RECT(0, 0, self.s(self.client_w), self.s(self.client_h))
        user32.AdjustWindowRectEx(ctypes.byref(rect), style, False, 0)
        win_w = rect.right - rect.left
        win_h = rect.bottom - rect.top

        x, y = self._corner_position(win_w, win_h)
        self._hwnd = user32.CreateWindowExW(
            0, _CLASS_NAME, "开机能耗统计", style,
            x, y, win_w, win_h, None, None, hinstance, None,
        )
        if not self._hwnd:
            return False

        _panels[self._hwnd] = self
        self._apply_dwm_style()
        return True

    def _apply_dwm_style(self) -> None:
        """把整窗调成「一张圆角卡片」。

        Win11 本来就给普通窗口切 8px 圆角（带抗锯齿，不用自己画），但默认的
        标题栏是系统灰、外面还有一圈亮边框，看着像「系统窗口里嵌了一张卡」。
        这里把标题栏底色、边框色、标题字色都改成面板自己的配色，圆角和整窗
        就融为一体了。

        旧系统（Win10 及更早）上这些属性不存在，DwmSetWindowAttribute 会返回
        错误码，直接忽略 —— 圆角退化成直角，别的都不受影响。
        """
        try:
            dwm = ctypes.windll.dwmapi
        except (AttributeError, OSError):
            return
        vals = (
            (_DWMWA_WINDOW_CORNER_PREFERENCE, _DWMWCP_ROUND),
            (_DWMWA_CAPTION_COLOR, BG),
            (_DWMWA_TEXT_COLOR, INK),
            (_DWMWA_BORDER_COLOR, LINE),
        )
        for attr, value in vals:
            try:
                data = ctypes.c_uint(value)
                dwm.DwmSetWindowAttribute(
                    wintypes.HWND(self._hwnd), ctypes.c_uint(attr),
                    ctypes.byref(data), ctypes.sizeof(data),
                )
            except (AttributeError, OSError, ValueError):
                return

    def _corner_position(self, win_w: int, win_h: int) -> tuple[int, int]:
        """贴到工作区右下角，靠近任务栏。"""
        work = wintypes.RECT()
        got = user32.SystemParametersInfoW(
            _SPI_GETWORKAREA, 0, ctypes.byref(work), 0
        )
        if not got:
            right = user32.GetSystemMetrics(SM_CXSCREEN)
            bottom = user32.GetSystemMetrics(SM_CYSCREEN)
            left = top = 0
        else:
            right, bottom, left, top = work.right, work.bottom, work.left, work.top
        margin = self.s(16)
        return right - win_w - margin, bottom - win_h - margin

    def destroy(self) -> None:
        if self._hwnd:
            _panels.pop(self._hwnd, None)
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

    # ------------------------------------------------------------- 显隐

    def show_panel(self) -> None:
        if not self._hwnd:
            return
        self.refresh()
        user32.ShowWindow(self._hwnd, 5)  # SW_SHOW
        # 直接 SetForegroundWindow 常常被系统的「前台锁」拒绝（尤其是由别的东西
        # 拉起来的进程），窗口就压在别的窗口后面。通用做法是先临时置顶、再取消
        # 置顶，这一来一回足以把它硬提到最前。
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
        user32.SetWindowPos(self._hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
        user32.SetWindowPos(self._hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
        user32.SetForegroundWindow(self._hwnd)

    def hide(self) -> None:
        if self._hwnd:
            user32.ShowWindow(self._hwnd, 0)  # SW_HIDE

    def toggle(self) -> None:
        if self.is_visible:
            self.hide()
        else:
            self.show_panel()

    @property
    def is_visible(self) -> bool:
        return bool(self._hwnd) and bool(user32.IsWindowVisible(self._hwnd))

    # ------------------------------------------------------------- 数据

    def set_data(self, snapshot, curve: list[tuple[float, float]]) -> None:
        self._snap = snapshot
        self._curve = curve
        if self.is_visible and self._hwnd:
            user32.InvalidateRect(self._hwnd, None, False)

    def refresh(self) -> None:
        if self._hwnd:
            user32.InvalidateRect(self._hwnd, None, False)

    # ------------------------------------------------------------- 消息

    def _on_message(self, msg, wparam, lparam):
        if msg == WM_PAINT:
            self._paint()
            return True, 0
        if msg == WM_ERASEBKGND:
            return True, 1  # 双缓冲自己擦，避免闪烁
        if msg == WM_CLOSE:
            self.hide()
            return True, 0
        if msg == WM_DESTROY:
            _panels.pop(self._hwnd, None)
            self._hwnd = None
            return True, 0
        return False, 0

    # ------------------------------------------------------------- 资源

    def _font(self, key: str, size: int, bold: bool = False):
        cached = self._fonts.get(key)
        if cached:
            return cached
        font = gdi32.CreateFontW(
            -self.s(size), 0, 0, 0, FW_BOLD if bold else FW_NORMAL,
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

    def _ensure_buffer(self, hdc) -> None:
        rect = wintypes.RECT()
        user32.GetClientRect(self._hwnd, ctypes.byref(rect))
        w, h = rect.right, rect.bottom
        if self._buffer_dc and self._buffer_w == w and self._buffer_h == h:
            return
        self._release_buffer()
        self._buffer_dc = gdi32.CreateCompatibleDC(hdc)
        self._buffer_bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        self._buffer_old = gdi32.SelectObject(self._buffer_dc, self._buffer_bmp)
        self._buffer_w, self._buffer_h = w, h

    def _release_buffer(self) -> None:
        if self._buffer_dc:
            if self._buffer_old:
                gdi32.SelectObject(self._buffer_dc, self._buffer_old)
            if self._buffer_bmp:
                gdi32.DeleteObject(self._buffer_bmp)
            gdi32.DeleteDC(self._buffer_dc)
        self._buffer_dc = None
        self._buffer_bmp = None
        self._buffer_old = None

    def _paint(self) -> None:
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(self._hwnd, ctypes.byref(ps))
        try:
            self._ensure_buffer(hdc)
            if not self._buffer_dc:
                return
            self._draw(self._buffer_dc)
            rect = wintypes.RECT()
            user32.GetClientRect(self._hwnd, ctypes.byref(rect))
            gdi32.BitBlt(
                hdc, 0, 0, rect.right, rect.bottom,
                self._buffer_dc, 0, 0, _SRCCOPY,
            )
        finally:
            user32.EndPaint(self._hwnd, ctypes.byref(ps))

    # --- 图元 ---

    def _fill(self, dc, x, y, w, h, color) -> None:
        rect = wintypes.RECT(int(x), int(y), int(x + w), int(y + h))
        user32.FillRect(dc, ctypes.byref(rect), self._brush(color))

    def _vgradient(self, dc, x, y, w, h, top, bottom) -> None:
        """逐行实色插值。内存 DC 没有 alpha，渐变只能这么硬算，但很便宜。"""
        x, y, w, h = int(x), int(y), int(w), int(h)
        if w <= 0 or h <= 0:
            return
        tr, tg, tb = top & 0xFF, (top >> 8) & 0xFF, (top >> 16) & 0xFF
        br, bg, bb = bottom & 0xFF, (bottom >> 8) & 0xFF, (bottom >> 16) & 0xFF
        last = max(1, h - 1)
        for i in range(h):
            t = i / last
            color = rgb(
                round(tr + (br - tr) * t),
                round(tg + (bg - tg) * t),
                round(tb + (bb - tb) * t),
            )
            # 渐变的每一行颜色都不同，缓存只会把缓存撑爆，建了就用掉
            brush = gdi32.CreateSolidBrush(color)
            rect = wintypes.RECT(x, y + i, x + w, y + i + 1)
            user32.FillRect(dc, ctypes.byref(rect), brush)
            gdi32.DeleteObject(brush)

    def _round_rect(self, dc, x, y, w, h, radius, fill, border=None) -> None:
        if border is not None:
            pen = self._pen(border, 1)
        else:
            pen = gdi32.GetStockObject(_NULL_PEN)
        old_brush = gdi32.SelectObject(dc, self._brush(fill))
        old_pen = gdi32.SelectObject(dc, pen)
        d = max(0, int(radius * 2))
        gdi32.RoundRect(dc, int(x), int(y), int(x + w), int(y + h), d, d)
        gdi32.SelectObject(dc, old_pen)
        gdi32.SelectObject(dc, old_brush)

    def _line(self, dc, x1, y1, x2, y2, color, width=1) -> None:
        old = gdi32.SelectObject(dc, self._pen(color, width))
        gdi32.MoveToEx(dc, int(x1), int(y1), None)
        gdi32.LineTo(dc, int(x2), int(y2))
        gdi32.SelectObject(dc, old)

    def _ellipse(self, dc, x, y, w, h, color) -> None:
        old_brush = gdi32.SelectObject(dc, self._brush(color))
        old_pen = gdi32.SelectObject(dc, gdi32.GetStockObject(_NULL_PEN))
        gdi32.Ellipse(dc, int(x), int(y), int(x + w), int(y + h))
        gdi32.SelectObject(dc, old_pen)
        gdi32.SelectObject(dc, old_brush)

    def _polygon(self, dc, points, color) -> None:
        if len(points) < 3:
            return
        old_brush = gdi32.SelectObject(dc, self._brush(color))
        old_pen = gdi32.SelectObject(dc, gdi32.GetStockObject(_NULL_PEN))
        array = (wintypes.POINT * len(points))(*points)
        gdi32.Polygon(dc, array, len(points))
        gdi32.SelectObject(dc, old_pen)
        gdi32.SelectObject(dc, old_brush)

    def _polyline(self, dc, points, color, width=1) -> None:
        if len(points) < 2:
            return
        old = gdi32.SelectObject(dc, self._pen(color, width))
        array = (wintypes.POINT * len(points))(*points)
        gdi32.Polyline(dc, array, len(points))
        gdi32.SelectObject(dc, old)

    def _clip_rounded(self, dc, x, y, w, h, radius) -> None:
        """用圆角区域裁剪，让进度条 / 堆叠条两端是圆的。"""
        rgn = gdi32.CreateRoundRectRgn(
            int(x), int(y), int(x + w + 1), int(y + h + 1),
            max(0, int(radius * 2)), max(0, int(radius * 2)),
        )
        self._clip_region = rgn
        gdi32.SelectClipRgn(dc, rgn)

    def _clip_polygon(self, dc, points) -> None:
        """按折线形状裁剪 —— 曲线下方的渐变面积要贴着线走。"""
        if len(points) < 3:
            return
        array = (wintypes.POINT * len(points))(*points)
        rgn = gdi32.CreatePolygonRgn(array, len(points), WINDING)
        self._clip_region = rgn
        gdi32.SelectClipRgn(dc, rgn)

    def _pop_clip(self, dc) -> None:
        gdi32.SelectClipRgn(dc, None)
        if self._clip_region:
            gdi32.DeleteObject(self._clip_region)
            self._clip_region = None

    def _text_width(self, dc, text: str, font, extra: int = 0) -> int:
        old = gdi32.SelectObject(dc, font)
        prev = 0
        if extra:
            prev = gdi32.SetTextCharacterExtra(dc, int(extra))
        size = SIZE()
        gdi32.GetTextExtentPoint32W(dc, text, len(text), ctypes.byref(size))
        if extra:
            gdi32.SetTextCharacterExtra(dc, prev)
        gdi32.SelectObject(dc, old)
        return size.cx

    def _ellipsize(self, dc, text: str, font, max_w: int) -> str:
        """超宽就截断加省略号 —— 宁可少显示一点，也不要被硬裁掉半个字。"""
        if max_w <= 0:
            return ""
        if self._text_width(dc, text, font) <= max_w:
            return text
        cut = text
        while cut and self._text_width(dc, cut + "…", font) > max_w:
            cut = cut[:-1]
        return (cut + "…") if cut else ""

    def _text(self, dc, text, x, y, w, h, font, color, align=DT_LEFT, extra=0) -> None:
        old = gdi32.SelectObject(dc, font)
        gdi32.SetTextColor(dc, color)
        gdi32.SetBkMode(dc, TRANSPARENT)
        prev = 0
        if extra:
            # 小号中文标签拉开一丝字距会显得透气；用完必须还原，
            # 否则后面所有文字都跟着被撑开。
            prev = gdi32.SetTextCharacterExtra(dc, int(extra))
        rect = wintypes.RECT(int(x), int(y), int(x + w), int(y + h))
        user32.DrawTextW(
            dc, text, len(text), ctypes.byref(rect),
            align | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX,
        )
        if extra:
            gdi32.SetTextCharacterExtra(dc, prev)
        gdi32.SelectObject(dc, old)

    # --- 复合图元 ---

    def _card(self, dc, x, y, w, h, fill=CARD, border=LINE,
              grad=None, shadow=None) -> None:
        """带柔影的卡片。

        内存 DC 不支持 alpha，真正的柔影做不了，就用三层逐级变淡的偏移圆角
        矩形假装 —— 只看下方露出的那几像素，观感和真阴影差别不大。
        """
        r = self.s(11)
        shadow = shadow or SHADOW
        off = max(1, self.s(1))
        for k, color in ((3, shadow[0]), (2, shadow[1]), (1, shadow[2])):
            self._round_rect(dc, x, y + off * k, w, h, r, color)
        if grad is None:
            self._round_rect(dc, x, y, w, h, r, fill, border)
        else:
            self._round_rect(dc, x, y, w, h, r, border)
            self._clip_rounded(dc, x + 1, y + 1, w - 2, h - 2, r - 1)
            self._vgradient(dc, x + 1, y + 1, w - 2, h - 2, grad[0], grad[1])
            self._pop_clip(dc)

    def _chip(self, dc, x, y, text, font, fg, bg, h=None, width=None, pad=None) -> int:
        """胶囊小标签。返回实际占用的宽度。"""
        h = self.s(20) if h is None else h
        pad = self.s(9) if pad is None else pad
        w = self._text_width(dc, text, font) + pad * 2 if width is None else width
        self._round_rect(dc, x, y, w, h, h / 2.0, bg)
        self._text(dc, text, x, y, w, h, font, fg, DT_CENTER)
        return int(w)

    # --- 版面 ---

    def _card_heights(self, snap) -> tuple[int, int, int, int]:
        """四张卡片的高度。量一遍再画，才能把富余空间摊到间距上。"""
        hero = self.s(96)
        power = self.s(120)
        curve = self.s(13) + self.s(15) + self.s(8) + self.s(102) + self.s(12)
        price = self.s(76) + (self.s(18) if self.cfg.tariff_verify else 0)
        return hero, power, curve, price

    def _footer_height(self, snap) -> int:
        lines = 2  # 数据源 + 操作提示
        if snap.estimated_wh > snap.session_wh * 1.02:
            lines += 1
        elif snap.first_seen_ts > snap.power_on_ts + 60:
            lines += 1
        return self.s(17) * lines

    def _draw(self, dc) -> None:
        w = self._buffer_w
        h = self._buffer_h
        self._fill(dc, 0, 0, w, h, BG)

        margin = self.s(18)
        inner = w - margin * 2
        snap = self._snap

        if snap is None:
            self._text(
                dc, "正在采集数据…", 0, h // 2 - self.s(10), w, self.s(20),
                self._font("body", 12), INK_FAINT, DT_CENTER,
            )
            return

        y = self.s(TOP_PAD)

        # ---- 抬头：先交代「这次开机多久」这个身份信息 ----
        self._text(dc, "本次开机", margin, y, inner, self.s(15),
                   self._font("cap", 12), INK_DIM, extra=self.s(1))
        y += self.s(18)

        self._text(dc, fmt_duration(snap.power_on_seconds), margin, y, inner,
                   self.s(31), self._font("hero", 24, bold=True), INK)
        y += self.s(34)

        self._text(
            dc,
            f"{_clock(snap.power_on_ts)} 开机　·　程序已统计 "
            f"{fmt_duration(snap.covered_seconds)}",
            margin, y, inner, self.s(15), self._font("tiny", 11), INK_FAINT,
        )
        y += self.s(15)

        # 先把自然高度量出来，多出来的空间平摊到卡片间距 —— 否则窗口底部
        # 会空一大块灰，整张面板看着像没画完。
        hero_h, power_h, curve_h, price_h = self._card_heights(snap)
        footer_h = self._footer_height(snap)
        gaps = 4
        natural = sum((hero_h, power_h, curve_h, price_h)) + footer_h + self.s(CARD_GAP) * gaps
        available = h - y - self.s(BOTTOM_PAD)
        gap = self.s(CARD_GAP) + max(0, available - natural) / gaps

        y = self._hero_card(dc, margin, y, inner, snap, hero_h)
        y = y + gap
        y = self._power_card(dc, margin, y, inner, snap, power_h)
        y = y + gap
        y = self._curve_card(dc, margin, y, inner, snap, curve_h)
        y = y + gap
        y = self._price_card(dc, margin, y, inner, snap, price_h)
        y = y + gap
        y = self._footer(dc, margin, y, inner, snap)

        # 内容没画完就被窗口裁掉是默默出错的典型，留个痕迹方便排障
        if y > self.client_h - self.s(4):
            debug.log("panel", f"内容高度 {y} 超出窗口高度 {self.client_h}")

    # ---- 顶部：电费 ----

    def _hero_card(self, dc, x, y, w, snap, height) -> int:
        self._card(dc, x, y, w, height, fill=None, border=HERO_LINE,
                   grad=(HERO_TOP, HERO_BOT), shadow=HERO_SHADOW)

        pad = self.s(16)
        ix = x + pad
        iw = w - pad * 2
        cy = y + self.s(13)

        self._text(dc, "本次已用电费", ix, cy, iw // 2, self.s(15),
                   self._font("cap", 12), INK_DIM, extra=self.s(1))
        value, unit = fmt_energy(snap.session_wh)
        self._text(dc, f"{value} {unit}", ix, cy, iw, self.s(15),
                   self._font("strong", 11, bold=True), INK_SOFT, DT_RIGHT)
        cy += self.s(19)

        money_font = self._font("money", 26, bold=True)
        money = f"{self.cfg.currency}{snap.session_cost:.3f}"
        self._text(dc, money, ix, cy, iw, self.s(34), money_font, ACCENT)
        mw = self._text_width(dc, money, money_font)
        self._text(dc, "元", ix + mw + self.s(7), cy + self.s(11), iw, self.s(20),
                   self._font("unit", 12), INK_DIM)
        cy += self.s(38)

        # 统计覆盖率：程序不一定从开机那刻就在跑，得让用户看见缺口
        ratio = 0.0
        if snap.power_on_seconds > 1:
            ratio = max(0.0, min(snap.covered_seconds / snap.power_on_seconds, 1.0))
        label_w = self.s(78)
        bar_h = self.s(6)
        bar_w = max(self.s(40), iw - label_w - self.s(8))
        bar_y = cy + self.s(1)
        self._round_rect(dc, ix, bar_y, bar_w, bar_h, bar_h / 2.0, TRACK)
        if ratio > 0:
            fill_w = max(float(bar_h), bar_w * ratio)
            self._clip_rounded(dc, ix, bar_y, fill_w, bar_h, bar_h / 2.0)
            self._vgradient(dc, ix, bar_y, fill_w, bar_h, ACCENT, ACCENT_DEEP)
            self._pop_clip(dc)
        self._text(dc, f"统计覆盖 {ratio * 100:.0f}%",
                   ix + bar_w + self.s(8), bar_y - self.s(5), label_w, self.s(16),
                   self._font("tiny", 10), INK_FAINT, DT_RIGHT)

        return y + height

    # ---- 功率构成 + 今日 ----

    def _power_card(self, dc, x, y, w, snap, height) -> int:
        self._card(dc, x, y, w, height)

        pad = self.s(16)
        ix = x + pad
        iw = w - pad * 2
        half = iw // 2
        rx = ix + half + self.s(14)
        right_w = iw - half - self.s(14)
        cy = y + self.s(13)

        self._text(dc, "当前功率", ix, cy, half - self.s(6), self.s(15),
                   self._font("cap", 12), INK_DIM, extra=self.s(1))
        self._text(dc, f"CPU 负载 {snap.cpu_util:.0f}%", ix, cy, half, self.s(15),
                   self._font("tiny", 10), INK_FAINT, DT_RIGHT)
        self._text(dc, "今日累计", rx, cy, right_w, self.s(15),
                   self._font("cap", 12), INK_DIM, extra=self.s(1))
        cy += self.s(19)

        num_font = self._font("num", 24, bold=True)
        left_val = f"{snap.current_w:.0f}"
        self._text(dc, left_val, ix, cy, half, self.s(32), num_font, INK)
        lw = self._text_width(dc, left_val, num_font)
        self._text(dc, "W", ix + lw + self.s(6), cy + self.s(10), half, self.s(18),
                   self._font("unit", 12), INK_DIM)

        right_val = f"{snap.today_wh / 1000:.2f}"
        self._text(dc, right_val, rx, cy, right_w, self.s(32), num_font, INK)
        rw = self._text_width(dc, right_val, num_font)
        self._text(dc, "kWh", rx + rw + self.s(6), cy + self.s(10), self.s(38),
                   self.s(18), self._font("unit", 12), INK_DIM)
        self._text(dc, f"≈ {self.cfg.currency}{snap.today_cost:.2f}",
                   rx, cy + self.s(22), right_w, self.s(16),
                   self._font("strong", 11, bold=True), ACCENT, DT_RIGHT)
        cy += self.s(38)

        # 两格之间的竖分隔线
        self._line(dc, ix + half + self.s(6), y + self.s(16),
                   ix + half + self.s(6), cy - self.s(14), LINE, 1)

        bar_h = self.s(10)
        self._round_rect(dc, ix, cy, iw, bar_h, bar_h / 2.0, TRACK)
        total = max(snap.cpu_w + snap.gpu_w + snap.base_w, 1.0)
        self._clip_rounded(dc, ix, cy, iw, bar_h, bar_h / 2.0)
        cursor = 0.0
        for watts, color in (
            (snap.cpu_w, CPU_COLOR),
            (snap.gpu_w, GPU_COLOR),
            (snap.base_w, OTHER_COLOR),
        ):
            seg = iw * max(0.0, watts) / total
            if seg >= 1:
                self._fill(dc, ix + cursor, cy, seg, bar_h, color)
            cursor += seg
        self._pop_clip(dc)
        cy += bar_h + self.s(11)

        legend_x = ix
        for label, watts, color in (
            ("CPU", snap.cpu_w, CPU_COLOR),
            ("GPU", snap.gpu_w, GPU_COLOR),
            ("其他", snap.base_w, OTHER_COLOR),
        ):
            dot = self.s(7)
            self._round_rect(dc, legend_x, cy + self.s(4), dot, dot, dot / 2.0, color)
            legend_x += dot + self.s(5)
            self._text(dc, label, legend_x, cy, self.s(34), self.s(16),
                       self._font("tiny", 10), INK_FAINT)
            legend_x += self._text_width(dc, label, self._font("tiny", 10)) + self.s(5)
            avail = label != "GPU" or snap.gpu_measured
            text = f"{watts:.0f} W" if avail else "不可读"
            strong = self._font("strong", 11, bold=True)
            self._text(dc, text, legend_x, cy, self.s(58), self.s(16),
                       strong, INK_SOFT if avail else INK_FAINT)
            legend_x += self._text_width(dc, text, strong) + self.s(14)

        return y + height

    # ---- 曲线 ----

    def _curve_card(self, dc, x, y, w, snap, height) -> int:
        chart_h = self.s(102)
        self._card(dc, x, y, w, height)

        pad = self.s(16)
        ix = x + pad
        iw = w - pad * 2
        cy = y + self.s(13)

        self._text(dc, "最近 30 分钟功率曲线", ix, cy, iw // 2, self.s(15),
                   self._font("cap", 12), INK_DIM, extra=self.s(1))
        self._text(dc, f"均值 {snap.average_w:.0f} W　·　峰值 {snap.peak_w:.0f} W",
                   ix, cy, iw, self.s(15), self._font("tiny", 10), INK_FAINT,
                   DT_RIGHT)
        cy += self.s(23)
        self._draw_curve(dc, ix, cy, iw, chart_h)
        return y + height

    def _draw_curve(self, dc, x, y, w, h) -> None:
        now = time.time()
        start = now - CURVE_WINDOW_SECONDS
        recent = [(ts, v) for ts, v in self._curve if ts >= start]

        if len(recent) < 2:
            self._text(dc, "正在采集数据…", x, y, w, h,
                       self._font("body", 12), INK_FAINT, DT_CENTER)
            return

        # 采样本身有抖动，原样连成折线会像锯齿；三点滑动平均就够顺眼了
        values = [v for _, v in recent]
        if len(values) >= 5:
            smooth = []
            for i in range(len(values)):
                lo, hi = max(0, i - 1), min(len(values), i + 2)
                window = values[lo:hi]
                smooth.append(sum(window) / len(window))
            values = smooth

        pad_l, pad_r = self.s(34), self.s(6)
        pad_t, pad_b = self.s(8), self.s(16)
        pw = max(1, w - pad_l - pad_r)
        ph = max(1, h - pad_t - pad_b)

        ymax = max(100.0, max(values) * 1.15)
        ymax = (int(ymax / 50) + 1) * 50.0

        for i in range(3):
            gy = y + pad_t + ph * i / 2.0
            self._line(dc, x + pad_l, gy, x + w - pad_r, gy, LINE_SOFT, 1)
            self._text(
                dc, str(int(ymax * (1 - i / 2.0))),
                x, gy - self.s(8), pad_l - self.s(6), self.s(16),
                self._font("tiny", 10), INK_FAINT, DT_RIGHT,
            )

        span = max(1, len(values) - 1)
        points = []
        for i, value in enumerate(values):
            px = x + pad_l + pw * i / span
            py = y + pad_t + ph * (1 - min(value, ymax) / ymax)
            points.append((int(px), int(py)))

        base = int(y + pad_t + ph)
        area = [(points[0][0], base)] + points + [(points[-1][0], base)]
        self._clip_polygon(dc, area)
        self._vgradient(dc, x + pad_l, y + pad_t, pw, ph, AREA_TOP, AREA_BOTTOM)
        self._pop_clip(dc)
        self._polyline(dc, points, ACCENT, max(1, self.s(2)))

        # 末端圆点：一眼看出现在读数落在哪
        ex, ey = points[-1]
        r = self.s(4)
        self._ellipse(dc, ex - r, ey - r, r * 2, r * 2, ACCENT)
        inner = max(1, self.s(2) - 1)
        self._ellipse(dc, ex - inner, ey - inner, inner * 2, inner * 2, CARD)

        label_y = y + h - pad_b + self.s(1)
        self._text(dc, "-30 分", x + pad_l, label_y, self.s(60), self.s(15),
                   self._font("tiny", 10), INK_FAINT)
        self._text(dc, "现在", x + w - pad_r - self.s(60), label_y, self.s(60),
                   self.s(15), self._font("tiny", 10), INK_FAINT, DT_RIGHT)

    # ---- 电价 ----

    def _valley_price(self) -> float:
        """谷段电价。只有部分地区（如四川）分丰枯水期，其他地区两个值相同。"""
        wet = time.localtime().tm_mon in (self.cfg.valley_wet_months or ())
        return self.cfg.price_valley_wet if wet else self.cfg.price_valley_dry

    def _price_card(self, dc, x, y, w, snap, height) -> int:
        self._card(dc, x, y, w, height)

        pad = self.s(16)
        ix = x + pad
        iw = w - pad * 2
        cy = y + self.s(13)

        self._text(dc, "电价", ix, cy, self.s(60), self.s(15),
                   self._font("cap", 12), INK_DIM, extra=self.s(1))

        chip_font = self._font("tiny", 10)
        place = f"{self.cfg.tariff_region} · {self.cfg.tariff_plan}"
        place_max = max(self.s(80), iw - self.s(80))
        place = self._ellipsize(dc, place, chip_font, place_max - self.s(16))
        place_w = self._text_width(dc, place, chip_font) + self.s(16)
        self._chip(dc, ix + iw - place_w, cy - self.s(3), place, chip_font,
                   INK_DIM, CHIP_BG, h=self.s(20), width=place_w)
        cy += self.s(21)

        # 峰段标签按「该省有没有独立峰段」决定，四川这类只有平/谷的地方
        # 不能把平价说成峰价
        items = [("峰段", self.cfg.price_peak)] if self.cfg.peak_hours else [
            ("平段", self.cfg.price_peak)
        ]
        if abs(self.cfg.price_flat - self.cfg.price_peak) > 1e-9:
            items.append(("平段", self.cfg.price_flat))
        items.append(("谷段", self._valley_price()))

        cx = ix
        for name, price in items:
            active = abs(price - snap.rate) < 1e-6
            cx += self._chip(
                dc, cx, cy, f"{name} {price:.4f}", chip_font,
                ACCENT_INK if active else INK_DIM,
                ACCENT_SOFT if active else CHIP_BG,
                h=self.s(21),
            ) + self.s(6)
        cy += self.s(26)

        self._text(dc, f"当前 {snap.segment} · {snap.rate:.4f} 元／度",
                   ix, cy, iw, self.s(15), self._font("tiny", 10), INK_FAINT)

        if self.cfg.tariff_verify:
            cy += self.s(18)
            self._text(dc, "⚠ 该地区电价来自网络汇总，建议按当地电网账单核对",
                       ix, cy, iw, self.s(15), self._font("tiny", 10), WARN)

        return y + height

    # ---- 页脚 ----

    def _footer(self, dc, x, y, w, snap) -> int:
        tiny = self._font("tiny", 10)
        gpu_desc = (
            f"GPU {snap.gpu_names[0]} · NVML 实测"
            if snap.gpu_measured and snap.gpu_names
            else "GPU 未检测到可读功耗的显卡"
        )
        self._text(dc, self._ellipsize(dc, f"数据源：{gpu_desc}", tiny, w),
                   x, y, w, self.s(15), tiny, INK_FAINT)
        y += self.s(17)

        estimate = snap.estimated_wh
        if estimate > snap.session_wh * 1.02:
            self._text(
                dc,
                self._ellipsize(
                    dc,
                    f"按均值外推整段开机 ≈ {estimate / 1000:.2f} kWh（估算，仅供参考）",
                    tiny, w,
                ),
                x, y, w, self.s(15), tiny, WARN,
            )
            y += self.s(17)
        elif snap.first_seen_ts > snap.power_on_ts + 60:
            self._text(
                dc,
                self._ellipsize(
                    dc,
                    f"注：程序于 {_clock(snap.first_seen_ts)} 才开始计量，"
                    "更早时段无实测数据。",
                    tiny, w,
                ),
                x, y, w, self.s(15), tiny, WARN,
            )
            y += self.s(17)

        self._text(dc, "左键打开面板　·　右键切换显示 / 设置电价",
                   x, y, w, self.s(15), tiny, INK_FAINT)
        return y + self.s(17)
