# 模型管家 · ModelKeeper

把中转站 / 官方端点的模型清单拉下来、测首字延迟与生成速度，再一键写进
**WorkBuddy**、**DeepSeekHarness**、**Codex** 与 **Claude Code** 的配置；这几个客户端的模型增删、
备份与还原也在这里管。

- 源码运行只需标准库 tkinter（读写 DSH 的 YAML 额外要 PyYAML），打包只多一个 PyInstaller。
- 页签 = `令牌探测`（拉模型 + 测速）+ **每个已注册 Agent 一页**（当前四个：WorkBuddy /
  DeepSeekHarness / Codex / ClaudeCode）。

## 新增一个 Agent

界面完全由注册表驱动，加第五个 Agent 不用碰 `gui_app.py`：

1. 写一个适配器模块（参考 `codex_import.py`），提供 `read_*` / `import_*` / `delete_*` /
   `status` / `find_*`；
2. 在 `agents.py` 里追加一条 `AgentDef`；
3. 把 kind 加进 `AGENTS` 元组。

`AgentDef` 里需要显式声明几个差异点，它们是四个 Agent 真实不同的地方，不是配置噪声：

| 字段 | 作用 | 取值示例 |
|---|---|---|
| `row_key` | 树行 iid 是否要带 provider | DSH 同一模型可挂多个 provider、Claude 同一模型可占多个槽位，必须带；Codex 只按 slug 唯一 |
| `column_head` | 首列标题与取值 | WorkBuddy 显示端点，DSH/Codex 显示 Provider，Claude 显示槽位名 |
| `multi_file_restore` | 还原时是否多文件按槽位一起回滚 | Codex 的 config.toml 与 models.json 必须配对；DSH/Claude 单文件 |
| `can_set_active` | 是否有「当前生效模型」概念 | Codex 有（顶层 `model =`），Claude 有（`ANTHROPIC_MODEL`），另两个没有 |
| `hidden_spec_fields` | 配置里没有落点的规格输入项 | Claude 没有逐模型的上下文 / 输出上限 / 图片开关，这几行必须隐藏 |
| `count_unit` / `delete_label` | 数量单位与按钮文案 | Claude 管的是「槽位」，按钮叫「清空选中槽位」 |

冒烟测试与页签数都从注册表推导，加 Agent 后不用改测试里的硬编码数字。

## Claude Code 适配的特殊之处

Claude Code **没有模型清单**——它只有 5 个环境变量槽位，写在 `~/.claude/settings.json`
的 `env` 段里。所以这一页的「一行」是一个槽位，不是一条模型目录：

| 槽位变量 | 界面名 |
|---|---|
| `ANTHROPIC_MODEL` | 主模型（= 当前生效） |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` / `..._SONNET_...` / `..._HAIKU_...` | 对应三个档位槽 |
| `CLAUDE_CODE_SUBAGENT_MODEL` | 子代理 |

三条来自实测的硬约束（`claude_import.py` 模块头有完整说明）：

- **Base URL 不能带 `/v1`**。Claude Code 自己拼 `/v1/messages`，填 `https://host/v1`
  会变成 `/v1/v1/messages`（404），而客户端的报错是
  `It may not exist or you may not have access to it`——看着像模型不存在，实际是路径重复。
  所以 `normalize_base()` 会剥掉尾部 `/v1` 并在结果里明确告知。
- **密钥写 `ANTHROPIC_AUTH_TOKEN`，不写 `ANTHROPIC_API_KEY`**。后者在自定义 baseURL 下
  会让客户端首次启动弹「是否使用此 API Key」的交互确认；两者并存时 AUTH_TOKEN 优先，
  留着旧 API_KEY 就是「看得见却没生效」，写入时会被移除并报出来。
- **上下文 / 输出上限 / 图片开关没有落点**。上限只有全局 `CLAUDE_CODE_MAX_CONTEXT_TOKENS`，
  导入对话框因此隐藏这几项并给出解释，而不是填了当没看见。

槽位分配规则（`assign_slots`）：单个模型导入时占满 5 个槽位；批量导入时名字里带
`opus` / `sonnet` / `haiku` 的各认领对应槽位，没被认领的仍落在第一个模型上——
留空槽会让 Claude Code 用回自己的默认模型名，那在这个端点上通常不存在。

```bash
python tests/test_claude_import.py     # 适配器回归（槽位分配 / URL 归一 / 密钥互斥 / 只动 env）
```

## Codex 适配的特殊之处

Codex 的模型配置是**双文件 + 强 schema**，与另两个 Agent 结构不同：

| | WorkBuddy | DeepSeek Harness | Codex |
|---|---|---|---|
| 文件 | `~/.workbuddy/models.json` | `cordis.patch.yml` + `.credentials.yaml` | `config.toml` + `models.json` |
| 端点在模型条目里 | 是 | 否（引用 provider） | 否（全局 provider） |
| 模型元数据 | 少数字段 | 少数字段 | **完整对象，缺一个必填字段就启动失败** |

三条实现要点：

- **TOML 只做行级局部改写**。标准库只有只读的 `tomllib`，整份重序列化会丢掉注释和
  未知段（plugins / marketplaces / mcp_servers 都是用户手写带注释的）。
