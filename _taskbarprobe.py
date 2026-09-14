"""探查任务栏的真实窗口结构（在沙箱里全是猜的，必须实测）。

想知道的三件事：
  1. 任务栏（Shell_TrayWnd）和各子窗口的类名 + 屏幕矩形；
  2. 开始按钮到底是不是一个独立 HWND —— 决定「放在开始按钮左边」能不能做；
  3. 任务栏是不是居中对齐（Win11 默认居中），居中的话开始按钮左侧那片空白
     从哪个 x 开始、到哪个 x 结束。

输出到 _preview/taskbar_probe.txt，同时打印到 stdout。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent / "_preview" / "taskbar_probe.txt"

_LOGPIXELSX = 88

u = ctypes.windll.user32
k = ctypes.windll.kernel32
g = ctypes.windll.gdi32

u.FindWindowW.argtypes = [wt.LPCWSTR, wt.LPCWSTR]
u.FindWindowW.restype = wt.HWND
u.FindWindowExW.argtypes = [wt.HWND, wt.HWND, wt.LPCWSTR, wt.LPCWSTR]
u.FindWindowExW.restype = wt.HWND
u.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
u.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
u.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
u.IsWindowVisible.argtypes = [wt.HWND]
u.EnumChildWindows.argtypes = [wt.HWND, ctypes.c_void_p, wt.LPARAM]
u.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
u.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_VISIBLE = 0x10000000
WS_DISABLED = 0x08000000
WS_CHILD = 0x40000000

ENUM_CB = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def class_name(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    u.GetClassNameW(hwnd, buf, 256)
    return buf.value


def window_text(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(256)
    u.GetWindowTextW(hwnd, buf, 256)
    return buf.value


def rect_of(hwnd):
    r = wt.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def dpi_of(hwnd) -> int:
    try:
        return int(u.GetDpiForWindow(hwnd))
    except AttributeError:
        dc = u.GetDC(hwnd)
        v = g.GetDeviceCaps(dc, _LOGPIXELSX)
        u.ReleaseDC(hwnd, dc)
        return int(v)


lines: list[str] = []


def out(text: str = "") -> None:
    lines.append(text)


def dump_tree(root, max_depth: int = 5) -> None:
    """广度优先把子窗口树打出来。EnumChildWindows 本身就递归，这里手动按层级走。"""
    level = [root]
    depth = 0
    while level and depth <= max_depth:
        nxt = []
        for parent in level:
            children = []

            def collect(hwnd, _lp):
                children.append(hwnd)
                return True

            cb = ENUM_CB(collect)
            u.EnumChildWindows(parent, ctypes.cast(cb, ctypes.c_void_p), 0)
            # EnumChildWindows 递归返回所有后代，只保留直接子级
            direct = []
            for hwnd in children:
                if u.GetParent(hwnd) == parent:
                    direct.append(hwnd)
            indent = "  " * (depth + 1)
            for hwnd in direct:
                st = u.GetWindowLongPtrW(hwnd, GWL_STYLE)
                vis = "V" if (st & WS_VISIBLE) else "."
                dis = "D" if (st & WS_DISABLED) else "."
                l, t, r, b = rect_of(hwnd)
                txt = window_text(hwnd)
                out(f"{indent}[{vis}{dis}] {class_name(hwnd):42s} "
                    f"({l:5d},{t:4d})-({r:5d},{b:4d})  {l:5d}..{r:5d} w={r-l:4d} h={b-t:4d}"
                    + (f'  "{txt}"' if txt else ""))
                nxt.append(hwnd)
        level = nxt
        depth += 1


def main() -> int:
    # 必须先声明 DPI 感知，否则拿到的坐标是被虚拟化过的
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:  # noqa: BLE001
        try:
            u.SetProcessDPIAware()
        except Exception:  # noqa: BLE001
            pass

    screen_w = u.GetSystemMetrics(0)
    screen_h = u.GetSystemMetrics(1)
    out(f"逻辑屏幕 = {screen_w}x{screen_h}（已声明 DPI 感知）")

    tray = u.FindWindowW("Shell_TrayWnd", None)
    out(f"Shell_TrayWnd = {tray}   rect={rect_of(tray) if tray else None}  "
        f"dpi={dpi_of(tray) if tray else 0}")
    if not tray:
        out("没有任务栏，后面都不用测了。")
        OUT.parent.mkdir(exist_ok=True)
        OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return 1

    # 任务栏自身尺寸 + 显示器工作区
    r = wt.RECT()
    u.GetWindowRect(tray, ctypes.byref(r))
    work = wt.RECT()
    u.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0)
    out(f"任务栏 rect   = ({r.left},{r.top})-({r.right},{r.bottom}) "
        f"高={r.bottom - r.top}")
    out(f"工作区 SPI_GETWORKAREA = ({work.left},{work.top})-({work.right},{work.bottom})")
    out(f"任务栏底边到屏幕底边 = {screen_h - r.bottom}")
    out()

    # 关键：开始按钮的猜测目标
    for cls in ("Start", "Shell_SecondaryTrayWnd", "TrayNotifyWnd", "ReBarWindow32",
                "MSTaskSwWClass", "MSTaskListWClass", "TaskListThumbnailWnd",
                "Windows.UI.Composition.DesktopWindowContentBridge",
                "Windows.UI.Core.CoreWindow", "TrayClockWClass"):
        h = u.FindWindowW(cls, None) or u.FindWindowExW(tray, None, cls, None)
        out(f"  FindWindow('{cls}') = {h}"
            + (f"  rect={rect_of(h)}" if h else ""))
    out()

    out("=== Shell_TrayWnd 子窗口树 ===")
    dump_tree(tray)
    out()

    # 任务栏上还有 SecondaryTrayWnd（多显示器）
    sec = u.FindWindowW("Shell_SecondaryTrayWnd", None)
    out(f"Shell_SecondaryTrayWnd = {sec}")
    if sec:
        dump_tree(sec)

    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"\n-> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
