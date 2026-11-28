#!/usr/bin/env python3
"""模型管家 · 网络与解析层。

输入 baseURL + API Key，拉取该供应商的可用模型列表。

解析逻辑对齐 DeepSeek Harness 的 `dsh-llm-pi-ai/discovery.ts`：
    GET {baseURL}/models  ->  data[].id / name|display_name
                              / context_window|context_length / max_output_tokens|max_tokens

另外支持 Anthropic / Gemini / Ollama 三家各自的列表接口（它们不遵循 [OI] 形状）。

本模块不依赖 GUI，可单独调用与测试；界面在 `gui_app.py`，参数规格在 `model_spec.py`。
零第三方依赖：只用标准库。
"""

from __future__ import annotations

import json
import re
import socket
import ssl
import statistics
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

APP_TITLE = "模型管家"
# 唯一版本号：exe 属性里的「文件版本 / 产品版本」都取自这里（见 build_exe.py），
# 想升版本只改这一处。
APP_VERSION = "1.0.17"
# 窗口标题 = 名字 + 版本，显示在标题栏左上角；弹窗仍用不带版本的 APP_TITLE。
WINDOW_TITLE = f"{APP_TITLE} v{APP_VERSION}"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
DEFAULT_TIMEOUT = 25

# ---- 测速默认参数 ----
# 提示词要求「稳定、可预期长度」的输出，避免模型自由发挥导致轮次间偏差过大。
# 数到 120 而不是 60：输出越长，解码窗口越长，中转站「攒一批再冲」的冲刷节奏
# 对速率的影响就越小（见 SPEED_MIN_WINDOW_S 的说明）。
SPEED_PROMPT = "请从 1 数到 120，每个数字之间用空格分隔。只输出数字，不要任何解释或标点。"
SPEED_MAX_TOKENS = 320      # 上限而非目标：够 120 个数字，又不至于被自由发挥撑爆
SPEED_DEFAULT_ROUNDS = 3    # 多轮测速，首字取最快、速率按各轮汇总
SPEED_STREAM_BYTES = 2 * 1024 * 1024

# 速率的可信门槛：合计解码窗口 < 0.5 秒或合计正文 < 64 token 时，误差主要来自
# 中转站的冲刷粒度而不是模型本身。实测同一个模型同一端点，单轮速率能在
# 234~869 tok/s 之间跳（3.7 倍），就是因为 160 token 的样本只覆盖十几个冲刷周期。
SPEED_MIN_WINDOW_S = 0.5
SPEED_MIN_TOKENS = 64

# 中转站不会一个 token 一个包地转发，而是攒一批再冲（实测每 30~120ms 冲一批）。
# 于是「首个正文帧」其实已经晚于模型开始吐字：第一批里的那些 token 是在窗口
# 之前产出的，却算进了分子。窗口两端各被推迟一个冲刷周期，速率就被放大
# （窗口 0.2 秒时能放大一倍）。用帧间隔的中位数估出冲刷周期，补回分母。
SPEED_BURST_GAP_S = 0.002   # 小于 2ms 的间隔属于「同一批」，不是冲刷周期

# 首字延迟的可用上限（毫秒）。超过它就没法用于生产制作，继续测只是白等，
# 因此超时即放弃并返回 ttft_ms=None —— 界面显示「-」。
SPEED_TTFT_LIMIT_MS = 10_000
# 推理模型的最小观察预算：输出上限低于这个值时，token 可能全被思考过程吃掉，
# 正文一个都不剩（实测 deepseek-v4.1-flash 用 16 就是这样），测得再多次也只有首字。
SPEED_REASONING_MIN_TOKENS = 192
# 非流式（降级）路径的**整体**时长上限（秒）。
# socket 超时在每次成功读取后都会重置，服务端缓慢滴流能突破任意单次超时——
# 账户日志里 662 秒的长尾就是这么来的。流式路径有「首字窗口 + 总时长」双重兜底，
# 整包计时这条路径原来只有 socket 超时，这里补齐。
SPEED_BUFFERED_TOTAL_S = 50
# 并行测速的默认并发数。测速是纯 IO 等待（等排队、等首字），线程本身几乎不吃 CPU，
# 所以并发数只受两条端点侧约束：中转站的并发限流（429），以及总带宽被跑满后
# 互相拖慢、把首字/速率测成「拥塞下的值」而不是模型的能力值。
# 8 路在实测中既能把百模型全量测速的耗时压到 1/8 左右，又很少触发限流。
SPEED_DEFAULT_WORKERS = 8
# 上限留给「自家不限流的端点」：再往上加，瓶颈一定在带宽或限流，测量值开始失真。
SPEED_MAX_WORKERS = 16

# 协议 -> 显示名
PROTOCOLS: dict[str, str] = {
    "openai": "[OI] 兼容（/models）",
    "anthropic": "Anthropic（/v1/models）",
    "gemini": "Google Gemini（/v1beta/models）",
    "ollama": "Ollama（/api/tags）",
}


@dataclass
class Model:
    """一条可用模型记录。context / max_out 为 None 表示端点没有披露。"""

    id: str
    name: str = ""
    context: int | None = None
    max_out: int | None = None
    note: str = ""

    def as_dict(self) -> dict:
        out: dict[str, object] = {"id": self.id}
        if self.name:
            out["name"] = self.name
        if self.context is not None:
            out["contextWindow"] = self.context
        if self.max_out is not None:
            out["maxTokens"] = self.max_out
        if self.note:
            out["note"] = self.note
        return out


@dataclass
class ProbeResult:
    models: list[Model] = field(default_factory=list)
    endpoint: str = ""
    status: int = 0
    raw: str = ""
    tried: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    @property
    def with_context(self) -> int:
        return sum(1 for m in self.models if m.context is not None)


