## 0.8.3 新增「换一家」按钮（按排序依次切换）

- 每条路由在「选最佳商家」旁新增「换一家」按钮。
  - **选最佳商家**：-force 重排候选表，固定排序快照，从第 1 家开始（`mode=reset`）。
  - **换一家**：沿刚固定的排序快照依次取第 2/3/4… 家，跳过已失效渠道，到末尾后重新排序从头来（`mode=next`）。
- 摘要显示「第 N/M 家」序号；后端 `selectBest` 加 `mode` 参数与 `manualOrder` 内存态（重启后自动降级为 reset）。
- endpoint `/a6-failover/select-best` 接受 `mode: reset|next`；固定失败会回退序号，不留半条账本。
- 测试：self-test 142 项（新增 5 项序号逻辑）、node --test 41/41 全绿。

## 0.8.2 设置页权重改用滑条

- 成本/可靠性/延迟三个权重字段从纯数字输入框改成**滑条 + 数值输入框**：滑条范围 0~200、步长 5，可拖动快速调，也可在旁边输入框填精确值（校验上限放宽到 0~1000）。
- `Field` 组件新增 `slider` 类型；保存校验与 numeric 字段初始化同步兼容 slider。其余参数输入方式不变。
- 测试：self-test 137 项、node --test 41/41 全绿。

## 0.8.1 统一门槛与打分的成本口径

解决「成本权重 90 却选出 effRatio 超门槛的贵商家」。

- **根因**：门槛对未实测商家用「官网同等命中率」的标价倍率判（如 0.23 通过），打分却用真实成本倍率 effRatio（1.37 排前面）——两个口径不一致，超线商家漏进候选表。实测候选 59 家里有 13 家 effRatio>0.7。
- **修复**：
  - `selector.js` 门槛统一用 `priced.ratio`（effRatio），删掉 headline 双口径逻辑；超 `maxEffectiveRatio` 直接剔除。
  - `config.js` 的 `unmeasuredMerchantCacheHitRate` 默认 0 → 0.2（用户在用值），未实测商家按 20% 命中率估算真实成本，不再按 0 命中率悲观判死。门槛与打分同一基准。
  - `status.js` 增加展示该假设值。
- **验收**：真实站点 deepseek-v4.1-flash 候选表中 effRatio>0.7 的商家从 13 家降到 0 家；用户 config.json 该键同步改为 0.2（备份 `.pre-gate-fix-bak`）。
- 测试：self-test 137 项、node --test 41/41 全绿（更新了 4 处因口径变化失效的 fixture）。

## 0.8.0 手动选最佳商家 + 设置页直观展示

- 新增设置页「选最佳商家」按钮：每条已管理的路由旁点击后，按当前候选表（真实成本/成功率/延迟打分）取第一名，排除当前固定商家，直接固定过去并回查确认，1~2 秒完成。
- 后端新增 `/a6-failover/select-best`（POST，body: provider/model）：`switcher.selectBest()` 复用现有选商与账本机制；失败时清理账本、不污染站点 pin。
- UI 每条路由的摘要直接显示「当前 #渠道ID」，展开可见商家/渠道、预计成本、缓存命中率、延迟，选完立即刷新状态。
- 端到端实测：选中 ch7274 并返回完整价格/缓存/延迟信息，测试后已恢复原固定商家。
- 测试仍全绿：self-test 136 项、node --test 41/41、编码检查通过。

## 0.7.9 商家非标准格式错误（reasoning_content）允许换商

- **背景**：某些中转站上游不是标准 OpenAI 格式——思考模式 + 工具调用时，强制要求历史 assistant 消息带 `reasoning_content`（标准格式不需要）。跨商家实测：同一标准请求在 1764/6940/7030 返回 200，只有 5927 返回 400 `invalid_request`。
- **修正**：`stream-guard.js` 的 `failureFacts` 现在把 `reason`/`upstream_message`/`detail` 等字段也纳入错误文本；`classifyFailure` 新增精确识别——`INVALID_REQUEST`/4xx 且错误文本含 `reasoning_content` + `required/missing/缺少/必填`，判为可换商（`switchImmediately`），不再当永久错误放行。其它 4xx（AUTH/QUOTA/MODEL_NOT_FOUND 等）保持 permanent。
- 这样遇到 5927 这类"格式挑剔"的商家会自动换到兼容的商家，无需改请求体。
- 测试：self-test 136 项（新增 5 项格式错误识别）、node --test 41 项全绿。

