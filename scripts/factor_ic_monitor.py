#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""因子滚动 IC 监控（T21）—— 准入/淘汰的量化依据。

做什么
------
对每个因子，按交易日做**横截面** spearman(z_value, 未来 h 日收益)，再取滚动
window 日均值，写入 ``factor_ic_roll``（migrations/004），并输出告警。

防前视口径（与 factor_ic_scan.py 一致，硬约束）
----------------------------------------------
* 因子值取 **trade_date = d** 当天可用的值（factor_value.trade_date 即决策日）；
* 前瞻收益取 **d 之后第 h 个交易日** 的收盘 / d 日收盘 − 1，绝不用 d 当天及之前；
* 每个品种用自己的交易日历对齐（shift(-h)），不做跨品种日期填充。

判据（默认，可在 CLI 覆盖）
--------------------------
* |IC_roll| < ic_floor(0.01)     → 「无效」告警
* IC_roll 方向与注册方向相反     → 「翻号」告警（direction 取 default_weight 符号）
* 因子最新 trade_date 落后 > 5 个交易日 → 「断更」告警

用法
----
    python scripts/factor_ic_monitor.py                        # 全因子 dry-run
    python scripts/factor_ic_monitor.py --apply                # 写入 factor_ic_roll
    python scripts/factor_ic_monitor.py --factors f_vol_z,f_vol_ratio --window 20
    python scripts/factor_ic_monitor.py --host host.docker.internal --port 15432 --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd
from scipy import stats
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pgconn import add_conn_args, conn_from_args  # noqa: E402

DEFAULT_FACTORS = ""   # 空 = 库内全部有数据的因子


# ---------------------------------------------------------------------------
# 取数
# ---------------------------------------------------------------------------
def _engine(a):
    c = conn_from_args(a)
    return create_engine(
        f"postgresql+psycopg2://{c['user']}:{c['password']}@{c['host']}:{c['port']}/{c['dbname']}"
    )


def load_closes(eng, symbols: list[str], since: str) -> pd.DataFrame:
    q = text(
        "SELECT symbol, trade_date, close FROM daily_bar "
        "WHERE trade_date >= :s AND close IS NOT NULL "
        "AND symbol = ANY(:syms)"
    )
    df = pd.read_sql(q, eng, params={"s": since, "syms": list(symbols)})
    return df.sort_values(["symbol", "trade_date"]).reset_index(drop=True)


def load_factor(eng, fid: str, since: str) -> pd.DataFrame:
    q = text(
        "SELECT symbol, trade_date, z_value FROM factor_value "
        "WHERE factor_id = :f AND trade_date >= :s AND z_value IS NOT NULL"
    )
    return pd.read_sql(q, eng, params={"f": fid, "s": since})


def list_factors(eng, only: str = "", enabled_only: bool = False) -> list[tuple[str, float]]:
    """返回 [(factor_id, default_weight)]，default_weight 的**符号**编码方向。"""
    sql = "SELECT f.factor_id, f.default_weight FROM factor_registry f"
    if enabled_only:
        sql += " WHERE f.enabled"
    if only:
        want = [x.strip() for x in only.split(",") if x.strip()]
        sql += (" WHERE" if not enabled_only else " AND") + " f.factor_id = ANY(:w)"
        rows = pd.read_sql(text(sql + " ORDER BY f.factor_id"), eng, params={"w": want})
    else:
        rows = pd.read_sql(text(sql + " ORDER BY f.factor_id"), eng)
    return [(r[0], float(r[1] or 0.0)) for r in rows.itertuples(index=False)]


