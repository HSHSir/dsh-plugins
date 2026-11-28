#!/usr/bin/env python3
"""`python -m modelkeeper` 入口。

等价于 `python main.py`。区别只在启动方式：`-m` 需要项目根目录在 `sys.path` 上
（在项目根执行即可），适合脚本化启动；`main.py` 会自己把根目录补进 `sys.path`，
双击或在任意目录下都能跑。
"""

from __future__ import annotations

from .gui_app import run

if __name__ == "__main__":
    run()
