#!/usr/bin/env python3
"""拉取公开价格快照，生成随程序分发的「预设价格表」`prices_preset.json`。

数据源是 LiteLLM 的 `model_prices_and_context_window.json`（社区维护，覆盖几千个模型，
字段是「每 token 美元」）。这里把它归一成「每 100 万 token 美元」，另存一个汇率，
运行时再折算成人民币 —— 不把汇率硬揉进价格里，否则以后调汇率就得重拉一遍。

**这是手动工具，不随程序运行**：价格变动很慢，每次发版前跑一次就够。
用法：

    python scripts/fetch_prices.py            # 拉最新的，覆盖项目里的预设文件
    python scripts/fetch_prices.py --dry-run  # 只看看能拉到多少条，不写文件
"""

from __future__ import annotations
# 直接运行本文件时，把项目根目录加入 sys.path（无需安装成包）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
import ssl
import urllib.request
from datetime import date

PRESET_PATH = Path(__file__).resolve().parents[1] / "modelkeeper" / "assets" / "prices_preset.json"

# 主源 + 两个镜像：GitHub raw 在国内时常抽风，jsDelivr 通常可用
SOURCES = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json",
    "https://cdn.jsdelivr.net/gh/BerriAI/litellm@main/"
    "model_prices_and_context_window.json",
    "https://openrouter.ai/api/v1/models",
)

# 1 USD 折多少 CNY。写进文件里，界面上会显示「预设价按此汇率折算」。
USD_CNY = 7.2

# 明显不是真实模型名的条目（示例、占位、聚合项）
SKIP_KEYS = {"sample_spec"}


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "model-keeper/1.0"})
    with urllib.request.urlopen(req, timeout=30, context=ssl.create_default_context()) as r:
        return r.read()


def _from_litellm(raw: dict) -> dict[str, float]:
    """LiteLLM：{model: {input_cost_per_token, ...}}，单位美元/token（只取输入价）。"""
    out: dict[str, float] = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict) or name in SKIP_KEYS:
            continue
        cin = entry.get("input_cost_per_token")
        if cin is None:
            continue          # 没有输入价就没有可记的数：输出价不进这张表
        try:
            # per-token → per-1M，保留 4 位小数（$0.0001/1M 这个量级够细了）
            in_1m = round(float(cin) * 1_000_000, 4)
        except (TypeError, ValueError):
            continue
        out[name.strip()] = in_1m
    return out


def _from_openrouter(raw: dict) -> dict[str, float]:
    """OpenRouter：{"data": [{id, pricing: {prompt}}]}，单位美元/token（字符串，只取输入价）。"""
    out: dict[str, float] = {}
    for item in raw.get("data") or []:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        pricing = item.get("pricing") or {}
        if not mid or not isinstance(pricing, dict):
            continue
        try:
            pin = float(pricing.get("prompt") or 0) * 1_000_000
        except (TypeError, ValueError):
            continue
        if pin:
            out[mid] = round(pin, 4)
    return out


def fetch() -> tuple[dict[str, float], str]:
    """依次尝试各个源，返回 (价格表, 来源 URL)。"""
    errors: list[str] = []
    for url in SOURCES:
        try:
            raw = json.loads(_get(url))
        except Exception as e:  # noqa: BLE001 - 换下一个源
            errors.append(f"{url} → {type(e).__name__}: {e}")
            continue
        table = _from_litellm(raw) if isinstance(raw, dict) and "data" not in raw else _from_openrouter(raw)
        if table:
            return table, url
        errors.append(f"{url} → 解析出 0 条")
    raise SystemExit("所有价格源都失败了：\n  " + "\n  ".join(errors))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="生成 prices_preset.json")
    ap.add_argument("--dry-run", action="store_true", help="只统计，不写文件")
    args = ap.parse_args(argv)

    table, source = fetch()
    payload = {
        "source": source,
        "fetched": date.today().isoformat(),
        "currency": "USD",
        "unit": "1M",
        "field": "input",       # 只存输入价：价格表的唯一口径
        "fx": {"usd_cny": USD_CNY,},
        "models": dict(sorted(table.items())),
    }
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"

    print(f"源：{source}")
    print(f"模型：{len(table)} 条，文件约 {len(text) / 1024:.0f} KB")
    if args.dry_run:
        sample = list(table.items())[:3]
        print("样例：", sample)
        return
    PRESET_PATH.write_text(text, encoding="utf-8")
    print(f"已写入：{PRESET_PATH}")


if __name__ == "__main__":
    sys.exit(main())
