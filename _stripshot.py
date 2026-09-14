"""截任务栏上的一条（默认左上角开始按钮那一片），确认长条真的贴上去了。

用法：
    python _stripshot.py [x y w h] [out_name]

不带参数就抓 (0, taskbar_top-4) 起 1200x68 的一条 —— 正好覆盖「长条 + 开始
按钮 + 左边空白」。整屏截图在纯 Python 里编码要十几秒，只抓需要的那块。
"""

from __future__ import annotations

import ctypes
import struct
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import taskbar, w32  # noqa: E402
from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.w32 import gdi32, user32  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "_preview"
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


def grab(x0: int, y0: int, w: int, h: int, name: str) -> None:
    screen = user32.GetDC(None)
    mem_dc = gdi32.CreateCompatibleDC(screen)
    info = w32.BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(w32.BITMAPINFOHEADER)
    info.bmiHeader.biWidth = w
    info.bmiHeader.biHeight = -h          # 自上而下
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = 0
    bits = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(mem_dc, ctypes.byref(info), 0,
                                 ctypes.byref(bits), None, 0)
    if not bmp or not bits:
        print("创建 DIB 失败")
        return
    old = gdi32.SelectObject(mem_dc, bmp)
    ok = gdi32.BitBlt(mem_dc, 0, 0, w, h, screen, x0, y0, SRCCOPY)
    buf = ctypes.string_at(bits, w * h * 4)
    gdi32.SelectObject(mem_dc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(None, screen)
    print(f"抓取 ({x0},{y0}) {w}x{h} BitBlt={ok}")
    if not ok:
        return
    OUT_DIR.mkdir(exist_ok=True)
    write_png(OUT_DIR / name, buf, w, h)
    print(f"已写出 {OUT_DIR / name}")


def main() -> int:
    enable_dpi_awareness()
    argv = sys.argv[1:]
    if len(argv) >= 5:
        x0, y0, w, h = (int(v) for v in argv[:4])
        name = argv[4]
    else:
        info = taskbar.taskbar()
        if info is None:
            print("没有任务栏，抓整个屏幕底部一条")
            sw = user32.GetSystemMetrics(0)
            sh = user32.GetSystemMetrics(1)
            x0, y0, w, h = 0, sh - 68, min(sw, 1400), 68
        else:
            _hwnd, rect, _dpi = info
            x0, y0 = rect[0], max(0, rect[1] - 4)
            w = min(rect[2] - rect[0], 1400)
            h = rect[3] - rect[1] + 8
        name = argv[0] if argv else "taskbar_left.png"
    name = name if name.endswith(".png") else name + ".png"
    print(taskbar.describe())
    grab(x0, y0, w, h, name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
