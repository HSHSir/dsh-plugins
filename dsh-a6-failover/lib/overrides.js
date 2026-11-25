/**
 * 本地覆盖层：设置页保存的值写进 $DSH_HOME/a6-failover/config.json，覆盖 profile 里那段 patch。
 *
 * 为什么不用「改写 cordis.patch.yml」：那是 YAML，手写生成的代价高、写错一次插件就起不来；
 * 而且 patch 只在启动时读。JSON 覆盖层的好处是——**改完立刻生效**，不用重启，
 * 也不会碰到 profile 里别人的配置。
 *
 * 优先级：patch 里的 config  <  config.json  <  设置页刚保存的值（同一次进程内即时生效）
 */

import fs from 'node:fs';
import path from 'node:path';

/** 设置页允许改的字段（其余字段只认 patch，避免 UI 把插件搞成不可用）。 */
export const EDITABLE_KEYS = Object.freeze([
  // 站点接入
  'enabled', 'providerPattern', 'providers', 'defaultControlBase', 'accessToken', 'userId', 'credentialSources',
  // 价格线
  'maxEffectiveRatio', 'cacheHitRate', 'officialCacheHitRate', 'merchantCacheHitRate', 'unmeasuredMerchantCacheHitRate',
  'outputToInputRatio', 'useMeasuredOutputRatio', 'outputRatioMinSamples', 'cacheWriteShare', 'assumeMissingCacheAsInput',
  'officialPriceOverride', 'maxPriceRatio', 'fxCnyPerUsd',
  'proxyUrl', 'connectionMode', 'directProbeMs', 'channelsRefreshMs', 'channelsTimeoutMs',
  // 打分权重
  'priceWeight', 'reliabilityWeight', 'cacheWeight', 'cacheFallbackDiscount', 'cacheMeasureMinSamples', 'latencyWeight', 'successWeight',
  // 延迟与质量门槛
  'latencyRefMs', 'hardP50Ms', 'maxFailRate', 'minSamples',
  // 切换策略
  'firstResponseTimeoutMs', 'firstResponseBudgetMs', 'streamStallTimeoutMs', 'sameRouteRetries',
  'maxSwitchesPerCall', 'maxSwitchesPerHour', 'degradeAfterFailures', 'degradeCooldownMs', 'switchDedupeMs',
  'switchOnThrown', 'switchOnFinishError', 'switchOnEmpty', 'switchOnTruncated',
  'preferFallbackWhenDegraded', 'fallbackToSmartRouting', 'takeOverExistingPin', 'fallbackRoutes',
  'autoSwitchOnContextFailure', 'contextHeadroomRatio', 'maxContextSwitchesPerHour',
  // 观测
  'verbose', 'logEvents', 'channelCacheTtlMs', 'controlTimeoutMs',
]);

/** 需要打码回传的字段（设置页只显示「已设置」，不回传明文）。 */
export const SECRET_KEYS = Object.freeze(['accessToken']);

const NUMERIC = new Set([
  'maxEffectiveRatio', 'cacheHitRate', 'officialCacheHitRate', 'merchantCacheHitRate', 'unmeasuredMerchantCacheHitRate',
  'outputToInputRatio', 'outputRatioMinSamples', 'cacheWriteShare', 'maxPriceRatio', 'fxCnyPerUsd',
  'priceWeight', 'reliabilityWeight', 'cacheWeight', 'cacheMeasureMinSamples', 'latencyWeight', 'successWeight', 'latencyRefMs', 'hardP50Ms', 'maxFailRate', 'minSamples',
  'firstResponseTimeoutMs', 'firstResponseBudgetMs', 'streamStallTimeoutMs', 'sameRouteRetries',
  'maxSwitchesPerCall', 'maxSwitchesPerHour', 'degradeAfterFailures', 'degradeCooldownMs', 'switchDedupeMs',
  'channelCacheTtlMs', 'controlTimeoutMs', 'contextHeadroomRatio', 'maxContextSwitchesPerHour',
]);

const BOOLEAN = new Set([
  'enabled', 'assumeMissingCacheAsInput', 'switchOnThrown', 'switchOnFinishError', 'switchOnEmpty',
  'switchOnTruncated', 'preferFallbackWhenDegraded', 'fallbackToSmartRouting', 'takeOverExistingPin', 'verbose', 'logEvents',
  'autoSwitchOnContextFailure', 'cacheFallbackDiscount', 'useMeasuredOutputRatio',
]);

