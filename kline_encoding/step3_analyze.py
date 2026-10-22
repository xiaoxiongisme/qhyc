# -*- coding: utf-8 -*-
"""Step 3: 规律统计分析 —— 形态分布 / 马尔可夫转移 / 条件方向 / 影线信息 / 自相关 / 互信息 / 分时段 / 序列模式 / 稳健性"""
import pandas as pd, numpy as np, json
from scipy import stats

OUT = "E:/抖音分析/2026-10-16-19-50-44/kline_encoding"
df = pd.read_pickle(f"{OUT}/all_2020_1m.pkl")
df["dt"] = pd.to_datetime(df.datetime)
df = df.sort_values(["sym", "dt"]).reset_index(drop=True)

STATES = ["000000", "000001", "000100", "000101", "010000", "010001", "010100", "010101"]
STATE_NAMES = {
    "000000": "跌·无上影·无下影", "000001": "跌·无上影·有下影", "000100": "跌·有上影·无下影", "000101": "跌·有上影·有下影",
    "010000": "涨·无上影·无下影", "010001": "涨·无上影·有下影", "010100": "涨·有上影·无下影", "010101": "涨·有上影·有下影",
}
R = {}

def two_prop_z(p1, n1, p2, n2):
    if n1 == 0 or n2 == 0: return 0.0, 1.0
    pp = (p1 * n1 + p2 * n2) / (n1 + n2)
    se = np.sqrt(max(pp * (1 - pp) * (1 / n1 + 1 / n2), 1e-18))
    z = (p1 - p2) / se
    return z, 2 * (1 - stats.norm.cdf(abs(z)))

