/**
 * 「上下文塞不下」这一类失败：换商家没用，得换一条**上下文更大的路由**。
 *
 * 典型现场（用户实测）：
 *   Cannot switch to a6api-a6-1/deepseek-v4.1-flash-1017: context compaction failed:
 *   compaction still above threshold after 2 compaction attempts
 *   (145682 estimated tokens >= threshold 134464).  →  MODEL_SWITCH_COMPACTION_FAILED
 *
 * 134464 = 200000 − 65536：DSH 的压缩阈值 = 模型 contextWindow − 预留输出。
 * 所以处置办法只有两条，插件能做的是第二条：
 *   1. 把那条模型的 contextWindow/maxTokens 配得更贴近实际（改 provider 配置）；
 *   2. 换到 contextWindow 更大的路由（本模块），再让 agent 重跑这一步。
 */

export const CONTEXT_FAILURE_CODES = new Set([
  'MODEL_SWITCH_COMPACTION_FAILED',
  'CONTEXT_WINDOW_EXCEEDED',
  'CONTEXT_OVERFLOW',
  'CONTEXT_LENGTH',
]);

const MESSAGE_PATTERN = /compaction|context window|estimated tokens|too many tokens|上下文|压缩/i;

/** 这个失败是不是「上下文/压缩」类。 */
export function isContextFailure(failure) {
  const code = String(failure?.code || '').toUpperCase();
  if (CONTEXT_FAILURE_CODES.has(code)) return true;
  return MESSAGE_PATTERN.test(String(failure?.message || ''));
}

/**
 * 从失败文案里抠出两个数：估算 token 与压缩阈值。
 * 抠不到就返回 0，调用方按「未知」处理。
 */
export function parseContextNumbers(message) {
  const text = String(message || '');
  const estimated = /(\d{2,})\s*estimated tokens/i.exec(text);
  const threshold = /threshold\s*(\d{2,})/i.exec(text);
  return {
    estimated: estimated ? Number(estimated[1]) : 0,
    threshold: threshold ? Number(threshold[1]) : 0,
  };
}

/**
 * 从候选里挑一条「放得下」的路由：够用的里面选最小的（别为了 150k 上下文去用 1M 的贵货）。
 *
 * @param {Array<{provider: string, model: string, contextWindow?: number, defaultMaxTokens?: number}>} routes
 * @param {{ estimated: number, reserve?: number, headroomRatio?: number, skip?: (route: object) => boolean }} need
 */
export function chooseBiggerRoute(routes, need) {
  const estimated = Math.max(0, Number(need?.estimated) || 0);
  const headroom = Number(need?.headroomRatio) > 0 ? Number(need.headroomRatio) : 1.05;
  const fallbackReserve = Math.max(0, Number(need?.reserve) || 0);
  const target = estimated * headroom;

  const usable = [];
  for (const route of routes || []) {
    if (!route || !route.provider || !route.model) continue;
    if (need?.skip && need.skip(route)) continue;
    const window = Number(route.contextWindow) || 0;
    if (window <= 0) continue;
    const reserve = Number(route.defaultMaxTokens) > 0 ? Number(route.defaultMaxTokens) : fallbackReserve;
    const capacity = window - reserve;
    if (capacity < target) continue;
    usable.push({ ...route, capacity, contextWindow: window, reserve });
  }
  usable.sort((a, b) => a.capacity - b.capacity || a.contextWindow - b.contextWindow);
  return usable[0] || null;
}

/**
 * 用 llm 服务把每条候选路由的真实 contextWindow 解析出来，再挑。
 * @param {object} input
 * @param {{ listProviders?: Function, listModels?: Function, resolveModelInfo?: Function }} input.llm
 * @param {Array<{provider: string, model: string}>} input.routes 想试的路由（按偏好排序）
 */
export async function findContextRoute({ llm, routes, estimated, reserve = 0, headroomRatio = 1.05, signal }) {
  const wanted = [];
  const seen = new Set();
  for (const route of routes || []) {
    if (!route || !route.provider || !route.model) continue;
    const key = `${route.provider}/${route.model}`;
    if (seen.has(key)) continue;
    seen.add(key);
    wanted.push(route);
  }
  if (wanted.length === 0) return { ok: false, reason: '没有候选路由' };

  const enriched = [];
  for (const route of wanted) {
    try {
      const info = typeof llm?.resolveModelInfo === 'function'
        ? await llm.resolveModelInfo(route.provider, route.model, signal)
        : null;
      enriched.push({
        ...route,
        contextWindow: Number(info?.context?.contextWindow) || 0,
        defaultMaxTokens: Number(info?.defaultMaxTokens) || 0,
      });
    } catch {
      enriched.push({ ...route, contextWindow: 0 });
    }
  }

  const picked = chooseBiggerRoute(enriched, { estimated, reserve, headroomRatio });
  if (picked) return { ok: true, route: picked, scanned: enriched.length };
  return {
    ok: false,
    reason: `没有上下文更大的路由（需要约 ${Math.round(estimated * headroomRatio)} tokens）`,
    scanned: enriched.map((row) => ({ provider: row.provider, model: row.model, contextWindow: row.contextWindow })),
  };
}

/** 枚举「当前可用的所有路由」，用作兜底候选（不含自己）。 */
export async function listAllRoutes(llm, { exclude } = {}) {
  const out = [];
  if (!llm || typeof llm.listProviders !== 'function') return out;
  let providers = [];
  try { providers = llm.listProviders() || []; } catch { providers = []; }
  for (const provider of providers) {
    const id = typeof provider === 'string' ? provider : provider?.id;
    if (!id) continue;
    let models = [];
    try { models = (await llm.listModels?.(id)) || []; } catch { models = []; }
    for (const model of models) {
      const modelId = typeof model === 'string' ? model : model?.id;
      if (!modelId) continue;
      if (exclude && exclude.provider === id && exclude.model === modelId) continue;
      out.push({ provider: id, model: modelId });
    }
  }
  return out;
}
