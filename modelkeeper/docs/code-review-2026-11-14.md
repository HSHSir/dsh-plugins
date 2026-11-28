# 模型管家 v1.0.4 — 代码审查与智能路由方案修订

审查日期：2026-11-14
审查范围：项目根目录全部 Python 模块（11,552 行），重点 `gui_app.py`、`model_probe.py`、`agent_import.py`、`price_book.py`、`endpoint_book.py`
审查方式：静态阅读 + 接口实测（使用用户授权的 a6-2 账户令牌）

---

## 0. 总体结论

代码质量**明显高于同类桌面工具的平均水平**，以下几点做得扎实，不应改动：

| 项 | 证据 | 评价 |
|---|---|---|
| 线程模型 | 工作线程 + `queue` + `after(120)` 消费（`gui_app.py:1827,1902`） | 正确的 Tkinter 并发范式，未在子线程碰控件 |
| 原子写 | 全部走 `tmp` + `os.replace`（`endpoint_book.py:78`、`price_book.py:151`、`agent_import.py:253`） | 不会产生半截文件 |
| YAML 反序列化 | `yaml.SafeLoader` 子类（`agent_import.py:577`） | 无任意对象构造风险 |
| 响应体积上限 | `MAX_RESPONSE_BYTES = 4MB` + 两段式校验（`model_probe.py:34,244`） | 防止大响应打爆内存 |
| 代理控制 | 直连时显式 `ProxyHandler({})`（`model_probe.py:341`） | 避免环境变量代理污染测量 |
| 备份机制 | 5 槽环形 + 槽位对齐多文件（`agent_import.py:205,287`） | 多文件 Agent 的还原不会出现中间态 |
| 错误信息 | 不回显 Key，只给掩码（`endpoint_book.py:149`） | 无凭据泄漏 |

以下 11 项为可改进点，按「性能 → 安全 → 健壮性」分级。**P1/S1 建议优先处理。**

---

## 1. 性能问题

### P1 · 每敲一个键全表重建 + O(n) 价格扫描（中高）

**位置**：`gui_app.py:1069` → `_fill()` `gui_app.py:1677`；`price_book.py:189-193`

```python
self.ent_filter.bind("<KeyRelease>", lambda _e: self._fill())   # 无防抖
```

`_fill()` 每次执行 `tree.delete(*所有子项)` 再逐行 `insert`，并对**每一行**调用 `pb.lookup()`。当模型名未精确命中预设表时，`_match(prefix=True)` 会执行：

```python
cands = [k for k in models if k.startswith(f"{base}-")]   # 3728 条键的全量线性扫描
```

`prices_preset.json` 实测 3,728 条键、168 KB。按当前 a6api 返回 165 个模型估算，**每次按键 ≈ 165 次线性扫描 + 165 次 Treeview 插入**；模型列表上千（部分中转站 `/v1/models` 返回 2,000+）时单次按键进入**秒级**卡顿，且输入法连打会叠加成积压。

**建议**：
1. 过滤加 150–250 ms 防抖（`after_cancel` + `after`），仅在停顿后重绘；
2. `_match` 的 prefix 分支改为预建前缀索引（`dict[str, list[str]]`，`load_preset` 时一次构建），或对 `lookup()` 加 `lru_cache`（键为 `model_id` + 数据版本号）；
3. `_fill` 改为「差异更新」或至少 `tree.insert` 前 `tree.delete` 一次而非逐行重建（当前已是批量 delete，插入仍逐行，可接受但配合防抖才不卡）。

### P2 · 空闲时 120 ms 固定轮询（低）

**位置**：`gui_app.py:1931` `self.after(120, self._drain)`

无论队列是否为空，主线程每秒唤醒 8 次。单次开销极小，但常驻进程（本工具支持打包 exe 长期开着）会有可测的 CPU 占用。

**建议**：改为「有消息时才续期」——`_drain` 取完队列若为空则延长到 400 ms，或由工作线程 `put` 后触发一次 `after(0, _drain)`。

### P3 · 渠道全量接口 28 MB / 3,371 家，且被 4 MB 上限直接拒绝（高，直接影响新功能）

**实测**（`GET /api/marketplace/channels?model_name=deepseek-v4.1-flash-1017`）：

