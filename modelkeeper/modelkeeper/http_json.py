#!/usr/bin/env python3
"""通用 JSON 请求：gzip 解压 + 大响应通道 + wall-clock 截止。

**为什么另开一条通路**：`model_probe._read_bounded` 是给 `/v1/models` 那种小响应
设计的，硬上限 4 MB。而中转站的渠道列表单模型就有 20 MB 以上（实测
3371 渠道 / 13806 条 listing / 27.8 MB），走那条路会被直接拒绝。这里不放松
原来的上限（它对模型列表仍是合理的保护），只是另开一条明确给大响应用的读取路径。

三个必要的细节：

  * **主动发 Accept-Encoding: gzip**。urllib 默认不发这个头，服务端就回未压缩体。
    实测同一接口 27.8 MB → 1.2 MB，6.1 s → 3.1 s。
  * **urllib 不会自动解压**，必须按 Content-Encoding 自己解（gzip / deflate）。
  * **读取循环带 wall-clock 截止**。socket timeout 在每次成功读取后都会重置，
    服务端缓慢滴流可以突破任意单次超时——这正是账户日志里出现 662 秒长尾的原因。

另外两件事也会让用户一头雾水，顺手在这里收掉：

  * **网关 5xx 直接重试**。中转站的 nginx 回源失败时会吐一个 HTML 错误页（502/503/504），
    隔一两秒再打往往就通了。重试有上限、有退避，且不越过总截止时间。
  * **错误正文转人话**。HTML 原样塞进状态栏会撑成好几行天书，这里只留 `<title>`
    里的一句话，并补一句状态码含义。
"""

from __future__ import annotations

import gzip
import json
import re
import socket
import time
import urllib.error
import urllib.request
import zlib

# 解压后的体积上限。渠道列表实测 28 MB，留一倍余量；再大就该怀疑是异常响应了。
MAX_BYTES = 64 * 1024 * 1024

# 单次请求的总时长上限（秒）。到点即中止，不无限等。
DEFAULT_DEADLINE_S = 120

UA = "model-keeper/1.0"

# 建连阶段按这个顺序退避重试。502/503/504 是网关侧的瞬时故障，
# 与令牌、模型、参数都无关，重打一次的成本远低于让用户去猜。
RETRY_STATUSES = (500, 502, 503, 504)
RETRIES = 3
BACKOFF_S = (0.0, 0.8, 2.0)

# 状态码 → 人话。没列到的状态码不猜。
_STATUS_HINT = {
    400: "请求被拒绝（参数或模型名不对）",
    401: "未授权：访问令牌或用户 ID 不对",
    402: "额度不足",
    403: "被拒绝：该令牌没有这个接口的权限",
    404: "接口不存在（检查控制面地址，或路径少了 /v1）",
    429: "请求过于频繁，被限流",
    500: "站点内部错误",
    502: "站点网关故障（后端没响应），通常是站点侧临时故障，稍等重试",
    503: "站点过载或维护中",
    504: "站点网关超时（后端太慢）",
}

# 这些正文只是状态短语，已经被上面的提示涵盖，没必要再拼一遍
_BARE_PHRASES = {
    "bad gateway", "service unavailable", "gateway timeout",
    "internal server error", "not found", "unauthorized", "forbidden",
}

_TAG_RE = re.compile(r"<[^>]+>")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def human_detail(text: str, limit: int = 160) -> str:
    """把错误正文压成一句能读的话：去标签、压空白、截断。

    nginx 的 502 页面整段塞进状态栏会把界面撑开好几行，用户看到的全是 `</html>`。
    """
    raw = (text or "").strip()
    if "<" in raw and ">" in raw:
        found = _TITLE_RE.search(raw)
        raw = found.group(1) if found else _TAG_RE.sub(" ", raw)
    return re.sub(r"\s+", " ", raw).strip()[:limit]


class HttpError(Exception):
    """HTTP 层错误：状态码 + 服务端详情，消息已经是给人看的一句话。"""

    def __init__(self, status: int, detail: str, url: str = "") -> None:
        self.status = status
        self.detail = human_detail(detail)
        self.url = url
        text = f"HTTP {status}"
        hint = _STATUS_HINT.get(status)
        if hint:
            text += f"：{hint}"
        # hint 为空（没收录的状态码）时不能拿 detail 去 in None。
        # 「502 Bad Gateway」这类正文去掉状态码后就是个状态短语，已经说过了，不重复。
        bare = re.sub(r"^\s*\d{3}\s*", "", self.detail).strip().lower()
        if self.detail and hint and bare not in _BARE_PHRASES and self.detail not in hint:
            text += f"（{self.detail}）"
        super().__init__(text)

    @property
    def retryable(self) -> bool:
        return self.status in RETRY_STATUSES


class NetError(Exception):
    """连接层错误：DNS / TLS / 拒绝连接 / 总时长超限。"""


