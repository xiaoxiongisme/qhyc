# -*- coding: utf-8 -*-
"""会员持仓前 20 汇总采集（PRD §4.2b → 新表 ``member_position_rank_summary``，P1）。

数据源：akshare
- ``get_rank_sum_daily(start_day, end_day, vars_list)`` → 每合约的 top5/10/15/20 汇总
  （注意：这是「品种内各标的加总」，不是真实品种排名，且**不能**回填逐会员表，见 PRD §4.2）。

落库表 ``member_position_rank_summary``，主键 ``(report_date, symbol, version)``。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from app.core.db import get_engine
from app.core.logging import logger
from sqlalchemy import text

VERSION = "v1.0"
SRC = "akshare:get_rank_sum_daily"

# (db 列, [候选源列名])
_FIELDS = [
    ("vol_top5", "vol_top5"),
    ("vol_chg_top5", "vol_chg_top5"),
    ("long_open_interest_top5", "long_open_interest_top5"),
    ("long_open_interest_chg_top5", "long_open_interest_chg_top5"),
    ("short_open_interest_top5", "short_open_interest_top5"),
    ("short_open_interest_chg_top5", "short_open_interest_chg_top5"),
    ("vol_top10", "vol_top10"),
    ("vol_chg_top10", "vol_chg_top10"),
    ("long_open_interest_top10", "long_open_interest_top10"),
    ("long_open_interest_chg_top10", "long_open_interest_chg_top10"),
    ("short_open_interest_top10", "short_open_interest_top10"),
    ("short_open_interest_chg_top10", "short_open_interest_chg_top10"),
    ("vol_top15", "vol_top15"),
    ("vol_chg_top15", "vol_chg_top15"),
    ("long_open_interest_top15", "long_open_interest_top15"),
    ("long_open_interest_chg_top15", "long_open_interest_chg_top15"),
    ("short_open_interest_top15", "short_open_interest_top15"),
    ("short_open_interest_chg_top15", "short_open_interest_chg_top15"),
    ("vol_top20", "vol_top20"),
    ("vol_chg_top20", "vol_chg_top20"),
    ("long_open_interest_top20", "long_open_interest_top20"),
    ("long_open_interest_chg_top20", "long_open_interest_chg_top20"),
    ("short_open_interest_top20", "short_open_interest_top20"),
    ("short_open_interest_chg_top20", "short_open_interest_chg_top20"),
]


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


def _col(row: dict, *keys: str) -> Any:
    low = {str(k).lower(): k for k in row.keys()}
    for key in keys:
        if key.lower() in low:
            return row[low[key.lower()]]
    return None


_INSERT = text(
    """
    INSERT INTO member_position_rank_summary
        (report_date, symbol, variety,
         vol_top5, vol_chg_top5, long_open_interest_top5, long_open_interest_chg_top5,
         short_open_interest_top5, short_open_interest_chg_top5,
         vol_top10, vol_chg_top10, long_open_interest_top10, long_open_interest_chg_top10,
         short_open_interest_top10, short_open_interest_chg_top10,
         vol_top15, vol_chg_top15, long_open_interest_top15, long_open_interest_chg_top15,
         short_open_interest_top15, short_open_interest_chg_top15,
         vol_top20, vol_chg_top20, long_open_interest_top20, long_open_interest_chg_top20,
         short_open_interest_top20, short_open_interest_chg_top20,
         src, version)
    VALUES (:report_date, :symbol, :variety,
         :vol_top5, :vol_chg_top5, :long_open_interest_top5, :long_open_interest_chg_top5,
         :short_open_interest_top5, :short_open_interest_chg_top5,
         :vol_top10, :vol_chg_top10, :long_open_interest_top10, :long_open_interest_chg_top10,
         :short_open_interest_top10, :short_open_interest_chg_top10,
         :vol_top15, :vol_chg_top15, :long_open_interest_top15, :long_open_interest_chg_top15,
         :short_open_interest_top15, :short_open_interest_chg_top15,
         :vol_top20, :vol_chg_top20, :long_open_interest_top20, :long_open_interest_chg_top20,
         :short_open_interest_top20, :short_open_interest_chg_top20,
         :src, :version)
    ON CONFLICT (report_date, symbol, version) DO UPDATE SET
        variety = EXCLUDED.variety,
        vol_top5 = EXCLUDED.vol_top5, vol_chg_top5 = EXCLUDED.vol_chg_top5,
        long_open_interest_top5 = EXCLUDED.long_open_interest_top5,
        long_open_interest_chg_top5 = EXCLUDED.long_open_interest_chg_top5,
        short_open_interest_top5 = EXCLUDED.short_open_interest_top5,
        short_open_interest_chg_top5 = EXCLUDED.short_open_interest_chg_top5,
        vol_top10 = EXCLUDED.vol_top10, vol_chg_top10 = EXCLUDED.vol_chg_top10,
        long_open_interest_top10 = EXCLUDED.long_open_interest_top10,
        long_open_interest_chg_top10 = EXCLUDED.long_open_interest_chg_top10,
        short_open_interest_top10 = EXCLUDED.short_open_interest_top10,
        short_open_interest_chg_top10 = EXCLUDED.short_open_interest_chg_top10,
        vol_top15 = EXCLUDED.vol_top15, vol_chg_top15 = EXCLUDED.vol_chg_top15,
        long_open_interest_top15 = EXCLUDED.long_open_interest_top15,
        long_open_interest_chg_top15 = EXCLUDED.long_open_interest_chg_top15,
        short_open_interest_top15 = EXCLUDED.short_open_interest_top15,
        short_open_interest_chg_top15 = EXCLUDED.short_open_interest_chg_top15,
        vol_top20 = EXCLUDED.vol_top20, vol_chg_top20 = EXCLUDED.vol_chg_top20,
        long_open_interest_top20 = EXCLUDED.long_open_interest_top20,
        long_open_interest_chg_top20 = EXCLUDED.long_open_interest_chg_top20,
        short_open_interest_top20 = EXCLUDED.short_open_interest_top20,
        short_open_interest_chg_top20 = EXCLUDED.short_open_interest_chg_top20,
        src = EXCLUDED.src
    """
)


def fetch(start: _dt.date, end: _dt.date, vars_list: list[str]) -> list[dict]:
    import akshare as ak  # type: ignore

    try:
        df = ak.get_rank_sum_daily(
            start_day=start.strftime("%Y%m%d"),
            end_day=end.strftime("%Y%m%d"),
            vars_list=vars_list,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[mpr_summary] 接口失败: {e}")
        return []
    if df is None or len(df) == 0:
        return []
    rows = df.to_dict("records") if hasattr(df, "to_dict") else []
    out: list[dict] = []
    for r in rows:
        sym_raw = _col(r, "symbol")
        var_raw = _col(r, "variety", "variety_name")
        if not sym_raw:
            continue
        rec = {
            "report_date": start,  # get_rank_sum_daily 区间通常返回单日快照，取 start
            "symbol": f"{str(sym_raw).upper().strip('0')}888",
            "variety": _str(var_raw),
            "src": SRC,
            "version": VERSION,
        }
        for db_col, src_col in _FIELDS:
            rec[db_col] = _num(_col(r, src_col))
        out.append(rec)
    return out


def _str(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


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


def run(start: _dt.date, end: _dt.date, vars_list: list[str]) -> dict:
    rows = fetch(start, end, vars_list)
    n = save_rows(rows)
    return {"rows": n}


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    d = _dt.date.today()
    print(run(d, d, ["RB", "CU", "I"]))
