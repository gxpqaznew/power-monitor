"""编译「开机能耗统计」的 Windows 安装程序。

用法：
    python installer/build.py

做三件事：
1. 把 PowerMonitor.nsi 与 使用说明.txt 统一成 **UTF-8 with BOM + CRLF**
   —— NSIS 3.x 在 Unicode 模式下靠 BOM 识别源文件编码，没有 BOM 中文会变乱码。
2. 找 makensis 并编译。
3. 打印产物路径、大小与版本信息。

makensis 的查找顺序：环境变量 NSIS_HOME / NSISDIR → PATH → 本机工具目录。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

# Windows 控制台默认是 GBK，直接 print 中文会变成乱码。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent
DIST = PROJECT / "dist"
SCRIPT = HERE / "PowerMonitor.nsi"

# 需要保证 UTF-8 BOM 的源文件
TEXT_SOURCES = [SCRIPT, HERE / "使用说明.txt"]

_FALLBACK_NSIS = Path.home() / ".workbuddy" / "tools" / "nsis-3.10" / "makensis.exe"


def normalise_encoding() -> None:
    """统一成 UTF-8 with BOM + CRLF（幂等）。"""
    for path in TEXT_SOURCES:
        if not path.exists():
            raise SystemExit(f"缺少源文件：{path}")
        text = path.read_text(encoding="utf-8-sig")
        text = text.replace("\r\n", "\n").replace("\n", "\r\n")
        path.write_bytes(text.encode("utf-8-sig"))
        print(f"  编码就绪  {path.relative_to(PROJECT)}")


def find_makensis() -> Path:
    for var in ("NSIS_HOME", "NSISDIR"):
        home = os.environ.get(var)
        if home:
            exe = Path(home) / "makensis.exe"
            if exe.exists():
                return exe
    which = shutil.which("makensis")
    if which:
        return Path(which)
    if _FALLBACK_NSIS.exists():
        return _FALLBACK_NSIS
    raise SystemExit(
        "找不到 makensis.exe。\n"
        "解压一份 NSIS 3.x 到 ~/.workbuddy/tools/nsis-3.10/，\n"
        "或设置环境变量 NSIS_HOME 指向 NSIS 安装目录。"
    )


def main() -> int:
    print("[1/2] 准备源文件编码")
    normalise_encoding()

    makensis = find_makensis()
    print(f"[2/2] 编译（{makensis}）")
    result = subprocess.run(
        [str(makensis), "/V3", str(SCRIPT)],
        cwd=str(HERE),
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        print("编译失败。")
        return result.returncode

    out = next(DIST.glob("*安装程序*.exe"), None)
    if out is None:
        print("编译声称成功，但没找到产物。")
        return 1
    size_mb = out.stat().st_size / 1048576
    print(f"\n产物：{out}")
    print(f"大小：{size_mb:.2f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
