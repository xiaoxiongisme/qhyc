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


def _months_between(sym_a: str, sym_b: str) -> int | None:
    """两个合约代码（品种+YYMM）之间的月差；无法解析返回 None。"""
    import re

    ma = re.search(r"(\d{2})(\d{2})$", str(sym_a))
    mb = re.search(r"(\d{2})(\d{2})$", str(sym_b))
    if not ma or not mb:
        return None
    ya, mma = int(ma.group(1)), int(ma.group(2))
    yb, mmb = int(mb.group(1)), int(mb.group(2))
    if not (1 <= mma <= 12 and 1 <= mmb <= 12):
        return None
    return (ya - yb) * 12 + (mma - mmb)


def fetch_roll_yield_db(date: _dt.date, var: str) -> list[dict]:
    """**库内兜底**：用 contract_daily 自算展期收益（akshare 链路不可用时）。

    为什么需要（2026-10-06 实测）：``ak.get_roll_yield`` 内部走
    ``get_futures_daily(market=...)``，**DCE 等品种该端点返回非 JSON** →
    ``Expecting value: line 1 column 1`` 逐品种重试全失败，roll_yield 的
    DCE 17 品种自 09-24 起停更。本函数不依赖任何外部源。

    口径与 akshare ``get_roll_yield`` **完全一致**（对照其源码）：
      1. 取该品种当日全部合约，按 **open_interest 降序**；
      2. symbol1 = 第一（主力/近月），symbol2 = 第二（次主力/远月）；
      3. ``c`` = 两者月份差；``roll = log(close2/close1) / c * 12``（年化）；
      4. 返回 (roll, near, far)：c>0 时 near=symbol2，否则 near=symbol1。

    与 akshare 版的差异：``near_price/far_price`` 这里**有值**（akshare 版留 NULL，
    因其价格接口需 JS）；``src`` 标记 ``db:contract_daily`` 以便溯源区分。
    """
    import math

    eng = get_engine()
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT symbol, close, oi FROM contract_daily "
                "WHERE trade_date = :d AND upper(product) = upper(:v) "
                "  AND close IS NOT NULL AND oi IS NOT NULL "
                "ORDER BY oi DESC"
            ),
            {"d": date, "v": var},
        ).fetchall()
    if len(rows) < 2:
        return []
    sym1, close1 = rows[0][0], rows[0][1]
    sym2, close2 = rows[1][0], rows[1][1]
    c = _months_between(sym1, sym2)
    if not c:
        return []
    try:
        c1, c2 = float(close1), float(close2)
    except (TypeError, ValueError):
        return []
    if c1 == 0 or c2 == 0:
        return []
    roll = math.log(c2 / c1) / c * 12
    near, far = (sym2, sym1) if c > 0 else (sym1, sym2)
    near_px, far_px = (c2, c1) if c > 0 else (c1, c2)
    return [{
        "report_date": date,
        "symbol": f"{var.upper()}888",
        "exchange": None,
        "near_contract": near,
        "far_contract": far,
        "near_price": near_px,
        "far_price": far_px,
        "roll_yield": roll,
        "src": "db:contract_daily",
        "version": VERSION,
    }]


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
    """单交易日批量抓取展期收益率。返回 {var: 行数/错误}。

    两级取数（2026-10-06 起）：先 akshare 官方链路；**取不到则回落库内
    contract_daily 自算**（口径等价，见 :func:`fetch_roll_yield_db` 注释）。
    绝不因为某个品种外部源挂了就整日留空。
    """
    summary: dict[str, Any] = {}
    for var in vars_list:
        try:
            rows = fetch_roll_yield(date, var)
            if not rows:
                rows = fetch_roll_yield_db(date, var)
                if rows:
                    logger.info(
                        f"[roll_yield] {var} {date} akshare 无数据，已用库内 contract_daily 兜底")
            n = save_rows(rows)
            summary[var] = n
        except Exception as e:  # noqa: BLE001
            try:
                rows = fetch_roll_yield_db(date, var)
                n = save_rows(rows)
                summary[var] = f"{n}(db_fallback)"
            except Exception as e2:  # noqa: BLE001
                summary[var] = f"失败: {type(e).__name__}: {e} | db兜底亦失败: {e2}"
        time.sleep(sleep)
    return summary


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    d = _dt.date.today()
    if len(sys.argv) > 1:
        d = _dt.datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    print(run(d, ["RB", "CU", "I"]))
