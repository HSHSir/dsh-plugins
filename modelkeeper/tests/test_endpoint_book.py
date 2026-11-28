#!/usr/bin/env python3
"""端点记录自测：同 URL 去重、Key 替换、空 Key 不抹、排序、上限、损坏容错、掩码。"""

from __future__ import annotations
# 直接运行本文件时，把项目根目录加入 sys.path（无需安装成包）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import os
import tempfile

from modelkeeper import endpoint_book as eb


class check:  # noqa: N801 - 与其它自测脚本保持同一种调用样式
    failed = 0

    def __init__(self, name: str, ok: object, detail: object = "") -> None:
        if ok:
            print(f"PASS  {name}")
        else:
            check.failed += 1
            print(f"FAIL  {name}  -> {detail}")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="endpoints-"))
    os.environ["MODEL_PROBE_ENDPOINT_FILE"] = str(tmp / "endpoints.json")
    path = eb.endpoint_path()

    # ---- 空 / 损坏都当空表 ----
    check("文件不存在返回空表", eb.load()["entries"] == [], eb.load())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 坏 json", encoding="utf-8")
    check("坏文件容错", eb.load()["entries"] == [], eb.load())
    path.unlink()

    # ---- 写入 / 读回 ----
    data = eb.upsert("https://a.com/v1", "sk-aaa", "openai", eb.load())
    eb.save(data)
    rows = eb.load()["entries"]
    check("写入后可读回", len(rows) == 1 and rows[0]["key"] == "sk-aaa", rows)
    check("记住了协议", rows[0]["protocol"] == "openai", rows)
    check("带时间戳", bool(rows[0]["updated"]), rows)

    # ---- 同 URL 只留一条（含尾斜杠与大小写差异） ----
    data = eb.upsert("https://A.COM/v1/", "sk-new", "openai", eb.load())
    eb.save(data)
    rows = eb.entries(eb.load())
    check("同 URL 归一后仍是一条", len(rows) == 1, rows)
    check("Key 被替换成最新", rows[0]["key"] == "sk-new", rows)
    check("保留首次登记的写法", rows[0]["base"] == "https://a.com/v1", rows[0]["base"])

    # ---- 空 Key 不覆盖已有密钥 ----
    data = eb.upsert("https://a.com/v1", "", "openai", eb.load())
    check("空 Key 不抹掉已存的", eb.find("https://a.com/v1", data)["key"] == "sk-new",
          eb.find("https://a.com/v1", data))

    # ---- 多条目 + 最近使用排前 ----
    data = eb.upsert("https://b.com/v1", "sk-bbb", "anthropic", data)
    eb.save(data)
    order = [r["base"] for r in eb.entries(eb.load())]
    check("最近写入的排最前", order[0] == "https://b.com/v1", order)
    # 再摸一次 a：它应该回到最前
    data = eb.upsert("https://a.com/v1", "sk-a2", "openai", eb.load())
    eb.save(data)
    order = [r["base"] for r in eb.entries(eb.load())]
    check("重新使用后提到最前", order[0] == "https://a.com/v1", order)

    # ---- 上限裁剪 ----
    data = eb.load()
    for i in range(eb.MAX_ENTRIES + 10):
        data = eb.upsert(f"https://x{i}.com", f"k{i}", "openai", data)
    eb.save(data)
    rows = eb.load()["entries"]
    check(f"最多保留 {eb.MAX_ENTRIES} 条", len(rows) == eb.MAX_ENTRIES, len(rows))
    check("保留的是最新的", rows[0]["base"] == f"https://x{eb.MAX_ENTRIES + 9}.com", rows[0])

    # ---- 删除 / 清空 ----
    data = eb.remove("https://x0.com", eb.load())
    check("删除后查不到", eb.find("https://x0.com", data) is None, eb.find("https://x0.com", data))
    check("清空后为空", eb.clear(data)["entries"] == [], eb.clear(data)["entries"])

    # ---- 掩码：认得出是哪把，又不摊完整串 ----
    long_key = "sk-abcdefghijklmnop"
    masked = eb.mask_key(long_key)
    check("长 Key 打码", long_key not in masked, masked)
    check("打码保留头尾", masked.startswith("sk-") and masked.endswith("mnop"), masked)
    check("空 Key 有明确提示", eb.mask_key("") == "（无 Key）", eb.mask_key(""))
    short = eb.mask_key("sk-abc")
    check("短 Key 全遮", "abc" not in short, short)

    # ---- find：大小写与尾斜杠都不影响 ----
    data = eb.upsert("https://c.com/v1", "sk-c", "openai", eb.load())
    check("find 忽略大小写", eb.find("HTTPS://C.COM/v1", data) is not None, data)
    check("find 忽略尾斜杠", eb.find("https://c.com/v1/", data) is not None, data)

    # ---- 文件权限尽力收紧（Windows 上 chmod 只能动只读位，失败不算错） ----
    check("保存返回真实路径", eb.save(data) == path, eb.save(data))
    check("文件内容确实是 JSON", json.loads(path.read_text(encoding="utf-8"))["version"] == 1)

    print("\nRESULT:", "ALL PASS" if not check.failed else f"{check.failed} FAILED")
    raise SystemExit(1 if check.failed else 0)


if __name__ == "__main__":
    main()
