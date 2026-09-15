"""离屏渲染任务栏长条并导出 PNG。

长条本身是盖在任务栏上的置顶窗口，肉眼对着真实任务栏看不清细节（它只有
47px 高，而且底色是采样来的），所以这里把同一套绘制代码跑在一块假任务栏
底板上，逐像素把预乘 alpha 合成回去，导成 PNG 检查：

  1. 浅色任务栏（本机就是这种，SystemUsesLightTheme=1）
  2. 深色任务栏
  3. 125% 缩放（本机实际 DPI）
  4. 数据很小 / 很大时的字段取舍（宽度不够要从后往前丢字段）
  5. 六种质感、三档大小、五档字号、不同显示内容组合 —— 也就是托盘菜单里
     能调的每一项都出一张，最后拼成对照图（``*_sheet_*.png``）

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

from powermon import stripopts
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

# 本次渲染出来的所有画布，最后拼成对照图（见 write_sheet）
_SHEET: list[tuple[str, bytes, int, int]] = []
# 按文件名留一份所有画布：放大特写要按名字回头取（_SHEET 每段都会清）
_CANVAS: dict[str, tuple[bytes, int, int]] = {}

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

    高度走 ``stripopts.height_ratio`` —— 和真实代码同一个来源，这样「大小 / 字号」
    档位在预览里也是准的，不会出现「预览好好的、装上去不一样」。
    """
    light = sum(bar_rgb) > 3 * 128
    # 采样函数直接给固定值：离屏没有真实桌面可采
    strip._sample_taskbar = lambda *a, **k: bar_rgb  # type: ignore[method-assign]
    stripmod.taskbar.uses_light_theme = lambda: light  # type: ignore[assignment]

    height = max(18, int(round(taskbar_h * stripopts.height_ratio(strip.cfg))))
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
        # _highlight（玻璃 / 强调色要铺高光）写的是 self._view，正常路径由
        # _render_if_needed 建缓冲区时挂上；离屏路径下得手动挂
        strip._view, strip._w, strip._h = view, width, height
        _w, _h, pal = strip._layout(pill, scale, snap, render=True,
                                    origin_x=0, origin_y=0, height=height)
        stripmod.compose_shape_alpha(
            view, width, height, margin=0,
            radius=min(stripmod._RADIUS, height / 2.0),
            shape_w=width, shape_h=height, shadow=0,
            # 半透明质感整块压 alpha；线框质感用哨兵色抠出透明底
            shape_alpha=pal["alpha"], key_rgb=pal["key"],
        )
        pill_px = bytes(view)
        gdi32.SelectObject(pill, old_bmp)
        gdi32.DeleteObject(bmp)
    finally:
        gdi32.DeleteDC(pill)
        user32.ReleaseDC(None, screen)

    # bmp 已经删了，_view 会变成悬空视图，必须清掉
    strip._view = None
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
    _SHEET.append((out_name, bytes(out), canvas_w, canvas_h))
    _CANVAS[out_name] = (bytes(out), canvas_w, canvas_h)

    print(f"{out_name}: 长条 {width}x{height}  画布 {canvas_w}x{canvas_h}  "
          f"缩放 {scale}")
    return width, height


def write_sheet(path: Path, pad: int = 4) -> None:
    """把这次渲染的所有画布竖着拼成一张「对照图」。

    单看一张 PNG 很难判断「玻璃和深色卡片到底差多少」「字号五档差多少」，
    拼在一起一眼就能比完。
    """
    if not _SHEET:
        return
    max_w = max(w for _n, _b, w, _h in _SHEET)
    total_h = sum(h for _n, _b, _w, h in _SHEET) + pad * (len(_SHEET) - 1)
    canvas = bytearray(b"\x14\x14\x14" * (max_w * total_h))
    y0 = 0
    for _name, buf, w, h in _SHEET:
        for y in range(h):
            src = y * w * 3
            dst = (y0 + y) * max_w * 3
            canvas[dst:dst + w * 3] = buf[src:src + w * 3]
        y0 += h + pad
    write_png(path, bytes(canvas), max_w, total_h)
    print(f"\n对照图 {path.name}: {max_w}x{total_h}"
          f"（本次 {len(_SHEET)} 张，自上而下按输出顺序）")


def magnify_sheet(names, path: Path, crop, zoom: int = 3, pad: int = 4) -> None:
    """把若干张画布的**同一小块区域**放大后竖排，专门用来看细节。

    缩略图上看不出来的毛病（文字镶边 / 圆角毛刺 / 描边断层）只有放大才看得见 ——
    之前吃过「按角落像素颜色差做数值判据」的亏，那会假阴性；放大特写是可靠办法。

    ``crop`` = (x0, y0, x1, y1)。
    """
    rows = []
    for name in names:
        got = _CANVAS.get(name)
        if got is None:
            continue
        buf, w, h = got
        x0, y0, x1, y1 = crop
        x0, y0 = max(0, x0), max(0, y0)
        x1, y1 = min(w, x1), min(h, y1)
        cw, ch = x1 - x0, y1 - y0
        if cw <= 0 or ch <= 0:
            continue
        big = bytearray(cw * zoom * ch * zoom * 3)
        row_bytes = cw * zoom * 3
        for y in range(ch):
            # 先把这一行横向放大
            line = bytearray(row_bytes)
            for x in range(cw):
                src = ((y0 + y) * w + x0 + x) * 3
                px = buf[src:src + 3]
                for z in range(zoom):
                    d = x * zoom * 3 + z * 3
                    line[d:d + 3] = px
            # 再整行重复 zoom 遍，纵向放大
            for z in range(zoom):
                d = ((y * zoom + z)) * row_bytes
                big[d:d + row_bytes] = line
        rows.append((big, cw * zoom, ch * zoom))
    if not rows:
        return
    max_w = max(w for _b, w, _h in rows)
    total_h = sum(h for _b, _w, h in rows) + pad * (len(rows) - 1)
    canvas = bytearray(b"\x14\x14\x14" * (max_w * total_h))
    y0 = 0
    for buf, w, h in rows:
        for y in range(h):
            src = y * w * 3
            dst = (y0 + y) * max_w * 3
            canvas[dst:dst + w * 3] = buf[src:src + w * 3]
        y0 += h + pad
    write_png(path, bytes(canvas), max_w, total_h)
    print(f"放大特写 {path.name}: {max_w}x{total_h}（{zoom}x，裁 {crop}，"
          f"{len(rows)} 张）")


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


