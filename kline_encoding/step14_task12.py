# -*- coding: utf-8 -*-
"""Step 14: 任务①影线位置独立二值特征 vs 实体方向 + 任务②波动率regime
统一框架: 2020-2026 全加载, 发现期/验证期/holdout 分层
"""
import pandas as pd, numpy as np, json
from scipy import stats

OUT = "E:/抖音分析/2026-10-16-19-50-44/kline_encoding"
STATES = ["000000", "000001", "000100", "000101", "010000", "010001", "010100", "010101"]

def encode_ohlc(o, h, l, c):
    body = np.where(c >= o, "01", "00")
    upper = np.where(h > np.maximum(o, c), "01", "00")
    lower = np.where(l < np.minimum(o, c), "01", "00")
    return np.char.add(np.char.add(body, upper), lower)

def sess_key(dt):
    h = dt.hour + dt.minute / 60
    s = 0 if 9 <= h < 11.5 else (1 if 13.5 <= h < 15 else (2 if 21 <= h < 23 else -1))
    return dt.normalize(), s

def load_all(sym):
    frames = []
    d0 = pd.read_csv(f"{OUT}/{sym}_2020_1m_raw.csv", parse_dates=["datetime"])
    d0["year"] = 2020; frames.append(d0)
    d1 = pd.read_csv(f"{OUT}/{sym}_2021_2025_1m_raw.csv", parse_dates=["datetime"])
    d1["year"] = d1.datetime.dt.year; frames.append(d1)
    d2 = pd.read_csv(f"{OUT}/{sym}_2026_1m_raw.csv", parse_dates=["datetime"])
    d2["year"] = 2026; frames.append(d2)
    return pd.concat(frames, ignore_index=True).sort_values("datetime").reset_index(drop=True)

def prep(sym):
    d = load_all(sym)
    o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
    d["code"] = encode_ohlc(o, h, l, c)
    d["chg"] = (c - o) / o * 100.0
    d["amp"] = (h - l) / o * 100.0
    d["body_up"] = (d.code.str[:2] == "01")
    d["has_upper"] = (d.code.str[2:4] == "01")
    d["has_lower"] = (d.code.str[4:] == "01")
    keys = [sess_key(t) for t in d.datetime]
    tkey = np.array([k[0].value * 10 + k[1] for k in keys])
    gap = d.datetime.diff().dt.total_seconds().values
    adj = np.zeros(len(d), bool); adj[1:] = gap[1:] == 60.0
    d["adj"] = adj
    d["year"] = d.datetime.dt.year.values
    d["day"] = d.datetime.dt.date.values
    # 量价特征 (只用 t 及之前)
    vol = d.volume.values.astype(float)
    vol_s = pd.Series(vol)
    m20 = vol_s.shift(1).rolling(20, min_periods=10).mean().values
    s20 = vol_s.shift(1).rolling(20, min_periods=10).std().values
    d["vol_ratio"] = vol / np.where(m20 > 0, m20, np.nan)
    d["vol_z"] = (vol - m20) / np.where(s20 > 0, s20, np.nan)
    oi = d.close_interest.values.astype(float)
    d["oi_chg"] = np.concatenate([[np.nan], np.diff(oi)]) / np.where(oi > 0, oi, np.nan)
    oi_s = pd.Series(oi)
    d["oi_trend"] = oi / oi_s.shift(1).rolling(20, min_periods=10).mean().values - 1
    return d

def t_of(x):
    x = np.asarray(x); x = x[~np.isnan(x)]
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and x.std(ddof=1) > 0 else 0.0

def grp_stats(mask_prev, d):
    """mask_prev: 前一根(信号根)的布尔; 返回下一根 chg 的统计"""
    sel = d.adj.values[1:] & mask_prev[:-1]
    x = d.chg.values[1:][sel]
    x = x[~np.isnan(x)]
    if len(x) < 30: return None
    return {"n": int(len(x)), "mean": float(x.mean()), "t": t_of(x), "win": float((x > 0).mean())}

def r2_groups(d, groups):
    """groups: list of bool masks over prev-bar (须为同一总体的互斥完备划分); 返回该分组对 next_chg 的 R²"""
    sel = d.adj.values[1:]
    y_all = d.chg.values[1:][sel]
    gmasks = []
    for m in groups:
        g = m[:-1][sel]
        gmasks.append(g)
    cover = np.zeros(len(y_all), bool)
    for g in gmasks: cover |= g
    y = y_all[cover]                      # 只在被分组覆盖的样本上计算, 避免组外样本白送解释力
    grand = y.mean(); ss_tot = ((y - grand) ** 2).sum(); ss_res = 0.0
    for g in gmasks:
        gg = g[cover]
        if gg.sum() < 10: continue
        ss_res += ((y[gg] - y[gg].mean()) ** 2).sum()
    return float(1 - ss_res / ss_tot)

