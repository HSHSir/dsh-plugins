#!/usr/bin/env python3
"""通用 JSON 请求自测：网关 5xx 重试、错误正文转人话、gzip、非 JSON 兜底。

重点覆盖「站点整站 502」那次事故暴露的两个问题：
  1. 502 是瞬时的，重打一次往往就通——此前一次就抛，用户只能干瞪眼；
  2. nginx 的 HTML 错误页被原样塞进状态栏，界面被撑成好几行天书。
"""

from __future__ import annotations
# 直接运行本文件时，把项目根目录加入 sys.path（无需安装成包）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gzip
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from modelkeeper import http_json as hj

HTML_502 = (b"<html>\r\n<head><title>502 Bad Gateway</title></head>\r\n<body>\r\n"
            b"<center><h1>502 Bad Gateway</h1></center>\r\n"
            b"<hr><center>nginx/1.28.3</center>\r\n</body>\r\n</html>\r\n")


class check:  # noqa: N801 - 与其它自测脚本保持同一种调用样式
    failed = 0

    def __init__(self, name: str, ok: object, detail: object = "") -> None:
        if ok:
            print(f"PASS  {name}")
        else:
            check.failed += 1
            print(f"FAIL  {name}  -> {detail}")


class Handler(BaseHTTPRequestHandler):
    """按路径决定行为；每个路径记录被打了多少次，用来断言重试次数。"""

    hits: dict[str, int] = {}

    def log_message(self, *args) -> None:   # 静音：测试输出只留断言
        pass

    def _send(self, status: int, body: bytes, ctype: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的约定
        path = self.path.split("?")[0]
        Handler.hits[path] = Handler.hits.get(path, 0) + 1
        n = Handler.hits[path]

        if path == "/flaky":                     # 前两次 502，第三次成功
            if n <= 2:
                return self._send(502, HTML_502, "text/html")
            return self._send(200, b'{"ok": true, "attempt": 3}', "application/json")
        if path == "/down":                      # 永远 502
            return self._send(502, HTML_502, "text/html")
        if path == "/unauth":                    # 401 不该重试
            return self._send(401, b'{"error": "invalid token"}', "application/json")
        if path == "/html500":
            return self._send(500, b"<html><head><title>Internal Server Error</title>"
                                   b"</head><body>boom</body></html>", "text/html")
        if path in ("/gzip", "/gzip-nohdr"):
            raw = json.dumps({"data": [1, 2, 3], "pad": "x" * 5000}).encode()
            # /gzip-nohdr 故意不声明 Content-Encoding：有网关压了却漏标头
            extra = {"Content-Encoding": "gzip"} if path == "/gzip" else None
            return self._send(200, gzip.compress(raw), "application/json", extra)
        if path == "/notjson":
            return self._send(200, b"<html>hello</html>", "text/html")
        return self._send(404, b'{"error": "no such path"}', "application/json")


def main() -> None:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"

    # ---- 网关 502 会被重试，第三次成功 ----
    out = hj.request_json(f"{base}/flaky", timeout=5)
    check("502 后重试成功", out == {"ok": True, "attempt": 3}, out)
    check("重试了两次（共 3 次请求）", Handler.hits.get("/flaky") == 3,
          Handler.hits.get("/flaky"))

    # ---- 一直 502：报错要是人话，且不含 HTML ----
    try:
        hj.request_json(f"{base}/down", timeout=5)
        check("持续 502 应抛错", False, "没抛")
    except hj.HttpError as e:
        message = str(e)
        check("状态码为 502", e.status == 502, e.status)
        check("消息说明是网关故障", "网关" in message, message)
        check("消息不含 HTML 标签", "<" not in message and ">" not in message, message)
        check("消息不含换行（不撑爆状态栏）", "\n" not in message and "\r" not in message,
              repr(message))
        check("网关错误标记可重试", e.retryable, message)
    check("持续 502 也重试了 3 次", Handler.hits.get("/down") == 3,
          Handler.hits.get("/down"))

    # ---- 4xx 不重试：参数错了重打一百次也还是错 ----
    try:
        hj.request_json(f"{base}/unauth", timeout=5)
        check("401 应抛错", False, "没抛")
    except hj.HttpError as e:
        check("401 说明是令牌问题", "令牌" in str(e), str(e))
        check("401 不可重试", not e.retryable, str(e))
    check("401 只请求一次", Handler.hits.get("/unauth") == 1, Handler.hits.get("/unauth"))

    # ---- 500 也转人话 ----
    try:
        hj.request_json(f"{base}/html500", timeout=5)
        check("500 应抛错", False, "没抛")
    except hj.HttpError as e:
        check("500 说明是站点内部错误", "内部错误" in str(e), str(e))
        check("500 消息不含标签", "<" not in str(e), str(e))

    # ---- gzip 仍能解（这是本模块存在的理由，别被改坏） ----
    got = hj.request_json(f"{base}/gzip", timeout=5)
    check("gzip 响应能解", got.get("data") == [1, 2, 3] and len(got.get("pad", "")) == 5000,
          str(got)[:80])

    # ---- 压了但没标头的 gzip 也认（按魔数兜） ----
    got2 = hj.request_json(f"{base}/gzip-nohdr", timeout=5)
    check("漏标头的 gzip 也能解", got2.get("data") == [1, 2, 3], str(got2)[:80])

    # ---- 200 但不是 JSON：给一段可诊断的原文，仍是人话 ----
    try:
        hj.request_json(f"{base}/notjson", timeout=5)
        check("非 JSON 应抛错", False, "没抛")
    except hj.NetError as e:
        check("非 JSON 报错带原文", "hello" in str(e), str(e))

    # ---- human_detail 的边界 ----
    check("无标签原样保留", hj.human_detail("  plain   text ") == "plain text")
    check("多行压成一行", hj.human_detail("a\n\nb") == "a b")
    check("超长截断", len(hj.human_detail("x" * 500)) == 160)
    check("空串不炸", hj.human_detail("") == "")
    check("无 title 时剥标签", "boom" in hj.human_detail("<div><b>boom</b></div>"),
          hj.human_detail("<div><b>boom</b></div>"))

    srv.shutdown()
    print()
    print("RESULT:", "ALL PASS" if not check.failed else f"{check.failed} FAILED")


if __name__ == "__main__":
    main()
