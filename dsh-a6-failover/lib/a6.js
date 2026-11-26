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

import http from 'node:http';
import https from 'node:https';
import tls from 'node:tls';

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

/**
 * A6API 的域名解析到私有地址段（main.apisnode.cc 的 A 记录是 172.80.127.x），
 * 直连必然超时；浏览器能打开是因为走了系统代理。Node 的 fetch 不会自动读系统代理，
 * 所以这里给请求显式挂一个代理。
 *
 * 实现上不依赖 undici（DSH 的 Node 里 import 不到它），改用 Node 内置的
 * http/https 手写 CONNECT 隧道。隧道建立后用 https.request 复用同一 socket，
 * 拿到完整响应体再交回调用方——控制面响应都是小 JSON，不需要流式。
 */
export function proxyUrlOf(explicit) {
  const pick = (value) => {
    const text = String(value || '').trim();
    return text && /^https?:\/\//i.test(text) ? text : '';
  };
  return pick(explicit)
    || pick(process.env.HTTPS_PROXY) || pick(process.env.https_proxy)
    || pick(process.env.HTTP_PROXY) || pick(process.env.http_proxy)
    || pick(process.env.ALL_PROXY) || pick(process.env.all_proxy)
    || '';
}

/**
 * 经过同一个本地代理的请求排队执行。
 *
 * 实测这个代理（verge-mihomo）在同时开多条 CONNECT 隧道时会直接掐掉新连接
 * （报 "socket disconnected before secure TLS connection was established"）。
 * 商家表 25 MB 本身就够慢，再和订阅/pin 抢隧道只会互相搞死，所以干脆串行。
 */
let proxyGate = Promise.resolve();
function withProxySlot(task) {
  const run = proxyGate.then(task, task);
  // 不让前一个任务的失败影响后面排队的人
  proxyGate = run.then(() => undefined, () => undefined);
  return run;
}

/**
 * 经 HTTP 代理发一次 HTTPS 请求（CONNECT 隧道）。
 *
 * 用自定义 Agent 的 createConnection 提供已打通的 socket，而不是把 socket 直接塞进
 * 单次请求的 options —— 后者在响应体较大时（商家表 25 MB）会中途 abort。
 * 交给 Agent 管生命期，Node 才会正确处理 keep-alive、背压和读满。
 *
 * @returns {Promise<{status:number, text:string}>}
 */
export function requestViaProxy(proxyUrl, targetUrl, options = {}) {
  return withProxySlot(() => proxyRequest(proxyUrl, targetUrl, options));
}

