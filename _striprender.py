"""离屏渲染任务栏长条并导出 PNG。

长条本身是盖在任务栏上的置顶窗口，肉眼对着真实任务栏看不清细节（它只有
47px 高，而且底色是采样来的），所以这里把同一套绘制代码跑在一块假任务栏
底板上，逐像素把预乘 alpha 合成回去，导成 PNG 检查：

  1. 浅色任务栏（本机就是这种，SystemUsesLightTheme=1）
  2. 深色任务栏
  3. 125% 缩放（本机实际 DPI）
  4. 数据很小 / 很大时的字段取舍（宽度不够要从后往前丢字段）

输出 _preview/strip_*.png
"""

from __future__ import annotations

import ctypes
import struct
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import strip as stripmod
from powermon import w32
from powermon.config import Config
from powermon.meter import Snapshot, rate_at
from powermon.strip import TaskbarStrip
from powermon.w32 import gdi32, user32, wintypes

gdi32.GetDIBits.argtypes = [
    wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
    wintypes.UINT, ctypes.c_void_p,
    ctypes.POINTER(w32.BITMAPINFO), wintypes.UINT,
]

OUT = Path(__file__).resolve().parent / "_preview"

# 假的「任务栏底色」。Win11 浅色任务栏实测接近 #F3F3F3，深色接近 #202020。
LIGHT_BAR = (243, 243, 243)
DARK_BAR = (32, 32, 32)


def write_png(path: Path, rgb: bytes, w: int, h: int) -> None:
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        raw += rgb[y * w * 3:(y + 1) * w * 3]
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


def render(strip, snap, bar_rgb, taskbar_h: int, out_name: str,
           scale: float = 1.0) -> tuple[int, int]:
    """把长条画在一块 taskbar_h 高的假任务栏底板上。

    和真实代码同构：先把长条画进一块「刚好长条大小」的 DIB，用
    ``compose_shape_alpha`` 补 alpha（GDI 会把 alpha 写 0，必须补），
    再逐像素预乘合成到假任务栏底板上。
    """
    light = sum(bar_rgb) > 3 * 128
    # 采样函数直接给固定值：离屏没有真实桌面可采
    strip._sample_taskbar = lambda *a, **k: bar_rgb  # type: ignore[method-assign]
    stripmod.taskbar.uses_light_theme = lambda: light  # type: ignore[assignment]

    height = max(18, int(round(taskbar_h * stripmod._HEIGHT_RATIO)))
    offset = (taskbar_h - height) // 2

    screen = user32.GetDC(None)
    tmp = gdi32.CreateCompatibleDC(screen)
    try:
        width = strip._layout(tmp, scale, snap, render=False)[0]
    finally:
        gdi32.DeleteDC(tmp)
    if width <= 0:
        raise SystemExit("长条宽度算不出来")

    # 画布 = 长条 + 右侧留一个「开始按钮」的间隙 + 一个假开始按钮
    gap = int(round(16 * scale))
    start_w = int(round(57 * scale))
    canvas_w = width + gap + start_w + int(round(24 * scale))
    canvas_h = taskbar_h

    # ---- 1) 长条本体画进自己的 DIB ----
    pill = gdi32.CreateCompatibleDC(screen)
    try:
        bmp, view = stripmod.dib_section(pill, width, height)
        if not bmp:
            raise SystemExit("DIB 创建失败")
        old_bmp = gdi32.SelectObject(pill, bmp)
        strip._rect = (0, 0, width, height)   # 让采样拿到非零基准
        strip._layout(pill, scale, snap, render=True,
                      origin_x=0, origin_y=0, height=height)
        stripmod.compose_shape_alpha(
            view, width, height, margin=0,
            radius=min(stripmod._RADIUS, height / 2.0),
            shape_w=width, shape_h=height, shadow=0,
        )
        pill_px = bytes(view)
        gdi32.SelectObject(pill, old_bmp)
        gdi32.DeleteObject(bmp)
    finally:
        gdi32.DeleteDC(pill)
        user32.ReleaseDC(None, screen)

    strip._rect = None

    # ---- 2) 合成到假任务栏底板上 ----
    out = bytearray(bytes(bar_rgb) * (canvas_w * canvas_h))
    for y in range(height):
        for x in range(width):
            i = (y * width + x) * 4
            a = pill_px[i + 3] / 255.0
            o = ((y + offset) * canvas_w + x) * 3
            out[o] = min(255, int(round(pill_px[i + 2] + bar_rgb[0] * (1 - a))))
            out[o + 1] = min(255, int(round(pill_px[i + 1] + bar_rgb[1] * (1 - a))))
            out[o + 2] = min(255, int(round(pill_px[i] + bar_rgb[2] * (1 - a))))
    _fake_start(out, canvas_w, canvas_h, width + gap, start_w, scale,
                offset, height)
    write_png(OUT / out_name, bytes(out), canvas_w, canvas_h)

    print(f"{out_name}: 长条 {width}x{height}  画布 {canvas_w}x{canvas_h}  "
          f"缩放 {scale}")
    return width, height


def _fake_start(out, w, h, x0, start_w, scale, offset, height) -> None:
    """画一个假「开始按钮」——四个色块，只为标出长条右边该留多少空。"""
    cell = max(2, int(round(9 * scale)))
    gap = max(1, int(round(3 * scale)))
    total = cell * 2 + gap
    left = x0 + (start_w - total) // 2
    up = offset + (height - total) // 2
    blue = (0x4C, 0x8B, 0xF5)
    for r in range(2):
        for c in range(2):
            for dy in range(cell):
                for dx in range(cell):
                    px = left + c * (cell + gap) + dx
                    py = up + r * (cell + gap) + dy
                    if px >= w or py >= h:
                        continue
                    o = (py * w + px) * 3
                    out[o], out[o + 1], out[o + 2] = blue


def main() -> int:
    OUT.mkdir(exist_ok=True)
    now = time.time()
    cfg = Config()
    cfg.strip_enabled = True

    taskbar_h = 60  # 本机 2560x1440 @125% 实测任务栏高 60px

    strip = TaskbarStrip(cfg)
    render(strip, make_snapshot(cfg, now), LIGHT_BAR, taskbar_h,
           "strip_light_125.png", scale=1.25)
    render(strip, make_snapshot(cfg, now), DARK_BAR, taskbar_h,
           "strip_dark_125.png", scale=1.25)
    render(strip, make_snapshot(cfg, now), LIGHT_BAR, 48,
           "strip_light_100.png", scale=1.0)

    # 数字很小：本次电量不到 1kWh，看 Wh 分支和位数
    render(strip, make_snapshot(
        cfg, now, session_wh=96.4, session_cost=0.05, today_wh=310.0,
        current_w=41.0, cpu_w=18.0, gpu_w=9.0, cpu_util=12.0,
    ), LIGHT_BAR, taskbar_h, "strip_small_125.png", scale=1.25)

    # 数字很大：三位数功率 + 四位数电费
    render(strip, make_snapshot(
        cfg, now, session_wh=3421.0, session_cost=12.84, today_wh=3890.0,
        current_w=286.0, cpu_w=142.0, gpu_w=108.0, cpu_util=93.0,
    ), LIGHT_BAR, taskbar_h, "strip_big_125.png", scale=1.25)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
