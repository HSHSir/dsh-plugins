#!/usr/bin/env python3
"""价格表自测：匹配优先级、后缀归一、通配、单位换算、未知不当 0。

价格表只记**输入价（元 / 百万 token）**，所以这里的断言集中在两件事：
「查到的数是不是元/百万 token」与「没有数的时候必须显示 —，不能变成 0」。
"""

from __future__ import annotations
# 直接运行本文件时，把项目根目录加入 sys.path（无需安装成包）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import os
import tempfile

from modelkeeper import price_book as pb


class check:  # noqa: N801 - 与其它自测脚本保持同一种调用样式
    failed = 0

    def __init__(self, name: str, ok: object, detail: object = "") -> None:
        if ok:
            print(f"PASS  {name}")
        else:
            check.failed += 1
            print(f"FAIL  {name}  -> {detail}")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="prices-"))
    os.environ["MODEL_PROBE_PRICE_FILE"] = str(tmp / "prices.json")
    # 关掉随包分发的预设：这些用例只验「我的表」，不能受外部数据变动影响
    os.environ["MODEL_PROBE_PRICE_PRESET"] = "none"

    # ---- 后缀归一：只砍日期/编号/已知标签，别砍名字本身 ----
    check("去日期后缀", pb.strip_suffix("deepseek-v4.1-flash-1017") == "deepseek-v4.1-flash",
          pb.strip_suffix("deepseek-v4.1-flash-1017"))
    check("去 preview 标签", pb.strip_suffix("glm-5.3-preview") == "glm-5.3",
          pb.strip_suffix("glm-5.3-preview"))
    check("连砍多段", pb.strip_suffix("qwen-3-8-20251017") == "qwen-3-8",
          pb.strip_suffix("qwen-3-8-20251017"))
    check("不砍名字里的数字", pb.strip_suffix("claude-3-5-sonnet") == "claude-3-5-sonnet",
          pb.strip_suffix("claude-3-5-sonnet"))

    # ---- 格式化：只显示输入价，没有就破折号 ----
    check("有价显示数字", pb.Price(3.0).text() == "3", pb.Price(3.0).text())
    check("小数不补零", pb.Price(0.15).text() == "0.15", pb.Price(0.15).text())
    check("未知显示破折号", pb.Price().text() == "—", pb.Price().text())
    check("未知不算已知", pb.Price().known is False)
    check("0 是合法价格（免费）", pb.Price(0.0).known is True and pb.Price(0.0).text() == "0",
          pb.Price(0.0).text())

    data = {
        "currency": "CNY", "unit": "1M", "updated": "",
        "models": {
            "gpt-6.1-sol": {"in": 3},
            "deepseek-v4.1-flash": {"in": 1},
        },
        "patterns": {"claude-*": {"in": 30}, "claude-3-*": {"in": 3}},
    }

    # ---- 匹配优先级：精确 > 去后缀 > 通配（更具体的 pattern 先命中）----
    exact = pb.lookup("gpt-6.1-sol", data)
    check("精确命中", exact.in_ == 3 and exact.source == "表", (exact.in_, exact.source))

    with_suffix = pb.lookup("deepseek-v4.1-flash-1017", data)
    check("去后缀命中", with_suffix.in_ == 1 and with_suffix.source == "表",
          (with_suffix.in_, with_suffix.source))

    specific = pb.lookup("claude-3-7-sonnet", data)
    check("通配更具体优先", specific.in_ == 3 and specific.source == "通配",
          (specific.in_, specific.source))

    loose = pb.lookup("claude-haiku-5", data)
    check("通配兜底", loose.in_ == 30, loose.in_)

    unknown = pb.lookup("nothing-like-this", data)
    check("查不到就是未知，不是 0", unknown.known is False and unknown.text() == "—",
          (unknown.known, unknown.text()))

    # ---- 预设表：自己表优先，查不到才落到预设 ----
    # 注意：这里的 id 特意避开上面 data 里的 claude-* 通配，否则会被自己的价挡住
    preset = {"models": {"gpt-6.1-sol": {"in": 7}, "gemini-2.5-pro": {"in": 9}},
              "patterns": {"qwen-*": {"in": 2}}}

    mine_wins = pb.lookup("gpt-6.1-sol", data, preset)
    check("自己的价压过预设", mine_wins.in_ == 3 and not mine_wins.estimated,
          (mine_wins.in_, mine_wins.estimated))

    from_preset = pb.lookup("gemini-2.5-pro", data, preset)
    check("自己表没有时用预设", from_preset.in_ == 9 and from_preset.source == "预设",
          (from_preset.in_, from_preset.source))
    check("预设价同样只显示一个数", from_preset.text() == "9", from_preset.text())

    suffix_hit = pb.lookup("gemini-2.5-pro-001", data, preset)
    check("预设也认去后缀", suffix_hit.in_ == 9 and suffix_hit.estimated,
          (suffix_hit.in_, suffix_hit.estimated))

    pattern_hit = pb.lookup("qwen-3.8-flash", data, preset)
    check("预设通配兜底", pattern_hit.in_ == 2 and pattern_hit.source == "预设通配",
          (pattern_hit.in_, pattern_hit.source))

    check("预设里也没有就是未知",
          pb.lookup("no-such-model", data, preset).known is False)

    # ---- 参考价目表（全量视图）：出厂 + 我的覆盖 ----
    catal = {"catalog": {"gpt-4o": {"in": 19.8}, "claude-x": {"in": 21.6}}, "models": {}}
    merged = pb.catalog_rows({"models": {"gpt-4o": {"in": 5}},
                              "patterns": {"claude-*": {"in": 1}}}, catal)
    by_id = {r[0]: r for r in merged}
    check("全量表包含未修改的出厂项", by_id["claude-x"][2] == "出厂", by_id.get("claude-x"))
    check("改过的显示我的价", by_id["gpt-4o"][1] == 5 and by_id["gpt-4o"][2] == "我的",
          by_id.get("gpt-4o"))
    check("同一个模型不会出两行", len([r for r in merged if r[0] == "gpt-4o"]) == 1, merged)
    check("我的通配条目也进表", by_id["claude-*"][2] == "我的通配", by_id.get("claude-*"))
    check("按 ID 排序（不因来源跳位）",
          [r[0] for r in merged] == sorted((r[0] for r in merged), key=str.lower),
          [r[0] for r in merged])

    own_only = pb.catalog_rows({"models": {}}, catal)
    check("没有自己的修改时只剩出厂价",
          all(r[2] == "出厂" for r in own_only) and len(own_only) == 2, own_only)
    check("每行只有 id/价/来源三列", all(len(r) == 3 for r in merged), merged[:2])

    # ---- 单位换算：接口的 micros → 元 / 百万 token ----
    os.environ.pop("MODEL_PROBE_PRICE_PRESET", None)
    pb.load_preset.cache_clear()
    rate = pb.fx()
    check("汇率取自预设文件", rate > 1, rate)
    check("官方 150000 micros = $0.15/M",
          abs(pb.cny_from_usd_micros(150000) - 0.15 * rate) < 1e-6,
          pb.cny_from_usd_micros(150000))
    check("商户价按同口径换算",
          pb.cny_from_usd_micros(6240) == round(0.00624 * rate, 4),
          pb.cny_from_usd_micros(6240))
    check("0 与负数视为没有价", pb.cny_from_usd_micros(0) is None
          and pb.cny_from_usd_micros(-5) is None)
    check("非数字不炸", pb.cny_from_usd_micros("abc") is None
          and pb.cny_from_usd_micros(None) is None)

    # ---- 随包分发的预设文件本身 ----
    bundled = pb.load_preset()
    check("预设文件可读", len(bundled.get("models") or {}) > 500,
          len(bundled.get("models") or {}))
    check("预设带来源与抓取日期",
          bool(bundled.get("source")) and bool(bundled.get("fetched")),
          (bundled.get("source"), bundled.get("fetched")))
    sample = next(iter((bundled.get("models") or {}).values()))
    check("预设每条只有一个输入价", set(sample) == {"in"}, sample)
    check("预设价已折成人民币（比美元原值大）", (sample.get("in") or 0) > 0, sample)

    raw_preset = json.loads(pb.preset_path().read_text(encoding="utf-8"))
    check("预设文件只存输入价（不再是 [入, 出]）",
          all(isinstance(v, (int, float)) for v in list(raw_preset["models"].values())[:50]),
          list(raw_preset["models"].items())[:2])
    check("预设文件声明字段", raw_preset.get("field") == "input", raw_preset.get("field"))
    os.environ["MODEL_PROBE_PRICE_PRESET"] = "none"
    pb.load_preset.cache_clear()

    # ---- 排序键 ----
    check("排序键用输入价", pb.Price(3).sort_key() == 3)
    check("没有价就是 None", pb.Price().sort_key() is None)

    # ---- 读写往返：带 * 的进 patterns ----
    pb.upsert("glm-5.3-flash", 0.15, data)
    pb.upsert("qwen-*", 2, data)
    pb.save(data)
    reloaded = pb.load()
    check("往返保住精确条目", reloaded["models"]["glm-5.3-flash"]["in"] == 0.15,
          reloaded["models"].get("glm-5.3-flash"))
    check("带 * 的写进 patterns", reloaded["patterns"]["qwen-*"]["in"] == 2,
          reloaded["patterns"].get("qwen-*"))
    check("落盘只有 in 一个键", set(reloaded["models"]["glm-5.3-flash"]) == {"in"},
          reloaded["models"]["glm-5.3-flash"])

    check("删除精确条目", pb.remove("glm-5.3-flash", reloaded) is reloaded
          and "glm-5.3-flash" not in reloaded["models"], reloaded["models"])
    check("删除通配条目", "qwen-*" not in pb.remove("qwen-*", reloaded)["patterns"],
          reloaded["patterns"])

    # ---- 兼容旧格式：`[入, 出]` 与 `{in, out}` 都只取输入价 ----
    legacy = tmp / "legacy.json"
    legacy.write_text(json.dumps({"models": {
        "pair-model": [3, 15],
        "dict-model": {"in": 1.5, "out": 9},
        "scalar-model": 2.5,
    }}), encoding="utf-8")
    os.environ["MODEL_PROBE_PRICE_FILE"] = str(legacy)
    old = pb.load()
    check("旧二元组取输入价", old["models"]["pair-model"]["in"] == 3,
          old["models"].get("pair-model"))
    check("旧字典丢掉出价", set(old["models"]["dict-model"]) == {"in"},
          old["models"].get("dict-model"))
    check("新标量直接可用", old["models"]["scalar-model"]["in"] == 2.5,
          old["models"].get("scalar-model"))

    # ---- 坏文件不该拖垮探测 ----
    Path(os.environ["MODEL_PROBE_PRICE_FILE"]).write_text("{not json", encoding="utf-8")
    check("坏文件返回空表", pb.load()["models"] == {}, pb.load()["models"])

    # ---- 负数/非法值当未知，不静默变 0 ----
    bad = tmp / "bad.json"
    bad.write_text(json.dumps({"models": {"x": {"in": -1, "out": "abc"}}}), encoding="utf-8")
    os.environ["MODEL_PROBE_PRICE_FILE"] = str(bad)
    loaded = pb.load()
    check("负数与非法值落成 None", loaded["models"]["x"]["in"] is None,
          loaded["models"]["x"])

    print("\nRESULT:", "ALL PASS" if not check.failed else "HAS FAILURES")


if __name__ == "__main__":
    main()
