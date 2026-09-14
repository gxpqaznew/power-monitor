"""量一下冻结包里面板窗口的实际位置，顺便看工作区。"""

import ctypes
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon.app import enable_dpi_awareness  # noqa: E402

# 必须先声明 DPI 感知：否则本进程拿到的是被虚拟化过的坐标，
# 和被测量程序（DPI 感知）的真实坐标对不上，裁剪位置就会错。
enable_dpi_awareness()

from powermon.w32 import user32, wintypes  # noqa: E402

user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.SystemParametersInfoW.argtypes = [
    wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]

ROOT = Path(__file__).resolve().parent
EXE = ROOT / "dist" / "能耗统计.exe"

for name in ("config.json", "state.json"):
    f = EXE.parent / name
    if f.exists():
        os.replace(f, os.path.join(tempfile.gettempdir(), "pm_rect_" + name))

DETACHED_PROCESS = 0x00000008
subprocess.Popen([str(EXE)], creationflags=DETACHED_PROCESS, close_fds=True)
time.sleep(7)

work = wintypes.RECT()
user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(work), 0)
print(f"屏幕 = {user32.GetSystemMetrics(0)}x{user32.GetSystemMetrics(1)}")
print(f"工作区 = ({work.left},{work.top})-({work.right},{work.bottom})")

hwnd = user32.FindWindowW("PowerMonitorPanelWnd", None)
print("面板 hwnd =", hwnd)
rect = wintypes.RECT()
if hwnd:
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    print(f"面板矩形 = ({rect.left},{rect.top})-({rect.right},{rect.bottom}) "
          f"尺寸 {rect.right - rect.left}x{rect.bottom - rect.top} "
          f"可见={user32.IsWindowVisible(hwnd)}")

# ---- 就地截屏，并裁出面板区域，1:1 看真实渲染 ----
import struct  # noqa: E402
import zlib  # noqa: E402

from powermon import w32 as W  # noqa: E402
from powermon.w32 import gdi32  # noqa: E402

gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.POINTER(W.BITMAPINFO), wintypes.UINT,
]


def save_png(path: Path, bgra: bytes, w: int, h: int) -> None:
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        row = bgra[y * w * 4:(y + 1) * w * 4]
        for x in range(w):
            raw += bytes((row[x * 4 + 2], row[x * 4 + 1], row[x * 4]))
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(
            ">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def grab(x0: int, y0: int, cw: int, ch: int) -> bytes:
    screen = user32.GetDC(None)
    memdc = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, cw, ch)
    gdi32.SelectObject(memdc, bmp)
    gdi32.BitBlt(memdc, 0, 0, cw, ch, screen, x0, y0, 0x00CC0020)
    info = W.BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(W.BITMAPINFOHEADER)
    info.bmiHeader.biWidth = cw
    info.bmiHeader.biHeight = -ch
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = W.BI_RGB
    buf = (ctypes.c_ubyte * (cw * ch * 4))()
    gdi32.GetDIBits(memdc, bmp, 0, ch, ctypes.byref(buf),
                    ctypes.byref(info), W.DIB_RGB_COLORS)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(None, screen)
    return bytes(buf)


OUT = ROOT / "_preview"
if hwnd and rect.right > rect.left:
    cw = rect.right - rect.left
    ch = rect.bottom - rect.top
    save_png(OUT / "panel_live.png", grab(rect.left, rect.top, cw, ch), cw, ch)
    print(f"已裁出面板区域 -> _preview/panel_live.png ({cw}x{ch})")

import psutil  # noqa: E402

for p in psutil.process_iter(["name"]):
    if p.info["name"] and "能耗统计" in p.info["name"]:
        try:
            p.kill()
        except Exception:
            pass
print("已结束")
