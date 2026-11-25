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
  cacheBenefitOf, measuredHitOf, parseCandidates, rankCandidates,
} = await load('lib/selector.js');
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
const mkListing = (id, inputMicros, p50) => ({
  channel_id: id, listing_id: id, model_name: 'm1',
  input_price_micros: inputMicros, output_price_micros: inputMicros * 4,
  cache_read_price_micros: inputMicros * 0.02,
  official_price: { input_price_micros: 1000000, output_price_micros: 4000000, cache_read_price_micros: 20000 },
  recent_p50_ms: p50, recent_success_rate: 9990, sample_count: 1000,
});
const payload = { data: [
  { channel_id: 1, listings: [mkListing(1, 300000, 2000)] },
  { channel_id: 2, listings: [mkListing(2, 290000, 2000)] },
] };
const measured = new Map([[1, { rate: 0.95, samples: 8 }], [2, { rate: 0.05, samples: 8 }]]);
const ranked = rankCandidates(parseCandidates(payload, 'm1', cfg, { measuredRates: measured }).candidates, cfg);
eq('两家都进候选', ranked.length, 2);
eq('命中率高者排第一（尽管贵 3.4%）', ranked[0].channelId, 1);
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
  const releaseRequest = await switcher.beginRequest('a6api-1', 'm1');
  let first;
  try {
    eq('凭据不可移到其他源', switcher.resolveSiteByBase('https://elsewhere.example').ok, false);
    const code = await a6Request({ controlBase, path: 'https://elsewhere.example/api', accessToken: 'test-token' })
      .catch((error) => error.code);
    eq('拒绝跨源请求', code, 'A6_CONTROL_ORIGIN');
    eq('拒绝时没有发请求', requests.length, 2);
    const cancelled = new AbortController();
    cancelled.abort();
    eq('已取消的切换不访问控制面', (await switcher.planSwitch({ provider: 'a6api-1', model: 'm1', signal: cancelled.signal })).ok, false);
    eq('取消时没有发请求', requests.length, 2);
    first = switcher.planSwitch({ provider: 'a6api-1', model: 'm1' });
    const reachedPin = await Promise.race([pinStarted.then(() => true), first.then(() => false)]);
    eq('正常切换到达 pin 请求', reachedPin, true);
    const concurrent = reachedPin ? switcher.planSwitch({ provider: 'a6api-2', model: 'm1' }) : null;
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
