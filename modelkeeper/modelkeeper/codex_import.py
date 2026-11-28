#!/usr/bin/env python3
"""读写 Codex 的模型配置：`~/.codex/config.toml` + `~/.codex/models.json`。

**与 WorkBuddy / DSH 的结构性差异**（这是本模块必须独立的原因）：

| | WorkBuddy | DeepSeek Harness | Codex |
|---|---|---|---|
| 端点与密钥 | 每条模型自带 | 每条引用 provider | **全局 provider**，模型条目不带端点 |
| 模型元数据 | 少数字段 | 少数字段 | **强 schema 元数据对象**，缺字段即启动失败 |
| 文件数 | 1 | 2（patch + credentials） | 2（且都必须改） |

Codex 的模型条目是强 schema：少任何一个必填字段，客户端会直接报
`failed to parse model_catalog_json ... missing field` 并拒绝启动。
必填集已实测为 13 项：

    slug, display_name, description, default_reasoning_level,
    supported_reasoning_levels, shell_type, visibility, supported_in_api,
    priority, support_verbosity, truncation_policy, experimental_supported_tools,
    model_messages（或 base_instructions）

其中 `model_messages.instructions_template` 是 Codex 的行为准则（约 21K 字符的
系统提示词）。本模块**不自己编**，而是从现有 models.json 里继承一条已有条目的
模板——那是用户机器上真实存在的官方文本；只有目录为空时才退到内置精简模板，
并给出明确警告（精简模板会让该模型缺少完整的工具使用与安全规范）。

写入原则与 `agent_import` 一致：
1. 只增改目标模型，其余条目与 provider 原样保留；
2. 写前把 config.toml 与 models.json **各备份一份**（环形 5 槽），备份失败即中止；
3. config.toml 用**文本级局部改写**而非整份重序列化——标准库只有只读的 tomllib，
   整份重建会丢掉用户的注释与未知字段；
4. 密钥写 `experimental_bearer_token`（与 DSH 的 .credentials.yaml 同级：本机明文）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .model_spec import DEFAULT_REASONING, ModelSpec, norm_id

from . import agent_import as ai

CODEX_HOME = Path.home() / ".codex"
CODEX_CONFIG = CODEX_HOME / "config.toml"
CODEX_MODELS = CODEX_HOME / "models.json"

# Codex 只认 Responses 协议。0.166 起 `wire_api = "chat"` 会在启动时被直接拒绝
# （`is no longer supported`），没有任何降级路径，所以这里是唯一合法值。
CODEX_WIRE_API = "responses"

# ModelSpec 档位 -> Codex 的 effort 列表。Codex 侧没有「关闭推理」这一档
# （最小是 low），因此 off 落到最低可用档 low。
CODEX_EFFORTS_BY_LEVEL: dict[str, tuple[str, ...]] = {
    "off": ("low",),
    "low": ("low",),
    "medium": ("low", "medium"),
    "high": ("low", "medium", "high"),
    "xhigh": ("low", "medium", "high", "xhigh"),
}

EFFORT_DESC = {
    "low": "Fast responses with lighter reasoning",
    "medium": "Balances speed and reasoning depth for everyday tasks",
    "high": "Greater reasoning depth for complex problems",
    "xhigh": "Extra high reasoning depth for complex problems",
}

# 每条条目都写死的固定字段：均为 schema 必填项或行为开关，不属于用户可调参数，
# 因此不暴露到界面上。
CODEX_FIXED: dict[str, Any] = {
    "shell_type": "unified_exec",
    "visibility": "list",
    "supported_in_api": True,
    "support_verbosity": True,
    "default_verbosity": "low",
    "apply_patch_tool_type": "freeform",
    "web_search_tool_type": "text_and_image",
    "truncation_policy": {"mode": "tokens", "limit": 10000},
    "supports_parallel_tool_calls": True,
    "supports_reasoning_summaries": True,
    "supports_reasoning_summary_parameter": True,
    "default_reasoning_summary": "none",
    "use_responses_lite": False,
    "prefer_websockets": False,
    "multi_agent_version": "v2",
    "experimental_supported_tools": [],
    "include_skills_usage_instructions": False,
    "include_plugin_usage_instructions": False,
    "include_apps_usage_instructions": False,
    "minimal_client_version": "0.144.0",
    "comp_hash": "3000",
    "node_repl_auto_review_required": False,
    "node_repl_disabled": False,
    "availability_nux": None,
    "upgrade": None,
}

# 目录为空时的兜底模板。**故意写得短**：它只保证 Codex 能启动，不保证行为完整；
# 实际使用时会向用户明确警告。
FALLBACK_INSTRUCTIONS = (
    "You are Codex, a coding agent. You and the user share one workspace, and your "
    "job is to collaborate with them until their goal is genuinely handled. Prefer "
    "reading the code before changing it, make the smallest change that solves the "
    "problem, and verify your work before reporting it done."
)

MODEL_MESSAGES_SHELL: dict[str, Any] = {
    "instructions_variables": None,
    "approvals": None,
    "collaboration_modes": None,
    "auto_review": None,
    "permissions": None,
    "multi_agent": None,
}


class CodexConfigError(Exception):
    """Codex 配置读不了 / 结构不认识。带人话说明抛给界面。"""


# --------------------------------------------------------------------------- #
# 极简 TOML 行级编辑器
# --------------------------------------------------------------------------- #
# 标准库只有 `tomllib`（只读）。整份重序列化会丢掉注释与未知字段，而 config.toml
# 里有大量 plugins / marketplaces / mcp_servers 段是用户手写且带注释的，重建等于
# 破坏配置。所以只做「按行定位、替换或插入单个键」。

_HEADER_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(?:#.*)?$")


def _toml_literal(value: Any) -> str:
    """Python 值 -> TOML 字面量。

    字符串直接用 JSON 编码：JSON 的转义集合（`\\"` `\\\\` `\\n` `\\t` `\\uXXXX`）
    与 TOML basic string 完全兼容，省掉自己维护一份转义表。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_literal(v) for v in value) + "]"
    raise TypeError(f"不支持的 TOML 值类型：{type(value)!r}")


