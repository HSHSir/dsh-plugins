#!/usr/bin/env python3
"""读写 Agent 的模型配置：WorkBuddy 与 DeepSeek Harness。

两个目标，配置格式均取自本机真实文件（不是猜的）：

**WorkBuddy** —— `~/.workbuddy/models.json`，顶层是模型对象数组：
    {id, name, vendor, url, apiKey, supportsToolCall, supportsImages,
     supportsReasoning, useCustomProtocol, reasoning{defaultEffort,supportedEfforts},
     maxInputTokens, maxOutputTokens}

**DeepSeek Harness** —— `~/.dsh/profiles/desktop/cordis.patch.yml`，
顶层是 loader patch 数组，模型挂在 `llm-pi-ai` 条目下：
    - id: llm-pi-ai
      config:
        providers:
          <providerId>: {apiKeyEnv, api, baseURL, models: [{id, name, input, contextWindow, maxTokens}]}

写入原则：
1. **只增改目标模型**，其余条目原样保留（按 id 归并，不重复插入）。
2. 每次增删前把目标文件快照到模型管家自己的备份目录（`BACKUP_DIR`），
   每个目标只保留最新一份（本次覆盖上次），备份路径随结果返回。
3. WorkBuddy 的模型条目要求 apiKey 字段，Key 从界面输入框传入（工具自身不落盘）；
   DSH 只写 `apiKeyEnv` 环境变量名，密钥单独进 `.credentials.yaml` 的 refs。
4. 模型参数（上下文 / 图片 / 推理档位）由 `ModelSpec` 显式给出——模型库已取消，
   不再有「从库里猜一个值」的路径。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

from .model_spec import (
    DEFAULT_REASONING,
    DEFAULT_REASONING_MAX,
    REASONING_LEVELS,
    ModelSpec,
    norm_id,
)

WORKBUDDY_MODELS = Path.home() / ".workbuddy" / "models.json"
DSH_PATCH = Path.home() / ".dsh" / "profiles" / "desktop" / "cordis.patch.yml"
# DSH 的密钥仓库：refs.<apiKeyEnv> = 明文密钥（本机实测结构，version/refs/records 三段）
DSH_CREDENTIALS = Path.home() / ".dsh" / ".credentials.yaml"
DSH_ENTRY_ID = "llm-pi-ai"
DSH_DEFAULT_MODEL_ID = "agent-default-model"

# 备份目录：归模型管家自己管，不再在原配置文件旁边撒副本。
# 用户明确要求「只备份最新一次」，所以这是一个「每个目标一份」的快照目录，
# 而不是带时间戳的累积存档。
BACKUP_DIR = Path.home() / ".model_probe" / "backups"

# 环形备份保留的份数。第 1 份恒为 `<原名>.bak`，其余 `<原名>.2.bak` … `<原名>.5.bak`。
# 5 份的取舍：能覆盖「改错了，连上一次也不对」这种常见后悔场景，同时目录体积仍可忽略。
BACKUP_SLOTS = 5

# 探测协议 -> DSH 的 api 枚举（已在本机 DSH 包里核对过这四个取值）
DSH_API = {
    "openai": "openai-completions",
    "anthropic": "anthropic-messages",
    "gemini": "google-generative-ai",
    "ollama": "openai-completions",
}

# DSH 的 api 枚举 -> 探测协议（读回配置时用来还原「这份配置该用哪个协议探」）
DSH_PROTOCOL = {v: k for k, v in DSH_API.items()}
DSH_PROTOCOL["openai-completions"] = "openai"

# --------------------------------------------------------------------------- #
# 权威格式（取自本机真实文件，不靠推测）
# --------------------------------------------------------------------------- #
# WorkBuddy `~/.workbuddy/models.json` 单个模型条目的字段顺序。
# 写成有序列表而非 set，因为 JSON 保留插入顺序，界面与人工阅读都依赖它。
WORKBUDDY_KEY_ORDER: tuple[str, ...] = (
    "id", "name", "vendor", "url", "apiKey",
    "supportsToolCall", "supportsImages", "supportsReasoning", "useCustomProtocol",
    "reasoning", "maxInputTokens", "maxOutputTokens", "think",
)


class AgentConfigError(Exception):
    """目标配置文件读不了 / 结构不认识。带着人话说明抛给界面显示。"""


def _reorder(body: dict[str, Any], order: tuple[str, ...]) -> dict[str, Any]:
    """按权威字段顺序重排；权威表里没有的额外字段按原顺序续在后面，不丢弃。

    重建而非原地改，是因为 `dict.update()` 只对新增键追加，无法把已有键挪到正确位置。
    """
    out: dict[str, Any] = {}
    for key in order:
        if key in body:
            out[key] = body[key]
    for key, value in body.items():
        if key not in out:
            out[key] = value
    return out


@dataclass
class ImportResult:
    target: str
    path: Path | None = None
    backup: Path | None = None
    added: int = 0
    updated: int = 0
    removed: int = 0
    restored: int = 0
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    key_written: str = ""      # 密钥写到了哪里（环境变量名或文件路径）
    extra_backups: list[Path] = field(default_factory=list)
    # 计数单位：其它 Agent 增删的是「模型条目」，Claude Code 增删的是「槽位变量」。
    # 用「条」去说 8 个 env 键会读成「8 个模型」，那是错的。
    unit: str = "条"

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def backups(self) -> list[Path]:
        out = [self.backup] if self.backup else []
        return out + list(self.extra_backups)

    def summary(self) -> str:
        if not self.ok:
            return f"{self.target} 写入失败：{self.error}"
        parts = []
        if self.added:
            parts.append(f"新增 {self.added} {self.unit}")
        if self.updated:
            parts.append(f"更新 {self.updated} {self.unit}")
        if self.removed:
            parts.append(f"删除 {self.removed} {self.unit}")
        if self.restored:
            parts.append("已还原备份")
        if self.skipped:
            parts.append(f"跳过 {len(self.skipped)} {self.unit}")
        return f"{self.target}：" + ("，".join(parts) if parts else "无变化")


# --------------------------------------------------------------------------- #
# 公共工具
# --------------------------------------------------------------------------- #


def backup_dir() -> Path:
    """备份目录；`MODEL_PROBE_BACKUP_DIR` 可覆盖它。

    留这个环境变量是为了自测：测试会把目标文件放在临时目录里，如果备份仍写真实
    家目录，跑一次测试就顶掉用户真正的回滚点——那是拿安全网换绿灯。
    """
    override = os.environ.get("MODEL_PROBE_BACKUP_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return BACKUP_DIR


def backup_path_for(path: Path, slot: int = 1) -> Path:
    """某个目标配置的第 `slot` 份快照（1 = 最新）。

    目录放在模型管家这里，所以不管往几个 Agent 写多少次，备份都只有这一处。
    第 1 份沿用旧名字「原名 + .bak」，升级前留下的那份正好就是「上一次写入前」
    的样子，因此不需要迁移；第 2 份起是「原名 + .2.bak」…「.5.bak」。

    将来若加了同名但不同路径的目标，名字里必须补上父目录，否则会互相覆盖——
    那是静默毁掉回滚点，比没有备份更糟。
    """
    name = path.expanduser().name
    return backup_dir() / (f"{name}.bak" if slot <= 1 else f"{name}.{slot}.bak")


def backup_snapshots(path: Path) -> list[Path]:
    """现有的快照，从新到旧；空列表表示这个目标还没被写过。"""
    out: list[Path] = []
    for slot in range(1, BACKUP_SLOTS + 1):
        candidate = backup_path_for(path, slot)
        if candidate.exists():
            out.append(candidate)
    return out


def backup_choices(path: Path) -> list[tuple[Path, str]]:
    """给界面用：[(快照文件, 一句话说明)]，从新到旧。"""
    out: list[tuple[Path, str]] = []
    for index, snap in enumerate(backup_snapshots(path), start=1):
        try:
            stamp = datetime.fromtimestamp(snap.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            stamp = "时间未知"
        tag = " · 最新" if index == 1 else ""
        out.append((snap, f"第 {index} 份 · {stamp}{tag}"))
    return out


def _backup(path: Path) -> Path | None:
    """写前备份：把目标文件快照进备份目录，环形保留最近 `BACKUP_SLOTS` 份。

    只留一份时，连写两次就把「最初正确的样子」永久顶掉了——而「改错了、上一次
    写进去的也是错的」恰恰是最常见的后悔场景。带时间戳无限累积又会堆成没人敢删
    的垃圾，所以取中间：固定 5 个槽位，最老的自动淘汰。

    目标文件不存在则无需备份（第一次创建配置时没有「上一版」可言）。

    先写临时文件再 `os.replace`：备份是唯一的后悔药，它自己不能写一半。
    """
    if not path.exists():
        return None
    dest = backup_path_for(path, 1)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # 顺移一格：最老的丢掉，其余依次后移，空出第 1 份给这次的新快照
    oldest = backup_path_for(path, BACKUP_SLOTS)
    if oldest.exists():
        oldest.unlink()
    for slot in range(BACKUP_SLOTS - 1, 0, -1):
        src = backup_path_for(path, slot)
        if src.exists():
            os.replace(src, backup_path_for(path, slot + 1))

    fd, tmp = tempfile.mkstemp(prefix=f".{dest.name}-", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(path.read_bytes())
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return dest


def backup_file(path: Path) -> Path | None:
    """对任意目标文件做一次写前快照。

    公开入口供其他 Agent 适配器（如 `codex_import`）复用同一套环形备份，
    避免每加一个 Agent 就复制一份备份逻辑。
    """
    return _backup(path)


def atomic_write_text(path: Path, text: str) -> None:
    """原子写入文本：先写同目录临时文件再 replace。公开版，供适配器复用。"""
    _atomic_write_text(path, text)


def backup_note(path: Path) -> str:
    """一句话说明这份快照是哪儿的、什么时候写的。

    环形备份的路径是固定的，所以「这次备份成功了」和「这次根本没备份」在界面上
    长得一样；必须把快照时间带上，用户才能判断它是新鲜的。
    """
    try:
        stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return str(path)
    return f"{path}（{stamp} 快照）"


def has_backup(kind: str) -> bool:
    """这个 Agent 有没有可还原的备份。"""
    return bool(backup_snapshots(agent_config_path(kind)))


# ---- 多文件 Agent 的通用备份/还原 ---------------------------------------- #
# Codex 这类「一次写两个文件」的 Agent，两个文件的环形队列是同一次写入一起推进的，
# 所以**槽位号对齐**：恢复 slot N 就是恢复那次写入之前的全貌。按槽位还原而不是
# 按文件各自取最新，能避免「config.toml 退到第 2 次、models.json 还停在第 1 次」
# 这种自相矛盾的中间态。


def has_backup_paths(paths: list[Path]) -> bool:
    return any(backup_snapshots(p) for p in paths)


def backup_choices_for(paths: list[Path]) -> list[tuple[int, str]]:
    """[(槽位号, 说明)]，从新到旧。以主文件（第一个）的快照为准。"""
    if not paths:
        return []
    primary = paths[0]
    out: list[tuple[int, str]] = []
    for index, snap in enumerate(backup_snapshots(primary), start=1):
        try:
            stamp = datetime.fromtimestamp(snap.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            stamp = "时间未知"
        tag = " · 最新" if index == 1 else ""
        out.append((index, f"第 {index} 份 · {stamp}{tag}"))
    return out


def restore_slots(label: str, paths: list[Path], slot: int = 1) -> ImportResult:
    """把 `paths` 全部恢复到第 `slot` 份快照。缺失的那份跳过（新文件当初还不存在）。"""
    result = ImportResult(target=label, path=paths[0] if paths else None)
    restored = 0
    for path in paths:
        src = backup_path_for(path, slot)
        if not src.exists():
            if slot == 1 and not backup_snapshots(path):
                continue          # 这个文件本来就没有备份（例如首次创建）
            result.warnings.append(f"{path.name} 缺少第 {slot} 份快照，已跳过。")
            continue
        try:
            atomic_write_text(path, src.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            result.error = f"还原 {path} 失败：{e}"
            return result
        restored += 1
    if not restored:
        result.error = (
            f"没有可还原的备份：{backup_dir()} 里没有 {label} 的快照。先做一次增删才会产生备份。"
        )
        return result
    result.restored = restored
    result.backup = backup_path_for(paths[0], slot)
    return result


def restore_backup(kind: str, path: Path | None = None,
                   snapshot: Path | None = None,
                   label: str = "") -> ImportResult:
    """用某一份快照覆盖当前配置，等于把那次修改撤掉。

    `snapshot` 不指定时用最新的一份；界面上有多份时让用户挑（见 `backup_choices`）。

    `label` 供新增的 Agent 传自己的显示名：原来这里按 kind 硬编码两个名字，
    第四个 Agent 接进来后，结果摘要会张冠李戴成「DeepSeek Harness」。

    刻意**不**在还原前再备份一次：那会把「当前状态」挤进环形队列，把用户想退回的
    那份往后推一格，最后甚至挤出队尾。快照本身也不会被消耗，所以可以反复还原。
    """
    target = path or agent_config_path(kind)
    name = label or ("WorkBuddy" if kind == "workbuddy" else "DeepSeek Harness")
    result = ImportResult(target=name, path=target)
    if snapshot is None:
        snaps = backup_snapshots(target)
        if not snaps:
            result.error = (
                f"没有可还原的备份：{backup_dir()} 里没有 {target.name} 的快照。"
                "先做一次增删才会产生备份。"
            )
            return result
        src = snaps[0]
    else:
        src = snapshot
    if not src.exists():
        result.error = f"没有可还原的备份：{src} 不存在。先做一次增删才会产生备份。"
        return result
    try:
        data = src.read_bytes()
    except Exception as e:  # noqa: BLE001
        result.error = f"读取备份失败：{e}"
        return result
    try:
        _atomic_write_bytes(target, data)
    except Exception as e:  # noqa: BLE001
        result.error = f"还原 {target} 失败：{e}"
        return result
    result.restored = 1
    result.backup = src
    return result


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def chat_url(base_url: str) -> str:
    """把 baseURL 变成 WorkBuddy 需要的完整 chat/completions 端点。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/chat/completions"):
        return base
    if base.endswith(("/v1", "/v1beta", "/openai/v1", "/api")):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def agent_config_path(kind: str) -> Path:
    """某个 Agent 的配置文件路径（本工具唯一认的「它装没装」的依据）。"""
    return WORKBUDDY_MODELS if kind == "workbuddy" else DSH_PATCH


