# -*- coding: utf-8 -*-
"""仓单日报采集（PRD §4.4 → 新表 ``warehouse_receipt``）。

数据源：akshare（按交易日逐日取，接口只接受单日）
- ``futures_warehouse_receipt_czce(date)``   郑商所
- ``futures_warehouse_receipt_dce(date)``     大商所
- ``futures_shfe_warehouse_receipt(date)``    上期所
- ``futures_gfex_warehouse_receipt(date)``    广期所

落库表 ``warehouse_receipt``，主键 ``(report_date, exchange, symbol, warehouse)``。
解析按列名关键字启发式匹配（仓库 / 仓单数量 / 增减 / 单位 / 品种），部署后少量样本复核。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from app.core.db import get_engine
from app.core.logging import logger
from sqlalchemy import text

VERSION = "v1.0"
SRC = "akshare:warehouse_receipt"


def _num(v) -> int | None:
    if v is None:
        return None
    s = str(v).replace(",", "").replace(" ", "").replace("\u3000", "")
    if s in ("", "-", "—", "nan", "NaN"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _str(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _col(row: dict, *keys: str) -> Any:
    low = {str(k).lower(): k for k in row.keys()}
    for key in keys:
        if key.lower() in low:
            return row[low[key.lower()]]
    return None


_INSERT = text(
    """
    INSERT INTO warehouse_receipt
        (report_date, exchange, symbol, warehouse, receipt_qty, change_qty, unit, src, version)
    VALUES (:report_date, :exchange, :symbol, :warehouse, :receipt_qty, :change_qty, :unit, :src, :version)
    ON CONFLICT (report_date, exchange, symbol, warehouse) DO UPDATE SET
        receipt_qty = EXCLUDED.receipt_qty,
        change_qty  = EXCLUDED.change_qty,
        unit        = EXCLUDED.unit,
        src         = EXCLUDED.src
    """
)


def _parse_df(df, date: _dt.date, exchange: str) -> list[dict]:
    if df is None or len(df) == 0:
        return []
    rows = df.to_dict("records") if hasattr(df, "to_dict") else []
    out: list[dict] = []
    for r in rows:
        wh = _str(_col(r, "warehouse", "交割仓库", "仓库", "warehouse_name"))
        if not wh:
            continue
        sym_raw = _str(_col(r, "symbol", "variety", "品种", "product"))
        symbol = f"{str(sym_raw).upper().strip('0')}888" if sym_raw else None
        if not symbol:
            continue
        out.append(
            {
                "report_date": date,
                "exchange": exchange,
                "symbol": symbol,
                "warehouse": wh,
                "receipt_qty": _num(_col(r, "receipt_qty", "仓单数量", "仓单", "qty", "receipt")),
                "change_qty": _num(_col(r, "change_qty", "增减", "变化", "change", "delta")),
                "unit": _str(_col(r, "unit", "单位")),
                "src": SRC,
                "version": VERSION,
            }
        )
    return out


def _fetch_one(date: _dt.date, exchange: str) -> list[dict]:
    import akshare as ak  # type: ignore

    fn = {
        "CZCE": ak.futures_warehouse_receipt_czce,
        "DCE": ak.futures_warehouse_receipt_dce,
        "SHFE": ak.futures_shfe_warehouse_receipt,
        "GFEX": ak.futures_gfex_warehouse_receipt,
    }.get(exchange)
    if fn is None:
        return []
    try:
        df = fn(date=date.strftime("%Y%m%d"))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[warehouse_receipt] {exchange} {date} 接口失败: {e}")
        return []
    return _parse_df(df, date, exchange)


def save_rows(rows: list[dict]) -> int:
    if not rows:
        return 0
    eng = get_engine()
    n = 0
    with eng.begin() as conn:
        for r in rows:
            conn.execute(_INSERT, r)
            n += 1
    return n


def run(date: _dt.date, exchanges: list[str] | None = None) -> dict:
    """单交易日批量抓取仓单。返回 {exchange: 行数/错误}。"""
    exchanges = exchanges or ["CZCE", "DCE", "SHFE", "GFEX"]
    summary: dict[str, Any] = {}
    for ex in exchanges:
        try:
            rows = _fetch_one(date, ex)
            n = save_rows(rows)
            summary[ex] = n
        except Exception as e:  # noqa: BLE001
            summary[ex] = f"失败: {type(e).__name__}: {e}"
    return summary


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    d = _dt.date.today()
    if len(sys.argv) > 1:
        d = _dt.datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    print(run(d))