def _section_bounds(lines: list[str], section: str | None) -> tuple[int, int] | None:
    """返回 `section` 段的 (表头行号, 段尾行号[不含])；`None` 表示顶层。

    顶层没有表头行，故返回 `(-1, 第一个表头行号)`。找不到该段则返回 None。
    """
    if section is None:
        for idx, line in enumerate(lines):
            if _HEADER_RE.match(line):
                return (-1, idx)
        return (-1, len(lines))
    start = -1
    for idx, line in enumerate(lines):
        m = _HEADER_RE.match(line)
        if not m:
            continue
        if start >= 0:
            return (start, idx)
        if m.group(1).strip() == section:
            start = idx
    if start >= 0:
        return (start, len(lines))
    return None


def toml_set(text: str, section: str | None, key: str, value: Any) -> str:
    """在 `section` 内设置 `key = value`：存在则就地替换，否则段内追加。

    `section=None` 表示顶层键（必须落在第一个表头之前）。段不存在时在文件末尾补建。
    """
    literal = _toml_literal(value)
    lines = text.splitlines()
    bounds = _section_bounds(lines, section)
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=")
    tail = "\n" if text.endswith("\n") else ""

    if bounds is None:
        out = lines[:]
        while out and not out[-1].strip():
            out.pop()
        out.extend(["", f"[{section}]", f"{key} = {literal}"])
        return "\n".join(out) + "\n"

    start, end = bounds
    body_from = start + 1 if start >= 0 else 0
    for idx in range(body_from, end):
        if key_re.match(lines[idx]):
            lines[idx] = f"{key} = {literal}"
            return "\n".join(lines) + tail

    # 键不在此段：插到该段最后一个非空行之后，保持在段内
    insert_at = body_from
    for idx in range(body_from, end):
        if lines[idx].strip():
            insert_at = idx + 1
    lines.insert(insert_at, f"{key} = {literal}")
    return "\n".join(lines) + tail


def toml_get(text: str, section: str | None, key: str) -> str | None:
    """读一个键的原始字面量文本（不解析），供「值没变就不动文件」的短路用。"""
    lines = text.splitlines()
    bounds = _section_bounds(lines, section)
    if bounds is None:
        return None
    start, end = bounds
    body_from = start + 1 if start >= 0 else 0
    key_re = re.compile(rf"^\s*{re.escape(key)}\s*=\s*(.*)$")
    for idx in range(body_from, end):
        m = key_re.match(lines[idx])
        if m:
            return m.group(1).split("#")[0].strip()
    return None


