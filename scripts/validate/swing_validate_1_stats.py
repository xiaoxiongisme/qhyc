"""
Step 1-3: 波段幅度统计的"可预测性"判决实验
=================================================
用户假说：K线由波段(低点-高)相连，"每涨100点回调40-50点"、"振幅约100点"这类
统计结构，可以用来预判当前点位的前瞻空间（不考虑历史高低位）。

判决三问：
  Q1 波段幅度有多大？跨品种/跨年份是否稳定（可用作"幅度基准"）？
  Q2 幅度是否可预测？下一段幅度 与 当前段幅度 有没有相关？
     -> 若自相关≈0，则"用幅度推测下一段"无信息（这是随机游走的数学性质）
  Q3 条件化是否增信息？给定"当前处于上涨趋势"时，回调幅度分布是否与
     震荡市/下跌市不同？且这个差异是否大于零假设（打乱序列的对照）？

因果性纪律（防前视）：
  - ZigZag 用【对数收益】的极值检测，拐点确认必须滞后（用未来 N% 幅度
    确认），确认日之后的信号才可交易 -> 记录 confirm_idx
  - 所有统计量按"确认日"归属，不按"极值日"归属
  - 不使用"当日高点"作为决策依据的当日即交易

数据：fut_kline daily/cont_adj（等差后复权，保留真实点数差）
     区间 2015-01-05 ~ 2026-09-02，81 品种
"""
import os
import sys
import numpy as np
import pandas as pd
import psycopg2

PG = dict(host="127.0.0.1", port=5432, user="futures",
          password=os.environ["POSTGRES_PASSWORD"], dbname="futures")

OUT = r"E:\Docker\qhyc\docs\_swing_out"
os.makedirs(OUT, exist_ok=True)


# ---------------------------------------------------------------- data
def load_daily():
    """
    取 daily 日线。口径裁决（实测 2026-10-02）：
      cont_adj（等差后复权）  -> 84 品种中 16 品种出现负价格（I888 低至 -1071.5，
          EC888 -2051.8，2015-2020 期间），累计 5765 根 <=0。属等差累积崩坏，
          点数差无意义 -> 弃用。
      continuous（前复权）   -> 仅 9 根 low=0，且 open/high/close 正常，
          系单日夜盘最低价缺失 -> 外科修复 low=min(open,close)。
    => 用 continuous + 修复。用户"100 点"表述在点数上成立（前复权对
       同一品种相邻 bar 的相对幅度几乎无扭曲），跨品种比较一律用百分比。
    """
    sql = """
        SELECT symbol, trade_datetime::date AS d,
               high::float8, low::float8, close::float8
        FROM fut_kline
        WHERE freq='daily' AND kind='continuous'
          AND trade_datetime >= '2015-01-01' AND trade_datetime < '2025-12-31'
        ORDER BY symbol, trade_datetime
    """
    with psycopg2.connect(**PG) as cn:
        df = pd.read_sql(sql, cn, parse_dates=["d"])

    # 外科修复：low<=0 -> min(open, close)
    bad = (df.low <= 0)
    n_bad = int(bad.sum())
    if n_bad:
        df.loc[bad, "low"] = df.loc[bad, ["close"]].min(axis=1)
        print(f"[fix] low<=0 外科修复 {n_bad} 根（night low 缺失）", flush=True)
    # 仍为非正的整根丢弃（本次应为 0）
    still = int((df.low <= 0).sum())
    if still:
        print(f"[drop] 仍非正 {still} 根 -> 丢弃", flush=True)
        df = df[df.low > 0].reset_index(drop=True)
    return df


# ---------------------------------------------------------------- ZigZag
def zigzag(high, low, thr_pct):
    """
    因果 ZigZag（百分比阈值 thr_pct，如 0.10 = 10%）。
    返回 pivot 列表 [(extreme_idx, pivot_price, direction, confirm_idx)]
      direction: +1 = 该极值是高点(向下转折确认)，-1 = 该极值是低点(向上转折确认)
      confirm_idx: 拐点被【确认】的 bar 索引（>= extreme_idx，滞后）
    规则：追踪当前趋势方向的极值；反向超过 thr_pct 才确认转折。
    """
    n = len(high)
    if n < 10:
        return []
    piv = []
    # 初始方向未知：先找第一个显著移动
    trend = 0
    ext_i = 0
    ext_p = high[0] if high[0] >= low[0] else low[0]
    ext_is_high = high[0] >= low[0]
    start_i = 0
    for i in range(1, n):
        if trend >= 0:  # 正在找高点 / 或未定
            if high[i] > ext_p and ext_is_high:
                ext_p, ext_i = high[i], i
            elif not ext_is_high:
                # 还没定方向，继续
                if low[i] < ext_p:
                    if ext_p / low[i] - 1 >= thr_pct:
                        # 从低起点确认上涨
                        trend = 1
                        ext_is_high = True
                        piv.append((start_i, ext_p, -1, i))
                        start_i = i
                        ext_p, ext_i = high[i], i
                else:
                    if high[i] > ext_p:
                        ext_p, ext_i = high[i], i
            else:  # ext_is_high and trend>=0
                if ext_p / low[i] - 1 >= thr_pct:
                    # 确认顶部
                    piv.append((ext_i, ext_p, +1, i))
                    trend = -1
                    start_i = i
                    ext_is_high = False
                    ext_p, ext_i = low[i], i
        if trend <= 0:  # 正在找低点
            if low[i] < ext_p and not ext_is_high:
                ext_p, ext_i = low[i], i
            elif ext_is_high:
                if high[i] / ext_p - 1 >= thr_pct:
                    trend = -1
                    ext_is_high = False
                    piv.append((start_i, ext_p, +1, i))
                    start_i = i
                    ext_p, ext_i = low[i], i
            else:
                if high[i] > ext_p:
                    if high[i] / ext_p - 1 >= thr_pct:
                        trend = 1
                        ext_is_high = True
                        piv.append((start_i, ext_p, -1, i))
                        start_i = i
                        ext_p, ext_i = high[i], i
                else:
                    ext_p, ext_i = low[i], i
    return piv