## 0.7.8 修复 tokenPlainKey 未传连接参数导致崩溃

- `tokenPlainKey()` 函数体引用了 `connectionMode` / `directProbeMs`，但函数签名没收这两个参数，任何调用都会在代理/直连配置下抛 `ReferenceError: connectionMode is not defined`。已补进签名并透传给 `a6Request`。
- 补回归测试：验证 `tokenPlainKey` 能正常返回明文 key（self-test 131 项）。
- 排查 `reasoning_content is required in thinking mode with tools` 报错（用户会话）：确认是**请求体问题**——思考模型要求历史 assistant 消息带 `reasoning_content`，换商家无效（实测带该字段或去掉历史 assistant 均 200）。插件把它判为 permanent 错误不换商是正确行为，未改动这块逻辑。

## 0.7.7 设置持久化：参数固化进默认值，配置文件只留个人凭证

解决「插件更新后设置被恢复默认」的问题。

- **把用户在用的全部非个人参数固化成插件 DEFAULTS**（13 项）：`reliabilityWeight` 10、`latencyWeight` 80、`hardP50Ms` 10000、`maxFailRate` 20、`minContextWindowTokens` 290000、`firstResponseTimeoutMs` 55000、`firstResponseBudgetMs` 60000、`maxSwitchesPerCall` 6、`maxSwitchesPerHour` 50、`takeOverExistingPin` true、`verbose` true 等。这样配置覆盖层被清空/覆盖安装后，生效值仍是用户在用的值。
- **个人凭证仍走外部配置**：`accessToken`、`userId`、`defaultControlBase` 等接入字段保留在 `~/.dsh/a6-failover/config.json`（插件目录外，覆盖安装不碰），该文件已从 57 项精简到 7 项。
- **保存加自动备份**：`saveOverrides` 写盘前先把当前 config.json 复制为 `config.json.bak`（只留上一版）。
- **启动自动恢复**：`loadOverrides` 发现主文件丢失但 `.bak` 存在时，自动恢复一次。
- 验收：精简后的 config.json 合并新默认值，生效配置与原 57 项完整配置逐字段一致。
- 测试：self-test 130 项（修正 1 处依赖旧默认 0 的用例）、node --test 41 项全绿。

> 原完整设置已备份到 `~/.dsh/a6-failover-settings-backup.json`（插件目录外）。

## 0.7.6 移除样本数门槛，每次取候选按最新实测重判真实成本

修掉一个"商家实测变贵了却仍被选中"的问题（典型：4183 实测命中率只有 40%，真实成本是官网的 1.3 倍，超过 0.7 门槛，却因为门槛只在拉表时判一次而一直留在候选里排第一）。

- **移除 `minSamples`（设置页「商家统计至少需要几次请求」）**：不再因站点 `sample_count` 少就淘汰商家。样本少的商家也进候选，好坏交给实测重判。配置/覆盖层/设置页/状态输出全链路删除。
- **每次取候选按最新实测重判真实成本门槛**（`refreshCandidateCache`）：用当前实测命中率重算 `effRatio`，超过 `maxEffectiveRatio`（默认 0.7）当场剔除；实测命中率的商家才重判，无实测的沿用快照判定不误杀。重判同时更新 `effRatio`/`cnyPerMillion`，让打分也用最新值。
- **候选快照补全官方报价字段**：新增 `officialOutputMicros / officialCacheReadMicros / officialCacheWriteMicros / pricingCurrency`，供重算使用。老快照缺字段时跳过重判（等下次刷新），不误杀。
- `selectionKey` 版本 4→5（快照结构变了，旧缓存失效重拉）。
- 实测：新快照 145 家经重判剔除 2 家超标商家，4183（实测命中率 40%、eff 1.30）被正确剔除。
- 测试：self-test 130 项（新增 3 项重判用例）、node --test 41 项全绿。

