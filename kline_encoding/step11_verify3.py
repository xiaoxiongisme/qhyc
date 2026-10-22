# -*- coding: utf-8 -*-
"""Step 11: 5m/15m口径修正 + CLV vs 形态码增量检验
V8 5m/15m: 连续段感知重采样(剔除跨休市桶) + 相邻bar按时间差==mins过滤, 前后对比
V10 增量检验: 形态码的预测力有多少来自CLV(收盘位置)? 控制CLV后影线字段是否还有增量?
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

def t_of(x):
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and x.std(ddof=1) > 0 else 0.0

RES = {"htf_fix": {}, "clv_vs_code": {}}
for sym in ["MA", "EG"]:
    print(f"\n{'='*26} {sym} {'='*26}")
    d0 = pd.read_csv(f"{OUT}/{sym}_2020_1m_raw.csv", parse_dates=["datetime"])
    d1 = pd.read_csv(f"{OUT}/{sym}_2021_2025_1m_raw.csv", parse_dates=["datetime"])
    d = pd.concat([d0, d1], ignore_index=True).sort_values("datetime").reset_index(drop=True)
    print(f"  [load] n={len(d):,} (2020-2025 全样本)")
    o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
    d["dt"] = d.datetime
    d["code"] = encode_ohlc(o, h, l, c)
    d["chg"] = (c - o) / o * 100.0
    keys = [sess_key(t) for t in d.datetime]
    d["sdate"] = [k[0] for k in keys]; d["sess"] = [k[1] for k in keys]
    gap = d.datetime.diff().dt.total_seconds().values
    d = d[d.sess >= 0].reset_index(drop=True)
    o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
    chg = d.chg.values
    gap = d.datetime.diff().dt.total_seconds().values
    # 连续段编号 (gap==60s 同一连续段; 休市/隔夜断开)
    seg = np.zeros(len(d), int)
    for i in range(1, len(d)):
        seg[i] = seg[i-1] if gap[i] == 60.0 else seg[i-1] + 1
    d["seg"] = seg
    d["seq_in_seg"] = d.groupby("seg").cumcount()

    # ---- V8 重采样: 连续段内分桶 ----
    htf_rows = []
    for tf, mins in (("5m", 5), ("15m", 15)):
        d["bucket"] = d.seq_in_seg // mins
        g = d.groupby(["sdate", "sess", "bucket"])
        agg = g.agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                    close=("close", "last"), dt=("dt", "first"), nbar=("close", "size")).reset_index()
        full = agg[agg.nbar == mins].reset_index(drop=True)
        # 桶内时间跨度检查: 应恰为 (mins-1) 分钟
        span_ok = True
        # 重新算 last dt
        agg2 = g.agg(dt_last=("dt", "last")).reset_index()
        full = full.merge(agg2, on=["sdate", "sess", "bucket"])
        span = (full.dt_last - full.dt).dt.total_seconds().values / 60.0
        n_bad_span = int((span != mins - 1).sum())
        # 相邻 bar: 时间差恰为 mins 分钟
        bdt = full.dt.values
        bgap = pd.Series(bdt).diff().dt.total_seconds().values
        adj_bar = np.zeros(len(full), bool); adj_bar[1:] = bgap[1:] == mins * 60.0
        # 编码+统计
        oo, hh, ll, cc = full.open.values, full.high.values, full.low.values, full.close.values
        code = encode_ohlc(oo, hh, ll, cc)
        bchg = (cc - oo) / oo * 100.0
        nxt = bchg[1:]; prev_code = code[:-1]
        row = {"tf": tf, "n_bars": int(len(full)), "n_bad_span": n_bad_span,
               "adj_share": float(adj_bar[1:].mean())}
        for s in ["010000", "000000", "000100", "010001"]:
            m = adj_bar[1:] & (prev_code == s)
            x = nxt[m]
            if len(x) > 30:
                row[f"st_{s}"] = {"n": int(len(x)), "mean": float(x.mean()), "t": t_of(x)}
            else:
                row[f"st_{s}"] = {"n": int(len(x)), "mean": None, "t": None}
        htf_rows.append(row)
        print(f"  [{tf}] bars={len(full):,} 桶内时间span异常={n_bad_span} 相邻bar占比={row['adj_share']:.3f}")
        for s in ["010000", "000000", "000100", "010001"]:
            v = row[f"st_{s}"]
            if v["mean"] is not None:
                print(f"    {s}: n={v['n']:,} mean={v['mean']:+.4f}% t={v['t']:+.1f}")
            else:
                print(f"    {s}: 样本不足 n={v['n']}")
    RES["htf_fix"][sym] = htf_rows

    # ---- V10 CLV vs 形态码 增量检验 (1m, 严格相邻) ----
    adj = np.zeros(len(d), bool); adj[1:] = gap[1:] == 60.0
    d["adj"] = adj
    clv = np.where(h > l, (c - l) / np.where(h > l, h - l, 1), 0.5)
    code = d.code.values
    nxt_chg = chg[1:][adj[1:]]
    prev_clv = clv[:-1][adj[1:]]
    prev_code = code[:-1][adj[1:]]
    prev_body_up = (prev_code[:2] == "01")
    # R²口径: 分组解释的方差比例
    def r2_of(groups):
        grand = nxt_chg.mean()
        ss_tot = ((nxt_chg - grand) ** 2).sum()
        ss_res = 0.0
        for g in groups:
            ss_res += ((nxt_chg[g] - nxt_chg[g].mean()) ** 2).sum()
        return 1 - ss_res / ss_tot
    # 1) 按 CLV 20 箱
    clv_bin = np.clip((prev_clv * 20).astype(int), 0, 19)
    g_clv = [clv_bin == i for i in range(20)]
    r2_clv = r2_of(g_clv)
    # 2) 按形态码 8 组
    g_code = [prev_code == s for s in STATES]
    r2_code = r2_of(g_code)
    # 3) 按 CLV箱×形态 (160组)
    g_both = [(clv_bin == i) & (prev_code == s) for i in range(20) for s in STATES]
    g_both = [g for g in g_both if g.sum() > 50]
    r2_both = r2_of(g_both)
    # 4) CLV 极端档内的形态对比 (控制CLV后影线字段的增量)
    inc = {}
    for name, m in [("CLV=1(恰好收极值)", prev_clv >= 0.999), ("CLV0.95-1", (prev_clv >= 0.95) & (prev_clv < 0.999)),
                    ("CLV0-0.05", prev_clv <= 0.05), ("CLV0-0.5(下半部)", prev_clv <= 0.5)]:
        row = {}
        for s in STATES:
            mm = m & (prev_code == s)
            if mm.sum() > 100:
                row[s] = {"n": int(mm.sum()), "mean": float(nxt_chg[mm].mean()), "t": t_of(nxt_chg[mm])}
        inc[name] = row
    # 5) 回归: next_chg ~ CLV + 形态哑变量 (F检验增量)
    X1 = np.column_stack([np.ones(len(prev_clv)), prev_clv])
    beta1, res1, _, _ = np.linalg.lstsq(X1, nxt_chg, rcond=None)
    rss1 = ((nxt_chg - X1 @ beta1) ** 2).sum()
    D = np.zeros((len(prev_code), 7))
    for j, s in enumerate(STATES[1:]):
        D[:, j] = (prev_code == s).astype(float)
    X2 = np.column_stack([np.ones(len(prev_clv)), prev_clv, D])
    beta2, _, _, _ = np.linalg.lstsq(X2, nxt_chg, rcond=None)
    rss2 = ((nxt_chg - X2 @ beta2) ** 2).sum()
    df1, df2 = 7, len(nxt_chg) - 9
    F = ((rss1 - rss2) / df1) / (rss2 / df2)
    p_F = float(1 - stats.f.cdf(F, df1, df2))
    # 6) 单因子 spearman IC
    ic_clv = float(stats.spearmanr(prev_clv, nxt_chg).statistic)
    # 形态码均值秩作为预测: 用各形态均值构造预测值
    code_mean = {s: float(nxt_chg[prev_code == s].mean()) for s in STATES}
    pred_code = np.array([code_mean[s] for s in prev_code])
    ic_code = float(stats.spearmanr(pred_code, nxt_chg).statistic)
    ic_abs_clv = float(stats.spearmanr(np.abs(prev_clv - 0.5), np.abs(nxt_chg)).statistic)
    RES["clv_vs_code"][sym] = {"r2_clv20": float(r2_clv), "r2_code8": float(r2_code), "r2_both": float(r2_both),
                               "F_incremental": float(F), "p_F": p_F,
                               "ic_clv": ic_clv, "ic_code": ic_code, "ic_absclv_vs_absnext": ic_abs_clv,
                               "increment": {k: {s: v for s, v in v0.items()} for k, v0 in inc.items()}}
    print(f"  [CLV vs 形态码] R²(CLV20箱)={r2_clv:.4f}  R²(形态8组)={r2_code:.4f}  R²(CLV×形态)={r2_both:.4f}")
    print(f"    增量F检验(控制CLV后形态哑变量): F={F:.1f} p={p_F:.2e}")
    print(f"    IC: spearman(CLV,next)={ic_clv:+.4f}  spearman(形态均值预测,next)={ic_code:+.4f}  |CLV-0.5| vs |next|={ic_abs_clv:+.4f}")
    for k, v0 in inc.items():
        print(f"    {k}: " + " ".join(f"{s}:{v['mean']:+.4f}(t{v['t']:+.0f})" for s, v in v0.items()))

json.dump(RES, open(f"{OUT}/results_verify3.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("\nsaved results_verify3.json")
