# -*- coding: utf-8 -*-
"""展期收益率采集（PRD §4.3 → 新表 ``roll_yield``）。

数据源：akshare
- ``get_roll_yield(date='20240102', var='RB')``          按交易日 + 品种
- ``get_roll_yield_bar(date=, symbol1=, symbol2=)``       按具体合约对（本模块不用）

落库表 ``roll_yield``，主键 ``(report_date, symbol, version)``；``symbol`` 统一为
``XXXX888``（主力连续口径，见 PRD §6.3）。``roll_yield`` 列若接口未直接给，则用
``(far_price - near_price) / near_price`` 反算（PRD §4.3 公式）。

解析按列名关键字启发式匹配，部署后在服务器端用少量样本复核过列结构即可全量跑。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from app.core.db import get_engine
from app.core.logging import logger
from sqlalchemy import text

VERSION = "v1.0"
SRC = "akshare:get_roll_yield"


def _num(v) -> float | None:
    if v is None:
        return None
    s = str(v).replace(",", "").replace(" ", "").replace("\u3000", "")
    if s in ("", "-", "—", "nan", "NaN"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _col(row: dict, *keys: str) -> Any:
    """按关键字在行里找首个匹配的列值（大小写不敏感）。"""
    low = {str(k).lower(): k for k in row.keys()}
    for key in keys:
        k = key.lower()
        if k in low:
            return row[low[k]]
    return None


_INSERT = text(
    """
    INSERT INTO roll_yield
        (report_date, symbol, exchange, near_contract, far_contract,
         near_price, far_price, roll_yield, src, version)
    VALUES (:report_date, :symbol, :exchange, :near_contract, :far_contract,
            :near_price, :far_price, :roll_yield, :src, :version)
    ON CONFLICT (report_date, symbol, version) DO UPDATE SET
        exchange     = EXCLUDED.exchange,
        near_contract = EXCLUDED.near_contract,
        far_contract  = EXCLUDED.far_contract,
        near_price    = EXCLUDED.near_price,
        far_price     = EXCLUDED.far_price,
        roll_yield    = EXCLUDED.roll_yield,
        src           = EXCLUDED.src
    """
)


def fetch_roll_yield(date: _dt.date, var: str) -> list[dict]:
    """单交易日单品种展期收益率。返回标准化行列表（可能为空）。"""
    import akshare as ak  # type: ignore

    try:
        df = ak.get_roll_yield(date=date.strftime("%Y%m%d"), var=var)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[roll_yield] {var} {date} 接口失败: {e}")
        return []
    if df is None or len(df) == 0:
        return []
    if hasattr(df, "to_dict"):
        rows = df.to_dict("records")
    else:  # 兜底：dict of columns
        rows = [dict(zip(df.keys(), vals)) for vals in zip(*df.values())]
    out: list[dict] = []
    for r in rows:
        near_p = _num(_col(r, "near_price", "near_contract_price", "near_close"))
        far_p = _num(_col(r, "far_price", "far_contract_price", "far_close"))
        ry = _num(_col(r, "roll_yield", "roll", "roll_yield_rate"))
        if ry is None and near_p and far_p:
            ry = (far_p - near_p) / near_p
        sym_raw = _col(r, "symbol", "var", "variety")
        symbol = f"{str(sym_raw or var).upper().strip('0')}888" if sym_raw else f"{var.upper()}888"
        out.append(
            {
                "report_date": date,
                "symbol": symbol,
                "exchange": None,
                "near_contract": _col(r, "near_contract", "near_symbol"),
                "far_contract": _col(r, "far_contract", "far_symbol"),
                "near_price": near_p,
                "far_price": far_p,
                "roll_yield": ry,
                "src": SRC,
                "version": VERSION,
            }
        )
    return out


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


def run(date: _dt.date, vars_list: list[str]) -> dict:
    """单交易日批量抓取展期收益率。返回 {var: 行数/错误}。"""
    summary: dict[str, Any] = {}
    for var in vars_list:
        try:
            rows = fetch_roll_yield(date, var)
            n = save_rows(rows)
            summary[var] = n
        except Exception as e:  # noqa: BLE001
            summary[var] = f"失败: {type(e).__name__}: {e}"
    return summary


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    d = _dt.date.today()
    if len(sys.argv) > 1:
        d = _dt.datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    print(run(d, ["RB", "CU", "I"]))
