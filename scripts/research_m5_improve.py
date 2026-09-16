"""M5 改进方向实证研究（只读）

目的：把 CodeBuddy 提出的 4 条改进建议从"听起来合理"变成"有数据支撑/反驳"，
并额外探测几个尚未入模的候选因子的信息含量。

全部只读，不写任何业务表。输出 Markdown 报告到 /app/logs/。

分析块：
  A. 现状基线（最新整批 run 的逐模型 dir_acc + Wilson CI + 二项检验）
  B. 建议2 成员门槛：离线重构集成（等权/概率加权/walk-forward 门槛筛选）
  C. 建议3 状态分层：model×state 分层准确率 + BH-FDR 多重检验校正 + 状态化集成
  D. 补充：选择性预测（按置信度/幅度分层的准确率 + 覆盖率-准确率曲线）
  E. 建议1 预测周期：长历史 H=1/3/5/10 方向可预测性 + 变量比检验（含小时线）
  F. 建议4 特征正交化：传导特征接入现状核查 + 候选新因子信息含量
     F1 持仓量（oi）：量仓配合方向因子
     F2 期限结构（carry）：近远月斜率因子
     F3 截面动量：Spearman IC + t 检验（相对强弱 vs 绝对方向）
  G. 数据就绪度盘点
"""
from __future__ import annotations

import sys
import math
import datetime as dt
from collections import defaultdict

sys.path.insert(0, "/app")

import numpy as np
import pandas as pd
from scipy import stats
from sqlalchemy import text

from app.core.db import session_scope

TS = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
OUT = f"/app/logs/research_m5_{TS}.md"
LINES: list[str] = []


def w(s: str = "") -> None:
    LINES.append(s)
    print(s)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


def binom_p(k: int, n: int, p0: float = 0.5) -> float:
    if n == 0:
        return float("nan")
    return float(stats.binomtest(k, n, p0, alternative="two-sided").pvalue)


def bh_fdr(pvals: list[float], alpha: float = 0.05) -> list[bool]:
    """Benjamini-Hochberg，返回是否通过 FDR 控制"""
    idx = [i for i, p in enumerate(pvals) if not math.isnan(p)]
    m = len(idx)
    out = [False] * len(pvals)
    if m == 0:
        return out
    order = sorted(idx, key=lambda i: pvals[i])
    thresh = 0
    for rank, i in enumerate(order, start=1):
        if pvals[i] <= alpha * rank / m:
            thresh = rank
    for rank, i in enumerate(order, start=1):
        if rank <= thresh:
            out[i] = True
    return out


w(f"# M5 改进方向实证研究 — {TS}")
w()
w("> 全部只读查询。目标：验证/反驳 CodeBuddy 的 4 条改进建议，并量化候选新因子的信息含量。")
w()

