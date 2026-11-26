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
  // 上游说明可能藏在 reason / upstream_message / detail 等字段里（A6 网关把「上游说明：…」
  // 拼进 error.message 或单列字段，各版本不一）。全部拼起来，供格式类错误精确识别。
  const detail = [details?.reason, details?.detail, details?.upstream_message, error?.reason,
    error?.upstream_message, error?.detail, error?.response?.data?.message]
    .filter(Boolean).map((value) => String(value)).join(' ');
  return { code, status, message, detail: `${message} ${detail}` };
}

/**
 * 某些中转站的上游不是标准 OpenAI 格式：思考模式 + 工具调用时，强制要求历史 assistant
 * 消息带 reasoning_content（标准格式不需要）。同一请求换一家就 200（实测 1764/6940/7030 通过，
 * 只有 5927 报 400）。这类商家特有的格式错误应当换商绕开，而不是当永久错误放行。
 * 只匹配这类特征，避免把真正的请求格式错误（所有商家都会 400）误判成可换商。
 */
function isMerchantFormatFacts({ code, status, detail }) {
  if (!(code === 'INVALID_REQUEST' || code === 'INVALID' || (status >= 400 && status < 500))) return false;
  return /reasoning_content/i.test(detail)
    && /required|missing|is required|不支持|缺少|必填/i.test(detail);
}

function classifyFailure({ code, status, message, detail }, kind, enabled, sawVisible) {
  // 408/425/429 由服务端或流量状况决定，其余明确的 4xx 需要修正请求。
  const permanent = isPermanentCode(code) || (status >= 400 && status < 500 && ![408, 425, 429].includes(status));
  // 商家特有的非标准格式要求（如 reasoning_content）：换一家就能用，放行换商。
  if (isMerchantFormatFacts({ code, status, detail })) {
    return { retryable: Boolean(enabled), recoverable: true, permanent: false,
      kind, code, message, switchImmediately: true, reason: 'merchant-format' };
  }
  // 上游限流占位响应：即使提示文案已作为正文出现，也必须允许换商家重试，
  // 否则 0.7.2 的 sawVisible 分支会把唯一可恢复的错误判成不可重试。
  if (code === 'A6_UPSTREAM_ACCESS_RESTRICTED') {
    return { retryable: Boolean(enabled), recoverable: true, permanent: false,
      kind, code, message, switchImmediately: true };
  }
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

// 上游「免费 IP 拦截」占位响应：正文是一句固定提示，且以正常 stop 结束，
// 只在 finish 的 responseId 里留下可判定的标记。先把这类文本按住不交给下游，
// 等 finish 确认后再决定丢弃（换商重试）还是照常交付，避免错误文案冒充成功回答。
const PLACEHOLDER_RESPONSE_ID = 'resp_freeipblk00000000000000000';
function isPlaceholderResponse(chunk) {
  return chunk?.replayState?.response?.responseId === PLACEHOLDER_RESPONSE_ID;
}
function isPlaceholderText(chunk) {
  if (chunk?.type !== 'text-delta' || typeof chunk.text !== 'string') return false;
  return chunk.text.includes('当前网络的请求存在异常') || chunk.text.includes('已被临时限制访问');
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
  A6_FIRST_RESPONSE_BUDGET: '本次尝试等待有效首块超时（每次换商独立计时）',
  A6_STREAM_STALL: '模型流在有效输出后持续无进展',
  A6_SWITCH_TIMEOUT: '等待站点分配商家超时（不计入首块预算）',
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
  /**
   * 首块预算按「每次尝试」独立计时，而不是整轮共享一笔。
   *
   * 共享预算的现场代价：一个 120 万 token 的超长请求在替换商家上慢慢等，把整轮预算吃光，
   * 后面的换商连尝试的机会都没有，界面直接报「等待有效首块的总预算已用尽」。
   * 现在原路用 firstResponseTimeoutMs，每次替换商家各自拿 firstResponseBudgetMs。
   * 总等待时间的上限改由 maxSwitchesPerCall 控制（次数而不是时间）。
   */
  const budgetMs = Math.max(1, Number(cfg.firstResponseBudgetMs) || 60000);
  const timeoutMs = Math.max(1, Number(cfg.firstResponseTimeoutMs) || 30000);
  const stallMs = cfg.streamStallTimeoutMs ?? 30000;
  // 原路（含它的同路重试）共用一笔 firstResponseTimeoutMs：用户设的「30 秒无响应就换商」
  // 必须真的在 30 秒后开始换商，不能被同路重试无限续命。
  const originalDeadline = start + Math.min(timeoutMs, budgetMs);

  for (;;) {
    signal?.throwIfAborted();
    // 每次尝试自己的截止时间：还没换商家时沿用原路那一笔，换过之后每家新商家各自新计时。
    const deadline = switches ? performance.now() + budgetMs : originalDeadline;
    const deadlineCode = switches ? 'A6_FIRST_RESPONSE_BUDGET' : 'A6_FIRST_RESPONSE_TIMEOUT';
    const scope = timedScope(signal, deadline, deadlineCode);
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
    let heldTexts = [];
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
          if (isPlaceholderResponse(chunk)) {
            // 确认是上游占位响应：丢弃刚才扣住的提示文案，按可恢复错误处理。
            heldTexts = [];
            finish = { ...chunk, reason: { kind: 'error', failure: {
              code: 'A6_UPSTREAM_ACCESS_RESTRICTED',
              message: '上游返回临时限制访问提示（正常结束标记无效）；请稍后重试或检查中转站。',
            } } }
          } else {
            // 不是占位响应：扣住的只是普通文本，照常交付。
            for (const held of heldTexts) { signal?.throwIfAborted(); yield held; }
            heldTexts = [];
            finish = chunk;
          }
          break;
        }
        if (chunk?.type === 'usage' && chunk.usage && typeof chunk.usage === 'object') lastUsage = chunk.usage;
        if (isPlaceholderText(chunk)) { heldTexts.push(chunk); continue; }
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
      // 不再有整轮总预算：这一层只保留「本次尝试已经用尽」的语义，
      // 后续换商次数由 maxSwitchesPerCall 限制。
      if (!verdict.switchImmediately
        && retries < (cfg.sameRouteRetries ?? 1) && performance.now() < deadline && verdict.code !== 'A6_FIRST_RESPONSE_TIMEOUT') {
        retries += 1;
        emit({ type: 'same-route-retry', ...route, attempt });
        continue;
      }
      if (switches >= maxSwitches) {
        emit({ type: 'switch-budget-exhausted', ...route });
      } else {
        failedRoutes.add(`${route.provider}|${route.model}`);
        let planned;
        // 控制面单独计时：它不该吃掉任何一次尝试的首块预算，
        // 更不该因为控制面慢就把整个请求判死——等不到候选只是这次换不了，交给上层降级。
        const controlLimit = performance.now() + Math.max(200, Number(cfg.controlTimeoutMs) || 15000);
        const control = timedScope(signal, controlLimit, 'A6_SWITCH_TIMEOUT');
        try {
          control.check();
          planned = await waitFor(planSwitch({
            ...route, failure: summary, failedChannels, failedRoutes, attempt: switches, signal: control.signal,
          }), control.signal);
          control.check();
        } catch {
          // 用户主动取消要照常抛出；控制面自己超时只记「这次换不了」。
          signal?.throwIfAborted();
          planned = { ok: false, reason: '控制面超时，这次不换商' };
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
