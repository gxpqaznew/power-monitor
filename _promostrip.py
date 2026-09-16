"""宣传片素材：把任务栏上那条**真实的长条**抓下来，平滑放大成高清 PNG。

为什么不能直接贴屏幕截图：125% 缩放下长条只有 504x55 物理像素，塞进 1080p
画面里要放大近 3 倍，最近邻放大后数字边缘全是台阶。所以这里：

  1. ``PrintWindow(PW_RENDERFULLCONTENT)`` 抓长条本体（它是 layered 窗口，
     ``GetDC(0)+BitBlt`` 抓不到，会拿到它下面那个窗口的像素 —— 踩过）；
  2. 纯 Python 双线性放大 3 倍（不用 GDI 的 StretchBlt：那几个函数项目里没
     声明 argtypes，句柄会被截断成 int32）；
  3. 四周补一圈模拟任务栏底色 ``#EEF2F7``（与 PromoComposition 里那条假
     任务栏同色，贴上去看不出接缝）；
  4. 手写 PNG（项目没有 Pillow，复用 ``_appearancepng.write_png``）。

输出：``_preview/strip_promo.png``。
"""

from __future__ import annotations

import ctypes
import math
import sys
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _appearancepng import write_png  # noqa: E402

from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.w32 import gdi32, user32  # noqa: E402

OUT = Path(__file__).resolve().parent / "_preview"
SRCCOPY = 0x00CC0020
# 模拟任务栏底色 #EEF2F7 → COLORREF 是 0x00BBGGRR
BAR_BG = 0x00F7F2EE
ZOOM = 3
PAD = 7


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
user32.PrintWindow.restype = wintypes.BOOL


def find_strip():
    """长条挂在 Shell_TrayWnd 下面（任务栏的**子窗口**，EnumWindows 找不到）。"""
    tray = user32.FindWindowW("Shell_TrayWnd", None)
    if not tray:
        return None
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _lp):
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        if buf.value == "PowerMonitorTaskbarStrip":
            found.append(hwnd)
            return False
        return True

    user32.EnumChildWindows(tray, cb, 0)
    return found[0] if found else None


def grab(hwnd, w: int, h: int) -> bytes:
    """PrintWindow 抓窗口本体（layered 窗口只能这么抓）。返回 BGRA，自上而下。"""
    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    old = gdi32.SelectObject(dc, bmp)
    ok = user32.PrintWindow(hwnd, dc, 2)     # PW_RENDERFULLCONTENT
    print("  PrintWindow ok =", ok)
    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = w
    info.bmiHeader.biHeight = -h             # 负数 = 自上而下
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(dc, bmp, 0, h, buf, ctypes.byref(info), 0)
    gdi32.SelectObject(dc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(dc)
    user32.ReleaseDC(None, screen)
    return buf.raw


def upscale(src: bytes, w: int, h: int, zoom: int, pad: int) -> tuple[bytes, int, int]:
    """双线性放大 + 补边。先横向再纵向两趟，比逐像素四邻域取快一倍。"""
    W, H = w * zoom + pad * 2, h * zoom + pad * 2
    mid_w = w * zoom
    bg = (BAR_BG & 0xFF, (BAR_BG >> 8) & 0xFF, (BAR_BG >> 16) & 0xFF, 255)

    # ---- 横向 ----
    mid = bytearray(mid_w * h * 4)
    xs = []
    for dx in range(mid_w):
        sx = (dx + 0.5) / zoom - 0.5
        x0 = int(math.floor(sx))
        fx = sx - x0
        x0 = max(0, min(w - 1, x0))
        x1 = min(w - 1, x0 + 1)
        if x0 == x1:
            fx = 0.0
        xs.append((x0, x1, fx))
    for y in range(h):
        base = y * w * 4
        o = y * mid_w * 4
        for dx in range(mid_w):
            x0, x1, fx = xs[dx]
            i0 = base + x0 * 4
            i1 = base + x1 * 4
            g0 = 1.0 - fx
            for c in range(3):
                mid[o + c] = int(src[i0 + c] * g0 + src[i1 + c] * fx + 0.5)
            mid[o + 3] = 255
            o += 4

    # ---- 纵向 + 补边 ----
    out = bytearray(W * H * 4)
    for i in range(W * H):
        out[i * 4:i * 4 + 4] = bytes(bg)
    for dy in range(h * zoom):
        sy = (dy + 0.5) / zoom - 0.5
        y0 = int(math.floor(sy))
        fy = sy - y0
        y0 = max(0, min(h - 1, y0))
        y1 = min(h - 1, y0 + 1)
        if y0 == y1:
            fy = 0.0
        r0 = y0 * mid_w * 4
        r1 = y1 * mid_w * 4
        g0 = 1.0 - fy
        o = ((dy + pad) * W + pad) * 4
        for dx in range(mid_w):
            a = r0 + dx * 4
            b = r1 + dx * 4
            out[o] = int(mid[a] * g0 + mid[b] * fy + 0.5)
            out[o + 1] = int(mid[a + 1] * g0 + mid[b + 1] * fy + 0.5)
            out[o + 2] = int(mid[a + 2] * g0 + mid[b + 2] * fy + 0.5)
            out[o + 3] = 255
            o += 4
    return bytes(out), W, H


def main() -> int:
    enable_dpi_awareness()
    OUT.mkdir(exist_ok=True)

    hwnd = find_strip()
    if not hwnd:
        print("没找到长条窗口 —— 程序没在跑，或长条被关掉了")
        return 1

    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    print(f"长条 0x{hwnd:x}  {w}x{h} @ ({r.left},{r.top})")

    raw = grab(hwnd, w, h)
    big, W, H = upscale(raw, w, h, ZOOM, PAD)
    out = OUT / "strip_promo.png"
    write_png(out, big, W, H)
    print(f"已写出 {out.name}  {W}x{H}  ({out.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
