#!/usr/bin/env python3
"""智能路由自测：候选过滤、倍率门槛、归一化排序、成功率口径、设置持久化。

全部离线：构造渠道响应，不访问网络，不产生任何 pin 操作。
"""

from __future__ import annotations
# 直接运行本文件时，把项目根目录加入 sys.path（无需安装成包）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import os
import tempfile
import time

from modelkeeper import smart_route as sr


class check:  # noqa: N801 - 与其它自测脚本保持同一种调用样式
    failed = 0

    def __init__(self, name: str, ok: object, detail: object = "") -> None:
        if ok:
            print(f"PASS  {name}")
        else:
            check.failed += 1
            print(f"FAIL  {name}  -> {detail}")


def _listing(**over) -> dict:
    """一条「刚好吃满所有门槛」的 listing，用例只改需要破坏的那一项。"""
    row = {
        "model_name": "m1",
        "channel_id": 100,
        "supplier_id": 9,
        "channel_name": "Ch 100",
        "supplier_name": "Sup 9",
        "supplier_channel_disabled": False,
        "user_channel_disabled": False,
        "input_price_micros": 60_000,          # 0.4 倍官方价
        "output_price_micros": 120_000,
        "official_price": {"input_price_micros": 150_000},
        "recent_p50_ms": 1200,
        "sample_count": 100,
        "recent_success_rate": 9900,           # 万分比 → 99%
    }
    row.update(over)
    return row


def _split(probes: list[dict]) -> tuple[list[dict], list[dict]]:
    """把请求分成「采样」和「终检」：终检带 tools，采样不带。"""
    return ([p for p in probes if not p.get("tools")],
            [p for p in probes if p.get("tools")])


def _copy(cand):
    """复制一个候选：rank() 会把 score 写回对象，复用同一个实例会互相污染。"""
    import dataclasses

    return dataclasses.replace(cand)


