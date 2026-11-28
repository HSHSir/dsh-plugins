#!/usr/bin/env python3
"""版本号自动递增：仅当源码有实质变更时把 patch 位 +1。

为什么要单独一个模块
--------------------
`build_exe.py` 每次打包都会重写 `version_info.txt`，而版本号的唯一来源是
`model_probe.APP_VERSION`。把「判断该不该升版本」的规则独立出来后，它可以被
单元测试覆盖，也不必让打包脚本自己长出一堆状态比对逻辑。

核心难点：版本号自身也是源码
----------------------------
`APP_VERSION` 就写在 `model_probe.py` 里。如果直接把所有 .py 的内容哈希当作
指纹，那么「升版本」这个动作本身就会改变指纹，导致下次打包又判定为「有变更」、
再次 +1 —— 无限自激。

所以算指纹前，必须先把 `model_probe.py` 里的 `APP_VERSION = "..."` 这一行
**归一化成固定占位符**。这样指纹只反映「除版本号以外」的代码，升版本不会自我
触发。这是本模块最关键的一条不变量，改动时不要绕过。

递增规则
--------
逢 10 不进位：`1.0.9` → `1.0.10` → `1.0.11`。语义是标准 semver，patch 位不封顶。

状态文件
--------
`.version_state.json` 记录上次打包时的指纹与版本，用于跨次比对。
它不在 `*.py` 的通配范围内，因此不会被算进指纹，也不会被 PyInstaller 打包。
"""

from __future__ import annotations
# 直接运行本文件时，把项目根目录加入 sys.path（无需安装成包）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import hashlib
import json
import re
from datetime import datetime

STATE_NAME = ".version_state.json"

# 这些目录里的内容不算源码：缓存、打包中间产物、构建输出、工具自身数据
EXCLUDE_DIRS = frozenset({
    "__pycache__", "build", "dist", ".workbuddy",
    ".git", ".svn", ".venv", "venv", ".idea", ".vscode",
})

# 认版本的源码文件（相对项目根）：版本号常量就写在它里面
PROBE_REL = Path("modelkeeper") / "model_probe.py"


def probe_path(root: Path) -> Path:
    """项目根目录下 `model_probe.py` 的实际位置。

    单独成函数而不是直接 `root / PROBE_REL`：源码布局调整时只改 `PROBE_REL` 一处，
    打包脚本、测试与状态判定都跟着走。
    """
    return root / PROBE_REL

# 匹配 `APP_VERSION = "1.0.0"` 这一整行（行首到行尾，不跨行）
# 行尾用 `(?=\r?$)` 而不是 `$`：CRLF 文件里 `$` 前面还站着一个 `\r`，直接写 `$`
# 会一行都匹配不到，打包脚本就会报「找不到 APP_VERSION」——而文件看上去完全正常。
# 用 lookahead 的另一个好处是替换不吞掉那个 `\r`，写回后换行风格不变。
_VERSION_LINE = re.compile(r'(?m)^APP_VERSION = "[^"]*"(?=\r?$)')
_VERSION_VALUE = re.compile(r'(?m)^APP_VERSION = "([^"]+)"(?=\r?$)')


def _read(path: Path) -> str:
    """按原始换行符读文件。

    不能用 `path.read_text(newline="")` —— 那个参数要 Python 3.13 才有，
    本项目跑在 3.12 上。文本模式默认会把 CRLF 归一成 LF，指纹和回写都会失真。
    """
    with path.open("r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(path: Path, text: str) -> None:
    """按原始换行符写文件，不做 \\n → \\r\\n 翻译。"""
    with path.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)


# --------------------------------------------------------------------------- #
# 版本号读写
# --------------------------------------------------------------------------- #

def read_version(probe: Path) -> str:
    """从 model_probe.py 读出当前版本号。"""
    text = _read(probe)
    found = _VERSION_VALUE.search(text)
    if not found:
        raise RuntimeError(f'{probe.name} 里找不到 APP_VERSION = "x.y.z" 这一行')
    return found.group(1)


