import assert from 'node:assert/strict';
import test from 'node:test';
import { performance } from 'node:perf_hooks';
import { setTimeout as sleep } from 'node:timers/promises';
import { guardedStream } from '../lib/stream-guard.js';

const stop = { type: 'finish', reason: { kind: 'stop' } };
const text = { type: 'text-delta', text: 'ok' };
const never = () => new Promise(() => {});
const chunks = (...items) => (async function* () { yield* items; })();
const stuck = () => ({ [Symbol.asyncIterator]() { return { next: never, return: never }; } });
function fixture(streams, overrides = {}, extra = {}) {
  const attempts = [], failures = [], switches = [], events = [], seen = [];
  const iter = guardedStream({
    cfg: { firstResponseTimeoutMs: 200, firstResponseBudgetMs: 500, streamStallTimeoutMs: 100,
      sameRouteRetries: 1, maxSwitchesPerCall: 1, switchOnEmpty: true, switchOnThrown: true,
      switchOnTruncated: true, switchOnFinishError: true, ...overrides },
    options: { provider: 'p', model: 'm' },
    openStream(opts, meta) { attempts.push({ opts, meta }); return streams.shift()(opts); },
    planSwitch(plan) { switches.push(plan); return { ok: true, mode: 'pin', channelId: 1 }; },
    onFailure(failure) { failures.push(failure); }, onEvent(event) { events.push(event); }, ...extra,
  });
  async function run() { for await (const chunk of iter) seen.push(chunk); }
  return { attempts, failures, switches, events, seen, iter, run };
}

test('quick failures retry same route then switch, each new merchant gets a bounded retry', async () => {
  const empty = () => chunks(stop);
  const f = fixture([empty, empty, empty, () => chunks(text, stop)]);
  await f.run();
  assert.equal(f.switches.length, 1);
  assert.deepEqual(f.attempts.map(a => a.meta.first), [true, false, false, false]);
  assert.equal(new Set(f.attempts.map(a => a.opts.signal)).size, 4);
  assert.deepEqual(f.seen, [text, stop]);
  assert.equal(new Set(f.failures.map(x => x.evidence.requestId)).size, 1);
});

test('same-route retries share the initial clock and heartbeat does not reset it', async (t) => {
  let now = 1000;
  t.mock.method(performance, 'now', () => now);
  const delayedFailure = () => (async function* () { now += 40; throw Error('quick failure'); })();
  const heartbeat = () => (async function* () { for (;;) { now += 5; yield { type: 'heartbeat' }; } })();
  const f = fixture([delayedFailure, heartbeat, () => chunks(text, stop)], { firstResponseTimeoutMs: 75 });
  await f.run();
  assert.equal(f.failures[1].code, 'A6_FIRST_RESPONSE_TIMEOUT');
  assert.equal(f.failures[1].latencyMs, 35);
  assert.equal(f.failures[1].evidence.elapsedMs, 75);
  assert.equal(f.switches.length, 1);
  assert.ok(f.attempts[1].opts.signal.aborted);
  assert.deepEqual(f.seen, [text, stop]);
});

test('late first output after a blocked event loop misses its deadline even before the timer fires', async (t) => {
  let now = 1000;
  t.mock.method(performance, 'now', () => now);
  const delayed = () => (async function* () { now += 65; yield text; })();
  const f = fixture([delayed], { firstResponseTimeoutMs: 50, firstResponseBudgetMs: 60 });
  await assert.rejects(f.run, { code: 'A6_FIRST_RESPONSE_BUDGET' });
  assert.equal(f.seen.length, 0);
  assert.equal(f.switches.length, 0);
});

test('late upstream next and cleanup rejections remain observed after a deadline', async () => {
  let rejectRead;
  const late = () => ({ [Symbol.asyncIterator]() { return {
    next: () => new Promise((_, reject) => { rejectRead = reject; }),
    return: () => Promise.reject(new Error('late cleanup')),
  }; } });
  const f = fixture([late], { firstResponseTimeoutMs: 10, firstResponseBudgetMs: 10 });
  await assert.rejects(f.run, { code: 'A6_FIRST_RESPONSE_BUDGET' });
  rejectRead(new Error('late read'));
  await sleep(0);
});

