"""探针：托盘右键之后，菜单到底是「从来没出现」还是「出现了一下就被关掉」？

背景：`_traylive.py` 里「右键托盘图标 → 菜单弹出来」这项**时好时坏**（约 50%）。
两种可能，修法完全不同：

  a) 回调没打过来 / popup 没执行 → 从来没出现；
  b) 菜单建出来了，但紧接着被 `WM_KILLFOCUS` 收掉（ctxmenu 的策略是「点别处就关」）
     → 出现过，只是我们 50ms 一次的轮询没抓到，或者它存活 < 一次轮询。

做法：右键之后**每 20ms** 采样一次「屏幕上有几个菜单窗口」，把时间线打出来。

用法：python _menuflakeprobe.py [次数]
"""

from __future__ import annotations

import ctypes
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _traylive as live  # noqa: E402
from powermon.w32 import user32  # noqa: E402


def main() -> int:
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    keyword = next((a for a in sys.argv[2:] if not a.startswith("--")), "能耗统计")
    after_panel = "--after-panel" in sys.argv
    live.enable_dpi_awareness()

    procs = live.processes(keyword)
    if not procs:
        print("找不到实例")
        return 1
    pids = {p for p, _n in procs}
    tray = live.find_window_by_class(pids, live.TRAY_CLASS)
    rect = live.icon_rect(tray) if tray else None
    if rect is None:
        print("拿不到图标矩形")
        return 1
    cx, cy = (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2
    print(f"图标 {rect} → 点 ({cx},{cy})")
    print(f"模式：{'双击出卡片 → 关掉卡片 → 再右键（复现 _traylive 的次序）' if after_panel else '直接右键'}\n")

    opened = died_early = stayed = 0
    for rnd in range(1, rounds + 1):
        live.close_menus_now()
        panel = user32.FindWindowW(live.PANEL_CLASS, None)
        if panel and user32.IsWindowVisible(panel):
            user32.PostMessageW(panel, live.WM_CLOSE, 0, 0)
            live.pump(0.4)

        if after_panel:
            # 完全照 _traylive 的次序来
            user32.SetCursorPos(cx, cy - 200)
            live.pump(0.2)
            live.click(cx, cy, count=2)
            panel = live.wait_window(live.PANEL_CLASS, 2.5)
            vis = bool(panel and user32.IsWindowVisible(panel))
            if vis:
                user32.PostMessageW(panel, live.WM_CLOSE, 0, 0)
                live.pump(0.5)

        user32.SetCursorPos(cx, cy - 200)
        live.pump(0.2)

        live.click(cx, cy, right=True)
        t0 = time.monotonic()
        timeline: list[str] = []
        seen = 0
        first_at = None
        closed_at = None
        while time.monotonic() - t0 < 3.0:
            n = len(live.windows_of_class(live.MENU_CLASS))
            dt = time.monotonic() - t0
            if n and first_at is None:
                first_at = dt
            if first_at is not None and n == 0 and closed_at is None:
                closed_at = dt
            seen = max(seen, n)
            timeline.append(f"{dt:.2f}s:{n}")
            live.pump(0.02)
        marks = [timeline[0]]
        for prev, cur in zip(timeline, timeline[1:]):
            if prev.split(":")[1] != cur.split(":")[1]:
                marks.append(cur)
        # 判定：出现过 → 打开；出现后 1.5 秒内就没了 → 早夭（用户看到的是「弹不出来」）
        if first_at is None:
            verdict = "从未出现"
        elif closed_at is not None and closed_at < 1.5:
            verdict = f"早夭（{closed_at:.2f}s 就没了）"
        else:
            verdict = "正常"
        opened += 1 if first_at is not None else 0
        died_early += 1 if verdict.startswith("早夭") else 0
        stayed += 1 if verdict == "正常" else 0
        extra = f"（面板可见={vis}）" if after_panel else ""
        print(f"第 {rnd:>2} 轮：最多同时 {seen} 个窗口 · {verdict}{extra}")
        print("        " + "  ".join(marks[:12]) + ("  …" if len(marks) > 12 else ""))

    print(f"\n合计 {rounds} 轮：正常 {stayed} · 早夭 {died_early} · 从未出现 {rounds - opened}")
    live.close_menus_now()
    return 1 if (died_early or opened != rounds) else 0


if __name__ == "__main__":
    raise SystemExit(main())