## 0.7.5 换用市场搜索接口：单模型 571KB，只刷 DSH 在用模型，汇率自动取

这一版把商家表从「每模型全量下载 15MB」改成「服务端按模型过滤 571KB」。

- **新增 `fetchChannelSearch()`**：改用网页市场页在用的 `/api/marketplace/channels/search?model=X&page=1&page_size=500`。
  - 实测同一模型：旧接口 15.47MB / 7.6s（服务端**忽略** model_name，返回全站 7455 listing）；新接口 571KB / 0.4s（服务端已过滤，193 条）。小 26 倍、快 19 倍。
  - 字段与旧接口逐字段一致（67 个），`classifyListing` 需要的报价/延迟/成功率/缓存价全在。
  - **保留旧接口回退**：新接口失败（404/权限变化）自动降级到 `/api/marketplace/channels`。
  - `parseCandidates` 兼容新接口的扁平 `data.items[]` 结构（原来只认 `channel[].listings[]`）。
- **只刷 DSH 实际用过的模型**：后台刷新的模型清单从「已缓存表的模型」改为
  `knownRoutes ∪ prewarmRoutes ∪ owned ∪ allChannels` 并集（只有真实请求过/配置过/固定过/缓存过的模型）。站点上新冒出来、DSH 没添加的模型永远不下载。
- **刷新频率 30min → 1h**：`channelsRefreshMs` 默认 3600000；`channelCacheTtlMs` 默认 3300000（55 分钟）；`channelsTimeoutMs` 默认 30000（新接口够快）。`QUOTE_MAX_AGE_MS` 60min 不变。
- **移除 `fxCnyPerUsd` 配置项**：汇率直接从 listing 的 `realtime_ratio_exchange_rate` 取（站点实时支付汇率，实测 6.6757），取不到时用内置兜底 7.2。设置页对应输入框已删除，配置覆盖层也清掉了这个键。
- `selectionKey` 版本 3→4（配置结构变了，让旧缓存失效重拉）。
- 实测（真实站点）：新接口拉表 1.62s / 71 家（原 25.7s），缓存命中 0ms，真实切换 1.20s 成功 pin ch=5697。
- 测试：self-test 127 项、node --test 41 项全绿，编码检查干净。

## 0.7.4 每次换商独立计时 + 上下文窗口下限

- **首块预算不再整轮共用**：去掉 `totalDeadline`，改成每次尝试各算各的。原商家连同它的同路重试共用一笔 `firstResponseTimeoutMs`（默认 30 秒，到点才换商）；每换到一家新商家重新计一笔 `firstResponseBudgetMs`。设置页把「换到新商家后等首块的上限」从 180 秒改成 60 秒，就是每家新商家最多等 60 秒，不会因为前一家慢就把后面的机会吃光。
- **控制面改用独立超时**：向站点确认固定商家的往返走 `controlTimeoutMs`（独立计时，下限 200 毫秒），既不吃首块预算，超时也只表示这次不换商（`A6_SWITCH_TIMEOUT`），不再让整个请求失败。
- **新增 `minContextWindowTokens`**（默认 `0`，不过滤）：设成 300000 就过滤明显放不下长上下文的商家。站点接口没有窗口字段，因此只认有证据的来源——商家描述里自述的「1M上下文 / 500k context / 25万上下文 / 上下文 128K」，或上游报错里直说的 `maximum context length is N`（本地保留 7 天）。两者都有取较小值；**无证据的商家一律保留**，不按猜测淘汰。设置页商家表新增「上下文」列区分声明值、实测值与未知。
- **新增 `parseContextCapacity`**：只接受上游明确写出的窗口上限。不用 DSH 自己的压缩报错反推，因为那个阈值本来就是按模型窗口减预留算出来的，会把模型的窗口当成商家的窗口。
- 回归：`scripts/guard-test.mjs` 三条依赖共享预算的旧测试改为验证独立计时（19 项全过）；`scripts/self-test.mjs` 新增 14 项窗口解析与过滤断言（127 项全过）；`routing-test` 5 项、`delivery-test` 16 项、编码检查均通过。

