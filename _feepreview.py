"""把「电价设置」窗口渲染成 PNG，用来肉眼核对版面（不需要真盯着屏幕）。

用 PrintWindow 让系统把窗口连同子控件一起画到内存 DC 里，再手写 PNG
（没有 Pillow，纯 zlib + struct）。
"""

from __future__ import annotations

import ctypes
import struct
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import config as config_mod  # noqa: E402
from powermon import w32  # noqa: E402
from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.config import Config  # noqa: E402
from powermon.fee_dialog import FeeSettingsDialog  # noqa: E402
from powermon.w32 import gdi32, user32, wintypes  # noqa: E402

gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.POINTER(w32.BITMAPINFO), wintypes.UINT,
]
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL
user32.PeekMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint, wintypes.UINT
]
user32.PeekMessageW.restype = wintypes.BOOL
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0


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


def pump(seconds: float) -> None:
    msg = wintypes.MSG()
    end = time.time() + seconds
    while time.time() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.02)


def capture(hwnd, out: Path, mode: str = "print") -> None:
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w = rect.right - rect.left
    h = rect.bottom - rect.top

    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    old = gdi32.SelectObject(dc, bmp)
    if mode == "print":
        ok = user32.PrintWindow(hwnd, dc, 2)  # PW_RENDERFULLCONTENT
        src = f"PrintWindow ok={ok}"
    else:
        # 直接从屏幕上抄这块矩形：窗口被遮住时抄到的就是别人的像素
        win_dc = user32.GetWindowDC(hwnd)
        ok = gdi32.BitBlt(dc, 0, 0, w, h, win_dc, 0, 0, SRCCOPY)
        user32.ReleaseDC(hwnd, win_dc)
        src = f"BitBlt ok={ok}"
    print(f"{src} 尺寸={w}x{h}")

    info = w32.BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(w32.BITMAPINFOHEADER)
    info.bmiHeader.biWidth = w
    info.bmiHeader.biHeight = -h  # 负数 = 自上而下
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = 0
    buf = ctypes.create_string_buffer(w * h * 4)
    got = gdi32.GetDIBits(dc, bmp, 0, h, buf, ctypes.byref(info), DIB_RGB_COLORS)
    print(f"GetDIBits 取到 {got} 行")

    write_png(out, buf.raw, w, h)
    print(f"已写出 {out.name}（{out.stat().st_size / 1024:.1f} KB）")

    gdi32.SelectObject(dc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(dc)
    user32.ReleaseDC(None, screen)


def main() -> int:
    enable_dpi_awareness()
    tmp = Path(__file__).resolve().parent / "_preview"
    tmp.mkdir(exist_ok=True)
    config_mod.CONFIG_PATH = tmp / "_feetest_config.json"
    cfg = Config()

    dlg = FeeSettingsDialog(cfg)
    if not dlg.create():
        print("窗口创建失败")
        return 1
    dlg.show()
    pump(1.2)

    # 默认四川
    capture(dlg.hwnd, tmp / "fee_sichuan.png", "print")
    capture(dlg.hwnd, tmp / "fee_sichuan_screen.png", "screen")
    pump(0.2)

    # 换成浙江（有峰谷两段）
    from powermon import tariffs
    from powermon.fee_dialog import CUSTOM_LABEL
    zj = tariffs.region_names().index("浙江") + 1
    dlg._combo_fill(100, [CUSTOM_LABEL] + tariffs.region_names(), zj)
    dlg._on_region_changed()
    pump(0.6)
    capture(dlg.hwnd, tmp / "fee_zhejiang.png", "print")

    dlg.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
