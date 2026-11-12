#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反推 888 主连「当日实际所跟的具体合约」，写入 l3_ref.dim_main_contract_inferred。

CL-20261112-11
==============
背景
----
``dim_main_contract_inferred.inferred_symbol`` 是执行层
``app/execution/roll_policy.py::select_real_contract`` 反解真实合约的**首选真源**
（优先于 ``main_contract_map.underlying``）。但该表的写入方从未进仓库、未挂调度，
最后写入停在 **2026-09-30**；而 ``_resolve_inferred`` 用
``trade_date <= :d ORDER BY trade_date DESC LIMIT 1`` 静默回退到最近一条，
2026-11-12 实测 **26/50 品种**反解到与当前主力不符的陈旧合约，且全程无告警。

方法
----
对某个交易日的日盘分钟线，把 ``X888`` 与同品种每个 ``X<ym>`` 具体合约按 ``ts``
内连接，算平均绝对差 MAD；MAD 最小者即 888 当日实际所跟合约。

为什么不能用「某一根 K 线收盘价相等」：akshare 的主连与具体合约是两条独立采集
的分钟序列，快照时刻 / 最后一笔成交可能不同。2026-11-12 14:15 实测
AG888=12382 / AG2702=12378（差 4 点），单点判定会把 14 个品种误判为「跟错」；
但全日 225 根的 MAD 只有 0.471，次名 AG2703 为 13.493 —— 序列判定稳定。
I888 vs I2701 的 MAD 为 0.033，次名 I2702 为 2.210，同样差两个数量级。

用法（生产容器内，PYTHONPATH=/app）
------------------------------------
    python scripts/ops/infer_main_contract.py --date 2026-11-12          # 预览，不写库
    python scripts/ops/infer_main_contract.py --days 5 --apply            # 写最近 5 个交易日
    python scripts/ops/infer_main_contract.py --since 2026-10-01 --apply  # 回填空洞

默认 dry-run；``--apply`` 才写。写入幂等（ON CONFLICT DO UPDATE）。
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date
from typing import Any

from sqlalchemy import text

from app.core.db import session_scope

TABLE = "l3_ref.dim_main_contract_inferred"
METHOD = "minute_bar_mad"
MIN_MINUTES = 60           # 少于该根数的候选不参与（防稀疏序列碰巧 MAD 小）
DAY_T0 = "09:00:00+08"     # 日盘窗口 (09:00, 15:01]，含上午/下午两个时段
DAY_T1 = "15:01:00+08"

# 品种码 + 888 主连 / 具体月份合约的限定正则；窗口化 + 前缀限定，避免扫 6000 万行大表。
_INFER_SQL = """
WITH b AS (
    SELECT ts, symbol, close, regexp_replace(symbol, '[0-9]+$', '') AS v
      FROM l0_raw.minute_bar
     WHERE ts >= :t0 AND ts < :t1
       AND symbol ~ :pat
       AND symbol !~ '8888$'
),
m AS (
    SELECT v, ts, close FROM b WHERE symbol ~ '888$'
),
c AS (
    SELECT v, symbol, ts, close FROM b
     WHERE symbol ~ '[0-9]{3,4}$' AND symbol !~ '888$'
),
d AS (
    SELECT m.v AS v, c.symbol AS symbol, count(*) AS n,
           avg(abs(c.close - m.close)) AS mad,
           avg(m.close) AS ref_close
      FROM m JOIN c ON c.v = m.v AND c.ts = m.ts
     GROUP BY 1, 2
),
ranked AS (
    SELECT v, symbol, n, mad, ref_close,
           row_number() OVER (PARTITION BY v ORDER BY mad, symbol) AS rn,
           count(*) OVER (PARTITION BY v) AS n_cand
      FROM d WHERE n >= :min_minutes
)
SELECT v, symbol, n, mad, ref_close, n_cand, rn
  FROM ranked WHERE rn <= 2
 ORDER BY v, rn
"""