## 0.7.3 限制访问提示改为可换商重试

- 识别到占位响应 ID 后，先扣住“临时限制访问”的提示正文；finish 确认是占位错误时丢弃这段文字并按可恢复错误处理，不再冒充成功回答，也不会把错误文案显示给用户。
- 这类错误即使已出现正文也允许换商家；跳过同商家重试，直接切换到下一个候选。普通错误在已输出正文后仍不重播，避免重复交付。
- 新增回归：占位错误触发换商且提示文案不外泄；普通响应 ID 正常交付。

## 0.7.2 识别上游限制访问占位响应

- 将实际会话中确认的 responseId `resp_freeipblk00000000000000000` 从正常 stop 转为 `A6_UPSTREAM_ACCESS_RESTRICTED`，不再记为成功。
- 只匹配该明确标记，不按普通回答的关键词判错。标记在 finish 才出现，已交付内容后不重播；未验证限制来源或换商能否解决。

## 0.7.1 直连优先，失败自动切代理

- **新增 `connectionMode`**（默认 `direct-first`）：先直连，直连在 `directProbeMs`（默认 1500ms）内连不上就自动切代理。可选 `proxy-first` / `direct-only` / `proxy-only`。
  - 直连给短探活超时，避免每次都等 10~20 秒才切代理；连得上就一直走直连，不碰代理。
  - HTTP 4xx/5xx 是站点真的回了，不算「连不上」，不会绕代理。
  - 修了 `viaProxy` 闭包引用了未声明的 `proxyUrl` 变量（会让所有代理请求 0 秒失败并被包装成 `A6_NETWORK`）。
- 设置页新增「连接方式」下拉与「直连探活超时」字段；`lib/overrides.js` 同步加入白名单。
- 实测：`proxy-only` 1.21s OK；`direct-first` 2.38s OK（1.5s 探活失败后切代理）。
- 修复后的全量测试：self-test 108 项、node --test 38 项全绿，编码检查干净。

## 0.7.0 代理支持 + 切换不再现场拉表

这一版解决「网页明明能打开，插件却一直失败」的根本原因。

- **新增 `proxyUrl`**：A6API 的域名（`main.apisnode.cc`）解析到 **172.80.127.x 私有地址段**，直连必然超时，浏览器能打开是因为走了系统代理。Node 的 fetch 不读系统代理，所以之前所有控制面请求都在连一个够不着的私网 IP。
  - 用 Node 内置 `http`/`https` 手写 CONNECT 隧道（不依赖 undici，DSH 的 Node 里 import 不到它）。
  - **CONNECT 之后必须手动 `tls.connect`**：直接把裸 socket 交给 `https.Agent.createConnection`，Agent 会认为「已是 TLS 会话」而跳过握手，明文发到 443 上，边缘节点回 `HTTP 400 Domain Not Configured`——看起来像域名没配，实际是没加密。
  - 隧道串行化：本地代理解析（verge-mihomo）在同时开多条隧道时会掐掉新连接，25 MB 的表和订阅列表并发会互相搞死。
- **补全 `proxyUrl` 透传**：`pinChannel` / `unpinChannel` / `tokenPlainKey` 三个函数之前没带这个参数，导致「查 pin 正常、写 pin 必然失败」。
- **切换只读缓存，绝不现场拉表**：商家表实测 25 MB、15~60 秒。现在 `pickCachedCandidates` 只读本地缓存（毫秒级），表由新增的 `channelsRefreshMs`（默认 30 分钟）后台刷新。
- **新增 `channelsTimeoutMs`**（默认 90 秒）：商家表专用超时，不再和「查 pin」那种几百字节的请求共用 `controlTimeoutMs`（20 秒）。
- `planSwitch` 的整体兜底超时改为 `controlTimeoutMs + channelsTimeoutMs`，只防挂死，不再误伤。
- 实测（真实站点）：预热商家表 25.7s / 73 家 → 切换 **5.81s 成功，pin 到 ch228**。

