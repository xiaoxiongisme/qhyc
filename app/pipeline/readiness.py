"""数据就绪闸门（PRD §6.2）。

`qhyc-scheduler` 的 rank_position（会员持仓入库）在 17:30；决策链路步骤①轻基本面
要读 `member_position_rank`。因此日链改到 17:40，并在开跑前轮询等待当日
`spot_basis` 与 `member_position_rank` 有数：

- 每 `poll_sec` 秒一次，最多等 `timeout_min` 分钟
- 超时仍不满足 → 返回 False，**照常往下跑**（轻基本面该品标 N/A），并告警

⚠ 期望日取**最近一个有日线的交易日**（`max(daily_bar.trade_date)`），不是
`date.today()`。原实现按 today 判定，而周末/节假日两张依赖表当日永远无行 →
必然空等 `timeout_min` 分钟、然后推一条「数据未就绪」的降级告警（2026-10-04~07
国庆、10-10/11、10-17/18 均命中）。非交易日消费的本来就是最近交易日的数据，
以最近交易日为口径才是对的；当日确有日线（真交易日）时行为与原先完全一致：
仍按当日严格判定并轮询等待。
"""
from __future__ import annotations

import time


def _max_bar_date(session):
    """最近一个有日线的交易日；查询失败返回 None（退回 today 口径）。"""
    from sqlalchemy import func, select

    from app.core.logging import logger
    from app.models import DailyBar

    try:
        return session.execute(select(func.max(DailyBar.trade_date))).scalar()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[pipeline] readiness 查询 daily_bar 最近日失败: {e}")
        return None


def _count_on(session, model, date_col, d) -> int:
    from sqlalchemy import func, select

    from app.core.logging import logger

    try:
        n = session.execute(
            select(func.count()).select_from(model).where(date_col == d)
        ).scalar_one()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[pipeline] readiness 查询 {model.__tablename__} 失败: {e}")
        return 0
    return int(n or 0)


def wait_ready(timeout_min: int = 10, poll_sec: int = 60) -> bool:
    """等待最近交易日的 spot_basis 与 member_position_rank 均有数。"""
    from datetime import date

    from app.core.db import session_scope
    from app.core.logging import logger
    from app.models import MemberPositionRank, SpotBasis

    deadline = time.time() + max(0, int(timeout_min)) * 60
    poll = max(5, int(poll_sec))
    attempt = 0
    today = date.today()
    while True:
        attempt += 1
        with session_scope() as s:
            target = _max_bar_date(s) or today
            n_spot = _count_on(s, SpotBasis, SpotBasis.report_date, target)
            n_rank = _count_on(
                s, MemberPositionRank, MemberPositionRank.trade_date, target
            )
        ok = n_spot > 0 and n_rank > 0
        tag = "" if target == today else "(当日无日线/非交易日，按最近交易日判定)"
        logger.info(
            f"[pipeline] readiness #{attempt} target={target}{tag} "
            f"spot_basis={n_spot} member_position_rank={n_rank} -> {ok}"
        )
        if ok:
            return True
        if target < today:
            # 当日无日线 ⇒ 非交易日（或当日日线尚未落库）。再等也不会出现当日数据，
            # 立即降级返回，避免每个周末空等 timeout_min 分钟并推假告警。
            logger.warning(
                f"[pipeline] readiness 未就绪：最近交易日 {target} 缺数"
                f"（spot_basis={n_spot} member_position_rank={n_rank}），按降级策略继续跑"
            )
            return False
        if time.time() >= deadline:
            logger.warning("[pipeline] readiness 超时：数据未就绪，按降级策略继续跑")
            return False
        time.sleep(poll)
