"""安装程序端到端验证（静默跑完整流程，最后把系统恢复到「已装好」的状态）。

流程：
  0  备份现有便携版数据（%LOCALAPPDATA%\\PowerMonitor）与注册表状态
  1  静默安装 → 校验 文件 / 注册表 / 快捷方式 / 旧数据迁移
  2  静默卸载 → 校验 清理是否干净
  3  重新静默安装 → 还原数据文件，系统回到可用状态
  4  清理备份

用法：python installer/_verify_installer.py
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import winreg
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
SETUP = PROJECT / "dist" / "开机能耗统计-安装程序-v1.0.0.exe"

APP_ID = "PowerMonitor"
APP_NAME = "开机能耗统计"
APP_EXE = "能耗统计.exe"
UNINST_EXE = f"卸载 {APP_NAME}.exe"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
UNINST_KEY = rf"Software\Microsoft\Windows\CurrentVersion\Uninstall\{APP_ID}"
NOTIFY_KEY = r"Control Panel\NotifyIconSettings"

LOCALAPPDATA = Path(os.environ["LOCALAPPDATA"])
INSTALL_DIR = LOCALAPPDATA / "Programs" / APP_ID
LEGACY_DIR = LOCALAPPDATA / APP_ID
BACKUP_DIR = Path(tempfile.gettempdir()) / "pm_state_backup"

shell32 = ctypes.WinDLL("shell32", use_last_error=True)

_ok = 0
_bad = 0


def check(label: str, cond: bool, detail: str = "") -> bool:
    global _ok, _bad
    mark = "PASS" if cond else "FAIL"
    if cond:
        _ok += 1
    else:
        _bad += 1
    tail = f"  ({detail})" if detail else ""
    print(f"  [{mark}] {label}{tail}")
    return cond


def known_folder(csidl: int) -> Path:
    buf = ctypes.create_unicode_buffer(260)
    shell32.SHGetFolderPathW(None, csidl, None, 0, buf)
    return Path(buf.value)


DESKTOP = known_folder(0x0010)          # CSIDL_DESKTOPDIRECTORY
PROGRAMS = known_folder(0x0002)         # CSIDL_PROGRAMS


def reg_read(hive, key: str, name: str):
    try:
        with winreg.OpenKey(hive, key) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def reg_exists(hive, key: str) -> bool:
    try:
        with winreg.OpenKey(hive, key):
            return True
    except OSError:
        return False


def run_setup(args: list[str], wait: float = 180.0) -> int:
    proc = subprocess.run(
        [str(SETUP)] + args,
        cwd=str(Path(os.environ["WINDIR"])),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=wait,
    )
    time.sleep(1.5)          # 给注册表/文件系统一点落盘时间
    return proc.returncode


def section(title: str) -> None:
    print(f"\n{'=' * 68}\n{title}\n{'=' * 68}")


# --------------------------------------------------------------------- 0 备份
section("0  备份现有状态")

if BACKUP_DIR.exists():
    shutil.rmtree(BACKUP_DIR, ignore_errors=True)

had_legacy = LEGACY_DIR.exists()
if had_legacy:
    shutil.copytree(LEGACY_DIR, BACKUP_DIR)
    print(f"  已备份 {LEGACY_DIR} → {BACKUP_DIR}")
    for f in sorted(BACKUP_DIR.iterdir()):
        print(f"      {f.name:24s} {f.stat().st_size:>9,d} 字节")
else:
    print(f"  {LEGACY_DIR} 不存在，无需备份")

old_run = reg_read(winreg.HKEY_CURRENT_USER, RUN_KEY, APP_ID)
print(f"  原自启项：{old_run!r}")

# --------------------------------------------------------------------- 1 安装
section("1  静默安装")
print(f"  运行：{SETUP.name} /S")
rc = run_setup(["/S"])
check("安装程序退出码为 0", rc == 0, f"rc={rc}")

check(f"安装目录存在：{INSTALL_DIR}", INSTALL_DIR.is_dir())
for name in (APP_EXE, "使用说明.txt", UNINST_EXE):
    p = INSTALL_DIR / name
    check(f"文件就位：{name}", p.exists(), f"{p.stat().st_size:,d} 字节" if p.exists() else "缺失")

check("旧数据已迁移：config.json", (INSTALL_DIR / "config.json").exists())
check("旧数据已迁移：state.json", (INSTALL_DIR / "state.json").exists())

if had_legacy and (BACKUP_DIR / "config.json").exists():
    same = json.loads((BACKUP_DIR / "config.json").read_text(encoding="utf-8")) == \
           json.loads((INSTALL_DIR / "config.json").read_text(encoding="utf-8"))
    check("迁移的 config.json 内容一致", same)
if had_legacy and (BACKUP_DIR / "state.json").exists():
    same = json.loads((BACKUP_DIR / "state.json").read_text(encoding="utf-8")) == \
           json.loads((INSTALL_DIR / "state.json").read_text(encoding="utf-8"))
    check("迁移的 state.json 内容一致", same)

check("旧便携目录已被清理", not LEGACY_DIR.exists())

expect_run = f'"{INSTALL_DIR / APP_EXE}"'
got_run = reg_read(winreg.HKEY_CURRENT_USER, RUN_KEY, APP_ID)
check("自启项指向新位置", got_run == expect_run, f"{got_run!r}")

check("已登记到「已安装的应用」", reg_exists(winreg.HKEY_CURRENT_USER, UNINST_KEY))
check("  DisplayName 正确",
      reg_read(winreg.HKEY_CURRENT_USER, UNINST_KEY, "DisplayName") == APP_NAME)
check("  DisplayVersion 正确",
      reg_read(winreg.HKEY_CURRENT_USER, UNINST_KEY, "DisplayVersion") == "1.0.0")
check("  UninstallString 正确",
      reg_read(winreg.HKEY_CURRENT_USER, UNINST_KEY, "UninstallString") == f'"{INSTALL_DIR / UNINST_EXE}"')
size_kb = reg_read(winreg.HKEY_CURRENT_USER, UNINST_KEY, "EstimatedSize")
check("  EstimatedSize 已写入", isinstance(size_kb, int) and size_kb > 1000, f"{size_kb} KB")

sm = PROGRAMS / APP_NAME
check("开始菜单项已创建", (sm / f"{APP_NAME}.lnk").exists() and (sm / f"卸载 {APP_NAME}.lnk").exists(),
      str(sm))
check("桌面快捷方式已创建", (DESKTOP / f"{APP_NAME}.lnk").exists(), str(DESKTOP))

# --------------------------------------------------------------------- 2 卸载
section("2  静默卸载")
uninst = INSTALL_DIR / UNINST_EXE
rc = subprocess.run(
    [str(uninst), "/S"], cwd=str(Path(os.environ["WINDIR"])),
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180,
).returncode
time.sleep(2.0)
check("卸载程序退出码为 0", rc == 0, f"rc={rc}")

check("安装目录已移除", not INSTALL_DIR.exists())
check("自启项已清除", reg_read(winreg.HKEY_CURRENT_USER, RUN_KEY, APP_ID) is None)
check("「已安装的应用」登记已移除", not reg_exists(winreg.HKEY_CURRENT_USER, UNINST_KEY))
check("开始菜单项已移除", not sm.exists())
check("桌面快捷方式已移除", not (DESKTOP / f"{APP_NAME}.lnk").exists())

# 托盘显示偏好是 explorer 的数据，卸载时故意保留：删掉之后 explorer 不会重建，
# 重装后图标就会掉回 ^ 里。这里只做记录，不作断言。
kept = []
with winreg.OpenKey(winreg.HKEY_CURRENT_USER, NOTIFY_KEY) as k:
    i = 0
    while True:
        try:
            sub = winreg.EnumKey(k, i)
        except OSError:
            break
        p = reg_read(winreg.HKEY_CURRENT_USER, f"{NOTIFY_KEY}\\{sub}", "ExecutablePath")
        if p and str(p).lower().endswith(APP_EXE.lower()):
            kept.append(str(p))
        i += 1
print(f"  [INFO] 托盘显示偏好条目保留 {len(kept)} 条（供重装复用）")
check("自动置顶记录已清除",
      reg_read(winreg.HKEY_CURRENT_USER, r"Software\PowerMonitor", "AutoPinnedExe") is None)

# --------------------------------------------------------------------- 3 恢复
section("3  重新安装（还原可用状态）")
rc = run_setup(["/S"])
check("安装程序退出码为 0", rc == 0, f"rc={rc}")
check("主程序就位", (INSTALL_DIR / APP_EXE).exists())

if BACKUP_DIR.exists():                     # 把原始数据文件放回去
    for name in ("config.json", "state.json"):
        src = BACKUP_DIR / name
        if src.exists():
            shutil.copy2(src, INSTALL_DIR / name)
            print(f"  已还原 {name}")
    shutil.rmtree(BACKUP_DIR, ignore_errors=True)

check("自启项已恢复并指向安装目录",
      reg_read(winreg.HKEY_CURRENT_USER, RUN_KEY, APP_ID) == expect_run)

# --------------------------------------------------------------------- 汇总
section("汇总")
print(f"  通过 {_ok} 项，失败 {_bad} 项")
print(f"  安装位置：{INSTALL_DIR}")
sys.exit(1 if _bad else 0)
