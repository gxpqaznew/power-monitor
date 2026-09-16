"""长条外观设置窗 —— 自绘 + 毛玻璃，让用户自己决定长条「长什么样」。

**为什么单开一个窗口，而不是继续往托盘二级菜单里塞**：菜单只能放「档位」，
放不下三样东西 —— **色板**（要显示出颜色本身）、**滑块**（连续值比六档好用）、
**选图**（背景图必然要一个文件对话框）。而颜色的取值有几百种，只有让用户
「看见颜色再点」，才算真的能自己选。

窗口从上到下：

    预览   用当前配置现场画一条**真的**长条（走 strip 自己的排版代码，不是
           示意图），改任何一项立刻重画；预览条背后的桌面也是真的毛玻璃
    排列   自动 / 一排 / 两排 / 三排
    字号   滑块（0.60~1.60，无级）+ 六档快捷
    大小   纤细 / 标准 / 宽大
    配色   10 套调好的方案（色块本身就是缩略预览）+ 三个自定义色
    背景   选图 / 填充方式 / 不透明度
    细节   显示标签、显示分隔线

**全部自绘**：和面板、右键菜单同一套视觉语言。原生控件（按钮 / 单选框）在这块
毛玻璃底上会是一排系统灰，和长条摆在一起非常割裂 —— 而这个窗口存在的意义就是
「好看」，用系统控件等于白做。

交互上只有一个坑值得写下来：命中测试用的是**绘制时登记的区域**
（``self._hits``）—— 也就是「看起来能点的地方」和「真的能点的地方」是同一份
数据算出来的。先画、再登记、鼠标来了反查，改布局时不会出现「按钮画在这儿、
点击判定在那儿」的经典错位。
"""

from __future__ import annotations

import ctypes
import os
import time

from . import debug, frost, stripopts
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
    WM_CLOSE,
    WM_DESTROY,
    WM_ERASEBKGND,
    WM_EXITSIZEMOVE,
    WM_LBUTTONDOWN,
    WM_LBUTTONUP,
    WM_MOUSEMOVE,
    WM_PAINT,
    WM_SETCURSOR,
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

_FONT_FACE = "Microsoft YaHei UI"
_CLASS_NAME = "PowerMonitorAppearanceWnd"
_SRCCOPY = 0x00CC0020
_LOGPIXELSX = 88
_SPI_GETWORKAREA = 0x0030
_NULL_PEN = 8

_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWA_BORDER_COLOR = 34
_DWMWA_CAPTION_COLOR = 35
_DWMWA_TEXT_COLOR = 36
_DWMWCP_ROUND = 2

# 窗口的设计尺寸（未乘 DPI 缩放）。高度是按内容实测定的（底部的「恢复默认 /
# 完成」两个按钮固定在底部，其余各段自上而下按固定间距排），内容排完还剩
# 十几像素的余量；改版面（加一段、换行高）时跑 `_appearanceshot.py` 会立刻
# 报「内容压到底部按钮」——这是最容易被忽略的一种回归。
CLIENT_W = 520
CLIENT_H = 768
PAD = 22
SEG_GAP = 15           # 段与段之间
ROW_GAP = 9            # 段内两行之间

# 预览卡片之前的这一段（顶部留白 / 标题 / 副标题 / 卡片前的间隙）和卡片自己的
# 高度。写成常量是必须的：``_warm_frost`` 在窗口显示**之前**就要按这套坐标去
# 抓屏（那时 ``_buffer_w/_buffer_h`` 还是 0，画都没画过），所以 ``_preview_y``
# 只能拿常量算，不能读 ``_draw`` 的局部变量 —— 而两处的数字一旦对不上，预览条
# 就会画在卡片外面（看着像贴歪了）。名字带 PREVIEW_ 的都只服务于这一件事。
TOP_GAP = 18
TITLE_H = 26
SUB_H = 18
PREVIEW_GAP = 8
PREVIEW_CARD_H = 58
PREVIEW_INSET = 9

# ---- 配色：和详情面板同一套 ----
BG = None              # 下面用函数填（COLORREF 是 0x00BBGGRR）
CARD = None
LINE = None
LINE_SOFT = None
INK = None
INK_SOFT = None
INK_DIM = None
INK_FAINT = None
ACCENT = None
ACCENT_SOFT = None
TRACK = None
HOVER = None


def _rgb(r: int, g: int, b: int) -> int:
    return (b << 16) | (g << 8) | r


BG = _rgb(0xF4, 0xF6, 0xF9)
BG_RGB = (0xF4, 0xF6, 0xF9)
BG_FROST = 196
CARD = _rgb(0xFF, 0xFF, 0xFF)
LINE = _rgb(0xE7, 0xEB, 0xF1)
LINE_SOFT = _rgb(0xF0, 0xF3, 0xF8)
INK = _rgb(0x12, 0x17, 0x22)
INK_SOFT = _rgb(0x4B, 0x55, 0x66)
INK_DIM = _rgb(0x76, 0x81, 0x95)
INK_FAINT = _rgb(0x9E, 0xA8, 0xB8)
ACCENT = _rgb(0x1E, 0x5C, 0xE0)
ACCENT_SOFT = _rgb(0xE7, 0xEE, 0xFE)
TRACK = _rgb(0xE9, 0xED, 0xF3)
HOVER = _rgb(0xEC, 0xF1, 0xF9)
WARN = _rgb(0xB4, 0x53, 0x09)


def _dbg(msg: str) -> None:
    debug.log("appearance", msg)


# ================================================================ 系统对话框
# 颜色 / 图片选择都交给系统对话框：它们是用户熟的那两个界面，还自带最近位置、
# 常用色记忆这些我们自己实现不好的东西。


class _CHOOSECOLORW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", wintypes.DWORD),
        ("hwndOwner", wintypes.HWND),
        ("hInstance", wintypes.HWND),
        ("rgbResult", wintypes.DWORD),
        ("lpCustColors", ctypes.POINTER(wintypes.DWORD)),
        ("Flags", wintypes.DWORD),
        ("lCustData", ctypes.c_void_p),
        ("lpfnHook", ctypes.c_void_p),
        ("lpTemplateName", wintypes.LPCWSTR),
    ]


