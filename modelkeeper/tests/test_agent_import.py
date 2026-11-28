"""Agent 配置读写自测：端点派生、规格写入、增删计数、备份覆盖、反解回读。

模型库取消后，规格（上下文/图片/推理档）改由 `ModelSpec` 显式传入，所以这里
的断言集中在「传进去的规格是否一字不差地落到配置文件里」+「从文件读回来的
规格是否还原成同一个 ModelSpec」这两头，中间不经过任何隐式默认值。
"""
# 直接运行本文件时，把项目根目录加入 sys.path（无需安装成包）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import os
import tempfile

from modelkeeper import agent_import as ai
from modelkeeper.model_spec import ModelSpec, norm_id


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        check.failed = True


check.failed = False


class P:
    """探测行的最小替身：只带 id/name，规格由测试显式给。"""

    def __init__(self, id, name="", context=None, max_out=None):
        self.id = id
        self.name = name
        self.context = context
        self.max_out = max_out


def main():
    import yaml

    # 现网 DSH 用 YAML 1.2 核心 schema（Node 侧 js-yaml）：`off` 是字符串而非布尔。
    # 用 `yaml.safe_load` 读回来会把键 `off` 解析成 False，得用项目自己的 loader。
    _y, loader12, _d = ai._load_yaml()

    def load12(path):
        return _y.load(Path(path).read_text(encoding="utf-8"), Loader=loader12)

    # ---- 端点与命名派生 ----
    check("v1 补 chat/completions", ai.chat_url("https://x.com/v1") == "https://x.com/v1/chat/completions")
    check("裸域补 /v1", ai.chat_url("https://api.a6api.com") == "https://api.a6api.com/v1/chat/completions")
    check("已是完整端点不重复", ai.chat_url("https://x.com/v1/chat/completions").endswith("/v1/chat/completions"))
    check("尾斜杠容错", ai.chat_url("https://x.com/v1/") == "https://x.com/v1/chat/completions")
    check("base_url_of 反解", ai.base_url_of("https://api.a6api.com/v1/chat/completions") == "https://api.a6api.com/v1",
          ai.base_url_of("https://api.a6api.com/v1/chat/completions"))
    check("provider 取二级域", ai.provider_id("https://api.a6api.com/v1") == "a6api", ai.provider_id("https://api.a6api.com/v1"))
    check("IP 退回 custom", ai.provider_id("http://127.0.0.1:8080/v1") == "custom")
    check("环境变量名", ai.api_key_env("https://api.a6api.com/v1") == "A6API_API_KEY")

    tmp = Path(tempfile.mkdtemp(prefix="agentimport-"))
    # 备份目录必须指到临时目录：否则跑一次自测就会顶掉用户真实的回滚点
    backup_dir = tmp / "backups"
    os.environ["MODEL_PROBE_BACKUP_DIR"] = str(backup_dir)

    # ---- WorkBuddy ----
    wb = tmp / "models.json"
    wb.write_text(json.dumps([
        {"id": "keep-me", "name": "keep-me", "vendor": "Custom", "url": "https://old/v1",
         "apiKey": "sk-old", "supportsImages": False, "maxInputTokens": 1024, "think": "keep"},
        {"id": "glm-5.3", "name": "旧名字", "vendor": "Custom", "url": "https://old/v1",
         "apiKey": "sk-keep", "supportsImages": False, "maxInputTokens": 1000},
    ], ensure_ascii=False, indent=2), encoding="utf-8")

    wb_original = wb.read_text(encoding="utf-8")
    specs = {
        norm_id("glm-5.3"): ModelSpec(context=32768, vision=True, reasoning_max="high", reasoning_level="medium"),
        norm_id("gpt-6-sol"): ModelSpec(context=524288, vision=True, reasoning_max="xhigh",
                                        reasoning_level="high", max_tokens=8192),
        norm_id("brand-new"): ModelSpec(context=65536, vision=False, reasoning_max="off"),
    }
    r = ai.import_workbuddy(
        [P("glm-5.3"), P("gpt-6-sol", context=131072, max_out=8192), P("brand-new")],
        specs, base_url="https://api.a6api.com/v1", api_key="sk-new", path=wb,
    )
    check("WB 导入成功", r.ok, r.error)
    check("WB 新增/更新计数", (r.added, r.updated) == (2, 1), (r.added, r.updated))
    check("WB 已备份", r.backup is not None and r.backup.exists())
    check("WB 备份写在模型管家备份目录",
          r.backup is not None and r.backup.parent == backup_dir, str(r.backup))
    check("备份内容就是写入前的原文件",
          r.backup is not None and r.backup.read_text(encoding="utf-8") == wb_original)

    data = json.loads(wb.read_text(encoding="utf-8"))
    by = {d["id"]: d for d in data}
    check("WB 未涉条目原样保留",
          by["keep-me"]["apiKey"] == "sk-old" and by["keep-me"]["think"] == "keep" and len(data) == 4,
          len(data))

    glm = by["glm-5.3"]
    check("WB 名称被更新", glm["name"] == "glm-5.3", glm.get("name"))
    check("WB 上下文按规格写入", glm.get("maxInputTokens") == 32768, glm.get("maxInputTokens"))
    check("WB 图片按规格写入", glm.get("supportsImages") is True)
    check("WB 推理开关按规格写入", glm.get("supportsReasoning") is True)
    check("WB 支持档位=list(off..high)",
          glm.get("reasoning", {}).get("supportedEfforts") == ["off", "low", "medium", "high"],
          glm.get("reasoning"))
    check("WB 默认档按规格写入", glm.get("reasoning", {}).get("defaultEffort") == "medium")
    check("WB Key 被写入", glm.get("apiKey") == "sk-new")
    check("WB 写入最大输出", by["gpt-6-sol"].get("maxOutputTokens") == 8192, by["gpt-6-sol"])
    check("WB 未指定最大输出时不写该字段",
          "maxOutputTokens" not in by["brand-new"], by["brand-new"])

    brand = by["brand-new"]
    check("WB 不支持推理时不写 reasoning 块",
          brand.get("supportsReasoning") is False and "reasoning" not in brand, brand)

    # 同主机保留原路径：老条目手填过非标准 path 时不该被 chat_url 覆盖
    check("WB 同主机保留旧 uri 路径",
          by["glm-5.3"]["url"].endswith("/chat/completions"), by["glm-5.3"]["url"])

    # 幂等：再写一次不新增条目，只更新
    r2 = ai.import_workbuddy([P("glm-5.3"), P("gpt-6-sol"), P("brand-new")], specs,
                             base_url="https://api.a6api.com/v1", api_key="sk-new", path=wb)
    check("WB 二次导入不新增", (r2.added, r2.updated) == (0, 3), (r2.added, r2.updated))
    check("WB 二次导入留下两份快照",
          len(ai.backup_snapshots(wb)) == 2,
          [p.name for p in backup_dir.iterdir()])
    check("第 1 份快照仍是最新那份",
          r2.backup is not None and r2.backup == ai.backup_path_for(wb, 1), r2.backup)
    check("WB 二次备份是覆盖后的新内容",
          r2.backup is not None and r2.backup.read_text(encoding="utf-8") != wb_original)

    # ---- 反解回读 ----
    rows = ai.read_workbuddy(wb)
    check("read_workbuddy 条数", len(rows) == 4, len(rows))
    check("read_workbuddy 无代理字段泄漏", all(r.agent == "workbuddy" for r in rows))
    sol = next(r for r in rows if r.model_id == "gpt-6-sol")
    check("反解上下文", sol.spec.context == 524288, sol.spec.context)
    check("反解图片", sol.spec.vision is True)
    check("反解支持档位上限", sol.spec.reasoning_max == "xhigh", sol.spec.reasoning_max)
    check("反解默认档", sol.spec.reasoning_level == "high", sol.spec.reasoning_level)
    check("反解 BaseURL", sol.base_url == "https://api.a6api.com/v1", sol.base_url)
    check("反解 Key", sol.api_key == "sk-new", sol.api_key)
    check("反解最大输出", sol.spec.max_tokens == 8192, sol.spec.max_tokens)
    nr = next(r for r in rows if r.model_id == "brand-new")
    check("反解「不支持推理」为 off", nr.spec.reasoning_max == "off" and not nr.spec.reasoning)

    # ---- 删除 ----
    d = ai.delete_workbuddy(["brand-new"], path=wb)
    check("WB 删除成功", d.ok and d.removed == 1, (d.error, d.removed))
    check("WB 删除后条目消失",
          "brand-new" not in {x["id"] for x in json.loads(wb.read_text(encoding="utf-8"))})
    check("WB 删除也留备份", d.backup is not None and d.backup.exists())
    d2 = ai.delete_workbuddy(["ghost-model"], path=wb)
    check("删除不存在的模型报错不动盘", not d2.ok and "没有找到" in d2.error, d2.error)

    # ---- 查找（大小写不敏感） ----
    found = ai.find_agent_model("workbuddy", "GLM-5.3", path=wb)
    check("find_agent_model 大小写不敏感", found is not None and found.model_id == "glm-5.3",
          found.model_id if found else None)

    # ---- DeepSeek Harness ----
    dsh = tmp / "cordis.patch.yml"
    cred = tmp / ".credentials.yaml"
    # 凭据文件先存在（现实里 DSH 一定已经有它），否则首次创建时无旧内容可备份
    cred.write_text(yaml.safe_dump({"version": 1, "refs": {"OTHER_KEY": "sk-other"}},
                                   allow_unicode=True), encoding="utf-8")
    dsh.write_text(yaml.safe_dump([
        {"id": "llm-pi-ai", "name": "@deepseek-ai/dsh-llm-pi-ai", "config": {"providers": {
            "gs": {
                "apiKeyEnv": "GS_API_KEY",
                "api": "openai-completions",
                "baseURL": "https://ai.example.xyz/v1",
                "models": [{"id": "old-model", "name": "Old"}],
            },
        }}},
        {"id": "agent-default-model", "config": {
            "provider": "gs", "model": "old-model", "reasoningEffort": "high"}},
    ], allow_unicode=True, sort_keys=False), encoding="utf-8")

    dsh_specs = {
        norm_id("deepseek-v4.1-flash"): ModelSpec(context=524288, vision=True,
                                                  reasoning_max="xhigh", reasoning_level="high",
                                                  max_tokens=4096),
        norm_id("old-model"): ModelSpec(context=131072, vision=False,
                                        reasoning_max="medium", reasoning_level="low"),
    }
    r3 = ai.import_dsh([P("deepseek-v4.1-flash"), P("old-model")], dsh_specs,
                       base_url="https://ai.example.xyz/v1", protocol="openai",
                       api_key="sk-gs", path=dsh, credentials_path=cred)
    check("DSH 导入成功", r3.ok, r3.error)
    check("DSH 复用同 baseURL 的 provider", r3.added == 1 and r3.updated == 1, (r3.added, r3.updated))
    check("DSH 未新建 provider",
          not any("新建 provider" in w for w in r3.warnings), r3.warnings)
    check("DSH 密钥写入 refs", "GS_API_KEY" in r3.key_written, r3.key_written)
    check("DSH 备份两份（patch + 凭据）", len(r3.backups) == 2, r3.backups)
    check("DSH 备份名固定", any(b.name == "cordis.patch.yml.bak" for b in r3.backups), r3.backups)
    check("DSH 提示注释会丢失", any("注释" in w for w in r3.warnings), r3.warnings)

    doc = load12(dsh)
    providers = next(e for e in doc if e.get("id") == "llm-pi-ai")["config"]["providers"]
    models = {m["id"]: m for m in providers["gs"]["models"]}
    flash = models["deepseek-v4.1-flash"]
    check("DSH 上下文写入", flash.get("contextWindow") == 524288, flash.get("contextWindow"))
    check("DSH 最大输出写入", flash.get("maxTokens") == 4096, flash.get("maxTokens"))
    check("DSH 未指定最大输出时不写该字段",
          "maxTokens" not in models["old-model"], models["old-model"])
    check("DSH 图片写入", flash.get("input") == ["text", "image"], flash.get("input"))
    check("DSH 推理档位映射 off→None",
          flash.get("reasoningEfforts") == {"off": None, "low": "low", "medium": "medium",
                                            "high": "high", "xhigh": "xhigh"},
          flash.get("reasoningEfforts"))
    check("DSH 不支持图片时不写 input", "input" not in models["old-model"], models["old-model"])
    check("DSH 档位截断到 medium",
          list(models["old-model"].get("reasoningEfforts", {})) == ["off", "low", "medium"],
          models["old-model"].get("reasoningEfforts"))
    check("DSH 凭据文件写入明文",
          load12(cred)["refs"]["GS_API_KEY"] == "sk-gs")

    # ---- 反解回读 ----
    drow = ai.read_dsh(dsh, credentials_path=cred)
    check("read_dsh 条数", len(drow) == 2, [r.model_id for r in drow])
    rf = next(r for r in drow if r.model_id == "deepseek-v4.1-flash")
    check("DSH 反解 provider", rf.provider == "gs", rf.provider)
    check("DSH 反解 api", rf.api == "openai-completions", rf.api)
    check("DSH 反解 baseURL", rf.base_url == "https://ai.example.xyz/v1", rf.base_url)
    check("DSH 反解 Key 来自凭据库", rf.api_key == "sk-gs", rf.api_key)
    check("DSH 反解 env 名", rf.api_key_env == "GS_API_KEY", rf.api_key_env)
    check("DSH 反解协议", rf.protocol == "openai", rf.protocol)
    check("DSH 反解上下文", rf.spec.context == 524288, rf.spec.context)
    check("DSH 反解默认档来自 agent-default-model",
          rf.spec.reasoning_level == "high", rf.spec.reasoning_level)

    # 新 baseURL → 新建 provider
    r4 = ai.import_dsh([P("solo-model")], {norm_id("solo-model"): ModelSpec()},
                       base_url="https://second.example.com/v1", protocol="openai",
                       api_key="sk-2", path=dsh, credentials_path=cred)
    check("DSH 新 baseURL 新建 provider",
          r4.ok and any("新建 provider" in w for w in r4.warnings), r4.warnings)
    doc2 = load12(dsh)
    provs2 = next(e for e in doc2 if e.get("id") == "llm-pi-ai")["config"]["providers"]
    check("DSH 新 provider 用二级域名", "second" in provs2, list(provs2))

    # ---- 删除只删模型，不删 provider ----
    d3 = ai.delete_dsh([("gs", "deepseek-v4.1-flash")], path=dsh)
    check("DSH 删除成功", d3.ok and d3.removed == 1, (d3.error, d3.removed))
    doc3 = load12(dsh)
    provs3 = next(e for e in doc3 if e.get("id") == "llm-pi-ai")["config"]["providers"]
    check("DSH provider 仍在", "gs" in provs3, list(provs3))
    check("DSH 模型已移除",
          "deepseek-v4.1-flash" not in {m["id"] for m in provs3["gs"]["models"]},
          [m["id"] for m in provs3["gs"]["models"]])
    check("DSH 删除后仍保留供别处引用的 baseURL",
          provs3["gs"].get("baseURL") == "https://ai.example.xyz/v1")
    d4 = ai.delete_dsh([("gs", "never-existed")], path=dsh)
    check("DSH 删不存在的模型报错不动盘", not d4.ok and "没有找到" in d4.error, d4.error)

    # ---- 备份目录卫生：原配置旁不得再出现 .bak；目录里只有环形快照 ----
    siblings = [p.name for p in tmp.iterdir() if ".bak" in p.name]
    check("原配置旁没有备份副本", siblings == [], siblings)
    ring = sorted(p.name for p in backup_dir.iterdir())
    stems = ("models.json", "cordis.patch.yml", ".credentials.yaml")
    allowed = {f"{stem}.bak" for stem in stems}
    allowed |= {f"{stem}.{slot}.bak" for stem in stems
                for slot in range(2, ai.BACKUP_SLOTS + 1)}
    check("备份目录只有环形快照",
          set(ring) <= allowed and "models.json.bak" in ring, ring)
    check("每个目标最多保留 BACKUP_SLOTS 份",
          all(len(list(backup_dir.glob(f"{stem}*.bak"))) <= ai.BACKUP_SLOTS
              for stem in stems), ring)

    # ---- 环形滚动：连写 7 次只留 5 份，最老的内容被淘汰 ----
    ring_file = tmp / "ring.json"
    ring_file.write_text("[]", encoding="utf-8")
    for round_no in range(1, 8):
        ai.import_workbuddy([P(f"m{round_no}")], {}, base_url="https://a.com/v1",
                            path=ring_file)
    snaps = ai.backup_snapshots(ring_file)
    check("环形只留 5 份", len(snaps) == ai.BACKUP_SLOTS, [p.name for p in snaps])
    check("环形最新一份是第 1 槽", snaps[0].name == "ring.json.bak", [p.name for p in snaps])
    check("环形最老的一份是第 5 槽", snaps[-1].name == "ring.json.5.bak", [p.name for p in snaps])
    check("第 1 份内容 = 第 7 次写入前的状态",
          "m6" in snaps[0].read_text(encoding="utf-8")
          and "m7" not in snaps[0].read_text(encoding="utf-8"),
          snaps[0].read_text(encoding="utf-8")[:120])
    check("第 5 份已被挤出队尾内容不再是最初的空数组",
          snaps[-1].read_text(encoding="utf-8").strip() != "[]",
          snaps[-1].read_text(encoding="utf-8")[:120])

    # ---- 坏文件不砸盘 ----
    bad = tmp / "bad.yml"
    bad.write_text("just a string, not a list", encoding="utf-8")
    rbad = ai.import_dsh([P("x")], {}, base_url="https://a.com/v1", path=bad, credentials_path=cred)
    check("DSH 结构不符时报错不动盘", not rbad.ok and "格式与预期不符" in rbad.error, rbad.error)
    check("DSH 坏文件内容未被改写", bad.read_text(encoding="utf-8") == "just a string, not a list")

    badjson = tmp / "bad.json"
    badjson.write_text('{"not": "a list"}', encoding="utf-8")
    rbad2 = ai.import_workbuddy([P("y")], {}, base_url="https://a.com/v1", path=badjson)
    check("WB 结构不符时报错不动盘", not rbad2.ok and "格式与预期不符" in rbad2.error, rbad2.error)

    # ---- 同一 baseURL、不同 Key：必须并存，不能互相覆盖 ----
    # 现实场景：中转站只有一个域名，但两个令牌各能调同一批模型（额度/白名单不同）。
    # DSH 的 provider 才是「地址 + 密钥」的单位，所以这里应当出现两个 provider。
    mpatch = tmp / "multi.yml"
    mcred = tmp / ".multi-credentials.yaml"
    mcred.write_text(yaml.safe_dump({"version": 1, "refs": {}}, allow_unicode=True),
                     encoding="utf-8")
    mbase = "https://api.a6api.vip/v1"
    same = [P("deepseek-v4.1-flash-1017")]
    same_specs = {norm_id("deepseek-v4.1-flash-1017"): ModelSpec(context=524288)}

    rk1 = ai.import_dsh(same, same_specs, base_url=mbase, api_key="sk-AAA1", key_label="a6-1",
                        path=mpatch, credentials_path=mcred)
    rk2 = ai.import_dsh(same, same_specs, base_url=mbase, api_key="sk-BBB2", key_label="a6-2",
                        path=mpatch, credentials_path=mcred)
    mdoc = next(e for e in load12(mpatch) if e.get("id") == "llm-pi-ai")["config"]["providers"]
    check("不同 Key 新建第二个 provider", len(mdoc) == 2, list(mdoc))
    check("provider 名带令牌标签", set(mdoc) == {"a6api-a6-1", "a6api-a6-2"}, list(mdoc))
    check("两个 provider 各自一个模型",
          all([m["id"] for m in c["models"]] == ["deepseek-v4.1-flash-1017"]
              for c in mdoc.values()),
          {k: [m["id"] for m in v["models"]] for k, v in mdoc.items()})
    check("两个 provider 的环境变量不撞",
          len({c["apiKeyEnv"] for c in mdoc.values()}) == 2,
          [c["apiKeyEnv"] for c in mdoc.values()])
    mrefs = load12(mcred)["refs"]
    check("两把 Key 都留在凭据文件",
          sorted(mrefs.values()) == ["sk-AAA1", "sk-BBB2"], mrefs)
    check("第二次导入提示并行存在",
          any("互不覆盖" in w for w in rk2.warnings), rk2.warnings)

    # 同一把 Key 再来一次：复用原 provider，不新增
    rk3 = ai.import_dsh(same, same_specs, base_url=mbase, api_key="sk-BBB2",
                        path=mpatch, credentials_path=mcred)
    mdoc2 = next(e for e in load12(mpatch) if e.get("id") == "llm-pi-ai")["config"]["providers"]
    check("同 Key 重复导入不新增 provider", len(mdoc2) == 2, list(mdoc2))
    check("同 Key 重复导入算更新", rk3.updated == 1 and rk3.added == 0,
          (rk3.added, rk3.updated))

    # sk- 前缀差异不算不同 Key
    rk4 = ai.import_dsh(same, same_specs, base_url=mbase, api_key="AAA1",
                        path=mpatch, credentials_path=mcred)
    mdoc3 = next(e for e in load12(mpatch) if e.get("id") == "llm-pi-ai")["config"]["providers"]
    check("sk- 前缀差异认作同一把 Key", len(mdoc3) == 2, list(mdoc3))

    # 不带 Key 的导入不该凭空造 provider
    rk5 = ai.import_dsh([P("solo-x")], {norm_id("solo-x"): ModelSpec()},
                        base_url=mbase, path=mpatch, credentials_path=mcred)
    mdoc4 = next(e for e in load12(mpatch) if e.get("id") == "llm-pi-ai")["config"]["providers"]
    check("无 Key 导入不动 provider 数量", len(mdoc4) == 2, list(mdoc4))

    # 反解回读要能区分同名模型来自哪个 provider
    mrows = ai.read_dsh(mpatch, credentials_path=mcred)
    by_prov = {(r.provider, r.model_id): r.api_key for r in mrows}
    check("反解保留两把 Key",
          by_prov.get(("a6api-a6-1", "deepseek-v4.1-flash-1017")) == "sk-AAA1"
          and by_prov.get(("a6api-a6-2", "deepseek-v4.1-flash-1017")) == "sk-BBB2",
          by_prov)

    # WorkBuddy 侧：模型清单以 id 唯一，只能替换，但必须明确告知
    wmulti = tmp / "wb-multi.json"
    wmulti.write_text("[]", encoding="utf-8")
    ai.import_workbuddy(same, same_specs, base_url=mbase, api_key="sk-AAA1", path=wmulti)
    rw = ai.import_workbuddy(same, same_specs, base_url=mbase, api_key="sk-BBB2", path=wmulti)
    wrows = json.loads(wmulti.read_text(encoding="utf-8"))
    check("WB 同 id 只留一条", len(wrows) == 1, len(wrows))
    check("WB 换成新 Key", wrows[0]["apiKey"] == "sk-BBB2", wrows[0].get("apiKey"))
    check("WB 换 Key 时给出提示",
          any("已替换" in w and "DeepSeek Harness" in w for w in rw.warnings), rw.warnings)

    print("\nRESULT:", "ALL PASS" if not check.failed else "HAS FAILURES")


if __name__ == "__main__":
    main()
