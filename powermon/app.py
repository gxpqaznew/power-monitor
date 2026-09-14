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
import os
import sys
import threading
import time
from pathlib import Path

from . import APP_NAME, __version__
from . import debug, trayreg
from .config import CONFIG_PATH, Config, app_dir
from .fee_dialog import FeeSettingsDialog
from .iconmake import make_icon, tray_label
from .meter import EnergyMeter, Snapshot
from .panel import Panel, fmt_duration
from .strip import TaskbarStrip
from .tray import (
    CMD_ABOUT,
    CMD_FEE_SETTINGS,
    CMD_MODE_AVERAGE,
    CMD_MODE_COST,
    CMD_MODE_CURRENT,
    CMD_MODE_SESSION,
    CMD_OPEN_CONFIG,
    CMD_PIN_TASKBAR,
    CMD_QUIT,
    CMD_RESET,
    CMD_TOGGLE_AUTOSTART,
    CMD_TOGGLE_PANEL,
    CMD_TOGGLE_STRIP,
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
    WM_PIN_READY,
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
        self.tray = TrayIcon(
            self._build_menu, self._on_command, self._on_extra_message
        )
        self._icon_key: tuple[str, int] | None = None
        self._timer_id = 0
        self._first_run = first_run
        self._pin_prompted = False
        self._pin_started = False
        self._tray_missing_since: float | None = None
        self._degraded = False
        self._strip_tried = False

    # ------------------------------------------------------------- 菜单

    def _build_menu(self):
        snap = self.meter.snapshot()
        menu = MenuBuilder()
        menu.label("本次开机", fmt_duration(snap.power_on_seconds))
        menu.label(
            "已统计能耗",
            f"{snap.session_wh:.0f} Wh · {self.cfg.currency}{snap.session_cost:.3f}",
        )
        menu.separator()

        menu.item(CMD_TOGGLE_PANEL, "打开详情面板")
        menu.separator()

        display = MenuBuilder.submenu()
        display.item(
            CMD_MODE_CURRENT, "当前功率 (W)", self.cfg.tray_display == "current"
        )
        display.item(
            CMD_MODE_COST,
            f"已用电费 ({self.cfg.currency})",
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
        menu.separator()
        menu.item(CMD_FEE_SETTINGS, "电价设置…")
        menu.item(CMD_OPEN_CONFIG, "打开配置文件")
        menu.separator()
        menu.item(CMD_RESET, "重置本次统计")
        menu.item(CMD_ABOUT, f"关于 {APP_NAME} v{__version__}")
        menu.separator()
        menu.item(CMD_QUIT, "退出")
        return menu.handle

    # ------------------------------------------------------------- 命令

    def _on_command(self, cmd: int) -> None:
        try:
            if cmd == CMD_TOGGLE_PANEL:
                self._toggle_panel()
            elif cmd in _MODE_BY_CMD:
                self._set_mode(_MODE_BY_CMD[cmd])
            elif cmd == CMD_TOGGLE_AUTOSTART:
                self._toggle_autostart()
            elif cmd == CMD_PIN_TASKBAR:
                self._toggle_pin()
            elif cmd == CMD_TOGGLE_STRIP:
                self._toggle_strip()
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
            self.tray.destroy()
            self.panel.destroy()
            self.strip.destroy()

    def quit(self) -> None:
        if self._timer_id:
            user32.KillTimer(None, self._timer_id)
            self._timer_id = 0
        self.fee.destroy()
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

    lines.append("")
    lines.append(f"通过 {counts['ok']} 项，失败 {counts['bad']} 项")

    try:
        (app_dir() / "selftest.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass
    return 1 if counts["bad"] else 0
