/**
 * 商家（渠道）选择：从站点返回的 channels 里挑下一家。
 *
 * 规则沿用模型管家 smart_route.py 的硬门槛，顺序即「先排除不能用的，再排除不够便宜的」：
 *   1. 只留目标模型的 listing
 *   2. 渠道/供应商未被禁用
 *   3. **按实际用量折算的成本倍率 ≤ maxEffectiveRatio**（默认 0.7，见 pricing.js）
 *   4. p50 延迟 ≤ hardP50Ms
 *   5. 站点失败率 ≤ maxFailRate
 *   6. 样本数 ≥ minSamples
 * 排序用绝对刻度打分（成本/延迟/失败率），不做 min-max 归一化——成功率那一列几乎
 * 都是 99%~100%，归一化会把 0.1% 的噪声放大成满量程，让排序失去意义。
 *
 * 缓存收益只参与排序打分，不参与淘汰：它决定「你实际花多少」（命中缓存的那部分按缓存读价
 * 计费），但站点缓存率有采样噪声、也可能整片没数据，拿它卡门槛会把商家一次性打死。
 * 缓存收益优先取有效的站点 24h 缓存率，其次用请求 usage 的实测命中率，再用缓存读价与官方
 * 缓存读价的比值打分。这个比值无需等待流量，且能区分相同折扣、实际报价不同的商家。
 */

import { normalizeWorkload, priceModel, quotedCacheReadRatio } from './pricing.js';

const COST_LOG_SCALE = 0.002;
const CACHE_SNAPSHOT_MAX_AGE_MS = 24 * 60 * 60 * 1000;

/**
 * 站点缓存率的字段名（按顺序取第一个有值的）。
 *
 * 实测（2026-11-23，a6api.vip）：
 *   - `/api/marketplace/channels` 的 listing **完全不含**缓存率字段；
 *   - 缓存率只出现在 `/api/subscriptions` 的路由池对象上（auto_channels /
 *     auto_pool_preview.items），字段名 **cache_hit_rate_bps**（10000 = 100%），
 *     旁边是 cache_hit_attempts / cache_hit_snapshot_at / cache_hit_snapshot_fresh 和快照到期时间；
 *   - 早期前端 bundle 里的原始字段为 cache_hit_rate_24h + cache_hit_state，
 *     别的站点/版本可能还在用，留着兼容。
 */
const CACHE_RATE_FIELDS = ['cache_hit_rate_bps', 'cache_hit_rate_24h', 'recent_cache_hit_rate', 'cache_hit_rate', 'cache_rate_24h'];

function toInt(value) {
  const n = Number(value);
  return Number.isFinite(n) ? Math.trunc(n) : 0;
}

function numericField(value) {
  if (typeof value !== 'number' && (typeof value !== 'string' || !value.trim())) return NaN;
  return Number(value);
}

function basisPointRate(value) {
  const n = numericField(value);
  return Number.isFinite(n) && n >= 0 && n <= 10000 ? n / 10000 : null;
}

function validCacheState(row) {
  const state = String(row.cache_hit_state || '').trim().toLowerCase();
  const fresh = row.cache_hit_snapshot_fresh;
  return (!state || state === 'rate' || state === 'zero') && (fresh == null || fresh === true);
}

function snapshotTimeSeconds(value) {
  const n = numericField(value);
  if (Number.isSafeInteger(n) && n > 0) return n >= 1e12 ? n / 1000 : n;
  if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T.*(?:Z|[+-]\d{2}:\d{2})$/i.test(value)) {
    return Date.parse(value) / 1000;
  }
  return NaN;
}

function validSnapshotTime(at, now, expiresAt) {
  if (expiresAt != null && (!Number.isSafeInteger(expiresAt) || expiresAt <= now || (at != null && expiresAt <= at * 1000))) return false;
  if (at == null) return true;
  const timestamp = at * 1000;
  return Number.isFinite(timestamp) && at > 0 && timestamp <= now && now - timestamp < CACHE_SNAPSHOT_MAX_AGE_MS;
}

