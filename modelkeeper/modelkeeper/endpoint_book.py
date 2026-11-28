#!/usr/bin/env python3
"""端点簿记：本机记录「Base URL → API Key」配对，供探测页一键回填。

**为什么单独存一个文件**：`~/.model_probe.json` 是界面偏好，历来不碰密钥；
把密钥混进去会让「配置损坏能随手删掉」这条退路变成「删掉就丢 Key」。
两者分开，坏哪份都只坏一半。

**这是本机私密文件**：密钥以明文落盘（本工具定位是单人本机使用，不联网上报）。
文件在用户目录下，权限随用户目录继承；`save()` 会额外尝试收紧到 0600。
Windows 上 `os.chmod` 只能动只读位，真正的边界是用户账户本身。

同一 URL 只留一条：再次成功获取时原地替换成最新的 Key（见 `upsert`）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

DEFAULT_PATH = Path.home() / ".model_probe" / "endpoints.json"

# 记录条数上限。这不是功能限制，是防止长期使用后无人清理：
# 每条都含密钥，列表越长越该让人察觉「我存了多少把钥匙」。
MAX_ENTRIES = 20


def endpoint_path() -> Path:
    """可用 `MODEL_PROBE_ENDPOINT_FILE` 覆盖（自测用，避免污染真实记录）。"""
    override = os.environ.get("MODEL_PROBE_ENDPOINT_FILE", "").strip()
    return Path(override).expanduser() if override else DEFAULT_PATH


def norm_base(base: str) -> str:
    """去重用的比较键：去空白、去尾部斜杠、忽略大小写。

    同一条端点常被写成 `https://x.com/v1` 与 `https://x.com/v1/`；
    当成两条记录会让下拉里出现两个看起来一模一样的选项。
    """
    return (base or "").strip().rstrip("/").lower()


def empty() -> dict:
    return {"version": 1, "entries": []}


def load() -> dict:
    """读记录；文件缺失或损坏都返回空表（记录是便利功能，不该拖垮启动）。"""
    path = endpoint_path()
    data = empty()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return data
    if not isinstance(raw, dict):
        return data
    rows = raw.get("entries")
    if not isinstance(rows, list):
        return data
    clean: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        base = str(row.get("base") or "").strip()
        if not base:
            continue
        clean.append({
            "base": base,
            "key": str(row.get("key") or ""),
            "protocol": str(row.get("protocol") or "openai"),
            "updated": str(row.get("updated") or ""),
        })
    data["entries"] = clean[:MAX_ENTRIES]
    return data


def save(data: dict) -> Path:
    """原子写 + 尽力收紧权限：半截文件比丢一条记录更麻烦。"""
    path = endpoint_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass          # Windows 上只能动只读位，失败不该拦住保存
    os.replace(tmp, path)
    return path


def upsert(base: str, key: str, protocol: str = "openai", data: dict | None = None) -> dict:
    """记一条；同 URL 已在册时**原地更新**，并把它挪到最前。

    `key` 为空时不覆盖已有的 Key：只填了 URL 就点「获取模型」也能成功
    （Ollama 之类本来不要 Key），那种情况下把之前存的密钥抹掉纯属损失。
    """
    data = data if data is not None else load()
    rows: list[dict] = data.setdefault("entries", [])
    text = (base or "").strip()
    if not text:
        return data
    want = norm_base(text)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    kept: list[dict] = []
    for row in rows:
        if norm_base(row.get("base", "")) == want:
            old_key = str(row.get("key") or "")
            # 保留首次登记的写法（含/不含尾斜杠以第一次为准），只换 Key 与时间：
            # 每次都用「当前输入」覆盖，下拉里会出现 `x/v1` 和 `x/v1/` 交替跳动。
            kept.insert(0, {
                "base": str(row.get("base") or text),
                "key": key.strip() if key.strip() else old_key,
                "protocol": protocol or row.get("protocol") or "openai",
                "updated": stamp,
            })
        else:
            kept.append(row)
    if not kept or norm_base(kept[0].get("base", "")) != want:
        # 新条目放最前：下拉按「最近用过」排序，刚用过的就在第一项
        kept.insert(0, {"base": text, "key": key.strip(),
                        "protocol": protocol or "openai", "updated": stamp})
    data["entries"] = kept[:MAX_ENTRIES]
    return data


def entries(data: dict | None = None) -> list[dict]:
    """按最近使用排序列出。`updated` 缺失的历史记录沉底。"""
    data = data if data is not None else load()
    rows = list(data.get("entries") or [])
    rows.sort(key=lambda r: str(r.get("updated") or ""), reverse=True)
    return rows


def remove(base: str, data: dict | None = None) -> dict:
    data = data if data is not None else load()
    want = norm_base(base)
    data["entries"] = [r for r in (data.get("entries") or [])
                       if norm_base(r.get("base", "")) != want]
    return data


def clear(data: dict | None = None) -> dict:
    data = data if data is not None else load()
    data["entries"] = []
    return data


def mask_key(key: str) -> str:
    """界面提示用的掩码：能认出是哪把钥匙，又不把整串摊在屏幕上。"""
    text = (key or "").strip()
    if not text:
        return "（无 Key）"
    if len(text) <= 8:
        return "•" * len(text)
    return f"{text[:3]}…{text[-4:]}"


def find(base: str, data: dict | None = None) -> dict | None:
    """按 URL 找一条（已配好的下拉点击回填走这里，便于测试）。"""
    data = data if data is not None else load()
    want = norm_base(base)
    for row in data.get("entries") or []:
        if norm_base(row.get("base", "")) == want:
            return row
    return None
