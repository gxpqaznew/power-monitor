"""把托盘图标固定到任务栏（Windows 11）。

Windows 11 把每个托盘图标的显示状态记在
``HKCU\\Control Panel\\NotifyIconSettings\\<hash>`` 下，其中 ``IsPromoted=1``
表示常驻任务栏（即 ^ 箭头左侧），缺省或 0 表示折叠进隐藏区。

条目由 explorer 在图标首次出现时创建，所以设置动作要延迟几秒再做。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

try:
    import winreg
except ImportError:  # pragma: no cover - 非 Windows
    winreg = None  # type: ignore[assignment]

_BASE = r"Control Panel\NotifyIconSettings"
IS_PROMOTED = "IsPromoted"


def _dbg(msg: str) -> None:
    from . import debug

    debug.log("trayreg", msg)


def icon_owner_path() -> str:
    """托盘的归属进程路径。Shell 记录的是进程的可执行文件。"""
    return str(Path(sys.executable).resolve())


def _norm(path: str) -> str:
    return os.path.normcase(os.path.normpath(path))


def find_entry(exe_path: str | None = None) -> str | None:
    """找到对应 exe 的注册表子键名，没有则返回 None。"""
    if winreg is None:
        return None
    target = _norm(exe_path or icon_owner_path())
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _BASE)
    except OSError:
        _dbg(f"find_entry: 打不开 {_BASE}")
        return None
    seen: list[str] = []
    try:
        index = 0
        while True:
            try:
                sub = winreg.EnumKey(root, index)
            except OSError:
                break
            index += 1
            try:
                with winreg.OpenKey(root, sub) as sk:
                    value, _ = winreg.QueryValueEx(sk, "ExecutablePath")
            except OSError:
                continue
            path = _norm(str(value))
            if "能耗" in str(value) or "power" in str(value).lower():
                seen.append(str(value))
            if path == target:
                _dbg(f"find_entry: 命中 sub={sub}")
                return sub
    finally:
        winreg.CloseKey(root)
    _dbg(f"find_entry: 未命中 target={target} 相关条目={seen}")
    return None


def is_pinned(exe_path: str | None = None) -> bool:
    """图标当前是否被设为常驻任务栏。"""
    if winreg is None:
        return False
    sub = find_entry(exe_path)
    if sub is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{_BASE}\\{sub}") as sk:
            value, _ = winreg.QueryValueEx(sk, IS_PROMOTED)
            return bool(value)
    except OSError:
        return False


def set_pinned(pinned: bool = True, exe_path: str | None = None) -> bool:
    """设置 / 取消常驻。返回是否写成功。

    注意：explorer 不会立刻感知这个改动，通常需要重启资源管理器才生效。
    """
    if winreg is None:
        return False
    sub = find_entry(exe_path)
    if sub is None:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, f"{_BASE}\\{sub}", 0, winreg.KEY_SET_VALUE
        ) as sk:
            winreg.SetValueEx(sk, IS_PROMOTED, 0, winreg.REG_DWORD, 1 if pinned else 0)
        return True
    except OSError:
        return False


def pin_when_ready(attempts: int = 8, delay: float = 2.5) -> bool:
    """等 explorer 把条目建出来后再设置，并**读回校验**写入是否被覆盖。

    两件事都要防：图标刚出现时条目可能还没登记（要等），而刚重启完
    explorer 时它又可能在稍后重写整个条目、把刚写的 IsPromoted 抹掉
    （要复查重试）。所以每轮都「写一次 + 读回确认」。
    """
    for i in range(attempts):
        sub = find_entry()
        if sub is not None:
            wrote = set_pinned(True)
            back = is_pinned()
            _dbg(f"pin_when_ready 第{i + 1}轮 sub={sub} 写入={wrote} 读回={back}")
            if wrote and back:
                return True
        time.sleep(delay)
    _dbg(f"pin_when_ready 放弃（{attempts} 轮后仍失败）")
    return False


def _shell_present() -> bool:
    """任务栏本体在不在。判断 shell 是否真的起来了，不能只看进程。"""
    try:
        import ctypes

        return bool(
            ctypes.WinDLL("user32", use_last_error=True).FindWindowW(
                "Shell_TrayWnd", None
            )
        )
    except OSError:
        return False


def wait_for_shell(timeout: float = 12.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _shell_present():
            return True
        time.sleep(0.5)
    return False


def restart_explorer(attempts: int = 3) -> bool:
    """重启资源管理器让改动生效。任务栏会闪一下，已打开的文件夹窗口会关闭。

    **必须先确认 shell 回来了再返回**。这一步做过一次实测教训：只发一次
    ``Popen(["explorer.exe"])`` 就当作成功，一旦那次启动没活下来，用户就
    被留在「没有任务栏、没有桌面」的状态里，而这个动作本来是程序自己发起的。
    所以每次都等任务栏窗口出现，没出现就重来，并且把结果如实返回给调用方。
    """
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.run(
            ["taskkill", "/F", "/IM", "explorer.exe"],
            capture_output=True,
            creationflags=flags,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    time.sleep(1.5)

    for i in range(1, attempts + 1):
        try:
            subprocess.Popen(["explorer.exe"], creationflags=flags)
        except OSError:
            _dbg(f"restart_explorer: 第 {i} 次启动 explorer 失败")
            continue
        if wait_for_shell():
            _dbg(f"restart_explorer: 第 {i} 次成功，任务栏已恢复")
            return True
        _dbg(f"restart_explorer: 第 {i} 次启动后任务栏没回来，重试")

    _dbg("restart_explorer: 多次重试后任务栏仍未恢复")
    return False