def write_version(probe: Path, version: str) -> None:
    """把版本号写回 model_probe.py，其余内容一字不动。

    必须走 `_write`（newline=""）：Windows 上文本模式默认把 `\\n` 翻成 `\\r\\n`，
    这个项目全用 LF，写一次就整份变 CRLF，diff 会脏掉 1000 多行。
    """
    text = _read(probe)
    new_text, count = _VERSION_LINE.subn(f'APP_VERSION = "{version}"', text)
    if count != 1:
        raise RuntimeError(f"APP_VERSION 行匹配到 {count} 处，期望 1 处，已中止以免写坏文件")
    _write(probe, new_text)


def incr_patch(version: str) -> str:
    """patch 位 +1，逢 10 不进位。"""
    parts = version.split(".")
    while len(parts) < 3:
        parts.append("0")
    major, minor, patch = parts[0], parts[1], parts[2]
    try:
        number = int(patch)
    except ValueError:
        number = 0
    return f"{major}.{minor}.{number + 1}"


# --------------------------------------------------------------------------- #
# 指纹
# --------------------------------------------------------------------------- #

def _iter_sources(root: Path):
    """项目内所有参与指纹的 .py，按相对路径排序保证结果稳定。"""
    picked: list[Path] = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if any(part in EXCLUDE_DIRS for part in rel.parts):
            continue
        picked.append(rel)
    return sorted(picked, key=lambda p: p.as_posix())


def source_fingerprint(root: Path) -> tuple[str, int]:
    """算出源码指纹，返回 (哈希, 参与文件数)。

    比对前把 model_probe.py 的版本号行归一化 —— 见模块开头「核心难点」。
    """
    digest = hashlib.sha256()
    files = _iter_sources(root)
    for rel in files:
        text = _read(root / rel)
        if rel == PROBE_REL:
            text = _VERSION_LINE.sub('APP_VERSION = "<version>"', text)
        digest.update(rel.as_posix().encode("utf-8"))
        digest.update(b"\x00")
        digest.update(text.encode("utf-8"))
        digest.update(b"\x00")
    return digest.hexdigest(), len(files)


# --------------------------------------------------------------------------- #
# 状态文件
# --------------------------------------------------------------------------- #

def load_state(state_path: Path) -> dict | None:
    if not state_path.exists():
        return None
    try:
        data = json.loads(_read(state_path))
    except (OSError, json.JSONDecodeError):
        return None          # 文件坏了就当没有，宁可多升一次也不要崩掉打包
    return data if isinstance(data, dict) else None


def save_state(state_path: Path, version: str, fingerprint: str, files: int) -> None:
    _write(state_path, json.dumps({
        "version": version,
        "fingerprint": fingerprint,
        "files": files,
        "bumped_at": datetime.now().isoformat(timespec="seconds"),
    }, ensure_ascii=False, indent=2) + "\n")


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def bump_if_changed(root: Path, force: bool = False,
                    dry_run: bool = False) -> tuple[str, bool, str]:
    """按需递增版本号。

    返回 (版本号, 是否递增了, 判定原因)。

    判定为「需要递增」的三种情况：
      1. `force=True`（命令行 --bump 手动触发）；
      2. 没有状态文件 —— 首次建立基线，视为一次变更；
      3. 指纹与状态文件记录的不一致 —— 源码确实改过。

    没有变更时保持原版本，不写入任何文件。
    """
    probe = probe_path(root)
    if not probe.exists():
        raise RuntimeError(f"找不到 {probe}")

    current = read_version(probe)
    fingerprint, files = source_fingerprint(root)
    state = load_state(root / STATE_NAME)

    if force:
        reason = "手动指定 --bump"
    elif state is None:
        reason = "首次运行，无历史指纹"
    elif state.get("fingerprint") != fingerprint:
        reason = f"源码有变更（{state.get('files', '?')} → {files} 个文件，指纹变化）"
    else:
        return current, False, "源码无变更"

    new_version = incr_patch(current)
    if dry_run:
        return new_version, True, reason + "（预演，未写入）"

    write_version(probe, new_version)
    save_state(root / STATE_NAME, new_version, fingerprint, files)
    return new_version, True, reason


def main(argv: list[str]) -> int:
    # 本文件位于 scripts/，项目根是上一级
    root = Path(__file__).resolve().parents[1]
    force = "--bump" in argv
    dry_run = "--check" in argv

    version, bumped, reason = bump_if_changed(root, force=force, dry_run=dry_run)
    if bumped:
        print(f"版本号 → {version}（{reason}）")
    else:
        print(f"版本号保持 {version}（{reason}）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