for sym in ["MA", "EG"]:
    d = df[df.sym == sym].reset_index(drop=True)
    r = {}
    o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
    code, chg, amp = np.asarray(d.code, dtype=object), d.chg_pct.values, d.amp_pct.values
    n = len(d)
    up = c >= o
    doji = c == o
    zero_rng = h == l

    # ---------- A. 基础统计 ----------
    r["n"] = int(n)
    r["days"] = int(d.dt.dt.date.nunique())
    r["up_rate"] = float(up.mean())
    r["up_rate_exdoji"] = float((c[~doji] > o[~doji]).mean())
    r["doji_rate"] = float(doji.mean())
    r["zero_range_rate"] = float(zero_rng.mean())
    r["chg_abs_med"] = float(np.median(np.abs(chg)))
    r["amp_med"] = float(np.median(amp))
    r["amp_mean"] = float(amp.mean())
    r["price_mean"] = float(c.mean())
    # 连续性: open_t == close_{t-1}
    cont = o[1:] == c[:-1]
    r["cont_rate"] = float(cont.mean())

    # ---------- B. 形态分布 ----------
    vc = pd.Series(code).value_counts()
    r["state_counts"] = {s: int(vc.get(s, 0)) for s in STATES}
    r["state_pct"] = {s: float(vc.get(s, 0) / n * 100) for s in STATES}
    # 各字段边际概率
    body = np.array([s[:2] for s in code]); up_s = np.array([s[2:4] for s in code]); lo_s = np.array([s[4:6] for s in code])
    r["p_upper"] = float((up_s == "01").mean()); r["p_lower"] = float((lo_s == "01").mean())
    r["p_upper_up"] = float((up_s == "01")[body == "01"].mean()); r["p_upper_down"] = float((up_s == "01")[body == "00"].mean())
    r["p_lower_up"] = float((lo_s == "01")[body == "01"].mean()); r["p_lower_down"] = float((lo_s == "01")[body == "00"].mean())

    # ---------- C. 马尔可夫转移矩阵 ----------
    s_now, s_nxt = code[:-1], code[1:]
    idx = {s: i for i, s in enumerate(STATES)}
    cm = np.zeros((8, 8), dtype=int)
    for a, b in zip(s_now, s_nxt):
        cm[idx[a], idx[b]] += 1
    r["trans_counts"] = cm.tolist()
    row = cm.sum(1, keepdims=True); col = cm.sum(0, keepdims=True).T
    P = cm / np.maximum(row, 1)
    marg = cm.sum(0) / cm.sum()
    lift = P / np.maximum(marg[None, :], 1e-12)
    r["trans_prob"] = P.tolist()
    r["trans_lift"] = lift.tolist()
    # 对角线（同形态延续）vs 1/8
    same = np.trace(cm) / cm.sum()
    z_same, p_same = two_prop_z(same, cm.sum(), 1 / 8, cm.sum())
    r["same_state_rate"] = float(same); r["same_state_z"] = float(z_same); r["same_state_p"] = float(p_same)
    # 显著偏离的单元格 (|z|>3)
    sig = []
    for i in range(8):
        for j in range(8):
            z, p = two_prop_z(P[i, j], row[i, 0], marg[j], cm.sum())
            if abs(z) > 3 and P[i, j] > 0.01:
                sig.append({"from": STATES[i], "to": STATES[j], "p": float(P[i, j]), "marg": float(marg[j]),
                            "lift": float(lift[i, j]), "z": float(z)})
    sig.sort(key=lambda x: -abs(x["z"]))
    r["sig_trans"] = sig[:24]

    # ---------- D. 方向持续性 ----------
    up_nxt = up[1:]
    p_up_after_up = up[1:][up[:-1]].mean(); n_uu = int(up[:-1].sum())
    p_up_after_dn = up[1:][~up[:-1]].mean(); n_ud = int((~up[:-1]).sum())
    z_mom, p_mom = two_prop_z(p_up_after_up, n_uu, p_up_after_dn, n_ud)
    r["p_up_after_up"] = float(p_up_after_up); r["p_up_after_down"] = float(p_up_after_dn)
    r["mom_z"] = float(z_mom); r["mom_p"] = float(p_mom); r["n_uu"] = n_uu; r["n_ud"] = n_ud
    # lag2/3
    for lag in (2, 3):
        u_now = up[:-lag]; u_next = up[lag:]
        r[f"p_up_after_up_lag{lag}"] = float(u_next[u_now].mean()); r[f"p_up_after_dn_lag{lag}"] = float(u_next[~u_now].mean())
    # 连涨/连跌后
    for k in (2, 3):
        streak = np.ones(n, bool)
        for i in range(1, k + 1):
            streak &= (np.roll(up, i) == up)
        streak[:k] = False
        r[f"p_up_after_{k}up"] = float(up[1:][streak[:-1] & up[:-1]].mean())
        r[f"n_after_{k}up"] = int((streak[:-1] & up[:-1]).sum())
        r[f"p_up_after_{k}down"] = float(up[1:][streak[:-1] & ~up[:-1]].mean())
        r[f"n_after_{k}down"] = int((streak[:-1] & ~up[:-1]).sum())

    # ---------- E. 条件下一根涨跌幅 ----------
    nxt_chg = chg[1:]
    cond = []
    for s in STATES:
        m = s_now == s
        x = nxt_chg[m]
        if len(x) > 30:
            t, p = stats.ttest_1samp(x, 0)
            cond.append({"state": s, "n": int(len(x)), "mean": float(x.mean()), "med": float(np.median(x)),
                         "t": float(t), "win": float((x > 0).mean())})
    r["cond_next_chg"] = cond

    # ---------- F. 自相关 ----------
    zc = (chg - chg.mean()) / chg.std()
    r["acf_chg"] = [float(pd.Series(zc).autocorr(k)) for k in range(1, 11)]
    r["acf_abschg"] = [float(pd.Series(np.abs(zc)).autocorr(k)) for k in range(1, 6)]
    # 符号自相关
    r["acf_sign"] = [float(pd.Series(np.sign(chg)).autocorr(k)) for k in range(1, 6)]

    # ---------- G. 互信息 ----------
    def mi_bits(x, y, kx, ky):
        cmh = np.zeros((kx, ky))
        xi = np.array([idx[s] for s in x]); yi = np.array([idx[s] for s in y]) if ky == 8 else y.astype(int)
        np.add.at(cmh, (xi, yi), 1)
        pxy = cmh / cmh.sum(); px = pxy.sum(1, keepdims=True); py = pxy.sum(0, keepdims=True)
        den = px @ py
        nz = pxy > 0
        return float((pxy[nz] * np.log2(pxy[nz] / den[nz])).sum())
    r["mi_state_next"] = mi_bits(s_now, s_nxt, 8, 8)
    r["mi_state_nextsign"] = mi_bits(s_now, up_nxt.astype(int), 8, 2)
    # 置换检验
    rng = np.random.default_rng(7)
    sh = np.asarray(s_nxt, dtype=object).copy()
    null = []
    for _ in range(300):
        rng.shuffle(sh)
        null.append(mi_bits(s_now, sh, 8, 8))
    null = np.array(null)
    r["mi_null_mean"] = float(null.mean()); r["mi_null_p"] = float((null >= r["mi_state_next"]).mean())

    # ---------- H. 分时段 ----------
    hh = d.dt.dt.hour.values; mm = d.dt.dt.minute.values
    seg = np.full(n, "other", dtype=object)
    seg[(hh == 21) | (hh == 22)] = "夜盘21-23"
    seg[hh == 9] = "早盘9-10"
    seg[(hh == 10) & (mm >= 30) | (hh == 11)] = "上午10:30-11:30"
    seg[(hh == 13) | (hh == 14)] = "下午13:30-15"
    d["seg"] = seg
    segstats = []
    for s in ["夜盘21-23", "早盘9-10", "上午10:30-11:30", "下午13:30-15"]:
        m = d.seg == s
        if m.sum() == 0: continue
        segstats.append({"seg": s, "n": int(m.sum()), "up_rate": float(up[m.values].mean()),
                         "chg_med": float(np.median(chg[m.values])), "amp_med": float(np.median(amp[m.values])),
                         "p_upper": float((up_s[m.values] == "01").mean()), "p_lower": float((lo_s[m.values] == "01").mean())})
    r["segments"] = segstats
    # 每时段第一根（跳空后首根）
    gap = d.dt.diff().dt.total_seconds().values
    first = np.zeros(n, bool); first[0] = True; first[1:] = gap[1:] > 1200
    r["first_bar_n"] = int(first.sum())
    r["first_bar_up_rate"] = float(up[first].mean())
    r["first_bar_chg_med"] = float(np.median(chg[first]))
    r["first_bar_chg_mean"] = float(chg[first].mean())
    t_fb, p_fb = stats.ttest_1samp(chg[first], 0)
    r["first_bar_t"] = float(t_fb); r["first_bar_p"] = float(p_fb)
    r["nonfirst_chg_mean"] = float(chg[~first].mean())

    # ---------- I. 两形态历史 → 下一根方向 ----------
    pair = {}
    for i in range(n - 2):
        key = code[i] + "|" + code[i + 1]
        e = pair.setdefault(key, [0, 0])
        e[0] += 1; e[1] += int(up[i + 2])
    pats = []
    base = up.mean()
    for k, (cnt, ups) in pair.items():
        if cnt >= 400:
            p_ = ups / cnt
            z, pv = two_prop_z(p_, cnt, base, n)
            pats.append({"pair": k, "n": cnt, "p_up_next": float(p_), "edge": float(p_ - base), "z": float(z), "p": float(pv)})
    pats.sort(key=lambda x: -x["z"])
    r["patterns_top"] = pats[:12]
    pats.sort(key=lambda x: x["z"])
    r["patterns_bottom"] = pats[:12]
    r["pattern_base_up"] = float(base)
    r["n_patterns"] = len(pats)

    # ---------- J. 稳健性: 剔除极端bar(|chg|>1%, 疑似换月/异常) ----------
    keep = np.abs(chg) <= 1.0
    r["extreme_n"] = int((~keep).sum())
    kk = keep[:-1] & keep[1:]
    su, sn = np.asarray(code[:-1], dtype=object)[kk], np.asarray(code[1:], dtype=object)[kk]
    cmk = np.zeros((8, 8), dtype=int)
    for a, b in zip(su, sn): cmk[idx[a], idx[b]] += 1
    Pk = cmk / np.maximum(cmk.sum(1, keepdims=True), 1)
    r["trans_prob_clean"] = Pk.tolist()
    uk = up[1:][kk]
    body_su = np.array([a[:2] for a in su])
    puu = uk[body_su == "01"].mean(); pud = uk[body_su == "00"].mean()
    zk, pk = two_prop_z(puu, (body_su == "01").sum(), pud, (body_su == "00").sum())
    r["p_up_after_up_clean"] = float(puu); r["p_up_after_down_clean"] = float(pud); r["mom_z_clean"] = float(zk)
    # 上下半年
    h1 = d.dt.dt.month.values <= 6
    r["h1_up_rate"] = float(up[h1].mean()); r["h2_up_rate"] = float(up[~h1].mean())
    m1 = h1[:-1] & up[:-1]; m2 = (~h1[:-1]) & up[:-1]
    r["h1_mom"] = float(up[1:][m1].mean()) if m1.sum() else None
    r["h2_mom"] = float(up[1:][m2].mean()) if m2.sum() else None

    R[sym] = r

