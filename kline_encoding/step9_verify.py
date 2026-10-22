# -*- coding: utf-8 -*-
"""Step 9: 深度证伪验证（2020-2025 全样本）
V1 三种相邻口径对比: naive(step3) / sess(step7) / gap60s(严格)
V2 数据完整性 + 换月断层检测
V3 按日聚类标准误 + 逐日符号一致率
V4 贡献度集中度 + 事件剔除
V5 收益分解: 下一根 open 跳空 vs 日内部分 (真实信号 vs 机械回归)
"""
import pandas as pd, numpy as np, json
from scipy import stats

OUT = "E:/抖音分析/2026-10-16-19-50-44/kline_encoding"
STATES = ["000000", "000001", "000100", "000101", "010000", "010001", "010100", "010101"]
CORE = ["010000", "000000", "000100", "010001", "010100", "000001", "000101", "010101"]

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
    d = pd.concat(frames, ignore_index=True).sort_values("datetime").reset_index(drop=True)
    return d

def prep(d):
    d = d.sort_values("datetime").reset_index(drop=True)
    dt = d.datetime
    o, h, l, c = d.open.values, d.high.values, d.low.values, d.close.values
    d["dt"] = dt; d["o"], d["h"], d["l"], d["c"] = o, h, l, c
    d["code"] = encode_ohlc(o, h, l, c)
    d["chg"] = (c - o) / o * 100.0
    d["up"] = c > o; d["dj"] = c == o
    d["oi"] = d["close_interest"].values.astype(float)
    keys = [sess_key(t) for t in dt]
    d["sdate"] = [k[0] for k in keys]; d["sess"] = [k[1] for k in keys]
    tkey = np.array([k[0].value * 10 + k[1] for k in keys])
    gap = dt.diff().dt.total_seconds().values
    d["gap"] = gap
    n = len(d)
    adj_naive = np.ones(n, bool); adj_naive[0] = False                      # step3 口径: 全部相邻
    adj_sess = np.zeros(n, bool); adj_sess[1:] = tkey[1:] == tkey[:-1]      # step7 口径
    adj_60 = np.zeros(n, bool); adj_60[1:] = gap[1:] == 60.0                # 严格: 仅真连续分钟
    d["adj_naive"], d["adj_sess"], d["adj_60"] = adj_naive, adj_sess, adj_60
    d["day"] = dt.dt.date.values
    d["year"] = dt.dt.year.values
    return d

def sig_stats(d, mask_col, state):
    """mask_col: 相邻口径列(标记'下一根'可用)。返回该 state 的下一根 chg 样本"""
    m_prev = (d.code.values == state)
    adj = d[mask_col].values[1:]          # True = 这根有合法的'下一根'
    prev = m_prev[:-1]
    sel = adj & prev
    x = d.chg.values[1:][sel]
    return x

def cluster_stats(x, day_sel):
    """按日聚类: 每天一个观测"""
    dfd = pd.DataFrame({"x": x, "day": day_sel})
    g = dfd.groupby("day")["x"].mean()
    nd = len(g)
    t_cl = g.mean() / (g.std(ddof=1) / np.sqrt(nd))
    neg = float((g < 0).mean()) if g.mean() < 0 else float((g > 0).mean())
    sign_cons = float((np.sign(g) == np.sign(g.mean())).mean())
    p_binom = float(stats.binomtest(int((np.sign(g) == np.sign(g.mean())).sum()), nd, 0.5).pvalue)
    return {"n": int(len(x)), "mean": float(x.mean()), "med": float(np.median(x)),
            "t_naive": float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))),
            "win": float((x > 0).mean()),
            "n_days": int(nd), "daily_mean": float(g.mean()), "daily_std": float(g.std(ddof=1)),
            "t_cluster": float(t_cl), "day_sign_cons": float(sign_cons), "binom_p": p_binom}

def decomp(d, state):
    """收益分解: open_jump = open_{t+1}/close_t - 1; intra = close_{t+1}/open_{t+1} - 1"""
    o, c = d.o.values, d.c.values
    adj = d["adj_60"].values[1:]
    prev = (d.code.values[:-1] == state)
    sel = adj & prev
    ct = c[:-1][sel]; ot1 = o[1:][sel]; ct1 = c[1:][sel]
    oj = (ot1 - ct) / ct * 100.0
    intra = (ct1 - ot1) / ot1 * 100.0
    tot = (ct1 - ct) / ct * 100.0
    def st(a):
        return {"mean": float(a.mean()), "t": float(a.mean() / (a.std(ddof=1) / np.sqrt(len(a)))), "n": int(len(a))}
    return {"open_jump": st(oj), "intra": st(intra), "total": st(tot)}