def parse_toml(text: str) -> dict:
    """用标准库解析；失败时抛 CodexConfigError。"""
    try:
        import tomllib  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover - Python < 3.11
        raise CodexConfigError(
            "读取 Codex 的 TOML 需要 Python 3.11+（标准库 tomllib）。"
        ) from e
    try:
        return tomllib.loads(text)
    except Exception as e:  # noqa: BLE001
        raise CodexConfigError(f"config.toml 解析失败：{e}") from e


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #


def _load_config(path: Path | None = None) -> tuple[Path, str, dict]:
    target = path or CODEX_CONFIG
    if not target.exists():
        return target, "", {}
    try:
        text = target.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        raise CodexConfigError(f"读取 {target} 失败：{e}") from e
    return target, text, parse_toml(text)


def _load_models(path: Path | None = None) -> tuple[Path, dict]:
    """models.json -> {"models": [...]}。缺失视为空目录，损坏则报错不覆盖。"""
    target = path or CODEX_MODELS
    if not target.exists():
        return target, {"models": []}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise CodexConfigError(f"读取 {target} 失败：{e}") from e
    if isinstance(raw, list):
        raw = {"models": raw}
    if not isinstance(raw, dict):
        raise CodexConfigError(f"{target} 顶层不是对象，格式与预期不符")
    if not isinstance(raw.get("models"), list):
        raise CodexConfigError(f'{target} 缺少 "models" 数组，格式与预期不符')
    return target, raw


def _entries(doc: dict) -> list[dict]:
    return [m for m in doc.get("models") or [] if isinstance(m, dict)]


def _find_provider(cfg: dict, base_url: str) -> tuple[str, dict] | None:
    """按 baseURL 找已存在的 provider（去尾部斜杠、忽略大小写比较）。"""
    want = (base_url or "").strip().rstrip("/").lower()
    providers = cfg.get("model_providers")
    if not isinstance(providers, dict):
        return None
    for pid, conf in providers.items():
        if not isinstance(conf, dict):
            continue
        if (str(conf.get("base_url") or "").strip().rstrip("/").lower()) == want:
            return str(pid), conf
    return None


def _level_of_efforts(efforts: Iterable[str]) -> str:
    """Codex 的 effort 列表 -> ModelSpec 的档位上限（取最高）。"""
    names = [str(e) for e in efforts]
    for name in ("xhigh", "high", "medium", "low"):
        if name in names:
            return name
    return "off"


def read_codex(
    path: Path | None = None,
    *,
    models_path: Path | None = None,
) -> list[ai.AgentModel]:
    """读 config.toml 的默认 provider + models.json 的模型条目。"""
    _cfg_path, _text, cfg = _load_config(path)
    _m_path, doc = _load_models(models_path)

    providers = cfg.get("model_providers")
    providers = providers if isinstance(providers, dict) else {}
    active_pid = str(cfg.get("model_provider") or "")
    active_model = str(cfg.get("model") or "")
    conf = providers.get(active_pid)
    conf = conf if isinstance(conf, dict) else {}
    base = str(conf.get("base_url") or "")
    key = str(conf.get("experimental_bearer_token") or "")

    out: list[ai.AgentModel] = []
    for item in _entries(doc):
        slug = str(item.get("slug") or "").strip()
        if not slug:
            continue
        levels = [
            str(lv.get("effort") or "")
            for lv in (item.get("supported_reasoning_levels") or [])
            if isinstance(lv, dict)
        ]
        modalities = item.get("input_modalities")
        vision = isinstance(modalities, list) and "image" in [str(x) for x in modalities]
        default_level = str(item.get("default_reasoning_level") or "")
        if default_level not in ("off", "low", "medium", "high", "xhigh"):
            default_level = DEFAULT_REASONING

        out.append(
            ai.AgentModel(
                agent="codex",
                model_id=slug,
                name=str(item.get("display_name") or slug),
                spec=ModelSpec(
                    context=int(item.get("context_window") or 0) or 0,
                    vision=vision,
                    reasoning_max=_level_of_efforts(levels),
                    reasoning_level=default_level,
                    # Codex 目录没有「最大输出 token」字段（truncation_policy 是截断
                    # 策略，不是输出上限），故恒为 0，界面显示空。
                    max_tokens=0,
                ),
                base_url=base,
                api_key=key,
                provider=active_pid,
                api_key_env="",
                api=CODEX_WIRE_API,
            )
        )
    # 标出当前实际在用的那一条（用显式字段，不靠改 name 字符串——
    # 界面要按它渲染「当前」列，字符串拼接一旦被别的逻辑改写就会静默失效）
    for row in out:
        if norm_id(row.model_id) == norm_id(active_model):
            row.is_active = True

    # 悬空默认值：config.toml 的 model 指向目录里不存在的条目时，Codex 启动会直接
    # 报错（`The model 'xxx' does not exist`），而界面上原本看不出任何异常。
    # 这里补一条占位行，让「删了模型没改默认值」「回滚到不匹配的快照」当场可见。
    if active_model and norm_id(active_model) not in {
        norm_id(str(m.get("slug") or "")) for m in _entries(doc)
    }:
        missing = ai.AgentModel(
            agent="codex",
            model_id=active_model,
            name=f"{active_model}（默认模型不存在）",
            spec=ModelSpec(),
            base_url=base,
            api_key=key,
            provider=active_pid,
            api=CODEX_WIRE_API,
        )
        missing.missing = True
        out.append(missing)
    return out


