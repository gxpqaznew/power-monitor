"""隔离实验：找出下拉框文字渲染异常的原因。

在同一窗口里放 4 个配置不同的下拉框，逐个排除样式 / 字体 / 创建顺序 / 高度。
"""

from __future__ import annotations

import ctypes
import struct
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import w32  # noqa: E402
from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.w32 import gdi32, kernel32, user32, wintypes  # noqa: E402

CB_ADDSTRING = 0x0143
CB_SETCURSEL = 0x014E
CB_GETCURSEL = 0x0147
CB_GETLBTEXTLEN = 0x0149
CB_GETLBTEXT = 0x0148
CB_GETITEMHEIGHT = 0x0154
WM_SETFONT = 0x0030
WM_GETTEXT = 0x000D
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_TABSTOP = 0x00010000
WS_VSCROLL = 0x00200000
WS_GROUP = 0x00020000
WS_OVERLAPPED = 0x00000000
WS_CAPTION = 0x00C00000
WS_SYSMENU = 0x00080000
CBS_DROPDOWNLIST = 0x0003
CBS_AUTOHSCROLL = 0x0040
CBS_HASSTRINGS = 0x0200
CBS_NOINTEGRALHEIGHT = 0x0400
SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0

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


def write_png(path, bgra, w, h):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        row = bgra[y * w * 4:(y + 1) * w * 4]
        for x in range(w):
            raw += bytes((row[x*4+2], row[x*4+1], row[x*4]))

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    Path(path).write_bytes(png)


def text_of(h, idx):
    n = user32.SendMessageW(h, CB_GETLBTEXTLEN, idx, 0)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.SendMessageW(h, CB_GETLBTEXT, idx, ctypes.cast(buf, ctypes.c_void_p).value)
    return buf.value


def window_text(h):
    n = user32.GetWindowTextLengthW(h) + 2
    buf = ctypes.create_unicode_buffer(n)
    user32.GetWindowTextW(h, buf, n)
    return buf.value


def pump(sec):
    msg = wintypes.MSG()
    end = time.time() + sec
    while time.time() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.02)


@w32.WNDPROC
def _proc(hwnd, msg, wparam, lparam):
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def main() -> int:
    enable_dpi_awareness()
    hinst = kernel32.GetModuleHandleW(None)

    wc = w32.WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(w32.WNDCLASSEXW)
    wc.lpfnWndProc = _proc
    wc.hInstance = hinst
    wc.hbrBackground = user32.GetSysColorBrush(15)
    wc.lpszClassName = "ComboProbeWnd"
    user32.RegisterClassExW(ctypes.byref(wc))

    hwnd = user32.CreateWindowExW(
        0, "ComboProbeWnd", "下拉框隔离实验",
        WS_OVERLAPPED | WS_CAPTION | WS_SYSMENU,
        60, 60, 620, 420, None, None, hinst, None,
    )
    font = gdi32.CreateFontW(-16, 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0, "Microsoft YaHei UI")
    print("字体句柄:", font)

    variants = [
        ("A 只 DROPDOWNLIST", CBS_DROPDOWNLIST, True, 200),
        ("B DROPDOWNLIST+AUTOHSCROLL", CBS_DROPDOWNLIST | CBS_AUTOHSCROLL, True, 200),
        ("C 全样式(现用)", CBS_DROPDOWNLIST | CBS_HASSTRINGS | CBS_AUTOHSCROLL | CBS_NOINTEGRALHEIGHT | WS_TABSTOP | WS_VSCROLL | WS_GROUP, True, 200),
        ("D 全样式但不设字体", CBS_DROPDOWNLIST | CBS_HASSTRINGS | CBS_AUTOHSCROLL | CBS_NOINTEGRALHEIGHT | WS_TABSTOP | WS_VSCROLL | WS_GROUP, False, 200),
    ]

    y = 30
    handles = []
    for name, style, use_font, h in variants:
        print(f"\n--- {name} ---")
        lbl = user32.CreateWindowExW(0, "STATIC", name, WS_CHILD | WS_VISIBLE,
                                     10, y, 600, 18, hwnd, 0, hinst, None)
        user32.SendMessageW(lbl, WM_SETFONT, font, 1)
        y += 20
        hc = user32.CreateWindowExW(0, "COMBOBOX", "", WS_CHILD | WS_VISIBLE | style,
                                    10, y, 520, h, hwnd, 0, hinst, None)
        user32.MoveWindow(hc, 10, y, 520, 200, True)
        if use_font:
            user32.SendMessageW(hc, WM_SETFONT, font, 1)
        for s in ("四川", "一户一表 第一档", "浙江 峰谷"):
            user32.SendMessageW(hc, CB_ADDSTRING, 0, ctypes.cast(ctypes.c_wchar_p(s), ctypes.c_void_p).value)
        user32.SendMessageW(hc, CB_SETCURSEL, 0, 0)
        print("  cursel =", user32.SendMessageW(hc, CB_GETCURSEL, 0, 0))
        print("  item0  =", repr(text_of(hc, 0)))
        print("  GetWindowText =", repr(window_text(hc)))
        print("  itemHeight =", user32.SendMessageW(hc, CB_GETITEMHEIGHT, -1, 0))
        r = wintypes.RECT()
        user32.GetWindowRect(hc, ctypes.byref(r))
        print(f"  窗口高 = {r.bottom - r.top}")
        handles.append(hc)
        y += 46

    user32.ShowWindow(hwnd, 5)
    user32.SetForegroundWindow(hwnd)
    pump(1.2)

    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
    old = gdi32.SelectObject(dc, bmp)
    win_dc = user32.GetWindowDC(hwnd)
    gdi32.BitBlt(dc, 0, 0, w, h, win_dc, 0, 0, SRCCOPY)
    user32.ReleaseDC(hwnd, win_dc)
    info = w32.BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(w32.BITMAPINFOHEADER)
    info.bmiHeader.biWidth = w
    info.bmiHeader.biHeight = -h
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(dc, bmp, 0, h, buf, ctypes.byref(info), DIB_RGB_COLORS)
    out = Path(__file__).resolve().parent / "_preview" / "combo_probe.png"
    write_png(out, buf.raw, w, h)
    print(f"\n已写出 {out}")
    gdi32.SelectObject(dc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(dc)
    user32.ReleaseDC(None, screen)
    user32.DestroyWindow(hwnd)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
