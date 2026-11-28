"""Codex 适配器自测：TOML 局部改写、双文件备份还原、强 schema 条目、注册表一致性。

重点不是「写进去对不对」，而是三条容易静默出错的边界：
1. TOML 局部改写必须只动目标键，其余段落（注释、plugins、mcp）原样保留；
2. 双文件备份必须按槽位对齐，否则会出现「config 退了 models 没退」的半截状态；
3. 生成的模型条目必须含全部 schema 必填字段，缺一个 Codex 就拒绝启动。
"""
# 直接运行本文件时，把项目根目录加入 sys.path（无需安装成包）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import os
import tempfile

from modelkeeper import agent_import as ai
from modelkeeper import agents
from modelkeeper import codex_import as cx
from modelkeeper.model_spec import ModelSpec, norm_id


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        check.failed = True


check.failed = False


class P:
    def __init__(self, id, name="", context=None, max_out=None):
        self.id = id
        self.name = name
        self.context = context
        self.max_out = max_out


# Codex 实测的 schema 必填字段集（少一个就 "missing field" 启动失败）
REQUIRED = (
    "slug", "display_name", "description", "default_reasoning_level",
    "supported_reasoning_levels", "shell_type", "visibility", "supported_in_api",
    "priority", "support_verbosity", "truncation_policy",
    "experimental_supported_tools", "model_messages",
)

CONFIG_TEMPLATE = '''model_provider = "custom"
model = "gpt-6-luna"

sandbox_mode = "workspace-write"
web_search = "indexed"
model_catalog_json = "old-catalog.json"

# 这行注释必须活下来
[model_providers.custom]
name = "custom"
wire_api = "responses"
requires_openai_auth = false
base_url = "https://relay.example/v1"
experimental_bearer_token = "sk-old"

[mcp_servers.node_repl]
command = "node.exe"
args = []

[desktop]
appearanceTheme = "dark"
selected-avatar-id = "bsod"
'''

MODELS_DOC = {
    "models": [
        {
            "slug": "gpt-6-luna",
            "display_name": "GPT-6-Luna",
            "description": "official template carrier",
            "default_reasoning_level": "medium",
            "supported_reasoning_levels": [{"effort": "low", "description": "x"},
                                           {"effort": "high", "description": "y"}],
            "shell_type": "unified_exec",
            "visibility": "list",
            "supported_in_api": True,
            "priority": 1000,
            "support_verbosity": True,
            "truncation_policy": {"mode": "tokens", "limit": 10000},
            "experimental_supported_tools": [],
            "context_window": 272000,
            "max_context_window": 272000,
            "input_modalities": ["text", "image"],
            "model_messages": {"instructions_template": "OFFICIAL-TEMPLATE-TEXT",
                               "instructions_variables": None, "approvals": None,
                               "collaboration_modes": None, "auto_review": None,
                               "permissions": None, "multi_agent": None},
        },
        {
            "slug": "keep-me",
            "display_name": "keep me",
            "description": "unrelated entry",
            "default_reasoning_level": "low",
            "supported_reasoning_levels": [{"effort": "low", "description": "x"}],
            "shell_type": "unified_exec",
            "visibility": "hide",
            "supported_in_api": False,
            "priority": 9000,
            "support_verbosity": True,
            "truncation_policy": {"mode": "tokens", "limit": 10000},
            "experimental_supported_tools": [],
            "context_window": 1000,
            "max_context_window": 1000,
            "input_modalities": ["text"],
            "model_messages": {"instructions_template": "keep template"},
        },
    ]
}