- **新条目继承本机已有的 `instructions_template`**（约 21K 字符的官方提示词），不自己编。
  只有目录为空时才用内置精简模板，并明确警告「能跑但缺完整行为规范」。
- **schema 必填集**（实测 13 项）：`slug`、`display_name`、`description`、
  `default_reasoning_level`、`supported_reasoning_levels`、`shell_type`、`visibility`、
  `supported_in_api`、`priority`、`support_verbosity`、`truncation_policy`、
  `experimental_supported_tools`、`model_messages`。

```bash
# 用真实 Codex 客户端校验写出的配置能否被接受（不碰真实 ~/.codex）
python scripts/verify_codex_live.py claude-opus-5-5
```

## 运行

| 方式 | 命令 | 说明 |
|---|---|---|
| 源码 | `python main.py` | 有控制台，方便看报错；在任意目录执行都可以 |
| 源码（模块方式） | `python -m modelkeeper` | 等价入口，需在项目根执行 |
| 源码（无控制台） | 双击 `启动模型管家.bat` | 用 pythonw 起 `main.py`，需要本机装 Python |
| 打包版 | 双击 `dist\模型管家.exe` | **目标机器不需要 Python** |

包内模块统一用相对导入（`from . import model_probe as mp`），因此**不要**再用
`python modelkeeper\model_probe.py` 这种方式直接跑单个模块——要么走 `main.py`，
要么用 `python -m modelkeeper.model_probe`。

## 打包 exe

本机已验证的环境：

| 组件 | 版本 / 位置 |
|---|---|
| Python | 3.14.10（`python --version` 自查；安装时勾选 Add python.exe to PATH） |
| tkinter | Tk 9.0（Python 官方安装包自带） |
| PyInstaller | 6.22.3（`python -m pip install --upgrade pyinstaller`） |
| pip 源 | 清华镜像（已在 pip config 里配好，无需代理） |

一键打包：

```bat
打包exe.bat
```

额外参数会透传，例如 `打包exe.bat --no-bump`（不升版本号）。

### 两个会让人白折腾半天的坑

**① 打包必须用带 tkinter 的解释器。** PyInstaller 不会替你补 Tcl/Tk：用无 tk 的
解释器照样能「打包成功」，但生成的 exe 双击瞬间退出，窗口都来不及画，日志里只有一行
容易被忽略的 WARNING。本机 `python` 恰好指向一个精简版 3.13（无 tkinter），
而 `py -3.14` 有——所以 `.bat` 会先逐个试 `py -3.14 / -3.13 / …`，挑第一个
`import tkinter` 通过的再打包；`build_exe.py` 自己也会做同样检查，缺 tk 就自动
换一个解释器重跑（找不到就明确报错，不会产出打不开的 exe）。

**② 源码被存成 CRLF 会让打包死在版本号上。** `version_bump` 用
`^APP_VERSION = "x.y.z"$` 取版本号；CRLF 文件里 `$` 前面多一个 `\r`，正则一行都
匹配不到，报「找不到 APP_VERSION」，而文件看上去完全正常。现在正则用
`(?=\r?$)` 收尾，CRLF 也能读能写且不改变原换行风格；项目自身仍统一 LF
（`test_version_bump.py` 里有专门用例钉住这条）。

它内部调 `scripts/build_exe.py`，等价于手写：

```bat
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name 模型管家 --icon "modelkeeper\assets\keeper.ico" ^
  --version-file version_info.txt ^
  --add-data "modelkeeper\assets\keeper.ico;assets" ^
  --add-data "modelkeeper\assets\prices_preset.json;assets" main.py
```

