#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CL-20261110-12：用新浪同源数据**覆盖修正** DCE 官网残缺/异常合约。

与 ``backfill_dce_rank_sina.py`` 的区别
----------------------------------------
CL-20261110-11 的补数只 ``INSERT`` 库内**缺失合约**（``ON CONFLICT DO NOTHING``），
不删不改官网已有行。2026-11-06 实测有 11 个合约官网行本身是坏的：

* 6 个 20 行齐全但数值腰斩：P2705 / PG2612 / PG2701 / PP2612 / PP2701 / PP2702
  （P2705 long 合计 220849→11955，PP2701 447868→26249）
* 5 个行数不足 20：P2709(18) / PG2702(15) / PG2703(12) / PP2703(8) / PP2705(17)

行数型监控（``OPS-MPR-EXCHANGE-COVERAGE`` / ``OPS-MPR-DCE-COVERAGE`` 只看品种数）
看不见这两类。本脚本对**指定日的异常合约**做 DELETE + INSERT 覆盖。

三道硬门（宁可漏修不可错修）
----------------------------
0. ``verify_date_sensitive``：同一合约两个交易日返回必须不同，防 date 参数失效
   把未来数据写进历史分区。
1. ``fetch_contract_rows`` 内建：三张榜单各 20 行、名次 1..20 连续、会员名非空、
   非负整数、数值降序。
2. 跨日规模连续：新浪合计 / 前一交易日同合约合计 ∈ [0.3, 3.0]
   （11-06 十一个合约实测 0.90~1.05，官网坏值 0.001~0.198）。

默认 **DRY-RUN 只读**；``--apply`` 才写，且先按日幂等备份原分区到
``l3_ref.bak_cl20261110_12``。回滚：从备份表 DELETE 现有新浪行后插回。

用法（生产容器内）::

    python scripts/ops/repair_dce_rank_sina.py --date 2026-11-06
    python scripts/ops/repair_dce_rank_sina.py --date 2026-11-06 --apply
    python scripts/ops/repair_dce_rank_sina.py --date 2026-11-06 \
        --contract P2705 --contract PP2701 --apply
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys

from sqlalchemy import text

from app.core.db import get_engine
from app.ingest.dce_sina import (
    SRC_TAG, TABLE, VERSION,
    fetch_contract_rows, verify_date_sensitive,
)

BACKUP_TABLE = "l3_ref.bak_cl20261110_12"
MIN_RATIO, MAX_RATIO = 0.30, 3.00
#: 相对前一交易日合计低于该比例视为「腰斩」，用于自动发现异常合约
COLLAPSE_RATIO = 0.50
MIN_PREV_SCALE = 10000

_INSERT = text(
    """
    INSERT INTO l0_raw.member_position_rank
        (trade_date, exchange, symbol, member, rank,
         long_pos, short_pos, long_chg, short_chg, vol_pos, src, version)
    VALUES (:trade_date, :exchange, :symbol, :member, :rank,
            :long_pos, :short_pos, :long_chg, :short_chg, :vol_pos, :src, :version)
    ON CONFLICT (trade_date, exchange, symbol, member, version) DO UPDATE SET
        rank = EXCLUDED.rank, long_pos = EXCLUDED.long_pos,
        short_pos = EXCLUDED.short_pos, long_chg = EXCLUDED.long_chg,
        short_chg = EXCLUDED.short_chg, vol_pos = EXCLUDED.vol_pos,
        src = EXCLUDED.src
    """
)


def prev_trade_date(d: _dt.date) -> _dt.date | None:
    with get_engine().connect() as conn:
        return conn.execute(text(
            f"SELECT max(trade_date) FROM {TABLE} "
            "WHERE exchange='DCE' AND trade_date < :d"), {"d": d}).scalar()


def suspicious_contracts(d: _dt.date, prev_d: _dt.date | None) -> list[dict]:
    """当日 DCE 异常合约 = 行数 <20 或 long 合计相对前一日腰斩。"""
    with get_engine().connect() as conn:
        rows = conn.execute(text(f"""
            WITH cur AS (
              SELECT symbol, count(*) n, sum(long_pos) sl
              FROM {TABLE} WHERE exchange='DCE' AND trade_date=:d GROUP BY 1),
            prev AS (
              SELECT symbol, count(*) n, sum(long_pos) sl
              FROM {TABLE} WHERE exchange='DCE' AND trade_date=:p GROUP BY 1)
            SELECT c.symbol, c.n, c.sl, p.sl AS prev_sl
            FROM cur c LEFT JOIN prev p USING (symbol)
            WHERE (p.n = 20 AND c.n < 20)
               OR (p.sl > :ms AND c.sl < p.sl * :cr)
            ORDER BY 1
        """), {"d": d, "p": prev_d, "ms": MIN_PREV_SCALE,
               "cr": COLLAPSE_RATIO}).all()
    return [{"symbol": r[0], "rows": int(r[1]), "long": int(r[2]),
             "prev_long": int(r[3]) if r[3] is not None else None} for r in rows]


