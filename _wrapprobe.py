"""折行探针：把「15 项全勾」在各字号下需要几行、每行多宽打出来。

用来回答一个具体问题：本机任务栏可用宽度只有 883px，15 个字段到底几行能排完？
在有答案之前不应该猜（改行高 / 改上限都是瞎调）。

跑法：python _wrapprobe.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import stripopts                          # noqa: E402
from powermon.config import Config                       # noqa: E402
from powermon.strip import _DENSITY, _PAD_X, _ROW_H, TaskbarStrip  # noqa: E402
from powermon.w32 import gdi32, user32                    # noqa: E402


class FakeSnap:
    current_w = 188.0
    cpu_w = 142.0
    gpu_w = 320.0
    base_w = 35.0
    session_wh = 1888.8
    session_cost = 88.88
    today_wh = 8888.0
    today_cost = 8.88
    average_w = 188.0
    peak_w = 320.0
    month_wh = 88888.0
    total_wh = 888888.0
    total_cost = 888.88
    segment = "平段"
    gpu_names = ["NVIDIA GeForce RTX 3080"]
    gpu_limits = [320.0]
    power_on_seconds = 88888.0


SCALE = 1.25
BAR_H = int(round(48 * SCALE))
LIMIT = 883
BUDGET = int(round(BAR_H * 0.94))


def main() -> int:
    cfg = Config()
    cfg.strip_fields = list(stripopts.FIELD_KEYS)
    strip = TaskbarStrip(cfg)
    snap = FakeSnap()

    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    try:
        pad_x = _PAD_X * SCALE * stripopts.size_spec(cfg)[1]
        print(f"可用 {LIMIT}px，内边距 {pad_x * 2:.0f}px，一行内容宽 "
              f"{LIMIT - int(round(pad_x * 2))}px，高度预算 {BUDGET}px\n")

        for density in range(len(_DENSITY)):
            compact, div_mult, gap_mult = _DENSITY[density]
            sections = stripopts.sections(snap, cfg, compact=compact)
            gap_label = 6.0 * SCALE * gap_mult
            gap_unit = 3.0 * SCALE * gap_mult
            div_margin = 13.0 * SCALE * div_mult
            content = LIMIT - int(round(pad_x * 2))
            print(f"===== 密度档 {density}  compact={compact} "
                  f"分隔x{div_mult} 间隙x{gap_mult} =====")
            print(f"{'字号':>6} {'行高':>6} {'最大行数':>8} {'实际行数':>8} "
                  f"{'最宽一行':>9} {'总高':>6}")
            for fs in stripopts.FONT_SCALE_VALUES:
                fonts = strip._fonts_for(SCALE, fs)
                rows = strip._pack(dc, sections, content, gap_label, gap_unit,
                                   div_margin, fonts)
                w = max(strip._row_width(dc, r, gap_label, gap_unit,
                                         div_margin, fonts) for r in rows)
                row_h = _ROW_H * SCALE * fs
                max_rows = max(1, min(3, int(BUDGET // row_h)))
                flag = "  <- 装得下" if len(rows) <= max_rows else ""
                print(f"{fs:>6.2f} {row_h:>6.1f} {max_rows:>8} {len(rows):>8} "
                      f"{w:>9} {row_h * len(rows):>6.0f}{flag}")
            print()

        # ---- 真·走 _plan，看最终选了什么 ----
        print("===== 实际计划（15 项全勾）=====")
        for limit in (883, 1200, 10 ** 6):
            strip._limit = limit
            strip._max_height = BUDGET
            plan = strip._plan(dc, snap, SCALE, limit, BUDGET)
            names = " | ".join(" ".join(x[1] for x in r) for r in plan["rows"])
            print(f"  可用 {limit:>7}px → {plan['placed']}/{plan['total']} 项，"
                  f"{len(plan['rows'])} 行，字号 {plan['font_scale']}，"
                  f"密度档 {plan['density']}，行高 {plan['row_h']:.1f}")
            print(f"      {names}")

        strip._limit = LIMIT
        for n in (4, 6, 8, 10, 12, 15):
            strip.cfg.strip_fields = list(stripopts.FIELD_KEYS)[:n]
            strip._max_height = int(round(BAR_H * 0.78))
            plan = strip._plan(dc, snap, SCALE, LIMIT, BUDGET)
            print(f"  勾 {n:>2} 项 → 排上 {plan['placed']:>2} 项，"
                  f"{len(plan['rows'])} 行，字号 {plan['font_scale']}，"
                  f"密度档 {plan['density']}")
    finally:
        gdi32.DeleteDC(dc)
        user32.ReleaseDC(None, screen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
