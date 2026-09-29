"""pytest 根配置（放在仓库根目录，pytest 会自动加载）。

作用：把 ``scripts/`` 加入 ``sys.path``，使 tests 可直接 import 其中的脚本级模块。

背景（2026-09-29 实测）：``tests/test_lookahead_guard.py`` 以顶层 import 方式引用
``compute_factor_v1v6``（位于 ``scripts/``，它是脚本而非 ``app`` 包内模块）。
未补 sys.path 时：

    python -m pytest tests/
    → ModuleNotFoundError: No module named 'compute_factor_v1v6'
    → Interrupted: 1 error during collection      # 注意：是**整轮 0 用例运行**，不是跳过

补上后同一条命令应全绿。容器内的等价路径为 ``/app/scripts``（Dockerfile COPY scripts）。
"""
from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent

for _p in (_ROOT, _ROOT / "scripts"):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)
