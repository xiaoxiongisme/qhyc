"""
离场侧元标签(meta-labeling) 研究 —— 融合策略 V3.4 原生离场(信号层 state→0)
=========================================================================
方向 #1（2026-10-13 元标签报告建议）：把元标签放到**离场侧**——
用模型预测"何时该提前平仓兑现 / 何时该让信号离场继续跑"，更贴近本系统真实 edge
（融合 edge 在离场不对称：盈 1.71R / 亏 0.735R，PF 由盈亏不对称驱动）。

设计：
- 主离场（primary）：融合信号层 state→0 平仓（=_simulate_trades 原生成交 R_std）。
- 元标签 y_t（在每根 in-trade bar t，t>入场）：
      r_now(t) = 方向*(close[t]-entry_px)/risk - cost/risk      # 若此刻平仓的净R
      y_t = 1  if r_now(t) > R_std  （现在兑现比等信号离场更划算）
      y_t = 0  otherwise
  即模型要学会识别"信号离场会回吐 / 会让亏损跑大"的时刻，提前兑现。
- 特征 X_t（严格 ≤ t，无前视）：当前R / 自峰值回撤 / 距峰值比 / 持仓时长 / 入场以来动量 /
      近5根斜率 / 已实现波动 / ATR比 / ADX / EMA距离 / 关键线距 / ATR regime / 缺口 /
      信号是否仍在(=state==side) / 方向哑元。
- 次级模型：LR(balanced) + RF(depth=6,min_leaf=50,balanced)，故意浅。
- 应用策略（正统元标签用法）：在每根 in-trade bar，若模型 P(y=1) ≥ 阈值(按测试集
      分数分位：top50%/top30%) → 提前平仓；否则交给信号离场。重算 PF / mean_R / 胜率。
- 防过拟合：按 entry_dt 扩窗 walk-forward（3 折 + 7天 embargo）；置换检验(shuffle
      标签重训 LR，AUC 应≈0.5)。

不调参：模型超参固定默认；阈值用分位法（非逐折选最优）。
"""
from __future__ import annotations
import sys, os, json, time, warnings
from datetime import date
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, "/Docker/qhyc")
from app.backtest.fusion_backtest import (
    fusion_params_from_config, _read_hourly, _generate_states, _simulate_trades,
)
from app.strategies.fusion_signal import adx14, atr14, ema
from app.core.db import session_scope
from app.core.config import get_settings

SEED = 20261013
W_VOL = 60
W_MOM = [20, 60]

FEAT_COLS = [
    "rv20", "atr_ratio", "adx", "ema_dist", "pdh_dist", "pdl_dist", "near_kl",
    "atr_z", "gap", "atr_pct",
    # trade-specific（in-trade 状态，≤t 无前视）
    "r_now", "giveback", "r_peak_ratio", "t_in", "mom_se", "slope",
    "state_intact", "dir",
]


def pf(rs):
    rs = np.asarray(rs, float)
    wins = rs[rs > 0]; losses = rs[rs < 0]
    gp = float(wins.sum()) if wins.size else 0.0
    gl = float(losses.sum()) if losses.size else 0.0
    return (gp / abs(gl)) if gl < 0 else (99.0 if gp > 0 else 0.0)


