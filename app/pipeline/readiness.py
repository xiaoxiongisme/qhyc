"""数据就绪闸门（PRD §6.2）。

`qhyc-scheduler` 的 rank_position（会员持仓入库）在 17:30；决策链路步骤①轻基本面
要读 `member_position_rank`。因此日链改到 17:40，并在开跑前轮询等待当日
`spot_basis` 与 `member_position_rank` 有数：

- 每 `poll_sec` 秒一次，最多等 `timeout_min` 分钟
- 超时仍不满足 → 返回 False，**照常往下跑**（轻基本面该品标 N/A），并告警
"""
from __future__ import annotations

import time

from sqlalchemy import func, select

from app.core.logging import logger


def _count_today(session, model, date_col) -> int:
    from datetime import date

    today = date.today()
    try:
        n = session.execute(
            select(func.count()).select_from(model).where(date_col == today)
        ).scalar_one()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[pipeline] readiness 查询 {model.__tablename__} 失败: {e}")
        return 0
    return int(n or 0)


def wait_ready(timeout_min: int = 10, poll_sec: int = 60) -> bool:
    """等待当日 spot_basis 与 member_position_rank 均有数。"""
    from app.core.db import session_scope
    from app.models import MemberPositionRank, SpotBasis

    deadline = time.time() + max(0, int(timeout_min)) * 60
    poll = max(5, int(poll_sec))
    attempt = 0
    while True:
        attempt += 1
        with session_scope() as s:
            n_spot = _count_today(s, SpotBasis, SpotBasis.report_date)
            n_rank = _count_today(s, MemberPositionRank, MemberPositionRank.trade_date)
        ok = n_spot > 0 and n_rank > 0
        logger.info(
            f"[pipeline] readiness #{attempt} spot_basis={n_spot} member_position_rank={n_rank} -> {ok}"
        )
        if ok:
            return True
        if time.time() >= deadline:
            logger.warning("[pipeline] readiness 超时：数据未就绪，按降级策略继续跑")
            return False
        time.sleep(poll)
