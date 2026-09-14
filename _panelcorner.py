"""把详情面板四个角各抓一张特写，拼成 2x2 一张 PNG，直接看圆不圆。

为什么之前会误判：面板浮在桌面/别的窗口上面，那片背景本身就接近白色，
和面板卡片底色 (248,248,248) 只差几个灰阶，用「颜色差 > 40 就算角被切掉」
的数值判据完全分不出来，于是把圆角误判成直角。
这次改成直接看图 —— 和 _roundtest.py 里那个已知圆角的对照窗口一个方法。

用法：
    python _panelcorner.py
"""

from __future__ import annotations

import ctypes
import os
import struct
import sys
import time
import zlib
from pathlib import Path

os.environ.setdefault("POWERMON_DEBUG", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import w32  # noqa: E402
from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.config import Config  # noqa: E402
from powermon.meter import Snapshot, rate_at  # noqa: E402
from powermon.panel import Panel  # noqa: E402
from powermon.w32 import gdi32, user32  # noqa: E402

OUT_DIR = Path(__file__).resolve().parent / "_preview"
SRCCOPY = 0x00CC0020
TILE = 72
GAP = 10
ZOOM = 4


def make_snapshot(cfg, now: float) -> Snapshot:
    segment, rate = rate_at(cfg)
    return Snapshot(
        session_wh=428.3, session_cost=0.32, covered_seconds=7350.0,
        average_w=118.0, peak_w=386.0, samples=3675, peak_wh=353.0,
        valley_wh=75.3, current_w=132.0, cpu_w=62.0, gpu_w=35.0, base_w=35.0,
        cpu_util=41.0, in_valley=False, today_wh=1240.0, today_cost=0.86,
        total_wh=45678.9, total_sessions=12,
        power_on_ts=now - 17700.0, power_on_source="电源事件",
        first_seen_ts=now - 7350.0,
        cpu_source="PDH 负载模型", gpu_source="nvml", cpu_estimated=True,
        gpu_measured=True, gpu_names=["NVIDIA GeForce RTX 3080"],
        gpu_limits=[320.0], segment=segment, rate=rate,
    )


def grab_bgra(x0: int, y0: int, w: int, h: int) -> bytes:
    screen = user32.GetDC(None)
    mem_dc = gdi32.CreateCompatibleDC(screen)
    info = w32.BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(w32.BITMAPINFOHEADER)
    info.bmiHeader.biWidth = w
    info.bmiHeader.biHeight = -h
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = 0
    bits = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(mem_dc, ctypes.byref(info), 0,
                                 ctypes.byref(bits), None, 0)
    old = gdi32.SelectObject(mem_dc, bmp)
    gdi32.BitBlt(mem_dc, 0, 0, w, h, screen, x0, y0, SRCCOPY)
    buf = ctypes.string_at(bits, w * h * 4)
    gdi32.SelectObject(mem_dc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(None, screen)
    return buf


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
    msg = w32.wintypes.MSG()
    end = time.time() + seconds
    while time.time() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.02)


def main() -> int:
    enable_dpi_awareness()
    cfg = Config()
    panel = Panel(cfg)
    if not panel.create():
        print("面板创建失败")
        return 1
    panel.set_data(make_snapshot(cfg, time.time()), [])
    panel.show_panel()
    pump(1.0)

    r = w32.wintypes.RECT()
    user32.GetWindowRect(panel._hwnd, ctypes.byref(r))
    L, T, R, B = r.left, r.top, r.right, r.bottom
    print(f"面板窗口 = ({L},{T})-({R},{B}) {R - L}x{B - T}")

    off = 6
    spots = {
        "左上": (L - off, T - off),
        "右上": (R + off - TILE, T - off),
        "左下": (L - off, B + off - TILE),
        "右下": (R + off - TILE, B + off - TILE),
    }
    tiles = {k: grab_bgra(x, y, TILE, TILE) for k, (x, y) in spots.items()}

    # 拼成 2x2 大图再放大 ZOOM 倍
    gw = TILE * 2 + GAP
    gh = TILE * 2 + GAP
    canvas = bytearray(gw * gh * 4)
    order = (("左上", 0, 0), ("右上", TILE + GAP, 0),
             ("左下", 0, TILE + GAP), ("右下", TILE + GAP, TILE + GAP))
    for name, ox, oy in order:
        src = tiles[name]
        for y in range(TILE):
            dst = ((oy + y) * gw + ox) * 4
            canvas[dst:dst + TILE * 4] = src[y * TILE * 4:(y + 1) * TILE * 4]

    big = bytearray()
    bw, bh = gw * ZOOM, gh * ZOOM
    for y in range(bh):
        sy = y // ZOOM
        for x in range(bw):
            p = (sy * gw + (x // ZOOM)) * 4
            big += canvas[p:p + 4]

    OUT_DIR.mkdir(exist_ok=True)
    out = OUT_DIR / "panel_corners_4x4.png"
    write_png(out, bytes(big), bw, bh)
    print(f"已写出 {out} ({bw}x{bh})")

    panel.destroy()
    print("已销毁")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
