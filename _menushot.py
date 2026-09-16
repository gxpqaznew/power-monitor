"""自绘菜单（ctxmenu）离屏渲染 + 像素断言 + 导出 PNG。

菜单真弹出来才肉眼验收太晚：先把 ``render_base`` / ``with_hover`` 跑在离屏
DIB 上，把预乘 alpha 合成回浅色 / 深色桌面底板，导成 PNG 检查，再做一组
**像素级断言**（数值判据必须「找不到目标就 FAIL」，见 _aligndbg 的教训）：

  1. 卡片底色 / 圆角外透明（合成后等于底板色）
  2. hover 条是强调蓝、且只在那一行
  3. radio 勾选行有蓝圆 + 白心点；checkbox 行有白勾
  4. 分隔线行有 _SEP 色横线
  5. 禁用项文字更暗
  6. with_hover 的性能（不能整图重画，否则一次切换上百毫秒）

输出 _preview/menu_*.png
"""

from __future__ import annotations

import ctypes
import struct
import sys
import time
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import ctxmenu
from powermon.w32 import user32

OUT = Path(__file__).resolve().parent / "_preview"
LIGHT_BG = (243, 243, 243)   # 浅色桌面
DARK_BG = (24, 26, 32)       # 深色桌面

_counts = {"ok": 0, "bad": 0}
_lines: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _counts["ok" if ok else "bad"] += 1 if ok else 0
    _counts["bad"] += 0 if ok else 1
    mark = "PASS" if ok else "FAIL"
    line = f"[{mark}] {name}" + (f" —— {detail}" if detail and not ok else
                                 (f"（{detail}）" if detail else ""))
    print(line)
    _lines.append(line)


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


def composite(bgra: bytes, w: int, h: int, bg) -> bytes:
    """预乘 BGRA 合成到底板 → RGB bytes。"""
    br, bgc, bb = bg
    out = bytearray(w * h * 3)
    for i in range(w * h):
        b, g, r, a = bgra[i * 4], bgra[i * 4 + 1], bgra[i * 4 + 2], bgra[i * 4 + 3]
        if a >= 255:
            out[i * 3], out[i * 3 + 1], out[i * 3 + 2] = r, g, b
        elif a == 0:
            out[i * 3], out[i * 3 + 1], out[i * 3 + 2] = br, bgc, bb
        else:
            k = a / 255.0
            out[i * 3] = int(r + (br - r) * (1 - k))
            out[i * 3 + 1] = int(g + (bgc - g) * (1 - k))
            out[i * 3 + 2] = int(b + (bb - b) * (1 - k))
    return bytes(out)


def px(rgb: bytes, w: int, x: int, y: int):
    i = (y * w + x) * 3
    return rgb[i], rgb[i + 1], rgb[i + 2]


def near(c, target, tol=14) -> bool:
    return all(abs(a - b) <= tol for a, b in zip(c, target))


def dist(c, target) -> int:
    return max(abs(a - b) for a, b in zip(c, target))


# ------------------------------------------------------------------ 样例菜单

def sample_root():
    """像模像样的根菜单：label / item / radio / checkbox / sub / sep / disabled。"""
    return [
        {"type": "label", "text": "本次开机", "value": "2 小时 3 分"},
        {"type": "label", "text": "今日", "value": "1.24 kWh · ¥0.86"},
        {"type": "sep"},
        {"type": "item", "cmd": 1, "text": "打开详情面板"},
        {"type": "item", "cmd": 20, "text": "开机自启动", "checked": True},
        {"type": "item", "cmd": 23, "text": "任务栏长条读数", "checked": True},
        {"type": "sep"},
        {"type": "sub", "text": "长条质感", "entries": [
            {"type": "item", "cmd": 50, "text": "石墨（深色）", "checked": True,
             "radio": True},
            {"type": "item", "cmd": 51, "text": "宣纸（浅色）", "radio": True},
            {"type": "item", "cmd": 52, "text": "液态玻璃", "radio": True},
        ]},
        {"type": "sub", "text": "长条字号", "entries": [
            {"type": "item", "cmd": 60, "text": "极小（0.72）", "radio": True},
            {"type": "item", "cmd": 61, "text": "小（0.88）", "radio": True},
            {"type": "item", "cmd": 62, "text": "标准（1.00）", "checked": True,
             "radio": True},
            {"type": "item", "cmd": 63, "text": "大（1.12）", "radio": True},
            {"type": "item", "cmd": 64, "text": "特大（1.30）", "radio": True},
            {"type": "item", "cmd": 65, "text": "折行兜底（1.50）", "radio": True},
            {"type": "sep"},
            {"type": "item", "cmd": 27, "text": "复位标准字号（1.00）"},
        ]},
        {"type": "sep"},
        {"type": "item", "cmd": 24, "text": "用量统计（全部明细）…"},
        {"type": "item", "cmd": 33, "text": "灰掉的项（不可点）", "enabled": False},
        {"type": "item", "cmd": 40, "text": "退出"},
    ]


