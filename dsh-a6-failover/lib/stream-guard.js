import { randomUUID } from 'node:crypto';
import { performance } from 'node:perf_hooks';

/** 使用单调时钟为同一逻辑请求内的所有尝试计时。 */
const PERMANENT_CODES = new Set([
  'AUTH', 'UNAUTHORIZED', 'FORBIDDEN', 'INVALID', 'INVALID_REQUEST', 'QUOTA', 'ACCOUNT_QUOTA',
  'ACCOUNT_SIGN_IN_REQUIRED', 'ACCOUNT_TOKEN_INVALID', 'BILLING', 'CLIENT_UPGRADE_REQUIRED',
  'CONTENT_REFUSED', 'IMAGE_OFFLOAD_REQUIRED', 'MODEL_NOT_FOUND', 'MODEL_UNSUPPORTED', 'UNKNOWN_MODEL',
  'NO_ADAPTER', 'NO_CREDENTIAL_STORE', 'MISSING_CREDENTIAL', 'INVALID_CREDENTIAL',
  'CONTEXT_OVERFLOW', 'CONTEXT_LENGTH', 'CONTEXT_WINDOW_EXCEEDED', 'MODEL_SWITCH_COMPACTION_FAILED',
  'REQUEST_EXTENSION', 'REQUEST_HEADERS', 'DUPLICATE_ADAPTER', 'REGISTRATION_DISPOSED', 'A6_ROUTE_NOT_READY',
]);
const CANCEL_CODES = new Set(['ABORTED', 'ABORT_ERR', 'ERR_CANCELED', 'CANCELLED', 'CANCELED']);

export function isPermanentCode(code) {
  const value = String(code || '').trim().toUpperCase();
  return PERMANENT_CODES.has(value) || /^(CONTEXT|INVALID|UNSUPPORTED)_/.test(value);
}

export function isVisibleChunk(chunk) {
  if (!chunk || typeof chunk !== 'object') return false;
  switch (chunk.type) {
    case 'text-delta':
    case 'reasoning-delta':
      return typeof chunk.text === 'string' && chunk.text.trim().length > 0;
    case 'tool-call-delta':
      return true;
    case 'block-start':
      return chunk.blockType === 'tool-call';
    case 'block-end':
      return ['tool-call', 'image', 'file'].includes(chunk.block?.type)
        || ((chunk.block?.type === 'text' || chunk.block?.type === 'reasoning') && typeof chunk.block.text === 'string' && chunk.block.text.trim().length > 0);
    default:
      return false;
  }
}

function failureFacts(error, fallbackMessage) {
  const details = error?.failure;
  const code = String(details?.code || error?.code || error?.cause?.code || '').trim().toUpperCase();
  const status = Number(details?.status || error?.status || error?.statusCode || error?.response?.status
    || error?.cause?.status || error?.cause?.statusCode || /^HTTP_(\d{3})$/.exec(code)?.[1] || 0);
  const message = String(details?.message || error?.message || fallbackMessage);
  return { code, status, message };
}

function classifyFailure({ code, status, message }, kind, enabled, sawVisible) {
  // 408/425/429 由服务端或流量状况决定，其余明确的 4xx 需要修正请求。
  const permanent = isPermanentCode(code) || (status >= 400 && status < 500 && ![408, 425, 429].includes(status));
  return {
    retryable: Boolean(enabled) && !sawVisible && !permanent,
    recoverable: Boolean(enabled) && !permanent,
    permanent,
    kind,
    code,
    message: `${status ? `HTTP ${status} ` : ''}${message}`,
  };
}

