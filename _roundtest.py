"""A/B 对照实验：Win11 的 DWM 圆角到底有没有生效？

背景：数值探测（GetPixel / BitBlt 抓屏）发现详情面板的左上角是满的，
      看着像直角。但 Win11 默认就给普通窗口切圆角，所以有两种可能：
        (a) 圆角真的没生效；
        (b) 圆角生效了，但 GetDC(NULL) 这条老抓屏路径看不到 DWM 合成结果。

做法：建两个一模一样的普通窗口（亮绿色客户区），
      A 设 DWMWCP_ROUND，B 设 DWMWCP_DONOTROUND，
      然后沿左上角对角线逐点读像素，直接对比。

用法：
    python _roundtest.py
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes
from pathlib import Path

os.environ.setdefault("POWERMON_DEBUG", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.w32 import (  # noqa: E402
    PAINTSTRUCT, WNDCLASSEXW, WNDPROC, gdi32, kernel32, user32,
)

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_DONOTROUND = 1
DWMWCP_ROUND = 2

WS_OVERLAPPEDWINDOW = 0x00CF0000
SW_SHOW = 5
HWND_TOPMOST = wintypes.HWND(-1)
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_SHOWWINDOW = 0x0040

_WM_PAINT, _WM_ERASEBKGND, _WM_DESTROY = 0x000F, 0x0014, 0x0002
_proc_ref = None


def _proc(hwnd, msg, wp, lp):
    if msg == _WM_ERASEBKGND:
        br = gdi32.CreateSolidBrush(0x0000FF00)  # COLORREF 0x00BBGGRR -> 纯绿
        r = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(r))
        user32.FillRect(wintypes.HDC(wp), ctypes.byref(r), br)
        gdi32.DeleteObject(br)
        return 1
    if msg == _WM_PAINT:
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
        user32.EndPaint(hwnd, ctypes.byref(ps))
        return 0
    return user32.DefWindowProcW(hwnd, msg, wp, lp)


def pump(seconds: float) -> None:
    msg = wintypes.MSG()
    end = time.time() + seconds
    while time.time() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.02)


def set_corner(hwnd, pref: int) -> int:
    dwm = ctypes.windll.dwmapi
    data = ctypes.c_uint(pref)
    return dwm.DwmSetWindowAttribute(
        wintypes.HWND(hwnd), ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
        ctypes.byref(data), ctypes.sizeof(data))


def get_corner(hwnd) -> int | None:
    dwm = ctypes.windll.dwmapi
    out = ctypes.c_uint(999)
    hr = dwm.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), ctypes.c_uint(DWMWA_WINDOW_CORNER_PREFERENCE),
        ctypes.byref(out), ctypes.sizeof(out))
    return out.value if hr == 0 else None


def rgb(px: int) -> tuple[int, int, int]:
    return (px & 0xFF, (px >> 8) & 0xFF, (px >> 16) & 0xFF)


OUT_DIR = Path(__file__).resolve().parent / "_preview"


def write_png(path: Path, bgra: bytes, w: int, h: int) -> None:
    import struct
    import zlib
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
    from powermon import w32 as _w
    screen = user32.GetDC(None)
    mem_dc = gdi32.CreateCompatibleDC(screen)
    info = _w.BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(_w.BITMAPINFOHEADER)
    info.bmiHeader.biWidth = w
    info.bmiHeader.biHeight = -h
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = 0
    bits = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(mem_dc, ctypes.byref(info), 0,
                                 ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(mem_dc, bmp)
    gdi32.BitBlt(mem_dc, 0, 0, w, h, screen, x0, y0, 0x00CC0020)
    buf = ctypes.string_at(bits, w * h * 4)
    gdi32.SelectObject(mem_dc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(None, screen)
    OUT_DIR.mkdir(exist_ok=True)
    write_png(OUT_DIR / name, buf, w, h)
    print(f"  已写出 {OUT_DIR / name} ({x0},{y0}) {w}x{h}")


def main() -> int:
    global _proc_ref
    enable_dpi_awareness()
    _proc_ref = WNDPROC(_proc)

    cls = "RoundABTestWnd"
    hinst = kernel32.GetModuleHandleW(None)
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.lpfnWndProc = _proc_ref
    wc.hInstance = hinst
    wc.lpszClassName = cls
    if not user32.RegisterClassExW(ctypes.byref(wc)):
        if ctypes.get_last_error() != 1410:
            print("RegisterClass 失败 err=", ctypes.get_last_error())
            return 1

    AX, AY, BX, BY, W, H = 260, 260, 780, 260, 260, 260
    hw_a = user32.CreateWindowExW(0, cls, "A ROUND", WS_OVERLAPPEDWINDOW,
                                  AX, AY, W, H, None, None, hinst, None)
    hw_b = user32.CreateWindowExW(0, cls, "B DONOTROUND", WS_OVERLAPPEDWINDOW,
                                  BX, BY, W, H, None, None, hinst, None)
    if not hw_a or not hw_b:
        print("窗口创建失败")
        return 1
    for hw in (hw_a, hw_b):
        user32.SetWindowPos(hw, HWND_TOPMOST, 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.ShowWindow(hw, SW_SHOW)
        user32.UpdateWindow(hw)

    hr_a = set_corner(hw_a, DWMWCP_ROUND)
    hr_b = set_corner(hw_b, DWMWCP_DONOTROUND)
    print(f"set ROUND      hr={hr_a:#x}  读回={get_corner(hw_a)}")
    print(f"set DONOTROUND hr={hr_b:#x}  读回={get_corner(hw_b)}")

    pump(1.0)

    ra, rb = wintypes.RECT(), wintypes.RECT()
    user32.GetWindowRect(hw_a, ctypes.byref(ra))
    user32.GetWindowRect(hw_b, ctypes.byref(rb))

    dc = user32.GetDC(None)
    desk = rgb(gdi32.GetPixel(dc, 5, 5))
    print(f"A 窗口 = ({ra.left},{ra.top})-({ra.right},{ra.bottom})")
    print(f"B 窗口 = ({rb.left},{rb.top})-({rb.right},{rb.bottom})")
    print(f"桌面参考色 (5,5) = {desk}")

    print("抓两张角部特写：")
    grab(ra.left - 10, ra.top - 10, 70, 70, "roundtest_A_corner.png")
    grab(rb.left - 10, rb.top - 10, 70, 70, "roundtest_B_corner.png")

    for tag, r in (("A(ROUND)", ra), ("B(DONOTROUND)", rb)):
        print(f"\n{tag} 左上角沿对角线 (1,1)->(41,41) 每 2px：")
        row = []
        for i in range(21):
            c = rgb(gdi32.GetPixel(dc, r.left + 1 + i * 2, r.top + 1 + i * 2))
            row.append(c)
            print(f"  偏移{i * 2:>2}px  RGB={c}")
        first_green = next((i * 2 for i, c in enumerate(row)
                            if c[1] > 180 and c[0] < 80 and c[2] < 80), None)
        print(f"  -> 首次出现纯绿的偏移 = {first_green}px  "
              f"({'圆角：角被切掉' if (first_green or 0) > 3 else '直角：角是满的'})")

    user32.ReleaseDC(None, dc)
    user32.DestroyWindow(hw_a)
    user32.DestroyWindow(hw_b)
    pump(0.2)
    print("\n已销毁")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
