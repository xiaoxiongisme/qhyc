#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""entry_filter_study3（2026-11-01 第六轮）：三个测量缺口一次补齐。

S1 入场后浮盈路径（"入场即盈利"的量化）：win@1/4/8、MAE、曾达0.5R比例与时间；
S2 支路归因：pullback(A) / breakout(B) / 反手首仓 的 IS/OOS 分组统计；
S3 门控参数敏感性：MID 带网格 × regime SMA 周期（纯过滤口径，复刻 study2 sim）。

口径：与 study2 同（生产入场流、无门控基线、R=2×ATR_entry 毛收益、IS<2021-01-01<=OOS）。
浮盈路径极值口径：多头用 low 计 MAE / high 计 0.5R 触达；空头镜像。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path("/tmp/eg_run")
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.backtest.fusion_backtest import fusion_params_from_config, _read_hourly, _htf_direction
from app.strategies.fusion_signal import walk_fusion_states
from app.strategies.entry_gates import k45_daily_state, market_regime_series, MID_LO, MID_HI

IS_END = pd.Timestamp("2021-01-01", tz="Asia/Shanghai")


def seg(v):
    if not v:
        return {"n": 0}
    r = np.array([x["r"] for x in v])
    out = {"n": len(v), "win_rate": round(float((r > 0).mean()) * 100, 2),
           "mean_R": round(float(r.mean()), 5), "sum_R": round(float(r.sum()), 1)}
    # 路径指标
    for k in ("win1", "win4", "win8", "be_hit"):
        vv = [x[k] for x in v if x.get(k) is not None]
        if vv:
            out[k + "_pct"] = round(float(np.mean(vv)) * 100, 1)
    for k in ("mae", "t_be", "mfe8"):
        vv = [x[k] for x in v if x.get(k) is not None]
        if vv:
            out[k + "_med"] = round(float(np.median(vv)), 3)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-symbols", type=int, default=0)
    ap.add_argument("--out", default="/tmp/efs3.json")
    args = ap.parse_args()

    settings = get_settings()
    symbols = [s.symbol for s in settings.main_contracts]
    if args.max_symbols:
        symbols = symbols[:args.max_symbols]
    eng = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=eng)()
    p = fusion_params_from_config()

    # ---- K45 / regime（前一日口径，与 entry_gates 模块一致）----
    all_syms = [s.symbol for s in settings.main_contracts]
    drows = session.execute(text(
        "SELECT symbol, trade_date, high, low, close FROM l0_raw.daily_bar "
        "WHERE symbol = ANY(:syms) ORDER BY symbol, trade_date"), {"syms": all_syms}).all()
    ddf = pd.DataFrame(drows, columns=["symbol", "dt", "high", "low", "close"])
    ddf["dt"] = pd.to_datetime(ddf["dt"])
    k45_cache = {}
    for sym, g in ddf.groupby("symbol"):
        g = g.sort_values("dt").reset_index(drop=True)
        st = k45_daily_state(g["dt"].values, g["high"].to_numpy(float),
                             g["low"].to_numpy(float), g["close"].to_numpy(float)).shift(1)
        st.index = pd.to_datetime(st.index)
        k45_cache[sym] = st
    # regime 三个 SMA 周期（敏感性）
    reg_by_n = {}
    for sma_n in (200, 250, 300):
        # market_regime_series 固定 250；此处自行按周期构造（同口径）
        rows = session.execute(text(
            "SELECT symbol, trade_date, close FROM l0_raw.daily_bar "
            "WHERE symbol = ANY(:syms) ORDER BY trade_date"), {"syms": all_syms}).all()
        dd = pd.DataFrame(rows, columns=["symbol", "dt", "close"])
        dd["dt"] = pd.to_datetime(dd["dt"])
        wide = dd.pivot_table(index="dt", columns="symbol", values="close")
        rets = wide.pct_change(fill_method=None)
        idx_eq = (1.0 + rets.mean(axis=1, skipna=True)).cumprod()
        sma = idx_eq.rolling(sma_n, min_periods=sma_n).mean()
        ru = (idx_eq > sma).astype(float)
        ru[~(idx_eq.notna() & sma.notna())] = np.nan
        rs = pd.Series(ru.values, index=idx_eq.index).shift(1)
        rs.index = pd.to_datetime(rs.index)
        reg_by_n[sma_n] = rs

    def prev_day_val(series, d):
        """series(index=交易日, 已shift1) 在日期 d 的前一日值；无则 None。"""
        if series is None or not len(series):
            return None
        pos = series.index.searchsorted(pd.Timestamp(d), side="left")
        if pos > 0 and series.index[pos - 1].date() < d:
            return series.iloc[pos - 1]
        return None

    rows_out = []
    for si, sym in enumerate(symbols):
        df = _read_hourly(session, sym, "bar_60m", None, None)
        if df is None or len(df) < 300:
            continue
        n = len(df)
        o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
        dts = df["dt"].to_numpy()
        htf = _htf_direction(c, int(p.ema_k))
        st45 = k45_cache.get(sym)
        # 逐 bar 前一日 prog 与 regime_up（250）
        progs = np.full(n, np.nan)
        regup = np.zeros(n, dtype=bool)
        for i in range(n):
            d = pd.Timestamp(dts[i]).date()
            if st45 is not None:
                pv = prev_day_val(st45["prog"], d)
                if pv is not None and np.isfinite(pv):
                    progs[i] = pv
            rv = prev_day_val(reg_by_n[250], d)
            if rv is not None and np.isfinite(rv):
                regup[i] = bool(rv > 0.5)

        states = np.zeros(n, dtype=int)
        open_pos = None
        prev_closed = None   # (exit_i, dirn)
        for kgen, d in enumerate(walk_fusion_states(o, h, l, c, htf, p)):
            i = kgen + 2
            st = d["state"]
            if open_pos is not None and (st == 0 or st != open_pos["dir_state"]):
                # 平仓（含反手跳变）：按旧方向在 c[i] 平
                e = open_pos
                xp = float(c[i])
                risk = 2.0 * e["entry_atr"]
                dirn = e["dirn"]
                r = dirn * (xp - e["entry_px"]) / risk
                # ---- 浮盈路径（entry_i → exit_i，含 exit bar）----
                ei, xi = e["entry_i"], i
                kk = np.arange(ei, xi + 1)
                pc = dirn * (c[kk] - e["entry_px"]) / risk          # close 口径
                ext = l[kk] if dirn > 0 else h[kk]                  # 不利极值
                fav = h[kk] if dirn > 0 else l[kk]                  # 有利极值
                adverse = dirn * (ext - e["entry_px"]) / risk
                favor = dirn * (fav - e["entry_px"]) / risk
                mae = float(adverse.min())
                be_idx = np.nonzero(favor >= 0.5)[0]
                be_hit = len(be_idx) > 0
                t_be = int(be_idx[0]) if be_hit else None
                w = lambda k: (bool(pc[k] > 0) if len(pc) > k else None)
                mfe8 = float(favor[:9].max())
                is_rev = bool(prev_closed is not None
                              and prev_closed[1] == -dirn
                              and ei - prev_closed[0] <= 8)
                rows_out.append({
                    "symbol": sym, "side": "LONG" if dirn > 0 else "SHORT",
                    "entry_dt": str(pd.Timestamp(dts[ei])),
                    "yr": pd.Timestamp(dts[xi]).year,
                    "r": float(r), "leg": e["leg"], "reverse": is_rev,
                    "prog": (None if not np.isfinite(progs[ei]) else float(progs[ei])),
                    "regup": bool(regup[ei]),
                    "win1": w(1), "win4": w(4), "win8": w(8),
                    "mae": mae, "be_hit": be_hit, "t_be": t_be, "mfe8": mfe8,
                    "hold_bars": int(xi - ei),
                })
                prev_closed = (i, dirn)
                open_pos = None
            if st != 0 and open_pos is None:
                e_atr = float(d.get("entry_atr") or 0.0)
                if e_atr > 0:
                    open_pos = {"entry_i": i, "entry_px": float(d["entry_px"]),
                                "entry_atr": e_atr, "dirn": 1.0 if st == 1 else -1.0,
                                "dir_state": st, "leg": d.get("entry_leg") or "unknown"}
        if (si + 1) % 10 == 0:
            print(f"[{si+1}/{len(symbols)}] rows={len(rows_out)}", flush=True)

    session.close()
    eng.dispose()

    def grp(rows, keyfn):
        out = {}
        for x in rows:
            out.setdefault(keyfn(x), []).append(x)
        return {k: seg(v) for k, v in sorted(out.items(), key=lambda t: str(t[0]))}

    isr = [x for x in rows_out if pd.Timestamp(x["entry_dt"]) < IS_END]
    oos = [x for x in rows_out if pd.Timestamp(x["entry_dt"]) >= IS_END]

    report = {"rows_count": len(rows_out), "summary": {}}
    R = report["summary"]
    R["IS_total"] = seg(isr)
    R["OOS_total"] = seg(oos)
    for tag, rr in (("IS", isr), ("OOS", oos)):
        R[f"{tag}__leg"] = grp(rr, lambda x: x["leg"])
        R[f"{tag}__side_x_leg"] = grp(rr, lambda x: f"{x['side']}|{x['leg']}")
        R[f"{tag}__reverse"] = grp(rr, lambda x: "reverse" if x["reverse"] else "fresh")

    # ---- S3 参数敏感性（纯过滤口径）----
    def sim(rows, mid_lo, mid_hi, reg_series, use_reg):
        """K45 中段(mid带)停双侧 + 指数>SMA(前一交易日)停空头。返回 sumR。"""
        tot = 0.0
        for x in rows:
            pr = x["prog"]
            if pr is not None and mid_lo <= pr < mid_hi:
                continue
            if use_reg and x["side"] == "SHORT" and x.get("regup_sma") and x["regup_sma"].get(id(reg_series)):
                continue
            tot += x["r"]
        return tot

    # 预计算每行在三个 SMA 周期下的 regup（regup_sma 字典，键=id(series)）
    # 重新逐行算代价高：改为在每行记录 entry_dt，用 searchsorted 查询
    for x in rows_out:
        d = pd.Timestamp(x["entry_dt"]).date()
        x["regup_sma"] = {}
        for sma_n, rs in reg_by_n.items():
            rv = prev_day_val(rs, d)
            x["regup_sma"][id(rs)] = (None if rv is None or not np.isfinite(rv) else bool(rv > 0.5))

    sens = {}
    for mid_lo, mid_hi in [(0.28, 0.45), (0.30, 0.50), (0.33, 0.50), (0.33, 0.55),
                           (0.38, 0.50), (0.35, 0.60)]:
        for use_reg, sma_n in ((False, 0), (True, 200), (True, 250), (True, 300)):
            key = f"MID[{mid_lo},{mid_hi})+REG{sma_n if use_reg else 'off'}"
            rs = reg_by_n.get(sma_n)
            sens[key] = {
                "IS": round(sim(isr, mid_lo, mid_hi, rs, use_reg), 1),
                "OOS": round(sim(oos, mid_lo, mid_hi, rs, use_reg), 1)}
    R["sensitivity"] = sens
    R["base_no_gate"] = {"IS": round(sum(x["r"] for x in isr), 1),
                         "OOS": round(sum(x["r"] for x in oos), 1)}

    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, default=str),
                              encoding="utf-8")
    # 逐笔明细（供板块归因 S5 / 后续二次分析；regup_sma 键不可 JSON 化，剔除）
    slim = [{k: v for k, v in x.items() if k != "regup_sma"} for x in rows_out]
    Path(args.out.replace(".json", "_rows.json")).write_text(
        json.dumps(slim, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"→ {args.out}  rows={len(rows_out)}")


if __name__ == "__main__":
    main()
