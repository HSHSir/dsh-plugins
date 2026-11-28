"""GUI 冒烟：驱动真实界面完成探测、测速、导入、Agent 增删，断言结果并截图。

三条硬性隔离，缺一条就会污染用户真实环境：
  1. `ga.CONFIG_PATH` 指到临时目录；
  2. `MODEL_PROBE_BACKUP_DIR` 指到临时目录（否则顶掉真实回滚快照）；
  3. `ai.WORKBUDDY_MODELS` / `ai.DSH_PATCH` / `ai.DSH_CREDENTIALS` 换成临时文件
     ——本脚本会真的走「导入」「添加」「删除」，不隔离就等于改用户的 Agent 配置；
  4. `cx.CODEX_CONFIG` / `cx.CODEX_MODELS` 同理（Codex 是双文件，两个都要换）；
  5. `CLAUDE_CONFIG_DIR` 指向临时目录（Claude Code 的路径是运行时算出来的，
     只能靠这个官方环境变量隔离；不隔离会直接改真实的 ~/.claude/settings.json）。

模态对话框（配置弹窗、确认框）用替身绕开：冒烟要验的是「点下去之后发生了什么」，
不是「对话框长什么样」；后者靠末尾截图人工看。
"""
# 直接运行本文件时，把项目根目录加入 sys.path（无需安装成包）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 截图统一落到 docs/images，避免污染项目根目录
SHOTS = Path(__file__).resolve().parents[1] / "docs" / "images"
SHOTS.mkdir(parents=True, exist_ok=True)


import ctypes
import json
import os
import tempfile
import threading
import time
import tkinter as import_tk
from http.server import ThreadingHTTPServer

from modelkeeper import agent_import as ai
from modelkeeper import agents
from modelkeeper import claude_import as cc
from modelkeeper import codex_import as cx
from modelkeeper import gui_app as ga
from modelkeeper import model_probe as mp
from modelkeeper import smart_route as sr
from modelkeeper.model_spec import ModelSpec, norm_id
from test_model_probe import H
from tkinter import messagebox

# 假端点端口可覆盖：同一台机器上常同时开着别的项目的本地服务，端口撞上时
# 测试会连到别人的服务，症状是「莫名其妙的 404 / KeyError」——看着像功能坏了，
# 实际打错了对方。撞了就换端口：set GUISMOKE_PORT=18766
TEST_PORT = int(os.environ.get("GUISMOKE_PORT", "8766"))
HOST = f"127.0.0.1:{TEST_PORT}"
MOCK_BASE = "http://" + HOST + "/openai"

# 单步卡死上限。任何一步超过这个时间没有推进，就判定卡死并**主动退出**。
# 起因是一次真实事故：删掉测速确认框时顺手删了 `askyesno` 的桩，
# 结果 `_delete_agent_model` 弹出真模态框，Tk 事件循环被它独占，
# 脚本既不报错也不退出，就这么挂着——等工具层超时才发现，白等好几分钟。
STALL_SECONDS = int(os.environ.get("SMOKE_STALL_SECONDS", "90"))

_beat_lock = threading.Lock()
_last_beat = time.time()
_current_step = "启动"


def beat(step: str) -> None:
    """标记「当前推进到哪一步了」。卡死时看门狗靠它打印现场。"""
    global _last_beat, _current_step
    with _beat_lock:
        _last_beat = time.time()
        _current_step = step


def start_watchdog() -> None:
    """后台看门狗：主线程停滞超过 STALL_SECONDS 就打印现场并强制退出。

    没有它，一次意外弹窗就能让整个测试无限期挂着——模态对话框会阻塞调用线程，
    `pump()` 里的超时判断根本没机会执行，所以超时必须由一个独立线程来判。
    """

    def run():
        while True:
            time.sleep(2.0)
            with _beat_lock:
                idle = time.time() - _last_beat
                step = _current_step
            if idle > STALL_SECONDS:
                print(
                    f"\n[WATCHDOG] 卡在「{step}」，已 {idle:.0f} 秒无进展"
                    f"（上限 {STALL_SECONDS}s）。多半是意外弹出的模态框挡住了事件循环。",
                    flush=True,
                )
                os._exit(2)

    threading.Thread(target=run, name="watchdog", daemon=True).start()


class MsgSpy:
    """把 messagebox 所有入口换成记录器 + 固定答案。

    一次装全，而不是用到哪个桩哪个：漏掉任何一个（上次漏的就是 askyesno）
    都会让模态框真的弹出来并挂死整个流程。
    """

    DEFAULTS = {
        "showinfo": None,
        "showwarning": None,
        "showerror": None,
        "askyesno": True,
        "askokcancel": True,
        "askretrycancel": False,
        "askyesnocancel": True,
    }

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def install(self) -> "MsgSpy":
        for name, answer in self.DEFAULTS.items():
            setattr(messagebox, name, self._make(name, answer))
        return self

    def _make(self, name: str, answer):
        def stub(title=None, message=None, **kwargs):
            self.calls.append((name, str(message)))
            print(f"  [msgbox] {name}: {str(message).splitlines()[0][:70]}")
            return answer
        return stub

    def texts(self) -> list[str]:
        return [msg for _name, msg in self.calls]

    def clear(self) -> None:
        self.calls.clear()


# 冒烟全程不得真的写用户配置：把备份目录指到临时目录
_real_fetch_models = mp.fetch_models


def _offline_fetch_models(protocol, base_url, api_key, **kw):
    if "127.0.0.1" not in base_url:
        print("offline-guard:", base_url, "->", MOCK_BASE)
        base_url = MOCK_BASE
    return _real_fetch_models(protocol, base_url, api_key, **kw)


mp.fetch_models = _offline_fetch_models


def pump(app, cond, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        app.update()
        if cond():
            return True
        time.sleep(0.02)
    return False


def _bring_to_front(win) -> None:
    """把窗口报到最前，否则 ImageGrab 抓到的是当时挡在前面的别的程序。

    只设 topmost 不够——Windows 的前台锁会拦下后台进程的 SetForegroundWindow，
    必须 AttachThreadInput 挂到当前前台线程上才抢得到焦点。
    """
    win.update()
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
    u.ShowWindow(hwnd, 9)                                    # SW_RESTORE
    u.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)    # HWND_TOPMOST, NOSIZE|NOMOVE
    fg = u.GetForegroundWindow()
    tid_fg = u.GetWindowThreadProcessId(fg, None)
    tid_me = k.GetCurrentThreadId()
    u.AttachThreadInput(tid_me, tid_fg, True)
    u.SetForegroundWindow(hwnd)
    u.BringWindowToTop(hwnd)
    u.AttachThreadInput(tid_me, tid_fg, False)
    win.lift()
    win.focus_force()
    win.update()
    time.sleep(1.0)
    win.update()


class FakeDialog:
    """配置对话框替身：直接返回预设结果，不阻塞事件循环。"""

    plan: dict | None = None
    calls: list = []

    def __init__(self, master, **kw):
        FakeDialog.calls.append(kw)
        # 记录打开时预填了什么——「用目标配置现值预填」这条必须能验到
        self.prefill = dict(kw)
        self.result = dict(FakeDialog.plan) if FakeDialog.plan is not None else None


def _kill_toplevels(widget) -> None:
    """递归销毁所有真实弹窗（截图前用）。"""
    for child in list(widget.winfo_children()):
        if isinstance(child, import_tk.Toplevel):
            try:
                child.destroy()
            except import_tk.TclError:
                pass
        else:
            _kill_toplevels(child)


# (状态, 延迟文案, 缓存率文案)
DEMO_STATES = (("pending", None, None), ("running", None, None),
               ("done", "1.86", "91%"), ("slow", None, None), ("error", None, None))


def _demo_rows(app) -> list:
    """挑几行能测速的 DSH 行（跳过错悬空引用）来做状态演示。"""
    return [r for r in app._agents["dsh"] if not getattr(r, "missing", False)][
        :len(DEMO_STATES)]


def _paint_state_demo(app) -> None:
    """给前几行分别打上五种测速状态，用来核对配色/文案。"""
    spec = agents.get("dsh")
    # 前面用例残留的弹窗会盖住表格，截图前先清掉（否则图上是「页面被挡住」）。
    # 必须递归找：弹窗的 master 不一定是 App 本身，挂在某个 Frame 下时
    # 只看 app.winfo_children() 会漏掉——上一版就是这么漏的。
    _kill_toplevels(app)
    app.tabbar.select(2)
    for row, (state, latency, cache) in zip(_demo_rows(app), DEMO_STATES):
        extra = {}
        if latency:
            extra["latency"] = latency
        if cache:
            extra["cache"] = cache
        app._set_route_cells("dsh", app._agent_endpoint(spec, row),
                             getattr(row, "api_key", "") or "", row.model_id,
                             state=state, **extra)
    app.update()


def _clear_state_demo(app) -> None:
    spec = agents.get("dsh")
    for row in _demo_rows(app):
        app._set_route_cells("dsh", app._agent_endpoint(spec, row),
                             getattr(row, "api_key", "") or "", row.model_id, state="")
    app.update()