/**
 * 站点成功率字段的口径（实测 a6api.vip）：
 *   10000 = 100%，9930 = 99.3%，1785 = 17.85%
 * 字段固定按基点解析，1 表示 0.01%。
 */
export function successRate(value) {
  return basisPointRate(value) ?? 0;
}

/**
 * 把 `/api/subscriptions` 的响应压成 `Map<listing_id, { rate, attempts, at, expiresAt }>`。
 *
 * 这条接口才是缓存率唯一的数据源：路由池里的 auto_channels 与 auto_pool_preview.items
 * 都带 cache_hit_rate_bps，且 listing_id 与商家列表 100% 对得上（实测 62/62）。
 * 只收有采样且仍有效的快照，包括有采样的真实 0%。
 */
export function cacheRatesFromSubscriptions(payload, now = Date.now()) {
  const out = new Map();
  const subs = payload && typeof payload === 'object' ? payload.data?.items : null;
  if (!Array.isArray(subs)) return out;
  const take = (row) => {
    if (!row || typeof row !== 'object' || !validCacheState(row)) return;
    const listingId = toInt(row.listing_id);
    if (listingId <= 0) return;
    const rate = basisPointRate(row.cache_hit_rate_bps);
    const attempts = numericField(row.cache_hit_attempts);
    const at = snapshotTimeSeconds(row.cache_hit_snapshot_at);
    // 控制台用 Date.now() 直接比较到期时间，其字段单位为毫秒。
    const expiresAt = row.cache_hit_snapshot_expires_at == null ? null : numericField(row.cache_hit_snapshot_expires_at);
    if (rate === null || !Number.isSafeInteger(attempts) || attempts <= 0 || !validSnapshotTime(at, now, expiresAt)) return;
    const prev = out.get(listingId);
    if (!prev || at > prev.at || (at === prev.at && attempts > prev.attempts)) {
      out.set(listingId, { rate, attempts, at, expiresAt });
    }
  };
  for (const sub of subs) {
    if (!sub || typeof sub !== 'object') continue;
    if (Array.isArray(sub.auto_channels)) for (const row of sub.auto_channels) take(row);
    const preview = sub.auto_pool_preview;
    if (preview && Array.isArray(preview.items)) for (const row of preview.items) take(row);
  }
  return out;
}

/**
 * 商家的 24h 缓存率。口径与 recent_success_rate 一致：10000 = 100%，9930 = 99.3%。
 *
 * 取值顺序：路由池快照（cacheRates，最权威）→ 行内字段 → 渠道层字段。
 * cache_hit_state 不在 {rate, zero} 里时，站点是在说「这一类没有数据」——返回 null，
 * 打分时按中性处理；当 0% 会把新商家一棍子打死。
 */
function siteCacheOf(item, cacheRates, now) {
  if (!item || typeof item !== 'object') return null;
  if (cacheRates instanceof Map) {
    const listingId = toInt(item.listing_id);
    const hit = listingId > 0 ? cacheRates.get(listingId) : null;
    if (Number.isFinite(hit?.rate) && hit.rate >= 0 && hit.rate <= 1 && validSnapshotTime(hit.at, now, hit.expiresAt)) {
      return { value: hit.rate, source: 'site-rate', at: hit.at ?? null, expiresAt: hit.expiresAt ?? null };
    }
  }
  if (!validCacheState(item)) return null;
  const at = item.cache_hit_snapshot_at == null ? null : snapshotTimeSeconds(item.cache_hit_snapshot_at);
  const expiresAt = item.cache_hit_snapshot_expires_at == null ? null : numericField(item.cache_hit_snapshot_expires_at);
  if (!validSnapshotTime(at, now, expiresAt)) return null;
  for (const field of CACHE_RATE_FIELDS) {
    const rate = basisPointRate(item[field]);
    if (rate === null) continue;
    const attempts = numericField(item.cache_hit_attempts);
    if ((item.cache_hit_attempts != null || field === 'cache_hit_rate_bps')
        && (!Number.isSafeInteger(attempts) || attempts <= 0)) continue;
    return { value: rate, source: 'site-rate', at, expiresAt };
  }
  return null;
}

