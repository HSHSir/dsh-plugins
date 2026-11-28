#!/usr/bin/env python3
"""打包 exe：PyInstaller 单文件、无控制台窗口、自带图标。

打包前会调用 `version_bump` 做版本号自增：**仅当源码有实质变更时** patch 位 +1，
无变更则沿用当前版本（反复打包不会空涨版本号）。判定逻辑与状态文件都在
`version_bump.py`，那里也解释了为什么改版本号本身不会自激。

中文（程序名、提示语）刻意留在本文件而不是 ``.bat`` 里：bat 的读取编码取决于
控制台代码页（936 / 65001），同一份文件在两种代码页下会读出两套字符，曾因此
生成出一个乱码名字的 exe。Python 源文件固定 UTF-8，不会踩这个坑。
"""

from __future__ import annotations
# 直接运行本文件时，把项目根目录加入 sys.path（无需安装成包）。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import os
import subprocess

from modelkeeper import model_probe as mp  # 版本号与产品名的唯一来源
from scripts import version_bump as vb

HERE = Path(__file__).resolve().parents[1]   # 项目根（本文件在 scripts/ 下）
APP_NAME = "模型管家"
ENTRY = HERE / "main.py"
ICON = HERE / "modelkeeper" / "assets" / "keeper.ico"
PRESET = HERE / "modelkeeper" / "assets" / "prices_preset.json"
VERSION_FILE = HERE / "version_info.txt"


# 当前解释器没有 tkinter 时，按这个顺序找替代品。`py` 是 Windows 的启动器，
# 能列出机器上所有官方安装；PATH 上的 `python` 可能是精简版（本机就是：
# 3.13 无 tk、3.14 有），所以它排在最后。
_GUI_CANDIDATES = (
    ["py", "-3.14"], ["py", "-3.13"], ["py", "-3.12"],
    ["py", "-3.11"], ["python"], ["python3"],
)
# 重跑自己的标记：换上来的解释器若仍没有 tk，必须停下来报错，
# 否则会「找不到 → 换一个 → 还是同一个 → 再换」无限循环。
_REEXEC_FLAG = "MODEL_KEEPER_BUILD_REEXEC"


