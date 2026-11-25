import assert from 'node:assert/strict';
import { test } from 'node:test';
import { installDeliveryCheck } from '../lib/delivery.js';

const text = value => [{ type: 'text', text: value }];
const user = (value, kind = 'user') => ({ type: 'user/message', data: { content: text(value), source: { kind } } });
const assistant = (value, provider = 'a6api') => ({ type: 'assistant/message', data: { turn: 2, step: 1, message: { content: text(value), source: { kind: 'model', provider, model: 'test' } } } });
const request = user('请帮我写一份需求文档');
const reply = assistant("Right, bash isn't available. I must use the CAPY_TOOL_CALLS bridge. Let me request acp_read_original_image and glob.");

function inspect(rows, reason = 'completed') {
  const records = [];
  let callback;
  const dispose = () => {};
  assert.equal(installDeliveryCheck({ on(name, fn) { assert.equal(name, 'session/event'); callback = fn; return dispose; } }, {
    store: { logEvent: record => records.push(record) }, isManaged: p => p === 'a6api',
  }), dispose);
  const events = [{ type: 'turn/start', data: { turn: 2 } }, ...rows, { type: 'turn/end', data: { turn: 2, reason: { kind: reason } } }].map((e, seq) => ({ ...e, seq }));
  const session = { id: 'private-session', eventAt: seq => events[seq] };
  for (const e of events) callback(session, e);
  return records;
}

test('screenshot-shaped reply records one redacted review event', () => {
  const [result, ...rest] = inspect([request, user('runtime reminder', 'system-prompt'), reply]);
  assert.deepEqual(rest, []);
  assert.match(result.requestRef, /^[a-f0-9]{16}\/1$/);
  assert.deepEqual(result, { requestRef: result.requestRef, type: 'delivery-review', rule: 'explicit-file-plan-only', code: 'A6_DELIVERY_UNVERIFIED', turn: 2, step: 1 });
});
test('short Chinese promise with a direct request', () => assert.equal(inspect([request, assistant('我会先查看图片。')]).length, 1));
for (const [name, rows, reason] of [
  ['ordinary chat', [user('你好'), reply]], ['injected request', [user('生成需求文档', 'system-prompt'), reply]],
  ['how-to question', [user('如何创建文件？'), reply]], ['negation', [user('请不要生成文件'), reply]],
  ['substantive inline response', [request, assistant('## 需求文档\n用户可以导出报告。')]],
  ['file link', [request, assistant('已生成 [需求文档](requirements.md)')]],
  ['earlier inline delivery', [request, assistant('需求文档正文'), reply]],
  ['tool result', [request, { type: 'tool/result' }, reply]], ['ambiguous new user message', [request, user('等一下'), reply]],
  ['other provider', [request, assistant('我会先查看图片。', 'other')]], ['aborted', [request, reply], 'aborted'],
  ['previous turn', [request, { type: 'turn/end', data: { turn: 1, reason: { kind: 'blocked' } } }, reply]],
  ['unknown side effect', [request, { type: 'assistant/attempt' }, reply]],
  ['over budget', [request, ...Array(128).fill({ type: 'step/start' }), reply]],
]) test(name, () => assert.deepEqual(inspect(rows, reason), []));