export function cacheRateOf(item, cacheRates, now = Date.now()) {
  return siteCacheOf(item, cacheRates, now)?.value ?? null;
}

/** 兼容旧报表的折扣读取接口，排序使用缓存读价相对官方的成本。 */
export function cacheDiscountOf(item) {
  if (!item || typeof item !== 'object') return null;
  const inputMicros = numericField(item.input_price_micros);
  if (inputMicros <= 0) return null;
  const raw = item.cache_read_price_micros;
  if (raw === null || raw === undefined || raw === '') return null;
  const readMicros = Number(raw);
  if (!Number.isFinite(readMicros) || readMicros < 0) return null;
  return Math.min(Math.max(1 - readMicros / inputMicros, 0), 1);
}

/**
 * 我们自己测出来的命中率：`Map<channel_id, { rate, samples }>`（来自 state.js 的 usage 累计）。
 * 样本少于 cacheMeasureMinSamples 就当没有——一次命中率 0% 的请求会把新商家冤死。
 */
export function measuredHitOf(item, channel, measured, cfg = {}) {
  return measuredCacheOf(toInt(item?.channel_id || channel?.channel_id), measured, cfg);
}

function measuredCacheOf(id, measured, cfg) {
  if (!(measured instanceof Map) || measured.size === 0) return null;
  if (id <= 0) return null;
  const stat = measured.get(id);
  if (!stat || !Number.isFinite(stat.rate) || stat.rate < 0 || stat.rate > 1) return null;
  const configured = numericField(cfg.cacheMeasureMinSamples);
  const minSamples = Number.isFinite(configured) ? Math.max(1, configured) : 3;
  const samples = numericField(stat.samples);
  // 样本数带时间衰减，新样本在读回时可能已经略低于整数。按展示使用的有效样本整数判断。
  if (!Number.isFinite(samples) || Math.round(samples) < minSamples) return null;
  return { value: stat.rate, source: 'measured', samples };
}

function effectiveLimit(cfg) {
  return Number.isFinite(cfg.maxEffectiveRatio) && cfg.maxEffectiveRatio >= 0 ? cfg.maxEffectiveRatio : 0.7;
}

function normalizedCost(ratio, cfg) {
  const ceiling = Math.max(effectiveLimit(cfg), COST_LOG_SCALE);
  const scaled = ratio / COST_LOG_SCALE;
  const logCost = Number.isFinite(scaled) ? Math.log1p(scaled) : Math.log(ratio) - Math.log(COST_LOG_SCALE);
  return logCost / Math.log1p(ceiling / COST_LOG_SCALE);
}

function cacheCostBenefit(cacheReadRatio, cfg) {
  if (cfg.cacheFallbackDiscount !== false && Number.isFinite(cacheReadRatio) && cacheReadRatio > 0) {
    return { value: 1 / (1 + normalizedCost(cacheReadRatio, cfg)), source: 'cache-read-cost' };
  }
  return { value: null, source: 'none' };
}

/** 缓存收益优先取有效站点值、实测值，再取缓存读取成本。 */
export function cacheBenefitOf(item, channel, cacheRates, cfg = {}, measured = null, now = Date.now()) {
  return siteCacheOf(item, cacheRates, now) ?? siteCacheOf(channel, cacheRates, now)
    ?? measuredHitOf(item, channel, measured, cfg) ?? cacheCostBenefit(quotedCacheReadRatio(item, item?.official_price), cfg);
}

function cacheFields(benefit) {
  return {
    cacheHealth: benefit.value,
    cacheSource: benefit.source,
    cacheSamples: benefit.samples ?? 0,
    cacheRate24h: benefit.source === 'site-rate' ? benefit.value : null,
    cacheSnapshotAt: benefit.at ?? null,
    cacheSnapshotExpiresAt: benefit.expiresAt ?? null,
  };
}

