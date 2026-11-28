#!/usr/bin/env python3
"""模型管家 · 界面层。

页签结构：① 令牌探测 + ②..N 每个 Agent 一页（顺序与标题由 `agents.py` 注册表决定）。

  ① 令牌探测 —— 输入 BaseURL + API Key，拉取模型列表；测首字与速度；
                 选中模型可直接导入任一已注册 Agent。
  ②..N 管理  —— 读该 Agent 的配置文件，增删自定义模型、还原备份。
                 新增 Agent 只需在 `agents.py` 里加一条 AgentDef，本文件不用改。

模型库已取消：上下文 / 图片 / 推理等级不再持久化，改为每次导入时在对话框里显式
选定，直接写进目标配置。测速结果只活在本次运行内（关掉即清空）。

模块分工：网络层 `model_probe.py`，Agent 适配器 `agent_import.py` +
`codex_import.py`，注册表 `agents.py`，参数规格 `model_spec.py`。
零第三方依赖：标准库 tkinter（DSH 的 YAML 读写额外需要 PyYAML）。
"""

from __future__ import annotations

import ctypes
import json
import queue
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from urllib.parse import urlsplit

from . import agent_import as ai
from . import agents
from . import endpoint_book as eb
from . import model_probe as mp
from . import model_spec as ms
from . import price_book as pb
from . import smart_route as sr
from .paths import asset_dir

CONFIG_PATH = Path.home() / ".model_probe.json"

# 窗口 / 任务栏图标：源码运行取 `modelkeeper/assets`，打包后取解包目录，差异见 `paths.py`。
ICON_PATH = asset_dir() / "keeper.ico"

# 测速：轮数与输出上限都是界面可调项，默认值取自网络层
SPEED_ROUND_CHOICES = ("1", "2", "3", "5")
# 输出上限档位：从「够出字」到「够算速率」。默认取最小档——探测页最常见的用法是
# 验证「这个模型能不能调通」，不是测准 tok/s；要测速率自己往上调。
SPEED_TOKEN_CHOICES = ("16", "32", "64", "128", "256", "512", "1024")
SPEED_TOKEN_DEFAULT = SPEED_TOKEN_CHOICES[0]
# 管理页「快速测速」的口径：1 轮 + 16 个输出 token。
# 目的是判断「这条路此刻通不通、首字够不够快」，不是量吞吐——所以花到最少。
QUICK_SPEED_ROUNDS = 1
QUICK_SPEED_TOKENS = 16
SPEED_WORKER_CHOICES = ("1", "2", "4", "8", "16")

# 测速按钮的固定文案；实际显示会在后面补 `(N)` 表示本次要测几个。
SPEED_BUTTON_TEXT = "测首字与速度"

AGENT_LABELS = agents.labels()

# --------------------------------------------------------------------------- #
# 主题
# --------------------------------------------------------------------------- #

BG = "#1e1f22"
PANEL = "#2b2d30"
FIELD = "#2b2d30"
FG = "#dfe1e5"
MUTED = "#9da0a8"
ACCENT = "#3574f0"
BORDER = "#393b40"
OK = "#5fb865"
WARN = "#d9a343"
ERR = "#e0645c"

# 页签悬停色：只比未选中态略亮，避免与选中蓝抢注意力
TAB_HOVER = "#33353a"

# 探测页过滤器：搜索框做多关键词 AND，下拉做互斥的测速状态筛选
FILTER_ALL = "全部"
# 「首字超时被放弃」的唯一文案：表格状态、筛选下拉、状态栏汇总都引它，
# 改措辞时不会只改到一处。阈值本身来自 mp.SPEED_TTFT_LIMIT_MS（10 秒）。
SLOW_LABEL = "超时10s以上"
FILTER_MODES = (FILTER_ALL, "已测速", SLOW_LABEL, "未测速")

# 筛选框防抖窗口（毫秒）。180 ms 在「感觉不到延迟」与「连打不积压」之间：
# 低于 120 ms 基本等同于不防抖，高于 300 ms 会让人觉得输入没反应。
FILTER_DEBOUNCE_MS = 180

# 测速一行的四种状态：(状态列文字, 整行前景色)。
# 灰 = 还没轮到它；蓝 = 线程正在跑这一条；绿 = 拿到数据；黄 = 超时放弃；
# 红 = 请求失败。整行染色而不只是状态列，是因为眼睛扫的是行，
# 逐格染色在 Treeview 里做不到，也没必要。
SPEED_STATES: dict[str, tuple[str, str]] = {
    "pending": ("待测试", MUTED),
    "running": ("测试中…", "#4a9eff"),
    "done": ("完成", OK),
    "slow": (SLOW_LABEL, WARN),
    "error": ("异常", ERR),
}
SPEED_STATE_TAGS = tuple(SPEED_STATES)


def apply_theme(root: tk.Tk) -> ttk.Style:
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    root.configure(bg=BG)
    style.configure(".", background=BG, foreground=FG, fieldbackground=FIELD, borderwidth=0)
    style.configure("TFrame", background=BG)
    style.configure("Panel.TFrame", background=PANEL)
    style.configure("TLabel", background=BG, foreground=FG, font=("Microsoft YaHei UI", 9))
    style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=("Microsoft YaHei UI", 9))
    style.configure("Status.TLabel", background=PANEL, foreground=MUTED, font=("Microsoft YaHei UI", 9))
    style.configure("Title.TLabel", background=BG, foreground=FG, font=("Microsoft YaHei UI", 12, "bold"))
    # 设置面板的分段标题：比正文重一档，比 Title 小一档
    style.configure("Section.TLabel", background=BG, foreground=FG,
                    font=("Microsoft YaHei UI", 9, "bold"))
    style.configure("TEntry", fieldbackground=FIELD, foreground=FG, insertcolor=FG,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER, padding=5)
    style.map("TEntry", bordercolor=[("focus", ACCENT)])
    style.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=FG,
                    arrowcolor=FG, bordercolor=BORDER, padding=4)
    style.map("TCombobox", fieldbackground=[("readonly", FIELD)],
              foreground=[("readonly", FG)], bordercolor=[("focus", ACCENT)])
    style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                    font=("Microsoft YaHei UI", 9, "bold"), padding=(16, 7), borderwidth=0)
    # 禁用态要明显比普通按钮更沉：Accent 的底色一旦只从蓝变灰，就和旁边的
    # 普通按钮（#393b40）几乎同色，看不出「点不了」。压暗底色、压暗文字才有区分。
    style.map("Accent.TButton", background=[("active", "#2b62d9"), ("disabled", "#2a2c30")],
              foreground=[("disabled", "#5c5f66")])
    style.configure("TButton", background="#393b40", foreground=FG, padding=(12, 6), borderwidth=0)
    style.map("TButton", background=[("active", "#45484f"), ("disabled", "#303236")],
              foreground=[("disabled", "#6b6e75")])
    style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG,
                    borderwidth=0, rowheight=26, font=("Microsoft YaHei UI", 9))
    style.configure("Treeview.Heading", background="#393b40", foreground=FG,
                    font=("Microsoft YaHei UI", 9, "bold"), padding=(8, 6), borderwidth=0)
    style.map("Treeview.Heading", background=[("active", "#45484f")])
    style.map("Treeview", background=[("selected", "#2e436e")], foreground=[("selected", "#ffffff")])
    style.configure("TScrollbar", background="#393b40", troughcolor=PANEL,
                    arrowcolor=MUTED, borderwidth=0)
    style.map("TScrollbar", background=[("active", "#45484f")])
    return style


def apply_window_icon(root: tk.Misc) -> None:
    """窗口与任务栏图标。

    只设 `iconbitmap` 还不够：从 bat 启动时进程是 pythonw.exe，Windows 会把窗口
    归到 pythonw 名下。先声明 AppUserModelID，任务栏才会画我们自己的图标，并把
    窗口归成独立的一组。
    """
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ModelKeeper.Desktop.1")
        except OSError:
            pass
    try:
        # 两条都要设：default= 管对话框，无 default 的那条管主窗口本身
        root.iconbitmap(default=str(ICON_PATH))
        root.iconbitmap(str(ICON_PATH))
    except tk.TclError:
        pass  # 图标缺失不该让程序起不来


class CheckBox(tk.Frame):
    """自绘复选框，替代 ttk.Checkbutton。

    clam 主题的判据是「选中 = 小 ✕、未选中 = 空框」，而 ✕ 在大众语义里是
    「关闭 / 不支持 / 错误」，读起来正好和真实状态相反——勾上图片支持像没勾上。
    预置图片、改 style 元素都绕不开主题自带的绘制，所以直接自己画：未选中是
    空方框，选中是蓝底白对勾，两种状态都不会被读成「叉」。

    仍然读写同一个 ``tk.BooleanVar``，容器换皮不影响调用点。
    """

    SIZE = 15

    def __init__(self, master: tk.Misc, *, text: str, variable: tk.BooleanVar,
                 font: tuple = ("Microsoft YaHei UI", 9)) -> None:
        super().__init__(master, bg=BG)
        self.var = variable

        self.canvas = tk.Canvas(self, width=self.SIZE, height=self.SIZE, bg=BG,
                                highlightthickness=1, highlightbackground=BG,
                                highlightcolor=ACCENT, bd=0, cursor="hand2",
                                takefocus=1)
        self.canvas.pack(side="left")
        self.label = tk.Label(self, text=text, bg=BG, fg=MUTED, font=font, cursor="hand2")
        self.label.pack(side="left", padx=(8, 0))

        for widget in (self.canvas, self.label):
            widget.bind("<Button-1>", self._on_click)
            widget.bind("<Enter>", lambda _e: self.label.configure(fg=FG))
            widget.bind("<Leave>", lambda _e: self.label.configure(fg=MUTED))
        self.canvas.bind("<space>", self._on_click)
        self.canvas.bind("<Return>", self._on_click)

        self._trace = variable.trace_add("write", lambda *_: self._draw())
        self._draw()

    def toggle(self) -> None:
        self.var.set(not bool(self.var.get()))

    def _on_click(self, _event: tk.Event) -> str:
        self.toggle()
        return "break"

    def _draw(self) -> None:
        """按当前值重画：空框 / 蓝底白勾。"""
        c = self.canvas
        c.delete("all")
        if self.var.get():
            c.create_rectangle(1, 1, 13, 13, fill=ACCENT, outline=ACCENT)
            c.create_line(3.6, 7.4, 6.2, 10.0, 10.4, 4.6, fill="#ffffff",
                          width=2, capstyle="round", joinstyle="round")
        else:
            c.create_rectangle(1, 1, 13, 13, fill=FIELD, outline=BORDER)


class TabBar(ttk.Frame):
    """自绘页签栏。

    ttk.Notebook 的 tab 宽度由文字长度决定，`-width` 在 clam 下会把选中项异常拉宽，
    因此改为 tk.Frame 固定像素宽度 + 手动切页：等宽、选中纯色蓝、未选中保持深灰。
    """

    TAB_W = 176
    TAB_H = 32
    GAP = 2

    def __init__(self, master: tk.Misc) -> None:
        super().__init__(master)
        self._pages: list[tk.Widget] = []
        self._items: list[tuple[tk.Frame, tk.Label]] = []
        self._index = -1

    def add(self, page: tk.Widget, text: str) -> None:
        holder = tk.Frame(self, width=self.TAB_W, height=self.TAB_H, bg=PANEL, cursor="hand2")
        holder.pack(side="left", padx=(0, self.GAP))
        holder.pack_propagate(False)  # 关掉自动收缩，宽度才是固定像素
        label = tk.Label(holder, text=text, bg=PANEL, fg=MUTED,
                         font=("Microsoft YaHei UI", 9), cursor="hand2")
        label.pack(fill="both", expand=True)

        idx = len(self._items)
        for widget in (holder, label):
            widget.bind("<Button-1>", lambda _e, i=idx: self.select(i))
            widget.bind("<Enter>", lambda _e, i=idx: self._hover(i, True))
            widget.bind("<Leave>", lambda _e, i=idx: self._hover(i, False))

        self._items.append((holder, label))
        self._pages.append(page)
        if self._index < 0:
            self.select(0)

    def select(self, index: int) -> None:
        if not 0 <= index < len(self._pages) or index == self._index:
            return
        for page in self._pages:
            page.pack_forget()
        self._pages[index].pack(fill="both", expand=True)
        for n, (holder, label) in enumerate(self._items):
            bg = ACCENT if n == index else PANEL
            fg = "#ffffff" if n == index else MUTED
            holder.configure(bg=bg)
            label.configure(bg=bg, fg=fg)
        self._index = index

    def index(self) -> int:
        return self._index

    def _hover(self, index: int, on: bool) -> None:
        if index == self._index:
            return
        bg = TAB_HOVER if on else PANEL
        holder, label = self._items[index]
        holder.configure(bg=bg)
        label.configure(bg=bg)


# --------------------------------------------------------------------------- #
# 模型配置对话框：导入 / 添加都走它
# --------------------------------------------------------------------------- #


def _wrap_ids(ids: list[str], per_line: int = 42, limit: int = 10) -> str:
    """把一批模型 ID 折成多行，只在 ID 之间断开。

    tkinter 的 `wraplength` 是逐字符折行的，`gemini-3.7-flash` 会被切成
    `gemini-3.` / `7-flash`，看起来像两个模型；所以这里自己按 ID 边界折。
    超过 `limit` 个只列前 `limit` 个，末尾补一句总数。
    """
    lines: list[str] = []
    cur = ""
    for mid in ids[:limit]:
        piece = mid if not cur else "、" + mid
        if cur and len(cur) + len(piece) > per_line:
            lines.append(cur)
            cur = mid
        else:
            cur += piece
    if cur:
        lines.append(cur)
    if len(ids) > limit:
        lines.append(f"…等共 {len(ids)} 个")
    return "\n".join(lines)


class ModelConfigDialog(tk.Toplevel):
    """设置一个模型写进 Agent 配置时的四项参数。

    同一个对话框服务两条路径：
      * 从探测页「导入」——模型 ID / 名称固定，只调参数与 Key；
      * 从管理页「添加」——ID / 名称也要填。

    打开时会用目标配置里**已有的值**预填：否则一个原本 1M 的模型，用户点确定
    就被默认的 512K 悄悄降级了，而这种降级在界面上看不出任何异常。
    """

    def __init__(
        self,
        master: tk.Misc,
        *,
        title: str,
        agent_label: str,
        model_id: str = "",
        model_name: str = "",
        base_url: str = "",
        api_key: str = "",
        spec: ms.ModelSpec | None = None,
        editable_id: bool = False,
        model_ids: list[str] | None = None,
        hidden_spec_fields: tuple[str, ...] = (),
        spec_note: str = "",
    ) -> None:
        super().__init__(master)
        self.title(title)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master.winfo_toplevel())
        self.result: dict | None = None

        spec = spec or ms.ModelSpec()
        self._editable_id = editable_id
        self._spec = spec
        self._hidden = set(hidden_spec_fields or ())
        self._spec_note = spec_note
        # 批量导入时 model_ids 是这一次要写的全部模型；它们共用下面这一套参数。
        batch = [m for m in (model_ids or []) if m]

        body = ttk.Frame(self, padding=(16, 14, 16, 14))
        body.pack(fill="both", expand=True)
        body.columnconfigure(1, weight=1)

        row = 0
        ttk.Label(body, text="写入到", style="Muted.TLabel").grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=(0, 8))
        ttk.Label(body, text=agent_label).grid(row=row, column=1, sticky="w", pady=(0, 8))
        row += 1

        ttk.Label(body, text="模型 ID").grid(row=row, column=0, sticky="w", padx=(0, 10), pady=(0, 8))
        self.var_id = tk.StringVar(value=model_id)
        if editable_id:
            ent = ttk.Entry(body, textvariable=self.var_id, width=42)
            ent.grid(row=row, column=1, sticky="ew", pady=(0, 8))
            ent.focus_set()
        else:
            ttk.Label(body, text=model_id or "（未选）").grid(row=row, column=1, sticky="w", pady=(0, 8))
        row += 1

        # 批量导入：这里必须把这一批全列出来。只显示第一个模型时，用户会以为
        # 其余勾选项被丢掉了——实际上它们都会用下面这套参数写进配置。
        if len(batch) > 1:
            ttk.Label(body, text="同批写入").grid(
                row=row, column=0, sticky="nw", padx=(0, 10), pady=(0, 8))
            box = ttk.Frame(body)
            box.grid(row=row, column=1, sticky="w", pady=(0, 8))
            ttk.Label(box, text=f"共 {len(batch)} 个模型，都会用下面这套参数写入：",
                      style="Muted.TLabel").pack(anchor="w")
            ttk.Label(box, text=_wrap_ids(batch), justify="left").pack(anchor="w")
            row += 1

        ttk.Label(body, text="名称").grid(row=row, column=0, sticky="w", padx=(0, 10), pady=(0, 8))
        self.var_name = tk.StringVar(value=model_name or model_id)
        if len(batch) > 1:
            # 批量写入时每个模型沿用自己探测到的名字，这个输入框本来就不会生效；
            # 留一个能改但改了没用的框，比不显示更糟。
            ttk.Label(body, text="每个模型各用各自的名字",
                      style="Muted.TLabel").grid(row=row, column=1, sticky="w", pady=(0, 8))
        else:
            ttk.Entry(body, textvariable=self.var_name, width=42).grid(
                row=row, column=1, sticky="ew", pady=(0, 8))
        row += 1

        ttk.Label(body, text="Base URL").grid(row=row, column=0, sticky="w", padx=(0, 10), pady=(0, 8))
        self.var_base = tk.StringVar(value=base_url)
        ttk.Entry(body, textvariable=self.var_base, width=42).grid(
            row=row, column=1, sticky="ew", pady=(0, 8))
        row += 1

        ttk.Label(body, text="API Key").grid(row=row, column=0, sticky="w", padx=(0, 10), pady=(0, 8))
        self.var_key = tk.StringVar(value=api_key)
        ttk.Entry(body, textvariable=self.var_key, width=42, show="•").grid(
            row=row, column=1, sticky="ew", pady=(0, 8))
        row += 1

        sep = ttk.Frame(body, height=1)
        sep.configure(style="Panel.TFrame")
        sep.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(4, 10))
        row += 1

        # 下面四项按 `hidden_spec_fields` 决定是否出现在界面上。变量一律建好，
        # 这样 `_ok()` 不必分支——隐藏字段在确定时沿用传入 spec 的原值，不会被
        # 「输入框为空」误读成 0/False 再写回配置。
        ttk.Label(body, text="上下文").grid(row=row, column=0, sticky="w", padx=(0, 10), pady=(0, 8))
        self.var_ctx = tk.StringVar(
            value=ms.context_label(spec.context) if spec.context else ms.context_label(ms.DEFAULT_CONTEXT))
        if "context" not in self._hidden:
            ttk.Combobox(body, textvariable=self.var_ctx, state="readonly", width=12,
                         values=[label for label, _ in ms.CONTEXT_OPTIONS]).grid(
                row=row, column=1, sticky="w", pady=(0, 8))
        else:
            ttk.Label(body, text="（该 Agent 无此字段）", style="Muted.TLabel").grid(
                row=row, column=1, sticky="w", pady=(0, 8))
        row += 1

        ttk.Label(body, text="最大输出").grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=(0, 8))
        # 0 就是不写进配置。探测到端点披露的输出上限时，_import_to 会把它填成默认值。
        self.var_max_out = tk.StringVar(
            value=ms.context_label(spec.max_tokens) if spec.max_tokens else "")
        out_row = ttk.Frame(body)
        out_row.grid(row=row, column=1, sticky="w", pady=(0, 8))
        if "max_out" not in self._hidden:
            ttk.Entry(out_row, textvariable=self.var_max_out, width=12).pack(side="left")
            ttk.Label(out_row, text="留空=不写（跟随提供方默认）",
                      style="Muted.TLabel").pack(side="left", padx=(8, 0))
        else:
            ttk.Label(out_row, text="（该 Agent 无此字段）",
                      style="Muted.TLabel").pack(side="left")
        row += 1

        ttk.Label(body, text="是否支持图片").grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=(0, 8))
        self.var_vision = tk.BooleanVar(value=spec.vision)
        if "vision" not in self._hidden:
            CheckBox(body, text="接受图片输入（input: text + image）",
                     variable=self.var_vision).grid(row=row, column=1, sticky="w", pady=(0, 8))
        else:
            ttk.Label(body, text="（该 Agent 无此字段）", style="Muted.TLabel").grid(
                row=row, column=1, sticky="w", pady=(0, 8))
        row += 1

        ttk.Label(body, text="支持推理等级").grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=(0, 8))
        self.var_max = tk.StringVar(value=spec.reasoning_max)
        cmb_max = ttk.Combobox(body, textvariable=self.var_max, state="readonly", width=12,
                               values=list(ms.REASONING_LEVELS))
        if "reasoning" not in self._hidden:
            cmb_max.grid(row=row, column=1, sticky="w", pady=(0, 8))
            cmb_max.bind("<<ComboboxSelected>>", lambda _e: self._sync_levels())
        else:
            ttk.Label(body, text="（该 Agent 无此字段）", style="Muted.TLabel").grid(
                row=row, column=1, sticky="w", pady=(0, 8))
        row += 1

        ttk.Label(body, text="默认推理等级").grid(
            row=row, column=0, sticky="w", padx=(0, 10), pady=(0, 8))
        self.var_level = tk.StringVar(value=spec.reasoning_level)
        self.cmb_level = ttk.Combobox(body, textvariable=self.var_level, state="readonly",
                                      width=12, values=list(ms.supported_levels(self.var_max.get())))
        if "reasoning" not in self._hidden:
            self.cmb_level.grid(row=row, column=1, sticky="w", pady=(0, 8))
        row += 1

        self.lbl_hint = ttk.Label(body, text="", style="Muted.TLabel", wraplength=430)
        self.lbl_hint.grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self._sync_levels()
        if self._spec_note:
            # 隐藏了输入行时必须说明为什么——无声少几行会让人以为界面缺了功能
            self.lbl_hint.configure(text=self._spec_note)

        btns = ttk.Frame(self, padding=(16, 0, 16, 14))
        btns.pack(fill="x")
        ttk.Button(btns, text="确定", style="Accent.TButton", command=self._ok).pack(side="right")
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right", padx=(0, 8))

        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self._center(master)

    def _center(self, master: tk.Misc) -> None:
        self.update_idletasks()
        top = master.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - self.winfo_width()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _sync_levels(self) -> None:
        """「支持推理等级」决定默认档的可选范围；选 off 表示不支持推理。"""
        levels = list(ms.supported_levels(self.var_max.get()))
        self.cmb_level.configure(values=levels)
        if self.var_level.get() not in levels:
            # 默认档必须落在支持范围内：high 不支持时退到最高可用档
            self.var_level.set(ms.DEFAULT_REASONING if ms.DEFAULT_REASONING in levels else levels[-1])
        if self.var_max.get() == "off":
            self.cmb_level.state(["disabled"])
            self.lbl_hint.configure(text="「off」表示这个模型不支持推理，配置里不会写推理档位。")
        else:
            self.cmb_level.state(["!disabled"])
            self.lbl_hint.configure(
                text=f"支持档位：{levels[0]}–{levels[-1]}（共 {len(levels)} 档）；"
                     f"默认用「{self.var_level.get()}」。")

    def _ok(self) -> None:
        mid = self.var_id.get().strip()
        base = self.var_base.get().strip()
        if not mid:
            messagebox.showwarning(self.title(), "模型 ID 不能为空。", parent=self)
            return
        if not base:
            messagebox.showwarning(self.title(), "Base URL 不能为空。", parent=self)
            return
        ctx = dict(ms.CONTEXT_OPTIONS).get(self.var_ctx.get(), ms.DEFAULT_CONTEXT)
        raw_out = self.var_max_out.get().strip()
        max_out = ms.parse_capacity(raw_out)
        if raw_out and not max_out:
            # 认不出来的写法当 0 处理，等于悄悄「不写」——那是最难发现的错误
            messagebox.showwarning(self.title(),
                                   "最大输出需为数字，可加 K / M 后缀，例如 8192、8K、1M；"
                                   "留空表示不写。", parent=self)
            return
        # 被隐藏的字段沿用传入的原始值：它们的控件虽在（便于统一取变量），
        # 但用户看不见也改不了，绝不能把界面默认值当成用户的选择写下去。
        self.result = {
            "id": mid,
            "name": self.var_name.get().strip() or mid,
            "base_url": base,
            "api_key": self.var_key.get().strip(),
            "spec": ms.ModelSpec(
                context=self._spec.context if "context" in self._hidden else ctx,
                vision=self._spec.vision if "vision" in self._hidden
                else bool(self.var_vision.get()),
                reasoning_max=self._spec.reasoning_max if "reasoning" in self._hidden
                else self.var_max.get(),
                reasoning_level=self._spec.reasoning_level if "reasoning" in self._hidden
                else self.var_level.get(),
                max_tokens=self._spec.max_tokens if "max_out" in self._hidden else max_out,
            ),
        }
        self.destroy()


