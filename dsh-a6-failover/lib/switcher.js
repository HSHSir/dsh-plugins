/**
 * 换商家：把「这次失败了」变成「换一家再来」。
 *
 * 手段按代价从低到高：
 *   1. pin 另一家（最精确：站点按 (令牌, 模型) 维度记住固定关系）
 *   2. 换已配置的 DSH 备用路由。
 */

import {
  A6Error, bareKey, controlOrigin, fetchChannels, fetchSubscriptions, listPins, listTokens, pinChannel, unpinChannel, tokenPlainKey,
} from './a6.js';
import { createHash } from 'node:crypto';
import { Worker } from 'node:worker_threads';
import { cacheRatesFromSubscriptions, rankFastestHalf, refreshCandidateCache } from './selector.js';
import { guessControlBase, isManagedProvider } from './config.js';

function normBase(url) {
  return controlOrigin(url);
}

const TOKEN_TTL_MS = 6 * 3600 * 1000;
const QUOTE_MAX_AGE_MS = 60 * 60 * 1000;

function identity(site) {
  return createHash('sha256').update(JSON.stringify([site.controlBase, site.userId, site.accessToken])).digest('hex');
}

function boundedSet(map, key, value) {
  if (!map.has(key) && map.size >= 128) map.delete(map.keys().next().value);
  map.set(key, value);
}

function hostOf(url) {
  const text = String(url || '').trim();
  if (!text) return '';
  try {
    return new URL(text.includes('://') ? text : `https://${text}`).hostname.toLowerCase();
  } catch {
    return '';
  }
}

/** provider 名尾部的令牌标签：a6api-a6-1 → a6-1 */
export function tokenLabelFromProvider(provider) {
  const match = /([a-z0-9]+-\d+)$/i.exec(String(provider || ''));
  return match ? match[1] : '';
}