## 0.6.7 控制面不可用时降级，不再让整轮对话失败

站点整体挂掉（TCP 通、HTTP 全超时）时，插件原本每轮都去试一次切换、失败锁 15 秒、下一轮再试，失败次数一路堆到 36，最后抛 `A6_ROUTE_NOT_READY` 让整轮对话失败。用户看到的就是「处理失败 · 控制面处于冷却期」反复出现。

- **新增控制面熔断**：连续失败超过 `controlUnavailableMs`（默认 **2 分钟**）后整体放弃，不再打扰控制面，直接把请求交给平台默认路由跑完。`0` 关闭（旧行为）。
- **新增低频探活**：降级期间每 `controlProbeMs`（默认 30 秒）只读探测一次，站点一活过来立刻恢复智能选商，并记 `control-recovered` 事件。
- **四处放行**（原本都抛 `A6_ROUTE_NOT_READY`）：
  - `planSwitch` 熔断短路带 `unavailable` 标记，不再去试备用路由；
  - `beginRequest` 遇到 `unavailable` 只记 `control-degraded` 事件并放行；
  - `ensurePlatform` 清理固定商家失败时，控制面不可用就放行（残留 pin 交给平台路由处理）；
  - `restoringAll` / `restorePending` 在控制面不可用时不再拦请求。
- 冷却期本身也参与熔断判定（冷却只表示「别立刻重试」，熔断才是「整体放弃」）。
- 实测（死站）：第 1 次 10.6 秒超时 → 第 2 次熔断 → 入口 **2 毫秒**返回放行，不再抛错。

## 0.6.6 按官网真实账单校准

- **修一个静默失效的 bug**：`normalizeWorkload` 不是幂等的——它读配置键 `unmeasuredMerchantCacheHitRate`，却输出短名 `unmeasuredCacheHitRate`，而 `priceModel` 内部会再洗一次，第二次就读不到，把商家缓存假设**静默清成 0**。后果是所有商家都被按「完全无缓存」估价，真实成本高估数倍，且没有任何报错。现在两套键名都认，`outputToInput` 同理。
- **新增 `officialPriceOverride`**：官网基准价（元/百万 token，`{input, output, cacheRead}`）。实测站点返回的 `official_price` 比 DeepSeek 官网真实账单低一个数量级（站点 ¥0.15 输入 vs 账单折算 ¥1.0 量级），拿站点价当分母会让所有商家都显得贵、门槛误杀合格商家。填了就替换分母，留空仍用站点价。
- 覆盖值按商家那条报文的单位解释，不篡改 `pricing_currency` / `pricing_unit`，避免 `priceBasis` 判币种冲突。
- 实测影响（193 条 listing，官网基准改用真实账单校准后）：成本超线淘汰从 8 家降到 **1 家**，真实成本回到 0.22~0.83 的合理区间。

## 0.6.5 选商口径三项修正

- **价格门槛改用商家自己的真实成本**：以前用「官网同等命中率」判门槛，会放进来「标价便宜、缓存读价极低、实际几乎不命中」的商家（实测案例：input 0.44 倍看着达标，按实测 15% 命中率算是官网的 2.59 倍）。现在有本机实测命中率就按真实成本判；**还没实测过的商家仍按标价倍率判**，不会被 `unmeasuredMerchantCacheHitRate=0` 一次打死。
- **p50 退出打分，只做入选门槛**：`hardP50Ms` 与「取较快一半」继续用 p50，`rankCandidates` 不再读 p50。以前 p50 既筛又算分，等于对历史慢的商家罚两次，还会让「历史上一直很快、此刻很慢」的商家占到不该有的延迟分。
- **延迟得分改用站点「延迟」列**：新增 `siteLatencyOf()`，优先取 listing 的 `last_test_response_ms`（站点最近一次真实响应耗时），没测过才退回 p50 并标记 `p50-fallback`；本机首块实测够 2 个样本时用实测覆盖，标记 `measured-first-block`。状态页新增「打分用延迟」一行，能看出这个数是站点实测、p50 兜底还是本机实测。
- 顺带修掉一个旧 bug：`classifyListing` 过去总把 `unmeasuredMerchantCacheHitRate` 当成实测值传给 `priceModel`，使 `merchantCacheHitMeasured` 误报为 true。现在命中率是三态（实测 / 配置写死 / 未测），未测时交给 `priceModel` 自己兜底。
- 实测影响（193 条 listing、本机 6 家已有实测）：门槛从「全部放行」变成淘汰 66 家成本超线；channel 4137 由第 1 名变为淘汰，7074 由第 8 名升到第 3 名。

