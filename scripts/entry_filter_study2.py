#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""入场过滤研究 II（2026-11-01）：V1 市场 regime 门 / V2 中段停 / V3 末段停 / V4 柱扩张软偏好。

在 entry_filter_study.py（生产入场流 + KDJ/MACD 标签）基础上新增两个结构层状态：
  ① 市场 regime：50 主连等权日收益指数 vs SMA250（前一交易日值，无前视）；
     顺 regime = 做多且指数在 SMA250 上方，或做空且在下方。
  ② K45 趋势进度：日线 K=45 摆动点（前后各 45 根极值、同类合并、高低交替），
     当前活跃段 = 最后一个已确认枢轴（确认时点 = 枢轴 + 45 根）之后；
     进度 = 收盘位移 / 段内至今运行极值幅度（历史口径 prog，单调不减）。
     全部取前一交易日收盘状态（无前视）。

模拟组合（纯过滤口径：被砍信号的 R 直接移出，减半 = R×0.5；
⚠ 真实引擎中砍信号会改变后续路径，本统计只是第一关初筛）：
  C_regime_stop / C_regime_half：逆 regime 信号停发 / 减半
  C_midstop：K45 进度 33~50% 停
  C_endstop：进度 >83% 停
  C_midend：中段+末段停
  C_combo：逆 regime 停 + 中段停 + 末段停
  C_expand_pref：MACD 柱非顺向扩张减半

用法（qhyc-api 容器内）：
  python scripts/entry_filter_study2.py [--out /tmp/entry_filter2.json]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.backtest.fusion_backtest import (
    fusion_params_from_config, _read_hourly, _htf_direction,
)
from app.strategies.fusion_signal import walk_fusion_states

IS_END = pd.Timestamp("2021-01-01", tz="Asia/Shanghai")
K45 = 45
SMA_N = 250


# ---------------------------------------------------------------- 指标
def macd_hist(c, fast=12, slow=26, sig=9):
    c = pd.Series(c)
    dif = c.ewm(span=fast, adjust=False).mean() - c.ewm(span=slow, adjust=False).mean()
    dea = dif.ewm(span=sig, adjust=False).mean()
    return (2.0 * (dif - dea)).to_numpy()


# ---------------------------------------------------------------- K45 摆动点段
def k45_daily_state(dates, h, l, c, K=K45):
    """返回 DataFrame(index=dates)：seg_dir(+1/-1/0), prog(NaN=无段)。

    活跃段规则（无前视）：枢轴 i 在 i+K 才确认；t 日状态 = 最后一个
    confirm_idx <= t 的枢轴之后的段，进度用截至 t 收盘与运行极值。
    输出再整体 shift(1) 由调用方处理（标签用前一日）。
    """
    n = len(c)
    # 摆动点候选（首个达到窗口极值者）
    cand: list[tuple[int, str, float]] = []
    for i in range(n):
        lo, hi = max(0, i - K), min(n, i + K + 1)
        wh = h[lo:hi].max()
        if h[i] == wh and lo + int(np.argmax(h[lo:hi])) == i:
            cand.append((i, "H", float(h[i])))
        wl = l[lo:hi].min()
        if l[i] == wl and lo + int(np.argmin(l[lo:hi])) == i:
            cand.append((i, "L", float(l[i])))
    cand.sort(key=lambda x: x[0])
    # 同类合并保留更极端、强制高低交替
    piv: list[tuple[int, str, float]] = []
    for i, tp, px in cand:
        if piv and piv[-1][1] == tp:
            if (tp == "H" and px > piv[-1][2]) or (tp == "L" and px < piv[-1][2]):
                piv[-1] = (i, tp, px)
        elif piv:
            # 相邻不同类：若同类间距中还有更极端的已被上面合并逻辑处理
            piv.append((i, tp, px))
        else:
            piv.append((i, tp, px))
    # 再做一遍交替清理（A-H-B-H 中 B 与 A 同侧时）
    clean: list[tuple[int, str, float]] = []
    for p in piv:
        if clean and clean[-1][1] == p[1]:
            if (p[1] == "H" and p[2] > clean[-1][2]) or (p[1] == "L" and p[2] < clean[-1][2]):
                clean[-1] = p
        else:
            clean.append(p)
    piv = clean

    seg_dir = np.zeros(n, dtype=int)
    prog = np.full(n, np.nan)
    # 段起点：第一个枢轴之前无段
    cur = None  # (idx, 'L'/'H', price) 段起点枢轴
    j = 0
    for t in range(n):
        # 推进：所有 confirm<=t 的枢轴都可作为最新段起点
        while j < len(piv) and piv[j][0] + K <= t:
            cur = piv[j]
            j += 1
        if cur is None:
            continue
        idx, tp, px = cur
        if tp == "L":  # 上涨段 L→(未来H)
            seg_dir[t] = 1
            runmax = h[idx:t + 1].max()
            denom = runmax - px
            prog[t] = (c[t] - px) / denom if denom > 0 else np.nan
        else:          # 下跌段 H→(未来L)
            seg_dir[t] = -1
            runmin = l[idx:t + 1].min()
            denom = px - runmin
            prog[t] = (px - c[t]) / denom if denom > 0 else np.nan
    return pd.DataFrame({"seg_dir": seg_dir, "prog": prog}, index=dates)


