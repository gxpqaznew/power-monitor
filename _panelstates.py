"""把详情面板的几种「边界状态」各渲染一张，检查排版有没有挤爆 / 留白。

_paneltest.py 只画正常态。这里补三种容易出问题的：
  1. tariff_verify=True   —— 电价卡多一行警告，总高度 +18
  2. 曲线还没数据          —— 曲线卡是空态
  3. 刚开始统计            —— 页脚多一行「才开始计量」，且外推提示要出现
  4. 峰/平/谷三段齐全       —— 电价卡三个胶囊，检查会不会超出卡宽

输出 _preview/panel_state_*.png
"""

from __future__ import annotations

import ctypes
import struct
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import tariffs, w32
from powermon.config import Config
from powermon.meter import Snapshot, rate_at
from powermon.panel import Panel
from powermon.w32 import gdi32, user32

gdi32.GetDIBits.argtypes = [
    w32.wintypes.HDC, w32.wintypes.HBITMAP, w32.wintypes.UINT, w32.wintypes.UINT,
    ctypes.c_void_p, ctypes.POINTER(w32.BITMAPINFO), w32.wintypes.UINT,
]

OUT = Path(__file__).resolve().parent / "_preview"


def write_png(path: Path, bgra: bytes, w: int, h: int) -> None:
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        row = bgra[y * w * 4:(y + 1) * w * 4]
        for x in range(w):
            raw += bytes((row[x * 4 + 2], row[x * 4 + 1], row[x * 4]))
    raw = bytes(raw)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw, 6))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


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


def make_curve(now: float, count: int = 180):
    base = now - 1800
    out = []
    for i in range(count):
        v = 120 + 70 * (((i * 37) % 23) / 23.0) + (60 if 60 < i < 90 else 0)
        out.append((base + i * 10, v))
    return out


def render(cfg, snap, curve, out_name: str, scale: float = 1.0) -> tuple[int, int]:
    panel = Panel(cfg)
    panel.scale = scale
    # 真实窗口的客户区是物理像素：client_w/h 还会再乘一次 scale
    w, h = panel.s(panel.client_w), panel.s(panel.client_h)

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
    gdi32.GetDIBits(memdc, bmp, 0, h, ctypes.byref(buf),
                    ctypes.byref(info), w32.DIB_RGB_COLORS)
    write_png(OUT / out_name, bytes(buf), w, h)
    return w, h


def main() -> int:
    OUT.mkdir(exist_ok=True)
    now = time.time()

    # 1) 电价来源待核对：电价卡多一行
    cfg = Config()
    region = tariffs.find_region("四川")
    if region is not None:
        tariffs.apply_plan(cfg, region, region.default_plan())
    cfg.tariff_verify = True
    print(f"电价卡多一行 -> {render(cfg, make_snapshot(cfg, now), make_curve(now), 'panel_state_verify.png')}")

    # 2) 三段电价齐全（峰/平/谷三个胶囊）
    cfg = Config()
    region = tariffs.find_region("浙江")
    if region is not None:
        tariffs.apply_plan(cfg, region, region.default_plan())
    print(f"三段电价 -> {render(cfg, make_snapshot(cfg, now), make_curve(now), 'panel_state_3chips.png')}")

    # 3) 刚启动：曲线没数据、页脚提示「程序才开始计量」
    cfg = Config()
    snap = make_snapshot(
        cfg, now,
        session_wh=38.0, session_cost=0.02, covered_seconds=600.0,
        samples=300, current_w=228.0, cpu_w=140.0, gpu_w=53.0,
        cpu_util=78.0, power_on_ts=now - 14400.0, first_seen_ts=now - 600.0,
    )
    print(f"刚启动 / 无曲线 -> {render(cfg, snap, [], 'panel_state_early.png')}")

    # 4) 覆盖不足但已满 15 分钟：页脚给外推估算
    cfg = Config()
    snap = make_snapshot(
        cfg, now,
        session_wh=412.0, session_cost=0.22, covered_seconds=4200.0,
        samples=2100, current_w=118.0,
        power_on_ts=now - 21600.0, first_seen_ts=now - 4200.0,
    )
    print(f"外推估算 -> {render(cfg, snap, make_curve(now), 'panel_state_estimate.png')}")

    # 5) 超长电价方案名：检查胶囊里的省略号
    cfg = Config()
    cfg.tariff_region = "内蒙古自治区"
    cfg.tariff_plan = "蒙西地区居民生活用电一户一表峰谷分时电价"
    cfg.tariff_verify = True
    print(f"超长方案名 -> {render(cfg, make_snapshot(cfg, now), make_curve(now), 'panel_state_longname.png')}")

    # 6) 125% 缩放：本机就是 2560x1440 @125%，必须确认排版按比例放大后不出错。
    #    用安装版的实际配置（四川默认，tariff_verify=False），这张就是用户看到的真实样子。
    cfg = Config()
    print(f"125% 缩放 -> {render(cfg, make_snapshot(cfg, now), make_curve(now), 'panel_state_125pct.png', scale=1.25)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
