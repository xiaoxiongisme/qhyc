"""M8 · 决策链路容器化（PRD `docs/M8_决策链路容器化_PRD_20260922.md`）。

把 WB 侧已跑通的「多空简报 / 研究层 / 基本面 / 盘中监控 / 信号推送」五条子链路
迁入 Docker 常驻执行（`qhyc-pipeline` 容器）。

设计要点（照 PRD §5）：
- **源码单一真源在 WB skill 侧**：容器只读挂载 `/app/pipeline_src`，启动时复制为
  容器内快照 `/app/runtime/pipeline_snapshot`，子进程一律跑快照（§5.3）。
- **子进程模型**：`subprocess` 调 `run_pipeline.py`，不做 `import`（§5.4）。
- **不改一行策略口径**：本模块只负责编排/留痕/告警，绝不触碰策略参数（§12）。
"""
from __future__ import annotations

__all__ = ["config", "snapshots", "readiness", "recorder", "healthcheck", "worker"]
