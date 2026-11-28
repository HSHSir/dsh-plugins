#!/usr/bin/env python3
"""打分实验室：拿真实行情数据核对智能路由的打分公式。

## 这份数据从哪来

中转站「模型广场 → deepseek-v4.1-flash-1017」页面（2026-11-15 17:25 截图），
每行一个渠道，抄下四列：输入价、缓存率、成功率、延迟（另有站点自己算的
「实际倍率」和并发数）。官方输入价取 0.15 元/百万 token（站点 listing 的
official_price.input_price_micros = 150000），缓存读价按各家的 2% 计。

## 为什么要这个脚本

打分公式涉及对数刻度、延迟封顶、失败率、三项权重折算，光看代码很难建立直觉。
这个脚本把「同一批商家、不同权重」下的每一分都是怎么来的摊开印出来，
用来回答一个问题：**选出来的那家，是不是我想要的那家**。

关键一点：打分不在这里重写，而是直接调 `smart_route.rank()`——重写一份
「看起来一样」的公式，等于在核对一个影子实现，那天改了真代码这里也不会变。

## 用法

    py -3.14 scripts/score_lab.py                     # 交互：反复输入三权重，空行退出
    py -3.14 scripts/score_lab.py 85 10 5             # 价格 / 延迟 / 成功率
    py -3.14 scripts/score_lab.py money               # 预设：money / balanced / speed
    py -3.14 scripts/score_lab.py 50 50 20 --lat 2    # 改延迟参考刻度（秒）
    py -3.14 scripts/score_lab.py 50 50 20 --ratio 0.5  # 改倍率上限
    py -3.14 scripts/score_lab.py 45 35 20 --sweep    # 附加：权重扫描，看谁在哪一段赢
    py -3.14 scripts/score_lab.py --set 1711          # 换一份截图快照（默认用最新那份）
    py -3.14 scripts/score_lab.py 45 35 20 --drop 5343  # 假设某家不可用，看下一层怎么排
"""
from __future__ import annotations
# 直接运行本文件时，把项目根目录加入 sys.path（无需安装成包）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import math
import unicodedata
from dataclasses import dataclass

from modelkeeper import smart_route as sr

# 官方价（元 / 百万 token），用于把「输入价」折成倍率。
# 与 price_book 里 deepseek-v4.1-flash-1017 的官方输入价一致。
OFFICIAL_IN = 0.15
# 缓存读价占输入价的比例：223 条 listing 全是 2%，与官方同构。
CACHE_READ_RATIO = 0.02


@dataclass(frozen=True)
class Row:
    """截图里的一行。价格与延迟都是站点显示的原值，不做任何加工。"""

    channel: int
    price: float                    # 输入价，元/百万 token
    site_ratio: float               # 站点自己算的「实际倍率」（含输出价，仅供对照）
    success: float                  # 成功率（取 24h 那一档），百分比
    latency_s: float                # 延迟，秒
    cache: float | None             # 缓存率，百分比；None = 站点这一格是「—」
    slots: str                      # 并发，形如 "209/1000"、"-" 表示不限制
    note: str = ""


