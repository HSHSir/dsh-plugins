/**
 * 本地自检：不联网、不碰状态文件（用临时目录），验证「缓存收益」这条链路。
 *
 * 覆盖三块：
 *   1. state 的实测用量记账（口径、模型隔离、样本数、非法输入）
 *   2. selector 的缓存收益取值优先级（站点 → 实测 → 读价 → 中性）与排序影响
 *   3. stream-guard 对 `{ type: 'usage' }` 块的采集（成功才回调、失败不记）
 *
 * 跑法：node scripts/self-test.mjs（或 pnpm run self-test）
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const root = path.resolve(here, '..');
const load = (rel) => import(pathToFileURL(path.join(root, rel)).href);

const {
  cacheBenefitOf, measuredHitOf, parseCandidates, rankCandidates, officialWithOverride,
} = await load('lib/selector.js');
const { normalizeWorkload, priceModel } = await load('lib/pricing.js');
const { createStore } = await load('lib/state.js');
const { normalizeConfig } = await load('lib/config.js');
const { guardedStream } = await load('lib/stream-guard.js');
const { createSwitcher } = await load('lib/switcher.js');
const { a6Request } = await load('lib/a6.js');
const { apply } = await load('lib/index.js');
const { VERSION } = await load('lib/version.js');

let pass = 0;
const fails = [];
const eq = (name, actual, expected) => {
  const a = JSON.stringify(actual);
  const e = JSON.stringify(expected);
  if (a === e) { pass += 1; return; }
  fails.push(`${name}: got ${a}, want ${e}`);
};
const near = (name, actual, expected, tol = 1e-6) => {
  if (Number.isFinite(actual) && Math.abs(actual - expected) <= tol) { pass += 1; return; }
  fails.push(`${name}: got ${actual}, want ~${expected}`);
};

// 设置页和安装包应显示同一版本。
{
  const pkg = JSON.parse(fs.readFileSync(path.join(root, 'package.json'), 'utf8'));
  eq('version.js 与 package.json 版本一致', VERSION, pkg.version);
}

const tempDirs = [];
const tempDir = (tag) => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), `a6-${tag}-`));
  tempDirs.push(dir);
  return dir;
};

// ---- 1. 实测用量记账 ----
const home1 = tempDir('measure');
const store = createStore({ dshHome: home1, logEvents: false, now: () => 1000000 });
for (const latencyMs of [90, 100, 9000]) store.noteChannelOutcome({ model: 'm1', channelId: 6, ok: true, latencyMs });
eq('延迟 P50 使用中位数', store.channelStats('m1').get(6)?.p50Ms, 100);
for (let i = 0; i < 5; i += 1) {
  store.noteUsage({ model: 'm1', channelId: 7316, cacheRead: 900, cacheWrite: 0, uncachedInput: 100 });
}
const rates = store.usageRates('m1');
eq('usage 条数', rates.size, 1);
near('实测命中率', rates.get(7316).rate, 0.9);
eq('实测样本数', Math.round(rates.get(7316).samples), 5);
near('提示词总量', rates.get(7316).promptTokens, 5000);

store.noteUsage({ model: 'm2', channelId: 7316, cacheRead: 10, uncachedInput: 990 });
near('不同模型独立统计', store.usageRates('m2').get(7316).rate, 0.01);
near('原模型不受影响', store.usageRates('m1').get(7316).rate, 0.9);
eq('channelId=0 不记账', store.noteUsage({ model: 'm1', channelId: 0, cacheRead: 1, uncachedInput: 1 }), null);

const zeroStore = createStore({ dshHome: tempDir('zero'), logEvents: false });
zeroStore.noteUsage({ model: 'm1', channelId: 5, cacheRead: 0, cacheWrite: 0, uncachedInput: 0 });
eq('全 0 用量不入表', zeroStore.usageRates('m1').size, 0);

// ---- 2. 缓存收益取值优先级 ----
const cfg = normalizeConfig({ cacheMeasureMinSamples: 3 });
const item = {
  channel_id: 7316, listing_id: 56322, model_name: 'm1',
  input_price_micros: 1000000, cache_read_price_micros: 20000,
  official_price: { cache_read_price_micros: 100000 },
};
const channel = { channel_id: 7316 };

eq('站点有数据时优先站点', cacheBenefitOf(item, channel, new Map([[56322, { rate: 0.42 }]]), cfg, rates).source, 'site-rate');
eq('站点没数据用实测', cacheBenefitOf(item, channel, null, cfg, rates).source, 'measured');
near('实测取值', cacheBenefitOf(item, channel, null, cfg, rates).value, 0.9);

const thinStore = createStore({ dshHome: tempDir('thin'), logEvents: false });
thinStore.noteUsage({ model: 'm1', channelId: 7316, cacheRead: 1, uncachedInput: 99 });
eq('样本不足改用缓存读价', cacheBenefitOf(item, channel, null, cfg, thinStore.usageRates('m1')).source, 'cache-read-cost');
near('缓存成本分由官网比值换算', cacheBenefitOf(item, channel, null, cfg, thinStore.usageRates('m1')).value, 1 / (1 + Math.log1p(100) / Math.log1p(350)));
eq('门槛调低后实测生效', cacheBenefitOf(item, channel, null, normalizeConfig({ cacheMeasureMinSamples: 1 }), new Map([[7316, { rate: 0.01, samples: 1 }]])).source, 'measured');
eq('关掉缓存读价后无数据=中性', cacheBenefitOf(item, channel, null, normalizeConfig({ cacheFallbackDiscount: false }), new Map()).source, 'none');
eq('measuredHitOf 空表', measuredHitOf(item, channel, new Map(), cfg), null);
eq('measuredHitOf 非 Map', measuredHitOf(item, channel, null, cfg), null);

// ---- 3. 排序：实测命中率高的先上 ----
// 缓存读价按官方一致的比例给（2%），这样 5% 命中率不会把真实成本推到门槛之外，
// 这条用例只考察「同一价格下命中率决定成本」。
const mkListing = (id, inputMicros, p50) => ({
  channel_id: id, listing_id: id, model_name: 'm1',
  input_price_micros: inputMicros, output_price_micros: inputMicros * 4,
  cache_read_price_micros: inputMicros * 0.02,
  official_price: { input_price_micros: 1000000, output_price_micros: 4000000, cache_read_price_micros: 20000 },
  recent_p50_ms: p50, last_test_response_ms: p50, recent_success_rate: 9990, sample_count: 1000,
});
// 两家都报 0.1 倍：命中率不同时真实成本不同，但都远在 0.7 门槛之内。
const payload = { data: [
  { channel_id: 1, listings: [mkListing(1, 100000, 2000)] },
  { channel_id: 2, listings: [mkListing(2, 96000, 2000)] },
] };
const measured = new Map([[1, { rate: 0.95, samples: 8 }], [2, { rate: 0.05, samples: 8 }]]);
const ranked = rankCandidates(parseCandidates(payload, 'm1', cfg, { measuredRates: measured }).candidates, cfg);
eq('两家都进候选', ranked.length, 2);
eq('命中率高者排第一（尽管贵 4.2%）', ranked[0].channelId, 1);
eq('来源标记', ranked[0].cacheSource, 'measured');
eq('样本数带出', ranked[0].cacheSamples, 8);

const zeroCfg = normalizeConfig({ cacheWeight: 0 });
const rankedNoCache = rankCandidates(parseCandidates(payload, 'm1', zeroCfg, {}).candidates, zeroCfg);
eq('cacheWeight=0 退回纯成本排序', rankedNoCache[0].channelId, 2);

const withSite = parseCandidates(payload, 'm1', cfg, {
  cacheRates: new Map([[1, { rate: 0.1 }]]), measuredRates: measured,
});
const rankedSite = rankCandidates(withSite.candidates, cfg);
eq('站点数据压过实测', rankedSite.find((c) => c.channelId === 1).cacheSource, 'site-rate');
eq('没站点数据的仍用实测', rankedSite.find((c) => c.channelId === 2).cacheSource, 'measured');

// ---- 3b. 门槛用真实成本，不是「官网同等命中率」 ----
// 标价看着便宜（input 0.44 倍），但缓存读价只有 input 的 2%，实测几乎不命中 → 真实成本远超 0.7。
{
  const cheat = {
    channel_id: 4137, listing_id: 4137, model_name: 'm1',
    input_price_micros: 131040, output_price_micros: 524160, cache_read_price_micros: 2621,
    official_price: { input_price_micros: 300000, output_price_micros: 1200000, cache_read_price_micros: 6000 },
    recent_p50_ms: 2251, last_test_response_ms: 3200, recent_success_rate: 9830, sample_count: 1000,
  };
  const payloadOne = { data: [{ channel_id: 4137, listings: [cheat] }] };
  const lowHit = new Map([[4137, { rate: 0.149, samples: 6 }]]);
  const verdict = parseCandidates(payloadOne, 'm1', cfg, { measuredRates: lowHit });
  eq('标价便宜但实测不命中 → 真实成本超线被淘汰', verdict.candidates.length, 0);
  eq('淘汰原因记为成本超线', Object.keys(verdict.rejected)[0], 'effective-over-70pct');

  // 同样的商家，实测命中率够高时应当入选
  const goodHit = new Map([[4137, { rate: 0.95, samples: 6 }]]);
  eq('命中率高时同一家可以入选', parseCandidates(payloadOne, 'm1', cfg, { measuredRates: goodHit }).candidates.length, 1);

  // 完全没有实测的新商家：不能因为 unmeasured=0 就被一次性打死
  eq('未实测的新商家仍按标价判定，不会全灭', parseCandidates(payloadOne, 'm1', cfg, {}).candidates.length, 1);
}

// ---- 3d. normalizeWorkload 必须幂等（二次规范化不能把假设值清成 0）----
{
  const once = normalizeWorkload({ unmeasuredMerchantCacheHitRate: 0.2, officialCacheHitRate: 0.98, outputToInputRatio: 0.005 });
  eq('一次规范化保留商家缓存假设', once.unmeasuredCacheHitRate, 0.2);
  const twice = normalizeWorkload(once);
  eq('二次规范化仍保留商家缓存假设（幂等）', twice.unmeasuredCacheHitRate, 0.2);
  eq('二次规范化仍保留输出占比（幂等）', twice.outputToInput, 0.005);
  eq('二次规范化仍保留官网命中率（幂等）', twice.officialCacheHitRate, 0.98);

  // priceModel 内部会再洗一次，这是最容易丢值的地方。
  const item = { input_price_micros: 27000, output_price_micros: 108000, cache_read_price_micros: 540 };
  const off = { input_price_micros: 300000, output_price_micros: 1200000, cache_read_price_micros: 6000 };
  const priced = priceModel(item, off, once, {});
  eq('priceModel 用上了 20% 假设而不是 0', priced.merchantCacheHitRate, 0.2);
  near('商家 blend 与手算一致', priced.merchantBlend, 0.8 * 27000 + 0.2 * 540 + 0.005 * 108000, 1e-6);
  near('官方 blend 与手算一致', priced.officialBlend, 0.02 * 300000 + 0.98 * 6000 + 0.005 * 1200000, 1e-6);
}

// ---- 3e. 官网基准价覆盖 ----
{
  const base = { input_price_micros: 9000, output_price_micros: 36000, cache_read_price_micros: 180,
    official_price: { input_price_micros: 150000, output_price_micros: 600000, cache_read_price_micros: 3000,
      pricing_currency: 'USD', pricing_unit: 'per_1m_tokens', charge_type: 'per_token' } };
  const raw = { channel_id: 900, listing_id: 900, model_name: 'm1', ...base,
    recent_p50_ms: 2000, last_test_response_ms: 2000, recent_success_rate: 9990, sample_count: 1000 };

  const withStation = parseCandidates({ data: [{ channel_id: 900, listings: [raw] }] }, 'm1', cfg, {}).candidates[0];
  // 站点 official 便宜 → 分母小 → 倍率高
  const overrideCfg = normalizeConfig({ ...cfg, officialPriceOverride: { input: 0.9997, output: 3.999, cacheRead: 0.02 } });
  const withOverride = parseCandidates({ data: [{ channel_id: 900, listings: [raw] }] }, 'm1', overrideCfg, {}).candidates[0];

  eq('覆盖后倍率明显低于站点价', withOverride.effRatio < withStation.effRatio, true);
  eq('覆盖不改变商家报价', withOverride.inputMicros, withStation.inputMicros);
  // 覆盖值按商家那条报文的单位解释，币种字段保持原样，priceBasis 才不会判币种冲突
  eq('覆盖后仍能算出有效倍率', Number.isFinite(withOverride.effRatio), true);

  // 非法覆盖（input 非正）应当被忽略，退回站点价
  const badCfg = normalizeConfig({ ...cfg, officialPriceOverride: { input: 0 } });
  eq('非法覆盖被忽略', badCfg.officialPriceOverride, null);
}

{
  // 两家成本完全一样，只有延迟不同：一家 p50 极快但最近实测很慢，另一家相反。
  const mk = (id, p50, testMs, micros) => ({
    channel_id: id, listing_id: id, model_name: 'm1',
    input_price_micros: micros, output_price_micros: micros * 4, cache_read_price_micros: micros * 0.02,
    official_price: { input_price_micros: 1000000, output_price_micros: 4000000, cache_read_price_micros: 20000 },
    recent_p50_ms: p50, last_test_response_ms: testMs, recent_success_rate: 9990, sample_count: 1000,
  });
  const latPayload = { data: [
    // 历史 p50 很快（1000），但最近一次真实响应 9000ms
    { channel_id: 11, listings: [mk(11, 1000, 9000, 300000)] },
    // 历史 p50 一般（3000），最近一次真实响应 1500ms
    { channel_id: 12, listings: [mk(12, 3000, 1500, 300000)] },
  ] };
  const c = parseCandidates(latPayload, 'm1', cfg, {}).candidates;
  const byId = new Map(c.map(x => [x.channelId, x]));
  eq('站点延迟取自 last_test_response_ms', byId.get(11).siteLatencyMs, 9000);
  eq('延迟来源标记为站点实测', byId.get(11).siteLatencyOrigin, 'site-test');
  eq('p50 仍保留在候选里供门槛使用', byId.get(11).p50Ms, 1000);

  const lr = rankCandidates(c, cfg);
  eq('最近实测慢的那家排在后面（p50 没有替它说话）', lr[0].channelId, 12);

  // 站点没测过时退回 p50，并标明来源
  const noTest = mk(13, 2500, 0, 300000);
  const c13 = parseCandidates({ data: [{ channel_id: 13, listings: [noTest] }] }, 'm1', cfg, {}).candidates[0];
  eq('没测过时退回 p50', c13.siteLatencyMs, 2500);
  eq('来源标明是 p50 兜底', c13.siteLatencyOrigin, 'p50-fallback');
}

// ---- 4. 流层采集 usage ----
const usage = { inputTokens: 100, cacheReadTokens: 900, cacheWriteTokens: 0, outputTokens: 50 };
async function* gen(chunks) { for (const c of chunks) yield c; }
async function drain(stream) { const out = []; for await (const c of stream) out.push(c); return out; }

{
  let got = null; let okCount = 0;
  const out = await drain(guardedStream({
    options: { provider: 'a6api-2', model: 'm1' },
    cfg: normalizeConfig({}),
    openStream: () => gen([
      { type: 'usage', usage },
      { type: 'text-delta', text: 'hi' },
      { type: 'usage', usage },
      { type: 'finish', reason: { kind: 'stop' } },
    ]),
    planSwitch: async () => ({ ok: false }),
    onUsage: (u, route) => { got = { u, route }; },
    onSuccess: () => { okCount += 1; },
  }));
  eq('正文照旧透传', out.filter((c) => c.type === 'text-delta').length, 1);
  eq('usage 块原样透传', out.filter((c) => c.type === 'usage').length, 2);
  eq('采集到最后一次 usage', got?.u.cacheReadTokens, 900);
  eq('带出路由', got?.route, { provider: 'a6api-2', model: 'm1' });
  eq('成功回调一次', okCount, 1);
}

{
  let got = null;
  await drain(guardedStream({
    options: { provider: 'a6api-2', model: 'm1' },
    cfg: normalizeConfig({ switchOnEmpty: false }),
    openStream: () => gen([{ type: 'usage', usage }, { type: 'finish', reason: { kind: 'stop' } }]),
    planSwitch: async () => ({ ok: false }),
    onUsage: (u) => { got = u; },
  }));
  eq('空正文不记样本', got, null);
}

{
  let got = null;
  let threw = false;
  const stream = guardedStream({
    options: { provider: 'a6api-2', model: 'm1' },
    cfg: normalizeConfig({ maxSwitchesPerCall: 0 }),
    openStream: () => (async function* bad() { throw new Error('boom'); })(),
    planSwitch: async () => ({ ok: false, reason: 'no' }),
    onUsage: (u) => { got = u; },
  });
  try { await drain(stream); } catch { threw = true; }
  eq('抛错透传出去', threw, true);
  eq('抛错不记样本', got, null);
}

{
  const terminal = { type: 'finish', reason: { kind: 'error', failure: { code: 'SERVER', message: 'upstream failed' } } };
  let switches = 0;
  let failureCode;
  const seen = [];
  try {
    for await (const chunk of guardedStream({
      options: { provider: 'a6api-2', model: 'm1' },
      cfg,
      openStream: () => gen([{ type: 'text-delta', text: 'partial' }, terminal]),
      planSwitch: async () => { switches++; return { ok: false }; },
    })) seen.push(chunk);
  } catch (error) {
    failureCode = error.failure?.code;
  }
  eq('输出后失败不切换', switches, 0);
  eq('输出后保留正文和失败终结块', seen.at(-1), terminal);
  eq('输出后保留宿主可归档的错误码', failureCode, 'SERVER');
}

// 控制面整体挂掉：连续失败超过 controlUnavailableMs 后必须熔断，
// 熔断期间不再打扰控制面，且把请求放行给平台默认路由（不抛 A6_ROUTE_NOT_READY）。
{
  const originalFetch = globalThis.fetch;
  const controlBase = 'https://down.example.com';
  let attempts = 0;
  // 所有控制面请求都超时，模拟「TCP 通、HTTP 全挂」的死站。
  globalThis.fetch = async () => { attempts++; throw new Error('connect timeout'); };
  const state = createStore({ dshHome: tempDir('down'), logEvents: false });
  const switcher = createSwitcher({ cfg: normalizeConfig({
    defaultControlBase: controlBase, accessToken: 't', userId: '7',
    controlUnavailableMs: 1, controlProbeMs: 60000,
    providerOverrides: { 'a6api-7': { tokenId: 10 } },
  }), store: state });
  try {
    const first = await switcher.planSwitch({ provider: 'a6api-7', model: 'm1' , waitForChannels: true });
    eq('死站下切换失败', first.ok, false);

    // 阈值 1ms，等它过期再进一次：这次必须短路成「不可用」。
    await new Promise((r) => setTimeout(r, 12));
    const before = attempts;
    const second = await switcher.planSwitch({ provider: 'a6api-7', model: 'm1' , waitForChannels: true });
    eq('超过阈值后短路返回不可用', second.unavailable, true);
    eq('熔断后不再打控制面', attempts, before);
    eq('熔断原因说清楚了', /降级到平台默认路由/.test(second.reason || ''), true);

    // beginRequest 遇到熔断必须放行，而不是抛 A6_ROUTE_NOT_READY。
    // 注意必须处在智能路由模式下才会走 planSwitch；平台默认模式下入口本来就不碰控制面。
    await switcher.setStrategy('smart').catch(() => {});
    const release = await switcher.beginRequest('a6api-7', 'm1', undefined, { waitForChannels: true }).then(
      (fn) => { fn(); return 'released'; },
      (error) => `threw:${error.code}`,
    );
    eq('熔断时入口放行，不抛错', release, 'released');

    // 关闭熔断（0）时一切照旧：只会冷却，不会短路。
    switcher.updateConfig(normalizeConfig({
      defaultControlBase: controlBase, accessToken: 't', userId: '7',
      controlUnavailableMs: 0, controlProbeMs: 60000,
      providerOverrides: { 'a6api-7': { tokenId: 10 } },
    }));
    const third = await switcher.planSwitch({ provider: 'a6api-7', model: 'm1' , waitForChannels: true });
    eq('熔断关闭时不短路（只冷却）', third.unavailable, undefined);
  } finally {
    switcher.dispose();
    state.dispose();
    globalThis.fetch = originalFetch;
  }
}

// 控制面请求通过本地模拟覆盖凭据范围、取消及跨 provider 的共享 pin。
{
  const originalFetch = globalThis.fetch;
  const controlBase = 'https://example.com';
  const requests = [];
  const reply = (value) => new Response(JSON.stringify(value));
  let announcePin;
  let releasePin;
  const pinStarted = new Promise((resolve) => { announcePin = resolve; });
  const allowPin = new Promise((resolve) => { releasePin = resolve; });
  let pinned;
  globalThis.fetch = async (url, options) => {
    requests.push([url.pathname, options]);
    switch (url.pathname) {
      case '/api/marketplace/pins': return reply({ data: pinned ? [pinned] : [] });
      case '/api/marketplace/channels': return reply(payload);
      case '/api/marketplace/pin':
        announcePin();
        await allowPin;
        pinned = { ...JSON.parse(options.body), status: 'active', route_effective: true };
        return reply({ success: true });
      default: throw new Error('Unexpected test request');
    }
  };
  const state = createStore({ dshHome: tempDir('control'), logEvents: false });
  const switcher = createSwitcher({ cfg: normalizeConfig({
    defaultControlBase: controlBase, accessToken: 'test-token', userId: '5', cacheWeight: 0,
    providerOverrides: { 'a6api-1': { tokenId: 10 }, 'a6api-2': { tokenId: 10 } },
  }), store: state });
  const releaseRequest = await switcher.beginRequest('a6api-1', 'm1', undefined, { waitForChannels: true });
  let first;
  try {
    eq('凭据不可移到其他源', switcher.resolveSiteByBase('https://elsewhere.example').ok, false);
    const code = await a6Request({ controlBase, path: 'https://elsewhere.example/api', accessToken: 'test-token' })
      .catch((error) => error.code);
    eq('拒绝跨源请求', code, 'A6_CONTROL_ORIGIN');
    eq('拒绝时没有发请求', requests.length, 2);
    const cancelled = new AbortController();
    cancelled.abort();
    eq('已取消的切换不访问控制面', (await switcher.planSwitch({ provider: 'a6api-1', model: 'm1', signal: cancelled.signal , waitForChannels: true })).ok, false);
    eq('取消时没有发请求', requests.length, 2);
    first = switcher.planSwitch({ provider: 'a6api-1', model: 'm1' , waitForChannels: true });
    const reachedPin = await Promise.race([pinStarted.then(() => true), first.then(() => false)]);
    eq('正常切换到达 pin 请求', reachedPin, true);
    const concurrent = reachedPin ? switcher.planSwitch({ provider: 'a6api-2', model: 'm1' , waitForChannels: true }) : null;
    releasePin();
    const chosen = await first;
    if (concurrent) {
      const joined = await concurrent;
      eq('同令牌并发恢复得到已确认路由', joined.ok, true);
      eq('同令牌并发只修改一次', requests.filter(([url]) => url.endsWith('/pin')).length, 1);
    }
    eq('商家固定成功', chosen.mode, 'pin');
    eq('同一令牌共享实际商家', switcher.activeChannel('a6api-2', 'm1')?.channelId, chosen.channelId);
    eq('控制面拒绝重定向', requests.every(([, opts]) => opts.redirect === 'error'), true);
    eq('默认固定严格商家', JSON.parse(requests.find(([url]) => url === '/api/marketplace/pin')?.[1].body || '{}').fallback_to_smart_routing, false);
  } finally {
    releasePin();
    await first;
    await releaseRequest();
    switcher.dispose();
    state.dispose();
    globalThis.fetch = originalFetch;
  }
}

// 站点上已有「不是本插件写的」固定商家：默认保留，开启接管后才动手。
{
  const originalFetch = globalThis.fetch;
  const controlBase = 'https://example.com';
  const paced = { data: [1, 2, 3, 4].map((id, i) => ({ channel_id: id, listings: [mkListing(id, 300000, 1000 + i * 100 + (i > 1 ? 300 : 0))] })) };

  /**
   * @param {{takeOver:boolean, externalChannel:number, breakPin?:boolean}} options
   */
  async function scenario({ takeOver, externalChannel, breakPin = false }) {
    const calls = [];
    let pinned = { token_id: 10, model_name: 'm1', channel_id: externalChannel,
      channel_name: `Channel ID ${externalChannel}`, status: 'active', route_effective: true, fallback_to_smart_routing: false };
    globalThis.fetch = async (url, options) => {
      const body = options?.body ? JSON.parse(options.body) : null;
      calls.push([url.pathname, body]);
      const reply = (value) => new Response(JSON.stringify(value));
      if (url.pathname.endsWith('/pins')) return reply({ data: pinned ? [{ ...pinned, token_name: 'a6-1' }] : [] });
      if (url.pathname.endsWith('/channels')) return reply(paced);
      if (url.pathname.endsWith('/unpin')) { pinned = undefined; return reply({ success: true }); }
      if (url.pathname.endsWith('/pin')) {
        // breakPin：第一次改绑「假成功」——接口回 200，但站点实际没变，回查会识破。
        const attempts = calls.filter(([p]) => p.endsWith('/pin')).length;
        if (breakPin && attempts === 1) return reply({ success: true });
        pinned = { ...body, status: 'active', route_effective: true };
        return reply({ success: true });
      }
      throw new Error('Unexpected test request');
    };
    const state = createStore({ dshHome: tempDir(`takeover-${takeOver}-${externalChannel}-${breakPin}`), logEvents: false });
    const switcher = createSwitcher({ cfg: normalizeConfig({
      defaultControlBase: controlBase, accessToken: 'test-token', userId: '5', cacheWeight: 0, takeOverExistingPin: takeOver,
      providerOverrides: { 'a6api-1': { tokenId: 10 } },
    }), store: state });
    try {
      const result = await switcher.planSwitch({ provider: 'a6api-1', model: 'm1', initial: true , waitForChannels: true });
      const kinds = calls.map(([path]) => path.split('/').pop());
      return { result, calls, kinds, state, owned: state.ownedPins(), active: switcher.activeChannel('a6api-1', 'm1'),
        finalPinned: pinned };
    } finally {
      switcher.dispose();
      globalThis.fetch = originalFetch;
    }
  }

  const preserved = await scenario({ takeOver: false, externalChannel: 3 });
  eq('默认不接管：保留外部固定', preserved.result.ok, false);
  eq('默认不接管：给出保留原因', preserved.result.reason, '保留平台已有的固定商家');
  eq('默认不接管：不解除也不改绑', preserved.kinds.includes('unpin') || preserved.kinds.includes('pin'), false);
  eq('默认不接管：不留归属记录', Object.keys(preserved.owned).length, 0);

  const taken = await scenario({ takeOver: true, externalChannel: 3 });
  eq('开启接管：解除外部固定后改绑', taken.result.ok && taken.result.mode === 'pin', true);
  eq('开启接管：先 unpin 再 pin', taken.kinds.filter(k => k === 'unpin' || k === 'pin'), ['unpin', 'pin']);
  eq('开启接管：解除的是外部那一家', taken.calls.find(([, b]) => b?.channel_id === 3 && b?.model_name)?.[1].channel_id, 3);
  eq('开启接管：改绑到候选商家', taken.result.channelId, 1);
  eq('开启接管：写入归属记录以便空闲归还', Object.keys(taken.owned).length, 1);
  eq('开启接管：当前活跃商家随之更新', taken.active?.channelId, 1);

  const adopted = await scenario({ takeOver: true, externalChannel: 1 });
  eq('外部那家就是最优：直接沿用', adopted.result.ok && adopted.result.mode === 'adopt', true);
  eq('外部那家就是最优：不动站点设置', adopted.kinds.includes('unpin') || adopted.kinds.includes('pin'), false);
  eq('外部那家就是最优：不写归属记录', Object.keys(adopted.owned).length, 0);
  eq('外部那家就是最优：请求照常发往该商家', adopted.active?.channelId, 1);

  const rolledBack = await scenario({ takeOver: true, externalChannel: 3, breakPin: true });
  eq('改绑回查失败：本次仍算失败', rolledBack.result.ok, false);
  eq('改绑回查失败：把原固定还原回去', rolledBack.finalPinned?.channel_id, 3);
  eq('改绑回查失败：还原时保留对方原有的兜底设置', rolledBack.finalPinned?.fallback_to_smart_routing, false);
  eq('改绑回查失败：不留归属记录冒充已接管', Object.keys(rolledBack.owned).length, 0);
}