def _decode_body(raw: bytes, encoding: str) -> bytes:
    """按 Content-Encoding 解压。认不出就原样返回（有些网关会乱标）。

    还有一种情况：包体明明是 gzip（`\x1f\x8b` 魔数）却没给 Content-Encoding。
    按魔数兜一下——不解的话，调用方会拿到一堆二进制并报「返回的不是 JSON」。
    """
    enc = (encoding or "").strip().lower()
    if not enc or enc == "identity":
        if raw[:2] == b"\x1f\x8b":
            try:
                return gzip.decompress(raw)
            except (OSError, EOFError):
                return raw
        return raw
    try:
        if "gzip" in enc:
            return gzip.decompress(raw)
        if "deflate" in enc:
            try:
                return zlib.decompress(raw)
            except zlib.error:
                return zlib.decompress(raw, -zlib.MAX_WBITS)  # 裸 deflate
    except (OSError, EOFError, zlib.error):
        # 解压失败时退回原文：至少让调用方拿到可诊断的错误，而不是一个空响应
        return raw
    return raw


def _error_body(err: urllib.error.HTTPError) -> str:
    """读错误响应正文（限量），拿不到就返回空串——正文只是补充说明。"""
    try:
        raw = _decode_body(err.read(2048), err.headers.get("content-encoding", ""))
    except Exception:  # noqa: BLE001 - 详情拿不到不影响主结论
        return ""
    return raw.decode("utf-8", "replace")


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: object = None,
    timeout: int = 30,
    max_bytes: int = MAX_BYTES,
    deadline_s: float = DEFAULT_DEADLINE_S,
    use_proxy: bool = False,
) -> object:
    """发一个请求并返回解析后的 JSON（dict / list）。

    失败一律抛异常：`HttpError` 带状态码与详情，`NetError` 带原因。
    调用方（工作线程）负责捕获并转成界面消息。
    """
    hdrs = {
        "accept": "application/json",
        "accept-encoding": "gzip, deflate",
        "user-agent": UA,
    }
    if headers:
        hdrs.update({k.lower(): v for k, v in headers.items()})

    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("content-type", "application/json")

    # 直连时显式给空 ProxyHandler，否则 urllib 会读环境里的 HTTP(S)_PROXY
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler() if use_proxy else urllib.request.ProxyHandler({})
    )
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method.upper())

    started = time.perf_counter()
    deadline = started + max(deadline_s, 1.0)
    resp_cm = None
    last_http: HttpError | None = None
    for attempt in range(RETRIES):
        try:
            resp_cm = opener.open(req, timeout=timeout)
            break
        except urllib.error.HTTPError as e:
            err = HttpError(e.code, _error_body(e), url)
            # 网关 5xx 多为瞬时：隔一会儿重打常常就通了。重试上限与总截止都守着，
            # 站点彻底挂了也只是多重试两次（约 3 秒），不会把界面卡死。
            if (err.retryable and attempt + 1 < RETRIES
                    and time.perf_counter() + BACKOFF_S[attempt + 1] < deadline):
                last_http = err
                time.sleep(BACKOFF_S[attempt + 1])
                continue
            raise err from e
        except urllib.error.URLError as e:
            reason = getattr(e, "reason", e)
            # 解析失败常常是一次性的（本地代理/VPN 的 DNS 抖动），重试一次再报错。
            # 但只重试连接类错误：HTTP 状态错误已经在上一个分支抛掉了。
            if isinstance(reason, socket.gaierror) and attempt == 0:
                time.sleep(0.4)
                continue
            hint = "（域名解析失败：检查地址拼写，或勾选「走系统代理」）" \
                if isinstance(reason, socket.gaierror) else ""
            raise NetError(f"无法连接 {url}：{reason}{hint}") from e
        except (TimeoutError, OSError) as e:
            raise NetError(f"请求 {url} 失败：{e}") from e
    if resp_cm is None:
        raise last_http or NetError(f"无法连接 {url}：重试后仍未建立连接")

    with resp_cm as resp:
        encoding = resp.headers.get("content-encoding", "")
        declared = resp.headers.get("content-length")
        # 声明体积已超限时提前拒绝，省得白读一遍
        if declared and declared.isdigit() and int(declared) > max_bytes * 2:
            raise NetError(f"响应声明体积 {int(declared) // 1048576} MB，超过上限")
        chunks: list[bytes] = []
        total = 0
        while True:
            if time.perf_counter() > deadline:
                raise NetError(
                    f"超过总时长上限 {deadline_s:.0f} 秒（已收 {total // 1024} KB），已中止"
                )
            chunk = resp.read(262144)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise NetError(f"响应超过 {max_bytes // 1048576} MB 上限")
            chunks.append(chunk)

    raw = _decode_body(b"".join(chunks), encoding)
    if len(raw) > max_bytes:
        raise NetError(f"解压后超过 {max_bytes // 1048576} MB 上限")
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except ValueError as e:
        raise NetError(f"{url} 返回的不是 JSON：{raw[:200]!r}") from e


def auth_headers(access_token: str, user_id: str | int | None = None) -> dict[str, str]:
    """new-api 控制台接口的两个必备头。

    `New-Api-User` 缺了报 `dashboard_auth_user_id_missing`，错了报
    `does not match logged in user`——两者都不是「令牌无效」，别混为一谈。
    """
    h = {"authorization": f"Bearer {(access_token or '').strip()}"}
    if user_id not in (None, ""):
        h["new-api-user"] = str(user_id)
    return h