class SnapshotDialog(tk.Toplevel):
    """挑一份备份快照来还原。

    环形备份保留最近若干份，但界面不该默认替用户决定退到哪一份：连写错两次时，
    「最新那份」恰恰是错的。只有一份快照时不需要它（直接确认即可）。
    """

    def __init__(self, master: tk.Misc, *, agent_label: str,
                 choices: list[tuple[Path, str]]) -> None:
        super().__init__(master)
        self.title(f"还原 {agent_label}")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master.winfo_toplevel())
        self.result: Path | None = None
        self._choices = choices

        body = ttk.Frame(self, padding=(16, 14, 16, 10))
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="选一份快照覆盖当前配置（越靠前越新）：",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 8))

        self.listbox = tk.Listbox(
            body, height=min(5, len(choices)), width=44,
            bg=FIELD, fg=FG, selectbackground=ACCENT, selectforeground="#ffffff",
            highlightthickness=0, borderwidth=0, activestyle="none",
            font=("Microsoft YaHei UI", 9),
        )
        for _, label in choices:
            self.listbox.insert("end", label)
        self.listbox.selection_set(0)
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<Double-Button-1>", lambda _e: self._ok())

        btns = ttk.Frame(self, padding=(16, 0, 16, 14))
        btns.pack(fill="x")
        ttk.Button(btns, text="还原这份", style="Accent.TButton",
                   command=self._ok).pack(side="right")
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right", padx=(0, 8))

        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self._center(master)

    def _center(self, master: tk.Misc) -> None:
        self.update_idletasks()
        top = master.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - self.winfo_width()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - self.winfo_height()) // 3
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _ok(self) -> None:
        picked = self.listbox.curselection()
        if not picked:
            return
        self.result = self._choices[picked[0]][0]
        self.destroy()


class PriceBookDialog(tk.Toplevel):
    """参考价格表：出厂自带一张全量价目表，用户改过的条目覆盖出厂价。

    为什么是「参考价」而不是「你的账单」：端点不返回价格，中转站又普遍按倍率计价，
    所以这里给的是**量级参考** —— 用来判断「这个模型贵不贵」，不追求分毫准确。
    改过的模型存进 `~/.model_probe/prices.json`；没改过的永远跟着出厂价走，
    升级程序时会自动带上新的出厂价。
    """

    def __init__(self, master: tk.Misc, *, data: dict) -> None:
        super().__init__(master)
        self.title("参考价格表")
        self.configure(bg=BG)
        self.resizable(True, True)
        self.transient(master.winfo_toplevel())
        self._data = data
        self._rows: list[tuple[str, float | None, str]] = []
        # 「已保存的样子」快照 (模型ID, 输入价)。保存按钮只在当前输入偏离它时才亮，
        # 避免点了才发现「什么都没改」。
        self._baseline: tuple[str, float | None] = ("", None)

        body = ttk.Frame(self, padding=(16, 14, 16, 10))
        body.pack(fill="both", expand=True)

        preset = pb.load_preset()
        if preset:
            head = (f"出厂自带 {len(preset.get('catalog') or {})} 个模型的参考价"
                    f"（按 1 USD = {pb.fmt(preset.get('fx'))} 折算，抓取于 "
                    f"{preset.get('fetched') or '未知'}）；你的修改优先，未改的跟随出厂价。")
        else:
            head = "未找到出厂价格文件（prices_preset.json），这里只有你手动填的条目。"
        ttk.Label(
            body,
            text=f"单位 {pb.UNIT_LABEL}，且只记「输入价」：判断模型贵不贵看它就够，"
                 f"输出价各家倍率口径不一，记了也没法横向比。\n{head}",
            style="Muted.TLabel", justify="left",
        ).pack(anchor="w", pady=(0, 8))

        search_row = ttk.Frame(body)
        search_row.pack(fill="x", pady=(0, 6))
        ttk.Label(search_row, text="搜索", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.var_search = tk.StringVar()
        ent = ttk.Entry(search_row, textvariable=self.var_search, width=34)
        ent.pack(side="left")
        self.var_search.trace_add("write", lambda *_: self._reload())
        self.var_count = tk.StringVar(value="")
        ttk.Label(search_row, textvariable=self.var_count,
                  style="Muted.TLabel").pack(side="right")

        table = ttk.Frame(body)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("id", "price"),
                                 show="headings", height=14, selectmode="browse")
        for col, (text, width, anchor) in {
            "id": ("模型 ID / 通配", 380, "w"),
            "price": ("输入价（元 / 百万 token）", 200, "e"),
        }.items():
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "id"))
        self.tree.pack(side="left", fill="both", expand=True)
        vsb = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        vsb.pack(side="left", fill="y")
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_select)

        form = ttk.Frame(body)
        form.pack(fill="x", pady=(10, 0))
        ttk.Label(form, text="模型 ID").pack(side="left", padx=(0, 4))
        self.var_id = tk.StringVar()
        ttk.Entry(form, textvariable=self.var_id, width=30).pack(side="left", padx=(0, 8))
        ttk.Label(form, text="输入价").pack(side="left", padx=(0, 4))
        self.var_in = tk.StringVar()
        ttk.Entry(form, textvariable=self.var_in, width=8).pack(side="left", padx=(0, 4))
        ttk.Label(form, text="元 / 百万 token", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        # 保存默认置灰：没有改动就没有可保存的东西，先灰着比点完弹「没变化」干净。
        self.btn_save = ttk.Button(form, text="保存", style="Accent.TButton",
                                   command=self._save, state="disabled")
        self.btn_save.pack(side="left", padx=(0, 8))
        ttk.Button(form, text="恢复预设价", command=self._restore).pack(side="left")
        # 三个输入框任一变化都重算按钮状态；trace 在控件建好之后挂，
        # 避免初始化时回调撞上还没创建的 btn_save。
        for var in (self.var_id, self.var_in):
            var.trace_add("write", lambda *_: self._sync_save_state())

        foot = ttk.Frame(self, padding=(16, 0, 16, 14))
        foot.pack(fill="x")
        self.var_note = tk.StringVar(value=f"我的价格文件：{pb.price_path()}")
        ttk.Label(foot, textvariable=self.var_note, style="Muted.TLabel").pack(side="left")
        ttk.Button(foot, text="关闭", command=self.destroy).pack(side="right")

        self.bind("<Escape>", lambda _e: self.destroy())
        self._reload()
        self._center(master)

    def _center(self, master: tk.Misc) -> None:
        self.update_idletasks()
        top = master.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - self.winfo_width()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - self.winfo_height()) // 4
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _reload(self) -> None:
        """按搜索词重画整张表。全量三千多条一次性插入没问题（Treeview 很快），
        搜索词只做子串过滤，够用且不需要索引。"""
        self._rows = pb.catalog_rows(self._data)
        needle = self.var_search.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        shown = 0
        for mid, price, _kind in self._rows:
            if needle and needle not in mid.lower():
                continue
            shown += 1
            self.tree.insert("", "end", iid=f"{shown}:{mid}",
                             values=(mid, pb.fmt(price)))
        mine = sum(1 for r in self._rows if r[2].startswith("我的"))
        self.var_count.set(f"共 {len(self._rows)} 条 · 已改 {mine} 条"
                           + (f" · 匹配 {shown}" if needle else ""))

    def _on_select(self, _event: tk.Event) -> None:
        picked = self.tree.selection()
        if not picked:
            return
        row = self.tree.item(picked[0])["values"]
        # 先立基准再填值：trace 会在填值过程中触发，基准若晚一步，
        # 保存按钮就会因为「刚选中」而误亮。
        self._baseline = (str(row[0]),
                          self._sig_part("" if row[1] == "—" else str(row[1])))
        self.var_id.set(str(row[0]))
        self.var_in.set("" if row[1] == "—" else str(row[1]))
        self._sync_save_state()

    @staticmethod
    def _sig_part(text: str) -> float | str | None:
        """把输入框内容归一成可比较的值：`3` 与 `3.0` 视为同一个价。"""
        raw = text.strip()
        if not raw:
            return None
        try:
            return float(raw)
        except ValueError:
            return raw          # 还没打完 / 非法输入，按原文比较，保存时再报错

    def _sync_save_state(self) -> None:
        """按「当前输入是否偏离已保存的样子」开关保存按钮。

        置灰而不是隐藏：按钮位置不变，用户不会因为按钮突然出现而误点。
        """
        current = (self.var_id.get().strip(), self._sig_part(self.var_in.get()))
        dirty = current != self._baseline
        self.btn_save.state(["!disabled"] if dirty else ["disabled"])

    def _parse(self, text: str, label: str) -> tuple[float | None, bool]:
        raw = text.strip()
        if not raw:
            return None, True
        try:
            value = float(raw)
        except ValueError:
            messagebox.showwarning(self.title(), f"{label}需要填数字（例如 3、0.15），留空表示未知。",
                                   parent=self)
            return None, False
        if value < 0:
            messagebox.showwarning(self.title(), f"{label}不能是负数。", parent=self)
            return None, False
        return value, True

    def _save(self) -> None:
        mid = self.var_id.get().strip()
        if not mid:
            messagebox.showwarning(self.title(), "模型 ID 不能为空；想批量匹配可以写 claude-*。",
                                   parent=self)
            return
        in_price, ok_in = self._parse(self.var_in.get(), "输入价")
        if not ok_in:
            return
        if in_price is None:
            messagebox.showwarning(self.title(),
                                   "输入价是这张表唯一的价格；填 0 表示免费，留空不保存。",
                                   parent=self)
            return
        pb.upsert(mid, in_price, self._data)
        pb.save(self._data)
        self._reload()
        # 存完就以刚存下的数为新基准，保存按钮随之回到置灰
        self._baseline = (mid, in_price)
        self._sync_save_state()
        self.var_note.set(f"已改成你的价：{mid} · {pb.price_path()}")

    def _restore(self) -> None:
        """删掉自己的覆盖，这一条就回到预设价。"""
        mid = self.var_id.get().strip()
        if not mid:
            return
        has_override = (mid in (self._data.get("models") or {})
                        or mid in (self._data.get("patterns") or {}))
        if not has_override:
            self.var_note.set(f"{mid} 没有改过，本来就是预设价。")
            return
        pb.remove(mid, self._data)
        pb.save(self._data)
        self._reload()
        # 恢复后输入框跟着回到预设值，否则按钮会因为「界面还停在旧价」而一直亮着
        self._adopt_row(mid)
        self.var_note.set(f"已恢复预设价：{mid}")

    def _adopt_row(self, mid: str) -> None:
        """把某一行的当前价（预设或别人的）填进输入框，并立为保存基准。"""
        for row_mid, price, _kind in self._rows:
            if row_mid != mid:
                continue
            self._baseline = (mid, price)
            self.var_in.set("" if price is None else pb.fmt(price))
            self._sync_save_state()
            return


SPEED_HINT = (
    "并行测速：每个模型按「轮数 × 输出上限」产生真实 token 消耗。\n"
    "首字超过 10 秒的模型立即放弃，标记为「超时10s以上」并显示「-」。\n"
    "生成速度 = 各轮正文 token ÷ 解码窗口之和（已扣除隐藏推理 token、"
    "补上中转站冲刷周期）；样本太短时数字前会标「≈」。\n"
    "测速前会清空上一轮的记录。"
)

def format_seconds(ms: int | float | None) -> str:
    """毫秒 → `0.87 s`。界面上一律用秒，毫秒只留给内部计算与平台原始数据。

    两处必须走同一个函数：探测页自己算首字、管理页由测速/路由回填，
    各写各的格式就会出现 `0.87 s` 与 `870 ms` 并列的同一件事。
    """
    if ms is None:
        return ""
    return f"{ms / 1000:.2f} s"


# 可排序列的表头基名。排序状态用箭头后缀表达：
# 未激活 ↕（可点）、升序 ↑、降序 ↓。
# 注意：`_refresh_sort_indicators` 会用这里的文本重设表头，所以带单位的长名字要写在这
# 表头只写「预估价格」，单位放在悬停说明里：一列数字后面跟着「¥ / 1M tokens」
# 会把列宽吃掉一半，而且每个模型的币种单位其实都一样，写在表头是重复信息。
PRICE_HEADING = "预估价格"
PRICE_TIP = "预估输入价格 / 百万 token（元）"
LATENCY_HEADING = "延迟"

SORT_HEADERS: dict[str, str] = {
    "id": "模型 ID",
    "price": PRICE_HEADING,
    "ttft": "首字延迟",
    "tps": "生成速度",
    "status": "状态",
}
SORT_MARK_IDLE = "↕"
SORT_MARK_ASC = "↑"
SORT_MARK_DESC = "↓"