| 指标 | 无 gzip | 带 `Accept-Encoding: gzip` |
|---|---|---|
| 传输体积 | 29,132,188 B（27.8 MB） | **1,223,274 B（1.2 MB）** |
| 耗时 | 6.09 s | **3.10 s** |
| 渠道数 | 3,371 | 3,371 |
| listing 数 | 13,806 | 13,806 |
| `json.loads` | 0.20 s | 0.20 s |

两个结论：

1. **`urllib` 默认不发 `Accept-Encoding`**，所以现有代码拿到的永远是 28 MB 那一份。加 gzip 后体积降 **23.8 倍**、耗时降 49%，这是零成本的改进。
2. **`MAX_RESPONSE_BYTES = 4MB` 会直接抛错**，智能路由**不能复用** `fetch_models()` 的读取路径，必须写独立的、允许大响应的下载器（或流式边下边筛）。

### P4 · 全量重建丢失选中态与滚动位置（中低）

`_fill()` 与 `_refresh_agent()` 都是 `delete(*children)` 全清再插。测速路径已用 `_refresh_row()` 单行更新规避了这个问题（`gui_app.py:1665`），但**筛选与排序**没有：用户筛选后滚动位置归零、勾选被裁（这一点注释里是有意为之，可接受），体验上仍有跳变。

**建议**：重绘前记录 `tree.yview()` 与首个可见行 iid，重绘后恢复。

---

## 2. 安全隐患

### S1 · 成功探测后静默持久化明文密钥（中）

**位置**：`gui_app.py:1399-1401`

```python
if pending is not None:
    base, key, _protocol = pending
    self._remember_endpoint(base, key)     # 自动写入 ~/.model_probe/endpoints.json
```

**只要探测成功一次**，API Key 就被明文写入磁盘，保留最近 20 条，**用户可以完全不知情**。文档里承认「密钥以明文落盘」，但「自动保存」与「用户主动保存」是两件事：前者等于未经同意收集凭据。

**建议**（任选）：
- 首次保存时弹一次「记住此端点的 Key？」并提供「不再询问」；
- 或默认**不存 Key**，只在用户点下拉历史项回填时存；
- 至少在下拉项旁显示「已保存密钥」标识，让用户看得见自己存了多少把钥匙。

### S2 · Windows 上 0600 实际无效（中，已知取舍）

`endpoint_book.py:84-87` 尝试 `os.chmod(tmp, 0o600)`，注释也承认「Windows 上只能动只读位」。同一用户下的任意进程都可读该文件。

**建议**：若需实质保护，用 DPAPI（`CryptProtectData`，绑定当前用户账户）加密 `key` 字段；或在 README 明确写出「密钥为明文，请勿在共享账户的机器上使用」。

### S3 · 备份目录使密钥副本数 ×5（低中）

`_backup()` 把含 `apiKey` 的 Agent 配置文件快照进备份目录，5 槽环形保留。结果是**同一个 Key 在磁盘上有 6 份副本**（原文件 + 5 快照），删除原文件并不会清除这些副本。

**建议**：README 增补「清理路径」章节；或备份时对 `apiKey` 字段做脱敏（但会破坏「还原即可用」的价值，需权衡）。

### S4 · 依赖 urllib 私有实现改 socket 超时（低）

**位置**：`model_probe.py:619` `sock = resp.fp.raw._sock`

已用 `try/except` 兜底，失败只影响掐断精度，不影响正确性。但 `_sock` 属 CPython 内部结构，小版本升级可能改名（历史上 `_sock`/`_socket` 变过一次）。

**建议**：保留现有兜底，同时在注释里标注适用的 Python 版本区间（当前 3.13 实测可用）。

### S5 · 通过项（无需改动）

- 无 `eval`/`exec`/`pickle`/`shell=True`/`verify=False`（全库 grep 为空）
- `yaml.load` 一律绑定 `SafeLoader` 子类
- 响应体积上限、错误详情截断（`detail[:200]`）均已实现
- 日志与异常路径未见 Key 回显

---

## 3. 健壮性问题

### C1 · 非流式降级路径无整体截止时间（中）

**位置**：`model_probe.py:751`

