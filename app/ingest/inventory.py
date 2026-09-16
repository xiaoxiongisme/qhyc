"""§18.5（v1.3）M6a：库存 / 仓单采集器

数据源：akshare
- futures_inventory_em(symbol)   → DataFrame[日期, 库存, 增减]  通用库存
- get_receipt(start_date, end_date, vars_list) → DataFrame  跨所批量仓单
- futures_shfe_warehouse_receipt(date) → dict  SHFE 仓单
- futures_warehouse_receipt_czce/dce/gfex → 各所仓单（按需）

周期：周频（§18.5：库存数据更新频率本身为周；采集在周五 16:30 调度）

符号映射：
- 品种代号（em 接口接受 'a'='豆一'/'m'='豆粕'/'y'='豆油' 等）
- 完整映射见 PRODUCT_TO_EM_SYM；未覆盖品种不入库
"""
from __future__ import annotations

from datetime import date
from typing import Any

import pandas as pd

from app.core.logging import logger
from app.models import Inventory


# akshare em 接口品种代号（与品种简称）
PRODUCT_TO_EM_SYM = {
    "A": "a", "AG": "ag", "AL": "al", "AU": "au", "B": "b", "BU": "bu",
    "C": "c", "CF": "cf", "CS": "cs", "CU": "cu", "EB": "eb", "EG": "eg",
    "FG": "fg", "I": "i", "J": "j", "JM": "jm", "L": "l", "M": "m",
    "MA": "ma", "NI": "ni", "NR": "nr", "O": "o", "P": "p", "PB": "pb",
    "PF": "pf", "PG": "pg", "PP": "pp", "RB": "rb", "RM": "rm", "RU": "ru",
    "SA": "sa", "SC": "sc", "SF": "sf", "SM": "sm", "SN": "sn", "SR": "sr",
    "SS": "ss", "TA": "ta", "V": "v", "Y": "y", "ZC": "zc", "ZN": "zn",
}


def _coerce_int(v) -> int:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return 0
    if isinstance(v, int):
        return v
    s = str(v).replace(",", "").strip()
    if not s or s in ("-", "--"):
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _to_date(s) -> date | None:
    try:
        return pd.to_datetime(s).date()
    except Exception:
        return None


def fetch_inventory_em(product: str, exchange: str = "") -> list[dict]:
    """em 接口：单品种时间序列（最近 60+ 天）"""
    import akshare as ak  # type: ignore

    sym = PRODUCT_TO_EM_SYM.get(product.upper())
    if not sym:
        return []
    try:
        df = ak.futures_inventory_em(symbol=sym)
    except Exception as e:
        logger.warning(f"[inventory] em {product}({sym}) 失败: {e}")
        return []
    if df is None or df.empty:
        return []
    rows = []
    for _, r in df.iterrows():
        d = _to_date(r.iloc[0])
        if d is None:
            continue
        rows.append(
            {
                "report_date": d,
                "exchange": exchange or "auto",
                "symbol": product.upper(),
                "warehouse": None,
                "inventory_qty": _coerce_int(r.iloc[1]),
                "receipt_qty": None,
                "unit": "手",
                "change_qty": _coerce_int(r.iloc[2]) if len(r) >= 3 else 0,
                "src": "akshare:futures_inventory_em",
                "version": "v1.0",
            }
        )
    return rows


def upsert_inventory(session, rows: list[dict]) -> int:
    """批量 upsert（PK: report_date+exchange+symbol+warehouse+version）"""
    if not rows:
        return 0
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    seen = {}
    for r in rows:
        wh = r.get("warehouse") or ""  # NULL → ''（PK 兼容）
        k = (r["report_date"], r["exchange"], r["symbol"], wh, r["version"])
        r["warehouse"] = wh
        seen[k] = r
    rows = list(seen.values())

    stmt = pg_insert(Inventory).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["report_date", "exchange", "symbol", "warehouse", "version"],
        set_={
            "inventory_qty": stmt.excluded.inventory_qty,
            "receipt_qty": stmt.excluded.receipt_qty,
            "unit": stmt.excluded.unit,
            "change_qty": stmt.excluded.change_qty,
            "src": stmt.excluded.src,
        },
    )
    session.execute(stmt)
    return len(rows)


def collect_inventory_em(session, products: list[str], exchange_map: dict[str, str] | None = None) -> dict[str, Any]:
    """采集 em 接口各品种库存（周频全量回填 + 增量）

    exchange_map：品种 → 交易所（默认按"PRODUCT_EXCHANGE"启发式：DCE/黑/C/A/M/Y/P/I/J/L/V/PP/JM/EG/B/CS,
        CZCE/玻璃FG/纯碱SA/MA/TA/UR/苹果AP/棉花CF/SR/白糖/OI, SHFE/CU/AU/AG/RB/RU/ZN/AL/PB/SN/NI/SS/HC/SP/BU 等）
    """
    if exchange_map is None:
        exchange_map = _default_exchange_map()
    stats: dict[str, Any] = {"products": 0, "rows": 0, "errors": []}
    for prod in products:
        try:
            ex = exchange_map.get(prod.upper(), "")
            rows = fetch_inventory_em(prod, exchange=ex)
            n = upsert_inventory(session, rows)
            stats["products"] += 1
            stats["rows"] += n
            session.commit()
        except Exception as e:
            session.rollback()
            stats["errors"].append(f"{prod}: {e}")
            logger.exception(f"[inventory] {prod} 失败: {e}")
    return stats


def _default_exchange_map() -> dict[str, str]:
    """品种→交易所启发式（用于入库存档时打 exchange 标签）"""
    dce = {"A", "B", "C", "CS", "EG", "FB", "BB", "I", "J", "JM", "L", "M", "P", "PP", "V", "Y"}
    czce = {"AP", "CF", "CJ", "CY", "FG", "JR", "LR", "MA", "OI", "PF", "PK", "PM", "RI", "RM", "SA", "SF", "SM", "SR", "TA", "UR", "WH", "WS", "WT", "ZC"}
    shfe = {"AG", "AL", "AU", "BU", "CU", "FU", "HC", "NI", "NR", "PB", "RB", "RU", "SC", "SN", "SP", "SS", "ZN"}
    ine = {"BC", "EC", "LU", "NR"}
    gfex = {"SI", "LC"}
    m: dict[str, str] = {}
    for p in dce:
        m[p] = "DCE"
    for p in czce:
        m[p] = "CZCE"
    for p in shfe:
        m[p] = "SHFE"
    for p in ine:
        m[p] = "INE"
    for p in gfex:
        m[p] = "GFEX"
    return m
