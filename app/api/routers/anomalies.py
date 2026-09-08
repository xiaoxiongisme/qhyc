"""/anomalies - 异常工单列表与裁决"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import fastapi_db_dep
from app.models import AnomalyTicket, DailyBar
from app.schemas.anomaly import AnomalyOut

router = APIRouter()


@router.get("", response_model=list[AnomalyOut])
def list_anomalies(
    status: str = "pending",
    symbol: Optional[str] = None,
    limit: int = 200,
    db: Session = Depends(fastapi_db_dep),
):
    stmt = select(AnomalyTicket).order_by(AnomalyTicket.id.desc()).limit(limit)
    if status != "all":
        stmt = stmt.where(AnomalyTicket.status == status)
    if symbol:
        stmt = stmt.where(AnomalyTicket.symbol == symbol)
    rows = db.execute(stmt).scalars().all()
    return [_to_out(r) for r in rows]


class ResolveRequest(BaseModel):
    action: str  # accept_tqsdk / false_positive
    note: str | None = None


@router.post("/{ticket_id}/resolve")
def resolve_anomaly(ticket_id: int, req: ResolveRequest, db: Session = Depends(fastapi_db_dep)):
    """⑰ 异常处置：只允许采纳 tqsdk 或标记误报，不手改数值"""
    ticket = db.get(AnomalyTicket, ticket_id)
    if not ticket:
        raise HTTPException(404, f"ticket {ticket_id} not found")
    if ticket.status != "pending":
        raise HTTPException(400, f"ticket already {ticket.status}")

    if req.action == "accept_tqsdk":
        # 用 tqsdk 的值覆盖 daily_bar 对应字段（⑰ 唯一允许的自动覆盖路径）
        if ticket.tqsdk_val is None:
            raise HTTPException(400, "tqsdk_val is null, cannot accept")
        row = db.get(DailyBar, (ticket.symbol, ticket.trade_date))
        if not row:
            raise HTTPException(404, f"daily_bar ({ticket.symbol}, {ticket.trade_date}) not found")
        if ticket.field == "close":
            row.close = ticket.tqsdk_val
        elif ticket.field == "settle":
            row.settle = ticket.tqsdk_val
        elif ticket.field == "volume":
            row.volume = int(ticket.tqsdk_val)
        else:
            raise HTTPException(400, f"unsupported field {ticket.field}")
        row.src = "tqsdk"
        ticket.status = "fixed"
        ticket.resolved_at = datetime.utcnow()
        ticket.note = (ticket.note or "") + f" | resolved: accept_tqsdk ({req.note or ''})"
    elif req.action == "false_positive":
        ticket.status = "false_positive"
        ticket.resolved_at = datetime.utcnow()
        ticket.note = (ticket.note or "") + f" | resolved: false_positive ({req.note or ''})"
    else:
        raise HTTPException(400, f"unknown action {req.action}")

    db.commit()
    return {"status": "ok", "ticket_id": ticket_id, "new_status": ticket.status}


def _to_out(t: AnomalyTicket) -> AnomalyOut:
    return AnomalyOut(
        id=t.id,
        symbol=t.symbol,
        trade_date=t.trade_date,
        field=t.field,
        akshare_val=float(t.akshare_val) if t.akshare_val is not None else None,
        tqsdk_val=float(t.tqsdk_val) if t.tqsdk_val is not None else None,
        diff=float(t.diff) if t.diff is not None else None,
        threshold=float(t.threshold) if t.threshold is not None else None,
        status=t.status,
        note=t.note,
        created_at=t.created_at,
        resolved_at=t.resolved_at,
    )