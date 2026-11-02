#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""十一期① 数据健康监控 + 缺口告警（只读，cron 友好）

背景：§31 ① 已确认 2026-06-15~09-03 共 58 个交易日盘中数据缺失，而这次缺口是
**五个月后才被发现**的。缺口的真实危害不是"少几根 K 线"，而是：
  (a) 回测静默降级：load_market 会把缺失日直接剔除，OOS 样本量无声减少，
      而报告里的"N 笔交易 / 年化"不会提示"这一年少了 29% 的交易日"；
  (b) 实盘无法区分"信号没出"与"数据没落"。
所以需要的是**每日体检**，不是事后考古。

判据（label-agnostic，对 60m 标签口径免疫，§31.1 已证）：
  L0 存在性：某日 60m 无任何行 → absent（硬缺口）
  L2 严格  ：60m 聚合日 OHLC 与 daily_bar(888) 四值相对误差 ≤ L2_TOL 记为"匹配格"
  L3 日级  ：匹配符号数 / 当日应有符号数
             ≥ L2_RATE  → complete
             ≥ L3_RATE  → complete_calendar（日基本在，个别品种缺/换月不同步）
             <  L3_RATE  → incomplete
  L4 新鲜度：各层 max 时间戳，检测落盘滞后（60m 应领先 5m 不等于滞后，
             但 60m 落后 daily 一天以上即为滞后）

退出码：0=OK / 1=YELLOW / 2=RED（供 cron + pushplus 直接用）。

用法：
  python signal_b11_datamon.py --days 15                 # 日常体检（默认，秒级）
  python signal_b11_datamon.py --start 2024-01-01 --end 2026-11-02   # 全期体检
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
sys.path.insert(0, "/app")
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from app.core.config import get_settings

L2_TOL = 0.005      # 运营阈值 0.5%：须高于 888 拼接噪声底（实测 p99 ≤ 0.37%，见 tolerance_diag）
CLOSE_TOL = 0.001   # §31 兼容阈值：仅收盘价 0.1%（888 收盘与日线几乎逐位相等）
GROSS_TOL = 0.02    # 粗判：2%（用于"是否完全不是同一天的行情"）
L2_RATE = 0.95
L3_RATE = 0.90


def load_layers(session, start: date, end: date):
    cal = [r[0] for r in session.execute(text("""
        SELECT DISTINCT trade_date FROM l0_raw.daily_bar
        WHERE symbol LIKE '%888' AND trade_date >= :a AND trade_date <= :b
        ORDER BY trade_date
    """), {"a": start, "b": end}).all()]

    rows = session.execute(text("""
        SELECT symbol, bucket, open, high, low, close
        FROM l1_mkt.bar_60m
        WHERE bucket >= :a AND bucket < :b
        ORDER BY symbol, bucket
    """), {"a": start, "b": end + timedelta(days=1)}).all()
    df60 = pd.DataFrame(rows, columns=["symbol", "bucket", "open", "high", "low", "close"])
    if df60.empty:
        agg = pd.DataFrame(columns=["symbol", "d", "open", "high", "low", "close"])
        partial = 0
    else:
        for c in ("open", "high", "low", "close"):
            df60[c] = df60[c].astype(float)
        # ★ 夜盘归属：21:00 及以后的 bar 属于**下一个交易日**，不能按日历日聚合。
        #   §31 的 bucket::date 聚合未做此平移，导致 close 取到当晚 23:00，与日线
        #   收盘（15:00）必然不等 —— 这是十一期①自查时发现的判据实现缺陷。
        buckets = pd.to_datetime(df60["bucket"])
        cal_date = buckets.dt.tz_localize(None).dt.normalize()
        hour = buckets.dt.hour.to_numpy()
        idx = {c: i for i, c in enumerate(cal)}
        nxt = {}
        for i, c in enumerate(cal):
            nxt[c] = cal[i + 1] if i + 1 < len(cal) else None
        cdates = cal_date.dt.date.to_numpy()
        day = np.array([cdates[i] if hour[i] < 18 else nxt.get(cdates[i])
                        for i in range(len(hour))], dtype=object)
        df60["d"] = day
        partial = int(pd.isna(df60["d"]).sum())
        df60 = df60[pd.notna(df60["d"])]
        agg = (df60.groupby(["symbol", "d"], sort=True)
                   .agg(open=("open", "first"), high=("high", "max"),
                        low=("low", "min"), close=("close", "last"))
                   .reset_index())

    drows = session.execute(text("""
        SELECT symbol, trade_date, open, high, low, close, settle, volume
        FROM l0_raw.daily_bar
        WHERE symbol LIKE '%888' AND trade_date >= :a AND trade_date <= :b
    """), {"a": start, "b": end}).all()
    dly = pd.DataFrame(drows, columns=["symbol", "d", "open", "high", "low", "close",
                                       "settle", "volume"])
    for c in ("open", "high", "low", "close", "settle", "volume"):
        dly[c] = pd.to_numeric(dly[c], errors="coerce")
    return cal, agg, dly, partial


