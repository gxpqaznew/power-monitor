"""看一眼「悬停」到底长什么样：普通态 vs 悬停态，1:1 + 放大特写。

拖动把手（两端两列小圆点）已经删掉了，改成「鼠标指上去描边转强调色」。
这个脚本用来确认两件事：
  * 普通态**干干净净** —— 胶囊两端没有任何常驻装饰；
  * 悬停态只改描边那一圈（+ 一档加粗、底色轻微提亮），别的什么都不动。

和 ``_striprender.py`` 的区别：这里**按真实任务栏算落点**
（``_target_rect``），所以宽度会被「开始按钮左边剩多少地方」真实地卡住，
折行与否、胶囊多宽多高都和装到机器上的样子一致 —— 而 ``_striprender`` 是
拿量宽函数硬算的，窄屏上会画出一条比实机宽得多的单行长条。

跑法：python _hoverprobe.py
出图：_preview/hover_normal.png / hover_active.png / hover_zoom.png
"""
from __future__ import annotations

import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 必须先声明 DPI 感知：不声明的话进程拿到的任务栏是**虚拟化过的 48px**，
# 算出来的 scale 会是 1.0（真机是 1.25），预览就和装上去的样子对不上。
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:  # noqa: BLE001 - 老系统上没有 shcore，忽略
    pass

import _striprender as R  # noqa: E402
from powermon import strip as stripmod  # noqa: E402
from powermon import taskbar as taskbar_mod  # noqa: E402
from powermon.config import Config  # noqa: E402
from powermon.strip import TaskbarStrip  # noqa: E402
from powermon.w32 import gdi32, user32  # noqa: E402

OUT = Path(__file__).resolve().parent / "_preview"
BAR = R.DARK_BAR


def _cfg() -> Config:
    """按用户真实 config.json 的那一档来（玻璃 / 纤细 / 0.85 / 6 个字段）。"""
    cfg = Config()
    cfg.strip_theme = "glass"
    cfg.strip_size = "slim"
    cfg.strip_font_scale = 0.85
    cfg.strip_fields = ["cost", "session", "today", "uptime", "total",
                        "total_cost"]
    cfg.strip_locked = False
    return cfg


def _shoot(strip, snap, out_name: str) -> tuple[int, int]:
    """按真实落点画一帧，合成到假任务栏底板上，写出 PNG。"""
    strip._sample_taskbar = lambda *a, **k: BAR  # type: ignore[method-assign]
    taskbar_mod.uses_light_theme = lambda: False  # type: ignore[assignment]
    rect = strip._target_rect(snap)
    if rect is None:
        raise SystemExit("算不出落点（先确认 explorer 在跑）")
    width, height = rect[2] - rect[0], rect[3] - rect[1]
    scale = strip._scale

    # 折行时胶囊可能比 48px 的标准任务栏还高（_MULTI_ROW_MAX_RATIO 允许到 0.94），
    # 所以假任务栏得按胶囊自己撑开，否则合成时会越界。
    bar_h = max(48, height + int(round(8 * scale)))
    offset = (bar_h - height) // 2
    canvas_w = width + int(round(100 * scale))
    canvas_h = bar_h

    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    try:
        bmp, view = stripmod.dib_section(dc, width, height)
        if not bmp:
            raise SystemExit("DIB 创建失败")
        old = gdi32.SelectObject(dc, bmp)
        strip._rect = (0, 0, width, height)
        strip._view, strip._w, strip._h = view, width, height
        _w, _h, pal = strip._layout(dc, scale, snap, render=True,
                                    origin_x=0, origin_y=0, height=height)
        stripmod.compose_shape_alpha(
            view, width, height, margin=0,
            radius=strip._radius or min(stripmod._RADIUS, height / 2.0),
            shape_w=width, shape_h=height, shadow=0,
            shape_alpha=pal["alpha"], key_rgb=pal["key"],
        )
        px = bytes(view)
        gdi32.SelectObject(dc, old)
        gdi32.DeleteObject(bmp)
    finally:
        gdi32.DeleteDC(dc)
        user32.ReleaseDC(None, screen)
    strip._view = None
    strip._rect = None

    out = bytearray(bytes(BAR) * (canvas_w * canvas_h))
    for y in range(height):
        base = (y + offset) * canvas_w * 3
        for x in range(width):
            i = (y * width + x) * 4
            a = px[i + 3] / 255.0
            o = base + x * 3
            out[o] = min(255, int(round(px[i + 2] + BAR[0] * (1 - a))))
            out[o + 1] = min(255, int(round(px[i + 1] + BAR[1] * (1 - a))))
            out[o + 2] = min(255, int(round(px[i] + BAR[2] * (1 - a))))
    R.write_png(OUT / out_name, bytes(out), canvas_w, canvas_h)
    print(f"{out_name}: 胶囊 {width}x{height} @scale {scale:.3f} "
          f"（{strip._plan_rows} 行）")
    return width, height


def main() -> int:
    cfg = _cfg()
    snap = R.make_snapshot(cfg, R.time.time())

    made = []
    for name, hover in (("hover_normal", False), ("hover_active", True)):
        strip = TaskbarStrip(cfg)
        strip._hover = hover
        made.append((name, *_shoot(strip, snap, f"{name}.png")))

    from PIL import Image  # noqa: PLC0415

    ims = [Image.open(OUT / f"{n}.png").convert("RGB") for n, _w, _h in made]
    zoom = 2
    cw = max(i.width for i in ims) * zoom
    ch = sum(i.height for i in ims) * zoom + 12 * (len(ims) - 1)
    sheet = Image.new("RGB", (cw, ch), (24, 24, 28))
    y = 0
    for im in ims:
        sheet.paste(im.resize((im.width * zoom, im.height * zoom),
                              Image.NEAREST), (0, y))
        y += im.height * zoom + 12
    sheet.save(OUT / "hover_zoom.png")
    print(f"拼图 {OUT / 'hover_zoom.png'} {sheet.size}")

    a, b = ims
    if a.size != b.size:
        print("[FAIL] 两张图尺寸不一致")
        return 1
    pa, pb = list(a.getdata()), list(b.getdata())
    diff = sum(1 for p, q in zip(pa, pb)
               if sum(abs(x - y) for x, y in zip(p, q)) > 24)
    total = a.width * a.height
    print(f"像素差异 {diff}/{total} = {diff / total:.2%}")
    if diff == 0:
        print("[FAIL] 悬停没有任何可见变化")
        return 1
    # 描边大约占「周长 × 2px」：胶囊 560×55 → 周长 ~1230 × 2 ≈ 2500。
    # 超过 25% 说明不止动了描边（比如整块底被洗过）。
    if diff / total > 0.25:
        print("[FAIL] 变化面积过大，悬停不该动整块底")
        return 1
    print("[PASS] 悬停只改了描边那一圈")
    return 0


if __name__ == "__main__":
    sys.exit(main())
