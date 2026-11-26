/**
 * 「这个商家到底贵不贵」——按**实际用量**算，而不是只看标价。
 *
 * 站点的 token 报价以 micros 表示，每百万 token 的报价除以 1,000,000 后得到币种金额：
 *   input_price_micros        未命中缓存的输入
 *   cache_read_price_micros   命中缓存的输入（DSH 这类长系统提示的场景，99% 都走这一档）
 *   output_price_micros       输出
 * 官方价也按同样分项返回（official_price.*）。两边使用相同币种与单位时，可以直接按同一份
 * 用量配比折算倍率。旧数据省略币种时沿用站点的 USD 口径。
 *
 * 一次调用的成本（按 1M **输入** token 折算）：
 *   blend = (1-h) * P_in  +  h * P_cache  +  r * P_out
 *   h = 缓存命中率，r = 输出 token / 输入 token
 * 实际倍率 = 商家 blend / 官方 blend。
 *
 * 为什么要这么算：只看 input 倍率会被「输入便宜、输出贵一倍」的商家骗过去；
 * 反过来，正确提供缓存折扣的商家在只看标价时又会被低估。实际用量配比是唯一
 * 说得清的口径。
 */

/** 默认用量配比：缓存命中 95%，输出约占输入 3%。 */
export const DEFAULT_WORKLOAD = Object.freeze({
  cacheHitRate: 0.95,
  outputToInput: 0.03,
  cacheWriteShare: 0,
});

const MICROS_PER_UNIT = 1_000_000;

function numericField(value) {
  if (typeof value !== 'number' && (typeof value !== 'string' || !value.trim())) return NaN;
  return Number(value);
}

function toMicros(value) {
  const n = numericField(value);
  // 当前站点将 0 用于免费或未标价，无法用它证明实际收费。
  return Number.isFinite(n) && n > 0 ? n : null;
}

const clampHit = (value) => Math.min(1, Math.max(0, value));

/**
 * 把配置洗成一个合法的用量配比。
 *
 * **必须幂等**：输入既可能是原始配置（键名 unmeasuredMerchantCacheHitRate / outputToInputRatio），
 * 也可能是上一次本函数的输出（键名 unmeasuredCacheHitRate / outputToInput）。priceModel 内部
 * 会再洗一次，如果只认前一套键名，第二次就会把 0.2 读成 NaN 再退回 0——缓存假设被静默清零，
 * 真实成本被高估好几倍，而且没有任何报错。
 */
export function normalizeWorkload(cfg = {}) {
  const hit = numericField(cfg.cacheHitRate);
  const officialHit = numericField(cfg.officialCacheHitRate);
  const merchantHit = numericField(cfg.merchantCacheHitRate);
  // 两套键名都认：输出用的是短名，配置用的是长名。
  const unmeasuredHit = numericField(cfg.unmeasuredMerchantCacheHitRate ?? cfg.unmeasuredCacheHitRate);
  const out = numericField(cfg.outputToInputRatio ?? cfg.outputToInput);
  const write = numericField(cfg.cacheWriteShare);
  // 汇率不再由配置提供：优先用 listing 自带的 realtime_ratio_exchange_rate（classifyListing 注入），
  // 这里只保留一个安全兜底，防止站点偶尔没给汇率字段时算出 NaN。
  const fx = numericField(cfg.fx);
  const baseline = Number.isFinite(hit) ? clampHit(hit) : DEFAULT_WORKLOAD.cacheHitRate;
  return {
    cacheHitRate: baseline,
    // 官网侧：官方缓存跨请求真命中（同一段系统提示反复出现），所以能吃到很低的缓存读价。
    officialCacheHitRate: Number.isFinite(officialHit) ? clampHit(officialHit) : baseline,
    // 商家侧：默认 null = 用实测命中率；没有实测时用 unmeasuredCacheHitRate（默认 0，按全价估）。
    merchantCacheHitRate: Number.isFinite(merchantHit) ? clampHit(merchantHit) : null,
    unmeasuredCacheHitRate: Number.isFinite(unmeasuredHit) ? clampHit(unmeasuredHit) : 0,
    outputToInput: Number.isFinite(out) ? Math.max(0, out) : DEFAULT_WORKLOAD.outputToInput,
    cacheWriteShare: Number.isFinite(write) ? Math.min(1, Math.max(0, write)) : DEFAULT_WORKLOAD.cacheWriteShare,
    fx: Number.isFinite(fx) && fx > 0 ? fx : 7.2,
    assumeMissingCacheAsInput: cfg.assumeMissingCacheAsInput === true,
  };
}

