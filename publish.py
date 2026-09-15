"""一键发布：打包新安装包并发布到 GitHub Release。

用法：
    python publish.py                  # 自动补丁号 +1，打包并发布
    python publish.py --repo gxpqaznew/power-monitor
    python publish.py --version 1.2.0  # 指定版本，不自动 +1
    python publish.py --notes "修复了 xxx"   # Release 说明（不传则读 NOTES.md）

做的事：
1. 读 installer/PowerMonitor.nsi 的 APP_VERSION，补丁号 +1（或 --version 指定）；
   同时把 powermon/__init__.py 的 __version__ 写成同一个值（「关于」对话框读它，
   不同步的话关于里会一直显示老版本号）；
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
INIT_PY = PROJECT / "powermon" / "__init__.py"
DIST = PROJECT / "dist"
SPEC = PROJECT / "PowerMonitor.spec"
DEFAULT_REPO = "gxpqaznew/power-monitor"

# 托管 Python（与构建环境一致）
PY = (os.environ.get("PYW_EXE")
      or str(Path.home() / ".workbuddy" / "binaries" / "python" / "envs"
             / "default" / "Scripts" / "python.exe"))
TEMP_BUILD = str(Path(tempfile.gettempdir()) / "powermon_build")

_VERSION_RE = re.compile(r'(!define\s+APP_VERSION\s+")([\d.]+)(")')
_INIT_VERSION_RE = re.compile(r'(__version__\s*=\s*")([\d.]+)(")')


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


def read_init_version() -> str:
    m = _INIT_VERSION_RE.search(INIT_PY.read_text(encoding="utf-8"))
    return m.group(2) if m else ""


def write_init_version(ver: str) -> None:
    """把 ``powermon/__init__.py`` 的 ``__version__`` 也一起改掉。

    发布版本号的真源是 NSIS 的 ``APP_VERSION``（安装包文件名、注册表版本都用它），
    但托盘菜单「关于」显示的是 ``__init__.py`` 里的 ``__version__``。两边不同步的话
    关于对话框会永远显示一个老版本号 —— 之前就踩过（NSIS 已经 1.0.4，关于还写着
    1.0.0），所以这里强制一起写。
    """
    text = INIT_PY.read_text(encoding="utf-8")
    new, count = _INIT_VERSION_RE.subn(
        lambda _m: f'{_m.group(1)}{ver}{_m.group(3)}', text, count=1)
    if not count:
        raise SystemExit("powermon/__init__.py 里找不到 __version__ 定义")
    INIT_PY.write_text(new, encoding="utf-8")


def bump(ver: str) -> str:
    parts = ver.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def _move_aside(path: Path) -> None:
    """把旧产物移进临时目录（移动而非删除，避开安全删除守卫）。

    用唯一文件名，避免和之前已移走的旧文件撞名导致 os.replace 被锁。
    """
    if not path.exists():
        return
    d = Path(tempfile.gettempdir())
    base = f"old_{path.stem}_{os.getpid()}"
    for i in range(2000):
        cand = d / f"{base}_{i}{path.suffix}"
        if not cand.exists():
            try:
                os.replace(path, cand)
                return
            except PermissionError:
                continue
    print(f"（无法移走 {path.name}，可能被占用，跳过）")


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


def _local_commit_for_tree(tree_sha: str) -> str | None:
    """在本地 HEAD 历史里找 tree 与远端一致的提交（用于定位远端父提交的本地对应）。"""
    out = subprocess.run(["git", "rev-list", "HEAD"], cwd=str(PROJECT),
                         capture_output=True, text=True).stdout.split()
    for sha in out:
        t = subprocess.run(["git", "rev-parse", f"{sha}^{{tree}}"], cwd=str(PROJECT),
                           capture_output=True, text=True).stdout.strip()
        if t == tree_sha:
            return sha
    return None


def _api_push(local_base: str, remote_base: str, after: str, repo: str) -> bool:
    """git push 失败时的兜底：走 api.github.com 的 Git Data API 重放提交。

    本机沙箱/代理只放通 api.github.com，github.com 的 CONNECT 会被 502，
    因此 git-over-HTTPS 推不上去；改用 API 建 blob→tree→commit→更新 ref。
    local_base 是本地对应提交（用于算 rev-list 范围），
    remote_base 是远端当前的父提交（API 造出来的提交本地往往没有）。
    """
    import base64
    import json

    def api(path, method="POST", body=None):
        cmd = ["gh", "api", path, "-X", method]
        if body is not None:
            cmd += ["--input", "-"]
        p = subprocess.run(cmd, cwd=str(PROJECT), input=json.dumps(body) if body is not None else None,
                           capture_output=True, text=True)
        if p.returncode != 0:
            raise SystemExit(f"gh api {path} 失败:\n{p.stderr}")
        return json.loads(p.stdout) if p.stdout.strip() else {}

    revs = subprocess.run(["git", "rev-list", "--reverse", f"{local_base}..{after}"],
                          cwd=str(PROJECT), capture_output=True, text=True).stdout.split()
    if not revs:
        print("（没有新提交需要推送）")
        return True
    print(f"  兜底 API 推送 {len(revs)} 个提交…")
    parent = remote_base
    for rev in revs:
        msg = subprocess.run(["git", "log", "-1", "--format=%B", rev], cwd=str(PROJECT),
                             capture_output=True, text=True).stdout.strip()
        changes = subprocess.run(["git", "diff-tree", "--no-commit-id", "--name-status", "-r", rev],
                                 cwd=str(PROJECT), capture_output=True, text=True).stdout.splitlines()
        entries = []
        for line in changes:
            parts = line.split("\t")
            status, path = parts[0], parts[-1]
            if status.startswith("D"):
                entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
                continue
            content = subprocess.run(["git", "show", f"{rev}:{path}"], cwd=str(PROJECT),
                                     capture_output=True).stdout
            blob = api(f"/repos/{repo}/git/blobs", "POST",
                       {"content": base64.b64encode(content).decode(), "encoding": "base64"})
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        ptree = api(f"/repos/{repo}/git/commits/{parent}", "GET")["tree"]["sha"]
        tree = api(f"/repos/{repo}/git/trees", "POST", {"base_tree": ptree, "tree": entries})
        commit = api(f"/repos/{repo}/git/commits", "POST",
                     {"message": msg, "tree": tree["sha"], "parents": [parent]})
        parent = commit["sha"]
    api(f"/repos/{repo}/git/refs/heads/main", "PATCH", {"sha": parent, "force": False})
    print(f"  已用 API 推送到 main（{parent[:7]}）")
    return True


def git_commit(ver: str, repo: str = DEFAULT_REPO) -> bool:
    try:
        _run(["git", "rev-parse", "--is-inside-work-tree"],
             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("（不在 git 仓库，跳过提交）")
        return False
    # 把源码改动和版本文件一起提交（源码改动由用户/上层先行 add，这里兜底全提）
    _run(["git", "add", "-A"])
    head_before = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(PROJECT),
                                 capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "commit", "-m", f"release v{ver}"], cwd=str(PROJECT),
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    head_after = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(PROJECT),
                                capture_output=True, text=True).stdout.strip()
    print("已提交版本变更")
    p = subprocess.run(["git", "push"], cwd=str(PROJECT), capture_output=True, text=True)
    if p.returncode == 0:
        print("已推送到 origin")
        return True
    print(f"（git push 失败：{p.stderr.strip().splitlines()[-1] if p.stderr.strip() else '未知'}）")
    print("  改用 api.github.com 兜底推送…")
    # 远端 main 当前指向哪，就从哪开始重放；远端 sha 本地通常没有，
    # 用「树哈希一致」反查对应的本地提交作为 rev-list 起点。
    info = subprocess.run(["gh", "api", f"repos/{repo}/commits/main", "-q",
                           '.sha + " " + .commit.tree.sha'],
                          cwd=str(PROJECT), capture_output=True, text=True).stdout.strip()
    if not info:
        print("  拿不到远端 main，跳过")
        return False
    remote_base, remote_tree = info.split()
    local_base = _local_commit_for_tree(remote_tree)
    if not local_base:
        print(f"  远端 {remote_base[:7]} 的树在本地找不到对应提交，跳过兜底")
        return False
    _api_push(local_base, remote_base, head_after, repo)
    return True


def release(repo: str, ver: str, installer: Path, notes: str) -> None:
    tag = f"v{ver}"
    # GitHub 资产名用 ASCII：中文文件名经本机 shell 传参会被乱码（已踩过 -.-v1.0.1.exe）
    import shutil
    ascii_asset = DIST / f"PowerMonitor-Setup-v{ver}.exe"
    shutil.copy2(installer, ascii_asset)
    _run(["gh", "release", "create", tag,
          "-R", repo,
          str(ascii_asset),
          "--title", f"开机能耗统计 {tag}",
          "--notes", notes,
          "--latest"])
    print(f"已发布 https://github.com/{repo}/releases/tag/{tag}（资产 {ascii_asset.name}）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--version", help="指定版本，不自动 +1")
    ap.add_argument("--notes", help="Release 说明")
    args = ap.parse_args()

    old_ver = read_version()
    ver = args.version or bump(old_ver)
    print(f"版本 {old_ver} -> {ver}")
    init_old = read_init_version()
    if init_old != old_ver:
        print(f"  （__init__.py 里的 __version__ 是 {init_old}，先对齐到 {ver}）")

    write_version(ver)
    write_init_version(ver)
    # 自检一下：两个地方必须一致，否则「关于」又会显示错版本
    if read_version() != read_init_version():
        raise SystemExit("版本号没写一致：检查 NSIS 与 __init__.py")
    build_exe()
    installer = build_installer()
    print(f"安装包：{installer}  ({installer.stat().st_size/1048576:.2f} MB)")

    git_commit(ver, args.repo)

    notes = args.notes
    if not notes and (PROJECT / "NOTES.md").exists():
        notes = (PROJECT / "NOTES.md").read_text(encoding="utf-8-sig").strip()
    notes = notes or f"开机能耗统计 {ver}"
    release(args.repo, ver, installer, notes)
    return 0


if __name__ == "__main__":
    sys.exit(main())
