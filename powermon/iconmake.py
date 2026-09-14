"""动态生成托盘图标（HICON）。

流程：建一张 32 位 top-down DIB → 用 GDI 铺竖向渐变底色、写带描边的数字 →
再用 Python 逐像素写 alpha 通道（GDI 不管 alpha），同时按圆角矩形 SDF 做边缘
抗锯齿 → CreateIconIndirect 得到 HICON。

这样既拿到了系统字体渲染的清晰数字，又不需要 Pillow。

图标美学上做了三件事，都是为了让它在 16px 下也不糊成一团：
  * 底色是竖向渐变（上亮下暗），不是一块实色，小尺寸下也有体积感；
  * 数字先画一层压暗的偏移「阴影」再叠白色，笔画边界清楚；
  * 数字留出约两成边距，不再顶到圆角上。
"""

from __future__ import annotations

import ctypes

from .w32 import (
    ANTIALIASED_QUALITY,
    BI_RGB,
    BITMAPINFO,
    BITMAPINFOHEADER,
    DEFAULT_CHARSET,
    DIB_RGB_COLORS,
    DT_CENTER,
    DT_NOPREFIX,
    DT_SINGLELINE,
    DT_VCENTER,
    FW_BOLD,
    ICONINFO,
    NULL_PEN,
    SIZE,
    SM_CXSMICON,
    TRANSPARENT,
    gdi32,
    user32,
    wintypes,
)

_FONT_FACE = "Segoe UI"

# 数字占底色的最大宽度 / 最大高度（相对图标边长）。留出边距是关键，
# 顶到圆角的数字在小尺寸下会显得脏。
_MAX_TEXT_W_RATIO = 0.74
_MAX_TEXT_H_RATIO = 0.68

# 渐变的上下端点相对基色的明暗系数
_TOP_LIGHTEN = 1.24
_BOTTOM_DARKEN = 0.84


def _colorref(r: int, g: int, b: int) -> int:
    """COLORREF 是 0x00BBGGRR。"""
    return (b << 16) | (g << 8) | r