# ---------------------------------------------------------------- 统计
def seg_stats(rows):
    if not rows:
        return {"n": 0}
    r = np.array([x["r"] for x in rows], dtype=float)
    return {"n": int(len(r)),
            "win_rate": round(float((r > 0).mean()) * 100, 2),
            "mean_R": round(float(r.mean()), 5),
            "sum_R": round(float(r.sum()), 1)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/entry_filter2.json")
    args = ap.parse_args()

    settings = get_settings()
    symbols = [s.symbol for s in settings.main_contracts] or []
    engine = create_engine(settings.db_url, pool_size=4, max_overflow=8)
    session = sessionmaker(bind=engine)()
    p = fusion_params_from_config()
    sl_atr = float(p.sl_atr)

    # ---------- ① 市场等权指数与 regime ----------
    rows = session.execute(text(
        "SELECT symbol, trade_date, close FROM l0_raw.daily_bar "
        "WHERE symbol = ANY(:syms) ORDER BY trade_date"), {"syms": symbols}).all()
    dd = pd.DataFrame(rows, columns=["symbol", "dt", "close"])
    dd["dt"] = pd.to_datetime(dd["dt"])
    wide = dd.pivot_table(index="dt", columns="symbol", values="close")
    rets = wide.pct_change()
    idx_eq = (1.0 + rets.mean(axis=1, skipna=True)).cumprod()
    sma = idx_eq.rolling(SMA_N, min_periods=SMA_N).mean()
    regime_up = (idx_eq > sma).astype(float)
    regime_up[~(idx_eq.notna() & sma.notna())] = np.nan
    reg_series = pd.Series(regime_up.values, index=idx_eq.index).shift(1)  # 前一日
    reg_dates = idx_eq.index.values
    print(f"[index] {len(idx_eq)} days, regime_up 有效 {int(reg_series.notna().sum())} 天")

    # 每品种 K45 日线状态（前一日）
    k45_cache: dict[str, pd.DataFrame] = {}
    drows = session.execute(text(
        "SELECT symbol, trade_date, high, low, close FROM l0_raw.daily_bar "
        "WHERE symbol = ANY(:syms) ORDER BY symbol, trade_date"),
        {"syms": symbols}).all()
    ddf = pd.DataFrame(drows, columns=["symbol", "dt", "high", "low", "close"])
    ddf["dt"] = pd.to_datetime(ddf["dt"])
    for sym, g in ddf.groupby("symbol"):
        g = g.sort_values("dt").reset_index(drop=True)
        st = k45_daily_state(g["dt"].values,
                             g["high"].to_numpy(float),
                             g["low"].to_numpy(float),
                             g["close"].to_numpy(float))
        st = st.shift(1)  # 前一日状态
        st.index = pd.to_datetime(st.index)
        k45_cache[sym] = st
    print(f"[k45] {len(k45_cache)} symbols")

    def _prev_regime(d):
        pos = np.searchsorted(reg_dates, np.datetime64(pd.Timestamp(d).date()), side="left")
        # 取 < d 的最近日期
        while pos > 0 and pd.Timestamp(reg_dates[pos - 1]).date() >= pd.Timestamp(d).date():
            pos -= 1
        if pos <= 0:
            return np.nan
        return reg_series.iloc[pos - 1]

    # ---------- ② 入场流重放（与 study1 相同） ----------
    all_rows: list[dict] = []
    for si, sym in enumerate(symbols):
        df = _read_hourly(session, sym, "bar_60m", None, None)
        if df is None or len(df) < 300:
            continue
        o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float); c = df["close"].to_numpy(float)
        dts = df["dt"].to_numpy()
        n = len(c)
        htf = _htf_direction(c, int(p.ema_k))
        hist = macd_hist(c)
        hist_abs = np.abs(hist)
        prev_abs = np.concatenate([[hist_abs[0]], hist_abs[:-1]])
        st45 = k45_cache.get(sym)

        # 预计算每个 bar 日期对应的 K45 状态（前一日）
        segdirs = np.zeros(n); progs = np.full(n, np.nan)
        if st45 is not None:
            sdates = st45.index
            for i in range(n):
                d = pd.Timestamp(dts[i]).date()
                pos = sdates.searchsorted(pd.Timestamp(d), side="left")
                if pos > 0:
                    prev = sdates[pos - 1]
                    if prev.date() < d:
                        segdirs[i] = st45.iloc[pos - 1]["seg_dir"]
                        progs[i] = st45.iloc[pos - 1]["prog"]

        pos_open = None
        rows: list[dict] = []
        prev_state = 0
        for kgen, d in enumerate(walk_fusion_states(o, h, l, c, htf, p)):
            i = kgen + 2
            st = d["state"]
            if prev_state != 0 and (st == 0 or st != prev_state) and pos_open:
                e = pos_open; pos_open = None
                xp = float(c[i])
                r = e["dirn"] * (xp - e["entry_px"]) / (sl_atr * e["entry_atr"])
                ed = pd.Timestamp(dts[e["i"]])
                kdir = int(segdirs[e["i"]])
                prog = float(progs[e["i"]]) if np.isfinite(progs[e["i"]]) else None
                reg = _prev_regime(ed)
                rows.append({
                    "symbol": sym, "side": e["side"],
                    "entry_dt": str(ed), "yr": int(ed.year), "r": float(r),
                    "expand": bool(hist[e["i"]] * e["dirn"] > 0
                                   and hist_abs[e["i"]] >= prev_abs[e["i"]]),
                    "kdir": kdir, "prog": prog,
                    "reg": (None if not np.isfinite(reg) else float(reg)),
                })
            if st != 0 and (prev_state == 0 or st != prev_state):
                e_atr = float(d.get("entry_atr") or 0.0)
                if e_atr > 0:
                    pos_open = {"i": i, "entry_px": float(d["entry_px"]),
                                "entry_atr": e_atr,
                                "dirn": 1.0 if st == 1 else -1.0,
                                "side": "LONG" if st == 1 else "SHORT"}
            prev_state = st
        all_rows.extend(rows)
        if (si + 1) % 10 == 0 or si == len(symbols) - 1:
            print(f"[{si+1}/{len(symbols)}] {sym}: trades={len(rows)}")

    session.close(); engine.dispose()

    # ---------- ③ 分组与模拟 ----------
    def favor_regime(x):
        if x["reg"] is None:
            return "无regime"
        up = x["reg"] > 0.5
        return "顺regime" if ((x["side"] == "LONG") == up) else "逆regime"

    def prog_band(x):
        if x["prog"] is None:
            return "无段"
        v = x["prog"]
        if v < 0.33: return "前0-33"
        if v < 0.50: return "中33-50"
        if v <= 0.83: return "后50-83"
        return "末>83"

    def k_favor(x):
        if x["kdir"] == 0:
            return "无段"
        return "顺K45" if ((x["side"] == "LONG") == (x["kdir"] > 0)) else "逆K45"

    is_rows = [x for x in all_rows if pd.Timestamp(x["entry_dt"]) < IS_END]
    oos_rows = [x for x in all_rows if pd.Timestamp(x["entry_dt"]) >= IS_END]

    def grp(rows, keyfn):
        out = {}
        for x in rows:
            out.setdefault(keyfn(x), []).append(x)
        return {" | ".join(map(str, kk)): seg_stats(vv)
                for kk, vv in sorted(out.items(), key=lambda t: str(t[0]))}

    report = {"total": seg_stats(all_rows),
              "IS_total": seg_stats(is_rows), "OOS_total": seg_stats(oos_rows)}
    for tag, rows in (("ALL", all_rows), ("IS", is_rows), ("OOS", oos_rows)):
        report[f"{tag}__side_x_regime"] = grp(rows, lambda x: (x["side"], favor_regime(x)))
        report[f"{tag}__side_x_prog"] = grp(rows, lambda x: (x["side"], prog_band(x)))
        report[f"{tag}__side_x_kfavor_x_prog"] = grp(
            rows, lambda x: (x["side"], k_favor(x), prog_band(x)))
        report[f"{tag}__side_x_expand"] = grp(
            rows, lambda x: (x["side"], "扩张" if x["expand"] else "其他"))

    # 模拟组合（对 ALL/IS/OOS 三个口径都算）
    def sim(rows, cut_fn, half_fn=None):
        """cut_fn(x)->bool 砍；half_fn(x)->bool 减半。"""
        kept, cut, halved = [], 0, 0
        for x in rows:
            if cut_fn(x):
                cut += 1
            elif half_fn and half_fn(x):
                kept.append(x["r"] * 0.5); halved += 1
            else:
                kept.append(x["r"])
        if not kept:
            return {"n_kept": 0}
        a = np.array(kept)
        return {"n_kept": int(len(a)), "n_cut": cut, "n_half": halved,
                "mean_R": round(float(a.mean()), 5),
                "sum_R": round(float(a.sum()), 1)}

    sims = {
        "base": lambda x: sim(x, lambda y: False),
        "C_regime_stop": lambda x: sim(x, lambda y: favor_regime(y) == "逆regime"),
        "C_regime_half": lambda x: sim(x, lambda y: False,
                                        lambda y: favor_regime(y) == "逆regime"),
        "C_midstop": lambda x: sim(x, lambda y: prog_band(y) == "中33-50"),
        "C_endstop": lambda x: sim(x, lambda y: prog_band(y) == "末>83"),
        "C_midend": lambda x: sim(x, lambda y: prog_band(y) in ("中33-50", "末>83")),
        "C_combo": lambda x: sim(
            x, lambda y: favor_regime(y) == "逆regime"
            or prog_band(y) in ("中33-50", "末>83")),
        "C_expand_half": lambda x: sim(x, lambda y: False, lambda y: not y["expand"]),
        # ---- 非对称规则（只作用于空头）----
        "C_s_revreg_stop": lambda x: sim(
            x, lambda y: y["side"] == "SHORT" and favor_regime(y) == "逆regime"),
        "C_s_revk45_stop": lambda x: sim(
            x, lambda y: y["side"] == "SHORT" and k_favor(y) == "逆K45"),
        "C_s_rev_union": lambda x: sim(
            x, lambda y: y["side"] == "SHORT"
            and (favor_regime(y) == "逆regime" or k_favor(y) == "逆K45")),
        "C_s_rev_inter": lambda x: sim(
            x, lambda y: y["side"] == "SHORT"
            and favor_regime(y) == "逆regime" and k_favor(y) == "逆K45"),
        # ---- 最终候选组合：中段停 + 只砍逆势空头（并集） ----
        "C_final": lambda x: sim(
            x, lambda y: prog_band(y) == "中33-50"
            or (y["side"] == "SHORT"
                and (favor_regime(y) == "逆regime" or k_favor(y) == "逆K45"))),
        "C_final_half": lambda x: sim(
            x, lambda y: prog_band(y) == "中33-50",
            lambda y: y["side"] == "SHORT"
            and (favor_regime(y) == "逆regime" or k_favor(y) == "逆K45")),
    }
    for tag, rows in (("ALL", all_rows), ("IS", is_rows), ("OOS", oos_rows)):
        report[f"{tag}__sims"] = {k: fn(rows) for k, fn in sims.items()}

    # OOS 分年度主要模拟
    yr_out = {}
    for yr in sorted({x["yr"] for x in oos_rows}):
        yr_rows = [x for x in oos_rows if x["yr"] == yr]
        yr_out[yr] = {"base": sims["base"](yr_rows),
                      "C_midstop": sims["C_midstop"](yr_rows),
                      "C_s_revreg_stop": sims["C_s_revreg_stop"](yr_rows),
                      "C_s_revk45_stop": sims["C_s_revk45_stop"](yr_rows),
                      "C_s_rev_union": sims["C_s_rev_union"](yr_rows),
                      "C_final": sims["C_final"](yr_rows)}
    report["OOS_by_year"] = yr_out

    # ---------- ④ 安慰剂对照（随机砍同样多信号，排除"少做单"假象） ----------
    import random

    def placebo(rows, cut_fn, n_iter=300, seed=7):
        rs = [x["r"] for x in rows]
        k = sum(1 for x in rows if cut_fn(x))
        kept_sum = sum(r for x, r in zip(rows, rs) if not cut_fn(x))
        rng = random.Random(seed)
        sums = []
        for _ in range(n_iter):
            idxs = set(rng.sample(range(len(rows)), min(k, len(rows))))
            sums.append(sum(r for i, r in enumerate(rs) if i not in idxs))
        sums.sort()
        q = lambda p: round(float(sums[int(p * (len(sums) - 1))]), 1)
        better = sum(1 for s in sums if s >= kept_sum)
        return {"cut_k": k, "actual_kept_sumR": round(kept_sum, 1),
                "rand_p05": q(0.05), "rand_p50": q(0.50), "rand_p95": q(0.95),
                "rand_ge_actual_pct": round(100.0 * better / len(sums), 1)}

    def _cut_final(y):
        return prog_band(y) == "中33-50" or (
            y["side"] == "SHORT"
            and (favor_regime(y) == "逆regime" or k_favor(y) == "逆K45"))

    def _cut_final_v2(y):
        return prog_band(y) == "中33-50" or (
            y["side"] == "SHORT" and favor_regime(y) == "逆regime")

    report["placebo"] = {}
    for tag, rows in (("IS", is_rows), ("OOS", oos_rows)):
        report["placebo"][tag] = {
            "C_final": placebo(rows, _cut_final),
            "C_final_v2": placebo(rows, _cut_final_v2),
            "C_midstop": placebo(rows, lambda y: prog_band(y) == "中33-50"),
            "C_s_revreg_stop": placebo(
                rows, lambda y: y["side"] == "SHORT"
                and favor_regime(y) == "逆regime"),
        }
    # 简版最终组合（只用市场级标尺，不用品种级 K45 方向）
    sims["C_final_v2"] = lambda x: sim(x, _cut_final_v2)
    for tag, rows in (("ALL", all_rows), ("IS", is_rows), ("OOS", oos_rows)):
        report[f"{tag}__sims"]["C_final_v2"] = sims["C_final_v2"](rows)

    Path(args.out).write_text(
        json.dumps({"summary": report, "rows_count": len(all_rows)},
                   ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")
    print(f"\n总笔数 {len(all_rows)}（IS {len(is_rows)} / OOS {len(oos_rows)}）→ {args.out}")


if __name__ == "__main__":
    main()