// 通过真实插件入口验收首包前门控、共享预热和热路径额外耗时。
{
  const originalFetch = globalThis.fetch;
  const originalHome = process.env.DSH_HOME;
  process.env.DSH_HOME = tempDir('routing');
  const listeners = new Map();
  const disposers = [];
  const endpoints = new Map();
  const ctx = {
    on: (name, handler) => listeners.set(name, handler),
    effect: setup => disposers.push(setup()),
    inject: (services, cb) => {
      if (!services.includes('webServer')) return;
      cb({ effect: setup => setup(), webServer: { port: 8181, protocol: 'http:', register: endpoint => endpoints.set(endpoint.path, endpoint.handler) } });
    },
  };
  async function invoke(path, method, body, extra = {}) {
    const { Readable } = await import('node:stream');
    const request = Readable.from([Buffer.from(JSON.stringify(body || {}))]);
    Object.assign(request, { method, headers: { host: 'localhost:8181', 'x-a6-failover': 'settings', 'content-type': 'application/json', ...extra }, socket: { localPort: 8181, remoteAddress: '127.0.0.1' } });
    let status, result;
    await endpoints.get(`/a6-failover/${path}`)(request, { writeHead: code => { status = code; }, end: text => { result = JSON.parse(text); } });
    return { status, result };
  }
  const okStream = () => gen([{ type: 'text-delta', text: 'ok' }, { type: 'finish', reason: { kind: 'stop' } }]);
  let dropNext = false;
  let hangNext = false;
  let closedSlowAttempt = false;
  let opens = 0;
  let openedAt;
  let quoteCalls = 0;
  let controlCalls = 0;
  let writes = 0;
  let pinned;
  const cfg = { defaultControlBase: 'https://example.com', accessToken: 'test-token', userId: '5', cacheWeight: 0,
    providerOverrides: { 'a6api-2': { tokenId: 10 } }, prewarmRoutes: [{ provider: 'a6api-2', model: 'm1' }],
    // 冷请求默认放行；等待预算收短，免得测试等 6 秒
    firstResponseTimeoutMs: 300, firstResponseBudgetMs: 4000 };
  const options = { provider: 'a6api-2', model: 'm1' };
  const runtime = { stream(opts) { return listeners.get('llm/stream').call(this, opts, () => { opens++;
    openedAt = performance.now();
    if (hangNext) {
      hangNext = false;
      return (async function* () {
        yield { type: 'heartbeat' };
        await new Promise(resolve => opts.signal.addEventListener('abort', () => { closedSlowAttempt = true; resolve(); }, { once: true }));
      })();
    }
    if (!dropNext) return okStream();
    dropNext = false;
    return gen([{ type: 'finish', reason: { kind: 'error', failure: { code: 'SERVER', message: 'failed' } } }]);
  }); } };
  let releaseQuote;
  const quoteDelay = new Promise((resolve) => { releaseQuote = resolve; });
  globalThis.fetch = async (url, request) => {
    controlCalls++;
    let body;
    if (url.pathname.endsWith('/pins')) body = { data: pinned ? [pinned] : [] };
    if (url.pathname.endsWith('/unpin')) { pinned = undefined; body = { success: true }; }
    if (url.pathname.endsWith('/channels')) {
      quoteCalls++;
      await quoteDelay;
      body = payload;
    }
    if (url.pathname.endsWith('/pin')) {
      writes++;
      pinned = { ...JSON.parse(request.body), status: 'active', route_effective: true };
      body = { success: true };
    }
    return new Response(JSON.stringify(body));
  };
  try {
    apply(ctx, cfg);
    await new Promise((resolve) => setImmediate(resolve));
    eq('启动策略为平台默认', (await invoke('status', 'GET')).result.strategy.manual, false);
    eq('拒绝跨站变更策略', (await invoke('strategy', 'POST', { mode: 'smart' }, { origin: 'https://other.example' })).status, 403);
    eq('拒绝未知策略', (await invoke('strategy', 'POST', { mode: 'other' })).status, 400);
    eq('干净启动不查询报价', quoteCalls, 0);
    const coldResults = await Promise.all([drain(runtime.stream(options)), drain(runtime.stream(options))]);
    eq('平台默认直接返回两条回复', coldResults.map(r => r[0]?.text), ['ok', 'ok']);
    eq('首次并发请求直接开流', opens, 2);
    eq('正常请求不选商家或写固定路由', [controlCalls, writes], [2, 0]);
    releaseQuote();
    const baseline = controlCalls;
    const durations = [];
    for (let i = 0; i < 1000; i++) {
      const before = performance.now();
      await drain(runtime.stream(options));
      durations.push(openedAt - before);
    }
    for (let i = 0; i < 30; i++) await drain(runtime.stream(options));
    const p95 = durations.sort((a, b) => a - b)[949];
    console.log(`热路径 P95 ${p95.toFixed(3)} ms，连续请求额外控制面调用 ${controlCalls - baseline} 次`);
    eq('热路径 P95 ≤ 5 毫秒', p95 <= 5, true);
    eq('30 次完整热请求无控制面调用', controlCalls - baseline, 0);
    dropNext = true;
    const out = await drain(runtime.stream(options));
    eq('原路快速恢复并返回正文', out.find(chunk => chunk.type === 'text-delta')?.text, 'ok');
    // 方案 B 之后，切换只读缓存：先让插件真正拉一次商家表，后面的换商家才有候选可用。
    // 这里直接走设置页那条约等于「重新拉取」的路径（waitForChannels），把缓存预热好。
    {
      const base = ctx?.switcher?.resolveSite?.(options.provider);
      if (base?.ok) {
        await ctx.switcher.loadCandidates(base, options.model, new Set(), { force: true }).catch(() => {});
      }
    }
    hangNext = true;
    const held = setTimeout(() => {}, 5000);
    try {
      const recovered = await drain(runtime.stream(options));
      eq('首块超时取消原始适配器并换商家继续', [closedSlowAttempt, recovered.find(c => c.type === 'text-delta')?.text, writes], [true, 'ok', 1]);
    } finally { clearTimeout(held); }
    const manual = await invoke('strategy', 'POST', { mode: 'smart' });
    eq('手动开启智能路由', [manual.status, manual.result.strategy?.manual], [200, true]);
    eq('手动策略在无活跃会话时保持', (await invoke('status', 'GET')).result.strategy.manual, true);
    const restored = await invoke('strategy', 'POST', { mode: 'platform' });
    eq('手动恢复平台路由', [restored.status, restored.result.strategy?.manual], [200, false]);
  } finally {
    releaseQuote();
    disposers.forEach((dispose) => dispose());
    if (originalHome === undefined) delete process.env.DSH_HOME;
    else process.env.DSH_HOME = originalHome;
    globalThis.fetch = originalFetch;
  }
}

for (const dir of tempDirs) fs.rmSync(dir, { recursive: true, force: true });
console.log(`\n${pass} 项通过，${fails.length} 项失败`);
for (const f of fails) console.log('  ✗ ' + f);
process.exit(fails.length ? 1 : 0);
