"""对「正在运行的 exe」做四角特写：不自己建窗口，直接找已经存在的面板窗口。

用来验证打包后的 exe 里圆角是不是也在（而不是只有源码跑起来才有）。

用法：
    python _livepanelcheck.py [out_name]
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _panelcorner import grab_bgra, write_png  # noqa: E402
from powermon.app import enable_dpi_awareness  # noqa: E402

TILE = 72
GAP = 10
ZOOM = 4


def main() -> int:
    enable_dpi_awareness()
    u = ctypes.windll.user32
    from ctypes import wintypes

    hwnd = u.FindWindowW("PowerMonitorPanelWnd", "开机能耗统计")
    if not hwnd:
        print("没找到正在运行的面板窗口")
        return 1
    r = wintypes.RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    L, T, R, B = r.left, r.top, r.right, r.bottom
    print(f"面板窗口 hwnd={hwnd} = ({L},{T})-({R},{B}) {R - L}x{B - T}")

    off = 6
    spots = {
        "左上": (L - off, T - off),
        "右上": (R + off - TILE, T - off),
        "左下": (L - off, B + off - TILE),
        "右下": (R + off - TILE, B + off - TILE),
    }
    tiles = {k: grab_bgra(x, y, TILE, TILE) for k, (x, y) in spots.items()}

    gw = gh = TILE * 2 + GAP
    canvas = bytearray(gw * gh * 4)
    for name, ox, oy in (("左上", 0, 0), ("右上", TILE + GAP, 0),
                         ("左下", 0, TILE + GAP),
                         ("右下", TILE + GAP, TILE + GAP)):
        src = tiles[name]
        for y in range(TILE):
            d = ((oy + y) * gw + ox) * 4
            canvas[d:d + TILE * 4] = src[y * TILE * 4:(y + 1) * TILE * 4]

    bw, bh = gw * ZOOM, gh * ZOOM
    big = bytearray()
    for y in range(bh):
        sy = y // ZOOM
        for x in range(bw):
            p = (sy * gw + (x // ZOOM)) * 4
            big += canvas[p:p + 4]

    out_dir = Path(__file__).resolve().parent / "_preview"
    out_dir.mkdir(exist_ok=True)
    name = sys.argv[1] if len(sys.argv) > 1 else "panel_corners_live_exe.png"
    out = out_dir / name
    write_png(out, bytes(big), bw, bh)
    print(f"已写出 {out} ({bw}x{bh})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