def _shade(color: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    """按比例提亮（>1）或压暗（<1）一个颜色。"""
    return tuple(
        min(255, max(0, int(round(channel * factor)))) for channel in color
    )


def _pick_color(ratio: float) -> tuple[int, int, int]:
    """负载比例 → 底色：绿（轻载）→ 琥珀（中载）→ 红（重载）。

    刻意压得比「糖果色」暗：白字在这三个底色上的对比度都在 4.5:1 以上，
    小尺寸下笔画才不会糊成一片。鲜亮的橙黄配白字只有 2:1 左右，看着就是
    廉价塑料感 —— 这正是原来那版图标最大的问题。
    """
    if ratio < 0.35:
        return (26, 122, 74)
    if ratio < 0.70:
        return (168, 100, 10)
    return (186, 50, 42)


def _rounded_sdf(px: float, py: float, size: float, pad: float, radius: float) -> float:
    """圆角矩形的有符号距离场：<0 在内部，>0 在外部。"""
    half = (size - 2 * pad) / 2.0
    qx = abs(px - size / 2.0) - (half - radius)
    qy = abs(py - size / 2.0) - (half - radius)
    outside = (max(qx, 0.0) ** 2 + max(qy, 0.0) ** 2) ** 0.5
    inside = min(max(qx, qy), 0.0)
    return outside + inside - radius


def _new_dib(dc, size: int):
    """建一张 32bpp top-down DIB，返回 (bitmap, bits_pointer)。"""
    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = size
    bmi.bmiHeader.biHeight = -size  # 负值 = top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = BI_RGB

    bits = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(
        dc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
    )
    return bmp, bits


def _fill_vertical_gradient(dc, size: int, top, bottom) -> None:
    """逐行实色插值。图标最大也就 48px，几十次 FillRect 完全无所谓。"""
    last = max(1, size - 1)
    for y in range(size):
        t = y / last
        color = tuple(
            int(round(top[i] + (bottom[i] - top[i]) * t)) for i in range(3)
        )
        brush = gdi32.CreateSolidBrush(_colorref(*color))
        rect = wintypes.RECT(0, y, size, y + 1)
        user32.FillRect(dc, ctypes.byref(rect), brush)
        gdi32.DeleteObject(brush)


def _draw_centered(dc, text: str, size: int, offset_y: int) -> None:
    rect = wintypes.RECT(0, offset_y, size, size + offset_y)
    user32.DrawTextW(
        dc, text, len(text), ctypes.byref(rect),
        DT_CENTER | DT_VCENTER | DT_SINGLELINE | DT_NOPREFIX,
    )


def build_pixels(text: str, ratio: float, size: int) -> bytes:
    """渲染图标为 BGRA 像素（顶行在前，可直接当 PNG/BMP 数据源）。"""
    screen_dc = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen_dc)
    color_bmp, bits = _new_dib(dc, size)
    old_bmp = gdi32.SelectObject(dc, color_bmp)

    base = _pick_color(ratio)
    _fill_vertical_gradient(
        dc, size, _shade(base, _TOP_LIGHTEN), _shade(base, _BOTTOM_DARKEN)
    )
    gdi32.SelectObject(dc, gdi32.GetStockObject(NULL_PEN))

    # 数字：从大到小找一个能塞进安全宽度的字高
    max_text_w = max(4, int(size * _MAX_TEXT_W_RATIO))
    height = max(5, int(size * 0.30))
    for candidate in range(int(size * _MAX_TEXT_H_RATIO), 4, -1):
        probe = gdi32.CreateFontW(
            -candidate, 0, 0, 0, FW_BOLD, 0, 0, 0,
            DEFAULT_CHARSET, 0, 0, ANTIALIASED_QUALITY, 0, _FONT_FACE,
        )
        if not probe:
            continue
        old_probe = gdi32.SelectObject(dc, probe)
        extent = SIZE()
        gdi32.GetTextExtentPoint32W(dc, text, len(text), ctypes.byref(extent))
        gdi32.SelectObject(dc, old_probe)
        gdi32.DeleteObject(probe)
        if extent.cx <= max_text_w:
            height = candidate
            break

    font = gdi32.CreateFontW(
        -height, 0, 0, 0, FW_BOLD, 0, 0, 0,
        DEFAULT_CHARSET, 0, 0, ANTIALIASED_QUALITY, 0, _FONT_FACE,
    )
    old_font = gdi32.SelectObject(dc, font) if font else None
    gdi32.SetBkMode(dc, TRANSPARENT)

    # 先画一层压暗的、下移 1px 的同形字当描边，再叠白色 —— 底色是浅色时
    # 白字容易糊，这层阴影能把笔画边界钉住
    offset = max(1, size // 22)
    gdi32.SetTextColor(dc, _colorref(*_shade(base, 0.55)))
    _draw_centered(dc, text, size, offset)
    gdi32.SetTextColor(dc, _colorref(255, 255, 255))
    _draw_centered(dc, text, size, 0)

    # 逐像素补 alpha：GDI 画出来的像素 alpha 恒为 0
    pad = max(1, size // 16)
    radius = size * 0.22
    buf = (ctypes.c_ubyte * (size * size * 4)).from_address(bits.value)
    for y in range(size):
        py = y + 0.5
        row = y * size
        for x in range(size):
            dist = _rounded_sdf(x + 0.5, py, size, pad, radius)
            idx = (row + x) * 4
            if dist >= 0.5:
                buf[idx] = buf[idx + 1] = buf[idx + 2] = buf[idx + 3] = 0
            elif dist > -0.5:
                buf[idx + 3] = int(255 * (0.5 - dist))
            else:
                buf[idx + 3] = 255

    data = bytes(buf)

    # 清理
    if old_font:
        gdi32.SelectObject(dc, old_font)
    if font:
        gdi32.DeleteObject(font)
    gdi32.SelectObject(dc, old_bmp)
    gdi32.DeleteObject(color_bmp)
    gdi32.DeleteDC(dc)
    user32.ReleaseDC(None, screen_dc)

    return data


def icon_from_pixels(pixels: bytes, size: int):
    """把 BGRA 像素打包成 HICON。调用方负责 DestroyIcon。"""
    screen_dc = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen_dc)
    color_bmp, bits = _new_dib(dc, size)
    if not color_bmp:
        gdi32.DeleteDC(dc)
        user32.ReleaseDC(None, screen_dc)
        return None

    ctypes.memmove(bits.value, pixels, min(len(pixels), size * size * 4))
    mask_bmp = gdi32.CreateBitmap(size, size, 1, 1, None)

    info = ICONINFO()
    info.fIcon = True
    info.hbmColor = color_bmp
    info.hbmMask = mask_bmp
    hicon = user32.CreateIconIndirect(ctypes.byref(info))

    gdi32.DeleteObject(color_bmp)
    gdi32.DeleteObject(mask_bmp)
    gdi32.DeleteDC(dc)
    user32.ReleaseDC(None, screen_dc)
    return hicon


def make_icon(text: str, ratio: float, size: int | None = None):
    """一步生成 HICON。size 省略时取系统托盘图标尺寸。"""
    if size is None:
        size = user32.GetSystemMetrics(SM_CXSMICON) or 16
    size = max(12, int(size))
    return icon_from_pixels(build_pixels(text, ratio, size), size)


def cost_text(cost: float) -> str:
    """已用电费 → 图标文本。图标最多放得下 4 个字符，所以按量级调精度。

    十几块钱时带两位小数已经没意义了，位数让给整数部分更划算。
    """
    cost = max(0.0, float(cost))
    if cost < 10:
        return f"{cost:.2f}"
    if cost < 100:
        return f"{cost:.1f}"
    return f"{int(cost)}"


def tray_label(snapshot, mode: str, cfg) -> tuple[str, float]:
    """按显示模式算出 (图标文本, 负载比例)。

    比例恒定表示「当前功率 / 硬件上限」，所以不管显示哪种模式，图标的
    颜色始终代表负载轻重。
    """
    limit = cfg.cpu_ppt + max(sum(snapshot.gpu_limits), 0.0) + cfg.baseline_watts
    if limit <= 0:
        limit = 300.0
    ratio = max(0.0, min(snapshot.current_w / limit, 1.0))

    if mode == "cost":
        text = cost_text(snapshot.session_cost)
    elif mode == "session":
        wh = snapshot.session_wh
        text = f"{int(wh)}" if wh < 1000 else f"{wh / 1000:.1f}"
    elif mode == "average":
        text = f"{int(round(snapshot.average_w))}"
    else:
        text = f"{int(round(snapshot.current_w))}"

    return text[:4], ratio
