"""分层窗口的公共底座：32bpp DIB + 圆角 / 柔影 alpha 合成 + UpdateLayeredWindow。

为什么非要走这条路：普通窗口的客户区是方的，`RoundRect` 只能画出"白底上的圆角"，
窗口本身还是方的（四角是背景色）；内存 DC 里 BitBlt 又会把 alpha 直接丢掉。
只有 `UpdateLayeredWindow` 能拿到**带预乘 alpha 的整块位图**，圆角才真能抗锯齿、
阴影才真是渐变。

alpha 全在 Python 里算，所以只处理"靠近边框的那一圈"像素 —— 整块几万个点，
一帧几十毫秒；要是整块逐像素跑就得上百万次循环，那就没法看了。
"""

from __future__ import annotations

import ctypes

from .w32 import (
    AC_SRC_ALPHA,
    AC_SRC_OVER,
    BI_RGB,
    BITMAPINFO,
    BITMAPINFOHEADER,
    BLENDFUNCTION,
    DIB_RGB_COLORS,
    SIZE,
    ULW_ALPHA,
    gdi32,
    user32,
    wintypes,
)


def dib_section(dc, w: int, h: int):
    """建一块 32bpp 顶朝下 DIB 段。

    返回 ``(hbitmap, view)``：view 是可直接按 ``[i]`` 下标读写的 BGRA 字节视图
    （每像素 4 字节，顺序 B,G,R,A；顶行在前）。
    必须用 DIB 段而不是 CreateCompatibleBitmap —— 后者拿不到原始像素指针。
    """
    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = w
    info.bmiHeader.biHeight = -h  # 负值 = 顶朝下
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = BI_RGB

    bits = ctypes.c_void_p()
    bmp = gdi32.CreateDIBSection(
        dc, ctypes.byref(info), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
    )
    if not bmp or not bits.value:
        return None, None
    view = (ctypes.c_ubyte * (w * h * 4)).from_address(bits.value)
    return bmp, view


def round_rect_sdf(px: float, py: float, w: float, h: float, radius: float) -> float:
    """到圆角矩形边界的距离：<0 在内部，>0 在外部（单位 = 像素）。"""
    half_w = max(0.0, w / 2.0 - radius)
    half_h = max(0.0, h / 2.0 - radius)
    qx = abs(px - w / 2.0) - half_w
    qy = abs(py - h / 2.0) - half_h
    outside = (max(qx, 0.0) ** 2 + max(qy, 0.0) ** 2) ** 0.5
    inside = min(max(qx, qy), 0.0)
    return outside + inside - radius


def compose_shape_alpha(view, w: int, h: int, margin: int, radius: float,
                       shape_w: int, shape_h: int, shadow: int = 0,
                       shadow_rgb=(14, 18, 26), shadow_alpha: int = 96,
                       shadow_dy: int = 0) -> None:
    """把 GDI 画好的内容补上 alpha，做成「圆角矩形 + 外圈柔影」。

    ``margin`` 是形状四周留出的空白（窗口比形状大这么多），形状位于
    ``(margin, margin)`` 处、尺寸 ``shape_w × shape_h``。

    **前提**：GDI 往 32bpp DIB 里画会把目标像素的 alpha 直接写成 0（实测
    ``FillRect`` / ``DrawText`` 都这样，GDI 行为本身不保留 alpha）。所以本函数
    必须把整个形状覆盖到的像素的 alpha 都重新写一遍 —— 漏掉哪儿，哪儿在
    ``UpdateLayeredWindow`` 下就是完全透明的空洞。

    语义：
      * 形状内部      → alpha 255，RGB 原样保留（就是 GDI 画的内容）
      * 边界 1px      → 按 SDF 抗锯齿，**RGB 同步预乘**（否则边缘会发白：
        预乘 alpha 下最终色 = RGB + 底层 ×(1-a)，RGB 不乘就会偏亮）
      * 形状外        → RGB 换成阴影色，alpha 按距离衰减（预乘）

    性能：只遍历靠边的一圈像素。圆角所在的上下各 ``radius+1`` 行整行处理
    （那几行的形状边界是弧线），中间各行只处理左右各几列，中间一整段内部直接
    补 alpha。整块逐像素跑要上百万次循环，那样每帧都得卡一下。
    """
    if view is None or margin < 0:
        return
    sr, sg, sb = shadow_rgb
    # 上下：圆角弧线会横跨 radius 行，这几行必须整行算
    corner_rows = max(shadow, int(radius)) + 1
    # 左右：中间各行只有贴着边的两三列是边界
    col_band = max(2, shadow + 1) if shadow > 0 else 2
    # 形状正好铺满整块画布时，中间各行的中段可以直接补 alpha（省掉逐像素算 SDF）
    full_shape = margin == 0 and shape_w == w and shape_h == h

    for y in range(h):
        if full_shape and not (y < corner_rows or y >= h - corner_rows):
            for x in range(col_band, max(col_band, w - col_band)):
                view[(y * w + x) * 4 + 3] = 255
            xs = list(range(0, min(w, col_band))) + \
                 list(range(max(0, w - col_band), w))
        else:
            xs = range(w)

        py = y + 0.5
        for x in xs:
            px = x + 0.5
            d = round_rect_sdf(px - margin, py - margin, shape_w, shape_h, radius)
            idx = (y * w + x) * 4
            if d <= -0.5:
                view[idx + 3] = 255
            elif d < 0.5:
                a = int(255 * (0.5 - d))
                k = a / 255.0
                view[idx] = int(view[idx] * k)
                view[idx + 1] = int(view[idx + 1] * k)
                view[idx + 2] = int(view[idx + 2] * k)
                view[idx + 3] = a
            else:
                if shadow <= 0:
                    view[idx] = view[idx + 1] = view[idx + 2] = view[idx + 3] = 0
                    continue
                # 阴影整体下移 shadow_dy，看着像从上方打光
                sd = round_rect_sdf(px - margin, py - shadow_dy - margin,
                                    shape_w, shape_h, radius)
                if sd <= 0.5:
                    alpha = shadow_alpha
                else:
                    t = min(1.0, sd / float(shadow))
                    alpha = int(shadow_alpha * (1.0 - t) ** 2)
                # 预乘：RGB 也要乘上 alpha
                if alpha <= 0:
                    view[idx] = view[idx + 1] = view[idx + 2] = view[idx + 3] = 0
                else:
                    k = alpha / 255.0
                    view[idx] = int(sb * k)
                    view[idx + 1] = int(sg * k)
                    view[idx + 2] = int(sr * k)
                    view[idx + 3] = alpha


def present_layered(hwnd, mem_dc, w: int, h: int, x: int, y: int) -> bool:
    """把 mem_dc 里那块 w×h 的位图贴到分层窗口的 (x, y)。"""
    blend = BLENDFUNCTION()
    blend.BlendOp = AC_SRC_OVER
    blend.BlendFlags = 0
    blend.SourceConstantAlpha = 255
    blend.AlphaFormat = AC_SRC_ALPHA

    dst = wintypes.POINT(int(x), int(y))
    size = SIZE(int(w), int(h))
    src = wintypes.POINT(0, 0)
    return bool(user32.UpdateLayeredWindow(
        hwnd, None, ctypes.byref(dst), ctypes.byref(size),
        mem_dc, ctypes.byref(src), 0, ctypes.byref(blend), ULW_ALPHA,
    ))
