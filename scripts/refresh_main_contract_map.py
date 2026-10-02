# -*- coding: utf-8 -*-
"""主力合约映射每日刷新：由 spot_basis.dominant_contract 数据驱动派生 main_contract_map。

为什么不是 smooth_extender（CSV 平滑锚点）？
  旧路径只在 MainContinuous 有 csv_smooth 锚点的品种上跑过（仅 FG/SA 试点），
  main_contract_map 因此只有 2 个品种。本脚本改用「每日维护的 spot_basis.dominant_contract」
  作为主力合约真值信号，覆盖全部品种（spot_basis 有 56 品种、dominant 100% 有值），
  且天然随 spot_basis 每日更新。

口径
----
  - underlying = spot_basis.dominant_contract（已 4 位标准码，如 PS2611）
  - main_symbol = {product}888
  - exchange 取自 futures_symbol（缺则跳过并告警）
  - change_flag = 与同品种上一交易日 underlying 不同
  - src = 'spot_basis'
  - delta 置 0（执行层偏移走 roll_segment，不依赖此列）

幂等：按 (trade_date, exchange, product) UPSERT；支持 --since 增量（默认取 main_contract_map
当前最大日之后，首跑全量）。

用法（容器内，scripts/ 已挂载）
  docker exec -w /app -e PYTHONPATH=/app qhyc-scheduler python scripts/refresh_main_contract_map.py
  docker exec -w /app -e PYTHONPATH=/app qhyc-scheduler python scripts/refresh_main_contract_map.py --since 2026-09-01
"""
from __future__ import annotations

import argparse
import sys
from datetime import date

from sqlalchemy import text

from app.core.db import session_scope
from app.core.logging import logger
from app.repositories.main_contract_repo import MainContractRepository


def _max_existing_date(session) -> date | None:
    return session.execute(text("SELECT MAX(trade_date) FROM main_contract_map")).scalar()


def derive_rows(session, since: date | None) -> list[dict]:
    since_clause = f"AND s.report_date > :since" if since else ""
    sql = text(f"""
        WITH base AS (
            SELECT DISTINCT s.symbol AS product, s.report_date AS trade_date,
                   s.dominant_contract AS underlying
            FROM spot_basis s
            WHERE s.dominant_contract IS NOT NULL AND s.dominant_contract <> ''
                  {since_clause}
        ),
        ex AS (
            SELECT product, exchange FROM (
                SELECT DISTINCT product, exchange, 1 AS pri
                FROM futures_symbol WHERE product IS NOT NULL AND product <> '' AND exchange IS NOT NULL
                UNION ALL
                SELECT DISTINCT product, exchange, 2 AS pri
                FROM contract_code_map WHERE product IS NOT NULL AND product <> '' AND exchange IS NOT NULL
            ) x GROUP BY product, exchange ORDER BY MIN(pri)
        ),
        joined AS (
            SELECT b.product, b.trade_date, b.underlying, ex.exchange
            FROM base b
            LEFT JOIN ex ON ex.product = b.product
        )
        SELECT product, trade_date, underlying, exchange,
               (underlying IS DISTINCT FROM LAG(underlying)
                    OVER (PARTITION BY product ORDER BY trade_date)) AS change_flag
        FROM joined
        ORDER BY product, trade_date
    """)
    params = {"since": since} if since else {}
    rows = session.execute(sql, params).fetchall()

    out: list[dict] = []
    skipped_exchange = set()
    for product, trade_date, underlying, exchange, change_flag in rows:
        if not exchange:
            skipped_exchange.add(product)
            continue
        out.append({
            "trade_date": trade_date,
            "exchange": str(exchange).upper(),
            "product": str(product).upper(),
            "main_symbol": f"{str(product).upper()}888",
            "underlying": str(underlying),
            "change_flag": bool(change_flag),
            "delta": 0,
            "src": "spot_basis",
        })
    if skipped_exchange:
        logger.warning(f"[refresh_mcm] 缺 exchange 跳过品种: {sorted(skipped_exchange)}")
    return out


def refresh(since: date | None = None, chunk: int = 5000) -> dict:
    with session_scope() as session:
        if since is None:
            since = _max_existing_date(session)
            if since is not None:
                logger.info(f"[refresh_mcm] 增量模式，since={since}（仅新日期）")
            else:
                logger.info("[refresh_mcm] 首跑全量")
        rows = derive_rows(session, since)
        repo = MainContractRepository(session)
        n = 0
        products: set[str] = set()
        for i in range(0, len(rows), chunk):
            batch = rows[i:i + chunk]
            n += repo.upsert(batch)
            products.update(r["product"] for r in batch)
        session.commit()
        logger.info(f"[refresh_mcm] 写入 {n} 行，覆盖品种 {len(products)} 个")
        return {"rows": n, "products": len(products)}


def main() -> None:
    ap = argparse.ArgumentParser(description="由 spot_basis 派生 main_contract_map（每日刷新）")
    ap.add_argument("--since", default=None, help="仅处理该日之后的 report_date（YYYY-MM-DD）")
    args = ap.parse_args()
    since = date.fromisoformat(args.since) if args.since else None
    r = refresh(since)
    print(f"OK rows={r['rows']} products={r['products']}")


if __name__ == "__main__":
    sys.exit(main())