test('control plane cannot consume more than remaining total budget', async () => {
  let switchSignal;
  const f = fixture([stuck], { firstResponseTimeoutMs: 20, firstResponseBudgetMs: 65 }, {
    planSwitch: ({ signal }) => { switchSignal = signal; return never(); },
  });
  await assert.rejects(f.run, { code: 'A6_FIRST_RESPONSE_BUDGET' });
  assert.ok(switchSignal.aborted);
  assert.equal(f.attempts.length, 1);
});

test('replacement merchant shares remaining absolute first response budget', async () => {
  const f = fixture([stuck, stuck], { firstResponseTimeoutMs: 20, firstResponseBudgetMs: 55 });
  await assert.rejects(f.run, { code: 'A6_FIRST_RESPONSE_BUDGET' });
  assert.equal(f.switches.length, 1);
  assert.ok(f.attempts.every(a => a.opts.signal.aborted));
});

for (const first of [text, { type: 'reasoning-delta', text: 'hmm' }, { type: 'block-start', blockType: 'tool-call' }]) {
  test(`post-output stall after ${first.type} preserves output and prevents replay`, async () => {
    const f = fixture([() => (async function* () { yield first; for (;;) { await sleep(5); yield { type: 'ping' }; } })()], { streamStallTimeoutMs: 20 });
    await assert.rejects(f.run, { code: 'A6_STREAM_STALL' });
    assert.deepEqual(f.seen[0], first);
    assert.equal(f.failures[0].postContent, true);
    assert.equal(f.failures[0].recoverable, true);
    assert.equal(f.switches.length, 0);
  });
}

test('consumer backpressure does not trigger stall', async () => {
  const f = fixture([() => chunks(text, stop)], { streamStallTimeoutMs: 5 });
  assert.deepEqual((await f.iter.next()).value, text);
  await sleep(20);
  assert.deepEqual((await f.iter.next()).value, stop);
  assert.ok((await f.iter.next()).done);
});

for (const [value, rule] of [[null, 'chunk-shape'], [{ type: 'text-delta', text: { secret: 'private' } }, 'text-delta-type'],
  [{ type: 'reasoning-delta' }, 'reasoning-delta-type'], [{ type: 'finish', reason: {} }, 'finish-reason-shape']]) {
  test(`local evidence only contains structural facts for ${rule}`, async () => {
    const f = fixture([() => chunks(value), () => chunks(text, stop)]);
    await f.run();
    const { evidence } = f.failures[0];
    assert.equal(evidence.rule, rule);
    assert.match(evidence.requestId, /^[0-9a-f-]{36}$/);
    assert.deepEqual(Object.keys(evidence).sort(), ['attempt', 'elapsedMs', 'firstResponseMs', 'requestId', 'rule']);
    assert.ok(!JSON.stringify([f.events, f.failures]).includes('private'));
  });
}

test('unknown extension or natural language formatting does not raise structural anomaly', async () => {
  const msg = { type: 'text-delta', text: '```unfinished' };
  const f = fixture([() => chunks({ type: 'extension' }, msg, stop)]);
  await f.run();
  assert.equal(f.failures.length, 0);
  assert.deepEqual(f.seen.at(-2), msg);
});

test('abort propagates upstream and releases an uncooperative pending read immediately', async () => {
  const caller = new AbortController();
  const f = fixture([stuck], {}, { options: { signal: caller.signal } });
  const running = f.run();
  caller.abort(new Error('user cancelled'));
  await assert.rejects(running, /user cancelled/);
  assert.ok(f.attempts[0].opts.signal.aborted);
  assert.equal(f.failures.length, 0);
  assert.equal(f.switches.length, 0);
});


test('known access-block response ID is an error, never success or replay after output', async () => {
  let successes = 0;
  const finish = { ...stop, replayState: { response: { responseId: 'resp_freeipblk00000000000000000' } } };
  const f = fixture([() => chunks(text, finish)], {}, { onSuccess() { successes++; } });
  await assert.rejects(f.run());
  assert.equal(successes, 0);
  assert.equal(f.switches.length, 0);
  assert.equal(f.failures[0].code, 'A6_UPSTREAM_ACCESS_RESTRICTED');
  assert.equal(f.seen.at(-1).reason.kind, 'error');
});

test('ordinary response IDs with normal stop stay successful', async () => {
  const f = fixture([() => chunks(text, { ...stop, replayState: { response: { responseId: 'resp_normal' } } })]);
  await f.run();
  assert.equal(f.failures.length, 0);
});
