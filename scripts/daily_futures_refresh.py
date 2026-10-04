# -*- coding: utf-8 -*-
"""每日期货刷新编排（由 crontab 收盘后调用）：

  ① 采集/更新合约日频价 contract_daily（经 spot_basis 的 dominant/near 合约，sina/天勤）
  ② 由 spot_basis.dominant_contract 派生主力合约映射 main_contract_map（全品种）

两步相互独立：任一步失败不影响另一步，错误记入日志。

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
    for name, fn in (("contract_daily", step_contract_daily),
                     ("main_contract_map", step_main_contract_map)):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            logger.error(f"[daily_refresh] 步骤 {name} 失败: {e}\n{traceback.format_exc()}")
    logger.info("[daily_refresh] 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
