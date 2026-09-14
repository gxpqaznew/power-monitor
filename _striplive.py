"""真机验证：把长条真的建出来、显示、截图，然后销毁。

为什么要在同一个进程里截图：agent 沙箱会把命令结束时还活着的进程一起清掉，
所以「先启动、再另起一个进程截图」是截不到的。这里全程在一个进程内完成：
建窗口 → 显示 → BitBlt 抓屏 → 写 PNG → 销毁，一步都不落。

用法：
    python _striplive.py [宽度] [x0] [out_name]
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

from powermon import debug, taskbar, w32  # noqa: E402
from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.config import Config  # noqa: E402
from powermon.meter import Snapshot, rate_at  # noqa: E402
from powermon.strip import TaskbarStrip  # noqa: E402
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


def main() -> int:
    enable_dpi_awareness()
    argv = sys.argv[1:]
    crop_w = int(argv[0]) if argv else 1300
    x0 = int(argv[1]) if len(argv) > 1 else 0
    name = argv[2] if len(argv) > 2 else "strip_live.png"

    cfg = Config()
    cfg.strip_enabled = True
    print(taskbar.describe())
    print(f"strip_position={cfg.strip_position}")

    snap = make_snapshot(cfg, time.time())
    strip = TaskbarStrip(cfg)
    ok = strip.create(snap)
    print(f"create()={ok} hwnd={strip.hwnd} 可见={strip.visible}")
    if not ok:
        return 1

    for _ in range(3):
        strip.tick(snap)
        time.sleep(0.25)

    info = taskbar.taskbar()
    top = info[1][1] - 8
    grab(x0, top, crop_w, info[1][3] - info[1][1] + 12, name)

    if strip.hwnd:
        r = wintypes_rect(strip.hwnd)
        print(f"长条实际窗口矩形 = {r}")
    strip.destroy()
    print("已销毁")
    return 0


def wintypes_rect(hwnd):
    r = w32.wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return (r.left, r.top, r.right, r.bottom)


if __name__ == "__main__":
    raise SystemExit(main())
