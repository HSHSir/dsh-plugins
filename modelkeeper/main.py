#!/usr/bin/env python3
"""模型管家 · 启动入口。

为什么入口留在项目根目录，而不是放进 `modelkeeper/` 包内：
PyInstaller 只对入口脚本做静态分析。入口 `import` 必须在**顶层**，依赖关系才能被
看到；包内的延迟导入（例如 `model_probe.main()` 里的 `run`）会被漏掉，界面层会
整个不进包。所以入口固定为 `main.py`，且第一条业务语句就是 `from modelkeeper.gui_app import run`。

启动方式：
    python main.py              # 有控制台，方便看报错
    python -m modelkeeper       # 等价入口（需在项目根执行）
    pythonw main.py             # 无控制台（见 启动模型管家.bat）
"""

from __future__ import annotations

import sys
from pathlib import Path

# 允许从任意工作目录启动：把项目根目录补到 sys.path 最前面
_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from modelkeeper.gui_app import run  # noqa: E402 - 必须在 sys.path 调整之后

if __name__ == "__main__":
    run()