def main():
    start_watchdog()
    beat("初始化")
    spy = MsgSpy().install()          # 一次桩全所有弹窗，任何模态框都不许真弹
    srv = ThreadingHTTPServer(("127.0.0.1", TEST_PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="guismoke-"))

    # ---- 隔离 ----
    ga.CONFIG_PATH = tmp / "config.json"
    os.environ["MODEL_PROBE_BACKUP_DIR"] = str(tmp / "backups")
    # 价格表也指到临时目录，否则会读写用户真实的价格表；预设同理，先关掉保证结果稳定
    os.environ["MODEL_PROBE_PRICE_FILE"] = str(tmp / "prices.json")
    os.environ["MODEL_PROBE_PRICE_PRESET"] = "none"
    ga.pb.load_preset.cache_clear()
    # 端点记录也必须隔离：本流程会真的「成功获取模型」，不指走就会把测试用的
    # sk-test 写进用户真实的 endpoints.json，把真实密钥挤到后面去。
    os.environ["MODEL_PROBE_ENDPOINT_FILE"] = str(tmp / "endpoints.json")
    (tmp / "prices.json").write_text(json.dumps({
        "currency": "CNY", "unit": "1M", "updated": "2026-11-02",
        "models": {"gpt-6.1-sol": {"in": 3},
                   "glm-5.3-flash": {"in": 0.15}},
        "patterns": {"deepseek-*": {"in": 1}},
    }, ensure_ascii=False), encoding="utf-8")

    wb = tmp / "wb-models.json"
    dsh = tmp / "cordis.patch.yml"
    cred = tmp / "credentials.yaml"
    ai.WORKBUDDY_MODELS = wb
    ai.DSH_PATCH = dsh
    ai.DSH_CREDENTIALS = cred
    ai.BACKUP_DIR = tmp / "backups"

    # Codex 是双文件（config.toml + models.json），两个都指到临时目录
    codex_cfg = tmp / "codex-config.toml"
    codex_models = tmp / "codex-models.json"
    cx.CODEX_CONFIG = codex_cfg
    cx.CODEX_MODELS = codex_models

    # Claude Code 的路径由 claude_import.home() 在运行时算（跟着 CLAUDE_CONFIG_DIR），
    # 没有模块级常量可替换，所以只能用官方环境变量隔离——否则下面一导入
    # 就直接改用户真实的 ~/.claude/settings.json。
    claude_home = tmp / "claude"
    claude_home.mkdir(parents=True, exist_ok=True)
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_home)
    claude_cfg = claude_home / "settings.json"
    claude_cfg.write_text(json.dumps({
        "env": {
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "ANTHROPIC_BASE_URL": "https://api.example.com",
            "ANTHROPIC_MODEL": "claude-existing",
            "ANTHROPIC_API_KEY": "sk-claude-old",
            "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "400000",
        },
        # 用户手写的顶层段：写 env 时一格都不许动
        "theme": "dark",
        "modelSettings": {"claude-existing": {"autoCompactWindow": "auto"}},
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    codex_cfg.write_text(
        'model_provider = "custom"\n'
        'model = "codex-existing"\n'
        'model_reasoning_effort = "high"\n'
        'model_catalog_json = "%s"\n'
        "\n"
        "[model_providers.custom]\n"
        'name = "custom"\n'
        'wire_api = "responses"\n'
        "requires_openai_auth = false\n"
        'base_url = "https://api.example.com/v1"\n'
        'experimental_bearer_token = "sk-codex"\n'
        % str(codex_models).replace("\\", "/"),
        encoding="utf-8",
    )
    # 一条「模板载体」+ 一条「既有模型」：前者提供官方长提示词，后者验证读回
    _tmpl = {"instructions_template": "OFFICIAL", "instructions_variables": None,
             "approvals": None, "collaboration_modes": None, "auto_review": None,
             "permissions": None, "multi_agent": None}
    codex_models.write_text(json.dumps({"models": [
        {"slug": "codex-existing", "display_name": "既有 Codex 模型",
         "description": "d", "default_reasoning_level": "high",
         "supported_reasoning_levels": [{"effort": "low", "description": "x"},
                                        {"effort": "high", "description": "y"}],
         "shell_type": "unified_exec", "visibility": "list", "supported_in_api": True,
         "priority": 10, "support_verbosity": True,
         "truncation_policy": {"mode": "tokens", "limit": 10000},
         "experimental_supported_tools": [], "context_window": 262144,
         "max_context_window": 262144, "input_modalities": ["text", "image"],
         "model_messages": _tmpl},
    ]}, ensure_ascii=False, indent=2), encoding="utf-8")

    wb.write_text(json.dumps([
        {"id": "existing-wb", "name": "既有模型", "vendor": "Custom",
         "url": "https://api.example.com/v1/chat/completions", "apiKey": "sk-existing",
         "supportsImages": True, "supportsReasoning": True, "maxInputTokens": 524288,
         "reasoning": {"defaultEffort": "high", "supportedEfforts": ["off", "low", "medium", "high", "xhigh"]}},
    ], ensure_ascii=False, indent=2), encoding="utf-8")

    import yaml
    dsh.write_text(yaml.safe_dump([
        {"id": "llm-pi-ai", "name": "@deepseek-ai/dsh-llm-pi-ai", "config": {"providers": {
            "a6api": {"apiKeyEnv": "A6API_API_KEY", "api": "openai-completions",
                      "baseURL": "https://api.a6api.vip/v1",
                      "models": [{"id": "existing-dsh", "name": "既有 DSH 模型",
                                  "contextWindow": 128000}]},
        }}},
        {"id": "agent-default-model", "config": {"provider": "a6api", "model": "existing-dsh",
                                                 "reasoningEffort": "medium"}},
    ], allow_unicode=True, sort_keys=False), encoding="utf-8")

    # 预置一份旧版配置：含 base64 混淆的 key_b64，验证「不再回填、退出即清除」
    ga.CONFIG_PATH.write_text(
        json.dumps({"protocol": "openai", "base": "https://old.example/v1",
                    "key_b64": "c2stb2xkLWtleQ=="}),
        encoding="utf-8",
    )

    # ---- 对话框替身 ----
    ga.ModelConfigDialog = FakeDialog
    app = ga.App()
    app.wait_window = lambda *a, **k: None       # 让 _import_to / _add_agent_model 不阻塞
    app.update()

    # 批量列表折行：tkinter 的 wraplength 会把 ID 从中间切开，看起来像两个模型
    batch_ids = ["deepseek-v4.1-flash-1017", "claude-fable-5", "gemini-3.7-flash", "grok-4.7"]
    wrapped = ga._wrap_ids(batch_ids)
    assert all(mid in wrapped for mid in batch_ids), wrapped
    assert "\n" in wrapped, f"长列表应折行：{wrapped}"
    assert ga._wrap_ids([f"m{i}" for i in range(14)]).endswith("…等共 14 个"), ga._wrap_ids(
        [f"m{i}" for i in range(14)])

    print("legacy key status:", app.var_status.get())
    assert app.var_key.get() == "", "旧版 key_b64 不应回填到界面"
    assert "残留" in app.var_status.get(), app.var_status.get()

    # ---- 页签：探测页 + 每个注册 Agent 一页，等宽、选中态与「获取模型」同色蓝 ----
    # 期望值由注册表推出来，新增 Agent 时这里不用改
    assert len(app.tabbar._items) == 1 + len(agents.AGENTS), len(app.tabbar._items)
    # 版本号在窗口标题里（标题栏左上角），和 exe 属性同源，不该出现第二处硬编码
    assert app.title() == mp.WINDOW_TITLE, app.title()
    widths = [holder.winfo_width() for holder, _ in app.tabbar._items]
    print("tab widths:", widths)
    assert len(set(widths)) == 1 and widths[0] > 0, widths
    assert app.tabbar._items[0][1].cget("bg").lower() == ga.ACCENT.lower()
    app.tabbar.select(1)
    app.update()
    assert app.tabbar._items[1][1].cget("bg").lower() == ga.ACCENT.lower(), "选中页签未变蓝"
    assert app.tabbar._items[0][1].cget("bg").lower() == ga.PANEL.lower(), "未选中页签未回到深灰"
    assert app.page_wb.winfo_ismapped() and not app.page_probe.winfo_ismapped(), "页签没真的切页"
    app.tabbar.select(0)
    app.update()

    # ---- 探测 ----
    app.var_base.set("http://" + HOST + "/openai")
    app.var_key.set("sk-test")
    app.update()
    beat("_start")
    app._start()
    ok = pump(app, lambda: not app._busy and len(app.tree.get_children()) > 0)
    rows = app.tree.get_children()
    print("rows:", len(rows), "| status:", app.var_status.get())
    print("hint:", app.var_ctx_hint.get())
    assert ok, "探测未在超时内完成"
    assert len(rows) == 5, rows

    # ---- 端点记录：成功获取即记录，同 URL 替换 Key，失败不记录 ----
    assert "disabled" not in app.btn_go.state(), app.btn_go.state()
    recorded = ga.eb.entries(ga.eb.load())
    print("recorded endpoints:", [(r["base"], r["key"]) for r in recorded])
    assert len(recorded) == 1, recorded
    assert recorded[0]["base"] == "http://" + HOST + "/openai", recorded
    assert recorded[0]["key"] == "sk-test", recorded

    # 同 URL 空 Key 再获取（端点不校验 Key 的那种），Key 必须留住
    app._remember_endpoint("http://" + HOST + "/openai/", "")
    kept = ga.eb.find("http://" + HOST + "/openai", ga.eb.load())
    assert kept and kept["key"] == "sk-test", ("空 Key 不该抹掉已存的", kept)
    assert len(ga.eb.entries(ga.eb.load())) == 1, "尾斜杠不该被当成第二条"

    # 历史下拉：能构建、能回填 URL + Key + 协议
    app._ep_data = ga.eb.load()
    menu = app._build_history_menu(ga.eb.entries(app._ep_data))
    labels = [menu.entrycget(i, "label") for i in range(menu.index("end") + 1)
              if menu.type(i) == "command"]
    print("history menu:", labels)
    assert any(HOST in text for text in labels), labels
    # 掩码不得把完整密钥摊在菜单上
    assert not any("sk-test" in text for text in labels), labels
    menu.destroy()

    app.var_base.set("")
    app.var_key.set("")
    app._apply_history("http://" + HOST + "/openai")
    print("after history fill:", app.var_base.get(), "|", app.var_key.get())
    assert app.var_base.get() == "http://" + HOST + "/openai", app.var_base.get()
    assert app.var_key.get() == "sk-test", app.var_key.get()

    # 失败不该新增记录：换一个必失败的端点再点一次
    probe_record_path = ga.eb.endpoint_path()
    before = probe_record_path.read_text(encoding="utf-8")
    app.var_base.set("http://" + HOST + "/nolist")
    app.var_key.set("sk-bad")
    app._start()
    pump(app, lambda: not app._busy)
    app.update()
    assert probe_record_path.read_text(encoding="utf-8") == before, "失败也写了记录"
    print("failure kept record:", app.var_status.get()[:40])
    # 复原：后面的测速用例要打在能出正文的端点上
    app.var_base.set("http://" + HOST + "/openai")
    app.var_key.set("sk-test")
    app.update()

    # 上下文/最大输出两列没有（端点不披露），但价格列有：它来自本地价格表，
    # 不指望端点，所以能稳定显示。
    assert len(app.tree["columns"]) == 6, app.tree["columns"]
    assert app.tree["columns"] == ("pick", "id", "price", "ttft", "tps", "status"), \
        app.tree["columns"]

    measured = app.tree.item("glm-5.3-flash")["values"]
    print("row glm-5.3-flash:", measured)
    assert measured[0] == "☐", measured                           # 默认没勾选
    assert measured[3] == "—" and measured[4] == "—", measured    # 未测速
    assert measured[5] == "—", measured                           # 没测过就没有状态

    # ---- 价格列：只有输入价一个数；查不到就是 — ----
    # 表头写「预估价格」，单位放在悬停说明里（PRICE_TIP），不占列宽
    assert app.tree.heading("price")["text"].startswith("预估价格"), \
        app.tree.heading("price")["text"]
    # 注意 Tk 会把「看起来像数字」的单元格回读成 int/float，比较前先 str()
    assert str(app.tree.item("gpt-6.1-sol")["values"][2]) == "3", \
        app.tree.item("gpt-6.1-sol")["values"]
    assert str(app.tree.item("glm-5.3-flash")["values"][2]) == "0.15", \
        app.tree.item("glm-5.3-flash")["values"]
    assert str(app.tree.item("deepseek-v4.1-flash-1017")["values"][2]) == "1", \
        app.tree.item("deepseek-v4.1-flash-1017")["values"]      # deepseek-* 通配命中
    assert str(app.tree.item("bad-ctx")["values"][2]) == "—", app.tree.item("bad-ctx")["values"]
    assert "百万 token" in ga.PRICE_TIP, ga.PRICE_TIP

    # 按价格排序：未知的永远垫底，升降序都不许翻上来（否则「最便宜」是假的）
    app._sort_by("price")
    app.update()
    ordered = [app.tree.item(i)["values"] for i in app.tree.get_children()]
    known = [r for r in ordered if str(r[2]) != "—"]
    assert ordered[:len(known)] == known, ordered
    firsts = [float(str(r[2])) for r in known]
    assert firsts == sorted(firsts), firsts
    app._sort_by("price")          # 再点一次反序
    app.update()
    flipped = [app.tree.item(i)["values"] for i in app.tree.get_children()]
    assert str(flipped[-1][2]) == "—", flipped
    app._sort_by("price")          # 复位成升序，后面的检查照旧

    # ---- 预设价格：自己表查不到时落到预设，并在格子里打星号 ----
    def fake_preset():
        return {"models": {"bad-ctx": {"in": 7}}}

    fake_preset.cache_clear = lambda: None       # 真实现是 lru_cache 包着的
    real_preset = ga.pb.load_preset
    ga.pb.load_preset = fake_preset
    app._reload_prices()
    app.update()
    assert str(app.tree.item("bad-ctx")["values"][2]) == "7", \
        app.tree.item("bad-ctx")["values"]
    assert str(app.tree.item("gpt-6.1-sol")["values"][2]) == "3", \
        "自己的价必须压过预设"
    ga.pb.load_preset = real_preset
    app._reload_prices()
    app.update()

    # ---- 参考价格表对话框：保存按钮只在有改动时才可点 ----
    dlg = ga.PriceBookDialog(app, data=ga.pb.load())
    app.update()
    try:
        assert "disabled" in dlg.btn_save.state(), dlg.btn_save.state()   # 刚打开必须置灰
        assert dlg.btn_save.cget("text") == "保存", dlg.btn_save.cget("text")

        dlg._on_select(type("E", (), {})())        # 没有选中行时不该崩
        dlg.tree.selection_set(dlg.tree.get_children()[0])
        dlg._on_select(type("E", (), {})())
        app.update()
        assert "disabled" in dlg.btn_save.state(), \
            ("选中一行（值没改）不该点亮保存", dlg.btn_save.state())

        before = dlg.var_in.get()
        dlg.var_in.set(before + "9")               # 只动入价：必须点亮
        app.update()
        assert "disabled" not in dlg.btn_save.state(), dlg.btn_save.state()

        dlg.var_in.set(before)                     # 改回原值：重新置灰
        app.update()
        assert "disabled" in dlg.btn_save.state(), \
            ("改回原值应回到置灰", dlg.btn_save.state())

        dlg.var_id.set("brand-new-model")          # 换 ID 同样算改动
        app.update()
        assert "disabled" not in dlg.btn_save.state(), dlg.btn_save.state()

        dlg._save()                                # 存完立刻回到置灰
        app.update()
        assert "disabled" in dlg.btn_save.state(), \
            ("保存后没有新改动，应回到置灰", dlg.btn_save.state())
        assert "brand-new-model" in (ga.pb.load().get("models") or {})

        dlg._restore()                             # 恢复预设价后也应回到置灰
        app.update()
        assert "disabled" in dlg.btn_save.state(), dlg.btn_save.state()
        assert "brand-new-model" not in (ga.pb.load().get("models") or {})
    finally:
        dlg.destroy()
        app.update()

    # ---- 勾选框：点行切换、表头全选/全不选 ----
    def click_cell(iid):
        """模拟点第一列（勾选框那一列）。"""
        bbox = app.tree.bbox(iid, "pick")
        assert bbox, ("行不可见，无法点击", iid)
        x, y = bbox[0] + bbox[2] // 2, bbox[1] + bbox[3] // 2
        event = type("E", (), {"x": x, "y": y})()
        app._on_tree_click(event)
        app.update()

    click_cell("glm-5.3-flash")
    assert app.tree.item("glm-5.3-flash")["values"][0] == "☑"
    assert app._checked == {"glm-5.3-flash"}, app._checked
    assert "已勾选 1 个模型" in app.var_import_hint.get(), app.var_import_hint.get()
    click_cell("glm-5.3-flash")
    assert app.tree.item("glm-5.3-flash")["values"][0] == "☐"
    assert not app._checked, app._checked

    # ---- 测速按钮跟着勾选数走：0 个置灰、N 个把 N 写进文案 ----
    # 勾选是散落在表格最左列的，勾了几个必须自己数；写进按钮就不必等跑完才发现测错范围。
    assert "disabled" in app.btn_speed.state(), \
        ("没勾选时测速按钮必须置灰", app.btn_speed.state())
    assert app.btn_speed.cget("text") == ga.SPEED_BUTTON_TEXT, app.btn_speed.cget("text")

    click_cell("glm-5.3-flash")
    app.update()
    assert app.btn_speed.cget("text") == f"{ga.SPEED_BUTTON_TEXT}(1)", app.btn_speed.cget("text")
    assert "disabled" not in app.btn_speed.state(), app.btn_speed.state()

    click_cell("gpt-6.1-sol")
    click_cell("deepseek-v4.1-flash-1017")
    click_cell("bad-ctx")
    app.update()
    print("speed button:", app.btn_speed.cget("text"))
    assert app.btn_speed.cget("text") == f"{ga.SPEED_BUTTON_TEXT}(4)", app.btn_speed.cget("text")
    assert "disabled" not in app.btn_speed.state(), app.btn_speed.state()

    # 取消到 0 个：文案回到不带数字，且重新置灰
    for iid in ("glm-5.3-flash", "gpt-6.1-sol", "deepseek-v4.1-flash-1017", "bad-ctx"):
        click_cell(iid)
    app.update()
    assert not app._checked, app._checked
    assert app.btn_speed.cget("text") == ga.SPEED_BUTTON_TEXT, app.btn_speed.cget("text")
    assert "disabled" in app.btn_speed.state(), app.btn_speed.state()

    # 表头全选：文案必须等于可见行数，而不是「全量行数」
    app._toggle_all()
    app.update()
    visible = len(app._view)
    print("after select-all:", app.btn_speed.cget("text"), "| visible:", visible)
    assert app.btn_speed.cget("text") == f"{ga.SPEED_BUTTON_TEXT}({visible})", \
        app.btn_speed.cget("text")
    app._toggle_all()
    app.update()
    assert "disabled" in app.btn_speed.state(), "全不选后应回到置灰"

    # ---- 双击行 = 切换勾选（再双击取消）----
    def double_click_row(iid):
        bbox = app.tree.bbox(iid, "id")          # 双击模型 ID 列，不是勾选列
        assert bbox, ("行不可见，无法双击", iid)
        x, y = bbox[0] + 20, bbox[1] + bbox[3] // 2
        event = type("E", (), {"x": x, "y": y})()
        app._on_tree_double(event)
        app.update()

    double_click_row("gpt-6.1-sol")
    assert app.tree.item("gpt-6.1-sol")["values"][0] == "☑", app.tree.item("gpt-6.1-sol")
    assert app._checked == {"gpt-6.1-sol"}, app._checked
    double_click_row("neg-ctx")
    assert app._checked == {"gpt-6.1-sol", "neg-ctx"}, app._checked
    # 再双击已勾选的行 → 取消它
    double_click_row("gpt-6.1-sol")
    assert app.tree.item("gpt-6.1-sol")["values"][0] == "☐", app.tree.item("gpt-6.1-sol")
    assert app._checked == {"neg-ctx"}, app._checked
    double_click_row("neg-ctx")
    assert not app._checked, app._checked
    app._fill()
    app.update()

    app._toggle_all()                                   # 表头：全选可见行
    app.update()
    print("after toggle_all:", len(app._checked), "| head:", app.tree.heading("pick")["text"])
    assert app._checked == {norm_id(i) for i in app.tree.get_children()}, app._checked
    assert app.tree.heading("pick")["text"] == "☑", app.tree.heading("pick")
    assert "已勾选 5 个模型" in app.var_import_hint.get(), app.var_import_hint.get()

    app._toggle_all()                                   # 再点：全不选
    app.update()
    assert not app._checked, app._checked
    assert app.tree.heading("pick")["text"] == "☐", app.tree.heading("pick")
    assert "disabled" in app.btn_import["workbuddy"].state(), app.btn_import["workbuddy"].state()
    assert "勾选" in app.var_import_hint.get(), app.var_import_hint.get()

    # 部分勾选时表头显示半选态
    click_cell("glm-5.3-flash")
    assert app.tree.heading("pick")["text"] == "◪", app.tree.heading("pick")
    app._checked.clear()
    app._fill()
    app.update()

    # ---- 框选多行后点其中一个勾选框 = 对这一批全选 / 全不选 ----
    all_rows = list(app.tree.get_children())
    group = {norm_id(i) for i in all_rows[:3]}
    app.tree.selection_set(all_rows[:3])
    app.update()
    click_cell(all_rows[1])                       # 点第 2 行，但整组一起翻
    assert app._checked == group, (app._checked, group)
    assert len(app.tree.selection()) == 3, "整组切换不该把框选弄丢"
    click_cell(all_rows[2])                       # 已全勾 → 再点整组取消
    assert not app._checked, app._checked

    # 部分勾选时再点 → 补全整组，而不是把已勾的翻掉
    app._checked = {norm_id(all_rows[0])}
    app._fill()
    app.update()
    app.tree.selection_set(all_rows[:3])
    app.update()
    click_cell(all_rows[1])
    assert app._checked == group, app._checked

    # 没有框选（只点一行）时仍只影响这一行
    app._checked.clear()
    app._fill()
    app.update()
    assert not app.tree.selection(), "重绘后应没有框选"
    click_cell(all_rows[1])
    assert app._checked == {norm_id(all_rows[1])}, app._checked
    app._checked.clear()
    app._fill()
    app.update()

    # ---- 测速三态：待测试 → 测试中… → 完成 / 超时10s以上 / 异常 ----
    ids3 = ["glm-5.3-flash", "deepseek-v4.1-flash-1017", "gpt-6.1-sol"]
    app._checked = {norm_id(i) for i in ids3}
    app._fill()
    app.var_speed_workers.set("1")                     # 压成 1 路，排队状态才看得见
    app.update()
    beat("_start_speed")
    app._start_speed()                                 # 不再弹确认框，直接开测
    # 测速期间按钮锁定并回到裸文案：此时改勾选不该让按钮数字跳动，
    # 本次测速的范围在开跑那一刻就固定了，跟着变会让人以为范围变了。
    assert "disabled" in app.btn_speed.state(), app.btn_speed.state()
    assert app.btn_speed.cget("text") == ga.SPEED_BUTTON_TEXT, app.btn_speed.cget("text")
    # 并发只有 1 路：第一个在跑，后两个必然还在排队——这一步就是要验「待测试」真实存在
    assert "pending" in app._speed_state.values(), app._speed_state
    assert app._speed_state[ids3[0]] in ("pending", "running"), app._speed_state
    pending_row = app.tree.item(ids3[-1])["values"]
    assert pending_row[5] == "待测试", pending_row
    assert "pending" in app.tree.item(ids3[-1])["tags"], app.tree.item(ids3[-1])["tags"]

    ok = pump(app, lambda: not app._speeding, timeout=25)
    print("speed status:", app.var_status.get(), "| progress:", app.var_speed_progress.get())
    assert ok, "测速未在超时内完成"
    for mid in ids3:
        res = app._speed.get(mid)
        assert res is not None and res.ok, (mid, res)
        assert res.ttft_ms and res.ttft_ms > 0, (mid, res.ttft_ms)
        assert app._speed_state[mid] == "done", app._speed_state
    done_row = app.tree.item("glm-5.3-flash")["values"]
    print("row after speed:", done_row)
    assert done_row[3].endswith(" s"), done_row            # 首字延迟以秒显示
    assert done_row[4].replace(".", "").isdigit(), done_row
    assert done_row[5] == "完成", done_row
    assert "done" in app.tree.item("glm-5.3-flash")["tags"]

    # ---- 首字超时 → 黄字「超时10s以上」，数值显示「-」 ----
    app._apply_speed(mp.SpeedResult(model="glm-5.3-flash", too_slow=True))
    app.update()
    slow_row = app.tree.item("glm-5.3-flash")["values"]
    print("row after too_slow:", slow_row)
    assert slow_row[3] == "-", slow_row
    assert slow_row[4] == "—", slow_row
    assert slow_row[5] == ga.SLOW_LABEL, slow_row
    assert "slow" in app.tree.item("glm-5.3-flash")["tags"], app.tree.item("glm-5.3-flash")["tags"]
    assert app._speed_slow == 1, app._speed_slow

    # ---- 请求失败 → 红字「异常」 ----
    app._apply_speed(mp.SpeedResult(model="deepseek-v4.1-flash-1017", error="401 凭据被拒"))
    app.update()
    err_row = app.tree.item("deepseek-v4.1-flash-1017")["values"]
    print("row after error:", err_row)
    assert err_row[5] == "异常", err_row
    assert "error" in app.tree.item("deepseek-v4.1-flash-1017")["tags"]

    # ---- 排序：首字 / 速度 / 状态三列都能点，且无数据的行永远沉底 ----
    app._speed = {
        "gpt-6.1-sol": mp.SpeedResult(model="gpt-6.1-sol", ttft_ms=100, tps=90.0, rounds=3),
        "glm-5.3-flash": mp.SpeedResult(model="glm-5.3-flash", ttft_ms=300, tps=50.0, rounds=3),
        "deepseek-v4.1-flash-1017": mp.SpeedResult(model="deepseek-v4.1-flash-1017", too_slow=True),
        "bad-ctx": mp.SpeedResult(model="bad-ctx", error="boom"),
    }
    app._speed_state = {}
    app._fill()
    app.update()

    def order_after(col):
        app._sort_by(col)
        app.update()
        # 第 0 列是勾选框，模型 ID 在第 1 列
        return [app.tree.item(i)["values"][1] for i in app.tree.get_children()]

    asc = order_after("ttft")
    print("sort ttft asc:", asc)
    assert asc[:2] == ["gpt-6.1-sol", "glm-5.3-flash"], asc     # 100ms < 300ms
    assert asc[2:] == ["deepseek-v4.1-flash-1017", "bad-ctx", "neg-ctx"], asc

    desc = order_after("ttft")                                   # 再点一次 = 降序
    print("sort ttft desc:", desc)
    assert desc[:2] == ["glm-5.3-flash", "gpt-6.1-sol"], desc
    # 反序不能把「没有首字」的行翻到最上面
    assert desc[2:] == ["deepseek-v4.1-flash-1017", "bad-ctx", "neg-ctx"], desc

    tps_asc = order_after("tps")
    print("sort tps asc:", tps_asc)
    assert tps_asc[:2] == ["glm-5.3-flash", "gpt-6.1-sol"], tps_asc   # 50 < 90
    assert tps_asc[2:] == ["deepseek-v4.1-flash-1017", "bad-ctx", "neg-ctx"], tps_asc

    st = order_after("status")
    print("sort status asc:", st)
    assert set(st[:2]) == {"gpt-6.1-sol", "glm-5.3-flash"}, st        # 完成排最前
    assert st[2] == "deepseek-v4.1-flash-1017", st                    # 超时放弃
    assert st[3] == "bad-ctx", st                                     # 异常
    assert st[4] == "neg-ctx", st                                     # 无状态沉底

    # 恢复到「上一轮测了 3 个」的状态，供后文「测速前清空」验证
    app._speed = {
        "glm-5.3-flash": mp.SpeedResult(model="glm-5.3-flash", ttft_ms=35, tps=263.9, rounds=3),
        "deepseek-v4.1-flash-1017": mp.SpeedResult(model="deepseek-v4.1-flash-1017", ttft_ms=40, tps=200.0, rounds=3),
        "gpt-6.1-sol": mp.SpeedResult(model="gpt-6.1-sol", ttft_ms=50, tps=150.0, rounds=3),
    }
    app._speed_state = {}
    app._fill()
    app.update()


    # ---- 测速只清本轮模型：先测一批，再测另一批，前一批结果必须留着 ----
    # 这就是「先测 deepseek-v4 再测 gpt-6，deepseek-v4 的数据不该被擦成 -」的回归锚点。
    app._speed = {
        "glm-5.3-flash": mp.SpeedResult(model="glm-5.3-flash", ttft_ms=35, tps=263.9, rounds=3),
        "deepseek-v4.1-flash-1017": mp.SpeedResult(model="deepseek-v4.1-flash-1017", ttft_ms=40, tps=200.0, rounds=3),
        "gpt-6.1-sol": mp.SpeedResult(model="gpt-6.1-sol", ttft_ms=50, tps=150.0, rounds=3),
    }
    app._speed_state = {
        "glm-5.3-flash": "done",
        "deepseek-v4.1-flash-1017": "done",
        "gpt-6.1-sol": "done",
    }
    app._fill()
    app.update()

    assert len(app._speed) == 3, app._speed.keys()      # 前两轮一共测了 3 个
    others = {"glm-5.3-flash", "deepseek-v4.1-flash-1017"}
    before_rows = {k: app.tree.item(k)["values"] for k in others}

    app._checked = {"deepseek-v4.1-flash-1017"}
    app._fill()
    app.update()
    beat("_start_speed")
    app._start_speed()                                  # 已无确认框
    pump(app, lambda: not app._speeding, timeout=15)
    print("speed after reset:", app.var_status.get(), "| progress:", app.var_speed_progress.get())
    # 本轮目标只有 deepseek-v4.1-flash-1017：它在重置后重新测过，仍是唯一被清的
    assert "deepseek-v4.1-flash-1017" in app._speed, app._speed.keys()
    # 非目标模型的结果必须原样留着——两条都在，数值不变
    for k in others - {"deepseek-v4.1-flash-1017"}:
        assert k in app._speed, f"{k} 的测速结果被误清了：{app._speed.keys()}"
        assert app._speed[k].ttft_ms == 35, app._speed[k]
        row = app.tree.item(k)["values"]
        print(f"  非目标行 {k}: {row}")
        assert row[3] != "—", f"{k} 的首字延迟被清成了 —：{row}"
        assert row[5] == "完成", f"{k} 的状态被清了：{row}"
    # 进度小结只统计本轮目标：1 个完成，不该把历史那两条算进来
    assert app.var_speed_progress.get() == "完成 1", app.var_speed_progress.get()

    # 反过来再验一次：改成测 glm-5.3-flash，gpt-6.1-sol 的旧值仍不能动
    app._checked = {"glm-5.3-flash"}
    app._fill()
    app.update()
    beat("_start_speed 第二批")
    app._start_speed()
    pump(app, lambda: not app._speeding, timeout=15)
    print("speed 2nd batch:", app.var_status.get(), "| progress:", app.var_speed_progress.get())
    assert "gpt-6.1-sol" in app._speed, app._speed.keys()
    assert app._speed["gpt-6.1-sol"].ttft_ms == 50, app._speed["gpt-6.1-sol"]
    assert app.tree.item("gpt-6.1-sol")["values"][3] != "—", app.tree.item("gpt-6.1-sol")
    assert app.var_speed_progress.get() == "完成 1", app.var_speed_progress.get()

    # 样本偏短的速率加「≈」：那个数主要反映端点冲刷节奏，只能横向比较
    app._speed["deepseek-v4.1-flash-1017"] = mp.SpeedResult(
        model="deepseek-v4.1-flash-1017", ttft_ms=90, tps=42.0, rounds=3, reliable=False)
    app._refresh_row("deepseek-v4.1-flash-1017")
    app.update()
    shaky = app.tree.item("deepseek-v4.1-flash-1017")["values"]
    print("shaky tps row:", shaky)
    assert shaky[4] == "≈42.0", shaky

    # ---- 过滤器：按测速状态 ----
    app._speed = {
        "glm-5.3-flash": mp.SpeedResult(model="glm-5.3-flash", too_slow=True),
        "gpt-6.1-sol": mp.SpeedResult(model="gpt-6.1-sol", ttft_ms=90, tps=42.0),
    }
    app._fill()
    app.var_filter_mode.set(ga.SLOW_LABEL)
    app._fill()
    app.update()
    print("filter", ga.SLOW_LABEL, ":", app.tree.get_children())
    assert app.tree.get_children() == ("glm-5.3-flash",), app.tree.get_children()

    app.var_filter_mode.set("已测速")
    app._fill()
    app.update()
    assert set(app.tree.get_children()) == {"glm-5.3-flash", "gpt-6.1-sol"}, app.tree.get_children()

    app.var_filter_mode.set("未测速")
    app._fill()
    app.update()
    assert len(app.tree.get_children()) == 3, app.tree.get_children()
    app._clear_filter()
    app.update()

    # ---- 搜索过滤：多关键词 AND ----
    app.var_filter.set("glm flash")
    app._fill()
    app.update()
    print("filter glm flash:", app.tree.get_children(), "|", app.var_count.get())
    assert app.tree.get_children() == ("glm-5.3-flash",), app.tree.get_children()
    assert app.var_count.get() == "显示 1 / 5", app.var_count.get()

    app.var_filter.set("zzz")
    app._fill()
    app.update()
    assert app.tree.get_children() == (), app.tree.get_children()
    assert app.var_count.get() == "显示 0 / 5", app.var_count.get()
    app._clear_filter()
    app.update()

    # ---- 勾选跟着可见行走：被筛选刷掉的行不再算选中 ----
    # 这是「UI 上只剩 4 个、底下却按 15 个跑测速」那个坑的回归锚点。
    app._checked = {norm_id(i) for i in app.tree.get_children()}
    app._fill()
    app.update()
    assert len(app._checked) == 5, len(app._checked)

    app.var_filter.set("glm flash")                # 只剩 1 行可见
    app._fill()
    app.update()
    print("checked after filter:", app._checked, "| view:", app.tree.get_children())
    assert app._checked == {"glm-5.3-flash"}, app._checked
    assert [m.id for m in app._selected()] == ["glm-5.3-flash"], app._selected()
    assert "已勾选 1 个模型" in app.var_import_hint.get(), app.var_import_hint.get()
    assert app.tree.heading("pick")["text"] == "☑", app.tree.heading("pick")

    # 全筛掉之后勾选集必须清空，导入按钮同步置灰
    app.var_filter.set("zzz")
    app._fill()
    app.update()
    assert not app._checked, app._checked
    assert not app._selected(), app._selected()
    assert "disabled" in app.btn_import["workbuddy"].state(), app.btn_import["workbuddy"].state()

    # 清空筛选不会把刚才被裁掉的选中「还回来」——选中是用户看得见的选择
    app._clear_filter()
    app.update()
    assert not app._checked, app._checked
    assert len(app.tree.get_children()) == 5, app.tree.get_children()

    # 筛选状态下的测速 / 导入只看可见且勾选的模型
    app.var_filter.set("glm flash")
    app._fill()
    app.update()
    click_cell("glm-5.3-flash")
    assert app._selected() and app._selected()[0].id == "glm-5.3-flash", app._selected()
    app._clear_filter()
    app.update()
    # 清空筛选后只剩那 1 个真的勾过的模型；先前被刷掉的 4 个不该复活
    assert app._checked == {"glm-5.3-flash"}, app._checked
    app._checked.clear()
    app._fill()
    app.update()

    # ---- 右键：只高亮被点的那一行，动作只作用于这一个模型，与勾选无关 ----
    class MenuSpy:
        """替换 tk.Menu：不真弹，只记录条目与被点的标签。"""

        last = None

        def __init__(self, master, **kw):
            self.items = []
            self._labels = {}
            self._states = {}            # label -> state，供「不许置灰」这类断言用
            self._cfg = {}
            MenuSpy.last = self

        def add_command(self, label=None, command=None, **kw):
            self._labels[label] = command
            self._states[label] = kw.get("state", "normal")
            self.items.append(label)

        def add_separator(self):
            self.items.append("-")

        def entryconfigure(self, index, **kw):
            self._cfg[index] = kw

        def state_of(self, label):
            return self._states.get(label, "normal")

        def tk_popup(self, x, y):
            pass

        def grab_release(self):
            pass

        def invoke(self, label):
            self._labels[label]()

    real_menu = ga.tk.Menu
    ga.tk.Menu = MenuSpy

    def right_click(iid):
        bbox = app.tree.bbox(iid, "id")
        assert bbox, ("行不可见，无法右键", iid)
        x, y = bbox[0] + 20, bbox[1] + bbox[3] // 2
        app._row_menu(type("E", (), {"x": x, "y": y,
                                     "x_root": 100, "y_root": 100})())
        app.update()
        return MenuSpy.last

    # 先摆一个「勾了两个」的现状，验证右键不会动它们
    app._checked = {"glm-5.3-flash", "deepseek-v4.1-flash-1017"}
    app._fill()
    app.update()
    app.tree.selection_set(("glm-5.3-flash", "deepseek-v4.1-flash-1017"))
    app.update()

    menu = right_click("gpt-6.1-sol")
    print("menu items:", menu.items, "| selection:", app.tree.selection())
    # 右键先做左键单击效果：独占高亮这一行，之前的双选被清掉
    assert app.tree.selection() == ("gpt-6.1-sol",), app.tree.selection()
    # 勾选集原封不动——右键不是勾选
    assert app._checked == {"glm-5.3-flash", "deepseek-v4.1-flash-1017"}, app._checked
    # 首行是作用对象，且只读；动作只有三个
    assert menu.items[0] == "gpt-6.1-sol", menu.items
    assert menu.items[1] == "-" and menu.items[-1] == "测速", menu.items
    # 回归锚点：菜单里**不许有任何 disabled 条目**。
    # Windows 下 Tk 用点阵(stipple)绘制禁用菜单文字，深色主题上笔画会碎成麻点、
    # 看着像字体糊了。标题行改用「normal + 悬停不变色」，忙时改文案而不是置灰。
    disabled_items = [lbl for lbl in menu.items
                      if lbl != "-" and menu.state_of(lbl) == "disabled"]
    assert not disabled_items, f"菜单里出现了 disabled 条目（会被点阵化成糊字）：{disabled_items}"
    assert menu._cfg.get(0, {}).get("activebackground"), "标题行应把悬停底色设成与静止态相同"

    # 菜单里的动作只针对被右键的这一个模型：即便勾选里还有另外两个
    wb_snapshot = wb.read_text(encoding="utf-8")      # 这段只为验单点行为，事后还原文件
    bak_before = {p.name for p in (tmp / "backups").iterdir()} if (tmp / "backups").exists() else set()
    FakeDialog.calls.clear()
    FakeDialog.plan = {"id": "gpt-6.1-sol", "name": "GPT Sol",
                       "base_url": app.var_base.get(), "api_key": app.var_key.get(),
                       "spec": ModelSpec(context=200000, vision=True,
                                         reasoning_max="high", reasoning_level="high")}
    menu.invoke("导入 WorkBuddy…")
    app.update()
    print("right-click import:", app.var_status.get(), "| dialog ids:",
          FakeDialog.calls[-1]["model_ids"])
    assert FakeDialog.calls[-1]["model_ids"] == ["gpt-6.1-sol"], FakeDialog.calls[-1]
    assert "新增 1 条" in app.var_status.get(), app.var_status.get()
    assert app._checked == {"glm-5.3-flash", "deepseek-v4.1-flash-1017"}, app._checked

    # 测速同理：只测这一个，勾选的那两个不参与
    beat("right-click speed")
    speed_before = dict(app._speed)          # 右键前的存量结果，用来验「非目标不动」
    menu.invoke("测速")
    pump(app, lambda: not app._speeding, timeout=15)
    print("right-click speed ids:", list(app._speed), "| status:", app.var_status.get())
    # 目标模型必须有本轮结果；之前测过的其他模型原样保留（只清本轮目标的语义）
    assert "gpt-6.1-sol" in app._speed, app._speed.keys()
    for gone_key, old in speed_before.items():
        if gone_key == "gpt-6.1-sol":
            continue
        assert gone_key in app._speed, f"右键单点测速把 {gone_key} 的历史结果清了"
        assert app._speed[gone_key] is old, f"{gone_key} 的结果对象被换掉了"
    assert app.var_speed_progress.get() == "完成 1", app.var_speed_progress.get()
    assert app._checked == {"glm-5.3-flash", "deepseek-v4.1-flash-1017"}, app._checked

    # 点在空白处（无行）不弹菜单、不动高亮
    menu_before = MenuSpy.last
    assert app._row_menu(type("E", (), {"x": 10, "y": 2000,
                                        "x_root": 100, "y_root": 100})()) is None
    assert MenuSpy.last is menu_before, "空白处不该新建菜单"

    # 忙态（测速进行中）：「测速」改文案而不是置灰，且仍不是 disabled
    app._speeding = True
    busy_menu = right_click("glm-5.3-flash")
    print("busy menu items:", busy_menu.items)
    assert "测速（进行中…）" in busy_menu.items, busy_menu.items
    assert busy_menu.state_of("测速（进行中…）") != "disabled", busy_menu._states
    app._speeding = False

    ga.tk.Menu = real_menu
    wb.write_text(wb_snapshot, encoding="utf-8")       # 还原，后面的批量导入断言按原样算
    # 这次真正的写入会产生一份额外备份（环形保留），把它删掉，别打乱后面对备份名的断言
    for p in (tmp / "backups").iterdir() if (tmp / "backups").exists() else []:
        if p.name not in bak_before:
            p.unlink()
    app._checked.clear()
    app._fill()
    app.update()

    # ---- 导入 WorkBuddy：走 GUI 路径（对话框替身 + 真写文件）----
    app._checked = {"glm-5.3-flash", "deepseek-v4.1-flash-1017"}
    app._fill()
    app.update()
    picked = app._selected()
    print("picked:", [m.id for m in picked])
    assert set(m.id for m in picked) == {"glm-5.3-flash", "deepseek-v4.1-flash-1017"}, picked

    FakeDialog.plan = {
        "id": "glm-5.3-flash",
        "name": "GLM Flash",
        "base_url": app.var_base.get(),
        "api_key": app.var_key.get(),
        "spec": ModelSpec(context=524288, vision=True, reasoning_max="xhigh",
                          reasoning_level="high"),
    }
    beat("导入 WorkBuddy")
    app._import_to("workbuddy")
    app.update()
    print("import -> WB:", app.var_status.get())
    assert "新增 2 条" in app.var_status.get(), app.var_status.get()
    # 勾了两个模型，对话框必须拿到这两个 ID 并把同一套参数写到两个模型上
    assert set(FakeDialog.calls[-1]["model_ids"]) == {"glm-5.3-flash",
                                                     "deepseek-v4.1-flash-1017"}, \
        FakeDialog.calls[-1]

    wb_data = json.loads(wb.read_text(encoding="utf-8"))
    wb_by = {d["id"]: d for d in wb_data}
    assert set(wb_by) == {"existing-wb", "glm-5.3-flash", "deepseek-v4.1-flash-1017"}, wb_by.keys()
    assert wb_by["glm-5.3-flash"]["maxInputTokens"] == 524288, wb_by["glm-5.3-flash"]
    assert wb_by["glm-5.3-flash"]["supportsImages"] is True
    assert wb_by["glm-5.3-flash"]["reasoning"]["defaultEffort"] == "high"
    assert wb_by["existing-wb"]["apiKey"] == "sk-existing", wb_by["existing-wb"]

    # 备份：每 Agent 各一份，固定名，写在模型管家的备份目录
    backups = sorted(p.name for p in (tmp / "backups").iterdir())
    print("backups:", backups)
    assert backups == ["wb-models.json.bak"], backups
    assert "bak-" not in " ".join(backups), "备份名不该再带时间戳"

    # ---- 导入 DSH ----
    FakeDialog.plan["base_url"] = app.var_base.get()
    beat("_import_to")
    app._import_to("dsh")
    app.update()
    print("import -> DSH:", app.var_status.get())
    assert "新增 2 条" in app.var_status.get(), app.var_status.get()
    _y, loader12, _d = ai._load_yaml()
    doc = _y.load(dsh.read_text(encoding="utf-8"), Loader=loader12)
    provs = next(e for e in doc if e.get("id") == "llm-pi-ai")["config"]["providers"]
    print("dsh providers:", list(provs))
    assert len(provs) == 2, list(provs)
    new_pid = [k for k in provs if k != "a6api"][0]
    assert provs[new_pid]["baseURL"] == app.var_base.get(), provs[new_pid]
    new_models = {m["id"]: m for m in provs[new_pid]["models"]}
    assert set(new_models) == {"glm-5.3-flash", "deepseek-v4.1-flash-1017"}, new_models.keys()
    assert new_models["glm-5.3-flash"]["contextWindow"] == 524288
    assert new_models["glm-5.3-flash"]["input"] == ["text", "image"]
    assert "off" in new_models["glm-5.3-flash"]["reasoningEfforts"]
    assert "existing-dsh" in {m["id"] for m in provs["a6api"]["models"]}, "既有 provider 被动了"
    assert cred.exists() and "refs" in _y.load(cred.read_text(encoding="utf-8"), Loader=loader12)

    backups = sorted(p.name for p in (tmp / "backups").iterdir())
    print("backups:", backups)
    assert backups == ["cordis.patch.yml.bak", "wb-models.json.bak"], backups

    # ---- 同一 Base URL、换一把 Key：必须新建 provider，两个同名模型并存 ----
    # 用户报的真实场景：一个中转站、两个令牌各能调同一批模型。
    # 这里同时验两件事：① 对话框预填的是**探测页刚填的** Key（不是目标配置里的旧 Key）；
    # ② 导入真的落到新 provider，旧 provider 的模型与密钥都没被动。
    keep_key = app.var_key.get()
    keep_checked = set(app._checked)
    other_key = "sk-token-b-0002"
    app.var_key.set(other_key)
    FakeDialog.plan = {
        "id": "deepseek-v4.1-flash-1017",
        "name": "deepseek-v4.1-flash-1017",
        "base_url": app.var_base.get(),
        "api_key": other_key,
        "spec": ModelSpec(),
    }
    app._checked = {"deepseek-v4.1-flash-1017"}
    app._fill()
    app.update()
    beat("换 Key 导入 DSH")
    app._import_to("dsh")
    app.update()
    print("换 Key 导入 ->", app.var_status.get())
    assert FakeDialog.calls[-1]["api_key"] == other_key, \
        ("对话框应预填探测页的 Key", FakeDialog.calls[-1])
    doc2 = _y.load(dsh.read_text(encoding="utf-8"), Loader=loader12)
    provs2 = next(e for e in doc2 if e.get("id") == "llm-pi-ai")["config"]["providers"]
    holders = [p for p, c in provs2.items()
               if "deepseek-v4.1-flash-1017" in {m["id"] for m in c["models"]}]
    print("同名模型所在 provider:", holders)
    assert len(holders) == 2, holders
    refs2 = _y.load(cred.read_text(encoding="utf-8"), Loader=loader12)["refs"]
    assert len({refs2[provs2[p]["apiKeyEnv"]] for p in holders}) == 2, refs2
    assert provs2["a6api"]["apiKeyEnv"] == "A6API_API_KEY", provs2["a6api"]
    # 复原现场：后面的用例还指望原来的勾选与 Key
    app.var_key.set(keep_key)
    app._checked = keep_checked
    app._fill()
    app.update()

    # ---- 管理页：读配置 ----
    app.tabbar.select(1)
    beat("_refresh_agent")
    app._refresh_agent("workbuddy")
    app.update()
    wb_rows = app._agent_tree("workbuddy").get_children()
    print("wb manage rows:", wb_rows, "|", app._agent_hint_var["workbuddy"].get())
    assert set(wb_rows) == {"existing-wb", "glm-5.3-flash", "deepseek-v4.1-flash-1017"}, wb_rows
    vals = app._agent_tree("workbuddy").item("existing-wb")["values"]
    print("wb manage row existing:", vals)
    assert vals[0].endswith("/v1") and vals[2] == "512K" and vals[3] == "是", vals

    app.tabbar.select(2)
    beat("_refresh_agent")
    app._refresh_agent("dsh")
    app.update()
    dsh_rows = app._agent_tree("dsh").get_children()
    print("dsh manage rows:", dsh_rows, "|", app._agent_hint_var["dsh"].get())
    # 4 行：existing-dsh / glm-5.3-flash / 两把 Key 各自的 deepseek-v4.1-flash-1017
    assert len(dsh_rows) == 4, dsh_rows
    dvals = app._agent_tree("dsh").item("a6api::existing-dsh")["values"]
    print("dsh manage row existing:", dvals)
    assert dvals[0] == "a6api" and dvals[2] == "128K", dvals

    # ---- 快速测速：两行同模型不同令牌，延迟必须各写各的 ----
    # 两把 Key 指向不同商家，延迟本来就可能不同。曾经按模型 ID 存延迟，
    # 两行会显示同一个数（后测的把先测的盖掉）；这里用假 probe_speed 把两者拉开，
    # 若退回「按模型 ID 一把刷」，断言立刻失败。
    def fake_probe(protocol, base_url, api_key, model, **kw):
        calls.append((base_url, api_key, model, kw))
        return mp.SpeedResult(model=model, rounds=1,
                              ttft_ms=1500 if ai.bare_key(api_key) == ai.bare_key(other_key)
                              else 3200, tps=20.0)

    calls = []
    real_probe = mp.probe_speed
    mp.probe_speed = fake_probe
    try:
        dtree = app._agent_tree("dsh")
        twins = [agents.row_iid(agents.get("dsh"), r) for r in app._agents["dsh"]
                 if r.model_id == "deepseek-v4.1-flash-1017"]
        assert len(twins) == 2, twins
        expect = {}
        for r in app._agents["dsh"]:
            if r.model_id != "deepseek-v4.1-flash-1017":
                continue
            same = ai.bare_key(r.api_key) == ai.bare_key(other_key)
            expect[agents.row_iid(agents.get("dsh"), r)] = "1.50 s" if same else "3.20 s"
        dtree.selection_set(twins)
        app.update()
        assert "disabled" not in app._agent_buttons["dsh"]["speed"].state(), \
            app._agent_buttons["dsh"]["speed"].state()
        beat("_quick_speed")
        app._quick_speed("dsh")
        assert pump(app, lambda: not app._quick_speeding, timeout=10), "快速测速没结束"
        got = {iid: str(dtree.set(iid, "latency")) for iid in twins}
        print("quick speed:", got)
        assert got == expect, (got, expect)
        # 与探测页同一套状态：成功整行绿（done），底下这两行不该有别的状态
        assert all(str(dtree.item(iid, "tags")) .find("done") >= 0 for iid in twins), \
            [dtree.item(iid, "tags") for iid in twins]
        assert len(calls) == 2, calls
        for _b, _k, _m, kw in calls:
            assert kw["rounds"] == 1 and kw["max_tokens"] == 16, kw
        assert "成功 2/2" in app.var_status.get(), app.var_status.get()
    finally:
        mp.probe_speed = real_probe
        dtree.selection_remove(*dtree.selection())
        app.update()

    # 失败不写数字，用 — 区分「测过但没拿到」与「从没测过」
    def fail_probe(protocol, base_url, api_key, model, **kw):
        return mp.SpeedResult(model=model, error="端点返回 502")

    mp.probe_speed = fail_probe
    try:
        dtree.selection_set(twins[0])
        app.update()
        app._quick_speed("dsh")
        assert pump(app, lambda: not app._quick_speeding, timeout=10), "失败路径没结束"
        # 失败与「测过但没数据」现在分开表达：格子写「失败」，整行红（error），
        # 与探测页状态列一致；「—」只留给「测过但拿不到首字」的非流式降级情况。
        assert str(dtree.set(twins[0], "latency")) == "失败", dtree.set(twins[0], "latency")
        assert "error" in tuple(dtree.item(twins[0], "tags")), dtree.item(twins[0], "tags")
        # 站点网关故障不要去重路由：换哪家都一样，只会白跑一轮
        assert not app._routing, "502 时不该自动重新路由"
    finally:
        mp.probe_speed = real_probe
        dtree.selection_remove(*dtree.selection())
        app.update()

    # HTTP 200 但正文是错误提示：也算失败，不能写延迟
    # （直接调 _apply_quick_speed，把总数设成 2，避开收尾的「要重路由吗」询问）
    app._quick_total, app._quick_done, app._quick_fail = 2, 0, 0
    app._apply_quick_speed(
        "dsh", app._agents["dsh"][0].base_url, app._agents["dsh"][0].api_key,
        twins[0],
        mp.SpeedResult(model=twins[0], rounds=1, ttft_ms=900,
                       content="上游服务器返回了无效响应或响应格式不可用，请重试"),
    )
    app.update()
    print("error-text latency:", dtree.set(twins[0], "latency"))
    assert str(dtree.set(twins[0], "latency")) == "失败", dtree.set(twins[0], "latency")
    assert "error" in tuple(dtree.item(twins[0], "tags")), dtree.item(twins[0], "tags")
    app._quick_total, app._quick_done, app._quick_fail = 0, 0, 0

    # ---- Codex 管理页：读配置 + 当前模型标记 + 列结构 ----
    app.tabbar.select(3)
    beat("_refresh_agent")
    app._refresh_agent("codex")
    app.update()
    cx_rows = app._agent_tree("codex").get_children()
    print("codex manage rows:", cx_rows, "|", app._agent_hint_var["codex"].get())
    assert tuple(cx_rows) == ("codex-existing",), cx_rows
    cvals = app._agent_tree("codex").item("codex-existing")["values"]
    print("codex manage row:", cvals)
    # 首列是 Provider·wire_api，不是端点：Codex 的模型条目本身不带端点
    assert cvals[0] == "custom · responses", cvals
    assert cvals[1] == "codex-existing", cvals
    assert cvals[2] == "✓", cvals              # 当前模型列（紧跟模型 ID）
    assert cvals[3] == "256K", cvals           # 262144 → 256K
    assert cvals[4] == "是", cvals
    assert "当前模型 codex-existing" in app._agent_hint_var["codex"].get(), \
        app._agent_hint_var["codex"].get()

    # ---- Codex 页：默认模型指向不存在条目时必须可见告警 ----
    # 场景：删掉模型但没改默认值，或回滚到不匹配的快照。Codex 启动会直接报
    # `The model 'xxx' does not exist`，界面原本看不出任何异常。
    wx = cx.toml_set(codex_cfg.read_text(encoding="utf-8"), None, "model", "ghost-model")
    codex_cfg.write_text(wx, encoding="utf-8")
    app._refresh_agent("codex")
    app.update()
    ghost_rows = app._agent_tree("codex").get_children()
    print("ghost rows:", ghost_rows)
    assert "ghost-model" in ghost_rows, ghost_rows
    gvals = app._agent_tree("codex").item("ghost-model")["values"]
    assert gvals[0].startswith("⚠"), gvals
    assert "不存在" in gvals[1], gvals
    assert app._agent_tree("codex").item("ghost-model")["tags"], "悬空行应带告警 tag"
    # 复位，别影响后续用例
    cx.set_active("codex-existing", path=codex_cfg)
    app._refresh_agent("codex")
    app.update()
    assert "ghost-model" not in app._agent_tree("codex").get_children()
    # 只有 Codex 页有「设为当前模型」按钮
    assert "active" in app._agent_buttons["codex"], list(app._agent_buttons["codex"])
    assert "active" not in app._agent_buttons["workbuddy"], "WorkBuddy 不该有该按钮"

    # ---- Codex 页：添加模型（走注册表统一入口） ----
    FakeDialog.plan = {
        "id": "codex-added", "name": "Codex 添加",
        "base_url": "https://api.example.com/v1", "api_key": "sk-codex",
        "spec": ModelSpec(context=131072, vision=True, reasoning_max="high",
                          reasoning_level="medium"),
    }
    beat("_add_agent_model(codex)")
    app._add_agent_model("codex")
    app.update()
    cx_doc = json.loads(codex_models.read_text(encoding="utf-8"))
    slugs = {m["slug"] for m in cx_doc["models"]}
    assert "codex-added" in slugs, slugs
    added = next(m for m in cx_doc["models"] if m["slug"] == "codex-added")
    # 新增条目必须继承模板载体的官方提示词，而非内置精简版
    assert added["model_messages"]["instructions_template"] == "OFFICIAL", \
        added["model_messages"]["instructions_template"][:40]
    assert "disabled" not in app._agent_buttons["codex"]["add"].state()

    # ---- Codex 页：设为当前模型 ----
    app._agent_tree("codex").selection_set(("codex-added",))
    beat("_set_active_agent")
    app._set_active_agent("codex")
    app.update()
    assert 'model = "codex-added"' in codex_cfg.read_text(encoding="utf-8"), \
        codex_cfg.read_text(encoding="utf-8")[:200]

    # ---- Codex 页：删除只动 models.json，provider 段保留 ----
    app._agent_tree("codex").selection_set(("codex-added",))
    beat("_delete_agent_model(codex)")
    app._delete_agent_model("codex")
    app.update()
    cx_doc2 = json.loads(codex_models.read_text(encoding="utf-8"))
    assert "codex-added" not in {m["slug"] for m in cx_doc2["models"]}, cx_doc2["models"]
    assert "custom" in cx.parse_toml(codex_cfg.read_text(encoding="utf-8"))["model_providers"], \
        "provider 不该被连带删除"

    # ---- Codex 页：双文件备份与还原 ----
    cx_snaps = ai.backup_snapshots(codex_cfg)
    assert cx_snaps, "Codex 写操作后应留下 config.toml 备份"
    assert ai.backup_snapshots(codex_models), "Codex 写操作后应留下 models.json 备份"
    assert ai.has_backup_paths([codex_cfg, codex_models])
    app._refresh_agent("codex")
    app.update()
    assert "disabled" not in app._agent_buttons["codex"]["restore"].state()
    app._restore_agent("codex")
    app.update()
    restored = json.loads(codex_models.read_text(encoding="utf-8"))
    assert "codex-added" not in {m["slug"] for m in restored["models"]}, \
        "还原到删除前的快照（那时已无 codex-added）"

    # ---- 导入 Claude Code：占满 5 槽 + 归一 URL + 换密钥 ----
    FakeDialog.plan["base_url"] = app.var_base.get() + "/v1"   # 故意带 /v1，验归一
    beat("_import_to(claude)")
    app._import_to("claude")
    app.update()
    print("import -> Claude:", app.var_status.get())
    # 计数单位是「个槽位」而不是「条」：这里改的是 8 个 env 键，说「8 个模型」是错的
    assert "更新 8 个槽位" in app.var_status.get(), app.var_status.get()
    cdoc = json.loads(claude_cfg.read_text(encoding="utf-8"))
    cenv = cdoc["env"]
    # 两批模型的名字里都没有 opus/sonnet/haiku 角色词 -> 5 个槽位全落在第一个模型上
    assert set(cenv[s] for s in cc.SLOT_ORDER) == {"glm-5.3-flash"}, cenv
    assert cenv[cc.SLOT_BASE_URL] == app.var_base.get(), cenv[cc.SLOT_BASE_URL]
    assert not cenv[cc.SLOT_BASE_URL].endswith("/v1"), cenv[cc.SLOT_BASE_URL]
    # 密钥用的是对话框里填的那把（这一步的 plan 沿用上一条用例），不是界面上的
    assert cenv[cc.SLOT_AUTH_TOKEN] == FakeDialog.plan["api_key"], cenv
    assert cc.SLOT_API_KEY not in cenv, "旧 ANTHROPIC_API_KEY 应被移除"
    assert cenv["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "400000", "全局设置不该被导入改写"
    assert cdoc["theme"] == "dark" and cdoc["modelSettings"], "顶层段被写坏了"
    assert ai.backup_snapshots(claude_cfg), "Claude 写入后应留下备份"

    # ---- 管理页：读配置 ----
    app.tabbar.select(4)
    beat("_refresh_agent")
    app._refresh_agent("claude")
    app.update()
    cl_rows = app._agent_tree("claude").get_children()
    print("claude manage rows:", cl_rows, "|", app._agent_hint_var["claude"].get())
    assert len(cl_rows) == 5, cl_rows
    cvals = app._agent_tree("claude").item(f"{cc.SLOT_PRIMARY}::glm-5.3-flash")["values"]
    print("claude manage row:", cvals)
    # 首列是可读槽位名（不是变量名），第二列才是模型 ID
    assert cvals[0] == "主模型", cvals
    assert cvals[1] == "glm-5.3-flash", cvals
    assert cvals[2] == "✓", cvals                # 当前模型列
    assert "当前模型" in app._agent_hint_var["claude"].get(), \
        app._agent_hint_var["claude"].get()
    assert "清空" in app._agent_buttons["claude"]["delete"].cget("text"), \
        app._agent_buttons["claude"]["delete"].cget("text")

    # ---- Claude 页：添加（走注册表统一入口，对话框隐藏了无落点字段）----
    FakeDialog.plan = {
        "id": "claude-added", "name": "Claude 添加",
        "base_url": "https://ai.freeflyagain.xyz/v1", "api_key": "sk-claude-new",
        "spec": ModelSpec(),
    }
    beat("_add_agent_model(claude)")
    app._add_agent_model("claude")
    app.update()
    assert FakeDialog.calls[-1]["hidden_spec_fields"] == agents.CLAUDE.hidden_spec_fields, \
        FakeDialog.calls[-1]
    cdoc = json.loads(claude_cfg.read_text(encoding="utf-8"))
    assert cdoc["env"][cc.SLOT_PRIMARY] == "claude-added", cdoc["env"][cc.SLOT_PRIMARY]
    assert cdoc["env"][cc.SLOT_OPUS] == "claude-added", "单模型导入应占满 5 槽"

    # ---- Claude 页：设为当前模型 ----
    # 单模型导入会把 5 个槽位全写成同一个模型，槽位不会分叉；这里手工把 Opus 槽
    # 改成另一个模型（等价于用户手改过配置，或早先做过一次批量导入），
    # 才能验到「从别的槽位提升为主模型」这条真路径。
    cdoc = json.loads(claude_cfg.read_text(encoding="utf-8"))
    cdoc["env"][cc.SLOT_OPUS] = "claude-opus-5-5"
    claude_cfg.write_text(json.dumps(cdoc, ensure_ascii=False, indent=2), encoding="utf-8")
    app._refresh_agent("claude")
    app.update()
    app._agent_tree("claude").selection_set((f"{cc.SLOT_OPUS}::claude-opus-5-5",))
    beat("_set_active_agent(claude)")
    app._set_active_agent("claude")
    app.update()
    cenv2 = json.loads(claude_cfg.read_text(encoding="utf-8"))["env"]
    assert cenv2[cc.SLOT_PRIMARY] == "claude-opus-5-5", cenv2[cc.SLOT_PRIMARY]
    assert "更新 1 个槽位" in app.var_status.get(), app.var_status.get()

    # ---- Claude 页：清空槽位只动目标行，其余槽位与端点保留 ----
    app._refresh_agent("claude")
    app.update()
    app._agent_tree("claude").selection_set((f"{cc.SLOT_OPUS}::claude-opus-5-5",))
    beat("_delete_agent_model(claude)")
    app._delete_agent_model("claude")
    app.update()
    print("clear -> Claude:", app.var_status.get())
    assert "删除 1 个槽位" in app.var_status.get(), app.var_status.get()
    cdoc = json.loads(claude_cfg.read_text(encoding="utf-8"))
    assert not cdoc["env"].get(cc.SLOT_OPUS), "目标槽位未被清空"
    assert cdoc["env"][cc.SLOT_PRIMARY] == "claude-opus-5-5", "别的槽位被误清"
    assert cdoc["env"][cc.SLOT_BASE_URL], "端点不该被连带删除"

    # ---- Claude 页：还原备份 ----
    app._refresh_agent("claude")
    app.update()
    assert "disabled" not in app._agent_buttons["claude"]["restore"].state(), \
        ("Claude 写过之后还原按钮必须可点", app._agent_buttons["claude"]["restore"].state())
    app._restore_agent("claude")
    app.update()
    assert "Claude Code" in app.var_status.get() or "还原" in app.var_status.get(), \
        app.var_status.get()

    # ---- 管理页：添加 ----
    FakeDialog.plan = {
        "id": "added-by-gui", "name": "GUI 添加", "base_url": "https://api.a6api.vip/v1",
        "api_key": "sk-added",
        "spec": ModelSpec(context=32768, vision=False, reasoning_max="low", reasoning_level="low"),
    }

    before_calls = len(FakeDialog.calls)
    beat("_add_agent_model")
    app._add_agent_model("dsh")
    app.update()
    print("add -> DSH:", app.var_status.get())
    assert "新增 1 条" in app.var_status.get(), app.var_status.get()
    assert len(FakeDialog.calls) > before_calls
    assert FakeDialog.calls[-1]["editable_id"] is True, "添加时模型 ID 必须可填"
    assert "a6api::added-by-gui" in app._agent_tree("dsh").get_children(), app._agent_tree("dsh").get_children()

    # ---- 管理页：送去探测（切页 + 填端点 + 开测）----
    app._agent_tree("dsh").selection_set(("a6api::existing-dsh",))
    app.update()
    beat("_probe_agent_model")
    app._probe_agent_model("dsh")
    app.update()
    print("probe from manage:", app.var_status.get(), "| base:", app.var_base.get())
    assert app.tabbar.index() == 0, "送去探测应切回令牌探测页"
    assert app.var_base.get() == "https://api.a6api.vip/v1", app.var_base.get()
    # 送去探测会顺手真的开一轮探测，必须等它收尾：否则它的状态栏回写会盖掉
    # 后面删除操作的结果（第一版就撞上了这个竞态）。
    pump(app, lambda: not app._busy, timeout=15)

    # ---- 管理页：删除 ----
    app.tabbar.select(2)
    app.update()
    app._agent_tree("dsh").selection_set(("a6api::added-by-gui",))
    app.update()
    beat("_delete_agent_model")
    app._delete_agent_model("dsh")
    app.update()
    print("delete -> DSH:", app.var_status.get())
    assert "删除 1 条" in app.var_status.get(), app.var_status.get()
    assert "a6api::added-by-gui" not in app._agent_tree("dsh").get_children(), app._agent_tree("dsh").get_children()

    # 删除也要留备份：配置里已经没有这个模型，provider 则必须保留
    doc2 = _y.load(dsh.read_text(encoding="utf-8"), Loader=loader12)
    provs2 = next(e for e in doc2 if e.get("id") == "llm-pi-ai")["config"]["providers"]
    assert "added-by-gui" not in {m["id"] for m in provs2["a6api"]["models"]}
    assert "a6api" in provs2, "provider 不该被连带删掉"

    # ---- 还原上一次修改：用备份覆盖当前配置 ----
    # 此时 DSH 已经写过三次（导入 / 添加 / 删除），环形里有多份快照，
    # 还原会先弹「挑一份」的对话框。替身直接选最新那份，并记录它被叫过几次。
    class FakeSnapshots:
        calls: list[tuple[str, int]] = []

        def __init__(self, master, *, agent_label, choices):
            FakeSnapshots.calls.append((agent_label, len(choices)))
            self.result = choices[0][0]

    ga.SnapshotDialog = FakeSnapshots
    app.wait_window = lambda _w=None: None  # 替身不是真窗口，别真去等

    beat("还原备份")
    backup_file = ai.backup_path_for(dsh)
    assert backup_file.exists(), "删除之后应该留下备份"
    snapshot = backup_file.read_text(encoding="utf-8")
    assert snapshot != dsh.read_text(encoding="utf-8"), "备份应与当前内容不同（备份是删除前的）"
    assert "disabled" not in app._agent_buttons["dsh"]["restore"].state(), \
        app._agent_buttons["dsh"]["restore"].state()

    app._restore_agent("dsh")
    app.update()
    print("restore -> DSH:", app.var_status.get())
    assert FakeSnapshots.calls, "多份快照时必须让用户挑，而不是默默用最新那份"
    assert FakeSnapshots.calls[-1][1] >= 2, FakeSnapshots.calls
    assert dsh.read_text(encoding="utf-8") == snapshot, "还原后文件内容应等于备份"
    assert "还原" in app.var_status.get(), app.var_status.get()
    restored_provs = next(
        e for e in _y.load(dsh.read_text(encoding="utf-8"), Loader=loader12)
        if e.get("id") == "llm-pi-ai"
    )["config"]["providers"]
    assert "added-by-gui" in {m["id"] for m in restored_provs["a6api"]["models"]}, \
        "还原应该把被删掉的模型找回来"

    # 重复点没有副作用，也不需要置灰
    assert "disabled" not in app._agent_buttons["dsh"]["restore"].state(), \
        "还原按钮不该一次性置灰"
    app._restore_agent("dsh")
    app.update()
    assert dsh.read_text(encoding="utf-8") == snapshot, "重复还原结果应一致"

    # 没有备份时点还原：给提示、不动文件（环形可能有多份，全清掉才算「没备份」）
    for stale in ai.backup_snapshots(wb):
        stale.unlink()
    app._refresh_agent("workbuddy")
    app.update()
    assert "disabled" in app._agent_buttons["workbuddy"]["restore"].state(), \
        "没有备份时还原按钮应置灰"
    before_wb = wb.read_text(encoding="utf-8")
    app._restore_agent("workbuddy")
    app.update()
    assert wb.read_text(encoding="utf-8") == before_wb, "没备份不该改动文件"

    # ---- 底部那排「复制/导出/原始响应」已整排删除，确认没有残留入口 ----
    app.tabbar.select(0)
    app.update()
    for gone in ("_export", "_copy_selected", "_copy_all", "_show_raw"):
        assert not hasattr(ga.App, gone), f"{gone} 应该已经删掉了"

    # ---- 稳定性：Agent 没装时不该崩，按钮置灰 + 给提示 ----
    real_wb, real_dsh = ai.WORKBUDDY_MODELS, ai.DSH_PATCH
    real_cfg, real_models = cx.CODEX_CONFIG, cx.CODEX_MODELS
    ai.WORKBUDDY_MODELS = tmp / "absent" / "models.json"
    ai.DSH_PATCH = tmp / "absent" / "cordis.patch.yml"
    cx.CODEX_CONFIG = tmp / "absent" / "config.toml"
    cx.CODEX_MODELS = tmp / "absent" / "models.json"
    abs_claude = tmp / "absent-claude"
    os.environ["CLAUDE_CONFIG_DIR"] = str(abs_claude)
    for kind in agents.kinds():
        beat("_refresh_agent")
        app._refresh_agent(kind)
    app.update()
    print("missing hints:", app._agent_hint_var["workbuddy"].get()[:20], "|", app._agent_hint_var["dsh"].get()[:20])
    assert app._agent_ok == {"workbuddy": False, "dsh": False, "codex": False,
                            "claude": False}, app._agent_ok
    assert "未检测到 WorkBuddy" in app._agent_hint_var["workbuddy"].get(), app._agent_hint_var["workbuddy"].get()
    assert "未检测到 DeepSeek Harness" in app._agent_hint_var["dsh"].get(), app._agent_hint_var["dsh"].get()
    assert "未检测到 Claude Code" in app._agent_hint_var["claude"].get(), \
        app._agent_hint_var["claude"].get()
    for kind in agents.kinds():
        btns = app._agent_buttons[kind]
        for key in ("add", "delete", "probe", "open", "restore"):
            if key in btns:
                assert "disabled" in btns[key].state(), (kind, key)
        assert "disabled" not in btns["refresh"].state(), (kind, "刷新必须还能点")
    # 勾了模型也不能导入：目标 Agent 不在
    app._checked = {"glm-5.3-flash"}
    app._sync_import_buttons()
    app.update()
    print("import hint when missing:", app.var_import_hint.get())
    for kind in agents.kinds():
        assert "disabled" in app.btn_import[kind].state(), (kind, app.btn_import[kind].state())
    assert "未检测到" in app.var_import_hint.get(), app.var_import_hint.get()
    # 探测页本身不受影响
    assert "disabled" not in app.btn_go.state(), app.btn_go.state()

    # 装回来（路径恢复）后点刷新应重新可用
    ai.WORKBUDDY_MODELS, ai.DSH_PATCH = real_wb, real_dsh
    cx.CODEX_CONFIG, cx.CODEX_MODELS = real_cfg, real_models
    os.environ["CLAUDE_CONFIG_DIR"] = str(claude_home)
    for kind in agents.kinds():
        beat("_refresh_agent")
        app._refresh_agent(kind)
    app.update()
    assert app._agent_ok == {"workbuddy": True, "dsh": True, "codex": True,
                            "claude": True}, app._agent_ok
    assert "disabled" not in app._agent_buttons["dsh"]["add"].state(), "恢复后应可添加"

    # ---- 失败态：错误 key ----
    beat("失败态：错误 key")
    app.var_key.set("sk-wrong")
    beat("_start")
    app._start()
    pump(app, lambda: not app._busy)
    app.update()
    print("failure status:", app.var_status.get())
    assert "凭据被拒" in app.var_status.get(), app.var_status.get()

    # ---- 恢复成功态并截图 ----
    app.var_key.set("sk-test")
    beat("_start")
    app._start()
    pump(app, lambda: not app._busy and len(app.tree.get_children()) > 0)
    app.update()
    # 前面的过滤器/排序用例往 _speed 里塞过假数据，截图前清掉：
    # 主图要呈现「刚探测完、还没测速」的正常样子
    app._speed.clear()
    app._speed_state.clear()
    app._fill()
    app._update_speed_progress()      # 状态清了，进度小结也要一起清
    app.update()

    _bring_to_front(app)

    try:
        from PIL import ImageGrab

        x, y = app.winfo_rootx(), app.winfo_rooty()
        w, h = app.winfo_width(), app.winfo_height()
        box = (x - 8, y - 32, x + w + 8, y + h + 8)
        ImageGrab.grab(bbox=box).save(SHOTS / "shot_main.png")
        print("saved shot_main.png")
        app.tabbar.select(1)
        app.update()
        time.sleep(0.4)
        app.update()
        ImageGrab.grab(bbox=box).save(SHOTS / "shot_workbuddy.png")
        print("saved shot_workbuddy.png")
        app.tabbar.select(2)
        app.update()
        time.sleep(0.4)
        app.update()
        ImageGrab.grab(bbox=box).save(SHOTS / "shot_dsh.png")
        print("saved shot_dsh.png")
        # 管理页的五种测速状态各占一行，留一张图核对配色与文案
        # （与探测页同一套 SPEED_STATES；行染色 + 延迟列写状态文案）
        _paint_state_demo(app)
        # 刚销毁的弹窗还在淡出，等一帧再抓，否则图上会有一层半透明的残影
        for _ in range(6):
            app.update()
            time.sleep(0.08)
        ImageGrab.grab(bbox=box).save(SHOTS / "shot_dsh_states.png")
        print("saved shot_dsh_states.png")
        _clear_state_demo(app)
        # Codex 页：多一列「当前」与「设为当前模型」按钮，值得单独留图。
        # 前面的用例把默认模型指向了已删条目，截图前先复位成有效值，
        # 否则图上是「悬空引用告警行」，不是这个页面正常的样子
        cx.set_active("codex-existing", path=codex_cfg)
        app._refresh_agent("codex")
        # 前面的还原用例会留下真实 Toplevel（替身只拦了一部分路径），截图前清掉，
        # 否则弹窗会盖住表格，图看起来像「页面被遮挡」
        for child in app.winfo_children():
            if isinstance(child, import_tk.Toplevel):
                child.destroy()
        app.tabbar.select(3)
        app.update()
        time.sleep(0.4)
        app.update()
        ImageGrab.grab(bbox=box).save(SHOTS / "shot_codex.png")
        print("saved shot_codex.png")
        # Claude Code 页：首列是槽位、多一列「当前」、删除按钮叫「清空选中槽位」
        app._refresh_agent("claude")
        app.tabbar.select(4)
        app.update()
        time.sleep(0.4)
        app.update()
        ImageGrab.grab(bbox=box).save(SHOTS / "shot_claude.png")
        print("saved shot_claude.png")
    except Exception as e:  # noqa: BLE001
        print("screenshot skipped:", e)

    # ---- 管理页的测速状态：五种状态各有颜色与文案，与探测页一致 ----
    # 放在截图 try 之外：断言被 except 吞掉过一次，那比没有断言更糟。
    _paint_state_demo(app)
    app.update()
    dsh_spec = agents.get("dsh")
    demo_rows = _demo_rows(app)
    want = [("pending", "待测试"), ("running", "测试中…"), ("done", "1.86"),
            ("slow", "超时"), ("error", "失败")]
    for row, (state, label) in zip(demo_rows, want):
        iid = agents.row_iid(dsh_spec, row)
        got_tag = tuple(app._agent_tree("dsh").item(iid, "tags"))
        got_cell = str(app._agent_tree("dsh").set(iid, "latency"))
        assert got_tag == (state,), (row.model_id, got_tag, state)
        assert got_cell == label, (row.model_id, got_cell, label)
    done_row = demo_rows[2]
    got_cache = str(app._agent_tree("dsh").set(agents.row_iid(dsh_spec, done_row), "cache"))
    assert got_cache == "91%", got_cache
    print("agent states:", [(r.model_id, app._agent_tree("dsh").item(
        agents.row_iid(dsh_spec, r), "tags")) for r in demo_rows])
    _clear_state_demo(app)
    app.update()

    # ---- 设置窗口的权重预设：三档按钮各改三项权重，标签跟着走 ----
    # 这里不测「选出来是哪一家」（那要联网），只测「点了按钮，填进去的数对不对、
    # 门槛有没有被顺手改掉」——后者是这类按钮最容易犯的错。
    dlg = ga.SettingsDialog(app, sr.load_settings(), "https://api.a6api.vip/v1")
    dlg.update_idletasks()
    free = {"max_ratio", "hard_p50_ms", "min_samples", "max_fail_rate", "candidates"}
    before = {name: dlg._vars[name].get() for name in dlg._vars if name in free}
    for preset, btn in zip(sr.WEIGHT_PRESETS, dlg._preset_buttons):
        btn.invoke()
        got = tuple(dlg._vars[name].get() for name in sr.WEIGHT_FIELDS)
        assert got == tuple(str(v) for v in preset.weights), (preset.name, got)
        assert dlg.var_preset.get() == f"当前：{preset.name}", dlg.var_preset.get()
        assert {n: dlg._vars[n].get() for n in before} == before, "预设不该改门槛"
    dlg._vars["price_weight"].set("70")
    assert dlg.var_preset.get() == "当前：自定义", dlg.var_preset.get()
    dlg._reset()
    assert dlg.var_preset.get().startswith("当前："), dlg.var_preset.get()
    print("preset buttons:", [b.cget("text") for b in dlg._preset_buttons],
          "|", dlg.var_preset.get())
    # 新窗口第一帧是透明的（系统淡入），立刻抓会得到一张「能看见主窗口」的图，
    # 和之前弹窗残影是同一个坑：多等几帧再抓。
    dlg.lift()
    for _ in range(8):
        dlg.update()
        time.sleep(0.06)
    dbox = (dlg.winfo_rootx(), dlg.winfo_rooty(),
            dlg.winfo_rootx() + dlg.winfo_width(), dlg.winfo_rooty() + dlg.winfo_height())
    ImageGrab.grab(bbox=dbox).save(SHOTS / "shot_settings.png")
    print("saved shot_settings.png")
    dlg.destroy()
    app.update()

    app._on_close()
    cfg_text = ga.CONFIG_PATH.read_text(encoding="utf-8")
    print("saved config:", cfg_text.replace("\n", " "))
    assert "sk-test" not in cfg_text, "API Key 被写入配置文件"
    assert "sk-existing" not in cfg_text, "配置里不该出现任何密钥"
    assert "key_b64" not in cfg_text, "旧版 key_b64 未被清除"
    assert "library" not in cfg_text, "配置里不该再有模型库路径"
    srv.shutdown()
    print("SMOKE OK")


if __name__ == "__main__":
    main()