def make_exit_features(o, h, l, c, atr_arr, adx_arr, ema140, entry_i, t,
                       direction, entry_px, risk, r_peak, state_at_t, side_state):
    """严格只用 ≤ t 的数据构造 in-trade 特征。返回 dict；前视/无效返回 None。"""
    i = t
    if i <= entry_i or i < 70:
        return None
    cw = c[max(0, i - W_VOL):i + 1]
    rets = np.diff(np.log(cw))
    rv20 = float(np.std(rets[-20:]) * np.sqrt(252 * 6.5)) if len(rets) >= 20 else 0.0
    atr_i = float(atr_arr[i]); px = float(c[i])
    atr_ratio = atr_i / px if px else 0.0
    adx_i = float(adx_arr[i])
    ema_d = (px - float(ema140[i])) / atr_i if atr_i > 0 else 0.0
    win24_h = float(np.max(h[max(0, i - 24):i + 1]))
    win24_l = float(np.min(l[max(0, i - 24):i + 1]))
    pdh_dist = (win24_h - px) / atr_i if atr_i > 0 else 0.0
    pdl_dist = (px - win24_l) / atr_i if atr_i > 0 else 0.0
    near_kl = min(abs(pdh_dist), abs(pdl_dist))
    atr_z = (atr_i - float(np.mean(atr_arr[max(0, i - W_VOL):i + 1]))) / \
            (float(np.std(atr_arr[max(0, i - W_VOL):i + 1])) + 1e-9)
    gap = (o[i] / c[i - 1] - 1.0) if i > 0 else 0.0
    atr_pct = float((atr_arr[max(0, i - W_VOL):i + 1] <= atr_i).mean())
    # trade-specific（全部 ≤ t）
    r_now = direction * (px - entry_px) / risk if risk > 0 else 0.0
    giveback = r_peak - r_now
    r_peak_ratio = r_now / (r_peak + 1e-9)
    t_in = (i - entry_i) / 60.0
    mom_se = c[i] / c[entry_i] - 1.0
    slope = (c[i] - c[i - 5]) / atr_i if i >= 5 and atr_i > 0 else 0.0
    state_intact = 1.0 if int(state_at_t) == int(side_state) else 0.0
    return {
        "rv20": rv20, "atr_ratio": atr_ratio, "adx": adx_i, "ema_dist": ema_d,
        "pdh_dist": pdh_dist, "pdl_dist": pdl_dist, "near_kl": near_kl,
        "atr_z": atr_z, "gap": gap, "atr_pct": atr_pct,
        "r_now": r_now, "giveback": giveback, "r_peak_ratio": r_peak_ratio,
        "t_in": t_in, "mom_se": mom_se, "slope": slope,
        "state_intact": state_intact,
        "dir": 1.0 if direction > 0 else -1.0,
    }


def build_symbol(session, sym, base_p):
    """返回 (recs, arrays)。recs=逐根 in-trade 记录；arrays=该品种重算所需数组。"""
    try:
        df = _read_hourly(session, sym, base_p.src, date(2022, 1, 1), date(2026, 9, 30))
    except Exception:
        return None, None
    if df is None or len(df) < base_p.min_bars:
        return None, None
    states_df = _generate_states(df, base_p)
    try:
        trades = _simulate_trades(states_df, base_p, symbol=sym)
    except Exception:
        return None, None
    trades = [t for t in trades if not t.get("open")]
    if not trades:
        return None, None
    dt_index = {d: i for i, d in enumerate(states_df["dt"])}
    o = states_df["open"].to_numpy(float)
    h = states_df["high"].to_numpy(float)
    l = states_df["low"].to_numpy(float)
    c = states_df["close"].to_numpy(float)
    atr_arr = states_df["atr"].to_numpy(float)
    state_arr = states_df["state"].to_numpy(int)
    adx_arr = adx14(h, l, c, 14)
    ema140 = ema(c, 140)

    recs = []
    for t in trades:
        ei = dt_index.get(t["entry_dt"])
        xi = dt_index.get(t["exit_dt"])
        if ei is None or xi is None or xi <= ei:
            continue
        direction = 1.0 if t["side"] == "LONG" else -1.0
        side_state = 1 if t["side"] == "LONG" else 2
        entry_px = float(t["entry_px"])
        atr_e = float(t.get("atr") or 0.0)
        risk = float(base_p.sl_atr * atr_e) if atr_e > 0 else 0.0
        if risk <= 0:
            continue
        cost_pts = float(t.get("cost_points") or 0.0)
        R_std = float(t.get("R") or 0.0)
        # 逐根 in-trade 记录
        r_peak = -1e9
        for tt in range(ei + 1, xi + 1):
            px_t = float(c[tt])
            r_now_gross = direction * (px_t - entry_px) / risk if risk > 0 else 0.0
            r_peak = max(r_peak, r_now_gross)
            feats = make_exit_features(o, h, l, c, atr_arr, adx_arr, ema140,
                                       ei, tt, direction, entry_px, risk, r_peak,
                                       state_arr[tt], side_state)
            if feats is None:
                continue
            r_now_net = r_now_gross - cost_pts / risk
            y = 1.0 if r_now_net > R_std else 0.0
            rec = dict(feats)
            rec["sym"] = sym
            rec["entry_dt"] = (t["entry_dt"].isoformat() if hasattr(t["entry_dt"], "isoformat")
                               else str(t["entry_dt"]))
            rec["ei"] = ei
            rec["tt"] = tt
            rec["xi"] = xi
            rec["side_state"] = side_state
            rec["direction"] = direction
            rec["entry_px"] = entry_px
            rec["risk"] = risk
            rec["cost_pts"] = cost_pts
            rec["R_std"] = R_std
            rec["r_now_net"] = r_now_net
            rec["y"] = y
            recs.append(rec)
    arrays = {"o": o, "h": h, "l": l, "c": c, "atr": atr_arr,
              "state": state_arr, "sym": sym}
    return recs, arrays


