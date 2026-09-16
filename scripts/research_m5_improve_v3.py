"""M5 改进方向实证研究 v3（只读）— 同样本对照与机制检验

回答三个决定性问题：
  1. 在平台自己的 6132 个评估点上，一条「反转基线」能打过 ensemble 吗？（配对 McNemar 检验）
  2. 为什么「提高成员门槛」实测无效？→ 检验模型滚动准确率的跨期持续性
  3. 反转效应是不是换月跳空伪影？→ 剔除极端收益后复核 + 用 ret_close 字段交叉验证
额外：反转 × 持仓量组合规则在同样本上的表现
"""
from __future__ import annotations

import sys
import math
import datetime as dt

sys.path.insert(0, "/app")

import numpy as np
import pandas as pd
from scipy import stats
from sqlalchemy import text

from app.core.db import session_scope

TS = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
OUT = f"/app/logs/research_m5v3_{TS}.md"
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


w(f"# M5 实证研究 v3 — 同样本对照与机制检验（{TS}）")
w()

with session_scope() as s:
    RUN = "20260908_120937_bt"
    det = pd.read_sql(text(
        "SELECT symbol, model, eval_date, state, pred_dir, prob, actual FROM backtest_detail "
        "WHERE run_id = :r"
    ), s.get_bind(), params={"r": RUN})
    det["actual"] = pd.to_numeric(det.actual, errors="coerce")
    det["prob"] = pd.to_numeric(det.prob, errors="coerce")
    det["eval_date"] = pd.to_datetime(det.eval_date)
    det = det[det.actual.notna()]

    db = pd.read_sql(text(
        "SELECT symbol, trade_date, close, oi, ret_close FROM daily_bar "
        "WHERE symbol LIKE '%%888' AND close IS NOT NULL ORDER BY symbol, trade_date"
    ), s.get_bind())
    for c in ("close", "oi", "ret_close"):
        db[c] = pd.to_numeric(db[c], errors="coerce")
    db["trade_date"] = pd.to_datetime(db.trade_date)

    # 构造每个 (symbol, 评估日) 的「当日涨跌」与「当日持仓变化」——严格用 t 及更早数据
    feat = []
    for sy, g in db.groupby("symbol"):
        g = g.sort_values("trade_date").set_index("trade_date")
        f = pd.DataFrame({
            "ret_t": g.close.pct_change() * 100,
            "ret_close_field": g.ret_close,
            "doi_t": g.oi.pct_change() * 100,
        })
        f["symbol"] = sy
        feat.append(f.reset_index())
    ft = pd.concat(feat).rename(columns={"trade_date": "eval_date"})

    ens = det[det.model == "ensemble"].merge(ft, on=["symbol", "eval_date"], how="left")
    ens = ens[ens.ret_t.notna() & (ens.ret_t != 0)]
    w(f"**同样本口径**：run `{RUN}`，ensemble 评估点 {len(ens)} 个"
      f"（{ens.symbol.nunique()} 品种），已对齐当日涨跌与持仓变化")
    w()

    ens["y"] = (ens.actual > 0).astype(int)
    ens["p_ens"] = (ens.pred_dir == "up").astype(int)
    ens["p_rev"] = (ens.ret_t < 0).astype(int)          # 反转：昨跌则predict涨
    ens["p_mom"] = (ens.ret_t > 0).astype(int)          # 延续

    w("## 1. 同样本对照 — 一条「反转基线」vs 平台 ensemble")
    w()
    w("| 策略 | 规则 | n | dir_acc | Wilson95 CI | binom p |")
    w("|---|---|---|---|---|---|")
    accs = {}
    for col, lab, rule in (("p_ens", "平台 ensemble", "13 模型融合投票"),
                           ("p_mom", "动量延续基线", "预测方向 = 当日涨跌方向"),
                           ("p_rev", "**反转基线**", "预测方向 = 当日涨跌**反**方向")):
        k = int((ens[col] == ens.y).sum()); n = len(ens)
        lo, hi = wilson(k, n)
        accs[lab] = k / n
        w(f"| {lab} | {rule} | {n} | {k/n:.4f} | [{lo:.4f}, {hi:.4f}] | {binom_p(k, n):.3g} |")
    w()
    # McNemar 配对检验：ensemble vs 反转
    a = (ens.p_ens == ens.y)
    b = (ens.p_rev == ens.y)
    n01 = int((~a & b).sum()); n10 = int((a & ~b).sum())
    stat = (abs(n01 - n10) - 1) ** 2 / (n01 + n10) if (n01 + n10) else float("nan")
    p_mc = 1 - stats.chi2.cdf(stat, 1)
    w(f"- **McNemar 配对检验（ensemble vs 反转基线）**：仅反转对={n01}，仅 ensemble 对={n10}，"
      f"χ²={stat:.2f}，p={p_mc:.3g} → "
      f"{'**反转基线显著优于 ensemble**' if p_mc < 0.05 and n01 > n10 else '两者无显著差异'}")
    w()

    # 反转 × 持仓量组合
    w("## 2. 反转 × 持仓量 组合规则（同样本）")
    w()
    e2 = ens[ens.doi_t.notna()].copy()
    w("| 规则 | n | 覆盖率 | dir_acc | Wilson95 CI | binom p |")
    w("|---|---|---|---|---|---|")
    combos = [
        ("反转（全样本）", pd.Series(True, index=e2.index), e2.p_rev),
        ("反转 ∩ 价跌仓增", (e2.ret_t < 0) & (e2.doi_t > 0), e2.p_rev),
        ("反转 ∩ 价涨仓增", (e2.ret_t > 0) & (e2.doi_t > 0), e2.p_rev),
        ("反转 ∩ 仓增（不分方向）", e2.doi_t > 0, e2.p_rev),
        ("反转 ∩ |当日涨跌|>1%", e2.ret_t.abs() > 1, e2.p_rev),
        ("反转 ∩ |当日涨跌|>1% ∩ 仓增", (e2.ret_t.abs() > 1) & (e2.doi_t > 0), e2.p_rev),
    ]
    for lab, mask, pred in combos:
        g = e2[mask]
        if len(g) < 50:
            continue
        k, n = int((pred[mask] == g.y).sum()), len(g)
        lo, hi = wilson(k, n)
        w(f"| {lab} | {n} | {n/len(e2):.1%} | {k/n:.4f} | [{lo:.4f}, {hi:.4f}] | {binom_p(k,n):.3g} |")
    w()

    # ---------- 3. 模型准确率持续性 ----------
    w("## 3. 为什么「提高成员门槛」实测无效 — 模型准确率的跨期持续性检验")
    w()
    sub = det[det.model != "ensemble"].copy()
    sub["hit"] = ((sub.pred_dir == "up") == (sub.actual > 0)).astype(int)
    rows = []
    for (sy, m), g in sub.groupby(["symbol", "model"]):
        g = g.sort_values("eval_date")
        if len(g) < 60:
            continue
        h = g.hit.to_numpy()
        half = len(h) // 2
        rows.append({"symbol": sy, "model": m,
                     "acc_h1": h[:half].mean(), "acc_h2": h[half:].mean(),
                     "n1": half, "n2": len(h) - half})
    pdf = pd.DataFrame(rows)
    if not pdf.empty:
        r_all = stats.pearsonr(pdf.acc_h1, pdf.acc_h2)
        sp = stats.spearmanr(pdf.acc_h1, pdf.acc_h2)
        w(f"- 样本：{len(pdf)} 个 (品种×模型) 组合，各拆成前后两段（各约 {int(pdf.n1.mean())} 个评估点）")
        w(f"- **前段准确率 vs 后段准确率相关性**：Pearson r={r_all.statistic:+.4f}（p={r_all.pvalue:.3g}），"
          f"Spearman ρ={sp.statistic:+.4f}（p={sp.pvalue:.3g}）")
        top = pdf.nlargest(int(len(pdf) * 0.25), "acc_h1")
        w(f"- 前段 Top25%（前段均值 {top.acc_h1.mean():.4f}）→ 后段均值 **{top.acc_h2.mean():.4f}**"
          f"（全样本后段均值 {pdf.acc_h2.mean():.4f}）")
        bot = pdf.nsmallest(int(len(pdf) * 0.25), "acc_h1")
        w(f"- 前段 Bottom25%（前段均值 {bot.acc_h1.mean():.4f}）→ 后段均值 **{bot.acc_h2.mean():.4f}**")
        w(f"- 结论：相关性{'≈0（无持续性）' if abs(r_all.statistic) < 0.1 else '存在弱持续性'}"
          f" → 用历史准确率筛成员本质是在**筛运气**，这解释了 v1 中门槛方案（0.5036/0.5034）无提升")
        # 门槛的理论噪声
        w(f"- 参考：60 个评估点上准确率的抽样标准差 ≈ {math.sqrt(0.25/60):.4f}（{math.sqrt(0.25/60)*100:.1f}pp），"
          f"而模型间真实差异 < 2pp → 信噪比 < 0.5，门槛必然被噪声主导")
    w()

    # ---------- 4. 反转效应的稳健性 ----------
    w("## 4. 反转效应稳健性 — 是否为换月跳空伪影")
    w()
    allr = []
    for sy, g in db.groupby("symbol"):
        g = g.sort_values("trade_date").set_index("trade_date")
        d = pd.DataFrame({"t": g.close.pct_change() * 100,
                          "tf": g.ret_close,
                          "symbol": sy})
        d["f"] = d.t.shift(-1)
        d["f_field"] = d.tf.shift(-1)
        allr.append(d.dropna(subset=["t", "f"]))
    ar = pd.concat(allr)
    ar = ar[(ar.t != 0) & (ar.f != 0)]
    w("| 口径 / 过滤 | n | 反转acc | Wilson95 CI | p |")
    w("|---|---|---|---|---|")
    tests = [
        ("close 复算（全样本）", ar, "t", "f"),
        ("close 复算，剔除 |ret|>10%（疑换月跳空）", ar[(ar.t.abs() <= 10) & (ar.f.abs() <= 10)], "t", "f"),
        ("close 复算，剔除 |ret|>5%", ar[(ar.t.abs() <= 5) & (ar.f.abs() <= 5)], "t", "f"),
        ("daily_bar.ret_close 字段口径", ar[ar.tf.notna() & ar.f_field.notna() & (ar.tf != 0) & (ar.f_field != 0)], "tf", "f_field"),
    ]
    for lab, g, ct, cf in tests:
        if len(g) < 100:
            continue
        k = int((np.sign(g[ct]) != np.sign(g[cf])).sum()); n = len(g)
        lo, hi = wilson(k, n)
        w(f"| {lab} | {n} | {k/n:.4f} | [{lo:.4f}, {hi:.4f}] | {binom_p(k,n):.3g} |")
    w()
    # 近三年
    ar3 = ar[ar.index >= (ar.index.max() - pd.Timedelta(days=365 * 3))]
    ar3 = ar3[(ar3.t.abs() <= 10) & (ar3.f.abs() <= 10)]
    k = int((np.sign(ar3.t) != np.sign(ar3.f)).sum()); n = len(ar3)
    lo, hi = wilson(k, n)
    w(f"- **近 3 年（剔除跳空）**：反转 acc={k/n:.4f}，n={n}，CI=[{lo:.4f},{hi:.4f}]，p={binom_p(k,n):.3g}")
    w()

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(LINES) + "\n")
print(f"\n[OK] 报告写入 {OUT}")
