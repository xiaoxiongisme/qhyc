"""十二期审计（§34）：对全程 一期→十二期 的矛盾/幻觉/遗漏做可复现举证。

只读。输出 JSON 供 §34 引用。覆盖五组事实：
  A 逐年口径：重置 vs 切片（§31.4 vs §32.4 的互斥来源）
  B 06-15 归属：日盘 0 行 + 前周五夜盘跨零（§32.1 的解释是否成立）
  C hourly_bar vs bar_60m：同网格、数值不等（"实盘零影响"的边界）
  D 5m/60m 标签语义（§30 R1 的举证是否成立）
  E 库结构事实：非 888 合约数、脚本数、交易日基数

用法：
  PYTHONPATH=/tmp/aeq:/tmp/aeq/scripts python scripts/signal_b12_audit.py --out /tmp/aeq/b12_audit.json
"""

from __future__ import annotations

import argparse
import json
import math
from datetime import date

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings


def q(session, sql, params=None):
    return session.execute(text(sql), params or {}).all()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/aeq/b12_audit.json")
    args = ap.parse_args()

    eng = create_engine(get_settings().db_url, pool_size=2, max_overflow=4)
    session = sessionmaker(bind=eng)()
    out: dict = {"meta": {"generated_at": pd.Timestamp.now().isoformat(timespec="seconds"),
                          "scope": "§34 全程审计"}}

    # ---------- A 逐年口径：重置 vs 切片 ----------
    reset_years = [1.00644, 1.219148, 1.177082, 1.090266, 0.87227]   # §31.4 表（重置）
    slice_years = [1.00644, 1.22479, 1.21832, 1.15144, 1.04236]      # kstar by_year（切片）
    out["A_year_basis"] = {
        "reset_product": round(math.prod(reset_years), 4),
        "reset_total_pct": round(math.prod(reset_years) - 1, 4),
        "slice_product": round(math.prod(slice_years), 4),
        "slice_total_pct": round(math.prod(slice_years) - 1, 4),
        "full_period_nav_doc": 1.815624,
        "full_period_nav_pct": 0.815624,
        "gap_pp": round(0.815624 - (math.prod(reset_years) - 1), 4),
        "verdict": "§31.4 逐年行连乘 +37.35% ≠ 同表全期 +81.56% → 该列是重置账户，非全期切片",
        "y2026_reset": {"ret": -0.12773, "mdd": 0.344201},
        "y2026_slice": {"ret": 0.04236, "mdd": 0.13506},
        "gate3_2026": {"by_reset": "不过", "by_slice": "过"},
    }

    # ---------- B 06-15 归属 ----------
    g_day = q(session, """SELECT count(*), count(DISTINCT symbol) FROM l1_mkt.bar_60m
                          WHERE bucket >= '2026-06-15' AND bucket < '2026-06-16'""")[0]
    night = q(session, """SELECT count(*), count(DISTINCT symbol) FROM l1_mkt.bar_60m
                          WHERE bucket >= '2026-06-12 20:00' AND bucket < '2026-06-13 03:00'""")[0]
    night_labels = [r[0].strftime("%m-%d %H:%M") for r in q(
        session, """SELECT DISTINCT bucket FROM l1_mkt.bar_60m
                    WHERE bucket >= '2026-06-12 20:00' AND bucket < '2026-06-13 03:00' ORDER BY 1""")]
    out["B_g0615"] = {
        "day_session_rows": g_day[0], "day_session_symbols": g_day[1],
        "prev_friday_night_rows": night[0], "prev_friday_night_symbols": night[1],
        "night_labels": night_labels,
        "verdict": ("06-15 日盘 0 行；47 品种来自 06-12 22:00–06-13 02:30 的周五夜盘（归入下一交易日）。"
                    "§32.1 的『47 品种 / 匹配率 0.186 的半日截断』数字对、机理错：不是半日，是夜盘跨零残留。"
                    "副作用：监控脚本在『整日缺失但夜盘残留』边界把 absent 判成 incomplete（少报 1 天）。"),
    }

    # ---------- C hourly_bar vs bar_60m ----------
    cmp = q(session, """WITH h AS (SELECT symbol, trade_datetime AS t, open, high, low, close
                                   FROM l0_raw.hourly_bar
                                   WHERE trade_datetime >= '2026-10-08' AND trade_datetime < '2026-11-01'),
                              b AS (SELECT symbol, bucket AS t, open, high, low, close
                                   FROM l1_mkt.bar_60m
                                   WHERE bucket >= '2026-10-08' AND bucket < '2026-11-01')
                        SELECT count(*) n,
                               count(*) FILTER (WHERE abs(h.open-b.open)<1e-6 AND abs(h.high-b.high)<1e-6
                                                 AND abs(h.low-b.low)<1e-6 AND abs(h.close-b.close)<1e-6) n_eq,
                               count(*) FILTER (WHERE abs(h.close-b.close)<1e-6) n_close_eq
                        FROM h JOIN b ON h.symbol=b.symbol AND h.t=b.t""")[0]
    rb = q(session, """SELECT bucket, open, close, volume FROM l1_mkt.bar_60m
                       WHERE symbol='RB888' AND bucket >= '2026-10-30' AND bucket < '2026-10-31'
                       ORDER BY bucket""")
    rb_h = q(session, """SELECT trade_datetime, open, close, volume FROM l0_raw.hourly_bar
                         WHERE symbol='RB888' AND trade_datetime >= '2026-10-30' AND trade_datetime < '2026-10-31'
                         ORDER BY trade_datetime""")
    out["C_hourly_vs_60m"] = {
        "n": cmp[0], "n_ohlc_all_eq": cmp[1], "n_close_eq": cmp[2],
        "ohlc_eq_rate": round(cmp[1] / cmp[0], 4) if cmp[0] else None,
        "close_eq_rate": round(cmp[2] / cmp[0], 4) if cmp[0] else None,
        "rb888_1030": [{"t": r[0].strftime("%H:%M"), "hourly": [float(r_h[1]), float(r_h[3])],
                        "bar60": [float(r[1]), float(r[3])]} for r, r_h in zip(rb, rb_h)],
        "verdict": ("同网格、数值不等。回测 fusion 读 bar_60m（fusion_backtest.py 2026-10-31 起硬编码），"
                    "实盘读 cfg.hourly_src=akshare → hourly_bar。『实盘零影响』只对 11-02 当天成立，"
                    "长期的回测-实盘价差在此。"),
    }

    # ---------- D 5m 标签语义 ----------
    lab = {}
    for d in ("2026-10-30", "2026-11-02"):
        rows = q(session, """SELECT bucket FROM l1_mkt.bar_5m WHERE symbol='RB888'
                             AND bucket >= :a AND bucket < :b ORDER BY bucket""",
                 {"a": d, "b": d + " 23:59:59"})
        lab[d] = {"first": rows[0][0].strftime("%H:%M"), "last": rows[-1][0].strftime("%H:%M"),
                  "n": len(rows)}
    m5 = q(session, """SELECT bucket, open, close, volume FROM l1_mkt.bar_5m
                       WHERE symbol='RB888' AND bucket >= '2026-10-30 09:00' AND bucket <= '2026-10-30 09:10'
                       ORDER BY bucket""")
    mins = q(session, """SELECT ts, volume FROM l0_raw.minute_bar
                         WHERE symbol='RB888' AND ts >= '2026-10-30 09:01' AND ts <= '2026-10-30 09:05'
                         ORDER BY ts""")
    out["D_label_semantics"] = {
        "rb888_5m": lab,
        "bar_0905": {"open": float(m5[0][1]), "close": float(m5[0][2]), "volume": int(m5[0][3])},
        "minutes_0901_0905_volume_sum": int(sum(r[1] for r in mins)),
        "verdict": ("正常日 5m 首根 09:05、其量 = ts 09:01–09:05 之和 → 5m 是**右端**标签；"
                    "11-02 首根 09:00 → 该日 5m 也变左端。§30 R1 用『最后一根 14:55』判左端不成立"
                    "（11-02 也有 15:00），应以**头部**判。"),
    }

    # ---------- E 库结构事实 ----------
    out["E_structure"] = {
        "daily_symbols": q(session, "SELECT count(DISTINCT symbol) FROM l0_raw.daily_bar")[0][0],
        "daily_888": q(session, "SELECT count(DISTINCT symbol) FROM l0_raw.daily_bar WHERE symbol LIKE '%888'")[0][0],
        "daily_non888": q(session, "SELECT count(DISTINCT symbol) FROM l0_raw.daily_bar WHERE symbol NOT LIKE '%888'")[0][0],
        "daily_non888_prefix": {r[0][:2]: r[1] for r in q(
            session, """SELECT symbol, count(*) FROM (SELECT DISTINCT symbol FROM l0_raw.daily_bar
                        WHERE symbol NOT LIKE '%888') x GROUP BY 1""")},
        "bar60_non888": q(session, "SELECT count(DISTINCT symbol) FROM l1_mkt.bar_60m WHERE symbol NOT LIKE '%888'")[0][0],
        "minute_non888": q(session, "SELECT count(DISTINCT symbol) FROM l0_raw.minute_bar WHERE symbol NOT LIKE '%888'")[0][0],
        "hourly_non888": q(session, "SELECT count(DISTINCT symbol) FROM l0_raw.hourly_bar WHERE symbol NOT LIKE '%888'")[0][0],
        "days_2026": q(session, """SELECT count(DISTINCT trade_date) FROM l0_raw.daily_bar
                                   WHERE symbol LIKE '%888' AND trade_date >= '2026-01-01'
                                     AND trade_date <= '2026-12-31'""")[0][0],
        "days_with_day_bars": q(session, """SELECT count(DISTINCT bucket::date) FROM l1_mkt.bar_60m
                                            WHERE bucket >= '2021-01-01' AND bucket < '2026-11-04'
                                              AND extract(hour from bucket) BETWEEN 9 AND 15""")[0][0],
        "wallclock_days": {
            "h9": q(session, "SELECT count(*), count(DISTINCT bucket::date) FROM l1_mkt.bar_60m WHERE extract(hour from bucket)=9")[0],
            "h13": q(session, "SELECT count(*), count(DISTINCT bucket::date) FROM l1_mkt.bar_60m WHERE extract(hour from bucket)=13")[0],
        },
        "rows_1102": {
            "bar60": q(session, "SELECT count(*), count(DISTINCT symbol) FROM l1_mkt.bar_60m WHERE bucket>='2026-11-02' AND bucket<'2026-11-03'")[0],
            "bar5": q(session, "SELECT count(*), count(DISTINCT symbol) FROM l1_mkt.bar_5m WHERE bucket>='2026-11-02' AND bucket<'2026-11-03'")[0],
            "hourly": q(session, "SELECT count(*), count(DISTINCT symbol) FROM l0_raw.hourly_bar WHERE trade_datetime>='2026-11-02' AND trade_datetime<'2026-11-03'")[0],
        },
        "mcm": {"rows": q(session, "SELECT count(*) FROM public.main_contract_map")[0][0],
                "products": q(session, "SELECT count(DISTINCT product) FROM public.main_contract_map")[0][0]},
        "rule_codes": q(session, "SELECT count(DISTINCT code) FROM public.futures_rule")[0][0],
    }

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1, default=str)

    print("[A]", out["A_year_basis"]["verdict"])
    print(f"    reset_product={out['A_year_basis']['reset_product']} vs full={out['A_year_basis']['full_period_nav_doc']}")
    print("[B]", out["B_g0615"]["day_session_rows"], "rows on 06-15 day;",
          out["B_g0615"]["prev_friday_night_symbols"], "symbols from prev Friday night")
    print(f"[C] OHLC 全等率 {out['C_hourly_vs_60m']['ohlc_eq_rate']:.1%} of {out['C_hourly_vs_60m']['n']}")
    print("[D]", out["D_label_semantics"]["rb888_5m"])
    print("[E] daily_non888 =", out["E_structure"]["daily_non888"],
          "| days_2026 =", out["E_structure"]["days_2026"])
    print(f"[out] {args.out}")


if __name__ == "__main__":
    main()
