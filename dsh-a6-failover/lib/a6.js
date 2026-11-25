/**
 * A6API（new-api 系）控制面客户端。
 *
 * 只碰站点自己的四个接口，不模拟网页操作：
 *   GET  /api/marketplace/channels?model_name=X   该模型的全部商家与报价
 *   GET  /api/subscriptions                       模型路由池（缓存率的唯一来源）
 *   GET  /api/marketplace/pins                    当前固定关系
 *   POST /api/marketplace/pin                     固定商家
 *   POST /api/marketplace/unpin                   取消固定
 * 外加两个令牌接口，用来把 DSH 里的 sk- key 对回站点的 token_id：
 *   GET  /api/token/?p=0&size=100
 *   POST /api/token/{id}/key
 *
 * 认证只要两个头：authorization: Bearer <access_token> 与 new-api-user: <user_id>。
 * 缺 user 头站点会报 dashboard_auth_user_id_missing（不是「令牌无效」）。
 */

export class A6Error extends Error {
  constructor(message, { code = 'A6_CONTROL', status = 0 } = {}) {
    super(message);
    this.name = 'A6Error';
    this.code = code;
    this.status = status;
  }
}

export function authHeaders(accessToken, userId) {
  const headers = { authorization: `Bearer ${String(accessToken || '').trim()}` };
  const id = String(userId ?? '').trim();
  if (id) headers['new-api-user'] = id;
  return headers;
}

export function controlOrigin(value) {
  try {
    const url = new URL(String(value || '').trim());
    if (url.protocol !== 'https:' || url.username || url.password || url.pathname !== '/' || url.search || url.hash) return '';
    return url.origin;
  } catch {
    return '';
  }
}

/** 把响应体读成文本，但绝不为了一个大响应把进程撑爆。 */
async function readTextCapped(response, maxBytes) {
  const declared = Number(response.headers.get('content-length') || 0);
  if (declared > maxBytes) {
    await response.body?.cancel().catch(() => {});
    throw new A6Error(`控制面响应过大（${declared} 字节 > 上限 ${maxBytes}）`, { code: 'A6_TOO_LARGE' });
  }
  if (!response.body) {
    const text = await response.text();
    if (text.length > maxBytes) throw new A6Error('控制面响应过大', { code: 'A6_TOO_LARGE' });
    return text;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  const chunks = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > maxBytes) {
      try { await reader.cancel(); } catch { /* 已决定放弃 */ }
      throw new A6Error(`控制面响应过大（> ${maxBytes} 字节）`, { code: 'A6_TOO_LARGE' });
    }
    chunks.push(decoder.decode(value, { stream: true }));
  }
  chunks.push(decoder.decode());
  return chunks.join('');
}

/**
 * 打一次控制面请求。
 * @returns 解析后的 JSON（站点约定 { success, message, data }）。
 */
export async function a6Request(options) {
  const {
    controlBase, path, method = 'GET', accessToken, userId, body,
    timeoutMs = 20000, maxBytes = 48 * 1024 * 1024, signal, rawJson = false,
  } = options;
  const origin = controlOrigin(controlBase);
  const url = origin && new URL(path, origin);
  if (!url || url.origin !== origin) throw new A6Error('控制面地址必须为 HTTPS 站点', { code: 'A6_CONTROL_ORIGIN' });
  const headers = { ...authHeaders(accessToken, userId), accept: 'application/json' };
  if (body !== undefined) headers['content-type'] = 'application/json';
  const timeout = AbortSignal.timeout(Math.trunc(timeoutMs));
  const controller = new AbortController();
  const cancel = (event) => controller.abort(event.target.reason);
  const signals = signal ? [signal, timeout] : [timeout];
  for (const source of signals) {
    if (source.aborted) controller.abort(source.reason);
    else source.addEventListener('abort', cancel, { once: true });
  }
  let payload;
  try {
    const response = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
      redirect: 'error',
    });
    if (!response.ok) {
      await response.body?.cancel().catch(() => {});
      throw new A6Error(`控制面 HTTP ${response.status}`, { code: 'A6_HTTP', status: response.status });
    }
    const text = await readTextCapped(response, maxBytes);
    if (rawJson) return text;
    try { payload = JSON.parse(text); } catch {
      throw new A6Error('控制面返回了无效 JSON', { code: 'A6_BAD_JSON', status: response.status });
    }
    if (!payload || typeof payload !== 'object' || payload.success === false) {
      throw new A6Error('控制面拒绝请求或返回无效数据', { code: 'A6_REJECTED', status: response.status });
    }
  } catch (error) {
    if (error instanceof A6Error) throw error;
    const reason = signal?.aborted ? '已取消' : timeout.aborted ? '超时' : '网络失败或站点跳转';
    throw new A6Error(`控制面 ${method} 请求失败：${reason}`, { code: 'A6_NETWORK' });
  } finally {
    for (const source of signals) source.removeEventListener('abort', cancel);
  }
  return payload;
}