def simulate_adaptive(recs_split, arrays_by_sym, P, thr):
    """对给定折的 in-trade 记录，按"首根 P>=thr 则提前平仓，否则信号离场"重算 R。
    recs_split: 该折记录(list)；arrays_by_sym: sym->数组；P: 与 recs_split 同序的预测分。
    返回重算后的 R 数组（每笔成交一个 R）。"""
    # 按 (sym, ei) 归组，按 tt 排序
    from collections import defaultdict
    groups = defaultdict(list)
    for k, rec in enumerate(recs_split):
        groups[(rec["sym"], rec["ei"])].append((rec["tt"], k))
    Rs = []
    for (sym, ei), items in groups.items():
        items.sort(key=lambda x: x[0])
        arr = arrays_by_sym[sym]
        c = arr["c"]; state = arr["state"]
        last = items[-1][0]
        exited = False
        for tt, k in items:
            if P[k] >= thr:
                # 提前平仓：用该 bar 的 r_now_net
                Rs.append(recs_split[k]["r_now_net"])
                exited = True
                break
            if int(state[tt]) != int(recs_split[k]["side_state"]):
                # 信号离场（基线）
                Rs.append(recs_split[k]["r_now_net"])
                exited = True
                break
        if not exited:
            # 兜底：到 exit_i（最后一根）仍未触发，必在最后一根信号离场
            tt, k = items[-1]
            Rs.append(recs_split[k]["r_now_net"])
    return np.asarray(Rs, float)