@dataclass
class SpeedResult:
    """一次测速的结果。

    两个核心指标：
      * `ttft_ms` 首字延迟 —— 请求发出到**首个含正文的增量包**到达的毫秒数。
        故意不用「首个 SSE 包」：某些中转站会先发一个空 delta 的心跳包，
        那种包到达只证明连接建立，不代表模型开始吐字。
      * `tps` 生成速率 —— **窗口内正文 token 数 ÷ 各轮解码窗口之和**。
        分子是 `completion_tokens` 扣掉 `reasoning_tokens`（推理 token 在首字
        之前产出，已计入首字延迟）；分母是多轮窗口相加，而不是单轮窗口，
        因为中转站一次冲刷几十毫秒，单轮窗口太短时速率会跳好几倍。
        `reliable=False` 表示合计样本仍偏短，这个数只够横向参考。

    `rounds` 为 0 表示本次没测到任何可用数据；`partial` 为真表示降级测量
    （如端点不支持流式，只能测总耗时），此时 ttft 不可拆分。
    """

    model: str = ""
    rounds: int = 0
    ttft_ms: int | None = None       # 取多轮中最快一轮
    tps: float | None = None         # 多轮汇总：Σ正文 token ÷ Σ解码窗口
    samples: list[dict] = field(default_factory=list)
    usage_tokens: int | None = None  # 端点回报的 completion_tokens（含推理 token）
    text_tokens: int = 0             # 各轮扣掉推理 token 后的正文 token 合计
    window_ms: int = 0               # 各轮解码窗口合计（速率的真实分母）
    reasoning_tokens: int = 0        # 被扣除的推理 token 合计
    reliable: bool = True            # 样本是否长到能信这个速率
    approx: bool = False             # token 数为按 chunk 估算，非端点回报
    partial: bool = False            # 降级测量（无流式），ttft 不可拆分
    total_ms: int = 0                # 最后一轮总耗时
    endpoint: str = ""          # 实际命中的对话端点
    content: str = ""           # 最后一轮的正文（截断），用于校验「真的在提供服务」
    error: str = ""
    warnings: list[str] = field(default_factory=list)
    # 首字超过 SPEED_TTFT_LIMIT_MS 而主动放弃：既不是「没测过」，也不是「出错」。
    # 界面据此显示「-」，表示这个模型延迟太长、无法用于生产制作。
    too_slow: bool = False

    @property
    def ok(self) -> bool:
        return not self.error and self.rounds > 0

    def as_speed_record(self) -> dict:
        """测速结果的扁平字段快照（导出用；模型库已取消，不再写库）。"""
        return {
            "model_id": self.model,
            "ttft_ms": self.ttft_ms,
            "tps": self.tps,
            "rounds": self.rounds,
        }

    def summary(self) -> str:
        if self.error:
            return f"{self.model}：{self.error}"
        if self.too_slow:
            return f"{self.model}：首字超过 {limit_label(SPEED_TTFT_LIMIT_MS)}上限，已放弃（-）"
        if not self.rounds:
            return f"{self.model}：未取到样本"
        parts = []
        if self.ttft_ms is not None:
            parts.append(f"首字 {self.ttft_ms / 1000:.2f} s")   # 界面一律用秒
        if self.tps is not None:
            parts.append(f"{self.tps:.1f} tok/s")
        text = " · ".join(parts) or "无有效指标"
        return f"{self.model}：{text}" + ("（降级）" if self.partial else "")


def _positive_int(*candidates: object) -> int | None:
    """DSH 的 capacity()：只接受正整数，其余一律 None。"""
    for c in candidates:
        if isinstance(c, bool):
            continue
        if isinstance(c, int) and c > 0:
            return c
        if isinstance(c, float) and c.is_integer() and c > 0:
            return int(c)
    return None


def _label(*candidates: object) -> str:
    for c in candidates:
        if isinstance(c, str) and c.strip():
            return c.strip()
    return ""


def _candidate_urls(protocol: str, base_url: str, auto_v1: bool) -> list[str]:
    """按 DSH 的方式把 baseURL 当**前缀**拼，不做 URL resolve，保留部署路径段。"""
    base = base_url.strip().rstrip("/")
    if protocol == "ollama":
        return [f"{base}/api/tags"]
    if protocol == "gemini":
        if base.endswith("/v1beta") or base.endswith("/v1"):
            return [f"{base}/models"]
        return [f"{base}/v1beta/models"] + ([f"{base}/v1/models"] if auto_v1 else [])
    if protocol == "anthropic":
        urls = [f"{base}/models"]
        if auto_v1 and not base.endswith("/v1"):
            urls.append(f"{base}/v1/models")
        return urls
    # openai 兼容
    urls = [f"{base}/models"]
    if auto_v1 and not base.endswith(("/v1", "/v1beta", "/openai/v1")):
        urls.append(f"{base}/v1/models")
    return urls


def _build_headers(protocol: str, api_key: str) -> dict[str, str]:
    key = api_key.strip()
    if protocol == "anthropic":
        h = {"accept": "application/json", "anthropic-version": "2023-06-01"}
        if key:
            h["x-api-key"] = key
        return h
    if protocol == "gemini":
        h = {"accept": "application/json"}
        if key:
            h["x-goog-api-key"] = key
        return h
    h = {"accept": "application/json", "user-agent": "model-probe/1.0"}
    if key:
        h["authorization"] = f"Bearer {key}"
    return h


def _read_bounded(resp, deadline: float | None = None) -> str:
    """DSH 的两段式上限：先看 content-length，再按实际累计字节拒绝。

    `deadline` 是 `time.perf_counter()` 口径的绝对时刻，用来兜住「缓慢滴流」：
    socket 超时每次读到数据就重置，只靠它挡不住一个每 20 秒吐 1 KB 的服务端。
    """
    declared = resp.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_RESPONSE_BYTES:
        raise ValueError(f"响应超过 {MAX_RESPONSE_BYTES // 1024 // 1024} MB 上限")
    chunks, total = [], 0
    while True:
        if deadline is not None and time.perf_counter() > deadline:
            raise TimeoutError("响应读取超过整体时长上限")
        chunk = resp.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_RESPONSE_BYTES:
            raise ValueError(f"响应超过 {MAX_RESPONSE_BYTES // 1024 // 1024} MB 上限")
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", "replace")


def _parse(protocol: str, body: object) -> list[Model]:
    models: list[Model] = []

    if protocol in ("openai", "anthropic"):
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise ValueError('响应里没有 "data" 数组，这个端点可能不是该协议的模型列表接口')
        for raw in data:
            if not isinstance(raw, dict):
                continue
            mid = _label(raw.get("id"))
            if not mid:  # 单条脏数据不该毁掉整个列表
                continue
            models.append(
                Model(
                    id=mid,
                    name=_label(raw.get("name"), raw.get("display_name")),
                    context=_positive_int(raw.get("context_window"), raw.get("context_length")),
                    max_out=_positive_int(raw.get("max_output_tokens"), raw.get("max_tokens")),
                )
            )
        return models

    if protocol == "gemini":
        data = body.get("models") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise ValueError('响应里没有 "models" 数组，检查 baseURL 是否为 /v1beta')
        for raw in data:
            if not isinstance(raw, dict):
                continue
            mid = _label(raw.get("name"))
            if not mid:
                continue
            models.append(
                Model(
                    id=mid.removeprefix("models/"),
                    name=_label(raw.get("displayName")),
                    context=_positive_int(raw.get("inputTokenLimit")),
                    max_out=_positive_int(raw.get("outputTokenLimit")),
                )
            )
        return models

    if protocol == "ollama":
        data = body.get("models") if isinstance(body, dict) else None
        if not isinstance(data, list):
            raise ValueError('响应里没有 "models" 数组，检查 baseURL 是否指向 ollama 服务')
        for raw in data:
            if not isinstance(raw, dict):
                continue
            mid = _label(raw.get("name"), raw.get("model"))
            if not mid:
                continue
            details = raw.get("details") if isinstance(raw.get("details"), dict) else {}
            note = " / ".join(
                str(v) for v in (details.get("parameter_size"), details.get("quantization_level")) if v
            )
            models.append(Model(id=mid, note=note))
        return models

    raise ValueError(f"未知协议 {protocol}")


