"""用真实 Codex 客户端校验模型管家写出的配置能否被接受。

这不是单元测试能覆盖的部分：`models.json` 是强 schema，字段缺一个 Codex 就
拒绝启动。只有在真客户端上跑一遍 `codex exec` 才能证明「写出来的东西是对的」。

流程：
1. 用 codex_import 在临时目录里造一套配置（不碰用户真实 ~/.codex）；
2. 用真实 codex.exe 以 CODEX_HOME 指向该目录跑一句话；
3. 断言退出码 0、输出不含 schema 警告。

用法：python scripts/verify_codex_live.py [模型名]
"""
# 直接运行本文件时，把项目根目录加入 sys.path（无需安装成包）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import os
import re
import shutil
import subprocess
import tempfile

from modelkeeper import codex_import as cx
from modelkeeper.model_spec import ModelSpec, norm_id


class P:
    def __init__(self, id, name="", context=None, max_out=None):
        self.id = id
        self.name = name
        self.context = context
        self.max_out = max_out


def find_codex_exe() -> Path | None:
    """在 Codex 安装目录里找可执行文件（版本号是哈希目录名，不能写死）。"""
    base = Path.home() / "AppData" / "Local" / "OpenAI" / "Codex" / "bin"
    if not base.exists():
        return None
    hits = sorted(base.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
    return hits[0] if hits else None


def main() -> int:
    model = sys.argv[1] if len(sys.argv) > 1 else "gpt-6-luna"
    exe = find_codex_exe()
    if exe is None:
        print("SKIP  找不到 codex.exe，跳过实机校验")
        return 0
    print(f"客户端：{exe}")

    # 模板来源：优先读用户真实 models.json（那里有官方长提示词）
    real_models = cx.CODEX_MODELS
    real_cfg = cx.CODEX_CONFIG
    if not real_models.exists():
        print(f"SKIP  {real_models} 不存在，没有可继承的官方模板")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="codexverify-"))
    cfg = tmp / "config.toml"
    mdls = tmp / "models.json"
    # 拷一份真实配置，保留 mcp/plugins 等段，验证「写进去不会破坏原有结构」
    shutil.copyfile(real_cfg, cfg)
    shutil.copyfile(real_models, mdls)

    base_url, api_key = "", ""
    if real_cfg.exists():
        parsed = cx.parse_toml(cfg.read_text(encoding="utf-8"))
        providers = parsed.get("model_providers") or {}
        pid = str(parsed.get("model_provider") or "")
        conf = providers.get(pid) or {}
        base_url = str(conf.get("base_url") or "")
        api_key = str(conf.get("experimental_bearer_token") or "")
    if not base_url:
        print("SKIP  真实配置里没有可用 provider")
        return 0

    # 用适配器写入一个新模型（模拟「导入」这一步）。
    # 注意 slug 必须用中转站真实支持的模型名：虚构名字会被**上游**拒绝
    # （`The model 'xxx' does not exist`），那不是配置问题，会掩盖真正的校验结果。
    # 为保证走「新增」而非「更新」分支，先从临时副本里剔除同名条目。
    target = model
    doc = json.loads(mdls.read_text(encoding="utf-8"))
    doc["models"] = [m for m in doc.get("models") or []
                     if norm_id(str(m.get("slug") or "")) != norm_id(target)]
    mdls.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = cx.import_codex(
        [P(target, "Verify Probe", context=272000)],
        {norm_id(target): ModelSpec(context=272000, vision=True,
                                    reasoning_max="high", reasoning_level="medium")},
        base_url=base_url, api_key=api_key, path=cfg, models_path=mdls,
    )
    print("写入:", result.summary())
    for w in result.warnings:
        print("  警告:", w)
    if not result.ok:
        return 1

    # 让默认模型指向新写入的那一条
    cx.set_active(target, path=cfg)

    env = dict(os.environ, CODEX_HOME=str(tmp))
    print(f"\n真实客户端启动（CODEX_HOME={tmp}）…")
    try:
        proc = subprocess.run(
            [str(exe), "exec", "--skip-git-repo-check", "reply with OK only"],
            capture_output=True, text=True, env=env, timeout=240,
            stdin=subprocess.DEVNULL, cwd=str(tmp),
        )
    except subprocess.TimeoutExpired:
        print("FAIL  客户端超时")
        return 1

    out = (proc.stdout or "") + (proc.stderr or "")
    print("退出码:", proc.returncode)
    for line in out.splitlines():
        low = line.lower()
        if any(k in low for k in ("error", "warning", "missing field", "does not support",
                                  "model:", "provider:")):
            print("  |", line.strip()[:200])

    ok = proc.returncode == 0
    schema_err = re.search(r"missing field|failed to parse model_catalog_json", out)
    bad_proto = "does not support" in out
    if schema_err:
        print("FAIL  模型目录 schema 校验未通过")
        ok = False
    if bad_proto:
        print("FAIL  协议不兼容（该模型不走 responses）")
        ok = False
    if ok:
        print(f"\nRESULT: PASS — 适配器写出的配置被 Codex {exe.parent.name} 接受")
    else:
        print(f"\nRESULT: FAIL\n{out[-1500:]}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
