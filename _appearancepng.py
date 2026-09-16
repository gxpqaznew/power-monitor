"""「长条外观」窗口的对照图 —— 出图用肉眼看的，不跑断言。

离屏渲染（窗口建出来但不显示，毛玻璃抓的就是它背后那块真实桌面），几套典型
配置各出一张，最后纵向拼成一张 ``_preview/appearance_sheet.png``。

为什么要出图：这个窗口的活儿一半是「能不能点」，另一半是**好不好看**。
好看这件事没有数值判据（对比度护栏只管「看得清」），只能出图看。
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _appearanceshot import build, read_pixels, render  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "_preview"
BG_IMG = OUT_DIR / "desktop.png"

# (说明, 配置覆盖)
CASES = [
    ("默认（跟随质感 / 自动排数）", {}),
    ("两排 + 石墨", {"strip_rows": "2", "strip_palette": "graphite"}),
    ("两排 + 宣纸 + 无标签", {"strip_rows": "2", "strip_palette": "paper",
                              "strip_show_label": False}),
    ("三排 + 薄荷 + 1.32×", {"strip_rows": "3", "strip_palette": "mint",
                             "strip_font_scale": 1.32}),
    ("纤细 + 樱花 + 自定义色", {"strip_size": "slim", "strip_palette": "rose",
                                "strip_value_color": "#7C2D12",
                                "strip_bg_color": "#FFF7ED"}),
]


def write_png(path: Path, bgra: bytes, w: int, h: int) -> None:
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        row = bgra[y * w * 4:(y + 1) * w * 4]
        for x in range(w):
            raw += bytes((row[x * 4 + 2], row[x * 4 + 1], row[x * 4]))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def shoot(over: dict) -> tuple[bytes, int, int]:
    cfg, _strip, win, _apply = build(**over)
    dc, w, h = render(win, warm=True)
    buf = read_pixels(dc, w, h)
    win.destroy()
    return buf, w, h


def main() -> int:
    OUT_DIR.mkdir(exist_ok=True)
    frames = []
    for name, over in CASES:
        if "strip_bg_image" not in over and BG_IMG.exists() and name.endswith("自定义色"):
            over = dict(over, strip_bg_image=str(BG_IMG), strip_bg_opacity=70)
        buf, w, h = shoot(over)
        png = OUT_DIR / f"appearance_{len(frames)}.png"
        write_png(png, buf, w, h)
        frames.append((name, buf, w, h, png))
        print(f"[{len(frames)}] {name}  ->  {png.name}  ({w}x{h})")

    gap = 12
    sheet_w = max(f[2] for f in frames)
    sheet_h = sum(f[3] for f in frames) + gap * (len(frames) + 1)
    canvas = bytearray(sheet_w * sheet_h * 4)
    for i in range(sheet_w * sheet_h):        # 先铺一层浅灰底
        canvas[i * 4 + 0] = 0xE8
        canvas[i * 4 + 1] = 0xEA
        canvas[i * 4 + 2] = 0xEE
        canvas[i * 4 + 3] = 0xFF
    y0 = gap
    for _name, buf, w, h, _png in frames:
        for y in range(h):
            src = y * w * 4
            dst = ((y0 + y) * sheet_w) * 4
            canvas[dst:dst + w * 4] = buf[src:src + w * 4]
        y0 += h + gap
    sheet = OUT_DIR / "appearance_sheet.png"
    write_png(sheet, bytes(canvas), sheet_w, sheet_h)
    print(f"\n拼图 {sheet}  ({sheet_w}x{sheet_h})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
