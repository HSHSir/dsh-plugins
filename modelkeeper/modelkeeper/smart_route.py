#!/usr/bin/env python3
"""智能路由：为「某个令牌 + 某个模型」挑一个**真实可用**的商家，并固定在平台上。

## 为什么不是模拟网页操作

A6API 购买方有三个原生接口，直接替代了「打开模型广场 → 选品牌 → 选模型 →
按延迟排序 → 挑令牌 → 逐个商家手点」的整套手工流程：

    GET  /api/marketplace/channels?model_name=X   该模型全部商家与报价
    GET  /api/marketplace/pins                    当前固定关系
    POST /api/marketplace/pin                     固定
    POST /api/marketplace/unpin                   取消固定

## 顺序为什么必须是「先固定、后实测」

没固定时，请求走的是**当前路由**（可能是旧 pin，也可能是平台智能路由）。
此时测出来的延迟与成功率属于别的商家，跟候选列表里那一行没有对应关系。
所以每测一个候选，都要先把它 pin 上，测完不合格再换下一个。

## 为什么 fallback_to_smart_routing 必须传 false

该字段默认 true，含义是「被 pin 的渠道失败时自动回落到平台智能路由」。
带着 true 去测，测通的可能是回落之后的其他商家，而你以为固定的是它。
测试阶段一律 false；通过后是否放开，交给调用方决定。

## 为什么不能并行测多个候选

pin 是 **(token_id, model_name) 维度**的全局状态，同一时刻只能有一个目标。
不存在按请求指定渠道的头部（已核对前端 bundle）。所以 N 个候选只能串行
测；想真正并行，需要 N 个令牌各自 pin 一个渠道，那是另一个量级的设计。
"""

from __future__ import annotations

import json
import math
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import http_json as hj
from . import model_probe as mp
from . import price_book as pb

# --------------------------------------------------------------------------- #
# 默认策略
# --------------------------------------------------------------------------- #

# 价格倍率硬门槛：商户输入价 / 官方输入价。高于它直接淘汰。
MAX_PRICE_RATIO = 0.8

# 内容校验的最小输出预算。推理模型会先把 token 花在思考上：16 个 token 全被吃光，
# 正文一个不剩，于是「内容校验」必然失败——而且失败得毫无意义（商家其实是好的）。
# 64 够吐一句「可用」，成本仍然可以忽略。
VERIFY_MIN_TOKENS = 64

# 实测时要求模型回的**标记**。选 PONG 而不是「可用」：
#   * 错误提示里不会出现 PONG，误判率低；
#   * 不含中文的话，还能顺带验证模型没有把中文请求搞坏。
# prompt 里也刻意不重复别的候选词，避免模型「复述题目」恰好命中。
VERIFY_WORD = "PONG"

# 长输出校验：要求模型数到 30（≈ 60–90 个 token）。短问答只用到「首字 + 一口气
# 说完」，因此漏得掉「说到一半被上游掐断」这类故障；让回答必须跨过好几帧、
# 持续一两秒，才会真正压到那条路径上。
VERIFY_LONG_PROMPT = "请从 1 数到 30，数字之间用空格分开，不要任何解释。"
VERIFY_LONG_TOKENS = 256
VERIFY_LONG_MIN_CHARS = 12

# ---- 终检：pin 生效之后、返回之前，用「像真实 Agent 一样」的请求再确认一次 ----
# 为什么还要多这一步：前面四次采样都是「一句话 → 一句话」，而 Codex / Claude Code /
# DSH 的真实请求带 system 提示、带工具定义、上下文动辄几万到几十万 token。
# 只测短问答会漏掉两类硬伤：
#   * 上游不支持 function calling（带 tools 直接 400）
#   * 上下文窗口比宣传的小（几千 token 就报超限）
# 这两类一旦漏过，用户「路由成功后第一次真实调用」就失败——正是最难受的时刻。
VERIFY_FINAL_TOKENS = 64
# 填充约 4000 token 的项目资料：足够把「只支持 8k/16k 上下文」的商家逼出来，
# 又不会让首字等到几十秒（实测 4k token 预填充约 3–5 秒）。
VERIFY_FINAL_FILLER_CHARS = 8000
VERIFY_FINAL_MIN_CHARS = 2
VERIFY_FINAL_SYSTEM = "你是一个在仓库里执行任务的编码助手，回答要简短，不要解释过程。"
VERIFY_FINAL_PROMPT = "以上是项目资料。现在请只回复 PONG 这四个字母。"
VERIFY_FINAL_TOOLS: tuple[dict, ...] = ({
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读取仓库里的一个文件",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "相对路径"}},
            "required": ["path"],
        },
    },
},)

# 缓存命中率（0–1）决定实际花多少钱：命中部分按缓存读价计费，本站实测 223 条
# listing 的「缓存读价 / 输入价」**全部**是 0.020（与官方同构），即命中部分只收
# 2% 的钱。于是每 100 万输入 token 的实际支出 ≈ 标价 × (1 − 0.98 × 缓存率)：
# 19% 与 91% 的差别是 0.81 倍 vs 0.11 倍，接近 7 倍的差距，比标价之间的差异大得多。
# 真实案例：Ch 2846 标价 0.060 但缓存 19%，实际 0.0486；Ch 2925 标价 0.079、
# 缓存 91%，实际只要 0.0089——标价贵的反而便宜 5.5 倍。
CACHE_MIN_INPUT_TOKENS = 50_000

# 完全没有任何调用记录时用的默认缓存率（我这边 1000 行日志的加权平均是 31.9%）。
# 它在「所有商家都未知」时是统一的缩放，不影响排序；只有在「一部分商家有记录、
# 一部分没有」时才起作用——那种场合拿 0 会冤枉没用过的商家，拿 1 又会美化它。
CACHE_DEFAULT_PRIOR = 0.32

# 成本打分的对数刻度下限（相对官方输入价）。为什么用对数而不是直接除：
# 实际成本从 0.002x 到 0.8x 横跨 400 倍，线性刻度下「0.008x 和 0.26x」只差
# 0.31 分，而它们真实差 30 倍——用户会看到「明明便宜几十倍却没选它」。
# 对数刻度让「4 倍差」和「40 倍差」都各自有分量。窗固定（0.002–max_ratio），
# 不做同组归一化，所以跨次可比、也不会被离群值带偏。
COST_LOG_FLOOR = 0.002

# 两次采样之间停一下。连着发等于同一个瞬间的重复样本；隔开几百毫秒，
# 才有可能撞上「商户刚抽风」的时间窗——商家侧故障往往是几秒到几十秒的抖动。
# 1.5 秒而不是 0.8：实测同一家商家在「零间隔连发」时立刻回
# 「429 固定商家当前不可用（上游限流或容量不足）」，隔 6 秒就一切正常。
# 间隔太短等于自己把自己限流，然后把这个商家判成不可用——冤枉了好商家，
# 还白白多花一次实测的钱。
VERIFY_GAP_S = 1.5

# 被限流后等多久重测。限流是**暂时性**的，和「这家坏了」是两件事：
# 直接换家会换掉一个其实能用的商家，而换来的下一家未必更好。
THROTTLE_BACKOFF_S = 3.0

# 一家商家「刚出过故障」的认定时限。站点日志里 502/no_healthy_account 是分钟级的
# 抖动，隔了几小时早恢复了，还记着就等于永久拉黑。
MERCHANT_BAD_WINDOW_S = 45 * 60

# 站点日志里翻多少页来找故障（每页 100 条）。翻得越多越准，但控制面响应不小，
# 3 页 ≈ 300 次调用，近一两个小时的记录，足够覆盖「同一批商家」的抖动窗口。
LOG_PAGES = 3
LOG_PAGE_SIZE = 100

# 调用流水（/api/log/self）并发取页数。这份数据必须全量读——7 天窗口下永远翻满
# 20 页，串行取要 12~17 秒，是整轮路由里最大的一笔固定开销，而且和候选家数无关。
# 实测：串行 14s → 6 路 5.4s → 10 路 2.5s → 20 路反而 5.8s（服务端并发排队）。
# 10 就是那个拐点：两批取完，再往上加只会更慢，也多一分被限流的风险。
LOG_FETCH_WORKERS = 10

# **固定后复核**：采样通过不代表真实调用能用——上游会成片抽风（实测同一家
# 3 分钟内 6 次 500/503、0 token、耗时 11 秒，中间夹着成功）。所以固定之后再看
# 一眼站点调用日志：只要这条 (模型, 渠道) 在我固定之后还失败过，就换下一家。
LOG_CONFIRM_WINDOW_S = 180      # 只看最近 3 分钟，太旧的失败不该否掉刚修好的商家
LOG_CONFIRM_PAGES = 2           # 日志是最新的在最前，2 页足够覆盖 3 分钟
LOG_CONFIRM_RETRY_S = 1.0       # 日志落库偶尔慢一拍：没看到任何记录时等一秒再看
WATCH_LOG_WINDOW_S = 300        # 体检时同样看日志：固定之后 5 分钟内失败 2 次就换家
WATCH_LOG_FAILS = 2


