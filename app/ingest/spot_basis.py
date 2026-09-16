"""§18.5（v1.3.2）M6a：基差因子采集器

数据源：akshare
- futures_spot_price_daily(start_day, end_day, vars_list) → DataFrame 13 列
- futures_spot_price(date, vars_list) → 同结构（单日查询）

字段：date, symbol, spot_price, near_contract, near_contract_price,
       dominant_contract, dominant_contract_price, near_month, dominant_month,
       near_basis, dom_basis, near_basis_rate, dom_basis_rate

因子（v1.3.2 §18.5 新增）：
- dom_basis_rate: 主力月基差率 %（基差信号核心）
- basis_change_5d: 5 日基差变化（趋势）
- carry_signal: (dom_basis - near_basis) / 跨月价差
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from app.core.logging import logger
from app.models import SpotBasis


def _to_date(s) -> date | None:
    try:
        return pd.to_datetime(s).date()
    except Exception:
        return None


def _to_dec(v) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _to_str(v) -> str | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    return str(v).strip() or None


def fetch_spot_basis(
    start_day: date,
    end_day: date,
    products: list[str] | None = None,
) -> list[dict]:
    """akshare futures_spot_price_daily 拉取区间内全部品种基差

    products: 品种列表（如 ["c", "a", "m"]）；None 时用接口默认全品种
    返回：标准化行列表
    """
    import akshare as ak  # type: ignore

    args = {
        "start_day": start_day.strftime("%Y%m%d"),
        "end_day": end_day.strftime("%Y%m%d"),
    }
    if products:
        args["vars_list"] = products
    try:
        df = ak.futures_spot_price_daily(**args)
    except Exception as e:
        logger.warning(f"[spot_basis] 接口失败: {e}")
        return []
    if df is None or df.empty:
        return []
    rows: list[dict] = []
    for _, r in df.iterrows():
        d = _to_date(r.get("date"))
        if d is None:
            continue
        sym = _to_str(r.get("symbol"))
        if not sym:
            continue
        rows.append(
            {
                "report_date": d,
                "exchange": "auto",
                "symbol": sym.upper(),
                "spot_price": _to_dec(r.get("spot_price")),
                "near_contract": _to_str(r.get("near_contract")),
                "near_contract_price": _to_dec(r.get("near_contract_price")),
                "dominant_contract": _to_str(r.get("dominant_contract")),
                "dominant_contract_price": _to_dec(r.get("dominant_contract_price")),
                "near_month": _to_str(r.get("near_month")),
                "dominant_month": _to_str(r.get("dominant_month")),
                "near_basis": _to_dec(r.get("near_basis")),
                "dom_basis": _to_dec(r.get("dom_basis")),
                "near_basis_rate": _to_dec(r.get("near_basis_rate")),
                "dom_basis_rate": _to_dec(r.get("dom_basis_rate")),
                "src": "akshare:futures_spot_price_daily",
                "version": "v1.0",
            }
        )
    return rows


def upsert_spot_basis(session, rows: list[dict]) -> int:
    """批量 upsert（PK: report_date+symbol+version）"""
    if not rows:
        return 0
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    seen = {}
    for r in rows:
        k = (r["report_date"], r["symbol"], r["version"])
        seen[k] = r
    rows = list(seen.values())

    stmt = pg_insert(SpotBasis).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["report_date", "symbol", "version"],
        set_={
            "exchange": stmt.excluded.exchange,
            "spot_price": stmt.excluded.spot_price,
            "near_contract": stmt.excluded.near_contract,
            "near_contract_price": stmt.excluded.near_contract_price,
            "dominant_contract": stmt.excluded.dominant_contract,
            "dominant_contract_price": stmt.excluded.dominant_contract_price,
            "near_month": stmt.excluded.near_month,
            "dominant_month": stmt.excluded.dominant_month,
            "near_basis": stmt.excluded.near_basis,
            "dom_basis": stmt.excluded.dom_basis,
            "near_basis_rate": stmt.excluded.near_basis_rate,
            "dom_basis_rate": stmt.excluded.dom_basis_rate,
            "src": stmt.excluded.src,
        },
    )
    session.execute(stmt)
    return len(rows)


def collect_spot_basis(
    session,
    start_day: date,
    end_day: date,
    products: list[str] | None = None,
) -> dict[str, Any]:
    """采集区间内基差数据并入库"""
    rows = fetch_spot_basis(start_day, end_day, products=products)
    n = upsert_spot_basis(session, rows)
    session.commit()
    return {
        "start": start_day.isoformat(),
        "end": end_day.isoformat(),
        "products_requested": len(products) if products else "default",
        "rows": n,
    }
