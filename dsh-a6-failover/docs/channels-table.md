# 渠道表（"25MB 表"）现状梳理

> 对应版本：dsh-a6-failover **0.7.4**
> 梳理日期：2026-11-26
> 标注约定：**【实测】**=本机真实请求验证；**【代码】**=从源码确认；**【推断】**=尚未证实

---

## 1. 这张表是什么

**【代码】** 它来自控制面接口：

```
GET /api/marketplace/channels?model_name=<模型>
```

定义在 `lib/a6.js:347` 的 `fetchChannels()`。认证头是 `authorization: Bearer <token>` + `new-api-user: <userId>`。

内容是站点上**全部渠道（channel）**，每个 channel 带一个 `listings[]` 数组——该渠道能跑的所有模型的报价。插件要的是其中 `model_name` 等于目标模型的那些 listing。

## 2. 规模：实测数据

**【实测】** 2026-11-26 对两个模型分别请求，结果：

| 请求模型 | 响应大小 | 耗时 | channel 数 | listing 总数 |
|---|---|---|---|---|
| `deepseek-v4.1-flash` | 15.47 MB | 7.6 s | 2937 | 7455 |
| `gpt-6-luna` | 15.49 MB | 6.5 s | 2937 | 7464 |

**关键发现：`model_name` 参数被服务端完全忽略。** 两次请求的 listing 分布几乎完全一样（前几名都是 `gpt-6-astra=547`、`gpt-6.1-sol=543`、`deepseek-v4.1-flash-1017=199` ……），都返回了全站全量数据。

> 历史记录里出现过 24~25MB / 15~60s 的数字，那是站点较慢时的观测；本次实测是 15.5MB / 6.5~7.6s。量级一致：**十几 MB、秒级到几十秒**。

**【代码】** 响应大小上限 `maxChannelsBytes = 48MB`（`config.js:112`），超过直接拒绝，保护内存。

## 3. 为什么要特殊对待

**【实测】** 它和其他请求的量级完全不是一个级别：

| 请求 | 大小 | 耗时 |
|---|---|---|
| 渠道表 | 15~25 MB | 6~60 s |
| `/api/subscriptions` | ~2 MB | 秒级 |
| `pins` / `pin` / `unpin` | 几 KB | 0.7~3 s |

踩过的坑（均为实测）：
- 用 `controlTimeoutMs`（20s）管它 → 站慢时拉不完，切换必然失败；
- 和订阅列表**并发**拉 → 抢同一条本地代理隧道，29s 变 90s 拉不完；
- 本地代理（verge-mihomo）同时开多条 CONNECT → 随机掐断新连接，TLS 握手失败。

因此现在给它独立的超时、串行下载、Worker 解析、只读缓存。

## 4. 处理链路

**【代码】** 唯一真正下载这张表的地方是 `lib/switcher.js:362` 的 `refreshCandidates()`：

```
refreshCandidates()
 ├─ 串行先等 subscriptionRates()      ← 缓存率来源，2MB，有 5min 缓存，通常命中
 ├─ fetchChannels()                  ← 下载全量 JSON
 │    timeout = channelsTimeoutMs (90s，下限 15s)
 │    rawJson = true                  ← 不在这里解析，直接拿文本
 │    signal  = lifetime.signal       ← 不受单次切换的短超时影响
 ├─ new Worker(quote-worker.js)      ← 解析放 Worker 线程，不阻塞主循环
 │    └─ parseCandidates(payload, model, cfg, {...})
 │         ├─ 按 model_name 过滤 (selector.js:532)
 │         ├─ classifyListing() 门槛判定
 │         └─ 按 channelId 去重
 └─ store.setChannels()              ← 落盘精简候选
```

`quote-worker.js`（16 行）只做一件事：`JSON.parse` 全量文本 → 调 `parseCandidates()` → 回传 `{candidates, total, rejected, eligible}`。

## 5. 落盘后的大小：25MB → 120KB

**【实测】** 当前 `state.json` 里缓存了 3 个模型：

| 模型 | 候选数 | 落盘 JSON | total listing | 缓存年龄 |
|---|---|---|---|---|
| `deepseek-v4.1-flash` | 136 家 | 142 KB | 196 | ~19.6 h |
| `deepseek-v4.1-flash-1017` | 128 家 | 125 KB | 217 | ~24 h |
| `gpt-6-luna` | 128 家 | 119 KB | 476 | ~46 h |

**【代码】** 原始 15~25MB 只在内存和 Worker 里存在；`store.setChannels()` 落盘的是过滤、判定、去重后的候选数组，所以每个模型只剩 120KB 左右。

## 6. 三条触发入口

**【代码】**

| 入口 | 位置 | 行为 |
|---|---|---|
| `pickCachedCandidates()` — 切换热路径 | `switcher.js:423` | **只读缓存，绝不下载**。缓存可用→毫秒级返回；不可用→触发后台刷新，本轮降级到平台默认路由 |
| `ensureRefresh()` / `startChannelsRefresh()` — 后台 | `switcher.js:489` / `:512` | 同 key 只跑一份；`setInterval` 按 `channelsRefreshMs`（30min）遍历缓存里所有模型逐个刷新 |
| `loadCandidates()` — 显式刷新 | `switcher.js:455` | 设置页「重新拉取」或缓存未就绪时调用；await 已在跑的那份，不新起下载 |

## 7. 缓存时效与失效

**【代码】** 相关配置（`config.js`）：

| 配置 | 默认 | 作用 |
|---|---|---|
| `channelsTimeoutMs` | 90 s | 下载专用超时，下限 15s |
| `channelsRefreshMs` | 30 min | 后台刷新间隔，下限 60s |
| `channelCacheTtlMs` | 15 min | 「新鲜」判定，过期→触发后台刷新，但旧表仍可用 |
| `maxChannelsBytes` | 48 MB | 响应大小上限 |
| `QUOTE_MAX_AGE_MS`（常量，`switcher.js:22`） | 60 min | 缓存绝对过期线，超过则旧表也不用了 |

两层时效：`channelCacheTtlMs`（15min）决定是否**触发刷新**，`QUOTE_MAX_AGE_MS`（60min）决定旧表**还能不能顶上**。刷新失败时，只要表不超过 60min，切换照常能用。

**【代码】** 每条缓存带 `selectionKey`（打分参数序列化）和 `account`。改任何影响选商的参数（权重、门槛、比率……）→ key 变化 → 旧缓存判定不兼容 → 触发重新下载。

## 8. 当前设计的问题（引出优化）

**【实测】** 服务端忽略 `model_name`，所以：

1. **每个模型的刷新都下载同一份全量数据。** 当前 3 个模型 × 每 30min 一轮 = 每轮下载 3 份 15MB（~45MB），其中内容几乎完全相同。
2. 模型越多，浪费越大：每加一个受管模型，每轮刷新就多下一份全量。
3. 下载 15MB 本身要 6~60s，还要占着串行代理槽，阻塞其他控制面请求。

**结论**：下载应该只做一次（全量响应一次拿到），然后本地按模型分别解析、分别缓存；而不是每个模型各自下载一份全量。详见同目录 `channels-table-optimization.md`。
