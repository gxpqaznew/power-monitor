"""长条闪烁探针：采样 IsWindowVisible / DWM cloaked / 相对任务栏的 z 序 / 矩形，
并抓几帧屏幕对比像素，判断闪烁属于哪种：tick 藏显振荡 / 被任务栏压住 / 重绘闪。
只读，不改任何状态。"""
import ctypes
import time
from ctypes import wintypes

import psutil

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from powermon.roundwin import dib_section  # noqa: E402

u = ctypes.windll.user32
g = ctypes.windll.gdi32
d = ctypes.windll.dwmapi
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

SRCCOPY = 0x00CC0020
ENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)


def cls(h):
    b = ctypes.create_unicode_buffer(64)
    u.GetClassNameW(h, b, 64)
    return b.value


def zorder():
    seen = []

    def cb(h, l):
        if u.IsWindowVisible(h):
            seen.append(h)
        return True
    u.EnumWindows(ENUMPROC(cb), None)
    return seen


def capture(rect):
    x, y, x2, y2 = rect
    w, h = x2 - x, y2 - y
    sdc = u.GetDC(None)
    mdc = g.CreateCompatibleDC(sdc)
    bmp, view = dib_section(mdc, w, h)
    old = g.SelectObject(mdc, bmp)
    g.BitBlt(mdc, 0, 0, w, h, sdc, x, y, SRCCOPY)
    g.SelectObject(mdc, old)
    g.DeleteObject(bmp)
    g.DeleteDC(mdc)
    u.ReleaseDC(None, sdc)
    return bytes(view)


def save_bmp(path, pixels, w, h):
    import struct
    row = w * 4
    header = struct.pack("<2sIHHI", b"BM", 14 + 40 + row * h, 0, 0, 14 + 40)
    info = struct.pack("<IiiHHIIiiII", 40, h, -h, 1, 32, 0, row * h,
                       2835, 2835, 0, 0)
    Path(path).write_bytes(header + info + pixels)


def main():
    # 谁在跑（确认是不是新版）
    for p in psutil.process_iter(["name", "exe"]):
        try:
            if p.info["name"] and "能耗统计" in p.info["name"]:
                print("proc:", p.info["exe"])
        except (psutil.NoSuchMethodError, psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    strip = u.FindWindowW("PowerMonitorTaskbarStrip", None)
    tray = u.FindWindowW("Shell_TrayWnd", None)
    if not strip:
        print("!! 找不到长条窗口（可能没装新版 / strip 功能关了）")
        return 1
    r = wintypes.RECT()
    u.GetWindowRect(strip, ctypes.byref(r))
    rect = (r.left, r.top, r.right, r.bottom)
    print(f"strip={strip:#x} tray={tray:#x} rect={rect}")

    t0 = time.time()
    prev = None
    shots = {}
    cap_at = {8: "A", 25: "B", 45: "C", 65: "D", 85: "E", 105: "F"}
    for i in range(120):
        vis = bool(u.IsWindowVisible(strip))
        cloaked = ctypes.c_uint(0)
        d.DwmGetWindowAttribute(strip, 14, ctypes.byref(cloaked), 4)  # DWMWA_CLOAKED
        zo = zorder()
        s_in = zo.index(strip) if strip in zo else -1
        t_in = zo.index(tray) if tray in zo else -1
        rr = wintypes.RECT()
        u.GetWindowRect(strip, ctypes.byref(rr))
        fg = cls(u.GetForegroundWindow())
        cur = (vis, cloaked.value, s_in - t_in,
               (rr.left, rr.top, rr.right, rr.bottom), fg)
        if cur != prev:
            print(f"{time.time()-t0:6.2f}s vis={vis} cloaked={cloaked.value} "
                  f"z_rel={s_in - t_in} rect={(rr.left, rr.top, rr.right, rr.bottom)} fg={fg}")
            prev = cur
        if i in cap_at:
            shots[cap_at[i]] = capture(rect)
        time.sleep(0.08)

    names = sorted(shots)
    print("\n=== 帧对比（不同字节数，>0 即视觉上有变化）===")
    for a, b in zip(names, names[1:]):
        diff = sum(1 for x, y in zip(shots[a], shots[b]) if x != y)
        total = len(shots[a])
        print(f"{a}->{b}: {diff}/{total} bytes differ ({diff*100//total}%)")
    out = Path("_preview")
    out.mkdir(exist_ok=True)
    w, h = rect[2] - rect[0], rect[3] - rect[1]
    for n in (names[0], names[-1]):
        save_bmp(out / f"flicker_{n}.bmp", shots[n], w, h)
        print("saved", out / f"flicker_{n}.bmp")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
