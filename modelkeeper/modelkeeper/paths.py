#!/usr/bin/env python3
"""资源目录：把「源码运行」与「PyInstaller 单文件运行」的差异收敛到一处。

单文件版运行时 `__file__` 指向 `%TEMP%\\_MEIxxxx` 解包目录，与源码布局不同；
打包时 `build_exe.py` 把资源放到包根的 `assets/`，因此两条路径只需在这里分叉。
"""

from __future__ import annotations

import sys
from pathlib import Path

# PyInstaller 单文件模式下资源解包到 `_MEIPASS/assets`
_BUNDLE_SUBDIR = "assets"


def asset_dir() -> Path:
    """随程序分发的只读资源目录（图标、出厂价格表）。"""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled) / _BUNDLE_SUBDIR
    return Path(__file__).resolve().parent / _BUNDLE_SUBDIR


def project_root() -> Path:
    """项目根目录：源码运行时为 `modelkeeper/` 的上一级。"""
    return Path(__file__).resolve().parents[1]
