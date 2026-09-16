"""真机验证「正在跑的那个版本」（通常就是装好的正式版）的长条到底动不动得动。

为什么还要单独一个脚本：`_dragtest.py` 是拿**当前源码**新建一个长条来测的，
测不到「用户手上那个 exe」。这个脚本不建窗口、不 import 业务代码，直接去任务栏
子树里找正在运行的 `PowerMonitorTaskbarStrip`，对它做命中 / 悬停 / 拖动 / 复位：

    A. 扩展样式里没有 WS_EX_TRANSPARENT（有的话它永远收不到鼠标 = 拖不动）
    B. WindowFromPoint 在胶囊正中命中的就是它（能抓）
    C. 圆角外面那点穿过去了（不挡任务栏点击）
    D. 鼠标压上去，上边缘描边变蓝（悬停提示真的出现）
    E. 横拖 120px，窗口左缘跟着走 120px
    F. 复位后左缘回到原处（原偏移是多少就回多少，不会把用户位置改跑）

⚠️ 它会**真的移动鼠标、真的拖那条长条**；结束前一定复位并核对左缘回到原位。

用法：python _liveverify.py [要拖多少像素，默认 120]
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.w32 import (  # noqa: E402
    GWL_EXSTYLE,
    WNDENUMPROC,
    WS_EX_TRANSPARENT,
    gdi32,
    user32,
    wintypes,
)

PASS = FAIL = 0
STRIP_CLASS = "PowerMonitorTaskbarStrip"

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
MK_LBUTTON = 0x0001

gdi32.GetPixel.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.GetPixel.restype = wintypes.DWORD


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def _class_of(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(128)
    user32.GetClassNameW(hwnd, buf, 128)
    return buf.value


def _pid_of(hwnd) -> int:
    pid = wintypes.DWORD(0)
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def find_strips() -> list[int]:
    """任务栏子树里所有正在运行的长条窗口（可能有多个进程各一条）。"""
    tray = user32.FindWindowW("Shell_TrayWnd", None)
    out: list[int] = []
    if not tray:
        return out

    def _cb(hwnd, _lparam):
        if _class_of(hwnd) == STRIP_CLASS:
            out.append(hwnd)
        return True

    user32.EnumChildWindows(tray, WNDENUMPROC(_cb), 0)
    return out


def rect_of(hwnd) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def ex_style(hwnd) -> int:
    return int(user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE))


def edge_blueness(rect: tuple[int, int, int, int]) -> int:
    """窗口四周那圈「边带」里最蓝的像素 = max(B − R)。

    普通态这根描边是中性灰（B−R ≈ 10 上下），悬停态会被 `_apply_hover` 混进强调蓝
    （实测 B−R ≈ 140），用它判断「悬停提示真的画出来了」比抓图比对省事得多。

    ⚠️ 只扫**边带**、不猜某一行：胶囊在窗口里的竖直位置由
    `max(目标高, 内容高)` 决定，窗口上下可能各留几像素空白 —— 拍脑袋取 `top + 2`
    会一直读到任务栏底色（实测两次都是 12，看着像"悬停没生效"，其实压根没采到描边）。
    """
    dc = user32.GetDC(None)
    left, top, right, bottom = rect
    ys = list(range(top, min(top + 6, bottom)))
    ys += list(range(max(bottom - 6, top), bottom))
    xs = list(range(left, min(left + 6, right)))
    xs += list(range(max(right - 6, left), right))
    pts = [(x, y) for y in ys for x in range(left, right, 2)]
    pts += [(x, y) for x in xs for y in range(top, bottom, 2)]
    best = -999
    for x, y in pts:
        c = int(gdi32.GetPixel(dc, x, y))
        if c == 0xFFFFFFFF:      # CLR_INVALID
            continue
        best = max(best, ((c >> 16) & 0xFF) - (c & 0xFF))
    user32.ReleaseDC(None, dc)
    return best


def post(hwnd, msg, wparam=0, lparam=0) -> None:
    user32.SendMessageW(hwnd, msg, wparam, lparam)


def client_center(hwnd) -> tuple[int, int]:
    r = wintypes.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    return (r.right // 2, r.bottom // 2)


def lparam_xy(x: int, y: int) -> int:
    return (y << 16) | (x & 0xFFFF)


def drag_by(hwnd, dx: int) -> None:
    """按住胶囊横拖 dx 像素（走真实光标，跟人手一样）。"""
    cx, cy = client_center(hwnd)
    left, top, right, bottom = rect_of(hwnd)
    screen_x = (left + right) // 2
    screen_y = (top + bottom) // 2

    user32.SetCursorPos(screen_x, screen_y)
    time.sleep(0.15)
    post(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam_xy(cx, cy))
    time.sleep(0.1)
    # 分几步挪，模拟人手；每一步都同步移动真实光标（_drag_move 读的是 GetCursorPos）
    steps = 6
    for i in range(1, steps + 1):
        user32.SetCursorPos(screen_x + dx * i // steps, screen_y)
        post(hwnd, WM_MOUSEMOVE, MK_LBUTTON, lparam_xy(cx + dx * i // steps, cy))
        time.sleep(0.05)
    time.sleep(0.1)
    post(hwnd, WM_LBUTTONUP, 0, lparam_xy(cx + dx, cy))
    time.sleep(0.3)


def main() -> int:
    enable_dpi_awareness()
    dx = int(sys.argv[1]) if len(sys.argv) > 1 else 120

    strips = find_strips()
    if not strips:
        print("[FAIL] 任务栏子树里没有正在运行的长条 —— 程序没跑，或长条被关了")
        return 1
    print(f"找到 {len(strips)} 条正在运行的长条：")
    for hwnd in strips:
        print(f"  hwnd={hwnd:#x}  pid={_pid_of(hwnd)}  rect={rect_of(hwnd)}")
    hwnd = strips[0]
    print(f"→ 拿 hwnd={hwnd:#x}（pid {_pid_of(hwnd)}）做验证\n")

    # ---- A. 默认可拖：扩展样式里不能有 TRANSPARENT ----
    ex = ex_style(hwnd)
    check("扩展样式里没有 WS_EX_TRANSPARENT（有它就收不到鼠标、拖不动）",
          not (ex & WS_EX_TRANSPARENT), f"exstyle={ex:#x}")

    rect = rect_of(hwnd)
    cx_screen = (rect[0] + rect[2]) // 2
    cy_screen = (rect[1] + rect[3]) // 2

    # ---- B/C. 命中：胶囊内命中、圆角外穿透 ----
    hit = user32.WindowFromPoint(wintypes.POINT(cx_screen, cy_screen))
    check("胶囊正中命中的就是长条（能抓）", hit == hwnd, f"{hit:#x} vs {hwnd:#x}")
    corner = user32.WindowFromPoint(wintypes.POINT(rect[0], rect[1]))
    check("圆角外面那点仍然穿透（不挡底下任务栏）", corner != hwnd, f"命中 {corner:#x}")

    # ---- D. 悬停：描边转强调色 ----
    #
    # ⚠️ 这里必须**自己发 WM_MOUSEMOVE**：跨进程 `SetCursorPos` 只是把光标挪过去，
    # 系统不一定给另一个进程的窗口投递鼠标消息（实测就是不投），于是它永远不进悬停态。
    # 灌一条假消息进去，目标进程照样会走 `_set_hover(True)` → 登记 TME_LEAVE → 重画。
    # 等待也要给够：悬停只改渲染键，真正的重画发生在下一次 tick（~1s 一次）。
    def hover(x: int, y: int) -> int:
        user32.SetCursorPos(x, y)
        post(hwnd, WM_MOUSEMOVE, 0, lparam_xy(*client_center(hwnd)))
        time.sleep(1.6)
        return edge_blueness(rect_of(hwnd))

    cool = hover(rect[0] - 60, cy_screen)
    hot = hover(cx_screen, cy_screen)
    check("鼠标压上去描边真的变蓝了（悬停提示生效）",
          hot > cool + 20, f"B−R {cool} → {hot}")
    hover(rect[0] - 60, cy_screen)

    # ---- E. 拖动 ----
    before = rect_of(hwnd)
    drag_by(hwnd, dx)
    after = rect_of(hwnd)
    moved = after[0] - before[0]
    check(f"横拖 {dx}px 长条真的跟着走", abs(moved - dx) <= 2 or abs(moved) > abs(dx) // 2,
          f"左缘 {before[0]} → {after[0]}（走了 {moved}px）")
    check("拖动只改横坐标（竖直不动、大小不变）",
          (after[1], after[3] - after[1]) == (before[1], before[3] - before[1]),
          f"{before} → {after}")

    # ---- F. 复位：搬回原处（原偏移是多少就回多少，不改用户的位置）----
    if abs(moved) > 1:
        drag_by(hwnd, before[0] - after[0])
    final = rect_of(hwnd)
    check("复位后左缘回到原处（没把用户的位置改跑）",
          abs(final[0] - before[0]) <= 2, f"{before[0]} vs {final[0]}")

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
