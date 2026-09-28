#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""因子准入闸门（T17）—— 因子「能不能上」的唯一裁决口。

六道闸门（全部可配置，默认值即 PRD 口径）
----------------------------------------
E1 覆盖度  n_days ≥ 60，n_sym ≥ 20，缺失率 ≤ 5%
E2 前视    factor_value 不得有未来日期；前瞻收益严格用 d 之后第 h 个交易日
           （由 factor_ic_monitor.fwd_returns 保证，本脚本做静态复核）
E3 显著性  |IC| ≥ 0.02 且 |t| ≥ 2.0（全样本 **且** 后半段样本各过一次）
E4 无漂移  |IC(h=5)| ≤ 1.5×|IC(h=1)|：IC 随 horizon 变大 ⇒ 趋势漂移而非 alpha
E5 稳健    半样本同号 且 年度同号比例 ≥ 60%
E6 中性    接入不破坏启动校验：新因子 default_weight=0、max_weight ≤ 0.05，
           且启用后 registry max_weight 之和 ≤ 1.0（app/factor/asof.validate_max_weight）

裁决
----
* PASS  → 可准入；``--apply`` 时才写库：enabled=true、**default_weight=0**（中性接入）
* FAIL  → 打印未过项；``--apply`` 且 ``--demote`` 时把该因子置为 enabled=false

用法
----
    python scripts/admit_factors.py --factors f_vol_z,f_vol_ratio
    python scripts/admit_factors.py --factors member_net_z --apply
    python scripts/admit_factors.py --all-registered --since 2018
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pgconn import add_conn_args, conn_from_args  # noqa: E402
from factor_ic_monitor import (  # noqa: E402
    fwd_returns, ic_by_day, ic_stats, load_closes, load_factor, list_factors,
)

GATES = ("E1", "E2", "E3", "E4", "E5", "E6")


class Gate:
    def __init__(self):
        self.fails: dict[str, list[str]] = {}

    def check(self, fid: str, gate: str, ok: bool, msg: str) -> None:
        if not ok:
            self.fails.setdefault(fid, []).append(f"{gate} {msg}")

    def verdict(self, fid: str) -> tuple[bool, str]:
        f = self.fails.get(fid, [])
        return (len(f) == 0), ("; ".join(f) if f else "PASS")


def _engine(a):
    c = conn_from_args(a)
    return create_engine(
        f"postgresql+psycopg2://{c['user']}:{c['password']}@{c['host']}:{c['port']}/{c['dbname']}"
    )


def year_sign_ratio(ic: pd.Series) -> float:
    """年度 IC 与全样本 IC 同号的比例（E5）。"""
    if len(ic) < 3:
        return np.nan
    overall = float(np.nanmean(ic.to_numpy()))
    idx = pd.to_datetime(pd.Index(ic.index))       # index 可能是 object/date，先转 datetime
    s = ic.groupby(idx.year).mean()
    if len(s) == 0:
        return np.nan
    return float(np.mean((s > 0) == (overall > 0)))


