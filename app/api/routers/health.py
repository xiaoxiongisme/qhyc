"""健康检查 + 数据就绪门控（R3①）"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from fastapi import APIRouter
from sqlalchemy import text

from app import __version__
from app.core.config import get_settings
from app.core.db import get_engine
from app.core.logging import logger

router = APIRouter()


def _readiness() -> dict:
    """R3① 数据就绪门控：
    - ready_ratio：有数据的主连品种数 / 目标品种数
    - max_lag_days：全市场最新数据日期距今天数
    未就绪时 /predict（M2）应拒绝计算，避免基于不完整数据静默算错。
    """
    settings = get_settings()
    target = len(settings.main_contracts)
    try:
        with get_engine().connect() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT count(DISTINCT symbol),
                           max(maxd)
                    FROM (
                        SELECT symbol, max(trade_date) AS maxd
                        FROM daily_bar
                        WHERE symbol LIKE '%888'
                        GROUP BY symbol
                    ) t
                    """
                )
            ).one()
            ready_symbols, latest = int(row[0] or 0), row[1]
            lag_days = (
                (date.today() - latest).days if latest else None
            )
        ratio = ready_symbols / target if target else 0.0
        ready = (
            ratio >= settings.yaml.readiness.ready_ratio
            and lag_days is not None
            and lag_days <= settings.yaml.readiness.max_lag_days
        )
        return {
            "target_symbols": target,
            "ready_symbols": ready_symbols,
            "ready_ratio": round(ratio, 4),
            "latest_data_date": latest.isoformat() if latest else None,
            "lag_days": lag_days,
            "ready": ready,
            "gate": {
                "ready_ratio_min": settings.yaml.readiness.ready_ratio,
                "max_lag_days": settings.yaml.readiness.max_lag_days,
            },
        }
    except Exception as e:
        logger.warning(f"[health] readiness 查询失败: {e}")
        return {"ready": False, "error": str(e)}


@router.get("/health")
def health() -> dict:
    """健康检查：DB 连通 + 版本 + 时间 + 数据就绪状态（R3①）"""
    info: dict = {
        "status": "ok",
        "version": __version__,
        "milestone": "M1",
        "ts": datetime.utcnow().isoformat() + "Z",
    }
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        info["db"] = "ok"
    except Exception as e:
        info["status"] = "degraded"
        info["db"] = f"fail: {e}"
        logger.warning(f"[health] db fail {e}")
        return info

    rd = _readiness()
    info["readiness"] = rd
    if not rd.get("ready"):
        info["status"] = "ingesting"  # 数据就绪门控未通过
    return info
