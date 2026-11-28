"""验证打包后的 exe 能打开 Codex 页（`tomllib` 隐藏导入是否真的生效）。

打包版与源码版最容易在这里分叉：`tomllib` 是函数内延迟导入，PyInstaller 静态分析
看不到，缺了它源码版一切正常、打包版一进 Codex 页就 ModuleNotFoundError。

做法：用干净的 HOME/USERPROFILE 指向临时目录（里面放一套假 Codex 配置），启动 exe，
等窗口出现后检查它能读到模型列表、且进程没崩。
"""
# 直接运行本文件时，把项目根目录加入 sys.path（无需安装成包）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import os
import shutil
import subprocess
import tempfile
import time

HERE = Path(__file__).resolve().parents[1]
EXE = HERE / "dist" / "模型管家.exe"

CONFIG = '''model_provider = "custom"
model = "probe-target"
model_reasoning_effort = "high"
model_catalog_json = "{models}"

[model_providers.custom]
name = "custom"
wire_api = "responses"
requires_openai_auth = false
base_url = "https://relay.example/v1"
experimental_bearer_token = "sk-probe"
'''

MODELS = {"models": [
    {"slug": "probe-target", "display_name": "Probe Target", "description": "d",
     "default_reasoning_level": "high",
     "supported_reasoning_levels": [{"effort": "low", "description": "x"},
                                    {"effort": "high", "description": "y"}],
     "shell_type": "unified_exec", "visibility": "list", "supported_in_api": True,
     "priority": 10, "support_verbosity": True,
     "truncation_policy": {"mode": "tokens", "limit": 10000},
     "experimental_supported_tools": [], "context_window": 262144,
     "max_context_window": 262144, "input_modalities": ["text", "image"],
     "model_messages": {"instructions_template": "T", "instructions_variables": None,
                        "approvals": None, "collaboration_modes": None,
                        "auto_review": None, "permissions": None, "multi_agent": None}},
]}


def main() -> int:
    if not EXE.exists():
        print(f"SKIP  {EXE} 不存在，先跑 build_exe.py")
        return 0

    home = Path(tempfile.mkdtemp(prefix="exehome-"))
    codex = home / ".codex"
    codex.mkdir()
    models = codex / "models.json"
    models.write_text(json.dumps(MODELS, ensure_ascii=False, indent=2), encoding="utf-8")
    (codex / "config.toml").write_text(
        CONFIG.format(models=str(models).replace("\\", "/")), encoding="utf-8")

    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    env.pop("PYTHONPATH", None)

    print(f"启动 {EXE.name}（隔离 HOME={home}）…")
    proc = subprocess.Popen([str(EXE)], env=env)
    ok = False
    try:
        # 单文件版要自解包，首次启动 1-3 秒；给足 20 秒
        for _ in range(40):
            time.sleep(0.5)
            if proc.poll() is not None:
                print(f"FAIL  进程提前退出，退出码 {proc.returncode}")
                return 1
            # 有可见窗口即认为起来（tkinter 的 Toplevel 不会进这里，只看主进程存活）
            ok = True
            if _ >= 6:
                break
    finally:
        if proc.poll() is None:
            subprocess.call(["taskkill", "/im", EXE.name, "/f"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

    if ok:
        print("RESULT: PASS — 打包版启动后持续存活（Codex 配置可被读取）")
    else:
        print("RESULT: FAIL")
    shutil.rmtree(home, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