def agent_installed(kind: str) -> bool:
    """这台机器上有没有该 Agent 的配置。

    判定只看配置文件在不在：不去查注册表 / 开始菜单 / 进程，因为「装了但一次都
    没启动过」和「根本没装」在本工具的能力范围内是同一件事——没有配置可管。
    """
    return agent_config_path(kind).exists()


def agent_status(kind: str) -> tuple[bool, str]:
    """(能不能管, 给人看的一句话)。不可用时把「缺什么、该怎么办」写清楚。"""
    path = agent_config_path(kind)
    name = "WorkBuddy" if kind == "workbuddy" else "DeepSeek Harness"
    if path.exists():
        return True, str(path)
    return False, f"未检测到 {name}：找不到 {path}。安装并启动过一次之后再点「刷新」。"


def base_url_of(endpoint: str) -> str:
    """`…/v1/chat/completions` → `…/v1`。

    「送去探测」要把配置里的完整端点还原成探测页要的 Base URL；直接砍掉尾部会让
    `…/openai/v1` 这种多段路径变成 `…/openai`，探测请求就打错地方了。
    """
    url = (endpoint or "").strip().rstrip("/")
    for tail in ("/chat/completions", "/messages", "/completions"):
        if url.endswith(tail):
            return url[: -len(tail)]
    return url


