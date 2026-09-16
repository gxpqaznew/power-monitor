"""探针：真机右键**长条**（不是托盘图标），看菜单弹不弹、会不会立刻消失。

为什么需要它：托盘图标的真机点击只能在**已常驻任务栏**的那份安装版上验
（dev 版是 `pythonw.exe`，图标落在 `^` 溢出里，`Shell_NotifyIconGetRect` 报出来的
坐标其实点不到东西 —— 踩过：探针 10/10「从未出现」，查半天才发现是点空了）。
而长条是任务栏的**子窗口**，一定点得到，所以用它来验菜单这条链路，
顺带覆盖「拖后抢前台 + 落座宽限」这套改动。

用法：python _stripmenuprobe.py [轮数] [exe 关键字]
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _traylive as live  # noqa: E402
from powermon.w32 import GWL_EXSTYLE, WS_EX_TRANSPARENT, user32  # noqa: E402

STRIP_CLASS = "PowerMonitorTaskbarStrip"


def find_strip(hwnds_of_class: str):
    """长条是 Shell_TrayWnd 的**子窗口**，必须用 EnumChildWindows 找。

    （`_traylive.find_window_by_class` 走的是 EnumWindows，只枚举顶层窗口，
    对长条永远返回 None —— 第一版探针就栽在这儿。）
    """
    tray = user32.FindWindowW("Shell_TrayWnd", None)
    if not tray:
        return None
    found: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _lp):
        buf = ctypes.create_unicode_buffer(128)
        user32.GetClassNameW(h, buf, 128)
        if buf.value == hwnds_of_class:
            found.append(h)
        return True

    user32.EnumChildWindows(tray, cb, 0)
    return found[0] if found else None


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    keyword = next((a for a in sys.argv[2:] if not a.startswith("--")), "pythonw")
    live.enable_dpi_awareness()

    procs = live.processes(keyword)
    if not procs:
        print(f"找不到实例（{keyword}）")
        return 1
    print(f"实例：{procs}")

    strip = find_strip(STRIP_CLASS)
    if not strip:
        print("找不到长条窗口（任务栏子树里没有 " + STRIP_CLASS + "）")
        return 1
    r = wintypes.RECT()
    user32.GetWindowRect(strip, ctypes.byref(r))
    ex = user32.GetWindowLongPtrW(strip, GWL_EXSTYLE) & 0xFFFFFFFF
    print(f"长条 {strip} rect=({r.left},{r.top},{r.right},{r.bottom}) exStyle=0x{ex:X}")
    if ex & WS_EX_TRANSPARENT:
        print("[SKIP] 长条处于「锁定位置」（WS_EX_TRANSPARENT = 故意点击穿透），"
              "在长条上右键本来就落到任务栏上，这项判据不适用。")
        return 0

    cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    print(f"点长条正中 ({cx},{cy})\n")

    ok = died = missed = 0
    for i in range(1, rounds + 1):
        live.close_menus_now()
        user32.SetCursorPos(r.left - 300, cy)
        live.pump(0.25)
        live.click(cx, cy, right=True)
        t0 = time.monotonic()
        seen = 0
        first = closed = None
        while time.monotonic() - t0 < 3.0:
            n = len(live.windows_of_class(live.MENU_CLASS))
            dt = time.monotonic() - t0
            if n and first is None:
                first = dt
            if first is not None and n == 0 and closed is None:
                closed = dt
            seen = max(seen, n)
            live.pump(0.02)
        if first is None:
            verdict = "从未出现"
            missed += 1
        elif closed is not None and closed - first < 1.5:
            verdict = f"早夭（{closed - first + first:.2f}s 就没了，存活 {closed - first:.2f}s）"
            died += 1
        else:
            verdict = "正常"
        print(f"第 {i} 轮：最多 {seen} 个 · {verdict}"
              + (f" · 首次出现 {first:.2f}s" if first else ""))
        ok += 1 if verdict == "正常" else 0

    print(f"\n合计 {rounds} 轮：正常 {ok} · 早夭 {died} · 从未出现 {missed}")
    live.close_menus_now()
    user32.SetCursorPos(20, 20)
    return 0 if (ok == rounds) else 1


if __name__ == "__main__":
    raise SystemExit(main())