_UPSERT = """
INSERT INTO l3_ref.dim_main_contract_inferred
    (variety_code, trade_date, inferred_symbol, ref_close, match_diff,
     n_candidates, method, mapped_symbol, agrees_with_map)
VALUES
    (:variety_code, :trade_date, :inferred_symbol, :ref_close, :match_diff,
     :n_candidates, :method, :mapped_symbol, :agrees_with_map)
ON CONFLICT (variety_code, trade_date) DO UPDATE SET
    inferred_symbol = EXCLUDED.inferred_symbol,
    ref_close       = EXCLUDED.ref_close,
    match_diff      = EXCLUDED.match_diff,
    n_candidates    = EXCLUDED.n_candidates,
    method          = EXCLUDED.method,
    mapped_symbol   = EXCLUDED.mapped_symbol,
    agrees_with_map = EXCLUDED.agrees_with_map
"""


def map_dates(session, *, limit: int | None = None,
              since: date | None = None) -> list[date]:
    """``main_contract_map`` 里已有的交易日，升序返回（它是每日 17:10 更新的真源）。"""
    cond = "trade_date >= :since" if since else "TRUE"
    sql = ("SELECT DISTINCT trade_date FROM l3_ref.main_contract_map "
           f"WHERE {cond} ORDER BY trade_date DESC")
    params: dict[str, Any] = {}
    if since:
        params["since"] = since
    if limit:
        sql += " LIMIT :lim"
        params["lim"] = limit
    return sorted(r[0] for r in session.execute(text(sql), params).fetchall())


def products_on(session, d: date) -> list[str]:
    return [r[0] for r in session.execute(text(
        "SELECT product FROM l3_ref.main_contract_map "
        "WHERE trade_date = :d ORDER BY product"), {"d": d}).fetchall()]


def mapped_on(session, d: date) -> dict[str, str | None]:
    return {r[0]: r[1] for r in session.execute(text(
        "SELECT product, underlying FROM l3_ref.main_contract_map "
        "WHERE trade_date = :d"), {"d": d}).fetchall()}


def _pattern(products: list[str]) -> str:
    # 长码优先，避免 A 抢先匹配 AO 前缀
    alts = "|".join(re.escape(p) for p in sorted(set(products), key=lambda x: (-len(x), x)))
    return f"^({alts})(888|[0-9]{{3,4}})$"


def _assemble(rows: list[Any], mp: dict[str, str | None],
              d: date) -> list[dict[str, Any]]:
    """把 ``_INFER_SQL`` 的 (v, symbol, n, mad, ref_close, n_cand, rn) 行组装成结果。

    纯函数（不连库），单测直接喂构造的行即可锁住「取 MAD 最小者」这条口径，
    不必 mock SQLAlchemy。
    """
    grouped: dict[str, list[Any]] = {}
    for r in rows:
        grouped.setdefault(r[0], []).append(r)

    out: list[dict[str, Any]] = []
    for v in sorted(grouped):
        cand = sorted(grouped[v], key=lambda r: r[6])   # 按 rn 排，不依赖 SQL ORDER BY
        best = cand[0]
        runner = cand[1] if len(cand) > 1 else None
        ref_close = float(best[4]) if best[4] is not None else 0.0
        mad = float(best[3])
        out.append({
            "variety_code": v,
            "trade_date": d,
            "inferred_symbol": best[1],
            "n_minutes": int(best[2]),
            "mad": mad,
            "ref_close": ref_close,
            "match_diff": round(mad / ref_close, 8) if ref_close > 0 else None,
            "n_candidates": int(best[5]),
            "method": METHOD,
            "mapped_symbol": mp.get(v),
            "agrees_with_map": best[1] == mp.get(v),
            "runner_up": runner[1] if runner else None,
            "runner_up_mad": float(runner[3]) if runner else None,
        })
    return out


def _with_fallback(items: list[dict[str, Any]], products: list[str],
                   mp: dict[str, str | None], d: date) -> list[dict[str, Any]]:
    """无 888 主连分钟数据的品种，用当日 ``main_contract_map.underlying`` 兜底。

    2026-11-12 实测 PG/PR/PS 只有具体合约分钟线、没有 ``X888``，MAD 无从判定；
    若留空，``roll_policy._resolve_inferred`` 又会静默回退到几周前的陈旧合约
    （PS 就停在 11-06 的 PS2612，而当日主力已是 PS2701）。兜底行显式标
    ``method='fallback_underlying'`` / ``n_candidates=0``，不冒充 MAD 实证。
    """
    have = {it["variety_code"] for it in items}
    out = list(items)
    for v in sorted(set(products) - have):
        out.append({
            "variety_code": v,
            "trade_date": d,
            "inferred_symbol": mp.get(v),
            "n_minutes": 0,
            "mad": None,
            "ref_close": None,
            "match_diff": None,
            "n_candidates": 0,
            "method": "fallback_underlying",
            "mapped_symbol": mp.get(v),
            "agrees_with_map": True,
            "runner_up": None,
            "runner_up_mad": None,
        })
    return sorted(out, key=lambda it: it["variety_code"])