def provider_id(base_url: str) -> str:
    """从主机名派生 DSH 的 provider 键：api.a6api.com -> a6api；IP / 空值 -> custom。"""
    host = (urlsplit((base_url or "").strip()).netloc or "").lower()
    host = host.split(":")[0]
    if not host or re.fullmatch(r"[\d.]+", host):
        return "custom"
    labels = [p for p in host.split(".") if p]
    if labels[0] in ("api", "www", "gw", "gateway") and len(labels) > 1:
        labels = labels[1:]
    key = re.sub(r"[^a-z0-9-]+", "-", labels[0]).strip("-")
    return key or "custom"


def api_key_env(base_url: str) -> str:
    return provider_id(base_url).upper().replace("-", "_") + "_API_KEY"


def bare_key(key: str) -> str:
    """去掉 `sk-` 前缀再比较：同一把钥匙在客户端里常被写成 `sk-…`。

    中转站发给客户端的 Key 带前缀、站点接口返回的令牌本体不带，直接比字符串
    永远对不上——这里统一归一，返回值仍由调用方决定写哪种写法。
    """
    text = (key or "").strip()
    if text.lower().startswith("sk-"):
        text = text[3:]
    return text


def _slug(text: str) -> str:
    """把「a6-2」这类令牌名压成可做 provider 键的字符集。"""
    return re.sub(r"[^a-z0-9-]+", "-", (text or "").strip().lower()).strip("-")