# 每份快照对应一次截图。渠道集合天天变，所以把快照留下来：想对比「昨天那张图」
# 重新跑一遍就行，不用再翻截图。《--set》选，默认用最新那份。
SNAPSHOTS: dict[str, tuple[str, tuple[Row, ...]]] = {
    "1750": ("2026-11-15 17:50 截图（12 家）", (
        Row(7081, 0.3603, 2.4019, 100.0, 0.516, 61.08, "0/50", "500tps，比官方都高"),
        Row(5343, 0.0080, 0.0534, 92.3, 0.384, 82.01, "9/-", "280k 上下文"),
        Row(6799, 0.1441, 0.9608, 100.0, 0.855, 84.58, "44/1000", "并发稳定"),
        Row(6295, 0.0721, 0.4804, 98.9, 1.21, 16.14, "2/-", "固模开发"),
        Row(7024, 0.1065, 0.7899, 99.3, 1.32, 95.98, "1/-", "原厂直连"),
        Row(6775, 0.1201, 0.8086, 2.6, 1.33, 2.12, "0/-", "国模稳定分组，实测常挂"),
        Row(7006, 0.0986, 0.6571, 98.0, 1.33, 96.40, "4/-", "满血低价通道"),
        Row(6038, 0.0600, 0.4003, 99.6, 1.40, 7.91, "17/-", "Pentest / bot"),
        Row(4866, 0.0780, 0.5201, 0.0, 1.60, None, "0/-", "高速稳定，24h 全挂"),
        Row(6852, 0.0656, 0.4376, 76.5, 1.60, 47.33, "1/-", "Stable supply"),
        Row(2925, 0.0745, 0.4964, 99.8, 1.74, 92.59, "42/-", "DeepSeek 零吞"),
        Row(2846, 0.0600, 0.4003, 99.3, 1.74, 15.38, "3/-", "Low Cache"),
    )),
    "1711": ("2026-11-15 17:11 截图（10 家）", (
        Row(7081, 0.3683, 2.4819, 100.0, 0.502, 43.35, "0/50", "500tps，比官方都高"),
        Row(5276, 0.0872, 0.7848, 95.1, 11.20, 76.96, "71/-", "260k 上下文"),
        Row(7089, 0.0440, 0.2936, 99.8, 2.12, 59.07, "23/-", ""),
        Row(6932, 0.0456, 0.3042, 99.2, 1.78, 87.52, "209/1000", "最便宜，最稳，缓存最多"),
        Row(7074, 0.0456, 0.3042, 96.0, 3.49, 62.44, "0/100", "workbuddy 千号直营"),
        Row(7076, 0.0464, 0.3896, 97.2, 5.65, 76.59, "7/100", "千号 workbuddy 欢迎"),
        Row(6769, 0.0480, 0.3283, 99.6, 2.49, 25.56, "32/-", "默认无痕"),
        Row(6780, 0.0525, 0.3581, 96.5, 7.76, 54.63, "303/100", "wb"),
        Row(5192, 0.0528, 0.3523, 98.5, 8.76, 73.97, "14/-", "固模 1017，稳定，高并发"),
        Row(6838, 0.0528, 0.3523, 96.0, 2.28, 27.28, "0/30", "低价高量，低延迟"),
    )),
}

DEFAULT_SNAPSHOT = "1750"
DATA: tuple[Row, ...] = SNAPSHOTS[DEFAULT_SNAPSHOT][1]


def build_candidates(rows: "tuple[Row, ...] | None" = None) -> list[sr.Candidate]:
    """把截图数据装成 Candidate，字段语义与 route() 里完全一致。

    只填打分用得到的字段。`fail_rate` 直接取 1−成功率（截图里没有我自己
    在这家身上的调用流水，所以没有「实测失败率」可用）；真跑路由时这一项
    来自 /api/log/self，比站点统计可信得多。

    缓存率那一格是「—」时按全局先验估（与路由器同一套 `CACHE_DEFAULT_PRIOR`），
    并标成 `cache_known=False`：把它当成 0% 会冤枉这家、当成 100% 会美化它。
    """
    out: list[sr.Candidate] = []
    for row in (DATA if rows is None else rows):
        micros = int(round(row.price * 1_000_000))
        cache_read = int(round(micros * CACHE_READ_RATIO))
        ratio = row.price / OFFICIAL_IN
        p50_ms = int(round(row.latency_s * 1000))
        rate = row.success / 100.0
        known = row.cache is not None
        cache_rate = (row.cache / 100.0) if known else sr.CACHE_DEFAULT_PRIOR
        out.append(sr.Candidate(
            channel_id=row.channel,
            supplier_id=0,
            channel_name="",
            supplier_name=row.note or f"Ch {row.channel}",
            input_micros=micros,
            official_input_micros=int(round(OFFICIAL_IN * 1_000_000)),
            output_micros=0,
            ratio=ratio,
            p50_ms=p50_ms,
            success_rate=rate,
            sample_count=1000,
            fail_rate=1.0 - rate,
            eff_p50_ms=p50_ms,
            cache_read_micros=cache_read,
            cache_rate=cache_rate,
            cache_known=known,
            eff_ratio=sr._eff_ratio(ratio, micros, cache_read, cache_rate),
        ))
    return out


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #

def _width(text: str) -> int:
    """终端显示宽度：中日韩字符占两格。不这么算，表格会一路歪到右边。"""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in text)


def pad(text: str, width: int, align: str = "<") -> str:
    gap = max(width - _width(text), 0)
    if align == ">":
        return " " * gap + text
    return text + " " * gap


def _table(headers: list[tuple[str, int, str]], rows: list[list[str]]) -> str:
    """按 (标题, 宽度, 对齐) 画一张等宽表。"""
    head = "  ".join(pad(t, w, a) for t, w, a in headers)
    line = "─" * _width(head)
    body = []
    for cells in rows:
        body.append("  ".join(pad(c, w, a) for c, (_t, w, a) in zip(cells, headers)))
    return "\n".join([head, line, *body])