# ---------------------------------------------------------------------------
# IC 计算
# ---------------------------------------------------------------------------
def fwd_returns(px: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """每个品种用自己的交易日历：未来第 horizon 个交易日的收益。"""
    out = []
    for sym, g in px.groupby("symbol", sort=False):
        g = g.sort_values("trade_date")
        c = g["close"].to_numpy(dtype=float)
        fwd = np.full(len(c), np.nan)
        if len(c) > horizon:
            fwd[:-horizon] = c[horizon:] / c[:-horizon] - 1.0
        out.append(pd.DataFrame({
            "symbol": sym,
            "trade_date": g["trade_date"].to_numpy(),
            "fwd_ret": fwd,
        }))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(
        columns=["symbol", "trade_date", "fwd_ret"])


def ic_by_day(fac: pd.DataFrame, ret: pd.DataFrame, min_n: int = 5) -> pd.Series:
    """横截面 spearman(因子 z, 前瞻收益)，按交易日返回 IC 序列。"""
    m = fac.merge(ret, on=["symbol", "trade_date"], how="inner")
    m = m.dropna(subset=["z_value", "fwd_ret"])
    res = {}
    for d, g in m.groupby("trade_date"):
        if len(g) < min_n:
            continue
        if g["z_value"].nunique() < 2 or g["fwd_ret"].nunique() < 2:
            continue
        ic, _ = stats.spearmanr(g["z_value"].to_numpy(), g["fwd_ret"].to_numpy())
        res[pd.Timestamp(d).date()] = float(ic)
    return pd.Series(res).sort_index()


def ic_stats(ic: pd.Series) -> dict:
    """IC 序列的汇总统计（mean / std / ICIR / t / 正比例 / 半样本同号）。"""
    if len(ic) < 3:
        return {"n": len(ic), "ic": np.nan, "icir": np.nan, "t": np.nan,
                "pos_ratio": np.nan, "split_half_same_sign": None}
    v = ic.to_numpy(dtype=float)
    mean, std = float(np.nanmean(v)), float(np.nanstd(v, ddof=1))
    n = len(v)
    half = n // 2
    same = None
    if half >= 3:
        a, b = float(np.nanmean(v[:half])), float(np.nanmean(v[half:]))
        same = (a > 0) == (b > 0)
    return {
        "n": n,
        "ic": mean,
        "icir": (mean / std) if std > 0 else np.nan,
        "t": (mean / (std / np.sqrt(n))) if std > 0 else np.nan,
        "pos_ratio": float(np.mean(v > 0)),
        "split_half_same_sign": same,
    }


def rolling_ic(ic: pd.Series, win: int) -> pd.Series:
    return ic.rolling(win, min_periods=max(3, win // 2)).mean()


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--factors", default=DEFAULT_FACTORS, help="逗号分隔；空=全部")
    ap.add_argument("--enabled-only", action="store_true")
    ap.add_argument("--since", default=str(date.today() - timedelta(days=730)))
    ap.add_argument("--window", type=int, default=20, help="滚动窗口（交易日）")
    ap.add_argument("--horizon", type=int, default=1, help="前瞻天数")
    ap.add_argument("--min-n", type=int, default=5, help="每日最少品种数")
    ap.add_argument("--ic-floor", type=float, default=0.01, help="|IC| 低于此值判无效")
    ap.add_argument("--stale-days", type=int, default=5, help="断更告警阈值（自然日）")
    ap.add_argument("--apply", action="store_true", help="写入 factor_ic_roll")
    ap.add_argument("--json", default="", help="可选：结果 JSON 落盘路径")
    add_conn_args(ap)
    a = ap.parse_args()

    eng = _engine(a)
    facs = list_factors(eng, a.factors, a.enabled_only)
    if not facs:
        print("[ic] 没有待监控因子（检查 --factors / --enabled-only）")
        return

    # 价格只取一次（全符号子集 = 因子用到的符号）
    with eng.connect() as c:
        syms = [r[0] for r in c.execute(text(
            "SELECT DISTINCT symbol FROM factor_value WHERE trade_date >= :s"
        ), {"s": a.since}).fetchall()]
    px = load_closes(eng, syms, a.since)
    if px.empty:
        print("[ic] daily_bar 无覆盖数据，退出")
        return
    ret = fwd_returns(px, a.horizon)

    rows = []
    today = date.today()
    for fid, w in facs:
        f = load_factor(eng, fid, a.since)
        if f.empty:
            print(f"[ic] {fid:<24} 无因子值，跳过")
            continue
        ic = ic_by_day(f, ret, a.min_n)
        if ic.empty:
            print(f"[ic] {fid:<24} 截面样本不足（min_n={a.min_n}），跳过")
            continue
        roll = rolling_ic(ic, a.window).dropna()
        last_ic = float(roll.iloc[-1]) if len(roll) else float(ic.iloc[-1])
        last_std = float(ic.rolling(a.window, min_periods=3).std().dropna().iloc[-1]) \
            if len(ic) > 3 else np.nan
        n_sym = int(f.groupby("trade_date")["symbol"].nunique().mean())
        latest = pd.Timestamp(f["trade_date"].max()).date()
        stale = (today - latest).days > a.stale_days

        st = ic_stats(ic)
        direction = 1.0 if w >= 0 else -1.0
        flags = []
        if abs(last_ic) < a.ic_floor:
            flags.append("弱/无效")
        if last_ic * direction < 0:
            flags.append("翻号")
        if stale:
            flags.append(f"断更({latest})")
        rows.append({
            "factor_id": fid, "n_days": st["n"], "n_sym": n_sym,
            "ic_full": st["ic"], "icir": st["icir"], "t": st["t"],
            "ic_roll": last_ic, "ic_roll_std": last_std,
            "pos_ratio": st["pos_ratio"], "split_same": st["split_half_same_sign"],
            "latest": latest, "flags": ",".join(flags) or "-",
        })

        if a.apply:
            with eng.begin() as conn:
                for d, v in roll.items():
                    sd = ic.rolling(a.window, min_periods=3).std().loc[:d]
                    sdv = float(sd.dropna().iloc[-1]) if len(sd.dropna()) else None
                    conn.execute(text("""
                        INSERT INTO factor_ic_roll
                            (factor_id, trade_date, win_days, horizon, ic, icir, n_symbols, method)
                        VALUES (:f, :d, :w, :h, :ic, :icir, :n, 'spearman')
                        ON CONFLICT (factor_id, trade_date, win_days, horizon) DO UPDATE
                           SET ic=EXCLUDED.ic, icir=EXCLUDED.icir,
                               n_symbols=EXCLUDED.n_symbols, created_at=now()
                    """), {"f": fid, "d": d, "w": a.window, "h": a.horizon,
                           "ic": float(v), "icir": (float(v) / sdv) if sdv else None,
                           "n": n_sym})

    df = pd.DataFrame(rows)
    if df.empty:
        print("[ic] 无结果")
        return
    df = df.sort_values("ic_roll", key=lambda s: s.abs(), ascending=False)
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(df.to_string(index=False, float_format=lambda x: f"{x: .4f}"))
    bad = df[df["flags"] != "-"]
    if len(bad):
        print(f"\n[ic] ⚠ 需复核 {len(bad)} 个因子：")
        for r in bad.itertuples(index=False):
            print(f"     {r.factor_id:<24} {r.flags}")
    if a.json:
        df.to_json(a.json, orient="records", force_ascii=False, indent=2)
        print(f"[ic] JSON → {a.json}")


if __name__ == "__main__":
    main()
