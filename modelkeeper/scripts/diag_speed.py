"""诊断脚本（不是产品的一部分）：把流式响应里**每一帧的到达时刻**打出来。

目的：判断「800 tok/s」到底是端点真的这么快，还是中转站把流式响应攒成一坨
再一次性灌下来（那样 client 端的解码区间会被压缩，速率被虚高）。

用法：
    python scripts/diag_speed.py [模型ID ...] [--rounds N] [--tokens N] [--prompt 字面量]

密钥从 DSH 的 cordis.patch.yml + .credentials.yaml 读，不打印、不落盘。
"""
from __future__ import annotations
# 直接运行本文件时，把项目根目录加入 sys.path（无需安装成包）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
import statistics
import time
import urllib.request

from modelkeeper import agent_import as ai
from modelkeeper import model_probe as mp

LONG_PROMPT = (
    "请从 1 数到 300，每个数字之间用空格分隔。只输出数字，不要任何解释或标点。"
)


def one_round(base, key, model, max_tokens, timeout, prompt) -> dict:
    url = mp.chat_candidates("openai", base, True)[0].replace("{model}", model)
    body = {
        "model": model,
        "stream": True,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
        "stream_options": {"include_usage": True},
    }
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}"}
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    t0 = time.perf_counter()
    arrivals: list[float] = []
    texts: list[str] = []
    raw_usage = None
    finish = None
    with opener.open(req, timeout=timeout) as resp:
        for obj in mp._iter_frames(resp):
            text, seen = mp._extract_delta("openai", obj)
            if seen is not None:
                raw_usage = seen
            if isinstance(obj.get("usage"), dict) and obj["usage"]:
                raw_usage = dict(obj["usage"])
            for ch in obj.get("choices") or []:
                if isinstance(ch, dict) and ch.get("finish_reason"):
                    finish = ch["finish_reason"]
            if not text:
                continue
            arrivals.append(time.perf_counter())
            texts.append(text)
        t_end = time.perf_counter()

    if not arrivals:
        return {"error": "没有正文帧"}
    first, last = arrivals[0], arrivals[-1]
    span = last - first
    gaps = [b - a for a, b in zip(arrivals, arrivals[1:])]
    text = "".join(texts)
    tokens = raw_usage.get("completion_tokens") if isinstance(raw_usage, dict) else None
    tokens = tokens or len(arrivals)
    return {
        "url": url,
        "ttft_ms": round((first - t0) * 1000),
        "decode_ms": round(span * 1000),
        "total_ms": round((t_end - t0) * 1000),
        "frames": len(arrivals),
        "chars": len(text),
        "tokens": tokens,
        "usage": raw_usage,
        "finish": finish,
        "gap_min": round(min(gaps) * 1000, 1) if gaps else 0.0,
        "gap_med": round(statistics.median(gaps) * 1000, 1) if gaps else 0.0,
        "gap_max": round(max(gaps) * 1000, 1) if gaps else 0.0,
        "zero_gaps": sum(1 for g in gaps if g < 0.002),
        "tps_window": round(tokens / span, 1) if span > 0 else None,
        "tps_e2e": round(tokens / (t_end - t0), 1),
        "tps_to_last": round(tokens / (last - t0), 1),
        "head": text[:50],
        "tail": text[-50:],
    }


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("models", nargs="*")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--tokens", type=int, default=mp.SPEED_MAX_TOKENS)
    ap.add_argument("--prompt", default=mp.SPEED_PROMPT)
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--probe", action="store_true",
                    help="走 mp.probe_speed 汇总口径，打印最终上报的 ttft/tps")
    args = ap.parse_args(argv)

    rows = ai.read_dsh()
    if args.models:
        picked = [r for r in rows if r.model_id in args.models]
    else:
        picked = rows[:1]
    if not picked:
        print("没有匹配的模型；可用：", ", ".join(r.model_id for r in rows))
        return 2

    for r in picked:
        if not r.api_key:
            print(f"{r.model_id}: 没有可用的 Key（{r.api_key_env}），跳过")
            continue
        print(f"\n=== {r.model_id} @ {r.base_url} (provider {r.provider}) ===")
        if args.probe:
            res = mp.probe_speed(
                "openai", r.base_url, r.api_key, r.model_id,
                rounds=args.rounds, max_tokens=args.tokens, prompt=args.prompt,
                timeout=args.timeout,
            )
            print(f"  上报：首字 {res.ttft_ms} ms · 速率 {res.tps} tok/s"
                  f" · 可信 {res.reliable}")
            print(f"  样本：正文 {res.text_tokens} tok / 窗口 {res.window_ms} ms"
                  f" · 扣掉推理 {res.reasoning_tokens} / usage {res.usage_tokens}")
            for s in res.samples:
                print(f"    · ttft {s['ttftMs']} ms · 窗口 {s['windowMs']} ms"
                      f" · {s['textTokens']} tok → {s['tps']} tok/s")
            for w in res.warnings:
                print(f"    ! {w}")
            if res.error:
                print(f"    错误：{res.error}")
            continue
        for i in range(args.rounds):
            try:
                got = one_round(r.base_url, r.api_key, r.model_id,
                                args.tokens, args.timeout, args.prompt)
            except Exception as e:  # noqa: BLE001
                print(f"  round{i + 1}: 失败 {type(e).__name__}: {e}")
                continue
            if "error" in got:
                print(f"  round{i + 1}: {got['error']}")
                continue
            print(f"  round{i + 1}: ttft {got['ttft_ms']} ms · 解码窗口 "
                  f"{got['decode_ms']} ms · 总 {got['total_ms']} ms · {got['chars']} 字符"
                  f" · finish={got['finish']} usage={got['usage']}")
            print(f"           {got['tokens']} tok / {got['frames']} 帧 · 帧间隔 "
                  f"min/med/max = {got['gap_min']}/{got['gap_med']}/{got['gap_max']} ms"
                  f" · 其中 {got['zero_gaps']} 个间隔 <2ms")
            print(f"           tps: 解码窗口 {got['tps_window']} | 到末帧 "
                  f"{got['tps_to_last']} | 端到端 {got['tps_e2e']}")
            print(f"           头：{got['head']!r}\n           尾：{got['tail']!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
