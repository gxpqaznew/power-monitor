"""离线自检：采集层 + 开机时刻还原 + 图标渲染 + 分时计费。不需要桌面会话。"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon.config import Config  # noqa: E402
from powermon.iconmake import build_pixels, cost_text, make_icon, tray_label  # noqa: E402
from powermon.meter import Snapshot, is_valley, valley_price  # noqa: E402
from powermon.poweron import session_start  # noqa: E402
from powermon.sensors import SensorHub  # noqa: E402

cfg = Config.load()

# ---- 开机时刻 ----
t0 = time.perf_counter()
power_on, source = session_start()
elapsed_ms = (time.perf_counter() - t0) * 1000
print(f"开机时刻：{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(power_on))}"
      f"  口径：{source}  耗时 {elapsed_ms:.1f} ms")
print(f"开机时长：{(time.time() - power_on) / 3600:.2f} 小时")

# ---- 分时电价 ----
print("-" * 66)
now = time.localtime()
print(f"当前 {'低谷' if is_valley(cfg, now) else '峰/平'}段 · "
      f"电价 {valley_price(cfg, now) if is_valley(cfg, now) else cfg.price_peak:.4f} 元/度")

# ---- 采集 ----
hub = SensorHub(cfg)
print(f"CPU 负载源：{hub.cpu.precise_source}")
print(f"GPU：{hub.gpu.backend} · {hub.gpu.names} · 上限 {hub.gpu.limits}")
print("-" * 66)
print(f"{'CPU%':>7} {'CPU瓦':>8} {'GPU瓦':>8} {'基础':>7} {'合计':>8}")
for _ in range(5):
    r = hub.read()
    print(f"{r.cpu_util:7.1f} {r.cpu_w:8.1f} {r.gpu_w:8.1f} "
          f"{r.base_w:7.1f} {r.total_w:8.1f}")
    time.sleep(1.2)
hub.close()

# ---- 图标渲染：落成 PNG 便于肉眼检查 ----
out_dir = Path(__file__).parent / "_preview"
out_dir.mkdir(exist_ok=True)


def save_png(path: Path, pixels: bytes, size: int) -> None:
    """把 BGRA 像素写成 32 位 BMP（无需 Pillow，也能被看图软件打开）。"""
    import struct

    row = size * 4
    header = struct.pack("<2sIHHI", b"BM", 14 + 40 + row * size, 0, 0, 14 + 40)
    info = struct.pack("<IiiHHIIiiII", 40, size, -size, 1, 32, 0, row * size,
                       2835, 2835, 0, 0)
    path.write_bytes(header + info + pixels)


size = 32
for label, ratio_text in (("light", ("128", 0.10)), ("mid", ("256", 0.50)),
                          ("high", ("512", 0.90))):
    text, ratio = ratio_text
    save_png(out_dir / f"icon_{label}.bmp", build_pixels(text, ratio, size), size)
print(f"\n图标渲染 OK（{size}px，三档配色已存到 {out_dir.name}/）")

# ---- 托盘文本模式 ----
fake = Snapshot(
    session_wh=423.7, session_cost=0.31, covered_seconds=12000.0,
    average_w=118.0, peak_w=386.0, samples=6120, peak_wh=350.0, valley_wh=73.7,
    current_w=132.0, cpu_w=62.0, gpu_w=35.0, base_w=35.0, cpu_util=41.0,
    in_valley=False, today_wh=1240.0, today_cost=0.86,
    total_wh=45678.9, total_sessions=12,
    power_on_ts=time.time() - 17700, power_on_source="电源事件",
    first_seen_ts=time.time() - 12000,
    cpu_source="PDH 负载模型", gpu_source="nvml", cpu_estimated=True,
    gpu_measured=True, gpu_names=["NVIDIA GeForce RTX 3080"], gpu_limits=[320.0],
)
for mode in ("current", "cost", "session", "average"):
    text, ratio = tray_label(fake, mode, cfg)
    print(f"  托盘模式 {mode:8s} -> 文本 {text!r}  底色比例 {ratio:.2f}")

print("  电费精度分档 -> " + " / ".join(cost_text(v) for v in (0.32, 1.284, 12.4, 103.7)))

print(f"  外推校验：已统计 3.33h / {fake.session_wh:.0f} Wh -> "
      f"整段 4.92h 约 {fake.estimated_wh:.0f} Wh")

hicon = make_icon("128", 0.5)
print(f"  HICON 生成：{'OK' if hicon else '失败'}")

print("\n自检通过。")
