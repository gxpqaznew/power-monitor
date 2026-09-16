"""毛玻璃（frost）离屏验证：是不是真的糊了、色调对不对、噪点有没有。

用**合成图案**当源（黑白条纹），不走抓屏 —— 真桌面不可控，条纹图的判据是硬的：

  A. 糊之前只有 0 / 255 两种像素；糊之后**必须出现中间灰**（边界被抹开）；
  B. 糊是「糊」不是「糊没了」：低频结构还在（极差仍然大），但**最大单步跳变
     显著下降**。

     ⚠️ 别用「相邻像素跳变总和 ∑|Δ|」当判据 —— 单调斜坡的总变差恒等于两端
     之差，模糊把 1 像素硬边摊成 8 像素斜坡后 ∑|Δ| **一点都不变**（实测
     4845 → 4845，看着像"没糊"，其实是这个指标瞎了）。要么看最大单步跳变，
     要么数中间灰的像素个数。
  C. 放大不能用最近邻：条纹图放大后一行里的**不同取值个数**要够多（双线性
     出连续斜坡；最近邻只能出 8px 一块的台阶，取值个数极少）。
  D. 色调浓度 0 → 原样；255 → 整块变成色调色；128 → 介于两者之间。
  E. 噪点关掉时两个像素应该完全相同，开着时明显不同（有颗粒）。
  F. 缓存：同一个 key 第二次不重抓（``has`` 命中且耗时明显更低）。
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import frost  # noqa: E402
from powermon.roundwin import dib_section  # noqa: E402
from powermon.w32 import gdi32, user32  # noqa: E402

W, H = 240, 120
PASS = FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
    tail = f"  ({detail})" if detail else ""
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{tail}")


def _new_dc(w: int, h: int):
    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    user32.ReleaseDC(None, screen)
    bmp, view = dib_section(dc, w, h)
    old = gdi32.SelectObject(dc, bmp)
    return dc, bmp, view, old


def make_source(stripe: int = 12):
    """黑白竖条纹源图。返回 (dc, bitmap, view, old)。"""
    dc, bmp, view, old = _new_dc(W, H)
    for y in range(H):
        for x in range(W):
            v = 0 if (x // stripe) % 2 == 0 else 255
            i = (y * W + x) * 4
            view[i] = view[i + 1] = view[i + 2] = v
            view[i + 3] = 255
    return dc, bmp, view, old


def make_sink():
    return _new_dc(W, H)


def pixels(view, row: int | None = None):
    """取出 (R 通道) 像素列表：整幅或某一行。"""
    out = []
    ys = range(H) if row is None else [row]
    for y in ys:
        for x in range(W):
            out.append(view[(y * W + x) * 4 + 2])       # DIB 是 B,G,R,A
    return out


def max_step(px):
    return max(abs(px[i + 1] - px[i]) for i in range(len(px) - 1))


def longest_run(px):
    best = cur = 1
    for i in range(1, len(px)):
        cur = cur + 1 if px[i] == px[i - 1] else 1
        best = max(best, cur)
    return best


def main() -> int:
    src_dc, src_bmp, src_view, src_old = make_source()
    dst_dc, dst_bmp, dst_view, dst_old = make_sink()

    check("GDI+ 可用（放大靠它做双线性插值）", frost.available())

    # ---- A/B. 糊 ----
    ok = frost.blur_into(dst_dc, src_dc, W, H, (0, 0, 0), strength=0,
                         noise=False, src_bmp=src_bmp)
    check("blur_into 跑通", ok)
    src_px = pixels(src_view, row=H // 2)
    out_px = pixels(dst_view, row=H // 2)

    src_mid = [v for v in src_px if 4 < v < 251]
    out_mid = [v for v in out_px if 4 < v < 251]
    check("源图只有黑白两色（条纹是硬边）", not src_mid,
          f"中间灰 {len(src_mid)} 个")
    check("糊完出现中间灰（硬边被抹开）", len(out_mid) > 20,
          f"中间灰 {len(out_mid)} 个 / {W} 像素")

    js, jo = max_step(src_px), max_step(out_px)
    check("最大单步跳变显著下降（真的柔化了，不是照抄）", jo < js * 0.35,
          f"max|Δ| {js} → {jo}")

    spread = max(out_px) - min(out_px)
    check("糊完没有糊成一块死板（还留着明暗结构）", spread > 60,
          f"极差 {spread}")

    # ---- C. 放大不能是最近邻 ----
    uniq = len(set(out_px))
    run = longest_run(out_px)
    check("放大是插值不是最近邻（一行里取值足够多）", uniq >= 40,
          f"不同取值 {uniq} 个")
    check("放大后没有 8px 一块的台阶（最长同值连续段够短）", run <= 8,
          f"最长连续同值 {run} 像素（最近邻放大 = 8）")

    # ---- D. 色调 ----
    frost.blur_into(dst_dc, src_dc, W, H, (0, 0, 0), strength=255, noise=False,
                    src_bmp=src_bmp)
    dark = pixels(dst_view, row=H // 2)
    check("浓度 255 → 整块压成色调色（纯黑）", max(dark) <= 2, f"max={max(dark)}")

    frost.blur_into(dst_dc, src_dc, W, H, (255, 0, 0), strength=255, noise=False,
                    src_bmp=src_bmp)
    red_row = []
    for x in range(W):
        i = ((H // 2) * W + x) * 4
        red_row.append((dst_view[i], dst_view[i + 1], dst_view[i + 2]))
    check("浓度 255 + 红色调 → 整块变红（B,G,R 顺序下 R 在第三字节）",
          all(b == 0 and g == 0 and r == 255 for b, g, r in red_row),
          f"样例 {red_row[0]}")

    frost.blur_into(dst_dc, src_dc, W, H, (0, 0, 0), strength=128, noise=False,
                    src_bmp=src_bmp)
    half = pixels(dst_view, row=H // 2)
    check("浓度 128 → 明显变暗但不是全黑（介于两者之间）",
          0 < sum(half) / len(half) < (sum(out_px) / len(out_px)) * 0.75,
          f"均值 {sum(half) / len(half):.1f} vs 未叠色 {sum(out_px) / len(out_px):.1f}")

    # ---- E. 噪点 ----
    frost.blur_into(dst_dc, src_dc, W, H, (0, 0, 0), strength=40, noise=False,
                    src_bmp=src_bmp)
    flat = pixels(dst_view, row=8)
    frost.blur_into(dst_dc, src_dc, W, H, (0, 0, 0), strength=40, noise=True,
                    src_bmp=src_bmp)
    grain = pixels(dst_view, row=8)
    diff = sum(1 for a, b in zip(flat, grain) if a != b)
    check("噪点开关真的改变了像素（有颗粒感）", diff > W * 0.5,
          f"{diff}/{W} 个像素不同")

    # ---- F. 缓存（抓屏路径）----
    frost.reset()
    t0 = time.perf_counter()
    first = frost.blit(dst_dc, "t", 100, 100, 0, 0, 200, 100, (0, 0, 0), 120)
    t1 = time.perf_counter()
    cached = frost.has("t")
    t2 = time.perf_counter()
    again = frost.blit(dst_dc, "t", 100, 100, 0, 0, 200, 100, (0, 0, 0), 120)
    t3 = time.perf_counter()
    check("抓屏版 blit 成功", first and again)
    check("第二次命中缓存（不重抓）", cached, "has() 为假")
    check(f"缓存命中更快（{(t1 - t0) * 1000:.1f}ms → {(t3 - t2) * 1000:.1f}ms）",
          (t3 - t2) <= (t1 - t0) + 0.002)
    check("缓存里的成品尺寸对得上",
          frost._entries["t"].w == 200 and frost._entries["t"].h == 100)
    frost.invalidate("t")
    check("invalidate 能清掉缓存", not frost.has("t"))
    frost.reset()

    gdi32.SelectObject(src_dc, src_old)
    gdi32.DeleteObject(src_bmp)
    gdi32.DeleteDC(src_dc)
    gdi32.SelectObject(dst_dc, dst_old)
    gdi32.DeleteObject(dst_bmp)
    gdi32.DeleteDC(dst_dc)
    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
