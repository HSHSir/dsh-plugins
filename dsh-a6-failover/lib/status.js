/**
 * 设置页要的数据：一次装配成可直接 JSON 化的快照。
 *
 * 只读：不 pin、不发模型请求。页面拿它渲染状态、最近事件、价格表。
 * 单独放一个模块，是为了能用 node --test 直接喂假数据跑。
 */

import fs from 'node:fs';
import path from 'node:path';

/** 读 JSONL 的最后 n 行（文件很大时不要整份读进来）。 */
export function tailLines(file, n = 12, maxBytes = 512 * 1024) {
  try {
    const stat = fs.statSync(file);
    const start = Math.max(0, stat.size - maxBytes);
    const fd = fs.openSync(file, 'r');
    const buf = Buffer.alloc(stat.size - start);
    try { fs.readSync(fd, buf, 0, buf.length, start); } finally { fs.closeSync(fd); }
    const lines = buf.toString('utf8').split('\n').filter(Boolean);
    // 从中间截断时第一行可能是半条，丢掉
    const usable = start > 0 ? lines.slice(1) : lines;
    return usable.slice(-n).map((line) => {
      try { return JSON.parse(line); } catch { return { raw: line.slice(0, 200) }; }
    });
  } catch {
    return [];
  }
}

function routeRows(state) {
  const rows = [];
  for (const [key, entry] of Object.entries(state.routes || {})) {
    const [provider, ...rest] = key.split('|');
    rows.push({
      provider,
      model: rest.join('|'),
      fails: entry.fails || 0,
      switches: entry.switches || 0,
      degradedUntil: entry.degradedUntil || 0,
      lastCode: entry.lastCode || '',
      lastFailAt: entry.lastFailAt || 0,
    });
  }
  return rows.sort((a, b) => (b.lastFailAt || 0) - (a.lastFailAt || 0));
}

/**
 * 组装设置页的 payload。
 * @param {object} input
 * @param {object} input.cfg        规范化后的配置
 * @param {object} input.store      createStore() 的返回值
 * @param {object} [input.switcher] createSwitcher() 的返回值（可缺）
 */
export function buildStatus({ cfg, store, switcher, version = '', now = Date.now() }) {
  const state = store.snapshot ? store.snapshot() : { routes: {}, channels: {}, tokens: {}, bad: {} };

  const channels = Object.entries(state.channels || {}).map(([key, row]) => {
    const [controlBase, ...rest] = key.split('|');
    return {
      controlBase,
      model: rest.join('|'),
      at: row.at || 0,
      total: row.total || 0,
      rejected: row.rejected || {},
      candidates: Array.isArray(row.candidates) ? row.candidates.slice(0, 10) : [],
    };
  }).sort((a, b) => b.at - a.at);

  const history = tailLines(path.join(store.dir, 'events.jsonl'), 512);
  const events = history.slice(-14);
  const evidence = history.filter(event => event.type === 'format-anomaly').slice(-20);
  const lastSwitch = events.filter((event) => event.type === 'switched').slice(-1)[0] || null;

  return {
    ok: true,
    version,
    evidence,
    now,
    plugin: {
      enabled: cfg.enabled,
      providerPattern: cfg.providerPattern,
      providers: cfg.providers,
      controlBase: cfg.defaultControlBase,
      credentialSources: cfg.credentialSources,
      fallbackRoutes: cfg.fallbackRoutes,
      verbose: cfg.verbose,
    },
    limits: {
      maxSwitchesPerCall: cfg.maxSwitchesPerCall,
      maxSwitchesPerHour: cfg.maxSwitchesPerHour,
      switchesLastHour: store.switchesLastHour(),
      degradeAfterFailures: cfg.degradeAfterFailures,
      switchDedupeMs: cfg.switchDedupeMs,
      autoSwitchOnContextFailure: cfg.autoSwitchOnContextFailure,
      maxContextSwitchesPerHour: cfg.maxContextSwitchesPerHour,
      contextHeadroomRatio: cfg.contextHeadroomRatio,
    },
    price: {
      maxEffectiveRatio: cfg.maxEffectiveRatio,
      cacheHitRate: cfg.cacheHitRate,
      officialCacheHitRate: cfg.officialCacheHitRate,
      merchantCacheHitRate: cfg.merchantCacheHitRate ?? 'measured',
      outputToInputRatio: cfg.outputToInputRatio,
      cacheWriteShare: cfg.cacheWriteShare,
      fxCnyPerUsd: cfg.fxCnyPerUsd,
      maxPriceRatio: cfg.maxPriceRatio,
      latencyRefMs: cfg.latencyRefMs,
      priceWeight: cfg.priceWeight,
      reliabilityWeight: cfg.reliabilityWeight,
      latencyWeight: cfg.latencyWeight,
      useMeasuredOutputRatio: cfg.useMeasuredOutputRatio,
      hardP50Ms: cfg.hardP50Ms,
      maxFailRate: cfg.maxFailRate,
      minSamples: cfg.minSamples,
      priceWeight: cfg.priceWeight,
      cacheWeight: cfg.cacheWeight,
      cacheFallbackDiscount: cfg.cacheFallbackDiscount,
      cacheMeasureMinSamples: cfg.cacheMeasureMinSamples,
      latencyWeight: cfg.latencyWeight,
      successWeight: cfg.successWeight,
      minContextWindowTokens: cfg.minContextWindowTokens,
    },
    routes: routeRows(state),
    channels,
    lastSwitch,
    events,
    files: {
      state: store.statePath,
      events: store.eventPath,
    },
  };
}