def active_model(path: Path | None = None) -> str:
    _p, _t, cfg = _load_config(path)
    return str(cfg.get("model") or "")


# --------------------------------------------------------------------------- #
# 条目构造
# --------------------------------------------------------------------------- #


def _template_messages(doc: dict) -> tuple[dict, str]:
    """取一份可复用的 model_messages。

    优先继承已有条目里的官方长模板（用户机器上真实存在），其次才用内置精简版。
    返回 (model_messages, 来源说明)。
    """
    for item in _entries(doc):
        mm = item.get("model_messages")
        if isinstance(mm, dict) and isinstance(mm.get("instructions_template"), str) \
                and mm["instructions_template"].strip():
            body = dict(mm)
            for key, value in MODEL_MESSAGES_SHELL.items():
                body.setdefault(key, value)
            return body, "本机已有条目"
        base = item.get("base_instructions")
        if isinstance(base, str) and base.strip():
            return ({"instructions_template": base, **MODEL_MESSAGES_SHELL}, "本机已有条目")
    return ({"instructions_template": FALLBACK_INSTRUCTIONS, **MODEL_MESSAGES_SHELL}, "内置精简模板")


def _efforts(spec: ModelSpec) -> list[dict]:
    names = CODEX_EFFORTS_BY_LEVEL.get(spec.reasoning_max, CODEX_EFFORTS_BY_LEVEL["xhigh"])
    return [{"effort": n, "description": EFFORT_DESC[n]} for n in names]


def build_entry(
    slug: str,
    name: str,
    spec: ModelSpec,
    messages: dict,
    *,
    priority: int = 100,
) -> dict:
    """一条 Codex 模型目录条目；字段集已实测可通过 schema 校验。"""
    ctx = int(spec.context or 0) or 272000
    efforts = _efforts(spec)
    default_level = spec.reasoning_level
    if default_level not in {e["effort"] for e in efforts}:
        default_level = efforts[-1]["effort"] if efforts else "low"

    body: dict[str, Any] = {
        "slug": slug,
        "display_name": name or slug,
        "description": f"{name or slug}（由模型管家写入）",
        "default_reasoning_level": default_level,
        "supported_reasoning_levels": efforts,
        "context_window": ctx,
        "max_context_window": ctx,
        "input_modalities": ["text", "image"] if spec.vision else ["text"],
        "supports_image_detail_original": bool(spec.vision),
        "supports_search_tool": True,
        "priority": int(priority),
        "model_messages": messages,
    }
    body.update(CODEX_FIXED)
    return body


def _next_priority(doc: dict) -> int:
    nums = [int(m.get("priority") or 0) for m in _entries(doc)]
    return (max(nums) if nums else 0) + 10


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #


def _backup_both(cfg_path: Path, m_path: Path) -> list[Path]:
    """两个文件各备份一份；任一失败即抛，由调用方中止写入。"""
    out: list[Path] = []
    for path in (cfg_path, m_path):
        snap = ai.backup_file(path)
        if snap:
            out.append(snap)
    return out