```python
with opener.open(req, timeout=timeout) as resp:   # timeout 只作用于单次 socket 操作
    raw = _read_bounded(resp)
```

`socket` 超时在每次成功读取后重置。若服务端**缓慢滴流**，单轮可远超 25 s。账户日志里观察到的 **662 s 长尾**与此吻合（流式路径有 `limit_s + timeout` 兜底 `model_probe.py:695`，非流式没有）。

**建议**：对 `_buffered_once` 增加 wall-clock 守卫（读循环里检查 `time.perf_counter() - t0 > deadline` 就抛 `SpeedTooSlow`），并与 `_stream_once` 口径统一。

### C2 · `load_preset()` 返回共享可变 dict（低）

`@lru_cache(maxsize=1)`（`price_book.py:247`）返回**同一个对象**。当前所有调用方只读，但一旦有人原地 `out["models"].update(...)` 就会污染全局且跨调用累积。

**建议**：在 docstring 显式标注「返回值只读，修改前必须 `copy.deepcopy`」，或返回 `MappingProxyType`。

### C3 · 还原操作不可逆（设计取舍，需 UI 提示）

`restore_backup()` 刻意不在还原前备份（`agent_import.py:340` 有充分理由：会挤占环形队列）。代价是**还原错了无法回退**。

**建议**：保留该取舍，但在确认对话框里明确写「还原后当前配置将不可恢复」；或将还原前状态写入独立的 `.pre-restore` 单槽（不参与环形队列），成本几乎为零。

### C4 · 临时文件名固定（极低）

`path.with_suffix(".json.tmp")`（`endpoint_book.py:82`、`price_book.py:155`）在单进程下安全；多实例并行会互踩。同时 `with_suffix` 对含多点的文件名会截断扩展名。当前场景可忽略，仅记录。

---

## 4. 智能路由方案修订

### 4.1 侦察结论：不需要模拟网页操作

原方案默认「像人一样去模型广场点选」，实际 A6API 提供三个**原生接口**，可直接替代全部手工步骤：

| 用途 | 接口 | 实测结果 |
|---|---|---|
| 拉取某模型的全部商家 | `GET /api/marketplace/channels?model_name=<模型>` | 3,371 家 / 13,806 条 listing |
| 查看当前固定 | `GET /api/marketplace/pins` | 返回 3 条现有 pin |
| **固定商家** | `POST /api/marketplace/pin` | body `{token_id, model_name, channel_id, fallback_to_smart_routing}` |
| **取消固定** | `POST /api/marketplace/unpin` | body `{token_id, model_name, channel_id}` |

认证与现有控制台接口一致：`Authorization: Bearer <访问令牌>` + `New-Api-User: <用户ID>`。

**每个渠道可用的关键字段**（`listings[]` 内）：

| 字段 | 含义 | 用于 |
|---|---|---|
| `input_price_micros` / `output_price_micros` | 商户价（微美元 / 1M token） | 价格因子 |
| `official_price.input_price_micros` | 官方价 | 价格倍率分母 |
| `recent_p50_ms` / `p50_ttft_ms` | 平台统计延迟 | 延迟因子 |
| `recent_success_rate` / `sample_count` | 成功率与样本量 | 可信度门槛 |
| `cache_read_price_micros` | 缓存读价 | Agent 场景有效价 |
| `charge_type` | `per_token` / 按次 | 图像类模型另算 |
| `supplier_channel_disabled` / `listing_availability` | 可用性 | 硬过滤 |
| `pin_status` / `route_type` | 当前路由状态 | 展示 |

**价格倍率**计算方式已确认：`input_price_micros ÷ official_price.input_price_micros`（同单位微美元，直接相除；例 396000 / 150000 = 2.64）。

### 4.2 原方案的 4 处关键缺陷

#### 缺陷 1（致命）：测试顺序反了

原设想「先测得通的，再固定」。但**未固定时，请求走的是当前路由**（可能是旧 pin，也可能是平台的智能路由）。此时测出来的延迟与成功率属于**别的商家**，与你列表里那一行没有对应关系。

> **必须改为：pin → 实测 → 通过则保留，失败则 unpin 换下一个。**

单次 pin/unpin 是毫秒级控制面请求，不产生 token 成本，顺序反转的代价可以忽略。

