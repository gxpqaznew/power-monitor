"""真机把详情面板显示出来并截图，专门看四角是不是圆角。

离屏渲染（_paneltest.py）只画客户区，看不出窗口本身有没有被 DWM 切圆角，
必须真的把窗口显示出来再抓屏。

用法：
    python _panellive.py [out_name]
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


def grab(x0: int, y0: int, w: int, h: int, name: str) -> None:
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
    OUT_DIR.mkdir(exist_ok=True)
    write_png(OUT_DIR / name, buf, w, h)
    print(f"已写出 {OUT_DIR / name}  ({x0},{y0}) {w}x{h}")


def make_snapshot(cfg, now: float, **overrides) -> Snapshot:
    segment, rate = rate_at(cfg)
    base = dict(
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
    base.update(overrides)
    return Snapshot(**base)


def pump(seconds: float, hwnd=None) -> None:
    """自己跑消息循环。

    不跑的话 WM_PAINT 永远不会被派发 —— 窗口看着是空白的，会误判成「绘制坏了」。
    """
    msg = w32.wintypes.MSG()
    end = time.time() + seconds
    while time.time() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 1):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.02)


def main() -> int:
    enable_dpi_awareness()
    name = sys.argv[1] if len(sys.argv) > 1 else "panel_live_corner.png"

    cfg = Config()
    panel = Panel(cfg)
    if not panel.create():
        print("面板创建失败")
        return 1
    panel.set_data(make_snapshot(cfg, time.time()), [])
    panel.show_panel()
    pump(0.8)

    r = w32.wintypes.RECT()
    user32.GetWindowRect(panel._hwnd, ctypes.byref(r))
    print(f"窗口 ({r.left},{r.top})-({r.right},{r.bottom}) "
          f"{r.right - r.left}x{r.bottom - r.top}  "
          f"客户区 {panel._buffer_w}x{panel._buffer_h}")
    # 整窗抓一张（含四周各 8px 桌面背景），方便看四角到底圆不圆
    pad = 8
    grab(r.left - pad, r.top - pad,
         (r.right - r.left) + pad * 2, (r.bottom - r.top) + pad * 2, name)
    panel.destroy()
    print("已销毁")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
