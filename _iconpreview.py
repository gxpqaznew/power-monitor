"""把托盘图标渲染成 PNG 看（真实尺寸太小，肉眼判断不了）。

输出：
  _preview/icon_strip.png     16/20/24/32/48 px 真实尺寸并排 + 放大版
  _preview/icon_modes.png     各显示模式 / 各负载色的 32px 图标放大 6 倍
  _preview/icon_realsize.png  各模式在浅色 / 深色任务栏底色上的效果（放大 3 倍）
"""

from __future__ import annotations

import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.iconmake import build_pixels  # noqa: E402

OUT = Path(__file__).resolve().parent / "_preview"

BG_LIGHT = (246, 246, 246)
BG_DARK = (32, 32, 32)


def write_rgb(path: Path, rows: list[bytes], w: int, h: int) -> None:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    raw = bytearray()
    for row in rows:
        raw.append(0)
        raw += row
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def blend(bgra: bytes, base: tuple[int, int, int]) -> bytes:
    """把带 alpha 的 BGRA 合成到纯色底上，返回不透明的 BGRA。"""
    out = bytearray(len(bgra))
    for i in range(0, len(bgra), 4):
        b, g, r, a = bgra[i], bgra[i + 1], bgra[i + 2], bgra[i + 3]
        al = a / 255.0
        out[i] = int(b * al + base[2] * (1 - al))
        out[i + 1] = int(g * al + base[1] * (1 - al))
        out[i + 2] = int(r * al + base[0] * (1 - al))
        out[i + 3] = 255
    return bytes(out)


def scaled(src: bytes, sw: int, factor: int) -> bytes:
    dw = sw * factor
    out = bytearray(dw * dw * 4)
    for y in range(dw):
        for x in range(dw):
            si = ((y // factor) * sw + (x // factor)) * 4
            di = (y * dw + x) * 4
            out[di:di + 4] = src[si:si + 4]
    return bytes(out)


def to_rows(bgra: bytes, w: int, h: int) -> list[bytes]:
    rows = []
    for y in range(h):
        row = bytearray()
        for x in range(w):
            i = (y * w + x) * 4
            row += bytes((bgra[i + 2], bgra[i + 1], bgra[i]))
        rows.append(bytes(row))
    return rows


def checker_rows(bgra: bytes, w: int, h: int) -> list[bytes]:
    """棋盘底 —— 看清 alpha 边缘。"""
    rows = []
    for y in range(h):
        row = bytearray()
        for x in range(w):
            i = (y * w + x) * 4
            base = 235 if ((x // 6 + y // 6) % 2 == 0) else 205
            al = bgra[i + 3] / 255.0
            row += bytes((
                int(bgra[i + 2] * al + base * (1 - al)),
                int(bgra[i + 1] * al + base * (1 - al)),
                int(bgra[i] * al + base * (1 - al)),
            ))
        rows.append(bytes(row))
    return rows


def canvas(w: int, h: int, fill=(255, 255, 255)) -> list[bytes]:
    return [bytes(fill) * w for _ in range(h)]


def paste(dst: list[bytes], dw: int, src: list[bytes], sw: int, sh: int,
          x0: int, y0: int) -> None:
    for y in range(sh):
        row = bytearray(dst[y0 + y])
        row[x0 * 3:(x0 + sw) * 3] = src[y]
        dst[y0 + y] = bytes(row)


SAMPLES = [
    ("132", 0.42, "当前功率(中载)"),
    ("423", 0.42, "已统计能耗"),
    ("118", 0.42, "平均功率"),
    ("0.32", 0.42, "电费 小"),
    ("1.35", 0.42, "电费 中"),
    ("12.4", 0.42, "电费 大"),
    ("132", 0.05, "轻载"),
    ("132", 0.85, "重载"),
    ("132", 1.00, "满载"),
]


def main() -> int:
    enable_dpi_awareness()
    OUT.mkdir(exist_ok=True)

    # --- 真实尺寸并排 + 放大 ---
    sizes = (16, 20, 24, 32, 48)
    ref, z, gap = 32, 4, 12
    big = ref * z
    W = gap + sum(s + gap for s in sizes) + big + gap
    H = big + gap * 2

    img = canvas(W, H)
    x = gap
    for s in sizes:
        paste(img, W, checker_rows(build_pixels("184", 0.55, s), s, s), s, s,
              x, gap + (big - s) // 2)
        x += s + gap
    paste(img, W, checker_rows(scaled(build_pixels("184", 0.55, ref), ref, z), big, big),
          big, big, x, gap)
    write_rgb(OUT / "icon_strip.png", img, W, H)
    print(f"icon_strip.png  {W}x{H}")

    # --- 各显示模式 / 各负载色（32px 放大 6 倍，白底）---
    z2, gap2 = 6, 14
    cell = 32 * z2
    W2 = gap2 + len(SAMPLES) * (cell + gap2)
    H2 = cell + gap2 * 2
    img2 = canvas(W2, H2)
    x = gap2
    for text, ratio, _label in SAMPLES:
        paste(img2, W2, checker_rows(scaled(build_pixels(text, ratio, 32), 32, z2),
                                     cell, cell), cell, cell, x, gap2)
        x += cell + gap2
    write_rgb(OUT / "icon_modes.png", img2, W2, H2)
    print(f"icon_modes.png  {W2}x{H2}")

    # --- 浅色 / 深色任务栏底色上的实际观感（20px 放大 3 倍）---
    z3, gap3 = 3, 16
    side = 20 * z3
    W3 = gap3 + len(SAMPLES) * (side + gap3)
    H3 = gap3 + 2 * (side + gap3) + 40
    img3 = canvas(W3, H3, (250, 250, 250))
    for bi, base in enumerate((BG_LIGHT, BG_DARK)):
        strip = canvas(W3, side, base)
        for i, (text, ratio, _l) in enumerate(SAMPLES):
            art = scaled(blend(build_pixels(text, ratio, 20), base), 20, z3)
            paste(strip, W3, to_rows(art, side, side), side, side,
                  gap3 + i * (side + gap3), 0)
        paste(img3, W3, strip, W3, side, 0, gap3 + bi * (side + gap3))
    write_rgb(OUT / "icon_realsize.png", img3, W3, H3)
    print(f"icon_realsize.png  {W3}x{H3}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
