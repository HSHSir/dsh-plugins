// dsh-a6-failover 的浏览器半侧：在「设置」里加一栏，能看状态、改参数、盯价格表。
// 格式必须是客户端模块系统的 lazy-CJS factory（和 dsh-computer-use-win / dsh-purge 一样）：
// 向页面的模块加载器登记包名与 factory(require)。没有打包器，全部手写。
window.__ModuleLoader__.load({
  id: 'dsh-a6-failover',
  factory: (require) => {
    const React = require('react');
    const h = React.createElement;

    const NS = 'a6-failover.settings';
    const REFRESH_MS = 10000;

    /** 页面可能挂在子路径下：把 /a6-failover/... 解析成绝对 URL。 */
    function apiUrl(pathname) {
      try {
        const base = new URL(document.baseURI || window.location.href);
        base.search = '';
        base.hash = '';
        return new URL(pathname.replace(/^\//, ''), base).toString();
      } catch {
        return pathname;
      }
    }

    async function getJson(pathname, init) {
      const response = await fetch(apiUrl(pathname), {
        ...init,
        headers: { accept: 'application/json', 'x-a6-failover': 'settings', ...(init?.body ? { 'content-type': 'application/json' } : {}) },
      });
      const text = await response.text();
      let payload;
      try { payload = text ? JSON.parse(text) : {}; } catch { payload = { ok: false, error: `返回的不是 JSON：${text.slice(0, 120)}` }; }
      if (!response.ok && payload.ok !== false) payload = { ok: false, error: `HTTP ${response.status}` };
      return payload;
    }

    const pct = (value, digits = 1) => (
      typeof value === 'number' && Number.isFinite(value) ? `${(value * 100).toFixed(digits)}%` : '—'
    );
    const money = (value) => (typeof value === 'number' && Number.isFinite(value) ? `¥${value.toFixed(3)}` : '—');
    const clock = (ms) => {
      if (!ms) return '—';
      const d = new Date(ms);
      return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
    };

    // ---- 样式 ----
    const S = {
      wrap: { display: 'flex', flexDirection: 'column', gap: 14, fontSize: 13, lineHeight: 1.6 },
      row: { display: 'flex', gap: 14, flexWrap: 'wrap' },
      card: { flex: '1 1 260px', minWidth: 240, border: '1px solid rgba(128,128,128,.28)', borderRadius: 10, padding: '12px 14px' },
      cardTitle: { fontSize: 12, opacity: 0.65, marginBottom: 8, letterSpacing: '.04em' },
      kv: { display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '4px 12px', alignItems: 'baseline' },
      key: { opacity: 0.6, whiteSpace: 'nowrap' },
      val: { fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace', wordBreak: 'break-all' },
      dot: (ok) => ({ display: 'inline-block', width: 8, height: 8, borderRadius: 4, marginRight: 6, background: ok ? '#2fbf71' : '#c0562b' }),
      table: { width: '100%', borderCollapse: 'collapse', fontSize: 12 },
      th: { textAlign: 'left', padding: '4px 6px', opacity: 0.6, fontWeight: 500, borderBottom: '1px solid rgba(128,128,128,.25)' },
      td: { padding: '4px 6px', borderBottom: '1px solid rgba(128,128,128,.12)', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace' },
      btn: { border: '1px solid rgba(128,128,128,.4)', background: 'transparent', color: 'inherit', borderRadius: 6, padding: '3px 10px', cursor: 'pointer', fontSize: 12 },
      btnPrimary: { border: '1px solid rgba(120,170,255,.7)', background: 'rgba(120,170,255,.16)', color: 'inherit', borderRadius: 6, padding: '4px 14px', cursor: 'pointer', fontSize: 12 },
      hint: { opacity: 0.6, fontSize: 12 },
      err: { color: '#d4574a', fontSize: 12 },
      ok: { color: '#2fbf71', fontSize: 12 },
      fieldGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))', gap: '10px 16px' },
      field: { display: 'flex', flexDirection: 'column', gap: 3 },
      label: { fontSize: 12, opacity: 0.75 },
      input: {
        background: 'rgba(128,128,128,.10)', border: '1px solid rgba(128,128,128,.35)', borderRadius: 6,
        padding: '5px 8px', color: 'inherit', fontSize: 12, fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      },
      summary: { cursor: 'pointer', fontWeight: 600, marginBottom: 10 },
    };

    function KV({ rows }) {
      return h('div', { style: S.kv }, rows.flatMap(([key, value], index) => [
        h('div', { key: `k${index}`, style: S.key }, key),
        h('div', { key: `v${index}`, style: S.val }, value),
      ]));
    }

    function Card({ title, children }) {
      return h('div', { style: S.card }, h('div', { style: S.cardTitle }, title), children);
    }

    function eventLine(event) {
      if (event.type === 'boot') return `启用（${event.providerPattern}）`;
      if (event.type === 'delivery-review') return `交付待核对 · 第 ${event.turn ?? '—'} 回合的最终回复只提到后续步骤，本轮未观察到文档交付。核对编号 ${event.requestRef || '—'}`;
      if (event.type === 'model-failure') return `异常 ${event.kind || ''} ${event.code || ''} @${event.provider}/${event.model}`;
      if (event.type === 'switched') return `换商家 → ${event.channelName || event.channelId || event.mode}`;
      if (event.type === 'pin') return `固定 ${event.channelName || event.channelId}（${pct(event.effRatio)} 官网价）`;
      if (event.type === 'unpin') return `撤销固定 ${event.channelId}`;
      if (event.type === 'switch-deduped' || event.type === 'switch-shared') return `并发合并（沿用 ${event.channelId}）`;
      if (event.type === 'step-retry') return `回合重跑（${event.mode}）`;
      if (event.type === 'config-saved') return `设置已保存（${(event.keys || []).length} 项）`;
      if (event.type === 'config-cleared') return '本地设置已清除';
      if (event.type === 'context-switched') {
        const to = event.to || {};
        return `上下文塞不下 → 换到大窗口路由 ${to.provider}/${to.model}（估算 ${event.estimated}）`;
      }
      if (event.type === 'context-switch-unavailable') return `上下文塞不下，但没有更大窗口可用：${event.reason || ''}`;
      if (event.type === 'context-switch-rate-limited') return '上下文换模型已达每小时上限';
      return event.type;
    }

    // ---- 字段定义（分组；label / 单位 / 步长） ----
    const GROUPS = [
  {
    "title": "1 · 门槛",
    "open": true,
    "description": "先淘汰不符合条件的商家，只有全部通过才进入打分。门槛比较的是这家自己的真实成本（报价 × 它自己的命中率）；还没实测过的商家按标价判定，不会被一次性打死。",
    "fields": [
      {
        "key": "maxEffectiveRatio",
        "label": "实际成本 / 官网成本上限",
        "type": "number",
        "hint": "0.7 表示最多为官网成本的 70%。有本机实测命中率时按真实成本判，否则按标价倍率判。",
        "step": 0.05,
        "min": 0
      },
      {
        "key": "proxyUrl",
        "label": "HTTP 代理地址",
        "type": "text",
        "hint": "例如 http://127.0.0.1:7897。A6API 的域名解析到私有地址段（172.80.127.x），不走代理时插件连不上控制面。留空则读环境变量 HTTPS_PROXY / HTTP_PROXY / ALL_PROXY。"
      },
      {
        "key": "connectionMode",
        "label": "连接方式",
        "type": "select",
        "options": [
          { "value": "direct-first", "label": "直连优先（不行切代理，推荐）" },
          { "value": "proxy-first", "label": "代理优先（不行切直连）" },
          { "value": "direct-only", "label": "只直连" },
          { "value": "proxy-only", "label": "只走代理" }
        ],
        "hint": "默认直连优先：直连在下面的探活超时内连不上，才自动切代理。"
      },
      {
        "key": "directProbeMs",
        "label": "直连探活超时（毫秒）",
        "type": "number",
        "step": 500,
        "min": 200,
        "hint": "默认 1500ms。直连超过这个时间没连上就判定不通，立即切代理。"
      },
      {
        "key": "channelsRefreshMs",
        "label": "后台刷新商家表间隔（分钟）",
        "type": "number",
        "scale": 60000,
        "step": 5,
        "hint": "默认 30 分钟。商家表约 25 MB，切换本身只读缓存、不会现场拉表，所以这张表的新鲜度由这里决定。"
      },
      {
        "key": "officialPriceOverride",
        "label": "官网基准价（元/百万 token）",
        "type": "price3",
        "hint": "留空则用站点返回的 official_price。站点这个价实测比 DeepSeek 真实账单低一个数量级，会把合格商家判成贵的；填成你账单上的口径（默认 98% 命中、0.5% 输出占比时约 5.96 元/亿）。"
      },
      {
        "key": "maxPriceRatio",
        "label": "额外限制输入单价（可选）",
        "type": "number",
        "hint": "1 表示关闭。0.5 表示输入单价最多为官网一半；不考虑缓存优惠。通常保持 1，仅使用实际成本门槛。",
        "step": 0.05,
        "min": 0
      },
      {
        "key": "hardP50Ms",
        "label": "p50 延迟上限（毫秒）",
        "type": "number",
        "hint": "只用于入选：p50 超过上限的商家直接淘汰，且在候选过半时优先保留 p50 更低的那一半。p50 本身不参与打分。",
        "step": 500,
        "min": 0
      },
      {
        "key": "maxFailRate",
        "label": "失败率上限（%）",
        "type": "number",
        "hint": "使用站点成功率换算的失败率。",
        "step": 1,
        "min": 0
      },
      {
        "key": "minSamples",
        "label": "商家统计至少需要几次请求",
        "type": "number",
        "hint": "站点统计请求数低于此值时不选该商家。10 表示至少有 10 次记录；0 关闭此项，可能选到数据不足的新商家。",
        "step": 10,
        "min": 0
      },
      {
        "key": "cacheHitRate",
        "label": "成本估算 · 缓存命中率",
        "type": "number",
        "hint": "0～1；是估算用量，不是插件实时测得的命中率。",
        "step": 0.01,
        "min": 0
      },
      {
        "key": "outputToInputRatio",
        "label": "成本估算 · 输出 / 输入 token",
        "type": "number",
        "hint": "例如 0.03 表示输出约占输入的 3%。",
        "step": 0.001,
        "min": 0
      },
      {
        "key": "cacheWriteShare",
        "label": "成本估算 · 缓存写入 / 输入 token",
        "type": "number",
        "hint": "仅站点提供缓存写入价时影响成本；通常设为 0。",
        "step": 0.01,
        "min": 0
      },
      {
        "key": "assumeMissingCacheAsInput",
        "label": "缺少缓存读取价时按输入价估算",
        "type": "bool",
        "hint": "关闭时缺失或零缓存价按 0 计算。"
      }
    ],
    "note": "固定门槛：模型匹配、渠道未禁用、价格有效、排除本次调用失败商家及已知坏商家。"
  },
  {
    "title": "2 · 打分",
    "open": true,
    "description": "分数越低越优先。成本、可靠性、延迟的默认权重为 60 / 30 / 10。权重按总和折算，无需凑成 100；全部为 0 时所有商家同分。p50 只在门槛阶段使用，不参与打分。",
    "fields": [
      {
        "key": "priceWeight",
        "label": "成本权重",
        "type": "number",
        "hint": "按实际成本倍率（商家报价 × 它自己的命中率 × 实测输出比）的对数刻度计分。",
        "step": 5,
        "min": 0
      },
      {
        "key": "reliabilityWeight",
        "label": "可靠性权重",
        "type": "number",
        "hint": "失败率越高，总分越高，排名越靠后。优先使用平滑后的实测失败率，缺少实测时使用站点失败率。",
        "step": 5,
        "min": 0
      },
      {
        "key": "latencyWeight",
        "label": "延迟权重",
        "type": "number",
        "hint": "用站点「延迟」列（最近一次真实响应耗时），越低越好；没测过才退回 p50 并标明来源。p50 本身不参与打分。",
        "step": 5,
        "min": 0
      },
      {
        "key": "latencyRefMs",
        "label": "延迟参考值（毫秒）",
        "type": "number",
        "hint": "达到参考值记 1 倍延迟惩罚；达到 3 倍后封顶。",
        "step": 500,
        "min": 0
      }
    ],
    "note": "缓存命中率已计入成本估算。保存权重后，点「重新拉取」查看更新后的排名。"
  },
  {
    "title": "3 · 切换与等待",
    "open": false,
    "description": "等待时间按每次模型请求计算；重试、换商和等待新商家首块共享总预算。控制面整体不可用时会自动降级到平台默认路由，不让整轮对话失败。",
    "fields": [
      {
        "key": "controlUnavailableMs",
        "label": "控制面连续无响应多久后降级（秒）",
        "hint": "默认 120 秒。站点整体挂掉时，超过这个时长就停止打扰控制面，把这轮请求直接交给平台默认路由。0 = 关闭（旧行为，会一直重试并反复报错）。",
        "type": "number",
        "scale": 1000,
        "step": 10,
        "min": 0
      },
      {
        "key": "controlProbeMs",
        "label": "降级后探活间隔（秒）",
        "hint": "默认 30 秒。降级期间只读探活，一旦通了立刻恢复智能选商。",
        "type": "number",
        "scale": 1000,
        "step": 5,
        "min": 5
      },
      {
        "key": "firstResponseTimeoutMs",
        "label": "原路无响应多久开始换商（秒）",
        "hint": "默认 30 秒；只统计有效首块，心跳不算。超过总预算时以总预算为准。",
        "type": "number",
        "scale": 1000,
        "step": 1
      },
      {
        "key": "firstResponseBudgetMs",
        "label": "首块等待总预算（秒）",
        "hint": "默认 180 秒，最多 180 秒。包含原路重试、换商和新商家响应；并非整个任务时长。",
        "type": "number",
        "scale": 1000,
        "step": 1
      },
      {
        "key": "streamStallTimeoutMs",
        "label": "输出开始后的停顿上限（秒）",
        "hint": "默认 30 秒。已有内容后连续无进展达到此值则结束，不重播已输出内容。",
        "type": "number",
        "scale": 1000,
        "step": 1
      },
      {
        "key": "sameRouteRetries",
        "label": "换商前原路重试次数",
        "hint": "0 不重试，1 重试一次。只用于可恢复的快速失败，不能延长等待预算。",
        "type": "number",
        "step": 1
      },
      {
        "key": "switchOnThrown",
        "label": "传输异常或请求抛错时切换",
        "type": "bool",
        "hint": ""
      },
      {
        "key": "switchOnFinishError",
        "label": "流以错误或异常中止结束时切换",
        "type": "bool",
        "hint": ""
      },
      {
        "key": "switchOnEmpty",
        "label": "无正文且无工具调用时切换",
        "type": "bool",
        "hint": ""
      },
      {
        "key": "switchOnTruncated",
        "label": "流结束但缺少 finish 时切换",
        "type": "bool",
        "hint": ""
      },
      {
        "key": "maxSwitchesPerCall",
        "label": "单次流调用最多切换次数",
        "type": "number",
        "hint": "范围 0～6；已输出有效内容后不再自动重播。",
        "step": 1,
        "min": 0
      },
      {
        "key": "maxSwitchesPerHour",
        "label": "每小时商家切换预算",
        "type": "number",
        "hint": "滑动一小时；耗尽后仍可尝试备用路由。",
        "step": 1,
        "min": 0
      },
      {
        "key": "takeOverExistingPin",
        "label": "接管站点已有的固定商家",
        "type": "bool",
        "hint": "关闭时：站点上若已有非本插件设置的固定商家，插件保留它并停止本次选商（报 A6_ROUTE_NOT_READY）。开启后：选出更合适的商家就解除原固定并改绑，失败会还原；原来那家最合适时直接沿用，不改站点设置。注意：被解除的那条外部固定不会自动恢复。"
      },
      {
        "key": "fallbackRoutes",
        "label": "备用路由（按顺序）",
        "type": "routes",
        "hint": "每行一个 provider/model；模型名称中可以包含 /。留空表示不使用备用路由。"
      },
      {
        "key": "autoSwitchOnContextFailure",
        "label": "上下文不足时自动换更大窗口路由",
        "type": "bool",
        "hint": "遇到明确的上下文错误时，只从已配置的备用路由中选择。"
      },
      {
        "key": "contextHeadroomRatio",
        "label": "上下文余量倍数",
        "type": "number",
        "hint": "1.05 = 在估算 token 之外多留 5%。",
        "step": 0.05,
        "min": 0
      },
      {
        "key": "maxContextSwitchesPerHour",
        "label": "上下文换路由每小时上限",
        "type": "number",
        "hint": "",
        "step": 1,
        "min": 0
      }
    ],
    "note": "用户取消不会触发切换；鉴权、配额等永久性错误会被拦截。上下文换路由使用独立预算。"
  },
  {
    "title": "4 · 其它",
    "open": false,
    "description": "插件启用、站点接入、显示与维护设置。",
    "fields": [
      {
        "key": "enabled",
        "label": "启用自动切换",
        "type": "bool",
        "hint": ""
      },
      {
        "key": "providerPattern",
        "label": "接管的 provider（正则）",
        "type": "text",
        "hint": "默认 ^a6api。"
      },
      {
        "key": "providers",
        "label": "额外接管的 provider",
        "type": "list",
        "hint": "以逗号分隔。"
      },
      {
        "key": "defaultControlBase",
        "label": "默认控制面地址",
        "type": "text",
        "hint": "无法从 provider 地址推导时使用，例如 https://a6api.vip。"
      },
      {
        "key": "accessToken",
        "label": "系统访问令牌",
        "type": "secret",
        "hint": "留空不修改；未填写时尝试读取模型管家凭据。"
      },
      {
        "key": "userId",
        "label": "站点用户 ID",
        "type": "text",
        "hint": ""
      },
      {
        "key": "fxCnyPerUsd",
        "label": "人民币 / 美元汇率",
        "type": "number",
        "hint": "仅影响人民币显示，不影响门槛、打分或排序。",
        "step": 0.1,
        "min": 0
      },
      {
        "key": "channelCacheTtlMs",
        "label": "商家候选缓存有效期（毫秒）",
        "type": "number",
        "hint": "过期后在下次选商时拉取；不会定时自动拉取。",
        "step": 60000,
        "min": 0
      },
      {
        "key": "controlTimeoutMs",
        "label": "控制面请求超时（毫秒）",
        "type": "number",
        "hint": "拉取商家表的超时至少为 30000 毫秒。",
        "step": 1000,
        "min": 0
      },
      {
        "key": "verbose",
        "label": "在 DSH 日志输出摘要",
        "type": "bool",
        "hint": ""
      },
      {
        "key": "logEvents",
        "label": "写入事件日志文件",
        "type": "bool",
        "hint": ""
      }
    ]
  }
];

    const LIMITS = {
      firstResponseTimeoutMs: [1000, 180000], firstResponseBudgetMs: [1000, 180000], streamStallTimeoutMs: [1000, 600000], sameRouteRetries: [0, 1],
      maxSwitchesPerCall: [0, 6], maxSwitchesPerHour: [0, 500],
      degradeAfterFailures: [1, 50], degradeCooldownMs: [0, 86400000], switchDedupeMs: [0, 60000],
      contextHeadroomRatio: [1, 4], maxContextSwitchesPerHour: [0, 100],
      maxEffectiveRatio: [0, 100], cacheHitRate: [0, 1], outputToInputRatio: [0, 100],
      cacheWriteShare: [0, 1], fxCnyPerUsd: [0.01, 100], hardP50Ms: [1, Number.MAX_SAFE_INTEGER],
      latencyRefMs: [1, Number.MAX_SAFE_INTEGER], maxFailRate: [0, 100],
      controlTimeoutMs: [1000, 180000],
      controlUnavailableMs: [0, 3600000], controlProbeMs: [5000, 600000],
    };
    for (const group of GROUPS) for (const field of group.fields) {
      if (field.type !== 'number') continue;
      [field.min, field.max] = LIMITS[field.key] || [0, Number.MAX_SAFE_INTEGER];
    }
    const ALL_FIELDS = GROUPS.flatMap((group) => group.fields.map((field) => ({ ...field, group: group.title })));

    function Field({ field, value, onChange, secretSet }) {
      const id = `a6f-${field.key}`;
      const common = { id, style: S.input };
      let control;
      if (field.type === 'bool') {
        control = h('input', {
          id,
          type: 'checkbox',
          checked: value === true,
          onChange: (event) => onChange(event.target.checked),
        });
      } else if (field.type === 'select') {
        control = h('select', { ...common, value: value || field.options?.[0]?.value || '',
          onChange: (event) => onChange(event.target.value) },
          ...(field.options || []).map((opt) => h('option', { key: opt.value, value: opt.value }, opt.label)));
      } else if (field.type === 'routes') {
        control = h('textarea', { ...common, rows: 3, value: Array.isArray(value) ? value.map((r) => `${r.provider}/${r.model}`).join('\n') : (value ?? ''), onChange: (event) => onChange(event.target.value) });
      } else if (field.type === 'price3') {
        // 三个数字塞在一行：输入 / 输出 / 缓存读，单位元每百万 token。
        const cur = value && typeof value === 'object' ? value : {};
        const sub = (key, label, scale) => h('label', { style: S.field, htmlFor: `${id}-${key}` },
          h('span', { style: S.label }, label),
          h('input', {
            id: `${id}-${key}`,
            style: S.input,
            type: 'number',
            step: key === 'cacheRead' ? 0.001 : 0.1,
            min: 0,
            value: cur[key] == null ? '' : cur[key],
            onChange: (event) => {
              const next = { ...cur };
              if (event.target.value === '') delete next[key];
              else next[key] = Number(event.target.value) / scale;
              onChange(Object.keys(next).length ? next : null);
            },
          }));
        control = h('div', { style: { display: 'grid', gap: 4 } },
          sub('input', '输入', field.scale || 1),
          sub('output', '输出', field.scale || 1),
          sub('cacheRead', '缓存读', field.scale || 1));
      } else if (field.type === 'secret') {
        control = h('input', {
          ...common,
          type: 'password',
          autoComplete: 'off',
          placeholder: secretSet ? '已设置（留空不改）' : '未设置',
          value: value ?? '',
          onChange: (event) => onChange(event.target.value),
        });
      } else if (field.type === 'number') {
        control = h('input', {
          ...common,
          type: 'number',
          step: field.step || 1,
          min: field.min / (field.scale || 1),
          max: field.max / (field.scale || 1),
          value: value === '' || value == null ? '' : Number(value) / (field.scale || 1),
          onChange: (event) => onChange(event.target.value === '' ? '' : Number(event.target.value) * (field.scale || 1)),
        });
      } else {
        control = h('input', {
          ...common,
          type: 'text',
          value: value ?? '',
          onChange: (event) => onChange(event.target.value),
        });
      }
      return h('label', { style: S.field, htmlFor: id },
        h('span', { style: S.label }, field.label),
        control,
        field.hint ? h('span', { style: S.hint }, field.hint) : null);
    }

    const modeNames = { platform: '平台默认策略', auto: '智能路由（故障接管）', manual: '智能路由（手动）', restoring: '待恢复平台默认', 'awaiting-request': '等待该模型的新请求', unknown: '策略待确认' };

    function Strategy({ strategy, version, enabled, reload }) {
      const [pending, setPending] = React.useState('');
      const [error, setError] = React.useState('');
      const routes = strategy?.routes || [];
      const change = async (mode) => {
        setPending(mode);
        setError('');
        try {
          const result = await getJson('/a6-failover/strategy', { method: 'POST', body: JSON.stringify({ mode }) });
          if (result.ok === false) throw new Error(result.error || '策略操作失败');
        } catch (err) { setError(err.message || '策略操作失败'); }
        finally { await reload(); setPending(''); }
      };
      const legacy = !strategy && version === '0.5.1';
      const restoring = strategy?.phase === 'restoring';
      const smart = strategy?.manual || strategy?.mode === 'smart';
      const target = smart || restoring ? 'platform' : 'smart';
      const label = !enabled ? '插件已停用' : legacy ? '旧版策略 · 优先固定商家'
        : !strategy ? '当前宿主未提供策略状态' : restoring ? '正在恢复平台默认策略'
        : strategy.manual ? '智能路由策略（手动开启）' : smart ? '智能路由策略（故障接管）' : '平台默认策略';
      return h(Card, { title: '当前路由策略' },
        h('div', { style: S.row }, h('strong', { 'aria-live': 'polite' }, label),
          h('button', { style: S.btnPrimary, disabled: !strategy || !!pending || (!enabled && target === 'smart'),
            onClick: () => change(target) }, pending ? '正在切换…' : target === 'platform' || legacy ? '切换到平台默认策略' : '切换到智能路由策略')),
        !strategy && h('div', { style: S.hint }, legacy ? '当前运行 v0.5.1。完全退出并重启 DSH 后，即可在这里一键切换。' : '请刷新状态或检查插件版本。'),
        error && h('div', { role: 'alert', style: S.err }, error),
        routes.map(r => {
          const source = { measured: '本机实测', 'site-rate': '站点统计' }[r.cacheSource];
          return h('details', { key: `${r.provider}/${r.model}`, open: r.mode !== 'platform' },
            h('summary', null, `${r.provider} / ${r.model} · ${modeNames[r.mode] || modeNames.unknown} · 活跃 ${r.active ?? '—'}`),
            h(KV, { rows: [
              ['商家 / 渠道', [r.supplierName, r.channelName, r.channelId ? `#${r.channelId}` : null].filter(Boolean).join(' / ') || '未确认'],
              ['预计成本 / 百万输入 token', money(r.cnyPerMillion)],
              ['缓存命中率', source && Number.isFinite(r.cacheRate) ? `${pct(r.cacheRate)}（${source}）` : '无观测数据'],
              ['候选延迟 P50', Number.isFinite(r.p50Ms) ? `${r.p50Ms} ms（${({ measured: '本机首块实测', 'site-ttft': '站点首块统计', site: '站点延迟指标' })[r.latencySource] || '来源待确认'}）` : '无数据'],
              ['打分用延迟', Number.isFinite(r.siteLatencyMs) ? `${r.siteLatencyMs} ms（${({ 'site-test': '站点最近实测', 'p50-fallback': 'p50 兜底', 'measured-first-block': '本机首块实测', none: '无数据' })[r.siteLatencyOrigin] || '来源待确认'}）` : '无数据'],
            ] }));
        }));
    }

    function Evidence({ events }) {
      const [feedback, setFeedback] = React.useState('');
      const entries = events.filter(e => e.type === 'format-anomaly').slice(-20).reverse();
      if (!entries.length) return null;
      return h(Card, { title: '格式异常 · 人工复核' },
        h('div', { style: S.hint }, '复制本地记录后可自行向平台举报。请结合实际响应确认原因。'),
        entries.map((e, index) => {
          const fields = ['at', 'requestId', 'code', 'rule', 'provider', 'model', 'channelId', 'attempt', 'elapsedMs', 'firstResponseMs', 'postContent'];
          const text = JSON.stringify(Object.fromEntries(fields.filter(k => ['string', 'number', 'boolean'].includes(typeof e[k])).map(k => [k, e[k]])), null, 2);
          return h('details', { key: `${e.at}|${index}` },
            h('summary', null, [e.at, e.requestId, e.code || '格式异常'].filter(Boolean).join(' · ')),
            h('pre', { style: { whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' } }, text),
            h('button', { style: S.btn, onClick: async () => {
              try { await navigator.clipboard.writeText(text); setFeedback('已复制脱敏记录'); }
              catch { setFeedback('剪贴板不可用，可选中上方记录手动复制。'); }
            } }, '复制脱敏证据'));
        }), h('span', { role: 'status', style: S.hint }, feedback));
    }

    function Panel() {
      const [status, setStatus] = React.useState({ phase: 'loading', data: null, error: '' });
      const [form, setForm] = React.useState({ values: null, error: '', notice: '', saving: false, secretSet: false });
      const [busy, setBusy] = React.useState('');
      const alive = React.useRef(true);

      const loadStatus = React.useCallback(async () => {
        try {
          const data = await getJson('/a6-failover/status');
          if (!alive.current) return;
          setStatus((prev) => ({
            phase: data.ok === false ? 'error' : 'ready',
            data: data.ok === false ? prev.data : data,
            error: data.ok === false ? (data.error || '接口报错') : '',
          }));
        } catch (error) {
          if (!alive.current) return;
          setStatus((prev) => ({ ...prev, phase: 'error', error: String(error && error.message ? error.message : error) }));
        }
      }, []);

      const loadConfig = React.useCallback(async () => {
        try {
          const data = await getJson('/a6-failover/config');
          if (!alive.current) return;
          if (data.ok === false) throw new Error(data.error || '读配置失败');
          setForm((prev) => ({ ...prev, values: data.values || {}, secretSet: Boolean(data.values?.accessTokenSet), error: '' }));
        } catch (error) {
          if (!alive.current) return;
          setForm((prev) => ({ ...prev, error: String(error && error.message ? error.message : error) }));
        }
      }, []);

      React.useEffect(() => {
        alive.current = true;
        loadStatus();
        loadConfig();
        const timer = setInterval(loadStatus, REFRESH_MS);
        return () => { alive.current = false; clearInterval(timer); };
      }, [loadStatus, loadConfig]);

      const setValue = (key, value) => setForm((prev) => ({
        ...prev,
        notice: '',
        values: { ...(prev.values || {}), [key]: value },
      }));

      const save = async () => {
        setForm((prev) => ({ ...prev, saving: true, error: '', notice: '' }));
        try {
          const payload = { ...(form.values || {}) };
          for (const field of ALL_FIELDS) {
            if (field.type !== 'number') continue;
            const raw = payload[field.key];
            if (raw === '' || raw === undefined || raw === null) { delete payload[field.key]; continue; }
            const parsed = Number(raw);
            if (!Number.isFinite(parsed)) throw new Error(`${field.label} 不是数字`);
            if (parsed < field.min || parsed > field.max) throw new Error(`${field.label}应在 ${field.min}～${field.max} 之间`);
            payload[field.key] = parsed;
          }
          if (typeof payload.fallbackRoutes === 'string') {
            payload.fallbackRoutes = payload.fallbackRoutes.split(/\r?\n/).map((line) => line.trim()).filter(Boolean).map((line) => {
              const slash = line.indexOf('/');
              if (slash <= 0 || slash === line.length - 1) throw new Error('备用路由格式应为 provider/model');
              const provider = line.slice(0, slash).trim();
              const model = line.slice(slash + 1).trim();
              if (!provider || !model) throw new Error('备用路由的 provider 和 model 都不能为空');
              return { provider, model };
            });
          }
          if (payload.providers && typeof payload.providers === 'string') {
            payload.providers = payload.providers.split(/[,，\s]+/).filter(Boolean);
          }
          const result = await getJson('/a6-failover/config', { method: 'POST', body: JSON.stringify(payload) });
          if (result.ok === false) throw new Error(result.error || '保存失败');
          setForm((prev) => ({ ...prev, values: result.values || prev.values, secretSet: Boolean(result.values?.accessTokenSet), notice: '已保存并立即生效（不用重启）', error: '' }));
          if (result.status) setStatus({ phase: 'ready', data: result.status, error: '' });
        } catch (error) {
          setForm((prev) => ({ ...prev, error: String(error && error.message ? error.message : error) }));
        } finally {
          setForm((prev) => ({ ...prev, saving: false }));
        }
      };

      const resetConfig = async () => {
        setForm((prev) => ({ ...prev, saving: true, error: '', notice: '' }));
        try {
          const result = await getJson('/a6-failover/config', { method: 'DELETE' });
          if (result.ok === false) throw new Error(result.error || '清除失败');
          setForm((prev) => ({ ...prev, values: result.values || {}, secretSet: Boolean(result.values?.accessTokenSet), notice: '已清除本地设置，回落到 profile patch 里的值', error: '' }));
          if (result.status) setStatus({ phase: 'ready', data: result.status, error: '' });
          else loadStatus();
        } catch (error) {
          setForm((prev) => ({ ...prev, error: String(error && error.message ? error.message : error) }));
        } finally {
          setForm((prev) => ({ ...prev, saving: false }));
        }
      };

      const refreshPrices = async (channel) => {
        setBusy(channel.model);
        try {
          const result = await getJson('/a6-failover/refresh', {
            method: 'POST',
            body: JSON.stringify({ model: channel.model, controlBase: channel.controlBase }),
          });
          if (result.ok === false) throw new Error(result.error || '刷新失败');
          if (result.status) setStatus({ phase: 'ready', data: result.status, error: '' });
        } catch (error) {
          setStatus((prev) => ({ ...prev, error: String(error && error.message ? error.message : error) }));
        } finally {
          setBusy('');
        }
      };

      const data = status.data;
      if (!data && status.phase === 'loading') {
        return h('div', { style: S.wrap }, h('div', { style: S.hint }, '正在读取 a6-failover 状态…'));
      }
      if (!data) {
        return h('div', { style: S.wrap },
          h('div', { style: S.err }, '读不到插件状态：', status.error || '未知错误'),
          h('div', { style: S.hint }, '插件可能没被加载；重启 DSH 后如果还这样，把这段报错发出来。'));
      }

      const plugin = data.plugin || {};
      const limits = data.limits || {};
      const price = data.price || {};
      const degraded = (data.routes || []).filter((route) => route.degradedUntil > Date.now());
      const channels = data.channels || [];
      const overridden = (data.overrides && data.overrides.keys) || [];
      const values = form.values || {};

      return h('div', { style: S.wrap },
        // 概览
        h('div', { style: { display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' } },
          h('span', { style: { fontWeight: 600 } }, 'A6 换商家'),
          h('span', { style: S.hint }, `v${data.version || '?'}`),
          h('span', null, h('span', { style: S.dot(plugin.enabled) }, ''), plugin.enabled ? '已启用' : '已停用'),
          overridden.length ? h('span', { style: S.hint }, `本地覆盖 ${overridden.length} 项`) : null,
          status.error ? h('span', { style: S.err }, status.error) : null,
          h('button', { style: S.btn, onClick: loadStatus }, '刷新状态')),

        h(Strategy, { strategy: data.strategy, version: data.version, enabled: plugin.enabled, reload: loadStatus }),
        h(Evidence, { events: data.evidence ?? data.events ?? [] }),

        // 设置表单
        h('div', { style: { border: '1px solid rgba(120,170,255,.35)', borderRadius: 10, padding: '12px 14px' } },
          h('div', { style: { display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 6 } },
            h('span', { style: { fontWeight: 600 } }, '插件设置'),
            h('span', { style: S.hint }, '保存后立即生效，不需要重启'),
            form.notice ? h('span', { style: S.ok }, form.notice) : null,
            form.error ? h('span', { style: S.err }, form.error) : null,
            h('span', { style: { flex: 1 } }),
            h('button', { style: S.btn, disabled: form.saving, onClick: loadConfig }, '重读'),
            h('button', { style: S.btn, disabled: form.saving, onClick: resetConfig }, '清除本地覆盖'),
            h('button', { style: S.btnPrimary, disabled: form.saving || !form.values, onClick: save }, form.saving ? '保存中…' : '保存')),

          GROUPS.map((group) => h('details', { key: group.title, open: group.open, style: { marginTop: 8 } },
            h('summary', { style: S.summary }, group.title),
            h('div', { style: { ...S.hint, marginBottom: 10 } }, group.description),
            h('div', { style: S.fieldGrid }, group.fields.map((field) => h(Field, {
              key: field.key,
              field,
              value: values[field.key],
              secretSet: field.key === 'accessToken' ? Boolean(values.accessTokenSet) : false,
              onChange: (next) => setValue(field.key, next),
            }))),
            group.note ? h('div', { style: { ...S.hint, marginTop: 10 } }, group.note) : null)),

          h('div', { style: { ...S.hint, marginTop: 10 } },
            '存在：', h('code', null, data.overrides?.file || ''),
            '（覆盖 profile patch 里的同名字段；清除后回落到 patch）',
            h('br', null),
            '凭据优先级：这里填的 > 模型管家的 ~/.model_probe/routing.json > 无')),

        // 概览卡片
        h('div', { style: S.row },
          h(Card, { title: '接管范围' },
            h(KV, {
              rows: [
                ['provider 匹配', plugin.providerPattern || '—'],
                ['控制面', plugin.controlBase || '—'],
                ['凭据来源', (plugin.credentialSources || []).join(' / ') || '—'],
                ['备用路由', (plugin.fallbackRoutes || []).length ? plugin.fallbackRoutes.map((r) => `${r.provider}/${r.model}`).join('，') : '未配置'],
              ],
            })),
          h(Card, { title: '切换预算' },
            h(KV, {
              rows: [
                ['单次调用', `最多 ${limits.maxSwitchesPerCall} 次`],
                ['本小时已用', `${limits.switchesLastHour} / ${limits.maxSwitchesPerHour}`],
                ['降级路由', degraded.length ? degraded.map((r) => `${r.provider}/${r.model}`).join('，') : '无'],
                ['并发去重窗口', `${limits.switchDedupeMs} ms`],
                ['上下文换模型', `${limits.autoSwitchOnContextFailure ? '开' : '关'}（上限 ${limits.maxContextSwitchesPerHour}/小时）`],
              ],
            })),
          h(Card, { title: '当前价格线' },
            h(KV, {
              rows: [
                ['实际 ≤ 官网', pct(price.maxEffectiveRatio, 0)],
                ['缓存命中假设', pct(price.cacheHitRate, 0)],
                ['输出/输入', pct(price.outputToInputRatio, 2)],
                ['权重 成本/可靠性/延迟', `${price.priceWeight ?? '—'} / ${price.reliabilityWeight ?? '—'} / ${price.latencyWeight ?? '—'}`],
                ['延迟参考 / 淘汰线', `${price.latencyRefMs} ms 记 1 倍惩罚 / >${price.hardP50Ms} ms 淘汰`],
              ],
            }))),

        // 商家价格表
        h(Card, { title: '商家候选排名（缓存过期后按需刷新）' },
          channels.length === 0
            ? h('div', { style: S.hint }, '还没有拉过商家列表。第一次换商家、或点下面的「重新拉取」都会拉。')
            : channels.map((channel) => h('div', { key: `${channel.controlBase}|${channel.model}`, style: { marginBottom: 12 } },
              h('div', { style: { display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap', marginBottom: 6 } },
                h('span', { style: { fontFamily: 'ui-monospace, monospace' } }, channel.model),
                h('span', { style: S.hint }, `${(channel.total || 0)} 条 ${channel.at ? `· ${clock(channel.at)} 拉取` : ''}`),
                h('button', {
                  style: S.btn,
                  disabled: busy === channel.model,
                  onClick: () => refreshPrices(channel),
                }, busy === channel.model ? '拉取中…' : '重新拉取')),
              Object.keys(channel.rejected || {}).length
                ? h('div', { style: S.hint }, '忽略原因：', Object.entries(channel.rejected).map(([reason, count]) => `${reason}×${count}`).join('，'))
                : null,
              h('table', { style: S.table },
                h('thead', null, h('tr', null,
                  ['排名 / 商家', '总分 ↓', '实际/官方', '¥/M输入', '缓存数据', 'p50', '成功率'].map((label) => h('th', { key: label, style: S.th }, label)))),
                h('tbody', null, (channel.candidates || []).map((candidate, index) => h('tr', { key: candidate.channelId },
                  h('td', { style: S.td }, `${index + 1}. ${candidate.channelId} ${candidate.channelName || ''}`.trim()),
                   h('td', { style: S.td }, Number.isFinite(candidate.score) ? candidate.score.toFixed(4) : '—'),
                  h('td', { style: S.td }, pct(candidate.effRatio ?? candidate.ratio)),
                  h('td', { style: S.td }, money(candidate.cnyPerMillion)),
                   h('td', { style: S.td },
                     candidate.cacheSource === 'site-rate' && Number.isFinite(candidate.cacheRate24h)
                       ? `站点 ${pct(candidate.cacheRate24h)}`
                       : candidate.cacheSource === 'measured' && Number.isFinite(candidate.cacheHealth)
                         ? `实测 ${pct(candidate.cacheHealth)}${candidate.cacheSamples ? `（${candidate.cacheSamples} 次）` : ''}`
                          : candidate.cacheSource === 'cache-read-cost' && Number.isFinite(candidate.cacheReadRatio)
                            ? `读价为官网的 ${pct(candidate.cacheReadRatio)}`
                           : '无数据'),
                  h('td', { style: S.td }, `${candidate.p50Ms} ms`),
                  h('td', { style: S.td }, pct(candidate.successRate))))))))),

        // 事件
        h(Card, { title: `最近事件（${(data.events || []).length}）` },
          (data.events || []).length === 0
            ? h('div', { style: S.hint }, '还没有事件。切到 a6 的模型跑一轮就会有了。')
            : h('table', { style: S.table },
              h('tbody', null, data.events.slice().reverse().map((event, index) => h('tr', { key: index },
                h('td', { style: { ...S.td, width: 70, opacity: 0.65 } }, clock(Date.parse(event.at || 0))),
                h('td', { style: S.td }, eventLine(event))))))),

        h('div', { style: S.hint },
          '数据文件：', h('code', null, data.files?.state || ''), ' · ', h('code', null, data.files?.events || '')));
    }

    function apply(ctx) {
      // 语言包：设置栏标题走 locale，和 dsh-computer-use-win 同一套写法
      const dictionaries = { zh: { nav: 'A6 换商家' }, en: { nav: 'A6 failover' } };
      if (ctx.locale && typeof ctx.locale.register === 'function') {
        ctx.effect(() => ctx.locale.register(NS, dictionaries));
      }
      const t = ctx.locale && typeof ctx.locale.bind === 'function' ? ctx.locale.bind(NS) : () => 'A6 换商家';

      ctx.slots.inject('settings.section', () => ctx.slots.register({
        name: 'settings.section',
        id: 'a6-failover',
        order: 17,
        label: () => t('nav'),
        locale: NS,
      }, Panel));
    }

    return { apply, inject: ['slots', 'locale'] };
  },
});
