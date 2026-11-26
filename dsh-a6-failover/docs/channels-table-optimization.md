# 渠道表优化：按模型过滤，避免重复全量下载

> 状态：**已于 0.7.5 实现**（含 search 接口、只刷在用模型、1 小时刷新、汇率自动取）
> 前置事实见 `channels-table.md`。核心实测结论：**旧接口服务端忽略 `model_name`，每个模型请求都返回同一份全量数据（~15MB / 2937 channel / 7455 listing）；网页在用的 `channels/search` 服务端按模型过滤，571KB / 0.4s**。

---

## -1. 【用户约束 2026-11-26 19:40】只取 DSH 添加的模型的表，每小时更新一次

用户明确要求（截图为 DSH「模型」设置里实际添加的中转站模型目录：
`deepseek-v4.1-flash-1017` / `gpt-6-luna` / `claude-haiku-5-5` / `deepseek-v4.1-flash`）：

1. **只下载 DSH 中实际添加/使用的中转站模型的数据**，不下载站点上所有模型的；
2. 中转站模型数据更新不频繁，**每小时刷新一次即可**（不是现在的每 30 分钟）。

这两条直接决定了最终方案的三件事（覆盖下面第 0/7 节里与之冲突的部分）：

### A. 数据源用 `channels/search`，服务端按模型过滤

见第 0/7 节：571KB / 0.4s，字段与旧接口一致。**一个模型一次请求，只拿这个模型的商家**，天然满足「不下载所有模型」。

### B. 模型清单：以「实际用到的模型」为准，不拉全站

**【代码事实】** 插件没有读取 DSH 模型配置文件的接口，但有两个可靠的"实际模型"来源：

| 来源 | 位置 | 说明 |
|---|---|---|
| `knownRoutes`（内存） | `switcher.js:663` `remember(provider, model)` | 每次 `beginRequest(provider, model)` 都会记下真实用到的模型；上限 256 条 |
| `prewarmRoutes` / `owned` | `switcher.js:676` `strategyStatus()` | 配置里显式预热 + 插件已固定的模型 |
| `store.allChannels()`（磁盘） | `switcher.js:516` 后台刷新当前遍历源 | 已缓存过表的模型，重启后仍在 |

**方案**：把后台刷新的模型清单从「已缓存表的模型」改为「`knownRoutes` ∪ `prewarmRoutes` ∪ `owned` ∪ `allChannels`」的并集。这样：
- 只有 DSH 真正用过/配置过的模型才刷新；
- 站点上新冒出来的、DSH 没添加的模型永远不会被下载；
- 重启后 `knownRoutes` 丢失也没关系——`allChannels` 里还留着上次用过的模型，且新请求一来 `remember` 会立刻补上。

### C. 刷新频率改为 1 小时

把 `channelsRefreshMs` 默认值从 **1800000（30 分钟）改为 3600000（1 小时）**；设置页的「后台刷新商家表间隔」默认值同步改。缓存时效配合调整：
- `channelCacheTtlMs` 默认 15 分钟 → 建议改为 **55 分钟**（略小于刷新间隔，保证 1 小时节奏）；
- `QUOTE_MAX_AGE_MS`（硬编码 60 分钟）保持——即使某轮刷新失败，旧表最多顶 1 小时，之后才强制重新拉。

> 效果：4 个 DSH 模型 × 每小时一轮 = 4 × 571KB = **2.2MB/小时**（对比现在 4 × 15MB = 60MB/小时，且那 15MB 还是全站数据）。

### D. 落地顺序（整合第 7 节）

1. `a6.js` 加 `fetchChannelSearch()`；`selector.js` 兼容扁平 `data.items`；`refreshCandidates` 换调用并保留旧接口回退。
2. `startChannelsRefresh()` 遍历源改为 B 节的模型并集，而不是 `store.allChannels()`。
3. `channelsRefreshMs` 默认 1 小时、`channelCacheTtlMs` 默认 55 分钟，设置页同步。
4. 补测试：只刷新已用模型、每小时节奏、新旧接口产出等价、回退路径。
5. 升版本 0.7.5、更新 CHANGELOG/README、同步运行时副本、推送 GitHub。

---

## 0. 【重大发现 2026-11-26】网站用的是另一个接口，快 26 倍

分析前端 bundle（`/ui/new/assets/index-Lm8U2LvE.js`，7.21MB）发现：**网页市场页根本不调用 `/api/marketplace/channels`**，而是调 `/api/marketplace/channels/search`。后者服务端就按模型过滤、支持排序分页。

前端市场页的查询构造（`h0()` 函数）：

```js
{
  model: 选中的模型,
  sort: 排序方式,          // 如 price_low_first
  page: 页码,
  page_size: 每页条数,
  channel_kinds, brand, category, description_keyword, token_id, ...
}
```

