"""验证长条宽度不再随实时数值变化（防左边缘左右抖动）。

比例字体下 '1' 比 '0' 窄，长条按内容自适应宽度时，
功率从 101 W 变成 115 W 整条会窄 15px、左边缘横跳。
本测试断言：不同数值下测量宽度完全一致。
"""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))
from powermon.strip import TaskbarStrip  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}  {detail}")
    if not cond:
        FAILS.append(name)


def snap(w, cost, wh, today):
    return SimpleNamespace(current_w=w, session_cost=cost, session_wh=wh, today_wh=today)


def main():
    cfg = SimpleNamespace(currency="¥", strip_position="start")
    st = TaskbarStrip(cfg)
    # 复刻 _measure_width 里的临时 DC 测量路径
    import ctypes
    from powermon import w32  # noqa: F401

    widths = {}
    for w in (101, 115, 108, 999, 100):
        s = snap(w, 0.51, 1170, 160)
        widths[w] = st._measure_width(1.0, s)
    print("  各功率下的宽度：", widths)
    check("不同功率宽度一致（防抖）", len(set(widths.values())) == 1, str(set(widths.values())))

    # 电费/电量数字变化同样不应改宽
    w2 = {}
    for cost in (0.51, 0.18, 0.99, 1.11):
        w2[cost] = st._measure_width(1.0, snap(117, cost, 1170, 160))
    print("  各电费下的宽度：", w2)
    check("不同电费宽度一致", len(set(w2.values())) == 1, str(set(w2.values())))

    w3 = {}
    for today in (0.16, 0.99, 1.23):
        w3[today] = st._measure_width(1.0, snap(117, 0.51, 1170, int(today * 1000)))
    print("  各今日电量下的宽度：", w3)
    check("不同今日电量宽度一致", len(set(w3.values())) == 1, str(set(w3.values())))

    st.destroy()
    print(f"\n共 {5} 项，失败 {len(FAILS)} 项")
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
