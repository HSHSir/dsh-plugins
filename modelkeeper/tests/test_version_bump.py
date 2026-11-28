#!/usr/bin/env python3
"""`version_bump.py` 的回归测试。

重点覆盖一条硬约束：**升版本这个动作本身不能改变源码指纹**，否则每次打包
都会判定「有变更」并再次 +1，版本号会无限自增。

运行：python tests/test_version_bump.py
"""

from __future__ import annotations
# 直接运行本文件时，把项目根目录加入 sys.path（无需安装成包）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import json
import shutil
import tempfile

from scripts import version_bump as vb

PASSED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        raise AssertionError(f"{name} 失败 {detail}")
    PASSED.append(name)


def read_raw(path: Path) -> str:
    """按原始换行符读，用于验证「没有偷偷改成 CRLF」。

    `Path.read_text(newline="")` 要 Python 3.13，本项目跑 3.12，所以走 open()。
    """
    with path.open("r", encoding="utf-8", newline="") as fh:
        return fh.read()


def write_raw(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def make_repo(root: Path, version: str = "1.0.0") -> None:
    """铺一个最小项目：一个带版本号的主模块 + 一个普通的副模块。

    版本文件的位置由 `vb.probe_path()` 决定（当前是 `modelkeeper/model_probe.py`），
    测试不自行拼路径——布局调整时这里不用跟着改。
    """
    probe = vb.probe_path(root)
    probe.parent.mkdir(parents=True, exist_ok=True)
    probe.write_text(
        "#!/usr/bin/env python3\n"
        'APP_TITLE = "模型管家"\n'
        f'APP_VERSION = "{version}"\n'
        'WINDOW_TITLE = f"{APP_TITLE} v{APP_VERSION}"\n',
        encoding="utf-8", newline="",
    )
    write_raw(root / "gui_app.py", "X = 1\n")


def test_incr_patch() -> None:
    check("patch 正常 +1", vb.incr_patch("1.0.0") == "1.0.1")
    # 逢 10 不进位：这是明确的产品决策，不是 bug
    check("9 之后是 10 而非进位", vb.incr_patch("1.0.9") == "1.0.10")
    check("10 之后是 11", vb.incr_patch("1.0.10") == "1.0.11")
    check("99 之后是 100", vb.incr_patch("1.0.99") == "1.0.100")
    check("major/minor 不受影响", vb.incr_patch("2.7.3") == "2.7.4")
    check("两位缺位补齐", vb.incr_patch("1.2") == "1.2.1")


def test_critical_no_self_trigger() -> None:
    """最重要的一条：改版本号不能导致指纹变化。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_repo(root, "1.0.0")
        before, _ = vb.source_fingerprint(root)
        vb.write_version(vb.probe_path(root), "1.0.7")
        after, _ = vb.source_fingerprint(root)
        check("改版本号不改指纹（否则会无限自增）", before == after,
              f"{before[:12]} != {after[:12]}")
        check("版本号确实写进去了", vb.read_version(vb.probe_path(root)) == "1.0.7")


def test_fingerprint_reacts_to_real_change() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_repo(root)
        before, n1 = vb.source_fingerprint(root)
        write_raw(root / "gui_app.py", "X = 2\n")
        after, n2 = vb.source_fingerprint(root)
        check("普通源码改动会改指纹", before != after, "指纹没变，判定会失效")
        check("文件数不变", n1 == n2)

        # 新增文件也要被感知
        write_raw(root / "newmod.py", "Y = 1\n")
        with_new, n3 = vb.source_fingerprint(root)
        check("新增源码文件会改指纹", with_new != after)
        check("文件数 +1", n3 == n2 + 1)


def test_excluded_dirs_ignored() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_repo(root)
        before, _ = vb.source_fingerprint(root)
        for sub in ("__pycache__", "build", "dist"):
            d = root / sub
            d.mkdir()
            (d / "junk.py").write_text("J = 1\n", encoding="utf-8", newline="")
        after, _ = vb.source_fingerprint(root)
        check("缓存/构建目录不计入指纹", before == after,
              "打包产物变动会误触发升版本")


def test_bump_flow() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_repo(root, "1.0.0")

        # 首次：无状态文件 → 建立基线并 +1
        v1, bumped, reason = vb.bump_if_changed(root)
        check("首次运行会递增", bumped and v1 == "1.0.1", f"得到 {v1} / {reason}")
        check("状态文件已写入", (root / vb.STATE_NAME).exists())

        # 无改动：保持原版本
        v2, bumped2, _ = vb.bump_if_changed(root)
        check("源码无变更不递增", not bumped2 and v2 == v1, f"得到 {v2}")

        # 再跑一次仍然不动（确认不是只稳定一次）
        v3, bumped3, _ = vb.bump_if_changed(root)
        check("连续多次打包版本稳定", not bumped3 and v3 == v1)

        # 改源码 → +1
        write_raw(root / "gui_app.py", "X = 99\n")
        v4, bumped4, reason4 = vb.bump_if_changed(root)
        check("源码变更后递增", bumped4 and v4 == "1.0.2", f"得到 {v4} / {reason4}")

        # 手动 --bump：无论有没有变更都 +1
        v5, bumped5, _ = vb.bump_if_changed(root, force=True)
        check("force 强制递增", bumped5 and v5 == "1.0.3", f"得到 {v5}")

        check("version 状态与文件同步",
              json.loads(read_raw(root / vb.STATE_NAME))["version"] == v5)
        check("实际文件版本一致", vb.read_version(vb.probe_path(root)) == v5)


def test_dry_run_writes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_repo(root, "1.0.0")
        before_version = vb.read_version(vb.probe_path(root))
        before_text = read_raw(vb.probe_path(root))

        v, bumped, _ = vb.bump_if_changed(root, dry_run=True)
        check("预演报告会递增", bumped and v == "1.0.1")
        check("预演不写源码", vb.read_version(vb.probe_path(root)) == before_version)
        check("源码内容一字节未变",
              read_raw(vb.probe_path(root)) == before_text)
        check("预演不产生状态文件", not (root / vb.STATE_NAME).exists())


def test_write_preserves_formatting() -> None:
    """写版本号不能把 LF 变成 CRLF，也不能动别的行。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        probe = vb.probe_path(root)
        probe.parent.mkdir(parents=True, exist_ok=True)
        # 故意混入中文、注释、相似变量名，确认只改目标那一行
        original = (
            "# 注释：APP_VERSION = \"9.9.9\" 出现在注释里不该被改\n"
            'APP_VERSION_EXTRA = "keep-me"\n'
            'APP_VERSION = "1.0.0"\n'
            'OTHER = "APP_VERSION = \\"x\\""\n'
        )
        write_raw(probe, original)
        vb.write_version(probe, "1.2.3")
        text = read_raw(probe)
        check("注释里的假版本号未被修改", '# 注释：APP_VERSION = "9.9.9"' in text)
        check("前缀相同的变量未被修改", 'APP_VERSION_EXTRA = "keep-me"' in text)
        check("目标行已更新", 'APP_VERSION = "1.2.3"' in text)
        check("仍为 LF，无 CRLF", "\r\n" not in text, "换行符被改成了 CRLF")
        check("行数不变", len(text.splitlines()) == len(original.splitlines()))


def test_corrupt_state_recovers() -> None:
    """状态文件损坏时不能崩，按「有变更」处理并重建。"""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        make_repo(root, "1.0.0")
        write_raw(root / vb.STATE_NAME, "{ 这不是 JSON")
        v, bumped, _ = vb.bump_if_changed(root)
        check("状态文件损坏时仍能递增", bumped and v == "1.0.1", f"得到 {v}")
        # 损坏内容已被合法 JSON 覆盖
        data = json.loads(read_raw(root / vb.STATE_NAME))
        check("状态文件已重建", data["version"] == "1.0.1")


def test_crlf_file_supported() -> None:
    """CRLF 的源码也要能读能写。

    编辑器或批量脚本很容易把 .py 存成 CRLF；一旦如此，`^...$` 这种正则就完全
    失效（`$` 前多一个 \r），打包会死在「找不到 APP_VERSION」上，而文件看起来
    毫无问题。这里钉住「读得出、写得回、换行风格不变」。
    """
    root = Path(tempfile.mkdtemp(prefix="crlf-"))
    try:
        probe = vb.probe_path(root)
        probe.parent.mkdir(parents=True, exist_ok=True)
        write_raw(probe, 'HEAD\r\nAPP_VERSION = "2.3.4"\r\nTAIL\r\n')
        check("CRLF 能读出版本", vb.read_version(probe) == "2.3.4",
              vb.read_version(probe))
        vb.write_version(probe, "2.3.5")
        text = read_raw(probe)
        check("CRLF 能写回版本", 'APP_VERSION = "2.3.5"' in text, text)
        check("写回后仍是 CRLF", "\r\n" in text and "\n" not in text.replace("\r\n", ""), text)
        check("没有被改回 LF", text.count("\r\n") == 3, text.count("\r\n"))
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    tests = [
        test_incr_patch,
        test_critical_no_self_trigger,
        test_fingerprint_reacts_to_real_change,
        test_excluded_dirs_ignored,
        test_bump_flow,
        test_dry_run_writes_nothing,
        test_write_preserves_formatting,
        test_corrupt_state_recovers,
        test_crlf_file_supported,
    ]
    for fn in tests:
        fn()
    print(f"version_bump 测试通过：{len(PASSED)} 项断言")
    for name in PASSED:
        print(f"  ✓ {name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