### 实测对比（同一模型 deepseek-v4.1-flash）

| 接口 | 响应 | 耗时 | 条目 | 服务端过滤 |
|---|---|---|---|---|
| `/api/marketplace/channels?model_name=X`（插件现用） | **15.47 MB** | 7.6 s | 7455 listing（全站） | ❌ 忽略参数 |
| `/api/marketplace/channels/search?model=X` | **571 KB** | **0.4 s** | 193 条（就是该模型） | ✅ |
| `...search?model=X&page=1&page_size=20` | **87 KB** | 1.2 s | 20 条 | ✅ + 分页 |

**571KB vs 15MB（26 倍小）、0.4s vs 7.6s（19 倍快）。**

### 字段兼容性（已实测）

新接口每条 listing 有 67 个字段，与旧接口**逐字段一致**。`classifyListing` 需要的全部字段都在：

```
input_price_micros / output_price_micros / cache_read_price_micros / pricing_currency / pricing_unit
last_test_response_ms / recent_p50_ms / recent_success_rate / sample_count / official_price
```

同渠道比对：新旧接口的 input/output/cache 报价完全相同。

### 结论：**换接口优于「一次下载多方复用」**

第 1~6 节的「一次下载、多方复用」方案是在**不知道有 search 接口**时的折中。既然 search 接口服务端就按模型过滤且快 26 倍，**直接换接口是更彻底的解法**：
- 无需 `rawTable` 内存缓存、无需按 account 分组；
- 每个模型一次请求 571KB / 0.4s，比共享一份 15MB 还快；
- 缓存结构、selectionKey、时效机制全部不动。

实现要点见第 7 节。原方案（1~6 节）保留作历史记录与兜底参考。

---

## 1. 问题定义（原方案背景）

当前 `refreshCandidates(site, model, ...)` 是「一个模型一次下载」：

```
模型 A 刷新 → GET channels?model_name=A → 下载全量 15MB → 本地过滤出 A → 落盘 A
模型 B 刷新 → GET channels?model_name=B → 下载全量 15MB → 本地过滤出 B → 落盘 B
模型 C 刷新 → GET channels?model_name=C → 下载全量 15MB → 本地过滤出 C → 落盘 C
```

**三份请求返回的是同一份数据**，却付了三次下载成本。当前 3 个受管模型、每 30 分钟一轮后台刷新 ≈ 每轮 45MB + 3 次串行代理占用。模型越多越浪费。

目标：**同一控制面账号的多个模型，共享一次下载**。

## 2. 核心思路

把「下载」和「按模型解析」拆成两层：

```
                    ┌─ 一次下载（全量 15MB）
refreshAll(site) ───┤
                    └─ 本地按模型拆成 N 份，分别走 Worker 解析、分别落盘
```

- 下载层按 **account**（controlBase + userId + token）去重，与模型无关；
- 解析层仍按 **model** 逐个跑，因为每个模型的 `selectionKey` 不同、门槛不同；
- 落盘仍保持现在的 `channels[controlBase|model]` 结构，不动缓存读取路径。

## 3. 具体改造点

### 3.1 新增「全量响应」内存缓存

在 `createSwitcher` 作用域内新增：

```js
const rawTableLoads = new Map();   // key = account
const rawTable = new Map();        // key = account → { text, at, signature }
```

- `rawTable`：最近一次全量响应文本 + 时间戳；
- `rawTableLoads`：正在进行的下载任务（同 account 只保留一份）。

新增函数：

```js
async function fetchRawTable(site, account, force = false) {
  const cached = rawTable.get(account);
  const fresh = cached && Date.now() - cached.at < RAW_TABLE_TTL;   // 建议 60s
  if (!force && fresh) return cached.text;
  if (rawTableLoads.has(account)) return rawTableLoads.get(account); // 复用在跑的那份
  const task = fetchChannels({
    ...site,
    modelName: '',                  // 反正服务端忽略
    timeoutMs: Math.max(cfg.channelsTimeoutMs || 90000, 15000),
    signal: lifetime.signal,
    maxBytes: cfg.maxChannelsBytes,
    rawJson: true,
  }).then((text) => {
    rawTable.set(account, { text, at: Date.now() });
    return text;
  }).finally(() => rawTableLoads.delete(account));
  rawTableLoads.set(account, task);
  task.catch(() => {});
  return task;
}
```

`RAW_TABLE_TTL` 建议 **60 秒**：后台刷新是一轮内连续处理多个模型，60s 足够让同轮所有模型复用；又不会让不相关的新请求拿到太旧的数据。

### 3.2 改造 `refreshCandidates`

原来的下载段：

```js
const text = await fetchChannels({ ... });
```

改成：

```js
const text = await fetchRawTable(site, account, force);
```