def print_source_table(rows: "tuple[Row, ...] | None" = None,
                       label: str = "") -> None:
    print(f"\n【一】截图里的原始数据（价格 / 延迟 / 缓存率）{'　' + label if label else ''}")
    print("     输入价单位：元/百万 token；官方输入价 0.15，缓存读价按输入价的 2%")
    table = []
    for row in (DATA if rows is None else rows):
        micros = int(round(row.price * 1_000_000))
        ratio = row.price / OFFICIAL_IN
        known = row.cache is not None
        cache_pct = row.cache if known else sr.CACHE_DEFAULT_PRIOR * 100
        eff = sr._eff_ratio(ratio, micros, int(round(micros * CACHE_READ_RATIO)),
                            cache_pct / 100.0)
        table.append([
            f"Ch {row.channel}",
            f"{row.price:.4f}",
            f"{ratio:.4f}",
            f"{row.site_ratio:.4f}",
            f"{cache_pct:.2f}%" if known else "— *",
            f"{eff:.4f}",
            f"{row.latency_s:.2f}",
            f"{row.success:.1f}%",
            row.slots,
        ])
    print(_table(
        [("渠道", 9, "<"), ("输入价", 7, ">"), ("标价倍率", 9, ">"),
         ("站点倍率", 9, ">"), ("缓存率", 8, ">"), ("实际成本", 9, ">"),
         ("延迟(s)", 7, ">"), ("成功率", 7, ">"), ("并发", 9, ">")],
        table,
    ))
    print("  · 标价倍率 = 输入价 ÷ 0.15，这是排序真正用的价格")
    print("  · 实际成本 = 标价倍率 × (1 − 0.98 × 缓存率)，缓存越高越便宜")
    print("  · 站点倍率是站点自己算的，把输出价也折了进去，所以有的行比标价倍率高；")
    print("    本工具只按输入价排序（参考价格表也只存输入价），这一列仅供对照")
    if any(row.cache is None for row in (DATA if rows is None else rows)):
        print(f"  · 缓存率带 * 的是站点没给数，按全局先验 {sr.CACHE_DEFAULT_PRIOR:.0%} 估")


def print_formula(settings: sr.RouteSettings) -> None:
    floor = sr.COST_LOG_FLOOR
    ceiling = max(settings.max_ratio, floor * 1.5)
    lat_ref = settings.max_p50_ms / 1000.0
    total = (settings.price_weight + settings.latency_weight
             + settings.success_weight) or 1
    shares = [settings.price_weight / total * 100,
              settings.latency_weight / total * 100,
              settings.success_weight / total * 100]
    print(f"\n【二】公式（权重 价格 {settings.price_weight} / 延迟 "
          f"{settings.latency_weight} / 成功率 {settings.success_weight}）")
    print(f"  实际占比        {shares[0]:.0f}% / {shares[1]:.0f}% / {shares[2]:.0f}%")
    print(f"  价格分  = log(实际成本 ÷ {floor:g}) ÷ log({ceiling:g} ÷ {floor:g})   "
          f"（对数刻度：0 = 最便宜，1 = 到达倍率上限）")
    print(f"  延迟分  = min(延迟 ÷ {lat_ref:g}s, 3)   "
          f"（线性，超过 {lat_ref * 3:g}s 一律记 3）")
    print(f"  失败分  = 1 − 成功率")
    print(f"  得分    = ({shares[0]:.0f}%×价格分 + {shares[1]:.0f}%×延迟分"
          f" + {shares[2]:.0f}%×失败分)，越小越好")


def run_once(settings: sr.RouteSettings, *, verbose: bool = True,
            rows: "tuple[Row, ...] | None" = None) -> list[tuple]:
    """跑一次打分。返回 [(候选, 各分项, 是否过硬门槛)]，按得分升序。"""
    cands = build_candidates(rows)
    ranked = sr.rank(cands, settings)
    ceiling = max(settings.max_ratio, sr.COST_LOG_FLOOR * 1.5)
    span = math.log(ceiling / sr.COST_LOG_FLOOR)
    out = []
    for cand in ranked:
        eff = max(cand.eff_ratio, sr.COST_LOG_FLOOR)
        cost = math.log(eff / sr.COST_LOG_FLOOR) / span if span > 0 else 0.0
        lat = min(cand.eff_p50_ms / max(settings.max_p50_ms, 1), 3.0)
        why = []
        if cand.ratio > settings.max_ratio:
            why.append(f"倍率 {cand.ratio:.3f} > 上限 {settings.max_ratio:.2f}")
        if cand.eff_p50_ms > settings.hard_p50_ms:
            why.append(f"延迟 {cand.eff_p50_ms / 1000:.1f}s > 硬上限 "
                       f"{settings.hard_p50_ms / 1000:g}s")
        if cand.fail_rate * 100 > settings.max_fail_rate:
            why.append(f"失败率 {cand.fail_rate * 100:.1f}% > 上限 "
                       f"{settings.max_fail_rate:g}%")
        out.append((cand, (cost, lat, cand.fail_rate), not why, "；".join(why)))
    if verbose:
        print_scoring(settings, out)
    return out