/** 缓存候选重排时更新实测值，并让已过期的站点快照回退。 */
export function refreshCandidateCache(candidate, cfg, measured, now = Date.now(), stats = null) {
  // 实测的失败率与延迟每次重排都要刷新：它们比缓存快照变得快得多。
  const stat = stats instanceof Map ? stats.get(candidate.channelId) : null;
  const live = stat ? {
    measuredCalls: Number(stat.calls || 0),
    measuredFails: Number((stat.fails || 0).toFixed(2)),
    realFailRate: smoothedFailRate(stat.calls, stat.fails, 1 - candidate.successRate),
    realP50Ms: stat.p50Ms > 0 && stat.latencySamples >= 2 ? Math.round(stat.p50Ms) : candidate.p50Ms,
    latencySource: stat.p50Ms > 0 && stat.latencySamples >= 2 ? 'measured' : (candidate.siteLatencySource || 'site'),
  } : {};
  if (candidate.cacheSource === 'site-rate'
      && validSnapshotTime(candidate.cacheSnapshotAt, now, candidate.cacheSnapshotExpiresAt)) {
    return { ...candidate, ...live };
  }
  const benefit = measuredCacheOf(candidate.channelId, measured, cfg) ?? cacheCostBenefit(candidate.cacheReadRatio, cfg);
  return { ...candidate, ...cacheFields(benefit), ...live };
}

/** 平滑失败率：样本少的时候别把「一次失败」当死刑。 */
export function smoothedFailRate(realCalls, realFails, siteFailRate) {
  const calls = Math.max(0, toInt(realCalls));
  const fails = Math.max(0, toInt(realFails));
  const prior = Number.isFinite(siteFailRate) ? Math.min(1, Math.max(0, siteFailRate)) : 0.02;
  return (fails + prior * 5) / (calls + 5);
}

/**
 * 判一条 listing 值不值得用：把「是否可用」与「贵不贵」一次算清。
 * 运行时（parseCandidates）与对账脚本（scripts/price-report.mjs）共用这一份判定，
 * 免得「报表说能用、运行时却不用」。
 *
 * @returns {{ ok: boolean, reason: string, candidate?: object, priced?: object }}
 */