#### 缺陷 2（致命）：`fallback_to_smart_routing` 必须显式设为 `false`

该字段默认 `true`（现有 3 条 pin 均为 `true`）。含义是：当被 pin 的渠道失败时，**自动回落到平台的智能路由**。这会导致「实测通过」的结论失真——测通的其实是**回落后的其他商家**，而你固定的是一个坏渠道。

> 智能路由测试阶段必须传 `fallback_to_smart_routing: false`；测试通过后是否恢复 `true`，由用户选择（建议默认 `false`，稳定性优先；`true` 仅在「可接受的降级」场景使用）。

#### 缺陷 3（严重）：`recent_p50_ms == 0` 会被当成「最快」

实测第一条渠道的 `recent_p50_ms`、`recent_success_rate`、`sample_count` **全部为 0**（无样本）。若按「延迟升序」排序，这些**零样本渠道会全部排在最前**，而它们的真实延迟可能非常差。这正是「前 20 名延迟 < 3 s」这条规则会踩的坑。

> 必须先用 `sample_count` 做可信度门槛（建议 ≥ 20），把无样本渠道排除或大幅降权，再进入排序。

#### 缺陷 4（严重）：加权前没有归一化

价格倍率范围约 0.5–5（无量纲），延迟范围 0–3000 ms。直接 `价格因子 × 权重 + 延迟因子 × 权重` 会**让延迟完全支配结果**（毫秒数值比倍率大三个数量级）。

> 必须先归一化到 0–1（min-max 或对数），再线性加权。

### 4.3 修订后的流程

```
① 取渠道表   GET /channels?model_name=X  → gzip 1.2MB / 3.1s，后台线程
② 硬过滤     listing_availability 可用、渠道未禁用、charge_type 匹配、
             sample_count ≥ 20、recent_success_rate ≥ 0.95
③ 评分排序   score = w_p·norm(log 倍率) + w_l·norm(p50) + w_s·(1 - 成功率)
             倍率以 official_price 为分母；Agent 场景可用「有效输入价」
             （按 cache_read 占比加权）替代名义输入价
④ 取前 N     N = 8（不是 20）。理由：实测才是真信号，平台统计已做过初筛；
             每多测一个就多花一次 token 与 1–3 秒
⑤ 逐个固定   优先级从高到低：POST /pin（fallback=false）→ 用该模型+令牌实测
             （1 轮、max_tokens ≤ 16、prompt 极短）
             通过 → 保留并结束；失败 → POST /unpin → 下一个
⑥ 落地       写字形：已固定 Supplier 666 / Ch 2925 · 延迟 0.87s · 倍率 1.44
             全部不通过 → 恢复原 pin，明确告知未找到可用渠道
⑦ 可撤销     界面提供「取消固定」，对应 unpin（pin 是账户级持久状态）
```

**并发策略**：原方案「两个同时进行」偏保守。建议 **4 路并发、批量推进**（与现有 `probe_speed_many` 的 `workers` 语义一致）：先固定前 4 个并同时实测，任一通过即停止其余、保留最优者。20 个串行两两测试的最坏耗时约 20 × 2.5 s ≈ 50 s；4 路并发 + 前 8 名只需 2 轮 ≈ 5–8 s。

### 4.4 评分模型建议

```
硬门槛（不满足直接淘汰）
  listing_availability 可用 且 未被禁用
  sample_count >= 20
  recent_success_rate >= 0.95
  charge_type 与模型类型匹配（图像类走 per_request_price_micros）

score = 0.45 * price_norm + 0.35 * latency_norm + 0.20 * (1 - success_rate)
  price_norm   = minmax( log(倍率) )
  latency_norm = minmax( p50 )
  权重可切「省钱优先 / 速度优先」两套预设
```

用户可在界面调整权重；默认按上述「均衡」。归一化用 min-max（对同一批候选内部归一），避免不同模型的绝对量级差异。

### 4.5 与平台原生智能路由的关系（定位说明）

站点已内置 `smart_routing`（渠道字段有 `smart_status` / `smart_routing_labels`），且 pin 支持 `fallback_to_smart_routing`。本功能的价值**不是替代平台智能路由**，而是：