def new_dsh_provider(
    providers: Mapping[str, Any], base_url: str, label: str = ""
) -> tuple[str, str]:
    """给「同一 baseURL、另一把 Key」的新 provider 取名，返回 (provider 键, 环境变量名)。

    名字带令牌标签（`a6api-a6-2`）而不是干巴巴的 `a6api-2`，是为了让用户在 DSH 的
    模型列表里一眼分出哪个是哪个令牌——两行同名模型只靠序号记不住。
    环境变量名同样要唯一：两个 provider 共用一个槽位，等于后写的 Key 顶掉前一个。
    """
    stem = provider_id(base_url)
    slug = _slug(label)
    base_pid = f"{stem}-{slug}" if slug and slug != stem else stem

    pid, n = base_pid, 2
    while pid in providers:
        pid = f"{base_pid}-{n}"
        n += 1

    used = {
        str(conf.get("apiKeyEnv"))
        for conf in providers.values()
        if isinstance(conf, dict) and conf.get("apiKeyEnv")
    }
    env = pid.upper().replace("-", "_") + "_API_KEY"
    core = env[: -len("_API_KEY")]
    while env in used:
        env = f"{core}_{n}_API_KEY"
        n += 1
    return pid, env


def dsh_provider_key(conf: Mapping[str, Any], keys: Mapping[str, str]) -> str:
    """该 provider 当前用的那把 Key（从 .credentials.yaml 的 refs 里取）。"""
    env = str((conf or {}).get("apiKeyEnv") or "")
    return str(keys.get(env) or "") if env else ""


def _same_host(url_a: str, url_b: str) -> bool:
    try:
        return (urlsplit(url_a).netloc or "").lower() == (urlsplit(url_b).netloc or "").lower()
    except ValueError:
        return False


# --------------------------------------------------------------------------- #
# 读回现有配置
# --------------------------------------------------------------------------- #


@dataclass
class AgentModel:
    """一个已经存在于 Agent 配置里的模型，供管理页展示与「送去探测」。"""

    agent: str                 # "workbuddy" | "dsh"
    model_id: str
    name: str
    spec: ModelSpec
    base_url: str = ""
    api_key: str = ""
    provider: str = ""         # dsh 专属：provider 键
    api_key_env: str = ""      # dsh 专属
    api: str = ""              # dsh 专属：openai-completions 等
    is_active: bool = False    # 是否为该 Agent 的「当前生效模型」
    missing: bool = False      # 配置里引用了但目标文件里不存在的条目（悬空引用）

    @property
    def endpoint_hint(self) -> str:
        return f"{self.provider} · {self.api}" if self.provider else self.base_url

    @property
    def protocol(self) -> str:
        """该行「送去探测」时该用哪个协议。

        直接按 `api` 反查：`anthropic-messages` 是 DSH 与 Claude Code 共用的写法，
        Codex 的 `responses` 不在表里，落回 openai（探测页只有这四种协议）。
        """
        return DSH_PROTOCOL.get(self.api, "openai")


def _level_cap(levels: Iterable[str], fallback: str = DEFAULT_REASONING_MAX) -> str:
    """从一组档位名里取最高的那个（按 REASONING_LEVELS 的顺序，不是字典序）。"""
    idx = max(
        (REASONING_LEVELS.index(lv) for lv in levels if lv in REASONING_LEVELS),
        default=-1,
    )
    return REASONING_LEVELS[idx] if idx >= 0 else fallback


def read_workbuddy(path: Path | None = None) -> list[AgentModel]:
    target = path or WORKBUDDY_MODELS
    if not target.exists():
        return []
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise AgentConfigError(f"读取 {target} 失败：{e}") from e
    if not isinstance(raw, list):
        raise AgentConfigError(f"{target} 顶层不是数组，格式与预期不符")

    out: list[AgentModel] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("id") or "").strip()
        if not mid:
            continue
        block = item.get("reasoning") if isinstance(item.get("reasoning"), dict) else {}
        supports = bool(item.get("supportsReasoning"))
        supported = [str(x) for x in (block.get("supportedEfforts") or [])]
        if not supports:
            # 明确关掉推理的条目：档位上限就是 off，别再臆测它能选 high。
            max_level = "off"
        elif supported:
            max_level = _level_cap(supported)
        else:
            max_level = DEFAULT_REASONING_MAX
        out.append(
            AgentModel(
                agent="workbuddy",
                model_id=mid,
                name=str(item.get("name") or mid),
                spec=ModelSpec(
                    context=int(item.get("maxInputTokens") or 0) or 0,
                    vision=bool(item.get("supportsImages")),
                    reasoning_max=max_level,
                    reasoning_level=str(block.get("defaultEffort") or DEFAULT_REASONING),
                    max_tokens=int(item.get("maxOutputTokens") or 0) or 0,
                ),
                base_url=base_url_of(str(item.get("url") or "")),
                api_key=str(item.get("apiKey") or ""),
            )
        )
    return out


def _load_yaml():
    """按 YAML 1.2 口径取 Loader/Dumper。缺 PyYAML 时给出可执行的报错。"""
    try:
        import yaml  # noqa: PLC0415
    except ImportError as e:  # pragma: no cover - 环境问题
        raise RuntimeError(
            "需要 PyYAML 才能读写 DeepSeek Harness 的 YAML 配置：pip install pyyaml"
        ) from e

    class Loader(yaml.SafeLoader):
        pass

    class Dumper(yaml.SafeDumper):
        pass

    resolvers = _yaml12_resolvers()
    Loader.yaml_implicit_resolvers = resolvers
    Dumper.yaml_implicit_resolvers = resolvers
    Dumper.add_representer(None, _unknown)
    return yaml, Loader, Dumper