def fetch_models(
    protocol: str,
    base_url: str,
    api_key: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    use_proxy: bool = False,
    auto_v1: bool = True,
) -> ProbeResult:
    """探测一个端点。永不抛异常，失败信息放在 result.error。"""
    result = ProbeResult()
    if not base_url.strip():
        result.error = "请先填写 API 地址（baseURL）。"
        return result
    urls = _candidate_urls(protocol, base_url, auto_v1)
    headers = _build_headers(protocol, api_key)
    # 直连时显式给空 ProxyHandler，否则 urllib 会读环境里的 HTTP(S)_PROXY
    proxy_handler = urllib.request.ProxyHandler() if use_proxy else urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(proxy_handler)

    last_error = ""
    for url in urls:
        result.tried.append(url)
        req = urllib.request.Request(url, headers=headers, method="GET")
        retry_next = False
        try:
            with opener.open(req, timeout=timeout) as resp:
                result.status = resp.status
                result.endpoint = url
                result.raw = _read_bounded(resp)
            break
        except urllib.error.HTTPError as e:
            result.status = e.code
            detail = ""
            try:
                detail = e.read(400).decode("utf-8", "replace")
            except Exception:
                pass
            if e.code in (401, 403):
                last_error = f"{url} 返回 {e.code}：凭据被拒（检查 API Key）。{_plain(detail)}"
            elif e.code in (404, 405):
                last_error = f"{url} 返回 {e.code}：该端点没有模型列表接口，或路径不对（可试 /v1）。"
                retry_next = True  # 只有路径类错误才值得换下一个候选
            elif e.code in (500, 502, 503, 504):
                last_error = (f"{url} 返回 {e.code}：站点网关故障，与令牌和模型无关，稍等重试。"
                              f"{_plain(detail, 100)}")
            else:
                last_error = f"{url} 返回 {e.code}。{_plain(detail)}"
        except urllib.error.URLError as e:
            reason = str(e.reason)
            if "CERTIFICATE" in reason.upper() or "SSL" in reason.upper():
                last_error = f"{url} TLS 失败：{reason}"
            else:
                last_error = f"无法连接 {url}：{reason}（国内直连不通时勾选「走系统代理」）"
        except ValueError as e:  # 体积上限
            last_error = str(e)
        except Exception as e:  # noqa: BLE001 - 兜底，GUI 不崩
            last_error = f"{url} 请求失败：{e}"
        if not retry_next:
            break

    if not result.raw:
        result.error = last_error or "没有拿到任何响应。"
        return result

    try:
        body = json.loads(result.raw)
    except json.JSONDecodeError:
        result.error = f"{result.endpoint} 返回的不是 JSON：{_plain(result.raw, 120)}"
        return result

    try:
        result.models = _parse(protocol, body)
    except ValueError as e:
        result.error = str(e)
        return result

    if not result.models:
        result.error = "端点连通，但没有列出任何模型。"
    return result


# --------------------------------------------------------------------------- #
# 测速：首字延迟（TTFT）与生成速率（TPS）
# --------------------------------------------------------------------------- #

def chat_url(protocol: str, base_url: str) -> str:
    """把 baseURL 补成该协议的对话端点（首选候选，用于界面展示）。

    口径与模型列表接口一致：baseURL 当**前缀**用。所以 `{base}/models` 能通，
    对应的对话端点就是 `{base}/chat/completions`。裸域（不带 /v1）会自动补 /v1，
    由 `chat_candidates()` 在 404 时回退，不在这里做多分支猜测。
    """
    cands = chat_candidates(protocol, base_url)
    return cands[0] if cands else ""


