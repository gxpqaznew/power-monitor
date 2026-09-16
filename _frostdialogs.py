"""真机抽查：用量统计窗 + 电价设置窗的底是不是也糊了。

⚠️ 这两个窗口**不能靠截屏判**：它们几乎被原生控件（Tab / ListView / 静态文本）
盖满，能看见的窗口底只有四周几个像素，而那几像素采到的通常是**控件自己的浅灰**
（Tab 控件就用系统灰刷自己刷底），看着和「没上毛玻璃」一模一样 —— 会得出错误结论。

正确做法是直接问绘制路径：往窗口发一条 ``WM_ERASEBKGND``、HDC 给一块**内存 DC**，
再把 DC 读回来。``_frost_bg`` 就是往这个 HDC 里填实色 + 盖毛玻璃的，所以读回来的
像素就是「窗口底本该长什么样」。为了让判据确定（不依赖背后桌面是不是纯色），这里
先把色调层换成醒目的纯红：底被染红 = 毛玻璃盖上了；还是系统灰 = 没盖上。
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path
from ctypes import wintypes

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import frost  # noqa: E402
from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.config import Config  # noqa: E402
from powermon.fee_dialog import FeeSettingsDialog  # noqa: E402
from powermon.roundwin import dib_section  # noqa: E402
from powermon.stats import StatsWindow  # noqa: E402
from powermon.w32 import gdi32, user32  # noqa: E402

WM_ERASEBKGND = 0x0014
PASS = FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


def pump(seconds: float) -> None:
    msg = wintypes.MSG()
    end = time.time() + seconds
    while time.time() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.02)


def read_erased_background(hwnd, w: int, h: int):
    """发 WM_ERASEBKGND 到一块内存 DC，读回像素。返回 (平均 RGB, 取值数, 返回值)。"""
    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    bmp, view = dib_section(dc, w, h)
    old = gdi32.SelectObject(dc, bmp)
    try:
        ret = user32.SendMessageW(hwnd, WM_ERASEBKGND, dc, 0)
        total = [0, 0, 0]
        n = 0
        seen = set()
        for i in range(0, w * h, 7):        # 抽样就够
            b, g, r = view[i * 4], view[i * 4 + 1], view[i * 4 + 2]
            seen.add((r, g, b))
            total[0] += r
            total[1] += g
            total[2] += b
            n += 1
        mean = tuple(round(t / max(1, n)) for t in total)
        return mean, len(seen), ret
    finally:
        gdi32.SelectObject(dc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(dc)
        user32.ReleaseDC(None, screen)


def probe(cls_name: str, maker, cache_key: str) -> None:
    win = maker()
    # ⚠️ 必须先 create() 再改色调层：`create()` 里会把 `_frost_tint` 初始化成真实
    # 底色，先改后 create 就被覆盖回去了（第一版就是这么写的，红色没生效，白判一轮）
    if not win.create():
        check(f"{cls_name} 窗口建得出来", False, "create() 失败")
        return
    win._frost_tint = (255, 0, 0)      # 醒目的色调层：红蓝差就是「有没有盖」的判据
    win._frost = 220
    win.show()
    pump(0.9)
    if not win._hwnd:
        check(f"{cls_name} 窗口建得出来", False, "_hwnd 为空")
        return
    check(f"{cls_name} 显示前就把毛玻璃暖好了", frost.has(cache_key),
          f"缓存键 {list(frost._entries)}")

    rect = wintypes.RECT()
    user32.GetClientRect(win._hwnd, ctypes.byref(rect))
    w, h = min(rect.right, 300), min(rect.bottom, 200)
    mean, kinds, ret = read_erased_background(win._hwnd, w, h)
    print(f"{cls_name}: 客户区 {rect.right}x{rect.bottom}，"
          f"擦背景返回 {ret}，读回底色平均 RGB {mean}，取值 {kinds} 种")
    check(f"{cls_name} 自己接管了擦背景（返回 1）", ret == 1, f"返回 {ret}")
    check(f"{cls_name} 底色被色调层染红（毛玻璃真的盖上了）",
          mean[0] - mean[2] > 60, f"R-B = {mean[0] - mean[2]}")
    check(f"{cls_name} 底色不是死色一块", kinds > 4, f"{kinds} 种取值")
    if hasattr(win, "destroy"):
        win.destroy()
    pump(0.4)


class _FakeMeter:
    """StatsWindow 只用到 meter 的这两样。"""

    def __init__(self) -> None:
        self.cfg = Config()

    def days(self):
        return []

    def sessions(self):
        return []


def main() -> int:
    enable_dpi_awareness()
    frost.reset()

    probe("用量统计", lambda: StatsWindow(_FakeMeter(), Config()), "stats")
    frost.reset()
    probe("电价设置", lambda: FeeSettingsDialog(Config()), "fee")

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
