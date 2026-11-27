/**
 * 换商家：把「这次失败了」变成「换一家再来」。
 *
 * 手段按代价从低到高：
 *   1. pin 另一家（最精确：站点按 (令牌, 模型) 维度记住固定关系）
 *   2. 换已配置的 DSH 备用路由。
 */

import {
  A6Error, bareKey, controlOrigin, fetchChannels, fetchChannelSearch, fetchSubscriptions, listPins, listTokens, pinChannel, unpinChannel, tokenPlainKey,
} from './a6.js';
import { createHash } from 'node:crypto';
import { Worker } from 'node:worker_threads';
import { belowContextFloor, cacheRatesFromSubscriptions, rankFastestHalf, refreshCandidateCache } from './selector.js';
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
  /**
   * 控制面不可用熔断。
   *
   * 站点整体挂掉时（TCP 通、HTTP 全超时），每轮请求都会进来试一次切换，失败就锁 15 秒，
   * 下一次又失败。日志里 fails 从 17 一路堆到 36，冷却期越滚越长，用户看到的是「本轮运行失败」
   * 反复出现——插件在死站上白转。
   *
   * 这里记「首次失败时刻」，连续失败超过 controlUnavailableMs（默认 2 分钟）就整体熔断：
   * 该账号下的切换全部短路返回 unavailable，直接把请求交给平台默认路由，不再打扰控制面。
   * 探测由定时器低频进行，一旦成功立刻解除熔断，恢复智能选商。
   */
  const controlFailureSince = new Map();
  const probeTimers = new Map();
  /** 后台刷新商家表的定时器。 */
  let channelsTimer = null;
  let channelsKick = null;
  const lifetime = new AbortController();
  // Persisted candidates were filtered and scored under a specific configuration.
  // 报价线程只拿这份子集算分：漏一个键，线程里就悄悄退回默认值（缓存命中率 0.9 就是这么漏掉的）。
  const quoteKeys = ['maxEffectiveRatio', 'maxPriceRatio', 'cacheHitRate', 'officialCacheHitRate',
    'merchantCacheHitRate', 'unmeasuredMerchantCacheHitRate', 'outputToInputRatio', 'useMeasuredOutputRatio',
    'outputRatioMinSamples', 'cacheWriteShare', 'assumeMissingCacheAsInput', 'hardP50Ms',
    'maxFailRate', 'priceWeight', 'reliabilityWeight', 'cacheWeight', 'cacheFallbackDiscount',
    'cacheMeasureMinSamples', 'latencyWeight', 'successWeight', 'latencyRefMs', 'minContextWindowTokens'];
  let quoteConfig = Object.fromEntries(quoteKeys.map((key) => [key, cfg[key]]));
  let selectionKey = JSON.stringify([5, ...Object.values(quoteConfig)]);

  /** 该账号是否还在「刚失败过、别立刻再打」的冷却窗口内。 */
  function isCoolingDown(account) {
    return Date.now() < (controlBlockedUntil.get(account) || 0);
  }

  /** provider 所属账号此刻是否已不可用（熔断中或在冷却期）。 */
  function controlDeadFor(provider) {
    const site = resolveSite(provider);
    if (!site.ok) return false;
    const account = JSON.stringify([site.controlBase, String(site.userId)]);
    return controlUnavailable(account) || isCoolingDown(account);
  }

  /** 该账号是否已熔断（连续失败超过 controlUnavailableMs）。 */
  function controlUnavailable(account) {
    const since = controlFailureSince.get(account);
    if (!since) return false;
    const limit = Number(cfg.controlUnavailableMs) || 0;
    if (!(limit > 0)) return false;
    return Date.now() - since >= limit;
  }

  /** 记一次控制面失败；首次失败记时间，之后一直累计（连续失败才算熔断）。 */
  function noteControlFailure(account) {
    if (!controlFailureSince.has(account)) controlFailureSince.set(account, Date.now());
    scheduleProbe(account);
  }

  /** 成功一次就清零并撤销探测定时器。 */
  function noteControlOk(account) {
    controlFailureSince.delete(account);
    const timer = probeTimers.get(account);
    if (timer) { clearTimeout(timer); probeTimers.delete(account); }
  }

  /**
   * 熔断期间低频探活：不改状态、不写站点，只打一个只读接口确认站是否活过来。
   * 已在计时器里的账号直接返回，避免每个请求都排一个定时器。
   */
  function scheduleProbe(account) {
    if (probeTimers.has(account) || lifetime.signal.aborted) return;
    const interval = Math.max(Number(cfg.controlProbeMs) || 30000, 5000);
    const timer = setTimeout(async () => {
      probeTimers.delete(account);
      if (lifetime.signal.aborted) return;
      let alive = false;
      try { alive = (await controlProbe(account)) === true; } catch { alive = false; }
      if (lifetime.signal.aborted) return;
      if (alive) {
        noteControlOk(account);
        store.logEvent({ type: 'control-recovered', account });
      } else if (controlFailureSince.has(account)) {
        scheduleProbe(account);
      }
    }, interval);
    timer.unref?.();
    probeTimers.set(account, timer);
  }

  /**
   * 探活：只读一次 pin 列表，确认控制面是否真的通了。
   * 不用 status 之类的公开接口——那种页站在边缘上仍可能返回 200，探不出回源已死。
   * account 是 JSON.stringify([controlBase, userId])，这里还原成站点参数。
   */
  async function controlProbe(account) {
    let controlBase = '';
    let userId = '';
    try { [controlBase, userId] = JSON.parse(account); } catch { return false; }
    const site = resolveSite(`a6api-${userId}`) || {};
    const target = {
      controlBase,
      userId,
      accessToken: site.accessToken,
      tokenIdOverride: site.tokenIdOverride,
      proxyUrl: cfg.proxyUrl || '',
      connectionMode: cfg.connectionMode || 'direct-first',
      directProbeMs: cfg.directProbeMs || 1500,
    };
    if (!target.accessToken) return false;
    try {
      const rows = await listPins({ ...target, timeoutMs: Math.min(Number(cfg.controlTimeoutMs) || 12000, 10000) });
      return Array.isArray(rows);
    } catch { return false; }
  }

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
      // 控制面全部请求都走这个代理：A6API 域名解析到私有地址段，直连必然超时。
      proxyUrl: cfg.proxyUrl || '',
      connectionMode: cfg.connectionMode || 'direct-first',
      directProbeMs: cfg.directProbeMs || 1500,
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
    return { ok: true, controlBase, ...credentials, tokenIdOverride: 0, tokenName: '', apiKey: '', proxyUrl: cfg.proxyUrl || '', connectionMode: cfg.connectionMode || 'direct-first', directProbeMs: cfg.directProbeMs || 1500 };
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
        // 接管外部固定时要把这家的原设置原样还原，不能自己改它的兜底语义。
        fallbackToSmartRouting: row.fallback_to_smart_routing === true || row.fallback_to_smart_routing === 1,
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
    const existing = subscriptions.get(account);    if (existing && (!force || !existing.done) && Date.now() < existing.until) return existing.task;
    const entry = { until: Date.now() + 60000, done: false };
    // 这里原本只给 2000ms。订阅列表虽不大，但它和商家表是并发拉的，
    // 站慢时 2 秒连 TLS 握手都不够，会一直返回 null（于是缓存率永远缺数据）。
    entry.task = fetchSubscriptions({ ...site, timeoutMs: Math.max(Number(cfg.controlTimeoutMs) || 20000, 10000),
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
    /*
     * 串行拉，不并发。
     *
     * 商家表有 25 MB，走的是同一条本地代理连接；和订阅列表并发时两者互相抢带宽，
     * 实测「单独 29 秒」会变成「90 秒还不完」。订阅列表本来就小、还带 5 分钟缓存，
     * 先等它（通常命中缓存，几毫秒）再拉大表，总耗时反而更短也更稳。
     */
    const cacheRates = await subscriptionRates(site, account, force);
    /*
     * 优先用市场页的 channels/search：服务端按模型过滤，实测 571KB / 0.4s，
     * 比旧接口的全量 15MB / 7.6s 小 26 倍、快 19 倍。
     * 失败（站点改接口/权限变化）时回退到旧的 channels 接口。
     * 两者都拿 rawJson 文本，交给 worker 里的 parseCandidates（已兼容两种结构）。
     */
    const fetchOpts = {
      ...site,
      modelName: model,
      timeoutMs: Math.max(Number(cfg.channelsTimeoutMs) || 30000, 5000),
      signal: lifetime.signal,
      maxBytes: cfg.maxChannelsBytes,
      rawJson: true,
    };
    let text;
    let via = 'search';
    try {
      text = await fetchChannelSearch(fetchOpts);
    } catch (err) {
      if (lifetime.signal.aborted) return [];
      log?.(`channels/search 失败（${err?.code || err?.message}），回退旧接口`);
      via = 'legacy';
      text = await fetchChannels(fetchOpts);
    }
    if (lifetime.signal.aborted) return [];
    const worker = new Worker(new URL('./quote-worker.js', import.meta.url), {
      execArgv: [],
      workerData: { text, model, cfg: quoteConfig, cacheRates,
        measuredRates: store.usageRates(model, site.controlBase),
        measuredStats: store.channelStats(model, site.controlBase),
        contextWindows: store.contextWindows(model, site.controlBase) },
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
    log?.(`商家列表[${via}]：${total} 条 listing → 候选 ${eligible} 家，缓存前 ${trimmed.length} 家；`
      + `缓存率快照 ${rateRows} 条，实际取自站点 ${fromSite} 家、实测命中 ${fromMeasured} 家、缓存读成本 ${fromDiscount} 家`);
    return trimmed;
  }

  /**
   * 取候选商家：**只读缓存，绝不现场拉表**。
   *
   * 商家表有 25 MB，站慢时要 15~60 秒，而且走本地代理时并发会互相抢带宽
   * （实测单独 29 秒、和订阅列表并发就 90 秒拉不完）。切换是热路径，
   * 卡在这上面等于整个插件不可用。
   *
   * 所以这里只做两件事：
   *   1) 缓存还能用 → 立刻返回（毫秒级）；
   *   2) 缓存不可用 → 起一个后台刷新，同时明确告诉调用方「现在没法选」，
   *      由上层降级到平台默认路由。下次请求时缓存大概率已经有了。
   *
   * @returns {{ rows: Array, stale: boolean, refreshing: boolean }}
   */
  function pickCachedCandidates(site, model, excludeChannels = new Set()) {
    const account = identity(site);
    const cached = store.getChannels(site.controlBase, model);
    const compatible = cached?.selectionKey === selectionKey && cached.account === account;
    const age = Date.now() - (cached?.at || 0);
    const filter = (rows) => {
      const known = badChannels(site.controlBase, model);
      const measured = store.usageRates(model, site.controlBase);
      const stats = store.channelStats(model, site.controlBase);
      // 上下文太小的商家的剔除放在这里（而不是只在解析时）：实测到的窗口上限是
      // 请求过程中学到的，缓存表里的老候选也要按最新知识重新筛。
      const windows = store.contextWindows(model, site.controlBase);
      return rankFastestHalf(rows.filter((row) => !excludeChannels.has(row.channelId) && !known.has(row.channelId)
        && !belowContextFloor(row, windows, cfg))
        .map((row) => refreshCandidateCache(row, cfg, measured, Date.now(), stats))
        .filter(Boolean), cfg);
    };
    // 表过期/不兼容时先触发后台刷新（不 await），再用旧表顶上。
    const fresh = compatible && age < Math.min(cfg.channelCacheTtlMs, QUOTE_MAX_AGE_MS);
    const refreshing = ensureRefresh(site, model, account, !fresh);
    if (compatible && age < QUOTE_MAX_AGE_MS) {
      const rows = filter(cached.candidates);
      if (rows.length) return { rows, stale: !fresh, refreshing };
    }
    return { rows: [], stale: true, refreshing };
  }

  /**
   * 显式刷新并等待结果。
   *
   * 只有「设置页点重新拉取」这种用户主动、能等、也知道要等几十秒的场景才用它。
   * 请求/切换路径一律走 pickCachedCandidates，绝不调这个。
   */
  async function loadCandidates(site, model, excludeChannels = new Set(), { force = false, signal } = {}) {
    const account = identity(site);
    const key = `${account}|${model}|${selectionKey}`;
    const cached = store.getChannels(site.controlBase, model);
    const compatible = cached?.selectionKey === selectionKey && cached.account === account;
    const age = Date.now() - (cached?.at || 0);
    const filter = (rows) => {
      const known = badChannels(site.controlBase, model);
      const measured = store.usageRates(model, site.controlBase);
      const stats = store.channelStats(model, site.controlBase);
      // 上下文太小的商家的剔除放在这里（而不是只在解析时）：实测到的窗口上限是
      // 请求过程中学到的，缓存表里的老候选也要按最新知识重新筛。
      const windows = store.contextWindows(model, site.controlBase);
      return rankFastestHalf(rows.filter((row) => !excludeChannels.has(row.channelId) && !known.has(row.channelId)
        && !belowContextFloor(row, windows, cfg))
        .map((row) => refreshCandidateCache(row, cfg, measured, Date.now(), stats))
        .filter(Boolean), cfg);
    };
    if (!force && compatible && age < Math.min(cfg.channelCacheTtlMs, QUOTE_MAX_AGE_MS)) return filter(cached.candidates);
    // 同一个 key 只跑一份刷新；已有的那份直接复用（不要另起，也不要抛 A6_BUSY）。
    let task = candidateLoads.get(key);
    if (!task && candidateLoads.size < 2 && !lifetime.signal.aborted) {
      task = refreshCandidates(site, model, account, force, quoteConfig, selectionKey).finally(() => candidateLoads.delete(key));
      candidateLoads.set(key, task);
      task.catch(() => {});
    }
    if (!task) {
      // 名额被别人占了（最多 2 个并发），别再排队；旧表能用就先用。
      if (compatible && age < QUOTE_MAX_AGE_MS) return filter(cached.candidates);
      throw new A6Error('商家列表正在刷新，请稍后重试', { code: 'A6_BUSY' });
    }
    return filter(await waitFor(task, signal));
  }

  /** 起一个后台刷新（同一个 key 只跑一份），立刻返回是否已在刷新。 */
  function ensureRefresh(site, model, account, needed) {
    const key = `${account}|${model}|${selectionKey}`;
    if (candidateLoads.has(key)) return true;
    if (!needed || candidateLoads.size >= 2 || lifetime.signal.aborted) return false;
    const task = refreshCandidates(site, model, account, true, quoteConfig, selectionKey)
      .then((rows) => {
        store.logEvent({ type: 'channels-refreshed', model, count: rows.length });
        return rows;
      })
      .catch((error) => {
        store.logEvent({ type: 'channels-refresh-failed', model, reason: error?.code || error?.message || '未知' });
        return [];
      })
      .finally(() => candidateLoads.delete(key));
    candidateLoads.set(key, task);
    task.catch(() => {});
    return true;
  }

  /**
   * 后台定时刷新商家表——只刷「DSH 实际用过/配置过」的模型，不碰站点上其他模型。
   *
   * 模型来源（并集）：
   *   knownRoutes   —— beginRequest 实际请求过的 provider|model（重启后为空）
   *   prewarmRoutes —— 配置里显式预热
   *   owned         —— 本插件固定过的
   *   allChannels   —— 磁盘里上次缓存过的（重启后仍在）
   * 这样站点上新冒出来、DSH 没添加的模型永远不会被下载。
   * 切换侧完全不依赖这个定时器：它慢、它失败，都不影响请求继续跑。
   */
  function startChannelsRefresh() {
    const interval = Math.max(Number(cfg.channelsRefreshMs) || 3600000, 60000);
    const tick = async () => {
      if (lifetime.signal.aborted) return;
      // 收集 { base, model } 去重
      const targets = new Map(); // key = base|model → { base, model }
      const add = (base, model) => {
        if (base && model) targets.set(`${base}|${model}`, { base, model });
      };
      // 1) provider|model 形式：从 provider 解析出 controlBase
      const fromRoutes = new Map(); // provider|model → model
      for (const r of knownRoutes.values()) if (r?.provider && r?.model) fromRoutes.set(`${r.provider}|${r.model}`, r.model);
      for (const r of cfg.prewarmRoutes || []) if (r?.provider && r?.model) fromRoutes.set(`${r.provider}|${r.model}`, r.model);
      for (const r of owned.values()) if (r?.provider && r?.model) fromRoutes.set(`${r.provider}|${r.model}`, r.model);
      for (const [routeId, model] of fromRoutes) {
        const provider = routeId.slice(0, routeId.lastIndexOf('|'));
        const site = resolveSite(provider);
        if (site.ok) add(site.controlBase, model);
      }
      // 2) 磁盘缓存里的模型（重启后仍能刷）
      for (const [base, byModel] of Object.entries(store.allChannels?.() || {})) {
        for (const model of Object.keys(byModel || {})) add(base, model);
      }
      // 逐个刷新；model 通过 resolveSiteByBase 拿到带凭证的 site
      for (const { base, model } of targets.values()) {
        if (lifetime.signal.aborted) return;
        const site = resolveSiteByBase(base);
        if (!site.ok) continue;
        ensureRefresh(site, model, identity(site), true);
      }
    };
    const timer = setInterval(() => { tick().catch(() => {}); }, interval);
    timer.unref?.();
    channelsTimer = timer;
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
  /** 已经为「保留外部固定」记过事件的 route|channel，避免每次请求都往日志里刷同一条。 */
  const preservedPins = new Set();
  const pendingRestore = new Set(owned.keys());
  const sites = new Map();
  const generations = new Map();
  /** 手动选商序列：key=provider|model → { order: channelId[], cursor: 当前序号 }。
   *  「选最佳」重置并固定整条排序；「换一家」按这条排序依次往下取，保证第2/3/4家稳定。 */
  const manualOrder = new Map();
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
      // 清理固定商家本身要打控制面。站整体挂掉时这一步必然失败，
      // 但「清不掉」不等于「这轮请求不能发」——把那轮的固定商家留给平台默认路由处理即可。
      // 只有控制面还活着（清不掉是别的原因）时才继续拦，避免带着残留 pin 跑错商家。
      // 控制面还活着（清不掉是别的原因）时才继续拦，避免带着残留 pin 跑错商家。
      if (!controlDeadFor(provider)) {
        throw new A6Error('平台默认路由待恢复：尚未确认该令牌全部模型已解除固定商家', { code: 'A6_ROUTE_NOT_READY' });
      }
      store.logEvent({ type: 'control-degraded', provider, model: '', reason: '清理固定商家失败，控制面不可用，放行平台默认路由' });
      return;
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
        // p50 只表示门槛口径；打分用的是 siteLatencyMs（站点最近实测，没有则 p50 兜底）。
        p50Ms: measured?.latencySamples >= 2 ? measured.p50Ms : row?.realP50Ms ?? null,
        latencySource: measured?.latencySamples >= 2 ? 'measured' : row?.latencySource,
        siteLatencyMs: row?.siteLatencyMs ?? null,
        siteLatencyOrigin: row?.siteLatencyOrigin ?? 'none',
        // 生成速度只有本机实测：站点不提供吞吐字段，宁可不显示也不拿 p50 反推。
        tokensPerSecond: Number.isFinite(cache?.tokensPerSecond) && cache.tokensPerSecond > 0 ? cache.tokensPerSecond : null,
        speedSamples: Math.round(Number(cache?.speedSamples || 0)) || 0 };
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

  async function beginRequest(provider, model, signal, { waitForChannels = false } = {}) {    signal?.throwIfAborted();
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
      // 控制面不可用时（熔断）一律放行：这轮请求交给平台默认路由跑完，
      // 比让整轮对话失败更有用。恢复由探活负责，站点一活过来就重新接管。
      const deadControl = controlDeadFor(provider);
      if (restoringAll && !deadControl) throw new A6Error('正在恢复平台默认策略', { code: 'A6_ROUTE_NOT_READY' });
      managedNames.add(provider);
      if (!deadControl) {
        await waitFor(ensurePlatform(provider), signal);
        if (affected(provider, model)) {
          await waitFor(Promise.resolve(restorePending()), signal);
          if (affected(provider, model) && !controlDeadFor(provider)) {
            throw new A6Error('平台默认路由待恢复', { code: 'A6_ROUTE_NOT_READY' });
          }
        }
      }
      if (manual && !activeChannel(provider, model)) {
        const result = await planSwitch({ provider, model, signal, initial: true, waitForChannels });
        if (!result.ok) {
          // 熔断/冷却意味着「控制面此刻不可用」，不是「这轮请求不该发」。
          // 抛错会让整轮对话失败；直接放行，让平台默认路由把这轮跑完。
          // 恢复由探活负责，站点一活过来就重新接管选商。
          if (result.unavailable) {
            store.logEvent({ type: 'control-degraded', provider, model, reason: result.reason });
          } else {
            throw new A6Error(result.reason, { code: 'A6_ROUTE_NOT_READY' });
          }
        }
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
    failedGeneration, failure, initial, signal, epoch, callerSignal, waitForChannels = false }) {
    if (!initial && failedGeneration < platformGeneration) return { ok: false, reason: '请求早于手动恢复平台策略' };
    const previous = lastSwitch.get(key);
    if (previous?.attribution && (initial || (failedGeneration !== undefined && failedGeneration < generations.get(key)))
      && previous.channelId !== failedChannelId && !failedChannels.has(previous.channelId)) {
      return { ok: true, ...previous, provider, model, mode: 'deduped' };
    }
    if (pendingRestore.has(key) || epoch !== policyEpoch) return { ok: false, reason: '正在恢复平台默认策略' };
    const pinned = await currentPin(site, tokenId, model, { signal });
    const claim = owned.get(key);
    const ours = Boolean(pinned && claim && pinned.strict && pinned.channelId === claim.channelId);
    /** 站点上一条不是本插件写的固定商家：用户手动固定的，或平台策略留下的。 */
    const external = pinned && !ours ? pinned : null;
    if (external && !cfg.takeOverExistingPin) {
      if (claim) { journal(key, null); lastSwitch.delete(key); generations.set(key, ++serial); }
      remember(provider, model).external = true;
      // 每次请求都会撞上这道闸门，同一个渠道只记一条，别把事件日志刷满。
      const seenKey = `${key}|${external.channelId}`;
      if (!preservedPins.has(seenKey)) {
        preservedPins.add(seenKey);
        store.logEvent({ type: 'pin-preserved', provider, model, channelId: external.channelId,
          reason: 'takeOverExistingPin=false' });
      }
      // 文案保持原样：stream-guard 用这条 reason 决定是否给出 A6_EXISTING_PIN 提示。
      return { ok: false, reason: '保留平台已有的固定商家' };
    }
    remember(provider, model).external = false;
    if (failedChannelId && pinned?.channelId === failedChannelId) store.noteBadChannel(site.controlBase, model, failedChannelId, failure?.code || '');
    const exclude = new Set(failedChannels);
    // 外部固定不放进排除集：接管模式下它也可能是最合适的那一家，那时直接认领，不为换而换。
    // 只有「本来就是本插件固定的」才在这里排除（那是刚失败/要换掉的那家）。
    if (!initial && pinned && !external) exclude.add(pinned.channelId);
    if (failedChannelId) exclude.add(failedChannelId);
    // 只读本地缓存，绝不在这里拉表：25 MB 的表会把切换拖死（实测 90 秒都没完）。
    // 缓存没有就明确返回「还没准备好」，交给上层降级；刷新在后台跑，下次就有了。
    let picked = pickCachedCandidates(site, model, exclude);
    if (!picked.rows.length && waitForChannels) {
      // 例外：调用方明确说「我可以等」（设置页重新拉取、自检、脚本）。
      // 这种情况下同步拉一次，拉不到再认输。
      picked = { rows: await loadCandidates(site, model, exclude, { force: false, signal: callerSignal }), stale: false };
    }
    if (!picked.rows.length) {
      // 商家表不在本地缓存里。**不在这里抛错也不返回 failed**：
      // 抛错会让整轮对话失败（这是最糟的结果），返回普通失败又会被上层当成「换商家失败」。
      // 所以先同步等一次刷新——通常后台已经拉好了；等不到再明确报「没有可用商家」。
      //
      // 注意：这不是「每次切换都拉 25 MB」的老问题，因为等的是**已经在跑的那一份**
      // （ensureRefresh 保证同一个 key 只有一份任务），而不是新起一次下载。
      await ensureRefresh(site, model, identity(site), true);
      picked = { rows: await loadCandidates(site, model, exclude, { force: false, signal: callerSignal }), stale: false };
    }
    if (!picked.rows.length) return { ok: false, reason: '没有满足价格与质量门槛的商家' };    const [candidate] = picked.rows;
    if (!candidate) return { ok: false, reason: '没有满足价格与质量门槛的商家' };
    lifetime.signal.throwIfAborted();
    callerSignal?.throwIfAborted();
    if (epoch !== policyEpoch) return { ok: false, reason: '路由策略已修改' };
    if (!initial && !store.allowSwitch(cfg.maxSwitchesPerHour - pendingMutations)) return { ok: false, reason: '本小时切换次数已达上限' };
    pendingMutations++;
    try {
      // 接管模式下选中的正好是外部固定那一家、且它当前生效：只借用，不改站点的设置。
      // 不写 ownedPins —— 没动过站点的东西，就没有资格在空闲时替它 unpin。
      if (external && external.strict && candidate.channelId === external.channelId) {
        sites.set(key, site);
        lastSwitch.delete(key);
        generations.set(key, ++serial);
        const row = { ...candidate, mode: 'adopt', at: Date.now(), controlBase: site.controlBase, attribution: true };
        lastSwitch.set(key, row);
        store.logEvent({ type: 'pin-adopted', provider, model, channelId: row.channelId, effRatio: row.effRatio });
        return { ok: true, provider, model, ...row };
      }
      const record = { provider, model, controlBase: site.controlBase, userId: String(site.userId), tokenId,
        channelId: candidate.channelId, previousChannelId: claim?.channelId || external?.channelId || null };
      journal(key, record);
      sites.set(key, site);
      lastSwitch.delete(key);
      generations.set(key, ++serial);
      let externalReleased = false;
      try {
        if (external) {
          await unpinChannel({ ...site, signal, tokenId, modelName: model, channelId: external.channelId });
          externalReleased = true;
          store.logEvent({ type: 'pin-takeover', provider, model, from: external.channelId, to: candidate.channelId });
        }
        await pinChannel({ ...site, signal, tokenId, modelName: model, channelId: candidate.channelId, fallbackToSmartRouting: false });
        const confirmed = await currentPin(site, tokenId, model, { signal });
        if (!confirmed?.strict || confirmed.channelId !== candidate.channelId) throw new Error('固定商家回查失败');
        journal(key, { ...record, previousChannelId: null });
      } catch (error) {
        // 接管失败就把站点还原成接管前的样子，不留一条被插件卸掉的固定记录。
        if (externalReleased) {
          try {
            await pinChannel({ ...site, signal, tokenId, modelName: model, channelId: external.channelId,
              fallbackToSmartRouting: external.fallbackToSmartRouting });
            journal(key, null);
            lastSwitch.delete(key);
            generations.set(key, ++serial);
            store.logEvent({ type: 'pin-takeover-rollback', provider, model, channelId: external.channelId,
              code: error?.message || 'pin-failed' });
          } catch { pendingRestore.add(key); }
        } else {
          pendingRestore.add(key);
        }
        throw error;
      }
      const row = { ...candidate, mode: 'pin', at: Date.now(), controlBase: site.controlBase, attribution: true };
      lastSwitch.set(key, row);
      if (!initial) store.noteSwitch(provider, model);
      store.logEvent({ type: 'pin', provider, model, channelId: row.channelId, effRatio: row.effRatio });
      return { ok: true, provider, model, ...row };
    } finally { pendingMutations--; }
  }

  /**
   * 手动选最佳商家：从当前候选表里取打分第一名（排除当前固定商家），固定到它并回查确认。
   * 与 planSwitch 的区别：这是设置页主动触发、不看失败历史、不占自动切换预算，
   * 目的是「按当前列表信息立即选最好的一家」，并把结果回给 UI 展示。
   */
  async function selectBest(provider, model, callerSignal, mode = 'reset') {
    if (lifetime.signal.aborted || callerSignal?.aborted) return { ok: false, reason: '已取消' };
    const site = resolveSite(provider);
    if (!site.ok) return { ok: false, reason: site.reason };
    const control = AbortSignal.any([lifetime.signal, ...(callerSignal ? [callerSignal] : [])]);
    const account = JSON.stringify([site.controlBase, String(site.userId)]);
    const { tokenId } = await resolveToken(site, provider, { signal: control });
    const key = bindPin(provider, model, account, tokenId);
    const orderKey = `${provider}|${model}`;
    const running = changes.get(key);
    if (running) { const r = await waitFor(running, control); return r.ok ? { ...r, mode: 'deduped' } : r; }
    const pinned = await currentPin(site, tokenId, model, { signal: control });
    const exclude = new Set();
    if (pinned?.channelId) exclude.add(pinned.channelId);

    // 拉一份当前候选（排除当前固定商家）。用户主动点击，允许等一次刷新。
    let picked = pickCachedCandidates(site, model, exclude);
    if (!picked.rows.length) {
      await ensureRefresh(site, model, account, true);
      picked = { rows: await loadCandidates(site, model, exclude, { force: false, signal: callerSignal }), stale: false };
    }
    const rows = picked.rows;
    if (!rows.length) return { ok: false, reason: '没有满足门槛的商家（排除当前固定后）' };

    let candidate;
    let cursor = 0;
    if (mode === 'next' && manualOrder.has(orderKey)) {
      // 换一家：沿已固定的排序往下走，跳过已经失效的 channelId。
      const state = manualOrder.get(orderKey);
      let i = (state.cursor ?? 0) + 1;
      const byId = new Map(rows.map((row) => [row.channelId, row]));
      while (i < state.order.length && !byId.has(state.order[i])) i++; // 快照里失效的跳过
      if (i < state.order.length) {
        candidate = byId.get(state.order[i]);
        cursor = i;
      } else {
        // 到末尾了：重新拉排序、从头再来。
        const fresh = rows.map((row) => row.channelId);
        manualOrder.set(orderKey, { order: fresh, cursor: 0 });
        candidate = rows[0];
        cursor = 0;
      }
    } else {
      // 选择最佳：重置排序快照并从第 1 家开始。
      const order = rows.map((row) => row.channelId);
      manualOrder.set(orderKey, { order, cursor: 0 });
      candidate = rows[0];
      cursor = 0;
    }
    if (!candidate) return { ok: false, reason: '没有更多可用商家' };
    const currentOrder = manualOrder.get(orderKey);
    if (currentOrder) currentOrder.cursor = cursor;

    const record = { provider, model, controlBase: site.controlBase, userId: String(site.userId), tokenId,
      channelId: candidate.channelId, previousChannelId: pinned?.channelId || null };
    journal(key, record);
    sites.set(key, site);
    generations.set(key, ++serial);
    try {
      await pinChannel({ ...site, signal: control, tokenId, modelName: model,
        channelId: candidate.channelId, fallbackToSmartRouting: false });
      const confirmed = await currentPin(site, tokenId, model, { signal: control });
      if (!confirmed?.strict || confirmed.channelId !== candidate.channelId) throw new Error('固定商家回查失败');
      journal(key, { ...record, previousChannelId: null });
      const row = { ...candidate, mode: 'pin', at: Date.now(), controlBase: site.controlBase, attribution: true,
        selectionIndex: cursor + 1, selectionTotal: currentOrder?.order.length || rows.length, selectionMode: mode };
      lastSwitch.set(key, row);
      remember(provider, model).external = false;
      store.logEvent({ type: mode === 'next' ? 'manual-next' : 'manual-select-best', provider, model,
        channelId: row.channelId, from: pinned?.channelId || null, index: row.selectionIndex,
        total: row.selectionTotal, effRatio: row.effRatio, cnyPerMillion: row.cnyPerMillion });
      return { ok: true, provider, model, ...row };
    } catch (error) {
      // 固定失败：把序号退回，清掉刚写的账本，站点 pin 保持点击前的原样。
      if (currentOrder && currentOrder.cursor === cursor) currentOrder.cursor = Math.max(0, cursor - 1);
      journal(key, null);
      lastSwitch.delete(key);
      throw error;
    }
  }

  async function planSwitch(options) {
    const { provider, model, signal, failedRoutes = new Set(), initial = false } = options;
    if (lifetime.signal.aborted || signal?.aborted) return { ok: false, reason: '已取消' };
    if (restoringAll || sweeps.size || sweepFailures.has(provider)) return { ok: false, reason: '平台默认路由待恢复' };
    const epoch = policyEpoch;
    /**
     * 切换是热路径，但它内部包含两件量级差三个数量级的事：
     *   1) 拉商家表：25 MB，站慢时 15~60 秒；
     *   2) 查/改 pin：几百字节，正常几百毫秒。
     *
     * 用同一个超时必然出事：按 (2) 的尺度给，拉表永远被掐断；按 (1) 的尺度给，
     * pin 出问题时要干等一分钟。所以这里只做「整体兜底」——够拉完表再做完 pin 就行，
     * 而每个具体请求自己的超时由 a6Request 的 timeoutMs 单独管（controlTimeoutMs）。
     */
    const outerDeadlineMs = Math.max(Number(cfg.controlTimeoutMs) || 20000, 8000)
      + Math.max(Number(cfg.channelsTimeoutMs) || 90000, 15000);
    const timeout = AbortSignal.timeout(outerDeadlineMs);
    const control = AbortSignal.any([lifetime.signal, timeout, ...(signal ? [signal] : [])]);
    // run() 里算出来的账号键，供 catch 分支判断是否已熔断。
    let lastAccount = '';
    async function run() {
      if (cleanup) await waitFor(cleanup, control);
      const site = resolveSite(provider);
      if (!site.ok) return { ok: false, reason: site.reason };
      // 账号键必须在 resolveToken 之前算出来：探 token 本身就要打控制面，
      // 站点死掉时正是它先抛错，等那之后再记就来不及标熔断了。
      const account = JSON.stringify([site.controlBase, String(site.userId)]);
      lastAccount = account;
      const { tokenId } = await resolveToken(site, provider, { signal: control });
      const key = bindPin(provider, model, account, tokenId);
      if (pendingRestore.has(key)) return { ok: false, reason: '平台默认路由待恢复' };
      const running = changes.get(key);
      if (running) {
        const result = await waitFor(running, control);
        return result.ok ? { ...result, provider, model, mode: 'deduped' } : result;
      }
      if (Date.now() < (controlBlockedUntil.get(account) || 0)) {
        // 冷却期内也判一次熔断：冷却只是「别立刻重试」，熔断才是「整体放弃」。
        if (controlUnavailable(account)) {
          return { ok: false, reason: '控制面连续无响应，已降级到平台默认路由', unavailable: true };
        }
        return { ok: false, reason: '控制面处于冷却期' };
      }
      if (controlUnavailable(account)) return { ok: false, reason: '控制面连续无响应，已降级到平台默认路由', unavailable: true };
      const task = mutate({ failedChannels: new Set(), ...options, initial, site, tokenId, key, epoch, signal: control, callerSignal: signal });
      changes.set(key, task);
      try {
        const done = await task;
        noteControlOk(account);
        return done;
      } catch (error) {
        if (!control.aborted) {
          // 只锁冷却，不在这里记熔断计数：run().catch 会统一记一次，
          // 两处都记会让「一次失败」被算成两次，缩短熔断窗口。
          boundedSet(controlBlockedUntil, account, Date.now() + 15000);
        }
        throw error;
      } finally { changes.delete(key); }
    }
    operations++;
    // 失败要带上「控制面到底能不能用」的判定：只回一句 reason 的话，上层无法区分
    // 「这家商家坏了」和「整个站挂了」，只能一律抛 A6_ROUTE_NOT_READY 让整轮对话失败。
    const task = run().catch((error) => {
      // 也可能死在 mutate。两种都算「控制面没给出结果」，统一记一次失败，
      // 连续失败累计到 controlUnavailableMs 就熔断。
      if (lastAccount && !lifetime.signal.aborted) {
        boundedSet(controlBlockedUntil, lastAccount, Date.now() + 15000);
        noteControlFailure(lastAccount);
      }
      const dead = lastAccount ? controlUnavailable(lastAccount) : false;
      return dead
        ? { ok: false, reason: '控制面连续无响应，已降级到平台默认路由', unavailable: true }
        : { ok: false, reason: '控制面切换失败' };
    });
    const id = Symbol();
    inflight.set(id, task);
    try {
      const result = await task;
      // 控制面已熔断：不要再去试备用路由。整站挂掉时备用路由多半也在同一个站上，
      // 试一遍只会多一次失败、多一条日志，最后仍然抛错让整轮对话失败。
      if (result.unavailable) return result;
      if (result.ok || initial || epoch !== policyEpoch || control.aborted || !store.allowSwitch(cfg.maxSwitchesPerHour - pendingMutations)) return result;
      const fallback = fallbackRoute({ provider, model }, result.reason, failedRoutes);
      if (fallback.ok) store.noteSwitch(provider, model);
      return fallback;
    } finally { inflight.delete(id); operations--; idle(); }
  }

  function updateConfig(next) {
    cfg = next;
    preservedPins.clear();
    for (const timer of probeTimers.values()) clearTimeout(timer);
    probeTimers.clear();
    quoteConfig = Object.fromEntries(quoteKeys.map(key => [key, cfg[key]]));
    selectionKey = JSON.stringify([5, ...Object.values(quoteConfig)]);
    routePins.clear();
    for (const [key, row] of owned) {
      const site = resolveSite(row.provider);
      if (site.ok && site.controlBase === row.controlBase && String(site.userId) === row.userId && site.tokenIdOverride === row.tokenId) {
        routePins.set(`${row.provider}|${row.model}`, key);
      }
    }
  }

  return { planSwitch, selectBest, beginRequest, startRecovery, setStrategy, strategyStatus, updateConfig,
    generation, resolveSite, resolveSiteByBase, resolveToken, badChannels, loadCandidates, pickCachedCandidates, ensureRefresh, activeChannel,
    dispose() {
      clearTimeout(sweepRetry); clearTimeout(retryTimer); stopIdleTimer();
      if (channelsTimer) { clearInterval(channelsTimer); channelsTimer = null; }
      if (channelsKick) { clearTimeout(channelsKick); channelsKick = null; }
      for (const timer of probeTimers.values()) clearTimeout(timer);
      probeTimers.clear();
      lifetime.abort();
    } };
}
