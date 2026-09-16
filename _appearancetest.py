"""长条「呈现方式」回归测试 —— 排数 / 配色方案 / 自定义颜色 / 背景图 / 细节。

守住三件事：

1. **配色目录本身是「设计资产」**，不是随便填的常数表。所以逐套查对比度
   （WCAG 相对亮度）：数值色对底色 ≥ 4.5:1，标签色 ≥ 4.0:1。以后有人想
   「再调淡一点好看」，这条会立刻拦下来 —— 长条是贴在任务栏上的一条细带，
   标签一淡就整条糊成一片，用户看到的是「看不清」而不是「淡雅」。
2. **非法配置不能把长条画崩**：排数写成 "7"、颜色写成 "red"、不透明度写成
   999、开关写成字符串 —— 都要被 ``stripopts.sanitize`` 夹回合法值，
   而且**再夹一次不应该有改动**（幂等：否则每次启动都会写一次盘）。
3. **配置真的作用到了渲染上**：固定两排就真的是两排、关掉标签宽度就变窄、
   换配色方案底色和**色调层**同时变（只改底色不改 frost_tint 会糊出发灰的脏色）。

跑法：python _appearancetest.py（需要一个桌面会话，但不建真实窗口）
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import stripopts  # noqa: E402
from powermon.strip import (  # noqa: E402
    TaskbarStrip,
    _apply_scheme,
    _fit_rect,
    _palette_for,
    _unpack,
)
from powermon.w32 import gdi32, user32  # noqa: E402

PASS = FAIL = 0
REAL_AVAILABLE = 883        # 本机实测：长条左边可用 883px（见 _stripfields_test）


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
    tail = f"  ({detail})" if detail else ""
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{tail}")


# ------------------------------------------------------------------ 对比度

def _chan(value: int) -> float:
    c = value / 255.0
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def _luma(rgb) -> float:
    return (0.2126 * _chan(rgb[0]) + 0.7152 * _chan(rgb[1])
            + 0.0722 * _chan(rgb[2]))


def contrast(a, b) -> float:
    la, lb = _luma(a), _luma(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# ------------------------------------------------------------------ 夹具

class FakeSnap:
    current_w = 188.0
    cpu_w = 142.0
    gpu_w = 320.0
    base_w = 35.0
    session_wh = 1888.8
    session_cost = 88.88
    today_wh = 8888.0
    today_cost = 8.88
    average_w = 188.0
    peak_w = 320.0
    month_wh = 88888.0
    total_wh = 888888.0
    total_cost = 888.88
    segment = "平段"
    gpu_names = ["NVIDIA GeForce RTX 3080"]
    power_on_seconds = 88888.0


def base_cfg(**over) -> SimpleNamespace:
    cfg = dict(
        strip_theme="auto", strip_font_scale=1.0, strip_size="normal",
        strip_fields=["current", "cost", "session", "today"],
        strip_locked=False, strip_offset_x=0.0,
        strip_rows="auto", strip_palette="theme",
        strip_label_color="", strip_value_color="", strip_bg_color="",
        strip_bg_image="", strip_bg_fit="cover", strip_bg_opacity=100,
        strip_show_label=True, strip_show_divider=True,
    )
    cfg.update(over)
    return SimpleNamespace(**cfg)


def measure(cfg, limit=REAL_AVAILABLE, max_height=48) -> tuple[int, int]:
    """在删除兼容 DC 上量一次：返回 (宽度, 排了几行)。"""
    strip = TaskbarStrip(cfg)
    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    try:
        strip._limit = limit
        strip._max_height = max_height
        strip._scale = 1.0
        strip._anchor_kind = "start"
        width = strip._layout(dc, 1.0, FakeSnap(), render=False)[0]
        return int(width), strip._plan_rows
    finally:
        gdi32.DeleteDC(dc)
        user32.ReleaseDC(None, screen)


WIDE = 4000     # 给足横向空间：三种排数都能在「不收紧、不降字号」下排出来


def row_plan(cfg, limit=REAL_AVAILABLE, max_height=96) -> dict:
    """把 ``_plan`` 的结论摘成几项：网格宽 / 排数 / 最终字号 / 排下了几个字段。

    比 ``measure`` 多两样关键信息：**最终用的是哪档字号**、**有没有丢字段**。
    排数对比必须拿同一档字号的两次结果比 —— 否则「单行排不下 → 自动降字号」
    这条兜底会让单行反而比两行还窄，看数字像是「排数越多越短」被推翻了，
    其实是两次比的不是同一个字号。
    """
    strip = TaskbarStrip(cfg)
    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    try:
        strip._limit = limit
        strip._max_height = max_height
        strip._scale = 1.0
        strip._anchor_kind = "start"
        plan = strip._plan(dc, FakeSnap(), 1.0, limit, max_height)
        return {
            "w": int(plan["grid"]["width"]),
            "rows": len(plan["rows"]),
            "font": plan["font_scale"],
            "placed": plan["placed"],
            "total": plan["total"],
        }
    finally:
        gdi32.DeleteDC(dc)
        user32.ReleaseDC(None, screen)


def main() -> int:
    # ============================================== 1. 目录
    keys = [k for k, _ in stripopts.ROWS]
    check("排列档位键唯一且含 auto/1/2/3",
          len(set(keys)) == len(keys) == 4
          and set(keys) == {"auto", "1", "2", "3"}, f"{keys}")
    check("排列档位都有中文标签",
          all(label for _k, label in stripopts.ROWS))

    pal_keys = [p[0] for p in stripopts.PALETTES]
    check("配色方案键唯一、共 11 套、theme 排第一",
          len(set(pal_keys)) == len(pal_keys) == 11
          and pal_keys[0] == "theme", f"{len(pal_keys)} 套")
    check("除「跟随质感」外每套四色齐全（bg/ink/value/accent）",
          all(all(isinstance(c, tuple) and len(c) == 3
                  and all(0 <= v <= 255 for v in c)
                  for c in (p[2], p[3], p[4], p[5]))
              for p in stripopts.PALETTES[1:]))
    check("「跟随质感」的四色就是 None（表示不覆盖）",
          stripopts.PALETTES[0][2:] == (None, None, None, None, False))

    worst_value = ("", 99.0)
    worst_ink = ("", 99.0)
    for key, label, bg, ink, value, accent, _light in stripopts.PALETTES[1:]:
        cv = contrast(value, bg)
        ci = contrast(ink, bg)
        if cv < worst_value[1]:
            worst_value = (key, cv)
        if ci < worst_ink[1]:
            worst_ink = (key, ci)
    # 数值是主角：AA 正文级 4.5:1
    check("★ 每套方案的数值色对底色 ≥ 4.5:1（WCAG AA）",
          worst_value[1] >= 4.5, f"最差 {worst_value[0]} {worst_value[1]:.2f}:1")
    # 标签是次要信息，但也不能糊 —— 4.0 是本项目自己定的线
    check("★ 每套方案的标签色对底色 ≥ 4.0:1",
          worst_ink[1] >= 4.0, f"最差 {worst_ink[0]} {worst_ink[1]:.2f}:1")

    check("填充方式键含 cover/contain/tile",
          set(stripopts.BG_FIT_KEYS) == {"cover", "contain", "tile"})
    check("不透明度的合法区间是 0~100",
          (stripopts.BG_OPACITY_MIN, stripopts.BG_OPACITY_MAX) == (0, 100))

    # ============================================== 2. 取值
    check("rows_mode 遇到非法值回落到 auto",
          stripopts.rows_mode(base_cfg(strip_rows="7")) == "auto")
    check("fixed_rows：auto → None，'2' → 2",
          stripopts.fixed_rows(base_cfg()) is None
          and stripopts.fixed_rows(base_cfg(strip_rows="2")) == 2)
    check("palette：theme → None（不是「字段全 None 的字典」）",
          stripopts.palette(base_cfg()) is None
          and stripopts.palette(base_cfg(strip_palette="graphite")) is not None)
    check("palette：非法方案名 → None",
          stripopts.palette(base_cfg(strip_palette="不存在")) is None)

    check("hex_to_rgb 支持 3 位与 6 位",
          stripopts.hex_to_rgb("#abc") == (0xAA, 0xBB, 0xCC)
          and stripopts.hex_to_rgb("1E5CE0") == (0x1E, 0x5C, 0xE0))
    check("hex_to_rgb 拒绝垃圾输入",
          stripopts.hex_to_rgb("red") is None
          and stripopts.hex_to_rgb("#12345") is None
          and stripopts.hex_to_rgb("") is None
          and stripopts.hex_to_rgb(None) is None)
    check("rgb_to_hex 与 hex_to_rgb 往返一致",
          stripopts.hex_to_rgb(stripopts.rgb_to_hex((3, 250, 77)))
          == (3, 250, 77))
    check("bg_opacity 越界夹回区间内",
          stripopts.bg_opacity(base_cfg(strip_bg_opacity=999)) == 100
          and stripopts.bg_opacity(base_cfg(strip_bg_opacity=-5)) == 0
          and stripopts.bg_opacity(base_cfg(strip_bg_opacity="x")) == 100)
    check("show_label / show_divider 非 bool 时当作 True",
          stripopts.show_label(base_cfg(strip_show_label="yes")) is True
          and stripopts.show_divider(base_cfg(strip_show_divider=0)) is True)

    # ============================================== 3. sanitize
    dirty = base_cfg(
        strip_rows="7", strip_palette="xxx", strip_label_color="red",
        strip_value_color="#abc", strip_bg_color=12345,
        strip_bg_image=999, strip_bg_fit="stretch", strip_bg_opacity=999,
        strip_show_label="yes", strip_show_divider=None,
    )
    changed = stripopts.sanitize(dirty)
    check("★ sanitize 把一堆脏值全部夹回合法",
          changed
          and dirty.strip_rows == "auto"
          and dirty.strip_palette == "theme"
          and dirty.strip_label_color == ""
          and dirty.strip_value_color == "#AABBCC"
          and dirty.strip_bg_color == ""
          and dirty.strip_bg_image == ""
          and dirty.strip_bg_fit == "cover"
          and dirty.strip_bg_opacity == 100
          and dirty.strip_show_label is True
          and dirty.strip_show_divider is True,
          f"rows={dirty.strip_rows} value_color={dirty.strip_value_color} "
          f"opacity={dirty.strip_bg_opacity}")
    check("★ sanitize 是幂等的（第二次不该再改，否则每次启动都写盘）",
          stripopts.sanitize(dirty) is False)
    check("干净的默认配置不需要 sanitize",
          stripopts.sanitize(base_cfg()) is False)

    # ============================================== 4. 配色叠加
    pal_auto = _palette_for("auto", (32, 32, 32), False)
    same = _apply_scheme(pal_auto, base_cfg())
    check("「跟随质感」且无自定义色时原样返回（不构造新字典）",
          same is pal_auto)

    graphite = base_cfg(strip_palette="graphite")
    out = _apply_scheme(pal_auto, graphite)
    scheme = stripopts.palette(graphite)
    check("★ 方案底色生效", _unpack(out["bg"]) == scheme["bg"],
          f"{_unpack(out['bg'])}")
    check("★ 毛玻璃色调层跟着底色一起换（只改底色会糊出发灰的脏色）",
          out["frost_tint"] == scheme["bg"],
          f"tint={out['frost_tint']}")
    check("方案的数值色 / 标签色 / 强调色都进了调色板",
          _unpack(out["ink"]) == scheme["value"]
          and _unpack(out["dim"]) == scheme["ink"]
          and out["accent"] == scheme["accent"])
    check("换底色后描边和分隔线跟着重算（不会留一圈旧色的线）",
          _unpack(out["border"]) != _unpack(pal_auto["border"]))
    check("alpha（不透明度）保持质感档的原值",
          out["alpha"] == pal_auto["alpha"])

    over = base_cfg(strip_palette="graphite", strip_bg_color="#112233",
                    strip_label_color="#FFFFFF", strip_value_color="#FFD166")
    out2 = _apply_scheme(pal_auto, over)
    check("自定义底色覆盖方案底色（色调层同样跟上）",
          _unpack(out2["bg"]) == (0x11, 0x22, 0x33)
          and out2["frost_tint"] == (0x11, 0x22, 0x33))
    check("自定义标签色 / 数值色覆盖方案",
          _unpack(out2["dim"]) == (255, 255, 255)
          and _unpack(out2["ink"]) == (0xFF, 0xD1, 0x66))

    only_color = base_cfg(strip_label_color="#FF0000")
    out3 = _apply_scheme(pal_auto, only_color)
    check("只填一个自定义色时，其余仍走质感档（不是整块被重置）",
          _unpack(out3["dim"]) == (255, 0, 0)
          and _unpack(out3["bg"]) == _unpack(pal_auto["bg"])
          and _unpack(out3["ink"]) == _unpack(pal_auto["ink"]))

    # ============================================== 5. 排数真的作用到排版
    many = ["current", "cpu", "gpu", "base", "cost", "session",
            "today", "today_cost", "avg", "peak", "uptime", "segment"]
    a = row_plan(base_cfg(strip_fields=many, strip_rows="1"), limit=WIDE)
    b = row_plan(base_cfg(strip_fields=many, strip_rows="2"), limit=WIDE)
    c = row_plan(base_cfg(strip_fields=many, strip_rows="3"), limit=WIDE)
    check("★ 选「一排」就真的只排一排", a["rows"] == 1, f"{a}")
    check("★ 选「两排」就真的排两排", b["rows"] == 2, f"{b}")
    check("★ 选「三排」就真的排三排", c["rows"] == 3, f"{c}")
    check("★ 同一档字号下比较：排数越多横向越短（这是用户选它的理由）",
          a["font"] == b["font"] == c["font"] == 1.0
          and c["w"] < b["w"] < a["w"],
          f"{c['w']} < {b['w']} < {a['w']}（字号 {a['font']}）")

    # 真实可用宽度（883px）：12 个字段在单排 / 双排下的实际取舍
    real2 = row_plan(base_cfg(strip_fields=many, strip_rows="2"))
    check("★ 真实可用宽度下「两排」装得下全部字段（不必降字号、不必丢字段）",
          real2["rows"] == 2 and real2["font"] == 1.0
          and real2["placed"] == real2["total"], f"{real2}")
    real1 = row_plan(base_cfg(strip_fields=many, strip_rows="1"))
    check("选「一排」时绝不折行 —— 真排不下就少显示末尾几项，而不是偷偷折成两排",
          real1["rows"] == 1 and 0 < real1["placed"] <= real1["total"], f"{real1}")
    check("同一批字段：选「两排」比选「一排」显示得更全（这就是排数的用处）",
          real2["placed"] >= real1["placed"] and real2["rows"] > real1["rows"],
          f"一排 {real1['placed']}/{real1['total']}，"
          f"两排 {real2['placed']}/{real2['total']}")

    # 高度不够时退到放得下的排数，而不是把长条整条藏掉
    _w, r_tight = measure(base_cfg(strip_fields=many, strip_rows="3"),
                          max_height=24)
    check("★ 任务栏太矮时固定排数退到能放下的排数（不是整条消失）",
          r_tight < 3 and r_tight >= 1, f"rows={r_tight}")

    # ============================================== 6. 标签 / 分隔线开关
    base_fields = ["current", "cost", "session", "today"]
    w_base, _ = measure(base_cfg(strip_fields=base_fields))
    w_nolabel, _ = measure(base_cfg(strip_fields=base_fields,
                                    strip_show_label=False))
    w_nodiv, _ = measure(base_cfg(strip_fields=base_fields,
                                  strip_show_divider=False))
    check("★ 关掉标签后长条明显变窄",
          w_nolabel < w_base - 40, f"{w_nolabel} vs {w_base}")
    check("★ 关掉分隔线后也会变窄",
          0 < w_base - w_nodiv <= len(base_fields) * 24,
          f"{w_nodiv} vs {w_base}")
    check("两个开关同开时与旧版完全一致（默认值不动老用户外观）",
          w_base == measure(base_cfg(strip_fields=base_fields,
                                     strip_show_label=True,
                                     strip_show_divider=True))[0])

    # ============================================== 7. 背景图几何
    dx, dy, dw, dh, sx, sy, sw, sh = _fit_rect(1600, 900, 600, 48, "cover")
    check("cover：目标铺满整块胶囊",
          (dx, dy, dw, dh) == (0, 0, 600, 48), f"{dx},{dy},{dw},{dh}")
    # 1600x900 塞进 600x48：源比目标「更方」，所以裁掉的是**上下**（sx 保持 0）
    check("cover：源按目标比例居中裁剪，比例一致",
          abs(sw / sh - 600 / 48) < 0.06 and sx == 0 and sy > 0
          and sw == 1600, f"src={sx},{sy},{sw},{sh}")
    # 反过来：把一张细长图塞进方块，裁掉的该是左右
    dx, dy, dw, dh, sx, sy, sw, sh = _fit_rect(1600, 100, 48, 48, "cover")
    check("cover：细长图塞进方块时改裁左右（两种裁剪方向都覆盖到）",
          (dx, dy, dw, dh) == (0, 0, 48, 48) and sy == 0 and sh == 100
          and sx > 0 and abs(sw / sh - 1.0) < 0.06, f"src={sx},{sy},{sw},{sh}")
    dx, dy, dw, dh, sx, sy, sw, sh = _fit_rect(1600, 900, 600, 48, "contain")
    check("contain：整图等比缩小、水平居中、源取全图",
          sx == 0 and sy == 0 and sw == 1600 and sh == 900
          and abs(dw / dh - 1600 / 900) < 0.06
          and dx > 0 and dy == 0, f"{dx},{dy},{dw},{dh}")
    # 目标矩形必须拿得出正数尺寸：0 宽 0 高的 BitBlt/AlphaBlend 会静默什么都不画
    cover_tiny = _fit_rect(1, 1, 600, 48, "cover")
    contain_tiny = _fit_rect(1, 1, 600, 48, "contain")
    zero = _fit_rect(0, 0, 0, 0, "cover")
    check("超小 / 极端比例也不会算出 0 尺寸",
          all(cover_tiny[i] > 0 for i in (2, 3, 6, 7))
          and all(contain_tiny[i] > 0 for i in (2, 3, 6, 7))
          and all(zero[i] >= 1 for i in (2, 3, 6, 7)),
          f"cover={cover_tiny} contain={contain_tiny} zero={zero}")
    check("_fit_rect 只认 contain，其余（含未知值）一律按 cover 算"
          "（tile 由 _paint_image 自己循环铺，不走这里）",
          _fit_rect(1600, 900, 600, 48, "tile")[:4] == (0, 0, 600, 48)
          and (_fit_rect(1600, 900, 600, 48, "???")
               == _fit_rect(1600, 900, 600, 48, "cover")))

    # ============================================== 8. 预览借用的旋钮
    strip = TaskbarStrip(base_cfg())
    check("毛玻璃缓存键默认是 strip（预览会临时换成 appearance）",
          strip._frost_key == "strip")
    check("毛玻璃 hold 默认 None = 按拖动状态自动",
          strip._frost_hold is None)

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
