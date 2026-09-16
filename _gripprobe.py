"""诊断：长条的「拖动把手」能不能收到鼠标。

长条整体是 ``WS_EX_LAYERED | WS_EX_TRANSPARENT`` 的**子窗口**（嵌在 Shell_TrayWnd
里），这个组合本身就是为了「穿透点击、不抢任务栏的右键菜单」。要加拖动，就必须在
两端留出一小块**不穿透**的区域，而这块区域到底能不能命中鼠标，是纯 Windows
命中测试（hit-test）行为的问题 —— 文档没写清楚，只能实测：

  A. 把把手做成长条的**子窗口**（长条自己是透明的）；父窗口的 WS_EX_TRANSPARENT
     会不会把子窗口一起带成穿透？
  B. 做成任务栏的**兄弟窗口**（透明的是旁边那个，不是自己），命不命中？

用 ``WindowFromPoint`` 直接问系统「这一点上是哪个窗口」——不需要真的动鼠标，
也就不会打扰用户正在用的桌面。跑法：python _gripprobe.py
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import taskbar, w32  # noqa: E402
from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.config import Config  # noqa: E402
from powermon.strip import TaskbarStrip  # noqa: E402
from powermon.w32 import gdi32, kernel32, user32, wintypes  # noqa: E402

user32.WindowFromPoint.argtypes = [wintypes.POINT]
user32.WindowFromPoint.restype = wintypes.HWND
user32.GetParent.argtypes = [wintypes.HWND]
user32.GetParent.restype = wintypes.HWND

GRIP_CLASS = "PowerMonitorGripProbe"
_grip_proc_ref = None


@w32.WNDPROC
def _grip_proc(hwnd, msg, wparam, lparam):
    if msg == 0x0014:      # WM_ERASEBKGND
        return 1
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def make_class():
    global _grip_proc_ref
    _grip_proc_ref = _grip_proc
    hinstance = kernel32.GetModuleHandleW(None)
    wc = w32.WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(w32.WNDCLASSEXW)
    wc.lpfnWndProc = _grip_proc
    wc.hInstance = hinstance
    wc.lpszClassName = GRIP_CLASS
    user32.RegisterClassExW(ctypes.byref(wc))
    return hinstance


def who(pt) -> int:
    return user32.WindowFromPoint(pt)


def name_of(hwnd: int) -> str:
    if not hwnd:
        return "None"
    buf = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, buf, 128)
    return f"{hwnd:#x} [{buf.value}]"


def main() -> int:
    enable_dpi_awareness()
    info = taskbar.taskbar()
    print(f"taskbar = {info}")
    if info is None:
        print("没有任务栏（explorer 没跑？）—— 这个探针无效")
        return 1

    cfg = Config()
    cfg.strip_enabled = True
    strip = TaskbarStrip(cfg)
    from _striplive import make_snapshot  # noqa: E402

    snap = make_snapshot(cfg, time.time())
    if not strip.create(snap):
        print("长条建不出来")
        return 1
    strip.tick(snap)
    time.sleep(0.3)
    bar_hwnd, trect, _dpi = info
    left, top, right, bottom = strip._rect
    print(f"长条 rect = {strip._rect}  hwnd = {name_of(strip.hwnd)}")
    print(f"任务栏 hwnd = {name_of(bar_hwnd)}  parent(长条) = {name_of(user32.GetParent(strip.hwnd))}")

    # 长条中点（透明区）：应该命中的**不是**长条自己
    mid = wintypes.POINT((left + right) // 2, (top + bottom) // 2)
    hit_mid = who(mid)
    print(f"\n[透明区] 点 {mid.x},{mid.y} -> {name_of(hit_mid)}"
          f"  {'❌ 命中了长条自己（会吃掉任务栏点击）' if hit_mid == strip.hwnd else '✅ 穿透'}")

    hinstance = make_class()
    w = 16
    ex = 0x00080000 | 0x08000000 | 0x00000080   # LAYERED | NOACTIVATE | TOOLWINDOW
    # ---- A: 把手 = 长条的子窗口 ----
    a_left, a_top = 0, 0
    hwnd_a = user32.CreateWindowExW(
        ex, GRIP_CLASS, "gripA", w32.WS_CHILD | w32.WS_VISIBLE,
        a_left, a_top, w, bottom - top, strip.hwnd, None, hinstance, None,
    )
    pt_a = wintypes.POINT(left + w // 2, (top + bottom) // 2)
    hit_a = who(pt_a)
    print(f"\n[A 子窗口] hwnd={name_of(hwnd_a)} parent={name_of(user32.GetParent(hwnd_a))}")
    print(f"  点 {pt_a.x},{pt_a.y} -> {name_of(hit_a)}"
          f"  {'✅ 命中把手' if hit_a == hwnd_a else '❌ 没命中'}")

    # ---- B: 把手 = 任务栏的兄弟窗口 ----
    hwnd_b = user32.CreateWindowExW(
        ex, GRIP_CLASS, "gripB", w32.WS_CHILD | w32.WS_VISIBLE,
        left - bar_hwnd * 0, 0, w, bottom - top, bar_hwnd, None, hinstance, None,
    )
    # 先按任务栏客户区坐标摆到长条左端
    cl, ct, _cr, _cb = strip._client_rect(strip._rect)
    user32.SetWindowPos(hwnd_b, 0, cl, ct, w, bottom - top, 0x0004 | 0x0010)
    pt_b = wintypes.POINT(left + w // 2, (top + bottom) // 2)
    hit_b = who(pt_b)
    print(f"\n[B 兄弟窗口] hwnd={name_of(hwnd_b)} parent={name_of(user32.GetParent(hwnd_b))}")
    print(f"  点 {pt_b.x},{pt_b.y} -> {name_of(hit_b)}"
          f"  {'✅ 命中把手' if hit_b == hwnd_b else '❌ 没命中'}")

    # ---- C: 子窗口放在长条外面一点点（背景是任务栏）也会穿透吗 ----
    pt_c = wintypes.POINT(left - 30, (top + bottom) // 2)
    print(f"\n[长条左边 30px] 点 {pt_c.x},{pt_c.y} -> {name_of(who(pt_c))}")

    for hwnd in (hwnd_a, hwnd_b):
        if hwnd:
            user32.DestroyWindow(hwnd)
    strip.destroy()
    print("\n已清理")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