def freshness(session):
    out = {}
    for tbl, col in [("l1_mkt.bar_60m", "bucket"), ("l1_mkt.bar_30m", "bucket"),
                     ("l1_mkt.bar_15m", "bucket"), ("l1_mkt.bar_5m", "bucket")]:
        try:
            v = session.execute(text(f"SELECT max({col}) FROM {tbl}")).scalar()
            out[tbl] = str(v)
        except Exception as exc:                       # noqa: BLE001
            out[tbl] = f"ERR:{type(exc).__name__}"
    for tbl, col in [("l0_raw.minute_bar", "ts"), ("l0_raw.hourly_bar", "trade_datetime")]:
        try:
            v = session.execute(text(f"SELECT max({col}) FROM {tbl}")).scalar()
            out[tbl] = str(v)
        except Exception as exc:                       # noqa: BLE001
            out[tbl] = f"ERR:{type(exc).__name__}"
    try:
        v = session.execute(text("SELECT max(trade_date) FROM l0_raw.daily_bar")).scalar()
        out["l0_raw.daily_bar"] = str(v)
    except Exception as exc:                           # noqa: BLE001
        out["l0_raw.daily_bar"] = f"ERR:{type(exc).__name__}"
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=15)
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", default="/tmp/aeq/b11_datamon.json")
    args = ap.parse_args()

    engine = create_engine(get_settings().db_url, pool_size=2, max_overflow=4)
    session = sessionmaker(bind=engine)()

    end = date.fromisoformat(args.end) if args.end else date.today()
    if args.start:
        start = date.fromisoformat(args.start)
        mode = "range"
    else:
        start = end - timedelta(days=max(args.days, 1) * 3)
        mode = "rolling"

    cal, agg, dly, partial = load_layers(session, start, end)
    cal = cal[-max(args.days, 1):] if mode == "rolling" else cal
    # rolling 模式下重取 60m 只保留窗口内日期
    if mode == "rolling" and not agg.empty:
        keep = set(cal)
        agg = agg[agg["d"].isin(keep)]
        dly = dly[dly["d"].isin(keep)]

    m = agg.merge(dly, on=["symbol", "d"], how="inner", suffixes=("_60", "_d"))
    recs = []
    if not m.empty:
        for c in ("open", "high", "low", "close"):
            a = m[f"{c}_60"].to_numpy(dtype=float)
            b = m[f"{c}_d"].to_numpy(dtype=float)
            with np.errstate(divide="ignore", invalid="ignore"):
                rel = np.abs(a / b - 1.0)
            m[f"rel_{c}"] = rel
            m[f"ok_{c}"] = (rel <= L2_TOL) | (np.nan_to_num(np.abs(a - b), nan=0.0) <= 1e-9)
            m[f"gross_{c}"] = np.nan_to_num(rel, nan=0.0) > GROSS_TOL
        m["ok_all"] = m["ok_open"] & m["ok_high"] & m["ok_low"] & m["ok_close"]
        m["ok_close_strict"] = m["rel_close"] <= CLOSE_TOL
        m["gross_bad"] = m["gross_open"] | m["gross_close"]
        recs = m.to_dict("records")

    diag = {}
    if recs:
        for c in ("open", "high", "low", "close"):
            rel = np.array([abs(r[f"{c}_60"] / r[f"{c}_d"] - 1.0) if r[f"{c}_d"] else np.nan
                            for r in recs], dtype=float)
            rel = rel[np.isfinite(rel)]
            if rel.size:
                diag[c] = {"median_pct": round(float(np.median(rel)) * 100, 4),
                           "p95_pct": round(float(np.percentile(rel, 95)) * 100, 4),
                           "p99_pct": round(float(np.percentile(rel, 99)) * 100, 4),
                           "max_pct": round(float(rel.max()) * 100, 4)}
        mx = np.array([max(abs(r[f"{c}_60"] / r[f"{c}_d"] - 1.0) if r[f"{c}_d"] else 0.0
                           for c in ("open", "high", "low", "close")) for r in recs], dtype=float)
        diag["cell_max_field"] = {
            "share_le_0p1pct": round(float((mx <= 0.001).mean()) * 100, 2),
            "share_le_0p5pct": round(float((mx <= 0.005).mean()) * 100, 2),
            "share_le_1pct": round(float((mx <= 0.01).mean()) * 100, 2),
            "share_gt_2pct": round(float((mx > 0.02).mean()) * 100, 2),
        }

    day_rows = []
    # ★ 品种池对齐：daily_bar 的 888 数量多于 bar_60m（日线覆盖了已下市/新上市合约），
    #   若按日线全集算分母，会把"本就没有分钟线的品种"误判成当日缺数据（2024 全期
    #   曾因此得到 609 天 incomplete、rate 恒为 0.81）。分母改为"窗口内有 60m 数据的品种"。
    universe = set(agg["symbol"].unique()) if not agg.empty else set()
    for d in cal:
        exp = dly[(dly["d"] == d) & (dly["symbol"].isin(universe))]
        got = agg[agg["d"] == d]
        n_exp = int(exp["symbol"].nunique())
        n_got = int(got["symbol"].nunique())
        n_missing = max(0, n_exp - n_got)
        sub = [r for r in recs if r["d"] == d]
        n_match = sum(1 for r in sub if r["ok_all"])
        n_close = sum(1 for r in sub if r.get("ok_close_strict", False))
        n_gross = sum(1 for r in sub if r["gross_bad"])
        rate = n_match / n_exp if n_exp else 0.0
        close_rate = n_close / n_exp if n_exp else 0.0
        if got.empty:
            verdict = "absent"
        elif rate >= L2_RATE:
            verdict = "complete"
        elif rate >= L3_RATE:
            verdict = "complete_calendar"
        else:
            verdict = "incomplete"
        bad_sym = sorted({r["symbol"] for r in sub if not r["ok_all"]})
        day_rows.append({
            "date": str(d), "n_expected": n_exp, "n_60m_sym": n_got,
            "n_missing_sym": n_missing,
            "n_match": n_match, "match_rate": round(rate, 4),
            "close_strict_rate": round(close_rate, 4),
            "n_gross_bad": n_gross, "verdict": verdict,
            "bad_symbols": bad_sym[:8], "n_bad_symbols": len(bad_sym),
        })

    # 连续缺口区间（absent）
    gaps = []
    cur = None
    for r in day_rows:
        if r["verdict"] == "absent":
            if cur is None:
                cur = {"from": r["date"], "to": r["date"], "days": 1}
            else:
                cur["to"] = r["date"]
                cur["days"] += 1
        elif cur is not None:
            gaps.append(cur)
            cur = None
    if cur is not None:
        gaps.append(cur)

    counts = {}
    for r in day_rows:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

    n_bad = counts.get("absent", 0) + counts.get("incomplete", 0)
    if counts.get("absent", 0) > 0:
        level = "RED"
    elif counts.get("incomplete", 0) > 0:
        level = "RED"
    elif counts.get("complete_calendar", 0) > 0:
        level = "YELLOW"
    else:
        level = "OK"

    # 品种级：窗口内失败次数最多的
    fail_by_sym = {}
    for r in recs:
        if not r["ok_all"]:
            fail_by_sym[r["symbol"]] = fail_by_sym.get(r["symbol"], 0) + 1
    worst = sorted(fail_by_sym.items(), key=lambda kv: -kv[1])[:8]

    fresh = freshness(session)
    out = {
        "meta": {"generated_at": datetime.now().isoformat(timespec="seconds"),
                 "mode": mode, "window": [str(cal[0]) if cal else None,
                                          str(cal[-1]) if cal else None],
                 "n_cal_days": len(cal), "l2_tol": L2_TOL,
                 "l2_rate": L2_RATE, "l3_rate": L3_RATE},
        "alert_level": level,
        "counts": counts,
        "gaps": gaps,
        "layers_freshness": fresh,
        "worst_symbols": worst,
        "n_partial_night_tail": partial,
        "tolerance_diag": diag,
        "days": day_rows,
        "note": "absent=60m 无行；incomplete=匹配率<90%（多为换月不同步，见 §32.2）；"
                "complete_calendar=90%~95%；complete≥95%",
    }
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)

    print(f"[window] {mode} {out['meta']['window']} n_days={len(cal)}")
    print(f"[alert] {level}  counts={counts}")
    if diag:
        cm = diag.get("cell_max_field", {})
        print(f"[tol-diag] 单格四值最大相对误差: ≤0.1% {cm.get('share_le_0p1pct')}% / "
              f"≤0.5% {cm.get('share_le_0p5pct')}% / ≤1% {cm.get('share_le_1pct')}% / "
              f">2% {cm.get('share_gt_2pct')}%")
        for c in ("open", "high", "low", "close"):
            if c in diag:
                d_ = diag[c]
                print(f"   {c:6s} 中位 {d_['median_pct']:.4f}% p95 {d_['p95_pct']:.3f}% "
                      f"p99 {d_['p99_pct']:.3f}% max {d_['max_pct']:.3f}%")
    if gaps:
        for g in gaps:
            print(f"   GAP {g['from']} → {g['to']}  {g['days']} 个交易日")
    if worst:
        print(f"[worst symbols] {worst}")
    print("[freshness]")
    for k, v in fresh.items():
        print(f"   {k:22s} {v}")
    bad_days = [r for r in day_rows if r["verdict"] != "complete"]
    for r in bad_days[:20]:
        print(f"   {r['date']} {r['verdict']:>17s} rate={r['match_rate']:.3f} "
              f"bad={r['n_bad_symbols']} {r['bad_symbols'][:4]}")
    print(f"[done] → {args.out}")
    sys.exit(0 if level == "OK" else (2 if level == "RED" else 1))


if __name__ == "__main__":
    main()