def infer_for_date(session, d: date, *,
                   min_minutes: int = MIN_MINUTES,
                   products: list[str] | None = None) -> list[dict[str, Any]]:
    """反推一个交易日的 888 实际跟随合约。

    返回逐品种 dict，含 ``inferred_symbol`` / ``runner_up`` / ``mad`` /
    ``mapped_symbol`` / ``agrees_with_map`` 等字段。**不写库**，供 CLI 与守卫共用
    （守卫靠它做行为级对拍，口径单一真源）。
    """
    products = products if products is not None else products_on(session, d)
    if not products:
        return []
    t0 = f"{d.isoformat()} {DAY_T0}"
    t1 = f"{d.isoformat()} {DAY_T1}"
    rows = session.execute(text(_INFER_SQL), {
        "t0": t0, "t1": t1, "pat": _pattern(products), "min_minutes": min_minutes,
    }).fetchall()
    mp = mapped_on(session, d)
    return _with_fallback(_assemble(rows, mp, d), products, mp, d)


def _db_rows(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    keys = ("variety_code", "trade_date", "inferred_symbol", "ref_close",
            "match_diff", "n_candidates", "method", "mapped_symbol", "agrees_with_map")
    return [{k: it[k] for k in keys} for it in items]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", type=date.fromisoformat, help="只处理该交易日")
    ap.add_argument("--days", type=int, default=1, help="处理最近 N 个交易日（默认 1）")
    ap.add_argument("--since", type=date.fromisoformat, help="回填补齐该日之后的所有交易日")
    ap.add_argument("--min-minutes", type=int, default=MIN_MINUTES)
    ap.add_argument("--apply", action="store_true", help="真正写库（默认 dry-run）")
    args = ap.parse_args(argv)

    with session_scope() as s:
        if args.date:
            dates = [args.date]
        elif args.since:
            dates = map_dates(s, since=args.since)
        else:
            dates = map_dates(s, limit=args.days)
        if not dates:
            print("main_contract_map 无匹配交易日 → 无事可做")
            return 1

        total = 0
        disagree = 0
        for d in dates:
            items = infer_for_date(s, d, min_minutes=args.min_minutes)
            disagree += sum(1 for it in items if not it["agrees_with_map"])
            n_fb = sum(1 for it in items if it["method"] == "fallback_underlying")
            if args.apply and items:
                s.execute(text(_UPSERT), _db_rows(items))
            total += len(items)
            print(f"[{d}] 推断 {len(items)} 个品种  "
                  f"与参考主力不一致 {sum(1 for it in items if not it['agrees_with_map'])}  "
                  f"兜底(无888主连) {n_fb}  method={METHOD}")
            if args.date or len(dates) <= 2:
                for it in items:
                    mad_s = "-" if it["mad"] is None else f"{it['mad']:.4f}"
                    print("  {0:>3} 888 -> {1:<9} MAD={2:>10} "
                          "次优={3:<9}({4}) map={5} {6}{7}".format(
                              it["variety_code"], it["inferred_symbol"], mad_s,
                              it["runner_up"] or "-",
                              "-" if it["runner_up_mad"] is None
                              else f"{it['runner_up_mad']:.4f}",
                              it["mapped_symbol"] or "-",
                              "OK" if it["agrees_with_map"] else "DIFF",
                              " [FALLBACK]" if it["method"] == "fallback_underlying" else ""))
        print(f"合计 {len(dates)} 个交易日 / {total} 行 / 与参考主力不一致 {disagree} 行；"
              f"{'已写入' if args.apply else 'DRY-RUN 未写库'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
