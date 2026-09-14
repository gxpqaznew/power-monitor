"""离屏渲染详情面板并导出 PNG，用于肉眼检查版面（无需弹出真实窗口）。"""

import ctypes
import struct
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import w32
from powermon import tariffs
from powermon.config import Config
from powermon.meter import Snapshot
from powermon.panel import Panel
from powermon.w32 import gdi32, user32, wintypes

gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
    ctypes.c_void_p, ctypes.POINTER(w32.BITMAPINFO), wintypes.UINT,
]


def write_png(path: Path, bgra: bytes, w: int, h: int) -> None:
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        row = bgra[y * w * 4:(y + 1) * w * 4]
        for x in range(w):
            b, g, r = row[x * 4], row[x * 4 + 1], row[x * 4 + 2]
            raw += bytes((r, g, b))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


cfg = Config.load()
# 可选：传一个省份名，用该省预设渲染一遍（检查峰段 / 丰枯这些分支的排版）。
# 例：python _paneltest.py 浙江
_region_arg = sys.argv[1].strip() if len(sys.argv) > 1 else ""
_region = tariffs.find_region(_region_arg) if _region_arg else None
if _region is not None:
    tariffs.apply_plan(cfg, _region, _region.default_plan())
    print(f"用「{_region.name} · {cfg.tariff_plan}」渲染")
elif _region_arg:
    print(f"认不出省份「{_region_arg}」，按当前配置渲染")
now = time.time()

from powermon.meter import rate_at  # noqa: E402

_segment, _rate = rate_at(cfg)

snap = Snapshot(
    session_wh=428.3, session_cost=0.32, covered_seconds=7350.0,
    average_w=118.0, peak_w=386.0, samples=3675, peak_wh=353.0, valley_wh=75.3,
    current_w=132.0, cpu_w=62.0, gpu_w=35.0, base_w=35.0, cpu_util=41.0,
    in_valley=False, today_wh=1240.0, today_cost=0.86,
    total_wh=45678.9, total_sessions=12,
    power_on_ts=now - 17700.0, power_on_source="电源事件",
    first_seen_ts=now - 7350.0,
    cpu_source="PDH 负载模型", gpu_source="nvml", cpu_estimated=True,
    gpu_measured=True, gpu_names=["NVIDIA GeForce RTX 3080"], gpu_limits=[320.0],
    segment=_segment, rate=_rate,
)

curve = []
base = now - 1800
for i in range(180):
    t = base + i * 10
    v = 120 + 70 * (((i * 37) % 23) / 23.0) + (60 if 60 < i < 90 else 0)
    curve.append((t, v))

panel = Panel(cfg)
w, h = panel.client_w, panel.client_h

screen = user32.GetDC(None)
memdc = gdi32.CreateCompatibleDC(screen)
bmp = gdi32.CreateCompatibleBitmap(screen, w, h)
gdi32.SelectObject(memdc, bmp)

panel._buffer_dc = memdc
panel._buffer_w = w
panel._buffer_h = h
panel._snap = snap
panel._curve = curve
panel._draw(memdc)

info = w32.BITMAPINFO()
info.bmiHeader.biSize = ctypes.sizeof(w32.BITMAPINFOHEADER)
info.bmiHeader.biWidth = w
info.bmiHeader.biHeight = -h
info.bmiHeader.biPlanes = 1
info.bmiHeader.biBitCount = 32
info.bmiHeader.biCompression = w32.BI_RGB

buf = (ctypes.c_ubyte * (w * h * 4))()
gdi32.GetDIBits(memdc, bmp, 0, h, ctypes.byref(buf), ctypes.byref(info), w32.DIB_RGB_COLORS)

out = Path(__file__).parent / "_preview" / (
    f"panel_{_region.name}.png" if _region is not None else "panel.png"
)
write_png(out, bytes(buf), w, h)
print(f"面板渲染完成 -> {out}  ({w}x{h})")