export function createSwitcher({ cfg, store, resolver, log, routing = () => ({ sites: {}, bad: {} }) }) {
  const tokenCache = new Map();
  /** 控制面按账号、令牌和模型共享 pin，多个 provider 可以指向同一条记录。 */
  const lastSwitch = new Map();
  const routePins = new Map();
  const changes = new Map();
  /** 正在进行的切换：并发的那次调用等它，不要各自 pin 一遍 */
  const inflight = new Map();


  const knownRoutes = new Map();
  const candidateLoads = new Map();
  const subscriptions = new Map();
  let pendingMutations = 0;
  const controlBlockedUntil = new Map();
  const lifetime = new AbortController();
  // Persisted candidates were filtered and scored under a specific configuration.
  // 报价线程只拿这份子集算分：漏一个键，线程里就悄悄退回默认值（缓存命中率 0.9 就是这么漏掉的）。
  const quoteKeys = ['maxEffectiveRatio', 'maxPriceRatio', 'cacheHitRate', 'officialCacheHitRate',
    'merchantCacheHitRate', 'unmeasuredMerchantCacheHitRate', 'outputToInputRatio', 'useMeasuredOutputRatio',
    'outputRatioMinSamples', 'cacheWriteShare', 'assumeMissingCacheAsInput', 'fxCnyPerUsd', 'hardP50Ms',
    'maxFailRate', 'minSamples', 'priceWeight', 'reliabilityWeight', 'cacheWeight', 'cacheFallbackDiscount',
    'cacheMeasureMinSamples', 'latencyWeight', 'successWeight', 'latencyRefMs'];
  let quoteConfig = Object.fromEntries(quoteKeys.map((key) => [key, cfg[key]]));
  let selectionKey = JSON.stringify([3, ...Object.values(quoteConfig)]);

  /** 只把近期确认、且禁用站点智能兜底的 pin 作为本地采样依据。 */
  function activeChannel(provider, model) {
    const key = routePins.get(`${provider}|${model}`);
    if (pendingRestore.has(key)) return null;
    const row = lastSwitch.get(key);
    return !lifetime.signal.aborted && row?.attribution ? row : null;
  }

  function bindPin(provider, model, account, tokenId) {
    const pinKey = JSON.stringify([account, tokenId, model]);
    boundedSet(routePins, `${provider}|${model}`, pinKey);
    return pinKey;
  }

  /** 站点上下文：控制面地址 + 凭据。 */
  function resolveSite(provider) {
    const override = Object.hasOwn(cfg.providerOverrides, provider) ? cfg.providerOverrides[provider] || {} : {};
    const providerBaseURL = typeof resolver?.baseURL === 'function' ? resolver.baseURL(provider) : '';
    // 显式写的控制面地址照用；只拿到 API 地址时要从它推（api.a6api.vip → a6api.vip）
    const defaultBase = normBase(cfg.defaultControlBase);
    const controlBase = normBase(override.controlBase || (providerBaseURL ? guessControlBase(providerBaseURL) : defaultBase));
    if (!controlBase || !hostOf(controlBase).includes('.')) {
      return { ok: false, reason: `provider ${provider} 没有可用的控制面地址` };
    }

    const credentials = credentialsFor(controlBase, override);
    if (!credentials) return { ok: false, reason: `没有 ${controlBase} 的控制台凭据` };
    return {
      ok: true,
      controlBase,
      ...credentials,
      tokenIdOverride: Number(override.tokenId || 0) || 0,
      tokenName: String(override.tokenName || tokenLabelFromProvider(provider) || ''),
      apiKey: String(override.apiKey || ''),
    };
  }

  function credentialsFor(controlBase, override = {}) {
    const configured = controlBase === normBase(cfg.defaultControlBase)
      ? { accessToken: cfg.accessToken, userId: cfg.userId } : {};
    const own = override.accessToken || override.userId
      ? { accessToken: override.accessToken, userId: override.userId } : configured;
    const complete = (source) => {
      const accessToken = String(source.accessToken || '').trim();
      const userId = String(source.userId || '').trim();
      return accessToken && userId ? { accessToken, userId } : null;
    };
    const direct = complete(own);
    if (direct) return direct;
    if (cfg.credentialSources.includes('model_probe')) {
      const { sites } = routing() || {};
      for (const [key, site] of Object.entries(sites || {})) {
        if (!site || typeof site !== 'object') continue;
        const rawBase = String(site.control_base || key);
        const siteBase = normBase(rawBase.includes('://') ? rawBase : `https://${rawBase}`);
        if (siteBase !== controlBase) continue;
        const pair = complete({ accessToken: site.access_token, userId: site.user_id });
        if (pair) return pair;
      }
    }
    return null;
  }

  /** 只按控制面地址拿凭据（设置页刷新商家表用：不需要令牌，也不需要 provider）。 */
  function resolveSiteByBase(controlBaseRaw) {
    const controlBase = normBase(controlBaseRaw);
    if (!controlBase) return { ok: false, reason: '控制面必须是 HTTPS 站点地址' };
    const credentials = credentialsFor(controlBase);
    if (!credentials) return { ok: false, reason: `没有 ${controlBase} 的控制台凭据` };
    return { ok: true, controlBase, ...credentials, tokenIdOverride: 0, tokenName: '', apiKey: '' };
  }

  /** 把 DSH 的 provider 对回站点的 token_id。 */
  async function resolveToken(site, provider, request = {}) {
    if (site.tokenIdOverride > 0) return { tokenId: site.tokenIdOverride, name: site.tokenName, source: 'config' };

    const cacheKey = `${identity(site)}|${provider}`;
    const memo = tokenCache.get(cacheKey);
    if (memo && memo.at > Date.now() - TOKEN_TTL_MS) return memo;

    const cached = store.getToken(identity(site), provider);
    if (cached?.tokenId && cached.at > Date.now() - TOKEN_TTL_MS) {
      boundedSet(tokenCache, cacheKey, cached);
      return cached;
    }

    let tokens = [];
    try {
      tokens = await listTokens({ ...site, timeoutMs: cfg.controlTimeoutMs, ...request });
    } catch (error) {
      throw error instanceof A6Error ? error : new A6Error(String(error));
    }

    let picked = null;
    if (site.apiKey && bareKey(site.apiKey).length >= 8) {
      const want = bareKey(site.apiKey);
      const head = want.slice(0, 4);
      const tail = want.slice(-4);
      const near = tokens.filter((row) => {
        const masked = bareKey(String(row?.key || ''));
        return masked.startsWith(head) && masked.endsWith(tail);
      });
      for (const row of near.slice(0, 3)) {
        if (request.signal?.aborted) break;
        const id = Number(row?.id || 0);
        if (!id) continue;
        try {
          const plain = await tokenPlainKey({ ...site, tokenId: id, timeoutMs: cfg.controlTimeoutMs, ...request });
          if (plain && bareKey(plain) === want) { picked = row; break; }
        } catch { /* 某个令牌读不到明文就跳过 */ }
      }
    } else if (!site.apiKey && site.tokenName) {
      const named = tokens.filter((row) => String(row?.name || '') === site.tokenName);
      if (named.length === 1) picked = named[0];
    }

    if (!Number.isSafeInteger(Number(picked?.id)) || Number(picked.id) <= 0) {
      throw new A6Error(
        `无法唯一确认 ${provider} 的令牌。可在 providerOverrides 中指定 tokenId。`,
        { code: 'A6_TOKEN_UNRESOLVED' },
      );
    }

    const info = { tokenId: Number(picked.id), name: String(picked.name || ''), at: Date.now() };
    boundedSet(tokenCache, cacheKey, info);
    store.setToken(identity(site), provider, { tokenId: info.tokenId, name: info.name });
    return info;
  }

  async function currentPin(site, tokenId, model, request = {}) {
      const pins = await listPins({ ...site, timeoutMs: cfg.controlTimeoutMs, ...request });
      const row = pins.find((item) => Number(item?.token_id || 0) === tokenId
        && String(item?.model_name || '') === model);
      if (!row) return null;
      return {
        channelId: Number(row.channel_id || row.channelId || 0),
        channelName: String(row.channel_name || ''),
        strict: row.status === 'active' && row.route_effective === true
          && (row.fallback_to_smart_routing === false || row.fallback_to_smart_routing === 0),
      };
  }

  /** 模型管家那份 bad 表也当先验：那是同一台机器上踩过的坑。 */
  function badChannels(controlBase, model) {
    const host = hostOf(controlBase);
    const hostOnly = host.replace(/^api\./, '');
    const out = new Set();
    const own = store.badChannels(controlBase, model);
    for (const id of own) out.add(id);
    const { bad } = routing() || {};
    for (const [key, rows] of Object.entries(bad || {})) {
      const [badHost, badModel] = String(key).split('|');
      if (badModel !== model) continue;
      if (badHost !== host && badHost !== hostOnly) continue;
      for (const id of Object.keys(rows || {})) {
        const at = Number(rows[id]?.at || rows[id]?.ts || 0) * 1000;
        if (at && Date.now() - at > 24 * 3600 * 1000) continue;
        out.add(Number(id));
      }
    }
    return out;
  }

  function subscriptionRates(site, account, force) {
    if (cfg.cacheWeight === 0) return null;
    const existing = subscriptions.get(account);
    if (existing && (!force || !existing.done) && Date.now() < existing.until) return existing.task;
    const entry = { until: Date.now() + 60000, done: false };
    entry.task = fetchSubscriptions({ ...site, timeoutMs: Math.min(cfg.controlTimeoutMs, 2000),
      signal: lifetime.signal, maxBytes: Math.min(cfg.maxChannelsBytes, 16 * 1024 * 1024),
    }).then(cacheRatesFromSubscriptions).catch(() => null).then((rates) => {
      entry.done = true;
      entry.until = Date.now() + (rates?.size ? 5 * 60000 : 60000);
      return rates;
    });
    boundedSet(subscriptions, account, entry);
    return entry.task;
  }

  async function refreshCandidates(site, model, account, force, quoteConfig, selectionKey) {
    const [text, cacheRates] = await Promise.all([
      fetchChannels({ ...site, modelName: model, timeoutMs: Math.min(cfg.controlTimeoutMs, 30000),
        signal: lifetime.signal, maxBytes: cfg.maxChannelsBytes, rawJson: true }),
      subscriptionRates(site, account, force),
    ]);
    if (lifetime.signal.aborted) return [];
    const worker = new Worker(new URL('./quote-worker.js', import.meta.url), {
      execArgv: [],
      workerData: { text, model, cfg: quoteConfig, cacheRates,
        measuredRates: store.usageRates(model, site.controlBase),
        measuredStats: store.channelStats(model, site.controlBase) },
    });
    const result = new Promise((resolve, reject) => {
      worker.once('message', resolve);
      worker.once('error', reject);
      worker.once('exit', () => reject(new A6Error('报价处理提前退出', { code: 'A6_BAD_QUOTES' })));
    });
    let parsed;
    try { parsed = await waitFor(result, lifetime.signal); }
    finally { await worker.terminate(); }
    if (!parsed) throw new A6Error('商家列表格式无效', { code: 'A6_BAD_QUOTES' });
    const { total, rejected, candidates: trimmed, eligible } = parsed;
    store.setChannels(site.controlBase, model, trimmed, { total, rejected, selectionKey, account });
    const rateRows = cacheRates ? cacheRates.size : 0;
    const fromSite = trimmed.filter((row) => row.cacheSource === 'site-rate').length;
    const fromMeasured = trimmed.filter((row) => row.cacheSource === 'measured').length;
    const fromDiscount = trimmed.filter((row) => row.cacheSource === 'cache-read-cost').length;
    log?.(`商家列表：${total} 条 listing → 候选 ${eligible} 家，缓存前 ${trimmed.length} 家；`
      + `缓存率快照 ${rateRows} 条，实际取自站点 ${fromSite} 家、实测命中 ${fromMeasured} 家、缓存读成本 ${fromDiscount} 家`);
    return trimmed;
  }

  async function loadCandidates(site, model, excludeChannels = new Set(), { force = false, signal, allowStale = false } = {}) {
    const account = identity(site);
    const key = `${account}|${model}|${selectionKey}`;
    const cached = store.getChannels(site.controlBase, model);
    const compatible = cached?.selectionKey === selectionKey && cached.account === account;
    const age = Date.now() - (cached?.at || 0);
    const filter = (rows) => {
      const known = badChannels(site.controlBase, model);
      const measured = store.usageRates(model, site.controlBase);
      const stats = store.channelStats(model, site.controlBase);
      return rankFastestHalf(rows.filter((row) => !excludeChannels.has(row.channelId) && !known.has(row.channelId))
        .map((row) => refreshCandidateCache(row, cfg, measured, Date.now(), stats)), cfg);
    };
    if (!force && compatible && age < Math.min(cfg.channelCacheTtlMs, QUOTE_MAX_AGE_MS)) return filter(cached.candidates);
    let task = candidateLoads.get(key);
    if (!task && candidateLoads.size < 2 && !lifetime.signal.aborted) {
      task = refreshCandidates(site, model, account, force, quoteConfig, selectionKey).finally(() => candidateLoads.delete(key));
      candidateLoads.set(key, task);
      task.catch(() => {});
    }
    if (!force && allowStale && compatible && age < QUOTE_MAX_AGE_MS) {
      const available = filter(cached.candidates);
      if (available.length) return available;
    }
    if (!task) throw new A6Error('商家列表正在刷新，请稍后重试', { code: 'A6_BUSY' });
    return filter(await waitFor(task, signal));
  }

  function waitFor(task, signal) {
    if (!signal) return task;
    if (signal.aborted) return Promise.reject(signal.reason);
    return new Promise((resolve, reject) => {
      const cancel = () => reject(signal.reason);
      signal.addEventListener('abort', cancel, { once: true });
      task.then(resolve, reject).finally(() => signal.removeEventListener('abort', cancel));
    });
  }

  function fallbackRoute(current, reason, failedRoutes = new Set()) {
    const routes = cfg.fallbackRoutes || [];
    if (routes.length === 0) return { ok: false, reason: reason || '没有配置备用路由' };
    for (const route of routes) {
      if (route.provider === current.provider && route.model === current.model) continue;
      if (failedRoutes.has(`${route.provider}|${route.model}`)) continue;
      return {
        ok: true, mode: 'fallback-route', provider: route.provider, model: route.model, reason,
      };
    }
    return { ok: false, reason: reason || '备用路由均已尝试或与当前路由相同' };
  }

  /**
   * 执行一次切换。
   * @returns {{ ok: boolean, provider?: string, model?: string, mode?: string, channelId?: number, channelName?: string, reason?: string }}
   */
  const owned = new Map(Object.entries(store.ownedPins()));
  const pendingRestore = new Set(owned.keys());
  const sites = new Map();
  const generations = new Map();
  let serial = 0;
  let active = 0;
  let operations = 0;
  let manual = false;
  let policyEpoch = 0;
  let cleanup;
  let retryTimer;
  let idleTimer;
  let platformGeneration = 0;
  const managedNames = new Set();
  const sweptTokens = new Set();
  const sweeps = new Map();
  const sweepFailures = new Set();
  let sweepRetry;
  let restoringAll = false;

  function discover(names = []) {
    for (const p of [...names, ...(cfg.providers || []), ...Object.keys(cfg.providerOverrides || {}),
      ...(cfg.prewarmRoutes || []).map(r => r.provider), ...(cfg.fallbackRoutes || []).map(r => r.provider),
      ...[...owned.values()].map(r => r.provider)]) {
      if (p && isManagedProvider(cfg, p)) managedNames.add(p);
    }
  }

  function retrySweeps() {
    if (sweepRetry || lifetime.signal.aborted) return;
    sweepRetry = setTimeout(() => {
      sweepRetry = null;
      startRecovery().catch(() => {});
    }, 5000);
    sweepRetry.unref?.();
  }

  async function ensurePlatform(provider) {
    const site = resolveSite(provider);
    try {
      if (!site.ok) throw new Error(site.reason);
      const signal = AbortSignal.any([lifetime.signal, AbortSignal.timeout(12000)]);
      const token = await resolveToken(site, provider, { signal });
      if (!token?.tokenId) throw new Error('无法确定受管令牌');
      const tokenId = Number(token.tokenId);
      const key = JSON.stringify([site.controlBase, String(site.userId), tokenId]);
      if (sweptTokens.has(key)) { sweepFailures.delete(provider); return; }
      if (!sweeps.has(key)) {
        const task = (async () => {
          // 所有切换共享此屏障：先等待已经发出的 pin，再读取和清除该令牌全部模型。
          await Promise.allSettled([...inflight.values()]);
          const read = async () => (await listPins({ ...site, signal })).filter(r => Number(r.token_id) === tokenId);
          const rows = await read();
          for (const row of rows) {
            if (!row.model_name || !(Number(row.channel_id) > 0)) throw new Error('固定商家记录不完整');
            await unpinChannel({ ...site, signal, tokenId, modelName: row.model_name, channelId: row.channel_id });
          }
          if ((await read()).length) throw new Error('固定商家仍存在');
          for (const [k, claim] of owned) {
            if (claim.controlBase === site.controlBase && String(claim.userId) === String(site.userId) && Number(claim.tokenId) === tokenId) {
              journal(k, null); lastSwitch.delete(k); generations.set(k, ++serial);
            }
          }
          sweptTokens.add(key);
        })().finally(() => sweeps.delete(key));
        sweeps.set(key, task);
      }
      await sweeps.get(key);
      sweepFailures.delete(provider);
    } catch {
      sweepFailures.add(provider);
      retrySweeps();
      throw new A6Error('平台默认路由待恢复：尚未确认该令牌全部模型已解除固定商家', { code: 'A6_ROUTE_NOT_READY' });
    }
  }

  async function startRecovery(names = []) {
    if (!cfg.enabled || lifetime.signal.aborted) return;
    discover(names);
    await Promise.all([...managedNames].map(p => ensurePlatform(p)));
  }

  function journal(key, row) {
    store.setOwnedPin(key, row);
    if (row) owned.set(key, row);
    else {
      owned.delete(key);
      pendingRestore.delete(key);
      sites.delete(key);
    }
  }

  function generation(provider, model) {
    return Math.max(platformGeneration, generations.get(routePins.get(`${provider}|${model}`)) || 0);
  }

  function remember(provider, model) {
    const id = `${provider}|${model}`;
    if (!knownRoutes.has(id)) {
      if (knownRoutes.size >= 256) {
        const old = [...knownRoutes.keys()].find(k => !knownRoutes.get(k).active && !owned.has(routePins.get(k)));
        if (old) knownRoutes.delete(old);
      }
      knownRoutes.set(id, { provider, model, active: 0 });
    }
    return knownRoutes.get(id);
  }

  function strategyStatus() {
    for (const r of cfg.prewarmRoutes || []) remember(r.provider, r.model);
    for (const [key, r] of owned) {
      remember(r.provider, r.model);
      if (pendingRestore.has(key)) routePins.set(`${r.provider}|${r.model}`, key);
    }
    const routes = [...knownRoutes.values()].map(r => {
      const key = routePins.get(`${r.provider}|${r.model}`);
      const row = lastSwitch.get(key);
      const mode = pendingRestore.has(key) ? 'restoring' : row?.attribution ? (manual ? 'manual' : 'auto')
        : manual ? 'awaiting-request' : r.external ? 'unknown' : 'platform';
      const measured = row?.attribution ? store.channelStats(r.model, row.controlBase).get(row.channelId) : null;
      const cache = row?.attribution ? store.usageRates(r.model, row.controlBase).get(row.channelId) : null;
      const rate = Number.isFinite(cache?.rate) ? cache.rate : row?.cacheRate24h;
      return { ...(row || {}), provider: r.provider, model: r.model, active: r.active, mode,
        cacheRate: rate ?? null, cacheSource: Number.isFinite(cache?.rate) ? 'measured' : row?.cacheSource,
        p50Ms: measured?.latencySamples >= 2 ? measured.p50Ms : row?.realP50Ms ?? null,
        latencySource: measured?.latencySamples >= 2 ? 'measured' : row?.latencySource };
    });
    const merchants = [...owned.entries()].flatMap(([key, claim]) => {
      const row = lastSwitch.get(key);
      return row?.attribution ? [{ provider: claim.provider, model: claim.model, ...row }] : [];
    });
    return { routes, mode: manual || merchants.length ? 'smart' : 'platform', manual, activeRequests: active,
      pendingSwitches: operations, pendingRestore: pendingRestore.size, merchants,
      phase: pendingRestore.size || sweeps.size || sweepFailures.size ? 'restoring' : manual ? 'manual' : merchants.length ? 'automatic' : 'platform' };
  }

  function scheduleRetry() {
    if (retryTimer || lifetime.signal.aborted) return;
    retryTimer = setTimeout(() => { retryTimer = null; restorePending(); }, 5000);
    retryTimer.unref?.();
  }

  async function restoreOne(key) {
    const claim = owned.get(key);
    if (!claim) { pendingRestore.delete(key); return; }
    const site = sites.get(key) || resolveSite(claim.provider);
    if (!site.ok || site.controlBase !== claim.controlBase || String(site.userId) !== claim.userId) {
      throw new Error('原账号凭据不可用');
    }
    const signal = AbortSignal.any([lifetime.signal, AbortSignal.timeout(Math.min(cfg.controlTimeoutMs, 12000))]);
    const read = () => currentPin(site, claim.tokenId, claim.model, { signal });
    let pinned = await read();
    const ours = row => row?.strict && (row.channelId === claim.channelId || row.channelId === claim.previousChannelId);
    const matchesId = row => row && (row.channelId === claim.channelId || row.channelId === claim.previousChannelId);
    if (matchesId(pinned) && !ours(pinned)) throw new Error('固定商家身份或生效状态待核实');
    if (ours(pinned)) {
      await unpinChannel({ ...site, signal, tokenId: claim.tokenId, modelName: claim.model, channelId: pinned.channelId });
      pinned = await read();
      if (matchesId(pinned)) throw new Error('固定商家仍生效');
    }
    signal.throwIfAborted();
    // A different binding belongs to the platform/user; leave it untouched.
    remember(claim.provider, claim.model).external = Boolean(pinned);
    journal(key, null);
    generations.set(key, ++serial);
    lastSwitch.delete(key);
  }

  function restorePending() {
    if (cleanup || retryTimer || !pendingRestore.size || operations || lifetime.signal.aborted) return cleanup;
    const batch = [...pendingRestore];
    cleanup = Promise.all(batch.map(key => restoreOne(key).catch(() => {}))).finally(() => {
      cleanup = null;
      if (pendingRestore.size) scheduleRetry();
      else idle();
    });
    return cleanup;
  }

  function idle() {
    if (!manual && !active && !operations && owned.size && !pendingRestore.size && !idleTimer && !lifetime.signal.aborted) {
      idleTimer = setTimeout(() => {
        idleTimer = null;
        if (manual || active || operations) return;
        for (const key of owned.keys()) pendingRestore.add(key);
        restorePending();
      }, 1000);
      idleTimer.unref?.();
    }
    return restorePending();
  }

  function stopIdleTimer() {
    clearTimeout(idleTimer);
    idleTimer = null;
  }

  function affected(provider, model) {
    const relevant = [...pendingRestore].map(key => owned.get(key)).filter(row => row?.model === model);
    if (!relevant.length) return false;
    const site = resolveSite(provider);
    if (!site.ok) return relevant.some(row => row.provider === provider);
    return relevant.some(row => site.controlBase === row.controlBase && String(site.userId) === row.userId
      && (!site.tokenIdOverride || site.tokenIdOverride === row.tokenId));
  }

  async function beginRequest(provider, model, signal) {
    signal?.throwIfAborted();
    lifetime.signal.throwIfAborted();
    stopIdleTimer();
    const route = remember(provider, model);
    route.active++;
    active++;
    if (owned.size && !routePins.has(`${provider}|${model}`)) {
      const site = resolveSite(provider);
      if (site.ok && site.tokenIdOverride) bindPin(provider, model, JSON.stringify([site.controlBase, String(site.userId)]), site.tokenIdOverride);
    }
    let released = false;
    function release() {
      if (released) return;
      released = true;
      route.active--;
      active--;
      return idle();
    }
    try {
      if (restoringAll) throw new A6Error('正在恢复平台默认策略', { code: 'A6_ROUTE_NOT_READY' });
      managedNames.add(provider);
      await waitFor(ensurePlatform(provider), signal);
      if (affected(provider, model)) {
        await waitFor(Promise.resolve(restorePending()), signal);
        if (affected(provider, model)) throw new A6Error('平台默认路由待恢复', { code: 'A6_ROUTE_NOT_READY' });
      }
      if (manual && !activeChannel(provider, model)) {
        const result = await planSwitch({ provider, model, signal, initial: true });
        if (!result.ok) throw new A6Error(result.reason, { code: 'A6_ROUTE_NOT_READY' });
      }
      signal?.throwIfAborted();
      return release;
    } catch (error) { release(); throw error; }
  }

  async function setStrategy(mode, routes = []) {
    if (mode !== 'platform' && mode !== 'smart') throw new Error('未知路由策略');
    if (restoringAll) throw new Error('正在恢复平台默认策略');
    stopIdleTimer();
    manual = mode === 'smart';
    if (!manual) platformGeneration = ++serial;
    const epoch = ++policyEpoch;
    if (!manual) {
      restoringAll = true;
      await Promise.allSettled([...inflight.values()]);
      if (epoch !== policyEpoch) return strategyStatus();
      sweptTokens.clear();
      discover(routes.map(r => r.provider));
      try { await startRecovery(); } finally { restoringAll = false; }
    } else {
      discover(routes.map(r => r.provider));
      // 手动策略只改变模式，新请求才会固定该模型。
    }
    return strategyStatus();
  }

  async function mutate({ provider, model, site, tokenId, key, failedChannelId, failedChannels,
    failedGeneration, failure, initial, signal, epoch }) {
    if (!initial && failedGeneration < platformGeneration) return { ok: false, reason: '请求早于手动恢复平台策略' };
    const previous = lastSwitch.get(key);
    if (previous?.attribution && (initial || (failedGeneration !== undefined && failedGeneration < generations.get(key)))
      && previous.channelId !== failedChannelId && !failedChannels.has(previous.channelId)) {
      return { ok: true, ...previous, provider, model, mode: 'deduped' };
    }
    if (pendingRestore.has(key) || epoch !== policyEpoch) return { ok: false, reason: '正在恢复平台默认策略' };
    const pinned = await currentPin(site, tokenId, model, { signal });
    const claim = owned.get(key);
    if (pinned && (!claim || !pinned.strict || pinned.channelId !== claim.channelId)) {
      if (claim) { journal(key, null); lastSwitch.delete(key); generations.set(key, ++serial); }
      remember(provider, model).external = true;
      return { ok: false, reason: '保留平台已有的固定商家' };
    }
    remember(provider, model).external = false;
    if (failedChannelId && pinned?.channelId === failedChannelId) store.noteBadChannel(site.controlBase, model, failedChannelId, failure?.code || '');
    const exclude = new Set(failedChannels);
    if (!initial && pinned) exclude.add(pinned.channelId);
    if (failedChannelId) exclude.add(failedChannelId);
    const [candidate] = await loadCandidates(site, model, exclude, { signal, allowStale: !initial });
    if (!candidate) return { ok: false, reason: '没有满足价格与质量门槛的商家' };
    signal.throwIfAborted();
    if (epoch !== policyEpoch) return { ok: false, reason: '路由策略已修改' };
    if (!initial && !store.allowSwitch(cfg.maxSwitchesPerHour - pendingMutations)) return { ok: false, reason: '本小时切换次数已达上限' };
    pendingMutations++;
    try {
      const record = { provider, model, controlBase: site.controlBase, userId: String(site.userId), tokenId,
        channelId: candidate.channelId, previousChannelId: claim?.channelId || null };
      journal(key, record);
      sites.set(key, site);
      lastSwitch.delete(key);
      generations.set(key, ++serial);
      try {
        await pinChannel({ ...site, signal, tokenId, modelName: model, channelId: candidate.channelId, fallbackToSmartRouting: false });
        const confirmed = await currentPin(site, tokenId, model, { signal });
        if (!confirmed?.strict || confirmed.channelId !== candidate.channelId) throw new Error('固定商家回查失败');
        journal(key, { ...record, previousChannelId: null });
      } catch (error) { pendingRestore.add(key); throw error; }
      const row = { ...candidate, mode: 'pin', at: Date.now(), controlBase: site.controlBase, attribution: true };
      lastSwitch.set(key, row);
      if (!initial) store.noteSwitch(provider, model);
      store.logEvent({ type: 'pin', provider, model, channelId: row.channelId, effRatio: row.effRatio });
      return { ok: true, provider, model, ...row };
    } finally { pendingMutations--; }
  }

  async function planSwitch(options) {
    const { provider, model, signal, failedRoutes = new Set(), initial = false } = options;
    if (lifetime.signal.aborted || signal?.aborted) return { ok: false, reason: '已取消' };
    if (restoringAll || sweeps.size || sweepFailures.has(provider)) return { ok: false, reason: '平台默认路由待恢复' };
    const epoch = policyEpoch;
    const timeout = AbortSignal.timeout(Math.min(cfg.controlTimeoutMs, 12000));
    const control = AbortSignal.any([lifetime.signal, timeout, ...(signal ? [signal] : [])]);
    async function run() {
      if (cleanup) await waitFor(cleanup, control);
      const site = resolveSite(provider);
      if (!site.ok) return { ok: false, reason: site.reason };
      const { tokenId } = await resolveToken(site, provider, { signal: control });
      const account = JSON.stringify([site.controlBase, String(site.userId)]);
      const key = bindPin(provider, model, account, tokenId);
      if (pendingRestore.has(key)) return { ok: false, reason: '平台默认路由待恢复' };
      const running = changes.get(key);
      if (running) {
        const result = await waitFor(running, control);
        return result.ok ? { ...result, provider, model, mode: 'deduped' } : result;
      }
      if (Date.now() < (controlBlockedUntil.get(account) || 0)) return { ok: false, reason: '控制面处于冷却期' };
      const task = mutate({ failedChannels: new Set(), ...options, initial, site, tokenId, key, epoch, signal: control });
      changes.set(key, task);
      try { return await task; }
      catch (error) {
        if (!control.aborted) boundedSet(controlBlockedUntil, account, Date.now() + 15000);
        throw error;
      } finally { changes.delete(key); }
    }
    operations++;
    const task = run().catch(() => ({ ok: false, reason: '控制面切换失败' }));
    const id = Symbol();
    inflight.set(id, task);
    try {
      const result = await task;
      if (result.ok || initial || epoch !== policyEpoch || control.aborted || !store.allowSwitch(cfg.maxSwitchesPerHour - pendingMutations)) return result;
      const fallback = fallbackRoute({ provider, model }, result.reason, failedRoutes);
      if (fallback.ok) store.noteSwitch(provider, model);
      return fallback;
    } finally { inflight.delete(id); operations--; idle(); }
  }

  function updateConfig(next) {
    cfg = next;
    quoteConfig = Object.fromEntries(quoteKeys.map(key => [key, cfg[key]]));
    selectionKey = JSON.stringify([3, ...Object.values(quoteConfig)]);
    routePins.clear();
    for (const [key, row] of owned) {
      const site = resolveSite(row.provider);
      if (site.ok && site.controlBase === row.controlBase && String(site.userId) === row.userId && site.tokenIdOverride === row.tokenId) {
        routePins.set(`${row.provider}|${row.model}`, key);
      }
    }
  }

  return { planSwitch, beginRequest, startRecovery, setStrategy, strategyStatus, updateConfig,
    generation, resolveSite, resolveSiteByBase, resolveToken, badChannels, loadCandidates, activeChannel,
    dispose() { clearTimeout(sweepRetry); clearTimeout(retryTimer); stopIdleTimer(); lifetime.abort(); } };
}