@dataclass
class RouteSettings:
    """一次智能路由的全部可调参数。默认值基于 a6api 实测数据标定。"""

    # ---- 硬门槛 ----
    max_ratio: float = MAX_PRICE_RATIO      # 商户输入价 / 官方输入价 的上限
    # 延迟的**参考上限**（只影响打分，不再一票否决）。旧版把它当硬门槛，结果
    # 把「站点 p50 5.2 秒、我自己 148 次调用中位 2.0 秒、倍率 0.06x、0 失败」的
    # 商家直接淘汰了，只留下一批 0.36x 的候选——这是「选上的不是最佳」的主因。
    max_p50_ms: int = 3000
    # 真正不可用的上限：超过它才排除（一分钟还不出结果的商家没有使用价值）。
    hard_p50_ms: int = 20_000
    min_samples: int = 20                   # 站点样本门槛（只用于冷启动商家）
    min_success: float = 95.0               # 站点成功率下限，**百分比**；冷启动时当先验
    # 我自己的实测失败率上限：超过就直接排除。19.6% 失败率的商家（Ch 6490）
    # 无论多便宜都不该被固定——用户为此每 5 次调用就要重试一次。
    max_fail_rate: float = 15.0             # 百分比

    # ---- 排序 ----
    # 三项权重是 1–100 的整数，rank() 内按总和折算成占比参与打分。
    # 用整数百分比而不是 0–1 小数：界面上「45」比「0.45」更不容易填错。
    price_weight: int = 45                  # 越大，低价格商家得分越高
    latency_weight: int = 35                # 越大，低延迟商家得分越高
    success_weight: int = 20                # 越大，低成功率商家扣分越重
    candidates: int = 8                     # 进入实测的候选数（不是 20：实测才是真信号）

    # ---- 实测 ----
    rounds: int = 1                         # 每轮内部测量次数（1 轮足够判断「能不能出字」）
    # 输出预算：64 而不是 16。推理模型会把 16 个 token 全花在思考上，正文为空，
    # 内容校验必然失败——那不是商家的问题，是预算给少了。
    max_tokens: int = VERIFY_MIN_TOKENS
    prompt: str = "请只回复 PONG 这四个字母，不要任何解释。"
    expect: str = VERIFY_WORD               # 正文必须包含它，否则判「假可用」
    # 采样次数。只测一次等于「碰巧通一次就固定」，而商家侧的故障是**间歇性**的
    # （站点日志里同一渠道 3 分钟内 6 次失败 / 4 次成功，每次都 10 秒、0 token、
    # `no_healthy_account`）。
    #
    # 为什么是 2：两档都走**流式**（真实调用全走 SSE），靠长/短输出区分——
    #   * 流式 + 长：撞的是「说到一半被上游掐断」（先收到半截正文、然后连接正常
    #     EOF，真实调用报 502），这是真实踩过的坑，所以放到第一次，最早暴露；
    #   * 流式 + 短：要求正文复述 PONG，撞的是「HTTP 200 但正文是错误提示 / 空正文」。
    # 以前第二档测的是非流式整包——真实调用根本不走那条路，等于浪费一个名额。
    # 误判率对五成失败率的商家是 25%，后面还有一次真实形态终检（带 tools +
    # 4 千 token 上下文）和**站点调用日志复核**兜底。
    attempts: int = 2
    settle_s: float = 0.3                   # pin 之后给服务端一点生效时间
    ttft_limit_ms: int = 6000               # 路由场景比测速更严：6 秒还不吐字就不要了
    # 采样是否核对站点自己的故障日志（近 45 分钟该商家出过 502/no_healthy_account
    # 就不选它）。这是**真实证据**，比我们自己抽几次样准得多，关掉只影响稳健性。
    check_merchant_log: bool = True
    # 固定之后的体检间隔（秒）。0 = 关闭。默认 5 分钟：一次探测几十个 token，
    # 一天不到两分钱，换来的是「坏了几分钟内自动换家」。
    watch_interval_s: int = 300

    # ---- 固定 ----
    fallback_to_smart_routing: bool = False  # 测试与首次固定都用 false，见模块说明


@dataclass
class ChannelStat:
    """我在某个商家上的**真实**调用记录（来自站点日志，不是站点统计）。

    这是全套判据里最可信的一份数据：站点给的 `recent_success_rate` 是它自己
    的抽样，实测会与真相相差很远（Ch 6490 站点报 97%，我这边 148 次里失败 29 次
    ＝ 19.6%，每次都是 10 秒 0 token 的 no_healthy_account）。而我自己的日志是
    「真花了钱、真拿到/没拿到结果」的流水。
    """

    calls: int = 0
    fails: int = 0
    p50_ms: int = 0            # 成功调用耗时的中位数（含生成，最贴近体感）
    tokens_in: int = 0
    tokens_out: int = 0
    cache_tokens: int = 0      # 其中命中缓存的部分（按缓存价计费）
    last_fail: int = 0         # 最近一次失败的时刻（短时抖动要看这个，不看总数）
    last_code: str = ""

    @property
    def fail_rate(self) -> float:
        return self.fails / self.calls if self.calls else 0.0

    @property
    def cache_rate(self) -> float:
        """缓存命中率。样本太小（几万 token 以下）时不可信，调用方应改用先验。"""
        if self.tokens_in < CACHE_MIN_INPUT_TOKENS:
            return 0.0
        return min(max(self.cache_tokens / self.tokens_in, 0.0), 1.0)

    @property
    def cache_known(self) -> bool:
        return self.tokens_in >= CACHE_MIN_INPUT_TOKENS


@dataclass
class Candidate:
    channel_id: int
    supplier_id: int
    channel_name: str
    supplier_name: str
    input_micros: int
    official_input_micros: int
    output_micros: int
    ratio: float
    p50_ms: int
    success_rate: float        # 0..1（接口是万分比，已折算）
    sample_count: int
    score: float = 0.0
    # ---- 实测补充（见 ChannelStat）：没有调用记录时全是默认值 ----
    real_calls: int = 0
    real_fails: int = 0
    real_p50_ms: int = 0
    fail_rate: float = 0.0     # 平滑后的失败率，打分与硬门槛都用它
    eff_p50_ms: int = 0        # 打分用的延迟：有实测取实测，没有才用站点 p50
    # ---- 缓存 ----
    cache_read_micros: int = 0
    cache_rate: float = 0.0    # 缓存命中率（实测，或冷启动时的全局先验）
    cache_known: bool = False  # True = 来自我自己的调用记录
    eff_ratio: float = 0.0     # 含缓存折扣的**实际**成本倍率，排序用它

    @property
    def label(self) -> str:
        return f"{self.supplier_name} / Ch {self.channel_id}"

    @property
    def stat_label(self) -> str:
        """一行说清「为什么是它 / 为什么不是它」，用于界面与状态栏。"""
        cache = (f"缓存 {self.cache_rate * 100:.0f}%"
                 if self.cache_known else "缓存未知")
        lat = secs(self.eff_p50_ms)
        if self.real_p50_ms and self.real_p50_ms != self.eff_p50_ms:
            lat += f"（实测总耗时 {secs(self.real_p50_ms)}）"
        # 4 位有效数字：便宜到 0.008x、或再乘上 99% 缓存后的 0.0003x 都要看得见，
        # 固定三位会把后者显示成「0.000x」，看着像算错了
        base = (f"{self.ratio:.3g}x → 实际 {self.eff_ratio:.3g}x · "
                f"{lat} · {cache}")
        if self.real_calls:
            return f"{base} · 失败 {self.real_fails}/{self.real_calls}"
        return f"{base} · 未用过"


@dataclass
class RouteResult:
    ok: bool
    model: str
    candidate: Candidate | None = None
    tested: list[tuple[Candidate, str, int | None]] = field(default_factory=list)
    total_candidates: int = 0
    total_listings: int = 0
    message: str = ""
    restored: bool = False
    elapsed_s: float = 0.0
    # 因「近 45 分钟出过故障」被排除的商家（label 列表），用于向用户解释为什么没选它
    unhealthy_skipped: list[str] = field(default_factory=list)


class RouteError(Exception):
    """配置或接口层面的失败（不是「没找到可用商家」）。"""


# --------------------------------------------------------------------------- #
# 控制面地址与凭据
# --------------------------------------------------------------------------- #

def guess_control_base(api_base: str) -> str:
    """从 API 地址猜控制面地址：`https://api.a6api.vip/v1` → `https://a6api.vip`。

    这只是个初值，用户可以在设置里改。猜不准的情况（自建反代、多级域名）
    一律让显式配置兜底，不做更花的推断。
    """
    from urllib.parse import urlsplit

    text = (api_base or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "https://" + text
    parts = urlsplit(text)
    host = (parts.hostname or "").strip()
    # 没有点的主机名不是域名（`a6api` 是 provider 键，不是站点），直接放弃猜测：
    # 猜出 `https://a6api` 只会把错误推迟到 DNS 那一层，报错还看不懂。
    if not host or "." not in host:
        return ""
    if host.startswith("api.") and host.count(".") >= 2:
        host = host[4:]
    # 非标准端口要带过来：自建反代常挂在 8443 这类端口上，丢了就连不上
    port = f":{parts.port}" if parts.port else ""
    return f"https://{host}{port}"


ROUTE_FILE = Path.home() / ".model_probe" / "routing.json"


def route_path() -> Path:
    """可用 `MODEL_PROBE_ROUTE_FILE` 覆盖（自测用，避免污染真实配置）。"""
    override = os.environ.get("MODEL_PROBE_ROUTE_FILE", "").strip()
    return Path(override).expanduser() if override else ROUTE_FILE


def _norm_url(url: str) -> str:
    return (url or "").strip().rstrip("/").lower()


def _host_of(url: str) -> str:
    text = (url or "").strip()
    if text and "://" not in text:
        text = "https://" + text
    return (urlsplit(text).hostname or "").lower()


# 配置结构版本。3 = 采样次数的默认值改成 3（旧版本里 2/4 都是旧默认值）。
# 4 = 默认改成 2，同时形态顺序换成「流式长 → 非流式短」。
# 有了它，迁移就只做一次：把 version 写回当前值之后，用户特意改的值不会再被动。
CONFIG_VERSION = 4


def _config_version() -> int:
    """只看盘上写着的版本号；文件不存在/坏掉一律当作最旧的 1。"""
    try:
        raw = json.loads(route_path().read_text(encoding="utf-8"))
        return int(raw.get("version") or 1)
    except (OSError, ValueError, TypeError, AttributeError):
        return 1


def load_config() -> dict:
    """读配置，并把**按 API 地址索引的旧结构**就地升级为按控制面域名索引。

    旧版把凭据挂在 `https://api.x.com/v1` 这类 API 地址下，于是同一个站点的
    另一种写法（`https://x.com/v1`）就查不到，表现为「明明填过了还反复弹窗」。
    新版统一挂在控制面域名（`https://x.com`）下，同一站点的任意端点都能命中。
    """
    path = route_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": CONFIG_VERSION, "sites": {}, "settings": {}, "bad": {}}
    if not isinstance(raw, dict):
        return {"version": CONFIG_VERSION, "sites": {}, "settings": {}, "bad": {}}
    sites = raw.get("sites")
    if not isinstance(sites, dict):
        sites = {}
    clean: dict[str, dict] = {}
    for key, entry in sites.items():
        if not isinstance(entry, dict):
            continue
        # 优先用记录里的 control_base；老记录若没有，就从它的键推出来
        control = _norm_url(str(entry.get("control_base") or "")) \
            or _norm_url(guess_control_base(str(key)))
        if not control or "." not in _host_of(control):
            continue
        clean[control] = {
            "control_base": control,
            "access_token": str(entry.get("access_token") or ""),
            "user_id": str(entry.get("user_id") or ""),
        }
    settings = raw.get("settings")
    if not isinstance(settings, dict):
        settings = {}
    # 本地「这家刚坏过」记忆。和 settings 分开存：settings 每次保存都由
    # RouteSettings 的字段重建，混在里面会被下一次保存抹掉。
    bad = raw.get("bad")
    if not isinstance(bad, dict):
        bad = {}
    return {"version": CONFIG_VERSION, "sites": clean, "settings": settings, "bad": bad}


def save_config(data: dict) -> Path:
    path = route_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    data["version"] = CONFIG_VERSION
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)      # Windows 上只影响只读位，真正的边界是用户账户
    except OSError:
        pass
    os.replace(tmp, path)
    return path


def all_sites() -> list[dict]:
    """已保存的全部站点凭据（按控制面地址排序），供设置界面展示。"""
    sites = list(load_config()["sites"].values())
    sites.sort(key=lambda e: e.get("control_base") or "")
    return sites