export function classifyListing(item, channel, cfg, options = {}) {
  const workload = options.workload || normalizeWorkload(cfg);
  const badChannels = options.badChannels instanceof Set ? options.badChannels : new Set();
  const excludeChannels = options.excludeChannels instanceof Set ? options.excludeChannels : new Set();
  const limit = effectiveLimit(cfg);

  if (!item || typeof item !== 'object') return { ok: false, reason: 'bad-row' };
  if (item.supplier_channel_disabled || item.user_channel_disabled) return { ok: false, reason: 'disabled' };

  const official = item.official_price && typeof item.official_price === 'object' ? item.official_price : {};
  const channelId = toInt(item.channel_id || channel?.channel_id);
  if (channelId <= 0) return { ok: false, reason: 'bad-row' };
  if (excludeChannels.has(channelId)) return { ok: false, reason: 'failed-this-call' };
  if (badChannels.has(channelId)) return { ok: false, reason: 'known-bad' };

  // 商家侧命中率：优先用我们自己请求出来的实测值——中转站上游经常换池，
  // 「报价里说缓存很便宜」和「你的请求真的命中缓存」是两件事。
  const measuredHit = options.measuredRates instanceof Map
    ? measuredHitOf(item, channel, options.measuredRates, cfg)
    : null;
  const merchantHitRate = measuredHit && Number.isFinite(measuredHit.value)
    ? measuredHit.value
    : (Number.isFinite(cfg.merchantCacheHitRate)
      ? cfg.merchantCacheHitRate
      // 没实测过就按保守值估，别默认它会缓存命中；配置缺失时也按 0（全价）算
      : (Number.isFinite(cfg.unmeasuredMerchantCacheHitRate) ? cfg.unmeasuredMerchantCacheHitRate : 0));

  // 渠道级实测：成功/失败次数与首块延迟。有了它就不用全靠站点给的群体口径。
  const stat = options.measuredStats instanceof Map ? options.measuredStats.get(channelId) : null;
  const measuredCalls = stat && Number.isFinite(stat.calls) ? stat.calls : 0;
  // 输出/输入比也按实测走：标价里 output 常常是 input 的 3~4 倍，这个比例直接决定真实成本。
  const useOutputRatio = stat && Number.isFinite(stat.outputRatio) && stat.outputRatio > 0
    && measuredCalls >= (Number.isFinite(cfg.outputRatioMinSamples) ? cfg.outputRatioMinSamples : 2)
    && cfg.useMeasuredOutputRatio !== false;
  const listingWorkload = useOutputRatio
    ? { ...workload, outputToInput: Math.min(Math.max(stat.outputRatio, 0), 3) }
    : workload;

  const priced = priceModel(item, official, listingWorkload, { merchantCacheHitRate: merchantHitRate });
  if (!priced.ok) return { ok: false, reason: priced.code, priced };
  // 门槛用「官网同等命中率」下的报价比：它回答的是「这家报价本身贵不贵」，
  // 不该因为你这次实测命中低就把它整条踢掉。真实成本只用来排序（见 effRatio）。
  const gate = Number.isFinite(merchantHitRate)
    ? priceModel(item, official, workload, { merchantCacheHitRate: workload.officialCacheHitRate })
    : priced;
  const gateRatio = gate.ok ? gate.ratio : priced.ratio;
  if (gateRatio > limit) {
    return { ok: false, reason: `effective-over-${Math.round(limit * 100)}pct`, priced };
  }
  if (cfg.maxPriceRatio < 1 && priced.categories.input > cfg.maxPriceRatio) {
    return { ok: false, reason: 'headline-over-limit', priced };
  }

  const p50 = toInt(item.p50_ttft_ms) || toInt(item.recent_p50_ms);
  // A channel with a measured first-token median can qualify even if the site omits its latency.

  const rate = successRate(item.recent_success_rate);
  // 可靠性：有实测就用实测（贝叶斯平滑，先验取站点的群体失败率），没有才退回站点口径。
  const failRate = smoothedFailRate(measuredCalls, stat?.fails, 1 - rate);
  if (failRate * 100 > cfg.maxFailRate) return { ok: false, reason: 'high-fail-rate', priced };
  // 延迟：实测首块延迟优先（至少两次采样），否则用站点 p50。
  const measuredP50 = stat && stat.p50Ms > 0 && stat.latencySamples >= 2 ? Math.round(stat.p50Ms) : 0;
  const effectiveP50 = measuredP50 || p50;
  if (effectiveP50 <= 0) return { ok: false, reason: 'no-latency', priced };
  if (effectiveP50 > cfg.hardP50Ms) return { ok: false, reason: 'too-slow', priced };
  const samples = toInt(item.sample_count);
  if (samples < cfg.minSamples) return { ok: false, reason: 'few-samples', priced };

  const benefit = cacheBenefitOf(item, channel, options.cacheRates, cfg, options.measuredRates, options.now);

  return {
    ok: true,
    reason: '',
    priced,
    candidate: {
      channelId,
      listingId: toInt(item.listing_id),
      supplierId: toInt(item.supplier_id || channel?.supplier_id),
      channelName: String(item.channel_name || channel?.channel_name || ''),
      supplierName: String(item.supplier_name || channel?.supplier_name || ''),
      inputMicros: numericField(item.input_price_micros),
      officialInputMicros: numericField(official.input_price_micros),
      outputMicros: numericField(item.output_price_micros),
      cacheReadMicros: numericField(item.cache_read_price_micros),
      // ratio = 只看 input 的标价倍率；effRatio = 实际用量折算后的倍率（判定与打分都用它）
      ratio: priced.categories.input,
      effRatio: priced.ratio,
      maxCategoryRatio: priced.maxCategoryRatio,
      cnyPerMillion: priced.cnyPerMillion,
      officialCnyPerMillion: priced.officialCnyPerMillion,
      merchantCacheHitRate: priced.merchantCacheHitRate,
      officialCacheHitRate: priced.officialCacheHitRate,
      merchantCacheHitMeasured: priced.merchantCacheHitMeasured,
      priceCategories: priced.categories,
      p50Ms: p50,
      // 真正参与打分的延迟：有实测就用实测首块延迟
      realP50Ms: effectiveP50,
      latencySource: measuredP50 ? 'measured' : (toInt(item.p50_ttft_ms) > 0 ? 'site-ttft' : 'site'),
      siteLatencySource: toInt(item.p50_ttft_ms) > 0 ? 'site-ttft' : 'site',
      outputRatio: useOutputRatio ? listingWorkload.outputToInput : null,
      outputRatioSource: useOutputRatio ? 'measured' : 'config',
      successRate: rate,
      ...cacheFields(benefit),
      cacheReadRatio: priced.cacheReadRatio,
      sampleCount: samples,
      realFailRate: failRate,
      measuredCalls,
      measuredFails: stat && Number.isFinite(stat.fails) ? Number(stat.fails.toFixed(2)) : 0,
      failRate,
    },
  };
}

