# 一次性研究探针归档（2026-11-07）

本目录 10 个 `_*.py` 由 `scripts/wb_backtest/` 经 `git mv` 迁入，**未删除、仍在版本库内、可完整回滚**。

迁入判据（全部同时满足）：
- git tracked 且工作树 CLEAN（不属于 130 个未提交热修）；
- 后缀法精确引用扫描与不限后缀全量 token 扫描均零命中；
- 独立 `grep -rFIn` 反证零命中，无 `wb_backtest._` 模块式导入；
- crontab / systemd / bash_history / journal / `ops.scheduled_task` 无注册；
- 内容为一次性研究诊断（R2/滚动品种池/S1b 分层等），非生产链路。

保留而非删除的原因：`.gitignore` 的 `scripts/_*.py` 规则对**已 tracked 文件无效**，
删除会产生 10 个 `D` 并触发 `check_git_prod_sync.py` 的 C3 告警；`git mv` 可在保持
「生产 = GitHub」一致性的前提下完成目录收敛。

复原：`git mv scripts/archive/deprecated_20261107/<name>.py scripts/wb_backtest/<name>.py`。