/** 在给定命中率下把三类单价压成「每 1M 输入 token 的实际花费」（micros）。 */
export function blendMicrosAt(prices, workload, cacheHitRate) {
  const cached = clampHit(cacheHitRate);
  const fresh = 1 - cached;
  return fresh * prices.input
    + cached * prices.cacheRead
    + workload.outputToInput * prices.output
    + workload.cacheWriteShare * (prices.cacheWrite || 0);
}

/**
 * 把三类单价压成「每 1M 输入 token 的实际花费」（单位仍是 micros）。
 * @param {{input:number, cacheRead:number, output:number, cacheWrite?:number}} prices
 */
export function blendMicros(prices, workload) {
  return blendMicrosAt(prices, workload, workload.cacheHitRate);
}

function pricesOf(raw) {
  return {
    input: toMicros(raw?.inputMicros ?? raw?.input_price_micros),
    output: toMicros(raw?.outputMicros ?? raw?.output_price_micros),
    cacheRead: toMicros(raw?.cacheReadMicros ?? raw?.cache_read_price_micros),
    cacheWrite: toMicros(raw?.cacheWriteMicros ?? raw?.cache_write_price_micros),
  };
}

function priceBasis(merchant, official) {
  for (const [raw, source] of [[merchant, '商家'], [official, '官方']]) {
    const chargeType = String(raw?.charge_type ?? '').trim().toLowerCase();
    const unit = String(raw?.pricing_unit ?? '').trim().toLowerCase();
    if ((chargeType && chargeType !== 'per_token') || (unit && unit !== 'per_1m_tokens')) {
      return { ok: false, code: 'unsupported-price-unit', reason: `${source}报价不适用百万 token 成本估算` };
    }
  }
  const currencyOf = (raw) => String(raw?.pricing_currency ?? '').trim().toUpperCase() || 'USD';
  const currency = currencyOf(merchant);
  const officialCurrency = currencyOf(official);
  if (currency !== officialCurrency) {
    return { ok: false, code: 'currency-mismatch', reason: `商家与官方报价币种不一致（${currency} / ${officialCurrency}）` };
  }
  if (currency !== 'USD' && currency !== 'CNY') {
    return { ok: false, code: 'unsupported-price-currency', reason: `报价币种不支持（${currency}）` };
  }
  return { ok: true, currency };
}

/** 仅比较商家与官方均已给出的缓存读价。 */
export function quotedCacheReadRatio(merchantRaw, officialRaw) {
  if (!priceBasis(merchantRaw, officialRaw).ok) return null;
  const merchant = toMicros(merchantRaw?.cacheReadMicros ?? merchantRaw?.cache_read_price_micros);
  const official = toMicros(officialRaw?.cacheReadMicros ?? officialRaw?.cache_read_price_micros);
  const ratio = merchant !== null && official !== null ? merchant / official : null;
  return Number.isFinite(ratio) && ratio > 0 ? ratio : null;
}

/**
 * 算一个商家的实际倍率与单价。
 *
 * @returns {{
 *   ok: boolean, reason?: string,
 *   ratio: number,                 // 实际用量下的商家/官方 成本比
 *   maxCategoryRatio: number,      // 已知分项中的最高倍率，仅供展示
 *   merchantBlend: number, officialBlend: number,   // micros / 1M 输入 token
 *   cnyPerMillion: number,         // 商家实际使用单价（元 / 百万输入 token）
 *   officialCnyPerMillion: number,
 *   categories: { input: number, cacheRead: number|null, output: number|null, cacheWrite: number|null },
 *   cacheReadRatio: number|null,    // 根据商家与官方实际报价计算，不采用推定的价格
 *   assumed: string[]              // 站点没给全时用了哪条兜底
 * }}
 */
