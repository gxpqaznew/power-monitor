"""「长条外观」窗口的离屏回归：真建窗口、真画一帧、真点一遍。

**为什么要单独测这个窗口**：它是一个 1000 多行的全自绘窗口（分段选择器 /
chip / 自定义滑块 / 圆形开关 / 色块网格），原生控件一个没用。这类代码的
故障模式非常固定 —— **逻辑全对、就是点不动或画不出来**：

  * ``_hits`` 少登记一块 → 那个按钮看着在，点了没反应；
  * 登记的区域和画出来的位置对不上 → 「点左边那个、右边那个亮」；
  * 滑块几何存成一个形状、按下时按另一个形状解包 → 异常被 ``_wnd_proc``
    吞掉，表现成「滑块拖不动」，而且**一点报错都看不到**（踩过）。
  * 内容排得比窗口高 → 底部按钮被切掉一半。

所以这里不比「某个像素是不是某个颜色」，而是比**结构**：
画一帧 → 把 ``_hits`` / ``_slider_geom`` 当成契约来查（控件齐不齐、有没有
越界、有没有互相压住）→ 再按登记的区域**真的走一遍点击派发**，看配置有没有
跟着变。像素只用来兜底证明「这一帧真的画了东西」。

跑法：python _appearanceshot.py（需要桌面会话，窗口建出来但不显示）。
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _appearancetest import FakeSnap, base_cfg  # noqa: E402
from powermon import appearance as ap  # noqa: E402
from powermon import stripopts  # noqa: E402
from powermon.app import APPEARANCE_KINDS, KIND_ATTR  # noqa: E402
from powermon.appearance import AppearanceWindow  # noqa: E402
from powermon.roundwin import dib_section  # noqa: E402
from powermon.strip import TaskbarStrip  # noqa: E402
from powermon.w32 import gdi32, user32  # noqa: E402

SRCCOPY = 0x00CC0020
PASS = FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


# ------------------------------------------------------------------ 夹具

class Applied:
    """假的 ``app`` 注入：记下每一次改动，并按 **app 的真实映射**写进 cfg。

    用 ``app.KIND_ATTR`` 而不是自己 ``setattr(cfg, f"strip_{kind}")``：后者会把
    ``font`` 写成 ``cfg.strip_font``（多出来一个没人读的属性），于是「点了没反应」
    这种真 bug 会被测试自己掩盖掉 —— 测试里踩过。
    """

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.log: list[tuple[str, object, bool]] = []

    def __call__(self, kind, value, persist=True) -> None:
        self.log.append((kind, value, persist))
        attr = KIND_ATTR.get(kind)
        if attr is None:
            raise AssertionError(f"app 不认识这个外观项：{kind!r}")
        setattr(self.cfg, attr, value)

    def kinds(self) -> list[str]:
        return [k for k, _v, _p in self.log]


def build(**over) -> tuple:
    cfg = base_cfg(**over)
    apply = Applied(cfg)
    strip = TaskbarStrip(cfg)
    strip._limit = 883          # 本机实测的可用宽度，见 _stripfields_test
    strip._max_height = 48
    win = AppearanceWindow(cfg, strip, FakeSnap, apply)
    win.create()                # 建出来但不显示（和点了菜单之后一样）
    return cfg, strip, win, apply


def render(win, warm: bool = False):
    """真画一帧（走 _paint 的同一条路：客户区尺寸 → 内存位图 → _draw）。"""
    hdc = user32.GetDC(win._hwnd)
    try:
        if warm:
            win._warm_frost()
        win._ensure_buffer(hdc)
        win._draw(win._buffer_dc)
    finally:
        user32.ReleaseDC(win._hwnd, hdc)
    return win._buffer_dc, win._buffer_w, win._buffer_h


def read_pixels(dc, w: int, h: int) -> bytes:
    tmp = gdi32.CreateCompatibleDC(dc)
    bmp, view = dib_section(tmp, w, h)
    old = gdi32.SelectObject(tmp, bmp)
    try:
        gdi32.BitBlt(tmp, 0, 0, w, h, dc, 0, 0, SRCCOPY)
        return bytes(view)
    finally:
        gdi32.SelectObject(tmp, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(tmp)


def pixel(data: bytes, w: int, x: int, y: int):
    i = (y * w + x) * 4
    return (data[i + 2], data[i + 1], data[i])       # view 是 BGRA


def hits_of(win) -> dict:
    out: dict[str, list] = {}
    for rect, action, payload in win._hits:
        out.setdefault(action, []).append((rect, payload))
    return out


def rect_of(hits: dict, action: str, payload=None):
    for rect, value in hits.get(action, ()):
        if payload is None or value == payload:
            return rect
    return None


def center(rect) -> tuple[int, int]:
    l, t, r, b = rect
    # 矩形里混着 float（chip 宽是算出来的），消息坐标必须是整数
    return (int((l + r) // 2), int((t + b) // 2))


def click(win, rect) -> None:
    """按登记的区域走一遍真实路径：WM_LBUTTONDOWN + WM_LBUTTONUP。"""
    x, y = center(rect)
    win._on_message(0x0201, 0, (y << 16) | (x & 0xFFFF))     # WM_LBUTTONDOWN
    win._on_message(0x0202, 0, (y << 16) | (x & 0xFFFF))     # WM_LBUTTONUP


# ------------------------------------------------------------------ 主流程

def main() -> int:
    # ============================================== 1. 建窗 + 画一帧
    cfg, strip, win, apply = build()
    check("窗口能建出来（类注册 + CreateWindowEx）", bool(win._hwnd), f"hwnd={win._hwnd}")
    if not win._hwnd:
        print("\n窗口建不出来，后面的都没意义")
        return 1

    dc, w, h = render(win, warm=True)
    data = read_pixels(dc, w, h)
    colors = {data[i * 4:i * 4 + 4] for i in range(0, w * h, 5)}
    check("★ 画一帧不抛异常，且画面不是一块死色",
          len(colors) > 60, f"{len(colors)} 种取值 / {w}x{h}")

    hits = hits_of(win)

    # ============================================== 2. 控件齐不齐
    check("★ 排列四档都可点（自动 / 一排 / 两排 / 三排）",
          len(hits.get("rows", [])) == 4, f"{len(hits.get('rows', []))} 个")
    check("★ 大小三档都可点", len(hits.get("size", [])) == 3)
    check("★ 11 套配色全都在网格里",
          len(hits.get("palette", [])) == len(stripopts.PALETTES),
          f"{len(hits.get('palette', []))} / {len(stripopts.PALETTES)}")
    check("配色键与目录一致（不是随手写死的字符串）",
          sorted(p for _r, p in hits.get("palette", [])) == sorted(stripopts.PALETTE_KEYS))
    check("★ 三种填充方式都可点", sorted(p for _r, p in hits.get("fit", []))
          == sorted(stripopts.BG_FIT_KEYS))
    check("★ 两个细节开关都可点",
          sorted(p for _r, p in hits.get("toggle", [])) == ["show_divider", "show_label"])
    check("★ 三个自定义色都可点",
          sorted(p for _r, p in hits.get("color", [])) == ["bg", "label", "value"])
    check("★ 字号六档快捷 chip 都可点",
          len(hits.get("font", [])) == len(stripopts.FONT_SCALES),
          f"{len(hits.get('font', []))}")
    check("选图 / 恢复默认 / 完成 都有落点",
          {"image_pick", "reset", "done"} <= set(hits))
    check("没有自定义色时不该出现「×」清除按钮（否则点了个空）",
          "color_clear" not in hits)

    # ============================================== 3. 区域合法性
    bad = [(r, a) for r, a, _p in win._hits
           if r[2] <= r[0] or r[3] <= r[1] or r[0] < 0 or r[1] < 0
           or r[2] > w or r[3] > h]
    check("★ 每个可点区域都是正尺寸、且不越出客户区", not bad, f"{bad[:3]}")
    fat = [r for r, _a, _p in win._hits if r[2] - r[0] < 18 or r[3] - r[1] < 16]
    check("没有小到点不中的落点（最小边 ≥ 16px）", not fat, f"{fat[:3]}")

    done_rect = rect_of(hits, "done")
    reset_rect = rect_of(hits, "reset")
    lowest = max(r[3] for r, a, _p in win._hits if a not in ("done", "reset"))
    check("★ 内容没压到底部按钮（最后一段整体在按钮之上）",
          lowest <= done_rect[1] and lowest <= reset_rect[1],
          f"内容底 {lowest} vs 按钮顶 {done_rect[1]}（余量 {done_rect[1] - lowest}px）")
    check("底部两个按钮同一行、右对齐（完成在最右）",
          reset_rect[1] == done_rect[1] and done_rect[2] > reset_rect[2],
          f"reset={reset_rect} done={done_rect}")

    # ============================================== 4. 预览真的画了
    px, py, pw, ph = win._preview_box()
    card_top = win.s(ap.TOP_GAP + ap.TITLE_H + ap.SUB_H + ap.PREVIEW_GAP)
    check("预览卡片顶边 == 「常量算式」那一份（两处数字没有各写各的）",
          win._preview_y() == card_top, f"{win._preview_y()} vs {card_top}")
    card_h = win.s(ap.PREVIEW_CARD_H)
    check("预览框整个落在卡片里（不是画到卡片外面）",
          py >= card_top and py + ph <= card_top + card_h,
          f"框 {py}..{py + ph} 卡片 {card_top}..{card_top + card_h}")
    check("预览框整个落在卡片里（不是画到卡片外面）",
          py >= card_top and py + ph <= card_top + card_h,
          f"框 {py}..{py + ph} 卡片 {card_top}..{card_top + card_h}")
    samples = [pixel(data, w, x, y) for x in range(px + 4, px + pw - 4, 3)
               for y in range(py + 4, py + ph - 4, 2)]
    card = pixel(data, w, px - 6, py + 4)      # 卡片上、预览框外的那一点
    same = sum(1 for p in samples if p == card)
    check("★ 预览区里真的画了一条长条（不是只剩卡片底色）",
          len(set(samples)) > 12 and same < len(samples) * 0.5,
          f"{len(set(samples))} 种取值，与卡片底色相同的占 {same / len(samples):.0%}")

    # ============================================== 5. 点击真的改到配置
    click(win, rect_of(hits, "rows", "2"))
    check("★ 点「两排」→ 配置变成两排", cfg.strip_rows == "2",
          f"{apply.log[-1] if apply.log else None}")
    click(win, rect_of(hits, "palette", "mint"))
    check("★ 点薄荷色块 → 配色方案跟着变", cfg.strip_palette == "mint")
    click(win, rect_of(hits, "size", "slim"))
    check("点「纤细」→ 大小跟着变", cfg.strip_size == "slim")
    click(win, rect_of(hits, "fit", "tile"))
    check("点「平铺」→ 填充方式跟着变", cfg.strip_bg_fit == "tile")
    chip = hits["font"][4]                       # 「特大」那一档
    click(win, chip[0])
    check("点字号快捷档 → 字号等于那一档的值",
          abs(cfg.strip_font_scale - float(chip[1])) < 1e-6,
          f"{cfg.strip_font_scale} vs 档位 {chip[1]}")
    before = cfg.strip_show_label
    click(win, rect_of(hits, "toggle", "show_label"))
    check("点「显示标签」开关 → 开关翻转", cfg.strip_show_label is (not before),
          f"{before} → {cfg.strip_show_label}")

    # 每一次点击都必须是 persist=True（用户点一下就该存盘）
    check("点击一律落盘（persist=True）", all(p for _k, _v, p in apply.log))
    check("点击没有误伤别的项（只动点的那一个）",
          apply.kinds() == ["rows", "palette", "size", "bg_fit", "font", "show_label"],
          f"{apply.kinds()}")

    # ============================================== 6. 滑块
    render(win)
    check("★ 两条滑块都登记了几何（字号 + 背景图不透明度）",
          set(win._slider_geom) == {"font", "bg_opacity"}, f"{set(win._slider_geom)}")
    geom = win._slider_geom["font"]
    check("滑块几何是四元组（左端 / 轨道宽 / 带顶 / 带高）",
          len(geom) == 4 and geom[1] > 0 and geom[3] > 0, f"{geom}")

    l, tw, top, band = geom
    win._drag_slider = None
    win._on_press(l + tw // 2, top + band // 2)       # 按在滑块带上
    check("★ 按在滑块带上就进入拖动（这里以前按 4 元组解包 3 元组，"
          "异常被吞掉 = 滑块拖不动）",
          win._drag_slider == "font", f"{win._drag_slider}")
    win._drag_slider = None
    win._on_press(l + tw // 2, top + band + win.s(40))
    check("按在滑块带外面不会误进拖动", win._drag_slider is None)

    apply.log.clear()
    win._drag_slider = "font"
    win._drag_to(l + tw)                     # 拖到最右（拖动中）
    check("★ 拖到最右 = 字号上限，且拖动中不落盘",
          abs(cfg.strip_font_scale - stripopts.FONT_SCALE_MAX) < 1e-6
          and apply.log[-1][2] is False,
          f"{cfg.strip_font_scale} persist={apply.log[-1][2]}")
    win._drag_to(l)                          # 拖到最左
    check("拖到最左 = 字号下限",
          abs(cfg.strip_font_scale - stripopts.FONT_SCALE_MIN) < 1e-6,
          f"{cfg.strip_font_scale}")
    win._drag_to(l, persist=True)            # 松手
    check("松手那一下才落盘（persist=True）", apply.log[-1][2] is True)

    win._drag_slider = "bg_opacity"
    win._drag_to(l + tw)
    check("不透明度滑到最右 = 100%", stripopts.bg_opacity(cfg) == 100,
          f"{cfg.strip_bg_opacity}")

    # 拖动中改了值 → 画面跟着变（不重画就是「拖了半天没反应」）
    win._drag_slider = "font"
    win._drag_to(l + tw * 0.75)
    fat_scale = cfg.strip_font_scale
    win._drag_slider = None
    dc, w2, h2 = render(win)
    check("拖完重画，预览跟着变（不再等于拖之前的画面）",
          read_pixels(dc, w2, h2) != data, "")

    # ============================================== 7. 契约：窗口发得出、app 认得
    # 这一节守的是最容易漏的一种错：窗口加了新控件、kind 拼错、或者 app 那边
    # 少写一个分支 —— 三种都不会报错，只是「点了没反应」。
    img = Path(__file__).resolve().parent / "_preview" / "desktop.png"
    cfg3, _s3, win3, apply3 = build(strip_label_color="#FF0000",
                                    strip_value_color="#00FF00",
                                    strip_bg_color="#0000FF",
                                    strip_bg_image=str(img))
    render(win3)
    h3 = hits_of(win3)
    check("（前置）给了自定义色 + 背景图之后，清除按钮才出现",
          "color_clear" in h3 and "image_clear" in h3, f"{sorted(h3)}")
    for rect, action, payload in list(win3._hits):
        if action in ("color", "image_pick"):
            continue          # 这两个会弹系统对话框（选色 / 选文件），没法自动化
        win3._dispatch(action, payload)
    win3._drag_slider = "font"
    win3._drag_to(win3._slider_geom["font"][0] + 12)
    win3._drag_slider = "bg_opacity"
    win3._drag_to(win3._slider_geom["bg_opacity"][0] + 12)
    win3._drag_slider = None
    emitted = set(apply3.kinds())
    check("★ 窗口发出的每一种外观项，app 都认得（认不出 = 点了没反应）",
          emitted <= set(APPEARANCE_KINDS),
          f"多出来的：{sorted(emitted - set(APPEARANCE_KINDS))}")
    check("★ 窗口点得动 / 拖得动的项，一个都没漏掉",
          emitted == {"rows", "size", "palette", "bg_fit", "bg_image", "bg_opacity",
                      "font", "label_color", "value_color", "bg_color",
                      "show_label", "show_divider"},
          f"{sorted(emitted)}")
    apply3.log.clear()
    win3._reset()
    reset_kinds = set(apply3.kinds())
    check("★ 「恢复默认」覆盖的项 == 窗口能改的项全集"
          "（漏一项 = 恢复默认之后那一项还留着旧值）",
          reset_kinds == emitted, f"少了：{sorted(emitted - reset_kinds)}")
    # 设了背景图之后，预览**后面**的内容还得画得出来。
    # 这一条是被对照图抓出来的：长条 ``_paint_image`` 里拿 ``SelectClipRgn`` 的
    # 返回值（其实是区域类型码，不是句柄）当旧区域还原，DC 的剪辑区被搞坏 ——
    # 表现成「设了背景图之后，『排列』整段和『字号』标题凭空消失」。长条自己看
    # 不出来（后面画的东西都在胶囊里），但外观窗口拿同一个 DC 继续画就露馅了。
    d3, w3, h3 = render(win3)
    p3 = read_pixels(d3, w3, h3)
    below3 = {pixel(p3, w3, x, y) for x in range(24, 496, 4)
              for y in range(132, 700, 4)}
    below0 = {pixel(data, w, x, y) for x in range(24, 496, 4)
              for y in range(132, 700, 4)}
    check("★ 带背景图时，预览下面的内容照样完整画出来",
          len(below3) >= len(below0) * 0.8,
          f"{len(below3)} vs {len(below0)} 种取值")
    win3.destroy()

    # ============================================== 8. 恢复默认
    cfg2, _s2, win2, apply2 = build(strip_rows="3", strip_palette="wine",
                                    strip_label_color="#FF0000",
                                    strip_bg_opacity=40,
                                    strip_show_label=False,
                                    strip_offset_x=133.0,
                                    strip_fields=["current", "cost", "total_cost"])
    check("（前置）这一份配置确实是「被改过」的",
          cfg2.strip_rows == "3" and cfg2.strip_palette == "wine"
          and cfg2.strip_label_color == "#FF0000")
    win2._reset()
    check("★ 恢复默认：外观项全部回出厂",
          cfg2.strip_rows == "auto" and cfg2.strip_palette == "theme"
          and cfg2.strip_label_color == "" and cfg2.strip_bg_opacity == 100
          and cfg2.strip_show_label is True
          and cfg2.strip_font_scale == 1.0 and cfg2.strip_size == "normal"
          and cfg2.strip_bg_image == "" and cfg2.strip_bg_fit == "cover",
          f"rows={cfg2.strip_rows} palette={cfg2.strip_palette}")
    check("★ 恢复默认不动「内容」和「位置」"
          "（勾了哪些字段、拖到哪儿是用户的内容决定，不该被顺手抹掉）",
          cfg2.strip_offset_x == 133.0
          and cfg2.strip_fields == ["current", "cost", "total_cost"],
          f"偏移 {cfg2.strip_offset_x} 字段 {cfg2.strip_fields}")

    # ============================================== 9. 反复重画 / 关闭
    for _ in range(3):
        render(win)
    check("反复重画不炸（缓存位图按客户区尺寸复用）", True)
    win.hide()
    check("hide 之后不再可见", win.is_visible is False)
    win.show()
    check("再 show 能回来（毛玻璃暖缓存路径不抛异常）", win.is_visible is True)
    win2.destroy()
    win.destroy()
    check("destroy 之后 hwnd 清空、GDI 对象都回收了",
          win._hwnd is None and not win._fonts and not win._brushes
          and not win._pens and win._buffer_dc is None)

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