其余不变：Worker 解析、`store.setChannels()`、日志都保持原样。

> 注意：`force=true` 时语义要区分——设置页点「重新拉取」应强制**重新下载一次**全量，然后所有模型都用这份新数据解析，而不是每个模型各下一遍。

### 3.3 改造后台定时刷新 `startChannelsRefresh`

当前它对每个模型调 `ensureRefresh(...)`，每个模型各自下载。改成**按 account 分组、先下一次、再逐模型解析**：

```js
const tick = async () => {
  const groups = new Map();   // account → { site, models: [] }
  for (const [base, byModel] of Object.entries(store.allChannels())) {
    for (const model of Object.keys(byModel)) {
      const site = resolveSiteByBase(base);
      if (!site.ok) continue;
      const acc = identity(site);
      if (!groups.has(acc)) groups.set(acc, { site, models: [] });
      groups.get(acc).models.push(model);
    }
  }
  for (const { site, models } of groups.values()) {
    const account = identity(site);
    try {
      const text = await fetchRawTable(site, account, true);   // 一轮只下一次
      for (const model of models) {
        if (lifetime.signal.aborted) return;
        await parseAndStore(site, model, account, text);       // 复用同一份文本
      }
    } catch { /* 记事件，本轮跳过这一组 */ }
  }
};
```

需要把 `refreshCandidates` 里「下载之后」的部分抽成 `parseAndStore(site, model, account, text, ...)`，供两处共用。

### 3.4 在 `parseCandidates` 层真正按模型过滤

其实现在的 `parseCandidates(payload, modelName, ...)` **已经是按模型过滤的**（`selector.js:532` `if (item.model_name !== modelName) continue;`）。所以方案不需要改解析逻辑，只需要让多个模型**复用同一份 payload 文本**。

### 3.5 可选增强：一次解析出所有模型

更彻底的做法是 Worker 一次遍历 listings，按 `model_name` 分组，产出 `Map<model, candidates>`，然后批量落盘。优点是从 15MB 文本只解析一次（现在是每模型各解析一遍 15MB，3 个模型=解析 3 次）。

但代价是：
- 每个模型的 `selectionKey` 不同（门槛不同），Worker 需要同时拿到所有模型的配置；
- 改动面更大，回归风险高。

**建议分两步**：先做 3.1~3.3（下载复用，收益最大、风险最低），观察效果后再决定是否做 3.5（解析复用）。

## 4. 预期收益

以当前 3 个模型、每 30min 刷新一轮估算：

| 指标 | 现状 | 优化后 |
|---|---|---|
| 每轮下载次数 | 3 | 1 |
| 每轮下载流量 | ~45 MB | ~15 MB |
| 代理串行槽占用 | 3 次 × 6~60s | 1 次 × 6~60s |
| 每轮 Worker 解析次数 | 3（各解析 15MB） | 3（第一步不变）→ 可降到 1（第二步） |
| 落盘缓存结构 | 不变 | 不变 |

模型数越多，收益线性放大。

## 5. 风险与边界

1. **全量响应文本的内存占用**：~15MB 字符串缓存 60s，可接受；但要注意 `rawTable` 按 account 存储，需加上限（如最多 4 个 account）。
2. **force 语义**：设置页「重新拉取」应强制下载一次、所有模型共用；不能变成 N 次强制下载。需在 `fetchRawTable` 里让 `force` 只触发一次下载，随后 `rawTable` 命中即可。
3. **selectionKey 变化**：改参数后，各模型缓存不兼容，会各自触发刷新；但因为共享 `rawTable`，只会下载一次，然后按新 key 分别解析。这点天然成立。
4. **账号隔离**：`rawTable` 必须按 `identity(site)` 隔离，不同 token/站点不能串用。
5. **失败处理**：下载失败时，所有模型本轮都拿不到新表，但旧缓存仍可用（`QUOTE_MAX_AGE_MS` 60min 内），不会中断请求。

## 6. 落地步骤（建议）

1. 实现 `fetchRawTable()` + `rawTable` / `rawTableLoads`，`refreshCandidates` 改用之。
2. 抽出 `parseAndStore()`，`startChannelsRefresh` 改为按 account 分组下载、逐模型解析。
3. 补测试：多模型共享一次下载（断言 `fetchChannels` 只调用一次）、force 语义、账号隔离、selectionKey 变化后仍只下一次。
4. 全量测试通过后升版本（0.7.5）、更新 CHANGELOG/README、同步运行时副本、推送 GitHub。
5. 第二步（可选）：Worker 一次解析所有模型，再降解析次数。

---

## 7. 推荐实现：换用 `channels/search` 接口（取代 1~6 节）

### 7.1 改动点

**a. `lib/a6.js` 新增 `fetchChannelSearch()`**：