RES = {"per_sym": {}, "compare_adj": {}, "roll_days": {}, "decomp": {}}
for sym in ["MA", "EG"]:
    print(f"\n{'='*30} {sym} {'='*30}")
    d = prep(load_all(sym))
    print(f"  n={len(d):,}  {d.datetime.min()} -> {d.datetime.max()}")

    # ---- V2 数据完整性 ----
    dup = int(d.datetime.duplicated().sum())
    bad_ohlc = int(((d.h < d.o) | (d.h < d.c) | (d.l > d.o) | (d.l > d.c) | (d.h < d.l)).sum())
    outside = int(((d.sess == -1)).sum())
    gap_vc = pd.Series(d.gap[1:]).value_counts().head(5)
    RES["per_sym"].setdefault(sym, {})["integrity"] = {
        "dup_ts": dup, "bad_ohlc": bad_ohlc, "outside_session": outside,
        "gap_top": {str(k): int(v) for k, v in gap_vc.items()},
        "gap60_share": float((d.gap[1:] == 60).mean())}
    print(f"  [完整性] 重复时间戳={dup} OHLC逻辑违例={bad_ohlc} 会话外K线={outside} gap=60s占比={RES['per_sym'][sym]['integrity']['gap60_share']:.3f}")

    # ---- V2 换月/断层检测: 日级收益 + 会话首根跳空 + OI跳变 ----
    daily = d.groupby("day").agg(o=("o", "first"), c=("c", "last"), oi=("oi", "last"))
    daily["ret"] = daily.c.pct_change() * 100
    daily["oi_chg"] = daily.oi.pct_change() * 100
    big_days = daily[daily.ret.abs() > 2.0]
    oi_jump = daily[daily.oi_chg.abs() > 40]
    # 会话首根跳空 (相对上一会话收盘)
    first_mask = np.zeros(len(d), bool); first_mask[0] = True
    first_mask[1:] = d.gap.values[1:] > 1200
    prev_c = np.append(d.c.values[0], d.c.values[:-1])
    d["gap_open"] = np.where(first_mask, (d.o.values - prev_c) / prev_c * 100.0, 0.0)
    big_gap = d[d.gap_open.abs() > 1.0]
    # 跳空日期 vs OI跳变日期 交集 = 疑似换月断层
    gap_dates = set(str(x.date()) for x in big_gap.datetime)
    oi_dates = set(str(x) for x in oi_jump.index)
    overlap = sorted(gap_dates & oi_dates)
    # 极端分钟K线
    ex_min = d[d.chg.abs() > 1.0]
    RES["per_sym"][sym]["roll"] = {
        "n_days_big_ret": int(len(big_days)),
        "big_ret_dates": [str(x) for x in big_days.index][:20],
        "big_ret_vals": [round(float(v), 2) for v in big_days.ret.values[:20]],
        "n_oi_jump": int(len(oi_jump)), "oi_jump_dates": [str(x) for x in oi_jump.index][:20],
        "oi_jump_vals": [round(float(v), 1) for v in oi_jump.oi_chg.values[:20]],
        "overlap_gap_oi": overlap,
        "n_sess_gap_gt1": int(len(big_gap)),
        "sess_gap_dates": [str(x.date()) for x in big_gap.datetime][:25],
        "sess_gap_vals": [round(float(v), 2) for v in big_gap.gap_open.values[:25]],
        "n_extreme_min": int(len(ex_min)),
        "extreme_min_dates": [str(x) for x in ex_min.datetime][:20]}
    print(f"  [断层] 日收益|>2%|天数={len(big_days)}/{len(daily)} | 会话首根跳空|>1%|={len(big_gap)} | OI日跳变|>40%|={len(oi_jump)}")
    print(f"    OI跳变日期: {[f'{x}:{v:+.0f}%' for x, v in zip([str(i) for i in oi_jump.index][:12], oi_jump.oi_chg.values[:12])]}")
    print(f"    跳空∩OI跳变(疑似换月断层): {overlap if overlap else '无'}")
    print(f"    分钟级|chg|>1%的K线: {len(ex_min)}根")

    # ---- V1 三种相邻口径对比 (+剔除极端分钟K线) ----
    d["adj_ex"] = d["adj_60"] & (np.abs(d.chg.values) <= 1.0) & (np.abs(np.append(d.chg.values[0], d.chg.values[:-1])) <= 1.0)
    cmp_rows = {}
    for state in CORE:
        row = {}
        for col, name in [("adj_naive", "naive(step3)"), ("adj_sess", "sess(step7)"), ("adj_60", "gap60s(严格)"), ("adj_ex", "gap60s+剔极端")]:
            x = sig_stats(d, col, state)
            if len(x) > 30:
                row[name] = {"n": int(len(x)), "mean": float(x.mean()),
                             "t": float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))),
                             "win": float((x > 0).mean())}
            else:
                row[name] = {"n": int(len(x)), "mean": None, "t": None, "win": None}
        cmp_rows[state] = row
    RES["compare_adj"][sym] = cmp_rows
    print("  [口径对比] state: n / mean% / t  (naive -> sess -> gap60s -> 剔极端)")
    for s in CORE:
        r = cmp_rows[s]
        cells = []
        for k in ["naive(step3)", "sess(step7)", "gap60s(严格)", "gap60s+剔极端"]:
            v = r[k]
            cells.append(f"{v['n']:>6}/{v['mean']:+.4f}/{v['t']:+.1f}" if v["mean"] is not None else f"{v['n']:>6}/  -  /  -")
        print(f"    {s}: " + " | ".join(cells))

    # ---- V3 聚类标准误 (用 gap60s 口径) ----
    cl = {}
    for state in CORE:
        x = sig_stats(d, "adj_60", state)
        if len(x) > 30:
            # 对应的 day
            adj = d["adj_60"].values[1:]; prev = (d.code.values[:-1] == state)
            days = d.day[1:][adj & prev]
            cl[state] = cluster_stats(x, days)
    RES["per_sym"][sym]["cluster"] = cl
    print("  [按日聚类] state: n / mean% / t_naive / n_days / t_cluster / 逐日符号一致率 / binom_p")
    for s in CORE:
        if s in cl:
            v = cl[s]
            print(f"    {s}: {v['n']:>7} / {v['mean']:+.4f} / {v['t_naive']:+.1f} / {v['n_days']} / {v['t_cluster']:+.2f} / {v['day_sign_cons']:.1%} / {v['binom_p']:.2e}")

    # ---- V4 贡献度集中度 (gap60s 口径, 按信号收益绝对值排序) ----
    conc = {}
    for state in CORE:
        x = sig_stats(d, "adj_60", state)
        if len(x) < 100: continue
        srt = np.sort(np.abs(x))[::-1]
        tot = srt.sum()
        conc[state] = {"n": int(len(x)),
                       "top0.1%": float(srt[:max(1, len(x)//1000)].sum() / tot),
                       "top1%": float(srt[:max(1, len(x)//100)].sum() / tot),
                       "top5%": float(srt[:max(1, len(x)//20)].sum() / tot)}
    RES["per_sym"][sym]["concentration"] = conc
    print("  [贡献度集中度] top0.1% / top1% / top5% 样本占总|收益|比例")
    for s, v in conc.items():
        print(f"    {s}: {v['top0.1%']:.1%} / {v['top1%']:.1%} / {v['top5%']:.1%}")

    # ---- V5 收益分解 ----
    dc = {s: decomp(d, s) for s in CORE}
    RES["decomp"][sym] = dc
    print("  [收益分解] state: 下一根 open跳空 mean(t) | 日内 mean(t) | 合计 mean(t)")
    for s in CORE:
        v = dc[s]
        print(f"    {s}: jump {v['open_jump']['mean']:+.4f}%({v['open_jump']['t']:+.1f}) | intra {v['intra']['mean']:+.4f}%({v['intra']['t']:+.1f}) | total {v['total']['mean']:+.4f}%({v['total']['t']:+.1f})")

json.dump(RES, open(f"{OUT}/results_verify.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("\nsaved results_verify.json")
