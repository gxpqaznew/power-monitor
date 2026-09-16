"""6 个质感档位的**真机**对照图：确认每一档都是毛玻璃。

离屏渲染是抓不到屏幕的（没有真实窗口位置 → 直接退化成实色底），所以必须一档一档
真建窗、放上任务栏、再抓屏。输出 `_preview/frost_themes.png`，6 条竖着叠，顺序和
`stripopts.THEMES` 一致（跟随任务栏 / 玻璃 / 线框 / 深色卡片 / 浅色卡片 / 强调色）。
"""

from __future__ import annotations

import ctypes
import struct
import sys
import time
import zlib
from pathlib import Path
from ctypes import wintypes

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import stripopts  # noqa: E402
from powermon.config import Config  # noqa: E402
from powermon.strip import TaskbarStrip  # noqa: E402
from powermon.w32 import gdi32, user32  # noqa: E402
from _panellive import make_snapshot  # noqa: E402

OUT = Path(__file__).resolve().parent / "_preview"
SRCCOPY = 0x00CC0020
ctypes.windll.shcore.SetProcessDpiAwareness(1)


def write_png(path: Path, rows: list[tuple[bytes, int]], w: int, gap: int = 6) -> None:
    """把若干张同宽的 BGRA 图竖着叠成一张（gap 里填中灰）。"""
    total_h = sum(h for _d, h in rows) + gap * (len(rows) - 1)
    raw = bytearray()
    for y in range(total_h):
        raw.append(0)
        line = bytearray(b"\x88\x88\x88") * w
        acc = 0
        for data, h in rows:
            if acc <= y < acc + h:
                sa = (y - acc) * w * 4
                line = bytearray()
                for x in range(w):
                    i = sa + x * 4
                    line += bytes((data[i + 2], data[i + 1], data[i]))
                break
            acc += h + gap
        raw += line

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, total_h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def grab(x0: int, y0: int, w: int, h: int) -> bytes:
    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    old = gdi32.SelectObject(dc, bmp)
    gdi32.BitBlt(dc, 0, 0, w, h, screen, x0, y0, SRCCOPY)

    class BI(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("x", ctypes.c_long),
                    ("y", ctypes.c_long), ("clrUsed", wintypes.DWORD),
                    ("clrImportant", wintypes.DWORD)]

    bi = BI()
    bi.biSize = ctypes.sizeof(BI)
    bi.biWidth, bi.biHeight = w, -h
    bi.biPlanes, bi.biBitCount = 1, 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(dc, bmp, 0, h, buf, ctypes.byref(bi), 0)
    gdi32.SelectObject(dc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(dc)
    user32.ReleaseDC(None, screen)
    return buf.raw


def pump(seconds: float) -> None:
    msg = wintypes.MSG()
    end = time.time() + seconds
    while time.time() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.02)


def main() -> int:
    OUT.mkdir(exist_ok=True)
    rows: list[tuple[bytes, int]] = []
    width = 0
    for theme in stripopts.THEME_KEYS:
        cfg = Config()
        cfg.strip_enabled = True
        cfg.strip_theme = theme
        cfg.strip_fields = list(stripopts.DEFAULT_FIELDS)
        snap = make_snapshot(cfg, time.time())
        strip = TaskbarStrip(cfg)
        if not strip.create(snap):
            print(f"{theme}: create 失败")
            continue
        pump(0.7)
        r = wintypes.RECT()
        user32.GetWindowRect(strip.hwnd, ctypes.byref(r))
        w, h = r.right - r.left, r.bottom - r.top
        pad = 8
        img = grab(r.left - pad, r.top - pad, w + pad * 2, h + pad * 2)
        pal = strip._pal or {}
        print(f"{theme:8s} {w}x{h}  frost={pal.get('frost')} alpha={pal.get('alpha')}")
        rows.append((img, h + pad * 2))
        width = max(width, w + pad * 2)
        strip.destroy()
        pump(0.2)

    if rows:
        write_png(OUT / "frost_themes.png", rows, width)
        print(f"已写出 {OUT / 'frost_themes.png'}  宽 {width} "
              f"高 {sum(h for _d, h in rows) + 6 * (len(rows) - 1)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
