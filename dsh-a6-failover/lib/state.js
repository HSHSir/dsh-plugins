/**
 * 插件自己的小状态：切换历史、路由降级、商家缓存、token 反查结果。
 *
 * 放在 $DSH_HOME/a6-failover/state.json，每处修改都原子写（先写 .tmp 再 rename），
 * 写坏了最多丢一次统计，不会挡住主流程——所有读写都包在 try 里。
 * 只读地参考模型管家的 ~/.model_probe/routing.json（bad 表）作为「别再去撞」的先验。
 */

import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';

const STATE_VERSION = 1;
const SWITCH_WINDOW_MS = 60 * 60 * 1000;
const MAX_EVENT_BYTES = 4 * 1024 * 1024;
/**
 * 实测缓存命中的半衰期：三天。
 * 命中率跟着「上游是不是真的缓存住了前缀」走，会随部署变化；不衰减的话，
 * 一个月前的数据会把当天的表现压住。
 */
const USAGE_HALF_LIFE_MS = 3 * 24 * 3600 * 1000;
/**
 * 渠道级成败与延迟的半衰期：一天。
 * 商家是会自愈也会变坏的，三天前的可用性不该压住今天的表现。
 */
const OUTCOME_HALF_LIFE_MS = 24 * 3600 * 1000;
/**
 * 商家上下文窗口观测的有效期：七天。
 * 窗口大小跟着上游型号走，商家换号后可能变大；但窗口也不太会天天变，
 * 太短会让同一堵墙反复撞。
 */
const CONTEXT_LIMIT_TTL_MS = 7 * 24 * 3600 * 1000;

export function resolveDshHome() {
  const fromEnv = String(process.env.DSH_HOME || '').trim();
  if (fromEnv) return fromEnv;
  return path.join(os.homedir(), '.dsh');
}

function emptyState() {
  return {
    version: STATE_VERSION,
    tokens: {},
    routes: {},
    channels: {},
    usage: {},
    outcomes: {},
    contextLimits: {},
    pins: {},
    bad: {},
    switches: [],
    updatedAt: 0,
  };
}

/** 按时间衰减一次累计量；没记过 at 的行当作空。 */
function decayUsage(entry, nowMs) {
  const at = Number(entry?.at || 0);
  if (!at) return { cacheRead: 0, cacheWrite: 0, uncachedInput: 0, samples: 0, at: 0 };
  const age = Math.max(0, nowMs - at);
  const factor = Math.pow(0.5, age / USAGE_HALF_LIFE_MS);
  return {
    cacheRead: Number(entry.cacheRead || 0) * factor,
    cacheWrite: Number(entry.cacheWrite || 0) * factor,
    uncachedInput: Number(entry.uncachedInput || 0) * factor,
    output: Number(entry.output || 0) * factor,
    samples: Number(entry.samples || 0) * factor,
    at: nowMs,
  };
}

/**
 * 命中率口径与 DSH 自己算的一致：cacheRead / (未命中输入 + 缓存读 + 缓存写)。
 * usage 里的 inputTokens 是「未命中的输入」，不是输入总量。
 */
function usageStat(entry) {
  if (!entry) return null;
  const cacheRead = Number(entry.cacheRead || 0);
  const cacheWrite = Number(entry.cacheWrite || 0);
  const uncachedInput = Number(entry.uncachedInput || 0);
  const output = Number(entry.output || 0);
  const promptTokens = cacheRead + cacheWrite + uncachedInput;
  if (!(promptTokens > 0)) return null;
  return {
    rate: Math.min(Math.max(cacheRead / promptTokens, 0), 1),
    samples: Number(entry.samples || 0),
    promptTokens,
    // 输出/输入比：同样标价下，输出贵三倍的商家实际花得更多。
    outputRatio: output > 0 ? output / promptTokens : 0,
  };
}

/** 按时间衰减一次渠道成败累计；没记过 at 的行当作空。 */
function decayOutcome(entry, nowMs) {
  const at = Number(entry?.at || 0);
  if (!at) return { ok: 0, fail: 0, latencies: [], at: 0 };
  const age = Math.max(0, nowMs - at);
  const factor = Math.pow(0.5, age / OUTCOME_HALF_LIFE_MS);
  return {
    ok: Number(entry.ok || 0) * factor,
    fail: Number(entry.fail || 0) * factor,
    latencies: Array.isArray(entry.latencies)
      ? entry.latencies.filter(sample => sample.at >= nowMs - 7 * 86400000 && Number.isFinite(sample.ms) && sample.ms > 0).slice(-63)
      : [],
    at: nowMs,
  };
}