def ensure_backup(d: _dt.date, contracts: list[str], table: str) -> int:
    """按日幂等备份目标合约的官网原行。"""
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text(
            f"CREATE TABLE IF NOT EXISTS {table} AS SELECT * FROM {TABLE} WHERE false"))
        r = conn.execute(text(
            f"INSERT INTO {table} SELECT m.* FROM {TABLE} m "
            "WHERE m.exchange='DCE' AND m.trade_date=:d AND m.symbol = ANY(:cs) "
            f"AND NOT EXISTS (SELECT 1 FROM {table} b WHERE b.trade_date=:d "
            "AND b.exchange='DCE' AND b.symbol=m.symbol AND b.member=m.member "
            "AND b.version=m.version)"),
            {"d": d, "cs": contracts})
        return r.rowcount or 0


def repair(d: _dt.date, contracts: list[str], prev_d: _dt.date | None,
           apply: bool, backup_table: str = BACKUP_TABLE) -> dict:
    result = {
        "date": str(d), "prev_date": str(prev_d), "mode": "APPLY" if apply else "DRY-RUN",
        "targets": contracts, "accepted": [], "rejected": [],
        "rows_deleted": 0, "rows_inserted": 0, "backup_rows": 0, "ok": True,
    }
    # 门 0：date 敏感性（取第一个目标合约做探针）
    if prev_d and contracts:
        good, msg = verify_date_sensitive(contracts[0], prev_d, d)
        result["gates"] = {"date_sensitive": msg}
        if not good:
            result["ok"] = False
            result["rejected"].append({"gate": "date_sensitive", "detail": msg})
            return result

    accepted_rows: list[dict] = []
    for c in contracts:
        rows, err = fetch_contract_rows(c, d)
        if rows is None:
            result["rejected"].append({"symbol": c, "gate": "fetch", "detail": err})
            continue
        sl = sum(r["long_pos"] for r in rows)
        # 门 2：跨日规模连续
        if prev_d:
            with get_engine().connect() as conn:
                prev_sl = conn.execute(text(
                    f"SELECT sum(long_pos) FROM {TABLE} WHERE exchange='DCE' "
                    "AND trade_date=:p AND symbol=:c"), {"p": prev_d, "c": c}).scalar()
            if prev_sl and int(prev_sl) > MIN_PREV_SCALE:
                ratio = sl / int(prev_sl)
                if not (MIN_RATIO <= ratio <= MAX_RATIO):
                    result["rejected"].append(
                        {"symbol": c, "gate": "scale", "detail":
                         f"新浪 long={sl} / 前日 {int(prev_sl)} = {ratio:.3f} "
                         f"超出 [{MIN_RATIO},{MAX_RATIO}]"})
                    continue
        accepted_rows.extend(rows)
        result["accepted"].append({"symbol": c, "rows": len(rows), "long": sl})

    result["gates"] = result.get("gates", {})
    result["gates"]["scale"] = f"[{MIN_RATIO},{MAX_RATIO}] × 前一交易日合计"
    result["rows_to_write"] = len(accepted_rows)
    if not accepted_rows:
        result["ok"] = False
        return result

    if apply:
        cs = [x["symbol"] for x in result["accepted"]]
        result["backup_rows"] = ensure_backup(d, cs, backup_table)
        with get_engine().begin() as conn:
            r = conn.execute(text(
                f"DELETE FROM {TABLE} WHERE exchange='DCE' AND trade_date=:d "
                "AND symbol = ANY(:cs)"), {"d": d, "cs": cs})
            result["rows_deleted"] = r.rowcount or 0
            for rec in accepted_rows:
                conn.execute(_INSERT, rec)
            result["rows_inserted"] = len(accepted_rows)
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description="DCE 官网异常合约覆盖修正")
    ap.add_argument("--date", required=True, help="交易日 YYYY-MM-DD")
    ap.add_argument("--contract", action="append", default=[],
                    help="指定合约（可重复）；不指定则自动发现异常合约")
    ap.add_argument("--apply", action="store_true", help="实际写库（默认只读预演）")
    ap.add_argument("--backup-table", default=BACKUP_TABLE)
    a = ap.parse_args()

    d = _dt.date.fromisoformat(a.date)
    prev_d = prev_trade_date(d)
    print(f"模式={'APPLY 写库' if a.apply else 'DRY-RUN 只读'} | 日期={d} "
          f"前一交易日={prev_d}", flush=True)
    if a.contract:
        targets = [c.upper() for c in a.contract]
        print(f"显式指定 {len(targets)} 个合约: {','.join(targets)}", flush=True)
    else:
        sus = suspicious_contracts(d, prev_d)
        targets = [s["symbol"] for s in sus]
        print(f"自动发现 {len(targets)} 个异常合约:", flush=True)
        for s in sus:
            print(f"  {s['symbol']:8s} rows={s['rows']:2d} long={s['long']:8d} "
                  f"prev={s['prev_long']}", flush=True)
    if not targets:
        print("无异常合约，无需修复")
        return 0

    r = repair(d, targets, prev_d, a.apply, a.backup_table)
    print(json.dumps(r, ensure_ascii=False, default=str, indent=2), flush=True)
    print(f"=== 接受 {len(r['accepted'])} / 拒绝 {len(r['rejected'])} 合约；"
          f"删除 {r['rows_deleted']} 行 / 写入 {r['rows_inserted']} 行 "
          f"/ 备份 {r['backup_rows']} 行 ===", flush=True)
    return 0 if r["ok"] else 2


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
