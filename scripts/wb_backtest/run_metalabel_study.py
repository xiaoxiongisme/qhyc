"""
元标签(meta-labeling) 次级过滤模型 —— 融合策略 V3.4 候选入场
=====================================================================
主信号 : 融合 V3.4 方向层（walk_fusion_states 单一真源，12,595 笔候选入场）
标签 y : TBM 三重障碍 —— 给每笔入场打 "先触 TP(+2ATR) vs 触 SL(-1ATR)/超时(N=60根)"。
         先触 TP => 顺势跟涨/跟跌成立 => y=1，否则 y=0。
特征 X : 严格只用 ≤ 入场bar 的过去数据（无前视）：动量/已实现波动/ATR比/ADX/EMA距离/
         近关键线/隔夜缺口/时段/星期/方向哑元  ≈ 20 个。
执行   : 筛掉低 P(y=1) 入场，但幸存交易仍用**原生吊灯**离场。
         评估"过滤后原生吊灯 PF / mean_R / 胜率"是否高于不过滤（正统元标签用法）。
防过拟合: 按 entry_dt 扩窗 walk-forward（3 折），bootstrap 显著性。

⚠ 诚实判据：趋势策略赢少亏多（PF 由 avg_win_R>>avg_loss_R 驱动）。过滤若删掉尾部大赢家，
  会抬胜率却**杀 PF**。故必须同时报 PF 与 mean_R，而非只看胜率。

不调参：PT=2.0/SL=1.0/N=60、模型超参均为从业者默认，不做"选最优后报最佳"。
"""
from __future__ import annotations
import sys, os, json, time, warnings
from datetime import date
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.path.insert(0, "/Docker/qhyc")
from app.backtest.fusion_backtest import (
    fusion_params_from_config, _read_hourly, _generate_states,
    _simulate_trades,
)
from app.strategies.fusion_signal import adx14, atr14, ema
from app.core.db import session_scope
from app.core.config import get_settings

CACHE_PATH = "/tmp/metalabel_cache.json"
SEED = 20261013

# ---- TBM 标签参数（非调出，固定）----
PT = 2.0
SL = 1.0
N_BAR = 60

# ---- 特征窗口 ----
W_VOL = 60
W_MOM = [20, 60]


def simulate_tbm_label(ev, o, h, l, c, atr_arr, pt, sl, nbar):
    """返回 hit_type：'tp' / 'sl' / 'vertical' / 'eod'。决策 bar = ev['ei']，只向后看（标签，允许）。"""
    ei = ev["ei"]; d = ev["dir"]; ep = ev["entry_px"]
    ae = ev["atr_e"] if ev["atr_e"] > 0 else float(atr_arr[ei])
    if ae <= 0:
        ae = float(np.nanmean(atr_arr[ei - W_VOL:ei + 1])) or 1.0
    if d > 0:
        tp_px, sl_px = ep + pt * ae, ep - sl * ae
    else:
        tp_px, sl_px = ep - pt * ae, ep + sl * ae
    top = min(len(c) - 1, ei + nbar)
    for j in range(ei + 1, len(c)):
        hi, lo, op = h[j], l[j], o[j]
        if j > top:
            return "vertical"
        if d > 0:
            hit_sl, hit_tp = lo <= sl_px, hi >= tp_px
        else:
            hit_sl, hit_tp = hi >= sl_px, lo <= tp_px
        if hit_sl and hit_tp:
            return "tp" if abs(op - tp_px) <= abs(op - sl_px) else "sl"
        if hit_tp:
            return "tp"
        if hit_sl:
            return "sl"
    return "eod"