class _OPENFILENAMEW(ctypes.Structure):
    _fields_ = [
        ("lStructSize", wintypes.DWORD),
        ("hwndOwner", wintypes.HWND),
        ("hInstance", wintypes.HWND),
        ("lpstrFilter", wintypes.LPCWSTR),
        ("lpstrCustomFilter", wintypes.LPWSTR),
        ("nMaxCustFilter", wintypes.DWORD),
        ("nFilterIndex", wintypes.DWORD),
        ("lpstrFile", wintypes.LPWSTR),
        ("nMaxFile", wintypes.DWORD),
        ("lpstrFileTitle", wintypes.LPWSTR),
        ("nMaxFileTitle", wintypes.DWORD),
        ("lpstrInitialDir", wintypes.LPCWSTR),
        ("lpstrTitle", wintypes.LPCWSTR),
        ("Flags", wintypes.DWORD),
        ("nFileOffset", wintypes.WORD),
        ("nFileExtension", wintypes.WORD),
        ("lpstrDefExt", wintypes.LPCWSTR),
        ("lCustData", ctypes.c_void_p),
        ("lpfnHook", ctypes.c_void_p),
        ("lpTemplateName", wintypes.LPCWSTR),
    ]


_CC_FULLOPEN = 0x00000002
_CC_ANYCOLOR = 0x00000100
_OFN_FILEMUSTEXIST = 0x00001000
_OFN_PATHMUSTEXIST = 0x00000800
_OFN_EXPLORER = 0x00080000

# 「自定义颜色」对话框里的 16 个记忆色。必须是**静态**的：结构体里存的是指针，
# 局部变量一被回收，对话框下次就写到野内存里去了。
_custom_colors = (wintypes.DWORD * 16)()

_comdlg32 = None


def _comdlg():
    global _comdlg32
    if _comdlg32 is None:
        try:
            _comdlg32 = ctypes.WinDLL("comdlg32", use_last_error=True)
        except OSError:  # pragma: no cover - 极老的系统
            _comdlg32 = False
    return _comdlg32 or None


def pick_color(owner, initial: tuple[int, int, int] | None):
    """弹系统调色板。返回 ``(r, g, b)``；用户取消返回 None。"""
    lib = _comdlg()
    if lib is None:
        return None
    cc = _CHOOSECOLORW()
    cc.lStructSize = ctypes.sizeof(_CHOOSECOLORW)
    cc.hwndOwner = owner
    cc.rgbResult = _rgb(*(initial or (0x1E, 0x5C, 0xE0)))
    cc.lpCustColors = _custom_colors
    cc.Flags = _CC_FULLOPEN | _CC_ANYCOLOR
    if not lib.ChooseColorW(ctypes.byref(cc)):
        return None
    return (cc.rgbResult & 0xFF, (cc.rgbResult >> 8) & 0xFF,
            (cc.rgbResult >> 16) & 0xFF)


def pick_image(owner, initial_dir: str = ""):
    """弹「打开」对话框选一张图。返回路径；取消返回 None。"""
    lib = _comdlg()
    if lib is None:
        return None
    buf = ctypes.create_unicode_buffer(1024)
    ofn = _OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(_OPENFILENAMEW)
    ofn.hwndOwner = owner
    ofn.lpstrFilter = (
        "图片 (*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.webp)\0"
        "*.png;*.jpg;*.jpeg;*.bmp;*.gif;*.webp\0"
        "所有文件 (*.*)\0*.*\0\0"
    )
    ofn.nFilterIndex = 1
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = 1024
    ofn.lpstrTitle = "选一张图片当长条背景"
    if initial_dir:
        ofn.lpstrInitialDir = initial_dir
    ofn.Flags = _OFN_FILEMUSTEXIST | _OFN_PATHMUSTEXIST | _OFN_EXPLORER
    if not lib.GetOpenFileNameW(ctypes.byref(ofn)):
        return None
    return buf.value or None


# ================================================================ 窗口


_windows: dict[int, "AppearanceWindow"] = {}
_proc_ref: WNDPROC | None = None


@WNDPROC
def _wnd_proc(hwnd, msg, wparam, lparam):
    win = _windows.get(hwnd)
    if win is not None:
        try:
            handled, result = win._on_message(msg, wparam, lparam)
            if handled:
                return result
        except Exception:  # noqa: BLE001 - 回调里不能让异常逃逸
            pass
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