# ---------- K. 跨品种共性 ----------
shared = []
for i in range(8):
    for j in range(8):
        l1, l2 = R["MA"]["trans_lift"][i][j], R["EG"]["trans_lift"][i][j]
        z1, _ = two_prop_z(R["MA"]["trans_prob"][i][j], R["MA"]["trans_counts"][i][j + 0] if False else sum(R["MA"]["trans_counts"][i]), R["MA"]["state_pct"][STATES[j]] / 100, R["MA"]["n"])
        z2, _ = two_prop_z(R["EG"]["trans_prob"][i][j], sum(R["EG"]["trans_counts"][i]), R["EG"]["state_pct"][STATES[j]] / 100, R["EG"]["n"])
        if z1 > 3 and z2 > 3:
            shared.append({"from": STATES[i], "to": STATES[j], "lift_MA": l1, "lift_EG": l2})
shared.sort(key=lambda x: -(x["lift_MA"] + x["lift_EG"]))
R["shared_trans"] = shared[:15]

with open(f"{OUT}/analysis_results.json", "w", encoding="utf-8") as f:
    json.dump(R, f, ensure_ascii=False, indent=1)
print("saved analysis_results.json")
for sym in ["MA", "EG"]:
    r = R[sym]
    print(f"\n== {sym} == n={r['n']:,} up={r['up_rate']:.3f} up_exdoji={r['up_rate_exdoji']:.3f} doji={r['doji_rate']:.3f} cont={r['cont_rate']:.4f}")
    print(f"  same-state={r['same_state_rate']:.4f} (z={r['same_state_z']:.1f})  MI(S;S+)={r['mi_state_next']:.4f}bits (null {r['mi_null_mean']:.4f}, p={r['mi_null_p']:.3f})  MI(S;sign+)={r['mi_state_nextsign']:.4f}bits")
    print(f"  P(up|up)={r['p_up_after_up']:.4f} P(up|dn)={r['p_up_after_down']:.4f} z={r['mom_z']:.2f}")
    print(f"  clean: P(up|up)={r['p_up_after_up_clean']:.4f} P(up|dn)={r['p_up_after_down_clean']:.4f} z={r['mom_z_clean']:.2f}")
    print(f"  acf_chg lags1-5: {[round(x,4) for x in r['acf_chg'][:5]]}")
    print(f"  acf_sign lags1-3: {[round(x,4) for x in r['acf_sign'][:3]]}")
    print(f"  first-bar: n={r['first_bar_n']} up={r['first_bar_up_rate']:.3f} meanchg={r['first_bar_chg_mean']:.4f}% t={r['first_bar_t']:.2f}")
    print(f"  segments: " + " | ".join(f"{s['seg']}:up={s['up_rate']:.3f},amp={s['amp_med']:.3f}" for s in r['segments']))
    print(f"  top patterns: " + " ; ".join(f"{p['pair']} n={p['n']} p={p['p_up_next']:.3f} z={p['z']:.1f}" for p in r['patterns_top'][:6]))
