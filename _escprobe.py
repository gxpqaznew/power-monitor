"""探针：托盘右键弹出菜单之后，ESC 到底能不能关掉？连打 5 轮看稳定不稳定。

背景：`_traylive.py` 里「ESC 关掉菜单」这一项时好时坏。可能是
  a) 真 bug（菜单没进 _chain / close_all 没覆盖到），
  b) 竞态（菜单窗口刚 CreateWindowEx 出来、还没 ShowWindow，
     FindWindowW 就把它捞到了，此刻发 ESC 落在一个半成品状态上）。

这个探针把「找到窗口 → 发 ESC → 看还在不在」重放 5 次，并且每轮多等一下，
把两种可能性区分开。

用法：python _escprobe.py [exe 关键字]
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _traylive as live  # noqa: E402

WM_CLOSE = 0x0010


def menus() -> list[int]:
    found = []

    from powermon.w32 import wintypes, user32

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lp):
        if live._class_of(hwnd) == live.MENU_CLASS:
            found.append(hwnd)
        return True

    user32.EnumWindows(cb, 0)
    return found


def main() -> int:
    keyword = sys.argv[1] if len(sys.argv) > 1 else "能耗统计"
    live.enable_dpi_awareness()

    procs = live.processes(keyword)
    if not procs:
        print(f"找不到实例（{keyword}）")
        return 1
    pids = {p for p, _n in procs}
    tray = live.find_window_by_class(pids, live.TRAY_CLASS)
    rect = live.icon_rect(tray) if tray else None
    if rect is None:
        print("拿不到图标矩形 —— 图标得设成「常驻任务栏」")
        return 1
    cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
    print(f"图标 {rect} → 点 ({cx},{cy})")

    # 对照实验：settle=0.0 是一发现窗口就发 ESC（复现 _traylive 的失败）；
    # settle=0.6 是等菜单把 __init__（含毛玻璃抓屏 + ShowWindow）跑完再发。
    tally: dict[float, list[int]] = {0.0: [0, 0], 0.6: [0, 0]}   # [关掉了, 没关掉]
    for settle in (0.0, 0.6):
        for round_no in range(1, 6):
            for h in menus():
                live.user32.PostMessageW(h, WM_CLOSE, 0, 0)
            live.pump(0.4)
            live.user32.SetCursorPos(cx, cy - 200)
            live.pump(0.15)

            live.click(cx, cy, right=True)
            menu = live.wait_window(live.MENU_CLASS, 2.5)
            if not menu:
                print(f"  settle={settle} 第 {round_no} 轮：右键没弹出菜单 —— 跳过")
                continue

            if settle:
                live.pump(settle)
            n_before = len(menus())
            live.user32.SendMessageW(menu, live.WM_KEYDOWN, live.VK_ESCAPE, 0)
            live.pump(0.5)
            alive = bool(live.user32.IsWindow(menu))
            tally[settle][1 if alive else 0] += 1
            print(f"  settle={settle} 第 {round_no} 轮：菜单 {menu} · "
                  f"ESC 前菜单数 {n_before} → 后 {len(menus())} · IsWindow={alive}")

    print()
    for settle, (closed, stuck) in tally.items():
        print(f"settle={settle}：关掉 {closed} 次 / 没关掉 {stuck} 次")

    for h in menus():
        live.user32.PostMessageW(h, WM_CLOSE, 0, 0)
    live.pump(0.3)
    print("探针结束。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