export function classifyAttempt({ finish, thrown, didThrow = Boolean(thrown), sawVisible, ended, cfg, signalAborted }) {
  if (signalAborted) return { retryable: false, kind: 'aborted-by-user', code: '', message: '' };

  if (didThrow) {
    const facts = failureFacts(thrown, typeof thrown === 'string' ? thrown : '模型流抛出异常');
    if (CANCEL_CODES.has(facts.code) || (!facts.code && thrown?.name === 'AbortError')) {
      return { ...facts, retryable: false, kind: 'cancelled' };
    }
    const enabled = facts.code === 'EMPTY_RESPONSE' ? cfg.switchOnEmpty : cfg.switchOnThrown;
    return classifyFailure(facts, 'thrown', enabled, sawVisible);
  }

  if (finish) {
    const reason = finish.reason || { kind: 'stop' };
    if (reason.kind === 'error' || reason.kind === 'aborted') {
      const facts = failureFacts(reason, reason.kind === 'error' ? '模型返回 error' : '模型流被中断');
      if (CANCEL_CODES.has(facts.code)) return { ...facts, retryable: false, kind: 'cancelled' };
      const enabled = facts.code === 'EMPTY_RESPONSE' ? cfg.switchOnEmpty : cfg.switchOnFinishError;
      return classifyFailure(facts, `finish-${reason.kind}`, enabled, sawVisible);
    }
    if (!sawVisible) {
      return {
        retryable: Boolean(cfg.switchOnEmpty), kind: 'empty', code: 'EMPTY_RESPONSE',
        message: `模型返回空正文（finish=${reason.kind}）`,
      };
    }
    return { retryable: false, kind: 'ok', code: '', message: '' };
  }

  if (!ended) return { retryable: false, kind: 'pending', code: '', message: '' };
  return {
    retryable: Boolean(cfg.switchOnTruncated) && !sawVisible, recoverable: Boolean(cfg.switchOnTruncated), kind: 'truncated', code: 'TRUNCATED',
    message: '模型流在 finish 之前结束（半路掉线）',
  };
}

/** 监听取消时仍接住上游的异步拒绝，并在正常完成后解除监听。 */
async function waitFor(pending, signal) {
  if (!signal) return pending;
  let onAbort;
  try {
    return await new Promise((resolve, reject) => {
      onAbort = () => reject(signal.reason);
      signal.addEventListener('abort', onAbort, { once: true });
      Promise.resolve(pending).then(resolve, reject);
      if (signal.aborted) onAbort();
    });
  } finally {
    signal.removeEventListener('abort', onAbort);
  }
}

function errorFinish({ code, message }) {
  return { type: 'finish', reason: { kind: 'error', failure: { code, message } } };
}

function streamError(failure, verdict) {
  const details = { code: verdict.code || 'STREAM_FAILURE', message: verdict.message, ...failure };
  return Object.assign(new Error(details.message), { code: details.code, failure: details });
}

function formatRule(chunk) {
  if (!chunk || typeof chunk !== 'object' || typeof chunk.type !== 'string' || !chunk.type) return 'chunk-shape';
  if (chunk.type === 'text-delta' && typeof chunk.text !== 'string') return 'text-delta-type';
  if (chunk.type === 'reasoning-delta' && typeof chunk.text !== 'string') return 'reasoning-delta-type';
  if (chunk.type === 'finish' && (typeof chunk.reason?.kind !== 'string' || !chunk.reason.kind)) return 'finish-reason-shape';
  return null;
}

const timeoutMessages = {
  A6_FIRST_RESPONSE_TIMEOUT: '原路等待有效首块超时',
  A6_FIRST_RESPONSE_BUDGET: '等待有效首块的总预算已用尽',
  A6_STREAM_STALL: '模型流在有效输出后持续无进展',
};

function timedScope(parent, initialLimit, initialCode) {
  const controller = new AbortController();
  const cancel = () => controller.abort(parent.reason);
  parent?.addEventListener('abort', cancel, { once: true });
  if (parent?.aborted) cancel();
  let timer, deadline, code;
  const expire = () => controller.abort(Object.assign(new Error(timeoutMessages[code]), { code }));
  function arm(limit, errorCode) {
    clearTimeout(timer);
    deadline = limit;
    code = errorCode;
    const remaining = deadline - performance.now();
    if (remaining <= 0) expire();
    else timer = setTimeout(expire, Math.ceil(remaining));
  }
  arm(initialLimit, initialCode);
  return {
    signal: controller.signal, arm,
    pause() {
      clearTimeout(timer);
      return Math.max(0, deadline - performance.now());
    },
    check() {
      if (performance.now() >= deadline) expire();
      controller.signal.throwIfAborted();
    },
    close() {
      clearTimeout(timer);
      parent?.removeEventListener('abort', cancel);
      controller.abort();
    },
  };
}

// Only fixed protocol rules and locally generated request identifiers enter evidence.
function safeFailureCode(code) {
  return PERMANENT_CODES.has(code) || Object.hasOwn(timeoutMessages, code)
    || /^(?:HTTP_[1-5]\d\d|SERVER|NETWORK|EMPTY_RESPONSE|TRUNCATED|A6_STREAM_FORMAT|A6_STREAM_PREFIX_LIMIT|A6_UPSTREAM_ACCESS_RESTRICTED)$/.test(code)
    ? code : 'STREAM_FAILURE';
}

