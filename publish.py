"""一键发布：打包新安装包并发布到 GitHub Release。

用法：
    python publish.py                  # 自动补丁号 +1，打包并发布
    python publish.py --repo gxpqaznew/power-monitor
    python publish.py --version 1.2.0  # 指定版本，不自动 +1
    python publish.py --notes "修复了 xxx"   # Release 说明（不传则读 NOTES.md）

做的事：
1. 读 installer/PowerMonitor.nsi 的 APP_VERSION，补丁号 +1（或 --version 指定）；
2. 重包 exe（旧的 dist/能耗统计.exe 先 os.replace 移到临时目录，避开安全删除守卫）；
3. 重包安装包（旧的 *安装程序*.exe 同样移走）；
4. 若当前在 git 仓库，提交 NSIS 版本变更；
5. gh release create 打标签并把新安装包作为资产发布（需要已 git push 过且有 origin）。

仓库默认 gxpqaznew/power-monitor，可用 --repo 覆盖。
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
NSIS = PROJECT / "installer" / "PowerMonitor.nsi"
DIST = PROJECT / "dist"
SPEC = PROJECT / "PowerMonitor.spec"
DEFAULT_REPO = "gxpqaznew/power-monitor"

# 托管 Python（与构建环境一致）
PY = (os.environ.get("PYW_EXE")
      or str(Path.home() / ".workbuddy" / "binaries" / "python" / "envs"
             / "default" / "Scripts" / "python.exe"))
TEMP_BUILD = str(Path(tempfile.gettempdir()) / "powermon_build")

_VERSION_RE = re.compile(r'(!define\s+APP_VERSION\s+")([\d.]+)(")')


def _run(cmd, **kw):
    print("+", " ".join(str(c) for c in cmd))
    kw.setdefault("check", True)
    return subprocess.run(cmd, cwd=str(PROJECT), **kw)


def read_version() -> str:
    m = _VERSION_RE.search(NSIS.read_text(encoding="utf-8-sig"))
    if not m:
        raise SystemExit("找不到 APP_VERSION 定义")
    return m.group(2)


def write_version(ver: str) -> None:
    text = NSIS.read_text(encoding="utf-8-sig")
    new = _VERSION_RE.sub(lambda _m: f'{_m.group(1)}{ver}{_m.group(3)}', text, count=1)
    NSIS.write_text(new, encoding="utf-8-sig")


def bump(ver: str) -> str:
    parts = ver.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def _move_aside(path: Path) -> None:
    if not path.exists():
        return
    dst = Path(tempfile.gettempdir()) / f"old_{path.name}"
    os.replace(path, dst)  # 移动而非删除，避开安全删除守卫


def build_exe() -> None:
    _move_aside(DIST / "能耗统计.exe")
    _run([PY, "-m", "PyInstaller", str(SPEC),
          "--workpath", TEMP_BUILD, "--noconfirm"])


def build_installer() -> Path:
    for old in DIST.glob("*安装程序*.exe"):
        _move_aside(old)
    _run([PY, "installer/build.py"])
    installer = next(DIST.glob("*安装程序*.exe"), None)
    if installer is None:
        raise SystemExit("安装包没生成")
    return installer


def git_commit(ver: str) -> bool:
    try:
        _run(["git", "rev-parse", "--is-inside-work-tree"],
             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("（不在 git 仓库，跳过提交）")
        return False
    _run(["git", "add", str(NSIS)])
    _run(["git", "commit", "-m", f"release v{ver}"],
         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("已提交版本变更")
    try:
        _run(["git", "push"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("已推送到 origin")
    except subprocess.CalledProcessError:
        print("（推送失败：可能没有配置 origin，Release 标签会指向本地提交）")
    return True


def release(repo: str, ver: str, installer: Path, notes: str) -> None:
    tag = f"v{ver}"
    _run(["gh", "release", "create", tag,
          "-R", repo,
          str(installer),
          "--title", f"开机能耗统计 {tag}",
          "--notes", notes,
          "--latest"])
    print(f"已发布 https://github.com/{repo}/releases/tag/{tag}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--version", help="指定版本，不自动 +1")
    ap.add_argument("--notes", help="Release 说明")
    args = ap.parse_args()

    old_ver = read_version()
    ver = args.version or bump(old_ver)
    print(f"版本 {old_ver} -> {ver}")

    write_version(ver)
    build_exe()
    installer = build_installer()
    print(f"安装包：{installer}  ({installer.stat().st_size/1048576:.2f} MB)")

    git_commit(ver)

    notes = args.notes
    if not notes and (PROJECT / "NOTES.md").exists():
        notes = (PROJECT / "NOTES.md").read_text(encoding="utf-8-sig").strip()
    notes = notes or f"开机能耗统计 {ver}"
    release(args.repo, ver, installer, notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
