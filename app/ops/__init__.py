# -*- coding: utf-8 -*-
"""运维层（六层解耦 · 应用层）：备份与告警。

整改优先级 P1：
  - P1-1 备份机制（定时 + 异地）——rebuild 大事务前必须有备份
  - P1-2 作业失败 / OOM 告警——三次 OOM 均靠人工 dmesg 排查才发现

容器形态约束（2026-10-02 实测）
-----------------------------
`qhyc-api` / `qhyc-scheduler` 容器内**没有 pg_dump、没有 docker CLI、没有 docker.sock**，
因此"调用 pg_dump 做物理备份"在容器内不可行。本模块采用 psycopg 的
``COPY (...) TO STDOUT`` 流式导出 + gzip，得到**逻辑备份**；宿主侧全量物理备份仍走
``scripts/ops/backup_db.sh``（由宿主 crontab 触发）。两者互补，不冲突。
"""
from __future__ import annotations

from app.ops.alerting import (
    AlertConfig,
    alert_config_from_env,
    check_all,
    install_scheduler_listener,
    send_alert,
)
from app.ops.backup import (
    DEFAULT_BACKUP_TABLES,
    BackupConfig,
    backup_config_from_env,
    run_backup,
    verify_backup,
)

__all__ = [
    "AlertConfig", "alert_config_from_env", "check_all",
    "install_scheduler_listener", "send_alert",
    "BackupConfig", "backup_config_from_env", "run_backup", "verify_backup",
    "DEFAULT_BACKUP_TABLES",
]