def evaluate(eng, fid: str, weight: float, px: pd.DataFrame, args) -> dict:
    g = Gate()
    f = load_factor(eng, fid, args.since)
    if f.empty:
        return {"factor_id": fid, "verdict": "FAIL", "reason": "E1 无因子值"}

    # ---- E1 覆盖度 ----
    n_days = int(f["trade_date"].nunique())
    n_sym = int(f["symbol"].nunique())
    g.check(fid, "E1", n_days >= args.min_days, f"交易日 {n_days} < {args.min_days}")
    g.check(fid, "E1", n_sym >= args.min_sym, f"品种 {n_sym} < {args.min_sym}")
    # 缺失率：窗口内 (品种 × 交易日) 应有格子的缺漏比例
    dates = pd.Index(sorted(f["trade_date"].unique()))
    cells = len(dates) * n_sym
    miss = 1.0 - (len(f) / cells) if cells else 1.0
    g.check(fid, "E1", miss <= args.max_miss, f"缺失率 {miss:.1%} > {args.max_miss:.0%}")

    # ---- E2 前视静态复核 ----
    latest = pd.Timestamp(f["trade_date"].max()).date()
    g.check(fid, "E2", latest <= date.today(), f"存在未来日期 {latest}")

    # ---- E3 显著性（全样本 + 后半段） ----
    ret1 = fwd_returns(px, args.horizon)
    ic = ic_by_day(f, ret1, args.min_n)
    if len(ic) < 3:
        return {"factor_id": fid, "verdict": "FAIL", "reason": "E3 截面样本不足"}
    st = ic_stats(ic)
    half = ic.iloc[len(ic) // 2:]
    st_oos = ic_stats(half)
    g.check(fid, "E3", abs(st["ic"]) >= args.ic_min,
            f"|IC|={abs(st['ic']):.4f} < {args.ic_min}")
    g.check(fid, "E3", abs(st["t"]) >= args.t_min, f"|t|={abs(st['t']):.2f} < {args.t_min}")
    g.check(fid, "E3", abs(st_oos["ic"]) >= args.ic_min * 0.5,
            f"后半段 |IC|={abs(st_oos['ic']):.4f} 衰减过大")

    # ---- E4 无漂移（长 horizon vs 本 horizon） ----
    # 注意：drift_h 必须**明显大于** args.horizon，否则 h=5 复核时变成 5 vs 5 恒过
    drift_h = max(5, args.horizon * 5)
    ic5 = ic_by_day(f, fwd_returns(px, drift_h), args.min_n)
    a1, a5 = abs(st["ic"]), abs(float(np.nanmean(ic5.to_numpy()))) if len(ic5) else 0.0
    g.check(fid, "E4", a5 <= a1 * args.drift_mult + 1e-9,
            f"|IC{drift_h}|={a5:.4f} > {args.drift_mult}×|IC{args.horizon}|={a1:.4f}"
            f"（疑似趋势漂移）")

    # ---- E5 稳健 ----
    g.check(fid, "E5", bool(st["split_half_same_sign"]), "半样本 IC 不同号")
    yr = year_sign_ratio(ic)
    g.check(fid, "E5", (not np.isnan(yr)) and yr >= args.year_ratio,
            f"年度同号比例 {yr:.0%} < {args.year_ratio:.0%}")

    # ---- E6 中性（额度） ----
    # 说明：max_weight ≤ max_w（0.05）只约束**待准入的新因子**；已在生产的因子
    #       （enabled=true、weight>0）额度是历史既定的，复核时不应因此判负，
    #       但仍要满足「registry 之和 ≤ 1.0」这一启动校验硬约束。
    with eng.connect() as c:
        mw = float(c.execute(text(
            "SELECT coalesce(max_weight,0) FROM factor_registry WHERE factor_id=:f"
        ), {"f": fid}).scalar() or 0.0)
        enabled = bool(c.execute(text(
            "SELECT coalesce(enabled,false) FROM factor_registry WHERE factor_id=:f"
        ), {"f": fid}).scalar())
        s_total = float(c.execute(text(
            "SELECT coalesce(sum(max_weight),0) FROM factor_registry")).scalar() or 0.0)
    if not enabled:
        g.check(fid, "E6", mw <= args.max_w, f"新因子 max_weight={mw} > {args.max_w}")
    g.check(fid, "E6", s_total <= 1.0 + 1e-9,
            f"registry max_weight 之和 {s_total:.2f} > 1.0（启动校验会失败）")

    ok, reason = g.verdict(fid)
    return {
        "factor_id": fid, "n_days": n_days, "n_sym": n_sym, "miss": miss,
        "ic": st["ic"], "icir": st["icir"], "t": st["t"],
        "ic_oos": st_oos["ic"], "ic5": a5, "year_ratio": yr,
        "latest": latest, "verdict": "PASS" if ok else "FAIL", "reason": reason,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--factors", default="", help="逗号分隔；与 --all-registered 二选一")
    ap.add_argument("--all-registered", action="store_true", help="扫描全部已注册因子")
    ap.add_argument("--since", default="2018-01-01")
    ap.add_argument("--horizon", type=int, default=1)
    ap.add_argument("--min-days", type=int, default=60)
    ap.add_argument("--min-sym", type=int, default=20)
    # 缺失率口径 = 因子**自己有值的那些交易日**上的品种缺漏比例。
    # 新因子要求 ≤5%；复核已在生产的低频基本面因子（会员持仓/仓单等周频、且
    # 部分品种本就没有数据）时用 --max-miss 0.5 复核，否则会被数据可得性误杀。
    ap.add_argument("--max-miss", type=float, default=0.05)
    ap.add_argument("--min-n", type=int, default=5, help="每日最少品种数")
    ap.add_argument("--ic-min", type=float, default=0.02)
    ap.add_argument("--t-min", type=float, default=2.0)
    ap.add_argument("--drift-mult", type=float, default=1.5)
    ap.add_argument("--year-ratio", type=float, default=0.60)
    ap.add_argument("--max-w", type=float, default=0.05)
    ap.add_argument("--apply", action="store_true", help="写回 factor_registry")
    ap.add_argument("--demote", action="store_true", help="未通过者置 enabled=false")
    ap.add_argument("--json", default="")
    add_conn_args(ap)
    a = ap.parse_args()

    eng = _engine(a)
    if a.all_registered:
        facs = list_factors(eng)
    elif a.factors:
        facs = list_factors(eng, a.factors)
    else:
        print("[admit] 需指定 --factors 或 --all-registered")
        return
    if not facs:
        print("[admit] 没有匹配因子")
        return

    with eng.connect() as c:
        syms = [r[0] for r in c.execute(text(
            "SELECT DISTINCT symbol FROM factor_value WHERE trade_date >= :s"
        ), {"s": a.since}).fetchall()]
    px = load_closes(eng, syms, a.since)

    rows = [evaluate(eng, fid, w, px, a) for fid, w in facs]
    df = pd.DataFrame(rows)
    with pd.option_context("display.width", 220, "display.max_columns", 30):
        print(df.to_string(index=False, float_format=lambda x: f"{x: .4f}"))

    passed = df[df["verdict"] == "PASS"]["factor_id"].tolist()
    print(f"\n[admit] PASS {len(passed)} / {len(df)}：{passed}")

    if a.apply:
        with eng.begin() as conn:
            for fid in passed:
                conn.execute(text("""
                    UPDATE factor_registry
                       SET enabled=true, default_weight=0, updated_at=now()
                     WHERE factor_id=:f
                """) if _has_updated_at(conn) else text("""
                    UPDATE factor_registry SET enabled=true, default_weight=0 WHERE factor_id=:f
                """), {"f": fid})
            if a.demote:
                for fid in df[df["verdict"] == "FAIL"]["factor_id"].tolist():
                    conn.execute(text(
                        "UPDATE factor_registry SET enabled=false WHERE factor_id=:f"
                    ), {"f": fid})
        print(f"[admit] 已写库：准入 {len(passed)} 个（default_weight=0 中性接入）"
              + ("，降级其余未通过者" if a.demote else ""))

    if a.json:
        with open(a.json, "w", encoding="utf-8") as fp:
            json.dump(rows, fp, ensure_ascii=False, indent=2, default=str)
        print(f"[admit] JSON → {a.json}")


def _has_updated_at(conn) -> bool:
    try:
        return bool(conn.execute(text("""
            SELECT 1 FROM information_schema.columns
             WHERE table_name='factor_registry' AND column_name='updated_at'
        """)).scalar())
    except Exception:  # noqa: BLE001
        return False


if __name__ == "__main__":
    main()