class AppearanceWindow:
    """「长条外观」窗口。平时不建，点了菜单才建（和统计 / 电价窗口一样）。"""

    def __init__(self, cfg, strip, snapshot, apply) -> None:
        """
        cfg      配置对象（就地改）
        strip    TaskbarStrip —— 预览就是借它的排版代码画的，所以预览必然
                 和任务栏上那条一模一样
        snapshot () -> Snapshot，取当前读数给预览用
        apply    (kind, value) -> None，由 app 注入：负责存盘 + 立刻重画长条
        """
        self.cfg = cfg
        self._strip = strip
        self._snapshot = snapshot
        self._apply = apply
        self._hwnd = None
        self._fonts: dict[str, int] = {}
        self._brushes: dict[int, int] = {}
        self._pens: dict[tuple[int, int], int] = {}
        self._buffer_dc = None
        self._buffer_bmp = None
        self._buffer_old = None
        self._buffer_w = 0
        self._buffer_h = 0
        self._clip_region = None
        self.scale = 1.0
        # 绘制时登记的「看起来能点的地方」；鼠标按下时反查它
        self._hits: list[tuple[tuple[int, int, int, int], str, object]] = []
        self._hover: tuple[str, object] | None = None
        self._drag_slider: str | None = None
        self._slider_geom: dict[str, tuple[int, int, int]] = {}
        self._status = ""
        self._status_until = 0.0

    # ------------------------------------------------------------- 尺寸

    def s(self, px: float) -> int:
        return int(round(px * self.scale))

    def _detect_dpi(self) -> None:
        screen = user32.GetDC(None)
        dpi = gdi32.GetDeviceCaps(screen, _LOGPIXELSX) or 96
        user32.ReleaseDC(None, screen)
        self.scale = max(1.0, dpi / 96.0)

    # ------------------------------------------------------------- 生命周期

    def create(self) -> bool:
        global _proc_ref
        _proc_ref = _wnd_proc
        self._detect_dpi()

        hinstance = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = _wnd_proc
        wc.hInstance = hinstance
        wc.lpszClassName = _CLASS_NAME
        if not user32.RegisterClassExW(ctypes.byref(wc)):
            if ctypes.get_last_error() != 1410:
                return False

        style = WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU | WS_MINIMIZEBOX
        rect = wintypes.RECT(0, 0, self.s(CLIENT_W), self.s(CLIENT_H))
        user32.AdjustWindowRectEx(ctypes.byref(rect), style, False, 0)
        win_w = rect.right - rect.left
        win_h = rect.bottom - rect.top
        x, y = self._corner_position(win_w, win_h)

        self._hwnd = user32.CreateWindowExW(
            0, _CLASS_NAME, "长条外观", style,
            x, y, win_w, win_h, None, None, hinstance, None,
        )
        if not self._hwnd:
            return False
        _windows[self._hwnd] = self
        self._apply_dwm_style()
        return True

    def _apply_dwm_style(self) -> None:
        try:
            dwm = ctypes.windll.dwmapi
        except (AttributeError, OSError):
            return
        for attr, value in (
            (_DWMWA_WINDOW_CORNER_PREFERENCE, _DWMWCP_ROUND),
            (_DWMWA_CAPTION_COLOR, BG),
            (_DWMWA_TEXT_COLOR, INK),
            (_DWMWA_BORDER_COLOR, LINE),
        ):
            try:
                data = ctypes.c_uint(value)
                dwm.DwmSetWindowAttribute(
                    wintypes.HWND(self._hwnd), ctypes.c_uint(attr),
                    ctypes.byref(data), ctypes.sizeof(data),
                )
            except (AttributeError, OSError, ValueError):
                return

    def _corner_position(self, win_w: int, win_h: int) -> tuple[int, int]:
        work = wintypes.RECT()
        if user32.SystemParametersInfoW(_SPI_GETWORKAREA, 0, ctypes.byref(work), 0):
            right, bottom, left, top = work.right, work.bottom, work.left, work.top
        else:
            right = user32.GetSystemMetrics(SM_CXSCREEN)
            bottom = user32.GetSystemMetrics(SM_CYSCREEN)
            left = top = 0
        margin = self.s(16)
        x = (left + right - win_w) // 2
        y = top + max(margin, (bottom - top - win_h) // 2)
        return x, y

    def destroy(self) -> None:
        if self._hwnd:
            _windows.pop(self._hwnd, None)
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

    def show(self) -> None:
        if not self._hwnd:
            if not self.create():
                return
        # 窗口还藏着时先把毛玻璃缓存暖上（抓屏抓到的是干净桌面，零闪烁）
        self._warm_frost()
        user32.ShowWindow(self._hwnd, 5)
        flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
        user32.SetWindowPos(self._hwnd, HWND_TOPMOST, 0, 0, 0, 0, flags)
        user32.SetWindowPos(self._hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, flags)
        user32.SetForegroundWindow(self._hwnd)
        self.refresh()

    def hide(self) -> None:
        if self._hwnd:
            user32.ShowWindow(self._hwnd, 0)

    @property
    def is_visible(self) -> bool:
        return bool(self._hwnd) and bool(user32.IsWindowVisible(self._hwnd))

    def refresh(self) -> None:
        if self._hwnd:
            user32.InvalidateRect(self._hwnd, None, False)

    # ------------------------------------------------------------- 消息

    def _on_message(self, msg, wparam, lparam):
        if msg == WM_PAINT:
            self._paint()
            return True, 0
        if msg == WM_ERASEBKGND:
            return True, 1        # 双缓冲自己擦
        if msg == WM_EXITSIZEMOVE:
            # 挪完地方背后的桌面换了一张 → 缓存失效重抓（预览条也要重画）
            frost.invalidate("appearance")
            self._warm_frost()
            self.refresh()
            return True, 0
        if msg == WM_LBUTTONDOWN:
            x = self._signed(lparam & 0xFFFF)
            y = self._signed((lparam >> 16) & 0xFFFF)
            self._on_press(x, y)
            return True, 0
        if msg == WM_MOUSEMOVE:
            x = self._signed(lparam & 0xFFFF)
            y = self._signed((lparam >> 16) & 0xFFFF)
            if self._drag_slider is not None:
                self._drag_to(x)
            else:
                self._set_hover(self._hit_at(x, y))
            return True, 0
        if msg == WM_LBUTTONUP:
            x = self._signed(lparam & 0xFFFF)
            y = self._signed((lparam >> 16) & 0xFFFF)
            if self._drag_slider is not None:
                # 松手：把拖动中攒下的最终值落一次盘
                self._drag_to(x, persist=True)
                self._drag_slider = None
                user32.ReleaseCapture()
            else:
                self._on_click(x, y)
            return True, 0
        if msg == WM_SETCURSOR:
            if (int(lparam) & 0xFFFF) == 1:      # HTCLIENT
                user32.SetCursor(user32.LoadCursorW(None, 32649))  # IDC_HAND
                return True, 1
            return False, 0
        if msg == WM_CLOSE:
            self.hide()
            return True, 0
        if msg == WM_DESTROY:
            _windows.pop(self._hwnd, None)
            self._hwnd = None
            return True, 0
        return False, 0

    @staticmethod
    def _signed(value: int) -> int:
        return value - 0x10000 if value >= 0x8000 else value

    # ------------------------------------------------------------- 交互

    def _hit_at(self, x: int, y: int):
        for rect, action, payload in reversed(self._hits):
            l, t, r, b = rect
            if l <= x < r and t <= y < b:
                return (action, payload)
        return None

    def _set_hover(self, hit) -> None:
        if hit != self._hover:
            self._hover = hit
            self.refresh()

    def _on_press(self, x: int, y: int) -> None:
        """按下：落在哪条滑块的**纵向带**里就进入拖动。

        🔴 ``_slider_geom`` 的值是 ``(轨道左端 x, 轨道宽, 纵向带顶 y, 带高)``
        四元组 —— 这里以前按 4 元组解包、``_slider`` 却只存了 3 个元素，于是
        **每次鼠标按下都抛 ValueError**，异常被 ``_wnd_proc`` 吞掉，表现成
        「滑块拖不动、点轨道也没反应」（点按钮倒是好的，因为那条路径在
        WM_LBUTTONUP）。改这个字段含义时两处必须一起改。
        """
        for name, geom in self._slider_geom.items():
            l, w, top, band_h = geom
            slack = self.s(6)
            if (l - slack <= x < l + w + slack
                    and top - slack <= y < top + band_h + slack):
                self._drag_slider = name
                # 按下就抓鼠标：不然拖出窗口再松手收不到 WM_LBUTTONUP，
                # 滑块会一直黏着光标跑（松手了还在动，最招人烦的那种）。
                if self._hwnd:
                    user32.SetCapture(self._hwnd)
                self._drag_to(x)
                return

    def _on_click(self, x: int, y: int) -> None:
        hit = self._hit_at(x, y)
        if hit is None:
            return
        action, payload = hit
        self._dispatch(action, payload)

    def _dispatch(self, action: str, payload) -> None:
        cfg = self.cfg
        if action == "rows":
            self._apply("rows", payload)
        elif action == "font":
            self._apply("font", float(payload))
        elif action == "size":
            self._apply("size", payload)
        elif action == "palette":
            self._apply("palette", payload)
        elif action == "fit":
            self._apply("bg_fit", payload)
        elif action == "toggle":
            key = str(payload)
            self._apply(key, not getattr(cfg, f"strip_{key}"))
        elif action == "color":
            name = str(payload)
            current = stripopts.color_override(cfg, name)
            rgb = pick_color(self._hwnd, current)
            if rgb is not None:
                self._apply(f"{name}_color", stripopts.rgb_to_hex(rgb))
        elif action == "color_clear":
            self._apply(f"{payload}_color", "")
        elif action == "image_pick":
            path = pick_image(self._hwnd, self._image_dir())
            if path:
                self._apply("bg_image", path)
        elif action == "image_clear":
            self._apply("bg_image", "")
        elif action == "reset":
            self._reset()
        elif action == "done":
            self.hide()
        else:
            return
        self.refresh()

    def _image_dir(self) -> str:
        import os

        path = stripopts.bg_image(self.cfg)
        if path:
            folder = os.path.dirname(path)
            if os.path.isdir(folder):
                return folder
        return ""

    def _reset(self) -> None:
        """恢复默认 —— 外观项一次性回到出厂，字段选择和位置不动。

        位置和「显示哪些字段」属于用户的**内容**决定，不该被「外观」里的一个
        按钮顺手抹掉；这里只清掉纯外观的那些。
        """
        for kind, value in (
            ("rows", "auto"), ("font", 1.0), ("size", "normal"),
            ("palette", "theme"), ("bg_image", ""), ("bg_fit", "cover"),
            ("bg_opacity", 100), ("show_label", True), ("show_divider", True),
        ):
            self._apply(kind, value)
        for name in ("label", "value", "bg"):
            self._apply(f"{name}_color", "")
        self._status = "已恢复默认外观"
        self._status_until = time.monotonic() + 2.5
        self.refresh()

    def _drag_to(self, x: int, persist: bool = False) -> None:
        """把滑块拖到 x 处。

        ``persist=False``：拖动中只改内存 + 重画，**不写盘**。滑块每移动一个
        像素就是一次回调，每次都原子写一遍 config.json 既慢又伤盘；松手时用
        ``persist=True`` 落一次就够。
        """
        name = self._drag_slider
        geom = self._slider_geom.get(name or "")
        if not name or geom is None:
            return
        l, w = geom[0], geom[1]        # 见 _slider：四元组的前两项
        lo, hi = self._slider_range(name)
        t = 0.0 if w <= 0 else max(0.0, min(1.0, (x - l) / w))
        value = lo + (hi - lo) * t
        if name == "font":
            value = round(value, 2)
        else:
            value = int(round(value))
        self._apply(name, value, persist=persist)
        self.refresh()

    @staticmethod
    def _slider_range(name: str) -> tuple[float, float]:
        if name == "font":
            return stripopts.FONT_SCALE_MIN, stripopts.FONT_SCALE_MAX
        return float(stripopts.BG_OPACITY_MIN), float(stripopts.BG_OPACITY_MAX)

    # ------------------------------------------------------------- 绘制基础

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
            gdi32.BitBlt(hdc, 0, 0, rect.right, rect.bottom,
                         self._buffer_dc, 0, 0, _SRCCOPY)
        finally:
            user32.EndPaint(self._hwnd, ctypes.byref(ps))

    def _fill(self, dc, x, y, w, h, color) -> None:
        rect = wintypes.RECT(int(x), int(y), int(x + w), int(y + h))
        user32.FillRect(dc, ctypes.byref(rect), self._brush(color))

    def _round_rect(self, dc, x, y, w, h, radius, fill, border=None,
                    width: int = 1) -> None:
        pen = self._pen(border, width) if border is not None \
            else gdi32.GetStockObject(_NULL_PEN)
        old_brush = gdi32.SelectObject(dc, self._brush(fill))
        old_pen = gdi32.SelectObject(dc, pen)
        d = max(0, int(radius * 2))
        gdi32.RoundRect(dc, int(x), int(y), int(x + w), int(y + h), d, d)
        gdi32.SelectObject(dc, old_pen)
        gdi32.SelectObject(dc, old_brush)

    def _line(self, dc, x1, y1, x2, y2, color, width: int = 1) -> None:
        old = gdi32.SelectObject(dc, self._pen(color, width))
        gdi32.MoveToEx(dc, int(x1), int(y1), None)
        gdi32.LineTo(dc, int(x2), int(y2))
        gdi32.SelectObject(dc, old)

    def _clip_rounded(self, dc, x, y, w, h, radius) -> None:
        rgn = gdi32.CreateRoundRectRgn(
            int(x), int(y), int(x + w + 1), int(y + h + 1),
            max(0, int(radius * 2)), max(0, int(radius * 2)),
        )
        self._clip_region = rgn
        gdi32.SelectClipRgn(dc, rgn)

    def _pop_clip(self, dc) -> None:
        gdi32.SelectClipRgn(dc, None)
        if self._clip_region:
            gdi32.DeleteObject(self._clip_region)
            self._clip_region = None

    def _text_width(self, dc, text: str, font) -> int:
        old = gdi32.SelectObject(dc, font)
        size = SIZE()
        gdi32.GetTextExtentPoint32W(dc, text, len(text), ctypes.byref(size))
        gdi32.SelectObject(dc, old)
        return size.cx

    def _ellipsize(self, dc, text: str, font, max_w: int) -> str:
        if max_w <= 0:
            return ""
        if self._text_width(dc, text, font) <= max_w:
            return text
        cut = text
        while cut and self._text_width(dc, cut + "…", font) > max_w:
            cut = cut[:-1]
        return (cut + "…") if cut else ""

    def _text(self, dc, text, x, y, w, h, font, color, align=DT_LEFT,
              extra: int = 0) -> None:
        old = gdi32.SelectObject(dc, font)
        gdi32.SetTextColor(dc, color)
        gdi32.SetBkMode(dc, TRANSPARENT)
        prev = 0
        if extra:
            prev = gdi32.SetTextCharacterExtra(dc, int(extra))
        rect = wintypes.RECT(int(x), int(y), int(x + w), int(y + h))
        user32.DrawTextW(dc, text, len(text), ctypes.byref(rect),
                         align | DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX)
        if extra:
            gdi32.SetTextCharacterExtra(dc, prev)
        gdi32.SelectObject(dc, old)

    # ------------------------------------------------------------- 毛玻璃

    def _screen_origin(self):
        if not self._hwnd:
            return None
        client = wintypes.RECT()
        if not user32.GetClientRect(self._hwnd, ctypes.byref(client)):
            return None
        if client.right <= 0 or client.bottom <= 0:
            return None
        origin = wintypes.POINT(0, 0)
        if not user32.ClientToScreen(self._hwnd, ctypes.byref(origin)):
            return None
        return origin.x, origin.y, client.right, client.bottom

    def _warm_frost(self) -> None:
        """显示**之前**把窗口背后的桌面糊好塞进缓存。

        顺便把预览条那一块的也一起暖了（预览走的是长条的排版代码，缓存键是
        ``appearance``）—— 窗口还藏着，抓到的是干净桌面，显示出来就是一张
        现成的毛玻璃，不会先闪一下实色底。
        """
        info = self._screen_origin()
        if info is None:
            return
        ox, oy, w, h = info
        screen = user32.GetDC(None)
        dc = gdi32.CreateCompatibleDC(screen)
        user32.ReleaseDC(None, screen)
        if not dc:
            return
        try:
            frost.blit(dc, "appearance", ox, oy, 0, 0, w, h,
                       BG_RGB, BG_FROST, hide_hwnd=self._hwnd)
            self._draw_preview(dc, *self._preview_box(), warm=True)
        finally:
            gdi32.DeleteDC(dc)

    def _frost_bg(self, dc, w, h) -> None:
        self._fill(dc, 0, 0, w, h, BG)
        info = self._screen_origin()
        if info is None:
            return
        ox, oy, _cw, _ch = info
        frost.blit(dc, "appearance", ox, oy, 0, 0, w, h,
                   BG_RGB, BG_FROST, hide_hwnd=self._hwnd, hold=True)

    # ------------------------------------------------------------- 版面

    def _m(self) -> dict:
        """这一帧的几何：所有控件的位置都在这里算，绘制和命中测试共用。"""
        s = self.s
        w = self._buffer_w
        return {"w": w, "h": self._buffer_h, "left": s(PAD),
                "inner": w - s(PAD) * 2}

    def _preview_box(self) -> tuple[int, int, int, int]:
        """预览卡片的内框（长条就画在里面）。

        客户区宽度在窗口还没画过第一帧时也要算得对 —— ``_warm_frost`` 是在
        ``ShowWindow`` 之前调的，那时缓冲区还没建，所以退化用设计宽度乘缩放。
        """
        s = self.s
        left = s(PAD)
        inner = (self._buffer_w or s(CLIENT_W)) - s(PAD) * 2
        y = self._preview_y()
        return (left + s(PREVIEW_INSET), y + s(PREVIEW_INSET),
                max(1, inner - s(PREVIEW_INSET * 2)),
                s(PREVIEW_CARD_H - PREVIEW_INSET * 2))

    def _preview_y(self) -> int:
        """预览卡片顶边 —— 必须和 ``_draw`` 里推进到那一步的 y 一致。

        写成常量算式而不是读 `_draw` 的局部变量：``_warm_frost`` 在窗口显示前
        就要用这个坐标去抓屏，那时还没画过任何一帧。两边共用同一组常量
        （``TOP_GAP`` / ``TITLE_H`` / ``SUB_H`` / ``PREVIEW_GAP``），
        改一处不会只改到一半。
        """
        return self.s(TOP_GAP + TITLE_H + SUB_H + PREVIEW_GAP)

    # ------------------------------------------------------------- 主绘制

    def _draw(self, dc) -> None:
        w = self._buffer_w
        h = self._buffer_h
        self._hits = []
        self._slider_geom = {}
        self._frost_bg(dc, w, h)

        s = self.s
        left = s(PAD)
        inner = w - s(PAD) * 2
        title_font = self._font("title", 17, bold=True)
        sub_font = self._font("sub", 11)
        cap_font = self._font("cap", 12)
        chip_font = self._font("chip", 11)
        tiny = self._font("tiny", 10)

        y = self.s(TOP_GAP)
        self._text(dc, "长条外观", left, y, inner, s(24), title_font, INK)
        y += s(TITLE_H)
        self._text(dc, "改哪一项都立刻生效 —— 任务栏上那条会同步变化，下面就是它。",
                   left, y, inner, s(16), sub_font, INK_FAINT)
        y += s(SUB_H)

        # ---- 预览 ----
        y += s(PREVIEW_GAP)
        box_h = s(PREVIEW_CARD_H)
        self._round_rect(dc, left, y, inner, box_h, s(14), CARD, LINE)
        px, py, pw, ph = self._preview_box()
        self._draw_preview(dc, px, py, pw, ph)
        y += box_h

        # ---- 排列 ----
        y += self._seg(dc, y, left, inner, "排列",
                       "字段多了自动折行；固定排数时宁可少显示几项",
                       [("rows", k, label) for k, label in stripopts.ROWS],
                       stripopts.rows_mode(self.cfg))
        y += s(4)

        # ---- 字号 ----
        y += s(14)
        scale = stripopts.font_scale(self.cfg)
        self._section(dc, left, y, inner, "字号",
                      f"{scale:.2f}×", "档位是快捷取值，滑块可以细调")
        y += s(18)
        y += self._slider(dc, left, y, inner, "font", scale,
                          stripopts.FONT_SCALE_MIN, stripopts.FONT_SCALE_MAX,
                          f"{scale:.2f}×")
        y += s(6)
        items = [((("font", value)), label)
                 for value, label in stripopts.FONT_SCALES]
        y += self._chip_row(dc, y, left, inner, items, scale)

        # ---- 大小 ----
        y += s(4)
        y += self._seg(dc, y, left, inner, "大小", "",
                       [("size", k, label)
                        for k, label, _r, _p in stripopts.SIZES],
                       stripopts.size_key(self.cfg))
        y += s(4)

        # ---- 配色 ----
        y += s(14)
        self._section(dc, left, y, inner, "配色方案",
                      stripopts.PALETTE_LABEL[stripopts.palette_key(self.cfg)],
                      "每套都是调好对比度的整组颜色")
        y += s(18)
        y += self._palette_grid(dc, y, left, inner)
        y += s(8)
        y += self._custom_colors(dc, y, left, inner)

        # ---- 背景图 ----
        y += s(4)
        y += self._background(dc, y, left, inner)

        # ---- 细节 ----
        y += s(4)
        y += s(14)
        self._section(dc, left, y, inner, "细节", "", "")
        y += s(18)
        half = (inner - s(8)) // 2
        sw_y = y
        self._switch(dc, left, sw_y, half, "显示标签",
                     stripopts.show_label(self.cfg), "show_label")
        self._switch(dc, left + half + s(8), sw_y, half,
                     "显示分隔线", stripopts.show_divider(self.cfg),
                     "show_divider")
        y += self._switch_h()

        # ---- 底部 ----
        btn_h = s(32)
        btn_y = h - s(PAD) - btn_h
        # 内容挤到按钮了就记一笔（改版面时一眼能看出来）
        if y > btn_y - s(6):
            _dbg(f"内容高度 {y} 已贴近底部按钮 {btn_y}")
        if self._status and time.monotonic() < self._status_until:
            self._text(dc, self._status, left, btn_y, inner - s(230), btn_h,
                       tiny, ACCENT)
        rw = self.s(96)
        self._button(dc, w - s(PAD) - rw, btn_y, rw, btn_h, "完成", "done",
                     primary=True)
        lw = self.s(108)
        self._button(dc, w - s(PAD) - rw - s(8) - lw, btn_y, lw, btn_h,
                     "恢复默认外观", "reset")

    def _section(self, dc, x, y, w, title: str, value: str, hint: str) -> None:
        s = self.s
        cap = self._font("cap", 12)
        self._text(dc, title, x, y, w // 2, s(18), cap, INK_SOFT, extra=s(1))
        if value:
            self._text(dc, value, x, y, w, s(18), cap, ACCENT, DT_RIGHT)
        elif hint:
            self._text(dc, hint, x, y, w, s(18), self._font("tiny", 10),
                       INK_FAINT, DT_RIGHT)

    def _seg(self, dc, y, x, w, title: str, hint: str, options, active) -> int:
        """一行「分段选择器」。返回占用高度。"""
        s = self.s
        self._section(dc, x, y, w, title, "", hint)
        y += s(19)
        row_h = s(30)
        n = max(1, len(options))
        gap = s(6)
        cw = (w - gap * (n - 1)) / n
        for i, (action, key, label) in enumerate(options):
            cx = x + i * (cw + gap)
            self._chip(dc, cx, y, cw, row_h, label, action, key,
                       active=(key == active),
                       hover=(self._hover == (action, key)))
        return int(s(19) + row_h)

    def _chip_row(self, dc, y, x, w, items, active_value) -> int:
        """一行快捷 chip。``items`` 是 ``[((action, key), 文字), …]``。"""
        s = self.s
        row_h = s(26)
        gap = s(6)
        n = max(1, len(items))
        cw = (w - gap * (n - 1)) / n
        for i, ((action, key), label) in enumerate(items):
            cx = x + i * (cw + gap)
            self._chip(dc, cx, y, cw, row_h, label, action, key,
                       active=(abs(float(key) - float(active_value)) < 1e-6),
                       hover=(self._hover == (action, key)))
        return row_h

    def _switch_h(self) -> int:
        return self.s(32)

    def _switch(self, dc, x, y, w, label: str, on: bool, key: str) -> None:
        s = self.s
        h = self._switch_h()
        hit = (self._hover == ("toggle", key))
        self._round_rect(dc, x, y, w, h, s(9), HOVER if hit else CARD, LINE)
        # 圆形开关：左关右开，滑块位置一眼可读
        tw = s(34)
        th = s(18)
        tx = x + w - s(12) - tw
        ty = y + (h - th) // 2
        self._round_rect(dc, tx, ty, tw, th, th / 2.0,
                         ACCENT if on else TRACK)
        knob = th - s(4)
        kx = tx + s(2) + (tw - knob - s(4) if on else 0)
        self._round_rect(dc, kx, ty + s(2), knob, knob, knob / 2.0, CARD)
        self._text(dc, label, x + s(13), y, w - tw - s(26), h,
                   self._font("chip", 11), INK_SOFT)
        self._hits.append(((x, y, x + w, y + h), "toggle", key))

    def _button(self, dc, x, y, w, h, label: str, action: str,
                primary: bool = False) -> None:
        key = (action, None)
        hit = self._hover == key
        if primary:
            fill = ACCENT if not hit else _rgb(0x1A, 0x4F, 0xC6)
            fg = CARD
            border = None
        else:
            fill = HOVER if hit else CARD
            fg = INK_SOFT
            border = LINE
        self._round_rect(dc, x, y, w, h, self.s(9), fill, border)
        self._text(dc, label, x, y, w, h, self._font("chip", 11), fg, DT_CENTER)
        self._hits.append(((x, y, x + w, y + h), action, None))

    def _chip(self, dc, x, y, w, h, label: str, action: str, key,
              active: bool, hover: bool) -> None:
        s = self.s
        if active:
            self._round_rect(dc, x, y, w, h, s(8), ACCENT_SOFT, ACCENT)
        elif hover:
            self._round_rect(dc, x, y, w, h, s(8), HOVER, LINE)
        else:
            self._round_rect(dc, x, y, w, h, s(8), CARD, LINE)
        color = ACCENT if active else (INK_SOFT if hover else INK_DIM)
        self._text(dc, label, x, y, w, h, self._font("chip", 11), color,
                   DT_CENTER)
        self._hits.append(((x, y, x + w, y + h), action, key))

    def _slider(self, dc, x, y, w, name: str, value: float, lo: float,
                hi: float, label: str) -> int:
        """自定义滑块。返回占用高度。

        为什么不等宽留白而是「轨道铺满 + 手柄内缩」：手柄半径要留在轨道两端
        之内，否则拖到最右时手柄会画到外面去。

        顺带把 ``_slider_geom[name]`` 登记成
        ``(轨道左端 x, 轨道宽, 纵向带顶 y, 纵向带高)`` —— 按下时靠它判断
        「这一下是不是落在滑块上」，拖动时靠前两项把 x 换算成取值。
        """
        s = self.s
        band = s(22)
        track_h = s(6)
        kx = s(9)
        tw = max(1, w - kx * 2)
        ty = y + (band - track_h) // 2
        self._round_rect(dc, x + kx, ty, tw, track_h, track_h / 2.0, TRACK)
        t = 0.0 if hi <= lo else max(0.0, min(1.0, (value - lo) / (hi - lo)))
        fill_w = max(float(track_h), tw * t)
        self._clip_rounded(dc, x + kx, ty, fill_w, track_h, track_h / 2.0)
        self._fill(dc, x + kx, ty, fill_w, track_h, ACCENT)
        self._pop_clip(dc)
        knob = s(15)
        knob_x = x + kx + tw * t - knob / 2.0
        knob_x = max(x + kx - knob / 2.0 + 1, min(knob_x, x + kx + tw - knob / 2.0 - 1))
        self._round_rect(dc, knob_x, y + (band - knob) // 2, knob, knob,
                         knob / 2.0, CARD, ACCENT, width=max(1, s(2)))
        self._slider_geom[name] = (int(x + kx), int(tw), int(y), int(band))
        return band

    def _palette_grid(self, dc, y, x, w) -> int:
        """10 套方案的色块网格：每块就是一个缩小版的长条（底色 + 数值色点）。"""
        s = self.s
        cols = 4
        gap = s(6)
        bw = (w - gap * (cols - 1)) / cols
        bh = s(32)
        active = stripopts.palette_key(self.cfg)
        for i, (key, label, bg, ink, value, _accent, _light) in \
                enumerate(stripopts.PALETTES):
            col = i % cols
            row = i // cols
            bx = x + col * (bw + gap)
            by = y + row * (bh + gap)
            hit_key = ("palette", key)
            on = key == active
            # 色块本体：底色 + 右边一个数值色的小圆点 —— 一眼看出「这配色长啥样」
            if bg is None:
                # 「跟随质感」没有固定底色，用斜纹灰表示「看情况」
                self._round_rect(dc, bx, by, bw, bh, s(8), CARD,
                                 ACCENT if on else LINE, width=2 if on else 1)
                for k in range(0, int(bw) + int(bh), s(7)):
                    self._line(dc, bx + k, by + bh, bx + k - bh, by,
                               LINE_SOFT, 1)
                self._clip_rounded(dc, bx, by, bw, bh, s(8))
                self._fill(dc, bx, by, bw, bh, LINE_SOFT)
                self._pop_clip(dc)
                self._text(dc, label, bx, by, bw, bh, self._font("tiny", 10),
                           INK_DIM, DT_CENTER)
            else:
                self._round_rect(dc, bx, by, bw, bh, s(8), _rgb(*bg),
                                 ACCENT if on else LINE, width=2 if on else 1)
                dot = s(9)
                self._round_rect(dc, bx + bw - dot - s(9), by + (bh - dot) / 2,
                                 dot, dot, dot / 2.0, _rgb(*value))
                self._text(dc, label, bx + s(9), by, bw - dot - s(18), bh,
                           self._font("tiny", 10), _rgb(*ink))
            self._hits.append(((bx, by, bx + bw, by + bh), "palette", key))
        rows = -(-len(stripopts.PALETTES) // cols)
        return int(rows * bh + (rows - 1) * gap)

    def _custom_colors(self, dc, y, x, w) -> int:
        """三个自定义色块 + 清除。色块显示当前颜色（没设就显示「方案色」）。"""
        s = self.s
        names = (("label", "标签色"), ("value", "数值色"), ("bg", "底色"))
        gap = s(8)
        bw = (w - gap * 2) / 3
        label_h = s(15)
        bh = s(28)
        self._text(dc, "自定义（覆盖上面的方案）", x, y, w, label_h,
                   self._font("tiny", 10), INK_FAINT)
        top = y + label_h
        for i, (name, text) in enumerate(names):
            bx = x + i * (bw + gap)
            explicit = stripopts.color_override(self.cfg, name)
            shown = explicit or self._scheme_color(name)
            hit = self._hover == ("color", name)
            self._round_rect(dc, bx, top, bw, bh, s(8),
                             _rgb(*shown) if shown else CARD,
                             ACCENT if hit else LINE)
            # 色块上的文字要压得住：按底色的明度选黑或白
            luma = (0.299 * shown[0] + 0.587 * shown[1] + 0.114 * shown[2]) \
                if shown else 255
            fg = _rgb(0x20, 0x24, 0x2C) if luma > 140 else _rgb(0xF6, 0xF8, 0xFB)
            mark = text if explicit else f"{text}（跟随）"
            self._text(dc, mark, bx + s(8), top, bw - s(16), bh,
                       self._font("tiny", 10), fg)
            self._hits.append(((bx, top, bx + bw, top + bh), "color", name))
            if explicit:
                cw = s(20)
                self._round_rect(dc, bx + bw - cw - s(4), top + s(4), cw,
                                 bh - s(8), s(5), INK_SOFT)
                self._text(dc, "×", bx + bw - cw - s(4), top + s(4), cw,
                           bh - s(8), self._font("tiny", 10),
                           _rgb(0xF6, 0xF8, 0xFB), DT_CENTER)
                self._hits.append(((bx + bw - cw - s(4), top + s(4),
                                    bx + bw - s(4), top + bh - s(4)),
                                   "color_clear", name))
        return int(label_h + bh)

    def _scheme_color(self, name: str):
        """没设自定义色时，色块上该显示什么颜色 —— 取当前方案/质感的实际色值。"""
        scheme = stripopts.palette(self.cfg)
        if scheme is not None:
            if name == "label":
                return scheme["ink"]
            if name == "value":
                return scheme["value"]
            return scheme["bg"]
        theme = stripopts.theme(self.cfg)
        light = theme in ("light", "paper")
        sample = (243, 244, 246) if light else (32, 34, 38)
        from . import strip as strip_mod

        pal = strip_mod._palette_for(theme, sample, light)
        if name == "label":
            return strip_mod._unpack(pal["dim"])
        if name == "value":
            return strip_mod._unpack(pal["ink"])
        return strip_mod._unpack(pal["bg"])

    def _background(self, dc, y, x, w) -> int:
        """「背景图」整段：选图 + 填充方式 + 不透明度。返回**占用高度**。

        🔴 返回高度（不是绝对 y）—— ``_draw`` 里是 ``y += self._background(...)``
        这么用的。这里踩过：一开始图省事直接 ``return y``（绝对坐标），结果整段
        被当成高度又加了一次，内容凭空多出 600 多像素，底部的「恢复默认 / 完成」
        被顶到窗口外面去了（画面看不出错，只是最后一段没了）。
        """
        s = self.s
        top = y
        path = stripopts.bg_image(self.cfg)
        self._section(dc, x, y, w, "背景图",
                      "", "贴在毛玻璃之上的那层「贴膜」")
        y += s(19)
        row_h = s(28)
        bw = s(96)
        self._button(dc, x, y, bw, row_h, "选择图片…", "image_pick")
        gap = s(6)
        n = len(stripopts.BG_FITS)
        cw = (w - bw - s(10) - gap * (n - 1)) / n
        active = stripopts.bg_fit(self.cfg)
        for i, (key, label, _hint) in enumerate(stripopts.BG_FITS):
            cx = x + bw + s(10) + i * (cw + gap)
            self._chip(dc, cx, y, cw, row_h, label, "fit", key,
                       active=(key == active), hover=(self._hover == ("fit", key)))
        y += row_h + s(6)
        # 路径 / 提示（有图时右边给一个「清除」）
        name = os.path.basename(path) if path else ""
        text = f"当前：{name}" if name else "未选择（长条只用毛玻璃底）"
        room = w - s(58) if path else w
        self._text(dc, self._ellipsize(dc, text, self._font("tiny", 10), room),
                   x, y, room, s(16), self._font("tiny", 10),
                   INK_DIM if name else INK_FAINT)
        if path:
            cw2 = s(50)
            self._button(dc, x + w - cw2, y - s(3), cw2, s(22), "清除",
                         "image_clear")
        y += s(20)
        # 不透明度：标签在左、读出值在右，滑块单独一行（滑块上不写字，免得挤）
        opacity = stripopts.bg_opacity(self.cfg)
        self._text(dc, "背景图不透明度", x, y, w // 2, s(16),
                   self._font("tiny", 10), INK_SOFT)
        self._text(dc, f"{opacity}%", x, y, w, s(16),
                   self._font("tiny", 10), ACCENT, DT_RIGHT)
        y += s(18)
        y += self._slider(dc, x, y, w, "bg_opacity", opacity,
                          stripopts.BG_OPACITY_MIN, stripopts.BG_OPACITY_MAX,
                          f"{opacity}%")
        return int(y - top)

    # ------------------------------------------------------------- 预览

    def _draw_preview(self, dc, x, y, w, h, warm: bool = False) -> None:
        """用长条**自己的排版代码**画一条真的。

        走 strip 的 ``_layout`` 而不是在这里另写一套：预览和任务栏上那条必须
        逐像素同源，否则「预览很好看、真到任务栏上不是这样」比没有预览更糟。

        代价是要临时借用 strip 实例的几个内部字段（限制宽度 / 最大高度 /
        抓屏坐标 / 毛玻璃键），用完立刻还原 —— 借的时候长条自己是不会重画的
        （同一个线程，中间没有消息泵）。
        """
        s = self._strip
        snap = None
        try:
            snap = self._snapshot()
        except Exception:  # noqa: BLE001
            snap = None
        if s is None or snap is None or w <= 2 or h <= 2:
            self._fill(dc, x, y, w, h, TRACK)
            return

        info = self._screen_origin()
        ox = info[0] if info else 0
        oy = info[1] if info else 0

        saved = (s.cfg, s._limit, s._max_height, s._scale, s._anchor_kind,
                 s._rect, s._hwnd, s._frost_key, s._frost_hold)
        try:
            s.cfg = self.cfg
            s._scale = 1.0
            s._limit = int(w)
            s._max_height = int(h)
            s._anchor_kind = "start"
            # 毛玻璃：键换成 appearance（别和真长条抢缓存），坐标改成预览条
            # 在屏幕上的位置，抓屏前藏的是**本窗口**（抓到的是它背后的桌面）。
            s._frost_key = "appearance"
            s._frost_hold = not warm
            s._hwnd = self._hwnd
            s._rect = (ox + x, oy + y, ox + x + w, oy + y + h)
            pw = int(s._layout(dc, 1.0, snap, render=False)[0])
            pw = max(1, min(int(w), pw))
            px = x + (int(w) - pw) // 2
            s._rect = (ox + px, oy + y, ox + px + pw, oy + y + h)
            s._layout(dc, 1.0, snap, render=True,
                      origin_x=px, origin_y=y, height=int(h))
        except Exception as exc:  # noqa: BLE001 - 预览失败不能带崩设置窗
            _dbg(f"预览绘制失败 {exc!r}")
            self._fill(dc, x, y, w, h, TRACK)
        finally:
            (s.cfg, s._limit, s._max_height, s._scale, s._anchor_kind,
             s._rect, s._hwnd, s._frost_key, s._frost_hold) = saved