产物 `dist\模型管家.exe` ≈ 13.7 MB，单文件；启动时自解包到 `%TEMP%\_MEIxxxx`，
所以第一次启动会慢 1–2 秒，属正常。`build\`（约 35 MB）与 `version_info.txt`
都是中间产物，可删；`模型管家.spec` 是手工维护的等价配置，用于排障，建议保留。

### 版本号

exe 属性的「文件版本 / 产品版本」只有一处来源：`model_probe.APP_VERSION`（当前 `1.0.17`）。
`scripts/build_exe.py` 每次打包都会据此重写 `version_info.txt` 再传给 PyInstaller，
所以不要手改 `version_info.txt`（会被覆盖）。
同一个常量还拼出窗口标题 `模型管家 v1.0.17`（见 `model_probe.WINDOW_TITLE`），标题栏左上角就能看到。

**版本号会自动递增**，规则在 `scripts/version_bump.py`：

| 情况 | 行为 |
|---|---|
| 源码有实质变更 | patch 位 +1，写回 `modelkeeper/model_probe.py` |
| 源码无变更 | 保持原版本不动（反复打包不会空涨） |
| 首次打包（无历史记录） | 视为有变更，+1 并建立基线 |
| `python scripts/build_exe.py --bump` | 强制 +1，不管有没有变更 |
| `python scripts/build_exe.py --no-bump` | 保持不动，不管有没有变更 |

进位规则是**逢 10 不进位**：`1.0.9` → `1.0.10` → `1.0.11`（标准 semver，patch 位不封顶）。

判定的依据是所有 `*.py` 的内容哈希，存在 `.version_state.json` 里（首次打包自动生成）。
`__pycache__`、`build`、`dist`、`.workbuddy` 等目录不计入，所以打包产物变动不会误触发。

> **一条必须守住的不变量**：算指纹前会先把 `modelkeeper/model_probe.py` 里的 `APP_VERSION` 行
> 归一化成占位符。否则「升版本」这个动作本身就会改变指纹，下次打包又判定为有变更、
> 再次 +1，版本号会无限自增。改动 `scripts/version_bump.py` 时不要绕过这一步。

手动只查不写：`python scripts/version_bump.py --check`（预演）；`python tests/test_version_bump.py` 跑回归。

## 勾选：框选后整组切

- 点第一列的勾选框 → 切换**这一行**；点其他列不动勾选（只移动行高亮）。
- **拖选 / Shift / Ctrl 框出多行后**，点其中任意一行的勾选框（或双击任意一行）→ 这**一批一起翻**：
  已全勾就整组取消，否则整组勾上（部分勾选时是「补全」而不是把已勾的翻掉）。
  逐行刷新、不整表重绘，所以框选的高亮不会在连点两下之后散掉。
- 表头的 ☐/☑ 仍然只作用于**当前可见行**（筛选后全选不该勾上被筛掉的行 —— 那种「看不见的选中」
  最容易在测速时白花钱）；部分勾选时表头显示 ◪。

## 备份与还原

每次写入/删除之前，把目标配置快照进 `~/.model_probe/backups`，**环形保留最近 5 份**：

| 槽位 | 文件名 | 含义 |
|---|---|---|
| 1 | `cordis.patch.yml.bak` | 最近一次写入前的内容 |
| 2…5 | `cordis.patch.yml.2.bak` … `.5.bak` | 更早的快照，最老的自动淘汰 |

- 管理页的「还原」：只有 1 份时直接确认；**有多份时弹出选择框**让你挑哪一份（连写错两次时，
  最新那份往往也是错的，所以不默认替你选）。
- 还原前不会再备份一次——那会把"当前状态"挤进队列，把你要退回的那份往后推。
- 快照读不到「这次备份成功」还是「这次根本没备份」时，界面会带快照时间。

## 最大输出（maxTokens）

导入对话框里新增「最大输出」，空着表示不写这个字段、跟随提供方默认。
预填顺序：目标配置里的现值 ＞ 端点探测披露的 `max_output_tokens` ＞ 空。
写入时 DSH 落到 `maxTokens`，WorkBuddy 落到 `maxOutputTokens`；填错格式（非数字且无 K/M 后缀）
会在确认时拦下，不允许静默变成"不写"。

## 同一 Base URL 下的多把 Key（多令牌同模型）

中转站常常只有一个域名，但**每个令牌能调的模型与额度不同**。于是会出现
「同一 Base URL + 同一模型名 + 不同 Key」需要并存的情况。各 Agent 的承载能力不一样：

| Agent | 能否并存两份 | 依据 |
|---|---|---|
| **DeepSeek Harness** | **能** | provider 才是「地址 + 密钥」的单位；两个 provider 各挂一份同名模型 |
| WorkBuddy | **不能** | 模型清单以 `id` 唯一，同名模型只能有一条 |
| Codex / Claude Code | 不能 | 供应商按 slug / 槽位全局唯一 |

**DSH 的判定规则**（`import_dsh`）：同一 `baseURL` 下先按 **Key 比对**找 provider，
找不到才新建。导入对话框的 Key 预填顺序是「**探测页刚填的那把** ＞ 目标配置里的现值」——
反过来的话，你把探测页切成令牌 B 再点导入，对话框会悄悄填回令牌 A 的 Key，
结果就是「又用 A 导了一遍、还是只有一条」。

- 同一把 Key 再来一次（含 `sk-` 前缀写法差异）→ 复用原 provider，只更新模型参数，
  **不改写凭据文件**。
- 不同 Key → 新建 provider，命名带令牌标签：`a6api` + `a6api-a6-2`，
  密钥分别落在 `A6API_API_KEY` 与 `A6API_A6_2_API_KEY`，互不覆盖。
  标签取自站点里的令牌名（`a6-2`）；查不到凭证时退回 Key 尾四位。
- 不带 Key 的导入 → 复用同地址已有的 provider，不会凭空多出一个。
- 老配置里 provider 已在、但凭据文件没有对应条目（手填 / 换机器）→ 认领它并写入新密钥。

**WorkBuddy 侧**在写入**前**就拦一下：同 `id` 且 Key 不同时会弹确认框
「继续会替换原有条目的 Key（不会变成两个模型）」，取消即不落盘；写入结果里也会再说明一次。
要真正并存只能走 DSH。

## 端点记录：Base URL + API Key

Base URL 输入框右侧有「历史」按钮，点开是最近成功用过的端点；点一条会**同时填回 URL、Key 与协议**。

- **何时记录**：点「获取模型」**成功之后**才记。失败一次多半是 Key 填错，
  把错钥匙存下来只会污染下拉。记录用的是**这次请求实际发送的** URL 与 Key
  （`_start` 里捕获），不是回调时再读一次输入框——请求可能跑了几秒，
  期间用户改过输入框，回头读到的「现值」已经不是让它成功的那个值了。
- **同一 URL 只留一条**：再去获取时原地替换成最新的 Key，并把这条提到最前。
  去重键会忽略大小写与结尾斜杠（`x/v1` 与 `x/v1/` 是同一个端点），
  但显示仍保留**首次登记时的写法**，否则下拉里会随输入习惯来回跳。
- **空 Key 不覆盖**：只填 URL 就点获取（Ollama 这类本来不要 Key）也应当成功，
  那种情况下不会把之前存的密钥抹掉。
- 最多保留 20 条，超出按最近使用淘汰；下拉底部有「删除全部记录…」。
- 菜单里 Key 显示为掩码（`sk-…1234`），能认出是哪把钥匙，又不把整串摊在屏幕上。

存哪：`~/.model_probe/endpoints.json`，与界面配置 `~/.model_probe.json` **分开**。
分开的用处在「坏哪份只坏一半」：删掉配置不会丢 Key，反之亦然。
密钥明文保存（单人本机工具，不联网上报），别同步到网盘或提交进仓库。

## 测速：首字与生成速度

### 推理模型：为什么默认 16 个 token 会「测不出正文」

`deepseek-v4.1-flash-1017` 这类模型的返回分两段：先 `reasoning_content`（思考），
再有 `content`（正文）。输出上限设成 16 时，16 个 token 全被思考吃掉，正文一个都没有——
**请求是 200 成功的**（站点日志里能看到），只是没有任何正文增量。

旧版本把这种情况判成「测速失败」，日志对不上就是从这里来的。现在的处理：

- **首字认推理帧**：`reasoning_content` / `reasoning` / `thinking_delta` / gemini 的
  `thought` 都算「模型开始产出」，首字延迟从**第一个推理帧**算起。
- **自动放大一次预算**：检测到「只有推理、没有正文」且当前上限 < 192 时，自动用
  192 再测一轮（`max_tokens × 12`，至少 192），这样既拿得到首字也量得到速率。
  只放大一次，不会无限替用户加预算。
- 放大后仍然只有推理：按**可用**处理（`ok=True`），首字有值、速率为 `—`，
  并提示「要测速率请把输出上限调到 192 以上」。
- 失败原因会写进探测页底部的提示行——以前只显示「失败」两个字，只能去站点翻日志对时间。

- **按钮文案带数量**：`测首字与速度(4)` 表示这次要测 4 个模型；没勾选时文案回到不带数字，
  且按钮置灰不可点。勾选框在表格最左列，勾了几个本来要自己数——写进按钮就不必等跑完
  才发现测错了范围。
- **测速进行中按钮锁定**，文案固定为 `测首字与速度`（不带数字）：本次范围在开跑那一刻就
  固定了，期间改勾选若让数字跳动，会让人误以为本次测速的范围变了。
- **并发默认 8 路，上限 16**。测速是纯 IO 等待（等排队、等首字），线程本身几乎不吃 CPU；
  真正的约束在端点侧，有两条：中转站的并发限流（429），以及带宽跑满后互相拖慢、
  把「模型能力」测成「拥塞下的值」。
- 本地假端点基准（32 个模型、每请求固定 250ms 首字 + 40 帧、帧间隔 10ms）：

  | 并发 | 总耗时 | 相对 1 路 | 首字中位 | 生成速度中位 |
  |---|---|---|---|---|
  | 1 | 22.9 s | 1.00 | 252 ms | 138 |
  | 4 | 6.1 s | 0.27 | 264 ms | 136 |
  | 8 | 3.4 s | 0.15 | 282 ms | 133 |
  | 16 | 2.4 s | 0.10 | 320 ms | 128 |

  16 路仍是线性收益、程序侧无瓶颈，但首字与速率开始有约 10% 的偏移——那是测量被自身流量
  干扰，不是程序变慢。所以**默认 8**，16 留给不限流的自家端点。
- **首字超过 10 秒即放弃**：状态列显示「超时10s以上」（黄色），数值列显示 `-`，
  不重试、不跑后续轮次。文案由 `gui_app.SLOW_LABEL` 统一供给表格状态、筛选下拉和状态栏汇总。

## 参考价格表（¥ / 1M tokens）

价格**不来自端点**：标准 `/models` 只返回 id，中转站的实付价和官方挂牌价也不是一回事。
所以工具**出厂自带一张价目表**，定位是「参考价」—— 用来判断模型贵不贵，不追求分毫精确。

### 出厂价（`prices_preset.json`）

发版前跑一次 `python scripts/fetch_prices.py`，从 LiteLLM / OpenRouter 的公开价格快照生成。
当前文件 **3705 条记录**（裸名去重后 1780 个模型，索引展开约 5169 个键），约 154 KB，
并带上来源 URL、抓取日期和汇率：

- 源数据是「美元 / token」，生成时归一成「美元 / 1M」，**运行时**再按 `fx.usd_cny`（1 USD = 7.2 CNY）
  折成人民币 —— 汇率变了不用重拉文件，也不会把汇率硬揉进价格里。
- 索引时会去掉厂商前缀和区域后缀（`anthropic.claude-x`、`aihubmix/claude-x`、
  `anthropic.claude-x-20241022-v1:0`），所以裸名 `claude-x` 也能命中；原始键会展开成约 5169 个索引名。
- 只剥「纯字母短词 + 点」的前缀：`longcat-2.0`、`Seed-2.0-code` 里的点属于模型名，不能按点暴力切。
- 查找还支持「前缀取最新」：`claude-3-5-sonnet` 会命中 `claude-3-5-sonnet-20241022`。
- 每条只存**一个数**（美元 / 1M 的输入价），出价不再收进文件。

### 你的修改（`~/.model_probe/prices.json`）

- **「参考价格表」给的是全量清单**：出厂 1787 条 + 你的修改，带搜索框（几千条不搜索没法用）。
  列表只有 **模型 ID / 输入价** 两列，按 ID 排序；右上角统计「共 1787 条 · 已改 2 条」。
  不显示「哪一条来自出厂、哪一条是你改的」—— 想知道某条改没改，选中它点「恢复预设价」，
  没改过会直接告诉你「本来就是预设价」。
- **不修改就一直是出厂价**：你的文件里只存覆盖项，所以升级程序带来新出厂价时，你没动过的模型会自动跟上。
- 匹配顺序：**精确 ID → 去尾部日期/编号 → 通配 `*`（更具体的先命中）**，且**永远压过出厂价**。
  `deepseek-v4.1-flash-1017` 命中 `deepseek-v4.1-flash`；`claude-3-5-sonnet` 不会被砍成 `claude`。
- 「保存」写入覆盖 —— 按钮默认置灰，动了任一输入框才点亮，存完立刻回到置灰；
  「恢复预设价」删掉覆盖，这一条就回到预设价。
- 输入价必填（填 0 表示免费，留空不保存）；通配条目（如 `claude-*`）直接写带 `*` 的 ID。
- 列表里只有一个数字；**查不到就是 `—`，绝不当 0**，排序时 `—` 永远垫底。
- 探测页与管理页那一列表头都叫 **预估价格**，悬停表头显示「预估输入价格 / 百万 token（元）」。
  单位不写进表头：一列数字后面挂「¥ / 1M tokens」会把列宽吃掉一半，而这列的量纲对每个模型都一样。

### 为什么只记输入价

价格表**只存「输入价 / 百万 token」一个数**，不再分别记入价与出价：

- 判断「贵不贵」看输入价就够，而它恰好也是智能路由打分用的那个量（商户输入价 ÷ 官方输入价）。
- 输出价各家倍率算法差异太大（有的按 3×、有的按 4×，还有按缓存命中分档），记下来也没法横向比，
  两列并排反而会让人以为有一列漏填了。
- 预设文件因此从 `[入, 出]` 二元组改成单个数字，文件从 400 KB 降到 154 KB。
  读盘时仍兼容旧写法（`[入, 出]` 与 `{"in":…,"out":…}` 都只取输入价），升级不必重新下载。

**不做**的两件事：运行时不联网拉价（跟中转站实价有差，也拖慢启动）、不做「性价比」派生列
（质量未知时价格÷速度会把"便宜"说成"划算"）。想对比就点「价格↑」+「速度↓」。

**换台电脑也能看到预估价格**：`prices_preset.json` 通过 `--add-data` 打进 exe，
运行时由 `sys._MEIPASS` 找到，不依赖用户目录里的任何文件。验证方式（干净配置目录、
本地假端点、真实 exe）见「踩过的坑」第 7 条。

## 智能路由（A6API 等 new-api 中转站）

中转站的同一个模型背后有几十上百个商家，价格与稳定性随时变动。手动挑商家的流程是
「模型广场 → 选模型 → 按延迟排序 → 选令牌 → 逐个点进去比对价格 → 再拿工具实测」，
本功能把这一整套自动化。

**入口**：Agent 管理页选中**一个**模型 → 「智能路由」按钮（未选中时置灰）。

### 三个原生接口，不模拟网页操作

| 用途 | 接口 |
|---|---|
| 该模型的全部商家 | `GET /api/marketplace/channels?model_name=<模型>` |
| 当前固定关系 | `GET /api/marketplace/pins` |
| 固定 / 取消固定 | `POST /api/marketplace/pin`、`POST /api/marketplace/unpin` |

认证要**两个头**：`Authorization: Bearer <系统访问令牌>` 与 `New-Api-User: <用户ID>`
（少前者报 `not logged in`，少后者报 `user_id_missing`，都不是「令牌无效」）。
凭据在**右上角「设置」**里填一次，按**控制面域名**存在 `~/.model_probe/routing.json`，
之后不再询问；同一站点的 `api.x.com/v1` 与 `x.com/v1` 两种写法都能命中同一条凭据。

实测需要一个 `sk-` Key：工具会用它反查属于哪个令牌（pin 要按令牌来）。
反查时会**去掉 `sk-` 前缀再比对**——客户端配置里普遍写成 `sk-xxxx`，而站点返回的
令牌本体是裸串，直接比必然对不上。找不到时会列出该账户现有的令牌名便于核对。

### 挑商家的顺序

```
① 拉商家     /marketplace/channels，gzip 后约 1.2 MB / 3 秒
② 硬门槛     渠道未禁用、商户输入价>0、倍率 ≤ 上限（默认 0.8）、
             p50 ≤ 3 秒、样本 ≥ 20、成功率 ≥ 95%
