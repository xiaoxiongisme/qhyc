# -*- coding: utf-8 -*-
"""用完整的 contract_daily 全量重算 dim_contract 生命周期（list_date/last_trade_date）。

注意：migrations/007 的 3.3 段只在 IS NULL 时回填，不会覆盖此前 90 天窗口
算出的「假」生命周期。此处强制按全量 contract_daily 的 min/max(trade_date) 覆盖所有合约。
"""
from sqlalchemy import text
from app.core.db import session_scope


def main() -> None:
    with session_scope() as s:
        s.execute(text(
            "UPDATE dim_contract dc SET "
            "list_date = s.first_td, last_trade_date = s.last_td, updated_at = now() "
            "FROM ("
            "  SELECT symbol, min(trade_date) AS first_td, max(trade_date) AS last_td "
            "  FROM contract_daily GROUP BY symbol"
            ") s "
            "WHERE dc.contract_code = s.symbol"
        ))
        s.commit()

        null_cnt = s.execute(text(
            "SELECT count(*) FROM dim_contract "
            "WHERE list_date IS NULL OR last_trade_date IS NULL"
        )).scalar()
        covered = s.execute(text(
            "SELECT count(*) FROM dim_contract WHERE last_trade_date IS NOT NULL"
        )).scalar()
        print(f"[lifecycle] 已重算；有生命周期的合约={covered}，仍 NULL={null_cnt}", flush=True)

        sample = s.execute(text(
            "SELECT contract_code, list_date, last_trade_date FROM dim_contract "
            "WHERE contract_code IN ('RB2601','CU2601','AG2601','MA2601','EG2611') "
            "ORDER BY contract_code"
        )).fetchall()
        for r in sample:
            print("   ", r, flush=True)


if __name__ == "__main__":
    main()
