#!/usr/bin/env python3
"""Agent 注册表：新增一个 Agent 只需在这里加一条，界面自动长出对应页签与按钮。

**为什么要有这一层**：原先把 `workbuddy` / `dsh` 写成散落在 GUI 各处的
`if kind == "workbuddy" else ...` 分支。加第三个 Agent 就要改 6 处，加第四个改 8 处，
而且总有一处漏改（例如右键菜单里少一个入口），表现为「功能在，但只有一种方式能进」。
把所有差异收敛成一个描述对象，界面只按描述渲染，差异就只有这一张表。

**差异必须显式建模，不能硬塞进同一模板**。四个 Agent 的真实结构差别：

| | WorkBuddy | DeepSeek Harness | Codex | Claude Code |
|---|---|---|---|---|
| 端点在模型条目里 | 是 | 否（引用 provider） | 否（全局 provider） | 否（全局 env） |
| 主配置文件格式 | JSON | YAML | TOML + JSON 双文件 | JSON 的 env 字典 |
| 行唯一键 | 模型 ID | provider + 模型 ID | 模型 ID | 槽位变量名 |
| 「模型」粒度 | 目录条目 | 目录条目 | 目录条目 | **只有 5 个槽位** |
| 「当前生效模型」概念 | 无 | 有（agent-default-model） | 有（顶层 model） | 有（ANTHROPIC_MODEL） |
| 行首列展示 | 端点 | Provider | Provider | 槽位名 |

`AgentDef.row_key` / `column_head` / `can_set_active` 就是把这几处差异显式化的地方。
`hidden_spec_fields` 则表达另一类差异：Claude Code 的配置里**没有**逐模型的
上下文 / 输出上限 / 图片开关，那几个输入框对它必须隐藏，而不是填了当没看见。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from . import agent_import as ai
from . import claude_import as cc
from . import codex_import as cx
from .model_spec import ModelSpec


@dataclass(frozen=True)
class AgentDef:
    """一个可管理的 Agent。字段全部是「界面需要知道的那几件事」。"""

    kind: str
    label: str                # 界面与弹窗里的正式名，如 "DeepSeek Harness"
    tab_label: str            # 页签标题
    button_label: str         # 「导入 XXX」按钮文案
    primary_path: Callable[[], Path]
    all_paths: Callable[[], list[Path]]
    read: Callable[..., list[ai.AgentModel]]
    import_fn: Callable[..., ai.ImportResult]
    delete_fn: Callable[..., ai.ImportResult]
    status_fn: Callable[[], tuple[bool, str]]
    supports_protocol: Callable[[str], bool]
    find_existing: Callable[[str], ai.AgentModel | None]
    row_key: str = "id"               # "id" 或 "provider"：树行 iid 是否需要 provider
    column_head: str = "端点"          # 首列标题
    can_set_active: bool = False       # 是否有「当前生效模型」概念
    set_active_fn: Callable[..., ai.ImportResult] | None = None
    head_of: Callable[[ai.AgentModel], str] | None = None
    # 还原时是否需要多文件按槽位一起回滚。
    # 只有「一次写入必须同时改多个文件，且文件间互相依赖」的 Agent 才为 True（Codex：
    # config.toml 的 model_catalog_json 与 models.json 必须配对）。DSH 的 credentials
    # 虽然也会备份，但它与 patch 无强耦合（缺密钥只是不可用，不会让文件不合法），
    # 所以沿用单文件还原，避免为了「整齐」把用户的其它密钥一起回滚掉。
    multi_file_restore: bool = False
    note: str = ""                     # 页面顶部的一句说明
    # 删除按钮文案与确认语里的动词。Claude Code 的「删」实际是清空槽位，
    # 沿用「删除 N 个模型」会让人以为能从它的配置里消灭一个模型条目。
    delete_label: str = "删除选中"
    delete_verb: str = "删除"
    # 数量单位：其它 Agent 管的是「模型条目」，Claude Code 管的是「槽位变量」。
    # 界面提示与结果摘要共用这一个词，避免同一件事在两处叫不同名字。
    count_unit: str = "个模型"
    # 该 Agent 配置里**没有落点**的规格字段，导入对话框据此隐藏对应输入行。
    # 留着可填却写不进去的控件比不显示更糟：用户会以为自己设的 1M 上下文生效了。
    hidden_spec_fields: tuple[str, ...] = ()
    spec_note: str = ""                # 隐藏了字段时，对话框底部的一句解释


def _wb_head(row: ai.AgentModel) -> str:
    return row.base_url


def _provider_head(row: ai.AgentModel) -> str:
    return row.provider


def _codex_head(row: ai.AgentModel) -> str:
    return f"{row.provider} · {row.api}"


def _claude_head(row: ai.AgentModel) -> str:
    """首列显示槽位的可读名，变量名挂在 tooltip 之外的页面说明里。

    直接显示 `ANTHROPIC_DEFAULT_SONNET_MODEL` 会把 240px 的列撑爆，而槽位名
    本身（Opus 槽 / 子代理）已足够辨认；页面顶部说明里给了完整变量名对照。
    """
    return cc.SLOT_LABELS.get(row.provider, row.provider)


WORKBUDDY = AgentDef(
    kind="workbuddy",
    label="WorkBuddy",
    tab_label="WorkBuddy 管理",
    button_label="导入 WorkBuddy",
    primary_path=lambda: ai.WORKBUDDY_MODELS,
    all_paths=lambda: [ai.WORKBUDDY_MODELS],
    read=ai.read_workbuddy,
    import_fn=ai.import_workbuddy,
    delete_fn=ai.delete_workbuddy,
    status_fn=lambda: ai.agent_status("workbuddy"),
    supports_protocol=lambda _p: True,
    find_existing=lambda mid: ai.find_agent_model("workbuddy", mid),
    row_key="id",
    column_head="端点",
    can_set_active=False,
    head_of=_wb_head,
    note="模型条目自带端点与密钥，写入时会统一成 chat/completions 端点。",
)

DSH = AgentDef(
    kind="dsh",
    label="DeepSeek Harness",
    tab_label="DeepSeekHarness 管理",
    button_label="导入 DeepSeekHarness",
    primary_path=lambda: ai.DSH_PATCH,
    all_paths=lambda: [ai.DSH_PATCH, ai.DSH_CREDENTIALS],
    read=ai.read_dsh,
    import_fn=ai.import_dsh,
    delete_fn=ai.delete_dsh,
    status_fn=lambda: ai.agent_status("dsh"),
    supports_protocol=lambda p: p in ai.DSH_API,
    find_existing=lambda mid: ai.find_agent_model("dsh", mid),
    row_key="provider",
    column_head="Provider",
    can_set_active=False,
    head_of=_provider_head,
    note="模型挂在 llm-pi-ai 条目下，密钥单独写入 .credentials.yaml 的 refs。",
)

CODEX = AgentDef(
    kind="codex",
    label="Codex",
    tab_label="Codex 管理",
    button_label="导入 Codex",
    primary_path=lambda: cx.CODEX_CONFIG,
    all_paths=lambda: [cx.CODEX_CONFIG, cx.CODEX_MODELS],
    read=cx.read_codex,
    import_fn=cx.import_codex,
    delete_fn=cx.delete_codex,
    status_fn=cx.status,
    # Codex 0.166 起只支持 Responses 协议，chat/completions 已被移除，无降级路径
    supports_protocol=lambda p: p in ("openai", ""),
    find_existing=lambda mid: cx.find_model(mid),
    row_key="id",
    column_head="Provider",
    can_set_active=True,
    set_active_fn=cx.set_active,
    head_of=_codex_head,
    multi_file_restore=True,
    note="模型目录为强 schema（缺字段会导致 Codex 拒绝启动），"
         "端点与密钥统一写在 config.toml 的 provider 段。",
)

# 注册顺序 = 页签顺序 = 导入按钮顺序。Claude Code 排在 Codex 之后：
# 它与 Codex 同属「端点全局、模型不是独立条目」的一类，放在一起更易对照。
CLAUDE = AgentDef(
    kind="claude",
    label="Claude Code",
    tab_label="ClaudeCode 管理",
    button_label="导入 ClaudeCode",
    primary_path=cc.settings_path,
    all_paths=lambda: [cc.settings_path()],
    read=cc.read_claude,
    import_fn=cc.import_claude,
    delete_fn=cc.delete_claude,
    status_fn=cc.status,
    # Claude Code 只说 Anthropic Messages；OpenAI 协议仅在探测阶段有意义
    supports_protocol=lambda p: p in ("anthropic", "openai", ""),
    find_existing=lambda mid: cc.find_model(mid),
    # 同一个模型可同时占多个槽位，iid 必须带槽位才唯一
    row_key="provider",
    column_head="槽位",
    can_set_active=True,
    set_active_fn=cc.set_active,
    head_of=_claude_head,
    delete_label="清空选中槽位",
    delete_verb="清空",
    count_unit="个槽位",
    hidden_spec_fields=("context", "max_out", "vision", "reasoning"),
    spec_note="Claude Code 的配置里没有逐模型的上下文 / 输出上限 / 图片开关，"
              "这几项对它无落点，故不显示；上限只有一个全局 "
              "CLAUDE_CODE_MAX_CONTEXT_TOKENS。",
    note=cc.NOTE,
)

AGENTS: tuple[AgentDef, ...] = (WORKBUDDY, DSH, CODEX, CLAUDE)
_BY_KIND: dict[str, AgentDef] = {a.kind: a for a in AGENTS}


def get(kind: str) -> AgentDef:
    try:
        return _BY_KIND[kind]
    except KeyError as e:
        raise KeyError(f"未注册的 Agent：{kind}（已注册：{', '.join(_BY_KIND)}）") from e


def kinds() -> tuple[str, ...]:
    return tuple(a.kind for a in AGENTS)


def labels() -> dict[str, str]:
    return {a.kind: a.label for a in AGENTS}


def row_iid(agent: AgentDef, row: ai.AgentModel) -> str:
    """树行 iid。DSH 同一模型 ID 可出现在多个 provider 下，必须带 provider 才唯一。

    分隔符用 "::" 而不是 "\\x00"：Tcl 字符串按 C 串处理，嵌入的 NUL 会被截断，
    iid 退化成 provider，同 provider 的第二个模型就报 "Item ... already exists"。
    """
    if agent.row_key == "provider":
        return f"{row.provider}::{row.model_id}"
    return row.model_id


def delete_args(agent: AgentDef, rows: Iterable[ai.AgentModel]) -> Any:
    """把选中行整理成该 Agent 删除函数需要的形状。

    判据是 `row_key` 而不是逐个 kind 分支：凡是「行唯一键含 provider」的 Agent
    （DSH 的 provider、Claude Code 的槽位），删除都要知道「删哪一个」，否则同一个
    模型出现在多个 provider/槽位下时不知道该动哪个。
    """
    rows = list(rows)
    if agent.row_key == "provider":
        return [(r.provider, r.model_id) for r in rows]
    return [r.model_id for r in rows]


def call_import(
    agent: AgentDef,
    picked: list[Any],
    specs: Mapping[str, ModelSpec],
    *,
    base_url: str,
    api_key: str,
    protocol: str,
    key_label: str = "",
) -> ai.ImportResult:
    """统一导入入口：把各 Agent 不一样的参数名挡在这里。

    `key_label` 只有 DSH 用得上：同一 Base URL 挂第二把 Key 时，它拿这个标签给新
    provider 取名（a6api-a6-2）。别的 Agent 的模型条目没有 provider 概念，不传。
    """
    if agent.kind == "workbuddy":
        return agent.import_fn(picked, specs, base_url=base_url, api_key=api_key)
    kwargs: dict[str, Any] = {"protocol": protocol}
    if agent.kind == "dsh" and key_label:
        kwargs["key_label"] = key_label
    return agent.import_fn(picked, specs, base_url=base_url, api_key=api_key, **kwargs)
