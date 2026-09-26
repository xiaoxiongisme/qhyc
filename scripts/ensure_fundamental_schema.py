# -*- coding: utf-8 -*-
"""基本面数据层 + 因子层 Schema 初始化（幂等，可反复跑）。

对应两份 PRD：
- 数据采集扩容 PRD（2026-09-26）：warehouse_receipt / roll_yield / member_position_rank_summary
- 因子接入 PRD（2026-09-26）：factor_registry / factor_value

所有语句均带 IF NOT EXISTS / CREATE OR REPLACE，重复执行安全。
_lag 视图统一提供 asof_date = 发布日 + 1（T 日发布只能用于 T+1 决策），
满足 PRD §6.1「T 日发布的数据只能用于 T+1」的硬性红线。
"""
from __future__ import annotations

import sys
from datetime import datetime

from app.core.db import get_engine
from sqlalchemy import text


# ---------------------------------------------------------------------------
# 数据层：基本面扩展表
# ---------------------------------------------------------------------------
_STMTS = [
    # ---- warehouse_receipt：仓单日报（PRD §4.4）----
    """
    CREATE TABLE IF NOT EXISTS warehouse_receipt (
        report_date  DATE        NOT NULL,
        exchange     TEXT        NOT NULL,
        symbol       TEXT        NOT NULL,
        warehouse    TEXT,
        receipt_qty  BIGINT,
        change_qty   BIGINT,
        unit         TEXT,
        src          TEXT,
        version      TEXT,
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (report_date, exchange, symbol, warehouse)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_wr_date ON warehouse_receipt (report_date)",
    "CREATE INDEX IF NOT EXISTS idx_wr_symbol ON warehouse_receipt (symbol)",

    # ---- roll_yield：展期收益率（PRD §4.3）----
    """
    CREATE TABLE IF NOT EXISTS roll_yield (
        report_date   DATE        NOT NULL,
        symbol        TEXT        NOT NULL,
        exchange      TEXT,
        near_contract TEXT,
        far_contract  TEXT,
        near_price    NUMERIC(16,4),
        far_price     NUMERIC(16,4),
        roll_yield    NUMERIC(12,6),
        src           TEXT,
        version       TEXT,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (report_date, symbol, version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_ry_date ON roll_yield (report_date)",
    "CREATE INDEX IF NOT EXISTS idx_ry_symbol ON roll_yield (symbol)",

    # ---- member_position_rank_summary：按合约前 20 汇总（PRD §4.2b）----
    """
    CREATE TABLE IF NOT EXISTS member_position_rank_summary (
        report_date                       DATE    NOT NULL,
        symbol                           TEXT    NOT NULL,
        variety                          TEXT,
        vol_top5                         BIGINT,
        vol_chg_top5                     BIGINT,
        long_open_interest_top5          BIGINT,
        long_open_interest_chg_top5      BIGINT,
        short_open_interest_top5         BIGINT,
        short_open_interest_chg_top5     BIGINT,
        vol_top10                        BIGINT,
        vol_chg_top10                    BIGINT,
        long_open_interest_top10         BIGINT,
        long_open_interest_chg_top10     BIGINT,
        short_open_interest_top10        BIGINT,
        short_open_interest_chg_top10    BIGINT,
        vol_top15                        BIGINT,
        vol_chg_top15                    BIGINT,
        long_open_interest_top15         BIGINT,
        long_open_interest_chg_top15     BIGINT,
        short_open_interest_top15        BIGINT,
        short_open_interest_chg_top15    BIGINT,
        vol_top20                        BIGINT,
        vol_chg_top20                    BIGINT,
        long_open_interest_top20         BIGINT,
        long_open_interest_chg_top20     BIGINT,
        short_open_interest_top20        BIGINT,
        short_open_interest_chg_top20    BIGINT,
        src                              TEXT,
        version                          TEXT,
        created_at                       TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (report_date, symbol, version)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_mprs_date ON member_position_rank_summary (report_date)",

    # -----------------------------------------------------------------------
    # 因子层（因子接入 PRD §4）：元数据 + 因子值长表
    # -----------------------------------------------------------------------
    """
    CREATE TABLE IF NOT EXISTS factor_registry (
        factor_id        TEXT    PRIMARY KEY,
        name             TEXT    NOT NULL UNIQUE,
        category         TEXT    NOT NULL,
        description      TEXT,
        data_sources     TEXT[]  NOT NULL,
        min_history_days INTEGER NOT NULL DEFAULT 60,
        horizon          TEXT    NOT NULL,
        lag_days         INTEGER NOT NULL DEFAULT 0,
        enabled          BOOLEAN NOT NULL DEFAULT true,
        default_weight   NUMERIC NOT NULL DEFAULT 0,
        max_weight       NUMERIC NOT NULL DEFAULT 0.3,
        version          TEXT    NOT NULL DEFAULT '1.0',
        created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS factor_value (
        factor_id    TEXT      NOT NULL,
        trade_date   DATE      NOT NULL,
        symbol       TEXT      NOT NULL,
        raw_value    NUMERIC,
        z_value      NUMERIC,
        available_at DATE      NOT NULL,
        version      TEXT      NOT NULL DEFAULT '1.0',
        created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (factor_id, trade_date, symbol)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_fv_factor_date ON factor_value (factor_id, available_at)",
]


# _lag 隔离视图：asof_date = 发布日 + 1（PRD §6.1）
_LAG_VIEWS = [
    "CREATE OR REPLACE VIEW v_spot_basis_lagged AS "
    "SELECT *, (report_date + INTERVAL '1 day')::DATE AS asof_date FROM spot_basis",
    "CREATE OR REPLACE VIEW v_warehouse_receipt_lagged AS "
    "SELECT *, (report_date + INTERVAL '1 day')::DATE AS asof_date FROM warehouse_receipt",
    "CREATE OR REPLACE VIEW v_member_position_rank_lagged AS "
    "SELECT *, (trade_date + INTERVAL '1 day')::DATE AS asof_date FROM member_position_rank",
    "CREATE OR REPLACE VIEW v_roll_yield_lagged AS "
    "SELECT *, (report_date + INTERVAL '1 day')::DATE AS asof_date FROM roll_yield",
]


def main() -> int:
    eng = get_engine()
    ok, fail = 0, 0
    with eng.begin() as conn:
        for stmt in _STMTS:
            try:
                conn.execute(text(stmt))
                ok += 1
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"[schema] FAIL: {e}\n--- SQL ---\n{stmt[:200]}")
        for v in _LAG_VIEWS:
            try:
                conn.execute(text(v))
                ok += 1
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"[schema] VIEW FAIL: {e}")
    print(f"[{datetime.now():%H:%M:%S}] schema ensured: ok={ok} fail={fail}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
