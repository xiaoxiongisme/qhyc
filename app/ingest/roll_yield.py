# -*- coding: utf-8 -*-
"""展期收益率采集（PRD §4.3 → 新表 ``roll_yield``）。

数据源：akshare
- ``get_roll_yield(date='20240102', var='RB')`` 返回 **元组** ``(roll_yield, near_contract, far_contract)``
  （非 DataFrame，实测确认），故解析按元组处理；near/far 价格接口需 JS 不支持，留 NULL。

落库表 ``roll_yield``，主键 ``(report_date, symbol, version)``；``symbol`` 统一为
``XXXX888``（主力连续口径，见 PRD §6.3）。
"""
from __future__ import annotations

import datetime as _dt
import time
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


_INSERT = text(
    """
    INSERT INTO roll_yield
        (report_date, symbol, exchange, near_contract, far_contract,
         near_price, far_price, roll_yield, src, version)
    VALUES (:report_date, :symbol, :exchange, :near_contract, :far_contract,
            :near_price, :far_price, :roll_yield, :src, :version)
    ON CONFLICT (report_date, symbol, version) DO UPDATE SET
        exchange      = EXCLUDED.exchange,
        near_contract = EXCLUDED.near_contract,
        far_contract  = EXCLUDED.far_contract,
        near_price    = EXCLUDED.near_price,
        far_price     = EXCLUDED.far_price,
        roll_yield    = EXCLUDED.roll_yield,
        src           = EXCLUDED.src
    """
)


def fetch_roll_yield(date: _dt.date, var: str, retries: int = 2) -> list[dict]:
    """单交易日单品种展期收益率。返回标准化行列表（可能为空）。"""
    import akshare as ak  # type: ignore

    last_err = None
    for attempt in range(retries):
        try:
            res = ak.get_roll_yield(date=date.strftime("%Y%m%d"), var=var)
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1.0)
            continue
        # 实测返回形态: (roll_yield, near_contract, far_contract)
        if isinstance(res, (tuple, list)) and len(res) >= 3:
            return [{
                "report_date": date,
                "symbol": f"{var.upper()}888",
                "exchange": None,
                "near_contract": res[1],
                "far_contract": res[2],
                "near_price": None,
                "far_price": None,
                "roll_yield": _num(res[0]),
                "src": SRC,
                "version": VERSION,
            }]
        if res is None or (hasattr(res, "empty") and res.empty):
            return []
        # 兜底：若为 DataFrame（未来版本），按列名取
        rows = res.to_dict("records") if hasattr(res, "to_dict") else []
        out: list[dict] = []
        for r in rows:
            def _c(*ks):
                low = {str(k).lower(): k for k in r.keys()}
                for k in ks:
                    if k.lower() in low:
                        return r[low[k.lower()]]
                return None
            near_p = _num(_c("near_price", "near_contract_price"))
            far_p = _num(_c("far_price", "far_contract_price"))
            ry = _num(_c("roll_yield", "roll"))
            if ry is None and near_p and far_p:
                ry = (far_p - near_p) / near_p
            out.append({
                "report_date": date,
                "symbol": f"{var.upper()}888",
                "exchange": None,
                "near_contract": _c("near_contract"),
                "far_contract": _c("far_contract"),
                "near_price": near_p,
                "far_price": far_p,
                "roll_yield": ry,
                "src": SRC,
                "version": VERSION,
            })
        return out
    logger.warning(f"[roll_yield] {var} {date} 接口失败(重试{retries}次): {last_err}")
    return []


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


def run(date: _dt.date, vars_list: list[str], sleep: float = 0.3) -> dict:
    """单交易日批量抓取展期收益率。返回 {var: 行数/错误}。"""
    summary: dict[str, Any] = {}
    for var in vars_list:
        try:
            rows = fetch_roll_yield(date, var)
            n = save_rows(rows)
            summary[var] = n
        except Exception as e:  # noqa: BLE001
            summary[var] = f"失败: {type(e).__name__}: {e}"
        time.sleep(sleep)
    return summary


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    d = _dt.date.today()
    if len(sys.argv) > 1:
        d = _dt.datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    print(run(d, ["RB", "CU", "I"]))
