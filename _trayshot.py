"""截任务栏右下角一小块，确认托盘图标真的在那儿。

只抓 720×96 的一小块：整屏截图的 PNG 编码在纯 Python 里要跑十几秒，
而且这块就够看了。
"""

from __future__ import annotations

import ctypes
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import w32  # noqa: E402
from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.w32 import gdi32, user32  # noqa: E402

OUT = Path(__file__).resolve().parent / "_preview" / "tray.png"
CROP_W, CROP_H = 720, 96
SRCCOPY = 0x00CC0020


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


def main() -> int:
    enable_dpi_awareness()

    sw = user32.GetSystemMetrics(0)
    sh = user32.GetSystemMetrics(1)
    x0, y0 = sw - CROP_W, sh - CROP_H
    print(f"屏幕 {sw}x{sh}，抓取 ({x0},{y0}) 起 {CROP_W}x{CROP_H}")

    screen = user32.GetDC(None)
    mem_dc = gdi32.CreateCompatibleDC(screen)

    info = w32.BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(w32.BITMAPINFOHEADER)
    info.bmiHeader.biWidth = CROP_W
    info.bmiHeader.biHeight = -CROP_H          # 自上而下
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = 0

    bits = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(mem_dc, ctypes.byref(info), 0, ctypes.byref(bits), None, 0)
    if not bmp or not bits:
        print("创建 DIB 失败")
        return 1

    old = gdi32.SelectObject(mem_dc, bmp)
    ok = gdi32.BitBlt(mem_dc, 0, 0, CROP_W, CROP_H, screen, x0, y0, SRCCOPY)
    print(f"BitBlt ok={ok}")

    buf = ctypes.string_at(bits, CROP_W * CROP_H * 4)
    write_png(OUT, buf, CROP_W, CROP_H)
    print(f"已写出 {OUT}")

    gdi32.SelectObject(mem_dc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(None, screen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
