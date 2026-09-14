"""任务栏几何 & 主题色。

「把一条长条放到开始按钮左边」这件事没有官方 API —— 任务栏不是可扩展的容器
（Win11 已经砍掉了 DeskBand / 工具栏那套），只能自己开一个**无边框置顶窗口**
盖在任务栏那片空白上。

好在实测下来开始按钮是一个独立 HWND（类名就是 ``"Start"``），位置能直接查到，
所以「贴着开始按钮左边显示」是可靠的；查不到时退回「通知区域左边」。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt

from . import debug
from .w32 import user32, wintypes

_SPI_GETWORKAREA = 0x0030
_LOGPIXELSX = 88

user32.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowW.restype = wt.HWND
user32.FindWindowExW.argtypes = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
user32.FindWindowExW.restype = wt.HWND
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetWindowRect.restype = wt.BOOL
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.IsWindowVisible.restype = wt.BOOL
user32.GetDpiForWindow.argtypes = [wt.HWND]
user32.GetDpiForWindow.restype = wt.UINT

Rect = tuple[int, int, int, int]  # (left, top, right, bottom)


def _rect(hwnd) -> Rect | None:
    if not hwnd:
        return None
    r = wt.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return (r.left, r.top, r.right, r.bottom)


def taskbar() -> tuple[object, Rect, int] | None:
    """主任务栏。(hwnd, 屏幕矩形, dpi)；没有任务栏返回 None。"""
    hwnd = user32.FindWindowW("Shell_TrayWnd", None)
    if not hwnd:
        return None
    rect = _rect(hwnd)
    if rect is None:
        return None
    try:
        dpi = int(user32.GetDpiForWindow(hwnd)) or 96
    except (AttributeError, OSError, ValueError):
        dpi = 96
    return hwnd, rect, dpi


def is_visible(rect: Rect | None) -> bool:
    """自动隐藏时任务栏会被挪到屏幕外，只剩一两像素；这种时候长条也该跟着藏。"""
    if rect is None:
        return False
    return (rect[3] - rect[1]) >= 8 and (rect[2] - rect[0]) >= 8


def start_button() -> Rect | None:
    """开始按钮的屏幕矩形。

    **坑**：它不是顶层窗口，``FindWindowW("Start")`` 永远返回 0（FindWindow
    只搜顶层窗口），必须从 ``Shell_TrayWnd`` 的子窗口里用 ``FindWindowExW``
    找。这个错误很隐蔽 —— 会静默退化成「贴通知区域左边」。
    """
    bar = user32.FindWindowW("Shell_TrayWnd", None)
    hwnd = None
    if bar:
        hwnd = user32.FindWindowExW(bar, None, "Start", None)
    if not hwnd:
        # 万一以后某个版本挪成顶层窗口，这条兜底还能用
        hwnd = user32.FindWindowW("Start", None)
    return _rect(hwnd)


def cluster_left() -> int | None:
    """任务栏「居中图标区」的最左边 —— 长条就该贴在这个位置左边。

    开始按钮就是这一区的第一项，所以它的左边缘等于整区左边缘（实测两者
    完全重合）。开始按钮查不到时退一步用 ``ReBarWindow32``（运行中程序
    那个容器）的左边缘。
    """
    start = start_button()
    if start is not None:
        return start[0]
    bar = user32.FindWindowW("Shell_TrayWnd", None)
    if not bar:
        return None
    rebar = _rect(user32.FindWindowExW(bar, None, "ReBarWindow32", None))
    if rebar is not None:
        return rebar[0]
    return None


def notification_area() -> Rect | None:
    """通知区域（托盘）的屏幕矩形，用来做「放右边」的兜底锚点。"""
    bar = user32.FindWindowW("Shell_TrayWnd", None)
    if not bar:
        return None
    return _rect(user32.FindWindowExW(bar, None, "TrayNotifyWnd", None))


def screen_rect() -> Rect:
    return (0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))


def scale_for(rect: Rect, dpi: int = 96) -> float:
    """按任务栏高度推一个缩放系数。

    任务栏高度已经包含了用户的 DPI 和「任务栏大小」设置，拿它当基准比自己算
    DPI 更贴合 —— 用户把小任务栏打开时，长条会跟着变矮。
    """
    height = max(1, rect[3] - rect[1])
    return height / 48.0


def uses_light_theme() -> bool:
    """系统（任务栏）是不是浅色主题。决定长条用浅底深字还是深底浅字。

    注意这是 ``SystemUsesLightTheme``（任务栏/开始菜单），不是
    ``AppsUseLightTheme``（应用窗口），两者可以不一样。
    """
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            return bool(winreg.QueryValueEx(key, "SystemUsesLightTheme")[0])
    except (OSError, ImportError, IndexError):
        return True


def dpi_awareness() -> None:
    """拿到物理像素坐标。不声明的话 GetWindowRect 给的是被虚拟化过的值。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
        return
    except (AttributeError, OSError):
        pass
    try:
        user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


def describe() -> str:
    """一次性把任务栏环境打出来，排障用。"""
    info = taskbar()
    if info is None:
        return "没有任务栏（Shell_TrayWnd 找不到）"
    _hwnd, rect, dpi = info
    start = start_button()
    tray = notification_area()
    return (
        f"任务栏={rect} dpi={dpi} 可见={is_visible(rect)} "
        f"开始按钮={start} 居中区左边={cluster_left()} 通知区域={tray} "
        f"浅色主题={uses_light_theme()}"
    )