def main():
    base_p = fusion_params_from_config({"add_max_lots": 1})
    syms = [s.symbol for s in get_settings().main_contracts]
    all_recs = []
    arrays_by_sym = {}
    skipped = []
    t0 = time.time()
    with session_scope() as s:
        for sym in syms:
            recs, arrays = build_symbol(s, sym, base_p)
            if recs is None:
                skipped.append(sym)
                continue
            all_recs.extend(recs)
            arrays_by_sym[sym] = arrays
    df = pd.DataFrame(all_recs)
    df["entry_dt"] = pd.to_datetime(df["entry_dt"]).dt.tz_convert(None)
    df = df.sort_values("entry_dt").reset_index(drop=True)
    # 基线：每个成交由 R_std 代表（同一 (sym,ei) 的多根记录共享 R_std）
    # ⚠ ei 是品种内 bar 序号，非全局唯一；去重必须按 (sym,ei)，否则不同品种共享
    #   同一 ei 会被错误合并（12,505→6,067），导致基线 PF 失真。
    base_R = df.drop_duplicates(["sym", "ei"])[["sym", "ei", "R_std"]]
    base_pf = pf(base_R["R_std"].values)
    base_win = float((base_R["R_std"] > 0).mean())
    base_meanR = float(base_R["R_std"].mean())
    print(f"# in-trade 记录 n={len(df)}  品种={df['sym'].nunique()}  "
          f"成交数(去重 ei)={len(base_R)}  标签正率(y=1 提前兑现更划算)={df['y'].mean():.3f}")
    print(f"# 基线(信号离场) PF={base_pf:.3f}  win%={base_win:.3f}  mean_R={base_meanR:.4f}  "
          f"elapsed={time.time()-t0:.1f}s  skipped={len(skipped)}")
    print(f"# skipped: {skipped}")

    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import roc_auc_score, average_precision_score

    cuts = [date(2023, 12, 31), date(2024, 12, 31), date(2025, 12, 31)]
    embargo_days = 7
    summary = []
    for k, cut in enumerate(cuts):
        train = df[df["entry_dt"] <= pd.Timestamp(cut)]
        test = df[(df["entry_dt"] > pd.Timestamp(cut)) &
                  (df["entry_dt"] > pd.Timestamp(cut) + pd.Timedelta(days=embargo_days))]
        if len(train) < 500 or len(test) < 200:
            print(f"# fold{k+1} 样本不足 train={len(train)} test={len(test)} 跳过")
            continue
        Xtr, Xte = train[FEAT_COLS].values, test[FEAT_COLS].values
        ytr, yte = train["y"].values, test["y"].values
        test_recs = test.to_dict("records")
        line = f"\n=== FOLD {k+1} (train≤{cut}, test n_rec={len(test)}, n_tr={test.drop_duplicates('ei').shape[0]}) ==="
        print(line)
        for mname, m in {
            "LR": LogisticRegression(max_iter=2000, class_weight="balanced"),
            "RF": RandomForestClassifier(n_estimators=200, max_depth=6, min_samples_leaf=50,
                                         class_weight="balanced", n_jobs=-1, random_state=SEED),
        }.items():
            m.fit(Xtr, ytr)
            P = m.predict_proba(Xte)[:, 1]
            auc = roc_auc_score(yte, P)
            ap = average_precision_score(yte, P)
            print(f"  [{mname}] bar-AUC={auc:.3f} AP={ap:.3f}")
            for q in (0.5, 0.3):
                thr = np.quantile(P, 1 - q)
                adR = simulate_adaptive(test_recs, arrays_by_sym, P, thr)
                fp, wp, mp = pf(adR), float((adR > 0).mean()), float(adR.mean())
                print(f"    提前离场 top{q}: n={len(adR)} win%={wp:.3f} "
                      f"mean_R={mp:.4f} PF={fp:.3f}  (基线PF={base_pf:.3f})")
            summary.append({"fold": k + 1, "model": mname, "auc": auc, "ap": ap})

    # 置换检验
    from sklearn.model_selection import train_test_split
    Xa = df[FEAT_COLS].values; ya = df["y"].values
    Xtr_p, Xte_p, ytr_p, yte_p = train_test_split(Xa, ya, test_size=0.3, random_state=SEED)
    rng_perm = np.random.default_rng(SEED)
    perm_aucs = []
    for _ in range(10):
        ytr_perm = rng_perm.permutation(ytr_p)
        lr2 = LogisticRegression(max_iter=2000, class_weight="balanced")
        lr2.fit(Xtr_p, ytr_perm)
        perm_aucs.append(roc_auc_score(yte_p, lr2.predict_proba(Xte_p)[:, 1]))
    print(f"\n=== 置换检验（shuffle 标签重训 LR ×10）===")
    print(f"  置换 AUC 均值={np.mean(perm_aucs):.3f} (应≈0.5；若显著>0.5 提示泄漏/结构偏差)")

    with open("/tmp/metalabel_exit_result.json", "w") as f:
        json.dump({"summary": summary, "n_rec": len(df), "n_trades": len(base_R),
                   "perm_auc_mean": float(np.mean(perm_aucs)),
                   "label_pos": float(df["y"].mean()),
                   "base_pf": base_pf, "base_win": base_win, "base_meanR": base_meanR}, f, default=str)
    print("\n[written] /tmp/metalabel_exit_result.json")


if __name__ == "__main__":
    main()
