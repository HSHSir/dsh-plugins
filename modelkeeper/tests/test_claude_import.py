"""Claude Code 适配器自测：槽位分配、Base URL 归一、密钥变量互斥、只动 env。

重点不是「写进去对不对」，而是四条容易静默出错的边界：
1. Base URL 带 /v1 会让客户端拼成 /v1/v1/messages（实测 404），必须剥掉；
2. ANTHROPIC_API_KEY 与 ANTHROPIC_AUTH_TOKEN 并存时前者不生效，留着就是误导；
3. 清空槽位必须校验现值，否则「读表到点删除」之间的改动会被盲删；
4. 写 env 不能碰顶层 theme / modelSettings / permissions 这些用户手写的段。
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
from modelkeeper import claude_import as cc
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


SETTINGS = {
    "env": {
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "ANTHROPIC_BASE_URL": "https://api.a6api.com",
        "ANTHROPIC_MODEL": "gpt-6-luna",
        "ANTHROPIC_API_KEY": "sk-old-key",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "600000",
    },
    "includeCoAuthoredBy": False,
    "tui": "fullscreen",
    "theme": "dark",
    "hasCompletedOnboarding": True,
    "modelSettings": {"gpt-6-luna": {"autoCompactWindow": "auto"}},
}


def write(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


def read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main():
    tmp = Path(tempfile.mkdtemp(prefix="claudeimport-"))
    os.environ["MODEL_PROBE_BACKUP_DIR"] = str(tmp / "backups")
    ai.BACKUP_DIR = tmp / "backups"

    cfg = tmp / "settings.json"
    write(cfg, json.loads(json.dumps(SETTINGS)))

    # ---- 1. Base URL 归一 ----
    print("\n-- Base URL 归一 --")
    cases = [
        ("https://ai.freeflyagain.xyz/v1", "https://ai.freeflyagain.xyz"),
        ("https://ai.freeflyagain.xyz/v1/", "https://ai.freeflyagain.xyz"),
        ("https://ai.freeflyagain.xyz", "https://ai.freeflyagain.xyz"),
        ("https://api.deepseek.com/anthropic", "https://api.deepseek.com/anthropic"),
        ("https://x.y/v1/chat/completions", "https://x.y"),
        ("https://x.y/openai/v1", "https://x.y/openai"),
    ]
    for raw, want in cases:
        got, note = cc.normalize_base(raw)
        check(f"{raw} -> {want}", got == want, got)
        if got != raw.rstrip("/"):
            check(f"  归一有解释文案", "自己会拼" in note, note)
    check("空 URL 归一成空串", cc.normalize_base("")[0] == "")
    check("未改动时不给多余警告", cc.normalize_base("https://a.b")[1] == "")

    # ---- 2. 读：只出有值的槽位，主模型槽算「当前」 ----
    print("\n-- 读回 --")
    rows = cc.read_claude(cfg)
    check("只有主模型槽有值 -> 一行", len(rows) == 1, len(rows))
    row = rows[0]
    check("首列槽位是主模型", row.provider == cc.SLOT_PRIMARY, row.provider)
    check("读回端点", row.base_url == "https://api.a6api.com", row.base_url)
    check("读回密钥（API_KEY 也认）", row.api_key == "sk-old-key", row.api_key)
    check("读回全局上下文", row.spec.context == 600000, row.spec.context)
    check("主模型槽标记为当前", row.is_active is True)
    check("协议落到 anthropic", row.protocol == "anthropic", row.protocol)

    # effort=max 折成 xhigh，否则会被当成未知档退到 off、表格显示「不支持」
    doc = read(cfg)
    doc["env"][cc.SLOT_EFFORT] = "max"
    write(cfg, doc)
    check("effort=max 读成 xhigh", cc.read_claude(cfg)[0].spec.reasoning_max == "xhigh",
          cc.read_claude(cfg)[0].spec.reasoning_max)

    # ---- 3. 导入：占满 5 槽 + 写端点 + 换密钥 + 删 API_KEY ----
    print("\n-- 导入 --")
    doc = read(cfg)
    doc["env"].pop(cc.SLOT_EFFORT, None)
    write(cfg, doc)
    r = cc.import_claude(
        [P("claude-sonnet-5-5", "Sonnet")],
        {norm_id("claude-sonnet-5-5"): ModelSpec(context=1048576, vision=True)},
        base_url="https://ai.freeflyagain.xyz/v1",
        api_key="sk-new", protocol="anthropic", path=cfg,
    )
    check("导入成功", r.ok, r.error)
    check("密钥写入路径已回报", "ANTHROPIC_AUTH_TOKEN" in r.key_written, r.key_written)
    check("Base URL 归一警告已给出",
          any("自己会拼" in w for w in r.warnings), r.warnings)

    d = read(cfg)
    env = d["env"]
    for slot in cc.SLOT_ORDER:
        check(f"{slot} 已指向该模型", env.get(slot) == "claude-sonnet-5-5", env.get(slot))
    check("端点已归一（无 /v1）", env[cc.SLOT_BASE_URL] == "https://ai.freeflyagain.xyz",
          env[cc.SLOT_BASE_URL])
    check("AUTH_TOKEN 已写入", env[cc.SLOT_AUTH_TOKEN] == "sk-new", env.get(cc.SLOT_AUTH_TOKEN))
    check("旧 API_KEY 已移除", cc.SLOT_API_KEY not in env, env.get(cc.SLOT_API_KEY))
    check("移除 API_KEY 有告知",
          any("它不生效" in w for w in r.warnings), r.warnings)
    check("槽位映射写进警告",
          any("主模型=claude-sonnet-5-5" in w for w in r.warnings), r.warnings)

    # 不相关的键与顶层段必须原样活着
    check("其它 env 键未动", env.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC") == "1")
    check("全局上下文未被导入改写", env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS") == "600000",
          env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS"))
    check("theme 段完好", d.get("theme") == "dark")
    check("modelSettings 段完好", d.get("modelSettings", {}).get("gpt-6-luna") is not None)
    check("顶层布尔未被字符串化", d.get("includeCoAuthoredBy") is False)
    check("上下文/输出无落点时有警告",
          any("没有逐模型字段" in w for w in r.warnings), r.warnings)

    # ---- 4. 读回 5 行，iid 靠槽位区分 ----
    print("\n-- 读回 5 行 --")
    rows = cc.read_claude(cfg)
    check("5 个槽位 5 行", len(rows) == 5, len(rows))
    check("同一个模型出现 5 次", len({r.model_id for r in rows}) == 1)
    ids = {agents.row_iid(agents.CLAUDE, r) for r in rows}
    check("同一模型的行 iid 不撞", len(ids) == 5, ids)
    actives = [r.provider for r in rows if r.is_active]
    check("只有主模型槽算当前", actives == [cc.SLOT_PRIMARY], actives)
    check("find_model 优先返回主模型槽",
          cc.find_model("claude-sonnet-5-5", path=cfg).provider == cc.SLOT_PRIMARY)

    # ---- 5. 批量导入按角色词分流 ----
    print("\n-- 批量槽位分配 --")
    got = cc.assign_slots([{"id": "claude-sonnet-5-5"}, {"id": "claude-opus-5-5"},
                           {"id": "claude-haiku-5-5"}])
    check("sonnet 认领 sonnet 槽", got[cc.SLOT_SONNET] == "claude-sonnet-5-5", got)
    check("opus 认领 opus 槽", got[cc.SLOT_OPUS] == "claude-opus-5-5", got)
    check("haiku 认领 haiku 槽", got[cc.SLOT_HAIKU] == "claude-haiku-5-5", got)
    check("未认领的槽位落到第一个模型",
          got[cc.SLOT_PRIMARY] == "claude-sonnet-5-5"
          and got[cc.SLOT_SUBAGENT] == "claude-sonnet-5-5", got)
    none = cc.assign_slots([{"id": "deepseek-v4-pro"}, {"id": "gpt-6-sol"}])
    check("没有角色词时 5 槽全落第一个模型",
          set(none.values()) == {"deepseek-v4-pro"}, none)
    check("空批次不分槽", cc.assign_slots([]) == {})

    # ---- 6. 清空槽位：校验现值 ----
    print("\n-- 清空槽位 --")
    r2 = cc.delete_claude([(cc.SLOT_HAIKU, "claude-sonnet-5-5")], path=cfg)
    check("清空成功", r2.ok and r2.removed == 1, f"{r2.error} {r2.removed}")
    check("目标槽位已空", not read(cfg)["env"].get(cc.SLOT_HAIKU))
    check("其它槽位未动", read(cfg)["env"].get(cc.SLOT_OPUS) == "claude-sonnet-5-5")
    check("端点与密钥未被连带删除",
          read(cfg)["env"].get(cc.SLOT_BASE_URL) and read(cfg)["env"].get(cc.SLOT_AUTH_TOKEN))

    # 现值已被别处改过：按 model_id 校验后拒绝，不能盲删
    r3 = cc.delete_claude([(cc.SLOT_OPUS, "claude-sonnet-5-5")], path=cfg)
    check("清空前两次能删", r3.ok, r3.error)
    doc = read(cfg)
    doc["env"][cc.SLOT_OPUS] = "someone-else"
    write(cfg, doc)
    before = cfg.read_text(encoding="utf-8")
    r4 = cc.delete_claude([(cc.SLOT_OPUS, "claude-sonnet-5-5")], path=cfg)
    check("现值不符时拒绝删除", not r4.ok and "现值已是" in r4.error, r4.error)
    check("拒绝时文件未动", cfg.read_text(encoding="utf-8") == before)
    check("非槽位变量被跳过", not cc.delete_claude([("ANTHROPIC_BASE_URL", "x")], path=cfg).ok)

    # 清空主模型槽要给出后果提示
    r5 = cc.delete_claude([(cc.SLOT_PRIMARY, "claude-sonnet-5-5")], path=cfg)
    check("清空主模型槽成功", r5.ok, r5.error)
    check("清空主模型槽有后果警告",
          any("主模型槽" in w for w in r5.warnings), r5.warnings)

    # ---- 7. 设为当前模型 ----
    print("\n-- 设为当前模型 --")
    r6 = cc.set_active("claude-opus-5-5", path=cfg)
    check("设为主模型成功", r6.ok, r6.error)
    check("主模型已换", read(cfg)["env"][cc.SLOT_PRIMARY] == "claude-opus-5-5")
    again = cc.set_active("claude-opus-5-5", path=cfg)
    check("重复设同一个不给改", not again.ok and "已经是" in again.error, again.error)

    # ---- 8. 备份与还原 ----
    print("\n-- 备份与还原 --")
    snaps = ai.backup_snapshots(cfg)
    check("产生了备份", bool(snaps), snaps)
    check("环形上限 5 份", len(snaps) <= ai.BACKUP_SLOTS, len(snaps))
    check("has_backup_paths 认得出来", ai.has_backup_paths([cfg]))
    current = cfg.read_text(encoding="utf-8")
    restored = ai.restore_backup("claude", path=cfg, snapshot=snaps[0], label="Claude Code")
    check("还原成功", restored.ok and restored.restored == 1, restored.error)
    check("还原摘要用对名字（不张冠李戴成别的 Agent）",
          "Claude Code" in restored.summary() and "DeepSeek" not in restored.summary(),
          restored.summary())
    check("还原确实改了文件", cfg.read_text(encoding="utf-8") != current)

    # ---- 9. 容错：坏 JSON / 非对象 env ----
    print("\n-- 容错 --")
    bad = tmp / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    rb = cc.import_claude([P("m")], {}, base_url="https://a.b", path=bad)
    check("坏 JSON 报错且不动盘", not rb.ok and bad.read_text(encoding="utf-8") == "{not json",
          rb.error)
    check("坏 JSON 时读也不抛（find 返回 None）", cc.find_model("m", path=bad) is None)

    weird = tmp / "weird.json"
    weird.write_text('{"env": "not-a-dict"}', encoding="utf-8")
    rw = cc.import_claude([P("m")], {}, base_url="https://a.b", path=weird)
    check("env 不是对象时报错", not rw.ok and "env" in rw.error, rw.error)

    # 缺 settings.json 也能首次创建
    fresh = tmp / "fresh" / "settings.json"
    rf = cc.import_claude([P("m1")], {norm_id("m1"): ModelSpec()},
                          base_url="https://a.b/v1", api_key="sk-1", path=fresh)
    check("文件不存在时能创建", rf.ok and fresh.exists(), rf.error)
    check("新建文件含 5 槽与端点",
          sum(1 for s in cc.SLOT_ORDER if read(fresh)["env"].get(s)) == 5)

    # ---- 10. 幂等：同样的导入第二次不该算「改动」 ----
    print("\n-- 幂等 --")
    before = fresh.read_text(encoding="utf-8")
    rf2 = cc.import_claude([P("m1")], {norm_id("m1"): ModelSpec()},
                           base_url="https://a.b/v1", api_key="sk-1", path=fresh)
    check("第二次导入报告未改动",
          any("未改动文件" in w for w in rf2.warnings), rf2.warnings)
    check("第二次导入真的没写盘", fresh.read_text(encoding="utf-8") == before)

    # ---- 11. 注册表一致性 ----
    print("\n-- 注册表 --")
    check("claude 已注册", "claude" in agents.kinds(), agents.kinds())
    check("row_key 用 provider（同模型多槽位）", agents.CLAUDE.row_key == "provider")
    check("有当前模型概念", agents.CLAUDE.can_set_active is True)
    check("删除按钮文案是「清空」", "清空" in agents.CLAUDE.delete_label,
          agents.CLAUDE.delete_label)
    check("隐藏了无落点的规格字段",
          set(agents.CLAUDE.hidden_spec_fields) == {"context", "max_out", "vision", "reasoning"},
          agents.CLAUDE.hidden_spec_fields)
    check("支持 anthropic 协议", agents.CLAUDE.supports_protocol("anthropic") is True)
    check("拒绝 gemini 协议", agents.CLAUDE.supports_protocol("gemini") is False)
    check("不是多文件还原（只有一个 settings.json）",
          agents.CLAUDE.multi_file_restore is False)
    check("all_paths 只有一个文件", len(agents.CLAUDE.all_paths()) == 1,
          agents.CLAUDE.all_paths())
    # 只改派生的首列文本，不改实际槽位名
    sample = ai.AgentModel(agent="claude", model_id="m", name="n",
                           spec=ModelSpec(), provider=cc.SLOT_SUBAGENT)
    check("首列显示可读槽位名",
          agents.CLAUDE.head_of(sample) == "子代理", agents.CLAUDE.head_of(sample))

    # delete_args 走 row_key：Claude 要带槽位，WorkBuddy 只带 ID
    crows = [ai.AgentModel(agent="claude", model_id="m", name="n",
                           spec=ModelSpec(), provider=cc.SLOT_OPUS)]
    check("Claude 删除参数带槽位",
          agents.delete_args(agents.CLAUDE, crows) == [(cc.SLOT_OPUS, "m")],
          agents.delete_args(agents.CLAUDE, crows))
    wrows = [ai.AgentModel(agent="workbuddy", model_id="m", name="n", spec=ModelSpec())]
    check("WorkBuddy 删除参数只有 ID",
          agents.delete_args(agents.WORKBUDDY, wrows) == ["m"],
          agents.delete_args(agents.WORKBUDDY, wrows))
    # Codex 仍是单键（回归：改成按 row_key 分支后不能波及它）
    check("Codex 删除参数仍只有 ID",
          agents.delete_args(agents.CODEX, [ai.AgentModel(
              agent="codex", model_id="s", name="n", spec=ModelSpec())]) == ["s"])

    # 状态：目录在就认为装过（settings.json 缺失时首写即创建）
    os.environ["CLAUDE_CONFIG_DIR"] = str(tmp / "absent")
    ok_absent, detail_absent = cc.status()
    check("目录不存在时报告未安装", ok_absent is False and "未检测到" in detail_absent,
          detail_absent)
    os.environ["CLAUDE_CONFIG_DIR"] = str(tmp)
    ok_present, _ = cc.status()
    check("目录存在时可用", ok_present is True)
    os.environ.pop("CLAUDE_CONFIG_DIR", None)

    print("\nRESULT:", "ALL PASS" if not check.failed else "HAS FAILURES")


if __name__ == "__main__":
    main()