export async function listPins(site) {
  const payload = await a6Request({ ...site, path: '/api/marketplace/pins', maxBytes: 4 * 1024 * 1024 });
  const rows = payload && typeof payload === 'object' ? payload.data : null;
  if (!Array.isArray(rows)) throw new A6Error('固定商家列表格式无效', { code: 'A6_BAD_PINS' });
  return rows;
}

export async function fetchChannels({ modelName, ...site }) {
  const query = `/api/marketplace/channels?model_name=${encodeURIComponent(modelName)}`;
  return a6Request({ ...site, path: query });
}

/**
 * 拉模型路由池。缓存率（cache_hit_rate_bps）只在这份响应的
 * `data.items[].auto_channels` 与 `data.items[].auto_pool_preview.items` 里，
 * `/api/marketplace/channels` 的 listing 一个字段都不带（实测 12810/12810 无）。
 * 响应约 2MB，失败就当没这份数据，不影响选商家。
 */
export async function fetchSubscriptions(site) {
  return a6Request({ ...site, path: '/api/subscriptions' });
}

export async function pinChannel({
  controlBase, accessToken, userId, tokenId, modelName, channelId,
  fallbackToSmartRouting = false, timeoutMs, signal,
}) {
  return a6Request({
    controlBase,
    path: '/api/marketplace/pin',
    method: 'POST',
    accessToken,
    userId,
    timeoutMs,
    signal,
    body: {
      token_id: Number(tokenId),
      model_name: modelName,
      channel_id: Number(channelId),
      fallback_to_smart_routing: Boolean(fallbackToSmartRouting),
    },
  });
}

export async function unpinChannel({ controlBase, accessToken, userId, tokenId, modelName, channelId, timeoutMs, signal }) {
  return a6Request({
    controlBase,
    path: '/api/marketplace/unpin',
    method: 'POST',
    accessToken,
    userId,
    timeoutMs,
    signal,
    body: { token_id: Number(tokenId), model_name: modelName, channel_id: Number(channelId) },
  });
}

export async function listTokens(site) {
  const payload = await a6Request({
    ...site, path: '/api/token/?p=0&size=100', maxBytes: 1024 * 1024,
  });
  const body = payload && typeof payload === 'object' ? payload.data : null;
  const rows = body && typeof body === 'object' ? body.items : null;
  return Array.isArray(rows) ? rows : [];
}

export async function tokenPlainKey({ controlBase, accessToken, userId, tokenId, timeoutMs, signal }) {
  const payload = await a6Request({
    controlBase,
    path: `/api/token/${Number(tokenId)}/key`,
    method: 'POST',
    accessToken,
    userId,
    timeoutMs,
    signal,
    body: {},
  });
  const body = payload && typeof payload === 'object' ? payload.data : null;
  return String((body && typeof body === 'object' && body.key) || '');
}

/** 站点令牌本体是 40 位裸串，客户端习惯写 sk-xxx；比对前统一去掉前缀。 */
export function bareKey(key) {
  const text = String(key || '').trim();
  return text.slice(0, 3).toLowerCase() === 'sk-' ? text.slice(3) : text;
}
