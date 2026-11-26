/**
 * 配置：所有可调项集中在这里，默认值按「用户已有的 a6api 工位」标定。
 *
 * 设计原则：**默认零配置可用**。用户桌面版里已经有两个东西：
 *   1. DSH profile 里的 `llm-pi-ai.providers.a6api-*`（baseURL 指向 api.a6api.vip）
 *   2. 模型管家写下的 `~/.model_probe/routing.json`（A6 控制面的 access_token / user_id）
 * 插件两处都只读不写，所以不填任何 config 也能跑起来。
 */

export const DEFAULTS = Object.freeze({
  enabled: true,

  /** provider 名命中这个正则就当作「中转站路由」，默认吃下 a6api / a6api-a6-1 / a6api-a6-2。 */
  providerPattern: '^a6api',
  /** 额外显式列出的 provider 名（正则失效时兜底）。 */
  providers: [],

  /** 猜控制面时用的默认站点；provider 里拿不到 baseURL 才用它。 */
  defaultControlBase: 'https://a6api.vip',

  /** 逐 provider 覆盖：{ "a6api-a6-1": { controlBase, accessToken, userId, tokenId, tokenName, apiKey } } */
  providerOverrides: {},

  /** 控制面凭据来源。model_probe = 读 ~/.model_probe/routing.json（模型管家的站点凭据）。 */
  credentialSources: ['model_probe'],
  /** 直接给凭据（优先级最高）。 */
  accessToken: '',
  userId: '',

  // ---- 何时判定「响应异常」 ----
  /** 抛错（传输层、HTTP 5xx、限流…）就切换。 */
  switchOnThrown: true,
  /** finish 以 error / aborted 收尾就切换。 */
  switchOnFinishError: true,
  /** 200 但没有一个字正文、也没有工具调用（中转站最常见的静默截断）。 */
  switchOnEmpty: true,
  /** 流没给 finish 就断了（半路掉线）。 */
  switchOnTruncated: true,
  firstResponseTimeoutMs: 55000,
  firstResponseBudgetMs: 60000,
  streamStallTimeoutMs: 30000,
  sameRouteRetries: 1,
  /**
   * 商家模型上下文窗口的下限（token）。低于它的商家不参与选商。
   *
   * 当前设为 290000（用户在用值）：低于 29 万窗口的商家不参与选商。站点**不提供**任何窗口字段
   * （见 selector.js 的说明），所以这个门槛只用「有证据的窗口」：商家自己在描述里写明的，
   * 以及我们自己请求撞过上下文超限学到的上限。没有证据的商家一律放行。
   */
  minContextWindowTokens: 290000,

  // ---- 切换预算（防止在商家之间反复横跳把一轮对话拖死） ----
  /** 单次模型调用最多切换几次商家。 */
  maxSwitchesPerCall: 6,
  /** 滑动一小时内的切换上限。 */
  maxSwitchesPerHour: 50,
  /** 同一路由连续失败几次后进入降级（后续调用直接走备用路由 / 先切商家）。 */
  degradeAfterFailures: 3,
  /** 降级保持时长。 */
  degradeCooldownMs: 180000,
  /**
   * 同一条路由上两次切换的最小间隔：模型调用常常一先一后并发（正文 + 标题），
   * 两边同时判定失败会各切一次、把商家白白换两遍。落在这个窗口里的第二次
   * 失败只重试、不再动控制面。
   */
  switchDedupeMs: 2000,
  /**
   * 控制面连续失败多久之后整体熔断。默认 2 分钟。
   *
   * 站点整体挂掉时（TCP 通、HTTP 全超时），每轮请求都进来试一次切换、失败锁 15 秒、
   * 下一轮再试，失败次数一路堆叠，用户看到「本轮运行失败」反复出现。超过这个时长就
   * 停止打扰控制面，直接把请求交给平台默认路由。0 = 关闭熔断（旧行为）。
   */
  controlUnavailableMs: 120000,
  /** 熔断期间多久探活一次。默认 30 秒，最小 5 秒。 */
  controlProbeMs: 30000,

  // ---- 备用路由：控制面不可用（没网、没凭据、站点改版）时的第二道防线 ----
  /** [{ provider: 'a6api-a6-2', model: 'deepseek-v4.1-flash-1017' }] */
  fallbackRoutes: [],
  /** 路由已降级时，主动先走备用路由（而不是先撞一次再说）。 */
  preferFallbackWhenDegraded: true,

  // ---- 上下文/压缩失败：换商家救不了，只能换上下文更大的路由 ----
  /** 遇到 MODEL_SWITCH_COMPACTION_FAILED / CONTEXT_WINDOW_EXCEEDED 时，自动换到大上下文路由。 */
  autoSwitchOnContextFailure: true,
  /** 选中路由的可用上下文要比估算 token 再宽一点（1.05 = 多留 5%）。 */
  contextHeadroomRatio: 1.05,
  /** 每小时最多因为上下文原因换几次模型（防止来回跳）。 */
  maxContextSwitchesPerHour: 3,
  /** 仅使用显式配置的上下文备用路由和 fallbackRoutes。 */
  contextFallbackRoutes: [],

  // ---- 商家选择 ----
  /** 显式允许平台自主选商家，可能超出插件价格线。 */
  fallbackToSmartRouting: false,
  /**
   * 站点上已有**不是本插件写的**固定商家时，是否接管它。
   *
   * 默认 false：保留别人的 pin（可能是用户在控制台手动固定的，或平台的策略），
   * 本插件这一步直接放弃并报 A6_ROUTE_NOT_READY。
   *
   * 打开后：先按插件门槛选出候选 ——
   *   · 有更合适的候选 → 解除外部固定，改 pin 候选，失败则把原固定恢复回去；
   *   · 只有原来那一家合格 → 直接认领（记进 ownedPins），不为换而换。
   * 代价：插件用完会 unpin，那条外部固定不会自动回来。
   */
  takeOverExistingPin: true,
  /** 渠道列表缓存「新鲜」时长。55 分钟——略小于后台刷新间隔 1 小时。 */
  channelCacheTtlMs: 3300000,
  /**
   * 渠道响应最大字节数，超了就当拿不到（保护内存）。
   * 换用 channels/search 后实测单模型只有 571KB，8MB 足够还留大余量；
   * 旧接口兜底路径理论上是 15MB，所以保留 48MB 不让回退失败。
   */
  maxChannelsBytes: 48 * 1024 * 1024,
  /** 价格倍率上限（只看 input 标价）。默认 1 = 关闭；判定以 maxEffectiveRatio 为准。 */
  maxPriceRatio: 1,
  /**
   * **按实际用量折算后**的商家/官方成本比上限，超过就不要这家。
   * 0.7 = 用户口径「超过官网 70% 直接忽略」。
   */
  maxEffectiveRatio: 0.7,
  /** 实际用量配比：缓存命中率（DSH 长系统提示实测 99%）。 */
  cacheHitRate: 0.95,
  /**
   * 官网侧命中率：官网的缓存按「同一段前缀」跨请求共享，长会话基本全命中，
   * 所以它对标的不是「输入价 × 命中率」，而是「缓存读价」。
   * 不填就沿用 cacheHitRate。
   */
  officialCacheHitRate: 0.95,
  /**
   * 商家侧命中率：null = 用我们自己请求的 usage 实测（推荐）。
   * 中转站大多上游独立账号，实测常常只有 0~5%，绝不能拿官网的命中率替它算。
   * 显式填数字时固定用该值（调试/对账用）。
   */
  merchantCacheHitRate: null,
  /**
   * 还没实测过的商家按什么命中率算成本。默认 0（保守）：
   * 中转站上游多为独立账号/独立池，实测普遍 0~5%，先按「全按输入价」估价，
   * 等我们自己请求出真命中率再替换。填 0.9 就是旧行为（乐观假设，会挑到不缓存的商家）。
   */
  unmeasuredMerchantCacheHitRate: 0,
  /** 实际用量配比：输出 token / 输入 token。 */
  outputToInputRatio: 0.03,
  /** 缓存写入占比，默认 0（DSH 侧不单独计这一档）。 */
  cacheWriteShare: 0,
  /**
   * 控制面请求走的 HTTP 代理，例如 http://127.0.0.1:7897。
   *
   * 为什么必须有：A6API 的域名（main.apisnode.cc）解析到私有地址段 172.80.127.x，
   * 直连必然超时；浏览器能打开是因为走了系统代理。Node 的 fetch 不读系统代理，
   * 所以不配这个就会出现「网页明明能开、插件一直报网络失败」。
   *
   * 留空时依次读环境变量 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY；都没有才直连。
   *
   * 连接策略（connectionMode）：
   *   direct-first（默认）—— 先直连，直连在 directProbeMs 内没连上就自动切代理。
   *                          直连够快就一直用直连，不碰代理。
   *   proxy-first         —— 先代理，失败再试直连（旧行为）。
   *   direct-only / proxy-only —— 只用一种。
   */
  proxyUrl: '',
  connectionMode: 'direct-first',
  /** direct-first 下直连的探活超时，默认 1500ms。够快才不算「连不上」。 */
  directProbeMs: 1500,
  /**
   * 商家表下载超时。换用 channels/search 后实测 571KB / 0.4s，30 秒足够，
   * 站慢留余量；旧接口兜底路径（15MB）可能需要更久，所以下限仍给到 15 秒起。
   */
  channelsTimeoutMs: 30000,
  /**
   * 后台刷新商家表的间隔。默认 1 小时。
   *
   * 中转站模型数据更新不频繁，每小时刷一次足够；只刷 DSH 实际用过的模型。
   * 切换只读缓存、绝不现场拉表，所以新鲜度完全由这个定时器负责。
   */
  channelsRefreshMs: 3600000,
  /** 站点没给缓存价时是否按输入价保守估算，默认拒绝缺价候选。 */
  assumeMissingCacheAsInput: false,
  /**
   * 官网基准价（元 / 百万 token），填了就用它替换站点返回的 official_price。
   *
   * 为什么要这个：站点 official_price 实测比 DeepSeek 官网真实账单低一个数量级
   * （站点给 ¥0.15 输入，真实账单按 98% 命中折算是 ¥1.0 量级）。拿站点价当分母，
   * 所有商家都会显得贵好几倍，门槛会把合格商家误杀。
   *
   * 形如 { input, output, cacheRead }，单位元/百万 token；留空则用站点提供的 official_price。
   * 建议先用真实账单校准：命中率 h、输出占比 c 时，
   *   每百万 = h*cacheRead + (1-h)*input + c*output
   * 让它约等于账单上的「元/百万」。
   */
  officialPriceOverride: null,
  /** 站点 p50 超过这个值直接不要。 */
  hardP50Ms: 10000,
  /** 打分时的延迟参考值：到这个值计 1 倍延迟惩罚，3 倍后封顶。 */
  latencyRefMs: 3000,
  /** 站点失败率上限（百分比）。 */
  maxFailRate: 20,
  /** 打分权重：成本 60 : 可靠性 10 : 延迟 80（用户在用值）。缓存已并入成本，不再单列。 */
  priceWeight: 60,
  reliabilityWeight: 10,
  latencyWeight: 80,
  /** 实测输出/输入比至少要有几次采样才采用（2 次以内噪声太大）。 */
  outputRatioMinSamples: 2,
  /** 关掉就用配置里的固定 outputToInputRatio。 */
  useMeasuredOutputRatio: true,
  /**
   * 旧版「缓存收益权重」。缓存现在直接进成本（真实命中率 × 报价），
   * 再单独计一次等于同一件事罚两遍，所以默认 0、不再参与打分。
   * 收益取自站点 24h 缓存率 cache_hit_rate_bps；没有站点数据时用本地实测命中率。
   */
  cacheWeight: 0,
  /** 预热并发额度：启动预热 + 首次请求同时最多准备几条路由。 */
  prewarmConcurrency: 4,
  /** 首次请求等待选商家的上限（毫秒）。超过就放行原始请求，不再直接报错。 */
  routeWaitMs: 6000,
  /** 路由还没确定时是否先放行（true=可用优先，false=严格等商家确认）。 */
  allowUnpinnedFallback: true,
  /**
   * 已经输出正文后才失败的请求，本轮不能重放（会重复内容），
   * 但可以把这家短期拉黑，让下一次请求换一家。false = 不拉黑。
   */
  badOnPostContentFailure: true,
  /** 上面那种拉黑的有效期。 */
  postContentBadTtlMs: 30 * 60 * 1000,
  /** 旧版权重键，保留兼容，不再参与打分。 */
  successWeight: 0,
  /**
   * 站点与本地均无有效缓存率时，使用相对官方的缓存读成本。沿用既有配置键。
   */
  cacheFallbackDiscount: true,
  /**
   * 用「我们自己请求的 usage」实测商家缓存命中率，样本达到这个数才参与折算。
   * 数据来自每次成功调用的 cacheReadTokens / (未命中输入 + 缓存读 + 缓存写)，
   * 因此它比站点快照更早生效——第一次切换商家之后就开始积累。
   */
  cacheMeasureMinSamples: 3,

  // ---- 观测 ----
  /** 写 ~/.dsh/a6-failover/events.jsonl。 */
  logEvents: true,
  /** 往 stderr 打一行摘要（DSH 日志里能看到）。 */
  verbose: true,
  /** 控制面请求超时。 */
  controlTimeoutMs: 20000,
});

