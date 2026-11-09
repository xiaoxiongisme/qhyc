# -*- coding: utf-8 -*-
"""每日期货刷新编排（由 crontab 收盘后调用）：

  ① 采集/更新合约日频价 contract_daily（经 spot_basis 的 dominant/near 合约，sina/天勤）
  ② 由 spot_basis.dominant_contract 派生主力合约映射 main_contract_map（全品种）

两步相互独立：任一步失败不影响另一步，错误记入日志；但**整轮有失败必须抛异常**。
⚠ CL-20261109-21：旧实现只 logger.error 后 `return 0`，调度器按「作业函数有没有抛异常」
记 success（apscheduler EVENT_JOB_EXECUTED），于是
`ModuleNotFoundError: refresh_main_contract_map` 连续多日被吞、流水全绿，
main_contract_map 静默停更到 11-06 才由数据新鲜度告警发现。
现在把失败汇总成 RuntimeError 冒泡 → EVENT_JOB_ERROR + 流水 failed + 即时告警。

用法（容器内，scripts/ 已挂载）
  docker exec -w /app -e PYTHONPATH=/app qhyc-scheduler python scripts/daily_futures_refresh.py
"""
from __future__ import annotations

import sys
import traceback

from app.core.logging import logger


def step_contract_daily() -> None:
    from app.core.db import session_scope
    from app.ingest.contract_bars import collect_contract_bars

    logger.info("[daily_refresh] ① contract_daily 采集开始")
    with session_scope() as s:
        stats = collect_contract_bars(s, products=None, days=90)
    logger.info(f"[daily_refresh] ① contract_daily 完成: {stats}")


def step_main_contract_map() -> None:
    # 脚本自身目录在 sys.path[0]，可直接 import 同目录模块
    import refresh_main_contract_map as mcm

    logger.info("[daily_refresh] ② main_contract_map 派生开始")
    r = mcm.refresh()  # 增量：仅 main_contract_map 当前最大日之后
    logger.info(f"[daily_refresh] ② main_contract_map 完成: {r}")


def main() -> int:
    failures: list[str] = []
    for name, fn in (("contract_daily", step_contract_daily),
                     ("main_contract_map", step_main_contract_map)):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            logger.error(f"[daily_refresh] 步骤 {name} 失败: {e}\n{traceback.format_exc()}")
            failures.append(f"{name}: {type(e).__name__}: {e}")
    if failures:
        # 不许静默收尾：调度器直接调用本函数，抛异常才会触发 EVENT_JOB_ERROR，
        # 让 ops.scheduled_task_run 记 failed 而非 success（见模块文档）。
        raise RuntimeError("[daily_refresh] 本轮有步骤失败：" + "; ".join(failures))
    logger.info("[daily_refresh] 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