RES = {"features": {}, "regime": {}, "meta": {}}
for sym in ["MA", "EG"]:
    print(f"\n{'='*26} {sym} {'='*26}")
    d = prep(sym)
    n_by_year = {int(y): int((d.year == y).sum()) for y in sorted(set(d.year))}
    print("  各年根数:", n_by_year)
    RES["meta"][sym] = {"n_by_year": n_by_year, "total": int(len(d))}

    # ============ 任务①: 影线位置独立二值特征 ============
    feats = {}
    # 单特征对比 (发现期 2020-2025, 验证期 2026)
    for feat in ["body_up", "has_upper", "has_lower"]:
        v = d[feat].values
        for period, yrs in [("disc_2020_2025", list(range(2020, 2026))), ("holdout_2026", [2026])]:
            m_year = np.isin(d.year.values, yrs)
            g1 = grp_stats(v & m_year, d); g0 = grp_stats((~v) & m_year, d)
            if g1 and g0:
                feats[f"{feat}_{period}"] = {"on": g1, "off": g0,
                                             "diff": g1["mean"] - g0["mean"],
                                             "t_diff": float((g1["mean"] - g0["mean"]) / np.sqrt(g1["mean"] ** 0 + 1e-12)) if False else None}
        # R² 单特征
        for period, yrs in [("disc_2020_2025", list(range(2020, 2026))), ("holdout_2026", [2026])]:
            m_year = np.isin(d.year.values, yrs)
            vv = d[feat].values & m_year
            feats[f"{feat}_r2_{period}"] = r2_groups(d, [vv, (~d[feat].values) & m_year])
    # 双特征交互 (发现期)
    for f1, f2 in [("body_up", "has_upper"), ("body_up", "has_lower"), ("has_upper", "has_lower")]:
        a, b = d[f1].values, d[f2].values
        cells = {}
        for i, j in [(True, True), (True, False), (False, True), (False, False)]:
            s = grp_stats(a == i, d) if False else grp_stats((a == i) & (b == j), d)
            if s: cells[f"{int(i)}{int(j)}"] = s
        feats[f"cell_{f1}_x_{f2}"] = cells
    # 三特征 R² 对比: 影线组合(4态) vs 实体(2态) vs 形态码(8态) vs CLV(20箱)
    sh = np.char.add(np.where(d.has_upper.values, "U", "-"), np.where(d.has_lower.values, "L", "-"))
    o_, h_, l_, c_ = d.open.values, d.high.values, d.low.values, d.close.values
    clv = np.where(h_ > l_, (c_ - l_) / np.where(h_ > l_, h_ - l_, 1), 0.5)
    d["clv"] = clv
    clv_bin = np.clip((clv * 20).astype(int), 0, 19)
    for period, yrs in [("disc_2020_2025", list(range(2020, 2026))), ("holdout_2026", [2026])]:
        m_year = np.isin(d.year.values, yrs)
        g_shadow = [np.isin(sh, [x]) & m_year for x in ["--", "U-", "-L", "UL"]]
        g_body = [(d.body_up.values == x) & m_year for x in [True, False]]
        g_code = [(d.code.values == x) & m_year for x in STATES]
        g_clv = [(clv_bin == i) & m_year for i in range(20)]
        feats[f"r2_shadow4_{period}"] = r2_groups(d, g_shadow)
        feats[f"r2_body2_{period}"] = r2_groups(d, g_body)
        feats[f"r2_code8_{period}"] = r2_groups(d, g_code)
        feats[f"r2_clv20_{period}"] = r2_groups(d, g_clv)
        # CLV箱内再分实体方向(检验控制CLV后实体是否还有增量)
        g_clv_body = [((clv_bin == i) & (d.body_up.values == b)) & m_year for i in range(20) for b in [True, False]]
        feats[f"r2_clv_x_body_{period}"] = r2_groups(d, g_clv_body)
    # 单特征 |t| 汇总 (发现期 + holdout)
    print("  [任务① 单特征预测力] 特征 | E[next|开]-E[next|关] 差值 t | R²")
    for feat in ["body_up", "has_upper", "has_lower"]:
        a = feats[f"{feat}_disc_2020_2025"]; b = feats[f"{feat}_holdout_2026"]
        print(f"    {feat:10s} 发现期 diff={a['diff']:+.4f}% (on {a['on']['mean']:+.4f} vs off {a['off']['mean']:+.4f}) R²={feats[f'{feat}_r2_disc_2020_2025']:.4f}"
              f" | holdout diff={b['diff']:+.4f}% R²={feats[f'{feat}_r2_holdout_2026']:.4f}")
    print(f"    分组R²: 影线组合4态={feats['r2_shadow4_disc_2020_2025']:.4f} 实体2态={feats['r2_body2_disc_2020_2025']:.4f} CLV20箱={feats['r2_clv20_disc_2020_2025']:.4f} CLV×实体={feats['r2_clv_x_body_disc_2020_2025']:.4f} 形态码8态={feats['r2_code8_disc_2020_2025']:.4f}"
          f" | holdout: 影线{feats['r2_shadow4_holdout_2026']:.4f}/实体{feats['r2_body2_holdout_2026']:.4f}/CLV{feats['r2_clv20_holdout_2026']:.4f}/CLV×实体{feats['r2_clv_x_body_holdout_2026']:.4f}/码8{feats['r2_code8_holdout_2026']:.4f}")
    RES["features"][sym] = feats

    # ============ 任务②: 形态码 → 下一根振幅 regime ============
    reg = {}
    nxt_amp = d.amp.values[1:]
    sel = d.adj.values[1:]
    prev_code = d.code.values[:-1][sel]
    na = nxt_amp[sel]
    for period, yrs in [("disc_2020_2025", list(range(2020, 2026))), ("holdout_2026", [2026])]:
        my = np.isin(d.year.values[:-1][sel], yrs)
        rows = {}
        for s in STATES:
            m = my & (prev_code == s)
            if m.sum() > 30:
                rows[s] = {"n": int(m.sum()), "mean_amp": float(na[m].mean()), "med_amp": float(np.median(na[m]))}
        reg[f"amp_{period}"] = rows
    # 方向信号在不同 regime 下的效应 (按发现期的 regime 分位划分, 应用到 holdout)
    amp_disc = reg["amp_disc_2020_2025"]
    order = sorted(STATES, key=lambda s: amp_disc.get(s, {}).get("mean_amp", 0))
    q1 = np.percentile([amp_disc[s]["mean_amp"] for s in STATES], 33)
    q2 = np.percentile([amp_disc[s]["mean_amp"] for s in STATES], 67)
    regime_map = {s: ("低波" if amp_disc[s]["mean_amp"] <= q1 else ("中波" if amp_disc[s]["mean_amp"] <= q2 else "高波")) for s in STATES}
    reg["regime_map"] = regime_map
    reg["amp_order"] = order
    # 每个方向信号按 regime 分层 (发现期 + holdout)
    sig_reg = {}
    for s in ["010000", "000000", "000100", "010001"]:
        rows = {}
        for period, yrs in [("disc_2020_2025", list(range(2020, 2026))), ("holdout_2026", [2026])]:
            my = np.isin(d.year.values, yrs)
            for rg in ["低波", "中波", "高波"]:
                states_in = [x for x in STATES if regime_map[x] == rg]
                m_prev = np.isin(d.code.values, states_in) & (d.code.values == s) if False else (d.code.values == s)
                # 信号自身形态 s, 但 regime 由 s 自己决定 -> 恒为同一regime; 改为: 用"信号后第一根"的形态码预测regime不成立
                # 正确做法: regime 用 t-1 形态 (信号出现前的波动状态), 看信号效应是否被事前regime调制
                m_pre = np.zeros(len(d), bool)
                m_pre[1:] = np.isin(d.code.values[:-1], states_in) & (d.code.values[1:] == s)
                st = grp_stats(m_pre & my, d)
                if st: rows[f"{rg}_{period}"] = st
        sig_reg[s] = rows
    reg["sig_by_preregime"] = sig_reg
    print("  [任务② 波动率regime] 按形态码预测的下一根振幅排序(发现期):")
    print("    " + " | ".join(f"{s}:{amp_disc[s]['mean_amp']:.3f}%({regime_map[s]})" for s in order))
    print("    方向信号按『事前regime』(信号前一根的形态)分层效应:")
    for s in ["010000", "000000", "000100", "010001"]:
        cells = []
        for rg in ["低波", "中波", "高波"]:
            a = sig_reg[s].get(f"{rg}_disc_2020_2025"); b = sig_reg[s].get(f"{rg}_holdout_2026")
            if a: cells.append(f"{rg}: {a['mean']:+.4f}(t{a['t']:+.0f},n{a['n']})" + (f" [26:{b['mean']:+.4f}]" if b else " [26:n/a]"))
        print(f"      {s}: " + " | ".join(cells))
    RES["regime"][sym] = reg

json.dump(RES, open(f"{OUT}/results_task12.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("\nsaved results_task12.json")
