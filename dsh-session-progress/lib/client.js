/**
 * dsh-session-progress — browser half.
 *
 * One contribution: a progress pill in the session header, always visible.
 * With a plan it renders the model's own steps (the Session's `todos`
 * projection, written by `todo_write`); without one it folds this turn's tool
 * calls into intent phases so the header still says what is happening.
 *
 * The step list is a second slot (`shell.overlay`), because the header clips
 * its own overflow. The two halves meet in one small store.
 */
window.__ModuleLoader__.load({
  id: "dsh-session-progress",
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;
    Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });

    const react = require("react");

    const STYLE_ID = "dsh-session-progress-style";

    const CSS = `
/* ── session progress pill ──────────────────────────────────────────────── */
/* One segment per step: the count of steps, and which one is running, are
   both readable at a glance without opening anything. */
.mwProgBtn {
  display: inline-flex;
  flex-direction: column;
  justify-content: center;
  align-items: stretch;
  gap: 3px;
  box-sizing: border-box;
  /* Fixed width and fixed slots. Every inner part renders on every frame, so a
     landing step or a new second only re-flows text inside the pill — never the
     row of header chips beside it. */
  flex: 0 1 208px;
  width: 208px;
  min-width: 0;
  height: 26px;
  padding: 2px 8px;
  border: 1px solid transparent;
  border-radius: var(--dsw-radius-sm, 6px);
  background: transparent;
  color: var(--dsw-alias-label-secondary);
  font: inherit;
  font-size: 11.5px;
  line-height: 1;
  white-space: nowrap;
  cursor: pointer;
}
.mwProgBtn:hover,
.mwProgBtn[data-open='true'] {
  background: var(--dsw-alias-interactive-bg-hover);
  color: var(--dsw-alias-label-primary);
}
.mwProgBtn:focus-visible {
  outline: none;
  box-shadow: 0 0 0 2px var(--dsw-focus-ring-color);
}
/* Two rows inside the height of one header chip: what is being worked on and
   the turn's elapsed time on top, the segmented bar underneath. Stacking is
   what makes the pill narrow — a bar beside the label spends width the label
   itself needs. */
.mwProgTop {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: none;
  min-width: 0;
  height: 12px;
}
.mwProgState {
  flex: 1 1 auto;
  min-width: 0;
  /* It grows into the space the fixed pill leaves over instead of sizing the
     pill: a short label would otherwise shrink the whole control. */
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--dsw-alias-label-secondary);
}
.mwProgBtn[data-busy='true'] .mwProgState {
  color: var(--dsw-alias-label-primary);
}
/* The bar spans the pill instead of sitting beside the label: at 4px it reads
   as a rule under the text, so nothing has to be reserved for it sideways. */
.mwProgTrack {
  display: flex;
  align-items: stretch;
  flex: none;
  width: 100%;
  height: 4px;
  overflow: hidden;
}
.mwProgBar {
  display: flex;
  align-items: stretch;
  gap: 2px;
  flex: 1 1 auto;
  min-width: 0;
  height: 4px;
}
.mwProgSeg {
  flex: 1 1 0;
  /* No floor: a long phase list must not push its neighbours out of the header
     row, so the segments shrink instead. */
  min-width: 0;
  border-radius: 2px;
  background: var(--dsw-alias-border-l2);
}
.mwProgSeg[data-state='completed'] {
  background: var(--dsw-alias-state-success-primary);
}
.mwProgSeg[data-state='in_progress'] {
  background: var(--dsw-alias-brand-primary);
  animation: mwProgPulse 1.4s ease-in-out infinite;
}
@keyframes mwProgPulse {
  0%,
  100% {
    opacity: 1;
  }
  50% {
    opacity: 0.4;
  }
}
@media (prefers-reduced-motion: reduce) {
  .mwProgSeg[data-state='in_progress'] {
    animation: none;
  }
}
/* Elapsed time of the turn: it ticks while the turn runs and freezes at the
   total the moment it ends. Tabular figures keep every second the same width,
   so the clock cannot jitter the text beside it. */
.mwProgTime {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  flex: none;
  font-variant-numeric: tabular-nums;
  color: var(--dsw-alias-label-tertiary);
}
.mwProgBtn[data-busy='true'] .mwProgTime {
  color: var(--dsw-alias-label-secondary);
}
.mwProgBtn[data-complete='true'] .mwProgTime {
  color: var(--dsw-alias-state-success-primary);
}
.mwProgIcon {
  display: block;
  flex: none;
}
/* Popover chrome. The class names are shared with other plugins' popovers, so
   every rule here is scoped to this popover: a package that can be installed
   alone must style its own shell instead of inheriting one that may not exist. */
.mwProgPop .mwPopBar {
  display: flex;
  flex: none;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--dsw-alias-border-l1);
}
.mwProgPop .mwPopTitle {
  font-size: 13px;
  line-height: 20px;
  font-weight: 500;
  color: var(--dsw-alias-label-primary);
}
.mwProgPop .mwSub {
  margin-top: 0;
  font-size: 12px;
  line-height: 18px;
  color: var(--dsw-alias-label-tertiary);
}
.mwProgPop .mwPopActions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}
.mwProgPop .mwPopTime {
  font-size: 12px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
  color: var(--dsw-alias-label-tertiary);
}
.mwProgPop .mwPopClose {
  width: 26px;
  height: 26px;
  padding: 0;
  border: 0;
  border-radius: var(--dsw-radius-sm, 6px);
  background: transparent;
  color: var(--dsw-alias-label-tertiary);
  font: inherit;
  font-size: 13px;
  line-height: 1;
  cursor: pointer;
}
.mwProgPop .mwPopClose:hover {
  background: var(--dsw-alias-interactive-bg-hover);
  color: var(--dsw-alias-label-primary);
}
.mwProgPop {
  position: absolute;
  display: flex;
  flex-direction: column;
  width: 340px;
  max-width: calc(100vw - 24px);
  max-height: min(60vh, 460px);
  overflow: hidden;
  border: 1px solid var(--dsw-alias-border-l2);
  border-radius: var(--dsw-radius-lg, 10px);
  background: var(--dsw-alias-bg-overlay, var(--dsw-alias-bg-layer-1));
  box-shadow: 0 12px 32px rgba(0, 0, 0, 0.28);
}
.mwProgPopBody {
  overflow: auto;
  padding: 4px 0 10px;
}
.mwProgGroup {
  padding: 6px 0 2px;
}
.mwProgGroupTitle {
  padding: 0 12px;
  font-size: 11px;
  line-height: 16px;
  letter-spacing: 0.02em;
  color: var(--dsw-alias-label-tertiary);
}
.mwProgRow {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 4px 12px;
  font-size: 12.5px;
  line-height: 18px;
  color: var(--dsw-alias-label-secondary);
}
.mwProgRow[data-state='in_progress'] {
  color: var(--dsw-alias-label-primary);
}
.mwProgRow[data-state='completed'] .mwProgText {
  color: var(--dsw-alias-label-tertiary);
  text-decoration: line-through;
}
/* A plan row is a checklist item, so finishing it strikes it out. A step of
   narrated progress is history, not a to-do — it keeps its full text. */
.mwProgLive .mwProgRow[data-state='completed'] .mwProgText {
  color: var(--dsw-alias-label-secondary);
  text-decoration: none;
}
.mwProgMark {
  flex: none;
  width: 12px;
  text-align: center;
  color: var(--dsw-alias-label-tertiary);
}
.mwProgRow[data-state='in_progress'] .mwProgMark {
  color: var(--dsw-alias-brand-primary);
}
.mwProgRow[data-state='completed'] .mwProgMark {
  color: var(--dsw-alias-state-success-primary);
}
.mwProgText {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}

/* The popover lives in the frame-wide overlay layer: the header clips, so it
   can never be a child of the button itself.
   The wrapper is a positioning context only — it must not become a full-screen
   hit target, or every hover and click underneath stops reaching the page.
   Dismissal is handled by a document-level pointerdown listener instead. */
.mwPopWrap {
  position: fixed;
  inset: 0;
  z-index: 60;
  pointer-events: none;
}
.mwProgPop {
  pointer-events: auto;
}
`;

    /** The bundle is one file: the stylesheet rides along. It is rewritten on
     * every apply, because a hot-reloaded module re-runs with the previous
     * stylesheet still in the document — keeping that one would leave the new
     * markup styled by the old rules. */
    function ensureStyle() {
      if (typeof document === "undefined") return;
      const selector = "style#" + STYLE_ID + ", style[data-dsh-session-progress]";
      const existing = typeof document.querySelectorAll === "function" ? document.querySelectorAll(selector) : [];
      let style = existing.length > 0 ? existing[0] : null;
      for (let index = 1; index < existing.length; index += 1) existing[index].remove();
      if (style === null) {
        style = document.createElement("style");
        style.id = STYLE_ID;
        if (typeof style.setAttribute === "function") style.setAttribute("data-dsh-session-progress", "");
        document.head.appendChild(style);
      }
      if (style.textContent !== CSS) style.textContent = CSS;
    }

    /* ── session progress bar ────────────────────────────────────────────── */

    /** The pill (header actions) and its step list (`shell.overlay`) are two
     * slot registrations, so they meet in one tiny store: the pill owns the
     * `todos` projection read and publishes the snapshot the overlay renders. */
    const progressStore = {
      open: false,
      rect: null,
      todos: [],
      busy: false,
      streaming: false,
      runningName: "",
      steps: [],
      totalSteps: 0,
      hiddenSteps: 0,
      doneSteps: 0,
      doneCount: 0,
      startedAt: 0,
      lastAt: 0,
      signature: null,
      listeners: new Set(),
    };

    /** Human label for one tool name; unknown names fall through verbatim. */
    const TOOL_LABELS = {
      read: "读取文件",
      write: "写入文件",
      edit: "编辑文件",
      apply_patch: "修改文件",
      glob: "查找文件",
      grep: "搜索代码",
      pwsh: "执行命令",
      todo_write: "更新任务清单",
      web_search: "联网搜索",
      web_fetch: "抓取网页",
      multi_search: "多引擎搜索",
      advanced_search: "检索",
      platform_search: "平台搜索",
      domain_search: "垂直检索",
      research: "调研",
      cache_search: "检索缓存",
      free_search_test: "检测搜索引擎",
      video_search: "搜索视频",
      weather: "查询天气",
      subagent: "派生子代理",
      subagent_fork: "派生子代理",
      send_message: "给协作代理发消息",
      list_agents: "查看协作代理",
      wait_agent: "等待协作代理",
      present: "交付文件",
      skill: "加载技能",
      compress: "压缩上下文",
      decompress: "展开上下文",
      acp_status: "查看上下文占用",
      memory_save: "记住一条",
      memory_search: "检索记忆",
      create_goal: "创建目标",
      update_goal: "更新目标",
      get_goal: "查看目标",
      ask_user_question: "向你提问",
      schedule_create: "创建定时任务",
      workflow: "编排多代理",
      job_output: "读取后台任务",
      job_list: "查看后台任务",
      job_kill: "终止后台任务",
      upload_image: "上传图片",
      read_image: "查看图片",
      mcp__tapd__get_todo: "读取 TAPD 待办",
    };

    function toolLabel(name) {
      if (typeof name !== "string" || name === "") return "";
      return TOOL_LABELS[name] ?? name;
    }

    /** Nodes of the current turn: everything after the last user message.
     * The trajectory keeps only a window of events, so when no user message
     * survives there is no turn boundary to trust — report nothing rather than
     * count every tool result left in the window. */
    function turnNodes(snapshot) {
      const nodes = snapshot?.eventNodes;
      if (!Array.isArray(nodes)) return [];
      for (let index = nodes.length - 1; index >= 0; index -= 1) {
        if (nodes[index]?.kind === "user") return nodes.slice(index + 1);
      }
      return [];
    }

    /** One line of plain text: markdown stripped, first sentence only. */
    function cleanLabel(raw) {
      if (typeof raw !== "string") return "";
      let text = raw
        .replace(/```[\s\S]*?```/g, " ")
        .replace(/`([^`]*)`/g, "$1")
        .replace(/[*_>#]+/g, " ")
        .replace(/\s+/g, " ")
        .trim();
      const stop = text.search(/[。！？!?\n]/);
      if (stop > 0) text = text.slice(0, stop).trim();
      if (text.length > 42) text = text.slice(0, 42).trim() + "…";
      return text;
    }

    /** The model's own words for one assistant step, or "" when it only
     * dispatched tools — then the tools themselves describe the step. */
    function assistantText(node) {
      const blocks = Array.isArray(node?.blocks) ? node.blocks : [];
      const parts = [];
      for (const block of blocks) {
        if (block?.kind === "text" && typeof block.text === "string") parts.push(block.text);
      }
      return cleanLabel(parts.join(" "));
    }

    /** The intent a tool serves. A step is one phase of intent, not one tool:
     * "adapting the pill" may touch six files and run three commands. Reading
     * and searching are the same intent (finding things out), so grep next to
     * web_search stays one phase instead of splitting the list in two. */
    const TOOL_PHASE = {
      read: "look",
      glob: "look",
      grep: "look",
      list_dir: "look",
      read_image: "look",
      cache_search: "look",
      search_context: "look",
      acp_status: "look",
      web_search: "look",
      multi_search: "look",
      advanced_search: "look",
      platform_search: "look",
      domain_search: "look",
      free_search_test: "look",
      video_search: "look",
      weather: "look",
      web_fetch: "look",
      research: "look",
      write: "edit",
      edit: "edit",
      apply_patch: "edit",
      pwsh: "run",
      subagent: "delegate",
      subagent_fork: "delegate",
      workflow: "delegate",
      send_message: "delegate",
      list_agents: "delegate",
      wait_agent: "delegate",
    };

    /** Bookkeeping, not work worth a step. The plan is already the pill's
     * primary view, so "updated the list" says nothing next to the list; and
     * context housekeeping (compress, acp_status, memory) is machinery the
     * user never asked to watch — worse, one of those calls landing between two
     * edits splits one "修改文件" phase into two and doubles the list. */
    const TOOL_SKIP = new Set([
      "todo_write",
      "compress",
      "decompress",
      "acp_status",
      "acp_cache",
      "cache_search",
      "search_context",
      "memory_save",
      "memory_search",
      "memory_get",
      "memory_list",
      "memory_update",
      "memory_pin",
      "get_goal",
      "update_goal",
      "create_goal",
      "job_output",
      "job_list",
      "job_kill",
      "list_agents",
      "wait_agent",
      "present",
    ]);

    /** null means "this tool is not a step". */
    function toolPhase(name) {
      if (TOOL_SKIP.has(name)) return null;
      return TOOL_PHASE[name] ?? "x:" + name;
    }

    /** A phase of same-intent work reads better as its intent than as whichever
     * tool happened to come first. */
    const PHASE_LABELS = {
      look: "查阅资料与代码",
      edit: "修改文件",
      run: "执行命令",
      delegate: "调度协作代理",
    };

    /** "修改文件 ×3" — one line per intent, honest about how much work it
     * covered. The specific tool is deliberately not the title: "编辑文件" and
     * "修改文件" are the same intent, and naming the tool makes two halves of
     * one phase look like two steps. */
    function stepLabel(tools, phase) {
      if (tools.length === 0) return "";
      const name = PHASE_LABELS[phase] ?? toolLabel(tools[0]);
      return tools.length === 1 ? name : name + " ×" + tools.length;
    }

    /** Narration is written as prose ("现在运行冒烟测试，确认…"), which reads as
     * a log line, not a step. A step title is a headline: strip the connectives
     * that open a sentence, and if what is left is still a sentence, hand the
     * step back to its phase name instead of truncating it into gibberish. */
    const NARRATION_LEAD =
      /^(?:好的?|那么|现在|接下来|然后|首先|其次|另外|同时|最后|再来|继续|让我|我来|我先|我|再|先)[，,、\s]*/;
    const STEP_TITLE_MAX = 16;

    function stepTitle(label) {
      let text = typeof label === "string" ? label.trim() : "";
      if (text === "") return "";
      for (let guard = 0; guard < 4; guard += 1) {
        const cut = text.replace(NARRATION_LEAD, "");
        if (cut === text) break;
        text = cut;
      }
      text = text.replace(/[，,、:：\s]+$/, "");
      if (text === "") return "";
      // Still a sentence: the phase name says the same thing in three words.
      if (text.length > STEP_TITLE_MAX) return "";
      if (/[，,。；;]/.test(text)) return "";
      return text;
    }

    /** A long turn still produces many phases; the list is a progress view, not
     * a transcript, so it keeps the recent end and counts the rest. */
    const MAX_STEPS = 6;

    /** Fold adjacent same-phase steps into one. Nothing else: an earlier rule
     * also absorbed a single-call "detour" sitting between two same-phase
     * neighbours, which read well on read → run → read but ran away on a long
     * turn — it folded six separate commands into the reading step that
     * followed them, so the list claimed work that never happened. Phases may
     * repeat; that is what a real turn looks like. */
    function mergePhases(steps) {
      const out = [];
      for (const step of steps) {
        const last = out.length > 0 ? out[out.length - 1] : null;
        if (last !== null && last.phase === step.phase) {
          last.tools.push(...step.tools);
          if (last.label === "") last.label = step.label;
          continue;
        }
        out.push(step);
      }
      return out;
    }

    /** Fold this turn's flow into intent phases. A phase ends where the intent
     * changes, and the model's own words become the phase title when it spoke
     * just before starting one — that is what makes the list read like
     * "查阅资料与代码 → 修改文件" instead of a tool log. */
    function turnSteps(snapshot) {
      const steps = [];
      let current = null;
      let pending = "";

      const begin = (label, phase) => {
        current = { label: stepTitle(label), phase: phase, tools: [] };
        steps.push(current);
        return current;
      };

      for (const node of turnNodes(snapshot)) {
        if (node?.kind === "assistant") {
          const label = assistantText(node);
          if (label === "") continue;
          // A step that has not called anything yet is waiting for its name;
          // otherwise the narration introduces whatever comes next.
          if (current !== null && current.tools.length === 0) current.label = stepTitle(label);
          else pending = label;
          continue;
        }
        if (node?.kind !== "tool-result") continue;
        const name = typeof node.name === "string" ? node.name : "";
        if (name === "") continue;
        const phase = toolPhase(name);
        if (phase === null) continue;
        if (current === null || current.phase !== phase) {
          begin(pending, phase);
          pending = "";
        } else if (current.label === "") {
          current.label = stepTitle(pending);
          pending = "";
        }
        current.tools.push(name);
      }

      // A reply still being written is the step in flight.
      const partial = snapshot?.partial;
      if (partial != null && Array.isArray(partial.blocks)) {
        const parts = [];
        for (const block of partial.blocks) {
          if (block?.kind === "text" && typeof block.text === "string") parts.push(block.text);
        }
        const label = cleanLabel(parts.join(" "));
        if (label !== "") begin(label, "reply");
      }

      // A dispatched call has no result yet, so it appears only here — it is
      // literally what is happening now.
      const runningCalls = Array.isArray(snapshot?.runningCalls) ? snapshot.runningCalls : [];
      for (const call of runningCalls) {
        const name = typeof call?.name === "string" ? call.name : "";
        if (name === "") continue;
        const phase = toolPhase(name);
        begin("正在" + toolLabel(name), phase === null ? "reply" : phase);
      }

      return mergePhases(steps).map((step) => ({
        label: step.label !== "" ? step.label : stepLabel(step.tools, step.phase),
        tools: step.tools,
      }));
    }

    function encodeSteps(steps) {
      return steps
        .map((step) => step.label + "\u0003" + step.tools.join("|"))
        .join("\u0004");
    }

    function decodeSteps(raw) {
      if (typeof raw !== "string" || raw === "") return [];
      return raw.split("\u0004").map((row) => {
        const cut = row.indexOf("\u0003");
        const label = cut < 0 ? row : row.slice(0, cut);
        const tools = cut < 0 ? "" : row.slice(cut + 1);
        return { label: label, tools: tools === "" ? [] : tools.split("|") };
      });
    }

    /** How many tool calls this turn already finished. */
    function doneToolCount(snapshot) {
      let count = 0;
      for (const node of turnNodes(snapshot)) if (node?.kind === "tool-result") count += 1;
      return count;
    }

    /** Start and last activity of the current turn, from the node clocks.
     * The turn starts at the user message and ends at the last event that
     * landed — as close to `turn/end` as the trajectory window gets. While a
     * turn is still running the caller reads the wall clock instead, so the
     * end here is only trusted once nothing is running. */
    function turnSpan(snapshot) {
      const nodes = snapshot?.eventNodes;
      if (!Array.isArray(nodes)) return { startedAt: 0, lastAt: 0 };
      let start = -1;
      for (let index = nodes.length - 1; index >= 0; index -= 1) {
        if (nodes[index]?.kind === "user") {
          start = index;
          break;
        }
      }
      if (start < 0) return { startedAt: 0, lastAt: 0 };
      const first = nodes[start]?.time;
      const startedAt = typeof first === "number" ? first : 0;
      let lastAt = startedAt;
      for (let index = start + 1; index < nodes.length; index += 1) {
        const time = nodes[index]?.time;
        if (typeof time === "number" && time > lastAt) lastAt = time;
      }
      return { startedAt, lastAt };
    }

    /** `0:07`, `1:23`, `1:02:03` — the compact form the pill has room for. */
    function formatClock(ms) {
      const total = Math.max(0, Math.floor(ms / 1000));
      const seconds = total % 60;
      const minutes = Math.floor(total / 60) % 60;
      const hours = Math.floor(total / 3600);
      const pad = (value) => (value < 10 ? "0" + value : String(value));
      return hours > 0 ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${minutes}:${pad(seconds)}`;
    }

    /** Spoken form for the tooltip: `7 秒`, `1 分 23 秒`, `1 小时 5 分`. */
    function formatElapsed(ms) {
      const total = Math.max(0, Math.floor(ms / 1000));
      if (total < 60) return `${total} 秒`;
      const minutes = Math.floor(total / 60);
      const seconds = total % 60;
      if (minutes < 60) return seconds === 0 ? `${minutes} 分钟` : `${minutes} 分 ${seconds} 秒`;
      const hours = Math.floor(minutes / 60);
      const rest = minutes % 60;
      return rest === 0 ? `${hours} 小时` : `${hours} 小时 ${rest} 分`;
    }

    /** A stopwatch drawn inline: an emoji would arrive in its own colours and
     * its own size, while this one inherits the label colour and the text
     * baseline, so it sits in the row like a glyph rather than a sticker. */
    function clockIcon() {
      return react.createElement(
        "svg",
        {
          className: "mwProgIcon",
          viewBox: "0 0 12 12",
          width: 11,
          height: 11,
          fill: "none",
          stroke: "currentColor",
          "stroke-width": 1.2,
          "stroke-linecap": "round",
          "aria-hidden": "true",
        },
        // crown and stem
        react.createElement("path", { d: "M4.9 1.7h2.2", key: "crown" }),
        react.createElement("path", { d: "M6 1.7v1.4", key: "stem" }),
        // dial
        react.createElement("circle", { cx: 6, cy: 7.1, r: 3.8, key: "dial" }),
        // hands: minute to the top, hour to the lower right
        react.createElement("path", { d: "M6 7.1V4.9", key: "minute" }),
        react.createElement("path", { d: "M6 7.1l1.5.9", key: "hour" }),
      );
    }

    function patchProgress(patch) {
      Object.assign(progressStore, patch);
      progressStore.listeners.forEach((listener) => listener());
    }

    function useProgressStore() {
      const [, bump] = react.useState(0);
      react.useEffect(() => {
        const listener = () => bump((count) => count + 1);
        progressStore.listeners.add(listener);
        return () => progressStore.listeners.delete(listener);
      }, []);
      return progressStore;
    }

    /** Step totals plus the first running step: the "where are we" summary. */
    function stepStats(todos) {
      let done = 0;
      let doing = 0;
      let current = null;
      for (const todo of todos) {
        if (todo.status === "completed") {
          done += 1;
        } else if (todo.status === "in_progress") {
          doing += 1;
          if (current === null) current = todo;
        }
      }
      return { total: todos.length, done, doing, current };
    }

    const STEP_GROUPS = [
      { status: "in_progress", title: "正在做", mark: "◐" },
      { status: "pending", title: "待开始", mark: "○" },
      { status: "completed", title: "已完成", mark: "✓" },
    ];

    /** States for one segmented bar. A 96-step turn cannot draw 96 segments in
     * 68px, so cells are capped and each one covers an equal slice of the
     * steps: the bar stays a proportion, never a fence. */
    const MAX_BAR_CELLS = 12;

    function barCells(total, doneCount, active) {
      const cells = Math.min(total, MAX_BAR_CELLS);
      const states = [];
      for (let index = 0; index < cells; index += 1) {
        const from = Math.floor((index * total) / cells);
        const to = Math.floor(((index + 1) * total) / cells);
        const complete = to <= doneCount;
        const running = active && from <= doneCount && doneCount < to;
        states.push(complete ? "completed" : running ? "in_progress" : "pending");
      }
      return states;
    }

    const EMPTY_VIEW = {
      running: [],
      streaming: false,
      steps: [],
      totalSteps: 0,
      doneCount: 0,
      startedAt: 0,
      lastAt: 0,
    };

    /** Fallbacks so the header never crashes on a slot that lacks a capability:
     * the call sites stay unconditional, so hook order cannot shift. */
    const noopProjection = () => undefined;
    const noopSession = (selector) => selector(undefined);
    const noopTrajectory = () => "";

    /** The whole per-turn activity view as ONE string.
     * A selector must return a value that is stable by identity between
     * snapshots, or the store re-renders forever — a string is stable. */
    function selectViewKey(snapshot) {
      const calls = Array.isArray(snapshot?.runningCalls) ? snapshot.runningCalls : [];
      const running = calls.map((call) => String(call?.name ?? "")).join("|");
      const steps = turnSteps(snapshot);
      const streaming = snapshot?.partial != null ? "1" : "0";
      const shown = steps.length > MAX_STEPS ? steps.slice(steps.length - MAX_STEPS) : steps;
      const span = turnSpan(snapshot);
      return `${running}\u0002${streaming}\u0002${doneToolCount(snapshot)}\u0002${
        String(span.startedAt) + ":" + String(span.lastAt)
      }\u0002${String(steps.length)}\u0005${encodeSteps(shown)}`;
    }

    function parseViewKey(key) {
      if (typeof key !== "string" || key === "") return EMPTY_VIEW;
      const parts = key.split("\u0002");
      if (parts.length !== 5) return EMPTY_VIEW;
      const colon = parts[3].indexOf(":");
      const startedAt = colon < 0 ? 0 : Number(parts[3].slice(0, colon)) || 0;
      const lastAt = colon < 0 ? 0 : Number(parts[3].slice(colon + 1)) || 0;
      const cut = parts[4].indexOf("\u0005");
      return {
        running: parts[0] === "" ? [] : parts[0].split("|"),
        streaming: parts[1] === "1",
        doneCount: Number(parts[2]) || 0,
        startedAt,
        lastAt,
        totalSteps: cut < 0 ? 0 : Number(parts[4].slice(0, cut)) || 0,
        steps: cut < 0 ? [] : decodeSteps(parts[4].slice(cut + 1)),
      };
    }

    /** Session header: always present, never hidden — with a plan it shows one
     * segment per step; without one it falls back to this turn's live activity. */
    function SessionProgress(props) {
      const store = useProgressStore();
      const readProjection =
        typeof props.useProjection === "function" ? props.useProjection : noopProjection;
      const readSession = typeof props.useSession === "function" ? props.useSession : noopSession;
      const readTrajectory =
        typeof props.useTrajectory === "function" ? props.useTrajectory : noopTrajectory;

      const todos = readProjection("todos");
      const list = Array.isArray(todos) ? todos : [];
      const running = readSession((snapshot) => snapshot?.running === true, Object.is) === true;
      const viewKey = readTrajectory(selectViewKey);
      const view = parseViewKey(viewKey);
      const names = view.running;
      const steps = view.steps;
      const busy = names.length > 0 || view.streaming === true || running === true;
      const startedAt = view.startedAt;
      const lastAt = view.lastAt;
      // A re-render per second IS the clock — the counter value is never read.
      // Once the turn ends the clock freezes at its last event, so a finished
      // turn keeps showing its total instead of counting forever.
      const [, setTick] = react.useState(0);
      react.useEffect(() => {
        if (!busy) return undefined;
        const timer = window.setInterval(() => setTick((count) => count + 1), 1000);
        return () => window.clearInterval(timer);
      }, [busy]);
      const elapsedMs =
        startedAt > 0
          ? Math.max(0, (busy ? Date.now() : lastAt > startedAt ? lastAt : startedAt) - startedAt)
          : 0;
      // Count the whole turn, not just the window on screen: phases merge down
      // to a handful, so "15/16" is a real answer where "96/96 · still running"
      // would not be. The list itself keeps only the recent end.
      const totalSteps = Math.max(view.totalSteps, steps.length);
      const hiddenSteps = Math.max(0, totalSteps - steps.length);
      // The last step is the one in flight while anything is still running.
      const doneSteps = busy && totalSteps > 0 ? Math.max(0, totalSteps - 1) : totalSteps;
      const signature = [
        list.map((todo) => todo.status + "\u0000" + todo.content).join("\u0001"),
        busy ? "1" : "0",
        view.streaming ? "1" : "0",
        view.running.join("|"),
        String(totalSteps),
        steps.map((step) => step.label).join("\u0001"),
        String(view.doneCount),
        String(startedAt),
        String(lastAt),
      ].join("\u0002");

      react.useEffect(() => {
        if (progressStore.signature === signature) return;
        patchProgress({
          todos: list,
          busy,
          streaming: view.streaming === true,
          runningName: names.length > 0 ? toolLabel(names[0]) : "",
          steps: steps,
          totalSteps: totalSteps,
          hiddenSteps: hiddenSteps,
          doneSteps: doneSteps,
          doneCount: view.doneCount,
          startedAt: startedAt,
          lastAt: lastAt,
          signature,
        });
      }, [signature]);

      react.useEffect(() => {
        if (!store.open) return undefined;
        const onKeyDown = (event) => {
          if (event.key === "Escape") patchProgress({ open: false });
        };
        // Same rule as the todo popover: the wrapper must stay transparent to
        // the mouse, so outside-click dismissal is a document-level listener.
        const onPointerDown = (event) => {
          const target = event.target;
          if (!(target instanceof Element)) return;
          if (target.closest(".mwProgPop") !== null) return;
          if (target.closest(".mwProgBtn") !== null) return;
          patchProgress({ open: false });
        };
        document.addEventListener("keydown", onKeyDown);
        document.addEventListener("pointerdown", onPointerDown, true);
        return () => {
          document.removeEventListener("keydown", onKeyDown);
          document.removeEventListener("pointerdown", onPointerDown, true);
        };
      }, [store.open]);

      // A popover left open must not survive a session switch.
      react.useEffect(() => () => patchProgress({ open: false }), []);

      const hasPlan = list.length > 0;
      const { total, done, doing, current } = stepStats(list);
      const complete = hasPlan && done === total;
      const lastStep = steps.length > 0 ? steps[steps.length - 1].label : "";
      const now = hasPlan
        ? current !== null
          ? current.content
          : complete
            ? "全部完成"
            : ""
        : busy
          ? lastStep !== ""
            ? lastStep
            : names.length > 0
              ? "正在" + toolLabel(names[0])
              : view.streaming
                ? "正在回复"
                : "正在忙碌"
          : lastStep !== ""
            ? lastStep
            : view.doneCount > 0
              ? `本轮完成 ${view.doneCount} 项`
              : "空闲";
      const summary = hasPlan
        ? complete
          ? `任务进度 ${done}/${total} · 全部完成`
          : `任务进度 ${done}/${total} · ${doing} 项进行中`
        : totalSteps === 0
          ? "本回合暂无进展 · 等待你的下一条指令"
          : busy
            ? `本回合进行中 · 已完成 ${doneSteps}/${totalSteps} 步`
            : `本回合已完成 ${doneSteps}/${totalSteps} 步`;
      // No ticking figure in the tooltip. The browser hides a native tooltip
      // whenever the title attribute changes and shows it again a moment later,
      // so a clock in here flickers once a second for as long as it is hovered.
      // The elapsed total still belongs here once the turn is over: that value
      // is frozen, so it changes at most once.
      const hint = startedAt > 0 && !busy ? `${summary} · 用时 ${formatElapsed(elapsedMs)}` : summary;

      // Every slot below renders on every frame, empty when it has nothing to
      // say. The pill is a fixed-width control in a row of fixed-width
      // controls: a slot that appears and disappears would shift the whole row
      // of header chips each time a step lands.
      const showBar = hasPlan || totalSteps > 0;
      const cells = hasPlan
        ? list.map((todo) => todo.status)
        : showBar
          ? barCells(totalSteps, doneSteps, busy)
          : [];

      return react.createElement(
        "button",
        {
          type: "button",
          className: "mwProgBtn",
          title: hint,
          "aria-haspopup": "dialog",
          "aria-expanded": store.open ? "true" : "false",
          "data-open": store.open ? "true" : undefined,
          "data-complete": complete || (!hasPlan && !busy && totalSteps > 0) ? "true" : undefined,
          "data-busy": busy ? "true" : undefined,
          onClick: (event) => {
            const rect = event.currentTarget.getBoundingClientRect();
            patchProgress({
              open: !progressStore.open,
              rect: { left: rect.left, top: rect.top, right: rect.right, bottom: rect.bottom },
            });
          },
        },
        react.createElement(
          "span",
          { className: "mwProgTop" },
          react.createElement("span", { className: "mwProgState" }, now),
          react.createElement(
            "span",
            { className: "mwProgTime", "aria-hidden": "true" },
            startedAt > 0 ? clockIcon() : null,
            startedAt > 0 ? formatClock(elapsedMs) : "",
          ),
        ),
        react.createElement(
          "span",
          { className: "mwProgTrack", "aria-hidden": "true" },
          showBar
            ? react.createElement(
                "span",
                { className: "mwProgBar" },
                cells.map((state, index) =>
                  react.createElement("span", {
                    key: String(index),
                    className: "mwProgSeg",
                    "data-state": state,
                  }),
                ),
              )
            : null,
        ),
      );
    }

    /** Frame-wide overlay: the header clips overflow, so the step list portals
     * and anchors itself under the pill. */
    function ProgressOverlay() {
      const store = useProgressStore();
      // Hooks run before the early return: this overlay mounts closed and opens
      // later, so a conditional hook would change order between renders.
      const [, setTick] = react.useState(0);
      react.useEffect(() => {
        if (!store.open || !store.busy) return undefined;
        const timer = window.setInterval(() => setTick((count) => count + 1), 1000);
        return () => window.clearInterval(timer);
      }, [store.open, store.busy]);
      if (!store.open) return null;

      const startedAt = store.startedAt > 0 ? store.startedAt : 0;
      const lastAt = store.lastAt > 0 ? store.lastAt : 0;
      const elapsedMs =
        startedAt > 0
          ? Math.max(0, (store.busy ? Date.now() : lastAt > startedAt ? lastAt : startedAt) - startedAt)
          : 0;
      const timeText = startedAt > 0 ? `用时 ${formatClock(elapsedMs)}` : "";

      const todos = store.todos ?? [];
      const { total, done, doing } = stepStats(todos);
      const hasPlan = todos.length > 0;
      const steps = Array.isArray(store.steps) ? store.steps : [];
      const rect = store.rect;
      const style =
        rect === null
          ? { top: 52, left: 24 }
          : {
              top: Math.round(rect.bottom + 8),
              left: Math.round(
                Math.min(Math.max(12, rect.left - 8), Math.max(12, window.innerWidth - 352)),
              ),
            };

      // No plan: the turn's flow folded into steps. The one in flight carries a
      // ◐ and the rest read as already done, which is what makes this a
      // progress list rather than a log.
      const activeIndex = store.busy && steps.length > 0 ? steps.length - 1 : -1;
      const liveBody =
        steps.length === 0
          ? [
              react.createElement(
                "div",
                { className: "mwProgGroup", key: "idle" },
                react.createElement(
                  "div",
                  { className: "mwProgRow", "data-state": "pending" },
                  react.createElement("span", { className: "mwProgMark", "aria-hidden": "true" }, "○"),
                  react.createElement("span", { className: "mwProgText" }, "空闲 · 等待你的下一条指令"),
                ),
              ),
            ]
          : [
              react.createElement(
                "div",
                { className: "mwProgGroup mwProgLive", key: "steps" },
              react.createElement(
                "div",
                { className: "mwProgGroupTitle" },
                (store.busy
                  ? `进展 · 已完成 ${store.doneSteps}/${store.totalSteps} 步`
                  : `进展 · 共 ${store.totalSteps} 步`) +
                  (store.hiddenSteps > 0 ? ` · 更早 ${store.hiddenSteps} 步已折叠` : ""),
              ),
                steps.map((step, index) =>
                  react.createElement(
                    "div",
                    {
                      className: "mwProgRow",
                      key: String(index),
                      "data-state": index === activeIndex ? "in_progress" : "completed",
                    },
                    react.createElement(
                      "span",
                      { className: "mwProgMark", "aria-hidden": "true" },
                      index === activeIndex ? "◐" : "✓",
                    ),
                    react.createElement("span", { className: "mwProgText" }, step.label),
                  ),
                ),
              ),
            ];

      return react.createElement(
        "div",
        {
          className: "mwPopWrap",
        },
        react.createElement(
          "div",
          { className: "mwProgPop", role: "dialog", "aria-label": "任务进度", style },
          react.createElement(
            "div",
            { className: "mwPopBar" },
            react.createElement(
              "span",
              { className: "mwPopTitle" },
              hasPlan ? "任务进度" : "本回合进展",
            ),
            react.createElement(
              "span",
              { className: "mwSub" },
              hasPlan
                ? `${done}/${total} 已完成 · ${doing} 进行中`
                : store.busy
                  ? `进行中 · 已完成 ${store.doneSteps}/${store.totalSteps} 步`
                  : `已完成 ${store.totalSteps} 步`,
            ),
            react.createElement(
              "div",
              { className: "mwPopActions" },
              timeText !== ""
                ? react.createElement(
                    "span",
                    // No title here either: the same ticking value would make the
                    // popover's own tooltip flicker while the clock runs.
                    { className: "mwPopTime" },
                    timeText,
                  )
                : null,
              react.createElement(
                "button",
                {
                  type: "button",
                  className: "mwPopClose",
                  "aria-label": "关闭任务进度",
                  onClick: () => patchProgress({ open: false }),
                },
                "✕",
              ),
            ),
          ),
          react.createElement(
            "div",
            { className: "mwProgPopBody" },
            hasPlan
              ? STEP_GROUPS.map((group) => {
                  const rows = todos.filter((todo) => todo.status === group.status);
                  if (rows.length === 0) return null;
                  return react.createElement(
                    "div",
                    { className: "mwProgGroup", key: group.status },
                    react.createElement(
                      "div",
                      { className: "mwProgGroupTitle" },
                      `${group.title} · ${rows.length}`,
                    ),
                    rows.map((todo, index) =>
                      react.createElement(
                        "div",
                        { className: "mwProgRow", key: String(index), "data-state": todo.status },
                        react.createElement(
                          "span",
                          { className: "mwProgMark", "aria-hidden": "true" },
                          group.mark,
                        ),
                        react.createElement("span", { className: "mwProgText" }, todo.content),
                      ),
                    ),
                  );
                })
              : liveBody,
          ),
        ),
      );
    }


    const inject = ["slots"];

    function apply(ctx) {
      ensureStyle();
      // Title-adjacent pill: fed by the host's `todos` projection, which resets
      // per turn and keeps the finished plan after a turn ends.
      ctx.slots.inject("conversation.session.header.actions", () =>
        ctx.slots.register(
          {
            name: "conversation.session.header.actions",
            id: "dsh-session-progress",
            order: 5,
          },
          SessionProgress,
        ),
      );
      ctx.slots.inject("shell.overlay", () =>
        ctx.slots.register(
          {
            name: "shell.overlay",
            id: "dsh-session-progress-overlay",
            order: 55,
            label: "任务进度",
          },
          ProgressOverlay,
        ),
      );
    }

    exports.inject = ["slots"];
    exports.apply = apply;
    return module.exports;
  },
});
