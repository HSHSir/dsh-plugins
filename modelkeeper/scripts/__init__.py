#!/usr/bin/env python3
"""开发与发布工具集（不随 exe 分发）。

    build_exe.py        一键打包：版本自增 → version_info.txt → PyInstaller
    version_bump.py     版本号规则与源码指纹（被 build_exe 与测试复用）
    fetch_prices.py     刷新出厂价格表 modelkeeper/assets/prices_preset.json
    rebuild_icon.py     重绘图标各尺寸图层
    score_lab.py        打分公式核算（只读，调 smart_route.rank）
    diag_speed.py       测速诊断
    verify_codex_live.py / verify_exe_codex.py   Codex 适配的端到端校验
    shot_states.py / shot_missing.py             界面截图

这些脚本都以项目根为基准定位文件，可从任意工作目录直接运行：
    py -3.14 scripts/build_exe.py
"""
