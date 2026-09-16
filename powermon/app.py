"""应用装配（纯 Win32）：托盘图标 + GDI 详情面板 + 后台采样。

完全不依赖 Qt / Pillow：
  * 托盘   自建隐藏消息窗口 + Shell_NotifyIcon
  * 面板   CreateWindowEx + WM_PAINT 自绘（双缓冲）
  * 图标   gdi32 现画 + 手写 alpha，动态生成 HICON
  * 计时   线程定时器，每秒一次，只在数字变化时才重画图标

常驻内存约为 Qt 方案的十分之一，空闲 CPU 占用接近 0。
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import threading
import time
from pathlib import Path

from . import APP_NAME, __version__
from . import debug, stripopts, trayreg
from .config import CONFIG_PATH, Config, app_dir
from .fee_dialog import FeeSettingsDialog
from .iconmake import make_icon, tray_label
from .meter import EnergyMeter, Snapshot
from .panel import Panel, fmt_duration
from .strip import TaskbarStrip
from .stats import StatsWindow
from .tray import (
    CMD_ABOUT,
    CMD_FEE_SETTINGS,
    CMD_FIELD_BASE,
    CMD_FONT_BASE,
    CMD_FONT_RESET,
    CMD_MODE_AVERAGE,
    CMD_MODE_COST,
    CMD_MODE_CURRENT,
    CMD_MODE_SESSION,
    CMD_OPEN_CONFIG,
    CMD_PIN_TASKBAR,
    CMD_POS_RESET,
    CMD_QUIT,
    CMD_RESET,
    CMD_SIZE_BASE,
    CMD_STATS,
    CMD_THEME_BASE,
    CMD_TOGGLE_AUTOSTART,
    CMD_TOGGLE_LOCK,
    CMD_TOGGLE_PANEL,
    CMD_TOGGLE_STRIP,
    CMD_VIEW_DAY_BASE,
    CMD_VIEW_SESSION_BASE,
    MENU_RECENT,
    MenuBuilder,
    TrayIcon,
)
from .w32 import (
    ERROR_ALREADY_EXISTS,
    IDYES,
    MB_ICONINFORMATION,
    MB_ICONQUESTION,
    MB_ICONWARNING,
    MB_OK,
    MB_YESNO,
    WM_ENDSESSION,
    WM_PIN_READY,
    WM_QUERYENDSESSION,
    WM_TIMER,
    WM_TRAY_READDED,
    gdi32,
    kernel32,
    user32,
    wintypes,
)

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_REG_NAME = "PowerMonitor"
_MUTEX_NAME = "PowerMonitorTrayWnd.SingleInstance"
_TIMER_ID = 1
_REFRESH_MS = 1000

# 托盘注册失败后先补偿多久再判定「系统真的没有通知区域」。
# 登录 / 开机时 Shell_NotifyIcon 会瞬时失败（任务栏还没准备好），
# 这个宽限期内不打扰用户。加上 create() 自身的重试，最坏约 18 秒。
_TRAY_GRACE_SECONDS = 10.0

_MODE_BY_CMD = {
    CMD_MODE_CURRENT: "current",
    CMD_MODE_COST: "cost",
    CMD_MODE_SESSION: "session",
    CMD_MODE_AVERAGE: "average",
}


# --------------------------------------------------------------------------- 开机自启


def _launch_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    python_exe = Path(sys.executable)
    pythonw = python_exe.with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else python_exe
    return f'"{exe}" "{app_dir() / "run.pyw"}"'


def autostart_enabled() -> bool:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, _REG_NAME)
            return bool(value)
    except (OSError, ImportError):
        return False


def set_autostart(enabled: bool) -> bool:
    """写入 / 删除 HKCU\\...\\Run 项。返回是否成功。"""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            if enabled:
                winreg.SetValueEx(key, _REG_NAME, 0, winreg.REG_SZ, _launch_command())
            else:
                try:
                    winreg.DeleteValue(key, _REG_NAME)
                except FileNotFoundError:
                    pass
        return True
    except (OSError, ImportError):
        return False


# --------------------------------------------------------------------------- 常驻任务栏

_PIN_KEY = r"Software\PowerMonitor"


def _dbg(msg: str) -> None:
    debug.log("app", msg)


def _kwh(wh: float) -> str:
    """菜单里那种一行放得下的电量写法：980 Wh / 3.41 kWh。

    菜单项没有第二行可用，所以口径和统计窗口的 ``_energy()`` 保持一致，
    只是不写「Wh」的十进小数尾巴（0.98 kWh 比 980.0 Wh 读得快）。
    """
    if abs(wh) >= 1000.0:
        return f"{wh / 1000.0:.2f} kWh"
    return f"{wh:.0f} Wh"



def _exe_path() -> str:
    """当前程序的「身份」路径——托盘条目就是按它登记的。"""
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve())
    return str(Path(app_dir()).resolve())


def autopin_recorded() -> str:
    """上次自动开启「常驻任务栏」时的程序路径；没做过返回空串。

    用路径判定，而不是「配置文件是否存在」：换了安装位置（升级、或者从
    便携版转成安装版）时 exe 路径变了，托盘条目必须重新登记一次。
    """
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _PIN_KEY) as key:
            return str(winreg.QueryValueEx(key, "AutoPinnedExe")[0])
    except (OSError, ImportError):
        return ""


def record_autopin(path: str) -> None:
    """记下「已经为这个路径自动登记过」，避免重复打扰用户。"""
    try:
        import winreg

        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _PIN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, "AutoPinnedExe", 0, winreg.REG_SZ, path)
        _dbg(f"record_autopin OK path={path}")
    except (OSError, ImportError) as exc:
        _dbg(f"record_autopin FAIL path={path} err={exc!r}")


# --------------------------------------------------------------------------- 主应用


class PowerMonitorApp:
    """托盘常驻应用。所有窗口操作都在主线程，采样在后台线程。"""

    def __init__(self, first_run: bool = False) -> None:
        self.cfg = Config.load()
        self.meter = EnergyMeter(self.cfg)
        self.panel = Panel(self.cfg)
        self.strip = TaskbarStrip(self.cfg)
        self.fee = FeeSettingsDialog(self.cfg, self._after_fee_saved)
        # 用量统计窗口：按天 / 按月 / 按年 / 按每次开机 / 按时段五个维度。
        # 跟电价窗口一样是「平时不建、点了才建」的独立窗口。
        self.stats = StatsWindow(self.meter, self.cfg)
        self.tray = TrayIcon(
            self._build_menu, self._on_command, self._on_extra_message
        )
        # 长条默认能拖 → 它不再穿透点击 → 会把长条那一片任务栏的右键菜单吃掉。
        # 所以让它右键时回调到托盘，把同一套菜单在原地弹出来（等于把「被吃掉」
        # 的那次右键还回去，还多给了长条自己的设置入口）。
        self.strip.menu_callback = self.tray.popup
        self._icon_key: tuple[str, int] | None = None
        self._timer_id = 0
        self._first_run = first_run
        self._pin_prompted = False
        self._pin_started = False
        self._tray_missing_since: float | None = None
        self._degraded = False
        self._strip_tried = False

    # ------------------------------------------------------------- 菜单

    # 四个「选项类」子菜单的锚点。两件事共用同一套名字：
    #   · _build_menu(anchor) 只返回这一级（点完接着弹用，见 tray.TrayIcon._popup）
    #   · _apply_strip_option(cmd) 返回该命令所属的锚点
    STRIP_MENUS = ("theme", "font", "size", "field", "position")
    # 「继续弹」的另一个锚点：整张根菜单。给顶层那几个勾选式开关用 ——
    # 用户点一个勾、还想点下一个时，菜单不该关掉。
    ROOT_MENU = "root"
    # 顶层勾选式开关：点完菜单留在原地（普通动作命令仍然照常关闭）。
    PERSIST_COMMANDS = (CMD_TOGGLE_AUTOSTART, CMD_PIN_TASKBAR, CMD_TOGGLE_STRIP)

    def _menu_theme(self) -> "MenuBuilder":
        menu = MenuBuilder.submenu()
        active = stripopts.theme(self.cfg)
        for i, (key, label, _hint) in enumerate(stripopts.THEMES):
            menu.item(CMD_THEME_BASE + i, label, key == active, radio=True)
        return menu

    def _menu_font(self) -> "MenuBuilder":
        """长条字号：六档快捷取值 + 复位。

        档位只是快捷键 —— 拖长条上下边缘能把字号调成**任意连续值**
        （0.60~1.60），所以菜单里如果当前值不在六档上，把它显示出来
        （不然用户看着六个都没勾，以为坏了），并给一键复位。
        """
        menu = MenuBuilder.submenu()
        active = stripopts.font_scale(self.cfg)
        in_scale = any(abs(value - active) < 1e-6
                       for value, _label in stripopts.FONT_SCALES)
        for i, (value, label) in enumerate(stripopts.FONT_SCALES):
            menu.item(CMD_FONT_BASE + i, label, abs(value - active) < 1e-6,
                      radio=True)
        menu.separator()
        if not in_scale:
            menu.label("当前字号（拖长条边缘微调）", f"{active:.2f}")
        menu.item(CMD_FONT_RESET, "复位标准字号（1.00）")
        return menu

    def _menu_size(self) -> "MenuBuilder":
        menu = MenuBuilder.submenu()
        active = stripopts.size_key(self.cfg)
        for i, (key, label, _ratio, _pad) in enumerate(stripopts.SIZES):
            menu.item(CMD_SIZE_BASE + i, label, key == active, radio=True)
        return menu

    def _menu_field(self) -> "MenuBuilder":
        menu = MenuBuilder.submenu()
        shown = stripopts.enabled_fields(self.cfg)
        for i, (key, label) in enumerate(stripopts.FIELDS):
            is_on = key in shown
            # 只剩一项时把它灰掉：长条总得显示点什么，最后一项不允许取消。
            # 直接灰掉比「点了弹框说不行」清爽得多。
            menu.item(CMD_FIELD_BASE + i, label, is_on,
                      enabled=not (is_on and len(shown) <= 1))
        return menu

    def _menu_position(self) -> "MenuBuilder":
        """长条位置：锁定开关 + 位置复位。

        长条默认**能拖**（整块胶囊就是拖动面，鼠标指上去描边会转成强调色），
        代价是它会吃掉那一片任务栏的点击。想要「看得见但完全摸不着」就勾上
        「锁定位置」——那时长条加回穿透点击，鼠标直接穿过去。
        """
        menu = MenuBuilder.submenu()
        menu.item(CMD_TOGGLE_LOCK, "锁定位置（穿透点击，不可拖动）",
                  stripopts.locked(self.cfg))
        menu.item(CMD_POS_RESET, "位置复位（回到开始按钮左边）")
        return menu

    # ---- 「查看某一天 / 某一次开机」：把最近这些条目直接摆到菜单上 ----

    def _menu_view_day(self) -> "MenuBuilder":
        """最近若干天，一天一项，带上那天的电量与电费。

        用户要的就是这个：不用先打开窗口再自己找，菜单里点一下就是那天。
        """
        menu = MenuBuilder.submenu()
        rows = self._recent_rows("day")
        if not rows:
            menu.item(CMD_VIEW_DAY_BASE, "（还没有记录）", False, enabled=False)
            return menu
        cur = self.cfg.currency
        for i, row in enumerate(rows):
            label = (f"{row.get('when', '')}    {_kwh(row.get('wh', 0.0))}"
                     f" · {cur}{row.get('cost', 0.0):.2f}")
            menu.item(CMD_VIEW_DAY_BASE + i, label)
        return menu

    def _menu_view_session(self) -> "MenuBuilder":
        """最近若干次开机，一次一项。当前这一次单独标出来。

        键是开机时刻，所以「每次开机」确实是每一次，不是只当前这次。
        """
        menu = MenuBuilder.submenu()
        rows = self._recent_rows("session")
        if not rows:
            menu.item(CMD_VIEW_SESSION_BASE, "（还没有记录）", False, enabled=False)
            return menu
        cur = self.cfg.currency
        boot_now = self.meter.snapshot().power_on_ts
        for i, row in enumerate(rows):
            mark = ("（本次）"
                    if abs(float(row.get("boot", 0)) - boot_now) < 30 else "")
            label = (f"{row.get('when', '')}{mark}    {_kwh(row.get('wh', 0.0))}"
                     f" · {cur}{row.get('cost', 0.0):.2f}")
            menu.item(CMD_VIEW_SESSION_BASE + i, label)
        return menu

    def _recent_rows(self, kind: str) -> list[dict]:
        try:
            rows = self.meter.stats_rows(kind, MENU_RECENT)
        except Exception:  # noqa: BLE001 - 菜单构建绝不能因为统计失败而炸
            return []
        return list(rows)[:MENU_RECENT]

    def _view_entry(self, cmd: int) -> bool:
        """「查看某一天 / 某一次开机」的命令号 → 打开统计窗口并定位到那一条。"""
        if CMD_VIEW_DAY_BASE <= cmd < CMD_VIEW_DAY_BASE + MENU_RECENT:
            rows = self._recent_rows("day")
            anchor, kind = CMD_VIEW_DAY_BASE, "day"
        elif (CMD_VIEW_SESSION_BASE <= cmd
              < CMD_VIEW_SESSION_BASE + MENU_RECENT):
            rows = self._recent_rows("session")
            anchor, kind = CMD_VIEW_SESSION_BASE, "session"
        else:
            return False
        index = cmd - anchor
        if index >= len(rows):
            return True          # 菜单是上一次拼的，数据变了 —— 当无事发生
        self.stats.show(kind, str(rows[index].get("key", "")))
        return True

    def _build_menu(self, anchor: str | None = None):
        """构建托盘菜单。

        ``anchor`` 命中 ``STRIP_MENUS`` 时**只返回那一级子菜单**：用户勾完一个
        显示字段后，在同一位置立刻再弹出同一级，可以连着勾 —— 否则每勾一项
        菜单就关掉，得回托盘重新点右键（用户投诉的就是这个）。

        命中 ``ROOT_MENU`` 或 ``None`` 时建的是整张菜单（前者用于顶层勾选式
        开关点完继续弹，后者是常规右键）。
        """
        if anchor in self.STRIP_MENUS:
            return getattr(self, f"_menu_{anchor}")().handle

        snap = self.meter.snapshot()
        cur = self.cfg.currency
        menu = MenuBuilder()
        menu.label("本次开机", fmt_duration(snap.power_on_seconds))
        menu.label(
            "已统计能耗",
            f"{snap.session_wh:.0f} Wh · {cur}{snap.session_cost:.3f}",
        )
        menu.separator()

        # 总统计：用户要「后面能看总统计」，就放在右键一下就能看到的位置，
        # 不必先打开面板。（累计 = 开这个程序以来所有天的总和）
        menu.label("今日", f"{snap.today_wh / 1000:.2f} kWh · {cur}{snap.today_cost:.2f}")
        menu.label(
            "本月",
            f"{snap.month_wh / 1000:.1f} kWh · {cur}{snap.month_cost:.2f}",
        )
        menu.label(
            "累计",
            f"{snap.total_wh / 1000:.1f} kWh · {cur}{snap.total_cost:.2f}",
        )
        menu.label("已记录", f"{snap.total_days} 天 · 开机 {snap.total_sessions} 次")
        menu.separator()

        menu.item(CMD_TOGGLE_PANEL, "打开详情面板")
        menu.separator()

        display = MenuBuilder.submenu()
        display.item(
            CMD_MODE_CURRENT, "当前功率 (W)", self.cfg.tray_display == "current"
        )
        display.item(
            CMD_MODE_COST,
            f"已用电费 ({cur})",
            self.cfg.tray_display == "cost",
        )
        display.item(
            CMD_MODE_SESSION, "已统计能耗 (Wh)", self.cfg.tray_display == "session"
        )
        display.item(
            CMD_MODE_AVERAGE, "平均功率 (W)", self.cfg.tray_display == "average"
        )
        menu.attach("图标显示", display)
        menu.separator()

        menu.item(CMD_TOGGLE_AUTOSTART, "开机自启动", autostart_enabled())
        menu.item(CMD_PIN_TASKBAR, "常驻任务栏（^ 左侧）", trayreg.is_pinned())
        menu.item(CMD_TOGGLE_STRIP, "任务栏长条读数", self.cfg.strip_enabled)

        # 长条外观 / 内容：都做成根菜单下的**二级**子菜单（右键图标点两下就到），
        # 不往三级里塞 —— 用户的原话是「角标点开后在二级菜单里调」，层级一深就没人找了。
        # 这四项点完会**继续留在原地**，方便连着调。
        menu.attach("长条质感", self._menu_theme())
        menu.attach("长条字号", self._menu_font())
        menu.attach("长条大小", self._menu_size())
        menu.attach("长条显示内容", self._menu_field())
        menu.attach("长条位置", self._menu_position())
        menu.separator()
        # 统计放在电价设置上面：用户是「看用量」来的，翻账本比改电价常用得多。
        # 「查看某一天 / 某次开机」是二级子菜单，把最近十条直接摆出来 ——
        # 用户说「菜单里可以自主选择某一天去看」，指的就是这个：点一下就是那天，
        # 不用先打开窗口再自己滚着找。
        menu.attach("查看某一天", self._menu_view_day())
        menu.attach("查看某次开机", self._menu_view_session())
        menu.item(CMD_STATS, "用量统计（全部明细）…")
        menu.item(CMD_FEE_SETTINGS, "电价设置…")
        menu.item(CMD_OPEN_CONFIG, "打开配置文件")
        menu.separator()
        menu.item(CMD_RESET, "重置本次统计")
        menu.item(CMD_ABOUT, f"关于 {APP_NAME} v{__version__}")
        menu.separator()
        menu.item(CMD_QUIT, "退出")
        return menu.handle

    # ------------------------------------------------------------- 命令

    def _on_command(self, cmd: int) -> str | None:
        """处理菜单命令。

        返回值是**下一个要继续弹出的菜单锚点**（``None`` = 菜单照常关闭）：
        子菜单类命令返回 ``"theme"`` / ``"font"`` / ``"size"`` / ``"field"``，
        顶层勾选式开关返回 ``"root"``（整张根菜单）。托盘那边拿它实现
        「勾完一项菜单不消失」。
        """
        try:
            # 长条外观 / 内容类命令号落在各自的区间里，先让它们吃掉
            anchor = self._apply_strip_option(cmd)
            if anchor is not None:
                return anchor
            # 「查看某一天 / 某一次开机」也是区间命令，同样先吃掉
            if self._view_entry(cmd):
                return None
            if cmd == CMD_TOGGLE_LOCK:
                self._toggle_lock()
                return "position"
            if cmd == CMD_POS_RESET:
                self._reset_strip_position()
                return None
            if cmd == CMD_FONT_RESET:
                # 拖边缘把字号调飞了一键拉回 1.00；菜单留在那级方便再选
                self._set_strip_option("font", 1.0)
                return "font"
            if cmd == CMD_TOGGLE_PANEL:
                self._toggle_panel()
            elif cmd in _MODE_BY_CMD:
                self._set_mode(_MODE_BY_CMD[cmd])
            elif cmd in self.PERSIST_COMMANDS:
                # 勾选式开关：改完把整张菜单原地再弹一次，方便连着勾好几项。
                if cmd == CMD_TOGGLE_AUTOSTART:
                    self._toggle_autostart()
                elif cmd == CMD_PIN_TASKBAR:
                    self._toggle_pin()
                else:
                    self._toggle_strip()
                return self.ROOT_MENU
            elif cmd == CMD_STATS:
                self._open_stats()
            elif cmd == CMD_FEE_SETTINGS:
                self._open_fee_settings()
            elif cmd == CMD_OPEN_CONFIG:
                self._open_config()
            elif cmd == CMD_RESET:
                self._reset()
            elif cmd == CMD_ABOUT:
                self._about()
            elif cmd == CMD_QUIT:
                self.quit()
        except Exception:  # noqa: BLE001 - 菜单动作失败不能拖垮消息循环
            pass
        return None

    def _apply_strip_option(self, cmd: int) -> str | None:
        """处理「长条质感 / 字号 / 大小 / 显示内容」的命令号。

        这几档的命令号是「基址 + 目录下标」的连续区间（见 tray.CMD_*_BASE），
        所以不用为每一档写一个分支。返回该命令所属子菜单的锚点（用于点完继续
        弹同一级），不是这类命令就返回 None，让调用方继续往下判。
        """
        pick = None
        anchor = None
        if CMD_THEME_BASE <= cmd < CMD_THEME_BASE + len(stripopts.THEMES):
            pick = ("theme", stripopts.THEMES[cmd - CMD_THEME_BASE][0])
            anchor = "theme"
        elif CMD_FONT_BASE <= cmd < CMD_FONT_BASE + len(stripopts.FONT_SCALES):
            pick = ("font", stripopts.FONT_SCALES[cmd - CMD_FONT_BASE][0])
            anchor = "font"
        elif CMD_SIZE_BASE <= cmd < CMD_SIZE_BASE + len(stripopts.SIZES):
            pick = ("size", stripopts.SIZES[cmd - CMD_SIZE_BASE][0])
            anchor = "size"
        elif CMD_FIELD_BASE <= cmd < CMD_FIELD_BASE + len(stripopts.FIELDS):
            pick = ("field", stripopts.FIELDS[cmd - CMD_FIELD_BASE][0])
            anchor = "field"
        if pick is None:
            return None
        self._set_strip_option(*pick)
        return anchor

    def _set_strip_option(self, kind: str, value) -> None:
        """改长条外观 / 显示内容：存盘 + 立刻重画。

        不重建窗口 —— ``_content_key`` 里已经带了质感 / 字号 / 大小 / 字段，配置一变
        键就变，下一帧 ``_render_if_needed`` 自然会重画。字号或大小引起高度变化时，
        ``tick`` 还会顺手把窗口挪到按新高度算出来的位置。
        """
        if kind == "theme":
            if self.cfg.strip_theme == value:
                return
            self.cfg.strip_theme = value
        elif kind == "font":
            if abs(stripopts.font_scale(self.cfg) - value) < 1e-6:
                return
            self.cfg.strip_font_scale = value
        elif kind == "size":
            if self.cfg.strip_size == value:
                return
            self.cfg.strip_size = value
        elif kind == "field":
            # 最后一项不允许取消：toggle_field 会拒绝并返回 False（菜单里也已灰掉）
            if not stripopts.toggle_field(self.cfg, value):
                return
        else:
            return
        self.cfg.save()
        self.strip.invalidate()
        self._ensure_strip()

    def _toggle_lock(self) -> None:
        """锁定 / 解锁长条位置。

        **不锁**（默认）= 长条能拖：鼠标指上去描边转强调色、光标变 ↔、按住横拖、
        双击复位。**锁上** = 加回 ``WS_EX_TRANSPARENT``，长条变回完全穿透点击的
        纯显示窗口（长条盖住的那片任务栏照样能点）。
        """
        target = not stripopts.locked(self.cfg)
        self.cfg.strip_locked = target
        self.cfg.save()
        self.strip.set_interactive(not target)

    def _reset_strip_position(self) -> None:
        """把长条拖回默认位置（开始按钮左边）。"""
        self.strip.reset_offset()
        self.strip.tick(self.meter.snapshot())

    # ------------------------------------------------------------- 消息

    def _on_extra_message(self, msg, wparam, lparam):
        if msg == WM_PIN_READY:
            self._on_pin_ready(bool(wparam))
            return True, 0
        if msg == WM_TRAY_READDED:
            # explorer 重启后图标被清空，托盘模块已重新注册。此时顺手补一次
            # 常驻登记：若之前因为托盘没起来而没做成，这次补上。
            _dbg(f"explorer 重启后托盘重新注册 ok={bool(wparam)}")
            if wparam:
                self._maybe_pin_taskbar()
            return True, 0
        return False, 0

    # ------------------------------------------------------------- 面板

    def _toggle_panel(self) -> None:
        if self.panel.is_visible:
            self.panel.hide()
            return
        if not self.panel._hwnd:
            self.panel.create()
        self.panel.set_data(self.meter.snapshot(), self.meter.curve())
        self.panel.show_panel()

    # ------------------------------------------------------------- 动作

    def _set_mode(self, value: str) -> None:
        self.cfg.tray_display = value
        self.cfg.save()
        self._icon_key = None  # 强制重绘
        self._tick()

    def _toggle_autostart(self) -> None:
        target = not autostart_enabled()
        if not set_autostart(target):
            user32.MessageBoxW(
                self.tray.hwnd, "写入开机自启项失败，请检查权限。",
                APP_NAME, MB_OK | MB_ICONWARNING,
            )
        self._icon_key = None

    def _ensure_strip(self) -> None:
        """按配置建 / 拆任务栏长条。放不上（没有任务栏）就静默跳过。"""
        if self.cfg.strip_enabled:
            if not self.strip.hwnd:
                if not self.strip.create(self.meter.snapshot()):
                    _dbg("长条创建失败：没有可用的任务栏")
                    return
                _dbg(f"长条已创建 hwnd={self.strip.hwnd}")
            self.strip.tick(self.meter.snapshot())
            return
        if self.strip.hwnd:
            self.strip.destroy()
            _dbg("长条已关闭")

    def _toggle_strip(self) -> None:
        self.cfg.strip_enabled = not self.cfg.strip_enabled
        self.cfg.save()
        self._ensure_strip()
        if self.cfg.strip_enabled and not self.strip.hwnd:
            user32.MessageBoxW(
                self.tray.hwnd,
                "现在没找到能落脚的任务栏，长条放不上。\n"
                "任务栏设成「自动隐藏」或系统精简掉任务栏时会这样。",
                APP_NAME, MB_OK | MB_ICONINFORMATION,
            )

    def _toggle_pin(self) -> None:
        want = not trayreg.is_pinned()
        if not trayreg.set_pinned(want):
            user32.MessageBoxW(
                self.tray.hwnd,
                "还没能在系统里找到本程序的托盘条目。\n"
                "图标刚出现时系统还没登记，稍等几秒再试一次。",
                APP_NAME, MB_OK | MB_ICONINFORMATION,
            )
            return
        if not want:
            self._ask_restart(
                "已取消「常驻任务栏」，下次资源管理器重启后图标会回到 ^ 里。"
            )
            return
        self._ask_restart(
            "已把图标设为「常驻任务栏」，会显示在 ^ 箭头左侧。"
        )

    def _ask_restart(self, headline: str) -> None:
        answer = user32.MessageBoxW(
            self.tray.hwnd,
            f"{headline}\n\n"
            "Windows 需要重启资源管理器才会生效：\n"
            "任务栏会闪一下，已打开的文件夹窗口会被关闭。\n\n现在重启吗？",
            APP_NAME, MB_YESNO | MB_ICONQUESTION,
        )
        if answer == IDYES:
            if not trayreg.restart_explorer():
                # 绝不能让用户卡在「没有任务栏」的状态里还不明所以。
                user32.MessageBoxW(
                    self.tray.hwnd,
                    "资源管理器没能正常重启，任务栏可能没有恢复。\n\n"
                    "找回方法：按 Ctrl+Shift+Esc 打开任务管理器 →\n"
                    "「文件」→「运行新任务」→ 输入 explorer.exe → 回车。",
                    APP_NAME, MB_OK | MB_ICONWARNING,
                )

    def _open_fee_settings(self) -> None:
        """打开电价设置窗口（省份预设 + 手工修改）。"""
        self.fee.show()

    def _open_stats(self) -> None:
        """打开用量统计窗口（按天 / 按月 / 按年 / 按每次开机 / 按时段）。"""
        # 电价或币种可能刚在电价窗口改过，窗口里要显示的是当前值
        self.stats.cfg = self.cfg
        self.stats.show()

    def _after_fee_saved(self) -> None:
        """电价一改，图标 / 提示 / 面板上的电费立刻跟着变。"""
        self._icon_key = None  # 强制重绘（图标上可能带着费用）
        self.panel.refresh()
        if self.panel._hwnd and not self.panel.is_visible:
            self.panel.refresh()
        self._tick()

    def _open_config(self) -> None:
        try:
            os.startfile(CONFIG_PATH)  # noqa: S606 - 交给系统默认程序
        except OSError:
            pass

    def _reset(self) -> None:
        self.meter.reset_session()
        self._icon_key = None
        self._tick()

    def _about(self) -> None:
        snap = self.meter.snapshot()
        gpu = snap.gpu_names[0] if snap.gpu_names else "未检测到可读功耗的显卡"
        text = "\n".join(
            [
                f"{APP_NAME} v{__version__}",
                "",
                "常驻通知区域，统计本次开机以来的整机能耗。",
                "",
                f"开机时刻：{time.strftime('%Y-%m-%d %H:%M', time.localtime(snap.power_on_ts))}"
                f"（{snap.power_on_source}）",
                f"GPU：{gpu}",
                f"CPU：{snap.cpu_source}（估算）",
                f"其他：主板 / 内存 / 存储 / 风扇 按常量 "
                f"{self.cfg.baseline_watts:.0f} W 计",
                *([f"校准：整机 ×{self.cfg.calibration:.2f}（对齐插座口径）"]
                  if abs(self.cfg.calibration - 1.0) > 1e-9 else []),
                f"电价：{self.cfg.tariff_region} · {self.cfg.tariff_plan}",
                f"      峰段 {self.cfg.price_peak:.4f} · 平段 {self.cfg.price_flat:.4f} · "
                f"谷段 {self.cfg.price_valley_dry:.4f} 元/度",
                f"      数据来源：{self.cfg.tariff_source}",
                "      各地电价不一样，可以右键托盘图标 →「电价设置…」改；",
                "      功耗模型（其他功耗 / 显示器 / 校准系数）也在同一个窗口里。",
                "",
                f"配置文件：{CONFIG_PATH}",
            ]
        )
        user32.MessageBoxW(
            self.tray.hwnd, text, f"关于 {APP_NAME}", MB_OK | MB_ICONINFORMATION
        )

    # ------------------------------------------------------------- 常驻任务栏

    def _maybe_pin_taskbar(self) -> None:
        """把图标设成常驻任务栏（^ 左侧）。

        注册表条目由 explorer 在图标首次出现时创建，所以只能等一会儿再写，
        放到后台线程去做，别卡住消息循环。

        每个程序路径只自动做一次；换了安装位置会再做一次——托盘条目是按
        exe 路径登记的，路径变了旧登记就失效了。之后是否常驻交给菜单开关，
        免得用户手动取消后又被程序改回来。
        """
        if self._pin_started:
            _dbg("pin: 已在处理中，跳过")
            return
        exe = _exe_path()
        recorded = autopin_recorded()
        _dbg(f"pin: 进入 exe={exe} 已记录={recorded or '（无）'} pinned={trayreg.is_pinned()}")
        if recorded == exe:
            _dbg("pin: 本路径已登记过，跳过")
            return
        if trayreg.is_pinned():          # 已经是常驻状态，记一笔就行
            _dbg("pin: 已是常驻态，直接记账")
            record_autopin(exe)
            return
        hwnd = self.tray.hwnd
        if not hwnd:
            _dbg("pin: 没有托盘窗口句柄，放弃")
            return
        self._pin_started = True

        def worker() -> None:
            ok = False
            try:
                ok = trayreg.pin_when_ready()
            except Exception as exc:  # noqa: BLE001
                _dbg(f"pin: pin_when_ready 异常 {exc!r}")
                ok = False
            _dbg(f"pin: 后台写入结果 ok={ok}")
            if ok:
                # 只有真的写成功才记账。失败不记 —— 重启电脑后 explorer 会
                # 重新把条目建出来，那时再自动补一次。
                record_autopin(exe)
            else:
                # 失败就解锁，让 explorer 重启等后续时机还能再补一次。
                self._pin_started = False
            try:
                user32.PostMessageW(hwnd, WM_PIN_READY, 1 if ok else 0, 0)
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(
            target=worker, name="pin-taskbar", daemon=True
        ).start()

    def _on_pin_ready(self, ok: bool) -> None:
        # 即便注册表里 IsPromoted 早就是 1，explorer 也可能还没读到，
        # 图标照样藏在 ^ 里——所以登记之后照样问一次要不要重启资源管理器。
        if not ok or self._pin_prompted:
            return
        self._pin_prompted = True
        self._ask_restart("已把图标登记为「常驻任务栏」，会显示在 ^ 箭头左侧。")

    # ------------------------------------------------------------- 刷新

    def _tick(self) -> None:
        try:
            if not self.tray.added:
                self._recover_tray()
            snap = self.meter.snapshot()
            text, ratio = tray_label(snap, self.cfg.tray_display, self.cfg)
            key = (text, round(ratio, 2))
            if key != self._icon_key and self.tray.hwnd:
                hicon = make_icon(text, ratio)
                if hicon:
                    self.tray.set_icon(hicon)
                    self._icon_key = key
            if self.tray.hwnd:
                self.tray.set_tooltip(self._tooltip(snap))
            if self.panel.is_visible:
                self.panel.set_data(snap, self.meter.curve())
            self.strip.tick(snap)
        except Exception:  # noqa: BLE001 - 单次刷新失败不应打断消息循环
            pass

    def _recover_tray(self) -> None:
        """托盘还没注册成功时的补偿。

        登录 / 开机时 Shell_NotifyIcon 会瞬时失败，这里逐秒重试；成功就
        补做「常驻任务栏」登记。超过宽限期仍失败，才判定系统真的没有
        通知区域，退化成普通窗口（关窗即退出），否则用户没有任何入口。
        """
        if self.tray.ensure_added():
            _dbg("补偿成功：托盘已注册")
            self._tray_missing_since = None
            if self._first_run:
                self.panel.set_data(self.meter.snapshot(), self.meter.curve())
                self.panel.show_panel()
            self._maybe_pin_taskbar()
            self._ensure_strip()
            return

        since = self._tray_missing_since
        if since is None:
            self._tray_missing_since = time.time()
            return
        if self._degraded or time.time() - since < _TRAY_GRACE_SECONDS:
            return

        self._degraded = True
        _dbg("降级：宽限期内始终没有通知区域")
        self.panel.quit_on_close = True
        self.panel.set_data(self.meter.snapshot(), self.meter.curve())
        self.panel.show_panel()
        user32.MessageBoxW(
            None,
            "系统没有可用的通知区域，程序将以普通窗口形式运行，关窗即退出。",
            APP_NAME, MB_OK | MB_ICONWARNING,
        )

    def _tooltip(self, snap: Snapshot) -> str:
        gpu = f"{snap.gpu_w:.0f}" if snap.gpu_measured else "--"
        return (
            "\n".join(
                [
                    f"本次开机 {fmt_duration(snap.power_on_seconds)} · "
                    f"已统计 {snap.session_wh:.0f} Wh",
                    f"当前 {snap.current_w:.0f} W = CPU {snap.cpu_w:.0f} + "
                    f"GPU {gpu} + 其他 {snap.base_w:.0f}",
                    f"均值 {snap.average_w:.0f} W · 今日 "
                    f"{snap.today_wh / 1000:.2f} kWh {self.cfg.currency}"
                    f"{snap.today_cost:.2f}",
                ]
            )
        )[:127]

    # ------------------------------------------------------------- 生命周期

    def run(self) -> int:
        self.meter.start()

        snap = self.meter.snapshot()
        text, ratio = tray_label(snap, self.cfg.tray_display, self.cfg)
        created = self.tray.create(make_icon(text, ratio), self._tooltip(snap))
        self._icon_key = (text, round(ratio, 2))

        self.panel.create()
        self._ensure_strip()

        _dbg(
            f"run: 启动 frozen={getattr(sys, 'frozen', False)} "
            f"sys.executable={sys.executable} created={created} "
            f"first_run={self._first_run} hwnd={self.tray.hwnd}"
        )

        if created:
            if self._first_run:
                self.panel.set_data(snap, self.meter.curve())
                self.panel.show_panel()
            self._maybe_pin_taskbar()
        else:
            # 先别急着判定「没有通知区域」：登录 / 开机时 Shell_NotifyIcon
            # 会瞬时失败，交给定时器持续补偿，宽限期内不打扰用户。
            self._tray_missing_since = time.time()
            _dbg("run: 托盘注册未成功，转入定时补偿")

        self._timer_id = user32.SetTimer(None, _TIMER_ID, _REFRESH_MS, None)
        self._tick()

        try:
            return self._loop()
        finally:
            self._shutdown()

    def _loop(self) -> int:
        msg = wintypes.MSG()
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret == 0:  # WM_QUIT
                return int(msg.wParam)
            if ret == -1:  # 出错
                return 1
            # 电价设置窗口用的是系统控件，Tab 轮转 / 回车 / Esc 得先让
            # IsDialogMessage 处理（它只认自己那个窗口的消息，别的窗口会返回 0）
            if self.fee.hwnd and user32.IsDialogMessageW(self.fee.hwnd, ctypes.byref(msg)):
                continue
            # 线程定时器（hWnd 为空）没有窗口过程，得在这儿接住
            if not msg.hWnd and msg.message == WM_TIMER:
                self._tick()
                continue
            # 关机 / 注销：这是账本最后的机会，先落盘再让系统继续
            # （消息照常派发下去，窗口过程会回 TRUE 表示同意关机）
            if msg.message in (WM_QUERYENDSESSION, WM_ENDSESSION):
                try:
                    self.meter.save_now()
                except Exception:  # noqa: BLE001
                    pass
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _shutdown(self) -> None:
        if self._timer_id:
            user32.KillTimer(None, self._timer_id)
            self._timer_id = 0
        try:
            self.meter.stop()
        finally:
            self.fee.destroy()
            self.stats.destroy()
            self.tray.destroy()
            self.panel.destroy()
            self.strip.destroy()

    def quit(self) -> None:
        if self._timer_id:
            user32.KillTimer(None, self._timer_id)
            self._timer_id = 0
        self.fee.destroy()
        self.stats.destroy()
        self.tray.destroy()
        self.strip.destroy()
        user32.PostQuitMessage(0)


# --------------------------------------------------------------------------- 入口


def enable_dpi_awareness() -> None:
    """让自绘窗口拿到物理像素，避免高 DPI 下字发虚。"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()

    enable_dpi_awareness()

    mutex = kernel32.CreateMutexW(None, False, _MUTEX_NAME)
    if mutex and ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        user32.MessageBoxW(
            None,
            f"{APP_NAME} 已经在运行了，请看任务栏右下角（^ 里）。",
            APP_NAME, MB_OK | MB_ICONINFORMATION,
        )
        kernel32.CloseHandle(mutex)
        return 0

    first_run = not CONFIG_PATH.exists()
    try:
        return PowerMonitorApp(first_run=first_run).run()
    finally:
        if mutex:
            kernel32.CloseHandle(mutex)


