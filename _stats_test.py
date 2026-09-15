"""统计账本回归测试：每次开机明细、时段分布、五个维度的聚合。

跑法：python _stats_test.py
不建窗口、不读真实 state.json —— 全程指向临时目录。统计窗口只消费
``meter.stats_rows()`` / ``stats_totals()``，所以这里把这两个出口钉死，
窗口那边就不会出现「某列永远是空的」这种只有点开才发现的错。
"""

from __future__ import annotations

import json
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
    tmp = Path(tempfile.mkdtemp(prefix="pm_stats_")) / "state.json"
    if payload is not None:
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    meter_mod.STATE_PATH = tmp
    return tmp


def read_back(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    cfg = Config()
    today = time.strftime("%Y-%m-%d")
    this_month = today[:7]
    this_year = today[:4]
    # 开机时刻只能从系统日志拿（meter.session_start），测试里造不出来 ——
    # 「哪一次开机」的键就是这么来的，所以这里用真实的那个。
    boot_now = int(meter_mod.session_start()[0])

    # ---- 1. 每次开机：本次开机自动进账本，同一次开机不会碎成多条 ----
    path = with_state({
        "power_on_ts": boot_now,
        "first_seen_ts": boot_now + 60.0,
        "total_wh": 1000.0,
        "total_cost": 0.5,
        "saved_at": time.time(),
    })
    m = meter_mod.EnergyMeter(cfg)
    check("本次开机自动进『每次开机』账本",
          set(m._sessions) == {boot_now},
          f"keys={sorted(m._sessions)} boot={boot_now}")

    # 同一次开机内重开程序（开机时刻没变）→ 仍然是那一条
    m.save_now()
    m2 = meter_mod.EnergyMeter(cfg)
    check("同一次开机内重开程序不会新增一条",
          set(m2._sessions) == {boot_now}, f"keys={sorted(m2._sessions)}")

    # 账本里已经有「上一次开机」的记录 → 本次开机加一条，旧的必须留着
    # 注意：with_state 每次都换一个新临时目录，所以要接住它返回的新路径，
    # 不然读到的是上一轮那个文件（踩过）。
    older = boot_now - 86400
    path = with_state(read_back(path) | {
        "sessions": {str(older): [1234.0, 0.65, 7200.0, older + 8000]},
    })
    m3 = meter_mod.EnergyMeter(cfg)
    check("重新开机后新增一条、上一次的留着",
          set(m3._sessions) == {older, boot_now},
          f"keys={sorted(m3._sessions)}")

    # ---- 2. 落盘 / 还原的往返 ----
    m3.save_now()
    raw = read_back(path)
    check("state.json 里有 sessions 段", isinstance(raw.get("sessions"), dict),
          f"{len(raw.get('sessions') or {})} 条")
    check("state.json 里有 hours 段（24 个桶）",
          isinstance(raw.get("hours"), list) and len(raw["hours"]) == 24,
          f"len={len(raw.get('hours') or [])}")
    m4 = meter_mod.EnergyMeter(cfg)
    check("sessions 完整还原",
          set(m4._sessions) == {int(k) for k in raw["sessions"]},
          f"{sorted(m4._sessions)}")
    check("上一次开机的电量没在还原时丢掉",
          abs(m4._sessions[older]["wh"] - 1234.0) < 1e-6,
          f"{m4._sessions[older]['wh']}")

    # ---- 3. 五个维度都要出得来东西，且行形状统一 ----
    payload = {
        "power_on_ts": time.time() - 3600.0,
        "first_seen_ts": time.time() - 3540.0,
        "total_wh": 9000.0,
        "total_cost": 4.5,
        "today_date": today,
        "today_wh": 500.0,
        "today_cost": 0.26,
        "days": {
            "2025-12-31": [1000.0, 0.5, 3600.0],
            "2026-01-15": [2000.0, 1.0, 7200.0],
            f"{this_month}-01": [3000.0, 1.5, 10800.0],
            today: [500.0, 0.26, 1800.0],
        },
        "sessions": {
            str(int(time.time() - 86400)): [1200.0, 0.6, 7200.0, time.time() - 80000],
            str(int(time.time() - 172800)): [800.0, 0.4, 3600.0, time.time() - 160000],
        },
        "hours": [10.0] * 8 + [100.0] * 8 + [5.0] * 8,
        "saved_at": time.time(),
    }
    with_state(payload)
    m5 = meter_mod.EnergyMeter(cfg)

    keys = ("when", "wh", "cost", "seconds", "note")
    shapes = {}
    for kind in ("day", "month", "year", "session", "hour"):
        rows = m5.stats_rows(kind)
        shapes[kind] = (len(rows), all(
            all(k in row for k in keys) for row in rows
        ))
    check("五个维度都取得到明细", all(n > 0 for n, _ in shapes.values()),
          str({k: v[0] for k, v in shapes.items()}))
    check("每行字段齐全（when/wh/cost/seconds/note）",
          all(ok for _, ok in shapes.values()), str(shapes))

    # ---- 4. 聚合数值要对得上 ----
    day_rows = m5.stats_rows("day")
    check("按天：新的排在前面",
          [r["when"][:10] for r in day_rows] == sorted(
              (r["when"][:10] for r in day_rows), reverse=True),
          str([r["when"][:10] for r in day_rows]))

    month_rows = {r["when"]: r for r in m5.stats_rows("month")}
    check("按月：同一月的天加在一起",
          abs(month_rows[this_month]["wh"] - (3000.0 + 500.0)) < 1e-6,
          f"{this_month} = {month_rows[this_month]['wh']}")
    check("按月：说明里带天数与日均",
          "2 天" in month_rows[this_month]["note"],
          month_rows[this_month]["note"])

    year_rows = {r["when"]: r for r in m5.stats_rows("year")}
    check("按年：整年合计",
          abs(year_rows[this_year]["wh"] - (2000.0 + 3000.0 + 500.0)) < 1e-6,
          f"{this_year} = {year_rows[this_year]['wh']}")
    check("按年：去年单独一行",
          abs(year_rows["2025"]["wh"] - 1000.0) < 1e-6,
          str(sorted(year_rows)))

    ses_rows = m5.stats_rows("session")
    check("按每次开机：本次开机也在列表里",
          len(ses_rows) == 3, f"{len(ses_rows)} 条")
    check("按每次开机：新的排最前",
          ses_rows[0]["when"] >= ses_rows[-1]["when"],
          f"{ses_rows[0]['when']} >= {ses_rows[-1]['when']}")
    check("按每次开机：说明里给的是整次开机时长",
          "开机 " in ses_rows[0]["note"], ses_rows[0]["note"])

    hour_rows = m5.stats_rows("hour")
    check("按时段：24 行，从 00 点排到 23 点",
          len(hour_rows) == 24 and hour_rows[0]["when"].startswith("00:00")
          and hour_rows[23]["when"].startswith("23:00"),
          f"{hour_rows[0]['when']} … {hour_rows[23]['when']}")
    shares = [r["note"] for r in hour_rows]
    check("按时段：给出占比", any("%" in s for s in shares), shares[8])
    pct = sum(float(s.split()[-1].rstrip("%")) for s in shares if "%" in s)
    check("按时段：占比加起来约 100%", 99.0 <= pct <= 101.0, f"合计 {pct:.1f}%")

    # ---- 5. 顶部汇总 ----
    totals = m5.stats_totals()
    check("汇总：今天 / 本月 / 今年 / 累计 四个口径都在",
          set(totals) >= {"today", "month", "year", "total"},
          str(sorted(totals)))
    check("汇总：今年 = 今年各天之和",
          abs(totals["year"][0] - (2000.0 + 3000.0 + 500.0)) < 1e-6,
          f"{totals['year'][0]}")
    check("汇总：本月天数统计得出",
          totals["month_days"] == 2, f"{totals['month_days']} 天")
    check("汇总：累计沿用旧账",
          abs(totals["total"][0] - 9000.0) < 1e-6, f"{totals['total'][0]}")
    check("汇总：开机次数 = 每次开机的条数",
          totals["sessions"] == len(m5._sessions), f"{totals['sessions']}")

    # ---- 6. 手改坏的数据不能把统计搞崩 ----
    bad_key = int(time.time())
    with_state({
        "sessions": {"坏键": [1, 1, 1], "0": [1, 1, 1],
                     str(bad_key): [10.0, "坏", None, None]},
        "hours": [1, 2, 3],          # 长度不对 → 整段重置
        "days": {"坏日期": [1, 1, 1]},
        "saved_at": time.time(),
    })
    m6 = meter_mod.EnergyMeter(cfg)
    check("坏开机键被丢掉（0 / 非数字）",
          all(k > 0 for k in m6._sessions), f"{sorted(m6._sessions)}")
    check("坏数字被当成 0 而不是抛异常",
          m6._sessions[bad_key]["cost"] == 0.0
          and m6._sessions[bad_key]["wh"] == 10.0,
          str(m6._sessions.get(bad_key)))
    check("hours 长度不对时整段重置为 24 桶",
          len(m6._hours) == 24 and sum(m6._hours) == 0.0, f"{m6._hours[:3]}…")
    check("坏日期不会让按天视图崩掉", isinstance(m6.stats_rows("day"), list))

    # ---- 7. 上限：开机条数只留最近的 ----
    many = {str(int(time.time()) - i * 3600): [1.0, 0.001, 60.0, time.time() - i * 3600]
            for i in range(400)}
    with_state({"sessions": many, "saved_at": time.time()})
    m7 = meter_mod.EnergyMeter(cfg)
    check(f"每次开机最多留 {meter_mod.HISTORY_SESSIONS} 条",
          len(m7._sessions) <= meter_mod.HISTORY_SESSIONS,
          f"留下 {len(m7._sessions)} 条")
    check("留下的是最近的那些次",
          max(m7._sessions) == max(int(k) for k in many), f"{max(m7._sessions)}")

    print(f"\n通过 {PASS} 项，失败 {FAIL} 项")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
