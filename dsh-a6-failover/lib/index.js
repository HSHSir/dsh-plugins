/**
 * dsh-a6-failover —— 插件入口。
 *
 * 目标（用户原话）：使用 a6 的模型时，遇到模型响应异常就**主动切换商家**，
 * 而不是遇到问题就把任务终止掉。
 *
 * 两个注入点：
 *   llm/stream          —— 还没吐出正文就坏了：关掉这条流，换一家再打开（同一回合继续）。
 *   agent/request-error —— 对明确的上下文错误选择已配置的更大路由。
 *
 * 只对 provider 名命中 providerPattern（默认 ^a6api）的路由生效；其他模型一点不碰。
 */

import { defaultsForForm, isManagedProvider, normalizeConfig } from './config.js';
import { guardedStream } from './stream-guard.js';
import { createStore, readModelProbeRouting, resolveDshHome } from './state.js';
import { createSwitcher } from './switcher.js';
import { createBaseURLResolver } from './settings-probe.js';
import { buildStatus } from './status.js';
import { installDeliveryCheck } from './delivery.js';
import { VERSION } from './version.js';
import { controlOrigin } from './a6.js';
import {
  clearOverrides, forDisplay, loadOverrides, mergeOverrides, overridesPath, saveOverrides,
} from './overrides.js';
import {
  CONTEXT_FAILURE_CODES, findContextRoute, parseContextNumbers,
} from './context-failover.js';

export const name = 'a6-failover';
export const inject = [];

const SESSION_HINT_TTL_MS = 30 * 60 * 1000;

