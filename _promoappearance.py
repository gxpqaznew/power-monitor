"""宣传片素材：渲染「长条外观」窗口。

与 ``_appearancelive.py`` 的区别：那个用测试夹具 ``FakeSnap``（数值是刻意
夸张的 188W / ¥88.88，一眼假）。宣传片里要的是**真实配置 + 合理读数**，
所以这里读用户自己的 config，把预览数值换成一台真实机器的量级。

输出：``_preview/appearance_promo.png``
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _appearancelive import grab_screen, pump, write_png  # noqa: E402
from _appearanceshot import Applied  # noqa: E402

from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.appearance import AppearanceWindow  # noqa: E402
from powermon.config import Config  # noqa: E402
from powermon.strip import TaskbarStrip  # noqa: E402
from powermon.w32 import user32  # noqa: E402

OUT = Path(__file__).resolve().parent / "_preview"


class PromoSnap:
    """预览里的数值：一台真实机器的合理量级（与 _promoshot.py 的口径一致）。"""

    current_w = 137.0
    cpu_w = 64.0
    gpu_w = 41.0
    base_w = 32.0
    session_wh = 1284.6
    session_cost = 0.96
    today_wh = 1284.0
    today_cost = 0.96
    average_w = 124.0
    peak_w = 418.0
    month_wh = 45680.0
    total_wh = 456800.0
    total_cost = 34.26
    segment = "平段"
    gpu_names = ["NVIDIA GeForce RTX 3080"]
    power_on_seconds = 21600.0


def main() -> int:
    enable_dpi_awareness()
    OUT.mkdir(exist_ok=True)

    # Config.load() 才会读盘；Config() 只有默认值（4 个字段 / 字号 1.0），
    # 那样预览和任务栏上真实那条长条对不上。
    cfg = Config.load()
    print(f"外观配置：rows={cfg.strip_rows} palette={cfg.strip_palette} "
          f"font_scale={cfg.strip_font_scale} fields={len(cfg.strip_fields)}")
    apply = Applied(cfg)
    strip = TaskbarStrip(cfg)
    strip._limit = 883          # 本机实测的可用宽度（见 _stripfields_test）
    strip._max_height = 48
    win = AppearanceWindow(cfg, strip, PromoSnap, apply)
    if not win.create():
        print("建窗失败")
        return 1

    win.show()
    pump(1.2)                   # 等它真的画出来（显示后系统还会擦一次背景）
    if not win.is_visible or win._screen_origin() is None:
        print("窗口没显示出来")
        return 1

    r = wintypes.RECT()
    user32.GetWindowRect(win._hwnd, ctypes.byref(r))
    buf, w, h = grab_screen(r.left, r.top, r.right, r.bottom)
    out = OUT / "appearance_promo.png"
    write_png(out, buf, w, h)
    print(f"已写出 {out.name}  {w}x{h}  ({out.stat().st_size / 1024:.1f} KB)")

    win.hide()
    win.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