/**
 * 解析候选商家。
 * @param {object} [options.workload] 用量配比（默认取 cfg 里的同名四项）
 * @returns {{ candidates: Array, total: number, rejected: Record<string, number> }}
 */
export function parseCandidates(payload, modelName, cfg, options = {}) {
  const workload = options.workload || normalizeWorkload(cfg);
  const listingOptions = { ...options, workload, now: options.now ?? Date.now() };
  const rejected = {};
  const bump = (reason) => { rejected[reason] = (rejected[reason] || 0) + 1; };

  const channels = payload && typeof payload === 'object' ? payload.data : null;
  if (!Array.isArray(channels)) return { candidates: [], total: 0, rejected: { 'no-data': 1 } };

  let total = 0;
  const seen = new Set();
  const candidates = [];

  for (const channel of channels) {
    if (!channel || typeof channel !== 'object') continue;
    const listings = channel.listings;
    if (!Array.isArray(listings)) continue;
    for (const item of listings) {
      if (!item || typeof item !== 'object') continue;
      if (String(item.model_name || '') !== modelName) continue;
      total += 1;

      const verdict = classifyListing(item, channel, cfg, listingOptions);
      if (!verdict.ok) {
        bump(verdict.reason);
        continue;
      }
      if (seen.has(verdict.candidate.channelId)) continue;
      seen.add(verdict.candidate.channelId);
      candidates.push(verdict.candidate);
    }
  }
  return { candidates, total, rejected };
}

/**
 * 打分：真正决定你掏多少钱的是「实测命中率 × 报价 + 实测输出比」，所以成本项
 * 直接吃 effRatio（已含这两项），不再单列缓存分——同一件事算两遍会把「缓存好但
 * 标价略高」的商家过度压低。
 *
 * 三个维度，分数越低越好：
 *   cost       = 对数归一的真实倍率（对数是为了让 2% 与 70% 的差别不被 0.02 淹没）
 *   failRate  = 平滑后的实测失败率（有实测用实测，没有用站点群体值）
 *   latency   = 实测首块延迟 / latencyRefMs（封顶 3 倍）
 *
 * 权重默认 成本 60 : 可靠性 30 : 延迟 10 —— 「便宜」仍然是主要目标，
 * 但一家 20% 失败率的便宜商家不该赢过 1% 失败率、贵 15% 的商家。
 */
