"""长条「勾了多少就显示多少」回归测试。

用户反馈：在「长条显示内容」里勾了七八项，长条上永远只有 4 项。

根因不在勾选逻辑（``stripopts.sections`` 老老实实把勾的都返回了），而在
``strip._width_cap()``：它返回的 ``_MAX_WIDTH = 560`` 被当成了真上限，
于是 ``_layout`` 里「宽度不够就从后往前丢字段」那段老老实实把第 5 项之后的
全丢了 —— 而当时锚点左边其实有一千多像素空地。代码看起来完全正常，
只有量一下「实际排了几段」才看得出来。

放开上限只解决一半：本机（2560 宽、图标簇居中）长条左边实际只有 **883px**
可用，单行装 6 项就到头了。所以后来又加了**折行**（不够再换短标签、收紧间距、
最后一档小字号）。这条链路每一环都要钉住，否则很容易悄悄退回「只显示 4 项」。

跑法：python _stripfields_test.py（需要一个桌面会话，但**不建真实窗口**，
只在一个删除兼容 DC 上量字宽）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import stripopts  # noqa: E402
from powermon.config import Config  # noqa: E402
from powermon.strip import (  # noqa: E402
    _ABS_MIN_WIDTH,
    _DENSITY,
    _MAX_ROWS,
    _ROW_H,
    _MAX_WIDTH,
    _MAX_WIDTH_TRAY,
    TaskbarStrip,
)
from powermon.w32 import gdi32, user32  # noqa: E402

PASS = FAIL = 0

# 本机实测（2560×1440 @125%）：任务栏 60px 高，图标簇左边 891px，
# 也就是长条能用 883px。测试里直接用它当可用宽度。
REAL_AVAILABLE = 883


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
    tail = f"  ({detail})" if detail else ""
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{tail}")


class FakeSnap:
    """值都取「最宽」的那种，宽度测试才有意义。"""

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
    gpu_limits = [320.0]
    power_on_seconds = 88888.0


class SpyStrip(TaskbarStrip):
    """记录最后一次排版计划：「排了几行、落下几个字段、走的哪档密度」。"""

    last = None

    def _plan(self, dc, snap, scale, limit, max_height):
        plan = super()._plan(dc, snap, scale, limit, max_height)
        SpyStrip.last = {
            "rows": len(plan["rows"]),
            "placed": plan["placed"],
            "total": plan["total"],
            "font_scale": plan["font_scale"],
            "row_h": plan["row_h"],
            "density": plan["density"],
        }
        return plan


def main() -> int:
    cfg = Config()
    cfg.strip_fields = list(stripopts.FIELD_KEYS)      # 15 项全勾
    strip = SpyStrip(cfg)
    snap = FakeSnap()
    all_sections = strip._sections(snap)
    total_fields = len(stripopts.FIELD_KEYS)
    check(f"勾了 {total_fields} 项，_sections 就得返回 {total_fields} 项",
          len(all_sections) == total_fields,
          f"{len(all_sections)} / {total_fields}")

    screen = user32.GetDC(None)
    dc = gdi32.CreateCompatibleDC(screen)
    try:
        scale = 1.25                      # 本机 DPI 下的实际缩放
        bar_h = int(round(48 * scale))    # 60px
        budget = int(round(bar_h * 0.94))  # 折行允许的高度预算 56px
        strip._anchor_kind = "start"

        # ---- 老行为：老上限（560）+ 只允许单行 ----
        # 这两条正是当年把字段吃到只剩 4 个的两个原因：宽度被 560 卡住，且代码
        # 完全没有折行 —— 排不下就老老实实从后往前丢。历史实测值是 4 项。
        #
        # 「只允许单行」要按**最小字号的那一行**去卡高度，否则 0.72 档能在同样的
        # 胶囊高度里塞下两行，就复现不出当年的单行场景了。
        strip._max_height = int(_ROW_H * scale * min(stripopts.FONT_SCALE_VALUES))
        strip._limit = int(round(_MAX_WIDTH_TRAY * scale))
        strip._layout(dc, scale, snap, render=False)
        old = dict(SpyStrip.last)
        check("旧上限 + 只许单行 → 明显排不下（复现「只有几项」）",
              old["rows"] == 1 and old["placed"] <= 8, str(old))

        # ---- 新行为：放开宽度上限 + 允许折行 ----
        strip._max_height = budget
        strip._limit = REAL_AVAILABLE
        width = strip._layout(dc, scale, snap, render=False)[0]
        new = dict(SpyStrip.last)
        check(f"可用 {REAL_AVAILABLE}px + 折行 → 勾的 {total_fields} 项全排上",
              new["placed"] == total_fields, str(new))
        check("确实折了不止一行", new["rows"] >= 2, f"{new['rows']} 行")
        check("折行没有超过行数上限", new["rows"] <= _MAX_ROWS, str(new["rows"]))
        check("宽度没有超出可用宽度", width <= REAL_AVAILABLE,
              f"{width} <= {REAL_AVAILABLE}")
        check("需要的总高度在预算内",
              new["row_h"] * new["rows"] <= budget + 1,
              f"{new['row_h'] * new['rows']:.0f} <= {budget}")
        check("折行时字号降了一档（拿高度换行数）",
              new["font_scale"] <= stripopts.font_scale(cfg),
              f"{new['font_scale']} <= {stripopts.font_scale(cfg)}")
        check("挤的时候启用了更密的档（短标签 / 收紧间距）",
              new["density"] > 0, f"密度档 {new['density']}")

        # ---- 绘制高度：画布必须占满窗口，不能只画一条矮胶囊 ----
        # 踩过的坑：折行改造时画布高度写成「行高 × 行数」，单行时只有 32.5px，
        # 而窗口是 47px 的胶囊 —— 下面 14.5px 没被填过，叠上半透明就是一条黑带。
        strip.cfg.strip_fields = ["current", "cost", "session", "today"]
        strip._limit = 10 ** 6
        strip._max_height = int(round(bar_h * 0.78))
        win_h = int(round(bar_h * stripopts.height_ratio(cfg)))
        _w0, canvas_h, _pal = strip._layout(dc, scale, snap, render=False,
                                            height=win_h)
        check("单行时画布高度 == 窗口胶囊高度（不会画出矮一截的胶囊）",
              canvas_h == win_h, f"{canvas_h} == {win_h}")
        _w1, meas_h, _p = strip._layout(dc, scale, snap, render=False)
        check("量宽阶段（还没有窗口高度）返回内容本身的高度",
              meas_h > 0 and meas_h <= win_h, f"{meas_h}")

        # ---- 字段少的时候外观必须和以前一样（单行、原字号、最松的档） ----
        strip._max_height = int(round(bar_h * 0.78))
        strip._layout(dc, scale, snap, render=False)
        few = dict(SpyStrip.last)
        check("只勾 4 项时仍是单行、原字号、不走密档（外观不跳变）",
              few["rows"] == 1 and few["font_scale"] == stripopts.font_scale(cfg)
              and few["density"] == 0, str(few))

        # ---- 逐档加字段：不许在任何一档上「偷偷丢字段」 ----
        strip._max_height = budget
        strip._limit = REAL_AVAILABLE
        for n in (4, 5, 6, 8, 10, 12, 15):
            strip.cfg.strip_fields = list(stripopts.FIELD_KEYS)[:n]
            strip._layout(dc, scale, snap, render=False)
            got = dict(SpyStrip.last)
            check(f"勾 {n:>2} 项 → 全排上（不丢字段）",
                  got["placed"] == got["total"] == n,
                  f"{got['placed']}/{got['total']}，{got['rows']} 行，"
                  f"密度 {got['density']}，字号 {got['font_scale']}")

        # ---- 可用宽度变窄时：允许少显示，但不能整条消失或排成一大坨 ----
        for available in (700, 560, 420, 300):
            strip.cfg.strip_fields = list(stripopts.FIELD_KEYS)
            strip._limit = available
            width = strip._layout(dc, scale, snap, render=False)[0]
            got = dict(SpyStrip.last)
            check(f"可用 {available}px → 排得下就整条不超宽、行数不超上限",
                  width <= available and got["rows"] <= _MAX_ROWS,
                  f"宽 {width}，{got['placed']}/{got['total']} 项，"
                  f"{got['rows']} 行")

        # ---- 排版计划必须稳定：数值变化不能让「排几行」跳来跳去 ----
        # 折行加入之后多了一条新的抖动来源：同一个字段集合，数值一变（95 W → 118 W、
        # 428 Wh → 1.02 kWh）就可能从 1 行方案跳到 2 行方案，长条高度跟着在 47px 和
        # 55px 之间来回弹 —— 比宽度抖动刺眼得多。所以拿「最窄值」和「最宽值」两组
        # 快照各排一次，计划必须完全一致。
        class NarrowSnap(FakeSnap):
            current_w = 9.0
            cpu_w = 3.0
            gpu_w = 12.0
            base_w = 5.0
            session_wh = 12.0
            session_cost = 0.01
            today_wh = 40.0
            today_cost = 0.02
            average_w = 8.0
            peak_w = 41.0
            month_wh = 900.0
            total_wh = 400.0
            total_cost = 0.30
            segment = "峰段"
            power_on_seconds = 300.0

        strip._limit = REAL_AVAILABLE
        strip._max_height = budget
        for n in (4, 6, 8, 10, 12, 15):
            strip.cfg.strip_fields = list(stripopts.FIELD_KEYS)[:n]
            strip._layout(dc, scale, snap, render=False)
            wide_plan = dict(SpyStrip.last)
            strip._layout(dc, scale, NarrowSnap(), render=False)
            narrow_plan = dict(SpyStrip.last)
            same = (wide_plan["rows"] == narrow_plan["rows"]
                    and wide_plan["font_scale"] == narrow_plan["font_scale"]
                    and wide_plan["density"] == narrow_plan["density"]
                    and wide_plan["placed"] == narrow_plan["placed"])
            check(f"勾 {n:>2} 项：最宽值 / 最窄值排出的计划一致（高度不跳）",
                  same, f"宽值 {wide_plan} / 窄值 {narrow_plan}")
    finally:
        gdi32.DeleteDC(dc)
        user32.ReleaseDC(None, screen)

    # ---- 雪崩线本身 ----
    strip.cfg.strip_fields = list(stripopts.FIELD_KEYS)
    strip._anchor_kind = "start"
    cap_start = strip._width_cap(1.0)
    strip._anchor_kind = "tray"
    cap_tray = strip._width_cap(1.0)
    check("贴开始按钮时上限已放开（不再卡 560）",
          cap_start >= 1200.0, f"{cap_start:.0f}px")
    check("退化成贴通知区域时仍是保守的 560（别盖住任务图标）",
          abs(cap_tray - _MAX_WIDTH_TRAY) < 1e-6, f"{cap_tray:.0f}px")
    check("_MAX_WIDTH 这条雪崩线本身也够大",
          _MAX_WIDTH >= 1600.0, f"{_MAX_WIDTH:.0f}px")

    cfg.strip_font_scale = 1.5
    strip._anchor_kind = "start"
    check("字号调大，上限跟着放宽",
          strip._width_cap(1.0) > cap_start, f"{strip._width_cap(1.0):.0f}px")

    check("可用宽度低于底线时整条消失的判据还在",
          _ABS_MIN_WIDTH >= 100.0, f"{_ABS_MIN_WIDTH}px")
    check("折行最多不超过 3 行（再多就没法看了）",
          2 <= _MAX_ROWS <= 4, f"{_MAX_ROWS} 行")

    # ---- 密度档契约 ----
    check("第 0 档密度必须是「原样」档（不换标签、不收间距）",
          _DENSITY[0] == (False, 1.0, 1.0), str(_DENSITY[0]))
    check("密度档从松到紧排列（分隔 / 间隙倍数单调不增）",
          all(_DENSITY[i][1] >= _DENSITY[i + 1][1]
              and _DENSITY[i][2] >= _DENSITY[i + 1][2]
              for i in range(len(_DENSITY) - 1)),
          str(_DENSITY))

    # ---- 短标签必须覆盖所有字段，否则密档下一部分字段还是老长名字 ----
    missing = [k for k in stripopts.FIELD_KEYS if k not in stripopts.SHORT_LABEL]
    check("每个字段都有紧凑短标签（密档才不会长一截）",
          not missing, str(missing))

    def _short_enough(text: str) -> bool:
        # 汉字算两格宽，「当前」= 4 格；「CPU」是三个 ASCII 字母，只有 3 格。
        # 所以中文限两字，ASCII 限四字符。
        return len(text) <= 2 or (text.isascii() and len(text) <= 4)

    too_long = [v for v in stripopts.SHORT_LABEL.values() if not _short_enough(v)]
    check("短标签确实够短（中文 ≤2 字 / ASCII ≤4 字符）",
          not too_long, str(too_long))

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