def main():
    tmp = Path(tempfile.mkdtemp(prefix="codeximport-"))
    os.environ["MODEL_PROBE_BACKUP_DIR"] = str(tmp / "backups")
    ai.BACKUP_DIR = tmp / "backups"

    cfg = tmp / "config.toml"
    mdls = tmp / "models.json"
    cfg.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    mdls.write_text(json.dumps(MODELS_DOC, ensure_ascii=False, indent=2), encoding="utf-8")

    # ---- 1. TOML 局部改写：只动目标键，其余原样 ----
    print("\n-- TOML 编辑器 --")
    text = CONFIG_TEMPLATE
    out = cx.toml_set(text, "model_providers.custom", "base_url", "https://new.example/v1")
    check("替换已存在的键", 'base_url = "https://new.example/v1"' in out)
    check("同段其他键不动", 'wire_api = "responses"' in out and 'name = "custom"' in out)
    check("注释保留", "# 这行注释必须活下来" in out)
    check("其他段落不动", "[mcp_servers.node_repl]" in out and "[desktop]" in out)
    check("键数不变（就地替换非追加）", out.count("base_url =") == 1, out.count("base_url ="))

    out2 = cx.toml_set(out, None, "show_raw_agent_reasoning", True)
    check("顶层新增键落在第一个表头之前",
          out2.index("show_raw_agent_reasoning") < out2.index("[model_providers.custom]"))
    check("新增顶层键取值正确", "show_raw_agent_reasoning = true" in out2)

    out3 = cx.toml_set(out2, "model_providers.brand-new", "base_url", "https://b.example/v1")
    check("不存在的段会被补建", "[model_providers.brand-new]" in out3)
    check("补建的段含目标键", out3.rstrip().endswith('base_url = "https://b.example/v1"')
          or 'base_url = "https://b.example/v1"' in out3)

    check("读回键值", cx.toml_get(text, "model_providers.custom", "wire_api") == '"responses"')
    check("读不存在的键返回 None",
          cx.toml_get(text, "model_providers.custom", "nope") is None)
    # 换行符处理：写入后仍能被 tomllib 解析
    check("改写结果仍是合法 TOML", bool(cx.parse_toml(out3)))

    # ---- 2. 导入：新增模型、继承官方模板、不动无关条目 ----
    print("\n-- 导入 --")
    specs = {norm_id("claude-opus-5-5"): ModelSpec(context=524288, vision=True,
                                                   reasoning_max="high", reasoning_level="high"),
             norm_id("deepseek-v4-pro"): ModelSpec(context=1048576, vision=False,
                                                   reasoning_max="off")}
    r = cx.import_codex(
        [P("claude-opus-5-5", "Opus"), P("deepseek-v4-pro", "V4 Pro")],
        specs, base_url="https://relay.example/v1", api_key="sk-new",
        path=cfg, models_path=mdls,
    )
    check("导入成功", r.ok, r.error)
    check("新增 2 条", r.added == 2, f"added={r.added} updated={r.updated}")
    check("密钥写入路径已回报", "experimental_bearer_token" in r.key_written, r.key_written)

    doc = json.loads(mdls.read_text(encoding="utf-8"))
    by_slug = {m["slug"]: m for m in doc["models"]}
    check("原有无关条目仍在", "keep-me" in by_slug and "gpt-6-luna" in by_slug)
    check("无关条目内容未被改", by_slug["keep-me"]["visibility"] == "hide")
    check("总条目数 2 + 2", len(doc["models"]) == 4, len(doc["models"]))

    new = by_slug["claude-opus-5-5"]
    missing = [k for k in REQUIRED if k not in new]
    check("新条目含全部 schema 必填字段", not missing, f"缺 {missing}")
    check("模板继承自本机官方条目",
          new["model_messages"]["instructions_template"] == "OFFICIAL-TEMPLATE-TEXT")
    check("context 按规格写入", new["context_window"] == 524288, new.get("context_window"))
    check("max_context 与 context 对齐", new["max_context_window"] == 524288)
    check("视觉开关落到 input_modalities", new["input_modalities"] == ["text", "image"])
    check("reasoning high 给三档",
          [e["effort"] for e in new["supported_reasoning_levels"]] == ["low", "medium", "high"])
    check("默认档在支持范围内",
          new["default_reasoning_level"] in
          {e["effort"] for e in new["supported_reasoning_levels"]})

    pro = by_slug["deepseek-v4-pro"]
    check("off 档退到最低可用档 low",
          [e["effort"] for e in pro["supported_reasoning_levels"]] == ["low"],
          pro["supported_reasoning_levels"])
    check("非视觉模型只声明 text", pro["input_modalities"] == ["text"])
    check("priority 递增不冲突",
          new["priority"] != pro["priority"] and pro["priority"] > new["priority"])

    # config.toml 侧
    cfg_after = cfg.read_text(encoding="utf-8")
    parsed = cx.parse_toml(cfg_after)
    check("provider 复用（baseURL 相同不新建）",
          list(parsed["model_providers"]) == ["custom"], list(parsed["model_providers"]))
    check("base_url 已更新",
          parsed["model_providers"]["custom"]["base_url"] == "https://relay.example/v1")
    check("密钥已更新",
          parsed["model_providers"]["custom"]["experimental_bearer_token"] == "sk-new")
    check("wire_api 恒为 responses",
          parsed["model_providers"]["custom"]["wire_api"] == "responses")
    check("model_catalog_json 指向本文件",
          Path(parsed["model_catalog_json"]) == mdls, parsed["model_catalog_json"])
    check("默认模型保持不变（原值仍可用）", parsed["model"] == "gpt-6-luna", parsed["model"])
    check("注释仍保留", "# 这行注释必须活下来" in cfg_after)
    check("mcp_servers 段完好", "node_repl" in parsed.get("mcp_servers", {}))
    check("desktop 段完好", parsed["desktop"]["appearanceTheme"] == "dark")

    # ---- 3. 读回 ----
    print("\n-- 读回 --")
    rows = cx.read_codex(cfg, models_path=mdls)
    check("读回 4 条", len(rows) == 4, len(rows))
    row = next(r for r in rows if r.model_id == "claude-opus-5-5")
    check("读回上下文", row.spec.context == 524288)
    check("读回视觉", row.spec.vision is True)
    check("读回档位上限", row.spec.reasoning_max == "high")
    check("读回端点", row.base_url == "https://relay.example/v1")
    check("读回密钥", row.api_key == "sk-new")
    check("当前模型被标记",
          next(r for r in rows if r.model_id == "gpt-6-luna").is_active is True)
    check("非当前模型不带标记",
          next(r for r in rows if r.model_id == "claude-opus-5-5").is_active is False)
    hidden = next(r for r in rows if r.model_id == "keep-me")
    check("隐藏条目也能读出来（管理页要看到全部）", hidden.spec.context == 1000)

    # ---- 4. 更新已存在的条目：保留其 model_messages ----
    print("\n-- 更新 --")
    r2 = cx.import_codex([P("keep-me")],
                         {norm_id("keep-me"): ModelSpec(context=65536, vision=False,
                                                        reasoning_max="medium")},
                         base_url="https://relay.example/v1", path=cfg, models_path=mdls)
    check("更新成功", r2.ok and r2.updated == 1, f"{r2.error} {r2.updated}")
    doc2 = json.loads(mdls.read_text(encoding="utf-8"))
    updated = next(m for m in doc2["models"] if m["slug"] == "keep-me")
    check("更新后上下文已改", updated["context_window"] == 65536)
    check("更新保留原 model_messages（用户可能定制过）",
          updated["model_messages"]["instructions_template"] == "keep template")

    # ---- 5. 双文件备份与按槽位还原 ----
    print("\n-- 备份与还原 --")
    snaps_cfg = ai.backup_snapshots(cfg)
    snaps_mdl = ai.backup_snapshots(mdls)
    check("两个文件都产生了备份", bool(snaps_cfg) and bool(snaps_mdl),
          f"{len(snaps_cfg)} / {len(snaps_mdl)}")
    check("槽位数一致（同一次写入一起推进）", len(snaps_cfg) == len(snaps_mdl),
          f"{len(snaps_cfg)} / {len(snaps_mdl)}")
    check("has_backup_paths 认得出来", ai.has_backup_paths([cfg, mdls]))
    choices = ai.backup_choices_for([cfg, mdls])
    check("还原选项来自主文件", len(choices) == len(snaps_cfg), len(choices))

    before = mdls.read_text(encoding="utf-8")
    r3 = ai.restore_slots("Codex", [cfg, mdls], 1)
    check("按槽位还原成功", r3.ok and r3.restored == 2, f"{r3.error} restored={r3.restored}")
    check("还原后 models.json 确实变了", mdls.read_text(encoding="utf-8") != before)
    restored_doc = json.loads(mdls.read_text(encoding="utf-8"))
    check("还原回滚到写入前（keep-me 上下文复原）",
          next(m for m in restored_doc["models"]
               if m["slug"] == "keep-me")["context_window"] == 1000)

    # ---- 6. 删除：只动 models.json，provider 保留 ----
    print("\n-- 删除 --")
    r4 = cx.delete_codex(["claude-opus-5-5"], path=cfg, models_path=mdls)
    check("删除成功", r4.ok and r4.removed == 1, f"{r4.error} {r4.removed}")
    doc3 = json.loads(mdls.read_text(encoding="utf-8"))
    check("目标条目已移除", "claude-opus-5-5" not in {m["slug"] for m in doc3["models"]})
    check("provider 段未被连带删除",
          "custom" in cx.parse_toml(cfg.read_text(encoding="utf-8"))["model_providers"])

    r5 = cx.delete_codex(["nonexistent"], path=cfg, models_path=mdls)
    check("删不存在的模型报错不动盘", not r5.ok and "未改动" in r5.error, r5.error)

    # ---- 7. 坏文件不砸盘 ----
    print("\n-- 容错 --")
    bad_cfg = tmp / "bad.toml"
    bad_cfg.write_text("this is = = not toml\n[[[", encoding="utf-8")
    before_bad = bad_cfg.read_text(encoding="utf-8")
    r6 = cx.import_codex([P("x")], {}, base_url="https://a.com/v1",
                         path=bad_cfg, models_path=mdls)
    check("坏 TOML 报错", not r6.ok, r6.ok)
    check("坏 TOML 未被改写", bad_cfg.read_text(encoding="utf-8") == before_bad)

    bad_models = tmp / "bad-models.json"
    bad_models.write_text('{"no_models_key": 1}', encoding="utf-8")
    r7 = cx.import_codex([P("y")], {}, base_url="https://a.com/v1",
                         path=cfg, models_path=bad_models)
    check("models.json 缺 models 数组时报错", not r7.ok and "格式与预期不符" in r7.error, r7.error)

    # ---- 8. 空目录时用内置精简模板，并给出警告 ----
    print("\n-- 空目录兜底 --")
    empty_models = tmp / "empty-models.json"
    r8 = cx.import_codex([P("brand-new", "Brand New")],
                         {norm_id("brand-new"): ModelSpec(context=131072)},
                         base_url="https://relay.example/v1", api_key="sk-z",
                         path=cfg, models_path=empty_models)
    check("空目录也能导入", r8.ok, r8.error)
    check("空目录时警告用了精简模板",
          any("精简" in w for w in r8.warnings), r8.warnings)
    doc8 = json.loads(empty_models.read_text(encoding="utf-8"))
    check("兜底条目也含全部必填字段",
          not [k for k in REQUIRED if k not in doc8["models"][0]],
          [k for k in REQUIRED if k not in doc8["models"][0]])

    # ---- 9. 注册表一致性 ----
    print("\n-- 注册表 --")
    check("四个 Agent 均已注册",
          set(agents.kinds()) == {"workbuddy", "dsh", "codex", "claude"}, agents.kinds())
    for spec in agents.AGENTS:
        check(f"{spec.kind} 的 AgentDef 字段齐全",
              all([spec.label, spec.tab_label, spec.button_label])
              and callable(spec.primary_path) and callable(spec.read)
              and callable(spec.import_fn) and callable(spec.delete_fn)
              and callable(spec.status_fn) and callable(spec.find_existing))
        check(f"{spec.kind} 的 row_key 合法", spec.row_key in ("id", "provider"), spec.row_key)
    check("Codex 不支持 chat 协议", agents.CODEX.supports_protocol("chat") is False)
    check("Codex 支持 openai（对应 responses）", agents.CODEX.supports_protocol("openai") is True)
    check("DSH 支持四种协议", all(agents.DSH.supports_protocol(p)
                                  for p in ("openai", "anthropic", "gemini", "ollama")))
    try:
        agents.get("nope")
        unknown_ok = False
    except KeyError:
        unknown_ok = True
    check("未注册 kind 抛 KeyError", unknown_ok)

    # row_iid 唯一性：DSH 同 ID 不同 provider 不能撞
    dsh_a = ai.AgentModel(agent="dsh", model_id="same", name="s",
                          spec=ModelSpec(), provider="p1")
    dsh_b = ai.AgentModel(agent="dsh", model_id="same", name="s",
                          spec=ModelSpec(), provider="p2")
    check("DSH 同行不同 provider 的 iid 不同",
          agents.row_iid(agents.DSH, dsh_a) != agents.row_iid(agents.DSH, dsh_b))
    codex_a = ai.AgentModel(agent="codex", model_id="same", name="s",
                            spec=ModelSpec(), provider="p1")
    codex_b = ai.AgentModel(agent="codex", model_id="same", name="s",
                            spec=ModelSpec(), provider="p2")
    check("Codex iid 只用模型 ID", agents.row_iid(agents.CODEX, codex_a) == "same")

    print("\nRESULT:", "ALL PASS" if not check.failed else "HAS FAILURES")


if __name__ == "__main__":
    main()