export function apply(ctx, config) {
  const dshHome = resolveDshHome();
  const patchConfig = config && typeof config === 'object' ? config : {};

  // 设置页保存的值存在 config.json 里，盖在 profile patch 之上；改完立刻生效，不用重启。
  let overrides = loadOverrides(dshHome);
  let cfg = normalizeConfig(mergeOverrides(patchConfig, overrides));

  const log = (message) => {
    const text = String(message || '');
    if (!text) return;
    if (cfg.verbose) console.warn(`[a6-failover] ${text}`);
  };
  const store = createStore({ dshHome, logEvents: () => cfg.logEvents });

  installDeliveryCheck(ctx, { store, isManaged: provider => cfg.enabled && isManagedProvider(cfg, provider) });

  // 模型管家的 routing.json 只读、按分钟缓存（切商家时可能连读好几次）。
  let routingCache = { at: 0, value: { sites: {}, bad: {} } };
  const routing = () => {
    if (Date.now() - routingCache.at < 60000) return routingCache.value;
    try {
      routingCache = { at: Date.now(), value: readModelProbeRouting() };
    } catch {
      routingCache = { at: Date.now(), value: { sites: {}, bad: {} } };
    }
    return routingCache.value;
  };

  // settings 服务可能晚于本插件就绪，也可能根本没有：两种情况都不影响启用。
  let baseURL = () => '';
  try {
    ctx.inject?.(['settings'], (scope) => {
      try {
        const resolve = createBaseURLResolver(scope?.settings, { log });
        baseURL = resolve;
        scope.effect?.(() => () => { baseURL = () => ''; });
      } catch (error) {
        log(`settings 注入失败：${error?.message || error}`);
      }
    });
  } catch (error) {
    log(`settings 注入不可用：${error?.message || error}`);
  }

  const makeSwitcher = () => createSwitcher({ cfg, store, resolver: { baseURL: (p) => baseURL(p) }, log, routing });
  const switcher = makeSwitcher();
  const recover = (llm) => {
    if (!cfg.enabled) return;
    let providers = [];
    try {
      providers = (llm?.listProviders?.() || []).map(p => typeof p === 'string' ? p : p?.id)
        .filter(p => p && isManagedProvider(cfg, p));
    } catch { /* 静态配置和请求入口也能识别受管供应商。 */ }
    return Promise.resolve(switcher.startRecovery(providers)).catch(() => log('等待恢复平台路由'));
  };
  recover();

  // llm 服务：只为「查模型真实上下文窗口」用（resolveModelInfo / listProviders）。
  let llmRuntime = null;
  try {
    ctx.inject?.(['llm'], (scope) => {
      llmRuntime = scope?.llm || null;
      recover(llmRuntime);
      scope.effect?.(() => () => { llmRuntime = null; });
    });
  } catch (error) {
    log(`llm 注入不可用：${error?.message || error}`);
  }

  /** 因为上下文原因换模型的滑动一小时预算。 */
  const contextSwitches = [];
  const allowContextSwitch = () => {
    const horizon = Date.now() - 60 * 60 * 1000;
    while (contextSwitches.length && contextSwitches[0] < horizon) contextSwitches.shift();
    return contextSwitches.length < cfg.maxContextSwitchesPerHour;
  };

  /** 设置变动时取消旧控制面请求，再创建新的切换器。 */
  const applyOverrides = (nextOverrides) => {
    overrides = nextOverrides || {};
    cfg = normalizeConfig(mergeOverrides(patchConfig, overrides));
    switcher.updateConfig(cfg);
    recover(llmRuntime);
    return cfg;
  };

  // 每个会话最近一次失败的路由：agent/request-error 那份 payload 里没有 provider/model。
  const sessionHints = new Map();

  function rememberFailure(options, info) {
    const sessionId = String(options?.sessionId || '');
    if (!sessionId) return;
    if (sessionHints.size > 200) {
      const oldest = sessionHints.keys().next().value;
      if (oldest !== undefined) sessionHints.delete(oldest);
    }
    sessionHints.set(sessionId, { ...info, at: Date.now() });
  }

  /** 本插件自己重开的那些请求：再进 waterfall 时直接放行，别再包一层。 */
  const replays = new WeakSet();
  const attemptSignals = new WeakSet();

  ctx.on('llm/stream', function onLlmStream(options, next) {
    // eslint-disable-next-line no-invalid-this
    const runtime = this;
    if (replays.has(options) || attemptSignals.has(options?.signal)) return next();
    const provider = String(options?.provider || '');
    if (!cfg.enabled || !isManagedProvider(cfg, provider)) return next();
    const activeSwitcher = switcher;
    const activeConfig = cfg;
    let requestPin;
    let routeAtOpen;
    let requestGeneration;
    const leases = new Map();

    const open = async function* (opts, meta) {
      const key = JSON.stringify([opts.provider, opts.model]);
      if (!leases.has(key) && isManagedProvider(activeConfig, opts.provider)) {
        const finish = await activeSwitcher.beginRequest(opts.provider, opts.model, opts.signal);
        if (opts.signal?.aborted) { finish(); opts.signal.throwIfAborted(); }
        leases.set(key, finish);
      }
      opts.signal?.throwIfAborted();
      requestGeneration = activeSwitcher.generation(opts.provider, opts.model);
      routeAtOpen = { provider: opts.provider, model: opts.model };
      requestPin = activeSwitcher.activeChannel(opts.provider, opts.model);
      // Cordis 的 next() 只能调一次，重试用新的 stream 调用。
      if (meta.first && opts.signal === options.signal) { yield* next(); return; }
      if (typeof runtime?.stream !== 'function') throw new Error('a6-failover: 无法重开模型流');
      const replay = { ...opts };
      replays.add(replay);
      if (replay.signal && replay.signal !== options.signal) attemptSignals.add(replay.signal);
      yield* runtime.stream(replay);
    };
    const guarded = () => guardedStream({
      options,
      cfg: activeConfig,
      openStream: open,
      planSwitch: (args) => activeSwitcher.planSwitch({ ...args, failedChannelId: requestPin?.channelId, failedGeneration: requestGeneration }),
      onFailure: (failure) => {
        const evidence = failure.evidence;
        if (evidence?.rule) {
          store.logEvent({ type: 'format-anomaly', code: 'A6_STREAM_FORMAT', rule: evidence.rule,
            requestId: evidence.requestId, attempt: evidence.attempt, elapsedMs: evidence.elapsedMs,
            firstResponseMs: evidence.firstResponseMs ?? null, postContent: Boolean(failure.postContent),
            provider: failure.provider, model: failure.model, channelId: requestPin?.channelId ?? null });
        }
        rememberFailure(options, {
          provider: failure.provider, model: failure.model, code: failure.code, kind: failure.kind,
          postContent: Boolean(failure.postContent),
        });
        if (failure.permanent || !isManagedProvider(activeConfig, failure.provider)) return;
        // 渠道级真实可用性：只记「打的是哪一家」已知的情况，否则统计会串味。
        const failedPin = requestPin === activeSwitcher.activeChannel(failure.provider, failure.model) ? requestPin : null;
        if (failedPin?.channelId) {
          store.noteChannelOutcome({
            controlBase: failedPin.controlBase, model: failure.model,
            channelId: failedPin.channelId, ok: false, latencyMs: failure.postContent ? failure.latencyMs : 0,
          });
          // 已经吐过字才失败时本轮不能重放（会重复内容），但这家明显有问题：
          // 先短期拉黑，让下一次请求直接换一家，别再撞同一堵墙。
          if (failure.postContent && activeConfig.badOnPostContentFailure !== false) {
            store.noteBadChannel(failedPin.controlBase, failure.model, failedPin.channelId, failure.code || '',
              Number.isFinite(activeConfig.postContentBadTtlMs) ? activeConfig.postContentBadTtlMs : 30 * 60 * 1000);
            store.logEvent({
              type: 'channel-quarantined',
              provider: failure.provider,
              model: failure.model,
              channelId: failedPin.channelId,
              code: failure.code,
              ttlMs: Number.isFinite(activeConfig.postContentBadTtlMs) ? activeConfig.postContentBadTtlMs : 30 * 60 * 1000,
            });
          }
        }
        if (failure.postContent && failure.recoverable && activeConfig.maxSwitchesPerCall > 0 && !options.signal?.aborted) {
          void activeSwitcher.planSwitch({ provider: failure.provider, model: failure.model,
            failedGeneration: requestGeneration, failedChannelId: requestPin?.channelId,
            code: failure.code }).catch(() => log('流停顿后未能确认备用商家'));
        }
        const entry = store.noteFailure(failure.provider, failure.model, {
          code: failure.code,
          degradeAfter: activeConfig.degradeAfterFailures,
          cooldownMs: activeConfig.degradeCooldownMs,
        });
        store.logEvent({
          type: 'model-failure',
          provider: failure.provider,
          model: failure.model,
          kind: failure.kind,
          code: failure.code,
          postContent: Boolean(failure.postContent),
          fails: entry.fails,
          degraded: entry.degradedUntil > Date.now(),
        });
        log(`${failure.provider}/${failure.model} 异常（${failure.kind} ${failure.code}）`);
      },
      onSuccess: (route, meta) => {
        if (isManagedProvider(activeConfig, route.provider)) {
          store.noteSuccess(route.provider, route.model);
          const okPin = requestPin === activeSwitcher.activeChannel(route.provider, route.model) ? requestPin : null;
          if (okPin?.channelId) {
            store.noteChannelOutcome({
              controlBase: okPin.controlBase, model: route.model,
              channelId: okPin.channelId, ok: true, latencyMs: meta?.latencyMs,
            });
          }
        }
        const sessionId = String(options?.sessionId || '');
        if (sessionId) sessionHints.delete(sessionId);
      },
      onUsage: (usage, route) => {
        if (!requestPin || activeSwitcher !== switcher || routeAtOpen?.provider !== route.provider
          || routeAtOpen?.model !== route.model || requestPin !== activeSwitcher.activeChannel(route.provider, route.model)) return;
        const channelId = requestPin.channelId;
        const cacheRead = Number(usage?.cacheReadTokens || 0);
        const cacheWrite = Number(usage?.cacheWriteTokens || 0);
        const uncachedInput = Number(usage?.inputTokens || 0);
        const outputTokens = Number(usage?.outputTokens || 0);
        if (!(cacheRead + cacheWrite + uncachedInput > 0)) return;
        const stat = store.noteUsage({
          controlBase: requestPin.controlBase, model: route.model, channelId, cacheRead, cacheWrite, uncachedInput,
          output: outputTokens,
        });
        if (stat) {
          store.logEvent({
            type: 'cache-sample',
            provider: route.provider,
            model: route.model,
            channelId,
            rate: Number(stat.rate.toFixed(4)),
            samples: Math.round(stat.samples),
            promptTokens: Math.round(stat.promptTokens),
          });
        }
      },
      onEvent: (event) => {
        const { message, error, ...summary } = event;
        store.logEvent(summary);
        if (event.type === 'switched') {
          const to = event.to || {};
          log(`已切换：${event.from?.provider}/${event.from?.model} → ${to.provider}/${to.model}（${event.mode} ${event.channelName || event.channelId || ''}）`);
        }
      },
    });
    return (async function* monitoredRequest() {
      try {
        const release = await activeSwitcher.beginRequest(provider, options.model, options.signal);
        leases.set(JSON.stringify([provider, options.model]), release);
      } catch (error) {
        options.signal?.throwIfAborted();
        if (error?.code !== 'A6_ROUTE_NOT_READY') throw error;
        yield { type: 'finish', reason: { kind: 'error', failure: { code: error.code, message: error.message } } };
        return;
      }
      try { yield* guarded(); } finally { for (const finish of leases.values()) finish(); }
    })();
  });

  /**
   * 上下文/压缩类失败的处置：换商家没用（同一个模型窗口装不下），
   * 只能把这一步换到**上下文更大的路由**上，再让 agent 重跑。
   */
  async function handleContextFailure({ payload, failure, code, signal, downstream, current }) {
    if (!cfg.autoSwitchOnContextFailure) return downstream;
    const agent = payload?.agent;
    const model = agent?.model;
    if (!model || typeof model.select !== 'function') {
      store.logEvent({ type: 'context-switch-unavailable', code, reason: '会话没有 model.select（DSH 版本不支持）' });
      return downstream;
    }
    if (!allowContextSwitch()) {
      store.logEvent({ type: 'context-switch-rate-limited', code, switches: contextSwitches.length });
      return downstream;
    }

    const numbers = parseContextNumbers(failure.message);
    let reserve = 0;
    let currentWindow = 0;
    if (llmRuntime && current?.provider && current?.model) {
      try {
        const info = await llmRuntime.resolveModelInfo(current.provider, current.model, signal);
        currentWindow = Number(info?.context?.contextWindow) || 0;
        if (currentWindow > numbers.threshold && numbers.threshold > 0) reserve = currentWindow - numbers.threshold;
        else reserve = Number(info?.defaultMaxTokens) || 0;
      } catch { /* 查不到就按 0 处理，后面用候选自己的 defaultMaxTokens */ }
    }

    if (!llmRuntime) {
      store.logEvent({ type: 'context-switch-unavailable', code, reason: 'llm 服务不可用，查不到模型窗口' });
      return downstream;
    }

    const routes = [...cfg.contextFallbackRoutes, ...cfg.fallbackRoutes]
      .filter((route) => route.provider !== current.provider || route.model !== current.model);
    if (!routes.length || !(numbers.estimated || currentWindow) || signal?.aborted) return downstream;
    const picked = await findContextRoute({
      llm: llmRuntime,
      routes,
      estimated: numbers.estimated || currentWindow,
      reserve,
      headroomRatio: cfg.contextHeadroomRatio,
      signal,
    });

    if (signal?.aborted || !allowContextSwitch()) return downstream;
    if (!picked.ok) {
      store.logEvent({
        type: 'context-switch-unavailable',
        code,
        estimated: numbers.estimated,
        threshold: numbers.threshold,
        currentWindow,
        reason: picked.reason,
        scanned: picked.scanned,
      });
      log(`上下文不够且没有更大的路由：${picked.reason}`);
      return downstream;
    }

    const target = picked.route;
    try {
      model.select({ provider: target.provider, model: target.model });
    } catch (error) {
      store.logEvent({ type: 'context-switch-unavailable', code, reason: '选择模型失败' });
      return downstream;
    }

    contextSwitches.push(Date.now());
    store.logEvent({
      type: 'context-switched',
      code,
      from: current ? { provider: current.provider, model: current.model } : null,
      to: { provider: target.provider, model: target.model },
      estimated: numbers.estimated,
      threshold: numbers.threshold,
      currentWindow,
      targetWindow: target.contextWindow,
      capacity: target.capacity,
    });
    log(`上下文塞不下（估算 ${numbers.estimated} > 阈值 ${numbers.threshold}），已把这一步换到 `
      + `${target.provider}/${target.model}（窗口 ${target.contextWindow}），重跑而不是终止`);
    return { kind: 'retry' };
  }

  /** 模型流的故障重试由流层统一处理。这里仅处理显式配置的上下文兜底。 */
  ctx.on('agent/request-error', async (payload, next) => {
    let downstream;
    try {
      downstream = typeof next === 'function' ? await next() : undefined;
    } catch (error) {
      downstream = undefined;
      log(`agent/request-error 下游抛错：${error?.message || error}`);
    }
    if (downstream && downstream.kind === 'retry') return downstream;
    if (!cfg.enabled) return downstream;

    const signal = payload?.signal;
    if (signal?.aborted) return downstream;

    const failure = payload?.failure || {};
    const code = String(failure.code || '').toUpperCase();

    if (!CONTEXT_FAILURE_CODES.has(code)) return downstream;

    const agent = payload?.agent;
    const sessionId = String(agent?.id || agent?.session?.id || '');
    const hint = sessionId ? sessionHints.get(sessionId) : null;
    const fresh = hint && Date.now() - hint.at < SESSION_HINT_TTL_MS;

    if (fresh && hint.postContent) return downstream;
    const current = agent?.model?.current?.() || (fresh && hint.code === code ? hint : null);
    if (!current?.model || !isManagedProvider(cfg, current.provider)) return downstream;
    return handleContextFailure({ payload, failure, code, signal, downstream, current });
  });

  // 设置页的数据源：只读快照 + 一个「重新拉商家表」的按钮。
  // 用 webServer 起两条本机路由（和 dsh-purge 一样的做法），前端只用 fetch。
  const isLocalRequest = (request, webServer) => {
    const address = String(request?.socket?.remoteAddress || '');
    if (!['127.0.0.1', '::1', '::ffff:127.0.0.1'].includes(address)) return false;
    const { host, origin, 'sec-fetch-site': site } = request.headers || {};
    const port = String(request.socket.localPort || webServer.port || '');
    if (!port || !['127.0.0.1', 'localhost', '[::1]'].some((name) => host === `${name}:${port}`)) return false;
    if (site && site !== 'same-origin' && site !== 'none') return false;
    return !origin || origin === `${webServer.protocol || 'http:'}//${host}`;
  };
  const sendJson = (response, code, payload) => {
    const body = JSON.stringify(payload);
    response.writeHead(code, {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
      'content-length': Buffer.byteLength(body),
    });
    response.end(body);
  };
  const readJsonBody = (request) => new Promise((resolve, reject) => {
    let bytes = 0;
    const chunks = [];
    request.on('data', (chunk) => {
      bytes += chunk.length;
      if (bytes <= 65536) chunks.push(chunk);
      else reject(new Error('请求体超过 64 KiB'));
    });
    request.once('end', () => {
      try {
        const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        if (!body || typeof body !== 'object' || Array.isArray(body)) throw new Error();
        resolve(body);
      } catch { reject(new Error('请求体必须是 JSON 对象')); }
    });
    request.once('error', () => reject(new Error('读取请求体失败')));
    request.once('aborted', () => reject(new Error('请求已取消')));
  });
  const allowsMutation = (request) => request.headers['x-a6-failover'] === 'settings'
    && (request.method !== 'POST' || /^application\/json(?:\s*;|$)/i.test(request.headers['content-type'] || ''));

  const statusPayload = () => {
    const payload = buildStatus({ cfg, store, switcher, version: VERSION });
    payload.strategy = switcher.strategyStatus();
    payload.prewarmRoutes = cfg.prewarmRoutes.map(({ provider, model }) => ({
      provider, model, channelId: switcher.activeChannel(provider, model)?.channelId ?? null,
    }));
    payload.overrides = {
      file: overridesPath(dshHome),
      keys: Object.keys(overrides || {}),
      values: forDisplay(overrides, patchConfig),
      patchKeys: Object.keys(patchConfig || {}),
    };
    return payload;
  };

  const configPayload = () => ({
    ok: true,
    version: VERSION,
    file: overridesPath(dshHome),
    // 密钥不回传明文，只告诉前端「有没有设过」；没设过的字段用默认值把表单填满
    values: { ...defaultsForForm(), ...forDisplay(cfg) },
    effective: {
      providerPattern: cfg.providerPattern,
      defaultControlBase: cfg.defaultControlBase,
      maxEffectiveRatio: cfg.maxEffectiveRatio,
      cacheHitRate: cfg.cacheHitRate,
      outputToInputRatio: cfg.outputToInputRatio,
      priceWeight: cfg.priceWeight,
      reliabilityWeight: cfg.reliabilityWeight,
      cacheWeight: cfg.cacheWeight,
      cacheFallbackDiscount: cfg.cacheFallbackDiscount,
      cacheMeasureMinSamples: cfg.cacheMeasureMinSamples,
      latencyWeight: cfg.latencyWeight,
      successWeight: cfg.successWeight,
      maxSwitchesPerCall: cfg.maxSwitchesPerCall,
      maxSwitchesPerHour: cfg.maxSwitchesPerHour,
    },
    overriddenKeys: Object.keys(overrides || {}),
    patchKeys: Object.keys(patchConfig || {}),
  });

  try {
    ctx.inject?.(['webServer'], (host) => {
      try {
        host.effect?.(() => host.webServer.register({
          kind: 'exact',
          path: '/a6-failover/status',
          handler: async (request, response) => {
            if (!isLocalRequest(request, host.webServer)) return sendJson(response, 403, { ok: false, error: 'local origin only' });
            if (request.method !== 'GET') return sendJson(response, 405, { ok: false, error: 'GET only' });
            try {
              sendJson(response, 200, statusPayload());
            } catch (error) {
              sendJson(response, 500, { ok: false, error: '读取插件状态失败' });
            }
          },
        }, 'a6-failover status'));

        host.effect?.(() => host.webServer.register({
          kind: 'exact', path: '/a6-failover/strategy',
          handler: async (request, response) => {
            if (!isLocalRequest(request, host.webServer)) return sendJson(response, 403, { ok: false });
            if (request.method !== 'POST') return sendJson(response, 405, { ok: false });
            if (!allowsMutation(request)) return sendJson(response, 403, { ok: false });
            let body;
            try { body = await readJsonBody(request); } catch { return sendJson(response, 400, { ok: false }); }
            if (!['platform', 'smart'].includes(body.mode)) return sendJson(response, 400, { ok: false });
            if (!cfg.enabled && body.mode === 'smart') return sendJson(response, 409, { ok: false, error: '请先启用插件' });
            try {
              const routes = cfg.prewarmRoutes.filter(r => isManagedProvider(cfg, r.provider));
              const result = await switcher.setStrategy(body.mode, routes);
              const ok = result?.ok !== false;
              return sendJson(response, ok ? 200 : 503, { ok, strategy: switcher.strategyStatus(), error: ok ? undefined : '策略变更尚未获平台确认' });
            } catch { return sendJson(response, 503, { ok: false, error: '策略变更尚未获平台确认' }); }
          },
        }, 'a6-failover strategy'));

        host.effect?.(() => host.webServer.register({
          kind: 'exact',
          path: '/a6-failover/config',
          handler: async (request, response) => {
            if (!isLocalRequest(request, host.webServer)) return sendJson(response, 403, { ok: false, error: 'local origin only' });
            try {
              if (request.method === 'GET') return sendJson(response, 200, configPayload());
              if (!allowsMutation(request)) return sendJson(response, 403, { ok: false, error: 'settings request required' });
              if (request.method === 'DELETE') {
                applyOverrides(clearOverrides(dshHome));
                store.logEvent({ type: 'config-cleared' });
                return sendJson(response, 200, { ok: true, ...configPayload() });
              }
              if (request.method !== 'POST') return sendJson(response, 405, { ok: false, error: 'GET/POST/DELETE' });
              let body;
              try { body = await readJsonBody(request); } catch (error) {
                return sendJson(response, 400, { ok: false, error: error.message });
              }
              if (Object.hasOwn(body, 'defaultControlBase')) {
                const target = controlOrigin(body.defaultControlBase);
                if (!target) return sendJson(response, 400, { ok: false, error: '控制面必须是 HTTPS 站点地址' });
                if (cfg.accessToken && target !== controlOrigin(cfg.defaultControlBase)
                  && (typeof body.accessToken !== 'string' || !body.accessToken.trim()
                    || body.accessToken.trim() === cfg.accessToken || !String(body.userId || '').trim())) {
                  return sendJson(response, 400, { ok: false, error: '更换控制面时请同时填写新站点的令牌和用户 ID' });
                }
              }
              const next = saveOverrides(dshHome, body);
              const before = {
                providerPattern: cfg.providerPattern,
                maxEffectiveRatio: cfg.maxEffectiveRatio,
                cacheHitRate: cfg.cacheHitRate,
                priceWeight: cfg.priceWeight,
                latencyWeight: cfg.latencyWeight,
              };
              const after = applyOverrides(next);
              store.logEvent({
                type: 'config-saved',
                keys: Object.keys(next || {}),
                changed: Object.entries(before)
                  .filter(([key, value]) => String(after[key]) !== String(value))
                  .map(([key, value]) => ({ key, from: value, to: after[key] })),
              });
              log(`设置已更新（立即生效）：${Object.keys(next || {}).length} 项`);
              sendJson(response, 200, {
                ok: true,
                applied: true,
                ...configPayload(),
                status: statusPayload(),
              });
            } catch (error) {
              sendJson(response, 500, { ok: false, error: '保存插件设置失败' });
            }
          },
        }, 'a6-failover config'));

        host.effect?.(() => host.webServer.register({
          kind: 'exact',
          path: '/a6-failover/refresh',
          handler: async (request, response) => {
            if (!isLocalRequest(request, host.webServer)) return sendJson(response, 403, { ok: false, error: 'local origin only' });
            if (request.method !== 'POST') return sendJson(response, 405, { ok: false, error: 'POST only' });
            if (!allowsMutation(request)) return sendJson(response, 403, { ok: false, error: 'settings request required' });
            let body;
            try { body = await readJsonBody(request); } catch (error) {
              return sendJson(response, 400, { ok: false, error: error.message });
            }
            const provider = String(body.provider || '');
            const model = String(body.model || '');
            const controlBase = String(body.controlBase || '');
            if (!model || model.length > 200 || (!provider && !controlBase) || (provider && !isManagedProvider(cfg, provider))) {
              return sendJson(response, 400, { ok: false, error: '需要 model，以及 provider 或 controlBase' });
            }
            try {
              const site = provider ? switcher.resolveSite(provider) : switcher.resolveSiteByBase(controlBase);
              if (!site.ok) return sendJson(response, 400, { ok: false, error: site.reason });
              const candidates = await switcher.loadCandidates(site, model, new Set(), { force: true });
              sendJson(response, 200, {
                ok: true,
                provider: provider || '',
                model,
                controlBase: site.controlBase,
                candidates: candidates.length,
                top: candidates.slice(0, 10),
                status: statusPayload(),
              });
            } catch (error) {
              sendJson(response, 503, { ok: false, error: '刷新商家报价失败，请稍后重试' });
            }
          },
        }, 'a6-failover refresh'));
        log(`设置页接口已就绪：/a6-failover/status、/a6-failover/refresh`);
      } catch (error) {
        log(`注册设置页接口失败：${error?.message || error}`);
      }
    });
  } catch (error) {
    log(`webServer 注入不可用：${error?.message || error}`);
  }

  // 收尾：把统计落盘。cordis 的 effect 不存在时就挂到进程退出上。
  let disposed = false;
  const dispose = () => {
    if (disposed) return;
    disposed = true;
    switcher.dispose();
    try { store.dispose(); } catch { /* ignore */ }
  };
  try {
    if (typeof ctx.effect === 'function') ctx.effect(() => dispose);
    else process.once('exit', dispose);
  } catch {
    process.once('exit', dispose);
  }

  store.logEvent({
    type: 'boot',
    version: VERSION,
    providerPattern: cfg.providerPattern,
    controlBase: cfg.defaultControlBase,
    maxSwitchesPerCall: cfg.maxSwitchesPerCall,
    fallbackRoutes: cfg.fallbackRoutes.length,
    prewarmRoutes: cfg.prewarmRoutes.length,
    fallbackToSmartRouting: cfg.fallbackToSmartRouting,
  });
  log(`已启用：provider 匹配 ${cfg.providerPattern}，控制面 ${cfg.defaultControlBase}，单次最多切 ${cfg.maxSwitchesPerCall} 次`);
}