def _cfg(**over) -> Config:
    """造一份只改了指定几项的长条配置。"""
    made = Config()
    made.strip_enabled = True
    for name, val in over.items():
        setattr(made, name, val)
    return made


def main() -> int:
    OUT.mkdir(exist_ok=True)
    now = time.time()

    taskbar_h = 60  # 本机 2560x1440 @125% 实测任务栏高 60px

    # ---- 默认档 + 深浅任务栏 / 不同 DPI / 字段取舍（原有回归）----
    cfg = _cfg()
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
    write_sheet(OUT / "strip_sheet_default.png")

    # ---- 六种质感（同一份数据、同一个浅色任务栏）----
    _SHEET.clear()
    for theme_key, _label, _hint in stripopts.THEMES:
        one = _cfg(strip_theme=theme_key)
        probe = TaskbarStrip(one)
        try:
            render(probe, make_snapshot(one, now), LIGHT_BAR, taskbar_h,
                   f"strip_theme_{theme_key}.png", scale=1.25)
        finally:
            probe.destroy()
    write_sheet(OUT / "strip_sheet_themes.png")
    # 文字边缘 / 胶囊左端圆角放大 3 倍：线框主题的抠色毛边只有这样才能看出来
    magnify_sheet([f"strip_theme_{k}.png" for k, _l, _h in stripopts.THEMES],
                  OUT / "strip_zoom_themes.png", crop=(0, 4, 168, 56), zoom=3)

    # ---- 三档大小 ----
    _SHEET.clear()
    for size_key, _label, _ratio, _pad in stripopts.SIZES:
        one = _cfg(strip_size=size_key)
        probe = TaskbarStrip(one)
        try:
            render(probe, make_snapshot(one, now), LIGHT_BAR, taskbar_h,
                   f"strip_size_{size_key}.png", scale=1.25)
        finally:
            probe.destroy()
    write_sheet(OUT / "strip_sheet_sizes.png")

    # ---- 五档字号 ----
    _SHEET.clear()
    for index, (scale_value, _label) in enumerate(stripopts.FONT_SCALES):
        one = _cfg(strip_font_scale=scale_value)
        probe = TaskbarStrip(one)
        try:
            render(probe, make_snapshot(one, now), LIGHT_BAR, taskbar_h,
                   f"strip_font_{index}.png", scale=1.25)
        finally:
            probe.destroy()
    write_sheet(OUT / "strip_sheet_fonts.png")

    # ---- 只勾关键几项 / 勾满全部（看丢字段的顺序和后段字段长什么样）----
    _SHEET.clear()
    for tag, fields in (
        ("minimal", ["current"]),
        ("compact", ["current", "cost"]),
        ("power", ["current", "cpu", "gpu", "base"]),
        ("energy", ["session", "today", "today_cost", "uptime"]),
    ):
        one = _cfg(strip_fields=list(fields))
        probe = TaskbarStrip(one)
        try:
            render(probe, make_snapshot(one, now), LIGHT_BAR, taskbar_h,
                   f"strip_fields_{tag}.png", scale=1.25)
        finally:
            probe.destroy()
    write_sheet(OUT / "strip_sheet_fields.png")

    # ---- 线框主题放在深色任务栏上（抠色最容易在这露出毛边）----
    _SHEET.clear()
    for bar, tag in ((DARK_BAR, "darkbar"), (LIGHT_BAR, "lightbar")):
        one = _cfg(strip_theme="outline")
        probe = TaskbarStrip(one)
        try:
            render(probe, make_snapshot(one, now), bar, taskbar_h,
                   f"strip_outline_{tag}.png", scale=1.25)
        finally:
            probe.destroy()
    write_sheet(OUT / "strip_sheet_outline.png")

    # ---- 暗色任务栏上再走一遍 ----
    # 玻璃 / 跟随任务栏都是「采样底色 + 判断深浅」，深浅任务栏走**不同分支**：
    # 只在浅色任务栏上测会漏掉一半代码（这是本机最容易踩的坑 —— 本机任务栏
    # 实际是深色的，而 SystemUsesLightTheme 却报 1）。
    _SHEET.clear()
    dark_order = ("auto", "glass", "accent", "light", "dark")
    for theme_key in dark_order:
        one = _cfg(strip_theme=theme_key)
        probe = TaskbarStrip(one)
        try:
            render(probe, make_snapshot(one, now), DARK_BAR, taskbar_h,
                   f"strip_darkbar_{theme_key}.png", scale=1.25)
        finally:
            probe.destroy()
    write_sheet(OUT / "strip_sheet_darkbar.png")
    magnify_sheet([f"strip_darkbar_{k}.png" for k in dark_order],
                  OUT / "strip_zoom_darkbar.png", crop=(0, 4, 168, 56), zoom=3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