def site_credentials(api_base: str) -> dict | None:
    """按 API 地址找到该站点的控制面凭据。

    三级匹配：控制面域名规范化后相等 → 主机名相等（容忍 `api.` 前缀差异）→
    全库只有一条记录时直接用它。三级都落空才返回 None（让界面引导去设置页）。
    """
    sites = load_config()["sites"]
    if not sites:
        return None
    guess = _norm_url(guess_control_base(api_base))
    if guess and guess in sites:
        return sites[guess]
    host = _host_of(guess or api_base)
    if host:
        for key, entry in sites.items():
            if _host_of(key) == host:
                return entry
        # `api.x.com` 与 `x.com` 视为同一站点
        bare = host[4:] if host.startswith("api.") else f"api.{host}"
        for key, entry in sites.items():
            if _host_of(key) == bare:
                return entry
    # 不设「只有一条就直接用」的兜底：换了站点还拿旧凭据去请求，只会得到
    # 一句莫名其妙的 401，比老实去设置页填一次更难排查。
    return None


def set_site_credentials(control_base: str, access_token: str, user_id: str) -> Path:
    """按控制面地址保存凭据。同一站点重复保存即覆盖，不会长出第二条。"""
    control = _norm_url(control_base)
    data = load_config()
    data["sites"][control] = {
        "control_base": control,
        "access_token": (access_token or "").strip(),
        "user_id": str(user_id or "").strip(),
    }
    return save_config(data)


def clear_site_credentials(control_base: str) -> Path:
    data = load_config()
    data["sites"].pop(_norm_url(control_base), None)
    return save_config(data)


# 可持久化的算法参数：(字段名, 类型, 允许范围)
SETTING_FIELDS: tuple[tuple[str, type, tuple[float, float]], ...] = (
    ("price_weight", int, (1, 100)),
    ("latency_weight", int, (1, 100)),
    ("success_weight", int, (1, 100)),
    ("max_ratio", float, (0.01, 10.0)),
    ("max_p50_ms", int, (100, 120_000)),
    ("hard_p50_ms", int, (1000, 300_000)),
    ("min_samples", int, (0, 100_000)),
    ("min_success", float, (0.0, 100.0)),
    ("max_fail_rate", float, (0.0, 100.0)),
    ("candidates", int, (1, 50)),
    ("rounds", int, (1, 5)),
    ("max_tokens", int, (1, 1024)),
    ("attempts", int, (1, 8)),
    ("settle_s", float, (0.0, 5.0)),
    ("ttft_limit_ms", int, (1000, 60_000)),
    ("watch_interval_s", int, (0, 3600)),
)


WEIGHT_FIELDS = ("price_weight", "latency_weight", "success_weight")


@dataclass(frozen=True)
class WeightPreset:
    """一组权重预设。只含三项权重，**不含门槛**。

    为什么不顺手改门槛（倍率上限、失败率上限这些）：用户点预设想换的是「偏好」，
    不是「放宽条件」。把门槛一起动，本来能选的商家会悄悄消失，而界面只显示
    权重变了——排查起来毫无线索。
    """

    key: str
    name: str
    note: str
    price: int
    latency: int
    success: int

    @property
    def weights(self) -> tuple[int, int, int]:
        return (self.price, self.latency, self.success)


# 三档常用取舍。数值按「份数」给，界面会折算成百分比。
# 「平衡」必须与 RouteSettings 的默认值一致，否则用户点「恢复默认」之后
# 预设标签会显示「自定义」，看着像默认值不是预设之一。
WEIGHT_PRESETS: tuple[WeightPreset, ...] = (
    WeightPreset(
        "money", "省钱优先",
        "价格占绝对主导：单价低、缓存率高的商家优先，慢几秒也接受。\n"
        "实测效果——会选到 0.002x、缓存 80%、但 p50 五秒左右的家。\n"
        "适合后台批量任务、长文摘要这类「不等人」的活。",
        85, 10, 5,
    ),
    WeightPreset(
        "balanced", "平衡（默认）",
        "价格略重于延迟，两头都不走极端。\n"
        "实测效果——会选到既便宜又快的家（0.2x 上下、p50 零点几秒）。\n"
        "适合日常对话与写代码：既不想为速度多付十倍钱，也不想等太久。",
        45, 35, 20,
    ),
    WeightPreset(
        "speed", "速度优先",
        "延迟占绝对主导：首字快、总耗时短的家优先，贵一点也接受。\n"
        "实测效果——会选到 p50 零点几秒、实际成本 0.2x 上下的家。\n"
        "适合交互式编码：每个工具调用都要等结果时，延迟比账单更要紧。",
        10, 85, 5,
    ),
)


def match_preset(settings: "RouteSettings | None" = None) -> WeightPreset | None:
    """当前权重正好等于哪一档预设；都不等返回 None（界面显示「自定义」）。"""
    st = settings or RouteSettings()
    want = (st.price_weight, st.latency_weight, st.success_weight)
    for preset in WEIGHT_PRESETS:
        if preset.weights == want:
            return preset
    return None


def _as_percent(value: float) -> float:
    """成功率门槛统一按**百分比**存（0–100）。

    对外「填 95 就是 95%」；读到旧版的 0–1 写法（0.95）自动放大 100 倍，
    否则那条配置会被当成「0.95%」——门槛形同虚设，一个商家都拦不住。
    """
    return value * 100.0 if 0 < value <= 1 else value


def load_settings() -> RouteSettings:
    """读用户调过的算法参数；缺项/坏值一律落回默认值，不让坏配置拦住功能。"""
    oldver = _config_version()
    old_schema = oldver < CONFIG_VERSION
    raw = dict(load_config().get("settings") or {})
    # 旧版权重是 0–1 的小数（0.45/0.35/0.20），新版是 1–100 的整数。
    # 读到旧值时整体放大 100 倍，否则会被范围校验判为非法而丢弃——
    # 用户会觉得「我调过的权重自己变回去了」。
    old_weights: list[float | None] = []
    for name in WEIGHT_FIELDS:
        try:
            old_weights.append(float(raw.get(name)))
        except (TypeError, ValueError):
            old_weights.append(None)
    migrated = all(v is not None and 0 < v <= 1 for v in old_weights)
    if migrated:
        for name, value in zip(WEIGHT_FIELDS, old_weights):
            raw[name] = int(round(float(value) * 100))

    st = RouteSettings()
    for name, kind, (lo, hi) in SETTING_FIELDS:
        if name not in raw:
            continue
        try:
            value = kind(raw[name])
        except (TypeError, ValueError):
            continue
        if lo <= float(value) <= hi:
            setattr(st, name, value)
    if isinstance(raw.get("fallback_to_smart_routing"), bool):
        st.fallback_to_smart_routing = raw["fallback_to_smart_routing"]
    if isinstance(raw.get("check_merchant_log"), bool):
        st.check_merchant_log = raw["check_merchant_log"]
    st.min_success = _as_percent(st.min_success)
    # 输出预算旧默认是 16：推理模型会把 16 个 token 全花在思考上，正文为空，
    # 内容校验必然失败——那不是商家坏，是预算给少了。旧配置一并抬到 64，
    # 免得界面显示 16、实际按 64 跑，两套数字对不上。
    clamped = st.max_tokens < VERIFY_MIN_TOKENS
    if clamped:
        st.max_tokens = VERIFY_MIN_TOKENS
    # 采样次数迁移，只在**旧 schema** 上做一次，之后写回磁盘就不再碰：
    # 否则用户特意把 4 填回去，下次启动又会被改回来，越用越迷惑。
    #   · version < 3：盘上的 2 / 4 都是当年没被改过的默认值 → 落到当前默认；
    #   · version = 3：3 是那版的默认值 → 落到当前默认（4 是用户自己加的，保留）。
    bumped_attempts = False
    if old_schema:
        if oldver < 3 and st.attempts in (2, 4):
            bumped_attempts = True
        elif st.attempts == 3:
            bumped_attempts = True
        if bumped_attempts:
            st.attempts = RouteSettings.attempts
    if migrated or clamped or bumped_attempts:
        # 光是「读时换算」会让盘上一直是旧口径，下次启动再换算一遍。
        # 这里顺手写回一次，把新旧两套写法收敛成一套；写失败不影响本次结果。
        try:
            save_settings(st)
        except OSError:
            pass
    return st


def save_settings(st: RouteSettings) -> Path:
    st.min_success = _as_percent(st.min_success)
    data = load_config()
    data["settings"] = {name: getattr(st, name) for name, _k, _r in SETTING_FIELDS}
    data["settings"]["fallback_to_smart_routing"] = st.fallback_to_smart_routing
    data["settings"]["check_merchant_log"] = st.check_merchant_log
    return save_config(data)


# --------------------------------------------------------------------------- #
# 控制面调用
# --------------------------------------------------------------------------- #

# 控制面调用是否走系统代理。界面在开跑前按探测页的开关设置一次；
# 只影响控制面请求，实测请求仍由 probe_speed 自己的 use_proxy 决定。
_PROXY_STATE = {"use": False}


def set_use_proxy(flag: bool) -> None:
    """由界面在启动路由前调用，让控制面调用与探测页的「走系统代理」保持一致。"""
    _PROXY_STATE["use"] = bool(flag)


def _call(control_base: str, path: str, access_token: str, user_id: str,
          *, method: str = "GET", body: object = None, timeout: int = 30,
          deadline_s: float = 60) -> object:
    url = f"{control_base.rstrip('/')}{path}"
    try:
        payload = hj.request_json(
            url, method=method, headers=hj.auth_headers(access_token, user_id),
            body=body, timeout=timeout, deadline_s=deadline_s,
            use_proxy=_PROXY_STATE["use"],
        )
    except hj.NetError as e:
        # 把「哪个主机解析不了」写清楚：同类报错只说 getaddrinfo failed，
        # 用户根本看不出是填错了地址还是网络确实不通。
        from urllib.parse import urlsplit

        host = urlsplit(url).hostname or url
        raise hj.NetError(f"{e}（目标主机 {host}；检查控制面地址或勾选「走系统代理」）") from e
    if isinstance(payload, dict) and payload.get("success") is False:
        raise RouteError(str(payload.get("message") or "接口返回失败"))
    return payload


def fetch_channels(control_base: str, access_token: str, user_id: str,
                   model_name: str) -> dict:
    """拉该模型的全部商家。

    这个响应很大（实测单模型 3371 渠道 / 13800 条 listing / 28 MB），
    所以走 `http_json`（gzip + 64 MB 上限 + 总时长截止），不走
    `model_probe` 那条 4 MB 上限的路径。
    """
    from urllib.parse import quote

    return _call(control_base, f"/api/marketplace/channels?model_name={quote(model_name)}",
                 access_token, user_id, timeout=60, deadline_s=120)