def _has_tkinter(cmd: list[str]) -> bool:
    try:
        done = subprocess.run(cmd + ["-c", "import tkinter"],
                              capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def ensure_gui_interpreter() -> None:
    """保证打包用的解释器带 tkinter，缺了就换一个重跑自己。

    PyInstaller 不会替你补 Tcl/Tk：用无 tk 的解释器打出来的 exe，双击瞬间退出，
    窗口都来不及画——而报错只出现在打包日志的一行 WARNING 里，很容易漏。
    所以这项检查必须在打包前做，不能等用户拿到一个打不开的 exe。
    """
    try:
        import tkinter  # noqa: F401
    except ImportError:
        pass
    else:
        return

    if os.environ.get(_REEXEC_FLAG):
        raise SystemExit(
            f"解释器 {sys.executable} 没有 tkinter，换一个之后仍然没有。\n"
            "请安装 python.org 的官方安装包（自带 tkinter），或用 "
            "`py -3.14 scripts/build_exe.py` 指定一个带 tk 的版本。"
        )

    for cmd in _GUI_CANDIDATES:
        if not _has_tkinter(cmd):
            continue
        print(f"当前解释器没有 tkinter（{sys.executable}）")
        print(f"改用 {' '.join(cmd)} 重新执行打包……")
        env = dict(os.environ, **{_REEXEC_FLAG: "1"})
        code = subprocess.call(
            cmd + [str(Path(__file__).resolve()), *sys.argv[1:]],
            cwd=HERE, env=env,
        )
        raise SystemExit(code)

    raise SystemExit(
        "找不到带 tkinter 的 Python，无法打包。\n"
        "PyInstaller 不会替你补 Tcl/Tk，用无 tk 的解释器打出来的 exe 会启动即退出。\n"
        "请安装 python.org 的官方安装包（默认包含 tkinter）。"
    )


def current_version() -> str:
    """从磁盘重新读一次版本号。

    必须重新读：`mp` 在模块导入时就绑定了 `APP_VERSION`，而 `version_bump` 可能
    刚刚改写了 `model_probe.py` 里的那一行，此时 `mp.APP_VERSION` 还是旧值。
    用旧的去写 `version_info.txt`，exe 属性里的版本就和标题栏对不上了。
    """
    return vb.read_version(vb.probe_path(HERE))

# PyInstaller 把这份文件当成 Python 求值（VSVersionInfo 等类由它注入，不用 import）。
# 只写版本号：exe 属性里「文件版本 / 产品版本」两项，其余字段留空。
VERSION_TEMPLATE = """\
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={quad},
    prodvers={quad},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([StringTable('080404b0', [
      StringStruct('FileVersion', '{version}'),
      StringStruct('ProductVersion', '{version}'),
    ])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])]),
  ],
)
"""


def quad(version: str) -> tuple[int, int, int, int]:
    """'1.2.3' -> (1, 2, 3, 0)；不足四位补零，多了截断。"""
    parts: list[int] = []
    for chunk in version.split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    parts = (parts + [0, 0, 0, 0])[:4]
    return tuple(parts)  # type: ignore[return-value]


def write_version_file(version: str) -> Path:
    """生成 version_info.txt；每次打包重写，别手改。"""
    VERSION_FILE.write_text(
        VERSION_TEMPLATE.format(quad=quad(version), version=version),
        encoding="utf-8",
    )
    return VERSION_FILE


def run(cmd: list[str]) -> int:
    print("$", " ".join(cmd))
    return subprocess.call(cmd, cwd=HERE)


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print(f"未检测到 PyInstaller（{sys.executable}），正在安装……")
        if run([sys.executable, "-m", "pip", "install", "--upgrade", "pyinstaller"]):
            raise SystemExit(
                "PyInstaller 安装失败。\n"
                "常见原因：网络不通、pip 源不可达，或这个解释器没有 pip。\n"
                f"可以手动执行：{sys.executable} -m pip install pyinstaller"
            )


def main() -> None:
    # 先确保解释器能跑 GUI：这一步可能会换一个解释器重跑整个脚本
    ensure_gui_interpreter()

    for path in (ENTRY, ICON, PRESET):
        if not path.exists():
            raise SystemExit(f"缺少 {path.relative_to(HERE)}，请在项目目录里运行。")

    ensure_pyinstaller()

    # 版本号自增放在最前面：它要先改写 model_probe.py，之后读到的才是新版本。
    # `--bump` / `--no-bump` 透传给手动控制（默认按源码变更自动判定）。
    force = "--bump" in sys.argv
    skip = "--no-bump" in sys.argv
    if skip:
        version = current_version()
        print(f"版本号：{version}（--no-bump，保持不动）")
    else:
        version, bumped, reason = vb.bump_if_changed(HERE, force=force)
        if bumped:
            print(f"版本号：{version}（自增：{reason}）")
        else:
            print(f"版本号：{version}（{reason}，不自增）")

    version_file = write_version_file(version)
    print(f"已写入 version_info.txt（exe 属性的「文件版本 / 产品版本」）")

    # 旧进程占着 dist 里的 exe 时，PyInstaller 覆写会失败；没有在跑就当没这回事
    subprocess.call(
        ["taskkill", "/im", APP_NAME + ".exe", "/f"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    code = run([
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean", "--onefile", "--windowed",
        "--name", APP_NAME,
        "--icon", str(ICON),
        "--version-file", str(version_file),
        # 资源统一放到包根的 assets/ 下，`modelkeeper/paths.py` 按这个布局取
        "--add-data", f"{ICON};assets",
        "--add-data", f"{PRESET};assets",
        # 这两个都是**函数内延迟导入**，PyInstaller 的静态分析看不到：
        #   tomllib —— codex_import.parse_toml() 里 import（只在读 Codex 时才走）
        #   yaml    —— agent_import._load_yaml() 里 import（DSH 页面才会走）
        # 漏掉的表现是「打包版打开 Codex 页就报 ModuleNotFoundError，源码版一切正常」。
        "--hidden-import", "tomllib",
        "--hidden-import", "yaml",
        str(ENTRY),
    ])
    if code:
        raise SystemExit("打包失败，看上面的报错。")

    exe = HERE / "dist" / f"{APP_NAME}.exe"
    print(f"\n完成：{exe}（{exe.stat().st_size / 1024 / 1024:.1f} MB，v{version}）")
    print(f"中间文件 build\\ 与 {APP_NAME}.spec 可以删，删了不影响运行。")


if __name__ == "__main__":
    main()
