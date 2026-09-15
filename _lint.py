"""静态检查：找出「只读取、从未赋值」的实例属性。

本机编辑工具偶尔会静默丢改动，这类 bug 编译期看不出来、只会在运行时炸，
所以单独扫一遍。同时把真正的外部属性（如 cfg.xxx）排除在外。

第二项检查是安装脚本的「保命线」：NSIS 里绝不允许出现针对 `LEGACY_DIR`
（= `$LOCALAPPDATA\\PowerMonitor`，也就是用户数据目录）的 Delete / RMDir。
v1.0.6 首次发布时收尾段落里就留着这么一句 `Delete "${LEGACY_DIR}\\state.json"`，
把用户刚攒下的 3605 Wh 账本删了个干净 —— 装一次丢一次，而且现象正好是
「更新之后记录全没了」，跟这一版想修的问题长得一模一样，很难第一眼认出来。
"""

import ast
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "powermon"
problems = 0

for path in sorted(ROOT.glob("*.py")):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        stores: set[str] = set()
        loads: dict[str, int] = {}
        # 类体里定义的方法 / property / 类常量都算「已定义」
        for sub in node.body:
            if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                stores.add(sub.name)
            elif isinstance(sub, ast.Assign):
                for target in sub.targets:
                    if isinstance(target, ast.Name):
                        stores.add(target.id)
            elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                stores.add(sub.target.id)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                    and sub.value.id == "self":
                if isinstance(sub.ctx, (ast.Store, ast.Del)):
                    stores.add(sub.attr)
                elif isinstance(sub.ctx, ast.Load):
                    loads[sub.attr] = loads.get(sub.attr, 0) + 1
        # 类级别的注解/属性赋值也算已定义
        for sub in node.body:
            if isinstance(sub, ast.Assign):
                for target in sub.targets:
                    if isinstance(target, ast.Name):
                        stores.add(target.id)
        missing = sorted(name for name in loads if name not in stores)
        if missing:
            problems += 1
            print(f"{path.name}: class {node.name} 只用不赋值的属性 -> {missing}")

# ---- 安装脚本不得动用户数据目录 ----
NSI = Path(__file__).resolve().parent / "installer" / "PowerMonitor.nsi"
_DESTRUCTIVE = re.compile(r"^\s*(Delete|RMDir)\b.*\$\{LEGACY_DIR\}", re.IGNORECASE)
for lineno, line in enumerate(NSI.read_text(encoding="utf-8-sig").splitlines(), 1):
    if _DESTRUCTIVE.match(line):
        problems += 1
        print(f"PowerMonitor.nsi:{lineno}: 安装脚本在删用户数据目录 -> {line.strip()}")

print("OK" if not problems else f"发现 {problems} 处可疑")
sys.exit(1 if problems else 0)