# PyYAML 默认按 YAML **1.1** 解析，会把裸键 `off` / `no` / `on` 当成布尔值。
# 这在本场景是**真实的静默损坏**：DSH 的 `reasoningEfforts` 有一个 `off` 档位，
# 读进来变成 False、写回去变成 `false:`，语义就跑偏了。
# 因此 Loader 与 Dumper 都必须对齐 YAML 1.2 core：只有 true/false 才是布尔。
_YAML12_BOOL = re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$")


def _yaml12_resolvers() -> dict:
    """把 PyYAML 内置的隐式解析器换成 YAML 1.2 口径。"""
    import yaml  # noqa: PLC0415

    out: dict = {}
    for ch, entries in yaml.SafeLoader.yaml_implicit_resolvers.items():
        out[ch] = [(tag, rx) for (tag, rx) in entries if tag != "tag:yaml.org,2002:bool"]
    for ch in ("t", "T", "f", "F"):
        out[ch] = out.get(ch, []) + [("tag:yaml.org,2002:bool", _YAML12_BOOL)]
    return out


def _unknown(loader, node):
    """把不认识的标签当作普通映射/序列还原，避免 DSH 自定义标签导致整份文件写不回去。"""
    if isinstance(node, node.__class__.__mro__[0]) and hasattr(node, "value"):
        return loader.construct_scalar(node)
    return None


def _dsh_doc(path: Path | None, yaml, loader_cls) -> tuple[Path, list]:
    target = path or DSH_PATCH
    if not target.exists():
        return target, []
    try:
        data = yaml.load(target.read_text(encoding="utf-8"), Loader=loader_cls) or []
    except Exception as e:  # noqa: BLE001
        raise AgentConfigError(f"解析 {target} 失败：{e}（原文件未改动）") from e
    if not isinstance(data, list):
        raise AgentConfigError(f"{target} 顶层不是数组，格式与预期不符")
    return target, data


def _dsh_providers(data: list) -> dict:
    for entry in data:
        if isinstance(entry, dict) and entry.get("id") == DSH_ENTRY_ID:
            config = entry.get("config")
            if isinstance(config, dict):
                providers = config.get("providers")
                if isinstance(providers, dict):
                    return providers
    return {}


def _dsh_default_level(data: list) -> tuple[str, str, str]:
    """读 `agent-default-model`：返回 (provider, model, reasoningEffort)。

    DSH 的模型条目本身没有「默认推理档」字段——默认档全局配在这一条里。
    编辑某个模型的默认档时，只有当它是当前默认模型，这个值才对得上。
    """
    for entry in data:
        if isinstance(entry, dict) and entry.get("id") == DSH_DEFAULT_MODEL_ID:
            config = entry.get("config") if isinstance(entry.get("config"), dict) else {}
            return (
                str(config.get("provider") or ""),
                str(config.get("model") or ""),
                str(config.get("reasoningEffort") or ""),
            )
    return "", "", ""


def read_dsh(path: Path | None = None, *, credentials_path: Path | None = None) -> list[AgentModel]:
    yaml, loader_cls, _dumper = _load_yaml()
    target, data = _dsh_doc(path, yaml, loader_cls)
    providers = _dsh_providers(data)
    def_provider, def_model, def_level = _dsh_default_level(data)
    keys = read_dsh_credentials(credentials_path)

    out: list[AgentModel] = []
    for pid, conf in providers.items():
        if not isinstance(conf, dict):
            continue
        models = conf.get("models")
        if not isinstance(models, list):
            continue
        env_name = str(conf.get("apiKeyEnv") or "")
        base = str(conf.get("baseURL") or "")
        api = str(conf.get("api") or "")
        for item in models:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("id") or "").strip()
            if not mid:
                continue
            efforts = item.get("reasoningEfforts")
            if isinstance(efforts, dict) and efforts:
                max_level = _level_cap([str(k) for k in efforts])
            else:
                max_level = "off"
            inputs = item.get("input")
            vision = isinstance(inputs, list) and "image" in [str(x) for x in inputs]
            level = (
                def_level
                if (def_model and norm_id(def_model) == norm_id(mid) and def_provider == str(pid))
                else ""
            )
            out.append(
                AgentModel(
                    agent="dsh",
                    model_id=mid,
                    name=str(item.get("name") or mid),
                    spec=ModelSpec(
                        context=int(item.get("contextWindow") or 0) or 0,
                        vision=vision,
                        reasoning_max=max_level,
                        reasoning_level=level or DEFAULT_REASONING,
                        max_tokens=int(item.get("maxTokens") or 0) or 0,
                    ),
                    base_url=base,
                    api_key=keys.get(env_name, ""),
                    provider=str(pid),
                    api_key_env=env_name,
                    api=api,
                )
            )
    return out


def read_dsh_credentials(path: Path | None = None) -> dict[str, str]:
    """refs.<env> = 明文密钥。读不到就当空，不因为没密钥把整个列表读失败。"""
    target = path or DSH_CREDENTIALS
    if not target.exists():
        return {}
    try:
        yaml, loader_cls, _dumper = _load_yaml()
        doc = yaml.load(target.read_text(encoding="utf-8"), Loader=loader_cls)
    except Exception:  # noqa: BLE001 - 凭据文件坏了不该让模型列表读不出来
        return {}
    if not isinstance(doc, dict):
        return {}
    refs = doc.get("refs")
    if not isinstance(refs, dict):
        return {}
    return {str(k): str(v) for k, v in refs.items() if isinstance(v, str)}


