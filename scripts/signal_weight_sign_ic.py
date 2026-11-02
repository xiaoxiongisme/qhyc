#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""三期预研：平台 default_weight 权重符号「翻号」系统性核验（研究分支 cb/v2-oos-investigation，§17）。

背景
----
二期 P1（§15）发现：按平台 ``factor_registry.default_weight`` 符号定向做横截面组合，
2024-2026 净 R 几乎全负（权重方向 0/18 过前三门槛），反方向却多为正
（cross_rank_z/member_net_z anti lag1 过①②③）→ 疑似「平台权重符号系统性反转」。
但 §15.8 留了三个未决解释：(a) 不排除 weight 本就定义为反向暴露；(b) 反方向过门槛两格
均在 lag=1，疑似抢跑盘后发布的前视假象；(c) 36 格扫出来的方向属选择偏差。

本预研「先定性、不挑方向」，用最干净的 IC（不依赖仓位构造/成本/换手）来判定：

1. **语义核查（离线，§17.1）**：``app/factor/asof.py`` 244-245 行「>0 看多、<0 看空」、
   ``total += w·z``；``scripts/seed_factors.py`` 第 5 行；``tests/test_factor_asof.py``
   ``test_bias_suppress_position_on_bearish`` 固化「库存高 z 正 × 权重负 → 合成偏空」。
   且 P1 ``build_targets``（权重符号定 top-q 方向）与平台语义对齐。
   → 排除「脚本写反」「平台定义为反向暴露」两种解释。
2. **翻号判据复用平台自己的定义**（``scripts/factor_ic_monitor.py`` 19/205-210 行）：
   ``IC × sign(default_weight) < 0`` = 翻号告警。direction = sign(weight)。
3. **IC 计算**：每日横截面 spearman(因子 z, 前瞻 h 日收益)，h∈{1,5,20}；因子值显式
   ``shift(lag)`` 防前视，lag∈{1,2,3}（沿用 §14/§15：探针证实 available_at 恒 == trade_date
   即 lag0，平台未编码盘后发布延迟，不可信）。
4. **分时段**：训练期(<=2023)/OOS(2024-2026)/逐年/半年，区分
   - 「静态先验方向本就无效/设反」：IS 就翻号；
   - 「regime 动态翻转」：IS 不翻、OOS 才翻；
   - 「前视/量级假象」：lag∈{1,2,3} 不稳健、或 |IC| 近零。
5. **纪律 3**：看 IC 随 horizon(h) 的变化 —— IC 随 h 变大 = 趋势漂移而非截面回复。

口径与 §9/P1 闭环：行情用 ``l1_mkt.bar_60m`` 重采样日线 close（与 signal_oos_backtest 一致），
因子用 ``l2_adj.factor_value.z_value``。Spearman 用 rank-Pearson 自实现（零 scipy 依赖）。
enabled 因子清单与权重**动态从库读取**，不硬编码。

用法
----
    python signal_weight_sign_ic.py --out /tmp/aeq/p3_weight_sign.json
    python signal_weight_sign_ic.py --oos-start 2024-01-01 --min-n 8 --lags 1,2,3 --horizons 1,5,20
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.core.config import get_settings
from signal_oos_backtest import load_market


def _spearman(a: np.ndarray, b: np.ndarray) -> float:
    """rank-Pearson 实现 Spearman（避免依赖容器 scipy 版本差异）。"""
    ra = pd.Series(a).rank().to_numpy()
    rb = pd.Series(b).rank().to_numpy()
    if np.std(ra) == 0 or np.std(rb) == 0:
        return np.nan
    c = np.corrcoef(ra, rb)[0, 1]
    return float(c) if np.isfinite(c) else np.nan


def _ic_stat(v: np.ndarray) -> dict:
    """IC 序列统计：mean / std / ICIR / t / 正比例 / 样本数。"""
    v = np.asarray(v, dtype=float)
    v = v[np.isfinite(v)]
    n = len(v)
    if n < 3:
        return {"n": n, "ic": np.nan, "icir": np.nan, "t": np.nan, "pos_ratio": np.nan}
    mu, sd = float(v.mean()), float(v.std(ddof=1))
    return {
        "n": n,
        "ic": round(mu, 5),
        "icir": round(mu / sd, 4) if sd > 0 else np.nan,
        "t": round(mu / (sd / np.sqrt(n)), 3) if sd > 0 else np.nan,
        "pos_ratio": round(float((v > 0).mean()), 4),
    }


