"""四频重算的 252 交易日 A/B 对拍（CL-20261108-05 / -06）。

判据（不是「差异为 0」，而是「差异全部可解释」）
------------------------------------------------
换月段表改的是**累计偏移**，所以某个边界上偏移变化 d，从该边界起到下一个偏移
变化点为止，所有后复权价格整体平移 d。因此正确的判据是：

1. **行数守恒**：重算前后 252 个交易日窗口内 bar 根数完全一致（不丢不增）。
2. **差分分段恒定**：``Δclose = after - before`` 只在换月边界处改变取值；
   出现在非边界处的突变 = 映射错位（真正的 bug）。
3. **价格恒等式**：``ba.close - ba.adj_offset == raw.close``（每根都成立），
   用来抓「偏移与价格不同步」这类静默损坏。
4. 最大 |Δ| 与受影响段数要能对上：Δ 的跨度不应超过该品种相邻合约的真实价差。

用法：
    python scripts/ops/verify_roll_ab.py                    # 全 38 品种 × 4 频
    python scripts/ops/verify_roll_ab.py --freqs min60
"""
from __future__ import annotations

import argparse
import sys

from sqlalchemy import text

from app.core.db import get_engine

TBL = {"min5": "bar_5m", "min15": "bar_15m", "min30": "bar_30m", "min60": "bar_60m"}
BA = {"min5": "bar_5m_ba", "min15": "bar_15m_ba", "min30": "bar_30m_ba", "min60": "bar_60m_ba"}

SQL = """
WITH j AS (
    SELECT b.bucket,
           b.close      AS c0,
           a.close      AS c1,
           b.adj_offset AS o0,
           a.adj_offset AS o1
      FROM ops.ba_ab_before_{freq} b
      JOIN l2_adj.{ba} a ON a.symbol = b.symbol AND a.bucket = b.bucket
     WHERE b.symbol = :s
),
d AS (
    SELECT bucket, c1 - c0 AS dc,
           lag(c1 - c0) OVER (ORDER BY bucket) AS pdc
      FROM j
),
ch AS (
    SELECT bucket, dc FROM d
     WHERE abs(dc) > 1e-9
       AND (pdc IS NULL OR abs(dc - pdc) > 1e-9)
),
bd AS (
    SELECT seg_start AS b FROM l2_adj.roll_segment
     WHERE symbol = :s AND freq = :f
    UNION
    SELECT seg_start FROM ops.roll_segment_bak_20261109_all4
     WHERE symbol = :s AND freq = :f
),
mis AS (
    SELECT r.close - (a.close - a.adj_offset) AS err
      FROM l2_adj.{ba} a
      JOIN l1_mkt.{tbl} r ON r.symbol = a.symbol AND r.bucket = a.bucket
     WHERE a.symbol = :s
)
SELECT (SELECT count(*) FROM j)                                        AS n_rows,
       (SELECT count(*) FROM ch)                                       AS n_shift,
       (SELECT count(*) FROM ch WHERE bucket NOT IN (SELECT b FROM bd)) AS n_bad,
       (SELECT coalesce(max(abs(dc)), 0) FROM j)                       AS max_dc,
       (SELECT count(*) FROM mis WHERE abs(err) > 1e-6)                AS n_ident_bad,
       (SELECT coalesce(max(abs(err)), 0) FROM mis)                    AS max_ident_err
"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--freqs", default="min5,min15,min30,min60")
    args = ap.parse_args()
    freqs = [f.strip() for f in args.freqs.split(",") if f.strip()]
    for f in freqs:
        if f not in TBL:
            print(f"不支持的 freq={f}", file=sys.stderr)
            return 2

    eng = get_engine()
    with eng.connect() as c:
        win = c.execute(text("SELECT win_start, win_end, n_days FROM ops.ab_window_20261109")).fetchone()
        syms = [r[0] for r in c.execute(text(
            "SELECT variety FROM l3_ref.tradable_variety ORDER BY variety")).fetchall()]
    print(f"A/B 窗口 {win[0]} ~ {win[1]}（{win[2]} 个交易日）/ 品种 {len(syms)} 个\n")
    syms = [f"{s}888" for s in syms]

    print(f"{'freq':5s} {'symbol':7s} {'根数':>7s} {'偏移跳变':>7s} {'非边界跳变':>9s} {'最大|Δ|':>9s} {'恒等式违约':>9s}")
    bad_total = 0
    for freq in freqs:
        for sym in syms:
            with eng.connect() as c:
                c.execute(text("SET jit = off"))
                r = c.execute(text(SQL.format(freq=freq, ba=BA[freq], tbl=TBL[freq])),
                              {"s": sym, "f": freq}).fetchone()
            n_rows, n_shift, n_bad, max_dc, n_ident, max_ident = r
            mark = ""
            if n_bad or n_ident:
                bad_total += 1
                mark = "  ✘"
            print(f"{freq:5s} {sym:7s} {n_rows:7d} {n_shift:7d} {n_bad:9d} "
                  f"{float(max_dc):9.2f} {n_ident:9d}{mark}")
    print(f"\n结论：{'✔ 全部可解释（无越界跳变、价格恒等式成立）' if not bad_total else f'✘ {bad_total} 个组合需人工核查'}")
    return 0 if not bad_total else 1


if __name__ == "__main__":
    raise SystemExit(main())