export async function* guardedStream({
  options, cfg, openStream, planSwitch, onFailure, onSuccess, onEvent, onUsage,
}) {
  const emit = (event) => { try { onEvent?.(event); } catch { /* 忽略日志故障 */ } };
  const original = { provider: String(options?.provider || ''), model: String(options?.model || '') };
  const signal = options?.signal;
  const configuredBudget = Number(cfg.maxSwitchesPerCall);
  const maxSwitches = Number.isFinite(configuredBudget) ? Math.max(0, Math.floor(configuredBudget)) : 0;
  const failedChannels = new Set();
  const failedRoutes = new Set();
  let route = original;
  let switches = 0;
  let attempt = 0;
  let retries = 0;
  let requestId;
  const start = performance.now();
  const totalDeadline = start + (cfg.firstResponseBudgetMs ?? 180000);
  const originalDeadline = Math.min(totalDeadline, start + (cfg.firstResponseTimeoutMs ?? 30000));
  const stallMs = cfg.streamStallTimeoutMs ?? 30000;

  for (;;) {
    signal?.throwIfAborted();
    const deadline = switches ? totalDeadline : originalDeadline;
    const scope = timedScope(signal, deadline, deadline === totalDeadline ? 'A6_FIRST_RESPONSE_BUDGET' : 'A6_FIRST_RESPONSE_TIMEOUT');
    const attemptOptions = { ...options, ...route, signal: scope.signal };
    attempt += 1;
    let rule;
    const buffered = [];
    let iterator;
    let sawVisible = false;
    let finish = null;
    let thrown;
    let didThrow = false;
    let ended = false;
    let lastUsage;
    const startedAt = performance.now();
    let firstVisibleAt = 0;

    try {
      for (;;) {
        signal?.throwIfAborted();
        let step;
        try {
          scope.check();
          iterator ??= openStream(attemptOptions, { first: attempt === 1, attempt })[Symbol.asyncIterator]();
          step = await waitFor(iterator.next(), scope.signal);
          scope.check();
        } catch (error) {
          didThrow = true;
          thrown = error;
          try { scope.check(); } catch (deadlineError) { thrown = deadlineError; }
          break;
        }
        signal?.throwIfAborted();
        if (!step || step.done) { ended = true; break; }
        const chunk = step.value;
        rule = formatRule(chunk);
        if (rule) {
          didThrow = true;
          thrown = Object.assign(new Error('模型流事件结构异常'), { code: 'A6_STREAM_FORMAT' });
          break;
        }
        if (chunk.type === 'finish') {
          const responseId = chunk.replayState?.response?.responseId;
          // 已在真实响应中确认的限流占位 ID；不凭自然语言关键词判断正常回答。
          finish = responseId === 'resp_freeipblk00000000000000000'
            ? { ...chunk, reason: { kind: 'error', failure: {
              code: 'A6_UPSTREAM_ACCESS_RESTRICTED',
              message: '上游返回临时限制访问提示（正常结束标记无效）；请稍后重试或检查中转站。',
            } } }
            : chunk;
          break;
        }
        if (chunk?.type === 'usage' && chunk.usage && typeof chunk.usage === 'object') lastUsage = chunk.usage;
        const progress = isVisibleChunk(chunk);
        if (!sawVisible && !progress) {
          if (chunk.type === 'heartbeat' || chunk.type === 'ping' || chunk.text === '') continue;
          if (buffered.length >= 512) {
            didThrow = true;
            thrown = Object.assign(new Error('有效首块前的元数据超过缓存上限'), { code: 'A6_STREAM_PREFIX_LIMIT' });
            break;
          }
          buffered.push(chunk);
          continue;
        }
        // 下游暂停消费时，上游没有正在等待的 next，不把背压计为商家停顿。
        const remaining = scope.pause();
        if (!sawVisible) {
          sawVisible = true;
          firstVisibleAt = performance.now();
          for (const prefix of buffered) {
            signal?.throwIfAborted();
            yield prefix;
          }
          buffered.length = 0;
        }
        signal?.throwIfAborted();
        yield chunk;
        scope.arm(performance.now() + (progress ? stallMs : remaining), 'A6_STREAM_STALL');
      }
    } finally {
      // 上游 return 可能排在尚未完成的 next 之后，取消时先发起释放即可。
      scope.close();
      if (!ended) {
        try { Promise.resolve(iterator?.return?.()).catch(() => {}); } catch { /* 保留主调用结果 */ }
      }
    }

    signal?.throwIfAborted();
    const verdict = classifyAttempt({ finish, thrown, didThrow, sawVisible, ended, cfg });
    // 首块时间：这是用户真正感知的等待，站点报的 p50 是群体口径，二者不是一回事。
    const latencyMs = Math.round((firstVisibleAt || performance.now()) - startedAt);
    if (verdict.kind === 'ok') {
      if (lastUsage) {
        try { onUsage?.(lastUsage, route); } catch { /* 记账故障 */ }
      }
      try { onSuccess?.(route, { latencyMs }); } catch { /* 统计故障 */ }
    } else if (verdict.kind !== 'cancelled') {
      // 在交出终结块之前同步记下失败，供上层辨认已输出内容的请求。
      try {
        onFailure?.({
          ...route, kind: sawVisible && didThrow ? 'mid-stream-thrown' : verdict.kind,
          code: safeFailureCode(verdict.code),
          permanent: Boolean(verdict.permanent), recoverable: Boolean(verdict.recoverable || verdict.retryable),
          postContent: sawVisible, latencyMs,
          evidence: { requestId: requestId ??= randomUUID(), attempt, elapsedMs: Math.round(performance.now() - start),
            firstResponseMs: sawVisible ? latencyMs : null, ...(rule ? { rule } : {}) },
        });
      } catch { /* 保留主调用结果 */ }
    }

    signal?.throwIfAborted();
    if (verdict.retryable) {
      const summary = { kind: verdict.kind, code: safeFailureCode(verdict.code) };
      emit({ type: 'attempt-failed', ...route, verdict: summary });
      if (performance.now() >= totalDeadline || verdict.code === 'A6_FIRST_RESPONSE_BUDGET') {
        throw streamError(null, { code: 'A6_FIRST_RESPONSE_BUDGET', message: timeoutMessages.A6_FIRST_RESPONSE_BUDGET });
      }
      if (retries < (cfg.sameRouteRetries ?? 1) && performance.now() < deadline && verdict.code !== 'A6_FIRST_RESPONSE_TIMEOUT') {
        retries += 1;
        emit({ type: 'same-route-retry', ...route, attempt });
        continue;
      }
      if (switches >= maxSwitches) {
        emit({ type: 'switch-budget-exhausted', ...route });
      } else {
        failedRoutes.add(`${route.provider}|${route.model}`);
        let planned;
        const control = timedScope(signal, totalDeadline, 'A6_FIRST_RESPONSE_BUDGET');
        try {
          control.check();
          planned = await waitFor(planSwitch({
            ...route, failure: summary, failedChannels, failedRoutes, attempt: switches, signal: control.signal,
          }), control.signal);
          control.check();
        } catch {
          signal?.throwIfAborted();
          control.check();
          planned = { ok: false };
        } finally { control.close(); }
        signal?.throwIfAborted();
        if (planned?.ok) {
          switches += 1;
          retries = 0;
          if ((planned.mode === 'pin' || planned.mode === 'deduped') && planned.channelId > 0) {
            failedChannels.add(planned.channelId);
          }
          const from = route;
          route = { provider: planned.provider || route.provider, model: planned.model || route.model };
          emit({ type: 'switched', from, to: route, mode: planned.mode,
            channelId: planned.channelId, channelName: planned.channelName });
          continue;
        }
        emit({ type: 'switch-unavailable', provider: route.provider, reason: planned?.reason || 'no-candidate' });
        if (planned?.reason === '保留平台已有的固定商家') {
          const failure = { code: 'A6_EXISTING_PIN', message: `${verdict.message}。平台已有固定商家，插件无法确认归属；要允许自动换商，请开启「接管站点已有固定商家」，或先核对该模型的固定记录。` };
          yield errorFinish(failure);
          return;
        }
      }
    }

    for (const chunk of buffered) {
      signal?.throwIfAborted();
      yield chunk;
    }
    signal?.throwIfAborted();
    if (didThrow) throw thrown;
    // DSH 将缺少 finish 的流解释为正常停止，补齐失败终结块。
    const terminal = verdict.kind === 'truncated'
      || (verdict.kind === 'empty' && (finish.reason?.kind || 'stop') === 'stop')
      ? errorFinish(verdict) : finish;
    if (terminal) yield terminal;
    signal?.throwIfAborted();
    // 保留终结块供 DSH 入档，再走流异常分支，避免请求级恢复器重播已输出内容。
    if (sawVisible && verdict.kind !== 'ok') throw streamError(terminal?.reason?.failure, verdict);
    return;
  }
}
