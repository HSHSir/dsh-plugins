#!/usr/bin/env python3
"""模型管家（ModelKeeper）应用包。

模块分工（自上而下，依赖只允许向下）：

    gui_app        界面层：tkinter 窗口、各 Agent 管理页、对话框
    agents         Agent 注册表：新增一个 Agent 只改这里
    integrations   agent_import / claude_import / codex_import：目标配置的读写
    core           model_probe / smart_route / price_book / endpoint_book / model_spec / http_json
    paths          资源定位（源码运行与 PyInstaller 单文件运行的差异只在这里）

版本号的唯一来源是 `model_probe.APP_VERSION`，`version_info.txt` 与窗口标题都由它派生。
"""

from __future__ import annotations

__all__ = ["__version__"]


def __getattr__(name: str) -> str:
    """`modelkeeper.__version__` 按需读取，避免导入包时就拉起整个网络层。"""
    if name == "__version__":
        from .model_probe import APP_VERSION

        return APP_VERSION
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