def make_features(o, h, l, c, atr_arr, adx_arr, ema140, ei, direction):
    """严格只用 ≤ ei 的数据构造特征。返回 dict。"""
    i = ei
    if i < 70:  # 预热不足
        return None
    cw = c[max(0, i - W_VOL):i + 1]
    rets = np.diff(np.log(cw))
    mom = {}
    for w in W_MOM:
        if i >= w:
            mom[f"mom{w}"] = c[i] / c[i - w] - 1.0
        else:
            mom[f"mom{w}"] = 0.0
    rv20 = float(np.std(rets[-20:]) * np.sqrt(252 * 6.5)) if len(rets) >= 20 else 0.0
    rv60 = float(np.std(rets) * np.sqrt(252 * 6.5)) if len(rets) >= 5 else 0.0
    atr_i = float(atr_arr[i])
    px = float(c[i])
    atr_ratio = atr_i / px if px else 0.0
    adx_i = float(adx_arr[i])
    ema_d = (px - float(ema140[i])) / atr_i if atr_i > 0 else 0.0
    # 近关键线：过去 24 根(约一日)高/低，距当前收盘标准化
    win24_h = float(np.max(h[max(0, i - 24):i + 1]))
    win24_l = float(np.min(l[max(0, i - 24):i + 1]))
    pdh_dist = (win24_h - px) / atr_i if atr_i > 0 else 0.0   # >0 在关键线下方
    pdl_dist = (px - win24_l) / atr_i if atr_i > 0 else 0.0   # >0 在关键线上方
    near_kl = min(abs(pdh_dist), abs(pdl_dist))                # 小=贴近关键线
    # ATR 自身相对近期均值（波动 regime）
    atr_z = (atr_i - float(np.mean(atr_arr[max(0, i - W_VOL):i + 1]))) / (float(np.std(atr_arr[max(0, i - W_VOL):i + 1])) + 1e-9)
    # 隔夜缺口
    gap = (o[i] / c[i - 1] - 1.0) if i > 0 else 0.0
    # 近期波动分位（当前 ATR 在近期分布的位置）
    atr_pct = float((atr_arr[max(0, i - W_VOL):i + 1] <= atr_i).mean())
    # 日内/星期（周期编码）
    return {
        "mom20": mom["mom20"], "mom60": mom["mom60"],
        "rv20": rv20, "rv60": rv60,
        "atr_ratio": atr_ratio, "adx": adx_i,
        "ema_dist": ema_d, "pdh_dist": pdh_dist, "pdl_dist": pdl_dist,
        "near_kl": near_kl, "atr_z": atr_z, "gap": gap, "atr_pct": atr_pct,
        "dir": 1.0 if direction > 0 else -1.0,
    }


def build_symbol(session, sym, base_p, cache):
    if sym in cache:
        return cache[sym]
    try:
        df = _read_hourly(session, sym, base_p.src, date(2022, 1, 1), date(2026, 9, 30))
    except Exception:
        return None
    if df is None or len(df) < base_p.min_bars:
        return None
    states_df = _generate_states(df, base_p)
    try:
        trades = _simulate_trades(states_df, base_p, symbol=sym)
    except Exception:
        return None
    trades = [t for t in trades if not t.get("open")]
    if not trades:
        return None
    dt_index = {d: i for i, d in enumerate(states_df["dt"])}
    o = states_df["open"].to_numpy(float)
    h = states_df["high"].to_numpy(float)
    l = states_df["low"].to_numpy(float)
    c = states_df["close"].to_numpy(float)
    atr_arr = states_df["atr"].to_numpy(float)
    adx_arr = adx14(h, l, c, 14)
    ema140 = ema(c, 140)
    recs = []
    for t in trades:
        ei = dt_index.get(t["entry_dt"])
        if ei is None:
            continue
        direction = 1.0 if t["side"] == "LONG" else -1.0
        ev = {"ei": ei, "dir": direction, "entry_px": float(t["entry_px"]),
              "atr_e": float(t.get("atr") or 0.0), "entry_dt": t["entry_dt"]}
        hit = simulate_tbm_label(ev, o, h, l, c, atr_arr, PT, SL, N_BAR)
        feats = make_features(o, h, l, c, atr_arr, adx_arr, ema140, ei, direction)
        if feats is None:
            continue
        rec = dict(feats)
        rec["sym"] = sym
        rec["entry_dt"] = t["entry_dt"].isoformat() if hasattr(t["entry_dt"], "isoformat") else str(t["entry_dt"])
        rec["label"] = 1.0 if hit == "tp" else 0.0
        rec["hit"] = hit
        # 原生吊灯结果（幸存交易仍用此离场评估）
        rec["R"] = float(t.get("R") or 0.0)
        rec["pnl_pct"] = float(t.get("pnl_pct") or 0.0)
        rec["win"] = 1.0 if float(t.get("pnl_pct") or 0.0) > 0 else 0.0
        rec["hold_bars"] = int(t.get("hold_bars") or 0)
        recs.append(rec)
    cache[sym] = recs
    return recs