export function priceModel(merchantRaw, officialRaw, workload = DEFAULT_WORKLOAD, options = {}) {
  const basis = priceBasis(merchantRaw, officialRaw);
  if (!basis.ok) return basis;
  const use = normalizeWorkload(workload);
  const assumed = [];
  const merchant = pricesOf(merchantRaw);
  const official = pricesOf(officialRaw);
  const categoryRatio = (key) => merchant[key] !== null && official[key] !== null
    ? merchant[key] / official[key]
    : null;
  const cacheReadRatio = categoryRatio('cacheRead');

  // 两侧命中率分开：官网能吃到自己的缓存，商家上游是不是同一份缓存要实测。
  const officialHit = Number.isFinite(options.officialCacheHitRate)
    ? clampHit(options.officialCacheHitRate) : use.officialCacheHitRate;
  const explicitMerchantHit = Number.isFinite(options.merchantCacheHitRate)
    ? clampHit(options.merchantCacheHitRate)
    : (Number.isFinite(use.merchantCacheHitRate) ? clampHit(use.merchantCacheHitRate) : null);
  const measuredMerchantHit = Number.isFinite(explicitMerchantHit);
  // 没有实测、也没有显式配置时按 unmeasured（默认 0，全按输入价）——不再拿官网命中率替它担保。
  const merchantHit = measuredMerchantHit ? explicitMerchantHit : clampHit(use.unmeasuredCacheHitRate ?? 0);
  if (measuredMerchantHit && merchantHit < officialHit) assumed.push('merchant.cacheHitRate=measured');

  if (official.input === null) return { ok: false, code: 'no-official-price', reason: '官方输入价缺失或无效' };
  if (merchant.input === null) return { ok: false, code: 'no-price', reason: '商家输入价缺失或无效' };
  const rawCacheRead = merchantRaw?.cacheReadMicros ?? merchantRaw?.cache_read_price_micros;
  const cacheReadUnquoted = rawCacheRead == null || rawCacheRead === '' || numericField(rawCacheRead) === 0;
  if (use.cacheHitRate > 0 && cacheReadUnquoted && use.assumeMissingCacheAsInput) {
    merchant.cacheRead = merchant.input;
    assumed.push('merchant.cacheRead=merchant.input');
  }

  const active = [
    ['cacheRead', use.cacheHitRate, '缓存读取'],
    ['output', use.outputToInput, '输出'],
    ['cacheWrite', use.cacheWriteShare, '缓存写入'],
  ];
  for (const [key, share, name] of active) {
    if (share === 0) continue;
    if (official[key] === null) return { ok: false, code: 'no-official-price', reason: `官方${name}价缺失或无效` };
    if (merchant[key] === null) return { ok: false, code: 'no-price', reason: `商家${name}价缺失或无效` };
  }

  const merchantBlend = blendMicrosAt(merchant, use, merchantHit);
  const officialBlend = blendMicrosAt(official, use, officialHit);
  const ratio = merchantBlend / officialBlend;
  const fx = basis.currency === 'USD' ? use.fx : 1;
  const cnyPerMillion = (merchantBlend / MICROS_PER_UNIT) * fx;
  const officialCnyPerMillion = (officialBlend / MICROS_PER_UNIT) * fx;
  if (ratio <= 0 || ![ratio, cnyPerMillion, officialCnyPerMillion].every(Number.isFinite)) {
    return { ok: false, code: 'invalid-blended-price', reason: '折算价超出可计算范围' };
  }

  const categories = {
    input: categoryRatio('input'),
    cacheRead: categoryRatio('cacheRead'),
    output: categoryRatio('output'),
    cacheWrite: categoryRatio('cacheWrite'),
  };
  const maxCategoryRatio = Math.max(...Object.values(categories).filter(Number.isFinite));

  return {
    ok: true,
    ratio,
    maxCategoryRatio,
    merchantBlend,
    officialBlend,
    cnyPerMillion,
    officialCnyPerMillion,
    categories,
    cacheReadRatio: Number.isFinite(cacheReadRatio) && cacheReadRatio > 0 ? cacheReadRatio : null,
    // 这次折算到底按哪两个命中率算的——报表和事件里要能看出来，别再被一个全局常数糊住。
    officialCacheHitRate: officialHit,
    merchantCacheHitRate: merchantHit,
    merchantCacheHitMeasured: measuredMerchantHit,
    assumed,
  };
}

/**
 * 报价人话：给日志和报表用。
 * @param {ReturnType<typeof priceModel>} priced
 */
export function describePrice(priced, workload = DEFAULT_WORKLOAD) {
  if (!priced?.ok) return priced?.reason || '无法计算';
  const pct = (value) => (Number.isFinite(value) ? `${(value * 100).toFixed(1)}%` : '未知');
  return `实际 ${pct(priced.ratio)} 官方价（¥${priced.cnyPerMillion.toFixed(3)}/M 输入 token，`
    + `缓存命中 ${pct(workload.cacheHitRate)}、输出/输入 ${pct(workload.outputToInput)}；`
    + `分项 输入 ${pct(priced.categories.input)} / 缓存 ${pct(priced.categories.cacheRead)} / 输出 ${pct(priced.categories.output)}）`;
}