def _payload(*listings: dict) -> dict:
    return {"data": [{"channel_id": x["channel_id"], "supplier_id": x["supplier_id"],
                      "listings": [x]} for x in listings]}


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="route-"))
    os.environ["MODEL_PROBE_ROUTE_FILE"] = str(tmp / "routing.json")

    # ---- key 归一化：客户端常写 sk- 前缀，站点令牌本体不带 ----
    check("去 sk- 前缀", sr.bare_key("sk-abc123") == "abc123")
    check("大小写都去", sr.bare_key("SK-abc123") == "abc123")
    check("裸串保持原样", sr.bare_key("abc123") == "abc123")
    check("空值不炸", sr.bare_key("") == "" and sr.bare_key(None) == "")
    check("两边归一后可比", sr.bare_key("sk-uh9DJOBL") == sr.bare_key("uh9DJOBL"))

    # ---- 成功率口径：接口是万分比，不是 0..1 ----    check("万分成 0..1", abs(sr._success_rate(9960) - 0.996) < 1e-9, sr._success_rate(9960))
    check("0..1 原样保留", abs(sr._success_rate(0.99) - 0.99) < 1e-9)
    check("空值当 0", sr._success_rate(None) == 0.0)
    check("越界截断到 1", sr._success_rate(20000) == 1.0)

    # ---- 控制面地址推断 ----
    check("api. 前缀剥掉", sr.guess_control_base("https://api.a6api.vip/v1") == "https://a6api.vip",
          sr.guess_control_base("https://api.a6api.vip/v1"))
    check("裸域补 scheme", sr.guess_control_base("a6api.vip/v1") == "https://a6api.vip")
    check("空串不报错", sr.guess_control_base("") == "")
    # provider 键不是域名：猜成 https://a6api 只会把错误推迟到 DNS 层
    check("provider 键不瞎猜", sr.guess_control_base("a6api") == "",
          sr.guess_control_base("a6api"))
    check("非 http 头也认", sr.guess_control_base("api.a6api.vip/v1") == "https://a6api.vip")
    check("带端口保留端口", sr.guess_control_base("https://api.x.com:8443/v1") == "https://x.com:8443",
          sr.guess_control_base("https://api.x.com:8443/v1"))

    # ---- 候选过滤 ----
    good = sr.parse_candidates(_payload(_listing()), "m1")
    check("合格商家进候选", len(good[0]) == 1 and good[1] == 1, good)

    # 倍率硬门槛：0.8 是上限，等于可以、超过就不行
    edge = _listing(input_price_micros=120_000)          # 刚好 0.8
    check("倍率=0.8 保留", len(sr.parse_candidates(_payload(edge), "m1")[0]) == 1)
    over = _listing(input_price_micros=120_001)          # 0.800006
    check("倍率>0.8 淘汰", len(sr.parse_candidates(_payload(over), "m1")[0]) == 0)

    # 无样本的 p50 恒为 0，必须被样本门槛挡住，不能当成「最快」
    zero = _listing(recent_p50_ms=0, sample_count=0)
    check("零样本不当最快", len(sr.parse_candidates(_payload(zero), "m1")[0]) == 0)

    check("样本不足淘汰", len(sr.parse_candidates(
        _payload(_listing(sample_count=5)), "m1")[0]) == 0)
    # 延迟不再是「超过参考值就淘汰」：参考值只用来打分，真正淘汰看 hard_p50_ms。
    # 旧语义把「站点 p50 5.2 秒、我自己 148 次调用中位 2.0 秒、倍率 0.06x」的商家
    # 直接扔掉，只剩下一批 0.36x 的候选——这正是「选上的不是最佳」的主因。
    check("延迟超参考值仍可入选", len(sr.parse_candidates(
        _payload(_listing(recent_p50_ms=5000)), "m1")[0]) == 1)
    check("超过硬上限才淘汰", len(sr.parse_candidates(
        _payload(_listing(recent_p50_ms=25_000)), "m1")[0]) == 0)
    check("成功率不足淘汰", len(sr.parse_candidates(
        _payload(_listing(recent_success_rate=9000)), "m1")[0]) == 0)

    # ---- 实测战绩：这一层才是「选中的商家能不能用」的关键 ----
    # 真实案例（站点日志 800 行实证）：Ch 6490 站点报成功率 97%、倍率 0.13x，
    # 但我自己在它上面 148 次调用失败了 29 次（19.6%，每次 10 秒 0 token、
    # error_code=no_healthy_account）。旧逻辑只看站点统计，于是每次都会选它。
    flaky = sr.ChannelStat(calls=148, fails=29, p50_ms=3000)
    good = sr.ChannelStat(calls=148, fails=0, p50_ms=2000)
    payload = _payload(_listing(channel_id=6490, input_price_micros=19_440,
                                recent_p50_ms=2247, recent_success_rate=9700),
                       _listing(channel_id=2846, input_price_micros=9_000,
                                recent_p50_ms=5155, recent_success_rate=9950))
    kept, _ = sr.parse_candidates(payload, "m1", sr.RouteSettings(),
                                  {6490: flaky, 2846: good})
    ids = [c.channel_id for c in kept]
    check("实测高失败率的商家被淘汰", 6490 not in ids, ids)
    check("站点 p50 慢但实测快的商家仍入选", 2846 in ids, ids)
    picked = next(c for c in kept if c.channel_id == 2846)
    # 延迟打分统一用站点 p50：它和别的商家同口径。实测 use_time 含生成，
    # 长上下文能到十几秒，混进来会把「总耗时」当成「首字」，重复惩罚慢的家。
    check("延迟打分优先站点 p50", picked.eff_p50_ms == 5155, picked.eff_p50_ms)
    check("实测延迟照样保留（旁证）", picked.real_p50_ms == 2000, picked.real_p50_ms)
    no_site, _ = sr.parse_candidates(
        _payload(_listing(channel_id=2846, recent_p50_ms=0)), "m1",
        sr.RouteSettings(min_samples=0), {2846: good})
    check("站点没有延迟时用实测兜底",
          no_site and no_site[0].eff_p50_ms == 2000,
          no_site[0].eff_p50_ms if no_site else None)
    check("实测战绩带到候选上", (picked.real_calls, picked.real_fails) == (148, 0), picked)
    check("候选带得出手的理由", "失败 0/148" in picked.stat_label, picked.stat_label)

    # ---- 权重预设：三档各自把权重改成什么，以及「省钱 vs 速度」真的选出不同的人 ----
    # 断言不写死 85/10/5 这类数字，而是核对三条性质：范围合法、默认值必属某档、
    # 两档极端确实会选出不同的家。数字将来调了，这三条仍然成立。
    bounds = {name: (lo, hi) for name, _kind, (lo, hi) in sr.SETTING_FIELDS}
    for preset in sr.WEIGHT_PRESETS:
        for name, value in zip(sr.WEIGHT_FIELDS, preset.weights):
            lo, hi = bounds[name]
            check(f"预设 {preset.name} 的 {name} 在允许范围内",
                  lo <= value <= hi, (name, value, lo, hi))
        check(f"预设 {preset.name} 留了成功率权重", preset.success >= 1, preset.success)
    check("默认权重正好是某一档预设", sr.match_preset(sr.RouteSettings()) is not None,
          sr.match_preset(sr.RouteSettings()))
    check("没有任何一档时返回 None",
          sr.match_preset(sr.RouteSettings(price_weight=99, latency_weight=98,
                                          success_weight=97)) is None)
    check("权重无序匹配（顺序不同就是自定义）",
          sr.match_preset(sr.RouteSettings(price_weight=45, latency_weight=20,
                                          success_weight=35)) is None)

    # 便宜的慢家 vs 贵的快家：省钱档必须选前者，速度档必须选后者。
    # 这一条是整个预设功能的意义所在——两档若选出同一家，按钮就没价值。
    pick_payload = _payload(
        _listing(channel_id=11, input_price_micros=3_000, recent_p50_ms=5_000),
        _listing(channel_id=12, input_price_micros=60_000, recent_p50_ms=500))
    pick_cands, _ = sr.parse_candidates(pick_payload, "m1")
    by_key = {p.key: p for p in sr.WEIGHT_PRESETS}
    picked = {}
    for key in ("money", "balanced", "speed"):
        st_p = sr.RouteSettings(price_weight=by_key[key].price,
                               latency_weight=by_key[key].latency,
                               success_weight=by_key[key].success)
        ranked = sr.rank([_copy(c) for c in pick_cands], st_p)
        picked[key] = ranked[0].channel_id
    check("省钱优先选便宜的慢家", picked["money"] == 11, picked)
    check("速度优先选贵的快家", picked["speed"] == 12, picked)
    check("两档预设结果确实不同", picked["money"] != picked["speed"], picked)

    # 预设本身不带门槛：WeightPreset 只有三项权重，没有第 4 个字段可被顺手改掉
    check("预设结构只含三项权重",
          set(sr.WeightPreset.__dataclass_fields__) ==
          {"key", "name", "note", "price", "latency", "success"},
          sorted(sr.WeightPreset.__dataclass_fields__))

    # ---- 缓存率：决定真实花费的那一项 ----
    # 用户实测踩过的坑：标价便宜但缓存率低的商家，最后账单比标价贵、缓存率高的
    # 商家还要高。实测数据：Ch 2846 标价 0.060 / 缓存 19.4% / 684 次调用；
    # Ch 2925 标价 0.079 / 缓存 90.6% / 208 次调用。
    check("无缓存时实际=标价", abs(sr._eff_ratio(0.06, 9000, 180, 0.0) - 0.06) < 1e-12,
          sr._eff_ratio(0.06, 9000, 180, 0.0))
    eff_high = sr._eff_ratio(0.06, 9000, 180, 0.906)
    check("全命中时接近 2 折的一折", 0.06 * 0.11 < eff_high < 0.06 * 0.13, eff_high)
    check("缓存读价与输入价相同时不给折扣",
          abs(sr._eff_ratio(0.06, 9000, 9000, 0.9) - 0.06) < 1e-12,
          sr._eff_ratio(0.06, 9000, 9000, 0.9))

    cheap_low_cache = sr.ChannelStat(calls=684, fails=0, p50_ms=2000,
                                     tokens_in=158_000_000, cache_tokens=30_000_000)
    dear_high_cache = sr.ChannelStat(calls=208, fails=0, p50_ms=3000,
                                     tokens_in=32_600_000, cache_tokens=29_500_000)
    payload_h = _payload(
        _listing(channel_id=2846, input_price_micros=9_000, cache_read_price_micros=180,
                 recent_p50_ms=2000),
        _listing(channel_id=2925, input_price_micros=11_850, cache_read_price_micros=237,
                 recent_p50_ms=3000))
    by_cache, _ = sr.parse_candidates(payload_h, "m1", sr.RouteSettings(),
                                      {2846: cheap_low_cache, 2925: dear_high_cache})
    ranked_cache = sr.rank(by_cache, sr.RouteSettings(
        price_weight=90, latency_weight=10, success_weight=5))
    check("缓存率高的一方排第一（即使标价更贵）",
          ranked_cache[0].channel_id == 2925,
          [(c.channel_id, round(c.ratio, 3), round(c.eff_ratio, 4)) for c in ranked_cache])
    low = next(c for c in by_cache if c.channel_id == 2846)
    high = next(c for c in by_cache if c.channel_id == 2925)
    check("低缓存家的实际成本高于标价高的一方", low.eff_ratio > high.eff_ratio,
          (low.eff_ratio, high.eff_ratio))
    check("缓存率标为实测", low.cache_known and high.cache_known, (low, high))
    check("状态文案带缓存与实际倍率", "缓存 19%" in low.stat_label, low.stat_label)

    # 冷启动：没有缓存数据时用全局先验，不能当成「零命中」直接淘汰
    prior = sr._cache_prior({2846: cheap_low_cache, 2925: dear_high_cache})
    check("全局先验是加权平均", 0.2 < prior < 0.4, prior)
    cold, _ = sr.parse_candidates(payload_h, "m1", sr.RouteSettings())
    check("冷启动用先验且标为未知", cold and not cold[0].cache_known, cold)
    # 完全没有日志时用默认先验（0.32），而不是 0——0 等于断言「这家从不命中缓存」
    check("完全无数据时用默认先验",
          abs(cold[0].eff_ratio - sr._eff_ratio(0.06, 9_000, 180, sr.CACHE_DEFAULT_PRIOR))
          < 1e-12, (cold[0].eff_ratio, sr.CACHE_DEFAULT_PRIOR))

    # 冷启动（我从没用过这家）才用站点成功率当硬门槛，用过的以实测为准
    cold_bad, _ = sr.parse_candidates(_payload(_listing(recent_success_rate=9000)), "m1")
    check("没用过的商家按站点成功率筛", len(cold_bad) == 0, cold_bad)
    warm_bad, _ = sr.parse_candidates(
        _payload(_listing(channel_id=6490, recent_success_rate=9700)), "m1",
        sr.RouteSettings(), {6490: sr.ChannelStat(calls=60, fails=20, p50_ms=2000)})
    check("老商家不看站点成功率、看实测", len(warm_bad) == 0, warm_bad)

    # 贝叶斯平滑：3 次全成功不该比 200 次全成功更可信
    thin = sr._smoothed_fail_rate(sr.ChannelStat(calls=3, fails=0), 0.05)
    thick = sr._smoothed_fail_rate(sr.ChannelStat(calls=200, fails=0), 0.05)
    check("薄样本被先验拉住", thin > thick, (thin, thick))
    check("无记录时用站点成功率当先验",
          abs(sr._smoothed_fail_rate(None, 0.03) - 0.03) < 1e-12,
          sr._smoothed_fail_rate(None, 0.03))
    check("渠道被禁用淘汰", len(sr.parse_candidates(
        _payload(_listing(supplier_channel_disabled=True)), "m1")[0]) == 0)
    check("价格为 0 淘汰", len(sr.parse_candidates(
        _payload(_listing(input_price_micros=0)), "m1")[0]) == 0)
    check("不是目标模型不计", len(sr.parse_candidates(
        _payload(_listing(model_name="other")), "m1")[0]) == 0)
    check("缺 data 数组报错", _raises(lambda: sr.parse_candidates({}, "m1")))

    # ---- 排序：权重真的起作用 ----
    cheap_slow = _listing(channel_id=1, input_price_micros=30_000, recent_p50_ms=2800)
    dear_fast = _listing(channel_id=2, input_price_micros=110_000, recent_p50_ms=600)
    cands, _ = sr.parse_candidates(_payload(cheap_slow, dear_fast), "m1")
    by_price = sr.rank(list(cands), sr.RouteSettings(
        price_weight=90, latency_weight=10, success_weight=1))
    check("价格权重大时选便宜的", by_price[0].channel_id == 1,
          [c.channel_id for c in by_price])
    by_lat = sr.rank(list(cands), sr.RouteSettings(
        price_weight=1, latency_weight=90, success_weight=1))
    check("延迟权重大时选快的", by_lat[0].channel_id == 2,
          [c.channel_id for c in by_lat])
    # 权重同比例缩放不改变结果（内部按总和折算成占比）
    # 必须用关键字：RouteSettings 的前几个字段是门槛，位置传参会悄悄设错东西。
    a = sr.rank(list(cands), sr.RouteSettings(price_weight=45, latency_weight=35,
                                              success_weight=20))
    b = sr.rank(list(cands), sr.RouteSettings(price_weight=90, latency_weight=70,
                                              success_weight=40))
    check("权重等比缩放结果一致", [c.channel_id for c in a] == [c.channel_id for c in b])

    # 填的是份数，实际权重比 = 各自 ÷ 总和。用性质核对，而不是照着实现再写一遍：
    #   ① 同一组候选，任一权重等比缩放不改变排序；
    #   ② 单项权重压倒性大时，那一项最好的就必须排第一；
    #   ③ 打分用**绝对刻度**：倍率减半必须让成本项跟着减半，不能被同组里
    #      别的商家「拉平」——这是旧版 min-max 的病根。
    triple = _payload(_listing(channel_id=1, input_price_micros=30_000, recent_p50_ms=2800),
                      _listing(channel_id=2, input_price_micros=60_000, recent_p50_ms=1500),
                      _listing(channel_id=3, input_price_micros=100_000, recent_p50_ms=500))
    tri, _ = sr.parse_candidates(triple, "m1")
    for weights in ((100, 100, 100), (50, 35, 15), (7, 11, 13)):
        a = sr.rank([_copy(c) for c in tri], sr.RouteSettings(
            price_weight=weights[0], latency_weight=weights[1], success_weight=weights[2]))
        b = sr.rank([_copy(c) for c in tri], sr.RouteSettings(
            price_weight=weights[0] * 3, latency_weight=weights[1] * 3,
            success_weight=weights[2] * 3))
        check(f"权重等比缩放不改变排序 {weights}",
              [c.channel_id for c in a] == [c.channel_id for c in b],
              ([c.channel_id for c in a], [c.channel_id for c in b]))
    only_price = sr.rank([_copy(c) for c in tri], sr.RouteSettings(
        price_weight=100, latency_weight=1, success_weight=1))
    check("只看价格时最便宜排第一", only_price[0].channel_id == 1,
          [c.channel_id for c in only_price])
    only_lat = sr.rank([_copy(c) for c in tri], sr.RouteSettings(
        price_weight=1, latency_weight=100, success_weight=1))
    check("只看延迟时最快排第一", only_lat[0].channel_id == 3,
          [c.channel_id for c in only_lat])
    # 绝对刻度：两家延迟与成功率完全相同、只有价格差一倍时，得分差就等于
    # 成本项之差；把两家价格同时减半，这个差也必须跟着减半。
    # min-max 归一化做不到这一点——它只看得见「同组内的相对位置」。
    def _rank_of(*listings) -> dict:
        items, _ = sr.parse_candidates(_payload(*listings), "m1")
        return {c.channel_id: c.score for c in sr.rank(items, sr.RouteSettings())}

    base = _rank_of(_listing(channel_id=1, input_price_micros=30_000, recent_p50_ms=1200),
                    _listing(channel_id=2, input_price_micros=100_000, recent_p50_ms=1200))
    # 对数刻度：两家价格同时减半，得分差保持不变（相对差距没变，绝对水平一起平移）
    halved = _rank_of(_listing(channel_id=1, input_price_micros=15_000, recent_p50_ms=1200),
                      _listing(channel_id=2, input_price_micros=50_000, recent_p50_ms=1200))
    check("价格项只看绝对值（等比缩放不改差距）",
          abs((halved[2] - halved[1]) - (base[2] - base[1])) < 1e-9,
          (base[2] - base[1], halved[2] - halved[1]))
    # 绝对刻度：再塞一个极便宜的商家进来，前两家的分数不能被「拉平」
    # （这正是 min-max 的病根：同组里出现一个离群值，其它人的相对差距就被压缩）
    with_outlier = _rank_of(
        _listing(channel_id=1, input_price_micros=30_000, recent_p50_ms=1200),
        _listing(channel_id=2, input_price_micros=100_000, recent_p50_ms=1200),
        _listing(channel_id=3, input_price_micros=2_000, recent_p50_ms=1200))
    check("离群值不改变其它人的打分",
          abs(with_outlier[1] - base[1]) < 1e-9 and abs(with_outlier[2] - base[2]) < 1e-9,
          (base, with_outlier))
    check("空候选不炸", sr.rank([], sr.RouteSettings()) == [])
    # 所有值相同时归一化不能除零
    same, _ = sr.parse_candidates(_payload(_listing(channel_id=3),
                                           _listing(channel_id=4)), "m1")
    check("同值归一化不除零", len(sr.rank(same, sr.RouteSettings())) == 2)

    # ---- 设置持久化 ----
    st = sr.load_settings()
    check("默认倍率门槛 0.8", abs(st.max_ratio - 0.8) < 1e-9, st.max_ratio)
    check("默认权重是整数百分比", (st.price_weight, st.latency_weight, st.success_weight) == (45, 35, 20),
          (st.price_weight, st.latency_weight, st.success_weight))
    st.price_weight, st.max_ratio, st.candidates = 70, 0.6, 5
    sr.save_settings(st)
    back = sr.load_settings()
    check("权重与门槛回读", (back.price_weight, back.max_ratio, back.candidates) == (70, 0.6, 5),
          (back.price_weight, back.max_ratio, back.candidates))

    raw = json.loads((tmp / "routing.json").read_text(encoding="utf-8"))
    raw["settings"]["max_ratio"] = 999        # 越界
    raw["settings"]["candidates"] = "abc"     # 类型错
    (tmp / "routing.json").write_text(json.dumps(raw), encoding="utf-8")
    bad = sr.load_settings()
    check("越界回落默认", abs(bad.max_ratio - 0.8) < 1e-9, bad.max_ratio)
    check("坏类型回落默认", bad.candidates == 8, bad.candidates)

    # 旧版 0–1 比例的权重读盘时自动放大成百分比，不能当成非法值丢掉
    raw["settings"] = {"price_weight": 0.45, "latency_weight": 0.35, "success_weight": 0.20}
    (tmp / "routing.json").write_text(json.dumps(raw), encoding="utf-8")
    old = sr.load_settings()
    check("旧比例权重升级为百分比",
          (old.price_weight, old.latency_weight, old.success_weight) == (45, 35, 20),
          (old.price_weight, old.latency_weight, old.success_weight))

    # 成功率门槛统一按百分比：填 95 就是 95%，旧的 0.95 写法自动升级
    raw["settings"] = {"min_success": 95}
    (tmp / "routing.json").write_text(json.dumps(raw), encoding="utf-8")
    check("填 95 保持 95", abs(sr.load_settings().min_success - 95) < 1e-9,
          sr.load_settings().min_success)
    raw["settings"] = {"min_success": 0.95}
    (tmp / "routing.json").write_text(json.dumps(raw), encoding="utf-8")
    check("旧值 0.95 升级为 95", abs(sr.load_settings().min_success - 95) < 1e-9,
          sr.load_settings().min_success)

    # ---- 凭据：按控制面域名索引，同一站点的不同端点写法都要命中 ----
    check("未配置时返回 None", sr.site_credentials("https://api.x.com/v1") is None)
    sr.set_site_credentials("https://x.com", "tok", "42")
    got = sr.site_credentials("https://api.x.com/v1")
    check("api. 端点命中控制面凭据", got and got["user_id"] == "42", got)
    check("反向写法也命中", (sr.site_credentials("https://x.com/v1") or {}).get("user_id") == "42")
    sr.set_site_credentials("https://x.com/", "tok2", "43")
    check("重复保存不新增站点", len(sr.all_sites()) == 1, sr.all_sites())
    check("覆盖为最新令牌", sr.site_credentials("https://x.com")["access_token"] == "tok2")
    check("别的站点不会被误配", sr.site_credentials("https://nope.example/v1") is None)
    sr.clear_site_credentials("https://x.com")
    check("可清除凭据", sr.all_sites() == [])

    # ---- 旧结构（按 API 地址索引）读盘时自动升级 ----
    legacy = {"version": 1, "sites": {
        "https://api.legacy.com/v1": {"control_base": "https://legacy.com",
                                      "access_token": "old", "user_id": "7"}}}
    (tmp / "routing.json").write_text(json.dumps(legacy), encoding="utf-8")
    migrated = sr.load_config()
    check("旧键升级为控制面域名", list(migrated["sites"]) == ["https://legacy.com"],
          list(migrated["sites"]))
    check("升级后可被端点命中", (sr.site_credentials("https://api.legacy.com/v1") or {})
          .get("user_id") == "7")

    # ---- 延迟文案统一用秒 ----
    check("秒文案保留两位", sr.secs(2150) == "2.15 s", sr.secs(2150))
    check("整秒也带小数位", sr.secs(3000) == "3.00 s", sr.secs(3000))
    check("没有值显示破折号", sr.secs(None) == "—", sr.secs(None))
    check("非法值不炸", sr.secs("x") == "—", sr.secs("x"))

    # ---- 路由成功后的写价：商户输入价 → 元 / 百万 token ----
    # 价格表指到临时文件，绝不碰用户真正的 prices.json
    os.environ["MODEL_PROBE_PRICE_FILE"] = str(tmp / "prices.json")
    from modelkeeper import price_book as pb
    pb.load_preset.cache_clear()
    cand = next(c for c in cands if c.channel_id == 1) if cands else None
    cand = sr.Candidate(channel_id=7, supplier_id=9, channel_name="Ch 7",
                        supplier_name="Sup 9", input_micros=6240,
                        official_input_micros=150_000, output_micros=24_960,
                        ratio=0.0416, p50_ms=1200, success_rate=0.99, sample_count=99)
    written = sr.record_price("deepseek-v4.1-flash-1017", cand)
    expect = round(6240 / 1_000_000 * pb.fx(), 4)
    check("写入值按 micros 换算", written == expect, (written, expect))
    table = pb.load()
    check("价格表里出现该模型",
          table["models"].get("deepseek-v4.1-flash-1017", {}).get("in") == expect,
          table["models"].get("deepseek-v4.1-flash-1017"))
    check("价格表只存输入价一个键",
          set(table["models"]["deepseek-v4.1-flash-1017"]) == {"in"},
          table["models"]["deepseek-v4.1-flash-1017"])
    check("查价立刻能查到", pb.lookup("deepseek-v4.1-flash-1017").in_ == expect,
          pb.lookup("deepseek-v4.1-flash-1017").in_)

    # 价格表坏掉时宁可这次不记，也不能用一条记录覆盖整张表
    (tmp / "prices.json").write_text("{broken", encoding="utf-8")
    check("坏价格表不写入", sr.record_price("m9", cand) == expect
          and (tmp / "prices.json").read_text(encoding="utf-8") == "{broken")

    # ---- route：连续校验次数决定「要不要固定这一家」 ----
    # 用户实际遇到的坑：只测一次就固定，坏商家「碰巧通过一次」之后每次真实调用都失败。
    # 这里把 pin 与实测全部换成假函数，验证三件事：
    #   ① 第 1 次通过、第 2 次失败 → 这家作废，继续看下一家；
    #   ② 某个商家连续两次都过 → 固定它，并说明「连续 N 次通过」；
    #   ③ 传给实测的输出预算不低于 64、且带上了期望词（否则推理模型测不出正文）。
    from modelkeeper import model_probe as mp

    orig = (sr.fetch_channels, sr.pin_channel, sr.unpin_channel, sr.list_pins,
            mp.probe_speed, sr.record_price, sr.merchant_stats,
            sr.recent_channel_faults)
    pinned: list[int] = []
    probes: list[dict] = []
    script: list[bool] = []          # 依次返回的「这一测是否通过」

    def fake_fetch(control, token, uid, model):
        return _payload(_listing(channel_id=1, recent_p50_ms=800),
                        _listing(channel_id=2, recent_p50_ms=900))

    def fake_pin(control, token, uid, *, token_id, model_name, channel_id, fallback):
        pinned.append(int(channel_id))

    def fake_unpin(control, token, uid, *, token_id, model_name, channel_id):
        pinned.append(-int(channel_id))

    def fake_probe(protocol, base, key, model, **kw):
        probes.append(kw)
        ok = script.pop(0) if script else True
        return mp.SpeedResult(model=model, rounds=1 if ok else 0,
                              ttft_ms=1200 if ok else None,
                              content="可用" if ok else "",
                              error="" if ok else "模型把错误信息当正文返回：上游服务器返回了无效响应，请重试")

    sr.fetch_channels, sr.pin_channel = fake_fetch, fake_pin
    sr.unpin_channel, sr.list_pins = fake_unpin, (lambda *a, **k: [])
    mp.probe_speed = fake_probe
    sr.record_price = lambda model, cand: None
    # 站点日志是网络调用，单测里一律假装「没有故障记录」，需要时单独注入
    sr.merchant_stats = lambda *a, **k: {}
    sr.recent_channel_faults = lambda *a, **k: {"fails": 0, "rows": 0, "code": ""}
    sr.clear_bad_channels()
    try:
        # ① 第一家第一次过、第二次挂 → 换第二家；第二家连续两次过
        script[:] = [True, False, True, True]
        pinned.clear()
        probes.clear()
        stages: list[str] = []
        out = sr.route(api_base="https://x/v1", control_base="https://x",
                       access_token="t", user_id="1", token_id=7, api_key="k",
                       model_name="m1", settings=sr.RouteSettings(attempts=2),
                       on_stage=stages.append)
        check("首测通过但复测失败时不固定该家", out.ok and out.candidate.channel_id == 2,
              (out.ok, out.candidate.channel_id if out.candidate else None, out.message))
        check("失败的商家仍然被 pin 过（先固定后测）", pinned[:2] == [1, 2], pinned)
        check("结果说明采样次数", "2/2 次采样通过" in out.message, out.message)
        # 2 次是「流式长 + 流式短」，两档全测到了（但都是流式）
        check("采样覆盖说明写明流式", "流式长/短输出都验过" in out.message, out.message)
        check("采样阶段有进度提示", any("继续采样" in x for x in stages), stages)

        samples, finals = _split(probes)
        # ② 每次实测都带期望词、预算不低于 64，且两次都走流式（长/短不同）
        # 长输出那一路刻意不要求复述 PONG（让它专心数数，才能真的跨帧/被掐断）
        short_samples = [p for p in probes
                         if p.get("prompt") != sr.VERIFY_LONG_PROMPT]
        check("短输出实测带期望词", bool(short_samples) and
              all(p.get("expect") == sr.VERIFY_WORD for p in short_samples),
              [p.get("expect") for p in probes])
        check("输出预算至少 64", all(p.get("max_tokens", 0) >= 64 for p in probes),
              [p.get("max_tokens") for p in probes])
        check("首次走流式·长输出", samples[0].get("force_buffered") is False
              and samples[0].get("prompt") == sr.VERIFY_LONG_PROMPT
              and samples[0].get("max_tokens", 0) >= sr.VERIFY_LONG_TOKENS, samples[0])
        check("复测走流式·短输出", samples[1].get("force_buffered") is False
              and samples[1].get("expect") == sr.VERIFY_WORD, samples[1])
        check("两次采样都不走非流式", all(p.get("force_buffered") is False for p in samples),
              [p.get("force_buffered") for p in samples])
        check("每个候选都测两次", len(samples) == 4, (len(samples), len(probes)))
        # 终检：只有通过的候选会被终检一次，且必须带 system + tools + 长上下文
        check("通过后做一次终检", len(finals) == 1, len(finals))
        check("终检带工具定义", bool(finals[0].get("tools")), finals[0].get("tools"))
        check("终检带 system 提示", bool(finals[0].get("system")), finals[0].get("system"))
        check("终检上下文明显更大", len(finals[0].get("prompt") or "")
              > 4 * len(sr.VERIFY_FINAL_PROMPT), len(finals[0].get("prompt") or ""))
        check("终检结果写进说明", "真实形态终检通过" in out.message, out.message)

        # ③ 全部候选都过不了 → 不固定、不返回候选
        script[:] = [False, False, False, False]
        pinned.clear()
        out = sr.route(api_base="https://x/v1", control_base="https://x",
                       access_token="t", user_id="1", token_id=7, api_key="k",
                       model_name="m1", settings=sr.RouteSettings(attempts=2))
        check("全败时不返回候选", not out.ok and out.candidate is None, out.message)
        # pin 是 (令牌, 模型) 全局唯一：测第二家时就顶掉了第一家，所以收尾只要
        # unpin 最后那一家就能清干净。这里断言「确实清了」，而不是「没 pin 过」。
        check("全败时清掉了固定", pinned and pinned[-1] < 0 and out.restored, (pinned, out.restored))

        # ④ attempts=1 时不该多测（只测一次是用户可选的省钱档）
        script[:] = [True]
        probes.clear()
        sr.route(api_base="https://x/v1", control_base="https://x",
                 access_token="t", user_id="1", token_id=7, api_key="k",
                 model_name="m1", settings=sr.RouteSettings(attempts=1))
        s1, f1 = _split(probes)
        check("attempts=1 只测一次", len(s1) == 1, (len(s1), len(probes)))
        check("attempts=1 也要终检", len(f1) == 1, len(f1))

        # ⑤ 采样 4 次：两档流式形态循环，长输出那一路不能被省掉，也不冒出非流式
        sr.clear_bad_channels()
        script[:] = [True, True, True, True]
        probes.clear()
        out4 = sr.route(api_base="https://x/v1", control_base="https://x",
                        access_token="t", user_id="1", token_id=7, api_key="k",
                        model_name="m1", settings=sr.RouteSettings(attempts=4))
        s4, _f4 = _split(probes)
        check("采样四次", len(s4) == 4, (len(s4), len(probes)))
        check("四次都是流式", [p.get("force_buffered") for p in s4] ==
              [False, False, False, False], [p.get("force_buffered") for p in s4])
        check("长/短输出交替", [p.get("prompt") == sr.VERIFY_LONG_PROMPT for p in s4]
              == [True, False, True, False], [p.get("prompt") for p in s4])
        check("长输出带最小长度", [p.get("min_chars") for p in s4][0] ==
              sr.VERIFY_LONG_MIN_CHARS, [p.get("min_chars") for p in s4])
        check("长输出不要求复述 PONG", s4[0].get("expect") == "", s4[0])
        check("采样四次写清覆盖范围", "流式长/短输出都验过" in out4.message, out4.message)

        # ⑤b 采样和终检都过，但日志复核发现固定之后真实调用还在失败 → 换下一家
        # （这正是「智能测试通过、实际用不了」那一类故障的兜底）
        sr.clear_bad_channels()
        script[:] = [True, True, True, True]
        pinned.clear()
        probes.clear()
        confirms = [{"fails": 1, "rows": 2, "code": "upstream_http"},
                    {"fails": 0, "rows": 3, "code": ""}]
        sr.recent_channel_faults = lambda *a, **k: dict(
            confirms.pop(0) if len(confirms) > 1 else confirms[0])
        out_log = sr.route(api_base="https://x/v1", control_base="https://x",
                           access_token="t", user_id="1", token_id=7, api_key="k",
                           model_name="m1", settings=sr.RouteSettings())
        check("日志复核失败时换家", out_log.ok and out_log.candidate.channel_id == 2,
              (out_log.ok, out_log.candidate.channel_id if out_log.candidate else None))
        check("日志复核失败原因写进 tested",
              any("站点日志复核" in v for _c, v, _t in out_log.tested), out_log.tested)
        check("日志复核通过写进说明", "站点日志复核通过" in out_log.message, out_log.message)
        sr.recent_channel_faults = lambda *a, **k: {"fails": 0, "rows": 0, "code": ""}

        # ⑥ 站点日志说「这家刚失败过」→ 直接跳过，不拿用户的钱去试
        sr.clear_bad_channels()
        sr.merchant_stats = lambda *a, **k: {
            1: sr.ChannelStat(calls=40, fails=3, p50_ms=1500,
                              last_fail=int(time.time()) - 60,
                              last_code="no_healthy_account")}
        script[:] = [True, True, True, True]
        pinned.clear()
        probes.clear()
        out5 = sr.route(api_base="https://x/v1", control_base="https://x",
                        access_token="t", user_id="1", token_id=7, api_key="k",
                        model_name="m1", settings=sr.RouteSettings(attempts=4))
        check("按站点日志跳过故障商家", out5.ok and out5.candidate.channel_id == 2,
              (out5.ok, out5.candidate.channel_id if out5.candidate else None))
        check("被跳过的不再 pin", pinned == [2], pinned)
        check("跳过原因写进结果", any("no_healthy_account" in x
                                 for x in out5.unhealthy_skipped),
          out5.unhealthy_skipped)

        # ⑦ 全都被标记过 → 不许空转，照样逐家实测
        sr.merchant_stats = lambda *a, **k: {
            1: sr.ChannelStat(calls=30, fails=1, p50_ms=1500,
                              last_fail=int(time.time()) - 60),
            2: sr.ChannelStat(calls=30, fails=1, p50_ms=1600,
                              last_fail=int(time.time()) - 60)}
        script[:] = [True, True, True, True]
        pinned.clear()
        out6 = sr.route(api_base="https://x/v1", control_base="https://x",
                        access_token="t", user_id="1", token_id=7, api_key="k",
                        model_name="m1", settings=sr.RouteSettings(attempts=4))
        check("全被标记时仍实测", out6.ok, out6.message)
        sr.merchant_stats = lambda *a, **k: {}

        # ⑧ 校验失败的商家写进本地名单，下次直接跳过；过期记录自动失效
        sr.clear_bad_channels()
        script[:] = [False]
        probes.clear()
        sr.route(api_base="https://x/v1", control_base="https://x",
                 access_token="t", user_id="1", token_id=7, api_key="k",
                 model_name="m1", settings=sr.RouteSettings(attempts=1))
        bad = sr.load_bad_channels("https://x", "m1")
        check("失败商家进入本地名单", bool(bad), bad)
        check("本地名单带失败次数", all(v.get("fails", 0) >= 1 for v in bad.values()), bad)
        data = sr.load_config()
        data.setdefault("bad", {})[sr._bad_key("https://x", "m1")] = {
            "99": {"ts": 0, "fails": 5}}
        sr.save_config(data)
        check("过期记录自动失效", 99 not in sr.load_bad_channels("https://x", "m1"))
        sr.clear_bad_channels()

        # ⑨ 限流 ≠ 商家坏了：实测同一家商家零间隔连发立刻 429、隔 6 秒就正常
        check("429 判为限流",
              sr.is_throttled(mp.SpeedResult(model="m", rounds=0,
                                             error="被限流（HTTP 429），稍后重试")))
        check("站点的「容量不足」文案也算限流",
              sr.is_throttled(mp.SpeedResult(model="m", rounds=0,
                                             content="该商家上游正在限流或容量不足")))
        check("502 不算限流",
              not sr.is_throttled(mp.SpeedResult(model="m", rounds=0,
                                                 error="网关错误 HTTP 502")))

        old_backoff = sr.THROTTLE_BACKOFF_S
        sr.THROTTLE_BACKOFF_S = 0        # 单测别真等 3 秒
        try:
            seq = [mp.SpeedResult(model="m1", rounds=0, error="被限流（HTTP 429）")]
            calls_t: list[dict] = []

            def throttled_then_ok(protocol, base, key, model, **kw):
                calls_t.append(kw)
                if seq:
                    return seq.pop(0)
                return mp.SpeedResult(model=model, rounds=1, ttft_ms=900, content="PONG")

            mp.probe_speed = throttled_then_ok
            sr.clear_bad_channels()
            pinned.clear()
            outT = sr.route(api_base="https://x/v1", control_base="https://x",
                            access_token="t", user_id="1", token_id=7, api_key="k",
                            model_name="m1", settings=sr.RouteSettings(attempts=1))
            # 1 次被限流 + 1 次退避重测 + 1 次终检
            check("被限流后退避重测一次", len(calls_t) == 3, len(calls_t))
            check("退避后通过就仍然固定", outT.ok, outT.message)

            # 一直限流 → 判这次路由失败，但不能把这几个商家记成「坏」
            sr.clear_bad_channels()
            mp.probe_speed = lambda *a, **k: mp.SpeedResult(
                model="m1", rounds=0, error="被限流（HTTP 429），请降低并发")
            outL = sr.route(api_base="https://x/v1", control_base="https://x",
                            access_token="t", user_id="1", token_id=7, api_key="k",
                            model_name="m1", settings=sr.RouteSettings(attempts=1))
            check("一直限流则不返回候选", not outL.ok, outL.message)
            check("限流不写进本地坏名单",
                  not sr.load_bad_channels("https://x", "m1"),
                  sr.load_bad_channels("https://x", "m1"))

            # ⑩ 终检（带 tools）不过 → 不固定这家，继续下一个候选
            flag = {"n": 0}

            def final_fails_once(protocol, base, key, model, **kw):
                if kw.get("tools") and flag["n"] == 0:
                    flag["n"] = 1
                    return mp.SpeedResult(model=model, rounds=0,
                                          error="HTTP 400: tools is not supported")
                return mp.SpeedResult(model=model, rounds=1, ttft_ms=800, content="PONG")

            mp.probe_speed = final_fails_once
            sr.clear_bad_channels()
            pinned.clear()
            outF = sr.route(api_base="https://x/v1", control_base="https://x",
                            access_token="t", user_id="1", token_id=7, api_key="k",
                            model_name="m1", settings=sr.RouteSettings(attempts=1))
            check("终检不过就换下一家", outF.ok and outF.candidate.channel_id == 2,
                  (outF.ok, outF.candidate.channel_id if outF.candidate else None))
            check("终检失败原因写进候选记录",
                  any("function calling" in v for _c, v, _h in outF.tested),
                  [v for _c, v, _h in outF.tested])

            # ⑪ 终检失败的原因要说得具体：换成人话解释「为什么不固定它」
            mp.probe_speed = lambda *a, **k: mp.SpeedResult(
                model="m1", rounds=0, error="HTTP 400: tools are not supported by this model")
            ok_f, why_f = sr.final_check("openai", "https://x/v1", "k", "m1",
                                         sr.RouteSettings())
            check("tools 400 说成「不支持 function calling」",
                  not ok_f and "function calling" in why_f, why_f)

            mp.probe_speed = lambda *a, **k: mp.SpeedResult(
                model="m1", rounds=0,
                error="HTTP 400: maximum context length is 8192 tokens")
            ok_c, why_c = sr.final_check("openai", "https://x/v1", "k", "m1",
                                         sr.RouteSettings())
            check("上下文超限说成「上下文不够」",
                  not ok_c and "上下文" in why_c, why_c)

            mp.probe_speed = lambda *a, **k: mp.SpeedResult(
                model="m1", rounds=0, error="被限流（HTTP 429）")
            ok_t, why_t = sr.final_check("openai", "https://x/v1", "k", "m1",
                                         sr.RouteSettings())
            check("终检被限流时点明是暂时性的",
                  not ok_t and "暂时性" in why_t, why_t)

            # ⑫ 带 tools 时模型「不回答、直接调用工具」→ 正文为空。
            # 去掉工具再确认，通了就算过：这种商家是能用，只是太听话。
            seen_tools: list[bool] = []

            def tool_acting(protocol, base, key, model, **kw):
                seen_tools.append(bool(kw.get("tools")))
                if kw.get("tools"):
                    return mp.SpeedResult(model=model, rounds=0,
                                          error="流式响应里没有任何增量（空响应）")
                return mp.SpeedResult(model=model, rounds=1, ttft_ms=500, content="PONG")

            mp.probe_speed = tool_acting
            ok_p, why_p = sr.final_check("openai", "https://x/v1", "k", "m1",
                                         sr.RouteSettings())
            check("带工具空正文会去掉工具再试", ok_p and not why_p, (ok_p, why_p))
            check("确实先试了带工具、再试不带", seen_tools == [True, False], seen_tools)

            # 上下文填充要真的够大：太小就逼不出「窗口比宣传的小」的商家
            check("终检填充够 8 千字",
                  len(sr._final_filler()) >= sr.VERIFY_FINAL_FILLER_CHARS,
                  len(sr._final_filler()))
        finally:
            sr.THROTTLE_BACKOFF_S = old_backoff
    finally:
        (sr.fetch_channels, sr.pin_channel, sr.unpin_channel, sr.list_pins,
         mp.probe_speed, sr.record_price, sr.merchant_stats,
         sr.recent_channel_faults) = orig

    # ---- 日志复核的字段口径：两套失败标记都要认，且只认指定渠道/模型 ----
    orig_pages = sr._log_pages
    try:
        now = int(time.time())
        diag = {"_merchant_diag_v1": {"reason": "upstream_http",
                                      "completion": "not_complete",
                                      "upstream_status": 500},
                "actual_channel_id": 7, "request_final_status": "failed",
                "error_code": "unknown_error"}
        sr._log_pages = lambda *a, **k: [{"data": {"items": [
            {"created_at": now, "model_name": "m1", "channel_id": 7,
             "other": json.dumps(diag)},
            {"created_at": now - 1, "model_name": "m1", "channel_id": 7,
             "other": json.dumps({"actual_channel_id": 7,
                                  "request_final_status": "success"})},
            {"created_at": now, "model_name": "m2", "channel_id": 7,
             "other": json.dumps(diag)},
            {"created_at": now, "model_name": "m1", "channel_id": 8,
             "other": json.dumps({**diag, "actual_channel_id": 8})},
        ]}}]
        got = sr.recent_channel_faults("https://x", "t", "1", "m1", 7)
        check("日志复核认上游 500 诊断", got["fails"] == 1 and got["rows"] == 2, got)
        check("日志复核带出错误码", got["code"] == "unknown_error", got)
        check("日志复核只算指定渠道模型", got["success"] == 1, got)
        # since 之后才发生的才算：把所有日志时间放到固定时刻之前
        got2 = sr.recent_channel_faults("https://x", "t", "1", "m1", 7,
                                        since=now + 60)
        check("日志复核不翻固定前的旧账", got2["fails"] == 0, got2)
        # 老站点没有诊断块，靠 failure_owner 也要认
        sr._log_pages = lambda *a, **k: [{"data": {"items": [
            {"created_at": now, "model_name": "m1", "channel_id": 7,
             "other": json.dumps({"actual_channel_id": 7,
                                  "failure_owner": "merchant",
                                  "error_code": "no_healthy_account"})},
        ]}}]
        got3 = sr.recent_channel_faults("https://x", "t", "1", "m1", 7)
        check("日志复核认 failure_owner", got3["fails"] == 1
              and got3["code"] == "no_healthy_account", got3)
    finally:
        sr._log_pages = orig_pages

    # ---- 守护：固定之后的体检判定 ----
    # 这是「选中的商家过一会儿又坏掉」唯一能自动补救的一环：连续两次不通就换家，
    # 换完进入冷却期，避免在商家成片故障时反复花钱实测。
    h = sr.PinHealth(need=2)
    check("体检通过清零", h.record("k", True, now=0) == "ok")
    check("首次失败只观察", h.record("k", False, now=10) == "watch")
    check("连续失败触发换家", h.record("k", False, now=20) == "reroute")
    check("冷却期内不重复换家",
          h.record("k", False, now=30) == "watch"
          and h.record("k", False, now=40) == "cooldown",
          h.record("k", False, now=40))
    check("冷却结束后可以再换", h.record("k", False, now=20 + sr.WATCH_COOLDOWN_S + 1) == "reroute")
    check("换家后计数归零", h.record("k", False, now=99999) == "watch")
    h.forget("k")
    check("forget 后从零开始", h.record("k", False, now=100000) == "watch")

    # ---- 采样次数：默认 2，且旧 schema 上的旧默认值只迁移一次 ----
    check("采样默认 2 次", sr.RouteSettings().attempts == 2,
          sr.RouteSettings().attempts)
    st_a = sr.load_settings()
    st_a.attempts = 4
    sr.save_settings(st_a)
    # 存盘时 schema 已是当前版本，所以「特意设的 4」不会再被改回默认
    check("新 schema 下 4 保持不变", sr.load_settings().attempts == 4,
          sr.load_settings().attempts)

    def _fake_version(version: int, attempts: int) -> None:
        """伪造一份旧 schema 的配置：先存成当前版本再改回 version。"""
        data = sr.load_config()
        data["settings"]["attempts"] = attempts
        sr.save_config(data)                 # 会写成当前版本
        raw = json.loads(sr.route_path().read_text(encoding="utf-8"))
        raw["version"] = version
        sr.route_path().write_text(json.dumps(raw), encoding="utf-8")

    # v3 的默认值就是 3：读到 3 说明用户没动过 → 落到新默认 2
    _fake_version(3, 3)
    check("v3 的默认 3 迁移到 2", sr.load_settings().attempts == 2,
          sr.load_settings().attempts)
    check("迁移后版本号写成 4",
          json.loads(sr.route_path().read_text(encoding="utf-8"))["version"] == 4,
          json.loads(sr.route_path().read_text(encoding="utf-8"))["version"])

    # v3 上用户特意设的 1（省钱档）不该被动
    _fake_version(3, 1)
    check("v3 特意设的 1 保持不变", sr.load_settings().attempts == 1,
          sr.load_settings().attempts)

    # 更旧 schema（version=2）的 4 也是当年没动过的默认值 → 迁移到 2
    _fake_version(2, 4)
    check("旧 schema 的 4 迁移到 2", sr.load_settings().attempts == 2,
          sr.load_settings().attempts)

    # 体检间隔要能存盘（0 = 关闭）
    st_w = sr.load_settings()
    st_w.watch_interval_s = 0
    sr.save_settings(st_w)
    check("体检间隔可关闭", sr.load_settings().watch_interval_s == 0)
    st_w.watch_interval_s = 300
    sr.save_settings(st_w)
    check("体检间隔可回读", sr.load_settings().watch_interval_s == 300)

    # ---- 内容校验：HTTP 200 但吐错误文本 / 空正文都要拦下 ----
    check("空正文判不合格", "空正文" in mp.check_content("", "可用"))
    check("错误文本判不合格",
          "错误信息" in mp.check_content("上游服务器返回了无效响应或响应格式不可用，请重试", "可用"))
    check("正常回答通过", mp.check_content("可用", "可用") == "")
    check("不含期望词判不合格", "预期不符" in mp.check_content("好的", "可用"))
    check("大小写不敏感", mp.check_content("OK", "ok") == "")
    check("不传期望词也拦错误文本",
          "错误信息" in mp.check_content("rate limit exceeded"))

    # ---- 旧配置的 16 token 预算会被抬到 64 ----
    raw = json.loads((tmp / "routing.json").read_text(encoding="utf-8"))
    raw["settings"] = {"max_tokens": 16}
    (tmp / "routing.json").write_text(json.dumps(raw), encoding="utf-8")
    check("旧预算 16 → 64", sr.load_settings().max_tokens >= 64,
          sr.load_settings().max_tokens)

    print()
    print("RESULT:", "ALL PASS" if not check.failed else f"{check.failed} FAILED")


def _raises(fn) -> bool:
    try:
        fn()
    except sr.RouteError:
        return True
    return False


if __name__ == "__main__":
    main()
