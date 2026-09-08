"""
推断日历生成器（R2，PRD §15.R2 决策）

M1 期间 trade_calendar 无官方来源时的降级方案：
- 品种间交叉：当日 ≥80% 的主连品种有数据 → 视为交易日
- 推断日历与官方日历**分列存储**：exchange='INF'（官方日历 M2 接入后写真实交易所代码）
- note 标注推断方式与覆盖率，看板据此显示"日历=推断模式"
- M2 接入官方日历后全量重跑缺失检测 + tqsdk 补缺
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import logger
from app.repositories.calendar_repo import CalendarRepository

INF_EXCHANGE = "INF"  # 推断日历专用 exchange 代码（与官方分列）


def infer_calendar(session: Session, min_ratio: float = 0.8) -> dict:
    """基于 daily_bar 品种间交叉推断交易日历

    返回 {inferred_days, min_ratio, covered_days} 统计。
    """
    settings = get_settings()
    target_n = len(settings.main_contracts)
    threshold = max(2, int(target_n * min_ratio))

    rows = session.execute(
        text(
            """
            SELECT trade_date, count(DISTINCT symbol) AS n
            FROM daily_bar
            WHERE symbol LIKE '%888'
              AND trade_date >= :start
            GROUP BY trade_date
            HAVING count(DISTINCT symbol) >= :threshold
            ORDER BY trade_date
            """
        ),
        {"start": settings.history_start, "threshold": threshold},
    ).all()

    open_days = [r[0] for r in rows]
    if not open_days:
        logger.warning("[calendar] 无可推断交易日（数据不足）")
        return {"inferred_days": 0, "min_ratio": min_ratio, "threshold": threshold}

    cal_rows = [
        {
            "exchange": INF_EXCHANGE,
            "trade_date": d,
            "is_open": True,
            "sessions": None,
            "note": f"inferred: >= {threshold}/{target_n} products ({min_ratio:.0%})",
        }
        for d in open_days
    ]
    n = CalendarRepository(session).upsert(cal_rows)
    session.commit()
    logger.info(
        f"[calendar] 推断日历写入 {n} 天（{open_days[0]} ~ {open_days[-1]}，"
        f"阈值 {threshold}/{target_n} 品种）"
    )
    return {
        "inferred_days": n,
        "min_ratio": min_ratio,
        "threshold": threshold,
        "range": [open_days[0].isoformat(), open_days[-1].isoformat()],
    }


def calendar_mode(session: Session) -> str:
    """当前日历模式：official（有官方数据）/ inferred（推断）/ empty"""
    from app.models import TradeCalendar

    has_official = session.execute(
        text(
            "SELECT count(*) FROM trade_calendar WHERE exchange <> :e AND is_open = TRUE"
        ),
        {"e": INF_EXCHANGE},
    ).scalar()
    if (has_official or 0) > 0:
        return "official"
    has_inferred = session.execute(
        text("SELECT count(*) FROM trade_calendar WHERE exchange = :e"),
        {"e": INF_EXCHANGE},
    ).scalar()
    return "inferred" if (has_inferred or 0) > 0 else "empty"