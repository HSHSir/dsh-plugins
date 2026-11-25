/**
 * 从 DSH 的 settings 服务里捞 provider 的 baseURL。
 *
 * 为什么要它：控制面地址得从 API 地址推（https://api.a6api.vip/v1 → https://a6api.vip），
 * 而 API 地址只写在 profile 的 llm-pi-ai 配置里。`ctx.llm.listProviders()` 只给
 * {id, name}，所以走 settings：`describe()` 会把每个条目的解析后取值给出来。
 *
 * 这里不依赖任何具体字段名——顺着对象往下找「带 baseURL 字符串的对象」，
 * 键名就是 provider 名。settings 服务不存在时一律返回空，插件照常用默认站点。
 */

const MAX_DEPTH = 8;

export function findProviderBaseURLs(descriptors) {
  const found = new Map();
  const seen = new Set();

  const visit = (node, depth) => {
    if (!node || typeof node !== 'object' || depth > MAX_DEPTH) return;
    if (seen.has(node)) return;
    seen.add(node);

    if (Array.isArray(node)) {
      for (const item of node) visit(item, depth + 1);
      return;
    }

    for (const [key, value] of Object.entries(node)) {
      if (value && typeof value === 'object' && !Array.isArray(value)) {
        if (typeof value.baseURL === 'string' && value.baseURL.trim()) {
          found.set(String(key), value.baseURL.trim());
        }
        visit(value, depth + 1);
      }
    }
  };

  visit(descriptors, 0);
  return found;
}

/**
 * 造一个「provider → baseURL」的查询函数。
 * @param {object} settings  DSH 的 settings 服务（ctx.settings）
 * @param {number} ttlMs     重新读取的间隔
 */
export function createBaseURLResolver(settings, { ttlMs = 60000, log } = {}) {
  let cache = new Map();
  let readAt = 0;

  function refresh() {
    if (!settings || typeof settings.describe !== 'function') return cache;
    if (Date.now() - readAt < ttlMs && cache.size) return cache;
    try {
      const descriptors = settings.describe();
      const found = findProviderBaseURLs(descriptors);
      if (found.size) cache = found;
      readAt = Date.now();
    } catch (error) {
      log?.(`读 settings 里的 provider 地址失败：${error?.message || error}`);
      readAt = Date.now();
    }
    return cache;
  }

  return function baseURL(provider) {
    const map = refresh();
    return map.get(String(provider)) || '';
  };
}
