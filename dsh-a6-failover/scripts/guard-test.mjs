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

test('late first output misses its own attempt deadline even before the timer fires', async (t) => {
  let now = 1000;
  t.mock.method(performance, 'now', () => now);
  const delayed = () => (async function* () { now += 65; yield text; })();
  const f = fixture([delayed, () => chunks(text, stop)], { firstResponseTimeoutMs: 50, firstResponseBudgetMs: 60 });
  await f.run();
  assert.deepEqual(f.seen, [text, stop]);
  assert.equal(f.switches.length, 1);
  assert.equal(f.failures[0].code, 'A6_FIRST_RESPONSE_TIMEOUT');
});

test('late upstream next and cleanup rejections remain observed after a deadline', async () => {
  let rejectRead;
  const late = () => ({ [Symbol.asyncIterator]() { return {
    next: () => new Promise((_, reject) => { rejectRead = reject; }),
    return: () => Promise.reject(new Error('late cleanup')),
  }; } });
  const f = fixture([late], { firstResponseTimeoutMs: 10, firstResponseBudgetMs: 10, maxSwitchesPerCall: 0 });
  await assert.rejects(f.run, { code: 'A6_FIRST_RESPONSE_TIMEOUT' });
  rejectRead(new Error('late read'));
  await sleep(0);
});

test('control plane has its own timeout and cannot eat an attempt budget', async () => {
  let switchSignal;
  const f = fixture([stuck], { firstResponseTimeoutMs: 20, firstResponseBudgetMs: 65, controlTimeoutMs: 40 }, {
    planSwitch: ({ signal }) => { switchSignal = signal; return never(); },
  });
  const started = Date.now();
  await assert.rejects(f.run, { code: 'A6_FIRST_RESPONSE_TIMEOUT' });
  assert.ok(switchSignal.aborted);
  assert.equal(f.attempts.length, 1);
  assert.ok(Date.now() - started < 1000, 'control wait must be bounded by controlTimeoutMs');
});

test('each replacement merchant gets its own full first response budget', async () => {
  const f = fixture([stuck, stuck], { firstResponseTimeoutMs: 20, firstResponseBudgetMs: 55 });
  await assert.rejects(f.run, { code: 'A6_FIRST_RESPONSE_BUDGET' });
  assert.equal(f.switches.length, 1);
  // 第一家用的是 firstResponseTimeoutMs，替换商家拿到的是完整的 firstResponseBudgetMs
  assert.equal(f.failures[0].code, 'A6_FIRST_RESPONSE_TIMEOUT');
  assert.equal(f.failures[1].code, 'A6_FIRST_RESPONSE_BUDGET');
  assert.ok(f.attempts.every(a => a.opts.signal.aborted));
});

const stallFirst = () => (async function* () { yield text; for (;;) { await sleep(5); yield { type: 'ping' }; } })();
const stallWithTool = () => (async function* () { yield { type: 'block-start', blockType: 'tool-call' }; for (;;) { await sleep(5); yield { type: 'ping' }; } })();

test('post-output text stall switches merchant and replays on the replacement to continue the task', async () => {
  // 纯文本输出后卡住：没有工具副作用，换商重放是安全的，当前这轮任务应继续。
  const f = fixture([stallFirst, () => chunks(text, stop)], { streamStallTimeoutMs: 20 });
  await f.run();
  assert.equal(f.switches.length, 1);
  assert.equal(f.failures[0].code, 'A6_STREAM_STALL');
  assert.equal(f.failures[0].postContent, true);
  assert.deepEqual(f.seen.at(-1), stop);
});

test('post-output stall with a tool call does not replay (avoids repeated side effects)', async () => {
  // 已经发出工具调用块后卡住：重放会重复执行副作用，保持不可重试。
  const f = fixture([stallWithTool, () => chunks(text, stop)], { streamStallTimeoutMs: 20 });
  await assert.rejects(f.run, { code: 'A6_STREAM_STALL' });
  assert.equal(f.switches.length, 0);
  assert.equal(f.failures[0].postContent, true);
  assert.equal(f.failures[0].recoverable, true);
});

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


test('access-block placeholder is hidden, reported as error, and switched to a working merchant', async () => {
  let successes = 0;
  const blockText = { type: 'text-delta', text: '当前网络的请求存在异常，已被临时限制访问，请稍后再试。' };
  const placeholder = { ...stop, replayState: { response: { responseId: 'resp_freeipblk00000000000000000' } } };
  const good = { type: 'text-delta', text: '这是换商后的正常回答' };
  const f = fixture([
    () => chunks(blockText, placeholder),
    () => chunks(good, { ...stop, replayState: { response: { responseId: 'resp_normal' } } }),
  ], {}, { onSuccess() { successes++; } });
  await f.run();
  // 占位错误触发换商，而不是原路重试
  assert.equal(f.switches.length, 1);
  assert.equal(f.failures[0].code, 'A6_UPSTREAM_ACCESS_RESTRICTED');
  // 错误文案从未交给下游，只有换商后的正常回答被交付
  assert.deepEqual(f.seen.slice(0, -1), [good]);
  assert.equal(f.seen.at(-1).type, 'finish');
  assert.ok(!f.seen.some(c => c.type === 'text-delta' && c.text.includes('临时限制访问')));
  assert.equal(successes, 1);
});

test('access-block placeholder skips same-route retry and goes straight to switch', async () => {
  const placeholder = { ...stop, replayState: { response: { responseId: 'resp_freeipblk00000000000000000' } } };
  const f = fixture([() => chunks(placeholder), () => chunks(text, stop)]);
  await f.run();
  assert.equal(f.switches.length, 1);
  assert.ok(!f.events.some(e => e.type === 'same-route-retry'));
});

test('ordinary response IDs with normal stop stay successful', async () => {
  const f = fixture([() => chunks(text, { ...stop, replayState: { response: { responseId: 'resp_normal' } } })]);
  await f.run();
  assert.equal(f.failures.length, 0);
});

test('generation speed is measured from the first visible chunk to the stream end', async (t) => {
  let now = 1000;
  t.mock.method(performance, 'now', () => now);
  // 首块在 300ms 处出现，之后每块推 100ms，共 4 块正文；usage 报 40 个输出 token。
  const slowStartFastBody = () => (async function* () {
    now += 300; yield text;
    for (let i = 0; i < 4; i += 1) { now += 100; yield { type: 'text-delta', text: `t${i}` }; }
    yield { type: 'usage', usage: { inputTokens: 10, outputTokens: 40 } };
    yield stop;
  })();
  let success = null, usageMeta = null;
  // 首块超时要放大，否则这里推进的模拟时钟会先撞上默认的 200ms 截止。
  const f = fixture([slowStartFastBody], { firstResponseTimeoutMs: 10000, firstResponseBudgetMs: 10000, streamStallTimeoutMs: 10000 }, {
    onSuccess(_route, meta) { success = meta; },
    onUsage(_usage, _route, meta) { usageMeta = meta; },
  });
  await f.run();
  // 生成耗时只算首块之后那 400ms，不含 300ms 的等待
  assert.equal(success.latencyMs, 300);
  assert.equal(success.generationMs, 400);
  assert.equal(success.throughput, 100);
  assert.deepEqual(usageMeta, success);
});
