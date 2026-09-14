"""长条宽度抖动探针：高频采样长条矩形，宽度一变化就抓那一帧，
把「窄相」和「宽相」两个状态各存一张，肉眼对比是内容变化还是渲染跳动。"""
import ctypes
import time
from ctypes import wintypes
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from powermon.roundwin import dib_section  # noqa: E402

u = ctypes.windll.user32
g = ctypes.windll.gdi32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

_HDC = ctypes.c_void_p
_HGDIOBJ = ctypes.c_void_p
g.CreateCompatibleDC.argtypes = [_HDC]
g.CreateCompatibleDC.restype = _HDC
g.SelectObject.argtypes = [_HDC, _HGDIOBJ]
g.SelectObject.restype = _HGDIOBJ
g.BitBlt.argtypes = [_HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                     _HDC, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
g.DeleteObject.argtypes = [_HGDIOBJ]
g.DeleteDC.argtypes = [_HDC]
u.GetDC.argtypes = [wintypes.HWND]
u.GetDC.restype = _HDC
u.ReleaseDC.argtypes = [wintypes.HWND, _HDC]
u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
u.FindWindowW.restype = wintypes.HWND
u.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
u.FindWindowExW.restype = wintypes.HWND

SRCCOPY = 0x00CC0020


def rect_of(h):
    r = wintypes.RECT()
    u.GetWindowRect(h, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


def grab(rect):
    x, y, x2, y2 = rect
    w, h = x2 - x, y2 - y
    sdc = u.GetDC(None)
    mdc = g.CreateCompatibleDC(sdc)
    bmp, view = dib_section(mdc, w, h)
    old = g.SelectObject(mdc, bmp)
    g.BitBlt(mdc, 0, 0, w, h, sdc, x, y, SRCCOPY)
    g.SelectObject(mdc, old)
    data = bytes(view)
    g.DeleteObject(bmp)
    g.DeleteDC(mdc)
    u.ReleaseDC(None, sdc)
    return data, w, h


def save_bmp(path, px, w, h):
    import struct
    row = w * 4
    hdr = struct.pack("<2sIHHI", b"BM", 14 + 40 + row * h, 0, 0, 14 + 40)
    info = struct.pack("<IiiHHIIiiII", 40, w, -h, 1, 32, 0, row * h, 2835, 2835, 0, 0)
    Path(path).write_bytes(hdr + info + px)


def main():
    tray = u.FindWindowW("Shell_TrayWnd", None)
    strip = u.FindWindowExW(tray, None, "PowerMonitorTaskbarStrip", None) if tray else 0
    if not strip:
        print("找不到长条")
        return 1
    # 取一个足够宽的固定窗口（覆盖两种宽度），保证两种状态都能完整拍到
    base = rect_of(strip)
    watch = (base[0] - 30, base[1], base[2] + 30, base[3])
    print(f"strip={strip:#x} 初始 rect={base}  观察区={watch}")

    shots = {}
    prev = None
    t0 = time.time()
    while time.time() - t0 < 12:
        r = rect_of(strip)
        w = r[2] - r[0]
        if w != prev:
            print(f"{time.time()-t0:5.2f}s 宽度 {prev} -> {w}  rect={r}")
            data, gw, gh = grab(watch)
            shots.setdefault(w, data)
            prev = w
        time.sleep(0.05)

    out = Path("_preview")
    out.mkdir(exist_ok=True)
    for w, data in sorted(shots.items()):
        save_bmp(out / f"strip_w{w}.bmp", data, watch[2] - watch[0], watch[3] - watch[1])
        print("saved", out / f"strip_w{w}.bmp")
    ws = sorted(shots)
    if len(ws) >= 2:
        a, b = shots[ws[0]], shots[ws[-1]]
        diff = sum(1 for x, y in zip(a, b) if x != y)
        print(f"两相差异 {diff}/{len(a)} 字节 ({diff*100//len(a)}%)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
