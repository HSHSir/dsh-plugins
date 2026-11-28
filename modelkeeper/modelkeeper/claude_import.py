#!/usr/bin/env python3
"""读写 Claude Code 的模型配置：`~/.claude/settings.json` 的 `env` 段。

**与另三个 Agent 的结构性差异**（这是本模块必须独立的原因）：

| | WorkBuddy | DeepSeek Harness | Codex | Claude Code |
|---|---|---|---|---|
| 载体 | JSON 数组 | YAML 数组 | TOML + JSON | **单个 JSON 的 env 字典** |
| 「模型」概念 | 每个条目一个模型 | 每个条目一个模型 | 目录条目 + 全局默认 | **没有模型清单，只有 5 个槽位** |
| 端点 | 每条自带 | 引用 provider | 全局 provider | 全局 `ANTHROPIC_BASE_URL` |
| 写一个模型 | 追加/更新条目 | 追加/更新条目 | 追加/更新条目 | **改写槽位变量，不产生新条目** |

所以本模块的「一行」= 一个槽位变量，而不是一个模型目录条目。

三个来自实测的硬约束（不是推测）：

1. **Base URL 不能带 `/v1`**。Claude Code 自己拼 `/v1/messages`，配成
   `https://host/v1` 会变成 `/v1/v1/messages`（404，客户端报
   "It may not exist or you may not have access to it"，看着像模型不存在，
   实际是路径重复）。DeepSeek 官方文档给的也是 `https://api.deepseek.com/anthropic`
   这种不带 `/v1` 的前缀，故 `normalize_base()` 会剥掉尾部 `/v1`。
2. **密钥写 `ANTHROPIC_AUTH_TOKEN`（Bearer）而非 `ANTHROPIC_API_KEY`**。
   `ANTHROPIC_API_KEY` 在自定义 baseURL 下会让客户端在首次启动时弹出
   "Do you want to use this API key?" 的交互确认；AUTH_TOKEN 不会。两者同时
   存在时 AUTH_TOKEN 优先，留着旧 API_KEY 会造成「看起来配的是 A，实际用 B」
   的静默错配，故写入时会移除它并明确告知。
3. **上下文与输出上限没有对应字段**。Claude Code 只有一个全局
   `CLAUDE_CODE_MAX_CONTEXT_TOKENS`（对所有槽位生效），没有「最大输出 token」
   开关，也没有逐模型的图片声明。导入对话框里这两项因此不落盘，只给警告。

写入原则与 `agent_import` / `codex_import` 一致：只改 `env` 里的目标键、其余
键与顶层其它段（theme / modelSettings / permissions / hooks）原样保留；
写前环形备份 5 份，备份失败即中止。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

from .model_spec import REASONING_LEVELS, ModelSpec, norm_id

from . import agent_import as ai

# Claude Code 的配置目录。`CLAUDE_CONFIG_DIR` 是官方支持的覆盖变量，
# 这里跟着它走：一来用户可能真改过，二来自测靠它做隔离，不必改写模块常量。
CLAUDE_HOME = Path.home() / ".claude"
SETTINGS_NAME = "settings.json"

# --------------------------------------------------------------------------- #
# 槽位（Claude Code 认的就是这 5 个环境变量名，见 DeepSeek 官方接入文档）
# --------------------------------------------------------------------------- #
SLOT_PRIMARY = "ANTHROPIC_MODEL"
SLOT_OPUS = "ANTHROPIC_DEFAULT_OPUS_MODEL"
SLOT_SONNET = "ANTHROPIC_DEFAULT_SONNET_MODEL"
SLOT_HAIKU = "ANTHROPIC_DEFAULT_HAIKU_MODEL"
SLOT_SUBAGENT = "CLAUDE_CODE_SUBAGENT_MODEL"

# 端点与凭据是另一类键：不属于「模型槽位」，但同样写在 env 里。
SLOT_BASE_URL = "ANTHROPIC_BASE_URL"
SLOT_AUTH_TOKEN = "ANTHROPIC_AUTH_TOKEN"
SLOT_API_KEY = "ANTHROPIC_API_KEY"          # 写入时会被移除（见模块说明第 2 条）
SLOT_CONTEXT = "CLAUDE_CODE_MAX_CONTEXT_TOKENS"
SLOT_EFFORT = "CLAUDE_CODE_EFFORT_LEVEL"

SLOT_ORDER: tuple[str, ...] = (
    SLOT_PRIMARY, SLOT_OPUS, SLOT_SONNET, SLOT_HAIKU, SLOT_SUBAGENT,
)

SLOT_LABELS: dict[str, str] = {
    SLOT_PRIMARY: "主模型",
    SLOT_OPUS: "Opus 槽",
    SLOT_SONNET: "Sonnet 槽",
    SLOT_HAIKU: "Haiku 槽",
    SLOT_SUBAGENT: "子代理",
}

# 多模型导入时按模型名里的角色词分流到对应槽位；匹配不到就落到主模型。
ROLE_TOKENS: tuple[tuple[str, str], ...] = (
    (SLOT_OPUS, "opus"),
    (SLOT_SONNET, "sonnet"),
    (SLOT_HAIKU, "haiku"),
)

NOTE = (
    "没有模型清单：只有 5 个槽位变量（主模型 / Opus / Sonnet / Haiku / 子代理），"
    "同名模型可同时占多个槽位。端点与密钥写在同一个 env 段，密钥走 "
    "ANTHROPIC_AUTH_TOKEN。上下文与「支持档位」两列读的是本机全局设置"
    "（CLAUDE_CODE_MAX_CONTEXT_TOKENS / CLAUDE_CODE_EFFORT_LEVEL），"
    "导入不会改它们；图片列恒为「否」——Claude Code 的配置里没有图片开关。"
)


def home() -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    return Path(override).expanduser() if override else CLAUDE_HOME


def settings_path() -> Path:
    return home() / SETTINGS_NAME


# --------------------------------------------------------------------------- #
# Base URL 归一化
# --------------------------------------------------------------------------- #


def normalize_base(base_url: str) -> tuple[str, str]:
    """把用户填的 Base URL 归一成 Claude Code 要求的「不带 /v1 的前缀」。

    返回 (归一后的 URL, 一句说明)；说明为空表示没改动。

    两步：先剥掉 `/chat/completions` 这类对话端点尾巴（用户常直接粘完整端点），
    再剥掉 `/v1`——Claude Code 自己会拼 `/v1/messages`，多一层就是 404。
    """
    original = (base_url or "").strip()
    base = ai.base_url_of(original).rstrip("/")
    if base.endswith("/v1") or base.endswith("/v1beta"):
        base = base.rsplit("/", 1)[0]
    if base == original.rstrip("/"):
        return base, ""
    return base, (
        f"Base URL 已归一成「{base}」：Claude Code 自己会拼 /v1/messages，"
        f"填「{original.rstrip('/')}」会变成 /v1/v1/messages 而 404。"
    )


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #


def _load(path: Path | None = None) -> tuple[Path, dict]:
    """settings.json -> dict。缺失视为空配置（首次写入时创建）。"""
    target = path or settings_path()
    if not target.exists():
        return target, {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ai.AgentConfigError(f"读取 {target} 失败：{e}") from e
    if not isinstance(raw, dict):
        raise ai.AgentConfigError(f"{target} 顶层不是对象，格式与预期不符")
    env = raw.get("env")
    if env is not None and not isinstance(env, dict):
        raise ai.AgentConfigError(f'{target} 的 "env" 不是对象，格式与预期不符')
    return target, raw


def _env_of(doc: dict) -> dict:
    env = doc.get("env")
    return env if isinstance(env, dict) else {}


def _level_from_effort(effort: str) -> str:
    """CLAUDE_CODE_EFFORT_LEVEL -> ModelSpec 档位。

    Codex 的最高档叫 xhigh，Claude Code 叫 max；两者语义同级，故 max 折成
    xhigh——否则 `_level_cap` 会把它当成未知值退到 off，表格显示「不支持」。
    """
    name = (effort or "").strip().lower()
    if name == "max":
        return "xhigh"
    return name if name in REASONING_LEVELS else "off"


def read_claude(path: Path | None = None) -> list[ai.AgentModel]:
    """每个**有值**的槽位一行。空槽位不出行——5 行里 4 行是空的没有意义。"""
    _target, doc = _load(path)
    env = _env_of(doc)
    base = str(env.get(SLOT_BASE_URL) or "").strip()
    key = str(env.get(SLOT_AUTH_TOKEN) or env.get(SLOT_API_KEY) or "")
    try:
        context = int(str(env.get(SLOT_CONTEXT) or "0").strip() or 0)
    except ValueError:
        context = 0
    level = _level_from_effort(str(env.get(SLOT_EFFORT) or ""))

    out: list[ai.AgentModel] = []
    for slot in SLOT_ORDER:
        mid = str(env.get(slot) or "").strip()
        if not mid:
            continue
        out.append(
            ai.AgentModel(
                agent="claude",
                model_id=mid,
                name=SLOT_LABELS.get(slot, slot),
                spec=ModelSpec(
                    context=context,
                    # 配置里没有图片开关，恒 False；能否识图只取决于模型本身。
                    vision=False,
                    reasoning_max=level,
                    reasoning_level=level if level != "off" else "off",
                    max_tokens=0,
                ),
                base_url=base,
                api_key=key,
                provider=slot,
                api="anthropic-messages",
                # 主模型槽就是「当前生效」的那一个，供界面「当前」列打勾
                is_active=(slot == SLOT_PRIMARY),
            )
        )
    return out


def find_model(model_id: str, *, path: Path | None = None) -> ai.AgentModel | None:
    """导入前预填用：同一个模型可能在多个槽位，优先返回主模型那一行。"""
    try:
        rows = read_claude(path)
    except ai.AgentConfigError:
        return None
    key = norm_id(model_id)
    hits = [r for r in rows if norm_id(r.model_id) == key]
    for row in hits:
        if row.provider == SLOT_PRIMARY:
            return row
    return hits[0] if hits else None


def active_model(path: Path | None = None) -> str:
    _t, doc = _load(path)
    return str(_env_of(doc).get(SLOT_PRIMARY) or "")


# --------------------------------------------------------------------------- #
# 槽位分配
# --------------------------------------------------------------------------- #


def assign_slots(entries: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """把一批待写模型分到 5 个槽位。

    规则（可预测、可解释，不臆造）：
      * 第一个模型占满 5 个槽位——用户导入单个模型时就是要它接管全部角色；
      * 批量导入时，名字里带 opus / sonnet / haiku 的各自认领对应槽位，
        同名冲突取靠前的那个；
      * 没被认领的槽位仍然落在第一个模型上（宁可全是主模型，也不留空槽——
        空槽会让 Claude Code 用回它自己的默认模型，那通常在这个端点上不存在）。
    """
    items = list(entries)
    if not items:
        return {}
    ids = [str(item["id"]) for item in items]
    primary = ids[0]
    out: dict[str, str] = {slot: primary for slot in SLOT_ORDER}
    for slot, token in ROLE_TOKENS:
        hit = next((i for i in ids if token in i.lower()), "")
        if hit:
            out[slot] = hit
    return out


def _mapping_note(mapping: Mapping[str, str]) -> str:
    """一行说清「哪个槽位拿到了哪个模型」——槽位规则不写出来就等于黑箱。"""
    return "槽位：" + " / ".join(
        f"{SLOT_LABELS.get(slot, slot)}={mapping[slot]}" for slot in SLOT_ORDER
    )


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #


def _prepare_env(doc: dict) -> dict:
    env = doc.get("env")
    if not isinstance(env, dict):
        env = doc["env"] = {}
    return env


def import_claude(
    models: list[Any],
    specs: Mapping[str, ModelSpec],
    *,
    base_url: str,
    api_key: str = "",
    protocol: str = "anthropic",
    path: Path | None = None,
) -> ai.ImportResult:
    """把模型写进 Claude Code 的 5 个槽位，并更新端点与密钥。"""
    target = path or settings_path()
    result = ai.ImportResult(target="Claude Code", path=target, unit="个槽位")
    entries = ai.spec_entries(models, specs)
    if not entries:
        result.error = "没有可写入的模型。"
        return result
    if protocol not in ("anthropic", "openai", ""):
        result.warnings.append(
            f"Claude Code 只说 Anthropic Messages 协议，已忽略所选协议「{protocol}」。"
        )
    elif protocol == "openai":
        result.warnings.append(
            "探测用的是 OpenAI 协议，但 Claude Code 只会请求 /v1/messages："
            "确认该端点在根路径下提供 Anthropic Messages 接口，否则启动即报模型不存在。"
        )

    base, note = normalize_base(base_url)
    if not base:
        result.error = "Base URL 为空。"
        return result
    if note:
        result.warnings.append(note)

    try:
        target, doc = _load(target)
    except ai.AgentConfigError as e:
        result.error = str(e) + "（原文件未改动）"
        return result
    env = _prepare_env(doc)

    mapping = assign_slots(entries)
    changed = 0
    for slot in SLOT_ORDER:
        mid = mapping[slot]
        if str(env.get(slot) or "") != mid:
            env[slot] = mid
            changed += 1
    if str(env.get(SLOT_BASE_URL) or "") != base:
        env[SLOT_BASE_URL] = base
        changed += 1

    key = (api_key or "").strip()
    if key:
        if str(env.get(SLOT_AUTH_TOKEN) or "") != key:
            env[SLOT_AUTH_TOKEN] = key
            changed += 1
        if SLOT_API_KEY in env:
            # 两个密钥变量并存时 AUTH_TOKEN 优先，旧的 API_KEY 会变成
            # 「看得见却没生效」的干扰项，删掉并说明，而不是留着让它误导人。
            env.pop(SLOT_API_KEY, None)
            changed += 1
            result.warnings.append(
                f"已移除 env.{SLOT_API_KEY}：与 ANTHROPIC_AUTH_TOKEN 并存时它不生效，"
                "留着只会让人以为在用另一把 Key。"
            )
        result.key_written = f"{target} → env.{SLOT_AUTH_TOKEN}"
    elif not (env.get(SLOT_AUTH_TOKEN) or env.get(SLOT_API_KEY)):
        result.warnings.append(
            f"没有可用的 API Key：env 里既无 {SLOT_AUTH_TOKEN} 也无 {SLOT_API_KEY}，"
            "Claude Code 启动时会要求登录或直接失败。"
        )

    # 对话框里这两项对 Claude Code 没有落点，必须说出来：静默忽略等于骗人。
    if any(item["spec"].context or item["spec"].max_tokens for item in entries):
        result.warnings.append(
            "上下文与最大输出没有逐模型字段（上限是全局 "
            f"env.{SLOT_CONTEXT}），本次未写入。"
        )

    if not changed:
        result.warnings.append("槽位与端点已是目标值，未改动文件。")
        return result

    try:
        result.backup = ai.backup_file(target)
    except Exception as e:  # noqa: BLE001
        result.error = f"备份 {target} 失败，已中止（未改动原文件）：{e}"
        return result
    try:
        ai.atomic_write_text(target, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    except Exception as e:  # noqa: BLE001
        result.error = f"写入 {target} 失败：{e}"
        return result

    result.updated = changed
    result.warnings.append(_mapping_note(mapping))
    result.warnings.append("改配置后需重启 Claude Code（新开会话）才会加载新槽位。")
    return result


def delete_claude(
    items: Iterable[tuple[str, str]],
    *,
    path: Path | None = None,
) -> ai.ImportResult:
    """清空指定槽位。items = [(槽位变量名, 期望的模型 ID), …]。

    不删端点与密钥：它们被其余槽位共用，连带删掉会让还在用的模型一起失效。
    `model_id` 参与校验——读表到点删除之间槽位可能已被改过，那时按行号盲删
    会删掉用户刚写进去的另一个模型。
    """
    target = path or settings_path()
    result = ai.ImportResult(target="Claude Code", path=target, unit="个槽位")
    pairs = [(str(slot), str(mid)) for slot, mid in items if str(slot or "").strip()]
    if not pairs:
        result.error = "没有指定要清空的槽位。"
        return result

    try:
        target, doc = _load(target)
    except ai.AgentConfigError as e:
        result.error = str(e) + "（原文件未改动）"
        return result
    env = _env_of(doc)
    if not env:
        result.error = f"{target} 里没有 env 段，未改动。"
        return result

    for slot, mid in pairs:
        if slot not in SLOT_LABELS:
            result.skipped.append(f"{slot}（不是模型槽位）")
            continue
        current = str(env.get(slot) or "").strip()
        if not current:
            result.skipped.append(f"{slot}（本来就是空的）")
            continue
        if norm_id(current) != norm_id(mid):
            result.skipped.append(
                f"{slot}（现值已是 {current}，不是 {mid}，未删除）"
            )
            continue
        env.pop(slot, None)
        result.removed += 1

    if not result.removed:
        result.error = "没有槽位被清空：" + "；".join(result.skipped or ["未改动"])
        return result
    if not str(env.get(SLOT_PRIMARY) or "").strip():
        result.warnings.append(
            f"主模型槽（{SLOT_PRIMARY}）已清空：Claude Code 下次启动会用它自己的默认"
            "模型名，在该端点上通常不存在，建议用「设为当前模型」补一个。"
        )

    try:
        result.backup = ai.backup_file(target)
    except Exception as e:  # noqa: BLE001
        result.error = f"备份 {target} 失败，已中止（未改动任何文件）：{e}"
        return result
    try:
        ai.atomic_write_text(target, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    except Exception as e:  # noqa: BLE001
        result.error = f"写入 {target} 失败：{e}"
        return result
    result.warnings.append("需重启 Claude Code 才会生效。")
    return result


def set_active(model_id: str, *, path: Path | None = None) -> ai.ImportResult:
    """把某个槽位里的模型提升为主模型（只改 `ANTHROPIC_MODEL`）。"""
    target = path or settings_path()
    result = ai.ImportResult(target="Claude Code", path=target, unit="个槽位")
    mid = (model_id or "").strip()
    if not mid:
        result.error = "没有指定模型。"
        return result
    try:
        target, doc = _load(target)
    except ai.AgentConfigError as e:
        result.error = str(e)
        return result
    env = _prepare_env(doc)
    if str(env.get(SLOT_PRIMARY) or "") == mid:
        result.error = f"「{mid}」已经是主模型，未改动。"
        return result
    env[SLOT_PRIMARY] = mid
    try:
        result.backup = ai.backup_file(target)
    except Exception as e:  # noqa: BLE001
        result.error = f"备份失败，已中止：{e}"
        return result
    try:
        ai.atomic_write_text(target, json.dumps(doc, ensure_ascii=False, indent=2) + "\n")
    except Exception as e:  # noqa: BLE001
        result.error = f"写入失败：{e}"
        return result
    result.updated = 1
    result.warnings.append("需重启 Claude Code 才会用新主模型。")
    return result


# --------------------------------------------------------------------------- #
# 状态
# --------------------------------------------------------------------------- #


def installed() -> bool:
    return settings_path().exists() or home().exists()


def status() -> tuple[bool, str]:
    """(能不能管, 一句话)。目录在就算装过——settings.json 缺失时首次写入会创建。"""
    target = settings_path()
    if target.exists():
        return True, str(target)
    if home().exists():
        return True, f"{home()}（还没有 settings.json，首次写入时创建）"
    return False, (
        f"未检测到 Claude Code：{home()} 不存在。"
        "安装并启动过一次 Claude Code 之后再点「刷新」。"
    )
