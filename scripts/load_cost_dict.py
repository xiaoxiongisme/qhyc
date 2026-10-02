# -*- coding: utf-8 -*-
"""把 dim_trading_cost 种子 CSV 装入库表（滚动语义：闭合旧行 + 插入新行）。

滚动（合约与费率都会变）
------------------------
交易所调费时**不原地 UPDATE**，而是：
  1. 把当前有效行（effective_to IS NULL）的 effective_to 置为本次的 effective_from
  2. 插入新行（effective_to = NULL）
这样「某历史日期当时的费率」永远查得到，不会因后续调费而集体偏移。

用法（云端容器内）：
  docker exec -w /app -e PYTHONPATH=/app qhyc-api \\
    python scripts/load_cost_dict.py --dry-run
  docker exec -w /app -e PYTHONPATH=/app qhyc-api \\
    python scripts/load_cost_dict.py --apply
"""
from __future__ import annotations

import argparse
import csv
import os

from sqlalchemy import text

from app.core.db import session_scope

DEFAULT_CSV = os.path.join("db", "seed", "dim_trading_cost_seed.csv")

_COLS = ("exchange, variety_code, instrument_kind, action, scope_kind, scope_months, "
         "scope_contracts, fee_type, fee_value, exchange_fee_value, "
         "broker_markup_type, broker_markup_value, slip_ticks, "
         "effective_from, effective_to, source, note")


def _split(v: str | None) -> list[str] | None:
    if not v:
        return None
    return [x for x in str(v).split(";") if x]


def _params(r: dict, close_only: bool = False) -> dict:
    return {
        "ex": r["exchange"], "vc": r["variety_code"].upper(),
        "kind": r.get("instrument_kind") or "FUTURE",
        "act": r["action"], "scope": r.get("scope_kind") or "ALL",
        "months": _split(r.get("scope_months")),
        "contracts": _split(r.get("scope_contracts")),
        "ftype": r["fee_type"], "fval": float(r["fee_value"] or 0),
        "exfval": float(r["fee_value"] or 0),
        "btype": r.get("broker_markup_type") or "NONE",
        "bval": float(r.get("broker_markup_value") or 0),
        "ef": r["effective_from"], "src": r.get("source") or "seed",
        "note": r.get("note") or None,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=DEFAULT_CSV)
    ap.add_argument("--apply", action="store_true", help="落库；默认 dry-run")
    args = ap.parse_args()

    if not os.path.exists(args.csv):
        print(f"✗ 找不到种子文件 {args.csv}；请先在本地跑 scripts/build_cost_dict.py")
        return 1
    with open(args.csv, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("✗ 种子文件为空")
        return 1

    with session_scope() as s:
        exists = s.execute(text(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_name='dim_trading_cost'")).scalar()
        if not exists:
            print("✗ dim_trading_cost 不存在，请先应用 migrations/013_trading_cost_dict.sql")
            return 1
        known = {r[0] for r in s.execute(text("SELECT exchange_code FROM dim_exchange")).fetchall()}
        cur_rows = s.execute(text(
            "SELECT count(*) FROM dim_trading_cost WHERE effective_to IS NULL")).scalar()
    print(f"种子 {len(rows)} 行；库内当前有效费率 {cur_rows} 行")

    bad = sorted({r["exchange"] for r in rows if r["exchange"] not in known})
    if bad:
        print(f"⚠ 交易所不在 dim_exchange（跳过）：{bad}")
    rows = [r for r in rows if r["exchange"] in known]
    if not rows:
        print("✗ 无可装载行")
        return 1

    close_sql = text(
        "UPDATE dim_trading_cost SET effective_to = :ef, updated_at = now() "
        "WHERE variety_code=:vc AND instrument_kind=:kind AND action=:act "
        "  AND scope_kind=:scope AND effective_to IS NULL AND effective_from < :ef")
    upsert_sql = text(
        f"INSERT INTO dim_trading_cost ({_COLS}) "
        "VALUES (:ex, :vc, :kind, :act, :scope, :months, :contracts, :ftype, :fval, "
        "        :exfval, :btype, :bval, 0, :ef, NULL, :src, :note) "
        "ON CONFLICT ON CONSTRAINT uq_dim_trading_cost DO UPDATE SET "
        "  fee_type=EXCLUDED.fee_type, fee_value=EXCLUDED.fee_value, "
        "  exchange_fee_value=EXCLUDED.exchange_fee_value, "
        "  broker_markup_type=EXCLUDED.broker_markup_type, "
        "  broker_markup_value=EXCLUDED.broker_markup_value, "
        "  scope_months=EXCLUDED.scope_months, scope_contracts=EXCLUDED.scope_contracts, "
        "  source=EXCLUDED.source, note=EXCLUDED.note, updated_at=now()")

    closed = upserted = 0
    if args.apply:
        with session_scope() as s:
            for r in rows:
                p = _params(r)
                closed += s.execute(close_sql, p).rowcount
                s.execute(upsert_sql, p)
                upserted += 1
        print(f"[done] 闭合旧行 {closed} 条；UPSERT {upserted} 条")
    else:
        with session_scope() as s:
            n_close = sum(s.execute(close_sql, _params(r, close_only=True)).rowcount
                          for r in rows)
        print(f"[dry-run] 将闭合旧行 {n_close} 条，并 UPSERT {len(rows)} 条；--apply 才落库")
        return 0

    with session_scope() as s:
        tot = s.execute(text("SELECT count(*) FROM dim_trading_cost")).scalar()
        cur = s.execute(text(
            "SELECT count(*) FROM dim_trading_cost WHERE effective_to IS NULL")).scalar()
        v = s.execute(text("SELECT count(DISTINCT variety_code) FROM dim_trading_cost")).scalar()
    print(f"[verify] dim_trading_cost 共 {tot} 行；当前有效 {cur} 行；覆盖品种 {v} 个")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