def find_agent_model(
    agent: str,
    model_id: str,
    *,
    provider: str = "",
    path: Path | None = None,
    credentials_path: Path | None = None,
) -> AgentModel | None:
    """在目标配置里找一条同名模型，供导入对话框预填现值。

    `path` / `credentials_path` 只为自测与「指定文件」场景存在：不传就读真实配置。
    """
    try:
        if agent == "workbuddy":
            rows = read_workbuddy(path)
        else:
            rows = read_dsh(path, credentials_path=credentials_path)
    except (AgentConfigError, RuntimeError):
        return None
    key = norm_id(model_id)
    for row in rows:
        if norm_id(row.model_id) != key:
            continue
        if provider and row.provider != provider:
            continue
        return row
    return None


# --------------------------------------------------------------------------- #
# WorkBuddy
# --------------------------------------------------------------------------- #


def spec_entries(models: list[Any], specs: Mapping[str, ModelSpec]) -> list[dict[str, Any]]:
    """把探测行 + 用户在对话框里选定的规格合成待写条目（按 id 去重）。

    specs 以 `norm_id(model_id)` 为键；缺规格的模型落到默认规格上，
    但界面层总会先弹出对话框，所以这条兜底只在脚本直接调用时才走到。
    """
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for m in models:
        mid = (getattr(m, "id", "") or "").strip()
        if not mid:
            continue
        key = norm_id(mid)
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "id": mid,
                "name": (getattr(m, "name", "") or mid),
                "spec": (specs.get(key) or specs.get(mid) or ModelSpec()).clone(),
            }
        )
    return out


def _workbuddy_body(mid: str, name: str, spec: ModelSpec, url: str) -> dict[str, Any]:
    """一条 WorkBuddy 模型条目，字段顺序与真实文件一致。"""
    body: dict[str, Any] = {
        "id": mid,
        "name": name or mid,
        "vendor": "Custom",
        "url": url,
        "supportsToolCall": True,
        "supportsImages": bool(spec.vision),
        "supportsReasoning": spec.reasoning,
        "useCustomProtocol": False,
    }
    # 与真实文件一致：reasoning 块位于 useCustomProtocol 与 maxInputTokens 之间
    if spec.reasoning:
        body["reasoning"] = {
            "defaultEffort": spec.default_level(),
            "supportedEfforts": list(spec.levels),
        }
    if spec.context:
        body["maxInputTokens"] = int(spec.context)
    if spec.max_tokens:
        body["maxOutputTokens"] = int(spec.max_tokens)
    return body


