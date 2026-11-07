# -*- coding: utf-8 -*-
"""Verify inventory backfill result on cloud."""
from app.core.db import session_scope
from sqlalchemy import text

with session_scope() as s:
    r = s.execute(text("SELECT max(report_date), count(distinct symbol) FROM inventory")).fetchone()
    print("inventory max(report_date)=", r[0], " distinct_symbol=", r[1])
    # per latest day distribution
    rows = s.execute(text(
        "SELECT report_date, count(*) FROM inventory GROUP BY 1 ORDER BY 1 DESC LIMIT 5")).fetchall()
    print("  recent report_date distribution:")
    for x in rows:
        print("    ", x)
    rows = s.execute(text(
        "SELECT id, status, started_at, message FROM task_run "
        "WHERE task_type='inventory' ORDER BY started_at DESC LIMIT 3")).fetchall()
    print("task_run inventory recent:")
    for x in rows:
        print("    ", x)
    r2 = s.execute(text("SELECT max(report_date) FROM warehouse_receipt")).fetchone()
    print("warehouse_receipt max(report_date)=", r2[0])