③ 打分排序   占比由界面填的整数权重折算：占比 = 单项 ÷ 三项之和
             默认 45/35/20 → 0.45×价格分 + 0.35×延迟分 + 0.20×失败率
             价格取 log(商户输入价/官方输入价)，与延迟各自 min-max 归一化
④ 取前 N     默认 8 家（不是 20：实测才是真信号）
⑤ 逐个固定   pin(fallback=false) → 真实调用 → 连续 2 次通过才留，否则换下一家
⑥ 全败还原   把原来的 pin 写回去，或 unpin
```

**「通过了」到底意味着什么**：一次 HTTP 200 远远不够。中转站在上游失败时，经常
**照样回 200、照样走 SSE**，只是正文写的是「上游服务器返回了无效响应或响应格式不可用，
请重试」；也有把正文给成空的。所以实测要过三道：

| 检查 | 判定 | 为什么需要 |
|---|---|---|
| 正文非空 | 空正文 → 失败 | 端点通了不等于模型产出了内容 |
| 正文不像错误提示 | 命中错误词表 → 失败 | 「HTTP 200 + 错误文本」是最常见的假可用 |
| 正文含期望标记 | 回复里必须有 `PONG` → 失败则淘汰 | 空话、乱码、模型被换都能被这一条抓住 |

再加两条**次数与形态**的约束：

- **连续 2 次通过**（`连续校验次数`，可在设置里改）。坏商家多是间歇性的：通一次
  就固定，之后每次真实调用都会失败；多花一个几十 token 的请求，换掉一次错误固定。
- **第一次走流式、第二次走非流式**。中转站常见「流式正常、非流式挂」或反过来，
  两种形态都过才说明这家真的能用。首字只取流式那一轮（非流式测不出首字）。

输出预算因此不能是 16：推理模型会把 16 个 token 全花在思考上，正文为空，内容校验
必然失败——那不是商家坏，是预算给少了。现在默认 **64**，旧配置读盘时自动抬到 64。

**选中之后商家又坏了怎么办**：Agent 页的「快速测速」就是干这个的。一家都测不通时，
会直接问「要立刻重新智能路由换一家吗」——点「是」就走完整路由。站点整站 502 时
不会问（换哪家都一样），只提示稍等重试。

**为什么必须「先固定、后实测」**：没固定时请求走的是当前路由（旧 pin 或平台智能路由），
测出来的是**别的商家**。顺序反过来，测通与固定对象就对不上了。

**为什么 `fallback_to_smart_routing` 要传 false**：它默认 true —— 被 pin 的渠道失败会自动
回落到平台智能路由。带着 true 去测，测通的可能是回落后的其他商家，而你以为固定的是它。

**为什么不能并行测**：pin 是 (令牌, 模型) 维度的全局状态，同一时刻只能有一个目标；
也不存在按请求指定渠道的请求头。想真并行得为每个候选配一个令牌，不在当前设计内。

### 站点整站 502 时会发生什么

2026-11-14 晚出现过一次：站点首页、`/api/status`、`/api/marketplace/pins`、`/v1/models`
全部返回 nginx 的 `502 Bad Gateway`，持续约二十分钟。**这不是令牌、模型或配置的问题**，
是站点侧后端不可用（同一时刻服务端网络抓 `/api/status` 是 200，说明边缘节点没回源）。

工具侧做了三件事，让这种情况可读、可恢复：

- **502/503/504/500 自动重试 3 次**（退避 0.8 s、2 s），且不越过该请求的总截止时间。
  网关抖动通常重打一次就通；站点真挂了也只是多等约 3 秒。
- **错误正文转人话**：nginx 的 HTML 错误页不再原样塞进状态栏，只取 `<title>` 里那句，
  并在前面说明含义——「HTTP 502：站点网关故障（后端没响应），通常是站点侧临时故障」。
- **失败信息带下一步**：含「网关/502/503/504」时补一句「站点侧故障，稍等一两分钟再点一次即可」。
  快速测速失败同理，写「站点网关故障（HTTP 502），与令牌和模型无关」。

顺带修了一个隐患：有些网关会把响应 gzip 压缩却漏标 `Content-Encoding`，
此前会当成二进制报「返回的不是 JSON」，现在按 `\x1f\x8b` 魔数兜底解压。

### 两个新增的列

- **价格倍率** = 商户输入价 ÷ 官方输入价（接口里两个都是 micros，同单位直接相除）。
  未查过显示 `—`。
- **预估价格** = 该模型在价格表里的输入价（元 / 百万 token）。路由成功时**自动写入**
  实测通过的商户价——商户价才是用户真正支付的数，官方价只是分母。
  表里没有就显示 `—`，写完后探测页与管理页同时刷新。
- **延迟（秒）** 留空 = 没测过，`—` = 测过但没拿到首字。三个途径都会写：本页的
  「快速测速」、令牌探测页测速后回填、智能路由成功后的实测值。
  没有首字的样本（降级为整包计时）不写，不拿总耗时冒充首字。

> 延迟与价格倍率都按 **(端点, 令牌, 模型)** 三件套存。同一中转站的两个令牌各自 pin 到
> 不同商家，延迟可能差一倍——只按 (端点, 模型) 存的话，后测的会把先测的盖掉，
> 两行显示同一个数，看起来像「两家一样快」。

### 快速测速（管理页内直接测）

选中一行或多行 → 「快速测速」，**固定 1 轮 + 输出上限 16 token**，只量首字够不够快，
不做别的。端点与 Key 直接取该行的：行里没有 Key 时退回端点簿（探测页用同一端点取过
模型列表就会记住）。

- 多选时**串行**测：几家同时跑会互相抢带宽，首字被自己人挤高，反而测不出单条链路的水平。
- 失败不写数字，延迟列写 `—`，具体原因进状态栏（如「端点返回 502」）。
- 探测页的「送去探测」是另一件事：那个会跳到探测页、附带拉模型列表。只是想看一眼
  这条配置快不快，不必来回切页。

> **单位约定**：界面上延迟一律用**秒**（`2.15 s`），毫秒只存在于内部计算与接口字段里；
> 价格一律是**元 / 百万 token 的输入价**。两处都不一样的话，同一个模型在两个页面
> 会显示成两个数，用户没法判断哪个是真的。

### 设置（窗口右上角）

只有一个入口，放在页签栏右侧：里面三段都是**全局**策略，与当前在哪一页无关。

- **探测行为**：走系统代理、路径 404 时自动再试 `/v1`。这两项原来在令牌探测页，
  现在收进来——它们影响所有请求（含智能路由的商家查询与实测），不是单页选项。
  保存即生效并立刻写盘，不用等退出程序。
- **站点凭据**：控制面地址 / 用户 ID / 访问令牌，填一次永久保存（令牌默认打码，勾「显示」可核对）。
  三项要么都填、要么都留空，半套凭据会被拦下。
- **打分参数**：价格/延迟/成功率权重，以及倍率上限、延迟上限、样本门槛、成功率门槛、
  实测家数等。三项权重填 **1–100 的整数**（相对份数，不是百分比），工具按
  「单项 ÷ 三项之和」折算成占比；`100/100/100` 等于各占 1/3，`50/35/15` 等于 50%/35%/15%。
  面板下方会实时显示折算后的占比与打分式，保存时再回报一次。
  坏值或越界值在读盘时回落默认，不会拦住功能。

### 已知边界

- 每次拉商家都要下 1.2 MB（gzip 后）、约 3 秒，暂未做服务端分页（接口不吃分页参数）。
- 只有 `charge_type = per_token` 的模型适用；按次计价的图像/视频模型倍率口径不同。
- 固定在平台侧是持久状态，换商家会覆盖前一个；换完想撤销，用站点「模型广场 → 模型详情 → 取消固定」。

## 目录

```
模型管家/
├── main.py                     启动/打包入口（顶层 import gui_app，PyInstaller 才能静态分析到）
├── modelkeeper/                应用包（只依赖标准库 + PyYAML）
│   ├── __init__.py             包元信息；__version__ 转发自 model_probe.APP_VERSION
│   ├── __main__.py             `python -m modelkeeper`
│   ├── gui_app.py              界面层：探测页 + 按注册表生成的 Agent 管理页、自绘 CheckBox、各对话框
│   ├── agents.py               Agent 注册表：新增 Agent 只改这里（差异显式化，见「新增一个 Agent」）
│   ├── agent_import.py         配置读写：WorkBuddy models.json / DSH cordis.patch.yml + 环形备份与还原
│   ├── codex_import.py         配置读写：Codex config.toml（行级 TOML 编辑）+ models.json（强 schema）
│   ├── claude_import.py        配置读写：Claude Code settings.json 的 env 段（5 个模型槽位）
│   ├── model_probe.py          网络层：模型列表、首字、测速（不依赖 GUI，可单测）
│   ├── smart_route.py          智能路由：拉商家、打分排序、pin/实测/还原（不依赖 GUI，可单测）
│   ├── http_json.py            通用 JSON 请求：gzip + 64 MB 上限 + 总时长截止 + 网关 5xx 重试
│   ├── price_book.py           参考价格表：出厂价折算 + 你的覆盖与匹配（纯标准库）
│   ├── endpoint_book.py        端点记录：Base URL → API Key 配对，供探测页一键回填
│   ├── model_spec.py           模型规格：上下文 / 最大输出 / 图片 / 推理档位 + 对话框默认值
│   ├── paths.py                资源目录（源码运行 vs PyInstaller 单文件运行的唯一分叉点）
│   └── assets/
│       ├── keeper.ico          图标（16–256 七档）
│       └── prices_preset.json  出厂价（scripts/fetch_prices.py 生成，随程序分发）
├── tests/                      自测（可直接 `python tests/xxx.py`，无需安装）
│   ├── test_*.py               单元测试：导入 / 探测 / 价格 / 路由 / 版本号
│   └── gui_smoke.py            界面冒烟：真开窗口走完三条主路径（含 Codex / ClaudeCode 页）
├── scripts/                    开发与发布工具（不随 exe 分发）
│   ├── build_exe.py            一键打包：版本自增 → version_info.txt → PyInstaller
│   ├── version_bump.py         版本号规则与源码指纹（被 build_exe 与测试复用）
│   ├── fetch_prices.py         刷新出厂价格表（联网，发版前跑一次）
│   ├── rebuild_icon.py         重绘图标各尺寸图层
│   ├── score_lab.py            打分公式核算（只读，调 smart_route.rank）
│   ├── diag_speed.py           测速诊断
│   ├── verify_codex_live.py    用真实 Codex 客户端校验写出的配置能否被接受（不碰真实 ~/.codex）
│   ├── verify_exe_codex.py     校验打包版能读到 Codex 配置
│   └── shot_states.py / shot_missing.py   界面截图
├── docs/
│   ├── code-review-2026-11-14.md
│   └── images/                 界面截图（shot_*.png，由冒烟与截图脚本生成）
├── archive/                    本地归档：.bak 备份、旧版源码 rar（可删，不入库）
├── build/  dist/               打包中间产物 / 成品 exe（可删，不入库）
├── 打包exe.bat                 打包入口（ASCII only，中文都在 build_exe.py 里）
├── 启动模型管家.bat             源码方式启动
├── 模型管家.spec               PyInstaller 等价配置（排障用，手工维护）
├── pyproject.toml              包元数据 + 依赖 + gui-scripts 入口
├── requirements.txt            运行时依赖（PyYAML）
├── requirements-dev.txt        开发依赖（PyInstaller / Pillow）
└── .gitignore
```

**分层约定**：`gui_app` → `agents` → `*_import` → `model_probe` / `smart_route` / `price_book`，
依赖只允许向下；`paths.py` 是唯一知道「源码运行 vs 打包运行」差异的模块，新增资源请走它。

**路径约定**：测试与脚本开头都会把项目根加入 `sys.path`，因此都支持在任意目录直接运行；
脚本内部一律以项目根为基准拼路径，不依赖当前工作目录。

PyInstaller 只分析 `main.py` 的依赖图，`tests/`、`scripts/` 不会被卷进 exe。

配置文件与备份**不在**项目目录：`~/.model_probe.json`（界面设置，不含密钥）、`~/.model_probe/backups`（每个目标最多 5 份快照）、`~/.model_probe/prices.json`（价格表）、`~/.model_probe/endpoints.json`（Base URL 与 API Key 配对记录）、`~/.model_probe/routing.json`（智能路由的访问令牌/用户 ID 与算法参数）。Claude Code 的目标文件是 `~/.claude/settings.json`（`CLAUDE_CONFIG_DIR` 优先）。

> **密钥边界**：API Key 只存在于 `endpoints.json`，以明文保存，供「历史」下拉一键回填；智能路由的**系统访问令牌**同样以明文存在 `routing.json`。这是单人本机工具的取舍，别把这些文件同步到网盘或提交进仓库。`~/.model_probe.json` 里永远不含任何密钥，删掉它不会丢 Key。

## 踩过的坑

1. **bat 里别写中文**：cmd 按控制台代码页（936 / 65001）解码 `.bat`，同一份文件在两种代码页下读出两套字符 —— 曾因此生成出一个乱码名字的 exe。中文只写在 `.py`（固定 UTF-8）里，bat 保持 ASCII。
2. **资源要按 `sys._MEIPASS` 找**：单文件运行时 `__file__` 位于临时解包目录，`Path(__file__).with_name("keeper.ico")` 会失效；统一走 `modelkeeper/paths.py::asset_dir()`，打包时配 `--add-data "...keeper.ico;assets"`。
3. **入口只能是 `main.py`**：包内的延迟导入（例如 `model_probe.main()` 里的 `run`）不会被 PyInstaller 的静态分析看到，界面层会整个漏掉；`main.py` 的第一条业务语句就是顶层导入 `modelkeeper.gui_app`。
4. **任务栏图标**：不声明 `SetCurrentProcessExplicitAppUserModelID` 时，从 bat 启动的窗口会被 Windows 归到 pythonw 名下。
5. **clam 主题把「选中」画成 ✕**：和「关闭 / 不支持」的语义撞车，故 `CheckBox` 自绘方框与对勾，未再使用 `ttk.Checkbutton`。
6. **预设价格文件也要 `--add-data`**：`prices_preset.json` 漏打包时不会报错，`load_preset()` 容错返回空表，界面只是「价格全是 —」——最容易被当成"数据源没数据"排查半天。`scripts/build_exe.py` 已把它列为必需文件。
7. **验证 exe 别被旧窗口骗了**：PyInstaller 单文件版会起父子两个进程，杀父进程后**子进程窗口还活着**；用窗口标题找窗口时还可能匹配到标题里带「模型管家」的资源管理器窗口。可靠做法：按进程映像名（`模型管家.exe`）+ 标题前缀 `模型管家 v` 双条件筛，跑完 `taskkill /IM 模型管家.exe` 一并清掉。干净机器验证用 `USERPROFILE`/`HOME` 指向临时目录 + 本地假 `/models` 端点，能直接看到出厂预估价格。