def list_pins(control_base: str, access_token: str, user_id: str) -> list[dict]:
    data = _call(control_base, "/api/marketplace/pins", access_token, user_id)
    rows = data.get("data") if isinstance(data, dict) else None
    return rows if isinstance(rows, list) else []


def recent_merchant_faults(control_base: str, access_token: str, user_id: str,
                           model_name: str, *,
                           window_s: int = MERCHANT_BAD_WINDOW_S,
                           pages: int = LOG_PAGES) -> dict[int, dict]:
    """「最近这么短的时间里真失败过哪些商家」。`merchant_stats` 之上的瘦封装。

    返回 `{channel_id: {"fails": 次数, "last": 时间戳, "code": 错误码}}`。

    为什么单独要看短期窗口：长期失败率会「洗白」一个刚刚坏掉的商家（200 次调用
    里失败 3 次 = 1.5%，看着很干净，但那 3 次可能全在刚才五分钟内）。固定之前
    先看最近这一小段，才能躲开正在抽风的商家。

    只看同一次日志抓取的结果——数据源和 `merchant_stats` 完全相同，没必要翻两遍。
    """
    horizon = time.time() - max(60, int(window_s))
    faults: dict[int, dict] = {}
    stats = merchant_stats(control_base, access_token, user_id, model_name,
                           horizon_s=max(3600, int(window_s) + 600),
                           max_pages=min(max(1, pages), LOG_PAGES))
    for channel_id, stat in stats.items():
        if stat.fails <= 0 or stat.last_fail < horizon:
            continue
        faults[channel_id] = {"fails": stat.fails, "last": stat.last_fail,
                              "code": stat.last_code}
    return faults


def recent_channel_faults(
    control_base: str, access_token: str, user_id: str,
    model_name: str, channel_id: int, *,
    window_s: int = LOG_CONFIRM_WINDOW_S,
    pages: int = LOG_CONFIRM_PAGES,
    since: float | None = None,
) -> dict:
    """复核**某一渠道、在我固定之后**的真实调用日志，返回失败证据。

    为什么需要它：采样只有几次、上下文只有几千 token，而真实调用是间歇性故障
    的照妖镜。站点日志里能看到采样看不到的东西——例如上游 HTTP 500/503、
    `completion=not_complete`、0 token、耗时 11 秒，而下一秒同一家又是成功的。
    固定之后只要日志里再出现这种失败，就说明这家现在真的不能用。

    判定失败时同时认两套字段：老站点写 `failure_owner=merchant` /
    `request_final_status=failed`，新站点（a6api）还会写 `_merchant_diag_v1`
    （`reason=upstream_http`、`upstream_status=500`、`completion=not_complete`）。
    两套都认，才不会因为站点换字段而漏掉。

    返回 `{"fails": 次数, "last": 最近失败时刻, "code": 错误码,
           "rows": 命中的日志行数, "success": 成功行数}`。
    读不到日志时返回全 0（退化成只信采样），不拦住路由。
    """
    out = {"fails": 0, "last": 0, "code": "", "rows": 0, "success": 0}
    try:
        cid = int(channel_id)
    except (TypeError, ValueError):
        return out
    if cid <= 0:
        return out
    horizon = time.time() - max(30, int(window_s))
    if since:
        # 时钟允许几秒误差；早于固定时刻的失败不算「固定之后又坏了」
        horizon = max(horizon, float(since) - 5)
    for data in _log_pages(control_base, access_token, user_id,
                           list(range(max(1, int(pages))))):
        if not isinstance(data, dict):
            continue
        body = data.get("data")
        rows = body.get("items") if isinstance(body, dict) else None
        if not isinstance(rows, list) or not rows:
            break
        reached_old = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            try:
                created = int(row.get("created_at") or 0)
            except (TypeError, ValueError):
                created = 0
            if created and created < horizon:
                reached_old = True
                continue
            if str(row.get("model_name") or "") != model_name:
                continue
            other = row.get("other")
            try:
                info = (json.loads(other) if isinstance(other, str) and other
                        else (other if isinstance(other, dict) else {}))
            except ValueError:
                info = {}
            try:
                actual = int(info.get("actual_channel_id")
                             or info.get("channel_id")
                             or row.get("channel_id") or 0)
            except (TypeError, ValueError):
                continue
            if actual != cid:
                continue
            out["rows"] += 1
            diag = info.get("_merchant_diag_v1")
            diag = diag if isinstance(diag, dict) else {}
            try:
                upstream = int(diag.get("upstream_status") or 0)
            except (TypeError, ValueError):
                upstream = 0
            failed = (
                str(info.get("failure_owner") or "") == "merchant"
                or str(info.get("request_final_status") or "") == "failed"
                or (diag and (str(diag.get("completion") or "") == "not_complete"
                              or upstream >= 500))
            )
            if failed:
                out["fails"] += 1
                if created >= out["last"]:
                    out["last"] = created
                    code = (str(info.get("error_code") or "")
                            or str(diag.get("reason") or ""))
                    if not code and upstream:
                        code = f"HTTP {upstream}"
                    out["code"] = code
            else:
                out["success"] += 1
        if reached_old:
            break
    return out


def _log_page(control_base: str, access_token: str, user_id: str, page: int) -> object:
    return _call(control_base,
                 f"/api/log/self?p={page}&page_size={LOG_PAGE_SIZE}&type=0",
                 access_token, user_id, timeout=30, deadline_s=60)


def _log_pages(control_base: str, access_token: str, user_id: str,
               pages: list[int]) -> list[object | None]:
    """并发取一批日志页，结果**按页号顺序**返回；取不到的那一页给 None。

    为什么值得并发：日志是这套判据里唯一必须全量读的东西，7 天窗口下永远翻满
    20 页（每页 100 条、每次 0.5 秒上下），串行就是十几秒的纯等待——比实测任何
    一家商家都慢，而且是**每一轮路由都要付一次**的固定成本。控制面接口是只读的，
    并发 6 路实测没有触发限流；失败的页按「没数据」处理，与串行时行为一致。
    """
    if not pages:
        return []
    if len(pages) == 1:
        try:
            return [_log_page(control_base, access_token, user_id, pages[0])]
        except (hj.HttpError, hj.NetError, RouteError, OSError):
            return [None]
    out: list[object | None] = []
    with ThreadPoolExecutor(max_workers=len(pages),
                            thread_name_prefix="logpage") as pool:
        futures = [pool.submit(_log_page, control_base, access_token, user_id, page)
                   for page in pages]
        for future in futures:
            try:
                out.append(future.result())
            except (hj.HttpError, hj.NetError, RouteError, OSError):
                out.append(None)
    return out