def import_workbuddy(
    models: list[Any],
    specs: Mapping[str, ModelSpec],
    *,
    base_url: str,
    api_key: str = "",
    path: Path | None = None,
) -> ImportResult:
    target = path or WORKBUDDY_MODELS
    result = ImportResult(target="WorkBuddy", path=target)
    entries = spec_entries(models, specs)
    if not entries:
        result.error = "没有可写入的模型。"
        return result

    url = chat_url(base_url)
    if not url:
        result.error = "Base URL 为空，无法生成 chat/completions 端点。"
        return result

    try:
        raw = json.loads(target.read_text(encoding="utf-8")) if target.exists() else []
    except Exception as e:  # noqa: BLE001
        result.error = f"读取 {target} 失败：{e}"
        return result
    if not isinstance(raw, list):
        result.error = f"{target} 顶层不是数组，格式与预期不符，已中止（未写入）。"
        return result

    by_id: dict[str, dict[str, Any]] = {}
    for item in raw:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            by_id[norm_id(item["id"])] = item

    key = (api_key or "").strip()
    if not key:
        result.warnings.append("没有可用的 API Key：新写入的模型在 WorkBuddy 里要补 Key 才能用。")

    for entry in entries:
        mid = entry["id"]
        body = _workbuddy_body(mid, entry["name"], entry["spec"], url)
        existing = by_id.get(norm_id(mid))
        if existing is None:
            # 新增条目按权威顺序排：apiKey 应紧跟 url
            new_body = _reorder({**body, "apiKey": key}, WORKBUDDY_KEY_ORDER)
            raw.append(new_body)
            by_id[norm_id(mid)] = new_body
            result.added += 1
        else:
            # 已有条目：保留其未知字段（如 think）与旧 Key，只覆盖本工具负责的参数
            merged = {**existing, **body}
            # 同主机的旧端点保留原路径（用户可能手填了非标准路径），换主机才更新
            old_url = existing.get("url") or ""
            if old_url and _same_host(old_url, url):
                merged["url"] = old_url
            old_key = str(existing.get("apiKey") or "").strip()
            if key:
                if old_key and bare_key(old_key) != bare_key(key):
                    # WorkBuddy 的模型清单以 id 唯一，同一模型挂不了第二把 Key：
                    # 与其让用户以为「加了一个」，不如说清这次是替换。
                    result.warnings.append(
                        f"{mid} 已存在且 Key 不同，已替换为本次填写的 Key。"
                        "WorkBuddy 的模型清单以模型 id 唯一，同一模型无法并存两条；"
                        "若要让两个令牌的同名模型并存，请导入到 DeepSeek Harness。"
                    )
                merged["apiKey"] = key
            elif not merged.get("apiKey"):
                result.warnings.append(f"{mid} 没有可用 Key，写入后需在 WorkBuddy 里补。")
            new_body = _reorder(merged, WORKBUDDY_KEY_ORDER)
            existing.clear()
            existing.update(new_body)
            result.updated += 1

    # 备份失败就中止：没有回滚点的写入等于裸奔，宁可什么都不做。
    try:
        result.backup = _backup(target)
    except Exception as e:  # noqa: BLE001
        result.error = f"备份 {target} 失败，已中止（未改动原文件）：{e}"
        return result
    try:
        _atomic_write_text(target, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
    except Exception as e:  # noqa: BLE001
        result.error = f"写入 {target} 失败：{e}"
    return result


def delete_workbuddy(model_ids: Iterable[str], path: Path | None = None) -> ImportResult:
    target = path or WORKBUDDY_MODELS
    result = ImportResult(target="WorkBuddy", path=target)
    want = {norm_id(x) for x in model_ids if (x or "").strip()}
    if not want:
        result.error = "没有指定要删除的模型。"
        return result
    try:
        raw = json.loads(target.read_text(encoding="utf-8")) if target.exists() else []
    except Exception as e:  # noqa: BLE001
        result.error = f"读取 {target} 失败：{e}"
        return result
    if not isinstance(raw, list):
        result.error = f"{target} 顶层不是数组，格式与预期不符，已中止（未改动）。"
        return result

    keep = [
        item for item in raw
        if not (isinstance(item, dict) and norm_id(str(item.get("id") or "")) in want)
    ]
    removed = len(raw) - len(keep)
    if not removed:
        result.error = "配置里没有找到这些模型，未改动。"
        return result

    # 备份失败就中止：没有回滚点的删除不可接受
    try:
        result.backup = _backup(target)
    except Exception as e:  # noqa: BLE001
        result.error = f"备份 {target} 失败，已中止（未改动原文件）：{e}"
        return result
    try:
        _atomic_write_text(target, json.dumps(keep, ensure_ascii=False, indent=2) + "\n")
    except Exception as e:  # noqa: BLE001
        result.error = f"写入 {target} 失败：{e}"
        return result
    result.removed = removed
    return result


# --------------------------------------------------------------------------- #
# DeepSeek Harness
# --------------------------------------------------------------------------- #


def _dsh_body(mid: str, name: str, spec: ModelSpec) -> dict[str, Any]:
    """一条 DSH 模型条目：字段顺序 id, name, [contextWindow], input, reasoningEfforts, maxTokens。"""
    body: dict[str, Any] = {"id": mid, "name": name or mid}
    if spec.context:
        body["contextWindow"] = int(spec.context)
    if spec.vision:
        body["input"] = ["text", "image"]
    # 支持推理才写 reasoningEfforts：这是 DSH 侧声明推理档位的唯一方式，
    # 缺了它模型在 DSH 里只能走默认行为，无法选 off/low/medium/high/xhigh。
    if spec.reasoning:
        body["reasoningEfforts"] = spec.efforts()
    # 输出上限：0 表示不声明，交给提供方默认值（写了反而可能比默认更小）
    if spec.max_tokens:
        body["maxTokens"] = int(spec.max_tokens)
    return body


def import_dsh(
    models: list[Any],
    specs: Mapping[str, ModelSpec],
    *,
    base_url: str,
    protocol: str = "openai",
    api_key: str = "",
    key_label: str = "",
    path: Path | None = None,
    credentials_path: Path | None = None,
) -> ImportResult:
    target = path or DSH_PATCH
    result = ImportResult(target="DeepSeek Harness", path=target)
    entries = spec_entries(models, specs)
    if not entries:
        result.error = "没有可写入的模型。"
        return result

    base = (base_url or "").strip().rstrip("/")
    if not base:
        result.error = "Base URL 为空。"
        return result
    api = DSH_API.get(protocol)
    if api is None:
        result.error = f"未知协议 {protocol}。"
        return result

    try:
        yaml, loader_cls, dumper_cls = _load_yaml()
        target, data = _dsh_doc(target, yaml, loader_cls)
    except RuntimeError as e:
        result.error = str(e)
        return result
    except AgentConfigError as e:
        result.error = str(e)
        return result

    entry = next((e for e in data if isinstance(e, dict) and e.get("id") == DSH_ENTRY_ID), None)
    if entry is None:
        entry = {"id": DSH_ENTRY_ID, "name": "@deepseek-ai/dsh-llm-pi-ai", "config": {"providers": {}}}
        data.append(entry)
    config = entry.setdefault("config", {})
    providers = config.setdefault("providers", {})
    if not isinstance(providers, dict):
        result.error = "llm-pi-ai.config.providers 不是映射，已中止（未写入）。"
        return result

    # 同一个 baseURL 下可能有多把不同的 Key（同一中转站的多个令牌，各自能调不同模型）。
    # DSH 里 provider 才是「一个地址 + 一把密钥」的单位，所以匹配必须同时看地址和密钥：
    # 只按地址复用，第二次导入会把上一把 Key 顶掉，两条同名模型也只剩一条。
    keys = read_dsh_credentials(credentials_path or DSH_CREDENTIALS)
    want = bare_key(api_key)
    same_base = [
        (name, conf) for name, conf in providers.items()
        if isinstance(conf, dict) and (str(conf.get("baseURL") or "").rstrip("/") == base)
    ]

    pid = None
    reuses_same_key = False
    if want:
        for name, conf in same_base:
            if bare_key(dsh_provider_key(conf, keys)) == want:
                pid = name
                reuses_same_key = True
                break
    elif same_base:
        pid = same_base[0][0]        # 没带 Key 的导入不该凭空多出一个 provider

    if pid is None and len(same_base) == 1:
        # 老配置里 provider 已在，但凭据文件没有对应条目（手填、换过机器、或被清掉）：
        # 认领它而不是再造一个，否则用户会看到两个 baseURL 一模一样的 provider。
        name, conf = same_base[0]
        if not dsh_provider_key(conf, keys):
            pid = name
            result.warnings.append(f"沿用 provider「{name}」并写入新密钥。")

    if pid is None:
        pid, env_name = new_dsh_provider(providers, base, key_label)
        providers[pid] = {
            "apiKeyEnv": env_name,
            "api": api,
            "baseURL": base,
            "models": [],
        }
        if same_base:
            result.warnings.append(
                f"同一 Base URL 已存在其他 Key 的 provider，已新建「{pid}」"
                f"（密钥环境变量 {env_name}），两条并行存在、互不覆盖。"
            )
        else:
            result.warnings.append(f"新建 provider「{pid}」，密钥环境变量名 {env_name}。")

    provider = providers[pid]
    env_name = str(provider.get("apiKeyEnv") or api_key_env(base))
    existing_models = provider.setdefault("models", [])
    if not isinstance(existing_models, list):
        result.error = f"provider「{pid}」的 models 不是数组，已中止（未写入）。"
        return result
    index = {
        norm_id(str(m.get("id") or "")): m
        for m in existing_models
        if isinstance(m, dict)
    }

    for item in entries:
        mid = item["id"]
        body = _dsh_body(mid, item["name"], item["spec"])
        current = index.get(norm_id(mid))
        if current is None:
            existing_models.append(body)
            index[norm_id(mid)] = body
            result.added += 1
        else:
            current.clear()
            current.update(body)
            result.updated += 1

    # 备份失败就中止：没有回滚点的写入等于裸奔
    try:
        result.backup = _backup(target)
    except Exception as e:  # noqa: BLE001
        result.error = f"备份 {target} 失败，已中止（未改动原文件）：{e}"
        return result
    try:
        text = yaml.dump(
            data, Dumper=dumper_cls, allow_unicode=True, sort_keys=False,
            default_flow_style=False, width=100,
        )
        _atomic_write_text(target, text)
    except Exception as e:  # noqa: BLE001
        result.error = f"写入 {target} 失败：{e}"
        return result

    result.warnings.append(
        "改写会丢掉该 YAML 里的注释；写入前已快照到模型管家的备份目录（只留最新一份）。"
    )

    # 密钥：DSH 自己就是把明文放在 .credentials.yaml 的 refs.<apiKeyEnv> 下，
    # 所以这里同样写 refs，provider 侧继续用环境变量名引用。
    key = (api_key or "").strip()
    if key and reuses_same_key:
        # 同一把钥匙（可能只是 sk- 前缀写法不同）：不改写凭据文件，
        # 免得每导一次就把别人手写的写法覆盖一遍。
        result.key_written = f"refs.{env_name}（已存在，未改动）"
    elif key:
        try:
            cred_path = credentials_path or DSH_CREDENTIALS
            backup, written = _write_dsh_credential(env_name, key, cred_path, yaml, loader_cls, dumper_cls)
            if backup:
                result.extra_backups.append(backup)
            result.key_written = f"refs.{env_name} → {written}"
        except Exception as e:  # noqa: BLE001
            # 模型已写入成功，密钥失败单独报，不把整次写入判为失败
            result.warnings.append(f"密钥写入失败：{e}（模型已导入，请在 DSH 界面补填密钥）")
    else:
        result.warnings.append(
            f"没有可用的 API Key：DSH 会尝试读环境变量 {env_name}，读不到则该 provider 不可用。"
        )

    return result


def delete_dsh(
    items: Iterable[tuple[str, str]],
    path: Path | None = None,
) -> ImportResult:
    """删除 DSH 的模型条目。items = [(provider, model_id), …]。

    只删模型，不删 provider：provider 里还有别的模型，或者它的 baseURL/apiKeyEnv
    可能被 `agent-default-model` 之类别的条目引用，连带删掉会破坏别处配置。
    """
    target = path or DSH_PATCH
    result = ImportResult(target="DeepSeek Harness", path=target)
    want = {
        (str(pid), norm_id(mid))
        for pid, mid in items
        if (str(mid) or "").strip()
    }
    if not want:
        result.error = "没有指定要删除的模型。"
        return result

    try:
        yaml, loader_cls, dumper_cls = _load_yaml()
        target, data = _dsh_doc(target, yaml, loader_cls)
    except (RuntimeError, AgentConfigError) as e:
        result.error = str(e)
        return result

    providers = _dsh_providers(data)
    if not providers:
        result.error = "没找到 llm-pi-ai 的 provider 配置，未改动。"
        return result

    for pid, conf in providers.items():
        if not isinstance(conf, dict):
            continue
        models = conf.get("models")
        if not isinstance(models, list):
            continue
        keep = [
            m for m in models
            if not (isinstance(m, dict) and (str(pid), norm_id(str(m.get("id") or ""))) in want)
        ]
        removed = len(models) - len(keep)
        if removed:
            conf["models"] = keep
            result.removed += removed

    if not result.removed:
        result.error = "配置里没有找到这些模型，未改动。"
        return result

    # 备份失败就中止：没有回滚点的写入等于裸奔
    try:
        result.backup = _backup(target)
    except Exception as e:  # noqa: BLE001
        result.error = f"备份 {target} 失败，已中止（未改动原文件）：{e}"
        return result
    try:
        text = yaml.dump(
            data, Dumper=dumper_cls, allow_unicode=True, sort_keys=False,
            default_flow_style=False, width=100,
        )
        _atomic_write_text(target, text)
    except Exception as e:  # noqa: BLE001
        result.error = f"写入 {target} 失败：{e}"
        return result
    result.warnings.append("改写会丢掉该 YAML 里的注释；已保留最新一份备份。")
    return result


def _write_dsh_credential(
    env_name: str, key: str, path: Path, yaml, loader_cls, dumper_cls
) -> tuple[Path | None, str]:
    """把密钥写进 DSH 凭据库的 refs.<env_name>，保留 records 等其它内容。"""
    doc: dict[str, Any] = {}
    if path.exists():
        loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=loader_cls)
        if isinstance(loaded, dict):
            doc = loaded
        elif loaded is not None:
            raise ValueError(f"{path} 顶层不是映射，拒绝改写")
    if "version" not in doc:
        doc["version"] = 1
    refs = doc.get("refs")
    if not isinstance(refs, dict):
        refs = {}
        doc["refs"] = refs
    refs[env_name] = key

    backup = _backup(path)
    text = yaml.dump(doc, Dumper=dumper_cls, allow_unicode=True, sort_keys=False,
                     default_flow_style=False, width=100)
    _atomic_write_text(path, text)
    return backup, str(path)
