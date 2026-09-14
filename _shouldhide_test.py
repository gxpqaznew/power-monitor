"""_should_hide 决策回归测试（不建真实窗口，全部 mock）。

验证修复后的判定：
  * 最大化（WS_MAXIMIZE）的普通窗口 -> 不藏（常驻）
  * 真·独占全屏（无 WS_MAXIMIZE 且盖满屏） -> 藏
  * 桌面 / 任务栏自己 -> 不藏
  * 任务栏不可见（自动隐藏滑走） -> 藏
"""
import ctypes
import sys

# 测试里让 byref 变成原样透传，方便 fake 直接写回 RECT 字段
ctypes.byref = lambda x: x

import powermon.w32 as w32
import powermon.strip as strip_mod
from powermon.strip import TaskbarStrip

# ---- 假的 user32 / taskbar，按 hwnd 脚本化返回 ----------------------------

SCREEN = (0, 0, 2560, 1440)          # 假显示器全分辨率
TASKBAR = (0, 1392, 2560, 1440)      # 假任务栏：底部一条，始终可见


class FakeUser32:
    def __init__(self, fg, classes, styles, rects, rect_ok=True):
        self._fg = fg
        self._classes = classes
        self._styles = styles
        self._rects = rects
        self._rect_ok = rect_ok

    def GetForegroundWindow(self):
        return self._fg

    def GetClassNameW(self, hwnd, buf, n):
        buf.value = self._classes.get(hwnd, "")
        return 1

    def GetWindowLongPtrW(self, hwnd, which):
        return self._styles.get(hwnd, 0)

    def GetWindowRect(self, hwnd, rect):
        r = self._rects.get(hwnd)
        if r is None or not self._rect_ok:
            return 0
        rect.left, rect.top, rect.right, rect.bottom = r
        return 1


class FakeTaskbar:
    def __init__(self, info, visible=True, screen=SCREEN):
        self._info = info
        self._visible = visible
        self._screen = screen

    def taskbar(self):
        return self._info

    def is_visible(self, rect):
        return self._visible

    def screen_rect(self):
        return self._screen


def make_strip():
    s = TaskbarStrip(cfg=None)
    s._hwnd = 0x1234  # 占位，_should_hide 不用它
    return s


def run(name, expect, *, fg=0, classes=None, styles=None, rects=None,
       taskbar_info=(0x9999, TASKBAR, 96), visible=True, screen=SCREEN):
    fake_u = FakeUser32(fg, classes or {}, styles or {}, rects or {})
    fake_t = FakeTaskbar(taskbar_info, visible=visible, screen=screen)
    strip_mod.user32 = fake_u
    strip_mod.taskbar = fake_t
    s = make_strip()
    got = s._should_hide()
    ok = (got == expect)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: _should_hide={got} (期望 {expect})")
    return ok


def main():
    results = []

    # 1) 最大化窗口（浏览器/资源管理器）-> 不藏
    results.append(run(
        "最大化普通窗口应常驻",
        False,
        fg=0xAAAA,
        classes={0xAAAA: "Chrome_WidgetWin_0"},
        styles={0xAAAA: w32.WS_MAXIMIZE},
        rects={0xAAAA: SCREEN},
    ))

    # 2) 同窗口但没 WS_MAXIMIZE（真全屏边框less）-> 藏
    results.append(run(
        "无 WS_MAXIMIZE 且盖满屏应藏",
        True,
        fg=0xBBBB,
        classes={0xBBBB: "UnityWndClass"},
        styles={0xBBBB: 0},
        rects={0xBBBB: SCREEN},
    ))

    # 3) 桌面 Progman -> 不藏
    results.append(run(
        "桌面 Progman 不藏",
        False,
        fg=0xCCCC,
        classes={0xCCCC: "Progman"},
        styles={0xCCCC: 0},
        rects={0xCCCC: SCREEN},
    ))

    # 4) 任务栏自身 -> 不藏
    results.append(run(
        "任务栏 Shell_TrayWnd 不藏",
        False,
        fg=0x9999,  # == bar_hwnd
        classes={0x9999: "Shell_TrayWnd"},
        styles={0x9999: 0},
        rects={0x9999: TASKBAR},
    ))

    # 5) 没有前台窗口 -> 不藏
    results.append(run(
        "无前台窗口不藏",
        False,
        fg=0,
    ))

    # 6) 任务栏不可见（自动隐藏滑走）-> 藏
    results.append(run(
        "任务栏不可见应藏",
        True,
        fg=0xAAAA,
        classes={0xAAAA: "Chrome_WidgetWin_0"},
        styles={0xAAAA: w32.WS_MAXIMIZE},
        rects={0xAAAA: SCREEN},
        visible=False,
    ))

    # 7) 没有任务栏 -> 藏
    results.append(run(
        "无任务栏应藏",
        True,
        fg=0xAAAA,
        classes={0xAAAA: "Chrome_WidgetWin_0"},
        styles={0xAAAA: w32.WS_MAXIMIZE},
        rects={0xAAAA: SCREEN},
        taskbar_info=None,
    ))

    # 8) 前台窗口没盖满屏（小窗口）-> 不藏
    results.append(run(
        "未盖满屏的小窗口不藏",
        False,
        fg=0xDDDD,
        classes={0xDDDD: "CabinetWClass"},
        styles={0xDDDD: 0},
        rects={0xDDDD: (200, 200, 1200, 800)},
    ))

    passed = sum(results)
    total = len(results)
    print(f"\n{passed}/{total} PASS")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
