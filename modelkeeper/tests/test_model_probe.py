"""本地 mock 端点自测：验证解析、/v1 回退、错误映射、流式测速。"""
# 直接运行本文件时，把项目根目录加入 sys.path（无需安装成包）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from modelkeeper import model_probe as mp

OPENAI_BODY = {
    "object": "list",
    "data": [
        {"id": "gpt-6.1-sol", "object": "model", "owned_by": "a6api"},
        {"id": "deepseek-v4.1-flash-1017", "name": "DS Flash", "context_length": 131072, "max_tokens": 8192},
        {"id": "glm-5.3-flash", "display_name": "GLM Flash", "context_window": 262144},
        {"object": "model", "owned_by": "x"},                     # 脏数据：无 id，应被跳过
        {"id": "bad-ctx", "context_length": "200000"},            # 字符串容量，应视为未披露
        {"id": "neg-ctx", "context_length": -5},                  # 负数，应视为未披露
    ],
}
ANTHROPIC_BODY = {"data": [{"id": "claude-x", "display_name": "Claude X", "type": "model"}]}
GEMINI_BODY = {"models": [
    {"name": "models/gemini-3-pro", "displayName": "Gemini 3 Pro", "inputTokenLimit": 1048576, "outputTokenLimit": 65536},
]}
OLLAMA_BODY = {"models": [{"name": "qwen3:8b", "details": {"parameter_size": "8B", "quantization_level": "Q4_K_M"}}]}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        p = self.path
        auth = self.headers.get("authorization", "")
        if p == "/openai/models":
            self._send(200, OPENAI_BODY) if auth == "Bearer sk-test" else self._send(401, {"error": "no key"})
        elif p == "/gw/v1/models":                       # 只有带 /v1 的路径存在
            self._send(200, OPENAI_BODY)
        elif p == "/anthropic/models":
            self._send(200, ANTHROPIC_BODY) if self.headers.get("x-api-key") else self._send(401, {})
        elif p == "/v1beta/models":
            self._send(200, GEMINI_BODY)
        elif p == "/api/tags":
            self._send(200, OLLAMA_BODY)
        elif p == "/notjson/models":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"<html>nope</html>")
        else:
            self._send(404, {"error": "not found"})

    # ---- 流式对话端点：用于测速 ----
    def _sse(self, frames, delay=0.02, done=True):
        """按 SSE 逐帧写出，每帧之间留出可测的时间间隔。

        `done=False` 用于模拟「上游被掐断」：正文吐了一半，连接正常关闭，
        既没有 finish_reason 也没有 [DONE]。
        """
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        for frame in frames:
            self.wfile.write(f"data: {json.dumps(frame, ensure_ascii=False)}\n\n".encode())
            self.wfile.flush()
            time.sleep(delay)
        if done:
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    def _read_body(self):
        n = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def do_POST(self):
        body = self._read_body()
        path = self.path
        if path == "/openai/chat/completions":
            if self.headers.get("authorization") != "Bearer sk-test":
                return self._send(401, {"error": "no key"})
            if body.get("stream"):
                # 首帧是无正文的 role 声明，随后 12 帧正文。
                # 首字延迟应从上第一个**正文**帧算起，而不是从 role 帧算起。
                lead = [{"choices": [{"delta": {"role": "assistant"}}]}]
                frames = lead + [
                    {"choices": [{"delta": {"content": f" {i}"}}]} for i in range(12)
                ]
                frames.append({"choices": [{"delta": {}}],
                               "usage": {"completion_tokens": 60, "prompt_tokens": 20}})
                return self._sse(frames)
            return self._send(200, {
                "choices": [{"message": {"content": "1 2 3 4 5"}}],
                "usage": {"completion_tokens": 60},
            })
        if path == "/gw/v1/chat/completions":
            # 只有补了 /v1 的路径存在：验证候选回退
            frames = [{"choices": [{"delta": {"content": " x"}}]} for _ in range(10)]
            frames.append({"choices": [{"delta": {}}], "usage": {"completion_tokens": 40}})
            return self._sse(frames, delay=0.01)
        if path == "/nolist/chat/completions":
            # 端点明确拒绝流式：用来验证降级到整包计时
            if body.get("stream"):
                return self._send(400, {"error": "streaming not supported"})
            return self._send(200, {
                "choices": [{"message": {"content": "1 2 3 " * 40}}],
                "usage": {"completion_tokens": 60},
            })
        if path == "/reasoning/chat/completions":
            # 推理模型：completion_tokens 含 reasoning_tokens。72 - 24 = 48 个正文
            # token 才是解码窗口里真正吐出来的量。
            frames = [{"choices": [{"delta": {"content": f" {i}"}}]} for i in range(12)]
            frames.append({"choices": [{"delta": {}}],
                           "usage": {"completion_tokens": 72,
                                     "completion_tokens_details": {"reasoning_tokens": 24}}})
            return self._sse(frames)
        if path == "/nousage/chat/completions":
            # 不回传 usage：验证按增量帧估算并标 approx
            frames = [{"choices": [{"delta": {"content": f" {i}"}}]} for i in range(20)]
            return self._sse(frames, delay=0.01)
        if path == "/truncate/chat/completions":
            # 上游断流：有正文、没有结束标记
            frames = [{"choices": [{"delta": {"content": "PONG"}}]}]
            frames += [{"choices": [{"delta": {"content": f" {i}"}}]} for i in range(3)]
            return self._sse(frames, delay=0.01, done=False)
        if path == "/limited/chat/completions":
            return self._send(429, {"error": "rate limited"})
        if path == "/thinkonly/chat/completions":
            # 推理模型 + 预算极小：每一帧都只有 reasoning，正文一个都没有。
            # 这种情况端点其实是通的（站内日志 200），不能判成「测速失败」。
            frames = [{"choices": [{"delta": {"reasoning_content": f"思考 {i}"}}]}
                      for i in range(6)]
            frames.append({"choices": [{"delta": {}}],
                           "usage": {"completion_tokens": 16,
                                     "completion_tokens_details": {"reasoning_tokens": 16}}})
            return self._sse(frames, delay=0.01)
        if path == "/thinkfirst/chat/completions":
            # 预算太小 → 只出推理；预算够（≥192）→ 推理之后正常吐正文。
            # 用来验证「自动放大输出上限重测一次」这条路径。
            if int(body.get("max_tokens") or 0) < 192:
                frames = [{"choices": [{"delta": {"reasoning": f"想 {i}"}}]}
                          for i in range(4)]
                frames.append({"choices": [{"delta": {}}],
                               "usage": {"completion_tokens": 16,
                                         "completion_tokens_details": {"reasoning_tokens": 16}}})
                return self._sse(frames, delay=0.01)
            frames = [{"choices": [{"delta": {"reasoning": "先想一下"}}]}]
            frames += [{"choices": [{"delta": {"content": f" {i}"}}]} for i in range(10)]
            frames.append({"choices": [{"delta": {}}],
                           "usage": {"completion_tokens": 40,
                                     "completion_tokens_details": {"reasoning_tokens": 8}}})
            return self._sse(frames, delay=0.01)
        if path == "/errortext/chat/completions":
            # 中转站最常见的假可用：HTTP 200、流式正常、正文却是错误提示。
            frames = [{"choices": [{"delta": {"content": "上游服务器返回了无效响应或响应格式不可用，请重试"}}]}]
            frames.append({"choices": [{"delta": {}}],
                           "usage": {"completion_tokens": 20}})
            return self._sse(frames, delay=0.01)
        if path == "/empty/chat/completions":
            # 只有心跳帧、没有任何正文：应报「没有正文增量」
            return self._sse([{"choices": [{"delta": {"role": "assistant"}}]}], delay=0.01)
        if path == "/slow/chat/completions":
            # 首字极慢：先挂 1.2 秒再吐正文，用 ttft_limit_ms 验证掐断
            time.sleep(1.2)
            frames = [{"choices": [{"delta": {"content": f" {i}"}}]} for i in range(5)]
            frames.append({"choices": [{"delta": {}}], "usage": {"completion_tokens": 30}})
            return self._sse(frames, delay=0.01)
        if path == "/heartbeat-only/chat/completions":
            # 只发心跳帧、永远不出正文：验证首字窗口内被掐断，而不是等 25s socket 超时。
            # 必须在这里真的把帧写出去——写成 `return gen()` 只是返回了生成器对象、
            # 帧一个都没发，服务端等于立刻断流，测出来的就不是首字窗口的效果了。
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for _ in range(60):
                self.wfile.write(b"data: {\"choices\":[{\"delta\":{\"role\":\"assistant\"}}]}\n\n")
                self.wfile.flush()
                time.sleep(0.1)
            return
        self._send(404, {"error": "not found"})