```js
export async function fetchChannelSearch({ modelName, ...site }) {
  // 服务端按模型过滤 + 排序；不加 page/page_size 即为该模型全部 listing（实测 193 条 / 571KB）
  return a6Request({
    ...site,
    path: `/api/marketplace/channels/search?model=${encodeURIComponent(modelName)}`,
  });
}
```

**b. `lib/selector.js` 的 `parseCandidates()` 兼容扁平结构**：

当前（`selector.js:519-530`）只认 `{data: [channel, ...]}`，每个 channel 有 `listings[]`。新接口是 `{data: {items: [listing, ...]}}`，listing 是扁平的。加一个分支：

```js
const data = payload?.data;
let rows;
if (Array.isArray(data)) {
  // 旧接口：channel[] → listings[]
  rows = data.flatMap(ch => Array.isArray(ch?.listings) ? ch.listings : []);
} else if (Array.isArray(data?.items)) {
  // 新接口：扁平 listing[]
  rows = data.items;
} else return { candidates: [], total: 0, rejected: { 'no-data': 1 } };
for (const item of rows) {
  if (String(item.model_name || '') !== modelName) continue;
  total += 1;
  ... classifyListing(item, /* channel 上下文 */, cfg, ...) ...
}
```

注意：`classifyListing(item, channel, ...)` 第二个参数是 channel 上下文（旧接口里是外层 channel 对象）。新接口的 listing 自带了 `channel_type` / `supplier_channel_disabled` 等原本从 channel 取的字段，需要用 listing 自身构造等价上下文（对照第 7.2 节的字段核对）。

**c. `lib/switcher.js` 的 `refreshCandidates()` 换调用**：

```js
const text = await fetchChannelSearch({ ...site, modelName: model, ... });
```

`maxBytes` 可以从 48MB 降到 8MB（新接口最大 571KB，留足余量）；`channelsTimeoutMs` 可以从 90s 降到 30s（实测 0.4s，站慢留余量）。

### 7.2 必须核对的差异（实现前逐项验证）

| 项 | 旧接口 | 新接口 | 处理 |
|---|---|---|---|
| 数据结构 | `data: channel[]`，内层 `listings[]` | `data.items: listing[]`（扁平） | parseCandidates 加分支 |
| `store.setChannels` 的 total | 该模型 listing 数 | `data.total` 字段直接给出 | 可用 `data.total` 更准 |
| 缓存率来源 | 单独拉 `/subscriptions` | listing 有 `cache_read_price_micros`，但命中率仍只在 subscriptions | 保留 subscriptionRates |
| 字段完整性 | 67 字段 | 67 字段，逐字段一致 | 已实测兼容 |
| 排序 | 无序，插件自己排 | 服务端 `sort`，但插件也自己排 | 不带 sort 即可 |

### 7.3 收益

| 指标 | 现状 | 换接口后 |
|---|---|---|
| 单模型下载 | 15.47 MB / 7.6 s | 571 KB / 0.4 s |
| 3 模型一轮后台刷新 | 45 MB / 23 s+ | 1.7 MB / 1.2 s |
| 本地代理串行占用 | 3 × 7.6s | 3 × 0.4s |
| 内存峰值（Worker 解析） | 15MB 字符串 × 3 | 571KB × 3 |

### 7.4 风险与回退

1. **接口稳定性**：`channels/search` 是网页在用的生产接口，稳定性比插件私用的 `channels` 更有保障；但仍保留旧接口作为回退——新接口失败时（404/权限变化）自动降级到 `fetchChannels`。
2. **分页**：不带 `page_size` 实测返回全部 193 条；若站点某天默认改成分页（如只回 20 条），要加 `page_size=500` 显式声明，并处理 `data.total > items.length` 时的翻页。
3. **字段漂移**：新接口是网页接口，字段可能随前端版本变化；解析时对缺失字段保持保守（缺价候选拒绝），不要因为字段变化误判为「便宜」。

### 7.5 落地步骤

1. 实现 `fetchChannelSearch()` + `parseCandidates` 扁平分支 + `refreshCandidates` 换调用（带旧接口回退）。
2. 补测试：新旧接口产出等价候选（用真实响应快照）、扁平解析、total 字段、回退路径。
3. 全量测试通过后升版本（0.7.5）、更新 CHANGELOG/README、同步运行时副本、推送 GitHub。
4. 上线后观察 `channels-refreshed` 事件的 count 与耗时，确认从 15MB/7s 降到 571KB/0.4s。

---

**一句话结论**：网页用的 `channels/search` 接口服务端就按模型过滤，571KB/0.4s，比插件现用接口小 26 倍、快 19 倍，字段完全一致。**直接换接口（第 7 节）优于「一次下载多方复用」（第 1~6 节）**，后者仅作历史记录与回退参考。
