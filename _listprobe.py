"""ListView 不画行的排查探针。

统计窗口的表格：`LVM_GETITEMCOUNT` 说 45 行、`LVM_GETITEMTEXTW` 也读得回文字，
但截图里表体是空的。文字在控件里、却没画出来 —— 两种情况：
  1. 控件真的在画，但画的不是我以为的那块地方（尺寸/位置/可见性不对）；
  2. 控件被要求不重画（WM_SETREDRAW 没恢复 / 更新区域被丢掉）。

这个探针把可以问控件的都问一遍，再对「刚插入、没被任何开关动过」的原始
ListView 做同样的操作作对照 —— 差别在哪一步，一眼就能定位。

跑法：python _listprobe.py
"""

from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import stats as stats_mod  # noqa: E402
from powermon.app import enable_dpi_awareness  # noqa: E402
from powermon.w32 import gdi32, user32, wintypes  # noqa: E402

LVM_FIRST = 0x1000
LVM_GETITEMCOUNT = LVM_FIRST + 4
LVM_GETITEMRECT = LVM_FIRST + 14
LVM_GETCOLUMNWIDTH = LVM_FIRST + 29
LVM_GETHEADER = LVM_FIRST + 31
LVIR_BOUNDS = 0

PM_REMOVE = 0x0001


def pump(seconds: float = 0.3) -> None:
    msg = wintypes.MSG()
    end = time.time() + seconds
    while time.time() < end:
        while user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, PM_REMOVE):
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))
        time.sleep(0.01)


def dump(title: str, lv) -> None:
    rect = wintypes.RECT()
    user32.GetWindowRect(lv, ctypes.byref(rect))
    parent = user32.GetParent(lv)
    prect = wintypes.RECT()
    user32.GetWindowRect(parent, ctypes.byref(prect))
    header = user32.SendMessageW(lv, LVM_GETHEADER, 0, 0)
    hrect = wintypes.RECT()
    if header:
        user32.GetWindowRect(header, ctypes.byref(hrect))
    style = user32.GetWindowLongPtrW(lv, -16)
    print(f"--- {title} ---")
    print(f"  hwnd={lv} parent={parent}")
    print(f"  IsWindowVisible={bool(user32.IsWindowVisible(lv))} "
          f"IsWindow={bool(user32.IsWindow(lv))}")
    print(f"  控件屏幕矩形=({rect.left},{rect.top},{rect.right},{rect.bottom}) 父窗口=({prect.left},{prect.top},{prect.right},{prect.bottom})")
    print(f"  样式=0x{style & 0xFFFFFFFF:08X} (LVS_REPORT={'是' if style & 1 else '否'})")
    print(f"  header={header} rect="
              f"({hrect.left},{hrect.top},{hrect.right},{hrect.bottom})")
    count = user32.SendMessageW(lv, LVM_GETITEMCOUNT, 0, 0)
    print(f"  item 数={count}")
    for c in range(5):
        print(f"  列{c} 宽={user32.SendMessageW(lv, LVM_GETCOLUMNWIDTH, c, 0)}", end="")
    print()
    for i in range(min(2, int(count))):
        r = wintypes.RECT(0, 0, LVIR_BOUNDS, 0)
        ok = user32.SendMessageW(lv, LVM_GETITEMRECT, i, ctypes.addressof(r))
        print(f"  行{i} LVM_GETITEMRECT={ok} rect=({r.left},{r.top},{r.right},{r.bottom})")


def main() -> int:
    enable_dpi_awareness()
    from powermon import meter as meter_mod
    from powermon.config import Config

    cfg = Config()
    meter = meter_mod.EnergyMeter(cfg)
    window = stats_mod.StatsWindow(meter, cfg)
    window.create()
    window.show()
    pump(0.6)

    lv = window._controls.get(stats_mod.IDC_LIST)
    # 先问一遍（此时 refresh 已经把行插进去了）
    dump("统计窗口的 ListView（refresh 之后）", lv)

    # 恢复重绘 + 强刷一次，看行会不会出来
    from powermon.w32 import WM_SETREDRAW  # noqa: E402
    user32.SendMessageW(lv, WM_SETREDRAW, 1, 0)
    RDW_INVALIDATE, RDW_UPDATENOW, RDW_ALLCHILDREN = 0x0001, 0x0100, 0x0080
    user32.RedrawWindow(lv, None, None,
                        RDW_INVALIDATE | RDW_UPDATENOW | RDW_ALLCHILDREN)
    user32.UpdateWindow(lv)
    pump(0.4)
    print("（已 RedrawWindow + UpdateWindow，截图看是否有变化）")

    # 对照：往同一个控件里再插一行，看能不能画出来
    item = stats_mod.LVITEMW()
    item.mask = stats_mod.LVIF_TEXT
    item.iItem = 0
    item.iSubItem = 0
    text = "对照行"
    item.pszText = text
    item.cchTextMax = len(text)
    stats_mod._send_text(lv, stats_mod.LVM_INSERTITEMW, 0, item)
    user32.UpdateWindow(lv)
    pump(0.4)
    print(f"插入一行后 item 数={user32.SendMessageW(lv, LVM_GETITEMCOUNT, 0, 0)}")

    rect = wintypes.RECT()
    user32.GetWindowRect(window._hwnd, ctypes.byref(rect))
    print(f"窗口矩形=({rect.left},{rect.top},{rect.right},{rect.bottom})")
    window.destroy()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