class SettingsDialog(tk.Toplevel):
    """全局设置：探针行为 + 站点凭据 + 智能路由打分参数。

    三段都放这里，是因为它们同属「全局策略」：探针的两个开关决定所有请求怎么发，
    凭据决定能不能查商家，权重与门槛决定选谁。分散在各自页面里，用户会以为是
    页级选项——同一个开关在两页看到两次，改哪一份生效说不清。
    """

    FIELDS = (
        ("price_weight", "价格权重", "int",
         "范围 1–100。填得越大，实际成本越低的商家得分越高。\n"
         "这里比的是**实际成本**，不是标价：命中缓存的部分只按输入价的 2% 计费，\n"
         "所以缓存率 91% 的商家虽然标价更高，真实花费可能只要低缓存那家的 1/5。\n"
         "三项权重会先求和，再各自折算成占比参与打分，只有相对大小有意义。"),
        ("latency_weight", "延迟权重", "int",
         "范围 1–100。填得越大，延迟越低的商家得分越高。\n"
         "延迟取平台统计的中位值（p50），三项权重按总和折算成占比。"),
        ("success_weight", "成功率权重", "int",
         "范围 1–100。填得越大，历史成功率低的商家被压得越靠后。\n"
         "它作用在「失败率」上，与上面的成功率门槛是两回事：门槛是淘汰，权重是排序。"),
        ("max_ratio", "倍率上限", "float",
         "商户输入价 ÷ 官方输入价 的上限。\n超过这条线的商家直接淘汰，不参与排序。"),
        ("max_p50_ms", "延迟参考(秒)", "seconds",
         "打分时用来衡量的延迟刻度，单位秒（可填小数）。\n"
         "延迟分 = 站点 p50 ÷ 这个值（超过它最多记 3 倍），\n"
         "所以它只影响排序，不再一票否决。\n"
         "旧版拿它当硬门槛，把「站点 5.2 秒、我自己实测 2.0 秒、0.06x、0 失败」\n"
         "的商家直接扔了，只剩下一批贵 6 倍的候选。"),
        ("hard_p50_ms", "延迟硬上限(秒)", "seconds",
         "真正不可用的上限，超过就淘汰，默认 20 秒。\n"
         "一分钟还出不了结果的商家没有使用价值，但「比参考值慢一点」不该被删。"),
        ("min_samples", "样本门槛", "int",
         "冷启动商家（我还没用过）至少要有这么多次站点测试样本。\n"
         "样本为 0 时平台给出的延迟也是 0，会被误判成「最快」。\n"
         "用过的商家以我自己的调用记录为准，不受这条限制。"),
        ("min_success", "成功率门槛", "float",
         "站点统计成功率的下限，填 95 或 0.95 都表示 95%。\n"
         "只对**我从没用过**的商家生效：站点统计与实际差得很远\n"
         "（Ch 6490 站点报 97%，我 148 次调用里失败了 29 次），\n"
         "用过的商家一律以实测失败率为准。"),
        ("max_fail_rate", "实测失败率上限(%)", "float",
         "我自己调用记录里的失败率上限，超过就淘汰，默认 15%。\n"
         "这一条专门挡住「站点说 97%、实际五次里失败一次」的商家。\n"
         "调小更稳（0 表示只接受零失败），调大则允许更多候选进来。"),
        ("candidates", "实测家数", "int",
         "排序后真正去调用测试的商家数量。\n"
         "越多越稳，但每家都要花一次调用、多等几秒。"),
        ("rounds", "测试轮数", "int",
         "每个商家内部测几次（取最快一轮）。判断「能不能出字」1 次就够，加轮数只会更慢。"),
        ("attempts", "采样次数", "int",
         "同一个商家要连续通过几次采样才算可用，默认 2。\n"
         "两档都走**流式**（与实际调用一致）：① 流式·长输出 ② 流式·短输出。\n"
         "· ① 专撞「说到一半被上游掐断」——真实踩过的坑，真实调用会报 502，\n"
         "  所以放在第一个，坏家第一秒就现形；\n"
         "· ② 流式短输出要求正文复述 PONG，专撞「HTTP 200 但正文是错误提示\n"
         "  / 空正文」。\n"
         "不再测非流式：实际调用全走流式，测一条不会被走到的路径没有意义。\n"
         "加到 3 次会重复这两档；误判率（对五成失败率的商家）：2 次 25%。\n"
         "采样全过之后还会做一次**真实形态终检**（带 system、带工具定义、\n"
         "约 4 千 token 上下文），并**复核站点调用日志**——固定之后真实调用\n"
         "若仍失败就换下一家（这正是对付「测试通过、实际用不了」的一层）。"),
        ("watch_interval_s", "自动体检(秒)", "int",
         "固定之后每隔这么久做一次体检：先看站点调用日志里这次固定之后有没有\n"
         "真实失败（2 次就立刻换家，不用等探测），再看要不要自己发一次小请求。\n"
         "连续 2 次不通才换家，避免一次网络抖动就误换。填 0 关闭。\n"
         "为什么需要它：选商只是**那一瞬间**的判断，而商家健康是时变的——\n"
         "实测同一家 3 分钟内 6 次失败、4 次成功。默认 300 秒。"),
        ("max_tokens", "输出上限", "int",
         "测试时允许模型生成的最大 token 数，默认 64。\n"
         "推理模型会先把 token 花在思考上：给 16 时正文为空，内容校验必然失败，\n"
         "所以低于 64 会自动按 64 执行。"),
    )

    def __init__(self, master: tk.Misc, settings=None, api_base: str = "",
                 *, proxy: bool = False, auto_v1: bool = True) -> None:
        super().__init__(master)
        self.title("设置")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master.winfo_toplevel())
        self._settings = settings or sr.RouteSettings()
        self._api_base = api_base
        self._vars: dict[str, tk.StringVar] = {}
        # 保存成功后才回填给主窗口；点「关闭」等于放弃本次改动
        self.result: dict | None = None
        self.var_proxy = tk.BooleanVar(value=bool(proxy))
        self.var_autov1 = tk.BooleanVar(value=bool(auto_v1))

        cred = sr.site_credentials(api_base) or {}
        self.var_control = tk.StringVar(
            value=str(cred.get("control_base") or sr.guess_control_base(api_base) or ""))
        self.var_uid = tk.StringVar(value=str(cred.get("user_id") or ""))
        self.var_token = tk.StringVar(value=str(cred.get("access_token") or ""))

        body = ttk.Frame(self, padding=(18, 16, 18, 10))
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="探测行为", style="Section.TLabel").pack(anchor="w")
        probe_box = ttk.Frame(body)
        probe_box.pack(fill="x", pady=(4, 0))
        CheckBox(probe_box, text="走系统代理（直连不通时勾选）",
                 variable=self.var_proxy).pack(anchor="w", pady=2)
        check_auto = CheckBox(probe_box, text="路径 404 时自动再试 /v1",
                              variable=self.var_autov1)
        check_auto.pack(anchor="w", pady=2)
        Tooltip(check_auto, "部分中转站的模型列表挂在 /v1 下，裸域直接访问会 404；\n"
                            "勾选后会自动补一段 /v1 再试一次。")
        ttk.Label(body, text="这两项对所有请求生效（含智能路由的商家查询与实测）。",
                  style="Muted.TLabel", justify="left").pack(anchor="w", pady=(4, 0))
        self.var_merchant_log = tk.BooleanVar(value=bool(self._settings.check_merchant_log))
        check_log = CheckBox(probe_box, text="按我的调用记录选商（推荐）",
                             variable=self.var_merchant_log)
        check_log.pack(anchor="w", pady=(6, 2))
        Tooltip(check_log,
                "读我自己的调用日志，算出每家商家的**真实**失败率与真实延迟，\n"
                "再用它们选商：连续失败过的直接排除，站点说好用但实测常挂的也排除。\n"
                "关掉就只剩站点统计（那是商家自己上报的抽样，实测偏差极大）。\n"
                "读不到日志时会自动退化成只靠站点统计 + 现场采样。")

        ttk.Separator(body, orient="horizontal").pack(fill="x", pady=12)

        ttk.Label(body, text="站点凭据", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            body,
            text="保存一次即可，之后智能路由不再询问。访问令牌在站点「个人设置 → 系统访问令牌」生成；\n"
                 "它不是 sk- 开头的 API Key —— 前者查商家与固定，后者实际调用，两者都需要。",
            style="Muted.TLabel", justify="left",
        ).pack(anchor="w", pady=(2, 8))

        cred_grid = ttk.Frame(body)
        cred_grid.pack(fill="x")
        self.ent_token: ttk.Entry | None = None
        for row, (label, var, width) in enumerate((
            ("控制面地址", self.var_control, 32),
            ("用户 ID", self.var_uid, 12),
            ("访问令牌", self.var_token, 44),
        )):
            ttk.Label(cred_grid, text=label).grid(row=row, column=0, sticky="w", pady=4, padx=(0, 10))
            entry = ttk.Entry(cred_grid, textvariable=var, width=width,
                              show="•" if label == "访问令牌" else "")
            entry.grid(row=row, column=1, sticky="w")
            if label == "访问令牌":
                self.ent_token = entry
        # 令牌默认打码；勾「显示」才明文，满足「能核对」又不把密钥一直摊在屏幕上
        self.var_reveal = tk.BooleanVar(value=False)
        CheckBox(cred_grid, text="显示", variable=self.var_reveal).grid(
            row=2, column=2, sticky="w", padx=(10, 0))
        self.var_reveal.trace_add("write", lambda *_: self._toggle_token())

        ttk.Separator(body, orient="horizontal").pack(fill="x", pady=12)

        ttk.Label(body, text="打分参数", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            body,
            text="权重填 1–100 的整数，是「相对份数」而不是百分比：工具先求和，再各自折算成占比。\n"
                 "例：100/100/100 → 各占 1/3；50/35/15 → 50% / 35% / 15%。填完下面会实时显示占比。\n"
                 "鼠标悬停在参数名上可看说明。",
            style="Muted.TLabel", justify="left", wraplength=520,
        ).pack(anchor="w", pady=(2, 8))

        # 预设：三档常用取舍。只改三项权重，不动任何门槛——点预设想换的是
        # 「偏好」，不是「放宽条件」，门槛跟着变会让候选悄悄消失。
        preset_bar = ttk.Frame(body)
        preset_bar.pack(fill="x", pady=(0, 10))
        ttk.Label(preset_bar, text="预设", style="Muted.TLabel").pack(side="left", padx=(0, 10))
        self._preset_buttons: list[ttk.Button] = []
        for preset in sr.WEIGHT_PRESETS:
            btn = ttk.Button(preset_bar, text=preset.name,
                             command=lambda p=preset: self._apply_preset(p))
            btn.pack(side="left", padx=(0, 6))
            Tooltip(btn, preset.note + "\n\n只改三项权重，不动下面的门槛参数。")
            self._preset_buttons.append(btn)
        self.var_preset = tk.StringVar(value="")
        ttk.Label(preset_bar, textvariable=self.var_preset,
                  style="Muted.TLabel").pack(side="left", padx=(10, 0))

        grid = ttk.Frame(body)
        grid.pack(fill="x")
        digits = (self.register(self._digits_only), "%P")
        for row, (name, label, kind, tip) in enumerate(self.FIELDS):
            var = tk.StringVar(value=self._show_value(name, kind))
            self._vars[name] = var
            lab = ttk.Label(grid, text=label)
            lab.grid(row=row, column=0, sticky="w", pady=4, padx=(0, 12))
            # 整数字段直接挡住非数字键：小数、负号、空格根本敲不进去，
            # 比留到「保存」再报错少一轮来回。
            opts: dict[str, object] = {}
            if kind == "int":
                opts = {"validate": "key", "validatecommand": digits}
            ent = ttk.Entry(grid, textvariable=var, width=10, **opts)
            ent.grid(row=row, column=1, sticky="w")
            # 说明不摆在面板上：十行文字会把窗口撑成两页宽，读起来也更累。
            # 悬停在参数名或输入框上都弹同一条，找说明时不用瞄准。
            for widget in (lab, ent):
                Tooltip(widget, tip)

        # 填的是「份数」，真正参与打分的是份数 ÷ 总和。不把这层换算摆出来，
        # 用户没法判断 45/35/20 到底意味着什么，也没法验证自己填对了。
        self.var_weight_hint = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.var_weight_hint, style="Muted.TLabel",
                  wraplength=620, justify="left").pack(anchor="w", pady=(8, 0))
        for name in sr.WEIGHT_FIELDS:
            self._vars[name].trace_add("write", lambda *_: self._refresh_weight_hint())
        self._refresh_weight_hint()

        self.var_note = tk.StringVar(value="")
        ttk.Label(body, textvariable=self.var_note, style="Muted.TLabel",
                  wraplength=620, justify="left").pack(anchor="w", pady=(10, 0))

        foot = ttk.Frame(self, padding=(18, 0, 18, 14))
        foot.pack(fill="x")
        ttk.Label(foot, text=f"配置：{sr.route_path()}", style="Muted.TLabel").pack(side="left")
        ttk.Button(foot, text="关闭", command=self.destroy).pack(side="right")
        ttk.Button(foot, text="恢复默认", command=self._reset).pack(side="right", padx=(0, 8))
        ttk.Button(foot, text="保存", style="Accent.TButton",
                   command=self._save).pack(side="right", padx=(0, 8))

        self.bind("<Escape>", lambda _e: self.destroy())
        self._center(master)

    @staticmethod
    def _digits_only(proposed: str) -> bool:
        """只放行空串与纯数字，供 ttk 的 key 级校验使用。"""
        return proposed == "" or proposed.isdigit()

    @staticmethod
    def _shares(values: list[int]) -> list[int]:
        """按最大余数法把份数折成整数百分比，保证加起来正好 100。

        直接各自 round 会出现 34/33/33 之外的 33/33/33=99，用户会以为算错了。
        """
        total = sum(values)
        if total <= 0:
            return [0 for _ in values]
        exact = [v * 100.0 / total for v in values]
        base = [int(x) for x in exact]
        rest = 100 - sum(base)
        order = sorted(range(len(exact)), key=lambda i: exact[i] - base[i], reverse=True)
        for idx in order[:max(rest, 0)]:
            base[idx] += 1
        return base

    def _apply_preset(self, preset: "sr.WeightPreset") -> None:
        """把一档预设的权重填进输入框。

        只写三格权重，门槛一律不碰；不直接落盘，仍要用户点「保存」——
        预设是「填好一组数」，不是「立刻改全局策略」，中途反悔关掉窗口即可。
        """
        for name, value in zip(sr.WEIGHT_FIELDS, preset.weights):
            self._vars[name].set(str(value))
        self.var_note.set(f"已套用「{preset.name}」的权重，点「保存」后生效（门槛参数未变）。")

    def _refresh_preset_label(self) -> None:
        """显示「当前：平衡（默认）」或「当前：自定义」。

        为什么要这行字：三项权重拆开看只是三个数字，用户改过几次之后分不清
        自己现在站在哪一档；和预设对上就直接说出来，对不上也明确讲是自定义，
        而不是让人猜「我这算偏便宜还是偏快」。
        """
        try:
            got = tuple(int(self._vars[name].get().strip()) for name in sr.WEIGHT_FIELDS)
        except (KeyError, ValueError):
            self.var_preset.set("")          # 有空格子：先不下判断，等填完再说
            return
        for preset in sr.WEIGHT_PRESETS:
            if preset.weights == got:
                self.var_preset.set(f"当前：{preset.name}")
                return
        self.var_preset.set("当前：自定义")

    def _refresh_weight_hint(self) -> None:
        """把三项权重实时折算成占比与实际打分式。

        权重是相对份数：价格 100 / 延迟 100 / 成功率 100 就是各占三分之一，
        50 / 35 / 15 就是 50% / 35% / 15%。这里显示的就是 rank() 真正用的数，
        免得用户以为填的是「百分比」。第三项作用在失败率上（1 − 成功率）。
        """
        values: list[tuple[str, int]] = []
        for name, label, _kind, _tip in self.FIELDS[:3]:
            try:
                raw = int(self._vars[name].get().strip())
            except (KeyError, ValueError):
                raw = 0
            values.append((label.replace("权重", ""), max(raw, 0)))
        total = sum(v for _label, v in values)
        if total <= 0:
            self.var_weight_hint.set(
                "实际占比：三项都填 1–100 的整数后显示（工具按 份数 ÷ 总和 折算）。")
            self._refresh_preset_label()
            return
        shares = self._shares([v for _label, v in values])
        pairs = " · ".join(f"{label} {pct}%" for (label, _v), pct in zip(values, shares))
        terms = " + ".join(f"{pct / 100:.2f}×{unit}"
                           for pct, unit in zip(shares, ("价格分", "延迟分", "失败率")))
        self.var_weight_hint.set(
            f"实际占比：{pairs}（合计 100%）\n"
            f"打分：得分 = {terms}　数值越小越优先")
        self._refresh_preset_label()

    def _toggle_token(self) -> None:
        if self.ent_token is not None:
            self.ent_token.configure(show="" if self.var_reveal.get() else "•")

    def _center(self, master: tk.Misc) -> None:
        self.update_idletasks()
        top = master.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - self.winfo_width()) // 2
        y = top.winfo_rooty() + (top.winfo_height() - self.winfo_height()) // 4
        self.geometry(f"+{max(x, 0)}+{max(y, 0)}")

    def _show_value(self, name: str, kind: str, source=None) -> str:
        """把内部值渲染成输入框里的文本。

        只有「秒」类字段需要换算：内部统一用毫秒（平台给的就是毫秒，
        换算只发生在界面这一层），显示时除以 1000。
        """
        value = getattr(source or self._settings, name)
        if kind == "seconds":
            return f"{float(value) / 1000:g}"
        return str(value) if isinstance(value, int) else f"{value:g}"

    def _reset(self) -> None:
        default = sr.RouteSettings()
        for name, _label, kind, _tip in self.FIELDS:
            self._vars[name].set(self._show_value(name, kind, default))
        self.var_merchant_log.set(bool(default.check_merchant_log))
        self.var_note.set("算法参数已恢复默认值，点「保存」后生效（凭据不受影响）。")

    def _save(self) -> None:
        # ---- 凭据先校验：三项要公同进退，半套凭据存下来只会让下次更迷惑 ----
        control = self.var_control.get().strip().rstrip("/")
        uid = self.var_uid.get().strip()
        token = self.var_token.get().strip()
        filled = [bool(control), bool(uid), bool(token)]
        if any(filled) and not all(filled):
            self.var_note.set("凭据三项（控制面地址 / 用户 ID / 访问令牌）要么都填，要么都留空。")
            return
        if all(filled):
            if not control.startswith("http"):
                self.var_note.set("控制面地址要以 http(s):// 开头。")
                return
            host = (urlsplit(control).hostname or "")
            if "." not in host or not host.isascii():
                self.var_note.set(f"主机名不可用：{host!r}（应为 a6api.vip 这类真实域名）。")
                return
            if not uid.isdigit():
                self.var_note.set("用户 ID 应该是纯数字（站点个人设置里能看到）。")
                return

        bounds = {name: (lo, hi) for name, _kind, (lo, hi) in sr.SETTING_FIELDS}
        pending: dict[str, object] = {}
        for name, label, kind, _tip in self.FIELDS:
            text = self._vars[name].get().strip()
            try:
                value = int(text) if kind == "int" else float(text)
            except ValueError:
                self.var_note.set(f"「{label}」要填数字，当前是 {text!r}。")
                return
            lo, hi = bounds.get(name, (0.0, 0.0))
            scale = 1000.0 if kind == "seconds" else 1.0
            if not lo <= float(value) * scale <= hi:
                self.var_note.set(
                    f"「{label}」要填 {lo / scale:g}–{hi / scale:g} 之间的数，当前是 {value:g}。")
                return
            pending[name] = int(round(value * scale)) if kind == "seconds" else value

        for name, value in pending.items():
            setattr(self._settings, name, value)
        self._settings.check_merchant_log = bool(self.var_merchant_log.get())
        try:
            sr.save_settings(self._settings)
            if all(filled):
                sr.set_site_credentials(control, token, uid)
        except OSError as e:
            self.var_note.set(f"保存失败：{e}")
            return
        self.result = {"proxy": self.var_proxy.get(), "auto_v1": self.var_autov1.get()}
        # 保存时把折算后的占比回报一次：这是唯一能确认「我填的份数被正确理解」的时机
        weights = [int(pending[name]) for name in sr.WEIGHT_FIELDS]
        shares = self._shares(weights)
        note = "已保存。权重占比：价格 {}% / 延迟 {}% / 成功率 {}%。".format(*shares)
        self.var_note.set(note + ("凭据也已写入，之后不再询问。" if all(filled) else ""))
        self.after(700, self.destroy)


class Tooltip:
    """极简 tooltip：鼠标在控件上停一会儿才弹，移开就消失。

    Tk 没有原生 tooltip。这里用一个 overrideredirect 的 Toplevel 顶掉标题栏，
    贴在控件正下方——放上方会盖住按钮本身，反而看不到要点的是什么。
    """

    def __init__(self, widget: tk.Misc, text: str, delay: int = 450) -> None:
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after_id: str | None = None
        self._win: tk.Toplevel | None = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _schedule(self, _e=None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.delay, self._show)

    def _cancel(self) -> None:
        if self._after_id is not None:
            try:
                self.widget.after_cancel(self._after_id)
            except Exception:  # noqa: BLE001 - 控件已销毁
                pass
            self._after_id = None

    def _show(self) -> None:
        if self._win is not None or not self.text:
            return
        x = self.widget.winfo_rootx()
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
        win = tk.Toplevel(self.widget)
        win.wm_overrideredirect(True)
        win.wm_geometry(f"+{x}+{y}")
        tk.Label(
            win, text=self.text, bg="#3a3d42", fg=FG, justify="left",
            font=("Microsoft YaHei UI", 9), padx=10, pady=6,
        ).pack()
        self._win = win

    def _hide(self, _e=None) -> None:
        self._cancel()
        if self._win is not None:
            self._win.destroy()
            self._win = None


def _row_iid(kind: str, row) -> str:
    """树行 iid。已由 `agents.row_iid` 统一接管，此处保留为兼容包装。"""
    return agents.row_iid(agents.get(kind), row)