> 用**你自己账户的真实调用**为平台的 pin 选择一个可靠目标——平台的路由基于平台侧统计（可能滞后、可能被刷量污染），而实测是唯一能验证「此刻、这条链路、这个 key 能不能出字」的手段。

这一点建议写进 README，避免用户误以为在与平台功能重复。

### 4.6 UI 接入的具体建议

**两列的位置**：Agent 管理页每行是「端点 + 模型 ID」，一行可能对应多个渠道候选。

| 列 | 数据来源 | 刷新时机 |
|---|---|---|
| 延迟 | 本地 `probe_speed` 实测缓存（`self._speed`） | 令牌探测页测速后；智能路由后 |
| 价格倍率 | `/channels` 查询结果（需按 `model_name` 拉全量） | 异步批量补齐 + 缓存 TTL |

**性能约束**：每个模型一次 `/channels` 是 1.2 MB / 3.1 s（gzip 后）。若 Agent 管理页有 10 个模型，串行拉取需 30 s。因此：

- 价格倍率列**必须异步**、**按行可见才拉**、带 **TTL 缓存**（如 10 分钟）；
- 首次进入页面时该列显示「—」，不要让表格等待网络；
- 若要更快，可对批量模型复用同一连接（`http.client` keep-alive）或并发 3 路拉取。

**按钮状态**：与原设想一致——默认 `disabled`，`<<TreeviewSelect>>` 选中一行且该行模型可路由时 `!disabled`。点击后不应阻塞 UI（走现有 `_queue` 消息通道，新增 `route-*` 消息类型）。

**新增的可选列（建议）**：`当前路由`（`pin_status` / `route_effective`）。平台已返回该状态，直接展示「已固定到 Ch 2925 / Supplier 666」比让用户自己去网页核对更有价值。

### 4.7 现存 pin 的事实核查（供用户参考）

当前 a6-1 令牌已有 3 条固定路由，均 `fallback_to_smart_routing = true`：

| 令牌 | 模型 | 渠道 | 供应商 | 状态 |
|---|---|---|---|---|
| a6-1 | deepseek-v4.1-flash-1017 | 2925 | Supplier 666 | active |
| a6-1 | deepseek-v4.1-flash | 2925 | Supplier 666 | active |
| a6-1 | grok-4.7 | 718 | Supplier 201 | active |

即：用户认为「每次手动选」的这个模型，**其实早已被固定**（可能是之前手动操作留下，或平台智能路由写入）。这解释了为什么延迟表现有时稳定、有时突然变差——若这些渠道抖动，`fallback=true` 会静默切换到其他商家，表现为「同一渠道时快时慢」。

a6-2 令牌目前没有任何 pin，全部走平台默认路由。

---

## 5. 实施优先级建议

| 顺序 | 事项 | 理由 |
|---|---|---|
| 1 | P3 的 gzip + 独立大响应下载器 | 新功能的硬前提，同时是零成本全局优化 |
| 2 | 缺陷 1 / 2（pin 先于测、fallback=false） | 顺序错了功能就是错的，与代码量无关 |
| 3 | S1 密钥保存改为显式 | 影响所有用户的隐私边界，越早越好 |
| 4 | 智能路由核心（评分 + pin + 实测） | 主功能 |
| 5 | GUI 两列 + 按钮 + unpin | 交互层 |
| 6 | P1 防抖 + 前缀索引 | 体验优化，独立于新功能 |
| 7 | C1 非流式总截止 | 消除 662 s 类长尾 |

---

## 附录：本次实测的关键数据（可复现）

```
端点  GET https://a6api.vip/api/marketplace/channels?model_name=deepseek-v4.1-flash-1017
请求头 Authorization: Bearer <访问令牌>
       New-Api-User: <用户ID>
       Accept-Encoding: gzip

响应  1,223,274 B（gzip）/ 29,132,188 B（未压缩）
      data: 3,371 个渠道，13,806 条 listing
      json.loads 耗时 0.20 s

单渠道样本字段示例
  input_price_micros      396000     (= $0.396 / 1M)
  official_price.input    150000     (= $0.150 / 1M)  → 倍率 2.64
  recent_p50_ms           0          ← 无样本，切勿当作 0ms
  recent_success_rate     0
  sample_count            0
```