function readJson(file) {
  try {
    const text = fs.readFileSync(file, 'utf8');
    const parsed = JSON.parse(text);
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch {
    return null;
  }
}

function writeJsonAtomic(file, value) {
  const dir = path.dirname(file);
  fs.mkdirSync(dir, { recursive: true });
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(value)}\n`, { encoding: 'utf8', mode: 0o600 });
  fs.renameSync(tmp, file);
}

export function createStore({ dshHome = resolveDshHome(), logEvents = true, now = () => Date.now() } = {}) {
  const dir = path.join(dshHome, 'a6-failover');
  const statePath = path.join(dir, 'state.json');
  const eventPath = path.join(dir, 'events.jsonl');

  let state = { ...emptyState(), ...(readJson(statePath) || {}) };
  for (const key of ['tokens', 'routes', 'channels', 'usage', 'outcomes', 'contextLimits', 'pins', 'bad']) {
    if (!state[key] || typeof state[key] !== 'object') state[key] = {};
  }
  if (!Array.isArray(state.switches)) state.switches = [];

  let dirty = false;
  let flushTimer = null;
  let disposed = false;
  let eventBytes;

  function record(bucket, key, value, maxEntries = 256) {
    delete bucket[key];
    Object.defineProperty(bucket, key, { value, writable: true, enumerable: true, configurable: true });
    const keys = Object.keys(bucket);
    for (const old of keys.slice(0, Math.max(0, keys.length - maxEntries))) delete bucket[old];
    scheduleFlush();
  }

  function flush(force = false) {
    if (!dirty && !force) return;
    try {
      state.updatedAt = now();
      writeJsonAtomic(statePath, state);
      dirty = false;
    } catch {
      /* 统计写不进去不影响模型调用 */
    }
  }

  function scheduleFlush() {
    if (disposed) return;
    dirty = true;
    if (flushTimer) return;
    flushTimer = setTimeout(() => {
      flushTimer = null;
      flush();
    }, 2000);
    if (typeof flushTimer.unref === 'function') flushTimer.unref();
  }

  function appendEvent(event) {
    if (disposed || !(typeof logEvents === 'function' ? logEvents() : logEvents)) return;
    try {
      if (eventBytes === undefined) {
        fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
        eventBytes = fs.statSync(eventPath, { throwIfNoEntry: false })?.size || 0;
      }
      if (eventBytes >= MAX_EVENT_BYTES) {
        fs.rmSync(`${eventPath}.1`, { force: true });
        fs.renameSync(eventPath, `${eventPath}.1`);
        eventBytes = 0;
      }
      const line = `${JSON.stringify({ at: new Date(now()).toISOString(), ...event })}\n`;
      fs.appendFileSync(eventPath, line, { encoding: 'utf8', mode: 0o600 });
      eventBytes += Buffer.byteLength(line);
    } catch {
      /* 日志失败无所谓 */
    }
  }

  return {
    dir,
    statePath,
    eventPath,
    now,
    snapshot: () => state,

    // Pin ownership is a write-ahead journal. A failed disk write must prevent the remote mutation.
    ownedPins: () => ({ ...(state.ownedPins || {}) }),
    setOwnedPin(key, row) {
      const ownedPins = { ...(state.ownedPins || {}) };
      if (row) ownedPins[key] = row;
      else delete ownedPins[key];
      const next = { ...state, ownedPins, updatedAt: now() };
      writeJsonAtomic(statePath, next);
      state = next;
    },

    flush,
    dispose() {
      if (disposed) return;
      if (flushTimer) clearTimeout(flushTimer);
      flushTimer = null;
      flush();
      disposed = true;
    },

    logEvent(event) {
      appendEvent(event);
    },

    // ---- token 反查缓存 ----
    getToken(controlBase, provider) {
      return state.tokens[`${controlBase}|${provider}`] || null;
    },
    setToken(controlBase, provider, info) {
      record(state.tokens, `${controlBase}|${provider}`, { ...info, at: now() });
    },

    // ---- 路由健康度 ----
    routeEntry(provider, model) {
      const key = `${provider}|${model}`;
      const entry = state.routes[key];
      if (entry) return entry;
      return { fails: 0, success: 0, lastFailAt: 0, degradedUntil: 0, switches: 0 };
    },
    noteSuccess(provider, model) {
      const key = `${provider}|${model}`;
      const entry = this.routeEntry(provider, model);
      record(state.routes, key, { ...entry, fails: 0, success: (entry.success || 0) + 1, degradedUntil: 0 });
    },
    noteFailure(provider, model, { code = '', degradeAfter = 3, cooldownMs = 180000 } = {}) {
      const key = `${provider}|${model}`;
      const entry = this.routeEntry(provider, model);
      // 降级期过了以后重新数：否则一次失败又会把路由打成降级，永远翻不了身
      const expired = entry.degradedUntil && entry.degradedUntil <= now();
      const baseFails = expired ? 0 : (entry.fails || 0);
      const fails = baseFails + 1;
      const degradedUntil = fails >= degradeAfter ? now() + cooldownMs : (expired ? 0 : (entry.degradedUntil || 0));
      record(state.routes, key, { ...entry, fails, lastFailAt: now(), lastCode: code, degradedUntil });
      return state.routes[key];
    },
    noteSwitch(provider, model) {
      const key = `${provider}|${model}`;
      const entry = this.routeEntry(provider, model);
      record(state.routes, key, { ...entry, switches: (entry.switches || 0) + 1 });
      state.switches.push(now());
      scheduleFlush();
      return state.routes[key];
    },
    isDegraded(provider, model) {
      const entry = state.routes[`${provider}|${model}`];
      return Boolean(entry && entry.degradedUntil && entry.degradedUntil > now());
    },

    /** 滑动一小时内的切换次数是否还在预算内。 */
    allowSwitch(maxPerHour) {
      const horizon = now() - SWITCH_WINDOW_MS;
      const recent = state.switches.filter((at) => at > horizon);
      if (recent.length !== state.switches.length) {
        state.switches = recent;
        scheduleFlush();
      }
      return recent.length < maxPerHour;
    },
    switchesLastHour() {
      const horizon = now() - SWITCH_WINDOW_MS;
      return state.switches.filter((at) => at > horizon).length;
    },

    // ---- 渠道缓存 ----
    getChannels(controlBase, model) {
      const row = state.channels[`${controlBase}|${model}`];
      if (!row || !Array.isArray(row.candidates)) return null;
      return row;
    },
    setChannels(controlBase, model, candidates, meta = {}) {
      record(state.channels, `${controlBase}|${model}`, { at: now(), candidates, ...meta }, 32);
    },
    /**
     * 所有已缓存的控制面 → { model: rows } 视图，供后台定时刷新遍历。
     * 只返回还有候选的条目，空表不参与刷新。
     */
    allChannels() {
      const out = {};
      for (const [key, row] of Object.entries(state.channels || {})) {
        if (!row || !Array.isArray(row.candidates) || !row.candidates.length) continue;
        const sep = key.indexOf('|');
        if (sep <= 0) continue;
        const base = key.slice(0, sep);
        const model = key.slice(sep + 1);
        (out[base] ||= {})[model] = row;
      }
      return out;
    },
    /** 某条路由已知的坏商家（来自本插件与模型管家的 bad 表）。 */
    badChannels(controlBase, model, ttlMs = 6 * 3600 * 1000) {
      const host = String(controlBase || '').replace(/^https?:\/\//, '').replace(/\/.*$/, '');
      const horizon = now() - ttlMs;
      const out = new Set();
      const own = state.bad[`${controlBase}|${model}`];
      if (own && typeof own === 'object') {
        for (const [id, row] of Object.entries(own)) {
          const at = Number(row?.at || 0);
          const until = Number(row?.until || 0);
          // until 是显式短期拉黑（例如「已输出内容后商家不可用」），到期就自动恢复
          if (until) {
            if (now() < until) out.add(Number(id));
            continue;
          }
          if (at && at < horizon) continue; // 旧账不再算数，商家是会自愈的
          out.add(Number(id));
        }
      }
      const adopted = state.bad[`${host}|${model}`];
      if (adopted && typeof adopted === 'object') {
        for (const id of Object.keys(adopted)) out.add(Number(id));
      }
      return out;
    },
    noteBadChannel(controlBase, model, channelId, code = '', ttlMs = 0) {
      const key = `${controlBase}|${model}`;
      const row = state.bad[key] && typeof state.bad[key] === 'object' ? state.bad[key] : {};
      for (const [id, entry] of Object.entries(row)) {
        if (Number(entry?.at || 0) < now() - 6 * SWITCH_WINDOW_MS) delete row[id];
      }
      const until = Number.isFinite(ttlMs) && ttlMs > 0 ? now() + ttlMs : 0;
      record(row, String(channelId), { at: now(), code, ...(until ? { until } : {}) });
      record(state.bad, key, row);
    },

    // ---- 商家实测缓存命中（来自我们自己那次请求的 usage）----
    /**
     * 记一次真实请求的用量。只有「知道这次打的是哪家」时才记，否则数据会串味。
     * @param {{ model?: string, channelId?: number, cacheRead?: number, cacheWrite?: number, uncachedInput?: number }} row
     */
    noteUsage({ controlBase = '', model = '', channelId = 0, cacheRead = 0, cacheWrite = 0, uncachedInput = 0, output = 0 } = {}) {
      const id = Number(channelId);
      const values = [cacheRead, cacheWrite, uncachedInput].map(Number);
      if (!Number.isSafeInteger(id) || id <= 0 || !values.every((value) => Number.isSafeInteger(value) && value >= 0)
        || !values.some((value) => value > 0)) return null;
      const scope = controlBase ? `${controlBase}|` : '';
      const key = `${scope}${String(model || '')}|${id}`;
      const nowMs = now();
      const base = decayUsage(state.usage[key], nowMs);
      const outTokens = Number.isSafeInteger(Number(output)) && Number(output) > 0 ? Number(output) : 0;
      const next = {
        cacheRead: Number(base.cacheRead || 0) + values[0],
        cacheWrite: Number(base.cacheWrite || 0) + values[1],
        uncachedInput: Number(base.uncachedInput || 0) + values[2],
        output: Number(base.output || 0) + outTokens,
        samples: Number(base.samples || 0) + 1,
        at: nowMs,
      };
      record(state.usage, key, next, 1024);
      return usageStat(next);
    },
    /** 某模型下所有商家的实测命中率：Map<channelId, { rate, samples, promptTokens }>。 */
    usageRates(model, controlBase = '') {
      const out = new Map();
      const scope = controlBase ? `${controlBase}|` : '';
      const prefix = `${scope}${String(model || '')}|`;
      const nowMs = now();
      for (const [key, entry] of Object.entries(state.usage)) {
        if (!key.startsWith(prefix)) continue;
        const id = Number(key.slice(prefix.length));
        if (!Number.isFinite(id) || id <= 0) continue;
        const stat = usageStat(decayUsage(entry, nowMs));
        if (stat) out.set(Math.trunc(id), stat);
      }
      return out;
    },

    // ---- 渠道级真实可用性（成功/失败次数 + 首块延迟）----
    /**
     * 记一次真实调用的结局。只有「知道打的是哪家」时才记。
     * @param {{controlBase?:string, model?:string, channelId?:number, ok?:boolean, latencyMs?:number}} row
     */
    noteChannelOutcome({ controlBase = '', model = '', channelId = 0, ok = false, latencyMs = 0 } = {}) {
      const id = Number(channelId);
      if (!Number.isSafeInteger(id) || id <= 0) return null;
      const scope = controlBase ? `${controlBase}|` : '';
      const key = `${scope}${String(model || '')}|${id}`;
      const nowMs = now();
      const base = decayOutcome(state.outcomes[key], nowMs);
      const latency = Number.isFinite(latencyMs) && latencyMs > 0 && latencyMs < 600000 ? latencyMs : 0;
      const next = {
        ok: Number(base.ok || 0) + (ok ? 1 : 0),
        fail: Number(base.fail || 0) + (ok ? 0 : 1),
        latencies: latency ? [...base.latencies, { ms: latency, at: nowMs }].slice(-63) : base.latencies,
        at: nowMs,
      };
      record(state.outcomes, key, next, 2048);
      return next;
    },
    /** 某模型下各渠道的实测成败与延迟：Map<channelId, { calls, fails, failRate, p50Ms, latencySamples }>。 */
    channelStats(model, controlBase = '') {
      const out = new Map();
      const scope = controlBase ? `${controlBase}|` : '';
      const prefix = `${scope}${String(model || '')}|`;
      const nowMs = now();
      for (const [key, entry] of Object.entries(state.outcomes)) {
        if (!key.startsWith(prefix)) continue;
        const id = Number(key.slice(prefix.length));
        if (!Number.isFinite(id) || id <= 0) continue;
        const row = decayOutcome(entry, nowMs);
        const calls = row.ok + row.fail;
        if (!(calls > 0)) continue;
        const times = row.latencies.map(sample => sample.ms).sort((a, b) => a - b);
        const middle = Math.floor(times.length / 2);
        const median = times.length % 2 ? times[middle] : times.length ? (times[middle - 1] + times[middle]) / 2 : 0;
        out.set(Math.trunc(id), {
          calls,
          fails: row.fail,
          failRate: row.fail / calls,
          p50Ms: median,
          latencySamples: times.length,
        });
      }
      return out;
    },

    // ---- 当前固定的商家（重启后仍然知道「上一个请求打的是谁」）----
    getPin(provider, model) {
      const row = state.pins[`${provider}|${model}`];
      return row && typeof row === 'object' ? row : null;
    },
    setPin(provider, model, info = {}) {
      state.pins[`${provider}|${model}`] = { ...info, at: now() };
      scheduleFlush();
    },

    // ---- 商家上下文窗口（只记「至少塞不下这么多」的证据）----
    /**
     * 记一次「这条路由装不下这么大的上下文」的实测。
     *
     * 站点不提供任何窗口字段，所以窗口只能靠撞墙学：某次请求在 X 个 token 时判了上下文超限，
     * 那这家的窗口必然 ≲ X。同一个商家只保留**最小**的那个观测（撞得越小，说明窗口越小），
     * 过期时间按 7 天算——商家换上游/换号后可能变大，旧账不该永久生效。
     * @param {{controlBase?:string, model?:string, channelId?:number, atMost?:number}} row
     */
    noteContextLimit({ controlBase = '', model = '', channelId = 0, atMost = 0 } = {}) {
      const id = Number(channelId);
      const limit = Number(atMost);
      if (!Number.isSafeInteger(id) || id <= 0 || !Number.isFinite(limit) || limit <= 0) return null;
      const scope = controlBase ? `${controlBase}|` : '';
      const key = `${scope}${String(model || '')}|${id}`;
      const previous = state.contextLimits[key];
      const atMostNext = previous && Number(previous.atMost) > 0
        ? Math.min(Number(previous.atMost), Math.round(limit))
        : Math.round(limit);
      const next = { atMost: atMostNext, at: now() };
      record(state.contextLimits, key, next, 1024);
      return next;
    },
    /** 某模型下各商家的窗口上限（观测值）：Map<channelId, { atMost, at }>。 */
    contextWindows(model, controlBase = '') {
      const out = new Map();
      const scope = controlBase ? `${controlBase}|` : '';
      const prefix = `${scope}${String(model || '')}|`;
      const horizon = now() - CONTEXT_LIMIT_TTL_MS;
      for (const [key, entry] of Object.entries(state.contextLimits || {})) {
        if (!key.startsWith(prefix)) continue;
        const id = Number(key.slice(prefix.length));
        if (!Number.isFinite(id) || id <= 0) continue;
        if (!entry || Number(entry.at || 0) < horizon) continue;
        const atMost = Number(entry.atMost) || 0;
        if (atMost > 0) out.set(Math.trunc(id), { atMost, at: Number(entry.at) || 0 });
      }
      return out;
    },
  };
}

/**
 * 只读模型管家的路由文件，取站点凭据与已知坏商家。
 * 读不到就返回空——插件不依赖它存在。
 */
export function readModelProbeRouting({ home = os.homedir() } = {}) {
  const file = path.join(home, '.model_probe', 'routing.json');
  const parsed = readJson(file);
  if (!parsed || typeof parsed !== 'object') return { file, sites: {}, bad: {} };
  const sites = parsed.sites && typeof parsed.sites === 'object' ? parsed.sites : {};
  const bad = parsed.bad && typeof parsed.bad === 'object' ? parsed.bad : {};
  return { file, sites, bad };
}