def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -> {detail}" if detail and not cond else ""))
    if not cond:
        check.failed = True


check.failed = False


class QuietServer(ThreadingHTTPServer):
    """多线程 mock 服务端。

    必须多线程：单线程会把并发请求排队，「并行更快」就永远测不出来。
    `handle_error` 压掉 BrokenPipe/ConnectionReset：客户端按首字上限主动掐断连接
    是本测试要验证的行为，服务端这时写失败属于预期，不该刷一屏 traceback 让人
    以为测试炸了。
    """

    def handle_error(self, request, client_address):
        # Windows 上客户端硬断给的是 ConnectionAbortedError(10053)，Linux 上多为
        # BrokenPipe，两者都算「我们自己掐的」，压掉。
        if isinstance(sys.exc_info()[1],
                      (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


# 假端点端口可覆盖：同机常有别的项目的本地服务占着默认端口，撞上就会连到
# 对方去（表现为莫名的 404）。换端口跑：set MODEL_PROBE_TEST_PORT=18765
TEST_PORT = int(os.environ.get("MODEL_PROBE_TEST_PORT", "8765"))
HOST = f"127.0.0.1:{TEST_PORT}"


def _bind_or_explain() -> QuietServer:
    """端口已被别人监听时直接说明，别让后续断言炸成看不懂的 KeyError。

    为什么不能靠 bind 报错：`allow_reuse_address` 在 Windows 上允许**抢绑**
    已被监听的端口，bind 会静默成功，请求却发给原来那个服务——症状是
    「莫名的 404 / KeyError」，看起来像功能回归，实际打错了对方。
    所以先主动连一下，连得上就说明有人在用。
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.3)
    try:
        busy = probe.connect_ex(("127.0.0.1", TEST_PORT)) == 0
    finally:
        probe.close()
    if busy:
        raise SystemExit(
            f"端口 {TEST_PORT} 已被别的程序占用，测试会连到它上面，结果不可信。\n"
            f"换端口重跑：set MODEL_PROBE_TEST_PORT=18765"
        )
    return QuietServer(("127.0.0.1", TEST_PORT), H)


def main():
    srv = _bind_or_explain()
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://" + HOST

    r = mp.fetch_models("openai", f"{base}/openai", "sk-test")
    check("openai 成功", r.ok, r.error)
    check("条数=5（跳过无 id 的脏行）", len(r.models) == 5, [m.id for m in r.models])
    ids = [m.id for m in r.models]
    check("顺序保持", ids == ["gpt-6.1-sol", "deepseek-v4.1-flash-1017", "glm-5.3-flash", "bad-ctx", "neg-ctx"], ids)
    by = {m.id: m for m in r.models}
    check("无容量字段 -> None", by["gpt-6.1-sol"].context is None)
    check("context_length 生效", by["deepseek-v4.1-flash-1017"].context == 131072)
    check("max_tokens 生效", by["deepseek-v4.1-flash-1017"].max_out == 8192)
    check("context_window 生效", by["glm-5.3-flash"].context == 262144)
    check("display_name 落到 name", by["glm-5.3-flash"].name == "GLM Flash")
    check("字符串容量被忽略", by["bad-ctx"].context is None)
    check("负数容量被忽略", by["neg-ctx"].context is None if "neg-ctx" in by else True)
    check("披露计数=2", r.with_context == 2, r.with_context)

    r = mp.fetch_models("openai", f"{base}/gw", "sk-test", auto_v1=True)
    check("/v1 自动回退成功", r.ok and len(r.models) == 5, r.error)
    check("/v1 回退后 endpoint 正确", r.endpoint.endswith("/gw/v1/models"), r.endpoint)

    r = mp.fetch_models("openai", f"{base}/gw", "sk-test", auto_v1=False)
    check("关闭 auto_v1 时失败", not r.ok and "404" in r.error, r.error)

    r = mp.fetch_models("openai", f"{base}/openai", "sk-wrong")
    check("401 提示凭据", not r.ok and "凭据被拒" in r.error, r.error)

    r = mp.fetch_models("openai", f"{base}/notjson", "")
    check("非 JSON 报错", not r.ok and "不是 JSON" in r.error, r.error)

    r = mp.fetch_models("openai", "", "k")
    check("空 baseURL 报错", not r.ok and "baseURL" in r.error, r.error)

    r = mp.fetch_models("anthropic", f"{base}/anthropic", "sk-ant")
    check("anthropic 解析", r.ok and r.models[0].id == "claude-x", r.error)

    r = mp.fetch_models("gemini", f"{base}/v1beta", "g-key")
    check("gemini 解析（去 models/ 前缀）", r.ok and r.models[0].id == "gemini-3-pro", r.error)
    check("gemini 上下文=1M", r.ok and r.models[0].context == 1048576)

    r = mp.fetch_models("ollama", base, "")
    check("ollama 解析", r.ok and r.models[0].id == "qwen3:8b", r.error)
    check("ollama 备注", r.ok and "8B" in r.models[0].note)

    r = mp.fetch_models("openai", "http://127.0.0.1:1/v1", "k")
    check("连接拒绝有提示", not r.ok and "无法连接" in r.error, r.error)

    # ---- 测速：TTFT / TPS ----
    s = mp.probe_speed("openai", f"{base}/openai", "sk-test", "gpt-6-sol", rounds=3)
    check("测速成功", s.ok, s.error)
    check("跑满 3 轮", s.rounds == 3, s.rounds)
    check("拿到首字延迟", s.ttft_ms is not None and 20 < s.ttft_ms < 1200, s.ttft_ms)
    check("拿到生成速率", s.tps is not None and s.tps > 0, s.tps)
    check("token 数取端点 usage", s.usage_tokens == 60 and not s.approx, (s.usage_tokens, s.approx))
    check("非降级样本", not s.partial, s.partial)
    # 12 帧 × 20ms ≈ 220~260ms 解码区间；60 token / 0.24s ≈ 250 tok/s
    check("TPS 量级合理(100~600)", 100 < s.tps < 600, s.tps)
    check("取样有明细", len(s.samples) == 3, s.samples)
    check("summary 含两项指标", "首字" in s.summary() and "tok/s" in s.summary(), s.summary())

    # 速率口径：Σ正文 token ÷ Σ各轮窗口，而不是「首字最快那一轮」的单轮窗口。
    # 每轮 12 帧 × 20ms ≈ 0.22s，3 轮合计 ≥ 0.5s 才算样本够长。
    check("正文 token 为各轮之和", s.text_tokens == 180, s.text_tokens)
    check("分母为各轮窗口之和", s.window_ms >= 500, s.window_ms)
    check("样本够长标可信", s.reliable, s.reliable)
    check("单轮速率不参与汇总", all(x["tps"] for x in s.samples), s.samples)

    # 推理模型的 completion_tokens 含 reasoning_tokens，必须在分子里扣掉：
    # 那些 token 在首字之前产出，已经计入首字延迟，重复计入会把速率放大。
    s_reason = mp.probe_speed("openai", f"{base}/reasoning", "sk-test", "m", rounds=3)
    check("推理 token 从分子扣除", s_reason.text_tokens == (72 - 24) * 3, s_reason.text_tokens)
    check("记录扣掉的推理 token", s_reason.reasoning_tokens == 24 * 3, s_reason.reasoning_tokens)
    check("推理 token 有说明", any("推理 token" in w for w in s_reason.warnings), s_reason.warnings)
    check("速率按正文 token 计算", 150 < s_reason.tps < 400, s_reason.tps)
    check("usage_tokens 仍是端点原值", s_reason.usage_tokens == 72, s_reason.usage_tokens)

    check("识别 OpenAI 推理 token",
          mp._reasoning_tokens_of({"usage": {"completion_tokens_details":
                                             {"reasoning_tokens": 7}}}) == 7)
    check("识别 gemini thoughtsTokenCount",
          mp._reasoning_tokens_of({"usageMetadata": {"thoughtsTokenCount": 9}}) == 9)
    check("无推理字段返回 None",
          mp._reasoning_tokens_of({"usage": {"completion_tokens": 5}}) is None)

    # 只出推理、没有正文：端点其实是通的（站内日志 200），不能判失败。
    s_think = mp.probe_speed("openai", f"{base}/thinkonly", "sk-test", "m", rounds=1,
                             max_tokens=16)
    check("纯推理响应不算失败", s_think.ok, (s_think.error, s_think.warnings))
    check("纯推理也能测到首字", (s_think.ttft_ms or 0) > 0, s_think.ttft_ms)
    check("纯推理没有正文速率", s_think.tps is None, s_think.tps)
    check("纯推理给出解释", any("只产出推理" in w for w in s_think.warnings), s_think.warnings)
    check("纯推理只放大了有限次数",
          sum(1 for w in s_think.warnings if "已自动改用" in w) <= 1, s_think.warnings)

    # 预算太小 → 自动放大输出上限重测一次，于是既拿到首字也拿到速率
    s_esc = mp.probe_speed("openai", f"{base}/thinkfirst", "sk-test", "m", rounds=1,
                           max_tokens=16)
    check("放大预算后测到正文", s_esc.ok and (s_esc.tps or 0) > 0,
          (s_esc.error, s_esc.tps, s_esc.warnings))
    check("放大预算有一次说明", any("已自动改用" in w for w in s_esc.warnings), s_esc.warnings)
    check("放大后的预算写进提示", any("192" in w for w in s_esc.warnings), s_esc.warnings)

    # 推理增量的解析：各家写法不同，都得认出来
    check("openai reasoning_content",
          mp._reasoning_delta("openai", {"choices": [{"delta": {"reasoning_content": "x"}}]}) == "x")
    check("openrouter reasoning",
          mp._reasoning_delta("openai", {"choices": [{"delta": {"reasoning": "y"}}]}) == "y")
    check("anthropic thinking_delta",
          mp._reasoning_delta("anthropic", {"type": "content_block_delta",
                                            "delta": {"type": "thinking_delta",
                                                      "thinking": "z"}}) == "z")
    check("正文增量不算推理",
          mp._reasoning_delta("openai", {"choices": [{"delta": {"content": "正文"}}]}) == "")

    # 内容校验：HTTP 200 + 有正文，也可能是「上游失败被当成回答」。
    # 智能路由靠 expect 区分「真能对话」与「假可用」。
    s_err = mp.probe_speed("openai", f"{base}/errortext", "sk-test", "m", rounds=1,
                           max_tokens=64)
    check("错误正文仍然算测速成功", s_err.ok, (s_err.error, s_err.content))
    check("错误正文会进警告", any("错误信息" in w for w in s_err.warnings), s_err.warnings)
    s_err2 = mp.probe_speed("openai", f"{base}/errortext", "sk-test", "m", rounds=1,
                            max_tokens=64, expect="可用")
    check("带期望词时错误正文判失败", not s_err2.ok and "错误信息" in (s_err2.error or ""),
          s_err2.error)
    s_ok = mp.probe_speed("openai", f"{base}/openai", "sk-test", "m", rounds=1,
                          max_tokens=64, expect="")
    check("正常回答通过校验", s_ok.ok and mp.check_content(s_ok.content) == "",
          (s_ok.content, s_ok.error))

    # 冲刷周期修正：中转站攒一批再冲，第一批 token 在首帧之前就已产出，
    # 计入了分子却没有对应的分母时长，速率因此被放大。
    check("单帧无所谓冲刷周期", mp._burst_gap([1.0]) == 0.0)
    check("同批帧不算冲刷周期", mp._burst_gap([0.0, 0.001, 0.002]) == 0.0)
    arr = [0.0, 0.0, 0.001, 0.101, 0.102, 0.202]
    check("冲刷周期取批间隔中位数", abs(mp._burst_gap(arr) - 0.1) < 1e-9, mp._burst_gap(arr))
    s_win = mp.probe_speed("openai", f"{base}/openai", "sk-test", "m", rounds=1)
    smp = s_win.samples[0]
    check("窗口=跨度+冲刷周期", smp["windowMs"] > smp["spanMs"], smp)

    # 首字延迟必须落在「首帧到达」之后：心跳帧不该被计入
    s_lead = mp.probe_speed("openai", f"{base}/openai", "sk-test", "m", rounds=1)
    check("心跳帧不计入首字", s_lead.ttft_ms is not None and s_lead.ttft_ms > 0, s_lead.ttft_ms)

    # 端点不回传 usage -> 按帧估算
    s2 = mp.probe_speed("openai", f"{base}/nousage", "sk-test", "m", rounds=1)
    check("无 usage 仍成功", s2.ok, s2.error)
    check("无 usage 标 approx", s2.approx and s2.usage_tokens is not None, (s2.approx, s2.usage_tokens))
    check("无 usage 提示估算", any("估算" in w for w in s2.warnings), s2.warnings)
    # 单轮 20 帧 × 10ms ≈ 0.19s、20 token：够长吗？不够——速率主要反映冲刷节奏。
    check("短样本标为不可信", not s2.reliable, (s2.reliable, s2.window_ms, s2.text_tokens))
    check("短样本给出提示", any("样本偏短" in w for w in s2.warnings), s2.warnings)

    # 端点拒绝流式 -> 降级整包
    s3 = mp.probe_speed("openai", f"{base}/nolist", "sk-test", "m", rounds=1)
    check("不支持流式仍成功", s3.ok, s3.error)
    check("降级样本标 partial", s3.partial, s3.partial)
    check("降级不出首字", s3.ttft_ms is None, s3.ttft_ms)
    check("降级仍算速率", s3.tps is not None and s3.tps > 0, s3.tps)
    check("降级有提示", any("不支持流式" in w or "降级" in w for w in s3.warnings), s3.warnings)

    # 候选回退：直拼 404 -> 补 /v1 命中
    s_fb = mp.probe_speed("openai", f"{base}/gw", "sk-test", "m", rounds=1)
    check("对话端点回退到 /v1", s_fb.ok and s_fb.endpoint.endswith("/gw/v1/chat/completions"),
          (s_fb.error, s_fb.endpoint))
    s_fb2 = mp.probe_speed("openai", f"{base}/gw", "sk-test", "m", rounds=1, auto_v1=False)
    check("关闭 auto_v1 则回退失败", not s_fb2.ok and "404" in s_fb2.error, s_fb2.error)

    # 错误映射
    s4 = mp.probe_speed("openai", f"{base}/limited", "sk-test", "m", rounds=1)
    check("429 映射为限流", not s4.ok and "限流" in s4.error, s4.error)
    check("429 不再误报 404", "404" not in s4.error, s4.error)

    s5 = mp.probe_speed("openai", f"{base}/openai", "sk-wrong", "m", rounds=1)
    check("401 映射为凭据", not s5.ok and "凭据被拒" in s5.error, s5.error)
    check("401 不再误报 404", "404" not in s5.error, s5.error)

    s6 = mp.probe_speed("openai", f"{base}/empty", "sk-test", "m", rounds=1)
    check("空响应报错", not s6.ok and "没有任何增量" in s6.error, s6.error)
    check("空响应不再误报 404", "404" not in s6.error, s6.error)

    s7 = mp.probe_speed("openai", f"{base}/openai", "sk-test", "")
    check("空模型 ID 报错", not s7.ok and "模型 ID" in s7.error, s7.error)

    s8 = mp.probe_speed("openai", "", "sk-test", "m")
    check("空 baseURL 报错", not s8.ok and "baseURL" in s8.error, s8.error)

    s9 = mp.probe_speed("openai", "http://127.0.0.1:1/v1", "k", "m", rounds=1)
    check("连不上有提示", not s9.ok and "无法连接" in s9.error, s9.error)

    check("as_speed_record 形状",
          set(mp.SpeedResult(model="x").as_speed_record()) == {"model_id", "ttft_ms", "tps", "rounds"})

    # ---- 流式中途断开：有正文但没有结束标记 ----
    # 用户实测的坑：站点日志里 0 token / 10 秒 / 502 的失败，客户端这一侧看到的
    # 却是「先收到半截正文，然后连接正常 EOF」。不检查结束标记，半截回答会被当成
    # 可用——这正是「智能路由说可用、真实调用全失败」最隐蔽的一种。
    s_cut = mp.probe_speed("openai", f"{base}/truncate", "sk-test", "m", rounds=1,
                           prompt="请只回复 PONG", expect="PONG")
    check("断流判不合格", not s_cut.ok, (s_cut.ok, s_cut.error))
    check("断流说明点了断流", "中途断开" in s_cut.error, s_cut.error)
    s_cut2 = mp.probe_speed("openai", f"{base}/truncate", "sk-test", "m", rounds=1)
    check("纯测速不因缺结束标记判失败", s_cut2.ok, (s_cut2.ok, s_cut2.error))
    s_minc = mp.probe_speed("openai", f"{base}/openai", "sk-test", "m", rounds=1,
                            min_chars=200)
    check("min_chars 拦下短正文", not s_minc.ok and "短于要求" in s_minc.error, s_minc.error)

    # ---- 商家侧没有可用上游账号：502 要说「换一家」而不是「站点稍等」 ----
    check("502/无可用账号说清是商家侧",
          "商家侧" in mp._speed_error_of(502, '{"error":{"code":"no_healthy_account"}}'),
          mp._speed_error_of(502, '{"error":{"code":"no_healthy_account"}}'))
    check("普通 502 仍按站点网关报",
          "站点网关" in mp._speed_error_of(502, "Bad Gateway"),
          mp._speed_error_of(502, "Bad Gateway"))

    # ---- 首字闸门：超过门槛立即放弃，返回 too_slow 且 ttft=None ----
    t_wall = time.perf_counter()
    s_slow = mp.probe_speed("openai", f"{base}/slow", "sk-test", "m", rounds=3,
                            ttft_limit_ms=400)
    slow_elapsed = time.perf_counter() - t_wall
    check("首字超限标 too_slow", s_slow.too_slow, s_slow.too_slow)
    check("首字超限不出 ttft", s_slow.ttft_ms is None, s_slow.ttft_ms)
    check("首字超限不出 tps", s_slow.tps is None, s_slow.tps)
    check("首字超限不算成功", not s_slow.ok, s_slow.ok)
    check("首字超限即中止（不等满 3 轮）", slow_elapsed < 1.1, f"{slow_elapsed:.2f}s")
    check("首字超限不消耗轮次", s_slow.rounds == 0 and not s_slow.samples, s_slow.rounds)
    check("首字超限有说明", any("上限" in w for w in s_slow.warnings), s_slow.warnings)
    check("首字超限 summary 提到放弃", "放弃" in s_slow.summary(), s_slow.summary())

    # 同一端点、门槛放宽 -> 正常测出（证明掐断的是「等太久」而非「测不到」）
    s_slow_ok = mp.probe_speed("openai", f"{base}/slow", "sk-test", "m", rounds=1,
                               ttft_limit_ms=5000)
    check("门槛放宽后能测出", s_slow_ok.ok and s_slow_ok.ttft_ms >= 1000,
          (s_slow_ok.error, s_slow_ok.ttft_ms))

    # 只发心跳不出正文：应在首字窗口内放弃，而不是等满 socket 超时
    t_wall = time.perf_counter()
    s_hb = mp.probe_speed("openai", f"{base}/heartbeat-only", "sk-test", "m", rounds=1,
                          timeout=25, ttft_limit_ms=500)
    hb_elapsed = time.perf_counter() - t_wall
    check("纯心跳标 too_slow", s_hb.too_slow, (s_hb.error, s_hb.too_slow))
    check("纯心跳在窗口附近放弃（<2s）", hb_elapsed < 2.0, f"{hb_elapsed:.2f}s")

    # ---- 并行批量测速 ----
    t_wall = time.perf_counter()
    results = mp.probe_speed_many("openai", f"{base}/openai", "sk-test",
                                  ["m1", "m2", "m3", "m4"], rounds=1, workers=4)
    par_elapsed = time.perf_counter() - t_wall
    check("批量返回全部结果", len(results) == 4, len(results))
    check("批量结果全部成功", all(r.ok for r in results), [r.error for r in results])
    # 串行 4 个约 4×0.25s≈1s；4 路并发应明显更快
    check("4 路并发快于串行", par_elapsed < 0.9, f"{par_elapsed:.2f}s")

    # 回调按完成顺序触发，done 从 1 递增到 total
    seen = []
    mp.probe_speed_many("openai", f"{base}/openai", "sk-test", ["a", "b", "c"],
                        rounds=1, workers=3,
                        on_result=lambda r, n, t: seen.append((r.model, n, t)))
    check("回调次数=模型数", len(seen) == 3, seen)
    check("回调 done 递增", [s[1] for s in seen] == [1, 2, 3], seen)
    check("回调 total 一致", all(s[2] == 3 for s in seen), seen)

    # 空列表 / 全空白：不该起线程，直接返回空
    check("空列表返回空", mp.probe_speed_many("openai", f"{base}/openai", "k", []) == [])
    check("全空白返回空", mp.probe_speed_many("openai", f"{base}/openai", "k", ["  ", ""]) == [])

    # 单个失败不拖垮整批
    mixed = mp.probe_speed_many("openai", f"{base}/limited", "sk-test", ["x", "y"],
                                rounds=1, workers=2)
    check("批量里失败也返回记录", len(mixed) == 2 and all(r.error for r in mixed),
          [r.error for r in mixed])

    # workers=1 等价串行
    serial = mp.probe_speed_many("openai", f"{base}/openai", "sk-test", ["s1", "s2"],
                                 rounds=1, workers=1)
    check("workers=1 仍全部成功", len(serial) == 2 and all(r.ok for r in serial),
          [r.error for r in serial])

    # should_stop 取消：一个都不启动
    check("取消后不启动任务",
          mp.probe_speed_many("openai", f"{base}/openai", "sk-test", ["z1", "z2"],
                              rounds=1, should_stop=lambda: True) == [])

    # 对话端点派生（四种协议）
    check("openai 对话端点", mp.chat_url("openai", "https://x.com/v1").endswith("/v1/chat/completions"))
    # 与列表接口同口径：baseURL 当前缀直拼，/v1 作为 404 时的回退候选
    check("baseURL 当前缀直拼", mp.chat_url("openai", "https://x.com") == "https://x.com/chat/completions")
    check("裸域有 /v1 回退候选",
          mp.chat_candidates("openai", "https://x.com")[1] == "https://x.com/v1/chat/completions")
    check("带 /v1 不再补 /v1", len(mp.chat_candidates("openai", "https://x.com/v1")) == 1)
    check("anthropic 对话端点", mp.chat_url("anthropic", "https://a.com/v1").endswith("/v1/messages"))
    check("gemini 流式端点", ":streamGenerateContent" in mp.chat_url("gemini", "https://g.com/v1beta"))
    check("ollama 对话端点", mp.chat_url("ollama", "http://h:11434").endswith("/api/chat"))
    check("空 baseURL 得空端点", mp.chat_url("openai", "") == "")
    check("空候选列表", mp.chat_candidates("openai", "") == [])

    srv.shutdown()
    print("\nRESULT:", "FAILED" if check.failed else "ALL PASS")


if __name__ == "__main__":
    main()