def merchant_stats(control_base: str, access_token: str, user_id: str, model_name: str,
                   *, horizon_s: int = 7 * 86400, max_pages: int = 20) -> dict[int, ChannelStat]:
    """从站点日志里算出「每个商家在**我这儿**的真实战绩」。

    为什么它是主判据：站点自己的 `recent_success_rate` 与实际体感差得很远
    （Ch 6490 站点报 97%，我这边 148 次里失败 29 次）。而日志是真实流水：
    谁被调用过多少次、失败多少次、一次调用等了多久、输入输出各多少 token。

    统计口径：
      * 失败 —— `failure_owner=merchant`，或没有 owner 但明确 failed / 带错误文本。
        我方自己的错（参数写错、余额不足）不算到商家头上。
      * 延迟 —— 只取**成功**调用的 `use_time` 中位数。它含生成时间，最贴近体感；
        站点的 p50 是它自己的抽样口径，和真实等待经常对不上。
      * 时间窗 —— 默认近 7 天，翻页到超出窗口或没有更多数据为止。

    读不到日志时返回空字典：退化成「只靠站点统计 + 现场采样」。
    """
    horizon = time.time() - max(3600, int(horizon_s))
    stats: dict[int, ChannelStat] = {}
    durations: dict[int, list[int]] = {}
    total_pages = max(1, max_pages)
    workers = max(1, int(LOG_FETCH_WORKERS))
    page = 0
    finished = False
    while page < total_pages and not finished:
        # 一批并发取，然后**按页号顺序**解析：日志是从新到旧排的，「已经翻到窗口
        # 以外」这个判断必须按顺序做，否则会把后面的新记录当成结束信号。
        batch = list(range(page, min(page + workers, total_pages)))
        for data in _log_pages(control_base, access_token, user_id, batch):
            if data is None:            # 这一页取不到：当作没有更多数据
                finished = True
                break
            body = data.get("data") if isinstance(data, dict) else None
            rows = body.get("items") if isinstance(body, dict) else None
            if not isinstance(rows, list) or not rows:
                finished = True
                break
            oldest = None
            for row in rows:
                if not isinstance(row, dict):
                    continue
                try:
                    created = int(row.get("created_at") or 0)
                except (TypeError, ValueError):
                    created = 0
                if created:
                    oldest = created if oldest is None else min(oldest, created)
                if str(row.get("model_name") or "") != model_name:
                    continue
                if created and created < horizon:
                    continue
                info: dict = {}
                other = row.get("other")
                if isinstance(other, str) and other:
                    try:
                        info = json.loads(other)
                    except ValueError:
                        info = {}
                elif isinstance(other, dict):
                    info = other
                try:
                    channel_id = int(info.get("actual_channel_id")
                                     or info.get("channel_id")
                                     or row.get("channel_id") or 0)
                except (TypeError, ValueError):
                    continue
                if channel_id <= 0:
                    continue
                owner = str(info.get("failure_owner") or "")
                status = str(info.get("request_final_status") or "")
                if owner == "merchant":
                    failed = True
                elif owner:
                    failed = False
                else:
                    failed = status == "failed" or bool(str(row.get("content") or "").strip())
                entry = stats.setdefault(channel_id, ChannelStat())
                entry.calls += 1
                if failed:
                    entry.fails += 1
                    # 日志按时间倒序返回，只让「更新的一次失败」覆盖错误码
                    if created >= entry.last_fail:
                        entry.last_fail = created
                        code = str(info.get("error_code") or "")
                        if code:
                            entry.last_code = code
                    continue
                try:
                    entry.tokens_in += int(row.get("prompt_tokens") or 0)
                    entry.tokens_out += int(row.get("completion_tokens") or 0)
                    # 命中缓存的 token 数：站点日志的 other.cache_tokens。
                    # 这是唯一能算出「这家实际收我多少钱」的数据——标价一样的两家，
                    # 缓存率 19% 和 91% 的实际支出差近 5 倍。
                    entry.cache_tokens += int(info.get("cache_tokens") or 0)
                    used = float(row.get("use_time") or 0)
                except (TypeError, ValueError):
                    used = 0.0
                if used > 0:
                    durations.setdefault(channel_id, []).append(int(used * 1000))
            if oldest is not None and oldest < horizon:
                finished = True
                break
            if len(rows) < LOG_PAGE_SIZE:
                finished = True
                break
        page += len(batch)
    for channel_id, values in durations.items():
        values.sort()
        stats[channel_id].p50_ms = values[len(values) // 2]
    return stats


def _smoothed_fail_rate(stat: ChannelStat | None, prior: float, alpha: float = 8.0) -> float:
    """把「失败次数/调用次数」平滑成可比较的失败率。

    直接除以调用次数会让「1 次调用 0 失败」看起来比「200 次调用 3 次失败」更可靠，
    于是每次都会去试那些没人用过的商家。这里用贝叶斯平滑：先验取站点成功率
    （冷启动时唯一的参考），样本越多越相信实测，`alpha` 是「相当于看过多少次
    站点样本」。8 次是个折中：十几个调用就能明显压过先验，又不至于被一两次
    偶发失败彻底打死。
    """
    if stat is None or stat.calls <= 0:
        return max(0.0, min(1.0, prior))
    return (stat.fails + alpha * prior) / (stat.calls + alpha)


def _cache_prior(stats: dict[int, ChannelStat]) -> float:
    """全局缓存命中率，冷启动商家用它当先验。

    为什么要一个先验：缓存命中率取决于「我的请求前缀重复得多不多」，我这边
    平均 31.9%（1000 行日志实证）。对没用过的商家，拿这个数当默认比拿 0 或
    100% 都更接近真相——用 0 会把它当成「从不命中缓存」的贵家直接淘汰，
    用 100% 又会让它看起来便宜得离谱。
    """
    total_in = sum(s.tokens_in for s in stats.values() if s.tokens_in > 0)
    total_cache = sum(s.cache_tokens for s in stats.values() if s.tokens_in > 0)
    if total_in <= 0:
        return CACHE_DEFAULT_PRIOR
    return min(max(total_cache / total_in, 0.0), 1.0)


def _eff_ratio(ratio: float, input_micros: int, cache_read_micros: int,
               cache_rate: float) -> float:
    """含缓存折扣的实际成本倍率。

    实际单价 = (1−h)·输入价 + h·缓存读价
             = 输入价 · (1 − h·(1 − 缓存读价/输入价))
    再除官方输入价，就是「实际成本倍率」——h=0 时它等于标价倍率，
    缓存率越高越小。排序必须用这个数：标价便宜但缓存率低的家，
    真实账单往往比标价贵、缓存率高的家还要高。
    """
    if input_micros <= 0:
        return ratio
    discount = 1.0 - min(max(cache_read_micros / input_micros, 0.0), 1.0)
    h = min(max(cache_rate, 0.0), 0.98)     # 留 2% 余量：全命中也不至于把成本算成 0
    return ratio * (1.0 - discount * h)


def _bad_key(control_base: str, model_name: str) -> str:
    return f"{_host_of(control_base)}|{model_name}"

def load_bad_channels(control_base: str, model_name: str,
                      window_s: int = MERCHANT_BAD_WINDOW_S) -> dict[int, dict]:
    """本地记录的「这家刚才校验没通过」。

    站点日志是主证据，但日志接口可能读不到（老站点/权限），而且我们自己校验
    失败的那一次也值得记着——同一家连着两次被固定上去、连着两次失败，
    只说明排序每次都在重复同一个错误选择。
    """
    entry = load_config().get("bad", {}).get(_bad_key(control_base, model_name))
    if not isinstance(entry, dict):
        return {}
    horizon = time.time() - max(60, int(window_s))
    out: dict[int, dict] = {}
    for key, value in entry.items():
        if not isinstance(value, dict):
            continue
        try:
            ts = int(value.get("ts") or 0)
            channel_id = int(key)
        except (TypeError, ValueError):
            continue
        if channel_id > 0 and ts >= horizon:
            out[channel_id] = {"fails": int(value.get("fails") or 1),
                               "last": ts, "code": str(value.get("code") or "")}
    return out


def mark_bad_channel(control_base: str, model_name: str, channel_id: int,
                     code: str = "", *, window_s: int = MERCHANT_BAD_WINDOW_S) -> None:
    """记下「这家刚才没通过校验」，顺便清掉过期的记录。"""
    data = load_config()
    key = _bad_key(control_base, model_name)
    entry = data.get("bad", {}).get(key)
    if not isinstance(entry, dict):
        entry = {}
    horizon = time.time() - max(60, int(window_s))
    clean = {cid: v for cid, v in entry.items()
             if isinstance(v, dict) and int(v.get("ts") or 0) >= horizon}
    try:
        cid = str(int(channel_id))
    except (TypeError, ValueError):
        return
    previous = clean.get(cid) if isinstance(clean.get(cid), dict) else {}
    clean[cid] = {"ts": int(time.time()),
                  "fails": int(previous.get("fails") or 0) + 1,
                  "code": (code or previous.get("code") or "")[:80]}
    data.setdefault("bad", {})[key] = clean
    try:
        save_config(data)
    except OSError:
        pass


def clear_bad_channels(control_base: str = "", model_name: str = "") -> None:
    """清空（或按站点/模型清空）本地失败记忆。用户手动「再试一次」时用。"""
    data = load_config()
    bad = data.get("bad")
    if not isinstance(bad, dict) or not bad:
        return
    if not control_base and not model_name:
        data["bad"] = {}
    else:
        target = _bad_key(control_base, model_name) if control_base and model_name else ""
        for key in list(bad):
            if target and key == target:
                bad.pop(key, None)
            elif not target and (not control_base
                                 or key.split("|", 1)[0] == _host_of(control_base)):
                bad.pop(key, None)
    try:
        save_config(data)
    except OSError:
        pass


def pin_channel(control_base: str, access_token: str, user_id: str, *, token_id: int,
                model_name: str, channel_id: int, fallback: bool = False) -> None:
    _call(control_base, "/api/marketplace/pin", access_token, user_id, method="POST",
          body={"token_id": int(token_id), "model_name": model_name,
                "channel_id": int(channel_id),
                "fallback_to_smart_routing": bool(fallback)})


def unpin_channel(control_base: str, access_token: str, user_id: str, *, token_id: int,
                  model_name: str, channel_id: int) -> None:
    _call(control_base, "/api/marketplace/unpin", access_token, user_id, method="POST",
          body={"token_id": int(token_id), "model_name": model_name,
                "channel_id": int(channel_id)})


def list_tokens(control_base: str, access_token: str, user_id: str) -> list[dict]:
    data = _call(control_base, "/api/token/?p=0&size=100", access_token, user_id)
    body = data.get("data") if isinstance(data, dict) else None
    rows = body.get("items") if isinstance(body, dict) else None
    return rows if isinstance(rows, list) else []


def token_plain_key(control_base: str, access_token: str, user_id: str,
                    token_id: int) -> str:
    """取令牌的明文 key（POST /api/token/{id}/key）。

    列表接口只给 `TVyH**********hhLH` 这种掩码，做「sk- → 令牌」的精确匹配
    必须拿到明文。该接口只接受 POST，且空 body。
    """
    data = _call(control_base, f"/api/token/{int(token_id)}/key", access_token, user_id,
                 method="POST", body={})
    body = data.get("data") if isinstance(data, dict) else None
    key = body.get("key") if isinstance(body, dict) else None
    return str(key or "")


def bare_key(key: str) -> str:
    """去掉 `sk-` 前缀。

    站点的令牌本体是 40 位裸串（`uh9DFyg…JOBL`），而客户端配置里通常写成
    `sk-uh9DFyg…JOBL`（OpenAI 习惯）。两边直接比对必然不等——这是「明明填对了
    却报找不到令牌」的根因。
    """
    text = (key or "").strip()
    return text[3:] if text[:3].lower() == "sk-" else text


def resolve_token(control_base: str, access_token: str, user_id: str,
                  api_key: str) -> tuple[int, str, str]:
    """按 sk- key 反查它属于哪个令牌，返回 (token_id, 令牌名, 用于实测的 key)。

    匹配一律在**去前缀**之后进行：先比掩码（便宜），再对候选取明文精确核对。
    只靠掩码会在 `TVyH…hhLH` 撞车时认错令牌，而钉错令牌等于改错配置。

    返回的 key 用调用方传进来的原样写法：站点虽然也接受裸串，但用户客户端
    里用的是哪种写法，就沿用它——那是已经被验证过能通的形式。
    """
    want_raw = (api_key or "").strip()
    want = bare_key(want_raw)
    if not want:
        raise RouteError("没有拿到该端点的 API Key，无法判断要固定哪个令牌。")
    head, tail = want[:4], want[-4:]
    tokens = list_tokens(control_base, access_token, user_id)
    if not tokens:
        raise RouteError("该账户下没有令牌。")

    near: list[dict] = []
    for row in tokens:
        masked = bare_key(str(row.get("key") or ""))
        if masked.startswith(head) and masked.endswith(tail):
            near.append(row)
    # 掩码命中优先，其次全量（掩码被平台改过格式时仍有机会精确核对）
    for row in near or tokens:
        try:
            token_id = int(row.get("id") or 0)
        except (TypeError, ValueError):
            continue
        if token_id <= 0:
            continue
        try:
            plain = token_plain_key(control_base, access_token, user_id, token_id)
        except (hj.HttpError, hj.NetError, RouteError):
            continue
        if bare_key(plain) == want:
            return token_id, str(row.get("name") or ""), want_raw

    names = "、".join(f"{row.get('name')}({row.get('id')})" for row in tokens[:6])
    raise RouteError(
        f"没有找到与当前 API Key 匹配的令牌（末尾 {tail}）。该账户下的令牌：{names}。"
        "请确认这个 Key 属于该站点，或在 Agent 配置里换成其中一个令牌的 Key。"
    )


def secs(ms: object) -> str:
    """毫秒 → 给人看的秒数。界面与日志一律用秒，毫秒只在内部和接口里存在。"""
    try:
        value = float(ms)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "—"
    return f"{value / 1000:.2f} s"


def record_price(model_name: str, candidate: Candidate) -> float | None:
    """把实测通过的商家输入价写进价格表（元 / 百万 token），返回写入值。

    写的是**商户价**而不是官方价：用户想知道的是自己实际付多少。价格表读不出来时
    跳过写入——宁可这次不记，也不能用一条记录覆盖掉他手工维护的整张表。
    """
    price = pb.cny_from_usd_micros(candidate.input_micros)
    if price is None:
        return None
    data = pb.load()
    path = pb.price_path()
    try:
        # 空表可能只是「还没填」，但非空文件读成空表就是坏了：此时不覆盖
        if not data.get("models") and path.exists() and path.stat().st_size > 2:
            return price
        pb.upsert(model_name, price, data)
        data["updated"] = time.strftime("%Y-%m-%d %H:%M")
        pb.save(data)
    except OSError:
        return price
    return price


# --------------------------------------------------------------------------- #
# 候选筛选与排序
# --------------------------------------------------------------------------- #

def _success_rate(raw: object) -> float:
    """接口的成功率是**万分比**（9960 = 99.60%），不是 0..1。

    兼容两种写法的理由：字段没有文档，万一以后口径改成 0..1，
    按 10000 去除会把 0.996 变成 0.0001，直接筛掉全部候选。
    """
    try:
        value = float(raw or 0)
    except (TypeError, ValueError):
        return 0.0
    if value <= 1.0:
        return max(value, 0.0)
    return min(value / 10000.0, 1.0)


def parse_candidates(payload: dict, model_name: str,
                     settings: RouteSettings | None = None,
                     stats: dict[int, ChannelStat] | None = None) -> tuple[list[Candidate], int]:
    """从渠道响应里筛出候选，返回 (候选列表, 该模型的 listing 总数)。

    筛选顺序即「先排除不能用的、再排除不够便宜的」：
      1. 只留目标模型的 listing
      2. 渠道/供应商未被禁用
      3. 商户输入价 > 0 且官方价 > 0（没有基准就谈不上倍率）
      4. **倍率 ≤ max_ratio**（用户的硬规则：高于 0.8 倍官方价直接不要）
      5. 延迟：**有实测取实测**，没有才用站点 p50；超过 hard_p50_ms 才排除
      6. 可靠性：平滑失败率 > max_fail_rate 直接排除（这条能挡住「站点说 97%、
         我这边五次里失败一次」的商家）
      7. 样本：站点样本或我自己的调用数，至少一边够 min_samples
      8. 冷启动（我从没用过这家）才用站点成功率做硬门槛

    价格只比输入价：本站实测 208 条 listing，输出价 **全部** 严格等于输入价的 4 倍
    （与官方同构），所以输入价之比就是总价之比，没必要再引入一个假设的 token 配比
    去算「预估总价」——那只会把误差引进一个本来干净的量。
    """
    st = settings or RouteSettings()
    stats = stats or {}
    cache_prior = _cache_prior(stats)
    channels = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(channels, list):
        raise RouteError("渠道响应里没有 data 数组，站点版本可能已变")

    total = 0
    seen: set[int] = set()
    out: list[Candidate] = []
    for ch in channels:
        if not isinstance(ch, dict):
            continue
        listings = ch.get("listings")
        if not isinstance(listings, list):
            continue
        for item in listings:
            if not isinstance(item, dict):
                continue
            if str(item.get("model_name") or "") != model_name:
                continue
            total += 1
            if item.get("supplier_channel_disabled") or item.get("user_channel_disabled"):
                continue
            official = item.get("official_price") or {}
            try:
                price_in = int(item.get("input_price_micros") or 0)
                official_in = int(official.get("input_price_micros") or 0)
                price_out = int(item.get("output_price_micros") or 0)
                price_cache = int(item.get("cache_read_price_micros") or 0)
                channel_id = int(item.get("channel_id") or ch.get("channel_id") or 0)
                supplier_id = int(item.get("supplier_id") or ch.get("supplier_id") or 0)
            except (TypeError, ValueError):
                continue
            if price_in <= 0 or official_in <= 0 or channel_id <= 0 or channel_id in seen:
                continue
            ratio = price_in / official_in
            if ratio > st.max_ratio:
                continue
            try:
                p50_site = int(item.get("recent_p50_ms") or 0)
                samples = int(item.get("sample_count") or 0)
            except (TypeError, ValueError):
                continue
            site_rate = _success_rate(item.get("recent_success_rate"))
            stat = stats.get(channel_id)
            # 延迟：**优先站点 p50**，站点没数才用我自己的实测中位。
            # 为什么不是反过来：两者口径不同（站点像首字，我的 use_time 含生成，
            # 长上下文时能到十几秒）。混在一列里比较会把「总耗时」和「首字」
            # 当成同一个量，慢的家会被重复惩罚。实测值仍然记录并显示，
            # 只是不参与打分——它更适合当「这家到底有多慢」的旁证。
            eff_p50 = (p50_site if p50_site > 0
                       else (stat.p50_ms if stat is not None else 0))
            if eff_p50 <= 0 or eff_p50 > st.hard_p50_ms:
                continue
            fail_rate = _smoothed_fail_rate(stat, 1.0 - site_rate)
            if stat is not None and stat.calls >= 5 and fail_rate * 100.0 > st.max_fail_rate:
                continue
            if samples < st.min_samples and not (stat is not None and stat.calls >= st.min_samples):
                continue
            # 站点成功率只在「我从没用过这家」时当硬门槛：用过的以实测为准，
            # 否则会重演「站点 97% / 实际 19.6% 失败」的误判。
            if stat is None and site_rate * 100.0 < st.min_success:
                continue
            # 缓存率：用我自己在这家身上的真实命中率；没用过就用全局先验。
            # 站点 UI 上有「缓存率」这一列，但接口并不返回它，只能自己量。
            cache_known = stat is not None and stat.cache_known
            cache_rate = stat.cache_rate if cache_known else cache_prior
            seen.add(channel_id)
            out.append(Candidate(
                channel_id=channel_id,
                supplier_id=supplier_id,
                channel_name=str(item.get("channel_name") or ch.get("channel_name") or ""),
                supplier_name=str(item.get("supplier_name") or ch.get("supplier_name") or ""),
                input_micros=price_in,
                official_input_micros=official_in,
                output_micros=price_out,
                ratio=ratio,
                p50_ms=p50_site,
                success_rate=site_rate,
                sample_count=samples,
                real_calls=stat.calls if stat else 0,
                real_fails=stat.fails if stat else 0,
                real_p50_ms=stat.p50_ms if stat else 0,
                fail_rate=fail_rate,
                eff_p50_ms=eff_p50,
                cache_read_micros=price_cache,
                cache_rate=cache_rate,
                cache_known=cache_known,
                eff_ratio=_eff_ratio(ratio, price_in, price_cache, cache_rate),
            ))
    return out, total


def rank(candidates: list[Candidate], settings: RouteSettings | None = None) -> list[Candidate]:
    """按「价格越低越好 + 延迟越低越好 + 失败率越低越好」打分排序。

    **不用 min-max 归一化**：那是旧版最隐蔽的一个坑。三项里「成功率」几乎都是
    99%–100%，一组常数做 min-max 会把 0.1% 的差异放大成满量程的 1.0，等于让
    20 分的权重项去决定排序——选出来的商家看着「成功率最高」，其实只是噪声。
    改成**绝对刻度**后，三项都在 0–1 之间、跨次可比，离群值也不会把别人压扁：

      * 成本分 = log(实际成本倍率 ÷ 0.002) ÷ log(倍率上限 ÷ 0.002)
        ——**实际成本**含缓存折扣（见 _eff_ratio），且用对数刻度，见 COST_LOG_FLOOR
      * 延迟分 = 站点 p50 ÷ 延迟参考上限（超参考值最多记 3 倍；1.5 倍会饱和，
        把 5 秒和 20 秒的家算成一样慢）
      * 失败分 = 平滑后的失败率（0–1）
    """
    st = settings or RouteSettings()
    if not candidates:
        return []
    ceiling = max(st.max_ratio, COST_LOG_FLOOR * 1.5)
    span = math.log(ceiling / COST_LOG_FLOOR)
    lat_span = max(st.max_p50_ms, 1)
    total_w = st.price_weight + st.latency_weight + st.success_weight
    if total_w <= 0:
        total_w = 1.0
    for cand in candidates:
        # 价格项用**实际**成本（含缓存折扣），不是标价：标价便宜但缓存率低的家，
        # 真实账单常常比标价贵、缓存率高的家还高——这是用户实测踩过的坑。
        eff = max(cand.eff_ratio, COST_LOG_FLOOR)
        cost = math.log(eff / COST_LOG_FLOOR) / span if span > 0 else 0.0
        lat = min(max(cand.eff_p50_ms, 0) / lat_span, 3.0)
        cand.score = (st.price_weight * cost + st.latency_weight * lat
                      + st.success_weight * cand.fail_rate) / total_w
    return sorted(candidates, key=lambda c: (c.score, c.eff_ratio, c.eff_p50_ms))


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #

def _current_pin(pins: list[dict], token_id: int, model_name: str) -> dict | None:
    for row in pins:
        try:
            if int(row.get("token_id") or 0) == int(token_id) \
                    and str(row.get("model_name") or "") == model_name:
                return row
        except (TypeError, ValueError):
            continue
    return None


# 校验形态矩阵：(是否整包/非流式, 是否长输出)。
#
# **只测流式**：用户实际调用（WorkBuddy / DSH / Codex / Claude Code）全走 SSE，
# 日志里实测也全是 `is_stream=True`。以前花一档去测「非流式整包」，验的是一条
# 真实调用根本不会走的路径——通过它并不能说明真实调用能用，反而把该测长输出的
# 名额挤掉了。现在两档都走流式，靠**长/短输出**区分信息量：
#   * 第一位「流式 + 长输出」：真实 Agent 正是这么调的，「说到一半被上游掐断」
#     只在这一档暴露（客户端先收到半截正文、然后连接正常 EOF，真实调用报 502），
#     坏家第一秒就现形；
#   * 第二位「流式 + 短输出」：要求正文复述 PONG，撞的是「HTTP 200 但正文是错误
#     提示 / 空正文」。它同样取首字延迟。
# 采样超过 2 次时循环这两档，不再引入非流式。
_VERIFY_SHAPES: tuple[tuple[bool, bool], ...] = (
    (False, True),    # 流式 + 长：真实 Agent 形态，专撞「说到一半被掐断」
    (False, False),   # 流式 + 短：正文校验（必须复述 PONG）+ 取首字
)


def _verify_shape(attempt: int) -> tuple[bool, bool]:
    """第 attempt 次（从 1 开始）采样用哪种形态：返回 (force_buffered, long_output)。"""
    return _VERIFY_SHAPES[(max(1, attempt) - 1) % len(_VERIFY_SHAPES)]


def _shape_label(buffered: bool, long_out: bool) -> str:
    return f"{'非流式' if buffered else '流式'}·{'长输出' if long_out else '短输出'}"


# 限流类错误的字面特征。这些词出现在错误文本里就说明「暂时忙」，不是「坏了」。
_THROTTLE_WORDS = ("429", "限流", "容量不足", "rate limit", "too many requests",
                   "overload", "overloaded", "并发")


def is_throttled(res) -> bool:
    """这次失败是不是「被限流/容量不足」——暂时性的，不该记成商家坏。"""
    text = f"{getattr(res, 'error', '') or ''} {getattr(res, 'content', '') or ''}".lower()
    return any(word in text for word in _THROTTLE_WORDS)


_FINAL_FILLER_UNIT = "以下是一条项目背景记录，用于占位上下文，请阅读但不要复述：\n"


def _final_filler(chars: int = VERIFY_FINAL_FILLER_CHARS) -> str:
    """拼一段约 `chars` 字的填充文本。内容无意义，只为把上下文撑到真实规模。"""
    unit = _FINAL_FILLER_UNIT
    return unit * max(1, chars // len(unit) + 1)


def final_check(protocol: str, api_base: str, api_key: str, model_name: str,
                settings: "RouteSettings") -> tuple[bool, str]:
    """固定生效后用「真实 Agent 形态」再确认一次，返回 (是否通过, 原因)。

    形态：system 提示 + 一个工具定义 + 约 4 千 token 的项目资料 + 要求回 PONG。
    刻意不带 `reasoning_effort`：非推理模型会因此 400，那属于我们测错东西，
    不能算商家的错。tools 与 system 是三家 Agent 客户端都会发的，必须验。
    """
    prompt = _final_filler() + "\n" + VERIFY_FINAL_PROMPT

    def _run(with_tools: bool):
        return mp.probe_speed(
            protocol, api_base, api_key, model_name,
            rounds=1, max_tokens=VERIFY_FINAL_TOKENS,
            prompt=prompt,
            ttft_limit_ms=max(settings.ttft_limit_ms, 30_000),
            use_proxy=_PROXY_STATE["use"], expect=VERIFY_WORD,
            min_chars=VERIFY_FINAL_MIN_CHARS,
            system=VERIFY_FINAL_SYSTEM,
            tools=list(VERIFY_FINAL_TOOLS) if with_tools else None,
        )

    res = _run(with_tools=True)
    if res.ok:
        return True, ""
    # 有工具在时，个别模型会「不回答、直接调用工具」，于是正文为空。
    # 那不是坏商家，是它太听话了——去掉工具再确认一次，通了就算过。
    # 不这么做会把能用的商家判死，还写进「坏过」名单关它 45 分钟。
    raw = f"{res.error or ''} {res.content or ''}".lower()
    if "没有任何增量" in raw or "空正文" in raw or "没有正文" in raw:
        retry = _run(with_tools=False)
        if retry.ok:
            return True, ""
        res = retry
        raw = f"{res.error or ''} {res.content or ''}".lower()
    if is_throttled(res):
        return False, "终检被限流（暂时性，稍后重试或手动再点一次）"
    if "tool" in raw or "function" in raw:
        return False, ("不支持 function calling（真实 Agent 会带 tools，"
                       "固定它等于每次调用都 400）")
    if "context" in raw or "maximum" in raw or "length" in raw:
        return False, "上下文不够：约 4 千 token 就报超限，真实调用会被截断或报错"
    return False, f"终检未通过：{res.error or '无有效响应'}"


# --------------------------------------------------------------------------- #
# 守护：固定之后的体检
# --------------------------------------------------------------------------- #

# 连续几次体检失败才认定「这家又坏了」。1 次太敏感（一次网络抖动就换家，
# 换完可能更差），2 次能过滤掉偶发，又不至于让用户连续失败好几分钟。
WATCH_FAILS_TO_ACT = 2

# 换家之后多久内不再自动换。商家侧故障成片出现时，频繁换家只会把钱花在
# 反复实测上，还让用户看不清到底发生了什么。
WATCH_COOLDOWN_S = 900.0


class PinHealth:
    """盯住「当前固定的这家」是否还活着：连续失败达到阈值就建议换家。

    为什么必须有这一层：固定是**一次性决策**，而商家健康是**时变**的
    （实测同一家 3 分钟内 6 次失败 / 4 次成功）。选得再准，也可能十分钟后就坏；
    客户端唯一能做的补救是「持续体检 + 自动换家」，而不是指望一次选对管到底。

    这里只放判定，不碰网络与界面：谁去探测、什么时候换家由调用方决定，
    这样它可以被单测覆盖（GUI 线程里跑的东西最难验证）。
    """

    def __init__(self, need: int = WATCH_FAILS_TO_ACT) -> None:
        self.need = max(1, int(need))
        self._fails: dict[str, int] = {}
        self._acted_at: dict[str, float] = {}

    def record(self, key: str, ok: bool, *, now: float | None = None,
               cooldown_s: float = WATCH_COOLDOWN_S) -> str:
        """记一次体检结果，返回：

        * `ok`     —— 通过，连续失败计数清零
        * `watch`  —— 失败但还没到阈值，继续观察
        * `reroute`—— 连续失败达到阈值，该换家了（同时进入冷却期）
        * `cooldown` —— 已经判定过、还在冷却期内，不重复换家
        """
        moment = time.time() if now is None else float(now)
        if ok:
            self._fails.pop(key, None)
            return "ok"
        fails = self._fails.get(key, 0) + 1
        self._fails[key] = fails
        if fails < self.need:
            return "watch"
        last = self._acted_at.get(key, 0.0)
        if last and moment - last < cooldown_s:
            return "cooldown"
        self._acted_at[key] = moment
        self._fails[key] = 0
        return "reroute"

    def forget(self, key: str) -> None:
        """换家之后调用，让新固定的一家从零开始计数。"""
        self._fails.pop(key, None)


def route(
    *,
    api_base: str,
    control_base: str,
    access_token: str,
    user_id: str,
    token_id: int,
    api_key: str,
    model_name: str,
    protocol: str = "openai",
    settings: RouteSettings | None = None,
    on_stage=None,
    should_stop=None,
) -> RouteResult:
    """跑一次完整的智能路由。阻塞调用，请放在工作线程里跑。

    `on_stage(text)` 用于把进度推给界面；`should_stop()` 返回真时尽快收尾
    （已发出的请求不强行打断，只是不再测下一个）。
    """
    st = settings or RouteSettings()
    started = time.perf_counter()
    result = RouteResult(ok=False, model=model_name)

    def stage(text: str) -> None:
        if on_stage is not None:
            try:
                on_stage(text)
            except Exception:  # noqa: BLE001 - 界面回调失败不该毁掉路由
                pass

    # ---- 1. 候选 ----
    stage(f"正在拉取「{model_name}」的商家列表…")
    payload = fetch_channels(control_base, access_token, user_id, model_name)
    # 我自己的调用记录：谁真的失败过、一次要等多久。这是全套判据里最可信的一份，
    # 站点自己的成功率与实际差得很远（Ch 6490 站点 97%，实测 148 次里 29 次失败）。
    if st.check_merchant_log:
        stage("正在读我自己的调用记录（真实失败率与延迟）…")
        stats = merchant_stats(control_base, access_token, user_id, model_name)
    else:
        stats = {}
    cands, total = parse_candidates(payload, model_name, st, stats)
    result.total_listings = total
    if not cands:
        result.message = (
            f"没有商家同时满足：倍率 ≤ {st.max_ratio:.2f}、"
            f"延迟 ≤ {secs(st.hard_p50_ms)}、实测失败率 ≤ {st.max_fail_rate:g}%、"
            f"样本 ≥ {st.min_samples}。（该模型共 {total} 条报价）"
        )
        result.elapsed_s = time.perf_counter() - started
        return result

    # ---- 1.5 排除「刚刚还在失败」的商家 ----
    # 长期失败率会「洗白」一个刚坏掉的商家（200 次里失败 3 次 = 1.5%，但那 3 次
    # 可能全在刚才五分钟内）。所以固定前再看一眼最近这一小段。
    faults: dict[int, dict] = {}
    horizon = time.time() - MERCHANT_BAD_WINDOW_S
    for cid, stat in stats.items():
        if stat.fails > 0 and stat.last_fail >= horizon:
            faults[cid] = {"fails": stat.fails, "last": stat.last_fail,
                           "code": stat.last_code}
    blocked: dict[int, str] = {}
    for cid, info in faults.items():
        code = f"（{info['code']}）" if info.get("code") else ""
        blocked[cid] = f"站点日志显示近 45 分钟失败 {info.get('fails', 1)} 次{code}"
    for cid, info in load_bad_channels(control_base, model_name).items():
        blocked.setdefault(cid, f"本机记录到 {info.get('fails', 1)} 次校验失败")
    healthy = [c for c in cands if c.channel_id not in blocked]
    if healthy and len(healthy) < len(cands):
        result.unhealthy_skipped = [
            f"{c.label}：{blocked[c.channel_id]}"
            for c in cands if c.channel_id in blocked]
        stage(f"已跳过 {len(cands) - len(healthy)} 家刚出过故障的商家，"
              f"在剩下 {len(healthy)} 家里选…")
        cands = healthy
    elif not healthy:
        # 全都被标记过：不能就此放弃（可能只是刚才那一阵抖动），照样往下测。
        stage("候选商家最近都出过故障，仍逐家实测（不做排除）。")

    ranked = rank(cands, st)[:max(1, st.candidates)]
    result.total_candidates = len(ranked)
    stage(f"筛选出 {len(cands)} 家候选，取前 {len(ranked)} 家逐个实测…")

    # ---- 2. 记下原状态，全败时能还原 ----
    try:
        original = _current_pin(
            list_pins(control_base, access_token, user_id), token_id, model_name)
    except (hj.HttpError, hj.NetError, RouteError, OSError):
        original = None      # 读不到就不冒险改，后面失败时不做还原

    # ---- 3. 逐个：固定 → 实测 → 通过则留 ----
    for index, cand in enumerate(ranked, start=1):
        if should_stop is not None and should_stop():
            result.message = "已取消。"
            break
        stage(f"[{index}/{len(ranked)}] 固定到 {cand.label}"
              f"（倍率 {cand.ratio:.3f} · {secs(cand.p50_ms)}）并实测…")
        try:
            pin_channel(control_base, access_token, user_id, token_id=token_id,
                        model_name=model_name, channel_id=cand.channel_id,
                        fallback=st.fallback_to_smart_routing)
            pinned_at = time.time()
        except (hj.HttpError, hj.NetError, RouteError) as e:
            result.tested.append((cand, f"固定失败：{e}", None))
            continue
        if st.settle_s > 0:
            time.sleep(st.settle_s)

        # 采样 `attempts` 次，全部通过才算可用。
        # 单次成功会误判：商家侧的故障是间歇性的（站点日志里同一渠道 3 分钟内
        # 6 次失败 / 4 次成功，每次 10 秒、0 token、no_healthy_account），
        # 而用户一旦被固定到它上面，之后每一次真实调用都要吃这个亏。
        # 默认 2 次，都是**流式**（真实调用全走 SSE）：①流式 + 长输出（撞「说到
        # 一半被掐断」）②流式 + 短输出（正文必须复述 PONG）。不再测非流式整包。
        want = max(1, st.attempts)
        base_tokens = max(VERIFY_MIN_TOKENS, st.max_tokens)
        probe: mp.SpeedResult | None = None
        failure = ""
        first_ttft: int | None = None
        for attempt in range(1, want + 1):
            # 形态按矩阵轮换，见 _VERIFY_SHAPES：第一位就是最容易坏、代价最大的
            # 「流式 + 长输出」。换形态比把同一个请求重复两遍信息量大得多。
            buffered, long_out = _verify_shape(attempt)
            if attempt > 1 and VERIFY_GAP_S > 0:
                time.sleep(VERIFY_GAP_S)     # 隔开一点，别把两次采样挤成同一个瞬间
            def _once(buffered=buffered, long_out=long_out, attempt=attempt):
                return mp.probe_speed(
                    protocol, api_base, api_key, model_name,
                    rounds=max(1, st.rounds) if attempt == 1 else 1,
                    max_tokens=(max(VERIFY_LONG_TOKENS, base_tokens) if long_out
                                else base_tokens),
                    prompt=VERIFY_LONG_PROMPT if long_out else st.prompt,
                    ttft_limit_ms=st.ttft_limit_ms,
                    use_proxy=_PROXY_STATE["use"],
                    expect="" if long_out else st.expect,
                    force_buffered=buffered,
                    min_chars=VERIFY_LONG_MIN_CHARS if long_out else 0,
                )

            probe = _once()
            if not probe.ok and is_throttled(probe):
                # 限流是「暂时忙」，不是「坏了」。实测同一家商家零间隔连发立刻 429、
                # 隔 6 秒就正常——此时直接换家，换掉的是个其实能用的商家。
                stage(f"[{index}/{len(ranked)}] {cand.label} 被限流，"
                      f"{THROTTLE_BACKOFF_S:g} 秒后重测这一次…")
                time.sleep(THROTTLE_BACKOFF_S)
                probe = _once()
                if probe.ok:
                    probe.warnings.append("首测被限流，退避后重测通过")
            if probe.ok and first_ttft is None and probe.ttft_ms is not None:
                first_ttft = probe.ttft_ms
            if not probe.ok:
                failure = probe.error or ("首字超过上限" if probe.too_slow else "无有效响应")
                if attempt > 1:
                    failure = (f"第 {attempt} 次采样（{_shape_label(buffered, long_out)}）"
                               f"未通过：{failure}")
                break
            if attempt < want:
                stage(f"[{index}/{len(ranked)}] {cand.label} "
                      f"{attempt}/{want} 通过（{_shape_label(buffered, long_out)}），继续采样…")
        # 终检：pin 已生效、采样全过，再用「带 system + tools + 4 千 token 上下文」
        # 的真实形态确认一次。用户要的是「选完这一刻就能用」，而真实 Agent 的第一
        # 个请求正是这个形状——只测短问答会漏掉「不支持 function calling」
        # 和「上下文窗口比宣传的小」这两类硬伤。
        if probe is not None and probe.ok and not failure:
            stage(f"[{index}/{len(ranked)}] {cand.label} 采样通过，"
                  f"用真实 Agent 形态（带 tools + 4 千 token 上下文）终检…")
            ok_final, why_final = final_check(protocol, api_base, api_key,
                                              model_name, st)
            if not ok_final:
                failure = why_final
                result.tested.append((cand, failure, None))
                if not failure.startswith("终检被限流"):
                    mark_bad_channel(control_base, model_name, cand.channel_id,
                                     code=failure)
                stage(f"[{index}/{len(ranked)}] {cand.label} 不可用：{failure}")
                continue

        # 日志复核：采样 + 终检都过了，再核对站点里这条 (模型, 渠道) 的**真实
        # 调用记录**。采样通过只能证明「那几秒能用」，而商家的故障是成片抽风的
        # （实测同一家 3 分钟内 6 次 500/503、0 token、耗时 11 秒，中间还夹着
        # 成功）——真实调用日志里只要固定之后又失败过，就换下一家。
        log_note = ""
        if probe is not None and probe.ok and not failure:
            stage(f"[{index}/{len(ranked)}] {cand.label} 采样与终检通过，"
                  f"核对站点调用日志（真实调用是否也正常）…")
            confirm = {"fails": 0, "rows": 0}
            try:
                confirm = recent_channel_faults(
                    control_base, access_token, user_id, model_name,
                    cand.channel_id, since=pinned_at)
                if confirm["rows"] == 0:
                    # 日志落库偶尔慢一拍。什么都没看到时不能当成「确认通过」，
                    # 等一秒再看；第二次还是空就只认采样结果，不拦住路由。
                    time.sleep(LOG_CONFIRM_RETRY_S)
                    confirm = recent_channel_faults(
                        control_base, access_token, user_id, model_name,
                        cand.channel_id, since=pinned_at)
                if confirm["fails"] > 0:
                    code = f"（{confirm['code']}）" if confirm.get("code") else ""
                    failure = (f"站点日志复核：固定后真实调用仍失败 "
                               f"{confirm['fails']} 次{code}，暂时不可用")
                    result.tested.append((cand, failure, None))
                    mark_bad_channel(control_base, model_name, cand.channel_id,
                                     code=failure)
                    stage(f"[{index}/{len(ranked)}] {cand.label} 不可用：{failure}")
                    continue
                if confirm["rows"] > 0:
                    log_note = (f"；站点日志复核通过（固定后 {confirm['rows']} 条"
                                f"真实调用、0 失败）")
            except (hj.HttpError, hj.NetError, RouteError, OSError) as e:
                stage(f"[{index}/{len(ranked)}] 调用日志读不到（{e}），"
                      f"仍以采样与终检为准")

        if probe is not None and probe.ok and not failure:
            # 首字取流式那一轮；两档都是流式，首字一定能测到。
            head_ms = first_ttft if first_ttft is not None else probe.ttft_ms
            head_ms = first_ttft if first_ttft is not None else probe.ttft_ms
            result.tested.append((cand, f"通过 {want}/{want}", head_ms))
            result.ok = True
            result.candidate = cand
            price = record_price(model_name, cand)
            # 说明里写**实际测过的形态**，不笼统夸口。
            coverage = "、".join(_shape_label(*_verify_shape(i))
                                 for i in range(1, want + 1))
            if want >= len(_VERIFY_SHAPES):
                coverage += "（流式长/短输出都验过）"
            else:
                coverage += f"（共 {len(_VERIFY_SHAPES)} 种形态，本次覆盖 {want} 种）"
            skip_note = (f"，已跳过 {len(result.unhealthy_skipped)} 家刚出过故障的商家"
                         if result.unhealthy_skipped else "")
            result.message = (
                f"已固定到 {cand.label}：{cand.stat_label}、"
                f"实测首字 {secs(head_ms) if head_ms is not None else '—'}"
                f"（{want}/{want} 次采样通过：{coverage}；"
                f"真实形态终检通过：带 tools + 4 千 token 上下文）{skip_note}"
                + log_note
                + (f"；价格表已更新为 {price:g} 元/百万 token。" if price is not None else "。")
            )
            result.elapsed_s = time.perf_counter() - started
            return result

        reason = failure or "无有效响应"
        result.tested.append((cand, reason, None))
        # 记进本地「刚坏过」名单：同一家连着被选上来、连着失败，说明排序每次都在
        # 重复同一个错误选择——下一轮直接跳过它。**限流不算坏**：那家只是忙，
        # 记进去等于把它永久拉黑 45 分钟，下次路由会白白少一个便宜候选。
        if not (probe is not None and is_throttled(probe)):
            mark_bad_channel(control_base, model_name, cand.channel_id, code=reason)
        stage(f"[{index}/{len(ranked)}] {cand.label} 不可用：{reason}")

    # ---- 4. 全败：还原 ----
    if not result.ok:
        try:
            if original is not None:
                pin_channel(control_base, access_token, user_id, token_id=token_id,
                            model_name=model_name,
                            channel_id=int(original.get("channel_id") or 0),
                            fallback=bool(original.get("fallback_to_smart_routing")))
                result.restored = True
            elif result.tested:
                unpin_channel(control_base, access_token, user_id,
                              token_id=token_id, model_name=model_name,
                              channel_id=result.tested[-1][0].channel_id)
                result.restored = True
        except (hj.HttpError, hj.NetError, RouteError) as e:
            result.message += f"（还原原路由失败：{e}）"
        if not result.message:
            result.message = f"{len(result.tested)} 家候选实测均未通过，未做任何固定。"
            if result.unhealthy_skipped:
                result.message += (f"（另有 {len(result.unhealthy_skipped)} 家因最近故障"
                                   f"被提前排除，例如 {result.unhealthy_skipped[0]}）")
    result.elapsed_s = time.perf_counter() - started
    return result


# --------------------------------------------------------------------------- #
# 命令行：不打开界面也能跑一次，便于排查
# --------------------------------------------------------------------------- #

def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="A6API 智能路由（命令行）")
    parser.add_argument("--api-base", required=True, help="API 地址，如 https://api.a6api.vip/v1")
    parser.add_argument("--control-base", default="", help="控制面地址，默认由 api-base 推断")
    parser.add_argument("--access-token", required=True)
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--token-id", type=int, required=True, help="要固定的令牌 ID")
    parser.add_argument("--api-key", required=True, help="该令牌的 sk- key，用于实测")
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-ratio", type=float, default=None,
                        help=f"覆盖设置里的倍率上限（默认 {MAX_PRICE_RATIO}）")
    parser.add_argument("--candidates", type=int, default=None,
                        help="覆盖设置里的实测家数")
    args = parser.parse_args()

    # 以设置里的参数为基准，命令行只覆盖显式给出的那几项——
    # 否则在界面上调好的权重会被命令行悄悄换回默认值，结果对不上。
    st = load_settings()
    if args.max_ratio is not None:
        st.max_ratio = args.max_ratio
    if args.candidates is not None:
        st.candidates = args.candidates
    out = route(
        api_base=args.api_base,
        control_base=args.control_base or guess_control_base(args.api_base),
        access_token=args.access_token,
        user_id=args.user_id,
        token_id=args.token_id,
        api_key=args.api_key,
        model_name=args.model,
        settings=st,
        on_stage=lambda t: print(t, flush=True),
    )
    print("=" * 60)
    total_w = st.price_weight + st.latency_weight + st.success_weight or 1
    print("权重占比: 价格 %.0f%% / 延迟 %.0f%% / 成功率 %.0f%%" % (
        100.0 * st.price_weight / total_w, 100.0 * st.latency_weight / total_w,
        100.0 * st.success_weight / total_w))
    print("结果:", "成功" if out.ok else "失败")
    print("说明:", out.message)
    print("耗时: %.1fs  候选: %d  报价总数: %d" % (out.elapsed_s, out.total_candidates,
                                                   out.total_listings))
    for cand, verdict, ttft in out.tested:
        print("  - %-40s 倍率 %.3f  p50 %7s  → %s" % (
            cand.label, cand.ratio, secs(cand.p50_ms), verdict))


if __name__ == "__main__":
    main()
