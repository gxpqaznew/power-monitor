"""账本回归测试：跨天不丢、旧字段折算、累计与本月汇总、原子写。

跑法：python _ledger_test.py
不建窗口、不读真实 state.json —— 全程指向临时目录，随便跑。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from powermon import meter as meter_mod  # noqa: E402
from powermon.config import Config  # noqa: E402

PASS = FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
    else:
        FAIL += 1
    tail = f"  ({detail})" if detail else ""
    print(f"[{'PASS' if cond else 'FAIL'}] {label}{tail}")


def with_state(payload: dict | None):
    """把 meter.STATE_PATH 指到临时文件，写入 payload。"""
    tmp = Path(tempfile.mkdtemp(prefix="pm_ledger_")) / "state.json"
    if payload is not None:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    meter_mod.STATE_PATH = tmp
    return tmp


def read_back(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    cfg = Config()
    yesterday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
    today = time.strftime("%Y-%m-%d")

    # ---- 1. 旧版单日字段跨天：必须折进 days，不能丢 ----
    path = with_state({
        "power_on_ts": time.time() - 100000.0,     # 上一次开机，必然是新会话
        "total_wh": 3605.658,
        "total_sessions": 1,
        "today_date": yesterday,
        "today_wh": 2592.2506,
        "today_cost": 1.1112,
        "saved_at": time.time() - 3600.0,
    })
    m = meter_mod.EnergyMeter(cfg)
    check("旧版昨天的数字折进了 days", yesterday in m._days,
          f"days 键 = {sorted(m._days)}")
    check("折算的电量对得上",
          abs(m._days.get(yesterday, {}).get("wh", 0) - 2592.2506) < 1e-6,
          str(m._days.get(yesterday)))
    check("重启后今日从 0 开始（不是继承昨天）", m._today_item()["wh"] == 0.0,
          f"today_wh={m._today_item()['wh']}")

    snap = m.snapshot()
    expected_month = 2592.2506 if yesterday[:7] == today[:7] else 0.0
    check("本月电量 = 当月各天之和",
          abs(snap.month_wh - expected_month) < 1e-6,
          f"month_wh={snap.month_wh} 期望≈{expected_month}")
    check("累计电量沿用旧账", abs(snap.total_wh - 3605.658) < 1e-6,
          f"total_wh={snap.total_wh}")
    check("历史天数统计得出", snap.total_days == len(m._days),
          f"total_days={snap.total_days}")
    # 旧账只有「电量 + 电费」，电费不可信（实测用户账本里它比本次电费还低），
    # 所以迁移时丢弃它，改由电量按当前电价现算。
    # 这份旧账没有峰谷拆分 → 全部按平段；累计里还有 1013 Wh 属于「只在 total_wh
    # 里、不在每日明细里」的历史（旧版本只存当天一个桶），所以累计按**总量**算。
    expect_total = 3605.658 / 1000.0 * cfg.price_flat
    check("旧账电费被丢弃、改由电量按当前电价现算",
          abs(snap.total_cost - expect_total) < 1e-6,
          f"total_cost={snap.total_cost:.4f} 期望≈{expect_total:.4f}")

    # ---- 1b 挪到第 8 节（它要换 STATE_PATH，会打断第 2 节的落盘检查）----

    # ---- 2. 落盘后重新加载：days 必须原样回来 ----
    m.save_now()
    raw = read_back(path)
    check("state.json 里有 days 段", isinstance(raw.get("days"), dict),
          str(list(raw.get("days", {}).keys())))
    check("days 写成紧凑数组", isinstance(raw["days"].get(yesterday), list),
          f"{yesterday} -> {raw['days'].get(yesterday)}")
    check("账本格式带版本号（v2）", raw.get("ledger_version") == 2,
          f"ledger_version={raw.get('ledger_version')}")
    check("落盘只存电量、不存电费（电费是换算出来的）",
          "total_cost" not in raw and "session_cost" not in raw
          and "today_cost" not in raw,
          f"键里还有 {[k for k in raw if k.endswith('_cost')]}")
    check("全局峰谷电量有落盘（累计电费靠它现算）",
          "total_peak_wh" in raw and "total_valley_wh" in raw)

    m2 = meter_mod.EnergyMeter(cfg)
    check("重载后 days 完整还原", m2._days.get(yesterday, {}).get("wh") ==
          pytest_approx(raw["days"][yesterday][0]),
          f"{m2._days.get(yesterday)}")

    # ---- 2b. v2 的峰谷电量要能存取往返 ----
    with_state({
        "ledger_version": 2,
        "days": {today: [1000.0, 3600.0, 250.0, 400.0]},
        "sessions": {"1789000000": [1000.0, 3600.0, 1789003600.0, 250.0, 400.0]},
        "hours": [0.0] * 24,
        "hours_valley": [0.0] * 24,
        "total_wh": 1000.0, "total_peak_wh": 250.0, "total_valley_wh": 400.0,
        "saved_at": time.time(),
    })
    m2b = meter_mod.EnergyMeter(cfg)
    check("v2 每日账本的峰谷电量读得回来",
          m2b._days[today]["peak"] == 250.0 and m2b._days[today]["valley"] == 400.0,
          str(m2b._days[today]))
    check("v2 每次开机的峰谷电量读得回来",
          m2b._sessions[1789000000]["peak"] == 250.0
          and m2b._sessions[1789000000]["valley"] == 400.0,
          str(m2b._sessions[1789000000]))
    m2b.save_now()
    raw2b = read_back(meter_mod.STATE_PATH)
    check("v2 落盘后 days 是 [电量, 秒数, 峰, 谷]",
          raw2b["days"][today][1:] == [3600.0, 250.0, 400.0],
          str(raw2b["days"][today]))
    check("v2 落盘后 sessions 是 [电量, 秒数, 最后, 峰, 谷]",
          raw2b["sessions"]["1789000000"][1:] == [3600.0, 1789003600.0, 250.0, 400.0],
          str(raw2b["sessions"]["1789000000"]))

    # ---- 3. 同一份账本里今天的数字要接上（不是清零） ----
    with_state({
        "power_on_ts": meter_mod.session_start()[0],
        "today_date": today,
        "today_wh": 123.5,
        "today_cost": 0.06,
        "days": {today: [123.5, 0.06, 3600.0]},
        "total_wh": 123.5,
        "total_cost": 0.06,
        "saved_at": time.time(),
    })
    m3 = meter_mod.EnergyMeter(cfg)
    check("今天早些时候的用量接上了", abs(m3._today_item()["wh"] - 123.5) < 1e-6,
          f"today_wh={m3._today_item()['wh']}")

    # ---- 4. 手改坏的账本不能把程序搞崩 ----
    with_state({
        "total_wh": "坏值",
        "total_cost": None,
        "days": {"2026-09-15": "不是列表", "2026-09-14": ["1.5", 0.1], "坏键": [1, 1, 1]},
        "today_date": 12345,
        "saved_at": time.time(),
    })
    m4 = meter_mod.EnergyMeter(cfg)
    check("坏值被当成 0 而不是抛异常", m4._total_wh == 0.0,
          f"total_wh={m4._total_wh}")
    check("坏日期被丢掉、好日期留下",
          set(m4._days) == {"2026-09-14"}, f"days={sorted(m4._days)}")
    check("字符串数字能读出来", abs(m4._days["2026-09-14"]["wh"] - 1.5) < 1e-6,
          str(m4._days["2026-09-14"]))

    # ---- 5. 天数上限：超过 HISTORY_DAYS 只留最近的 ----
    many = {f"2025-{m:02d}-{d:02d}": [1.0, 0.001, 60.0]
            for m in range(1, 13) for d in range(1, 29)}
    with_state({"days": many, "saved_at": time.time()})
    m5 = meter_mod.EnergyMeter(cfg)
    check(f"每日账本上限裁剪到 {meter_mod.HISTORY_DAYS} 天",
          len(m5._days) <= meter_mod.HISTORY_DAYS, f"留下 {len(m5._days)} 天")
    check("留下的是最近的那些天", max(m5._days) == max(many),
          f"最新一天 = {max(m5._days)}")

    # ---- 6. state.json 被清掉：必须从「上一代」副本自动接手 ----
    # 冲着一个真实事故写的：v1.0.6 的安装脚本在收尾段删了用户数据目录里的
    # state.json（它以为那是旧便携目录），3605 Wh 的账本当场归零。有了
    # state.json.prev，这种「文件凭空消失」至少还能自己站起来。
    from powermon import config as config_mod

    saved_paths = (config_mod.STATE_PATH, config_mod.STATE_BACKUP_PATH,
                   config_mod.DATA_DIR)
    rec = Path(tempfile.mkdtemp(prefix="pm_recover_"))
    try:
        config_mod.DATA_DIR = rec
        config_mod.STATE_PATH = rec / "state.json"
        config_mod.STATE_BACKUP_PATH = rec / "state.json.prev"
        config_mod.STATE_BACKUP_PATH.write_text(
            json.dumps({
                "total_wh": 3605.658,
                "days": {today: [10.0, 0.008, 60.0]},
                "saved_at": time.time(),
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        moved = config_mod.migrate_user_data()
        check("state.json 被清掉时自动接手上一代副本",
              config_mod.STATE_PATH.exists()
              and abs(read_back(config_mod.STATE_PATH)["total_wh"] - 3605.658) < 1e-6,
              str(moved))
        meter_mod.STATE_PATH = config_mod.STATE_PATH
        m6 = meter_mod.EnergyMeter(cfg)
        check("接手后累计与每日账本都在",
              abs(m6.snapshot().total_wh - 3605.658) < 1e-6 and today in m6._days,
              f"total_wh={m6.snapshot().total_wh} days={sorted(m6._days)}")
    finally:
        (config_mod.STATE_PATH, config_mod.STATE_BACKUP_PATH,
         config_mod.DATA_DIR) = saved_paths

    # ---- 7. 退出必须立刻落盘，不能等 15 秒的定时器 ----
    # 用户的原话是「每次退出之前一定要能保存」。正常退出 / 注销 / 关机 / atexit
    # 都走 stop() 或 save_now()，它们必须无条件写盘 —— 哪怕上一秒刚写过。
    path7 = with_state(None)
    m7 = meter_mod.EnergyMeter(cfg)
    with m7._lock:
        m7._total_wh = 999.5
    m7._last_persist = time.monotonic()      # 假装刚存过，定时器这一轮不触发
    m7.stop()
    check("退出时强制落盘（不等定时器）",
          abs(read_back(path7)["total_wh"] - 999.5) < 1e-6,
          f"total_wh={read_back(path7).get('total_wh')}")
    m7.stop()                                # 退出路径 + atexit 兜底会各调一次
    check("stop() 幂等（双路径不会互相踩）",
          abs(read_back(path7)["total_wh"] - 999.5) < 1e-6)

    # ---- 8. 电费一致性：累计 ≥ 本次 ≥ 今日（本轮修复的核心不变量）----
    # 冲着用户的真实反馈写的：「累计电费 ¥1.16 比本次电费 ¥1.64 还低」。
    # 成因是 total_cost 从残缺的每日账本回填、session_cost 继承旧电价累加器。
    # 现在电费一律由电量现算，电量单调 → 电费必然单调，结构上不可能再反。
    with_state({
        "ledger_version": 2,
        "power_on_ts": meter_mod.session_start()[0],
        "session_wh": 4416.6194,
        "peak_wh": 0.0,
        "valley_wh": 1383.6967,
        "total_wh": 4416.6194,
        "total_peak_wh": 0.0,
        "total_valley_wh": 1383.6967,
        # 每日账本只覆盖一部分（旧版本只存当天一个桶，更早的只在 total_wh 里）
        "days": {yesterday: [2592.251, 0.0, 0.0, 812.0],
                 today: [810.962, 0.0, 0.0, 254.0]},
        "saved_at": time.time(),
    })
    m8 = meter_mod.EnergyMeter(cfg)
    s8 = m8.snapshot()
    check("累计电费 ≥ 本次电费（不再出现倒挂）",
          s8.total_cost + 1e-9 >= s8.session_cost,
          f"累计 ¥{s8.total_cost:.4f} vs 本次 ¥{s8.session_cost:.4f}")
    check("本次电费 ≥ 今日电费",
          s8.session_cost + 1e-9 >= s8.today_cost,
          f"本次 ¥{s8.session_cost:.4f} vs 今日 ¥{s8.today_cost:.4f}")
    check("累计 ≥ 本月 ≥ 今日",
          s8.total_cost + 1e-9 >= s8.month_cost >= s8.today_cost - 1e-9,
          f"累计 {s8.total_cost:.4f} / 本月 {s8.month_cost:.4f} "
          f"/ 今日 {s8.today_cost:.4f}")
    check("每日明细之和 ≤ 累计（差额是无明细的历史）",
          sum(r["cost"] for r in m8.stats_rows("day")) <= s8.total_cost + 1e-9,
          f"{sum(r['cost'] for r in m8.stats_rows('day')):.4f} ≤ "
          f"{s8.total_cost:.4f}")
    # 统计窗口每一行都要有 cost，且明细行 cost 之和不能超过累计
    for kind in ("day", "month", "year", "session", "hour"):
        rows = m8.stats_rows(kind)
        check(f"「{kind}」每行都带有 cost / key",
              all("cost" in r and "key" in r for r in rows),
              f"{len(rows)} 行")
    # 改电价：历史数字必须跟着变，而且顺序关系不能破
    cheap = Config()
    cheap.price_flat *= 0.5
    cheap.price_valley_wet *= 0.5
    cheap.price_valley_dry *= 0.5
    m8._cfg = cheap
    s_cheap = m8.snapshot()
    check("改电价后历史电费跟着变（电费不是存下来的）",
          abs(s_cheap.total_cost - s8.total_cost) > 1e-6,
          f"{s8.total_cost:.4f} → {s_cheap.total_cost:.4f}")
    check("改电价后顺序关系依然成立",
          s_cheap.total_cost + 1e-9 >= s_cheap.session_cost
          >= s_cheap.today_cost - 1e-9,
          f"累计 {s_cheap.total_cost:.4f} / 本次 {s_cheap.session_cost:.4f} "
          f"/ 今日 {s_cheap.today_cost:.4f}")

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


def pytest_approx(value: float) -> float:
    """落盘会 round 到 3 位，比较时按同样的精度对齐。"""
    return round(float(value), 3)


if __name__ == "__main__":
    raise SystemExit(main())