def print_scoring(settings: sr.RouteSettings, scored: list[tuple]) -> None:
    total = (settings.price_weight + settings.latency_weight
             + settings.success_weight) or 1
    rows = []
    for place, (cand, (cost, lat, fail), ok, _why) in enumerate(scored, 1):
        rows.append([
            str(place),
            f"Ch {cand.channel_id}",
            f"{cand.ratio:.4f}",
            f"{cand.cache_rate * 100:.1f}%",
            f"{cand.eff_ratio:.4f}",
            f"{cand.eff_p50_ms / 1000:.2f}",
            f"{(1 - cand.fail_rate) * 100:.1f}%",
            f"{cost:.3f}",
            f"{lat:.3f}",
            f"{fail:.3f}",
            f"{cand.score:.4f}",
            "✓" if ok else "淘汰",
        ])
    print("\n【三】打分结果（按得分升序）")
    print(_table(
        [("排名", 4, ">"), ("渠道", 9, "<"), ("倍率", 7, ">"), ("缓存", 7, ">"),
         ("实际成本", 9, ">"), ("延迟", 6, ">"), ("成功率", 7, ">"),
         ("价格分", 7, ">"), ("延迟分", 7, ">"), ("失败分", 7, ">"),
         ("得分", 7, ">"), ("门槛", 5, "<")],
        rows,
    ))
    passed = [x for x in scored if x[2]]
    if passed:
        best, _parts, _ok, _why = passed[0]
        print(f"  → 实际会固定：Ch {best.channel_id}（{best.supplier_name}），"
              f"得分 {best.score:.4f}，倍率 {best.ratio:.4f}、"
              f"缓存 {best.cache_rate * 100:.0f}%、延迟 {best.eff_p50_ms / 1000:.2f}s")
    else:
        print("  ! 所有候选都没过硬门槛，这次路由会失败")
    dropped = [x for x in scored if not x[2]]
    if dropped:
        print("  ! 被门槛淘汰（不参与选择，得分仅供参考）：")
        for cand, _p, _o, why in dropped:
            print(f"      Ch {cand.channel_id:<6} {why}")
    print(f"  （权重合计 {total}，改权重只改相对占比，不影响分数的绝对刻度）")


def sweep(settings: sr.RouteSettings, field: str, *, quiet: bool = False,
          rows: "tuple[Row, ...] | None" = None) -> list[tuple[int, int, str]]:
    """固定另两项，把某一项权重从 1 扫到 100，看选中的人在哪一段变。

    这是「公式符不符合预期」最直接的检查方式：一眼能看出「价格权重压到多少
    才会换人」，以及权重是不是足够灵敏——如果 1 和 100 选的是同一家，
    说明这家在所有维度上都占优，换权重也换不掉它。
    """
    import dataclasses

    label = {"price_weight": "价格", "latency_weight": "延迟",
             "success_weight": "成功率"}[field]
    runs: list[tuple[int, int, str]] = []
    for value in range(1, 101):
        st = dataclasses.replace(settings, **{field: value})
        cands = build_candidates(rows)
        ok = [c for c in sr.rank(cands, st)
              if c.ratio <= st.max_ratio and c.eff_p50_ms <= st.hard_p50_ms
              and c.fail_rate * 100 <= st.max_fail_rate]
        name = f"Ch {ok[0].channel_id}" if ok else "（无合格商家）"
        if runs and runs[-1][2] == name:
            runs[-1] = (runs[-1][0], value, name)
        else:
            runs.append((value, value, name))
    if quiet:
        return runs
    others = [f"{k}={getattr(settings, k)}"
              for k in ("price_weight", "latency_weight", "success_weight")
              if k != field]
    print(f"\n【四】{label}权重 1→100（另两项固定 {'，'.join(others)}）")
    for lo, hi, name in runs:
        span = f"{lo:>3}" if lo == hi else f"{lo:>3}–{hi:<3}"
        print(f"  {span}  →  {name}")
    return runs