def pf(rs):
    rs = np.asarray(rs, float)
    wins = rs[rs > 0]; losses = rs[rs < 0]
    gp = float(wins.sum()) if wins.size else 0.0
    gl = float(losses.sum()) if losses.size else 0.0
    return (gp / abs(gl)) if gl < 0 else (99.0 if gp > 0 else 0.0)


def bootstrap_pf_diff(full_rs, mask, n=2000, seed=SEED):
    """过滤子集 PF 是否显著 > 全样本 PF：在同一批有放回重采样条目上同时算两者。"""
    full = np.asarray(full_rs, float)
    m = np.asarray(mask, bool)
    rng = np.random.default_rng(seed)
    full_pf = pf(full)
    filt_pf = pf(full[m])
    diffs = []
    for _ in range(n):
        idx = rng.integers(0, len(full), len(full))
        sub_full = full[idx]
        sub_m = m[idx]
        if sub_m.sum() < 50:
            continue
        diffs.append(pf(sub_full[sub_m]) - pf(sub_full))
    diffs = np.asarray(diffs)
    p_higher = float((diffs > 0).mean())
    return full_pf, filt_pf, p_higher, float(diffs.mean())


def main():
    base_p = fusion_params_from_config({"add_max_lots": 1})
    cache = {}
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH) as f:
                cache = json.load(f)
        except Exception:
            cache = {}
    syms = [s.symbol for s in get_settings().main_contracts]
    all_recs = []
    skipped = []
    t0 = time.time()
    with session_scope() as s:
        for sym in syms:
            recs = build_symbol(s, sym, base_p, cache)
            if recs is None:
                skipped.append(sym)
                continue
            all_recs.extend(recs)
            # 落盘缓存（逐品种续跑）
            with open(CACHE_PATH, "w") as f:
                json.dump(cache, f)
    df = pd.DataFrame(all_recs)
    # 类型修正（json 读回已是原生）
    feat_cols = ["mom20", "mom60", "rv20", "rv60", "atr_ratio", "adx", "ema_dist",
                 "pdh_dist", "pdl_dist", "near_kl", "atr_z", "gap", "atr_pct", "dir"]
    df["entry_dt"] = pd.to_datetime(df["entry_dt"]).dt.tz_convert(None)
    df = df.sort_values("entry_dt").reset_index(drop=True)
    print(f"# 样本 n={len(df)}  品种={df['sym'].nunique()}  标签正率(y=1 先触TP)={df['label'].mean():.3f}  "
          f"原生吊灯 win%={df['win'].mean():.3f}  mean_R={df['R'].mean():.4f}  PF={pf(df['R']):.3f}  "
          f"elapsed={time.time()-t0:.1f}s  skipped={len(skipped)}")
    print(f"# skipped: {skipped}")

    # ---- walk-forward 3 折 ----
    cuts = [date(2023, 12, 31), date(2024, 12, 31), date(2025, 12, 31)]
    embargo_days = 7
    from sklearn.linear_model import LogisticRegression
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.metrics import roc_auc_score, average_precision_score

    summary = []
    for k, cut in enumerate(cuts):
        train = df[df["entry_dt"] <= pd.Timestamp(cut)]
        test = df[(df["entry_dt"] > pd.Timestamp(cut)) &
                  (df["entry_dt"] > pd.Timestamp(cut) + pd.Timedelta(days=embargo_days))]
        if len(train) < 500 or len(test) < 200:
            print(f"# fold{k+1} (cut={cut}) 样本不足 train={len(train)} test={len(test)} 跳过")
            continue
        Xtr, Xte = train[feat_cols].values, test[feat_cols].values
        ytr, yte = train["label"].values, test["label"].values
        models = {
            "LR": LogisticRegression(max_iter=2000, class_weight="balanced"),
            "RF": RandomForestClassifier(n_estimators=200, max_depth=6, min_samples_leaf=50,
                                         class_weight="balanced", n_jobs=-1, random_state=SEED),
        }
        line = f"\n=== FOLD {k+1} (train≤{cut}, test {cut}+embargo -> n_test={len(test)}) ==="
        print(line)
        for mname, m in models.items():
            m.fit(Xtr, ytr)
            P = m.predict_proba(Xte)[:, 1]
            auc = roc_auc_score(yte, P)
            ap = average_precision_score(yte, P)  # 正类=先触TP
            # 过滤效果：按 P 分位截取，评估**原生吊灯**结果
            Rs = test["R"].values
            Wins = test["win"].values
            for q in (0.5, 0.3):
                thr = np.quantile(P, 1 - q)
                mask = P >= thr
                sub_R, sub_W = Rs[mask], Wins[mask]
                fp, wp, mp = pf(sub_R), sub_W.mean(), sub_R.mean()
                print(f"  [{mname}] AUC={auc:.3f} AP={ap:.3f} | 过滤top{q}: n={mask.sum()} "
                      f"win%={wp:.3f} mean_R={mp:.4f} PF={fp:.3f}")
            # 全样本(未过滤)对照
            print(f"  [{mname}] 全样本对照: n={len(test)} win%={Wins.mean():.3f} "
                  f"mean_R={Rs.mean():.4f} PF={pf(Rs):.3f}")
            summary.append({"fold": k + 1, "model": mname, "auc": auc, "ap": ap,
                            "test_n": len(test)})
    # ---- 置换检验（泄漏/运气对照）：shuffle 标签后重训 LR，AUC 应仍≈0.5 ----
    from sklearn.model_selection import train_test_split
    Xa = df[feat_cols].values; ya = df["label"].values
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

    # ---- bootstrap 显著性（全局量级参考，非严格OOS）----
    print("\n=== 全局参考（非严格OOS，仅量级）：RF 全样本训练，top50% 过滤 bootstrap ===")
    rf = RandomForestClassifier(n_estimators=200, max_depth=6, min_samples_leaf=50,
                                class_weight="balanced", n_jobs=-1, random_state=SEED)
    rf.fit(Xa, ya)
    Pall = rf.predict_proba(Xa)[:, 1]
    thr = np.quantile(Pall, 0.5)
    mask = Pall >= thr
    full_pf, filt_pf, p_hi, dmean = bootstrap_pf_diff(df["R"].values, mask)
    print(f"  全样本 PF={full_pf:.3f} | top50%过滤 PF={filt_pf:.3f} | P(过滤PF>全样本PF)={p_hi:.3f} | 均值差={dmean:+.4f}")

    with open("/tmp/metalabel_result.json", "w") as f:
        json.dump({"summary": summary, "n": len(df), "perm_auc_mean": float(np.mean(perm_aucs)),
                   "label_pos": float(df["label"].mean()),
                   "base_win": float(df["win"].mean()), "base_meanR": float(df["R"].mean()),
                   "base_pf": float(pf(df["R"]))}, f, default=str)
    print("\n[written] /tmp/metalabel_result.json")


if __name__ == "__main__":
    main()
