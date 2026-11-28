#!/usr/bin/env python3
"""模型价格表：人民币 / 每 100 万 token 的本地簿记。

**为什么价格不来自端点**：标准 `/models` 只返回 id（有的中转站连上下文都不给），
而中转站的实际扣费与官方挂牌价也不是一回事。所以这里只记「你自己确认过的数」：

  * ``models``   精确的模型 ID → 单价
  * ``patterns`` 通配（如 ``claude-*``），给同族模型兜底

查不到就是 ``None``（界面显示 `—`），绝不当 0 —— 0 会让「最便宜」的排序说谎，
这跟上下文列「空值不冒充数据」是同一条原则。

文件默认在 ``~/.model_probe/prices.json``，可用 ``MODEL_PROBE_PRICE_FILE``
覆盖（自测用，避免顶掉用户真正的价格表）。
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from bisect import bisect_left
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .paths import asset_dir

CURRENCY = "CNY"
UNIT_LABEL = "¥ / 1M tokens"
DEFAULT_PATH = Path.home() / ".model_probe" / "prices.json"
PRESET_NAME = "prices_preset.json"
DEFAULT_FX = 7.2        # 预设文件里的美元价按这个汇率折成人民币


def fx() -> float:
    """当前折算汇率（取预设文件里的 usd_cny，读不到用默认）。"""
    rate = (load_preset().get("fx") or DEFAULT_FX)
    return float(rate) if rate else DEFAULT_FX


def cny_from_usd_micros(micros: object) -> float | None:
    """把中转站接口的 `input_price_micros` 折成「元 / 百万 token」。

    接口里的 micros 是「美元 / 百万 token」的百万分之一（官方 150000 即 $0.15/M），
    所以先除 1e6 得美元价，再乘汇率。负数/非数字一律当没有，不猜。
    """
    try:
        value = float(micros)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    return round(value / 1_000_000 * fx(), 4)


def preset_path() -> Path:
    return asset_dir() / PRESET_NAME

# 版本/日期后缀：`deepseek-v4.1-flash-1017`、`glm-5-preview`、`qwen-0724`
# 这些尾巴几乎不是模型身份的一部分，去掉后更容易命中共用的那条价格。
_TAGS = {
    "preview", "latest", "beta", "exp", "experimental", "chat", "instruct",
    "thinking", "nothinking", "reasoning", "nonreasoning", "free",
}


def price_path() -> Path:
    override = os.environ.get("MODEL_PROBE_PRICE_FILE", "").strip()
    return Path(override).expanduser() if override else DEFAULT_PATH


def norm(model_id: str) -> str:
    return (model_id or "").strip().lower()


def strip_suffix(model_id: str) -> str:
    """去掉尾部的「日期 / 编号 / 常用标签」，只保留主体。

    只在整段确实是三位以上数字、或是已知标签时才动手：
      * ``deepseek-v4.1-flash-1017`` → ``deepseek-v4.1-flash``（编号）
      * ``qwen-3-8-20251017`` → ``qwen-3-8``（日期）
      * ``claude-3-5-sonnet`` 原样保留 —— ``5`` 是名字的一部分
      * ``claude-haiku-5-5`` 原样保留 —— 单位数不当后缀砍
    """
    cur = norm(model_id)
    while True:
        head, sep, tail = cur.rpartition("-")
        if not sep or not tail:
            break
        if (tail.isdigit() and len(tail) >= 3) or tail in _TAGS:
            cur = head
            continue
        break
    return cur


@dataclass(frozen=True)
class Price:
    """一条命中的价格：**输入价，元 / 百万 token**。

    只记输入价是刻意的：它是判断「贵不贵」最直接的量，也正好是智能路由打分用的
    那个数（商户输入价 ÷ 官方输入价）。输出价各站倍率算法差异太大，记了也没法横向比，
    反而会让人以为漏填了一半。
    """

    in_: float | None = None
    source: str = ""          # "表" / "通配" / "预设" / "预设通配" / ""
    estimated: bool = False   # True = 来自预设（官方参考价折算），不是你的实付价

    @property
    def known(self) -> bool:
        return self.in_ is not None

    def text(self) -> str:
        """界面用：`3`、`—`（没数据）。

        没数据就显示破折号，不显示 0——0 元模型不存在，写 0 会让人以为免费。
        """
        return _fmt(self.in_)

    def sort_key(self) -> float | None:
        """排序用：没有价格的排最后（None 交给调用方垫底）。"""
        return self.in_


def _fmt(value: float | None) -> str:
    if value is None:
        return "—"
    # 3.0 → "3"，0.10 → "0.1"：价格表里没人关心尾随零
    return f"{value:g}"


# 界面上想单独格式化一格（比如价格表编辑框）时用它，别直接调私有名
fmt = _fmt


def empty() -> dict:
    return {"currency": CURRENCY, "unit": "1M", "updated": "",
            "models": {}, "patterns": {}, "catalog": {}}


def _price_entry(raw: object) -> dict[str, float | None]:
    """把各种历史写法收敛成 `{"in": 数}`。

    兼容三种：新格式 `3.5`、旧格式 `{"in": 3, "out": 9}`、更早的 `[3, 9]`。
    出价一律丢弃——表里只保留输入价，留着它只会让界面又长出第二列。
    """
    if isinstance(raw, dict):
        return {"in": _num(raw.get("in"))}
    if isinstance(raw, (list, tuple)):
        return {"in": _num(raw[0]) if raw else None}
    return {"in": _num(raw)}


def load() -> dict:
    """读价格表；文件不存在或坏掉都返回空表（价格是可选信息，不该拖垮探测）。"""
    path = price_path()
    data = empty()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return data
    if not isinstance(raw, dict):
        return data
    for key in ("models", "patterns"):
        block = raw.get(key)
        if isinstance(block, dict):
            data[key] = {str(mid): _price_entry(entry) for mid, entry in block.items()}
    if isinstance(raw.get("updated"), str):
        data["updated"] = raw["updated"]
    return data


def save(data: dict) -> Path:
    """原子写：价格表损坏比丢一次探测结果更烦。"""
    path = price_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _num(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        num = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return num if num >= 0 else None


_UNSET = object()

# 「base-*」前缀命中的有序键缓存：(表对象, 键数量, 有序键)。
# 原实现对未精确命中的模型要遍历全部 3728 条预设键做 startswith；筛选表时
# 每敲一个键、每一行都要跑一遍（实测每次按键 60–120 ms，模型上千则进秒级）。
# 排序一次 + 二分查找与之**完全等价**（同样是 base- 开头的键），复杂度降到 O(log n)。
# 缓存里握住表对象本身，`is` 比对可防 id() 复用导致的串味。
_SORTED_KEYS: dict[int, tuple[dict, int, list[str]]] = {}


def _sorted_model_keys(table: dict) -> list[str]:
    models = table.get("models") or {}
    cached = _SORTED_KEYS.get(id(table))
    if cached is not None and cached[0] is table and cached[1] == len(models):
        return cached[2]
    keys = sorted(models)
    _SORTED_KEYS[id(table)] = (table, len(models), keys)
    return keys


def _match(model_id: str, table: dict, *, prefix: bool = False) -> tuple[dict | None, str]:
    """在一张表里找一条：精确 → 去后缀 → 通配（更具体优先）。

    `prefix=True` 额外允许「base-*」式前缀命中，并且取字典序最大的一条当最新版本
    （`claude-3-5-sonnet` → `claude-3-5-sonnet-20241022`）。只给预设表开这个口子：
    用户自己的表讲究「写什么就是什么」，多猜一步反而容易串味。
    """
    mid = norm(model_id)
    models: dict = table.get("models") or {}
    patterns: dict = table.get("patterns") or {}
    entry = models.get(mid)
    if entry is None:
        entry = models.get(strip_suffix(mid))
    if entry is not None:
        return entry, "exact"
    if prefix:
        base = strip_suffix(mid)
        head = f"{base}-"
        keys = _sorted_model_keys(table)
        lo = bisect_left(keys, head)
        cands = keys[lo:bisect_left(keys, head + "\U0010ffff")]
        if cands:
            return models[max(cands)], "exact"
    for pattern in sorted(patterns, key=len, reverse=True):
        if fnmatch.fnmatch(mid, pattern) or fnmatch.fnmatch(strip_suffix(mid), pattern):
            return patterns[pattern] or {}, "pattern"
    return None, ""


def lookup(model_id: str, data: dict | None = None, preset: object = _UNSET) -> Price:
    """查价格：**我的表**优先，查不到再落到**预设表**（官方参考价，带星号）。

    顺序不能倒过来：预设价是官方挂牌价，中转站通常另按倍率计费；用户自己填的数
    永远比预设更接近账单，所以必须先命中自己的表。
    """
    data = data if data is not None else load()
    entry, kind = _match(model_id, data)
    if entry is not None:
        return Price(_num(entry.get("in")), "表" if kind == "exact" else "通配")

    table = load_preset() if preset is _UNSET else preset
    if table:
        entry, kind = _match(model_id, table, prefix=True)
        if entry is not None:
            return Price(_num(entry.get("in")),
                         "预设" if kind == "exact" else "预设通配", estimated=True)
    return Price()


def _bare_name(model_id: str) -> str:
    """去掉**厂商/区域前缀**后的裸名：`anthropic.claude-x-v1:0` → `claude-x`。

    注意只能按「前面的段是纯字母短词」来剥：`longcat-2.0`、`embed-v4.0`、
    `Seed-2.0-code` 里的点属于模型名，按点暴力切会把它们切成一堆 `0`、`0-code`。
    """
    name = model_id.rsplit("/", 1)[-1]
    while True:
        head, sep, tail = name.partition(".")
        if sep and tail.strip() and head.isalpha() and len(head) <= 12:
            name = tail
            continue
        break
    return norm(re.sub(r"-v\d+(:\d+)?$", "", name))


def _preset_aliases(model_id: str) -> set[str]:
    """给一条预设键生成「探测页可能拿到的写法」。

    LiteLLM 同一个模型会以 ``anthropic.claude-x``、``aihubmix/claude-x``、
    ``anthropic.claude-x-20241022-v1:0`` 几种写法出现，而探测页拿到的是裸名。
    不去掉这些前后缀，预设命中率会低得莫名其妙。
    """
    return {norm(model_id), _bare_name(model_id)}


@lru_cache(maxsize=1)
def load_preset() -> dict:
    """读随程序分发的预设价格：美元/1M 按 `fx` 折成人民币/1M。

    没网也能用 —— 预设是打包时生成的文件，不在运行时抓取。文件缺失/损坏返回空表
    （价格是可选信息，不该拖垮探测）。设 `MODEL_PROBE_PRICE_PRESET=none` 可整体关掉，
    自测靠它保证结果与外部数据脱钩。
    """
    if os.environ.get("MODEL_PROBE_PRICE_PRESET", "").strip().lower() in ("none", "off", "0"):
        return empty()
    try:
        raw = json.loads(preset_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty()
    if not isinstance(raw, dict):
        return empty()
    rate = _num((raw.get("fx") or {}).get("usd_cny") if isinstance(raw.get("fx"), dict) else None)
    models = raw.get("models")
    if not rate or not isinstance(models, dict):
        return empty()

    out = empty()
    out["source"] = str(raw.get("source") or "")
    out["fetched"] = str(raw.get("fetched") or "")
    out["fx"] = rate
    official = 0
    for mid, raw_price in models.items():
        # 预设只存「美元 / 百万 token 的输入价」。老文件是 `[入, 出]` 二元组，
        # 现在也认它，取第一个——升级后不必要求用户重新下载预设。
        usd = _num(raw_price[0]) if isinstance(raw_price, (list, tuple)) and raw_price \
            else (_num(raw_price.get("in")) if isinstance(raw_price, dict) else _num(raw_price))
        entry = {"in": round(usd * rate, 4) if usd is not None else None}
        official += 1
        # 原始键 + 去掉厂商前缀/区域后缀的别名。LiteLLM 里同一个模型会有
        # `anthropic.claude-x`、`aihubmix/claude-x`、`anthropic.claude-x-20241022-v1:0`
        # 这几种写法，而探测页拿到的是裸名 `claude-x`，不建别名就永远对不上。
        for alias in _preset_aliases(str(mid)):
            out["models"].setdefault(alias, entry)
        # 参考价目表展示用「裸名」去重：同一个模型的云端/代理商写法只留一行，
        # 否则 3728 条原始键在界面上看着像三千多个不同模型。
        out["catalog"].setdefault(_bare_name(str(mid)), entry)
    out["official"] = official
    return out


def catalog_rows(data: dict | None = None,
                 preset: dict | None = None) -> list[tuple[str, float | None, float | None, str]]:
    """参考价目表的**全量**数据：出厂预设全集 + 我的修改（同 ID 覆盖）。

    用户要的是「一张能查所有模型的价目表」：出厂自带一版，改过的显示自己的数，
    没改过的仍是出厂价。所以这里不做「只列当前列表命中」那套裁剪。
    """
    own = data if data is not None else load()
    table = load_preset() if preset is None else preset

    rows: dict[str, tuple[float | None, str]] = {}
    for mid, entry in (table.get("catalog") or {}).items():
        rows[mid] = (_num(entry.get("in")), "出厂")
    for mid, entry in (own.get("models") or {}).items():
        rows[mid] = (_num(entry.get("in")), "我的")
    out = [(mid, in_, src) for mid, (in_, src) in rows.items()]
    for pattern, entry in sorted((own.get("patterns") or {}).items()):
        out.append((pattern, _num(entry.get("in")), "我的通配"))
    # 只按 ID 排序：来源不展示，就不该让行列莫名其妙地跳位置
    out.sort(key=lambda r: r[0].lower())
    return out


def upsert(model_id: str, in_: float | None, data: dict | None = None) -> dict:
    """新增/更新一条（只写输入价）。带 `*` 的 ID 写进 patterns，其余写进 models。"""
    data = data if data is not None else load()
    bucket = "patterns" if "*" in model_id else "models"
    data.setdefault(bucket, {})[model_id.strip()] = {"in": in_}
    return data


def remove(model_id: str, data: dict | None = None) -> dict:
    data = data if data is not None else load()
    key = model_id.strip()
    for bucket in ("models", "patterns"):
        data.get(bucket, {}).pop(key, None)
    return data


def entries(data: dict) -> list[tuple[str, float | None, str]]:
    """给界面列表用：(id/pattern, 输入价, 来源标签)。"""
    out: list[tuple[str, float | None, str]] = []
    for mid, entry in sorted((data.get("models") or {}).items()):
        out.append((mid, _num(entry.get("in")), "精确"))
    for pattern, entry in sorted((data.get("patterns") or {}).items()):
        out.append((pattern, _num(entry.get("in")), "通配"))
    return out