function num(value, fallback, min = 0, max = Number.MAX_SAFE_INTEGER) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, n));
}

function bool(value, fallback) {
  return typeof value === 'boolean' ? value : fallback;
}


function connectionModeOf(value) {
  const text = typeof value === 'string' ? value.trim().toLowerCase() : '';
  return ['direct-first', 'proxy-first', 'direct-only', 'proxy-only'].includes(text) ? text : 'direct-first';
}

/**
 * 代理地址：只接受 http(s):// 开头的合法 URL，其余一律当作「未配置」。
 * 允许 host 带端口；不校验可达性（那是运行时的事）。
 */
function proxyOf(value) {
  const text = typeof value === 'string' ? value.trim() : '';
  if (!text) return '';
  if (!/^https?:\/\//i.test(text)) return '';
  try {
    const url = new URL(text);
    return url.hostname ? text : '';
  } catch { return ''; }
}

/**
 * 官网基准价覆盖：{ input, output, cacheRead }，单位元/百万 token。
 * 只接受正数且至少给了 input；缺项按 0 处理（缓存价常被设成极低值，允许显式写 0）。
 */
function officialOverride(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const pick = (key) => {
    const n = Number(value[key]);
    return Number.isFinite(n) && n >= 0 ? n : null;
  };
  const input = pick('input');
  const output = pick('output');
  const cacheRead = pick('cacheRead');
  if (!(input > 0)) return null;
  return { input, output: output ?? input * 4, cacheRead: cacheRead ?? input * 0.02 };
}

function str(value, fallback = '') {
  return typeof value === 'string' ? value.trim() : fallback;
}

/** 把 config 洗成可用值；坏值一律退回默认，绝不让插件因为一个笔误起不来。 */
export function normalizeConfig(raw = {}) {
  const src = raw && typeof raw === 'object' ? raw : {};
  const routes = Array.isArray(src.fallbackRoutes) ? src.fallbackRoutes : [];
  const overrides = src.providerOverrides && typeof src.providerOverrides === 'object' ? src.providerOverrides : {};
  const pattern = str(src.providerPattern, DEFAULTS.providerPattern) || DEFAULTS.providerPattern;
  let matcher;
  try {
    matcher = new RegExp(pattern);
  } catch {
    matcher = new RegExp(DEFAULTS.providerPattern);
  }
  return {
    enabled: bool(src.enabled, DEFAULTS.enabled),
    providerPattern: pattern,
    matcher,
    providers: Array.isArray(src.providers) ? src.providers.map((v) => String(v)).filter(Boolean) : [],
    defaultControlBase: str(src.defaultControlBase, DEFAULTS.defaultControlBase).replace(/\/+$/, ''),
    providerOverrides: overrides,
    prewarmRoutes: (Array.isArray(src.prewarmRoutes) ? src.prewarmRoutes : [])
      .filter((row) => row && typeof row === 'object')
      .map((row) => ({ provider: str(row.provider), model: str(row.model) }))
      .filter((row) => row.provider && row.model),
    credentialSources: Array.isArray(src.credentialSources)
      ? src.credentialSources.map((v) => String(v))
      : DEFAULTS.credentialSources.slice(),
    accessToken: str(src.accessToken),
    userId: str(src.userId),

    switchOnThrown: bool(src.switchOnThrown, DEFAULTS.switchOnThrown),
    switchOnFinishError: bool(src.switchOnFinishError, DEFAULTS.switchOnFinishError),
    switchOnEmpty: bool(src.switchOnEmpty, DEFAULTS.switchOnEmpty),
    switchOnTruncated: bool(src.switchOnTruncated, DEFAULTS.switchOnTruncated),
    firstResponseTimeoutMs: num(src.firstResponseTimeoutMs, DEFAULTS.firstResponseTimeoutMs, 1, 180000),
    firstResponseBudgetMs: num(src.firstResponseBudgetMs, DEFAULTS.firstResponseBudgetMs, 1, 180000),
    streamStallTimeoutMs: num(src.streamStallTimeoutMs, DEFAULTS.streamStallTimeoutMs, 1, 600000),
    sameRouteRetries: num(src.sameRouteRetries, DEFAULTS.sameRouteRetries, 0, 1),
    minContextWindowTokens: num(src.minContextWindowTokens, DEFAULTS.minContextWindowTokens, 0, 10000000),

    maxSwitchesPerCall: num(src.maxSwitchesPerCall, DEFAULTS.maxSwitchesPerCall, 0, 6),
    maxSwitchesPerHour: num(src.maxSwitchesPerHour, DEFAULTS.maxSwitchesPerHour, 0, 500),
    degradeAfterFailures: num(src.degradeAfterFailures, DEFAULTS.degradeAfterFailures, 1, 50),
    degradeCooldownMs: num(src.degradeCooldownMs, DEFAULTS.degradeCooldownMs, 0, 24 * 3600 * 1000),
    switchDedupeMs: num(src.switchDedupeMs, DEFAULTS.switchDedupeMs, 0, 60000),
    controlUnavailableMs: num(src.controlUnavailableMs, DEFAULTS.controlUnavailableMs, 0, 3600000),
    controlProbeMs: num(src.controlProbeMs, DEFAULTS.controlProbeMs, 5000, 600000),

    fallbackRoutes: routes
      .filter((row) => row && typeof row === 'object')
      .map((row) => ({ provider: str(row.provider), model: str(row.model) }))
      .filter((row) => row.provider && row.model),
    preferFallbackWhenDegraded: bool(src.preferFallbackWhenDegraded, DEFAULTS.preferFallbackWhenDegraded),
    autoSwitchOnContextFailure: bool(src.autoSwitchOnContextFailure, DEFAULTS.autoSwitchOnContextFailure),
    contextHeadroomRatio: num(src.contextHeadroomRatio, DEFAULTS.contextHeadroomRatio, 1, 4),
    maxContextSwitchesPerHour: num(src.maxContextSwitchesPerHour, DEFAULTS.maxContextSwitchesPerHour, 0, 100),
    contextFallbackRoutes: (Array.isArray(src.contextFallbackRoutes) ? src.contextFallbackRoutes : [])
      .filter((row) => row && typeof row === 'object')
      .map((row) => ({ provider: str(row.provider), model: str(row.model) }))
      .filter((row) => row.provider && row.model),

    fallbackToSmartRouting: bool(src.fallbackToSmartRouting, DEFAULTS.fallbackToSmartRouting),
    takeOverExistingPin: bool(src.takeOverExistingPin, DEFAULTS.takeOverExistingPin),
    channelCacheTtlMs: num(src.channelCacheTtlMs, DEFAULTS.channelCacheTtlMs, 0),
    maxChannelsBytes: num(src.maxChannelsBytes, DEFAULTS.maxChannelsBytes, 1024),
    maxPriceRatio: num(src.maxPriceRatio, DEFAULTS.maxPriceRatio, 0),
    maxEffectiveRatio: num(src.maxEffectiveRatio, DEFAULTS.maxEffectiveRatio, 0, 100),
    cacheHitRate: num(src.cacheHitRate, DEFAULTS.cacheHitRate, 0, 1),
    officialCacheHitRate: num(src.officialCacheHitRate, num(src.cacheHitRate, DEFAULTS.cacheHitRate, 0, 1), 0, 1),
    // null = 用实测；填数字则固定该值
    merchantCacheHitRate: (() => {
      const raw = src.merchantCacheHitRate;
      if (raw === null || raw === undefined || raw === '') return null;
      const n = Number(raw);
      return Number.isFinite(n) ? Math.min(1, Math.max(0, n)) : null;
    })(),
    unmeasuredMerchantCacheHitRate: num(src.unmeasuredMerchantCacheHitRate, DEFAULTS.unmeasuredMerchantCacheHitRate, 0, 1),
    outputToInputRatio: num(src.outputToInputRatio, DEFAULTS.outputToInputRatio, 0, 100),
    cacheWriteShare: num(src.cacheWriteShare, DEFAULTS.cacheWriteShare, 0, 1),
    assumeMissingCacheAsInput: bool(src.assumeMissingCacheAsInput, DEFAULTS.assumeMissingCacheAsInput),
    proxyUrl: proxyOf(src.proxyUrl),
    connectionMode: connectionModeOf(src.connectionMode),
    directProbeMs: num(src.directProbeMs, DEFAULTS.directProbeMs, 200, 30000),
    channelsTimeoutMs: num(src.channelsTimeoutMs, DEFAULTS.channelsTimeoutMs, 5000, 600000),
    channelsRefreshMs: num(src.channelsRefreshMs, DEFAULTS.channelsRefreshMs, 60000, 86400000),
    officialPriceOverride: officialOverride(src.officialPriceOverride),
    hardP50Ms: num(src.hardP50Ms, DEFAULTS.hardP50Ms, 1),
    latencyRefMs: num(src.latencyRefMs, DEFAULTS.latencyRefMs, 1),
    maxFailRate: num(src.maxFailRate, DEFAULTS.maxFailRate, 0, 100),
    priceWeight: num(src.priceWeight, DEFAULTS.priceWeight, 0),
    reliabilityWeight: num(src.reliabilityWeight, DEFAULTS.reliabilityWeight, 0),
    outputRatioMinSamples: num(src.outputRatioMinSamples, DEFAULTS.outputRatioMinSamples, 1),
    prewarmConcurrency: num(src.prewarmConcurrency, DEFAULTS.prewarmConcurrency, 1, 16),
    routeWaitMs: num(src.routeWaitMs, DEFAULTS.routeWaitMs, 500, 60000),
    allowUnpinnedFallback: bool(src.allowUnpinnedFallback, DEFAULTS.allowUnpinnedFallback),
    badOnPostContentFailure: bool(src.badOnPostContentFailure, DEFAULTS.badOnPostContentFailure),
    postContentBadTtlMs: num(src.postContentBadTtlMs, DEFAULTS.postContentBadTtlMs, 0),
    useMeasuredOutputRatio: bool(src.useMeasuredOutputRatio, DEFAULTS.useMeasuredOutputRatio),
    cacheWeight: num(src.cacheWeight, DEFAULTS.cacheWeight, 0),
    cacheFallbackDiscount: bool(src.cacheFallbackDiscount, DEFAULTS.cacheFallbackDiscount),
    cacheMeasureMinSamples: num(src.cacheMeasureMinSamples, DEFAULTS.cacheMeasureMinSamples, 1, 500),
    latencyWeight: num(src.latencyWeight, DEFAULTS.latencyWeight, 0),
    successWeight: num(src.successWeight, DEFAULTS.successWeight, 0),

    logEvents: bool(src.logEvents, DEFAULTS.logEvents),
    verbose: bool(src.verbose, DEFAULTS.verbose),
    controlTimeoutMs: num(src.controlTimeoutMs, DEFAULTS.controlTimeoutMs, 1000, 180000),
  };
}

/** 这个 provider 是不是要去管的中转站路由。 */
export function isManagedProvider(cfg, provider) {
  const name = String(provider || '');
  if (!name) return false;
  if (cfg.providers.includes(name)) return true;
  return cfg.matcher.test(name);
}

/** 设置页用的默认值快照（JSON 安全，不含正则与派生态）。 */
export function defaultsForForm() {
  const d = DEFAULTS;
  return {
    firstResponseTimeoutMs: d.firstResponseTimeoutMs,
    firstResponseBudgetMs: d.firstResponseBudgetMs,
    streamStallTimeoutMs: d.streamStallTimeoutMs,
    sameRouteRetries: d.sameRouteRetries,
    minContextWindowTokens: d.minContextWindowTokens,
    enabled: d.enabled,
    providerPattern: d.providerPattern,
    providers: [],
    defaultControlBase: d.defaultControlBase,
    credentialSources: d.credentialSources.slice(),
    switchOnThrown: d.switchOnThrown,
    switchOnFinishError: d.switchOnFinishError,
    switchOnEmpty: d.switchOnEmpty,
    switchOnTruncated: d.switchOnTruncated,
    maxSwitchesPerCall: d.maxSwitchesPerCall,
    maxSwitchesPerHour: d.maxSwitchesPerHour,
    degradeAfterFailures: d.degradeAfterFailures,
    degradeCooldownMs: d.degradeCooldownMs,
    switchDedupeMs: d.switchDedupeMs,
    controlUnavailableMs: d.controlUnavailableMs,
    controlProbeMs: d.controlProbeMs,
    preferFallbackWhenDegraded: d.preferFallbackWhenDegraded,
    autoSwitchOnContextFailure: d.autoSwitchOnContextFailure,
    contextHeadroomRatio: d.contextHeadroomRatio,
    maxContextSwitchesPerHour: d.maxContextSwitchesPerHour,
    fallbackToSmartRouting: d.fallbackToSmartRouting,
    takeOverExistingPin: d.takeOverExistingPin,
    channelCacheTtlMs: d.channelCacheTtlMs,
    maxEffectiveRatio: d.maxEffectiveRatio,
    cacheHitRate: d.cacheHitRate,
    officialCacheHitRate: d.officialCacheHitRate,
    merchantCacheHitRate: d.merchantCacheHitRate,
    unmeasuredMerchantCacheHitRate: d.unmeasuredMerchantCacheHitRate,
    outputToInputRatio: d.outputToInputRatio,
    cacheWriteShare: d.cacheWriteShare,
    assumeMissingCacheAsInput: d.assumeMissingCacheAsInput,
    proxyUrl: d.proxyUrl,
    connectionMode: d.connectionMode,
    directProbeMs: d.directProbeMs,
    channelsTimeoutMs: d.channelsTimeoutMs,
    channelsRefreshMs: d.channelsRefreshMs,
    officialPriceOverride: d.officialPriceOverride,
    maxPriceRatio: d.maxPriceRatio,
    hardP50Ms: d.hardP50Ms,
    latencyRefMs: d.latencyRefMs,
    maxFailRate: d.maxFailRate,
    priceWeight: d.priceWeight,
    reliabilityWeight: d.reliabilityWeight,
    outputRatioMinSamples: d.outputRatioMinSamples,
    prewarmConcurrency: d.prewarmConcurrency,
    routeWaitMs: d.routeWaitMs,
    allowUnpinnedFallback: d.allowUnpinnedFallback,
    badOnPostContentFailure: d.badOnPostContentFailure,
    postContentBadTtlMs: d.postContentBadTtlMs,
    useMeasuredOutputRatio: d.useMeasuredOutputRatio,
    cacheWeight: d.cacheWeight,
    cacheFallbackDiscount: d.cacheFallbackDiscount,
    cacheMeasureMinSamples: d.cacheMeasureMinSamples,
    latencyWeight: d.latencyWeight,
    successWeight: d.successWeight,
    logEvents: d.logEvents,
    verbose: d.verbose,
    controlTimeoutMs: d.controlTimeoutMs,
  };
}

/** 从 API baseURL 猜控制面地址：https://api.a6api.vip/v1 → https://a6api.vip */
export function guessControlBase(apiBase) {
  const text = String(apiBase || '').trim();
  if (!text) return '';
  let url;
  try {
    url = new URL(text.includes('://') ? text : `https://${text}`);
  } catch {
    return '';
  }
  let host = url.hostname;
  if (!host || !host.includes('.')) return '';
  if (host.startsWith('api.') && host.split('.').length >= 3) host = host.slice(4);
  const port = url.port ? `:${url.port}` : '';
  // 协议要跟着原样走：自建反代常挂在 http 上，一律抬成 https 会连不上
  return `${url.protocol}//${host}${port}`;
}
