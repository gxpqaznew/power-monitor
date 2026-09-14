"""截取安装 / 卸载向导的界面，确认中文本地化、字体与图标渲染正常。

做法：把向导拉起来（停在第一页，不做任何实际操作），把窗口置顶后
从屏幕 DC 抓图，保存到 installer/_preview/。抓完直接结束进程。
"""

from __future__ import annotations

import ctypes
import os
import struct
import subprocess
import sys
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
sys.path.insert(0, str(PROJECT))

from powermon.app import enable_dpi_awareness  # noqa: E402

enable_dpi_awareness()      # 必须在任何窗口/DC 之前

from powermon import w32 as W  # noqa: E402
from powermon.w32 import gdi32, kernel32, user32, wintypes  # noqa: E402

SETUP = PROJECT / "dist" / "开机能耗统计-安装程序-v1.0.0.exe"
UNINST = Path(os.environ["LOCALAPPDATA"]) / "Programs" / "PowerMonitor" / "卸载 开机能耗统计.exe"
OUT = HERE / "_preview"
OUT.mkdir(exist_ok=True)

user32.EnumWindows.argtypes = [ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM), wintypes.LPARAM]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.POINTER(W.BITMAPINFO), wintypes.UINT,
]

DETACHED_PROCESS = 0x00000008
SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002


def top_window_matching(needle: str) -> tuple[int, str]:
    """扫描可见顶层窗口，取标题含 needle 且面积最大的那个。

    不能按 PID 找：NSIS 的卸载程序会先把自身复制到 %TEMP% 再运行，
    真正把界面画出来的是另一个进程。
    """
    best = (0, "")
    best_area = 0

    def cb(hwnd, _):
        nonlocal best, best_area
        if not user32.IsWindowVisible(hwnd):
            return True
        buf = ctypes.create_unicode_buffer(300)
        user32.GetWindowTextW(hwnd, buf, 300)
        if needle not in buf.value:
            return True
        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        area = (r.right - r.left) * (r.bottom - r.top)
        if area > best_area:
            best_area, best = area, (hwnd, buf.value)
        return True

    user32.EnumWindows(ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(cb), 0)
    return best


def kill_window_process(hwnd: int) -> None:
    """结束正在显示这个窗口的进程。"""
    owner = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x0001, False, owner.value)   # PROCESS_TERMINATE
    if handle:
        kernel32.TerminateProcess(handle, 0)
        kernel32.CloseHandle(handle)


def save_png(path: Path, bgra: bytes, w: int, h: int) -> None:
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        row = bgra[y * w * 4:(y + 1) * w * 4]
        line = bytearray(w * 3)
        line[0::3] = row[2::4]        # R
        line[1::3] = row[1::4]        # G
        line[2::3] = row[0::4]        # B
        raw += line

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

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
    gdi32.GetDIBits(memdc, bmp, 0, ch, ctypes.byref(buf), ctypes.byref(info), W.DIB_RGB_COLORS)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(None, screen)
    return bytes(buf)


def shoot(exe: Path, tag: str, needle: str) -> None:
    if not exe.exists():
        print(f"  跳过（不存在）：{exe}")
        return
    proc = subprocess.Popen([str(exe)], cwd=str(Path(os.environ["WINDIR"])),
                            creationflags=DETACHED_PROCESS, close_fds=True)
    hwnd = 0
    try:
        title = ""
        for _ in range(30):                     # 最多等 15 秒
            time.sleep(0.5)
            hwnd, title = top_window_matching(needle)
            if hwnd:
                break
        if not hwnd:
            print(f"  [{tag}] 没找到窗口")
            return
        # 拉前台，免得被别的窗口挡住抓错内容
        user32.SetWindowPos(hwnd, W.HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE)
        user32.SetForegroundWindow(hwnd)
        time.sleep(1.2)

        r = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        cw, ch = r.right - r.left, r.bottom - r.top
        path = OUT / f"{tag}.png"
        save_png(path, grab(r.left, r.top, cw, ch), cw, ch)
        print(f"  [{tag}] 标题={title!r}  尺寸={cw}x{ch}  -> {path.name}")
    finally:
        proc.kill()
        if hwnd:                    # 卸载程序跑在别的进程里，按窗口再收一次
            kill_window_process(hwnd)


print("截安装向导：")
shoot(SETUP, "setup", "安装向导")
print("截卸载向导：")
shoot(UNINST, "uninstall", "卸载")
print("完成")
