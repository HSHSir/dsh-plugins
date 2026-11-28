#!/usr/bin/env python3
"""打分实验室自测：数据换算与扫描区间。

score_lab 是给人看公式的工具，不是业务代码，但它引用的每个定义都必须跟
`smart_route` 一致——否则用户照着这张表调权重，调的是另一个公式。所以这里
只验三件事：数据换算（倍率、缓存折扣）、门槛判定、扫描区间覆盖完整。
"""

from __future__ import annotations
# 直接运行本文件时，把项目根目录加入 sys.path（无需安装成包）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from modelkeeper import smart_route as sr
from scripts import score_lab as lab


class check:  # noqa: N801 - 与其它自测脚本保持同一种调用样式
    failed = 0

    def __init__(self, name: str, ok: object, detail: object = "") -> None:
        if ok:
            print(f"PASS  {name}")
        else:
            check.failed += 1
            print(f"FAIL  {name}  -> {detail}")


def main() -> None:
    print("== score_lab ==")

    # ---- 数据本身 ----
    check("默认快照有 12 行", len(lab.DATA) == 12, len(lab.DATA))
    check("两份快照都留着", set(lab.SNAPSHOTS) == {"1750", "1711"}, set(lab.SNAPSHOTS))
    check("默认快照就是最新那份",
          lab.DATA == lab.SNAPSHOTS[lab.DEFAULT_SNAPSHOT][1])
    check("渠道不重复", len({r.channel for r in lab.DATA}) == len(lab.DATA))
    check("成功率都在 0–100", all(0 <= r.success <= 100 for r in lab.DATA))
    check("缓存率都在 0–100（允许缺失）",
          all(r.cache is None or 0 <= r.cache <= 100 for r in lab.DATA),
          [r.cache for r in lab.DATA])

    cands = lab.build_candidates()
    check("候选数与数据行数一致", len(cands) == len(lab.DATA), len(cands))
    by_id = {c.channel_id: c for c in cands}

    row = next(r for r in lab.DATA if r.channel == 5343)
    cand = by_id[5343]
    check("标价倍率 = 输入价 ÷ 官方价",
          abs(cand.ratio - row.price / lab.OFFICIAL_IN) < 1e-12, cand.ratio)
    check("实际成本低于标价（有缓存）", cand.eff_ratio < cand.ratio,
          (cand.eff_ratio, cand.ratio))
    check("实际成本 = 公式算出来的那个数",
          abs(cand.eff_ratio - sr._eff_ratio(cand.ratio, cand.input_micros,
                                             cand.cache_read_micros,
                                             cand.cache_rate)) < 1e-12,
          cand.eff_ratio)
    check("缓存读价按输入价的 2%",
          abs(cand.cache_read_micros / cand.input_micros - lab.CACHE_READ_RATIO) < 1e-9,
          cand.cache_read_micros)
    check("失败率 = 1 − 成功率",
          abs(cand.fail_rate - (1 - row.success / 100)) < 1e-12, cand.fail_rate)
    check("延迟用截图里那一列",
          cand.eff_p50_ms == int(round(row.latency_s * 1000)), cand.eff_p50_ms)

    # 站点那一格是「—」时不能当成 0%：会把这家的实际成本算贵
    miss = by_id[4866]
    check("缓存缺失标为未知", not miss.cache_known, miss.cache_known)
    check("缓存缺失按全局先验估", abs(miss.cache_rate - sr.CACHE_DEFAULT_PRIOR) < 1e-12,
          miss.cache_rate)
    check("缓存缺失仍然算出了折扣",
          miss.eff_ratio < miss.ratio * 0.8, (miss.eff_ratio, miss.ratio))

    # ---- 排序：用 rank() 而不是另写一份 ----
    ranked = sr.rank(lab.build_candidates(), sr.RouteSettings())
    check("默认权重下 Ch 5343 第一", ranked[0].channel_id == 5343, ranked[0].channel_id)
    # 旧快照仍然能算（多快照不是摆设）
    old_rows = lab.SNAPSHOTS["1711"][1]
    old_top = sr.rank(lab.build_candidates(old_rows), sr.RouteSettings())[0]
    check("--set 1711 那份仍然选出 Ch 6932", old_top.channel_id == 6932, old_top.channel_id)
    check("得分单调不减", all(a.score <= b.score for a, b in zip(ranked, ranked[1:])))
    check("Ch 7081 的倍率超上限",
          by_id[7081].ratio > sr.RouteSettings().max_ratio, by_id[7081].ratio)

    # ---- 扫描：区间必须首尾相接、覆盖 1–100 ----
    for field in ("price_weight", "latency_weight"):
        runs = lab.sweep(sr.RouteSettings(), field, quiet=True)
        check(f"{field} 扫描从 1 开始", runs[0][0] == 1, runs[0])
        check(f"{field} 扫描到 100 结束", runs[-1][1] == 100, runs[-1])
        gaps = [b for a, b in zip(runs, runs[1:]) if b[0] != a[1] + 1]
        check(f"{field} 区间连续无缝", not gaps, gaps)
        check(f"{field} 区间不重复命名相邻同值",
              all(a[2] != b[2] for a, b in zip(runs, runs[1:])), runs)

    # 放宽倍率上限之后必须出现「换人」，否则扫描没起作用。
    # 用 1711 那份做：1750 里 5343 价格与延迟双料第一，任何权重都换不掉它
    # （这不是公式失灵，是存在支配解，见 sweep 全段同一个名字）。
    loose = sr.RouteSettings()
    loose.max_ratio = 3.0
    runs = lab.sweep(loose, "price_weight", quiet=True, rows=lab.SNAPSHOTS["1711"][1])
    check("放宽门槛后扫描能看见换人", len(runs) > 1, runs)

    # 当前快照的支配解：三项权重各自 1→100 都不会换人
    for field in ("price_weight", "latency_weight", "success_weight"):
        runs = lab.sweep(sr.RouteSettings(), field, quiet=True)
        check(f"{field} 全段都是同一家（此快照存在支配解）",
              len(runs) == 1 and runs[0][2] == "Ch 5343", runs)

    print()
    if check.failed:
        print(f"RESULT: {check.failed} FAILED")
        raise SystemExit(1)
    print("RESULT: ALL PASS")


if __name__ == "__main__":
    main()
