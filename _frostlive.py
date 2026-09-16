"""真机抽查毛玻璃：长条 + 右键菜单是不是真的「糊了背后的东西」。

判据分两层，缺一不可：

  A. **像素层**：卡片内部不能是纯色。实色卡片那一块只有 1~2 种取值，毛玻璃会
     把背后的桌面糊进来，取值会散开（本机实测实色 = 1 种，毛玻璃 = 十几种以上）。
     这一条是"真的抓屏了"的硬证据 —— 只看 PNG 好看不够，粗心写死一个渐变色也能
     好看。
  B. **肉眼层**：同时写出放大 3 倍的 PNG，人眼确认糊的纹路像不像毛玻璃。

⚠️ 右键必须用 **SendInput 真输入**：合成 SendMessageW 不给前台权，自绘菜单会被
系统收回焦点秒关（KILLFOCUS），那测出来的是假阴性。
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

from powermon.w32 import GWL_EXSTYLE, WS_EX_TRANSPARENT, gdi32, user32  # noqa: E402

OUT = Path(__file__).resolve().parent / "_preview"
SRCCOPY = 0x00CC0020
PASS = FAIL = 0
ctypes.windll.shcore.SetProcessDpiAwareness(1)


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


def write_png(path: Path, bgra: bytes, w: int, h: int, zoom: int = 1) -> None:
    zw, zh = w * zoom, h * zoom
    raw = bytearray()
    for y in range(zh):
        raw.append(0)
        sy = y // zoom
        row = bgra[sy * w * 4:(sy + 1) * w * 4]
        line = bytearray()
        for x in range(zw):
            sx = x // zoom
            line += bytes((row[sx * 4 + 2], row[sx * 4 + 1], row[sx * 4]))

        raw += line

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", zw, zh, 8, 2, 0, 0, 0))
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


def class_of(hwnd) -> str:
    buf = ctypes.create_unicode_buffer(64)
    user32.GetClassNameW(hwnd, buf, 64)
    return buf.value


def find_strip():
    tray = user32.FindWindowW("Shell_TrayWnd", None)
    if not tray:
        return None
    hits = []
    enum = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @enum
    def cb(h, lp):
        if class_of(h) == "PowerMonitorTaskbarStrip":
            hits.append(h)
        return True
    user32.EnumChildWindows(tray, cb, 0)
    return hits[0] if hits else None


def find_menu():
    hits = []
    enum = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @enum
    def cb(h, lp):
        if class_of(h) == "PowerMonitorCtxMenu" and user32.IsWindowVisible(h):
            hits.append(h)
        return True
    user32.EnumWindows(cb, 0)
    return hits[0] if hits else None


def real_right_click(x: int, y: int) -> None:
    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("mi", MOUSEINPUT)]

    user32.SetCursorPos(x, y)
    time.sleep(0.3)
    for flags in (0x0008, 0x0010):     # RIGHTDOWN / RIGHTUP
        inp = INPUT(0, MOUSEINPUT(0, 0, 0, flags, 0, 0))
        user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))
        time.sleep(0.05)


def dark_stats(bgra: bytes, w: int, h: int, x0: int, y0: int, x1: int, y1: int):
    """只统计「暗像素」（卡片底色）。文字、卡片外的桌面 / 阴影都是亮的，
    这样不用手工算 margin 也能取到纯底色那一片。"""
    seen = {}
    mn = 255
    mx = 0
    total = 0
    n = 0
    total_all = 0
    n_all = 0
    for y in range(y0, y1):
        base = y * w * 4
        for x in range(x0, x1):
            i = base + x * 4
            g = (bgra[i] + bgra[i + 1] + bgra[i + 2]) / 3.0
            n_all += 1
            total_all += g
            if g < 120:
                rgb = (bgra[i + 2], bgra[i + 1], bgra[i])
                seen[rgb] = seen.get(rgb, 0) + 1
                mn = min(mn, g)
                mx = max(mx, g)
                total += g
                n += 1
    frac = n / max(1, n_all)
    mean = total / max(1, n)
    # 底色的「起伏」只能在**主色簇**里量：暗像素里还混着抗锯齿的字边（0~120 全
    # 都有），拿全体极差当判据会被字边撑到 120，明明只有底色在起伏也判不过。
    # 取出现最多的那个色，再看灰度落在它 ±30 内的像素铺开多少 —— 实色底这块恒为
    # 0，毛玻璃底会有几个到几十个灰阶。
    core = _smoothness(bgra, w, h, x0, y0, x1, y1)
    return len(seen), core, mean, frac


def _smoothness(bgra: bytes, w: int, h: int, x0: int, y0: int, x1: int, y1: int) -> float:
    """相邻像素灰度差的中位数（只数两边都够暗的像素对）。

    「是糊的还是噪点」看这个最准：纯色底 = 0；毛玻璃是平滑过渡，中位差 1~3；
    真噪点会跳到十几。用中位数而不是均值，字边那种孤立大跳变影响不了它。"""
    diffs = []
    for y in range(y0, y1):
        base = y * w * 4
        prev = None
        for x in range(x0, x1):
            i = base + x * 4
            g = (bgra[i] + bgra[i + 1] + bgra[i + 2]) / 3.0
            if g < 100:
                if prev is not None:
                    diffs.append(abs(g - prev))
                prev = g
            else:
                prev = None
    if not diffs:
        return -1.0
    diffs.sort()
    return diffs[len(diffs) // 2]


def interior_stats(bgra: bytes, w: int, h: int, x0: int, y0: int,
                   x1: int, y1: int):
    """取一块矩形内像素的 (不同取值数, 最大偏离, 平均色)。"""
    seen = {}
    mn = [255, 255, 255]
    mx = [0, 0, 0]
    total = [0, 0, 0]
    n = 0
    for y in range(y0, y1):
        base = y * w * 4
        for x in range(x0, x1):
            i = base + x * 4
            b, g, r = bgra[i], bgra[i + 1], bgra[i + 2]
            seen[(r, g, b)] = seen.get((r, g, b), 0) + 1
            for k, v in enumerate((r, g, b)):
                mn[k] = min(mn[k], v)
                mx[k] = max(mx[k], v)
                total[k] += v
            n += 1
    mean = tuple(round(t / max(1, n)) for t in total)
    spread = max(mx[k] - mn[k] for k in range(3))
    return len(seen), spread, mean


def main() -> int:
    OUT.mkdir(exist_ok=True)
    strip = find_strip()
    if not strip:
        print("找不到长条窗口 —— 先在任务栏上跑起来")
        return 1

    r = wintypes.RECT()
    user32.GetWindowRect(strip, ctypes.byref(r))
    print(f"长条 rect = ({r.left},{r.top},{r.right},{r.bottom})")

    # ---- 1. 长条本体 ----
    pad = 10
    sw, sh = r.right - r.left, r.bottom - r.top
    sx, sy = r.left - pad, r.top - pad
    strip_px = grab(sx, sy, sw + pad * 2, sh + pad * 2)
    write_png(OUT / "frost_strip_live.png", strip_px, sw + pad * 2, sh + pad * 2, zoom=3)
    # 胶囊内部取一条窄带（避开描边和字）：贴着上边缘往里 6px
    # 采样带要落在胶囊**内部的上半**：胶囊 36px 高，文字竖直居中（约 y=11~25），
    # 所以 y=4~10 是纯底色。之前把带子写在 6~14（相对抓图，而 pad=10）等于采到了
    # 长条**上方**的任务栏，读数全是桌面色，白忙一场。
    n_col, spread, mean = interior_stats(strip_px, sw + pad * 2, sh + pad * 2,
                                         pad + 30, pad + 4, pad + 200, pad + 10)
    print(f"长条内部：不同取值 {n_col} 种，最大极差 {spread}，均值 {mean}")
    check("长条内部不是纯色（背后任务栏真的被糊进来了）", n_col > 4,
          f"{n_col} 种取值")

    # ---- 2. 右键菜单（真输入）----
    # 🔴 先看长条是不是「锁定位置」状态。锁定时程序会给它加回 WS_EX_TRANSPARENT，
    #    也就是**故意点击穿透**（这样才不会挡住任务栏那一片的点击）；此时在长条上
    #    右键会被任务栏吃掉，拿不到菜单 —— 这是设计行为，不是 bug。
    #    这个判据以前没看这个开关，碰上用户把长条锁了就会假 FAIL（踩过）。
    locked = bool(user32.GetWindowLongPtrW(strip, GWL_EXSTYLE) & WS_EX_TRANSPARENT)
    if locked:
        print("[SKIP] 长条处于「锁定位置」状态（WS_EX_TRANSPARENT = 故意点击穿透），"
              "在长条上右键本来就该落到任务栏上，不适用这项判据。")
        print("       想看菜单毛玻璃：右键**托盘图标**（_menulive.py 走那条路），"
              "或先在托盘菜单里点「解锁位置」再跑本脚本。")
    cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
    if not locked:
        real_right_click(cx, cy)
        time.sleep(1.2)
    menu = None if locked else find_menu()
    if not locked and not menu:
        check("真输入右键后菜单存在", False)
        return 1
    if menu:
        mr = wintypes.RECT()
        user32.GetWindowRect(menu, ctypes.byref(mr))
        mw, mh = mr.right - mr.left, mr.bottom - mr.top
        print(f"菜单 rect = ({mr.left},{mr.top},{mr.right},{mr.bottom}) {mw}x{mh}")
        m_pad = 8
        menu_px = grab(mr.left - m_pad, mr.top - m_pad, mw + m_pad * 2, mh + m_pad * 2)
        write_png(OUT / "frost_menu_live.png", menu_px, mw + m_pad * 2,
                  mh + m_pad * 2, zoom=3)

        # 菜单卡片：只统计暗像素（底色），文字和卡片外的阴影 / 桌面自动被滤掉
        n_col2, spread2, mean2, frac2 = dark_stats(
            menu_px, mw + m_pad * 2, mh + m_pad * 2,
            m_pad, m_pad, m_pad + mw, m_pad + mh)
        print(f"菜单：暗像素占比 {frac2:.2f}，其中不同取值 {n_col2} 种，"
              f"相邻灰度差中位 {spread2:.1f}，均值 {mean2:.0f}")

        check("菜单主体仍是深色卡片", frac2 > 0.4, f"暗像素占比 {frac2:.2f}")
        check("卡片底色不是纯色（背后真的被糊进来了）", n_col2 > 4,
              f"暗像素里有 {n_col2} 种取值")
        check("底色是平滑过渡不是噪点（相邻灰度差中位数够小）",
              0 <= spread2 <= 6, f"中位 |Δ| = {spread2:.1f}")

        # 关掉菜单
        user32.keybd_event(0x1B, 0, 0, 0)
        user32.keybd_event(0x1B, 0, 2, 0)
        time.sleep(0.6)

    print(f"\n图片已写到 {OUT}")
    print(f"通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