def import_codex(
    models: list[Any],
    specs: Mapping[str, ModelSpec],
    *,
    base_url: str,
    api_key: str = "",
    path: Path | None = None,
    models_path: Path | None = None,
    protocol: str = "openai",
) -> ai.ImportResult:
    """把模型写进 Codex：更新 provider、默认模型与模型目录。

    `protocol` 仅为与其他 Agent 保持同一调用签名；Codex 只支持 responses，
    传入其他值会被忽略并附一条警告。
    """
    cfg_path = path or CODEX_CONFIG
    m_path = models_path or CODEX_MODELS
    result = ai.ImportResult(target="Codex", path=cfg_path)
    entries = ai.spec_entries(models, specs)
    if not entries:
        result.error = "没有可写入的模型。"
        return result
    if protocol not in ("openai", ""):
        result.warnings.append(
            f"Codex 只走 Responses 协议，已忽略所选协议「{protocol}」。"
        )

    base = (base_url or "").strip().rstrip("/")
    if not base:
        result.error = "Base URL 为空。"
        return result

    try:
        _cfg_path, text, cfg = _load_config(cfg_path)
        _m_path, doc = _load_models(m_path)
    except CodexConfigError as e:
        result.error = str(e) + "（原文件未改动）"
        return result

    # ---- provider：同 baseURL 复用，否则按主机名新建 ----
    found = _find_provider(cfg, base)
    if found:
        pid = found[0]
    else:
        pid = ai.provider_id(base)
        providers = cfg.get("model_providers")
        providers = providers if isinstance(providers, dict) else {}
        suffix = 2
        while pid in providers:
            pid = f"{ai.provider_id(base)}-{suffix}"
            suffix += 1
        result.warnings.append(f'新建 provider「{pid}」（[model_providers.{pid}]）。')

    section = f"model_providers.{pid}"
    text = toml_set(text, section, "name", pid)
    text = toml_set(text, section, "base_url", base)
    text = toml_set(text, section, "wire_api", CODEX_WIRE_API)
    text = toml_set(text, section, "requires_openai_auth", False)
    key = (api_key or "").strip()
    if key:
        text = toml_set(text, section, "experimental_bearer_token", key)
        result.key_written = f"{cfg_path} → [{section}] experimental_bearer_token"
    else:
        result.warnings.append(
            "没有可用的 API Key：Codex 会读不到凭据，需在 config.toml 的 "
            f"[{section}] 里补 experimental_bearer_token。"
        )

    # ---- 顶层：模型目录 / provider / 默认模型 ----
    text = toml_set(text, None, "model_catalog_json", str(m_path))
    text = toml_set(text, None, "model_provider", pid)
    # 默认模型只在「当前为空」或「目标已不在目录里」时才改，避免导入一个探测结果
    # 就把用户正在用的模型悄悄换掉。
    current_default = str(cfg.get("model") or "")
    existing_slugs = {norm_id(str(m.get("slug") or "")) for m in _entries(doc)}
    first_slug = entries[0]["id"]
    if not current_default or norm_id(current_default) not in existing_slugs:
        text = toml_set(text, None, "model", first_slug)
        result.warnings.append(f'默认模型已设为「{first_slug}」（原默认值不可用或为空）。')

    # ---- 模型目录 ----
    messages, source = _template_messages(doc)
    if source == "内置精简模板":
        result.warnings.append(
            "models.json 里没有可继承的官方模板，新条目使用了内置精简提示词："
            "模型能跑，但缺少 Codex 的完整行为规范。建议先保留一个已有模型启动一次，"
            "待目录里出现完整条目后再导入。"
        )

    index = {norm_id(str(m.get("slug") or "")): m for m in _entries(doc)}
    priority = _next_priority(doc)
    for item in entries:
        slug = item["id"]
        body = build_entry(slug, item["name"], item["spec"], messages, priority=priority)
        priority += 10
        current = index.get(norm_id(slug))
        if current is None:
            doc["models"].append(body)
            index[norm_id(slug)] = body
            result.added += 1
        else:
            # 已有条目：保留其原 model_messages（用户可能已定制过），只覆盖参数
            keep = current.get("model_messages")
            if isinstance(keep, dict) and keep.get("instructions_template"):
                body["model_messages"] = keep
            current.clear()
            current.update(body)
            result.updated += 1

    if any(item["spec"].max_tokens for item in entries):
        result.warnings.append(
            "Codex 模型目录没有「最大输出 token」字段（truncation_policy 是截断策略，"
            "不是输出上限），该项已忽略。"
        )

    try:
        snaps = _backup_both(cfg_path, m_path)
    except Exception as e:  # noqa: BLE001
        result.error = f"备份失败，已中止（未改动任何文件）：{e}"
        return result
    if snaps:
        result.backup = snaps[0]
        result.extra_backups.extend(snaps[1:])

    try:
        ai.atomic_write_text(m_path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
        ai.atomic_write_text(cfg_path, text)
    except Exception as e:  # noqa: BLE001
        result.error = f"写入失败：{e}"
        return result

    result.warnings.append("改配置后需重启 Codex（或重新打开对话）才会加载新模型目录。")
    return result


def delete_codex(
    rows: Iterable[Any],
    *,
    path: Path | None = None,
    models_path: Path | None = None,
) -> ai.ImportResult:
    """只从 models.json 删条目，不动 config.toml 的 provider。

    provider 段可能被多个模型共用，连带删除会让其余模型一起失效。
    """
    cfg_path = path or CODEX_CONFIG
    m_path = models_path or CODEX_MODELS
    result = ai.ImportResult(target="Codex", path=cfg_path)
    want = {
        norm_id(getattr(r, "model_id", r) if not isinstance(r, str) else r)
        for r in rows
        if str(getattr(r, "model_id", r) or "").strip()
    }
    if not want:
        result.error = "没有指定要删除的模型。"
        return result

    try:
        _cfg_path, _text, cfg = _load_config(cfg_path)
        _m_path, doc = _load_models(m_path)
    except CodexConfigError as e:
        result.error = str(e) + "（原文件未改动）"
        return result

    keep = [m for m in _entries(doc) if norm_id(str(m.get("slug") or "")) not in want]
    removed = len(_entries(doc)) - len(keep)
    if not removed:
        result.error = "目录里没有找到这些模型，未改动。"
        return result
    doc["models"] = keep
    current = norm_id(str(cfg.get("model") or ""))
    if current in want:
        result.warnings.append(
            f"被删的模型里有当前的默认模型「{cfg.get('model')}」：Codex 下次启动可能因找不到"
            "它而回退或报错，建议把默认模型改成另一个再重启。"
        )

    try:
        snaps = _backup_both(cfg_path, m_path)
    except Exception as e:  # noqa: BLE001
        result.error = f"备份失败，已中止（未改动任何文件）：{e}"
        return result
    if snaps:
        result.backup = snaps[0]
        result.extra_backups.extend(snaps[1:])

    try:
        ai.atomic_write_text(m_path, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    except Exception as e:  # noqa: BLE001
        result.error = f"写入失败：{e}"
        return result
    result.removed = removed
    return result


def set_active(slug: str, *, path: Path | None = None) -> ai.ImportResult:
    """把某个模型设为 Codex 的默认模型（顶层 `model =`）。"""
    cfg_path = path or CODEX_CONFIG
    result = ai.ImportResult(target="Codex", path=cfg_path)
    slug = (slug or "").strip()
    if not slug:
        result.error = "没有指定模型。"
        return result
    try:
        _p, text, _cfg = _load_config(cfg_path)
    except CodexConfigError as e:
        result.error = str(e)
        return result
    new_text = toml_set(text, None, "model", slug)
    if new_text == text:
        result.error = f"「{slug}」已经是默认模型，未改动。"
        return result
    try:
        result.backup = ai.backup_file(cfg_path)
    except Exception as e:  # noqa: BLE001
        result.error = f"备份失败，已中止：{e}"
        return result
    try:
        ai.atomic_write_text(cfg_path, new_text)
    except Exception as e:  # noqa: BLE001
        result.error = f"写入失败：{e}"
        return result
    result.updated = 1
    result.warnings.append("需重启 Codex 才会用新默认模型。")
    return result


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #


def installed() -> bool:
    return CODEX_CONFIG.exists() or CODEX_MODELS.exists()


def status() -> tuple[bool, str]:
    """(能不能管, 一句话)。两个文件里只要有一个在，就认为装过。"""
    if CODEX_CONFIG.exists() and CODEX_MODELS.exists():
        return True, f"{CODEX_CONFIG} · {CODEX_MODELS}"
    if CODEX_CONFIG.exists():
        return True, f"{CODEX_CONFIG}（还没有 models.json，首次写入时创建）"
    if CODEX_MODELS.exists():
        return True, f"{CODEX_MODELS}（缺少 config.toml，导入时补上 provider）"
    return False, (
        f"未检测到 Codex：{CODEX_CONFIG} 与 {CODEX_MODELS} 都不存在。"
        "安装并启动过一次 Codex 之后再点「刷新」。"
    )


def find_model(slug: str, *, path: Path | None = None,
               models_path: Path | None = None) -> ai.AgentModel | None:
    try:
        rows = read_codex(path, models_path=models_path)
    except CodexConfigError:
        return None
    key = norm_id(slug)
    for row in rows:
        if norm_id(row.model_id) == key:
            return row
    return None