function proxyRequest(proxyUrl, targetUrl, { method = 'GET', headers = {}, body, timeoutMs = 20000, signal } = {}) {
  return new Promise((resolve, reject) => {
    const startedAt = Date.now();
    const proxy = new URL(proxyUrl);
    const target = new URL(targetUrl);
    const proxyPort = Number(proxy.port) || (proxy.protocol === 'https:' ? 443 : 80);
    const proxyLib = proxy.protocol === 'https:' ? https : http;
    const authority = `${target.hostname}:${Number(target.port) || 443}`;

    let settled = false;
    const settle = (fn, value) => { if (!settled) { settled = true; fn(value); } };
    const onAbort = () => settle(reject, new A6Error(`已取消（${target.pathname.split('?')[0]}，${((Date.now() - startedAt) / 1000).toFixed(1)}s 时收到 abort）`, { code: 'A6_ABORTED' }));
    signal?.addEventListener('abort', onAbort, { once: true });
    const cleanup = () => signal?.removeEventListener('abort', onAbort);

    const tunnel = proxyLib.request({
      host: proxy.hostname,
      port: proxyPort,
      method: 'CONNECT',
      path: authority,
      headers: {
        host: authority,
        ...(proxy.username
          ? { 'proxy-authorization': 'Basic ' + Buffer.from(`${decodeURIComponent(proxy.username)}:${decodeURIComponent(proxy.password || '')}`).toString('base64') }
          : {}),
      },
      timeout: timeoutMs,
    });

    tunnel.on('timeout', () => {
      tunnel.destroy();
      cleanup(); settle(reject, new A6Error('代理连接超时', { code: 'A6_PROXY_TIMEOUT' }));
    });
    tunnel.on('error', (error) => {
      cleanup(); settle(reject, new A6Error(`代理连接失败：${error.message}`, { code: 'A6_PROXY' }));
    });
    tunnel.on('connect', (response, rawSocket) => {
      if (response.statusCode !== 200) {
        rawSocket.destroy(); cleanup();
        settle(reject, new A6Error(`代理拒绝隧道 HTTP ${response.statusCode}`, { code: 'A6_PROXY', status: response.statusCode }));
        return;
      }
      rawSocket.setNoDelay(true);
      rawSocket.setTimeout(0);           // 大响应不能被 socket 空闲超时打断

      /*
       * 必须手动包一层 TLS。
       *
       * 直接把 CONNECT 拿到的裸 socket 塞给 https.Agent.createConnection 是不行的：
       * Agent 认为「你能给我一个 socket，那它已经是 TLS 会话了」，于是不再握手。
       * 结果明文 HTTP/1.1 被发到 443 上，边缘节点看不懂，回一句
       * 「Domain Not Configured / HTTP 400」——看起来像域名没配，实际是没加密。
       */
      const secure = tls.connect({
        socket: rawSocket,
        servername: target.hostname,
        ALPNProtocols: ['http/1.1'],
      }, () => {
        secure.setNoDelay(true);
        secure.setTimeout(0);
        const agent = new https.Agent({ keepAlive: false, maxSockets: 1 });
        agent.createConnection = () => secure;

        const payload = body === undefined ? undefined : (typeof body === 'string' ? body : JSON.stringify(body));
        const req = https.request({
          host: target.hostname,
          port: Number(target.port) || 443,
          path: `${target.pathname}${target.search}`,
          method,
          headers: { host: target.host, ...headers, ...(payload === undefined ? {} : { 'content-length': Buffer.byteLength(payload) }) },
          agent,
          timeout: timeoutMs,
        }, (res) => {
          const chunks = [];
          let bytes = 0;
          res.on('data', (chunk) => { chunks.push(chunk); bytes += chunk.length; });
          res.on('end', () => {
            agent.destroy(); cleanup();
            settle(resolve, { status: res.statusCode || 0, text: Buffer.concat(chunks, bytes).toString('utf8') });
          });
          res.on('aborted', () => {
            agent.destroy(); cleanup();
            settle(reject, new A6Error('响应被中断', { code: 'A6_PROXY' }));
          });
          res.on('error', (error) => {
            agent.destroy(); cleanup();
            settle(reject, new A6Error(`读取响应失败：${error.message}`, { code: 'A6_PROXY' }));
          });
        });
        req.on('timeout', () => {
          req.destroy();
          settle(reject, new A6Error('请求超时', { code: 'A6_PROXY_TIMEOUT' }));
        });
        req.on('error', (error) => {
          agent.destroy(); cleanup();
          settle(reject, new A6Error(`隧道请求失败：${error.message}`, { code: 'A6_PROXY' }));
        });
        if (payload !== undefined) req.write(payload);
        req.end();
      });
      secure.on('timeout', () => {
        secure.destroy();
        cleanup(); settle(reject, new A6Error('TLS 握手超时', { code: 'A6_PROXY_TIMEOUT' }));
      });
      secure.on('error', (error) => {
        cleanup(); settle(reject, new A6Error(`TLS 握手失败：${error.message}`, { code: 'A6_PROXY' }));
      });
    });
    tunnel.end();
  });
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
  // 直连：用 Node 原生 fetch，短超时探活。连得上就一直用直连，不碰代理。
  const direct = async (budgetMs) => {
    const local = AbortSignal.any([controller.signal, AbortSignal.timeout(Math.trunc(budgetMs))]);
    const response = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: local,
      redirect: 'error',
    });
    const status = response.status;
    if (!response.ok) {
      await response.body?.cancel().catch(() => {});
      throw new A6Error(`控制面 HTTP ${status}`, { code: 'A6_HTTP', status });
    }
    return { status, text: await readTextCapped(response, maxBytes) };
  };
  // 代理地址（直连优先也要知道代理在哪，才能在直连失败时切过去）。
  const proxyUrl = proxyUrlOf(options.proxyUrl);
  // 代理：CONNECT 隧道 + 手动 TLS。
  const viaProxy = async (budgetMs) => {
    const reply = await requestViaProxy(proxyUrl, url.toString(), {
      method,
      headers,
      body: body === undefined ? undefined : body,
      timeoutMs: budgetMs,
      signal: controller.signal,
    });
    if (reply.status < 200 || reply.status >= 300) {
      throw new A6Error(`控制面 HTTP ${reply.status}`, { code: 'A6_HTTP', status: reply.status });
    }
    return reply;
  };

  let payload;
  try {
    const proxy = proxyUrl;
    const mode = String(options.connectionMode || 'direct-first').trim();
    const directBudget = Math.max(Number(options.directProbeMs) || 1500, 200);
    let status;
    let text;

    // 连接策略：默认直连优先——直连在 directBudget 内连不上就自动切代理，
    // 只有配置文件明确要 proxy-first 时才反过来。
    if (mode === 'direct-only' || (mode === 'direct-first' && !proxy)) {
      ({ status, text } = await direct(timeoutMs));
    } else if (mode === 'proxy-only' || (mode === 'proxy-first' && !proxy)) {
      ({ status, text } = await viaProxy(timeoutMs));
    } else if (mode === 'proxy-first') {
      try { ({ status, text } = await viaProxy(Math.min(timeoutMs, directBudget))); }
      catch { ({ status, text } = await direct(timeoutMs)); }
    } else {
      // direct-first（默认）
      try {
        ({ status, text } = await direct(directBudget));
      } catch (err) {
        if (err?.code === 'A6_ABORTED' || controller.signal.aborted) throw err;
        // HTTP 4xx/5xx 是站点真的回了，不算「连不上」，别绕代理。
        if (err?.code === 'A6_HTTP' || err?.code === 'A6_TOO_LARGE') throw err;
        ({ status, text } = await viaProxy(timeoutMs));
      }
    }
    if (text.length > maxBytes) throw new A6Error('控制面响应过大', { code: 'A6_TOO_LARGE', status });
    if (rawJson) return text;
    try { payload = JSON.parse(text); } catch {
      throw new A6Error('控制面返回了无效 JSON', { code: 'A6_BAD_JSON', status });
    }
    if (!payload || typeof payload !== 'object' || payload.success === false) {
      throw new A6Error('控制面拒绝请求或返回无效数据', { code: 'A6_REJECTED', status });
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
 * 市场页在用的接口：服务端按模型过滤，返回该模型的全部 listing。
 *
 * 旧接口 `/api/marketplace/channels?model_name=X` 实测**忽略** model_name，
 * 每次都返回全站约 15MB / 7455 listing。本接口实测同一模型只有 571KB / 193 条，
 * 快 19 倍、小 26 倍，字段与旧接口逐字段一致。
 *
 * 不带 page/page_size 即为全部条目（实测 193 条）；显式带 sort 也没必要——
 * 插件有自己的打分排序。
 */
export async function fetchChannelSearch({ modelName, pageSize = 500, rawJson = false, ...site }) {
  const query = `/api/marketplace/channels/search?model=${encodeURIComponent(modelName)}`
    + `&page=1&page_size=${Math.max(1, Math.trunc(Number(pageSize) || 500))}`;
  return a6Request({ ...site, path: query, rawJson });
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
  fallbackToSmartRouting = false, timeoutMs, signal, proxyUrl, connectionMode, directProbeMs,
}) {
  return a6Request({
    controlBase,
    path: '/api/marketplace/pin',
    method: 'POST',
    accessToken,
    userId,
    timeoutMs,
    signal,
    proxyUrl, connectionMode, directProbeMs,
    body: {
      token_id: Number(tokenId),
      model_name: modelName,
      channel_id: Number(channelId),
      fallback_to_smart_routing: Boolean(fallbackToSmartRouting),
    },
  });
}

export async function unpinChannel({ controlBase, accessToken, userId, tokenId, modelName, channelId, timeoutMs, signal, proxyUrl, connectionMode, directProbeMs }) {
  return a6Request({
    controlBase,
    path: '/api/marketplace/unpin',
    method: 'POST',
    accessToken,
    userId,
    timeoutMs,
    signal,
    proxyUrl, connectionMode, directProbeMs,
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

export async function tokenPlainKey({ controlBase, accessToken, userId, tokenId, timeoutMs, signal, proxyUrl }) {
  const payload = await a6Request({
    controlBase,
    path: `/api/token/${Number(tokenId)}/key`,
    method: 'POST',
    accessToken,
    userId,
    timeoutMs,
    signal,
    proxyUrl, connectionMode, directProbeMs,
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
