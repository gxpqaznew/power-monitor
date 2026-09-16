"""诊断：长条渲染出来的像素里，分隔线到底是什么颜色、落在哪些 x。

为什么要留着这个脚本：`pal` 里存的是 **COLORREF**（低字节是 R），而长条的
DIB 是 **BGRA**（低字节是 B）。这两个顺序搞反过一次 —— 结果「一条分隔线都
没找到」，而空集合让「两行的分隔线在同一批 x 上」这条断言恒真，看着 PASS
其实什么都没验（`_striplive.check_alignment` 现在专门挡了这种情况）。
真要对颜色/像素下判断时，先用这个脚本把实际值打出来。
"""

from __future__ import annotations

import ctypes
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import stripopts  # noqa: E402
from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.config import Config  # noqa: E402
from powermon.strip import TaskbarStrip  # noqa: E402

enable_dpi_awareness()
from _striplive import make_snapshot  # noqa: E402

cfg = Config()
cfg.strip_enabled = True
cfg.strip_theme = "auto"
cfg.strip_fields = list(stripopts.FIELD_KEYS)
snap = make_snapshot(cfg, time.time())
strip = TaskbarStrip(cfg)
if not strip.create(snap):
    print("建不出来")
    raise SystemExit(1)
strip.tick(snap)
time.sleep(0.3)
strip._ensure_above_siblings(force=True)
strip._render_if_needed(snap)

w, h = strip._w, strip._h
view = strip._view
pal = strip._pal
print(f"w={w} h={h} rows={strip._plan_rows} content_h={strip._plan_height}")
print("pal:", {k: (f"#{v:06x}" if isinstance(v, int) else v)
               for k, v in pal.items() if k in ("bg", "div", "ink", "dim", "unit", "alpha")})

band_y = int(strip._plan_height / strip._plan_rows * 0.5)   # 第一行中线
row = [(view[(band_y * w + x) * 4], view[(band_y * w + x) * 4 + 1],
        view[(band_y * w + x) * 4 + 2]) for x in range(w)]
count = Counter(row)
print(f"第 {band_y} 行最常见的 8 种颜色（BGR）：")
for color, n in count.most_common(8):
    print(f"   {color}  x{n}")
div_bgr = (pal["div"] & 0xFF, (pal["div"] >> 8) & 0xFF, (pal["div"] >> 16) & 0xFF)
print(f"pal['div'] 的 BGR = {div_bgr}，出现 {count.get(div_bgr, 0)} 次")
# 分隔线候选：单独成列、上下都有
cands = [x for x in range(w)
         if all((view[(y * w + x) * 4], view[(y * w + x) * 4 + 1],
                 view[(y * w + x) * 4 + 2]) == div_bgr
                for y in range(band_y - 3, band_y + 4))]
print(f"中线上下 7px 都是 div 色的 x = {cands}")
strip.destroy()