with session_scope() as s:
    # ---------------- 选定基准 run ----------------
    runs = s.execute(text(
        "SELECT run_id, count(DISTINCT symbol) ns, count(*) n, min(eval_date) d0, max(eval_date) d1 "
        "FROM backtest_detail GROUP BY run_id ORDER BY n DESC"
    )).all()
    RUN = runs[0].run_id
    for r in runs[:3]:
        if r.ns >= 70 and r.d1 == max(x.d1 for x in runs if x.ns >= 70):
            RUN = r.run_id
            break
    base = next(r for r in runs if r.run_id == RUN)
    w(f"**基准 run**：`{RUN}`　品种 {base.ns}　明细 {base.n} 行　评估期 {base.d0} → {base.d1}")
    w()

    det = pd.read_sql(text(
        "SELECT symbol, model, eval_date, state, pred_dir, prob, point, low, high, actual "
        "FROM backtest_detail WHERE run_id = :r"
    ), s.get_bind(), params={"r": RUN})

    for c in ("prob", "point", "low", "high", "actual"):
        det[c] = pd.to_numeric(det[c], errors="coerce")
    det["eval_date"] = pd.to_datetime(det["eval_date"])
    det["hit"] = ((det.pred_dir == "up") == (det.actual > 0)).astype(int)
    det = det[det.actual.notna()].copy()

    n_pts = det.groupby("symbol").eval_date.nunique()
    w(f"- 评估点：每品种中位 {int(n_pts.median())} 个（min {n_pts.min()} / max {n_pts.max()}），"
      f"共 {det.eval_date.nunique()} 个不同日期")
    w()

    # ---------------- A. 现状基线 ----------------
    w("## A. 现状基线：逐模型方向准确率")
    w()
    w("| 模型 | n | dir_acc | Wilson95 CI | binom p | 判定 |")
    w("|---|---|---|---|---|---|")
    base_acc = {}
    for m, g in det.groupby("model"):
        k, n = int(g.hit.sum()), len(g)
        acc = k / n
        lo, hi = wilson(k, n)
        p = binom_p(k, n)
        base_acc[m] = acc
        verdict = "显著≠随机" if p < 0.05 else "与随机无异"
        if p < 0.05 and acc < 0.5:
            verdict = "显著**低于**随机"
        w(f"| {m} | {n} | {acc:.4f} | [{lo:.4f}, {hi:.4f}] | {p:.3g} | {verdict} |")
    w()

    # ---------------- B. 建议2：成员门槛 ----------------
    w("## B. 建议2 实测 — 提高成员门槛（离线重构集成）")
    w()
    members = [m for m in det.model.unique() if m != "ensemble"]
    piv_dir = det[det.model != "ensemble"].pivot_table(
        index=["symbol", "eval_date"], columns="model", values="pred_dir", aggfunc="first")
    piv_prob = det[det.model != "ensemble"].pivot_table(
        index=["symbol", "eval_date"], columns="model", values="prob", aggfunc="first")
    act = det.groupby(["symbol", "eval_date"]).actual.first()
    piv_up = piv_dir.apply(lambda col: col.map({"up": 1.0, "down": 0.0}))
    y = (act.reindex(piv_up.index) > 0).astype(int)

    def score(pred_up: pd.Series, label: str, extra: str = "") -> dict:
        mask = pred_up.notna()
        k = int((pred_up[mask] == y[mask]).sum())
        n = int(mask.sum())
        acc = k / n if n else float("nan")
        lo, hi = wilson(k, n)
        p = binom_p(k, n)
        cov = n / len(pred_up)
        w(f"| {label} | {n} | {cov:.1%} | {acc:.4f} | [{lo:.4f}, {hi:.4f}] | {p:.3g} | {extra} |")
        return {"label": label, "n": n, "acc": acc, "lo": lo, "hi": hi, "p": p, "cov": cov}

    w("| 方案 | n | 覆盖率 | dir_acc | Wilson95 CI | binom p | 说明 |")
    w("|---|---|---|---|---|---|---|")
    res_b = []
    stored = det[det.model == "ensemble"].set_index(["symbol", "eval_date"])
    st_up = (stored.pred_dir == "up").astype(float).reindex(piv_up.index)
    res_b.append(score(st_up, "①落库 ensemble（现状）", "engine 当前输出"))

    maj = (piv_up.mean(axis=1) > 0.5).astype(float)
    maj[piv_up.mean(axis=1) == 0.5] = np.nan
    res_b.append(score(maj, "②等权多数票（全成员）", f"{len(members)} 个成员"))

    # 概率加权（模拟线上）
    conf = piv_prob.where(piv_up == 1, 1 - piv_prob)  # 每模型对"上涨"的支持度
    upw = (piv_prob.where(piv_up == 1, 1 - piv_prob) * piv_up).sum(axis=1, min_count=1)
    tot = conf.sum(axis=1, min_count=1)
    pw = (upw / tot > 0.5).astype(float)
    res_b.append(score(pw, "③概率加权投票（全成员）", "等权版线上公式"))

    # walk-forward 门槛：仅用严格早于 t 的历史 60 点
    K = 60
    rows_keep_pt, rows_keep_lb = [], []
    for sym, g in det[det.model != "ensemble"].groupby("symbol"):
        gp = g.pivot_table(index="eval_date", columns="model", values="pred_dir", aggfunc="first")
        gh = g.pivot_table(index="eval_date", columns="model", values="hit", aggfunc="first")
        gpr = g.pivot_table(index="eval_date", columns="model", values="prob", aggfunc="first")
        gp, gh, gpr = gp.sort_index(), gh.sort_index(), gpr.sort_index()
        gup = gp.apply(lambda c: c.map({"up": 1.0, "down": 0.0}))
        roll_k = gh.rolling(K, min_periods=20).sum().shift(1)
        roll_n = gh.notna().rolling(K, min_periods=20).sum().shift(1)
        acc_hist = roll_k / roll_n
        lb = pd.DataFrame(np.nan, index=gh.index, columns=gh.columns)
        for c in gh.columns:
            kk, nn = roll_k[c].to_numpy(), roll_n[c].to_numpy()
            vals = [wilson(int(a), int(b))[0] if (not np.isnan(a) and not np.isnan(b) and b >= 20)
                    else np.nan for a, b in zip(kk, nn)]
            lb[c] = vals
        for tag, gate, store in (("pt", acc_hist > 0.5, rows_keep_pt),
                                 ("lb", lb > 0.5, rows_keep_lb)):
            sel_up = gup.where(gate)
            m_ = sel_up.mean(axis=1)
            cnt = sel_up.notna().sum(axis=1)
            pred = pd.Series(np.where(cnt >= 1, np.where(m_ > 0.5, 1.0, np.where(m_ < 0.5, 0.0, np.nan)), np.nan),
                             index=gup.index)
            for d_, v_ in pred.items():
                store.append((sym, d_, v_, int(cnt.get(d_, 0))))

    for tag, store, lab in (("pt", rows_keep_pt, "④门槛：滚动60点 acc>0.5"),
                            ("lb", rows_keep_lb, "⑤门槛：滚动60点 CI下界>0.5")):
        df_ = pd.DataFrame(store, columns=["symbol", "eval_date", "pred", "n_members"]).set_index(["symbol", "eval_date"])
        pred = df_.pred.reindex(piv_up.index)
        avg_mem = df_.n_members.mean()
        res_b.append(score(pred, lab, f"平均入池成员 {avg_mem:.1f} 个"))
    w()

    b_best = max((r for r in res_b if r["n"] > 500), key=lambda r: r["acc"])
    w(f"- 最优方案：**{b_best['label']}** acc={b_best['acc']:.4f} "
      f"CI=[{b_best['lo']:.4f},{b_best['hi']:.4f}] p={b_best['p']:.3g}")
    w(f"- 现状 ensemble acc={res_b[0]['acc']:.4f}；门槛方案是否突破 50%："
      f"{'是' if b_best['lo'] > 0.5 else '**否**（CI 下界仍 ≤ 0.5）'}")
    w()

    # ---------------- C. 建议3：状态分层 ----------------
    w("## C. 建议3 实测 — 状态分层准确率（BH-FDR 多重检验校正）")
    w()
    cells = []
    for (m, st), g in det[det.model != "ensemble"].groupby(["model", "state"]):
        k, n = int(g.hit.sum()), len(g)
        if n < 30:
            continue
        lo, hi = wilson(k, n)
        cells.append({"model": m, "state": st, "n": n, "acc": k / n, "lo": lo, "hi": hi, "p": binom_p(k, n)})
    cdf = pd.DataFrame(cells)
    if not cdf.empty:
        flags = bh_fdr(cdf.p.tolist(), alpha=0.05)
        cdf["fdr_sig"] = flags
        cdf["bonf_sig"] = cdf.p < 0.05 / len(cdf)
        w(f"- 检验格子数：{len(cdf)}（model×state，n≥30）；Bonferroni 阈值 p<{0.05/len(cdf):.2e}")
        w()
        w("### C1. 显著格子（FDR 校正后）")
        sig = cdf[cdf.fdr_sig].sort_values("acc", ascending=False)
        if sig.empty:
            w("**无任何 model×state 格子通过 FDR 校正**——状态分层未发现可利用的稳定优势。")
        else:
            w("| 模型 | 状态 | n | acc | Wilson95 | p | Bonferroni |")
            w("|---|---|---|---|---|---|---|")
            for _, r in sig.iterrows():
                w(f"| {r.model} | {r.state} | {r.n} | {r.acc:.4f} | [{r.lo:.4f},{r.hi:.4f}] | "
                  f"{r.p:.3g} | {'✓' if r.bonf_sig else '—'} |")
        w()
        w("### C2. CodeBuddy 点名的两个格子")
        w("| 断言 | n | acc | Wilson95 | p | FDR 显著 |")
        w("|---|---|---|---|---|---|")
        for mm, ss, lab in (("markov", "mean_revert", "markov @ mean_revert"),
                            ("arima", "trend", "arima @ trend")):
            r = cdf[(cdf.model == mm) & (cdf.state == ss)]
            if r.empty:
                w(f"| {lab} | — | 无数据 | — | — | — |")
            else:
                r = r.iloc[0]
                w(f"| {lab} | {r.n} | {r.acc:.4f} | [{r.lo:.4f},{r.hi:.4f}] | {r.p:.3g} | "
                  f"{'✓' if r.fdr_sig else '✗'} |")
        w()
        w("### C3. 各状态最佳/最差格子（供参考，注意多重检验）")
        w("| 状态 | 总样本 | 最佳模型 | acc | 最差模型 | acc |")
        w("|---|---|---|---|---|---|")
        for st, g in cdf.groupby("state"):
            b_, wo = g.loc[g.acc.idxmax()], g.loc[g.acc.idxmin()]
            w(f"| {st} | {int(g.n.sum())} | {b_.model} | {b_.acc:.4f} | {wo.model} | {wo.acc:.4f} |")
        w()

    # 状态化集成（walk-forward）
    rows_state = []
    for sym, g in det[det.model != "ensemble"].groupby("symbol"):
        gp = g.pivot_table(index="eval_date", columns="model", values="pred_dir", aggfunc="first").sort_index()
        gh = g.pivot_table(index="eval_date", columns="model", values="hit", aggfunc="first").sort_index()
        stt = g.groupby("eval_date").state.first().sort_index()
        gup = gp.apply(lambda c: c.map({"up": 1.0, "down": 0.0}))
        dates = gup.index.tolist()
        for i, d_ in enumerate(dates):
            cur = stt.get(d_)
            hist = [dd for dd in dates[:i] if stt.get(dd) == cur]
            if len(hist) < 15:
                rows_state.append((sym, d_, np.nan, 0))
                continue
            h = gh.loc[hist]
            acc_s = h.mean()
            n_s = h.notna().sum()
            keep = acc_s[(acc_s > 0.5) & (n_s >= 15)].index.tolist()
            if not keep:
                rows_state.append((sym, d_, np.nan, 0))
                continue
            v = gup.loc[d_, keep].mean()
            rows_state.append((sym, d_, (1.0 if v > 0.5 else 0.0 if v < 0.5 else np.nan), len(keep)))
    sdf = pd.DataFrame(rows_state, columns=["symbol", "eval_date", "pred", "nk"]).set_index(["symbol", "eval_date"])
    w("### C4. 状态化集成（walk-forward，同状态历史≥15 点且 acc>0.5 才入池）")
    w()
    w("| 方案 | n | 覆盖率 | dir_acc | Wilson95 CI | binom p | 说明 |")
    w("|---|---|---|---|---|---|---|")
    r_state = score(sdf.pred.reindex(piv_up.index), "⑥状态化集成", f"平均入池 {sdf.nk.mean():.1f} 个")
    w()

    # ---------------- D. 选择性预测 ----------------
    w("## D. 补充方向实测 — 选择性预测（只在高置信时出信号）")
    w()
    ens = det[det.model == "ensemble"].copy()
    ens["conf_prob"] = (ens.prob - 0.5).abs()
    ens["absp"] = ens.point.abs()
    for col, lab in (("conf_prob", "|prob−0.5| 置信度"), ("absp", "|预测幅度%|")):
        try:
            ens["_q"] = pd.qcut(ens[col], 5, labels=False, duplicates="drop")
        except ValueError:
            continue
        w(f"### 按 {lab} 五分层")
        w()
        w("| 分层 | 区间 | n | dir_acc | Wilson95 CI | binom p |")
        w("|---|---|---|---|---|---|")
        for q, g in ens.groupby("_q"):
            k, n = int(g.hit.sum()), len(g)
            lo, hi = wilson(k, n)
            w(f"| Q{int(q)+1} | [{g[col].min():.3f}, {g[col].max():.3f}] | {n} | {k/n:.4f} | "
              f"[{lo:.4f},{hi:.4f}] | {binom_p(k,n):.3g} |")
        w()
    w("### 覆盖率-准确率曲线（按 |prob−0.5| 降序取前 X%）")
    w()
    w("| 覆盖率 | n | dir_acc | Wilson95 下界 | 是否>0.5 |")
    w("|---|---|---|---|---|")
    es = ens.sort_values("conf_prob", ascending=False)
    for cov in (0.05, 0.10, 0.20, 0.30, 0.50, 1.00):
        g = es.head(max(30, int(len(es) * cov)))
        k, n = int(g.hit.sum()), len(g)
        lo, _ = wilson(k, n)
        w(f"| {cov:.0%} | {n} | {k/n:.4f} | {lo:.4f} | {'✅' if lo > 0.5 else '—'} |")
    w()

    # ---------------- E. 建议1：预测周期 ----------------
    w("## E. 建议1 实测 — 拉长预测周期 / 换频率的可预测性")
    w()
    mc = pd.read_sql(text(
        "SELECT product, trade_date, adj_close FROM main_continuous ORDER BY product, trade_date"
    ), s.get_bind())
    mc["adj_close"] = pd.to_numeric(mc.adj_close, errors="coerce")
    mc["trade_date"] = pd.to_datetime(mc.trade_date)
    w(f"- 长历史样本：{mc['product'].nunique()} 品种 / {len(mc)} 根日线 "
      f"（{mc.trade_date.min().date()} → {mc.trade_date.max().date()}）")
    w()

    def horizon_table(frames: dict[str, pd.Series], title: str, horizons=(1, 3, 5, 10, 20)) -> None:
        w(f"### {title}")
        w()
        w("| H | 基准(上涨占比) | 动量延续acc(重叠) | n | p | 非重叠acc | n | p | VR(H) |")
        w("|---|---|---|---|---|---|---|---|---|")
        for H in horizons:
            k_o = n_o = k_n = n_n = 0
            up_cnt = tot_cnt = 0
            vrs = []
            for _, px in frames.items():
                r = px.pct_change().dropna() * 100
                if len(r) < 60 + H:
                    continue
                fwd = r.rolling(H).sum().shift(-H)          # 未来 H 期累计
                trail = r.rolling(H).sum()                  # 过去 H 期累计
                d = pd.DataFrame({"trail": trail, "fwd": fwd}).dropna()
                if d.empty:
                    continue
                hit = (np.sign(d.trail) == np.sign(d.fwd)) & (d.trail != 0) & (d.fwd != 0)
                use = d[(d.trail != 0) & (d.fwd != 0)]
                k_o += int(hit.loc[use.index].sum()); n_o += len(use)
                nol = use.iloc[::H]
                k_n += int(hit.loc[nol.index].sum()); n_n += len(nol)
                up_cnt += int((d.fwd > 0).sum()); tot_cnt += len(d)
                v1 = r.var()
                vh = r.rolling(H).sum().dropna().var() / H if H > 1 else v1
                if v1 > 0:
                    vrs.append(vh / v1)
            if n_o == 0:
                continue
            w(f"| {H} | {up_cnt/max(tot_cnt,1):.4f} | {k_o/n_o:.4f} | {n_o} | {binom_p(k_o,n_o):.3g} | "
              f"{k_n/max(n_n,1):.4f} | {n_n} | {binom_p(k_n,max(n_n,1)):.3g} | "
              f"{np.mean(vrs):.3f} |")
        w()
        w("> VR(H)=方差比：>1 趋势延续、<1 均值回归、=1 随机漫步。动量延续 acc=用过去 H 期累计涨跌符号预测未来 H 期累计涨跌符号。")
        w()

    frames_d = {p: g.set_index("trade_date").adj_close for p, g in mc.groupby("product") if len(g) >= 200}
    horizon_table(frames_d, "E1. 日线（73 品种全历史）")

    hb = pd.read_sql(text(
        "SELECT symbol, trade_datetime, close FROM hourly_bar ORDER BY symbol, trade_datetime"
    ), s.get_bind())
    hb["close"] = pd.to_numeric(hb.close, errors="coerce")
    if not hb.empty:
        frames_h = {sy: g.set_index("trade_datetime").close for sy, g in hb.groupby("symbol")}
        w(f"- 小时线样本：{hb.symbol.nunique()} 品种 / {len(hb)} 根 "
          f"（{hb.trade_datetime.min()} → {hb.trade_datetime.max()}）")
        w()
        horizon_table(frames_h, "E2. 小时线（仅 FG888/SA888）", horizons=(1, 2, 4, 8, 16))

    # ---------------- F. 特征正交化与新因子 ----------------
    w("## F. 建议4 核查 — 传导特征接入现状 + 候选新因子信息含量")
    w()
    w("### F0. 传导特征是否已入模")
    w()
    w("- `app/features/transmission.py` 定义 9 个特征：sector_ret_1d/5d/20d、sector_excess、"
      "upstream_ret_lag1/lag5、cost_gap、sector_corr_regime、te_topk")
    w("- `app/predictors/ml_models.py` 通过 `FEATURE_COLS` 把它们拼进 rf/xgb 特征矩阵；lstm 亦消费")
    w("- 回测 `engine.py:170` 用 `tframe[tframe.index <= t]` 做 walk-forward 切片（无前视）")
    w(f"- **实测结果**：rf={base_acc.get('rf', float('nan')):.4f}、xgb={base_acc.get('xgb', float('nan')):.4f}、"
      f"lstm={base_acc.get('lstm', float('nan')):.4f}，"
      f"其余不吃传导特征的模型均值={np.mean([v for k, v in base_acc.items() if k not in ('rf','xgb','lstm','ensemble')]):.4f}")
    w(f"- `transmission_weights` {s.execute(text('SELECT count(*) FROM transmission_weights')).scalar()} 行，"
      f"`sector_index` {s.execute(text('SELECT count(*) FROM sector_index')).scalar()} 行 → 数据层已就绪")
    w()

    db = pd.read_sql(text(
        "SELECT symbol, trade_date, close, settle, volume, oi, ret_close FROM daily_bar "
        "WHERE oi IS NOT NULL ORDER BY symbol, trade_date"
    ), s.get_bind())
    for c in ("close", "settle", "volume", "oi", "ret_close"):
        db[c] = pd.to_numeric(db[c], errors="coerce")
    db["trade_date"] = pd.to_datetime(db.trade_date)
    w(f"### F1. 持仓量（oi）因子探针 — daily_bar {len(db)} 行 / {db.symbol.nunique()} 个合约代码")
    w()

    # 用主连口径做 oi 因子：把合约级 oi 汇总到品种级（同日全合约合计）
    db["product"] = db.symbol.str.extract(r"^([A-Za-z]+)")[0].str.upper()
    prod_oi = db.groupby(["product", "trade_date"]).agg(
        oi=("oi", "sum"), vol=("volume", "sum")).reset_index()
    mcx = mc.copy()
    mcx["product_key"] = mcx["product"].str.replace(r"888$", "", regex=True).str.upper()
    merged = mcx.merge(prod_oi, left_on=["product_key", "trade_date"],
                       right_on=["product", "trade_date"], how="inner", suffixes=("", "_p"))
    w(f"- 主连×品种持仓量成功对齐 {len(merged)} 行 / {merged.product_key.nunique()} 品种")
    w()
    if len(merged) > 2000:
        rows = []
        for pk, g in merged.groupby("product_key"):
            g = g.sort_values("trade_date").copy()
            g["ret"] = g.adj_close.pct_change() * 100
            g["doi"] = g.oi.pct_change() * 100
            g["fwd"] = g.ret.shift(-1)
            g = g.dropna(subset=["ret", "doi", "fwd"])
            if len(g) < 100:
                continue
            rows.append(g[["product_key", "trade_date", "ret", "doi", "fwd"]])
        oif = pd.concat(rows) if rows else pd.DataFrame()
        if not oif.empty:
            w("| 量仓形态（当日） | 含义 | n | 次日上涨占比 | Wilson95 CI | binom p |")
            w("|---|---|---|---|---|---|")
            pats = [
                ((oif.ret > 0) & (oif.doi > 0), "价涨仓增（多头增仓）"),
                ((oif.ret > 0) & (oif.doi < 0), "价涨仓减（空头平仓）"),
                ((oif.ret < 0) & (oif.doi > 0), "价跌仓增（空头增仓）"),
                ((oif.ret < 0) & (oif.doi < 0), "价跌仓减（多头平仓）"),
            ]
            for mask, lab in pats:
                g = oif[mask]
                k, n = int((g.fwd > 0).sum()), len(g)
                if n < 50:
                    continue
                lo, hi = wilson(k, n)
                w(f"| {lab} | 次日方向 | {n} | {k/n:.4f} | [{lo:.4f},{hi:.4f}] | {binom_p(k,n):.3g} |")
            w()
            # 持仓变化的 IC
            ic = oif.groupby("trade_date").apply(
                lambda g: g.doi.corr(g.fwd, method="spearman") if len(g) >= 8 else np.nan).dropna()
            if len(ic) > 30:
                t = ic.mean() / (ic.std() / math.sqrt(len(ic)))
                w(f"- **Δoi 截面 IC**：均值 {ic.mean():+.4f}，t={t:+.2f}，样本 {len(ic)} 日"
                  f"（|t|>2 视为有信息）")
                w()

    # F2 期限结构 carry
    w("### F2. 期限结构（carry）因子探针")
    w()
    db["cmon"] = db.symbol.str.extract(r"(\d{3,4})$")[0]
    has_contract = db.cmon.notna().mean()
    w(f"- `daily_bar.symbol` 中可解析出合约月份的比例：{has_contract:.1%}"
      f"（样例：{', '.join(db.symbol.dropna().unique()[:6])}）")
    if has_contract > 0.5:
        d2 = db[db.cmon.notna() & (db.oi > 0)].copy()

        def expiry_key(row):
            c = row.cmon
            if len(c) == 4:
                yy, mm = int(c[:2]), int(c[2:])
                return 2000 + yy + (0 if mm else 0), mm
            yy, mm = int(c[0]), int(c[1:])
            base_y = row.trade_date.year
            y = (base_y // 10) * 10 + yy
            if y < base_y:
                y += 10
            return y, mm

        ek = d2.apply(expiry_key, axis=1, result_type="expand")
        d2["exp_y"], d2["exp_m"] = ek[0], ek[1]
        d2["exp_idx"] = d2.exp_y * 12 + d2.exp_m
        rows = []
        for (p_, d_), g in d2.groupby(["product", "trade_date"]):
            g = g.sort_values("exp_idx")
            g = g[g.oi >= g.oi.max() * 0.05]
            if len(g) < 2:
                continue
            n1, n2 = g.iloc[0], g.iloc[1]
            gap = max(1, int(n2.exp_idx - n1.exp_idx))
            px1 = n1.settle if pd.notna(n1.settle) else n1.close
            px2 = n2.settle if pd.notna(n2.settle) else n2.close
            if not (px1 and px2 and px2 > 0):
                continue
            rows.append({"product": p_, "trade_date": d_,
                         "carry": (px1 - px2) / px2 / gap * 100})
        cf = pd.DataFrame(rows)
        w(f"- 成功计算 carry 的 (品种,日期) 组合：{len(cf)} 行 / {cf['product'].nunique() if not cf.empty else 0} 品种")
        if len(cf) > 2000:
            cm = cf.merge(mcx.assign(product_key=mcx.product_key)[["product_key", "trade_date", "adj_close"]],
                          left_on=["product", "trade_date"], right_on=["product_key", "trade_date"], how="inner")
            out = []
            for p_, g in cm.groupby("product"):
                g = g.sort_values("trade_date").copy()
                g["fwd"] = g.adj_close.pct_change().shift(-1) * 100
                out.append(g.dropna(subset=["carry", "fwd"]))
            cm = pd.concat(out) if out else pd.DataFrame()
            if not cm.empty:
                k = int(((cm.carry > 0) == (cm.fwd > 0)).sum()); n = len(cm)
                lo, hi = wilson(k, n)
                w(f"- **carry 符号预测次日方向**：acc={k/n:.4f}，n={n}，"
                  f"CI=[{lo:.4f},{hi:.4f}]，p={binom_p(k,n):.3g}")
                ic = cm.groupby("trade_date").apply(
                    lambda g: g.carry.corr(g.fwd, method="spearman") if len(g) >= 8 else np.nan).dropna()
                if len(ic) > 30:
                    t = ic.mean() / (ic.std() / math.sqrt(len(ic)))
                    w(f"- **carry 截面 IC**：均值 {ic.mean():+.4f}，t={t:+.2f}，样本 {len(ic)} 日")
        w()

    # F3 截面动量 IC
    w("### F3. 截面视角 — 相对强弱是否比绝对方向更可预测")
    w()
    panel = mcx.pivot_table(index="trade_date", columns="product_key", values="adj_close").sort_index()
    ret1 = panel.pct_change() * 100
    w("| 因子 | 截面 IC 均值 | t 值 | 有效日数 | 判定 |")
    w("|---|---|---|---|---|")
    for lb_, lab in ((5, "过去5日动量"), (20, "过去20日动量"), (60, "过去60日动量"), (1, "过去1日反转")):
        sig_ = panel.pct_change(lb_) * 100
        fwd_ = ret1.shift(-1)
        ics = []
        for d_ in panel.index:
            a, b = sig_.loc[d_], fwd_.loc[d_]
            m_ = a.notna() & b.notna()
            if m_.sum() >= 10:
                ics.append(stats.spearmanr(a[m_], b[m_]).correlation)
        ics = pd.Series([x for x in ics if not np.isnan(x)])
        if len(ics) > 30:
            t = ics.mean() / (ics.std() / math.sqrt(len(ics)))
            verdict = "有信息" if abs(t) > 2 else "无信息"
            w(f"| {lab} | {ics.mean():+.4f} | {t:+.2f} | {len(ics)} | {verdict} |")
    w()

    # ---------------- G. 数据就绪度 ----------------
    w("## G. 数据就绪度盘点（决定各方向的落地成本）")
    w()
    hb_syms = s.execute(text("SELECT count(DISTINCT symbol), count(*) FROM hourly_bar")).one()
    w("| 数据 | 现状 | 可用性 |")
    w("|---|---|---|")
    w(f"| 日线主连 | {mc['product'].nunique()} 品种 / {len(mc)} 根 | ✅ 全量 |")
    w(f"| 小时线 | **仅 {hb_syms[0]} 品种**（FG888/SA888）/ {hb_syms[1]} 根 | ⚠️ 非全品种，来自用户 CSV |")
    w(f"| 持仓量 oi | daily_bar {len(db)} 行 100% 覆盖 | ✅ 零成本可用 |")
    w(f"| 结算价 settle | 覆盖 {db.settle.notna().mean():.1%} | ✅ 已存（口径已裁决用 close） |")
    w("| 传导/板块 | transmission_weights + sector_index 已就绪且已入模 | ✅ 已用 |")
    w("| 会员持仓排名（龙虎榜） | 未接入 | 🔧 akshare 现成：futures_dce_position_rank / get_shfe_rank_table / get_rank_table_czce / get_cffex_rank_table / get_rank_sum_daily |")
    w("| 仓单/库存 | 未接入 | 🔧 akshare：futures_inventory_em / futures_inventory_99 / futures_warehouse_receipt_czce / futures_warehouse_receipt_dce / futures_shfe_warehouse_receipt |")
    w("| 海外机构持仓（CFTC） | 未接入 | 🔧 akshare：macro_usa_cftc_nc_holding / macro_usa_cftc_c_holding / macro_usa_cftc_merchant_goods_holding（周频，仅外盘对应品种） |")
    w("| 现货基差 | 未接入 | 🔧 需第三方现货报价（卓创/隆众），akshare 无稳定免费源 |")
    w()

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(LINES) + "\n")
print(f"\n[OK] 报告写入 {OUT}")