def _parse_weights(tokens: list[str]) -> tuple[int, int, int]:
    """支持三种写法：三数字 / 预设名 / 单个数字（其余沿用默认）。"""
    by_key = {p.key: p.weights for p in sr.WEIGHT_PRESETS}
    by_name = {p.name: p.weights for p in sr.WEIGHT_PRESETS}
    if not tokens:
        return sr.RouteSettings().price_weight, sr.RouteSettings().latency_weight, \
            sr.RouteSettings().success_weight
    if len(tokens) == 1:
        key = tokens[0]
        if key in by_key:
            return by_key[key]
        for name, weights in by_name.items():
            if key in name:
                return weights
        number = int(key)
        base = sr.RouteSettings()
        return number, base.latency_weight, base.success_weight
    if len(tokens) != 3:
        raise ValueError("要么给三个数字（价格 延迟 成功率），要么给一个预设名")
    return int(tokens[0]), int(tokens[1]), int(tokens[2])


def _separate(argv: list[str]) -> tuple[list[str], dict[str, object]]:
    """把 `--lat 3` 这类选项从参数里挑出来，剩下的当权重。"""
    opts: dict[str, object] = {}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("--lat", "--ratio", "--hard"):
            opts[arg] = float(argv[i + 1])
            i += 2
            continue
        if arg == "--drop":
            opts["--drop"] = argv[i + 1]
            i += 2
            continue
        if arg == "--set":
            opts["--set"] = argv[i + 1]
            i += 2
            continue
        if arg == "--sweep":
            opts["--sweep"] = 1
            i += 1
            continue
        rest.append(arg)
        i += 1
    return rest, opts


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")     # 免得 cmd 用 GBK 炸中文
    except (AttributeError, OSError):
        pass

    if any(a in ("-h", "--help") for a in argv):
        print(__doc__)
        return 0

    weights, opts = _separate(argv)
    settings = sr.RouteSettings()
    if "--lat" in opts:
        settings.max_p50_ms = int(float(opts["--lat"]) * 1000)
    if "--ratio" in opts:
        settings.max_ratio = float(opts["--ratio"])
    if "--hard" in opts:
        settings.hard_p50_ms = int(float(opts["--hard"]) * 1000)

    key = str(opts.get("--set") or DEFAULT_SNAPSHOT)
    if key not in SNAPSHOTS:
        print(f"没有这份快照：{key}\n可选：{'、'.join(SNAPSHOTS)}")
        return 2
    label, rows = SNAPSHOTS[key]

    # --drop：把某几家当作「不可用」直接拿走。用途是看第二层排序——
    # 遇到 5343 这种价格与延迟双料第一的商家，权重怎么调都不换人，
    # 公式的区分能力就藏起来了；拿走它才能看出剩下几家按什么排。
    if "--drop" in opts:
        drop = {int(x) for x in str(opts["--drop"]).replace("，", ",").split(",")
                if x.strip().isdigit()}
        kept = tuple(r for r in rows if r.channel not in drop)
        if not kept:
            print("--drop 把所有商家都拿掉了")
            return 2
        print(f"\n（已按 --drop 移除：{'、'.join('Ch %d' % c for c in sorted(drop))}）")
        rows = kept

    print_source_table(rows, label)

    if weights:
        try:
            settings.price_weight, settings.latency_weight, settings.success_weight = \
                _parse_weights(weights)
        except (ValueError, TypeError) as exc:
            print(f"\n参数看不懂：{exc}")
            return 2
        print_formula(settings)
        run_once(settings, rows=rows)
        if "--sweep" in opts:
            # 三项都扫：成败往往在「成功率」这一项上——这台站点里就有一批
            # 24h 成功率 0–3% 的商家，价格再低也不该选
            for field in ("price_weight", "latency_weight", "success_weight"):
                sweep(settings, field, rows=rows)
        return 0

    # 交互：反复试权重，空行退出
    print(f"\n当前快照 {key}（{label}）。换快照：--set {'/'.join(SNAPSHOTS)}")
    print("输入三个权重（价格 延迟 成功率，1–100 的整数），空行退出。")
    print("也可以只写一个预设名：money / balanced / speed")
    while True:
        try:
            line = input("\n权重> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            break
        try:
            values = _parse_weights(line.replace(",", " ").split())
        except (ValueError, TypeError) as exc:
            print(f"  看不懂：{exc}")
            continue
        settings.price_weight, settings.latency_weight, settings.success_weight = values
        print_formula(settings)
        run_once(settings, rows=rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
