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
                       shadow_dy: int = 0, shape_alpha: int = 255,
                       key_rgb=None) -> None:
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

    ``shape_alpha``（0~255）用来做「整块半透明」的质感（玻璃 / 固定深浅卡片）：
    形状内部不再写 255，而是写这个值，同时把内部 RGB 一并预乘 —— 预乘下
    不把 RGB 乘下去，半透明胶囊会整体发白发亮，像蒙了层灰。

    ``key_rgb`` 是「抠色」模式（只留描边和文字的线框主题靠它）：先拿这个颜色铺满
    底，再在上面画描边和文字，最后把这个颜色的像素抠成全透明。**调用方必须把这个
    颜色设成真实的背景色**，否则抗锯齿的字边会留下难看的彩色描边 —— 原因见
    ``inside()`` 里的注释。指定 key_rgb 时 ``shape_alpha`` 不再生效。

    性能：只遍历靠边的一圈像素。圆角所在的上下各 ``radius+1`` 行整行处理
    （那几行的形状边界是弧线），中间各行只处理左右各几列，中间一整段内部直接
    补 alpha。整块逐像素跑要上百万次循环，那样每帧都得卡一下。
    半透明 / 抠色模式必须逐像素处理内部（要动 RGB 或判颜色），长条只有
    两万来像素，代价可以接受；默认档（不透明）仍然走快路径。
    """
    if view is None or margin < 0:
        return
    sr, sg, sb = shadow_rgb

    sa = max(0, min(255, int(shape_alpha)))
    key = None
    if key_rgb is not None:
        key = (int(key_rgb[0]), int(key_rgb[1]), int(key_rgb[2]))

    if key is not None:
        kr, kg, kb = key

        def inside(idx: int) -> None:
            # 抠色：底色（= 键色）像素全透明，其余原样保留为不透明。
            #
            # **调用方必须把「底色」设成背后的真实颜色**，不能用醒目的哨兵色。
            # 文字是抗锯齿画的，字边那圈像素是「文字色 × 底色」的混合色：底色
            # 一旦是哨兵色（比如品红），这些边缘像素就真带着品红，而且不等于
            # 哨兵色、抠不掉，于是整字镶一圈紫边。底色取真实背景色时，边缘像素
            # 恰好就是「文字画在背景上」应有的颜色，原样保留即正确 —— 不需要
            # 按覆盖率反解（覆盖率无法从颜色可靠反推，反解会留下残留）。
            if (abs(view[idx] - kb) <= 2 and abs(view[idx + 1] - kg) <= 2
                    and abs(view[idx + 2] - kr) <= 2):
                view[idx] = view[idx + 1] = view[idx + 2] = view[idx + 3] = 0
            else:
                view[idx + 3] = 255
    elif sa < 255:
        # 注意：这个名字不能叫 k —— 下面「边缘」和「阴影」两处也要用一个 0~1 的
        # 系数，如果都叫 k 就会把这里的 k 覆盖掉，而 inside() 是闭包、取的是**调用
        # 时**的 k。后果是：每行一旦处理过一个边缘像素，之后整行的内部像素就都
        # 按那个边缘像素的系数去乘 RGB，画面上出现规则的横条纹。
        # （这是老代码里就埋着的坑，之前长条一直是不透明的 255，走不到这条分支，
        #  加了半透明质感才暴露出来。）
        inside_k = sa / 255.0

        def inside(idx: int) -> None:
            view[idx] = int(view[idx] * inside_k)
            view[idx + 1] = int(view[idx + 1] * inside_k)
            view[idx + 2] = int(view[idx + 2] * inside_k)
            view[idx + 3] = sa
    else:
        def inside(idx: int) -> None:
            view[idx + 3] = 255

    # 上下：圆角弧线会横跨 radius 行，这几行必须整行算
    corner_rows = max(shadow, int(radius)) + 1
    # 左右：中间各行只有贴着边的两三列是边界
    col_band = max(2, shadow + 1) if shadow > 0 else 2
    # 形状正好铺满整块画布时，中间各行的中段可以直接补 alpha（省掉逐像素算 SDF）
    full_shape = margin == 0 and shape_w == w and shape_h == h

    for y in range(h):
        if full_shape and not (y < corner_rows or y >= h - corner_rows):
            for x in range(col_band, max(col_band, w - col_band)):
                inside((y * w + x) * 4)
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
                inside(idx)
            elif d < 0.5:
                if key is not None:
                    # 抠色模式下边缘也走抠色：底色像素直接透明、描边像素保持不透明。
                    # 若按 d 给边缘做半透明，底色哨兵色就会以「半透明品红」的形态
                    # 留成胶囊外圈的一道毛边 —— 线框主题最扎眼的缺陷。
                    inside(idx)
                else:
                    a = int(255 * (0.5 - d))
                    # 半透明模式下边缘也要跟着压下去，否则会留一圈「发光」的硬边。
                    if sa < 255:
                        a = int(a * sa / 255.0)
                    edge_k = a / 255.0
                    view[idx] = int(view[idx] * edge_k)
                    view[idx + 1] = int(view[idx + 1] * edge_k)
                    view[idx + 2] = int(view[idx + 2] * edge_k)
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
                    shadow_k = alpha / 255.0
                    view[idx] = int(sb * shadow_k)
                    view[idx + 1] = int(sg * shadow_k)
                    view[idx + 2] = int(sr * shadow_k)
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