def legs_from_pivots(piv, close):
    """
    由 pivot 序列构造"波段腿"（swing legs）。
    每条腿 = (start_pivot, end_pivot)，幅度 = |价差|，方向 = 涨/跌。
    可用性：腿的【终点确认日】= end.confirm_idx -> 之后才知道这腿结束。
    """
    out = []
    for k in range(len(piv) - 1):
        (_, p0, d0, _), (i1, p1, d1, c1) = piv[k], piv[k + 1]
        out.append(dict(
            start_i=i1, end_i=i1, start_p=p1,
            # 幅度以点差计（cont_adj 保留点数差）
            amp_pts=abs(p1 - p0),
            amp_pct=abs(p1 - p0) / max(p0, 1e-9),
            up=(p1 > p0),
            confirm_i=c1,
        ))
    return out


# ---------------------------------------------------------------- main
def main():
    df = load_daily()
    print(f"[data] daily/cont_adj rows={len(df)} syms={df.symbol.nunique()} "
          f"{df.d.min().date()}~{df.d.max().date()}", flush=True)

    THR_LIST = [0.08, 0.12]   # 8% / 12% 两档，只 2 个参数档，避免过拟合
    recs = []
    sym_summary = []

    for sym, g in df.groupby("symbol", sort=True):
        g = g.reset_index(drop=True)
        if len(g) < 250:      # 约 1 年
            continue
        h = g.high.values
        l = g.low.values
        c = g.close.values
        yr = g.d.dt.year.values
        for thr in THR_LIST:
            piv = zigzag(h, l, thr)
            legs = legs_from_pivots(piv, c)
            if len(legs) < 8:
                continue
            # 归一化：点差 / 该品种日均绝对收益（让跨品种可比）
            r = np.diff(np.log(c))
            scale = np.median(np.abs(r)) * 1e4  # bp 量级
            for lg in legs:
                recs.append(dict(
                    sym=sym, thr=thr, up=lg["up"],
                    amp_pts=lg["amp_pts"],
                    amp_pct=lg["amp_pct"] * 100.0,   # 百分比，跨品种可比
                    yr=int(yr[lg["start_i"]]) if lg["start_i"] < len(yr) else 0,
                    confirm_i=lg["confirm_i"],
                ))
            sym_summary.append(dict(sym=sym, thr=thr, n_legs=len(legs)))

    L = pd.DataFrame(recs)
    L.to_csv(os.path.join(OUT, "legs.csv"), index=False)
    S = pd.DataFrame(sym_summary)
    S.to_csv(os.path.join(OUT, "sym_legs_count.csv"), index=False)
    print(f"[legs] total legs={len(L)}  symbols={L.sym.nunique()}", flush=True)
    print(L.groupby("thr").size().to_string(), flush=True)

    # ---- Q1: 幅度分布（跨品种/跨年稳定性）
    q1 = L.groupby(["thr", "up"]).amp_pct.agg(
        ["count", "median", "mean", "std", lambda s: s.quantile(.25), lambda s: s.quantile(.75)]
    )
    q1.columns = ["n", "median", "mean", "std", "p25", "p75"]
    q1["cv"] = q1["std"] / q1["mean"]
    print("\n=== Q1 波段幅度分布（百分比，跨品种可比）===")
    print(q1.to_string())

    # 跨品种中位数的离散度（稳定性）：log 空间标准差
    q1b = L.groupby(["thr", "sym"]).amp_pct.median().reset_index()
    print("\n--- 跨品种中位幅度离散（log 空间）---")
    for thr, g in q1b.groupby("thr"):
        lg = np.log(g.amp_pct[g.amp_pct > 0])
        print(f"thr={thr}: 品种数={len(lg)} log标准差={lg.std():.3f} "
              f"→ 品种间中位数极差 {np.exp(lg.max()-lg.min()):.1f} 倍")

    # 跨年份中位数漂移
    print("\n--- 跨年份中位幅度(%)（漂移检验）---")
    q1c = L.groupby(["thr", "yr"]).amp_pct.median().unstack(0)
    print(q1c.tail(12).to_string())

    # ---- Q2: 幅度可预测性？滞后自相关（幅度序列）
    print("\n=== Q2 幅度序列自相关（可预测性的必要条件）===")
    rows = []
    for thr, g in L.groupby("thr"):
        acf_rows = []
        for sym, gg in g.sort_values(["sym", "confirm_i"]).groupby("sym"):
            a = gg.amp_pct.values
            if len(a) < 20:
                continue
            a = a - a.mean()
            denom = np.dot(a, a)
            if denom <= 0:
                continue
            r1 = np.dot(a[:-1], a[1:]) / denom
            r2 = np.dot(a[:-2], a[2:]) / denom if len(a) > 2 else np.nan
            r3 = np.dot(a[:-3], a[3:]) / denom if len(a) > 3 else np.nan
            acf_rows.append((r1, r2, r3))
        A = np.array(acf_rows)
        acf_rows_txt = (f"thr={thr}: 品种数={len(A)} "
                        f"acf1={np.nanmean(A[:,0]):+.3f} (sd {np.nanstd(A[:,0]):.3f}) "
                        f"acf2={np.nanmean(A[:,1]):+.3f} acf3={np.nanmean(A[:,2]):+.3f}")
        print(acf_rows_txt)
        # t 检验：acf1 均值是否显著异于 0
        t = np.nanmean(A[:, 0]) / (np.nanstd(A[:, 0]) / np.sqrt(len(A)))
        rows.append(dict(thr=thr, n=len(A), acf1=np.nanmean(A[:, 0]),
                         t=t))
    pd.DataFrame(rows).to_csv(os.path.join(OUT, "acf.csv"), index=False)

    # ---- Q3: 条件化增益（上涨趋势中回调幅度是否特殊）
    # 趋势状态定义（因果）：用均线 + 斜率，不看点位高低
    print("\n=== Q3 条件化：趋势状态 x 回调幅度 ===")
    q3rows = []
    for thr, g in L.groupby("thr"):
        # 需要 close 序列来定状态 -> 重算
        pass
    # 单独算：每品种的 swing leg + 该腿终点的趋势状态
    state_recs = []
    for sym, g in df.groupby("symbol", sort=True):
        g = g.reset_index(drop=True)
        if len(g) < 250:
            continue
        h, l, c = g.high.values, g.low.values, g.close.values
        ma20 = pd.Series(c).rolling(20).mean().values
        for thr in THR_LIST:
            piv = zigzag(h, l, thr)
            for k in range(len(piv) - 1):
                (i0, p0, d0, _), (i1, p1, d1, c1) = piv[k], piv[k + 1]
                up = p1 > p0
                ma = ma20[c1] if not np.isnan(ma20[c1]) else np.nan
                if np.isnan(ma):
                    continue
                state = "up" if c1 > ma else "down"      # 拐点确认时价格在均线上方=上涨趋势
                state_recs.append(dict(sym=sym, thr=thr, up=up, state=state,
                                       amp=abs(p1 - p0),
                                       amp_pct=abs(p1 - p0) / max(abs(p0), 1e-9) * 100.0,
                                       confirm_i=c1,
                                       ma=ma, price=p1))
    T = pd.DataFrame(state_recs)
    T.to_csv(os.path.join(OUT, "legs_state.csv"), index=False)

    print("\n--- 下跌腿(回调)幅度 按趋势状态分组（合并 thr）---")
    dn = T[~T.up]
    g1 = dn.groupby("state").amp_pct.agg(["count", "median", "std"])
    g1["cv"] = g1["std"] / g1["median"]
    print(g1.to_string())

    print("\n--- 上涨腿幅度 按趋势状态分组 ---")
    up_leg = T[T.up]
    g2 = up_leg.groupby("state").amp_pct.agg(["count", "median", "std"])
    g2["cv"] = g2["std"] / g2["median"]
    print(g2.to_string())

    # 零假设对照：打乱 amp 序列（保留 n、保留状态边际），检验状态间差异显著性
    print("\n--- 零假设对照（打乱幅度标签，1000 次 bootstrap）---")
    rng = np.random.default_rng(20261002)
    obs = dn.groupby("state").amp_pct.median()
    if len(obs) >= 2:
        diff_obs = obs.max() - obs.min()
        pool = dn.amp_pct.values
        stat = dn.state.values
        null = []
        for _ in range(1000):
            perm = rng.permutation(len(pool))
            fake = pd.Series(pool[perm]).groupby(pd.Series(stat)).median()
            null.append(fake.max() - fake.min())
        null = np.array(null)
        p = float((null >= diff_obs).mean())
        print(f"观测 最大组间中位差 = {diff_obs:.3f} 个百分点；"
              f"零假设均值 {null.mean():.3f}；p = {p:.3f}")
        q3rows.append(dict(obs_diff=diff_obs, null_mean=null.mean(), p=p))

    print("\n[done] 输出目录:", OUT)
    if q3rows:
        print(pd.DataFrame(q3rows).to_string())


if __name__ == "__main__":
    main()