class HeaderTooltip:
    """Treeview 表头说明：鼠标停在哪一格表头上，就弹那一格的解释。

    Tk 的表头不是控件，没有 `<Enter>` 可绑，只能按坐标反查落在哪一列——
    `identify_region` 判断是不是表头，`identify_column` 给出 `#3` 这种序号。
    这样做的好处是列宽被用户拖动后依然认得准。
    """

    def __init__(self, tree: ttk.Treeview, tips: dict[str, str], delay: int = 400) -> None:
        self.tree = tree
        self.tips = tips
        self.delay = delay
        self._after: str | None = None
        self._win: tk.Toplevel | None = None
        self._col = ""
        tree.bind("<Motion>", self._motion, add="+")
        tree.bind("<Leave>", lambda _e: self._hide(), add="+")
        tree.bind("<ButtonPress>", lambda _e: self._hide(), add="+")

    def _column_at(self, x: int, y: int) -> str:
        if self.tree.identify_region(x, y) != "heading":
            return ""
        try:
            index = int(self.tree.identify_column(x).lstrip("#")) - 1
            return str(self.tree["columns"][index])
        except (ValueError, IndexError, TypeError):
            return ""

    def _motion(self, event: tk.Event) -> None:
        name = self._column_at(event.x, event.y)
        if not self.tips.get(name):
            self._hide()
            return
        # 同一格上一直晃鼠标不重复弹：已经在显示或已经排队，就别重来一遍
        if name == self._col and (self._win is not None or self._after is not None):
            return
        self._hide()
        self._col = name
        x, y = event.x_root, event.y_root
        self._after = self.tree.after(self.delay, lambda: self._show(name, x, y))

    def _show(self, name: str, x_root: int, y_root: int) -> None:
        self._after = None
        text = self.tips.get(name, "")
        if not text:
            return
        win = tk.Toplevel(self.tree)
        win.wm_overrideredirect(True)
        win.wm_geometry(f"+{x_root + 12}+{y_root + 18}")
        tk.Label(win, text=text, bg="#3a3d42", fg=FG, justify="left",
                 font=("Microsoft YaHei UI", 9), padx=10, pady=6).pack()
        self._win = win

    def _hide(self) -> None:
        if self._after is not None:
            try:
                self.tree.after_cancel(self._after)
            except Exception:  # noqa: BLE001 - 控件已销毁
                pass
            self._after = None
        self._col = ""
        if self._win is not None:
            self._win.destroy()
            self._win = None


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(mp.WINDOW_TITLE)
        self.geometry("1240x800")
        self.minsize(1060, 660)
        apply_theme(self)
        apply_window_icon(self)

        self._queue: queue.Queue = queue.Queue()
        self._busy = False
        self._rows: list[mp.Model] = []      # 全量探测结果
        self._view: list[mp.Model] = []      # 过滤后可见行
        # 测速结果只活在本次运行内：关掉程序就该忘掉，没有库可写。
        self._speed: dict[str, mp.SpeedResult] = {}
        # 每个模型当前处在 SPEED_STATES 的哪一档；没有条目 = 从没测过。
        self._speed_state: dict[str, str] = {}
        # 价格表按需载入：一次几十行，每行读一遍 prices.json 太蠢；编辑后整体重载。
        self._price_data: dict | None = None
        # 勾选框状态（存 norm_id）。这是「业务选择」——测速与导入都只看这里，
        # 不看 Treeview 的高亮行：高亮用来「读」，勾选用来「选一批」。
        self._checked: set[str] = set()
        self._agents: dict[str, list[ai.AgentModel]] = {k: [] for k in agents.kinds()}
        # 每个 Agent 是否真的装在这台机器上；没装就只用「看」，不给写。
        self._agent_ok: dict[str, bool] = {k: False for k in agents.kinds()}
        self._agent_buttons: dict[str, dict] = {}
        self._agent_hint_label: dict[str, ttk.Label] = {}
        self._agent_hint_var: dict[str, tk.StringVar] = {}
        self._agent_tree_ref: dict[str, ttk.Treeview] = {}
        self._sort_col = ""
        self._sort_desc = False
        self._show_key = False

        self.var_protocol = tk.StringVar(value="openai")
        self.var_base = tk.StringVar()
        self.var_key = tk.StringVar()
        self.var_proxy = tk.BooleanVar(value=False)
        self.var_autov1 = tk.BooleanVar(value=True)
        self.var_status = tk.StringVar(value="填入 baseURL 与 API Key，点「获取模型」。")
        self.var_ctx_hint = tk.StringVar(value="")
        self.var_filter = tk.StringVar()
        self.var_filter_mode = tk.StringVar(value=FILTER_ALL)
        self.var_count = tk.StringVar(value="")
        self.var_speed_rounds = tk.StringVar(value=str(mp.SPEED_DEFAULT_ROUNDS))
        self.var_speed_tokens = tk.StringVar(value=SPEED_TOKEN_DEFAULT)
        self.var_speed_workers = tk.StringVar(value=str(mp.SPEED_DEFAULT_WORKERS))
        self.var_speed_progress = tk.StringVar(value="")
        self._speeding = False
        self._speed_ok = 0
        self._speed_fail = 0
        self._speed_slow = 0
        # 本轮测速的目标集合（norm_id）。进度小结只统计这里面的模型：
        # 分多次测不同模型时，`_speed_state` 里会同时留着历次结果，
        # 全量统计会出现「本轮只测 1 个，却显示 完成 3 · 待测试 1」的矛盾。
        self._speed_target: set[str] = set()
        self._speed_notes: list[str] = []
        self._speed_cancel = False
        # 历史端点：`_ep_data` 缓存记录表，`_pending_endpoint` 保存本次请求
        # 实际用过的 (base, key, protocol)，成功回调据此记录而不是读输入框。
        self._ep_data: dict = {"version": 1, "entries": []}
        self._pending_endpoint: tuple[str, str, str] | None = None
        # 筛选防抖的 after id：输入法连打时每键重绘整表会积压成卡顿，见 _schedule_fill
        self._fill_after: str | None = None
        # 智能路由/快速测速的两列缓存：{(端点, 令牌, 模型ID): {"ratio":…, "latency":…}}。
        # 延迟默认空、价格倍率默认「—」——没测过就是没测过，不拿 0 冒充数据。
        # 令牌必须进键：同一中转站的两个令牌各自 pin 到不同商家，延迟与倍率都可能不同，
        # 只按 (端点, 模型) 存会让两行显示同一个数，看着像「都一样快」。
        self._route_cells: dict[tuple[str, str, str], dict[str, str]] = {}
        # 当前固定了哪些 (端点, 令牌, 模型)，供后台体检用；键与 _route_cells 一致
        self._pinned: dict[tuple[str, str, str], dict] = {}
        # 管理页每行处在 SPEED_STATES 的哪一档（与探测页同一套状态与配色）。
        # 键与 _route_cells 相同：(端点, 令牌, 模型)。
        self._quick_state: dict[tuple[str, str, str], str] = {}
        self._watch_stop = threading.Event()
        self._routing = False
        # 快速测速：一轮跑完才算结束，进度按已完成条数计
        self._quick_speeding = False
        self._quick_total = 0
        self._quick_done = 0
        self._quick_fail = 0
        self._quick_last_error = ""

        self._build()
        self._load_config()
        for kind in agents.kinds():
            self._refresh_agent(kind)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(120, self._drain)
        # 固定之后的守护：定时戳一下当前路由，坏了自动换家（见 _watch_worker）
        threading.Thread(target=self._watch_worker, daemon=True).start()

    # ---------------- 布局 ----------------
    def _build(self) -> None:
        # 状态栏先 pack，Tk 按 pack 顺序分配空间，否则会被内容区挤出窗口
        bar = ttk.Frame(self, style="Panel.TFrame", padding=(14, 7))
        bar.pack(fill="x", side="bottom")
        self.lbl_status = ttk.Label(bar, textvariable=self.var_status, style="Status.TLabel")
        self.lbl_status.pack(side="left")

        # 顶部一行：页签靠左，「设置」贴右上角。
        # 设置里的「走系统代理 / 404 自动试 v1」是**全局**探针行为，与当前页无关；
        # 智能路由的参数与站点凭据同样全局 —— 所以只在窗口层留一个入口，
        # 不再每页各摆一个（那样看起来像页级选项）。
        top = ttk.Frame(self)
        top.pack(fill="x", padx=10, pady=(8, 0))
        self.btn_settings = ttk.Button(top, text="设置", command=self._open_settings)
        self.btn_settings.pack(side="right")
        self.tabbar = TabBar(top)
        self.tabbar.pack(side="left", fill="x", expand=True)
        self.nb = self.tabbar  # 兼容旧调用方（外部脚本里的 app.nb.select）

        self.body = ttk.Frame(self)
        self.body.pack(fill="both", expand=True, padx=10, pady=(0, 4))
        self.page_probe = ttk.Frame(self.body, padding=(12, 12, 12, 10))
        self.tabbar.add(self.page_probe, "令牌探测")

        # 每个 Agent 一页，按注册表顺序生成：新增 Agent 不必再改这里。
        self.pages: dict[str, ttk.Frame] = {}
        for spec in agents.AGENTS:
            page = ttk.Frame(self.body, padding=(12, 12, 12, 10))
            self.tabbar.add(page, spec.tab_label)
            self.pages[spec.kind] = page

        self._build_probe(self.page_probe)
        for spec in agents.AGENTS:
            self._build_agent_page(self.pages[spec.kind], spec.kind)

        # 兼容旧引用（外部脚本里有 `app.page_wb` / `app.tree_wb` 这类写法）
        self.page_wb = self.pages.get("workbuddy")
        self.page_dsh = self.pages.get("dsh")
        self.page_codex = self.pages.get("codex")

    # ---- 页 1：令牌探测 ----
    def _build_probe(self, outer: ttk.Frame) -> None:
        ttk.Label(
            outer,
            text="输入中转站的 Base URL 与 API Key，拉取它当前提供的模型；"
                 "再测首字与速度。选中模型后可一键写入 WorkBuddy / DeepSeekHarness。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 10))

        form = ttk.Frame(outer)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="协议").grid(row=0, column=0, sticky="w", padx=(0, 10))
        self.cmb_protocol = ttk.Combobox(
            form, state="readonly", width=26,
            values=[mp.PROTOCOLS[k] for k in mp.PROTOCOLS],
        )
        self.cmb_protocol.current(0)
        self.cmb_protocol.grid(row=0, column=1, sticky="w")
        self.cmb_protocol.bind("<<ComboboxSelected>>", self._on_protocol_change)

        ttk.Label(form, text="Base URL").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=(8, 0))
        self.ent_base = ttk.Entry(form, textvariable=self.var_base)
        self.ent_base.grid(row=1, column=1, sticky="ew", pady=(8, 0))
        # 历史端点下拉：点了就把那一条的 URL **和 Key 一起**填回来。
        # 与 API Key 右侧的「显示」按钮同一列，右边的对齐线才不会断。
        self.btn_history = ttk.Button(form, text="历史", width=6, command=self._show_history_menu)
        self.btn_history.grid(row=1, column=2, padx=(8, 0), pady=(8, 0))
        Tooltip(self.btn_history, "最近成功获取过模型的 Base URL；\n点一条会同时填回它当时用的 API Key。")

        ttk.Label(form, text="API Key").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=(8, 0))
        self.ent_key = ttk.Entry(form, textvariable=self.var_key, show="•")
        self.ent_key.grid(row=2, column=1, sticky="ew", pady=(8, 0))
        self.btn_eye = ttk.Button(form, text="显示", width=6, command=self._toggle_key)
        self.btn_eye.grid(row=2, column=2, padx=(8, 0), pady=(8, 0))

        opts = ttk.Frame(outer)
        opts.pack(fill="x", pady=(10, 10))
        # 「走系统代理 / 路径 404 自动试 /v1」已移到右上角「设置」：
        # 它们是全局行为，摆在这里容易和「本次探测」的参数混为一谈。
        ttk.Label(opts, text="代理与路径重试在右上角「设置」里", style="Muted.TLabel").pack(side="left")
        self.btn_go = ttk.Button(opts, text="获取模型", style="Accent.TButton", command=self._start)
        self.btn_go.pack(side="right")

        self.ent_base.bind("<Return>", lambda _e: self._start())
        self.ent_key.bind("<Return>", lambda _e: self._start())

        # 两行并一行：左侧筛选，右侧测速参数，省下的高度全给表格。
        # 那个「并行；首字超过 10 秒…」的长说明挂到「并发」上做悬停提示，
        # 常驻文字只留真正需要一直看见的东西。
        filt = ttk.Frame(outer)
        filt.pack(fill="x", pady=(0, 8))
        ttk.Label(filt, text="筛选", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        self.ent_filter = ttk.Entry(filt, textvariable=self.var_filter, width=18)
        self.ent_filter.pack(side="left")
        self.ent_filter.bind("<KeyRelease>", lambda _e: self._schedule_fill())
        self.ent_filter.bind("<Escape>", lambda _e: self._clear_filter())
        # 宽度 11：最长项「超时10s以上」实测 68px，半角数字 `10s` 比四个汉字窄，
        # 按字符数估会低估，width=8 会被截成「超时10s以…」。
        self.cmb_filter = ttk.Combobox(filt, textvariable=self.var_filter_mode, state="readonly",
                                       values=FILTER_MODES, width=11)
        self.cmb_filter.pack(side="left", padx=(8, 0))
        self.cmb_filter.bind("<<ComboboxSelected>>", lambda _e: self._fill())
        ttk.Button(filt, text="清空", command=self._clear_filter).pack(side="left", padx=(8, 0))

        # 测速控件整体贴右：内部从左到右排，外层挂到 filt 右侧。
        # 不再放「测速」二字标签——按钮文字已经把事说清楚了，省下的宽度留给并发下拉，
        # 否则最后一个控件会被窗口右边界切掉。
        speed_row = ttk.Frame(filt)
        speed_row.pack(side="right")
        self.btn_speed = ttk.Button(speed_row, text="测首字与速度", style="Accent.TButton",
                                    command=self._start_speed)
        self.btn_speed.pack(side="left", padx=(0, 10))
        ttk.Label(speed_row, text="轮数", style="Muted.TLabel").pack(side="left", padx=(0, 4))
        self.cmb_rounds = ttk.Combobox(speed_row, textvariable=self.var_speed_rounds,
                                       values=SPEED_ROUND_CHOICES, state="readonly", width=2)
        self.cmb_rounds.pack(side="left", padx=(0, 8))
        ttk.Label(speed_row, text="输出上限", style="Muted.TLabel").pack(side="left", padx=(0, 4))
        self.cmb_tokens = ttk.Combobox(speed_row, textvariable=self.var_speed_tokens,
                                       values=SPEED_TOKEN_CHOICES, state="readonly", width=4)
        self.cmb_tokens.pack(side="left", padx=(0, 8))
        lbl_workers = ttk.Label(speed_row, text="并发", style="Muted.TLabel")
        lbl_workers.pack(side="left", padx=(0, 4))
        # 宽度 3：要放得下两位数 16，width=2 会只剩半个字形
        self.cmb_workers = ttk.Combobox(speed_row, textvariable=self.var_speed_workers,
                                        values=SPEED_WORKER_CHOICES, state="readonly", width=3)
        self.cmb_workers.pack(side="left", padx=(0, 2))
        # 悬停「并发」或它的下拉才显示说明
        Tooltip(lbl_workers, SPEED_HINT)
        Tooltip(self.cmb_workers, SPEED_HINT)

        table_wrap = ttk.Frame(outer)
        table_wrap.pack(fill="both", expand=True)
        # 上下文 / 最大输出两列已删：标准 /models 只返回 id，这两列长期是空的，
        # 占着宽度反而把真正要看的状态挤到边上。
        # 价格列之所以能留下来，是因为它不指望端点披露，而是来自用户自己的价格表
        # （price_book）；查不到就显示 —，排序永远垫底，绝不当 0。
        # 首列是勾选框：Treeview 没有原生复选框，用字形 + 点击切换模拟。
        columns = ("pick", "id", "price", "ttft", "tps", "status")
        self.tree = ttk.Treeview(table_wrap, columns=columns, show="headings", selectmode="extended")
        headings = {
            "pick": ("☐", 56, "center"),
            "id": ("模型 ID", 330, "w"),
            "price": (PRICE_HEADING, 100, "e"),
            "ttft": ("首字延迟", 130, "e"),
            "tps": ("生成速度", 130, "e"),
            "status": ("状态", 130, "w"),
        }
        for col, (text, width, anchor) in headings.items():
            command = self._toggle_all if col == "pick" else (lambda c=col: self._sort_by(c))
            self.tree.heading(col, text=text, command=command)
            self.tree.column(col, width=width, anchor=anchor, stretch=(col == "id"))
        vsb = ttk.Scrollbar(table_wrap, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        for key, (_text, color) in SPEED_STATES.items():
            self.tree.tag_configure(key, foreground=color)
        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<Double-1>", self._on_tree_double)
        self.tree.bind("<Button-3>", self._row_menu)
        # 表头只有「预估价格」需要解释单位：一列数字不带单位容易读成别的量纲
        HeaderTooltip(self.tree, {"price": PRICE_TIP})
        self.bind("<Control-f>", lambda _e: self._focus_filter())
        self._refresh_sort_indicators()

        hint_row = ttk.Frame(outer)
        hint_row.pack(fill="x", pady=(8, 0))
        # 右侧从右往左：测速进度 → 显示条数；左侧是端点提示。
        # 「显示 x/y」原本挤在筛选行，会把右边的测速控件顶出窗口。
        ttk.Label(hint_row, textvariable=self.var_speed_progress,
                  style="Muted.TLabel").pack(side="right")
        ttk.Label(hint_row, textvariable=self.var_count,
                  style="Muted.TLabel").pack(side="right", padx=(0, 16))
        ttk.Label(hint_row, textvariable=self.var_ctx_hint,
                  style="Muted.TLabel").pack(side="left")

        # 导入入口：只在选中模型后可用。没选中时置灰，比「点了再弹提示」少一次空点击。
        # 按注册表生成，新增 Agent 时这里不用改。
        agent_row = ttk.Frame(outer)
        agent_row.pack(fill="x", pady=(10, 0))
        ttk.Label(agent_row, text="导入到", style="Muted.TLabel").pack(side="left", padx=(0, 8))
        self.btn_import: dict[str, ttk.Button] = {}
        for spec in agents.AGENTS:
            btn = ttk.Button(agent_row, text=spec.button_label, style="Accent.TButton",
                             command=lambda k=spec.kind: self._import_to(k))
            btn.pack(side="left", padx=(0, 8))
            self.btn_import[spec.kind] = btn
        self.var_import_hint = tk.StringVar(value="先在表格里选中模型")
        ttk.Label(agent_row, textvariable=self.var_import_hint,
                  style="Muted.TLabel").pack(side="left")
        ttk.Button(agent_row, text="参考价格表", command=self._edit_prices).pack(side="right")
        self._sync_import_buttons()

    # ---- 页 2..N：Agent 配置管理 ----
    def _build_agent_page(self, outer: ttk.Frame, kind: str) -> None:
        """按注册表里的描述渲染一页。三个 Agent 共用这一套控件，差异只在数据。"""
        spec = agents.get(kind)
        target = spec.primary_path()
        ttk.Label(
            outer,
            text=f"读取 {target}，管理其中已有的自定义模型。{spec.note}"
                 f"每次增删前都会在模型管家的备份目录留一份最新快照（环形 5 份）。",
            style="Muted.TLabel",
            wraplength=1100,
        ).pack(anchor="w", pady=(0, 10))

        top = ttk.Frame(outer)
        top.pack(fill="x", pady=(0, 8))
        # 未安装该 Agent 时，除「刷新」外的按钮全部置灰：刷新是唯一的自救入口，
        # 用户装好之后点它就能重新检测，不用重启程序。
        btns: dict[str, ttk.Button] = {
            "refresh": ttk.Button(top, text="刷新", command=lambda: self._refresh_agent(kind)),
            "add": ttk.Button(top, text="添加模型", style="Accent.TButton",
                              command=lambda: self._add_agent_model(kind)),
            # 文案由注册表给：Claude Code 的「删」是清空槽位，叫「删除选中」
            # 会让人以为能从它的配置里消灭一个模型条目。
            "delete": ttk.Button(top, text=spec.delete_label,
                                 command=lambda: self._delete_agent_model(kind)),
            "probe": ttk.Button(top, text="送去探测",
                                command=lambda: self._probe_agent_model(kind)),
            # 直接在管理页测，不用来回切页：固定 1 轮 + 16 个输出 token，
            # 只量首字够不够快，不做「能不能用」以外的事
            "speed": ttk.Button(top, text="快速测速",
                                command=lambda: self._quick_speed(kind),
                                state="disabled"),
            # 默认置灰：没选中模型时它无事可做，先灰着比点了弹窗干净
            "route": ttk.Button(top, text="智能路由",
                                command=lambda: self._smart_route(kind), state="disabled"),
            "restore": ttk.Button(top, text="还原上一次修改",
                                  command=lambda: self._restore_agent(kind)),
            "open": ttk.Button(top, text="打开配置文件目录",
                               command=lambda: self._open_config_dir(kind)),
        }
        if spec.can_set_active:
            btns["active"] = ttk.Button(top, text="设为当前模型", style="Accent.TButton",
                                        command=lambda: self._set_active_agent(kind))
        order = ["refresh", "add", "delete", "probe", "speed", "route", "restore"]
        if "active" in btns:
            order.append("active")
        for key in order:
            btns[key].pack(side="left", padx=(0, 8))
        # 「设置」已上移到窗口顶部：它管的是全局探针与路由行为，不属于某一页
        btns["open"].pack(side="right")
        self._agent_buttons[kind] = btns

        wrap = ttk.Frame(outer)
        wrap.pack(fill="both", expand=True)
        # 列顺序：「当前」紧跟模型 ID。放最右端会被竖向滚动条挤掉表头与内容——
        # 一列读不到值的「当前」等于没有这一列。
        columns = ["head", "id"]
        headings = {
            "head": (spec.column_head, 240, "w"),
            "id": ("模型 ID", 280, "w"),
        }
        if spec.can_set_active:
            columns.append("active")
            headings["active"] = ("当前", 70, "center")
        columns += ["context", "vision", "reasoning", "ratio", "cache", "price", "latency"]
        headings["context"] = ("上下文", 90, "e")
        headings["vision"] = ("图片", 60, "center")
        headings["reasoning"] = ("支持档位", 120, "w")
        # 三列的默认值：价格倍率「—」= 还没查过；预估价格「—」= 表里没有；
        # 延迟留空 = 还没测过。三者都不拿 0 冒充数据。
        headings["ratio"] = ("价格倍率", 78, "e")
        # 缓存率决定实际花多少钱：命中缓存的部分只按输入价的 2% 计费。
        # 标价 0.06x 但缓存 19% 的家，实际比标价 0.079x、缓存 91% 的家贵 5 倍。
        headings["cache"] = ("缓存率", 68, "e")
        headings["price"] = (PRICE_HEADING, 88, "e")
        # 96 而不是 80：这一格现在还要放「测试中…」这类状态文案，
        # 80 会把省略号切掉，看起来像界面坏了
        headings["latency"] = ("延迟(秒)", 96, "e")
        tree = ttk.Treeview(wrap, columns=tuple(columns), show="headings",
                            selectmode="extended")
        tree.tag_configure("missing", foreground=ERR)
        # 与令牌探测页共用 SPEED_STATES：灰=待测、蓝=测试中、绿=完成、黄=超时、红=失败。
        # Treeview 只能整行染色，所以状态落在行上、文案落在「延迟」列——管理页没有
        # 独立的状态列，两处信息合在一格反而更好扫。
        for state_key, (_text, color) in SPEED_STATES.items():
            tree.tag_configure(state_key, foreground=color)
        for col, (text, width, anchor) in headings.items():
            tree.heading(col, text=text)
            # 只有模型 ID 列吃掉多余宽度：端点/Provider 长短不一，撑开反而更难扫
            tree.column(col, width=width, anchor=anchor, stretch=(col == "id"))
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        tree.bind("<Delete>", lambda _e: self._delete_agent_model(kind))
        tree.bind("<Double-1>", lambda _e: self._probe_agent_model(kind))
        tree.bind("<<TreeviewSelect>>", lambda _e: self._sync_agent_buttons(kind))
        HeaderTooltip(tree, {"cache":
                             "缓存命中率，来自我自己的调用记录（站点接口不返回这个数）。\n"
                             "命中部分只按输入价的 2% 计费，所以：\n"
                             "　实际成本倍率 ≈ 标价倍率 × (1 − 0.98 × 缓存率)\n"
                             "标价便宜但缓存率低，真实账单往往更贵。\n"
                             "带 * 表示这家还没有调用记录，数字是按全局平均值估的。",
                             "price": PRICE_TIP,
                             "latency": "首字延迟，单位秒。\n"
                                        "「快速测速」（1 轮 · 16 token）可直接在本页测得，"
                                        "探测页测速或智能路由成功后也会回填。\n\n"
                                        "整行颜色与探测页一致：\n"
                                        "　灰 = 待测试　蓝 = 测试中　绿 = 完成\n"
                                        "　黄 = 超时10s以上　红 = 失败"})

        hint = tk.StringVar()
        lbl = ttk.Label(outer, textvariable=hint, style="Muted.TLabel", wraplength=1100)
        lbl.pack(anchor="w", pady=(8, 0))
        # 未安装时这行要变黄，所以得留着控件引用改前景色
        self._agent_hint_label[kind] = lbl
        self._agent_hint_var[kind] = hint
        self._agent_tree_ref[kind] = tree
        # 兼容旧引用名（app.tree_wb / app.tree_dsh）
        setattr(self, f"tree_{kind}", tree)

    # ---------------- 读者：当前页 / 协议 ----------------
    def _agent_tree(self, kind: str) -> ttk.Treeview:
        return self._agent_tree_ref[kind]

    # ---------------- 智能路由 ----------------
    @staticmethod
    def _norm_base(base: str) -> str:
        return (base or "").strip().rstrip("/").lower()

    def _agent_endpoint(self, spec, row) -> str:
        """该行真实的 Base URL。

        **不能用 head_of**：它是给表格首列「看」的——DSH 返回 provider 键（`a6api`）、
        Codex 返回 `provider · api`、Claude 返回槽位名，三者都不是 URL。
        拿它去推断控制面域名，会把 `a6api` 当成主机名，DNS 必然失败。
        """
        base = str(getattr(row, "base_url", "") or "").strip()
        if base:
            return base
        head = str(spec.head_of(row) if spec.head_of else "").strip()
        return head if head.startswith("http") else ""

    @staticmethod
    def _row_key(base: str, api_key: str, model_id: str) -> tuple[str, str, str]:
        """一行结果的身份：端点 + 令牌 + 模型。

        令牌不能省。同一个中转站只给一个域名，两个令牌各自 pin 到不同商家，
        延迟与价格倍率都可能不一样；只按 (端点, 模型) 存，后测的会把先测的盖掉，
        两行显示同一个数，看起来像「两家一样快」，实际是其中一家被顶替了。
        """
        return ((base or "").strip().rstrip("/").lower(),
                ai.bare_key(api_key or ""), model_id)

    def _route_key(self, spec, row) -> tuple[str, str, str]:
        return self._row_key(self._agent_endpoint(spec, row),
                             getattr(row, "api_key", "") or "", row.model_id)

    def _row_iids(self, kind: str, base: str, api_key: str, model_id: str,
                  *, ignore_key: bool = False) -> list[str]:
        """找出与该身份对应的树行。`ignore_key` 用于探测页没有 Key 时按端点+模型匹配。"""
        spec = agents.get(kind)
        want = self._row_key(base, api_key, model_id)
        out: list[str] = []
        for row in self._agents.get(kind) or []:
            if row.model_id != model_id:
                continue
            key = self._row_key(self._agent_endpoint(spec, row),
                                getattr(row, "api_key", "") or "", row.model_id)
            if key == want or (ignore_key and key[0] == want[0]):
                out.append(agents.row_iid(spec, row))
        return out

    def _sync_agent_buttons(self, kind: str) -> None:
        """按当前选中情况刷新「智能路由」与「快速测速」。

        两个按钮的可用条件不同：
          * 智能路由 —— 恰好一行。pin 是 (令牌, 模型) 维度的操作，多选就产生
            「到底固定哪个」的歧义；悬空引用行连配置都不完整，也不该往上写。
          * 快速测速 —— 一行或多行都行。它只读不写，多测几个没有副作用，
            而且「选一排一起测」正是这个入口存在的理由。
        """
        btns = self._agent_buttons.get(kind) or {}
        picked = self._picked_agent(kind)
        usable = [r for r in picked if not getattr(r, "missing", False)]
        route = btns.get("route")
        if route is not None:
            ok = len(usable) == 1 and not self._routing and not self._quick_speeding
            route.state(["!disabled"] if ok else ["disabled"])
        speed = btns.get("speed")
        if speed is not None:
            ok = bool(usable) and not self._quick_speeding and not self._routing
            speed.state(["!disabled"] if ok else ["disabled"])

    def _current_api_base(self) -> str:
        """当前界面能看到的第一个真实端点，用于在设置里预填控制面地址。"""
        for kind in agents.kinds():
            for row in self._agents.get(kind) or []:
                base = str(getattr(row, "base_url", "") or "").strip()
                if base:
                    return base
        return self.var_base.get().strip()

    def _open_settings(self) -> None:
        dialog = SettingsDialog(
            self, sr.load_settings(), self._current_api_base(),
            proxy=self.var_proxy.get(), auto_v1=self.var_autov1.get(),
        )
        self.wait_window(dialog)
        if dialog.result is None:
            return
        # 探针开关是即时生效的：改完立刻写盘，不用等退出程序
        self.var_proxy.set(bool(dialog.result["proxy"]))
        self.var_autov1.set(bool(dialog.result["auto_v1"]))
        self._save_config()
        for kind in agents.kinds():
            self._sync_agent_buttons(kind)

    def _smart_route(self, kind: str) -> None:
        spec = agents.get(kind)
        if self._routing:
            messagebox.showinfo(mp.APP_TITLE, "上一次智能路由还在跑，等它结束。")
            return
        picked = self._picked_agent(kind)
        if len(picked) != 1:
            messagebox.showinfo(mp.APP_TITLE, "先在列表里选中**一个**模型，再点「智能路由」。")
            return
        row = picked[0]
        endpoint = self._agent_endpoint(spec, row)
        model_id = row.model_id
        if not endpoint.startswith("http"):
            messagebox.showwarning(
                mp.APP_TITLE,
                f"这一行没有解析出 Base URL（拿到的是 {endpoint or '空'}）。\n\n"
                "该 Agent 的 provider 配置里缺少 url，先补上再点智能路由。",
            )
            return

        cred = sr.site_credentials(endpoint) or {}
        if not (cred.get("access_token") and cred.get("user_id")):
            if messagebox.askyesno(
                mp.APP_TITLE,
                "还没有保存该站点的访问令牌与用户 ID。\n\n"
                "现在打开「设置」填写吗？填一次即可，之后不再询问。",
            ):
                self._open_settings()
            return
        control = str(cred.get("control_base") or "").strip() or sr.guess_control_base(endpoint)

        api_key = str(getattr(row, "api_key", "") or "").strip()
        if not api_key:
            found = eb.find(endpoint)
            api_key = str((found or {}).get("key") or "").strip()
        if not api_key:
            messagebox.showwarning(
                mp.APP_TITLE,
                f"没有可用于实测的 API Key。\n\n"
                f"请在「令牌探测」页用 {endpoint} 获取一次模型列表，"
                f"让端点簿记住 Key；或先在该 Agent 的配置里填好 Key。",
            )
            return

        self._routing = True
        self._sync_agent_buttons(kind)
        self._status(f"智能路由：正在为 {model_id} 选择商家…", ACCENT)
        # 控制面调用跟随探测页的「走系统代理」：同一个站点，探测能通、
        # 路由却直连失败的话，用户会以为是两个功能坏了。
        sr.set_use_proxy(bool(self.var_proxy.get()))
        args = (kind, control, str(cred["access_token"]), str(cred["user_id"]),
                endpoint, api_key, model_id)
        threading.Thread(target=self._route_worker, args=args, daemon=True).start()

    def _route_worker(self, kind, control, access_token, user_id, endpoint, api_key,
                      model_id) -> None:
        """工作线程：整条智能路由都在这里跑，结果通过队列回主线程。"""
        try:
            token_id, token_name, plain = sr.resolve_token(
                control, access_token, user_id, api_key)
            self._queue.put(("route-stage",
                             f"智能路由：使用令牌 {token_name or token_id}，开始拉取商家…"))
            result = sr.route(
                api_base=endpoint,
                control_base=control,
                access_token=access_token,
                user_id=user_id,
                token_id=token_id,
                api_key=plain,
                model_name=model_id,
                protocol="openai",
                settings=sr.load_settings(),
                on_stage=lambda text: self._queue.put(("route-stage", text)),
            )
        except Exception as e:  # noqa: BLE001 - 任何失败都要让按钮恢复
            # HttpError / NetError / RouteError 的文本本身就是给人看的（已含状态码含义），
            # 前面再糊一个类名只会挤占本就不宽的状态栏。
            text = str(e).strip() or type(e).__name__
            if type(e).__name__ not in ("HttpError", "NetError", "RouteError"):
                text = f"{type(e).__name__}：{text}"
            result = sr.RouteResult(ok=False, model=model_id, message=text)
        self._queue.put(("route-done", (kind, endpoint, api_key, model_id, result)))

    def _finish_route(self, kind, endpoint, api_key, model_id, result) -> None:
        self._routing = False
        self._sync_agent_buttons(kind)
        if result.ok and result.candidate is not None:
            cand = result.candidate
            # 取本轮实测到的首字：优先写真实测得的值，没测到就留空，
            # 不拿平台的 p50 顶替——那个是统计值，和此刻这条链路无关。
            ttft = next((t for _c, _v, t in result.tested if t is not None), None)
            latency = format_seconds(ttft) if ttft else None
            self._set_route_cells(kind, endpoint, api_key, model_id, state="done",
                                  ratio=f"{cand.ratio:.2f}x",
                                  cache=self._cache_cell(cand),
                                  latency=latency)
            # 成功时 smart_route 已把商户价写进价格表：重读一次，让两个页面的
            # 「预估价格」立刻反映刚测出来的数，而不是等下次启动。
            self._reload_prices()
            # 记下这次固定，交给后台体检盯着——选商是一瞬间的判断，
            # 商家半小时后坏掉，只有持续体检才发觉得了。
            self._pinned[self._row_key(endpoint, api_key, model_id)] = {
                "kind": kind, "endpoint": endpoint, "api_key": api_key,
                "model": model_id, "channel": cand.channel_id, "at": time.time(),
            }
            self._status(result.message, OK)
            return
        if result.tested:
            # 真去测过但一家都没通：这行就是不可用，按失败染色
            self._set_route_cells(kind, endpoint, api_key, model_id, state="error")
        message = result.message
        # 站点整站 502 那次事故：错误本身说清了原因，但用户不知道「是不是该重试」。
        # 网关故障几乎都是一两分钟内自愈，直接告诉他再点一次就行。
        # 「商家侧没有可用上游账号」是另一种 502：等没用，换一家才有用——它是
        # 单家商户的容量问题，站点的其它商家此刻多半是好的，不能建议「稍等」。
        if "商家侧" in message:
            message += "（这是那一家商户的问题，换一家通常立刻就好，可再点一次智能路由）"
        elif any(word in message for word in ("网关", "502", "503", "504")):
            message += "（站点侧故障，稍等一两分钟再点一次即可）"
        self._status(f"智能路由失败：{message}", WARN)

    def _set_route_cells(self, kind: str, endpoint: str, api_key: str,
                         model_id: str, *, state: str | None = None, **cells) -> None:
        """把结果写进缓存并刷新对应的那一行。

        定位用 `_row_iids`（端点 + 令牌 + 模型），而不是「所有同模型的行」：
        两个令牌各一条同名模型时，按模型 ID 刷会把两行写成同一个数。

        `state` 给定时整行按 SPEED_STATES 染色，与令牌探测页完全一致。
        """
        key = self._row_key(endpoint, api_key, model_id)
        bucket = self._route_cells.setdefault(key, {})
        bucket.update({k: v for k, v in cells.items() if v is not None})
        if state is not None:
            if state:
                self._quick_state[key] = state
            else:
                self._quick_state.pop(key, None)
        writable = dict(cells)
        if state is not None and "latency" not in writable:
            # 只改状态没给延迟时，延迟格也要跟着状态变（测试中/超时/失败/待测试）。
            # 否则会出现「行变蓝了、格子还写着上一个数」这种半新半旧的样子。
            writable["latency"] = bucket.get("latency", "")
        tree = self._agent_tree(kind)
        iids = [iid for iid in self._row_iids(kind, endpoint, api_key, model_id)
                if tree.exists(iid)]
        for iid in iids:
            for col, text in writable.items():
                if text is None:
                    continue
                if col == "latency":
                    text = self._latency_cell(key, text)
                tree.set(iid, col, text)
            if state is not None:
                self._paint_state(tree, iid, state)

    def _paint_state(self, tree: ttk.Treeview, iid: str, state: str) -> None:
        """给一行上色。悬空引用行保持红色——它连端点和 Key 都缺，
        测速状态对它没有意义，覆盖掉反而把「这行配置坏了」这个更要紧的信息藏了。"""
        tags = tuple(tree.item(iid, "tags") or ())
        if "missing" in tags:
            return
        tree.item(iid, tags=(state,) if state in SPEED_STATES else ())

    @staticmethod
    def _cache_cell(cand) -> str:
        """缓存率单元格：实测值，或冷启动时按全局平均估算（带 * 标明是估的）。"""
        rate = float(getattr(cand, "cache_rate", 0.0) or 0.0)
        mark = "" if getattr(cand, "cache_known", False) else "*"
        return f"{rate * 100:.0f}%{mark}"

    def _latency_cell(self, key: tuple[str, str, str], text: str) -> str:
        """延迟列最终显示什么：有状态时状态优先（测试中/超时/失败），否则显示数值。

        与探测页的「首字延迟 + 状态」两列等价：那边状态单独一列，管理页没有，
        所以把状态文案压进同一格——一格能读懂的，不该拆成两处。
        """
        state = self._quick_state.get(key, "")
        if state == "running":
            return "测试中…"
        if state == "slow":
            return "超时"
        if state == "error":
            return "失败"
        if state == "pending":
            return text or "待测试"
        return text


    # ---------------- 固定之后的守护 ----------------
    def _watch_worker(self) -> None:
        """后台体检：定时戳一下当前固定的商家，连续两次不通就自动换一家。

        为什么必须有这一层：选商只是**那一刻**的判断，而商家健康是时变的
        （实测同一家 3 分钟内 6 次失败 / 4 次成功）。没有持续体检，用户只能
        自己发现「又不行了」再回来点一次按钮——这正是之前最难受的地方。

        跑在守护线程里，只通过队列与界面通信；间隔来自设置，0 表示关闭。
        """
        health = sr.PinHealth()
        while not self._watch_stop.is_set():
            try:
                interval = int(getattr(sr.load_settings(), "watch_interval_s", 0) or 0)
            except OSError:
                interval = 0
            except Exception:  # noqa: BLE001 - 设置读坏了不该让线程死掉
                interval = 0
            if interval <= 0:
                if self._watch_stop.wait(60):
                    break
                continue
            if self._watch_stop.wait(interval):
                break
            if self._routing or not self._pinned:
                continue
            for key, target in list(self._pinned.items()):
                if self._watch_stop.is_set():
                    break
                try:
                    self._watch_one(health, key, target)
                except Exception:  # noqa: BLE001 - 体检失败只跳过，不影响界面
                    continue

    def _watch_one(self, health: "sr.PinHealth", key, target: dict) -> None:
        """体检一条固定：先看站点日志里的真实失败，再自己戳一次。

        为什么先读日志：采样能通过、但用户下一秒的真实调用照样 500——这种间歇
        故障正是「智能测试通过、实际用不了」的来源。日志里记的是**用户真实调用**
        的结果，比我们再发一次小请求更有说服力：固定之后同一家已经失败两次，
        就不必再等两轮体检（最长十分钟）才换家。
        """
        endpoint = str(target.get("endpoint") or "")
        api_key = str(target.get("api_key") or "")
        model_id = str(target.get("model") or "")
        if not (endpoint.startswith("http") and api_key and model_id):
            return
        try:
            channel = int(target.get("channel") or 0)
        except (TypeError, ValueError):
            channel = 0
        cred = sr.site_credentials(endpoint) or {}
        control = str(cred.get("control_base") or "").strip() or sr.guess_control_base(endpoint)
        token = str(cred.get("access_token") or "").strip()
        uid = str(cred.get("user_id") or "").strip()

        def reroute(reason: str) -> None:
            self._queue.put(("route-stage", reason))
            if not (control and token and uid):
                return
            self._queue.put(("route-stage", f"体检：{model_id} 正在自动换一家…"))
            self._routing = True
            try:
                self._route_worker(str(target.get("kind") or ""), control, token, uid,
                                   endpoint, api_key, model_id)
            finally:
                # 无论成功失败都要让下一次体检能继续；_finish_route 会把 _routing 置回
                self._routing = False
                health.forget(key)

        # ① 日志判据：只看「这次固定之后」发生的失败，历史旧账不算
        if channel and control and token and uid:
            try:
                faults = sr.recent_channel_faults(
                    control, token, uid, model_id, channel,
                    window_s=sr.WATCH_LOG_WINDOW_S,
                    since=float(target.get("at") or 0))
            except Exception:      # noqa: BLE001 - 日志读不到就退回主动探测
                faults = {"fails": 0, "last": 0, "code": ""}
            if faults["fails"] >= sr.WATCH_LOG_FAILS:
                verdict = health.record(key, False,
                                        now=(faults["last"] or None))
                if verdict == "reroute":
                    code = f"（{faults['code']}）" if faults.get("code") else ""
                    reroute(f"体检：站点日志显示 {model_id} 固定后真实调用失败 "
                            f"{faults['fails']} 次{code}，正在自动换一家…")
                    return
                if verdict == "watch":
                    self._queue.put(("route-stage",
                                     f"体检：站点日志显示 {model_id} 失败 1 次，再观察…"))
                return              # ok / cooldown 都不再发探测请求

        # ② 主动探测：日志里没证据时，仍按老规矩「连续两次不通换家」
        probe = mp.probe_speed(
            "openai", endpoint, api_key, model_id,
            rounds=1, max_tokens=sr.VERIFY_MIN_TOKENS,
            prompt=sr.RouteSettings().prompt, expect=sr.VERIFY_WORD,
            ttft_limit_ms=15_000,
        )
        verdict = health.record(key, probe.ok)
        if verdict == "ok":
            return
        if verdict == "watch":
            self._queue.put(("route-stage",
                             f"体检：{model_id} 这次没通（{probe.error[:40] if probe.error else '无响应'}），"
                             f"再确认一次…"))
            return
        if verdict != "reroute":
            return            # 冷却期内：不重复换家，免得反复花实测的钱
        reroute(f"体检：{model_id} 连续两次不通，正在自动换一家…")

    # ---------------- 快速测速 ----------------
    def _quick_speed(self, kind: str) -> None:
        """在本页直接测选中模型的首字延迟：固定 1 轮 + 16 个输出 token。

        为什么不复用探测页那套：那边得先拉模型列表、勾选、再点测速，而用户此刻
        就站在管理页上，端点与 Key 都在行里。来回复述一遍已知信息没有意义。
        """
        spec = agents.get(kind)
        if not self._require_agent(kind):
            return
        if self._quick_speeding:
            messagebox.showinfo(mp.APP_TITLE, "上一轮快速测速还在跑，等它结束。")
            return
        picked = self._picked_agent(kind)
        if not picked:
            messagebox.showinfo(mp.APP_TITLE, "先在列表里选中要测速的模型（可多选）。")
            return

        targets: list[tuple[str, str, str, str]] = []   # base, key, model, protocol
        skipped: list[str] = []
        for row in picked:
            if getattr(row, "missing", False):
                skipped.append(f"{row.model_id}（悬空引用）")
                continue
            base = self._agent_endpoint(spec, row)
            if not base.startswith("http"):
                skipped.append(f"{row.model_id}（没有 Base URL）")
                continue
            key = str(getattr(row, "api_key", "") or "").strip()
            if not key:
                key = str((eb.find(base) or {}).get("key") or "").strip()
            if not key:
                skipped.append(f"{row.model_id}（没有 API Key）")
                continue
            targets.append((base, key, row.model_id, row.protocol or "openai"))

        if not targets:
            messagebox.showwarning(
                mp.APP_TITLE,
                "这几行都没法测：\n  " + "\n  ".join(skipped)
                + "\n\n端点或 Key 缺失时，先在令牌探测页用同一个端点取一次模型列表，"
                  "端点簿会记住 Key。",
            )
            return

        # 与探测页一致：先把要测的几行标灰「待测试」，让用户知道排了哪些、排了多久
        for base, key, model, _proto in targets:
            self._set_route_cells(kind, base, key, model, state="pending")

        self._quick_speeding = True
        self._quick_total = len(targets)
        self._quick_done = 0
        self._quick_fail = 0
        self._quick_last_error = ""
        self._sync_agent_buttons(kind)
        self._status(
            f"快速测速：{len(targets)} 个模型，1 轮 / 输出上限 {QUICK_SPEED_TOKENS} token…"
            + (f"（跳过 {len(skipped)} 个）" if skipped else ""), ACCENT)
        threading.Thread(
            target=self._quick_speed_worker,
            args=(kind, targets, bool(self.var_proxy.get()), bool(self.var_autov1.get())),
            daemon=True,
        ).start()

    def _quick_speed_worker(self, kind, targets, use_proxy, auto_v1) -> None:
        """工作线程：逐条实测，结果回主线程。

        串行是有意的：多家中转同时跑会互相抢带宽，首字被自己人挤高，测出来的数
        反而不能反映单条链路的水平。一轮 16 token 很快，串行的总耗时可接受。
        """
        for base, key, model, protocol in targets:
            # 先报「开始测这一条」：串行跑时，界面要让用户看见当前在哪一行。
            # Tk 只能在主线程改，所以走队列，由 _drain 落到行上。
            self._queue.put(("quick-running", (kind, base, key, model)))
            self._queue.put(("quick-progress",
                             f"快速测速 {self._quick_done + 1}/{len(targets)}：{model} …"))
            try:
                res = mp.probe_speed(
                    protocol, base, key, model,
                    rounds=QUICK_SPEED_ROUNDS, max_tokens=QUICK_SPEED_TOKENS,
                    use_proxy=use_proxy, auto_v1=auto_v1,
                )
            except Exception as e:  # noqa: BLE001 - probe_speed 声称不抛，兜一层保险
                res = mp.SpeedResult(model=model, error=f"{type(e).__name__}：{e}")
            self._queue.put(("quick-done", (kind, base, key, model, res)))

    def _apply_quick_speed(self, kind, base, api_key, model_id, res) -> None:
        """单条结果落地：成功的写首字，「测过但没拿到数」写 —，与「从没测过」的空白区分开。

        「成功」不只看 `res.ok`：HTTP 200 且能出字也可能是假可用——正文其实是
        「上游服务器返回了无效响应，请重试」这种错误提示。这类要判失败。
        """
        self._quick_done += 1
        body = str(getattr(res, "content", "") or "")
        bad_text = bool(body.strip()) and mp.looks_like_error(body)
        healthy = getattr(res, "ok", False) and not bad_text and (
            res.ttft_ms is not None or body.strip())
        too_slow = bool(getattr(res, "too_slow", False))
        if healthy and res.ttft_ms is not None:
            self._set_route_cells(kind, base, api_key, model_id, state="done",
                                  latency=format_seconds(res.ttft_ms))
        elif healthy:
            # 只有非流式能通：测不到首字，但服务是好的，不能标成失败
            self._set_route_cells(kind, base, api_key, model_id, state="done", latency="—")
            self._status(f"快速测速：{model_id} 端点不支持流式，只测到总耗时 "
                         f"{format_seconds(res.total_ms)}", WARN)
        else:
            self._quick_fail += 1
            # 超时与失败要分开：前者是「这家慢」，后者是「这家不通」，
            # 用户看到黄和红时的下一步动作不一样（等/换 vs 直接重路由）。
            self._set_route_cells(kind, base, api_key, model_id,
                                  state="slow" if too_slow else "error", latency="—")
            if too_slow:
                self._quick_last_error = "首字超过 10 秒上限，已放弃（延迟列标「超时」）"
            else:
                self._quick_last_error = (f"模型把错误信息当正文返回：{body[:60]}"
                                          if bad_text else (res.error or "没有测到首字"))
            self._status(f"快速测速：{model_id} {'超时' if too_slow else '失败'}"
                         f" — {self._quick_last_error}", WARN)

        if self._quick_done < self._quick_total:
            self._status(f"快速测速 {self._quick_done}/{self._quick_total} …", ACCENT)
            return

        self._quick_speeding = False
        self._sync_agent_buttons(kind)
        ok = self._quick_total - self._quick_fail
        message = f"快速测速完成：成功 {ok}/{self._quick_total}"
        if self._quick_fail:
            message += f"，{self._quick_fail} 个没测到首字（延迟列标 —）"
        self._status(message, OK if ok else WARN)

        # 唯一一行都测不通：这多半就是「固定到了坏商家」。别只丢一句「失败」，
        # 直接把下一步递过去——重新智能路由换一家。
        if ok or self._quick_total != 1 or self._routing:
            return
        reason = self._quick_last_error or "没有测到首字"
        if any(word in reason for word in ("网关", "502", "503", "504")):
            return          # 站点整站故障，换哪家都一样，别误导用户去重路由
        if messagebox.askyesno(
            mp.APP_TITLE,
            f"「{model_id}」现在测不通：{reason}\n\n"
            "要立刻重新「智能路由」换一家吗？",
        ):
            self._smart_route(kind)

    def _protocol(self) -> str:
        label = self.cmb_protocol.get()
        for key, text in mp.PROTOCOLS.items():
            if text == label:
                return key
        return "openai"

    def _toggle_key(self) -> None:
        self._show_key = not self._show_key
        self.ent_key.configure(show="" if self._show_key else "•")
        self.btn_eye.configure(text="隐藏" if self._show_key else "显示")

    def _status(self, text: str, color: str = MUTED) -> None:
        self.var_status.set(text)
        self.lbl_status.configure(foreground=color)

    def _on_protocol_change(self, _e=None) -> None:
        proto = self._protocol()
        placeholder = {
            "openai": "https://api.deepseek.com/v1",
            "anthropic": "https://api.anthropic.com/v1",
            "gemini": "https://generativelanguage.googleapis.com/v1beta",
            "ollama": "http://127.0.0.1:11434",
        }[proto]
        if not self.var_base.get().strip():
            self.var_base.set(placeholder)

    # ---------------- 历史端点 ----------------
    def _show_history_menu(self) -> None:
        """在「历史」按钮下方弹出已记录的端点。

        空记录也要弹，但要给出「为什么空」而不是什么都不发生——
        按钮点了没反应，用户会以为坏了。
        """
        self._ep_data = eb.load()
        rows = eb.entries(self._ep_data)
        if not rows:
            menu = tk.Menu(self, tearoff=0, bg=PANEL, fg=FG, activebackground=PANEL,
                           activeforeground=MUTED, borderwidth=0)
            menu.add_command(label="（还没有记录）", command=lambda: None)
            menu.entryconfigure(0, foreground=MUTED, activeforeground=MUTED,
                                activebackground=PANEL)
            menu.add_command(label="成功获取一次模型后会自动记录", command=lambda: None)
            menu.entryconfigure(1, foreground=MUTED, activeforeground=MUTED,
                                activebackground=PANEL)
        else:
            menu = self._build_history_menu(rows)
        x = self.btn_history.winfo_rootx()
        y = self.btn_history.winfo_rooty() + self.btn_history.winfo_height() + 2
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()

    def _build_history_menu(self, rows: list[dict]) -> tk.Menu:
        """构建历史下拉（只构建不弹出），供 `_show_history_menu` 与测试共用。"""
        menu = tk.Menu(self, tearoff=0, bg=PANEL, fg=FG, activebackground=ACCENT,
                       activeforeground="#ffffff", borderwidth=0)
        for row in rows:
            base = str(row.get("base") or "")
            # 第二列显示掩码 Key：一眼能确认「这条记的是哪把钥匙」，
            # 又不至于把密钥整串摊在屏幕上。
            menu.add_command(
                label=f"{base}    {eb.mask_key(str(row.get('key') or ''))}",
                command=lambda b=base: self._apply_history(b),
            )
        menu.add_separator()
        menu.add_command(label="删除全部记录…", command=self._clear_history)
        return menu

    def _apply_history(self, base: str) -> None:
        """把某条记录的 URL + Key 填回表单。

        协议也一并回填：同一个 Key 换错协议必然失败，回填却只给 URL 与 Key
        等于把「为什么失败」留给用户自己猜。
        """
        row = eb.find(base, self._ep_data)
        if row is None:
            return
        if str(row.get("protocol") or "") in mp.PROTOCOLS:
            self.cmb_protocol.current(list(mp.PROTOCOLS).index(row["protocol"]))
        self.var_base.set(str(row.get("base") or ""))
        self.var_key.set(str(row.get("key") or ""))
        self._status(f"已填入记录的端点：{row.get('base')}（{row.get('updated') or '时间未知'}）", ACCENT)

    def _clear_history(self) -> None:
        if not messagebox.askyesno(
            mp.APP_TITLE,
            "删除本机记录的全部 Base URL 与 API Key？\n\n"
            f"文件：{eb.endpoint_path()}\n这个动作不可撤销。",
        ):
            return
        eb.save(eb.clear(eb.load()))
        self._ep_data = eb.load()
        self._status("已删除全部端点记录。", MUTED)

    def _remember_endpoint(self, base: str, key: str) -> None:
        """获取模型成功后记一条；同 URL 只更新 Key，不新增重复项。

        记的是**这一次请求真正用过的** URL 与 Key（由 `_start` 捕获后一路带过来），
        而不是回调时再读一次输入框——那次请求可能跑了几秒，用户中途改过输入框，
        回过头的「当前值」已经不是让它成功的那个值了。
        """
        try:
            data = eb.load()
            eb.save(eb.upsert(base, key, self._protocol(), data))
            self._ep_data = eb.load()
        except OSError as e:
            # 记录失败绝不能影响「已经拿到模型列表」这个好结果
            self._status(f"{self.var_status.get()}（端点记录未保存：{e}）", WARN)

    # ---------------- 探测 ----------------
    def _start(self) -> None:
        if self._busy:
            return
        base = self.var_base.get().strip()
        if not base:
            messagebox.showwarning(mp.APP_TITLE, "请先填写 Base URL。")
            return
        self._busy = True
        self.btn_go.state(["disabled"])
        self._status("正在获取模型列表 …")
        self.var_ctx_hint.set("")
        key = self.var_key.get()
        protocol = self._protocol()
        # 记下本次请求的身份，成功回调时据此记录端点：不能用回调时的输入框现值
        self._pending_endpoint = (base, key, protocol)
        args = (protocol, base, key, mp.DEFAULT_TIMEOUT,
                self.var_proxy.get(), self.var_autov1.get())
        threading.Thread(target=self._worker, args=args, daemon=True).start()

    def _worker(self, protocol, base, key, timeout, proxy, auto_v1) -> None:
        try:
            self._queue.put(mp.fetch_models(protocol, base, key, timeout=timeout,
                                            use_proxy=proxy, auto_v1=auto_v1))
        except Exception as e:  # noqa: BLE001 - 网络层兜底，别让窗口卡在「获取中」
            self._queue.put(mp.ProbeResult(error=f"探测异常：{e}"))

    def _apply(self, result: mp.ProbeResult) -> None:
        self._busy = False
        self.btn_go.state(["!disabled"])
        pending, self._pending_endpoint = self._pending_endpoint, None
        if not result.ok:
            self._status(f"失败 · {result.error}", ERR)
            return
        # 成功才记录：失败一次多半是 Key 填错，把错钥匙存下来只会污染下拉。
        if pending is not None:
            base, key, _protocol = pending
            self._remember_endpoint(base, key)
        self._rows = result.models
        # 新一批模型，旧的勾选不再对应任何行；留着会让「已勾选 N 个」对不上列表
        self._checked.clear()
        self._fill(result.models)
        n, withctx = len(result.models), result.with_context
        self._status(f"{result.status} OK · {n} 个模型 · {result.endpoint}", OK)
        if withctx:
            self.var_ctx_hint.set(f"{withctx}/{n} 个模型披露了上下文长度；其余留空，导入时按你选的填。")
        else:
            self.var_ctx_hint.set("该端点未披露任何上下文长度（标准 /models 只返回 id），导入时按你选的填。")

    def _visible(self) -> list[mp.Model]:
        """按搜索词与筛选模式算出当前可见行。

        搜索词按空格切分做 AND：`claude opus` 命中 id/name/note 同时包含两者的行。
        """
        mode = self.var_filter_mode.get() or FILTER_ALL
        keywords = [k for k in self.var_filter.get().lower().split() if k]
        out: list[mp.Model] = []
        for m in self._rows:
            res = self._speed.get(ms.norm_id(m.id))
            if mode == "已测速" and res is None:
                continue
            if mode == "未测速" and res is not None:
                continue
            if mode == SLOW_LABEL and not (res is not None and res.too_slow):
                continue
            if keywords:
                haystack = f"{m.id} {m.name} {m.note}".lower()
                if not all(k in haystack for k in keywords):
                    continue
            out.append(m)
        return out

    def _schedule_fill(self) -> None:
        """筛选框的防抖重绘。

        原来每按一个键就 `_fill()` 一次：整表 delete + 逐行 insert，且每行还要
        查一次价格。165 个模型时单次约 60–120 ms，输入法连打会积压成明显卡顿；
        模型上千时直接进入秒级。这里攒到停顿后再画，语义不变、手感变稳。
        下拉框（模式切换）仍走即时重绘——那是单次点击，不需要防抖。
        """
        if self._fill_after is not None:
            try:
                self.after_cancel(self._fill_after)
            except Exception:  # noqa: BLE001 - 已被执行或窗口正在销毁
                pass
        self._fill_after = self.after(FILTER_DEBOUNCE_MS, self._run_fill)

    def _run_fill(self) -> None:
        self._fill_after = None
        self._fill()

    def _clear_filter(self) -> None:
        self.var_filter.set("")
        self.var_filter_mode.set(FILTER_ALL)
        self._fill()

    def _focus_filter(self) -> None:
        """Ctrl+F：跳到筛选框并全选，直接敲关键词即可覆盖旧词。"""
        self.tabbar.select(0)
        self.ent_filter.focus_set()
        self.ent_filter.select_range(0, "end")
        self.ent_filter.icursor("end")

    def _ttft_label(self, res: mp.SpeedResult | None) -> str:
        """首字延迟统一用秒：毫秒数在横向比较时要数位数，秒更直观。"""
        if res is None:
            return ms.UNSET_LABEL
        if res.too_slow:
            return ms.SKIP_LABEL
        if res.ttft_ms is None:
            return "失败" if res.error else ms.UNSET_LABEL
        if res.partial:
            return f"≈{format_seconds(res.ttft_ms)}"
        return format_seconds(res.ttft_ms)

    def _tps_label(self, res: mp.SpeedResult | None) -> str:
        """生成速率。样本太短时前面加「≈」——那个数主要反映端点冲刷节奏。"""
        if res is None or res.tps is None:
            return ms.UNSET_LABEL
        if not res.reliable:
            return f"≈{res.tps:.1f}"
        return f"{res.tps:.1f}"

    def _state_text(self, state: str) -> str:
        return SPEED_STATES.get(state, (ms.UNSET_LABEL, MUTED))[0]

    def _state_of(self, key: str) -> str:
        """某模型当前状态：状态表优先，缺失时按已有的测速结果反推。

        两张表理论上同进同退（开始测速时一起重置、出结果时一起写）。留这条兜底
        是因为「有数值却没有状态」比两者都空更让人困惑——那会显示成
        「90 ms / 42.0 / —」，看起来像状态列坏了。
        """
        state = self._speed_state.get(key, "")
        if state:
            return state
        res = self._speed.get(key)
        if res is None:
            return ""
        if res.too_slow:
            return "slow"
        return "done" if res.ok else "error"

    def _refresh_sort_indicators(self) -> None:
        """给可排序表头加箭头：↕ 可点未激活 / ↑ 升序 / ↓ 降序。

        勾选框那列的表头由 `_update_head_check` 独占（显示 ☐/☑/◪），这里不碰它。
        """
        for col, base in SORT_HEADERS.items():
            if col == self._sort_col:
                mark = SORT_MARK_DESC if self._sort_desc else SORT_MARK_ASC
            else:
                mark = SORT_MARK_IDLE
            self.tree.heading(col, text=f"{base} {mark}")

    def _sync_import_buttons(self) -> None:
        """勾选变化后的统一收口：导入按钮与测速按钮的可用状态都在这里定。

        导入按钮的启用条件是 **勾了模型** 且 **对应 Agent 装在这台机器上**；
        只勾选模型是不够的——目标 Agent 没装的话，导进去等于往空气里写文件。

        测速按钮顺带把数量写进文案（`测首字与速度(4)`）：勾选框在表格最左列，
        勾了几个要自己数；写进按钮就不必数了，也不必等跑完才发现测错了范围。
        """
        if not hasattr(self, "btn_import"):
            return
        picked = len(self._checked)
        for spec in agents.AGENTS:
            btn = self.btn_import.get(spec.kind)
            if btn is None:
                continue
            usable = picked > 0 and self._agent_ok.get(spec.kind, False)
            btn.state(["!disabled"] if usable else ["disabled"])
        self._sync_speed_button(picked)

        installed = [spec.label for spec in agents.AGENTS
                     if self._agent_ok.get(spec.kind, False)]
        if picked and not installed:
            self.var_import_hint.set("未检测到 " + " / ".join(
                spec.label for spec in agents.AGENTS) + "，无法导入")
        elif picked:
            self.var_import_hint.set(f"已勾选 {picked} 个模型")
        else:
            self.var_import_hint.set("先勾选要导入的模型")

    def _sync_speed_button(self, picked: int) -> None:
        """测速按钮：0 个时置灰不可点，>0 时把数量写进文案。

        置灰而不是「点了再弹提示」：没有目标的按钮本来就没有意义，
        让它看起来能点就得多一次空点击。

        测速进行中（`_speeding`）必须保持禁用，且**不改文案**——此时文案里
        已经没有意义再跟勾选数走：用户可能在测速过程中改勾选，
        按钮跟着跳数字会让人以为「本次测速的范围变了」，而实际范围在开跑那刻
        就固定了。
        """
        if not hasattr(self, "btn_speed"):
            return
        if self._speeding:
            self.btn_speed.configure(text=SPEED_BUTTON_TEXT)
            self.btn_speed.state(["disabled"])
            return
        self.btn_speed.configure(
            text=f"{SPEED_BUTTON_TEXT}({picked})" if picked else SPEED_BUTTON_TEXT
        )
        self.btn_speed.state(["!disabled"] if picked else ["disabled"])

    def _update_speed_progress(self) -> None:
        """测速行的右侧小结：待测 / 测试中 / 完成 / 超限 / 异常 各多少条。

        只统计**本轮目标**：非本轮模型的旧结果仍留在表里（不该被清），
        但它们不属于这次测速，算进小结会与状态栏的 `n/total` 对不上。
        """
        counts: dict[str, int] = {}
        for key in self._speed_target:
            state = self._speed_state.get(key)
            if state:
                counts[state] = counts.get(state, 0) + 1
        parts = []
        for key, (text, _color) in SPEED_STATES.items():
            if counts.get(key):
                # 进度里不需要「测试中…」的省略号，那是表格里表示「正在进行」的
                parts.append(f"{text.rstrip('…')} {counts[key]}")
        self.var_speed_progress.set(" · ".join(parts))

    # ---------------- 勾选框 ----------------
    def _on_tree_click(self, event):
        """点第一列切换勾选；点其他列不动它（保留 Treeview 原生的行高亮）。"""
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        if self.tree.identify_column(event.x) != "#1":
            return None
        iid = self.tree.identify_row(event.y)
        if iid:
            self._toggle_group(iid)
        return "break"      # 别让这次点击顺带改掉行高亮（框选的那几行要留着）

    def _on_tree_double(self, event):
        """双击一行 = 切换勾选：第一次勾上，再双击取消。

        双击勾选列本身也安全：那两下 `<Button-1>` 会各 toggle 一次回到原状，
        再由这里的 `Double-1` toggle 一次，净效果仍是「切换一次」。
        """
        if self.tree.identify_region(event.x, event.y) != "cell":
            return None
        iid = self.tree.identify_row(event.y)
        if not iid:
            return None
        self._toggle_group(iid)
        return "break"

    def _toggle_group(self, iid: str) -> None:
        """切换勾选；**被框选的多行整组一起翻**。

        体感目标：拖选/Shift 选出一批模型后，点其中任意一行的勾选框，等于对这批
        「全选 / 全不选」；只点单独一行时仍只影响这一行。判断的是「这一组是不是
        已经全勾上了」：全勾 → 整组取消；否则 → 整组勾上（部分勾选时补全，
        而不是把已勾的翻掉，那样按一下反而越点越乱）。
        """
        selected = [i for i in self.tree.selection() if self.tree.exists(i)]
        rows = selected if (len(selected) > 1 and iid in selected) else [iid]
        keys = [ms.norm_id(i) for i in rows]
        target = not all(k in self._checked for k in keys)
        for key in keys:
            if target:
                self._checked.add(key)
            else:
                self._checked.discard(key)
        # 逐行刷新而不是整表 _fill()：整表重绘会把框选丢掉，连点两下就散了
        for row in rows:
            self._refresh_row(row)
        self._update_head_check()
        self._sync_import_buttons()

    def _toggle_all(self) -> None:
        """表头勾选框：当前可见行全选 / 全不选。

        只作用于可见行——筛选之后点全选，不该把被筛掉的行也一起勾上，
        那种「看不见的选中」最容易在测速时白花钱。
        """
        keys = [ms.norm_id(m.id) for m in self._view]
        if not keys:
            return
        if all(k in self._checked for k in keys):
            for k in keys:
                self._checked.discard(k)
        else:
            self._checked.update(keys)
        self._fill()

    def _update_head_check(self) -> None:
        keys = [ms.norm_id(m.id) for m in self._view]
        picked = sum(1 for k in keys if k in self._checked)
        if not keys or not picked:
            mark = "☐"
        elif picked == len(keys):
            mark = "☑"
        else:
            mark = "◪"          # 部分勾选
        self.tree.heading("pick", text=mark)

    def _price(self, m_id: str) -> pb.Price:
        """查一条价格（首次调用时载入价格表）。查不到返回空 Price，界面显示 —。"""
        if self._price_data is None:
            self._price_data = pb.load()
        return pb.lookup(m_id, self._price_data)

    def _row_values(self, m_id: str) -> tuple:
        """一行要显示的六个单元格，按勾选、价格与测速状态算出来。"""
        key = ms.norm_id(m_id)
        res = self._speed.get(key)
        return (
            "☑" if key in self._checked else "☐",
            m_id,
            self._price(m_id).text(),
            self._ttft_label(res),
            self._tps_label(res),
            self._state_text(self._state_of(key)),
        )

    def _refresh_row(self, m_id: str) -> None:
        """只重画一行。

        测速过程中每完成一条就整表重绘会闪、还会丢掉用户的选择，
        所以状态推进走单行更新，不做 `_fill()`。
        """
        if not self.tree.exists(m_id):
            return
        state = self._state_of(ms.norm_id(m_id))
        self.tree.item(m_id, values=self._row_values(m_id),
                       tags=(state,) if state in SPEED_STATES else ())

    def _fill(self, rows: list[mp.Model] | None = None) -> None:
        """重绘探测表。rows 传 None 表示沿用现有全量结果（仅过滤条件变化时用）。"""
        if rows is not None:
            self._rows = rows
        view_rows = self._visible()
        self._view = view_rows
        # 勾选跟着可见性走：被筛掉的行从 UI 上消失了，就不能继续算「选中」。
        # 否则会出现「表里只有 4 行、状态栏写着已勾选 15 个、测速却跑了 15 个」——
        # 看不见的选中在测速时是要真花钱的，宁可让用户重新勾，也不替他在暗中花钱。
        visible_keys = {ms.norm_id(m.id) for m in view_rows}
        if self._checked - visible_keys:
            self._checked &= visible_keys
        self.tree.delete(*self.tree.get_children())
        seen: set[str] = set()
        for m in view_rows:
            if m.id in seen:
                continue
            seen.add(m.id)
            key = ms.norm_id(m.id)
            state = self._state_of(key)
            self.tree.insert(
                "", "end", iid=m.id,
                values=(
                    "☑" if key in self._checked else "☐",
                    m.id,
                    self._price(m.id).text(),
                    self._ttft_label(self._speed.get(key)),
                    self._tps_label(self._speed.get(key)),
                    self._state_text(state),
                ),
                tags=(state,) if state in SPEED_STATES else (),
            )
        total = len({m.id for m in self._rows})
        self.var_count.set(f"显示 {len(view_rows)} / {total}" if total else "")
        self._update_head_check()
        self._sync_import_buttons()

    def _sort_by(self, col: str) -> None:
        """点表头排序。首字 / 速度 / 状态三列都支持，再点一次反序。

        关键约定：**没有数据的行永远排最后**，升降序都如此。用 `reverse=True` 直接
        翻整个列表会把「未测速」的行翻到最上面——那正是最不想先看到的东西。
        所以拿一个「分组位」参与排序：0 = 有数据、1 = 没有，反序时只翻数值不翻分组。
        """
        if col not in SORT_HEADERS:
            return
        if self._sort_col == col:
            self._sort_desc = not self._sort_desc
        else:
            self._sort_col = col
            self._sort_desc = False
        # 箭头先刷新：即使当前一行都没有，点了表头也该立刻看到升/降变化
        self._refresh_sort_indicators()
        if not self._rows:
            return

        def rank(m: mp.Model) -> tuple[int, float]:
            key = ms.norm_id(m.id)
            res = self._speed.get(key)
            if col == "status":
                state = self._state_of(key)
                if state not in SPEED_STATES:
                    return (1, 0.0)
                # 用字典顺序当「语义顺序」：待测试 → 测试中 → 完成 → 超时放弃 → 异常
                return (0, float(list(SPEED_STATES).index(state)))
            if col == "ttft":
                if res is None or res.too_slow or res.ttft_ms is None:
                    return (1, 0.0)
                return (0, float(res.ttft_ms))
            if col == "tps":
                if res is None or res.too_slow or res.tps is None:
                    return (1, 0.0)
                return (0, float(res.tps))
            if col == "price":
                price = self._price(m.id)
                if not price.known:
                    return (1, 0.0)
                return (0, float(price.sort_key() or 0.0))
            return (0, 0.0)

        if col == "id":
            self._rows.sort(key=lambda m: m.id.lower(), reverse=self._sort_desc)
        else:
            sign = -1.0 if self._sort_desc else 1.0
            self._rows.sort(key=lambda m: (rank(m)[0], rank(m)[1] * sign))
        self._fill()

    # ---------------- 测速 ----------------
    def _start_speed(self, models: list[mp.Model] | None = None) -> None:
        """对选中的模型并行实测首字与速度。

        `models` 显式给出目标时只测这一批（右键菜单走这条路，只测被点的那一个模型）；
        传 None 才回落到勾选集合。两条路径共用同一套并发与状态机，不复制逻辑。

        并行而非串行：单次测量绝大部分时间在等网络（等排队、等首字），
        串行测 30 个模型意味着 30 次首字等待排队叠加。并发默认 8 路，
        上限 16；再高会争抢带宽/触发中转站限流，反而把被测指标测成拥塞值。
        首字超过 `mp.SPEED_TTFT_LIMIT_MS`（10 秒）的模型直接放弃并显示「-」。

        开始前只清**本轮目标**的旧记录，非目标模型的结果原样保留：分多次测不同模型时，
        前一批的结果是已经花过 token 换来的，一并擦掉等于白测。
        """
        if self._busy or self._speeding:
            return
        picked = list(models) if models is not None else self._selected()
        if not picked:
            messagebox.showinfo(mp.APP_TITLE,
                                "先勾选要测速的模型（点列表左侧的勾选框，或点表头全选）。")
            return
        base = self.var_base.get().strip()
        if not base:
            messagebox.showwarning(mp.APP_TITLE, "请先填写 Base URL，测速需要它来构造对话端点。")
            return
        rounds = int(self.var_speed_rounds.get() or mp.SPEED_DEFAULT_ROUNDS)
        max_tokens = int(self.var_speed_tokens.get() or SPEED_TOKEN_DEFAULT)
        workers = int(self.var_speed_workers.get() or mp.SPEED_DEFAULT_WORKERS)
        proto = self._protocol()

        ids = [m.id for m in picked]
        # 不再弹「将消耗 N token，继续？」——每次点都要确认一遍太碍事。
        # 代价写进状态栏，让它在测速过程中一直可见，而不是弹一次就消失。
        #
        # 只清本轮目标模型，别人的结果一律留着：先测 A 再测 B 时，A 的
        # 首字/速度是已经花过 token 换来的，把它一起擦成「-」等于让用户重测一遍。
        # 清空是为了「这次没测」不被误读成「测过且很快」，这个理由只对**本轮要测的**
        # 那几条成立——非目标模型的旧数值是真实测过的，留着才不误导。
        target_keys = {ms.norm_id(mid) for mid in ids}
        self._speed_target = target_keys      # 进度小结只认这一批
        for key in target_keys:
            self._speed.pop(key, None)
            self._speed_state.pop(key, None)
        # 本轮目标全部先置「待测试」：并发只有 8 路，剩下的在排队，
        # 让用户一眼看出「这批里有几个还没轮到」。
        self._speed_state.update({key: "pending" for key in target_keys})
        self._fill()              # 让表格立刻回到统一状态并染上待测色
        self._update_speed_progress()
        self._speeding = True
        self._speed_cancel = False
        self._speed_ok = self._speed_fail = self._speed_slow = 0
        self._speed_notes = []
        self.btn_speed.state(["disabled"])
        self._sync_speed_button(len(self._checked))
        self.btn_go.state(["disabled"])
        self._status(
            f"正在测速：0/{len(ids)} · {workers} 路并发 · 每个约 {rounds * max_tokens} output token",
            ACCENT,
        )
        self.var_ctx_hint.set("")
        args = (proto, base, self.var_key.get(), mp.DEFAULT_TIMEOUT,
                self.var_proxy.get(), rounds, max_tokens, workers, ids)
        threading.Thread(target=self._speed_worker, args=args, daemon=True).start()

    def _speed_worker(self, protocol, base, key, timeout, proxy, rounds, max_tokens,
                      workers, ids) -> None:
        """并行测速。结果逐条回传，界面边测边刷新。

        全部读取共享的 `self._speed_cancel` 做协作式取消：已提交的任务会跑完
        （请求已发出，强行中断也省不下 token），但不再启动新的。
        """
        total = len(ids)

        def emit(result: mp.SpeedResult, n: int, _total: int) -> None:
            self._queue.put(("speed-done", result))
            self._queue.put(("speed-progress",
                             f"正在测速：{n}/{total} 路完成 · 最近 {result.model} …"))

        def started(mid: str) -> None:
            # 真正被线程取走才回调，所以「测试中」只落在并发窗口里那几个模型上，
            # 排队中的仍是「待测试」——这才是并行测速真实的形态。
            self._queue.put(("speed-start", mid))

        try:
            mp.probe_speed_many(
                protocol, base, key, ids, timeout=timeout, use_proxy=proxy,
                rounds=rounds, max_tokens=max_tokens, workers=workers,
                on_result=emit, on_start=started, should_stop=lambda: self._speed_cancel,
            )
        except Exception as e:  # noqa: BLE001 - 批量层兜底，别让窗口卡在「测速中」
            self._queue.put(("speed-note", f"测速中断：{e}"))
        self._queue.put(("speed-finish", total))

    def _apply_speed(self, result: mp.SpeedResult) -> None:
        """记录单个模型的测速结果，并把该行从「测试中」翻成终态。"""
        key = ms.norm_id(result.model)
        self._speed[key] = result
        if result.too_slow:
            self._speed_slow += 1
            self._speed_state[key] = "slow"
        elif result.ok:
            self._speed_ok += 1
            self._speed_state[key] = "done"
        else:
            self._speed_fail += 1
            self._speed_state[key] = "error"
            # 失败原因必须露出来：只写「失败」两个字，用户只能去站点翻日志对时间
            if result.error:
                self._speed_notes.append(f"{result.model}：{result.error}")
        for note in result.warnings:
            self._speed_notes.append(f"{result.model}：{note}")
        self._refresh_row(result.model)
        self._sync_agent_latency(result.model, result)
        self._update_speed_progress()

    def _sync_agent_latency(self, model_id: str, res: mp.SpeedResult | None) -> None:
        """探测页测完一个模型，把各 Agent 管理页里同名行的「延迟」一起刷新。

        两个页面看的是同一个模型：探测页显示 0.87 秒、管理页还空着，会让人以为
        后者没测过。只认首字延迟（ttft_ms），没有首字的（降级为整包计时）不写，
        不拿总耗时冒充首字。
        """
        if res is None or res.ttft_ms is None:
            return
        # 单位统一成秒：探测页显示的就是秒，管理页再写毫秒，同一模型两个数会看着矛盾
        text = format_seconds(res.ttft_ms)
        base = self.var_base.get().strip()
        key = self.var_key.get().strip()
        for kind in agents.kinds():
            # 先按「端点 + 令牌 + 模型」精确匹配；探测页没填 Key 时放宽到端点 + 模型，
            # 免得因为少一个输入框就一行都不更新。
            iids = self._row_iids(kind, base, key, model_id)
            if not iids and not key:
                iids = self._row_iids(kind, base, "", model_id, ignore_key=True)
            tree = self._agent_tree(kind)
            for iid in iids:
                if tree.exists(iid):
                    tree.set(iid, "latency", text)
                    self._route_cells.setdefault(
                        self._row_key(base, key, model_id), {})["latency"] = text

    def _finish_speed(self, total: int) -> None:
        self._speeding = False
        # 走统一入口而不是无条件启用：测速期间用户可能已经清空了勾选，
        # 那样按钮应当回到置灰态，而不是变成可点却无目标。
        self._sync_speed_button(len(self._checked))
        self.btn_go.state(["!disabled"])
        parts = [f"测速完成 {self._speed_ok}/{total}"]
        if self._speed_slow:
            parts.append(f"{SLOW_LABEL} {self._speed_slow}（已显示「-」）")
        if self._speed_fail:
            parts.append(f"失败 {self._speed_fail}")
        # 样本太短的速率前面会加「≈」，这里说明那些「≈」是怎么来的。
        # 同样只看本轮目标：非本轮模型的「≈」不是这次测出来的，报出来没有意义。
        shaky = sum(1 for key in self._speed_target
                    for r in (self._speed.get(key),)
                    if r is not None and r.tps is not None and not r.reliable)
        if shaky:
            parts.append(f"速率存疑 {shaky}（样本偏短，标「≈」）")
        color = OK if not (self._speed_fail or self._speed_slow) else WARN
        self._status(" · ".join(parts), color)
        self._update_speed_progress()
        if self._speed_notes:
            self.var_ctx_hint.set("；".join(self._speed_notes[:3])
                                  + ("…" if len(self._speed_notes) > 3 else ""))
            self._speed_notes = []

    def _drain(self) -> None:
        """统一消费后台线程的消息。

        队列里有两类消息，靠类型区分：
          * `ProbeResult` —— 模型列表探测
          * `("speed-*", payload)` —— 测速进度 / 单模型结果 / 全部完成
        """
        try:
            while True:
                item = self._queue.get_nowait()
                if isinstance(item, tuple) and item and isinstance(item[0], str) \
                        and item[0].startswith(("speed-", "route-", "quick-")):
                    kind, payload = item
                    if kind == "route-stage":
                        self._status(str(payload), ACCENT)
                    elif kind == "route-done":
                        self._finish_route(*payload)
                    elif kind == "speed-progress":
                        self._status(payload)
                    elif kind == "speed-start":
                        self._speed_state[ms.norm_id(str(payload))] = "running"
                        self._refresh_row(str(payload))
                        self._update_speed_progress()
                    elif kind == "speed-done":
                        self._apply_speed(payload)
                    elif kind == "speed-note":
                        self._speed_notes.append(str(payload))
                    elif kind == "speed-finish":
                        self._finish_speed(payload)
                    elif kind == "quick-running":
                        agent_kind, base, api_key, model_id = payload
                        # 只改状态：文案由 _latency_cell 推出来，不往缓存里塞「测试中…」，
                        # 否则中途取消会把这个临时文案当成测速结果留下来。
                        self._set_route_cells(agent_kind, base, api_key, model_id,
                                              state="running")
                    elif kind == "quick-progress":
                        self._status(str(payload), ACCENT)
                    elif kind == "quick-done":
                        self._apply_quick_speed(*payload)
                else:
                    self._apply(item)
        except queue.Empty:
            pass
        self.after(120, self._drain)

    # ---------------- 选择 ----------------
    def _selected(self) -> list[mp.Model]:
        """勾选框选中、且当前在列表里可见的模型（不是 Treeview 的高亮行）。

        只认可见行：筛选把某几行刷掉之后，它们既看不到也不该继续参与导入 / 测速。
        `_fill()` 已经把勾选集裁到可见范围，这里再按 `_view` 兜一道，防的是
        「筛选条件没变、但 `_view` 还没重算」这类中间态被别的入口直接调用。
        """
        picked: list[mp.Model] = []
        seen: set[str] = set()
        for m in self._view:
            key = ms.norm_id(m.id)
            if key in self._checked and key not in seen:
                seen.add(key)
                picked.append(m)
        return picked

    def _row_model(self, iid: str) -> mp.Model | None:
        """按 Treeview 的 iid 取回对应的模型对象（找不到返回 None）。"""
        for m in self._view:
            if m.id == iid:
                return m
        return None

    def _build_row_menu(self, model: mp.Model) -> tk.Menu:
        """构建右键菜单（只构建，不弹出），供 `_row_menu` 与测试共用。

        **刻意不使用 `state="disabled"` 做标题行**：Windows 下的 Tk 用点阵
        （stipple）绘制禁用菜单项的文字，深色主题上就表现为笔画破碎、发虚，
        看上去像字体被压糊了。改成 normal 态 + 把 active 前景/背景都设成与静止态
        相同，视觉上是一行不可点的标题，文字仍然走正常的抗锯齿渲染。

        同理，「测速进行中」也不再置灰，而是改文案——置灰同样会被点阵化。
        """
        menu = tk.Menu(self, tearoff=0, bg=PANEL, fg=FG, activebackground=ACCENT,
                       activeforeground="#ffffff", disabledforeground=MUTED, borderwidth=0)

        # 首行是被右键的模型 ID：菜单里全部动作都只作用于它，先把它摆在眼前，
        # 免得「右键了两下、菜单还停在上一次那个模型」时看不清作用对象。
        menu.add_command(label=model.id, command=lambda: None)
        menu.entryconfigure(0, foreground=MUTED, activeforeground=MUTED,
                            activebackground=PANEL)   # 悬停不变色 = 读起来是标题
        menu.add_separator()
        for spec in agents.AGENTS:
            menu.add_command(label=f"{spec.button_label}…",
                             command=lambda k=spec.kind: self._import_to(k, [model]))
        menu.add_separator()
        # 测速进行中改文案而不是置灰：`_start_speed` 本来就忙时直接返回，
        # 文案说清「为什么点了没用」，比灰掉一条看不清字还让人怀疑程序卡了要好。
        busy = self._busy or self._speeding
        menu.add_command(label="测速（进行中…）" if busy else "测速",
                         command=lambda: self._start_speed([model]))
        return menu

    def _row_menu(self, event) -> None:
        """右键：先做一次「左键单击」的高亮，再对**被点的这一个**模型弹菜单。

        与勾选框彻底解耦：右键不改 `_checked`，菜单里每个动作都只作用于这一行
        （见 `_import_to` / `_start_speed` 的 models 参数）。勾选是「批量」，
        右键是「单点」——两者混在一起时，用户点右键只想修一个模型，却被顺手勾上、
        再被批量测速带走，而那是要真花 token 的。

        点在空白处不弹菜单，也不动已有的高亮。
        """
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        model = self._row_model(iid)
        if model is None:
            return

        # 复现左键单击的观感：独占高亮这一行（清掉之前的多选），必要时滚动到可见
        self.tree.selection_set(iid)
        self.tree.focus(iid)
        self.tree.see(iid)
        # tk_popup 在 Windows 上会阻塞到菜单关闭，主窗口这期间不重绘；
        # 先强制刷一次，让「右键即高亮」在菜单出现之前就落到屏幕上。
        self.update_idletasks()

        menu = self._build_row_menu(model)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ---------------- 导入到 Agent ----------------
    def _require_agent(self, kind: str) -> bool:
        """写操作前的守卫：目标 Agent 没装就别往下走。

        按钮已经置灰了，这里防的是快捷键、双击、右键菜单这些绕过按钮的入口——
        统一从函数入口挡一道，比在每个绑定上分别判断可靠。
        """
        if self._agent_ok.get(kind, False):
            return True
        _ok, detail = agents.get(kind).status_fn()
        messagebox.showwarning(
            mp.APP_TITLE,
            f"{detail}\n\n装好并启动过一次之后，点对应页面的「刷新」重新检测。",
        )
        return False

    def _import_to(self, kind: str, models: list[mp.Model] | None = None) -> None:
        """把选中的模型写进对应 Agent 的配置文件（写前弹配置对话框）。

        `models` 显式给出目标时只导这一批（右键菜单走这条路，只导被点的那一个模型）；
        传 None 才回落到勾选集合。
        """
        spec = agents.get(kind)
        if not self._require_agent(kind):
            return
        picked = list(models) if models is not None else self._selected()
        if not picked:
            # 按钮在没勾选时是灰的，能走到这里只可能是脚本调用
            messagebox.showinfo(mp.APP_TITLE, "先勾选要导入的模型（点列表左侧的勾选框）。")
            return
        base = self.var_base.get().strip()
        if not base:
            messagebox.showwarning(mp.APP_TITLE, "请先填写 Base URL，导入需要它来生成端点。")
            return
        if not spec.supports_protocol(self._protocol()):
            messagebox.showwarning(
                mp.APP_TITLE,
                f"{spec.label} 不支持协议「{self._protocol()}」，请换一个协议再导入。",
            )
            return

        # 单选时预填目标配置里该模型的现值；多选时用默认规格统一应用。
        existing = spec.find_existing(picked[0].id) if len(picked) == 1 else None
        if existing is not None:
            # 同名模型可能分布在多个 provider 上（不同令牌各一份）。此时「目标的现值」
            # 并不唯一，拿任意一条的 Key 当默认值会让用户以为在改 A、其实改了 B——
            # 退回探测页填的那把 Key，导入落到哪由 Key 决定，语义才确定。
            same_id = [r for r in self._agents.get(kind, [])
                       if ms.norm_id(r.model_id) == ms.norm_id(picked[0].id)]
            if len(same_id) > 1:
                existing = None
        # Key 的预填顺序：探测页刚填的那把 ＞ 目标配置里的现值。
        # 反过来会把「我用令牌 B 导这个模型」悄悄变成「再用令牌 A 导一次」——
        # 导入落到哪个 provider 由 Key 决定，Key 就不能替用户换成上一把。
        probe_key = self.var_key.get().strip()
        dialog = ModelConfigDialog(
            self,
            title=(f"导入到 {spec.label}" if len(picked) == 1
                   else f"导入到 {spec.label}（{len(picked)} 个模型）"),
            agent_label=spec.label,
            model_id=picked[0].id,
            model_name=picked[0].name or picked[0].id,
            base_url=existing.base_url if existing and existing.base_url else base,
            api_key=probe_key or (existing.api_key if existing and existing.api_key else ""),
            spec=existing.spec if existing else ms.ModelSpec(
                # 上下文与输出上限优先用探测结果；图片沿用 ModelSpec 的默认（支持图片）
                context=next((m.context for m in picked if m.context), ms.DEFAULT_CONTEXT),
                max_tokens=next((m.max_out for m in picked if m.max_out), ms.DEFAULT_MAX_TOKENS),
            ),
            editable_id=False,
            model_ids=[m.id for m in picked],
            hidden_spec_fields=spec.hidden_spec_fields,
            spec_note=spec.spec_note,
        )
        self.wait_window(dialog)
        if dialog.result is None:
            return

        chosen = dialog.result
        specs = {ms.norm_id(m.id): chosen["spec"] for m in picked}

        # WorkBuddy 的模型清单以 id 唯一（同名只能一条），换 Key 时不要静默覆盖：
        # 用户以为是「再加一个」，实际是「把上一个改了」，这件事得他自己点头。
        if kind == "workbuddy" and chosen.get("api_key"):
            new_key = ai.bare_key(chosen["api_key"])
            targets = {ms.norm_id(m.id) for m in picked}
            clash = sorted({r.model_id for r in self._agents.get(kind, [])
                            if ms.norm_id(r.model_id) in targets and r.api_key
                            and ai.bare_key(r.api_key) != new_key})
            if clash and not messagebox.askyesno(
                mp.APP_TITLE,
                "这些模型在 WorkBuddy 里已存在，但用的是另一把 Key：\n"
                f"{'、'.join(clash)}\n\n"
                "WorkBuddy 的模型清单以模型 id 唯一，同名模型只能有一条，"
                "继续会「替换」原有条目的 Key（不会变成两个模型）。\n\n"
                "要让两个令牌各留一份，请导入 DeepSeek Harness（它按 provider 分开存）。\n\n"
                "仍要继续替换吗？",
            ):
                return
        result = agents.call_import(
            spec, picked, specs,
            base_url=chosen["base_url"],
            api_key=chosen["api_key"],
            protocol=self._protocol(),
            key_label=self._key_label(chosen["base_url"], chosen["api_key"]),
        )
        self._report(result, kind)

    def _key_label(self, base_url: str, api_key: str) -> str:
        """给「同一地址的第二把 Key」取个可读标签，供 DSH 给新 provider 命名。

        优先用站点里的令牌名（a6-2），因为那正是用户在站内认得的名字；查询要凭证，
        没配或查不到就退回 Key 尾四位——有个能区分的名字就够了，不值得为它拦住导入。
        """
        key = (api_key or "").strip()
        if not key:
            return ""
        try:
            cred = sr.site_credentials(base_url) or {}
            token, uid = str(cred.get("access_token") or ""), str(cred.get("user_id") or "")
            if token and uid:
                control = str(cred.get("control_base") or "") or sr.guess_control_base(base_url)
                _tid, name, _plain = sr.resolve_token(control, token, uid, key)
                if name:
                    return name
        except Exception:  # noqa: BLE001 - 标签只是锦上添花，查不到不影响导入
            pass
        bare = key[3:] if key.lower().startswith("sk-") else key
        return f"key-{bare[-4:]}" if len(bare) >= 4 else ""

    def _report(self, result: ai.ImportResult, kind: str) -> None:
        """统一的写入结果反馈：摘要 + 备份位置 + 警告。"""
        self._refresh_agent(kind)
        if not result.ok:
            self._status(result.summary(), ERR)
            messagebox.showerror(mp.APP_TITLE, result.summary())
            return
        detail = [result.summary()]
        if result.key_written:
            detail.append(f"密钥：{result.key_written}")
        for backup in result.backups:
            detail.append(f"备份：{ai.backup_note(backup)}")
        if result.warnings:
            detail.append("")
            detail.extend(f"· {w}" for w in result.warnings)
        self._status(result.summary(), OK)
        messagebox.showinfo(mp.APP_TITLE, "\n".join(detail))

    # ---------------- Agent 管理页 ----------------
    def _set_agent_writable(self, kind: str, writable: bool) -> None:
        """未安装 / 读不了时，把写操作按钮全部置灰；「刷新」永远可点。"""
        btns = self._agent_buttons.get(kind) or {}
        state = ["!disabled"] if writable else ["disabled"]
        for key in ("add", "delete", "probe", "open", "active"):
            if key in btns:
                btns[key].state(state)
        # 还原还多一个前提：确实存在备份可还原
        if "restore" in btns:
            btns["restore"].state(["!disabled"] if writable and self._has_backup(kind)
                                  else ["disabled"])

    def _paths_of(self, kind: str) -> list[Path]:
        return agents.get(kind).all_paths()

    def _has_backup(self, kind: str) -> bool:
        """有没有可还原的快照。

        统一从注册表的 `all_paths()` 推：`ai.has_backup(kind)` 只认 workbuddy/dsh
        两个硬编码路径，新注册的 Agent 走它必然返回 False——表现为「明明写过、
        还原按钮却一直灰着」。
        """
        return ai.has_backup_paths(self._paths_of(kind))

    def _refresh_agent(self, kind: str) -> None:
        spec = agents.get(kind)
        tree = self._agent_tree(kind)
        hint = self._agent_hint_var[kind]
        label = self._agent_hint_label.get(kind)

        ok, detail = spec.status_fn()
        if not ok:
            # 没装：不当成错误，只是这一页暂时没东西可管
            self._agent_ok[kind] = False
            self._agents[kind] = []
            tree.delete(*tree.get_children())
            self._set_agent_writable(kind, False)
            hint.set(detail)
            if label is not None:
                label.configure(foreground=WARN)
            self._sync_import_buttons()
            return

        try:
            rows = spec.read()
        except (ai.AgentConfigError, RuntimeError) as e:
            # 装了但读不动：多半是格式坏了或缺依赖。仍然不给写——
            # 对着读不出来的配置做增删，等于盲改用户文件。
            self._agent_ok[kind] = False
            self._agents[kind] = []
            tree.delete(*tree.get_children())
            self._set_agent_writable(kind, False)
            hint.set(f"读取失败：{e}")
            if label is not None:
                label.configure(foreground=ERR)
            self._sync_import_buttons()
            return

        self._agent_ok[kind] = True
        self._agents[kind] = rows
        tree.delete(*tree.get_children())
        active_name = ""
        for row in rows:
            iid = agents.row_iid(spec, row)
            # 档位是连续区间，写成「off–xhigh」比逐档罗列更短，也不会被列宽截断
            levels = f"{row.spec.levels[0]}–{row.spec.levels[-1]}" if row.spec.reasoning else "不支持"
            values = [
                spec.head_of(row) if spec.head_of else row.base_url,
                row.model_id,
            ]
            if spec.can_set_active:
                is_active = bool(getattr(row, "is_active", False))
                if is_active:
                    active_name = row.model_id
                values.append("✓" if is_active else "")
            values += [
                ms.context_label(row.spec.context) or ms.UNSET_LABEL,
                "是" if row.spec.vision else "否",
                levels,
            ]
            row_key = self._route_key(spec, row)
            cells = self._route_cells.get(row_key, {})
            values += [cells.get("ratio", "—"),
                       cells.get("cache", "—"),
                       self._price(row.model_id).text(),
                       self._latency_cell(row_key, cells.get("latency", ""))]
            if getattr(row, "missing", False):
                # 悬空引用行：首列标警告，第二列直接说明原因——只把原因塞进 name
                # 字段是看不见的（表格显示的是 model_id），等于没提示。
                values[0] = f"⚠ {values[0]}"
                values[1] = f"{row.model_id}（默认模型不存在）"
                tree.insert("", "end", iid=iid, values=tuple(values),
                            tags=("missing",))
            else:
                # 重绘后颜色要跟着状态走，否则刷新一次列表，测速结果的颜色就没了
                row_state = self._quick_state.get(row_key, "")
                tree.insert("", "end", iid=iid, values=tuple(values),
                            tags=(row_state,) if row_state in SPEED_STATES else ())
        self._set_agent_writable(kind, True)
        extra = f" · 当前模型 {active_name}" if active_name else ""
        hint.set(f"{detail} · 共 {len(rows)} 个自定义模型{extra}"
                 + ("" if rows else "（还没有任何模型，点「添加模型」新建）"))
        if label is not None:
            label.configure(foreground=MUTED)
        self._sync_import_buttons()

    def _picked_agent(self, kind: str) -> list[ai.AgentModel]:
        spec = agents.get(kind)
        tree = self._agent_tree(kind)
        sel = set(tree.selection())
        out: list[ai.AgentModel] = []
        for row in self._agents.get(kind, []):
            if agents.row_iid(spec, row) in sel:
                out.append(row)
        return out

    def _add_agent_model(self, kind: str) -> None:
        spec = agents.get(kind)
        if not self._require_agent(kind):
            return
        dialog = ModelConfigDialog(
            self,
            title=f"添加到 {spec.label}",
            agent_label=spec.label,
            base_url=self.var_base.get().strip(),
            api_key=self.var_key.get(),
            spec=ms.ModelSpec(),
            editable_id=True,
            hidden_spec_fields=spec.hidden_spec_fields,
            spec_note=spec.spec_note,
        )
        self.wait_window(dialog)
        if dialog.result is None:
            return
        chosen = dialog.result
        stub = mp.Model(id=chosen["id"], name=chosen["name"])
        specs = {ms.norm_id(chosen["id"]): chosen["spec"]}
        result = agents.call_import(
            spec, [stub], specs,
            base_url=chosen["base_url"],
            api_key=chosen["api_key"],
            protocol=self._protocol(),
            key_label=self._key_label(chosen["base_url"], chosen["api_key"]),
        )
        self._report(result, kind)

    def _delete_agent_model(self, kind: str) -> None:
        spec = agents.get(kind)
        if not self._require_agent(kind):
            return
        picked = self._picked_agent(kind)
        if not picked:
            messagebox.showinfo(mp.APP_TITLE, "先在列表里选中要删除的模型。")
            return
        names = "、".join(r.model_id for r in picked[:4]) + ("…" if len(picked) > 4 else "")
        # 数量单位由注册表给：Claude Code 清空的是槽位，说「1 个模型」会让用户以为
        # 那把模型从配置里彻底消失了。
        if not messagebox.askyesno(
            mp.APP_TITLE,
            f"从 {spec.label} 的配置里{spec.delete_verb} {len(picked)} {spec.count_unit}？"
            f"\n{names}\n\n"
            "写入前会把当前配置快照到模型管家的备份目录（环形 5 份），可据此恢复。继续？",
        ):
            return
        self._report(spec.delete_fn(agents.delete_args(spec, picked)), kind)

    def _set_active_agent(self, kind: str) -> None:
        """把选中的模型设为该 Agent 的当前模型（仅 can_set_active 的 Agent 有）。"""
        spec = agents.get(kind)
        if not spec.can_set_active or spec.set_active_fn is None:
            return
        if not self._require_agent(kind):
            return
        picked = self._picked_agent(kind)
        if len(picked) != 1:
            messagebox.showinfo(mp.APP_TITLE, "请选中一个模型（只能设一个当前模型）。")
            return
        row = picked[0]
        if getattr(row, "missing", False):
            messagebox.showinfo(
                mp.APP_TITLE,
                f"「{row.model_id}」只存在于配置文件里，模型目录中没有它，无需设为当前模型。",
            )
            return
        if not messagebox.askyesno(
            mp.APP_TITLE,
            f"把「{row.model_id}」设为 {spec.label} 的当前模型？\n\n"
            "会改写配置文件顶层的默认模型字段，重启后生效。继续？",
        ):
            return
        self._report(spec.set_active_fn(row.model_id), kind)

    def _probe_agent_model(self, kind: str) -> None:
        """把配置里的模型送回令牌探测页：填好 BaseURL / Key / 协议并直接开测。"""
        spec = agents.get(kind)
        if not self._require_agent(kind):
            return
        picked = self._picked_agent(kind)
        if not picked:
            messagebox.showinfo(mp.APP_TITLE, "先在列表里选中要探测的模型。")
            return
        row = picked[0]
        if getattr(row, "missing", False):
            messagebox.showinfo(
                mp.APP_TITLE,
                f"「{row.model_id}」是默认模型，但模型目录里没有这一条。\n\n"
                "请先在模型目录里导入它，或用「设为当前模型」换成另一个已有模型。",
            )
            return
        if not row.base_url:
            messagebox.showwarning(mp.APP_TITLE, "这条配置里没有可用的 Base URL，无法探测。")
            return
        self.tabbar.select(0)
        proto = row.protocol
        if proto in mp.PROTOCOLS:
            self.cmb_protocol.current(list(mp.PROTOCOLS).index(proto))
        self.var_base.set(row.base_url)
        self.var_key.set(row.api_key)
        self._status(f"已从 {spec.label} 带入 {row.model_id} 的端点与密钥，正在探测 …", OK)
        self._start()

    def _restore_agent(self, kind: str) -> None:
        """用某一份快照覆盖当前配置，把那次修改撤掉。

        有多份快照时先让用户挑：连写错两次的情况下，「最新那份」也是错的，
        默认取最新的等于把用户推进第二次错误。
        """
        spec = agents.get(kind)
        if not self._require_agent(kind):
            return
        paths = self._paths_of(kind)
        multi = spec.multi_file_restore
        choices = ai.backup_choices_for(paths) if multi else ai.backup_choices(paths[0])
        if not choices:
            messagebox.showinfo(
                mp.APP_TITLE,
                f"还没有可还原的备份——先对 {spec.label} 做过一次添加或删除，才会产生备份。",
            )
            self._set_agent_writable(kind, True)
            return
        if len(choices) == 1:
            snapshot = choices[0][0]
        else:
            dialog = SnapshotDialog(self, agent_label=spec.label, choices=choices)
            self.wait_window(dialog)
            if dialog.result is None:
                return
            snapshot = dialog.result
        extra = (f"\n将同时恢复 {len(paths)} 个文件（按同一槽位对齐）。" if multi else "")
        if not messagebox.askyesno(
            mp.APP_TITLE,
            f"用这份快照覆盖 {spec.label} 的当前配置？\n\n"
            f"{ai.backup_note(snapshot)}{extra}\n\n"
            f"环形保留最近 {ai.BACKUP_SLOTS} 份，现在有 {len(choices)} 份。"
            "当前配置将被整份替换；快照不会被消耗，可以再还原一次。继续？",
        ):
            return
        if multi:
            result = ai.restore_slots(spec.label, paths, int(snapshot))
        else:
            # 必须显式给 path：`restore_backup` 缺省时按 kind 找路径，而那个函数
            # 只认得 workbuddy/dsh，新注册的 Agent 会被当成 DSH 写错文件。
            result = ai.restore_backup(kind, path=paths[0], snapshot=snapshot,
                                       label=spec.label)
        self._report(result, kind)

    # ---------------- 参考价格表 ----------------
    def _edit_prices(self) -> None:
        """打开参考价格表；关闭后重载并重绘，价格列立刻反映新数据。"""
        if self._price_data is None:
            self._price_data = pb.load()
        dialog = PriceBookDialog(self, data=self._price_data)
        self.wait_window(dialog)
        self._reload_prices()

    def _reload_prices(self) -> None:
        self._price_data = pb.load()
        pb.load_preset.cache_clear()   # 预设文件可能被换过（升级/手工替换）
        if self._rows:
            self._fill()
        # 管理页的「预估价格」读的是同一张表：不重画就会留着一屏旧价。
        # 只在页面已经建好之后刷新，避免启动期碰到还没创建的树。
        if getattr(self, "_agent_tree_ref", None):
            for kind in agents.kinds():
                if self._agents.get(kind):
                    self._refresh_agent(kind)

    def _open_config_dir(self, kind: str) -> None:
        target = agents.get(kind).primary_path()
        folder = target.parent
        try:
            folder.mkdir(parents=True, exist_ok=True)
            import os
            os.startfile(str(folder))  # noqa: S606 - Windows 资源管理器
        except Exception as e:  # noqa: BLE001
            messagebox.showinfo(mp.APP_TITLE, f"{folder}\n({e})")

    # ---------------- 配置（不含密钥） ----------------
    def _load_config(self) -> None:
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        proto = cfg.get("protocol")
        if proto in mp.PROTOCOLS:
            self.cmb_protocol.current(list(mp.PROTOCOLS).index(proto))
        if cfg.get("base"):
            self.var_base.set(cfg["base"])
        self.var_proxy.set(bool(cfg.get("proxy", False)))
        self.var_autov1.set(bool(cfg.get("auto_v1", True)))
        if str(cfg.get("speed_rounds", "")) in SPEED_ROUND_CHOICES:
            self.var_speed_rounds.set(str(cfg["speed_rounds"]))
        if str(cfg.get("speed_tokens", "")) in SPEED_TOKEN_CHOICES:
            self.var_speed_tokens.set(str(cfg["speed_tokens"]))
        if str(cfg.get("speed_workers", "")) in SPEED_WORKER_CHOICES:
            self.var_speed_workers.set(str(cfg["speed_workers"]))
        # 历史版本可能残留 key_b64 / 模型库字段：读到就丢弃，绝不回填到界面
        if cfg.get("key_b64"):
            self._status("检测到旧版残留的密钥字段，下次退出时会被清除。", WARN)

    def _save_config(self) -> None:
        """落盘界面偏好。**密钥不在这里**：它跟着 Base URL 存在
        `~/.model_probe/endpoints.json`（见 `endpoint_book`），
        两份文件分开，坏哪一份都不会连带毁掉另一份。"""
        cfg = {
            "protocol": self._protocol(),
            "base": self.var_base.get().strip(),
            "proxy": self.var_proxy.get(),
            "auto_v1": self.var_autov1.get(),
            "speed_rounds": int(self.var_speed_rounds.get() or mp.SPEED_DEFAULT_ROUNDS),
            "speed_tokens": int(self.var_speed_tokens.get() or SPEED_TOKEN_DEFAULT),
            "speed_workers": int(self.var_speed_workers.get() or mp.SPEED_DEFAULT_WORKERS),
        }
        try:
            CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _on_close(self) -> None:
        self._watch_stop.set()
        self._save_config()
        self.destroy()


def run() -> None:
    if sys.platform == "win32":
        try:  # 高分屏不糊
            import ctypes

            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    App().mainloop()


def main() -> None:
    run()


if __name__ == "__main__":
    main()