export function rankCandidates(candidates, cfg) {
  if (!Array.isArray(candidates) || candidates.length === 0) return [];
  const latencySpan = Math.max(cfg.latencyRefMs || cfg.hardP50Ms || 1, 1);
  const weightOf = (value, fallback = 0) => (Number.isFinite(Number(value)) ? Number(value) : fallback);
  const priceWeight = weightOf(cfg.priceWeight, 60);
  const reliabilityWeight = weightOf(cfg.reliabilityWeight, 30);
  const latencyWeight = weightOf(cfg.latencyWeight, 10);
  const weightSum = (priceWeight + reliabilityWeight + latencyWeight) || 1;

  return candidates
    .map((candidate) => {
      const costRatio = Number.isFinite(candidate.effRatio) ? candidate.effRatio : candidate.ratio;
      const cost = Number.isFinite(costRatio) && costRatio >= 0 ? normalizedCost(costRatio, cfg) : 1;
      const reliability = Math.min(Math.max(Number.isFinite(candidate.realFailRate)
        ? candidate.realFailRate : candidate.failRate, 0), 1);
      const latencyMs = Number.isFinite(candidate.realP50Ms) ? candidate.realP50Ms : candidate.p50Ms;
      const latency = Math.min(Math.max(latencyMs, 0) / latencySpan, 3);
      const score = (priceWeight * cost + reliabilityWeight * reliability + latencyWeight * latency) / weightSum;
      return { ...candidate, score };
    })
    .sort((a, b) => a.score - b.score || a.channelId - b.channelId);
}

/**
 * 挑下一家：已排好序的候选里，跳过本条路由的「已知坏」与本轮的失败集合。
 * @returns 候选对象或 null
 */
export function pickNext(candidates, { excludeChannels = new Set() } = {}) {
  for (const candidate of candidates) {
    if (excludeChannels.has(candidate.channelId)) continue;
    return candidate;
  }
  return null;
}

/** 只要挑的那几家，别把几 MB 的候选表塞进状态文件。 */
export function trimCandidates(ranked, limit = 24) {
  return ranked.slice(0, limit).map((candidate) => ({
    channelId: candidate.channelId,
    listingId: candidate.listingId,
    channelName: candidate.channelName,
    supplierName: candidate.supplierName,
    ratio: Number((candidate.ratio ?? 0).toFixed(4)),
    effRatio: Number((candidate.effRatio ?? candidate.ratio ?? 0).toFixed(4)),
    cnyPerMillion: Number((candidate.cnyPerMillion ?? 0).toFixed(3)),
    p50Ms: candidate.p50Ms,
    failRate: candidate.failRate,
    successRate: Number(candidate.successRate.toFixed(4)),
    cacheRate24h: Number.isFinite(candidate.cacheRate24h) ? Number(candidate.cacheRate24h.toFixed(4)) : null,
    cacheHealth: Number.isFinite(candidate.cacheHealth) ? Number(candidate.cacheHealth.toFixed(4)) : null,
    cacheSource: candidate.cacheSource || 'none',
    cacheReadRatio: candidate.cacheReadRatio ?? null,
    cacheSnapshotAt: candidate.cacheSnapshotAt ?? null,
    cacheSnapshotExpiresAt: candidate.cacheSnapshotExpiresAt ?? null,
    cacheSamples: Math.round(Number(candidate.cacheSamples || 0)),
    sampleCount: candidate.sampleCount,
    score: Number(candidate.score.toFixed(4)),
  }));
}

/** Cut the eligible pool once, after exclusions; score only the faster half. */
export function rankFastestHalf(rows, cfg) {
  const eligible = rows.filter(row => row.realP50Ms > 0 && row.realP50Ms <= cfg.hardP50Ms
    && row.realFailRate * 100 <= cfg.maxFailRate);
  eligible.sort((a, b) => a.realP50Ms - b.realP50Ms || a.channelId - b.channelId);
  return rankCandidates(eligible.slice(0, Math.ceil(eligible.length / 2)), cfg);
}