def load_registry(session):
    """enabled B 类因子 + default_weight（动态，不硬编码）。

    schema 兼容：§12 探针见 ``l2_adj.factor_registry``，而 factor_ic_monitor.py 用无前缀
    ``factor_registry``（依赖 search_path）。这里多写法容错，并留痕实际命中的写法。
    """
    bases = ["l2_adj.factor_registry", "factor_registry"]
    sqls = []
    for b in bases:
        sqls.append(f"SELECT factor_id, default_weight, category, enabled "
                    f"FROM {b} WHERE enabled AND category='B' ORDER BY factor_id")
    for b in bases:  # category 列缺失/取值不同时的兜底
        sqls.append(f"SELECT factor_id, default_weight, category, enabled "
                    f"FROM {b} WHERE enabled ORDER BY factor_id")
    for sql in sqls:
        try:
            rows = session.execute(text(sql)).all()
            if rows:
                print(f"  [reg] 命中写法: ...FROM {sql.split('FROM ')[1].split(' WHERE')[0]}",
                      flush=True)
                return [{"factor_id": r[0], "weight": float(r[1] or 0.0),
                         "category": r[2], "enabled": bool(r[3])} for r in rows]
        except Exception:
            continue
    return []


def load_factor_matrix(session, fid, start, end):
    """取单因子 z_value，pivot 成 date×symbol。"""
    rows = session.execute(text(
        "SELECT trade_date, symbol, z_value, raw_value FROM l2_adj.factor_value "
        "WHERE factor_id = :f AND trade_date >= :s AND trade_date <= :e"
    ), {"f": fid, "s": start - timedelta(days=450), "e": end}).all()
    df = pd.DataFrame(rows, columns=["date", "symbol", "z", "raw"])
    if df.empty:
        return pd.DataFrame(), {}
    df["z"] = pd.to_numeric(df["z"], errors="coerce")
    df["raw"] = pd.to_numeric(df["raw"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    mat = df.pivot(index="date", columns="symbol", values="z")
    # z 性质诊断：截面离散度 vs 时序离散度
    cs_std = float(mat.std(axis=1, ddof=0).mean()) if not mat.empty else np.nan
    ts_mean_of_std = float(mat.std(axis=0, ddof=0).mean()) if not mat.empty else np.nan
    overall_mean = float(np.nanmean(mat.values)) if not mat.empty else np.nan
    diag = {
        "rows": int(len(df)),
        "n_symbol": int(mat.shape[1]),
        "date_min": str(mat.index.min().date()) if not mat.empty else None,
        "date_max": str(mat.index.max().date()) if not mat.empty else None,
        "z_notnull_ratio": round(float(df["z"].notna().mean()), 4),
        "raw_notnull_ratio": round(float(df["raw"].notna().mean()), 4),
        "z_cross_section_std_mean": round(cs_std, 4),      # 每日跨品种 std 均值（横截面标准化→较大）
        "z_time_series_std_mean": round(ts_mean_of_std, 4),  # 每品种时序 std 均值（时序标准化→≈1）
        "z_overall_mean": round(overall_mean, 4),
    }
    return mat, diag


def fwd_return_matrix(closes: pd.DataFrame, h: int) -> pd.DataFrame:
    """未来第 h 交易日收益 = close[d+h]/close[d] - 1（平台 factor_ic_monitor 口径）。"""
    out = {}
    for sym in closes.columns:
        c = closes[sym].astype(float)
        out[sym] = c.shift(-h) / c - 1.0
    return pd.DataFrame(out, index=closes.index)


def daily_ic(lagged: pd.DataFrame, fwd: pd.DataFrame, min_n: int) -> pd.Series:
    """逐交易日横截面 spearman(滞后因子 z, 前瞻收益)。"""
    # 列对齐（因子列可能与行情列不完全一致）
    cols = lagged.columns.intersection(fwd.columns)
    lagged = lagged[cols]
    fwd = fwd[cols]
    res = {}
    for d in lagged.index:
        a = lagged.loc[d].to_numpy(dtype=float)
        b = fwd.loc[d].to_numpy(dtype=float)
        m = np.isfinite(a) & np.isfinite(b)
        if m.sum() < min_n:
            continue
        ic = _spearman(a[m], b[m])
        if np.isfinite(ic):
            res[pd.Timestamp(d).date()] = ic
    return pd.Series(res).sort_index()


def split_stats(ic: pd.Series, oos_start: date) -> dict:
    """全样本 / IS / OOS / 逐年 / 半年 分解。"""
    out = {"full": _ic_stat(ic.to_numpy())}
    is_mask = ic.index < oos_start
    oos_mask = ~is_mask
    out["is"] = _ic_stat(ic[is_mask].to_numpy())
    out["oos"] = _ic_stat(ic[oos_mask].to_numpy())
    by_year = {}
    for y, g in ic.groupby(ic.index.map(lambda d: d.year)):
        by_year[str(y)] = _ic_stat(g.to_numpy())
    out["by_year"] = by_year
    # 半年段（IS/OOS 分界两侧各自再分，观察后期是否连续塌）
    half = {}
    for (y, h), g in ic.groupby(ic.index.map(lambda d: (d.year, 1 if d.month <= 6 else 2))):
        half[f"{y}H{h}"] = _ic_stat(g.to_numpy())
    out["by_half"] = half
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--oos-start", default="2024-01-01")
    ap.add_argument("--min-n", type=int, default=8)
    ap.add_argument("--lags", default="1,2,3")
    ap.add_argument("--horizons", default="1,5,20")
    ap.add_argument("--out", default="/tmp/aeq/p3_weight_sign.json")
    args = ap.parse_args()

    lags = [int(x) for x in args.lags.split(",")]
    horizons = [int(x) for x in args.horizons.split(",")]
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    oos_start = date.fromisoformat(args.oos_start)
    data_start = start - timedelta(days=400)

    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    symbols = [s.symbol for s in settings.main_contracts]

    reg = load_registry(session)
    print(f"[init] enabled B 类因子={len(reg)}  lags={lags} horizons={horizons} "
          f"oos_start={oos_start} min_n={args.min_n}", flush=True)
    for r in reg:
        print(f"  [reg] {r['factor_id']:24s} weight={r['weight']:+.2f}", flush=True)

    opens, closes, highs, lows = load_market(session, symbols, data_start, end)
    print(f"[load] n_dates={len(closes.index)} n_symbols={len(closes.columns)} "
          f"range=[{closes.index.min()},{closes.index.max()}]", flush=True)

    # 因子矩阵 + 诊断
    mats, diag = {}, {}
    for r in reg:
        fid = r["factor_id"]
        m, dg = load_factor_matrix(session, fid, start, end)
        mats[fid] = m
        diag[fid] = dg
        print(f"  [factor] {fid:24s} z={m.shape} z_cs_std={dg.get('z_cross_section_std_mean')} "
              f"z_ts_std={dg.get('z_time_series_std_mean')} rng=[{dg.get('date_min')},{dg.get('date_max')}]",
              flush=True)
    session.close(); engine.dispose()

    ci = pd.to_datetime(closes.index)
    wmap = {r["factor_id"]: r["weight"] for r in reg}

    records = {}
    for fid in mats:
        w = wmap[fid]
        direction = 1.0 if w >= 0 else -1.0
        mat = mats[fid]
        if mat.empty or mat.notna().sum().sum() == 0:
            continue
        aligned = mat.reindex(ci).ffill(limit=5)
        aligned.index = closes.index
        for h in horizons:
            fwd = fwd_return_matrix(closes, h)
            for lag in lags:
                lagged = aligned.shift(lag)
                ic = daily_ic(lagged, fwd, args.min_n)
                if ic.empty:
                    continue
                st = split_stats(ic, oos_start)
                ic_is = st["is"]["ic"]
                ic_oos = st["oos"]["ic"]
                ic_full = st["full"]["ic"]
                flip_is = (ic_is * direction < 0) if np.isfinite(ic_is) else None
                flip_oos = (ic_oos * direction < 0) if np.isfinite(ic_oos) else None
                flip_full = (ic_full * direction < 0) if np.isfinite(ic_full) else None
                key = f"{fid}|lag{lag}|h{h}"
                records[key] = {
                    "factor_id": fid, "weight": w, "direction": direction,
                    "lag": lag, "horizon": h,
                    **st,
                    "flip_is": flip_is, "flip_oos": flip_oos, "flip_full": flip_full,
                }

    # ---- per-factor lag 稳健性 + 翻号分类（以 h=1 为主判据）----
    per_factor = {}
    for fid in mats:
        w = wmap[fid]
        direction = 1.0 if w >= 0 else -1.0
        h1 = [records[k] for k in records if k.startswith(f"{fid}|") and records[k]["horizon"] == 1]
        h1 = sorted(h1, key=lambda r: r["lag"])
        ic_oos_by_lag = [r["oos"]["ic"] for r in h1]
        ic_full_by_lag = [r["full"]["ic"] for r in h1]
        # lag 稳健：三个 lag 的 OOS IC 同号且都不近零
        finite = [x for x in ic_oos_by_lag if np.isfinite(x)]
        lag_stable = (len(finite) == len(ic_oos_by_lag) >= 2
                      and (all(x > 0 for x in finite) or all(x < 0 for x in finite))
                      and all(abs(x) >= 0.005 for x in finite))
        flip_is = h1[0]["flip_is"] if h1 else None
        flip_oos = h1[0]["flip_oos"] if h1 else None
        if flip_is and flip_oos:
            cls = "静态：IS 就翻号（先验方向本就无效/设反）"
        elif (flip_is is False) and flip_oos:
            cls = "动态：IS 不翻、OOS 才翻（regime 翻转）"
        elif flip_oos is False:
            cls = "未翻号（OOS 方向与权重一致）"
        else:
            cls = "数据不足/不定"
        per_factor[fid] = {
            "weight": w, "direction": direction,
            "h1_ic_full_by_lag": ic_full_by_lag,
            "h1_ic_oos_by_lag": ic_oos_by_lag,
            "h1_ic_is_by_lag": [r["is"]["ic"] for r in h1],
            "lag_stable_oos": bool(lag_stable),
            "flip_is_lag1": flip_is, "flip_oos_lag1": flip_oos,
            "classification": cls,
            # horizon 趋势（纪律3）：h1 vs h20 的 OOS IC
            "oos_ic_by_horizon_lag1": {
                str(h): records.get(f"{fid}|lag1|h{h}", {}).get("oos", {}).get("ic")
                for h in horizons
            },
        }

    n_flip_full = sum(1 for v in per_factor.values() if v["flip_oos_lag1"] is True)
    n_static = sum(1 for v in per_factor.values() if v["classification"].startswith("静态"))
    n_dynamic = sum(1 for v in per_factor.values() if v["classification"].startswith("动态"))
    n_stable = sum(1 for v in per_factor.values() if v["lag_stable_oos"])
    summary = {
        "n_enabled_factors": len(per_factor),
        "n_flip_oos_lag1": n_flip_full,
        "n_static_flip": n_static,
        "n_dynamic_flip": n_dynamic,
        "n_lag_stable": n_stable,
        "judge_note": "翻号=IC×sign(weight)<0（平台 factor_ic_monitor 定义）；"
                      "静态(IS就翻)/动态(OOS才翻)/lag稳健性三者共同判定；"
                      "IC 近零(|IC|<0.005)不计稳健",
    }

    # ---- 紧凑输出 ----
    print("\n======== 翻号核验（h=1，主判据）========", flush=True)
    print("%-24s %7s %10s %10s %10s %8s %9s  %s" %
          ("factor", "weight", "IC_full", "IC_IS", "IC_OOS", "lag稳", "翻号OOS", "分类"), flush=True)
    for fid, v in per_factor.items():
        f = v["h1_ic_full_by_lag"][0] if v["h1_ic_full_by_lag"] else np.nan
        i = v["h1_ic_is_by_lag"][0] if v["h1_ic_is_by_lag"] else np.nan
        o = v["h1_ic_oos_by_lag"][0] if v["h1_ic_oos_by_lag"] else np.nan
        print("%-24s %+7.2f %10s %10s %10s %8s %9s  %s" %
              (fid, v["weight"], f"{f:+.4f}" if np.isfinite(f) else "—",
               f"{i:+.4f}" if np.isfinite(i) else "—",
               f"{o:+.4f}" if np.isfinite(o) else "—",
               "是" if v["lag_stable_oos"] else "否",
               str(v["flip_oos_lag1"]), v["classification"]), flush=True)
    print("\n======== lag 稳健性明细（h=1 OOS IC）========", flush=True)
    for fid, v in per_factor.items():
        print(f"  {fid:24s} OOS IC by lag1/2/3 = "
              f"{[round(x,4) if np.isfinite(x) else None for x in v['h1_ic_oos_by_lag']]}  "
              f"by horizon(1/5/20) = {v['oos_ic_by_horizon_lag1']}", flush=True)
    print("\n======== SUMMARY ========", flush=True)
    print(json.dumps(summary, ensure_ascii=False, indent=1), flush=True)

    report = {
        "meta": {"start": str(start), "end": str(end), "oos_start": str(oos_start),
                 "lags": lags, "horizons": horizons, "min_n": args.min_n,
                 "price_source": "l1_mkt.bar_60m 重采样日线 close（与 §9/P1 同口径）",
                 "factor_source": "l2_adj.factor_value.z_value",
                 "lookahead_guard": "因子值显式 shift(lag) 防前视；探针已证 available_at==trade_date(lag0) 不可信",
                 "spearman_impl": "rank-Pearson 自实现（零 scipy 依赖）",
                 "flip_definition": "IC × sign(default_weight) < 0 = 翻号（复用 factor_ic_monitor.py 判据）",
                 "direction_note": "IC 用因子 z 原始值（不乘权重）；符号与权重符号对照判定翻号"},
        "registry": reg, "factor_diagnostics": diag,
        "records": records, "per_factor": per_factor, "summary": summary,
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                              encoding="utf-8")
    print(f"\nsaved {args.out}", flush=True)


if __name__ == "__main__":
    main()