# --------------------------------------------------------------------------- 自检


def self_test() -> int:
    """打包后自检：运行 ``能耗统计.exe --self-test``。

    冻结成单文件之后就再也跑不了测试脚本了，只能让程序自己报。检查的重点是
    「模块有没有真的打进包里、电价窗口能不能建出来」——这正是 PyInstaller
    最容易静默丢东西的地方。

    结果写到程序目录的 ``selftest.txt``：exe 是无控制台的窗口程序，
    往 stdout 打印没人看得见。
    """
    import tempfile

    from . import config as config_mod
    from . import tariffs
    from .fee_dialog import FeeSettingsDialog

    lines: list[str] = []
    counts = {"ok": 0, "bad": 0}

    def check(label: str, cond: bool, detail: str = "") -> None:
        counts["ok" if cond else "bad"] += 1
        tail = f"  ({detail})" if detail else ""
        lines.append(f"[{'PASS' if cond else 'FAIL'}] {label}{tail}")

    lines.append(f"{APP_NAME} v{__version__} 自检")
    lines.append(f"frozen={bool(getattr(sys, 'frozen', False))}  python={sys.version.split()[0]}")
    lines.append(f"exe={sys.executable}")
    lines.append("")

    check("内置省份电价预设已打进包", len(tariffs.REGIONS) >= 25, f"{len(tariffs.REGIONS)} 个")
    check("时段解析可用（23-7 跨零点）",
          sorted(tariffs.parse_hours("23-7")) == [0, 1, 2, 3, 4, 5, 6, 23])

    # 图标显示模式是菜单里最容易改坏的地方（常量 ID、字符串、格式化三处要同时对），
    # 冻结成 exe 后跑不了单测，只能让程序自己把四条路都走一遍。
    from .iconmake import cost_text, tray_label

    class _ProbeSnap:
        current_w = 132.0
        session_wh = 428.3
        session_cost = 1.284
        average_w = 118.0
        today_wh = 1240.0
        gpu_limits = [320.0]
        # 累计类字段（长条的「本月 / 累计 / 累计电费」用得到）
        month_wh = 12300.0
        total_wh = 156700.0
        total_cost = 81.9

    class _ProbeCfg:
        cpu_ppt = 142.0
        baseline_watts = 35.0
        currency = "\u00a5"

    probe_modes = {
        mode: tray_label(_ProbeSnap(), mode, _ProbeCfg())[0]
        for mode in ("current", "cost", "session", "average")
    }
    check("四种图标显示模式都能算出文本",
          all(probe_modes.values()), str(probe_modes))
    check("电费模式按量级调精度",
          (cost_text(0.32), cost_text(12.4), cost_text(103.7))
          == ("0.32", "12.4", "103"),
          "/".join(cost_text(v) for v in (0.32, 12.4, 103.7)))
    check("图标文本不超过 4 个字符（放不下会被截断）",
          all(len(text) <= 4 for text in probe_modes.values()),
          str({k: len(v) for k, v in probe_modes.items()}))

    enable_dpi_awareness()

    tmp = Path(tempfile.mkdtemp(prefix="pm-selftest-"))
    config_mod.CONFIG_PATH = tmp / "config.json"
    cfg = config_mod.Config()
    cfg.save()

    # ---- 任务栏长条 ----
    # 这里埋的都是「屏幕上什么都不显示」那一类故障，看代码看不出来：
    #   * 开始按钮是 Shell_TrayWnd 的子窗口，FindWindow 永远找不到（会静默
    #     退化成贴通知区域，位置全错）；
    #   * GDI 往 32bpp DIB 里画会把 alpha 直接清成 0，漏补一块，那一块在
    #     UpdateLayeredWindow 下就是透明空洞。
    from . import taskbar
    from . import strip as strip_mod
    from .roundwin import compose_shape_alpha, dib_section
    from .strip import TaskbarStrip

    strip = TaskbarStrip(cfg)
    check("长条能排出 4 个字段", len(strip._sections(_ProbeSnap())) == 4)

    screen_dc = user32.GetDC(None)
    strip_dc = gdi32.CreateCompatibleDC(screen_dc)
    try:
        probe_h = 47
        width = strip._layout(strip_dc, 1.25, _ProbeSnap(), render=False)[0]
        check("长条宽度算得出来", width > 100, f"{width}px")
        # 画布高度必须等于窗口胶囊高度。折行改造时这里一度写成「行高 × 行数」
        # （单行只有 32.5px，而窗口高 47px），于是底色和描边只画了上面一截，
        # 下面那条从没被填过 —— 叠上半透明就是一条黑带。
        _w0, canvas_h, _p0 = strip._layout(strip_dc, 1.25, _ProbeSnap(),
                                           render=False, height=probe_h)
        check("单行时画布占满胶囊高度（不会画出矮一截的胶囊）",
              canvas_h == probe_h, f"{canvas_h} == {probe_h}")
        bmp, view = dib_section(strip_dc, width, probe_h)
        if bmp:
            old_bmp = gdi32.SelectObject(strip_dc, bmp)
            strip._layout(strip_dc, 1.25, _ProbeSnap(), render=True,
                          origin_x=0, origin_y=0, height=probe_h)
            compose_shape_alpha(view, width, probe_h, margin=0,
                                radius=probe_h / 2.0,
                                shape_w=width, shape_h=probe_h, shadow=0)
            mid = probe_h // 2
            opaque = sum(1 for x in range(width)
                         if view[(mid * width + x) * 4 + 3] == 255)
            check("长条内部 alpha 补满（没有透明空洞）",
                  opaque >= width - 4, f"{opaque}/{width}")
            gdi32.SelectObject(strip_dc, old_bmp)
            gdi32.DeleteObject(bmp)
        else:
            check("长条离屏缓冲能建出来", False, "CreateDIBSection 失败")
    finally:
        gdi32.DeleteDC(strip_dc)
        user32.ReleaseDC(None, screen_dc)

    # ---- 长条外观档位（质感 / 字号 / 大小 / 显示内容）----
    # 这几档最典型的故障是「菜单里有、渲染不认识」和「点完没反应」，所以不能只查
    # 配置值 —— 每一档都得真的离屏画一遍，检查像素。
    base = config_mod.Config()

    def _variant(**over):
        """造一份只改了指定几项的长条配置。Config 是纯 dataclass，直接改属性。"""
        made = config_mod.Config()
        for name, val in over.items():
            setattr(made, name, val)
        return made

    def _hover_palette_differs() -> bool:
        """悬停配色必须「只动描边 / 底色」——顺带守住「别再长出常驻装饰」。

        检查两件事：① 悬停帧和普通帧的配色确实不一样（否则鼠标指上去毫无反应）；
        ② 除了 ``border``/``border_w``/``bg`` 之外**没有别的键变化**，
        也就是说悬停没有顺手改字色之类的东西（那种改动看起来会像画面在闪）。
        """
        made = config_mod.Config()
        probe = TaskbarStrip(made)
        normal = strip_mod._palette_for("dark", None, False)
        hot = strip_mod._apply_hover(normal, True)
        if hot["border"] == normal["border"]:
            return False
        changed = {k for k in normal if hot.get(k) != normal[k]}
        allowed = {"border", "border_w", "bg"}
        return changed <= allowed and probe is not None

    check("长条质感有 6 档", len(stripopts.THEMES) == 6,
          "、".join(stripopts.THEME_KEYS))
    # 6 档 = 常规 5 档 + 折行兜底的「极小」。多这一档是为了让「勾满 15 项」也
    # 排得进去（0.85 那档的纯文本宽度就已经顶到两行的总宽了）。
    check("长条字号有 6 档（含折行兜底的极小）", len(stripopts.FONT_SCALES) == 6)
    check("长条大小有 3 档", len(stripopts.SIZES) == 3)
    # 关键回归：默认档必须和改造前**一模一样**，否则老用户升个级长条就变高度了
    check("默认档高度比例仍是 0.78",
          abs(stripopts.height_ratio(base) - 0.78) < 1e-9,
          f"{stripopts.height_ratio(base):.4f}")
    size_ratios = [stripopts.height_ratio(_variant(strip_size=k))
                   for k in ("slim", "normal", "large")]
    check("长条大小三档高度递增",
          all(a < b for a, b in zip(size_ratios, size_ratios[1:])),
          " < ".join(f"{r:.3f}" for r in size_ratios))
    font_ratios = [stripopts.height_ratio(_variant(strip_font_scale=v))
                   for v, _label in stripopts.FONT_SCALES]
    check("长条字号各档高度递增",
          all(a < b for a, b in zip(font_ratios, font_ratios[1:])),
          " < ".join(f"{r:.3f}" for r in font_ratios))

    solo = _variant(strip_fields=["current"])
    check("最后一项显示内容不允许取消",
          stripopts.toggle_field(solo, "current") is False
          and solo.strip_fields == ["current"])
    order = _variant(strip_fields=["current", "uptime"])
    stripopts.toggle_field(order, "cpu")
    check("新勾的项按目录顺序插回（不甩到末尾）",
          order.strip_fields == ["current", "cpu", "uptime"], str(order.strip_fields))
    bad = _variant(strip_theme="neon", strip_size="huge", strip_font_scale=9.0,
                   strip_fields=["nope"])
    # 字号是连续值（拖边缘能调出任意 0.60~1.60），「夹」就是夹进区间 ——
    # 9.0 夹到上限 1.60。关键是别让一个手改出来的离谱值把长条撑爆。
    check("手改出来的非法配置能夹回合法档",
          stripopts.sanitize(bad) is True and bad.strip_theme == "auto"
          and bad.strip_size == "normal"
          and bad.strip_font_scale == stripopts.FONT_SCALE_MAX
          and bad.strip_fields == list(stripopts.DEFAULT_FIELDS),
          f"{bad.strip_theme}/{bad.strip_size}/{bad.strip_font_scale}/{bad.strip_fields}")
    too_small = _variant(strip_font_scale=0.05)
    check("手改出来的过小字号夹到下限 0.60",
          stripopts.sanitize(too_small) is True
          and too_small.strip_font_scale == stripopts.FONT_SCALE_MIN,
          f"{too_small.strip_font_scale}")
    continuous = _variant(strip_font_scale=1.17)
    check("连续字号（拖边缘调出来的）是合法值、不被夹回档位",
          stripopts.sanitize(continuous) is False
          and abs(continuous.strip_font_scale - 1.17) < 1e-9,
          f"{continuous.strip_font_scale}")

    # ---- 拖动（长条位置）：偏移的合法性与折行表格 ----
    # 拖动量是用户拖出来的，手改 config.json 能写成任何东西；这里保证离谱的值
    # 会被夹住，而不是让长条飞到屏幕外面（用户会以为「长条不见了」）。
    check("不拖时偏移就是 0（默认落点）",
          abs(stripopts.offset_x(config_mod.Config())) < 1e-9)
    check("长条默认不锁（不锁才能拖，整块胶囊就是拖动面）",
          stripopts.locked(config_mod.Config()) is False)
    wild = _variant(strip_offset_x=1e9, strip_locked="yes")
    check("手改出来的拖动偏移会被夹住、锁定开关会转成布尔",
          stripopts.sanitize(wild) is True
          and abs(wild.strip_offset_x - stripopts.OFFSET_LIMIT) < 1e-6
          and wild.strip_locked is True,
          f"{wild.strip_offset_x}/{wild.strip_locked}")
    junk = _variant(strip_offset_x="左边一点")
    check("偏移写成非数字时退回 0（不能让长条算不出位置）",
          stripopts.sanitize(junk) is True and junk.strip_offset_x == 0.0,
          repr(junk.strip_offset_x))
    # 悬停提示：以前靠两端两列小圆点当抓手，用户嫌丑。现在不留任何常驻装饰，
    # 悬停时才把描边转成强调色 —— 这两条守着「别把圆点加回来」和「悬停真的改变画面」。
    check("悬停配色只改描边（不引入任何常驻装饰）",
          _hover_palette_differs() is True)

    screen_dc = user32.GetDC(None)
    theme_dc = gdi32.CreateCompatibleDC(screen_dc)
    try:
        probe_h = 47

        def _paint(strip_obj, height):
            """离屏画一帧，返回 (宽, 内部中间一行的 alpha 列表, 该帧配色)。"""
            wide = strip_obj._layout(theme_dc, 1.0, _ProbeSnap(), render=False)[0]
            bmp, view = dib_section(theme_dc, wide, height)
            if not bmp:
                return 0, [], None
            old = gdi32.SelectObject(theme_dc, bmp)
            # _highlight 要写 self._view，离屏路径下得手动挂上（正常路径由
            # _render_if_needed 建缓冲区时挂）
            strip_obj._view, strip_obj._w, strip_obj._h = view, wide, height
            _t, _h, pal = strip_obj._layout(
                theme_dc, 1.0, _ProbeSnap(), render=True,
                origin_x=0, origin_y=0, height=height,
            )
            compose_shape_alpha(view, wide, height, margin=0, radius=height / 2.0,
                                shape_w=wide, shape_h=height, shadow=0,
                                shape_alpha=pal["alpha"], key_rgb=pal["key"])
            mid = height // 2
            row = [view[(mid * wide + x) * 4 + 3] for x in range(4, wide - 4)]
            gdi32.SelectObject(theme_dc, old)
            gdi32.DeleteObject(bmp)
            return wide, row, pal

        for theme_key, theme_label, _hint in stripopts.THEMES:
            probe = TaskbarStrip(_variant(strip_theme=theme_key))
            try:
                _w, row, pal = _paint(probe, probe_h)
                if pal is None:
                    check(f"质感「{theme_label}」离屏缓冲建得出", False,
                          "CreateDIBSection 失败")
                    continue
                # v1.0.13 起 6 个档位全部走毛玻璃/半透明（原来的「线框」也改成
                # 毛玻璃了，不再靠抠色把底色抠成透明），所以一律要求 alpha 整齐：
                # 既不能有空洞（某处没画到），也不能超范围（该透明的没透明）。
                invisible = [a for a in row if a != pal["alpha"]]
                ok = not invisible
                detail = (f"alpha={pal['alpha']} 全部命中" if ok
                          else f"异常 alpha：{sorted(set(invisible))[:5]}")
                check(f"质感「{theme_label}」画得出来且内部无空洞", ok, detail)
            finally:
                probe.destroy()

        # ---- 半透明合成必须「逐行一致」----
        # 这里埋过一个极隐蔽的坑：compose_shape_alpha 里 inside() 是闭包，捕获的是
        # 外层的 k；而「边缘」和「阴影」两个分支也往 k 里写值，于是每行一旦处理过
        # 一个边缘像素，该行之后所有内部像素就都按那个边缘像素的系数去乘 RGB ——
        # 画面上是规则的横向条纹。老代码里就有，但长条一直是不透明 255（走不到
        # 这条分支）所以没暴露，加了半透明质感才炸出来。
        # 用一块纯色内存缓冲直接调它、逐行断言，最能抓住这类「逐行不一致」。
        cw, ch = 240, 40
        buf = (ctypes.c_ubyte * (cw * ch * 4))()
        for i in range(cw * ch):
            buf[i * 4] = 0xF0        # B
            buf[i * 4 + 1] = 0xE8    # G
            buf[i * 4 + 2] = 0xE0    # R
        compose_shape_alpha(buf, cw, ch, margin=0, radius=ch / 2.0,
                            shape_w=cw, shape_h=ch, shadow=0, shape_alpha=200)
        mid_x = cw // 2
        rows = {tuple(buf[(y * cw + mid_x) * 4:(y * cw + mid_x) * 4 + 4])
                for y in range(ch // 4, ch * 3 // 4)}
        check("半透明合成逐行一致（没有横条纹）", len(rows) == 1,
              f"{len(rows)} 种：{sorted(rows)[:4]}")

        # ---- 抠色模式不能烂掉 ----
        # 6 个档位现在都不用它了（线框也改了毛玻璃），但 compose_shape_alpha 的
        # 这条分支得留着 —— 以后要加「真·透明」档还得靠它。直接喂一块纯底色缓冲
        # 断言它被抠成全透明，别让这条路径悄悄坏掉。
        kw, kh = 40, 24
        kbuf = (ctypes.c_ubyte * (kw * kh * 4))()
        for i in range(kw * kh):
            kbuf[i * 4] = 0x33        # B
            kbuf[i * 4 + 1] = 0x22    # G
            kbuf[i * 4 + 2] = 0x11    # R
        compose_shape_alpha(kbuf, kw, kh, margin=0, radius=0,
                            shape_w=kw, shape_h=kh, shadow=0,
                            shape_alpha=255, key_rgb=(0x11, 0x22, 0x33))
        keyed = {kbuf[(kh // 2 * kw + x) * 4 + 3] for x in range(4, kw - 4)}
        check("抠色模式：纯底色像素被抠成全透明", keyed == {0}, f"{sorted(keyed)}")

        # 大字号 + 宽大：内容更宽更高，但仍不能有空洞。（顺带验证字体缓存的键
        # 带了字号倍率 —— 不带的话这里会拿回标准字号的字体，宽度就不会变）
        plain = TaskbarStrip(base)
        small_w = plain._layout(theme_dc, 1.0, _ProbeSnap(), render=False)[0]
        plain.destroy()
        big = TaskbarStrip(_variant(strip_font_scale=1.5, strip_size="large"))
        big_h = max(18, int(round(48 * stripopts.height_ratio(big.cfg))))
        big_w, big_row, big_pal = _paint(big, big_h)
        check("字号调大会让长条变宽", big_w > small_w, f"{small_w}px → {big_w}px")
        check("大字号 + 宽大档内部无空洞",
              bool(big_row) and all(a == big_pal["alpha"] for a in big_row),
              f"alpha={sorted(set(big_row)) if big_row else '无数据'}")
        big.destroy()

        # ---- 「勾了多少就显示多少」----
        # 曾经的故障：宽度上限被一个 560 的常量卡成了真上限，勾 8 项也只显示 4 项
        # （「从后往前丢字段」那段看起来完全正常，只有量一下排了几段才看得出来）。
        # 这条链路是「放开上限 → 折行 → 短标签 → 收紧间距 → 更小字号」，任何一环
        # 断掉都会退回去，所以不逐环测，只测最终结果：全勾上之后一个都不许丢。
        full_strip = TaskbarStrip(_variant(strip_fields=list(stripopts.FIELD_KEYS)))
        try:
            sections = full_strip._sections(_ProbeSnap())
            plan = full_strip._plan(theme_dc, _ProbeSnap(), 1.25, 883,
                                    int(round(60 * 0.94)))
            check(f"可用 883px：{len(sections)} 项全勾一个不丢",
                  plan["placed"] == plan["total"] == len(sections),
                  f"排上 {plan['placed']}/{plan['total']}，{len(plan['rows'])} 行，"
                  f"密度档 {plan['density']}，字号 {plan['font_scale']}")
            check("字段多的时候确实折了行", len(plan["rows"]) >= 2,
                  f"{len(plan['rows'])} 行")
            # 折行必须摊成「行 × 列」的表格：行数 = 要求的行数、每行尽量一样多，
            # 并且**每列宽度取该列所有格子的最大值** —— 两条加起来才叫「两排对齐」
            # （列起点和分隔线的 x 都只由列决定，和行号无关）。
            # 真机上由 _stripfields_test.py 的 DrawSpy 按绘制调用的 x 再验一遍。
            check("折行摊成表格：每行格数只差一个（第一行多一格）",
                  len(plan["rows"]) >= 2
                  and max(len(r) for r in plan["rows"])
                  - min(len(r) for r in plan["rows"]) <= 1
                  and len(plan["rows"][0]) >= len(plan["rows"][-1]),
                  str([len(r) for r in plan["rows"]]))
            grid = plan["grid"]
            ok_grid = all(
                grid["col_w"][j] + 1e-6
                >= grid["label_w"][j] + plan["gap_label"] + grid["rest_w"][j]
                for j in range(grid["cols"])
            )
            check("每列宽度 ≥ 该列任何一个格子所需（不会把字截掉）", ok_grid,
                  f"{grid['cols']} 列 / 列宽 {[round(v) for v in grid['col_w']]}")
        finally:
            full_strip.destroy()
    finally:
        gdi32.DeleteDC(theme_dc)
        user32.ReleaseDC(None, screen_dc)

    info = taskbar.taskbar()
    if info is not None:
        check("开始按钮能定位（子窗口，必须 FindWindowEx）",
              taskbar.cluster_left() is not None,
              f"居中区左边缘={taskbar.cluster_left()}")

    dialog = None
    try:
        dialog = FeeSettingsDialog(cfg)
        built = dialog.create()
        check("电价设置窗口能建出来", built, "" if built else f"err={ctypes.get_last_error()}")
        if built:
            region_combo = dialog._controls[100]
            plan_combo = dialog._controls[101]
            check("省份项数 = 预设数 + 1 个自定义",
                  int(user32.SendMessageW(region_combo, 0x0146, 0, 0)) == len(tariffs.REGIONS) + 1)
            region = dialog._selected_region()
            check("打开时定位到默认省份", region.name if region else None, "四川")
            # 下拉框里存的必须是真中文。传字符串给 SendMessage 时如果没留引用，
            # 内容会变成野内存里的垃圾字符——而项数、选中下标全都是对的，
            # 只看选中项根本发现不了。
            length = int(user32.SendMessageW(region_combo, 0x0149, 1, 0))
            buf = ctypes.create_unicode_buffer(max(2, length + 2))
            user32.SendMessageW(
                region_combo, 0x0148, 1, ctypes.cast(buf, ctypes.c_void_p).value or 0
            )
            check("下拉框第 1 项文字是「四川」", buf.value == "四川", repr(buf.value))
            check("方案下拉框有内容",
                  int(user32.SendMessageW(plan_combo, 0x0146, 0, 0)) > 0)
    except Exception as exc:  # noqa: BLE001 - 自检就是要抓住任何异常
        check("电价设置流程无异常", False, repr(exc))
    finally:
        if dialog is not None:
            try:
                dialog.destroy()
            except Exception:  # noqa: BLE001
                pass

    # ---- 用量统计窗口（Tab + ListView 明细表）----
    # 这一段专门抓「单元测试抓不到」的那类故障，它们全都在这一块真实发生过：
    #   * 窗口过程没套 @WNDPROC → 建窗口直接抛 TypeError；
    #   * 给 SendMessageW 传结构指针时忘了取地址 → ArgumentError，或者传成野指针
    #     后控件显示垃圾字符（项数、选中下标却是对的）；
    #   * 把 WS_CLIPCHILDREN 当成 exStyle 传 → 那个位在扩展样式里正好是
    #     WS_EX_COMPOSITED，于是 ListView 的**表体一个字都不画**，而
    #     LVM_GETITEMCOUNT / LVM_GETITEMTEXTW 读回来一切正常。
    # 所以这里必须真的建窗口、真的把格子文字读回来。
    from . import stats as stats_mod

    class _StubMeter:
        """自检不能真起传感器线程，给统计窗口喂一份固定账本。"""

        def stats_totals(self):
            return {"today": (1200.0, 0.67), "month": (24000.0, 13.4),
                    "year": (156700.0, 81.9), "total": (156700.0, 81.9),
                    "month_days": 18, "year_days": 120, "days": 120,
                    "sessions": 41}

        def stats_rows(self, kind, limit=500):
            return [{"when": f"2026-01-{i:02d}", "wh": 100.0 * i,
                     "cost": 0.06 * i, "seconds": 3600.0 * i,
                     "note": "自检", "day": "2026-01-01"}
                    for i in range(1, min(6, limit + 1))]

    stats_win = None
    try:
        stats_win = StatsWindow(_StubMeter(), cfg)
        built = stats_win.create()
        check("用量统计窗口能建出来", built,
              "" if built else f"err={ctypes.get_last_error()}")
        if built:
            listview = stats_win._controls.get(stats_mod.IDC_LIST)
            tabctl = stats_win._controls.get(stats_mod.IDC_TAB)
            check("统计窗口的 Tab 与 ListView 都建出来了",
                  bool(listview) and bool(tabctl))
            stats_win.show()
            # 自己抽消息，不进消息循环（自检不能卡住）
            probe_msg = wintypes.MSG()
            for _ in range(40):
                while user32.PeekMessageW(ctypes.byref(probe_msg), None, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(probe_msg))
                    user32.DispatchMessageW(ctypes.byref(probe_msg))
            count = int(user32.SendMessageW(listview, stats_mod.LVM_GETITEMCOUNT,
                                            0, 0))
            check("统计表格真的填进去了", count == 5, f"{count} 行")
            cell = ctypes.create_unicode_buffer(256)
            row_item = stats_mod.LVITEMW()
            row_item.mask = stats_mod.LVIF_TEXT
            row_item.iItem = 0
            row_item.iSubItem = 0
            row_item.pszText = ctypes.cast(cell, ctypes.c_wchar_p)
            row_item.cchTextMax = 256
            user32.SendMessageW(listview, stats_mod.LVM_GETITEMTEXTW, 0,
                                ctypes.addressof(row_item))
            check("表格第一格能原样读回（不是乱码）",
                  cell.value == "2026-01-01", repr(cell.value))
            labels = []
            for i in range(len(stats_mod.TABS)):
                tbuf = ctypes.create_unicode_buffer(64)
                titem = stats_mod.TCITEMW()
                titem.mask = stats_mod.TCIF_TEXT
                titem.pszText = ctypes.cast(tbuf, ctypes.c_wchar_p)
                titem.cchTextMax = 64
                user32.SendMessageW(tabctl, stats_mod.TCM_GETITEMW, i,
                                    ctypes.addressof(titem))
                labels.append(tbuf.value)
            check("五个分页标题都能原样读回（按天/按月/按年/按每次开机/按时段）",
                  labels == [label for _k, label in stats_mod.TABS], str(labels))
    except Exception as exc:  # noqa: BLE001 - 自检就是要抓住任何异常
        check("用量统计流程无异常", False, repr(exc))
    finally:
        if stats_win is not None:
            try:
                stats_win.destroy()
            except Exception:  # noqa: BLE001
                pass

    # ---- 账本：存哪儿、跨天怎么算、旧字段怎么折算 ----
    # 这一类故障的共性是「用户觉得记录丢了」，而且往往发生在升级之后 ——
    # 所以下面这些检查里，磁盘位置是最关键的一条。
    from . import meter as meter_mod

    check("账本放在固定的用户数据目录（不跟 exe 走）",
          config_mod.DATA_DIR.name == "PowerMonitor"
          and config_mod.DATA_DIR != config_mod.app_dir(),
          str(config_mod.DATA_DIR))

    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    today = time.strftime("%Y-%m-%d")
    real_state = meter_mod.STATE_PATH
    try:
        meter_mod.STATE_PATH = tmp / "state.json"
        meter_mod.STATE_PATH.write_text(
            json.dumps({
                "total_wh": 3605.658,
                "today_date": yesterday,
                "today_wh": 2592.25,
                "today_cost": 1.1112,
                "saved_at": time.time(),
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        ledger = meter_mod.EnergyMeter(config_mod.Config())
        check("旧版的单日数字折进了每日账本",
              abs(ledger._days.get(yesterday, {}).get("wh", 0) - 2592.25) < 1e-6,
              str(sorted(ledger._days)))
        snap2 = ledger.snapshot()
        expected_month = 2592.25 if yesterday[:7] == today[:7] else 0.0
        check("本月电量 = 当月各天之和",
              abs(snap2.month_wh - expected_month) < 1e-6,
              f"{snap2.month_wh:.1f} Wh / {snap2.month_days} 天")
        check("累计电量沿用旧账", abs(snap2.total_wh - 3605.658) < 1e-6,
              f"{snap2.total_wh:.1f} Wh")
        # v2 起账本只记电量、电费是按峰谷电价现算的**视图**，所以旧账里那笔
        # total_cost 不再继承（它的口径可能是旧电价，直接继承就会出现「累计电费
        # 比本次电费还低」那种倒挂）。这里验的是「电费仍然算得出来」+「单调」。
        price = config_mod.Config().price_flat or 0.5
        check("旧账没有电费字段也能算出累计电费（按电量现算）",
              abs(snap2.total_cost - 3605.658 / 1000.0 * price) < 0.05,
              f"¥{snap2.total_cost:.2f}（按 ¥{price}/kWh 估）")
        check("电费单调：累计 ≥ 本次 ≥ 今日",
              snap2.total_cost + 1e-6 >= snap2.session_cost
              and snap2.session_cost + 1e-6 >= snap2.today_cost,
              f"累计 ¥{snap2.total_cost:.2f} ≥ 本次 ¥{snap2.session_cost:.2f} "
              f"≥ 今日 ¥{snap2.today_cost:.2f}")
        ledger.save_now()
        saved = json.loads(meter_mod.STATE_PATH.read_text(encoding="utf-8"))
        check("落盘能读回每日账本", isinstance(saved.get("days"), dict),
              str(list(saved.get("days", {}))))
        check("落盘只记电量、不记电费（电费是视图）",
              "total_cost" not in saved and saved.get("ledger_version") == 2
              and "total_peak_wh" in saved,
              f"ledger_version={saved.get('ledger_version')} "
              f"cost_keys={sorted(k for k in saved if 'cost' in k)}")
        check(f"每日账本保留 {meter_mod.HISTORY_DAYS} 天上限",
              meter_mod.HISTORY_DAYS >= 365, str(meter_mod.HISTORY_DAYS))
        check("落盘间隔缩到 15 秒内（少丢数据）",
              0 < meter_mod.PERSIST_INTERVAL <= 15.0,
              f"{meter_mod.PERSIST_INTERVAL}s")
    finally:
        meter_mod.STATE_PATH = real_state

    check("账本留有『上一代』副本（state.json.prev）",
          (not config_mod.STATE_PATH.exists())
          or config_mod.STATE_BACKUP_PATH.exists(),
          config_mod.STATE_BACKUP_PATH.name)

    # state.json 被清掉也要能自己站起来。这一条是冲着「安装程序多手删了账本」
    # 那个真实事故写的：数据目录里还有上一代副本时，启动必须直接接手它，
    # 而不是退回去翻 exe 同目录那份又小又旧的账。
    recov = tmp / "recover"
    recov.mkdir()
    origin = (config_mod.STATE_PATH, config_mod.STATE_BACKUP_PATH, config_mod.DATA_DIR)
    try:
        config_mod.DATA_DIR = recov
        config_mod.STATE_PATH = recov / "state.json"
        config_mod.STATE_BACKUP_PATH = recov / "state.json.prev"
        config_mod.STATE_BACKUP_PATH.write_text(
            json.dumps({"total_wh": 4321.0, "saved_at": time.time()}),
            encoding="utf-8",
        )
        moved = config_mod.migrate_user_data()
        got_wh = 0.0
        if config_mod.STATE_PATH.exists():
            got_wh = float(json.loads(
                config_mod.STATE_PATH.read_text(encoding="utf-8")).get("total_wh", 0))
        check("state.json 被清掉时自动接手上一代副本",
              abs(got_wh - 4321.0) < 1e-6, str(moved))
    finally:
        (config_mod.STATE_PATH, config_mod.STATE_BACKUP_PATH,
         config_mod.DATA_DIR) = origin

    # ---- 长条的新字段（本月 / 累计 / 累计电费） ----
    extra_fields = {
        key: stripopts.field_value(key, _ProbeSnap(), _ProbeCfg())
        for key in ("month", "total", "total_cost")
    }
    check("累计类长条字段都算得出文本",
          all(value is not None for value in extra_fields.values()),
          str(extra_fields))

    # ---- 托盘菜单：选项类子菜单点完要能「继续弹」 ----
    # 菜单选中一项就必然关闭（系统行为），所以靠「重建同一级子菜单」来模拟
    # 不关闭。这里保证「命令号 → 锚点」的映射完整，否则会静默退化成老行为。
    # 用子类当探针：菜单那几个方法就是 PowerMonitorApp 上的，直接继承最省事，
    # 只把 __init__ 换掉 —— 不建窗口、不起采样线程，也就不会抢托盘图标。
    class _MenuSnap:
        power_on_seconds = 7200.0
        session_wh = 428.3
        session_cost = 1.284
        today_wh = 1240.0
        today_cost = 0.65
        month_wh = 12300.0
        month_cost = 6.4
        total_wh = 156700.0
        total_cost = 81.9
        total_days = 12
        total_sessions = 5

    class _MenuMeter:
        def snapshot(self):
            return _MenuSnap()

    class _MenuProbe(PowerMonitorApp):
        def __init__(self) -> None:
            self.cfg = config_mod.Config()
            self.meter = _MenuMeter()

        def _set_strip_option(self, kind, value) -> None:
            pass

    found = {}
    for base, count, anchor in (
        (CMD_THEME_BASE, len(stripopts.THEMES), "theme"),
        (CMD_FONT_BASE, len(stripopts.FONT_SCALES), "font"),
        (CMD_SIZE_BASE, len(stripopts.SIZES), "size"),
        (CMD_FIELD_BASE, len(stripopts.FIELDS), "field"),
    ):
        got = {
            PowerMonitorApp._apply_strip_option(_MenuProbe(), base + i)
            for i in range(count)
        }
        found[anchor] = got == {anchor}
    check("每一档都能反查出它属于哪级子菜单", all(found.values()), str(found))
    check("普通命令不会被当成选项（锚点为 None）",
          PowerMonitorApp._apply_strip_option(_MenuProbe(), CMD_RESET) is None)
    # 「锁定位置 / 位置复位」是单号命令（不是档位），绝不能落进任何档位区间里 ——
    # 落了就会被 _apply_strip_option 提前吃掉，静默退化成「点了一下没反应」。
    check("锁定位置 / 位置复位不会被当成档位命令",
          PowerMonitorApp._apply_strip_option(_MenuProbe(), CMD_TOGGLE_LOCK) is None
          and PowerMonitorApp._apply_strip_option(_MenuProbe(), CMD_POS_RESET) is None)
    check("字号复位不会被当成档位命令（单号，且字号档位区间不含它）",
          PowerMonitorApp._apply_strip_option(_MenuProbe(), CMD_FONT_RESET) is None)
    built = [
        PowerMonitorApp._build_menu(_MenuProbe(), anchor)
        for anchor in PowerMonitorApp.STRIP_MENUS
    ]
    check("每级子菜单都能单独建出来（继续弹的前提）",
          all(bool(handle) for handle in built),
          str([bool(handle) for handle in built]))
    pos_menu = built[PowerMonitorApp.STRIP_MENUS.index("position")]
    check("长条位置子菜单有两个入口（锁定开关 + 位置复位）",
          int(user32.GetMenuItemCount(pos_menu)) == 2,
          f"{int(user32.GetMenuItemCount(pos_menu))} 项")
    font_menu = built[PowerMonitorApp.STRIP_MENUS.index("font")]
    # 六档 + 分隔线 + 复位 = 8 项（当前值不在档位上时再多一条只读行）
    font_entries = MenuBuilder.entries_of(font_menu) or []
    check("字号子菜单尾部有「复位标准字号」入口",
          any(e.get("cmd") == CMD_FONT_RESET for e in font_entries
              if e.get("type") == "item"),
          f"{len(font_entries)} 行")
    check("字号六档都标成 radio（单选视觉）",
          all(e.get("radio") for e in font_entries
              if e.get("type") == "item" and e.get("cmd", 0) >= CMD_FONT_BASE),
          "存在非 radio 的字号档")
    root = PowerMonitorApp._build_menu(_MenuProbe(), PowerMonitorApp.ROOT_MENU)
    check("整张根菜单也能为「继续弹」重建（顶层勾选式开关用）",
          bool(root) and PowerMonitorApp.ROOT_MENU not in PowerMonitorApp.STRIP_MENUS)
    check("顶层三个勾选式开关都登记成「点完不关」",
          set(PowerMonitorApp.PERSIST_COMMANDS)
          == {CMD_TOGGLE_AUTOSTART, CMD_PIN_TASKBAR, CMD_TOGGLE_STRIP},
          str(len(PowerMonitorApp.PERSIST_COMMANDS)))
    for handle in built + [root]:
        if handle:
            user32.DestroyMenu(handle)

    lines.append("")
    lines.append(f"通过 {counts['ok']} 项，失败 {counts['bad']} 项")

    try:
        (app_dir() / "selftest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass
    return 1 if counts["bad"] else 0
