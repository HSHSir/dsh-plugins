import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createStore } from '../lib/state.js';
import { normalizeConfig } from '../lib/config.js';
import { createSwitcher } from '../lib/switcher.js';

const delay = ms => new Promise(resolve => setTimeout(resolve, ms));
const model = 'm';
const a = 'a6api-1';
const b = 'a6api-2';
const cfg = normalizeConfig({ defaultControlBase: 'https://example.invalid', accessToken: 'mock', userId: '5',
  cacheWeight: 0, controlTimeoutMs: 3000, providerOverrides: { [a]: { tokenId: 10 }, [b]: { tokenId: 10 } } });
const rows = Array.from({ length: 205 }, (_, i) => ({ channel_id: i + 1, model_name: model,
  listing_id: i + 1, input_price_micros: 300000 - i * 100, output_price_micros: 1200000,
  cache_read_price_micros: 6000, recent_p50_ms: 100 + i, recent_success_rate: 9990, sample_count: 1000,
  official_price: { input_price_micros: 1000000, output_price_micros: 4000000, cache_read_price_micros: 20000 } }));

function setup(t) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'a6-router-'));
  const server = { pinned: null, unpinWorks: true, pinEffective: true, writes: 0, deletes: 0, calls: 0 };
  const previous = globalThis.fetch;
  globalThis.fetch = async (url, req) => {
    assert.equal(url.host, 'example.invalid');
    server.calls++;
    const reply = data => new Response(JSON.stringify({ success: true, data }));
    if (url.pathname.endsWith('/pins')) return reply(server.pinned ? [server.pinned] : []);
    if (url.pathname.endsWith('/channels')) return reply([{ listings: [...rows, rows[0]] }]);
    if (url.pathname.endsWith('/subscriptions')) return reply([]);
    if (url.pathname.endsWith('/unpin')) {
      server.deletes++;
      if (server.unpinWorks) server.pinned = null;
      return reply({});
    }
    if (url.pathname.endsWith('/pin')) {
      server.writes++;
      await delay(2);
      server.pinned = { ...JSON.parse(req.body), status: 'active', route_effective: server.pinEffective };
      return reply({});
    }
    throw new Error(url.pathname);
  };
  function boot() {
    const store = createStore({ dshHome: home, logEvents: false });
    const sw = createSwitcher({ cfg, store });
    t.after(() => { sw.dispose(); store.dispose(); });
    return { store, sw };
  }
  t.after(() => { globalThis.fetch = previous; });
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  return { server, boot };
}

test('healthy platform, shared alias generation, idle grace, and strict restoration', async t => {
  const { server, boot } = setup(t);
  const { sw } = boot();
  await sw.startRecovery();
  const endA = await sw.beginRequest(a, model, undefined, { waitForChannels: true });
  const endB = await sw.beginRequest(b, model, undefined, { waitForChannels: true });
  assert.equal(server.calls, 2); // 同令牌两个别名只执行一轮启动清理并回查。
  const initial = sw.generation(b, model);
  const [x, y] = await Promise.all([a, b].map(provider => sw.planSwitch({ provider, model, failedGeneration: initial, waitForChannels: true })));
  assert.ok(x.ok && y.ok);
  assert.equal(server.writes, 1);
  assert.equal(x.channelId, y.channelId);
  assert.equal((await sw.planSwitch({ provider: b, model, failedGeneration: initial })).mode, 'deduped');
  assert.equal(server.writes, 1);
  await endA();
  await endB();
  const endC = await sw.beginRequest(a, model, undefined, { waitForChannels: true });
  await delay(1050);
  assert.equal(server.deletes, 0);
  await endC();
  await delay(1200);
  assert.equal(server.deletes, 1);
  assert.equal(sw.strategyStatus().phase, 'platform');
});

test('all eligible unique channels survive cache then fastest half calculated after exclusions', async t => {
  const { boot } = setup(t);
  const { sw } = boot();
  const site = sw.resolveSite(a);
  const all = await sw.loadCandidates(site, model);
  assert.equal(all.length, 103);
  assert.ok(all.every(r => r.channelId <= 103));
  const remaining = await sw.loadCandidates(site, model, new Set(Array.from({ length: 200 }, (_, i) => i + 1)));
  assert.deepEqual(remaining.map(row => row.channelId).sort(), [201, 202, 203]);
});

test('manual smart survives idle and config save; restart clears managed bindings', async t => {
  const { server, boot } = setup(t);
  const { sw, store } = boot();
  await sw.setStrategy('smart', [{ provider: a, model }]);
  assert.equal(server.writes, 0); // 手动开关不预先固定商家。
  sw.updateConfig({ ...cfg });
  const end = await sw.beginRequest(b, model, undefined, { waitForChannels: true });
  await end();
  assert.equal(sw.strategyStatus().manual, true);
  assert.equal(server.writes, 1);
  assert.equal(Object.keys(store.ownedPins()).length, 1);
  sw.dispose(); store.dispose();
  const { sw: resumed } = boot();
  await resumed.startRecovery();
  assert.equal(resumed.strategyStatus().manual, false);
  assert.equal(server.deletes, 1);
  assert.equal(server.pinned, null);
});

test('pending unpin fences affected request; restart also clears a changed managed-token binding', async t => {
  const { server, boot } = setup(t);
  const { sw, store } = boot();
  await sw.setStrategy('smart', [{ provider: a, model }]);
  const end = await sw.beginRequest(a, model, undefined, { waitForChannels: true });
  await end();
  server.unpinWorks = false;
  await assert.rejects(sw.setStrategy('platform'));
  assert.equal(sw.strategyStatus().phase, 'restoring');
  await assert.rejects(sw.beginRequest(b, model, undefined, { waitForChannels: true }));
  server.pinned.channel_id = 999;
  sw.dispose(); store.dispose();
  const { sw: resumed } = boot();
  const prior = server.deletes;
  server.unpinWorks = true;
  await resumed.startRecovery();
  assert.equal(server.deletes, prior + 1);
  assert.equal(server.pinned, null);
  assert.equal(resumed.strategyStatus().pendingRestore, 0);
});

test('startup clears every managed-token model including paused pins and preserves other tokens', async t => {
  const { boot } = setup(t);
  let pins = [
    { token_id: 10, model_name: 'unknown-model', channel_id: 90, status: 'paused' },
    { token_id: 10, model_name: 'another-model', channel_id: 91 },
    { token_id: 20, model_name: 'unknown-model', channel_id: 92 },
  ];
  let deletes = 0;
  globalThis.fetch = async (url, req) => {
    if (url.pathname.endsWith('/unpin')) {
      const body = JSON.parse(req.body);
      assert.equal(body.token_id, 10);
      pins = pins.filter(p => !(p.token_id === body.token_id && p.model_name === body.model_name));
      deletes++;
    } else assert.ok(url.pathname.endsWith('/pins'));
    return new Response(JSON.stringify({ success: true, data: pins }));
  };
  const { sw } = boot();
  await sw.startRecovery([a, b]);
  assert.equal(deletes, 2);
  assert.deepEqual(pins.map(p => p.token_id), [20]);
  await sw.setStrategy('smart');
  assert.equal(deletes, 2);
});
