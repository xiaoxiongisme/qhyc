# -*- coding: utf-8 -*-
"""#1 从 888 连续价反推「真实被跟踪的合约」→ 主力字典表。

动机
----
MA888 连续序列跟踪的是**近月 MA2610**，而 main_contract_map 说主力是**远月 MA2701**
（近远月价差 878 点 / 29%）。这不是采集缺口，而是**两套选合约规则不一致**。
要裁决「888 到底跟谁」，最直接的证据就是**用价格匹配反推**。

方法
----
对每个 (品种, 交易日)：
  1) 取该日 888 连续序列的收盘价 v888（优先 hourly_bar 15:00，退化 daily_bar）
  2) 取 contract_daily 同日该品种**全部合约**的收盘价
  3) 选相对差最小者作为「888 实际跟踪的合约」，记录差值
  4) 与 main_contract_map.underlying 比对 → 标记一致 / 不一致

产出
----
表 ``dim_main_contract_inferred``：字典表，可直接 JOIN 供反解与下单口径裁决。

用法：
    python scripts/infer_main_contract.py --varieties MA,EG,FG,SA --days 30
    python scripts/infer_main_contract.py --varieties MA,EG,FG,SA --days 30 --apply
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta

from sqlalchemy import text

from app.core.db import session_scope

sys.stdout.reconfigure(encoding="utf-8")

#: 相对差容差：|888 - 合约| / 合约 <= 此值才算「同一序列」
MATCH_TOL = 0.005


def ensure_table(s):
    s.execute(text("""
        CREATE TABLE IF NOT EXISTS dim_main_contract_inferred (
            variety_code   text NOT NULL,
            trade_date     date NOT NULL,
            inferred_symbol text,
            ref_close      numeric(20,4),
            match_diff     numeric(12,8),
            n_candidates   int,
            method         text,
            mapped_symbol  text,
            agrees_with_map boolean,
            PRIMARY KEY (variety_code, trade_date)
        )"""))
    s.execute(text("CREATE INDEX IF NOT EXISTS ix_dmci_date ON dim_main_contract_inferred(trade_date)"))


def latest_dates(s, varieties: list[str], days: int) -> dict[str, list]:
    """取每个品种在 hourly_bar / daily_bar 中最近有数据的日期。"""
    out = {}
    for v in varieties:
        rows = s.execute(text(
            "SELECT DISTINCT trade_datetime::date FROM hourly_bar WHERE symbol = :s "
            "ORDER BY 1 DESC LIMIT :n"), {"s": v + "888", "n": days}).fetchall()
        if not rows:
            rows = s.execute(text(
                "SELECT trade_date FROM daily_bar WHERE symbol = :s "
                "ORDER BY trade_date DESC LIMIT :n"),
                {"s": v + "888", "n": days}).fetchall()
        out[v] = [r[0] for r in rows]
    return out


def ref_888(s, vc: str, d):
    """888 连续序列在该日的参考价（优先 hourly_bar 收盘，退化 daily_bar）。"""
    r = s.execute(text(
        "SELECT close FROM hourly_bar WHERE symbol=:s AND trade_datetime::date=:d "
        "ORDER BY trade_datetime DESC LIMIT 1"), {"s": vc + "888", "d": d}).fetchone()
    if r and r[0]:
        return float(r[0]), "hourly_bar"
    r = s.execute(text(
        "SELECT close FROM daily_bar WHERE symbol=:s AND trade_date=:d"),
        {"s": vc + "888", "d": d}).fetchone()
    if r and r[0]:
        return float(r[0]), "daily_bar"
    return None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--varieties", required=True,
                    help="逗号分隔，如 MA,EG,FG,SA")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--tol", type=float, default=MATCH_TOL)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    vs = [x.strip().upper() for x in a.varieties.split(",") if x.strip()]
    rows, stats = [], {"agree": 0, "disagree": 0, "nomatch": 0}

    with session_scope() as s:
        dates = latest_dates(s, vs, a.days)
        if not a.apply:
            ensure_table(s)
        for vc in vs:
            ds = dates.get(vc) or []
            print(f"\n=== {vc}888 ===")
            print("  888参考价   最佳匹配合约   差值      main_contract_map   判定")
            for d in ds:
                ref, method = ref_888(s, vc, d)
                if not ref:
                    continue
                cands = s.execute(text(
                    "SELECT symbol, close FROM contract_daily "
                    "WHERE upper(left(symbol, :n)) = :v AND trade_date = :d "
                    "AND close IS NOT NULL AND close > 0"),
                    {"n": len(vc), "v": vc, "d": d}).fetchall()
                if not cands:
                    continue
                best, bestd = None, None
                for sym, close in cands:
                    diff = abs(ref - float(close)) / float(close)
                    if bestd is None or diff < bestd:
                        best, bestd = sym, diff
                mapped = s.execute(text(
                    "SELECT underlying FROM main_contract_map "
                    "WHERE upper(product)=:v AND trade_date=:d"),
                    {"v": vc, "d": d}).scalar()
                if bestd is not None and bestd <= a.tol:
                    agree = (mapped is not None
                             and str(mapped).upper() == str(best).upper())
                    verdict = "一致" if agree else "!! 不一致"
                    stats["agree" if agree else "disagree"] += 1
                else:
                    verdict = "无匹配(差异 %.2f%%)" % ((bestd or 0) * 100)
                    stats["nomatch"] += 1
                    agree = None
                print("  %-10s %-14s %-9s %-18s %s"
                      % (round(ref, 2), best,
                         "%.3f%%" % ((bestd or 0) * 100), mapped or "-", verdict))
                rows.append((vc, d, best, ref, bestd, len(cands), method,
                             mapped, agree))
        if a.apply:
            ensure_table(s)
            for r in rows:
                s.execute(text(
                    "INSERT INTO dim_main_contract_inferred "
                    " (variety_code, trade_date, inferred_symbol, ref_close, "
                    "  match_diff, n_candidates, method, mapped_symbol, "
                    "  agrees_with_map) "
                    "VALUES (:v,:d,:i,:r,:df,:n,:m,:mp,:ag) "
                    "ON CONFLICT (variety_code, trade_date) DO UPDATE SET "
                    " inferred_symbol=EXCLUDED.inferred_symbol, "
                    " ref_close=EXCLUDED.ref_close, match_diff=EXCLUDED.match_diff, "
                    " n_candidates=EXCLUDED.n_candidates, method=EXCLUDED.method, "
                    " mapped_symbol=EXCLUDED.mapped_symbol, "
                    " agrees_with_map=EXCLUDED.agrees_with_map"),
                    {"v": r[0], "d": r[1], "i": r[2], "r": r[3], "df": r[4],
                     "n": r[5], "m": r[6], "mp": r[7], "ag": r[8]})
            print(f"\n[done] dim_main_contract_inferred 写入 {len(rows)} 行")
        print("\n[summary] 一致=%s 不一致=%s 无匹配=%s"
              % (stats["agree"], stats["disagree"], stats["nomatch"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
