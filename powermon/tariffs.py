"""内置的各地居民电价预设。

数据来源都是省级发改委通知或电网公司价目表（见每个地区 ``source`` 字段），
``verify=True`` 的表示只有网络汇总口径、没找到官方原文，界面上会提示核对。

时段写法：``"23-7"`` 表示 23:00 到次日 06:59；``"11-17,20-22"`` 表示
11:00-16:59 与 20:00-21:59。左闭右开，支持跨零点。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache


@lru_cache(maxsize=64)
def parse_hours(spec: str) -> frozenset[int]:
    """把 ``"8-22"`` / ``"11-17,20-22"`` 解析成小时集合。

    左闭右开：``"8-22"`` = 8..21，``"23-7"`` = 23,0..6。
    """
    hours: set[int] = set()
    for chunk in str(spec).replace("，", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            left, _, right = chunk.partition("-")
            try:
                start, end = int(left), int(right)
            except ValueError:
                continue
            if start == end:
                hours.add(start % 24)
                continue
            cur = start % 24
            for _ in range(24):
                hours.add(cur)
                cur = (cur + 1) % 24
                if cur == end % 24:
                    break
        else:
            try:
                hours.add(int(chunk) % 24)
            except ValueError:
                continue
    return frozenset(hours)


@dataclass(frozen=True)
class Plan:
    """一个可选的用电方案（同一地区可以有多个档位 / 计费制式）。"""

    label: str
    peak: float
    flat: float
    valley: float
    valley_wet: float = 0.0  # 0 表示该地区没有丰枯季节差
    peak_hours: str = ""     # 空 = 该地区没有独立峰段
    valley_hours: str = ""   # 空 = 该地区没有低谷优惠
    wet_months: tuple[int, ...] = ()
    note: str = ""

    def resolved_valley_wet(self) -> float:
        return self.valley_wet if self.valley_wet > 0 else self.valley


@dataclass(frozen=True)
class Region:
    name: str
    plans: tuple[Plan, ...]
    source: str
    effective: str = ""
    verify: bool = False  # True = 只有网络汇总口径，建议用户核对
    keywords: tuple[str, ...] = field(default_factory=tuple)

    def default_plan(self) -> Plan:
        return self.plans[0]


def _flat(label: str, tier1: float, tier2: float, tier3: float, note: str = "") -> tuple[Plan, ...]:
    """没有居民分时电价的地区：三个档位都按平价计。"""
    return tuple(
        Plan(label=f"{label}（不分时）", peak=p, flat=p, valley=p, note=note)
        for label, p in (("第一档", tier1), ("第二档", tier2), ("第三档", tier3))
    )


REGIONS: tuple[Region, ...] = (
    Region(
        name="四川",
        plans=(
            Plan("一户一表 第一档", 0.5224, 0.5224, 0.2535, 0.175, "", "23-7", (6, 7, 8, 9, 10),
                 "7-9月第一档电量为260度及以下，其他月份180度及以下"),
            Plan("一户一表 第二档", 0.6224, 0.6224, 0.3535, 0.275, "", "23-7", (6, 7, 8, 9, 10)),
            Plan("一户一表 第三档", 0.8224, 0.8224, 0.5535, 0.475, "", "23-7", (6, 7, 8, 9, 10)),
            Plan("合表用户", 0.5464, 0.5464, 0.5464, note="合表用户不执行低谷优惠"),
        ),
        source="四川省电网居民生活电价表（川发改价格〔2012〕560号、〔2026〕255号）",
        effective="2026-07-01",
        keywords=("成都", "绵阳", "德阳", "自贡", "泸州", "南充", "宜宾"),
    ),
    Region(
        name="浙江",
        plans=(
            Plan("一户一表 第一档 · 峰谷", 0.5680, 0.5380, 0.2880, peak_hours="8-22", valley_hours="22-8",
                 note="年用电2760度及以下"),
            Plan("一户一表 第二档 · 峰谷", 0.6180, 0.5880, 0.3380, peak_hours="8-22", valley_hours="22-8"),
            Plan("一户一表 第三档 · 峰谷", 0.8680, 0.8380, 0.5880, peak_hours="8-22", valley_hours="22-8"),
            Plan("一户一表 不分时", 0.5380, 0.5380, 0.5380),
        ),
        source="国网浙江电力 居民生活用电分时电价表",
        keywords=("杭州", "宁波", "温州", "金华", "绍兴"),
    ),
    Region(
        name="安徽",
        plans=(
            Plan("一户一表 第一档 · 峰谷", 0.5953, 0.5953, 0.3153, peak_hours="", valley_hours="22-8",
                 note="平段8:00-22:00在分档价基础上加0.03元，谷段减0.25元"),
            Plan("一户一表 第二档 · 峰谷", 0.6453, 0.6453, 0.3653, valley_hours="22-8"),
            Plan("一户一表 第三档 · 峰谷", 0.8953, 0.8953, 0.6153, valley_hours="22-8"),
            Plan("一户一表 不分时", 0.5653, 0.5653, 0.5653),
        ),
        source="阜阳市人民政府 电价知识库（安徽一户一表居民电价标准）",
        keywords=("合肥", "芜湖", "蚌埠", "安庆"),
    ),
    Region(
        name="山东",
        plans=(
            Plan("一户一表 第一档 · 峰谷", 0.5769, 0.5469, 0.3769, peak_hours="8-22", valley_hours="22-8",
                 note="采暖季（11月-次年3月）谷价再降至0.3469元，且峰段改为8:00-20:00"),
            Plan("一户一表 第二档 · 峰谷", 0.6269, 0.5969, 0.4269, peak_hours="8-22", valley_hours="22-8"),
            Plan("一户一表 第三档 · 峰谷", 0.8769, 0.8469, 0.6769, peak_hours="8-22", valley_hours="22-8"),
            Plan("一户一表 不分时", 0.5469, 0.5469, 0.5469),
        ),
        source="山东电网销售电价表（鲁发改价格〔2026〕556号）、居民分时电价（鲁发改价格〔2022〕158号）",
        effective="2026-07-30",
        keywords=("济南", "青岛", "烟台", "潍坊", "临沂"),
    ),
    Region(
        name="河南",
        plans=(
            Plan("一户一表 第一档 · 峰谷", 0.59, 0.56, 0.44, peak_hours="8-22", valley_hours="22-8",
                 note="峰段加0.03元、谷段减0.12元；“煤改电”用户采暖季谷段延长为20:00-次日8:00"),
            Plan("一户一表 第二档 · 峰谷", 0.64, 0.61, 0.49, peak_hours="8-22", valley_hours="22-8"),
            Plan("一户一表 第三档 · 峰谷", 0.89, 0.86, 0.74, peak_hours="8-22", valley_hours="22-8"),
            Plan("一户一表 不分时", 0.56, 0.56, 0.56),
        ),
        source="河南省发展和改革委员会《关于进一步完善分时电价机制有关事项的通知》",
        keywords=("郑州", "洛阳", "南阳", "新乡"),
    ),
    Region(
        name="重庆",
        plans=(
            Plan("一户一表 第一档 · 峰谷", 0.62, 0.52, 0.34, peak_hours="11-17,20-22", valley_hours="0-8",
                 note="峰段加0.10元、谷段减0.18元；执行满一年后可申请退出"),
            Plan("一户一表 第二档 · 峰谷", 0.67, 0.57, 0.39, peak_hours="11-17,20-22", valley_hours="0-8"),
            Plan("一户一表 第三档 · 峰谷", 0.92, 0.82, 0.64, peak_hours="11-17,20-22", valley_hours="0-8"),
            Plan("一户一表 不分时", 0.52, 0.52, 0.52),
        ),
        source="重庆市人民政府《建立重庆市居民分时电价机制》（2023-06-01 执行）",
        keywords=("渝中", "万州", "涪陵", "潼南"),
    ),
    Region(
        name="广东（深圳）",
        plans=(
            Plan("一户一表 第一档 · 峰谷", 1.1121, 0.6542, 0.2486, peak_hours="10-12,14-19", valley_hours="0-8",
                 note="深圳供电局口径，不含政府性基金及附加；夏季（5-10月）第一档0-260度，非夏季0-200度"),
            Plan("一户一表 第二档 · 峰谷", 1.1621, 0.7042, 0.2986, peak_hours="10-12,14-19", valley_hours="0-8"),
            Plan("一户一表 第三档 · 峰谷", 1.4121, 0.9542, 0.5486, peak_hours="10-12,14-19", valley_hours="0-8"),
            Plan("一户一表 不分时", 0.6542, 0.6542, 0.6542),
            Plan("合表用户", 0.6912, 0.6912, 0.6912),
        ),
        source="深圳市发展和改革委员会 深圳市居民生活电价价目表",
        keywords=("深圳", "广州", "东莞", "佛山", "珠海"),
    ),
    Region(
        name="贵州",
        plans=_flat(
            "一户一表", 0.4556, 0.5056, 0.7556,
            "按自然年度累计：0-3000度、3000-4700度、4700度以上；12月1日起为新周期",
        ) + (Plan("合表用户", 0.4820, 0.4820, 0.4820),),
        source="贵州电网销售电价表（黔发改价格〔2016〕1299号）、贵州省发改委 2026 年提案答复",
        effective="2016-08-25",
        keywords=("贵阳", "遵义", "六盘水", "都匀", "黔南"),
    ),
    Region(
        name="云南",
        plans=(
            Plan("一户一表 第一档", 0.423625, 0.423625, 0.423625,
                 note="每年5-11月执行统一电价0.423625元"),
            Plan("一户一表 第二档（12月-4月）", 0.473625, 0.473625, 0.473625,
                 note="仅每年12月至次年4月执行阶梯"),
            Plan("一户一表 第三档（12月-4月）", 0.773625, 0.773625, 0.773625,
                 note="仅每年12月至次年4月执行阶梯"),
            Plan("合表用户", 0.483625, 0.483625, 0.483625),
        ),
        source="云南电网销售电价表",
        keywords=("昆明", "大理", "曲靖", "红河"),
    ),
    Region(
        name="广西",
        plans=_flat(
            "一户一表", 0.5283, 0.5783, 0.8283,
            "按自然年度累计：0-3120度、3120-4440度、4440度以上",
        ) + (Plan("合表用户", 0.5491, 0.5491, 0.5491),),
        source="柳州市政府定价商品价格目录清单（桂发改价格〔2021〕16号、桂价格函〔2018〕172号）",
        effective="2021-10-24",
        keywords=("南宁", "柳州", "桂林", "北海"),
    ),
    Region(
        name="海南",
        plans=_flat(
            "一户一表", 0.6083, 0.6583, 0.9083,
            "夏季（4-10月）第一档0-220度、第二档221-360度；冬季档位电量更少",
        ) + (Plan("合表用户", 0.6295, 0.6295, 0.6295),),
        source="海南省 12345 政务服务热线 电价答复口径",
        keywords=("海口", "三亚", "文昌", "昌江"),
    ),
    Region(
        name="北京",
        plans=_flat("一户一表", 0.4883, 0.5383, 0.7883,
                    "年累计：0-2880度、2880-4800度、4800度以上")
        + (Plan("一户一表 · 峰谷", 0.5603, 0.5103, 0.3103, peak_hours="8-22", valley_hours="22-8",
                note="网络汇总口径，峰谷时段与价格建议向国网北京核实"),),
        source="国网各省现行居民阶梯电价汇总（2026），建议以国网北京官网为准",
        verify=True,
    ),
    Region(
        name="上海",
        plans=(
            Plan("一户一表 第一档 · 峰谷", 0.617, 0.617, 0.307, peak_hours="6-22", valley_hours="22-6",
                 note="年累计0-3120度为第一档；峰谷时段6:00-22:00 / 22:00-次日6:00"),
            Plan("一户一表 第二档 · 峰谷", 0.667, 0.667, 0.357, peak_hours="6-22", valley_hours="22-6"),
            Plan("一户一表 第三档 · 峰谷", 0.917, 0.917, 0.607, peak_hours="6-22", valley_hours="22-6"),
            Plan("一户一表 不分时", 0.617, 0.617, 0.617),
        ),
        source="网络公开汇总口径，建议以国网上海官网 / 电费账单为准",
        verify=True,
        keywords=("上海",),
    ),
    Region(
        name="江苏",
        plans=_flat("一户一表", 0.5283, 0.5783, 0.8283,
                    "年累计：0-2760度、2760-4800度、4800度以上；江苏可申请居民峰谷，"
                    "具体峰谷价请按电费账单填写")
        + (Plan("一户一表 · 峰谷（按账单填写）", 0.5583, 0.5283, 0.3583,
                peak_hours="8-21", valley_hours="21-8",
                note="峰谷价差为常见口径，务必按自己电费账单核对"),),
        source="国网各省现行居民阶梯电价汇总（2026）；峰谷值需用户按账单核对",
        verify=True,
        keywords=("南京", "苏州", "无锡", "常州", "徐州"),
    ),
    Region(
        name="福建",
        plans=_flat("一户一表", 0.598, 0.648, 0.898,
                    "年累计：0-2760度、2760-4800度、4800度以上"),
        source="国网各省现行居民阶梯电价汇总（2026）",
        verify=True,
        keywords=("福州", "厦门", "泉州"),
    ),
    Region(
        name="湖北",
        plans=_flat("一户一表", 0.558, 0.608, 0.858,
                    "年累计：0-2160度、2160-4800度、4800度以上")
        + (Plan("一户一表 · 峰谷（按账单填写）", 0.62, 0.558, 0.36,
                peak_hours="9-15,20-22", valley_hours="23-7",
                note="湖北峰谷时段：尖峰20-22、高峰9-15、低谷23-次日7；"
                     "峰谷浮动系数为工商业口径，居民请按账单核对"),),
        source="国网各省现行居民阶梯电价汇总（2026）、湖北省发改委峰谷分时电价说明",
        verify=True,
        keywords=("武汉", "宜昌", "襄阳"),
    ),
    Region(
        name="湖南",
        plans=_flat("一户一表", 0.588, 0.638, 0.888,
                    "按月阶梯，冬夏季电量上限动态调整"),
        source="国网各省现行居民阶梯电价汇总（2026）",
        verify=True,
        keywords=("长沙", "株洲", "湘潭"),
    ),
    Region(
        name="江西",
        plans=_flat("一户一表", 0.60, 0.65, 0.90,
                    "年累计：0-2160度、2160-4200度、4200度以上"),
        source="国网各省现行居民阶梯电价汇总（2026）",
        verify=True,
        keywords=("南昌", "赣州", "九江"),
    ),
    Region(
        name="河北",
        plans=_flat("一户一表", 0.52, 0.57, 0.82,
                    "年累计：0-2160度、2160-4800度、4800度以上"),
        source="国网各省现行居民阶梯电价汇总（2026）",
        verify=True,
        keywords=("石家庄", "唐山", "保定"),
    ),
    Region(
        name="山西",
        plans=_flat("一户一表", 0.477, 0.527, 0.777,
                    "年累计：0-2160度、2160-4800度、4800度以上"),
        source="国网各省现行居民阶梯电价汇总（2026）",
        verify=True,
        keywords=("太原", "大同", "临汾"),
    ),
    Region(
        name="陕西",
        plans=_flat("一户一表", 0.4983, 0.5483, 0.7983,
                    "年累计：0-2160度、2160-4200度、4200度以上"),
        source="国网各省现行居民阶梯电价汇总（2026）",
        verify=True,
        keywords=("西安", "宝鸡", "咸阳"),
    ),
    Region(
        name="天津",
        plans=_flat("一户一表", 0.51, 0.56, 0.81,
                    "年累计：0-2160度、2160-4200度、4200度以上"),
        source="国网各省现行居民阶梯电价汇总（2026）",
        verify=True,
    ),
    Region(
        name="辽宁",
        plans=_flat("一户一表", 0.50, 0.55, 0.80,
                    "年累计：0-2160度、2160-4200度、4200度以上"),
        source="国网各省现行居民阶梯电价汇总（2026）",
        verify=True,
        keywords=("沈阳", "大连", "鞍山"),
    ),
    Region(
        name="吉林",
        plans=_flat("一户一表", 0.525, 0.575, 0.825,
                    "年累计：0-2160度、2160-4200度、4200度以上"),
        source="国网各省现行居民阶梯电价汇总（2026）",
        verify=True,
        keywords=("长春", "吉林", "延边"),
    ),
    Region(
        name="黑龙江",
        plans=_flat("一户一表", 0.51, 0.56, 0.81,
                    "年累计：0-2160度、2160-4200度、4200度以上"),
        source="国网各省现行居民阶梯电价汇总（2026）",
        verify=True,
        keywords=("哈尔滨", "大庆", "齐齐哈尔"),
    ),
    Region(
        name="甘肃",
        plans=_flat("一户一表", 0.51, 0.56, 0.81,
                    "年累计：0-2160度、2160-4200度、4200度以上"),
        source="国网各省现行居民阶梯电价汇总（2026）",
        verify=True,
        keywords=("兰州", "天水"),
    ),
    Region(
        name="青海",
        plans=_flat("一户一表", 0.5142, 0.5642, 0.8142,
                    "年累计：0-2160度、2160-4200度、4200度以上"),
        source="国网各省现行居民阶梯电价汇总（2026）",
        verify=True,
        keywords=("西宁",),
    ),
    Region(
        name="宁夏",
        plans=_flat("一户一表", 0.49, 0.54, 0.79,
                    "年累计：0-2160度、2160-4200度、4200度以上"),
        source="国网各省现行居民阶梯电价汇总（2026）",
        verify=True,
        keywords=("银川",),
    ),
    Region(
        name="新疆",
        plans=_flat("一户一表", 0.52, 0.57, 0.82,
                    "年累计：0-2160度、2160-4200度、4200度以上"),
        source="国网各省现行居民阶梯电价汇总（2026）",
        verify=True,
        keywords=("乌鲁木齐", "喀什"),
    ),
    Region(
        name="内蒙古",
        plans=_flat("一户一表", 0.50, 0.55, 0.80,
                    "年累计：0-2160度、2160-4200度、4200度以上"),
        source="国网各省现行居民阶梯电价汇总（2026）",
        verify=True,
        keywords=("呼和浩特", "包头"),
    ),
)


def region_names() -> list[str]:
    return [r.name for r in REGIONS]


def find_region(name: str) -> Region | None:
    """按名称匹配；支持用城市名（关键词）反查省份。"""
    target = (name or "").strip()
    if not target:
        return None
    for region in REGIONS:
        if region.name == target:
            return region
    for region in REGIONS:
        if target in region.name or region.name in target:
            return region
    for region in REGIONS:
        if any(k in target for k in region.keywords):
            return region
    return None


def plan_labels(region: Region) -> list[str]:
    return [p.label for p in region.plans]


def apply_plan(cfg, region: Region, plan: Plan) -> None:
    """把预设写进配置对象（不落盘）。"""
    cfg.tariff_region = region.name
    cfg.tariff_plan = plan.label
    cfg.tariff_source = region.source
    cfg.tariff_effective = region.effective
    cfg.tariff_verify = region.verify
    cfg.price_peak = plan.peak
    cfg.price_flat = plan.flat
    cfg.price_valley_dry = plan.valley
    cfg.price_valley_wet = plan.resolved_valley_wet()
    cfg.peak_hours = plan.peak_hours
    cfg.valley_hours = plan.valley_hours
    cfg.valley_wet_months = list(plan.wet_months)
    cfg.tariff_note = plan.note


__all__ = [
    "Plan",
    "Region",
    "REGIONS",
    "apply_plan",
    "find_region",
    "parse_hours",
    "plan_labels",
    "region_names",
]