## 0.6.4 接管站点已有的固定商家

- 新增 `takeOverExistingPin`（设置页「2 · 商家筛选与打分」→「接管站点已有的固定商家」，默认关闭）。关闭时行为与 0.6.3 一致：站点上出现非本插件设置的固定商家就保留它，本次选商以 `A6_ROUTE_NOT_READY` 结束。
- 开启后：先按价格与质量门槛选候选。选出更合适的商家 → 解除外部固定、改绑候选，失败则把原固定按原 `fallback_to_smart_routing` 还原，并记 `pin-takeover` / `pin-takeover-rollback` 事件；原来那家就是最优 → 只借用（`adopt`），不改站点设置，也不写归属记录，因此空闲时不会替它 unpin。
- 新增事件 `pin-preserved`（保留外部固定时记下渠道与原因）、`pin-adopted`。
- 注意：被解除的外部固定不会在插件空闲后自动恢复，这是接管的代价。

## 0.6.3 设置页更新

- 切换与等待新增：原路首块等待（默认30秒，1～180）、首块总预算（默认180秒，1～180）、输出后停顿（默认30秒，1～600）、原路快速失败重试（0或1次）。保存后对后续请求生效。
- 解释输入单价额外门槛和最少站点样本数；删除无参数排序分组及已不参与切换的旧降级、去重设置。

# 0.6.2 发布说明

- 有效首块总预算从 60 秒提高到 180 秒，包含原路重试、换商及新商家响应。原路 30 秒换商触发和首块后的停顿规则保持不变。

# 0.6.1 发布说明

- 启动和手动恢复平台默认：清除受管令牌下全部模型固定记录并回查，保留其他令牌。失败显示恢复中并后台重试。
- 手动智能路由改为按新请求固定模型，不再提前绑定全部已知模型。
- 启动确认失败返回明确的 A6_ROUTE_NOT_READY。设置页增加等待模型新请求状态。
- 38 项测试通过，包含 63 项集成断言；真实账户曾完成受管令牌 7→0 解绑，其他令牌 4→4 保留。尚未验证真实模型响应速度。

## 0.6.0 发布说明

- 新启动的 DSH 默认交给平台选路，健康请求直接发出。自动接管跟踪所有受管会话，空闲后释放插件持有的固定商家。
- 手动智能路由对当前 DSH 进程生效，设置页可查看各路由的商家、价格、缓存率来源和历史 P50，并可手动恢复平台路由。
- 从发起模型流开始计算首块有效响应。30 秒无进展触发切换，原路重试、切换和等待新商家共享 60 秒首块预算。已产生有效内容时保留输出。
- 候选先过价格和质量门槛，再从历史 P50 最低的一半中选择商家。格式异常留下本地证据，供用户自行判断和举报。

## 0.5.1 发布说明

- 固定商家请求携带 `fallbackToSmartRouting`。开启时允许站点在固定商家不可用时自行选路；只有能确认固定渠道的用量才归入该渠道。
- 已输出内容的请求遇到可恢复故障时，本轮报告错误。默认对可识别的固定渠道记录 `channel-quarantined`，后续选商在隔离期内排除该渠道。`postContentBadTtlMs` 默认 30 分钟，到期后该渠道可重新参与筛选。
- `events.jsonl` 中的 `boot` 事件记录 `version`、预热路由数量 `prewarmRoutes` 和站点智能兜底开关 `fallbackToSmartRouting`。核验安装时，查看最近一次 `boot` 的 `version` 是否为 `0.5.1`。