def row_of(geom, index):
    e, ry, rh = geom.rows[index]
    return e


def find_row(rows_or_geom, pred):
    rows = rows_or_geom.rows if hasattr(rows_or_geom, "rows") else [
        (e, 0, 0) for e in rows_or_geom]
    for i, (e, _ry, _rh) in enumerate(rows):
        if pred(e):
            return i
    return -1


def main() -> int:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    OUT.mkdir(exist_ok=True)
    entries = sample_root()

    for scale in (1.0, 1.25):
        base, geom = ctxmenu.render_base(entries, scale)
        check(f"scale={scale}: render_base 成功", base is not None and geom is not None)
        if base is None:
            continue

        rgb = composite(base, geom.w, geom.h, DARK_BG)
        write_png(OUT / f"menu_base_s{scale}.png", rgb, geom.w, geom.h)

        # ---- 卡片底色（行中间、避开文字与勾选位的空白处） ----
        i_sub = find_row(geom, lambda e: e.get("type") == "sub")
        e, ry, rh = geom.rows[i_sub]
        mid_y = geom.margin + ry + rh // 2
        # 卡片中间偏右的空白带（行文字右侧到箭头之间未必空，取卡片最右内侧）
        x_probe = geom.margin + geom.shape_w - 2
        got = px(rgb, geom.w, x_probe, mid_y)
        # 卡片是半透明 _BG@234 合成到深底板
        k = 234 / 255.0
        want = tuple(int(ctxmenu._BG[i] * k + DARK_BG[i] * (1 - k)) for i in range(3))
        check(f"scale={scale}: 卡片底色 ≈ 半透明深色（{got} vs {want}）",
              near(got, want, 10), f"{got} vs {want}")

        # ---- 圆角外透明：卡片外角（margin 内、圆角外）应等于底板 ----
        corner = px(rgb, geom.w, geom.margin + 1, geom.margin + 1)
        check(f"scale={scale}: 圆角外穿透（{corner} == 底板 {DARK_BG}）",
              near(corner, DARK_BG, 6), str(corner))

        # ---- 分隔线 ----
        i_sep = find_row(geom, lambda e: e.get("type") == "sep")
        if i_sep >= 0:
            _e, sry, srh = geom.rows[i_sep]
            sy = geom.margin + sry + srh // 2
            sx = geom.margin + geom.shape_w // 2
            line_px = px(rgb, geom.w, sx, sy)
            k2 = 234 / 255.0
            sep_want = tuple(int(ctxmenu._SEP[i] * k2 + DARK_BG[i] * (1 - k2))
                             for i in range(3))
            check(f"scale={scale}: 分隔线存在（{line_px} ≈ {sep_want}）",
                  near(line_px, sep_want, 12), str(line_px))

        # ---- radio 勾选（蓝圆白心）—— radio 档位都在子菜单里，
        #      这里对「长条质感」子结构单独渲染后断言（见下方 sub 段）。----

        # ---- checkbox 勾选（蓝圆白勾，圆心不全蓝）----
        i_chk = find_row(
            geom, lambda e: e.get("type") == "item" and not e.get("radio")
            and e.get("checked"))
        if i_chk >= 0:
            _e, cry, crh = geom.rows[i_chk]
            cy = geom.margin + cry + crh // 2
            d = int(round(ctxmenu._CHECK_D * scale))
            cx = (geom.margin + geom.pad + int(round(4 * scale))
                  + int(round(6 * scale)) + d // 2)
            a = px(rgb, geom.w, cx, cy)
            b = px(rgb, geom.w, cx - d // 2 + 1, cy)
            check(f"scale={scale}: checkbox 圆内勾线有变化（{a} vs {b}）",
                  max(abs(x - y) for x, y in zip(a, b)) > 20,
                  f"{a} vs {b}")

        # ---- 文字存在：扫一段横带，任一像素明显亮于卡片底色即算画出了文字
        #      （单个探针可能落在字形间隙上，必须扫带 —— _aligndbg 的教训）----
        text_x = geom.text_x()
        if i_chk >= 0:
            _e, cry, crh = geom.rows[i_chk]
            ty = geom.margin + cry + crh // 2
            band = [px(rgb, geom.w, dx, ty)
                    for dx in range(text_x, min(geom.w - 1, text_x + 140))]
            brightest = max(band, key=lambda c: sum(c))
            check(f"scale={scale}: 行文字已画出（最亮 {brightest}）",
                  not near(brightest, want, 25), str(brightest))

        # ---- 禁用项更暗：比「行里最亮的文字像素」，不能比最暗的（那是底板）----
        i_dis = find_row(geom, lambda e: e.get("type") == "item"
                         and not e.get("enabled", True))
        if i_dis >= 0:
            def row_text_brightest(idx):
                _e, dy, dh = geom.rows[idx]
                yy = geom.margin + dy + dh // 2
                best = None
                for dx in range(text_x, min(geom.w - 1, text_x + 120)):
                    c = px(rgb, geom.w, dx, yy)
                    lum = 0.3 * c[0] + 0.5 * c[1] + 0.2 * c[2]
                    if best is None or lum > best[0]:
                        best = (lum, c)
                return best[1]
            dis_c = row_text_brightest(i_dis)
            nor_c = row_text_brightest(find_row(
                geom, lambda e: e.get("type") == "item"
                and e.get("enabled", True) and not e.get("checked")))
            dl = (0.3 * dis_c[0] + 0.5 * dis_c[1] + 0.2 * dis_c[2])
            nl = (0.3 * nor_c[0] + 0.5 * nor_c[1] + 0.2 * nor_c[2])
            check(f"scale={scale}: 禁用项文字更暗（{dl:.0f} < {nl:.0f}）",
                  dl < nl, f"{dis_c} vs {nor_c}")

        # ---- hover 条 ----
        i_plain = find_row(geom, lambda e: e.get("type") == "item"
                           and e.get("enabled", True) and not e.get("checked")
                           and not e.get("radio"))
        t0 = time.perf_counter()
        hot = ctxmenu.with_hover(base, geom, i_plain)
        dt_ms = (time.perf_counter() - t0) * 1000
        check(f"scale={scale}: with_hover 够快（{dt_ms:.0f}ms < 120ms）",
              dt_ms < 120, f"{dt_ms:.0f}ms")
        rgb_hot = composite(hot, geom.w, geom.h, DARK_BG)
        write_png(OUT / f"menu_hover_s{scale}.png", rgb_hot, geom.w, geom.h)
        # hover 条内部 alpha 被补成 255，颜色就是纯 _ACCENT —— 但文字是白的，
        # 单个探针可能落在字形上，必须扫带数「接近强调蓝的像素占比」
        _e, hy, hh = geom.rows[i_plain]
        hy_mid = geom.margin + hy + hh // 2
        bar_x0 = geom.margin + int(round(ctxmenu._HOVER_INSET * scale)) + 2
        bar_x1 = geom.margin + geom.shape_w - int(round(
            ctxmenu._HOVER_INSET * scale)) - 2
        band = [px(rgb_hot, geom.w, dx, hy_mid)
                for dx in range(bar_x0, bar_x1)]
        hits = sum(1 for c in band if near(c, ctxmenu._ACCENT, 22))
        check(f"scale={scale}: hover 行大面积强调蓝（{hits}/{len(band)}）",
              hits >= len(band) * 0.5, f"命中 {hits}/{len(band)}")
        # 别的行不受影响：探一个非 hover 行的卡片空白带
        other = px(rgb_hot, geom.w, x_probe, mid_y)
        check(f"scale={scale}: 非 hover 行保持卡片底色（{other}）",
              near(other, want, 12), str(other))

    # ---- 子菜单单独出图（radio / 复位项 / 浅色桌面）----
    i_font = find_row(entries, lambda e: e.get("text") == "长条字号")
    sub_entries = entries[i_font]["entries"]
    # radio 断言用「长条质感」子菜单（有勾选的 radio 行），渲染到深底板
    i_theme = find_row(entries, lambda e: e.get("text") == "长条质感")
    theme_entries = entries[i_theme]["entries"]
    base_t, geom_t = ctxmenu.render_base(theme_entries, 1.25)
    check("子菜单（质感档）render_base 成功", base_t is not None)
    if base_t is not None:
        rgb_t = composite(base_t, geom_t.w, geom_t.h, DARK_BG)
        write_png(OUT / "menu_sub_theme.png", rgb_t, geom_t.w, geom_t.h)
        i_radio = find_row(geom_t, lambda e: e.get("radio") and e.get("checked"))
        check("质感子菜单里有勾选的 radio 行", i_radio >= 0)
        if i_radio >= 0:
            _e, rry, rrh = geom_t.rows[i_radio]
            cy = geom_t.margin + rry + rrh // 2
            d = int(round(ctxmenu._CHECK_D * 1.25))
            cx = (geom_t.margin + geom_t.pad + int(round(4 * 1.25))
                  + int(round(6 * 1.25)) + d // 2)
            # 蓝圆：合成管线后的实际色与纯 _ACCENT 有偏差（半透明卡片 + 预乘），
            # 判据用「蓝主导 + 离强调色不远」，别硬卡某个混合公式
            dot = px(rgb_t, geom_t.w, cx, cy - d // 2 + 2)   # 圆上部边缘 = 蓝
            check(f"radio 蓝圆（{dot}，蓝主导且近强调色）",
                  dot[2] - dot[0] > 150 and dist(dot, ctxmenu._ACCENT) < 50,
                  str(dot))
            # 白心：圆心处应明显亮于蓝圆边缘
            heart = px(rgb_t, geom_t.w, cx, cy)
            check(f"radio 白心（心 {heart} vs 圆边 {dot}）",
                  sum(heart) - sum(dot) > 60,
                  f"心和圆边太接近 {heart} vs {dot}")

    base_s, geom_s = ctxmenu.render_base(sub_entries, 1.25)
    check("子菜单（字号档）render_base 成功", base_s is not None)
    if base_s is not None:
        i_std = find_row(geom_s, lambda e: e.get("text") == "标准（1.00）")
        hot_s = ctxmenu.with_hover(base_s, geom_s, i_std)
        rgb_s = composite(hot_s, geom_s.w, geom_s.h, LIGHT_BG)
        write_png(OUT / "menu_sub_light.png", rgb_s, geom_s.w, geom_s.h)
        check("子菜单行数 = 6 档 + 分隔线 + 复位 = 8", len(geom_s.rows) == 8,
              f"{len(geom_s.rows)} 行")
        check("子菜单最后一条是「复位标准字号」",
              geom_s.rows[-1][0].get("text") == "复位标准字号（1.00）",
              str(geom_s.rows[-1][0].get("text")))
        # 浅底板下卡片仍是深色（固定深色卡片，不跟桌面变浅）
        _e, ly, lh = geom_s.rows[2]
        yc = geom_s.margin + ly + lh // 2
        xc = geom_s.margin + geom_s.shape_w - 2
        c = px(rgb_s, geom_s.w, xc, yc)
        k5 = 234 / 255.0
        want_l = tuple(int(ctxmenu._BG[i] * k5 + LIGHT_BG[i] * (1 - k5))
                       for i in range(3))
        check(f"浅色桌面上卡片仍深色（{c} ≈ {want_l}）",
              near(c, want_l, 12), str(c))

    print()
    print(f"通过 {_counts['ok']} 项，失败 {_counts['bad']} 项")
    (OUT / "menushot.txt").write_text("\n".join(_lines) + "\n", encoding="utf-8")
    return 1 if _counts["bad"] else 0


if __name__ == "__main__":
    sys.exit(main())