export function overridesPath(dshHome) {
  return path.join(dshHome, 'a6-failover', 'config.json');
}

function readJson(file) {
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf8'));
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

function writeJsonAtomic(file, value) {
  fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, `${JSON.stringify(value, null, 2)}\n`, { encoding: 'utf8', mode: 0o600 });
  fs.renameSync(tmp, file);
}

/** 把任意输入洗成「只含可编辑字段、类型正确」的一层。 */
export function pickEditable(source = {}) {
  const out = {};
  if (!source || typeof source !== 'object') return out;
  for (const key of EDITABLE_KEYS) {
    if (!Object.prototype.hasOwnProperty.call(source, key)) continue;
    const value = source[key];
    if (value === undefined || value === null) continue;
    if (NUMERIC.has(key)) {
      const n = Number(value);
      if (Number.isFinite(n)) out[key] = n;
      continue;
    }
    if (BOOLEAN.has(key)) {
      out[key] = typeof value === 'boolean' ? value : String(value) === 'true';
      continue;
    }
    if (key === 'providers' || key === 'credentialSources') {
      if (Array.isArray(value)) out[key] = value.map((item) => String(item).trim()).filter(Boolean);
      else if (typeof value === 'string') out[key] = value.split(/[,，\s]+/).map((s) => s.trim()).filter(Boolean);
      continue;
    }
    if (key === 'fallbackRoutes') {
      if (Array.isArray(value)) {
        out[key] = value
          .filter((row) => row && typeof row === 'object')
          .map((row) => ({ provider: String(row.provider || '').trim(), model: String(row.model || '').trim() }))
          .filter((row) => row.provider && row.model);
      }
      continue;
    }
    if (key === 'officialPriceOverride') {
      // 官网基准价：三个数都是元/百万 token；input 必须为正，否则当作「不用覆盖」。
      if (value && typeof value === 'object' && !Array.isArray(value)) {
        const take = (k) => {
          const n = Number(value[k]);
          return Number.isFinite(n) && n >= 0 ? n : null;
        };
        const input = take('input');
        if (input > 0) {
          out[key] = { input, output: take('output') ?? input * 4, cacheRead: take('cacheRead') ?? input * 0.02 };
        }
      }
      continue;
    }
    out[key] = String(value).trim();
  }
  return out;
}

export function loadOverrides(dshHome) {
  return pickEditable(readJson(overridesPath(dshHome)) || {});
}

/**
 * 合并并写回：只写可编辑字段，其余保留。
 * 传进来的 `accessToken: ''` 表示「不改」，不会把已有值清空。
 */
export function saveOverrides(dshHome, incoming) {
  const file = overridesPath(dshHome);
  const current = pickEditable(readJson(file) || {});
  const next = { ...current };
  const picked = pickEditable(incoming);
  for (const [key, value] of Object.entries(picked)) {
    if (SECRET_KEYS.includes(key) && String(value) === '') continue; // 留空 = 不动
    next[key] = value;
  }
  writeJsonAtomic(file, next);
  return next;
}

export function clearOverrides(dshHome) {
  const file = overridesPath(dshHome);
  try { fs.unlinkSync(file); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  return {};
}

/** 只把「有效且非空」的覆盖值盖到 patch 配置上。 */
export function mergeOverrides(baseConfig = {}, overrides = {}) {
  const merged = { ...baseConfig };
  for (const [key, value] of Object.entries(overrides || {})) {
    if (!EDITABLE_KEYS.includes(key)) continue;
    if (SECRET_KEYS.includes(key) && String(value) === '') continue;
    merged[key] = value;
  }
  return merged;
}

/** 回传给设置页的样子：密钥打码，其余原样。 */
export function forDisplay(overrides = {}, patchConfig = {}) {
  const out = {};
  for (const key of EDITABLE_KEYS) {
    if (SECRET_KEYS.includes(key)) {
      const current = String(overrides[key] ?? patchConfig[key] ?? '');
      out[key] = '';
      out[`${key}Set`] = current.length > 0;
      continue;
    }
    const value = overrides[key] ?? patchConfig[key];
    if (value !== undefined) out[key] = value;
  }
  return out;
}