def chat_candidates(protocol: str, base_url: str, auto_v1: bool = True) -> list[str]:
    """对话端点候选列表，与 `_candidate_urls` 同构：先按前缀直拼，再试 /v1。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return []
    if protocol == "ollama":
        return [base if base.endswith("/api/chat") else f"{base}/api/chat"]
    if protocol == "gemini":
        if base.endswith("/v1beta") or base.endswith("/v1"):
            stem = base
        else:
            stem = f"{base}/v1beta"
        return [f"{stem}/models/{{model}}:streamGenerateContent?alt=sse"]
    if protocol == "anthropic":
        if base.endswith("/v1"):
            return [f"{base}/messages"]
        return [f"{base}/messages"] + ([f"{base}/v1/messages"] if auto_v1 else [])
    # openai 兼容：与 _candidate_urls 完全对称
    urls = [f"{base}/chat/completions"]
    if auto_v1 and not base.endswith(("/v1", "/v1beta", "/openai/v1")):
        urls.append(f"{base}/v1/chat/completions")
    return urls


def _speed_headers(protocol: str, api_key: str) -> dict[str, str]:
    h = _build_headers(protocol, api_key)
    h["content-type"] = "application/json"
    return h


def _speed_payload(protocol: str, model: str, *, prompt: str, max_tokens: int,
                   include_usage: bool, system: str = "",
                   tools: list | None = None, reasoning_effort: str = "") -> dict:
    """拼请求体。`system` / `tools` / `reasoning_effort` 用来模仿真实 Agent 调用。

    为什么要能带这些：智能路由的校验请求如果只有一句「请回复 PONG」，
    就测不出「这家不支持 function calling」「带上 system 与工具定义后上游 400」
    这类问题——而 Codex / Claude Code / DSH 的真实请求里两样都有。
    只对 openai 方言生效：别的方言工具 schema 形状不同，硬塞只会测出假失败。
    """
    if protocol == "anthropic":
        body: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "stream": True,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system:
            body["system"] = system
        return body
    if protocol == "gemini":
        return {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {"maxOutputTokens": max_tokens},
        }
    if protocol == "ollama":
        return {
            "model": model,
            "stream": True,
            "messages": [{"role": "user", "content": prompt}],
            "options": {"num_predict": max_tokens},
        }
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    body = {
        "model": model,
        "stream": True,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if tools and protocol == "openai":
        body["tools"] = tools
    if reasoning_effort and protocol == "openai":
        body["reasoning_effort"] = reasoning_effort
    if include_usage:
        # 没有它，多数 OpenAI 兼容端点不会在流式响应里回传 usage
        body["stream_options"] = {"include_usage": True}
    return body


def _extract_delta(protocol: str, obj: dict) -> tuple[str, int | None]:
    """从一帧增量对象里抠出「正文文本」与「累计输出 token 数」。

    返回 (text, completion_tokens)。text 为空表示这一帧没有正文
    （心跳包、role 声明帧、推理帧、结束帧等），调用方应忽略它。
    """
    if protocol == "anthropic":
        kind = obj.get("type")
        if kind == "content_block_delta":
            delta = obj.get("delta") or {}
            return (delta.get("text") or ""), None
        if kind == "message_delta":
            usage = obj.get("usage") or {}
            return "", _positive_int(usage.get("output_tokens"))
        return "", None

    if protocol == "gemini":
        cands = obj.get("candidates") or []
        text = ""
        if cands and isinstance(cands[0], dict):
            parts = ((cands[0].get("content") or {}).get("parts")) or []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        usage = obj.get("usageMetadata") or {}
        return text, _positive_int(usage.get("candidatesTokenCount"))

    if protocol == "ollama":
        msg = obj.get("message") or {}
        usage = _positive_int(obj.get("eval_count"))
        if obj.get("done"):
            return "", usage
        return (msg.get("content") or ""), usage

    # openai 兼容
    choices = obj.get("choices") or []
    text = ""
    if choices and isinstance(choices[0], dict):
        text = (choices[0].get("delta") or {}).get("content") or ""
    usage = obj.get("usage") or {}
    return text, _positive_int(usage.get("completion_tokens"))


def _reasoning_delta(protocol: str, obj: dict) -> str:
    """从一帧增量里抠出**推理/思考**文本（不是正文）。

    推理模型（deepseek-v4、o 系列、claude thinking）会把思考过程单独放在
    `reasoning_content` / `reasoning` / `thinking_delta` 里。这些内容不是给用户看的
    正文，但它确实是「模型开始产出了」的信号——首字计时必须认它，否则把预算全花在
    推理上的模型会被误判成「没有任何增量」。
    """
    if protocol == "anthropic":
        if obj.get("type") == "content_block_delta":
            delta = obj.get("delta") or {}
            if delta.get("type") in ("thinking_delta", "signature_delta"):
                return str(delta.get("thinking") or "")
        return ""

    if protocol == "gemini":
        cands = obj.get("candidates") or []
        if not (cands and isinstance(cands[0], dict)):
            return ""
        parts = ((cands[0].get("content") or {}).get("parts")) or []
        return "".join(str(p.get("text") or "") for p in parts
                       if isinstance(p, dict) and p.get("thought"))

    if protocol == "ollama":
        msg = obj.get("message") or {}
        return str(msg.get("thinking") or "")

    choices = obj.get("choices") or []
    if choices and isinstance(choices[0], dict):
        delta = choices[0].get("delta") or {}
        # 各家写法不一：DeepSeek 用 reasoning_content，OpenRouter/o 系列用 reasoning
        return str(delta.get("reasoning_content") or delta.get("reasoning") or "")
    return ""


def _reasoning_tokens_of(obj: dict) -> int | None:
    """端点回报的「隐藏推理 token」数。

    推理模型（deepseek-r1 一类）的 `completion_tokens` **包含** reasoning_tokens，
    而那些 token 是在首字之前产出的、已经算进首字延迟里。若拿它除以「首字到末字」
    的正文窗口，速率会被凭空放大 1.3~1.6 倍——实测 deepseek-v4.1-flash 一轮
    160 token 里有 51 个推理 token，不扣掉就是 869 tok/s，扣掉是 592 tok/s。
    """
    for key in ("usage", "usageMetadata"):
        usage = obj.get(key)
        if not isinstance(usage, dict):
            continue
        details = usage.get("completion_tokens_details") or usage.get("output_tokens_details")
        if isinstance(details, dict):
            n = _positive_int(details.get("reasoning_tokens"))
            if n is not None:
                return n
        n = _positive_int(usage.get("thoughtsTokenCount"))   # gemini
        if n is not None:
            return n
    return None


def _extract_full(protocol: str, obj: dict) -> str:
    """从**非流式**响应体里取正文。用于端点无视 `stream:true` 的降级测量。"""
    if protocol == "anthropic":
        return "".join(
            b.get("text", "") for b in (obj.get("content") or []) if isinstance(b, dict)
        )
    if protocol == "gemini":
        cands = obj.get("candidates") or []
        if cands and isinstance(cands[0], dict):
            parts = ((cands[0].get("content") or {}).get("parts")) or []
            return "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        return ""
    if protocol == "ollama":
        return ((obj.get("message") or {}).get("content")) or ""
    choices = obj.get("choices") or []
    if choices and isinstance(choices[0], dict):
        return ((choices[0].get("message") or {}).get("content")) or ""
    return ""


def _iter_frames(resp, state: dict | None = None):
    """逐行读取 SSE（`data: {...}`）或 NDJSON（每行一个 JSON）流。

    只处理 `data:` 前缀行；`event:` / `id:` / 注释行直接跳过。

    `state["done"]` 会被置为 True 表示「看见了结束标记」。这个信息必须显式带出来：
    上游被掐断时连接同样是「正常 EOF」，光看循环有没有抛异常分不清
    「答完了」和「说到一半断了」。
    """
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            line = line[5:].strip()
        if not line or line == "[DONE]":
            if line == "[DONE]":
                if state is not None:
                    state["done"] = True
                return
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # 脏帧不该毁掉整次测量
        if isinstance(obj, dict):
            yield obj


def _finish_reason_of(obj: dict) -> str:
    """取本帧的「结束原因」。非空即表示模型正常收尾（stop / length / tool_calls）。

    这是判断「流有没有走完」的第二路证据：OpenAI 方言在最后一帧给
    `choices[0].finish_reason`，Anthropic 给 `stop_reason`，Ollama 给 `done`。
    中转站在把上游掐断时，这三种标记一个都不会出现——正文看着有，流其实是断的。
    """
    choices = obj.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        reason = choices[0].get("finish_reason")
        if reason:
            return str(reason)
    for key in ("stop_reason", "finishReason"):
        if obj.get(key):
            return str(obj[key])
    if obj.get("done") is True:
        return "done"
    return ""


class SpeedTooSlow(Exception):
    """首字延迟超过 SPEED_TTFT_LIMIT_MS。

    这不是「出错」，而是「测了也不可用」：生产场景等不起十几秒的首字，
    所以到此为止，不再消耗后续轮次与 token。界面把它显示为「-」。
    """

    def __init__(self, elapsed_ms: int, limit_ms: int) -> None:
        self.elapsed_ms = elapsed_ms
        self.limit_ms = limit_ms
        super().__init__(f"首字延迟超过 {limit_label(limit_ms)}上限，已放弃")


def limit_label(ms: int) -> str:
    """把毫秒门槛说成人话：整秒用「10 秒」，不足 1 秒保留一位小数。"""
    if ms >= 1000:
        if ms % 1000 == 0:
            return f"{ms // 1000} 秒"
        return f"{ms / 1000:.1f} 秒"
    return f"{ms} 毫秒"


def _set_stream_timeout(resp, seconds: float) -> None:
    """把已建立连接后的读超时改成 seconds，用于「首字窗口」内的精确掐断。

    urllib 的 timeout 只在建连时生效，之后读 socket 仍沿用该值；
    首字到达后我们希望按剩余总时长继续读，所以要能中途改。
    拿不到底层 socket 时静默跳过——这只影响掐断的精度，不影响正确性。
    """
    try:
        sock = resp.fp.raw._sock  # http.client.HTTPResponse -> BufferedReader -> SocketIO
        sock.settimeout(max(seconds, 0.05))
    except Exception:  # noqa: BLE001 - 实现细节，取不到就算了
        pass


def _burst_gap(arrivals: list[float]) -> float:
    """估算中转站的冲刷周期：帧间隔里那些「明显不是同批」的间隔的中位数。

    真流式（一帧一包）时多数间隔只有几毫秒，中位数自然很小，修正可忽略；
    攒批转发时会出现一批 ~100ms 的间隔，中位数就是那一批的节奏。
    没有可用的间隔（只有一帧）时返回 0，不做修正。
    """
    gaps = [b - a for a, b in zip(arrivals, arrivals[1:]) if b - a > SPEED_BURST_GAP_S]
    return statistics.median(gaps) if gaps else 0.0


def _stream_once(
    opener,
    protocol: str,
    url: str,
    headers: dict[str, str],
    payload: dict,
    timeout: int,
    ttft_limit_ms: int = SPEED_TTFT_LIMIT_MS,
) -> dict:
    """跑一轮流式请求，返回这一轮的原始计时样本。

    计时口径：
      * t0 = 发请求前
      * t_first = 首个**含正文**帧到达（首字延迟的终点）
      * t_last = 末个含正文帧到达
      * t_end = 流结束（总耗时）
    速率 = token 数 / (t_last - t_first + 冲刷周期)，见 SPEED_BURST_GAP_S。

    首字掐断：连接与首帧等待共用一个 `ttft_limit_ms` 窗口。窗口内没等到正文
    （含连接就卡住/服务器排队/纯推理模型长时间不出字）就抛 SpeedTooSlow，
    不再白等到 25 秒 socket 超时。
    """
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    limit_s = max(ttft_limit_ms / 1000.0, 0.5)
    t0 = time.perf_counter()
    text_len = 0
    parts: list[str] = []           # 正文全文（限量），供内容校验用
    chunks = 0
    usage: int | None = None
    reasoning: int | None = None
    first_at: float | None = None
    last_at: float | None = None
    first_any_at: float | None = None   # 首个**任何产出**帧（含推理）的到达时刻
    reasoning_chars = 0                 # 只看过推理、没看过正文时用它判断「确实在产出」
    arrivals: list[float] = []      # 每个含正文帧的到达时刻，用来估算冲刷周期
    stream_state: dict = {}         # 由 _iter_frames 置 done=True：看见过结束标记
    finished = False                # 看见过 finish_reason / stop_reason / done

    try:
        resp_cm = opener.open(req, timeout=limit_s)
    except (TimeoutError, socket.timeout) as e:
        # 连建连都超过首字窗口：这个链路等不起，按「太慢」处理而不是报错
        raise SpeedTooSlow(round((time.perf_counter() - t0) * 1000), ttft_limit_ms) from e

    with resp_cm as resp:
        try:
            for obj in _iter_frames(resp, stream_state):
                if _finish_reason_of(obj):
                    finished = True
                text, seen_usage = _extract_delta(protocol, obj)
                if seen_usage is not None:
                    usage = seen_usage
                seen_reasoning = _reasoning_tokens_of(obj)
                if seen_reasoning is not None:
                    reasoning = seen_reasoning
                think = _reasoning_delta(protocol, obj)
                if think:
                    if first_any_at is None:
                        first_any_at = time.perf_counter()
                    reasoning_chars += len(think)
                if not text:
                    now = time.perf_counter()
                    if now - t0 > limit_s:
                        raise SpeedTooSlow(round((now - t0) * 1000), ttft_limit_ms)
                    continue
                now = time.perf_counter()
                if first_at is None:
                    first_at = now
                    # 首字已到：后续读取按正常总超时，让生成阶段有充分时间
                    _set_stream_timeout(resp, timeout)
                elif now - t0 > limit_s + timeout:
                    # 兜底：生成阶段整体也设一个上限，避免个别端点无限吐字
                    break
                last_at = now
                arrivals.append(now)
                chunks += 1
                text_len += len(text)
                if len(parts) < 400:          # 只留够校验的量，别为大输出吃内存
                    parts.append(text)
        except (TimeoutError, socket.timeout) as e:
            elapsed = round((time.perf_counter() - t0) * 1000)
            if first_at is None:
                raise SpeedTooSlow(elapsed, ttft_limit_ms) from e
            raise ValueError(f"生成阶段读取超时（已收到 {chunks} 帧）") from e
        t_end = time.perf_counter()

    if text_len and first_any_at is None:
        first_any_at = first_at

    if first_at is None or last_at is None:
        # 没有正文，但有推理增量：这不是失败，是「输出预算被推理吃光了」。
        # 端点确实在产出，只是没轮到正文——按可用样本返回，首字用推理首帧计时。
        if first_any_at is not None and (reasoning_chars or (reasoning or 0) > 0):
            return {
                "ttft_ms": round((first_any_at - t0) * 1000),
                "total_ms": round((t_end - t0) * 1000),
                "window_s": max(t_end - first_any_at, 1e-6),
                "span_s": max(t_end - first_any_at, 1e-6),
                "flush_s": 0.0,
                "chunks": 0,
                "chars": 0,
                "content": "",
                "usage": usage,
                "reasoning": reasoning if reasoning is not None else reasoning_chars,
                "reasoning_only": True,
                "complete": bool(finished or stream_state.get("done")),
            }
        raise ValueError("流式响应里没有任何增量（空响应，或端点没按 SSE 返回）")
    if first_at - t0 > limit_s:
        raise SpeedTooSlow(round((first_at - t0) * 1000), ttft_limit_ms)

    # 速率分母：解码区间 + 一个冲刷周期。下限 1ms 防止同帧到达导致的除零放大。
    # 加冲刷周期的道理见 SPEED_BURST_GAP_S：第一批 token 是在首帧到达之前产出的，
    # 它们计入了分子，却没有对应的分母时长。
    flush = _burst_gap(arrivals)
    window = max(last_at - first_at + flush, 0.001)
    return {
        "ttft_ms": round((first_at - t0) * 1000),
        "total_ms": round((t_end - t0) * 1000),
        "window_s": window,
        "span_s": max(last_at - first_at, 0.0005),
        "flush_s": flush,
        "chunks": chunks,
        "chars": text_len,
        "content": "".join(parts),
        "usage": usage,
        "reasoning": reasoning,
        "complete": bool(finished or stream_state.get("done")),
    }


def _buffered_once(
    opener,
    protocol: str,
    url: str,
    headers: dict[str, str],
    payload: dict,
    timeout: int,
) -> dict:
    """降级路径：端点不支持流式时，退化为整包计时。

    这样至少能拿到总耗时与 token 数，也就还能算平均生成速率；
    但首字延迟无法与「等模型想完」区分开，因此标 partial=True 且不出 ttft。
    """
    body = dict(payload)
    body.pop("stream", None)
    body.pop("stream_options", None)
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    t0 = time.perf_counter()
    limit_s = SPEED_BUFFERED_TOTAL_S
    with opener.open(req, timeout=timeout) as resp:
        try:
            # 整包计时的总上限：没有这条，缓慢滴流的端点能让单轮无限拖长
            raw = _read_bounded(resp, deadline=t0 + limit_s)
        except TimeoutError as e:
            raise ValueError(
                f"非流式请求整体超过 {limit_s} 秒仍未结束，已放弃"
                f"（已等待 {time.perf_counter() - t0:.1f} 秒）"
            ) from e
    t_end = time.perf_counter()
    obj = json.loads(raw)
    text = _extract_full(protocol, obj) if isinstance(obj, dict) else ""
    usage = (obj.get("usage") or {}) if isinstance(obj, dict) else {}
    tokens = _positive_int(usage.get("completion_tokens"), usage.get("output_tokens"),
                           usage.get("eval_count"))
    if isinstance(obj, dict):  # gemini 的用量在另一个字段
        tokens = tokens or _positive_int((obj.get("usageMetadata") or {}).get("candidatesTokenCount"))
    if not text and tokens is None:
        raise ValueError("非流式响应里既没有正文也没有 token 用量")
    return {
        "ttft_ms": None,
        "total_ms": round((t_end - t0) * 1000),
        "window_s": t_end - t0,
        "chunks": 0,
        "chars": len(text),
        "content": text,
        "usage": tokens,
        "reasoning": _reasoning_tokens_of(obj) if isinstance(obj, dict) else None,
        # 非流式是「整包到手」，没有中途断流这回事（断了就是异常，走不到这里）
        "complete": True,
    }


_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def _plain(text: str, limit: int = 160) -> str:
    """错误正文转人话：去掉标签、压成一行、截断。

    中转站回源失败时 nginx 直接吐一整个 HTML 错误页，原样塞进状态栏会把界面
    撑开好几行，用户看到的全是 `</html>`——真正的原因（502）反而看不见。
    """
    raw = (text or "").strip()
    if "<" in raw and ">" in raw:
        found = _TITLE_RE.search(raw)
        raw = found.group(1) if found else _TAG_RE.sub(" ", raw)
    return re.sub(r"\s+", " ", raw).strip()[:limit]


# 「HTTP 200 且能出字」不等于「这家在提供真服务」。上游失败时中转站常把
# 错误提示当正文吐回来（站点日志里的「上游服务器返回了无效响应或响应格式不可用，
# 请重试」就是这种），或者干脆给一段空正文。这些词用来识别前一种情况。
_ERROR_WORDS = (
    "无效响应", "无效的响应", "响应格式不可用", "响应格式错误", "请重试",
    "请求失败", "上游服务器", "上游错误", "无可用渠道", "渠道不可用",
    "upstream error", "upstream connect", "bad gateway", "service unavailable",
    "rate limit", "too many requests", "invalid request", "insufficient quota",
    "model not found",
)


def looks_like_error(text: str) -> bool:
    """正文看起来像错误提示，而不是模型正常回答。"""
    low = (text or "").lower()
    return any(word in low for word in _ERROR_WORDS)


def check_content(content: str, expect: str = "") -> str:
    """校验正文是否「真的是一次正常回答」。通过返回空串，否则返回问题描述。

    `expect` 给了就比对：小请求（「只回复可用」）能拿到预期词，才说明这条链路
    连最简单的对话都走得通。空正文与错误提示都要拦下——只看「有没有增量」会把
    假可用的商家判成通过。
    """
    text = (content or "").strip()
    if not text:
        return "返回了空正文（端点通了，但没有任何内容）"
    if looks_like_error(text):
        return f"模型把错误信息当正文返回：{_plain(text, 80)}"
    if expect and expect.lower() not in text.lower():
        return f"返回内容与预期不符，期望「{expect}」：{_plain(text, 60)}"
    return ""


def _speed_error_of(status: int, detail: str) -> str:
    body = _plain(detail, 150)
    if status in (401, 403):
        return f"凭据被拒（HTTP {status}），检查 API Key。{body}"
    if status == 404:
        return f"对话端点不存在（HTTP 404），检查 baseURL 是否为对话接口同层路径。{body}"
    if status == 429:
        return f"被限流（HTTP 429），稍后重试或降低并发。{body}"
    if status == 400:
        return f"请求被拒（HTTP 400）：{body}"
    if status in (402,):
        return f"余额或配额不足（HTTP 402）：{body}"
    if status in (500, 502, 503, 504):
        # 「商家侧没有可用上游账号」是中转站市场里最常见的 502：站点本身好好的，
        # 只是这家商户此刻的上游账号额度/并发被榨干（站点日志里 error_code 为
        # `no_healthy_account`、reason 为 `upstream_stream_interrupted`，耗时约 10 秒、
        # 0 token）。它跟令牌、模型、路径都无关，**换一家**才有用，等是等不来的。
        low = body.lower()
        if ("no_healthy_account" in low or "no healthy account" in low
                or "上游服务器" in body or "无可用" in body):
            return (f"商家侧此刻没有可用上游账号（HTTP {status}，no_healthy_account）："
                    f"这家暂时不可用，换一家。{body}")
        # 网关类错误与请求本身无关：令牌、模型、参数都是对的，是站点后端此刻不可用。
        # 正文若只是状态短语（「502 Bad Gateway」）就并进这句话，别重复一遍。
        tail = re.sub(r"^\d{3}\s*", "", body).strip().lower()
        extra = "" if tail in ("bad gateway", "service unavailable",
                               "gateway timeout", "internal server error") else body
        head = f"站点网关故障（HTTP {status}），与令牌和模型无关，稍等重试。"
        return head + (f"（{extra}）" if extra else "")
    return f"HTTP {status}：{body}"


def probe_speed(
    protocol: str,
    base_url: str,
    api_key: str,
    model: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    use_proxy: bool = False,
    rounds: int = SPEED_DEFAULT_ROUNDS,
    max_tokens: int = SPEED_MAX_TOKENS,
    prompt: str = SPEED_PROMPT,
    auto_v1: bool = True,
    ttft_limit_ms: int = SPEED_TTFT_LIMIT_MS,
    expect: str = "",
    force_buffered: bool = False,
    min_chars: int = 0,
    system: str = "",
    tools: list | None = None,
    reasoning_effort: str = "",
) -> SpeedResult:
    """测一个模型的首字延迟与生成速率。永不抛异常，失败信息放在 result.error。

    `expect` 非空时额外做**内容校验**：正文必须包含该词且不像错误提示，否则判失败。
    智能路由用它区分「真的能对话」与「HTTP 200 但吐错误文本 / 空正文」。

    `expect` 或 `min_chars` 不为空时还会做**完整性校验**：流必须走到结束标记
    （`[DONE]` / `finish_reason`）。中转站把上游掐断时，客户端会先收到半截正文、
    然后连接正常 EOF——不检查这一条，半截回答会被当成「可用」，而真实调用报 502。

    多轮取**最快一轮**：最小值代表该链路在当前条件下的能力上限，
    均值会被偶发拥塞拉高，反而低估了模型真实速度。

    端点按 `chat_candidates()` 逐个尝试，只在路径类错误（404/405）时换下一个，
    与模型列表探测的 `_candidate_urls` 行为保持一致。

    **首字闸门**：任何一轮的首字超过 `ttft_limit_ms`（默认 10 秒）立即终止整次
    测量——第 2 轮不可能因为「再试一次」而变快，而生产场景里十几秒首字的模型
    根本不可用。此时返回 `too_slow=True` 且 `ttft_ms=None`，界面显示「-」。
    """
    result = SpeedResult(model=model)
    model = (model or "").strip()
    if not model:
        result.error = "模型 ID 为空。"
        return result
    cands = chat_candidates(protocol, base_url, auto_v1)
    if not cands:
        result.error = "请先填写 API 地址（baseURL）。"
        return result
    cands = [u.replace("{model}", model) for u in cands]
    result.endpoint = cands[0]

    headers = _speed_headers(protocol, api_key)
    proxy_handler = urllib.request.ProxyHandler() if use_proxy else urllib.request.ProxyHandler({})
    opener = urllib.request.build_opener(proxy_handler)

    # `stream_options` 是 OpenAI 方言；不支持时端点会 400，去掉重试一次。
    include_usage = protocol in ("openai", "ollama")
    attempts = max(1, rounds)
    last_error = ""
    # "buffered" 表示该端点已确定不支持流式，后续轮次直接走整包。
    # force_buffered 用来**主动**走整包：智能路由拿它验证另一种请求形态，
    # 中转站经常是流式正常、非流式挂掉（或反过来）。
    mode = "buffered" if force_buffered else "stream"
    ci = 0            # 当前候选端点下标
    done = 0          # 已成功采集的轮数
    # 只放大一次输出上限：无限放大等于替用户悄悄花钱
    escalated = False

    while ci < len(cands) and done < attempts:
        endpoint = cands[ci]
        payload = _speed_payload(protocol, model, prompt=prompt,
                                 max_tokens=max_tokens, include_usage=include_usage,
                                 system=system, tools=tools,
                                 reasoning_effort=reasoning_effort)
        try:
            if mode == "buffered":
                sample = _buffered_once(opener, protocol, endpoint, headers, payload, timeout)
            else:
                sample = _stream_once(opener, protocol, endpoint, headers, payload, timeout,
                                      ttft_limit_ms=ttft_limit_ms)
        except SpeedTooSlow as e:
            # 首字超时：整个模型就此放弃，不重试、不再跑后面的轮次
            result.too_slow = True
            result.ttft_ms = None
            result.tps = None
            result.partial = False
            result.warnings.append(
                f"首字延迟超过 {limit_label(e.limit_ms)}上限，已中止测量并显示「-」"
                f"（延迟过长，不适用于生产制作）。"
            )
            result.endpoint = endpoint
            return result
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read(400).decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            if e.code in (404, 405):
                # 路径不对：换下一个候选端点（通常是补 /v1），不计入轮数
                last_error = f"{endpoint} 返回 {e.code}：对话端点不存在，或路径不对（可试 /v1）。"
                ci += 1
                mode = "stream"
                continue
            if e.code == 400 and mode == "stream" and include_usage \
                    and "stream_option" in detail.lower():
                # 不认 stream_options：去掉该字段，同一端点重试
                include_usage = False
                result.warnings.append("端点不接受 stream_options.include_usage，改为按增量估算 token。")
                continue
            if e.code == 400 and mode == "stream":
                # 该端点大概率不支持流式（部分反代 / 老网关）：同一端点改走整包计时
                mode = "buffered"
                result.warnings.append(
                    f"端点拒绝了流式请求（HTTP 400），已降级为整包计时：只出总耗时与平均速率，"
                    f"首字延迟不可拆分。原始信息：{detail[:120]}"
                )
                continue
            # 401 / 403 / 429 / 402 等：端点路径是对的，换 /v1 没有意义，直接结束
            last_error = _speed_error_of(e.code, detail)
            break
        except urllib.error.URLError as e:
            last_error = f"无法连接 {endpoint}：{e.reason}（国内直连不通时勾选「走系统代理」）"
            break
        except ValueError as e:
            # 拿到了响应但没有可用正文（空响应 / 纯推理模型）
            last_error = str(e)
            break
        except Exception as e:  # noqa: BLE001
            last_error = f"测速请求失败：{e}"
            break

        if sample.get("reasoning_only"):
            # 这一轮有推理、没正文：先试着把输出上限放大一次——推理模型要留出
            # 正文预算才量得到生成速度，16 个 token 连一句话都轮不上。
            if not escalated and max_tokens < SPEED_REASONING_MIN_TOKENS:
                escalated = True
                bigger = max(max_tokens * 12, SPEED_REASONING_MIN_TOKENS)
                result.warnings.append(
                    f"输出上限 {max_tokens} 个 token 全被推理占用、没有一个正文，"
                    f"已自动改用 {bigger} 再测一轮（推理模型需要更多预算才会产出正文）。")
                max_tokens = bigger
                continue
            result.warnings.append(
                "该模型只产出推理、没有正文：首字按推理首帧计时，生成速率测不到。"
                f"要测速率请把「输出上限」调到 {SPEED_REASONING_MIN_TOKENS} 以上。")
            sample["chunks"] = 0

        if sample["ttft_ms"] is None:
            result.partial = True
        result.endpoint = endpoint
        done += 1
        # 窗口内真正吐出的正文 token：扣掉首字之前就已产出的推理 token。
        # 端点没回 usage 时退化用帧数（1 帧≈1 token，常见实现如此）。
        tokens = sample["usage"]
        used = sample["chunks"]
        approx = False
        if tokens is None:
            approx = True
        else:
            excluded = min(sample.get("reasoning") or 0, tokens)
            used = tokens - excluded
            if used <= 0:      # 全是推理、正文为空：只能按帧算
                used, approx = sample["chunks"], True
            elif excluded:
                result.reasoning_tokens += excluded
        sample["text_tokens"] = used
        sample["approx"] = approx
        sample["tps"] = round(used / sample["window_s"], 1) if sample["window_s"] > 0 else None
        result.samples.append({
            "ttftMs": sample["ttft_ms"],
            "totalMs": sample["total_ms"],
            "windowMs": round(sample["window_s"] * 1000),
            "spanMs": round(sample.get("span_s", sample["window_s"]) * 1000),
            "flushMs": round(sample.get("flush_s", 0.0) * 1000),
            "chunks": sample["chunks"],
            "tokens": sample["usage"],
            "textTokens": used,
            "tps": sample["tps"],
            "approx": approx,
            "content": sample.get("content") or "",
            "complete": bool(sample.get("complete", True)),
        })

    if not result.samples:
        result.error = last_error or "测速失败：没有取到任何样本。"
        return result

    result.rounds = len(result.samples)
    # 首字取**最快**一轮：延迟是「能不能用」的门槛，最优值才有决策意义。
    with_ttft = [s for s in result.samples if s["ttftMs"] is not None]
    head = (min(with_ttft, key=lambda s: s["ttftMs"]) if with_ttft
            else min(result.samples, key=lambda s: s["totalMs"] or 0))
    result.ttft_ms = head["ttftMs"]
    result.total_ms = head["totalMs"]
    result.content = str(head.get("content") or "")[:500]
    # 端点不回传 usage 时退回按帧估算值，界面照旧标「≈」。
    result.usage_tokens = head["tokens"] if head["tokens"] is not None else head["textTokens"]

    # 速率用**汇总口径**：Σ正文 token ÷ Σ解码窗口。
    # 不用单轮、也不用「最快一轮」：每个样本窗口只有一两百毫秒，而中转站一次
    # 冲刷就是几十毫秒，单轮速率因此能在几倍范围内跳；把各轮窗口接起来相当于
    # 免费延长观测时间，误差按 1/轮数 收敛。
    usable = [s for s in result.samples if s["textTokens"] > 0 and s["windowMs"] > 0]
    total_tokens = sum(s["textTokens"] for s in usable)
    total_window = sum(s["windowMs"] for s in usable) / 1000.0
    result.text_tokens = total_tokens
    result.window_ms = round(total_window * 1000)
    if total_tokens and total_window > 0:
        result.tps = round(total_tokens / total_window, 1)
        result.reliable = (total_window >= SPEED_MIN_WINDOW_S
                           and total_tokens >= SPEED_MIN_TOKENS)
        if not result.reliable:
            result.warnings.append(
                f"样本偏短（合计 {total_window:.2f} 秒 / {total_tokens} 个正文 token），"
                f"速率主要反映端点冲刷节奏，仅供参考——调大「输出上限」或「轮数」可提高可信度。"
            )
    if any(s["approx"] for s in usable):
        result.approx = True
        result.warnings.append("token 数为按增量帧估算（端点未回传 usage），仅供横向比较。")
    if result.reasoning_tokens:
        result.warnings.append(
            f"已扣除 {result.reasoning_tokens} 个隐藏推理 token：它们在首字之前产出，"
            f"已经算进首字延迟，不能重复计入生成速率。"
        )
    if result.ttft_ms is None and result.tps is None and not result.too_slow:
        result.error = last_error or "测速失败：样本里没有可用的指标。"
        return result

    # 内容校验放在最后：只有「拿到了样本」才谈得上内容对不对。
    # 纯推理样本没有正文，用它自己的说明就够了，不再叠一条「空正文」。
    problem = check_content(result.content, expect)
    if not problem and (expect or min_chars) and not head.get("complete", True):
        # 半截正文 + 没有结束标记：上游被掐断。真实客户端（流式）此时会收到
        # 「上游服务器返回了无效响应」的 502，而我们这边看起来「有字」——
        # 这正是「路由判可用、实际不可用」最隐蔽的一种。
        problem = (f"流式中途断开（收到 {result.text_tokens or 0} 个正文 token 后没有结束标记），"
                   f"真实调用会报 502 / 上游响应无效")
    if not problem and min_chars and len(result.content.strip()) < min_chars:
        problem = (f"正文只有 {len(result.content.strip())} 个字符，"
                   f"短于要求的 {min_chars}（多半是中途断了）")
    if problem and not (expect == "" and result.reasoning_tokens):
        if expect or min_chars:
            result.error = problem
        else:
            result.warnings.append(problem)
    return result


def probe_speed_many(
    protocol: str,
    base_url: str,
    api_key: str,
    models: list[str],
    *,
    timeout: int = DEFAULT_TIMEOUT,
    use_proxy: bool = False,
    rounds: int = SPEED_DEFAULT_ROUNDS,
    max_tokens: int = SPEED_MAX_TOKENS,
    prompt: str = SPEED_PROMPT,
    auto_v1: bool = True,
    workers: int = SPEED_DEFAULT_WORKERS,
    ttft_limit_ms: int = SPEED_TTFT_LIMIT_MS,
    on_result=None,
    on_start=None,
    should_stop=None,
) -> list[SpeedResult]:
    """并行测多个模型，按**完成顺序**回调 `on_result(result, done, total)`。

    为什么要并行：串行测 N 个模型的总耗时 ≈ N × (首字 + 生成)，而单次测量里
    绝大部分时间是**等网络**（等排队、等首字），CPU 与带宽大量空闲。
    并发只会在真正抢带宽时才互相影响；把并发压在 8 路，对首字延迟的污染
    在实测中远小于「串行等到天荒地老」的代价。

    并发数的取舍：`workers` 默认为 8，上限 16。设成 1 就是原来的串行行为。
    `on_result` 在**调用线程**（通常是工作线程）里被调用，GUI 需自行转投队列。

    `on_start(mid)` 在某个模型**真正被线程取走那一刻**触发，用于把界面上的
    「待测试」翻成「测试中」。它只能在线程池里发：提交时机 ≠ 开始时机，
    8 路并发时排在后面的模型会等前面的测完，提前标「测试中」是假象。

    `should_stop` 是无参可调用对象，返回真时不再启动新任务（用于「取消」）。
    """
    ids = [m.strip() for m in models if (m or "").strip()]
    total = len(ids)
    out: list[SpeedResult] = []
    if not ids:
        return out

    workers = max(1, min(int(workers or 1), SPEED_MAX_WORKERS, total))

    def one(mid: str) -> SpeedResult:
        if on_start is not None:
            try:
                on_start(mid)
            except Exception:  # noqa: BLE001 - 通知界面失败不该毁掉测量
                pass
        try:
            return probe_speed(protocol, base_url, api_key, mid, timeout=timeout,
                               use_proxy=use_proxy, rounds=rounds, max_tokens=max_tokens,
                               prompt=prompt, auto_v1=auto_v1, ttft_limit_ms=ttft_limit_ms)
        except Exception as e:  # noqa: BLE001 - 单个模型失败不该拖垮整批
            return SpeedResult(model=mid, error=f"内部错误：{e}")

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="speed") as pool:
        futures = {}
        for mid in ids:
            if should_stop is not None and should_stop():
                break
            futures[pool.submit(one, mid)] = mid
        done = 0
        for fut in as_completed(futures):
            result = fut.result()
            out.append(result)
            done += 1
            if on_result is not None:
                on_result(result, done, total)
    return out


def main() -> None:
    """兼容入口：`python -m modelkeeper.model_probe` 仍能启动界面。

    打包请走 `main.py`——PyInstaller 看不到这里的延迟导入。
    """
    from .gui_app import run

    run()


if __name__ == "__main__":
    main()
