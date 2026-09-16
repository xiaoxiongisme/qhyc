"""M5 改进方向 — 指标验证（样本外 OOS）只读脚本 v4

验证目标（用户点名"需要验证的指标"）：
  (a) 条件反转 0.5628 是否存在选择偏差 → TRAIN(<=2023) 选阈值 / TEST(2024+) 无前视应用
  (b) 幅度单调性、oi 因子、截面 IC 在 TEST 期是否仍显著
  (c) 净边际是否如报告所言"很薄" → 估算 OOS 毛边际 vs 成本(0.05-0.08%)

全程只读，不写库。复用 v3 的 DB 会话方式（app.core.db.session_scope）。
"""
from __future__ import annotations
import sys, math, datetime as dt
sys.path.insert(0, "/app")
import numpy as np
import pandas as pd
from scipy import stats
from sqlalchemy import text
from app.core.db import session_scope

TS = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
OUT = f"/app/logs/research_m5_validate_{TS}.md"
L: list[str] = []

def w(s: str = "") -> None:
    L.append(s); print(s)

def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n; d = 1 + z * z / n; c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)

def bp(k: int, n: int, p0: float = 0.5):
    if n == 0:
        return float("nan")
    return float(stats.binomtest(k, n, p0, alternative="two-sided").pvalue)

TRAIN_END = pd.Timestamp("2023-12-31")
CAND = [0.5, 1.0, 1.5, 2.0]   # |ret_t| 阈值候选 (%)

w(f"# M5 指标验证（样本外 OOS） {TS}")
w()
w(f"划分：TRAIN <= {TRAIN_END.date()} ｜ TEST > {TRAIN_END.date()}")
w()

with session_scope() as s:
    db = pd.read_sql(text(
        "SELECT symbol, trade_date, close, oi, ret_close FROM daily_bar "
        "WHERE symbol LIKE '%%888' AND close IS NOT NULL ORDER BY symbol, trade_date"
    ), s.get_bind())
    for c in ("close", "oi", "ret_close"):
        db[c] = pd.to_numeric(db[c], errors="coerce")
    db["trade_date"] = pd.to_datetime(db["trade_date"])
    w(f"- 载入 daily_bar *888：{db.symbol.nunique()} 品种，{len(db)} 行")
    w()

    # 构造面板（严格用 t 及更早数据，fwd 为次日收益）
    frames = []
    for sy, g in db.groupby("symbol"):
        g = g.sort_values("trade_date").copy()
        g["ret_t"] = g.close.pct_change() * 100
        g["ret5"] = g.close.pct_change(5) * 100
        g["ret20"] = g.close.pct_change(20) * 100
        g["ret60"] = g.close.pct_change(60) * 100
        g["fwd"] = g.ret_t.shift(-1)                       # 次日收益（标签）
        g["doi_t"] = g.oi.pct_change() * 100
        g["symbol"] = sy
        frames.append(g)
    pan = pd.concat(frames)
    pan = pan.dropna(subset=["ret_t", "fwd"]).query("ret_t != 0 and fwd != 0").copy()
    pan["is_train"] = pan.trade_date <= TRAIN_END
    pan["rev_hit"] = (np.sign(pan.ret_t) != np.sign(pan.fwd)).astype(int)
    w(f"- 构造面板（去零、需前后非空）：{len(pan)} 行；TRAIN {int(pan.is_train.sum())} ／ TEST {int((~pan.is_train).sum())}")
    w()

    tr = pan[pan.is_train]
    te = pan[~pan.is_train]

    # ---------- (a) OOS 条件反转 ----------
    w("## 1. 条件反转 OOS 验证（核心：0.5628 是否选择偏差）")
    w()

    def rule_acc(df, mask):
        g = df[mask]
        if len(g) < 50:
            return None
        k = int(g.rev_hit.sum()); n = len(g); lo, hi = wilson(k, n)
        return dict(n=n, acc=k / n, lo=lo, hi=hi, p=bp(k, n), cov=len(g) / len(df))

    w("### 1a. 复现报告规则 `反转 ∩ |ret|>1% ∩ 持仓增` 直接套 TEST 期")
    r_repro = rule_acc(te, (te.ret_t.abs() > 1.0) & (te.doi_t > 0))
    if r_repro:
        w(f"| TEST 期 | n={r_repro['n']} | 覆盖率 {r_repro['cov']:.1%} | dir_acc={r_repro['acc']:.4f} | "
          f"CI[{r_repro['lo']:.4f},{r_repro['hi']:.4f}] | p={r_repro['p']:.3g} |")
    else:
        w("TEST 期样本不足")
    w("（对照报告原值：同规则在平台 6040 评估点上 0.5628；此处为全面板 TEST 期，口径更大）")
    w()

    w("### 1b. TRAIN 选阈值 → TEST 应用（无前视）")
    rows = []
    for tau in CAND:
        g = tr[tr.ret_t.abs() > tau]
        if len(g) >= 200:
            k = int(g.rev_hit.sum()); n = len(g); lo, hi = wilson(k, n)
            rows.append((tau, k / n, lo, hi, bp(k, n), n))
    w("| τ(%) | TRAIN 反转acc | CI | p | n |")
    w("|---|---|---|---|---|")
    for tau, a, lo, hi, p, n in rows:
        w(f"| {tau} | {a:.4f} | [{lo:.4f},{hi:.4f}] | {p:.3g} | {n} |")
    if not rows:
        w("(TRAIN 无充足样本，终止)"); 
        with open(OUT, "w", encoding="utf-8") as f: f.write("\n".join(L) + "\n")
        raise SystemExit
    best = max(rows, key=lambda r: r[1])
    tau_star = best[0]
    w(f"- **TRAIN 最佳阈值 tau* = {tau_star}%**（TRAIN acc={best[1]:.4f}）")
    g0 = tr[tr.ret_t.abs() > tau_star]
    g1 = tr[(tr.ret_t.abs() > tau_star) & (tr.doi_t > 0)]
    acc0 = g0.rev_hit.mean(); acc1 = g1.rev_hit.mean() if len(g1) else float("nan")
    use_oi = (len(g1) >= 200) and (acc1 > acc0 + 0.005)
    w(f"- tau* 下不叠加 oi：TRAIN acc={acc0:.4f}(n={len(g0)})；叠加 oi>0：TRAIN acc={acc1:.4f}(n={len(g1)}) "
      f"→ {'采用 oi 门控' if use_oi else '不采用 oi 门控'}")
    w()

    if use_oi:
        test_mask_te = (te.ret_t.abs() > tau_star) & (te.doi_t > 0)
        tr_mask = (tr.ret_t.abs() > tau_star) & (tr.doi_t > 0)
        sel_desc = f"反转 ∩ |ret|>{tau_star}% ∩ 持仓增"
    else:
        test_mask_te = (te.ret_t.abs() > tau_star)
        tr_mask = (tr.ret_t.abs() > tau_star)
        sel_desc = f"反转 ∩ |ret|>{tau_star}%"
    r_tr = rule_acc(tr, tr_mask)
    r_te = rule_acc(te, test_mask_te)

    w("### 1c. 选定规则 OOS 结果")
    w(f"- 选定规则：**{sel_desc}**")
    w(f"- 反转基线（无门控）TEST acc = {te.rev_hit.mean():.4f}（n={len(te)}，p={bp(int(te.rev_hit.sum()), len(te)):.3g}）")
    if r_tr:
        w(f"- TRAIN（选参同样本，仅供对照）：n={r_tr['n']}，acc={r_tr['acc']:.4f}，"
          f"CI[{r_tr['lo']:.4f},{r_tr['hi']:.4f}]，p={r_tr['p']:.3g}")
    if r_te:
        w(f"- **TEST（无前视，关键）：n={r_te['n']}，覆盖率 {r_te['cov']:.1%}，acc={r_te['acc']:.4f}，"
          f"CI[{r_te['lo']:.4f},{r_te['hi']:.4f}]，p={r_te['p']:.3g}**")
        verdict = "通过（OOS 仍显著 >0.5，选择偏差不成立）" if (r_te['acc'] > 0.5 and r_te['p'] < 0.05) \
            else "未通过（OOS 不显著，原 0.5628 确为选择偏差）"
        w(f"- **判定：{'✅' if r_te['acc']>0.5 and r_te['p']<0.05 else '⚠️'} {verdict}**")
    w()

    # ---------- (b) 幅度单调性 TEST ----------
    w("## 2. 幅度分层单调性（TEST 期）")
    w()
    te2 = te.copy(); te2["absr"] = te2.ret_t.abs()
    try:
        te2["q"] = pd.qcut(te2.absr, 5, labels=["Q1", "Q2", "Q3", "Q4", "Q5"])
        w("| 分层 | n | 反转acc | p |")
        w("|---|---|---|---|")
        for q in ["Q1", "Q2", "Q3", "Q4", "Q5"]:
            g = te2[te2.q == q]
            k = int(g.rev_hit.sum()); n = len(g); lo, hi = wilson(k, n)
            w(f"| {q} | {n} | {k/n:.4f} | {bp(k, n):.3g} |")
    except Exception as e:
        w(f"(分层失败: {e})")
    w()

    # ---------- oi 因子 TEST ----------
    w("## 3. 持仓量(oi) 截面信息（TEST 期）")
    w()
    te3 = te.dropna(subset=["doi_t"]).copy()
    w("| 量仓形态 | n | 次日反转占比 | p |")
    w("|---|---|---|---|")
    for lab, mask in [("价涨仓增", (te3.ret_t > 0) & (te3.doi_t > 0)),
                      ("价涨仓减", (te3.ret_t > 0) & (te3.doi_t < 0)),
                      ("价跌仓增", (te3.ret_t < 0) & (te3.doi_t > 0)),
                      ("价跌仓减", (te3.ret_t < 0) & (te3.doi_t < 0))]:
        g = te3[mask]; k = int(g.rev_hit.sum()); n = len(g)
        if n >= 50:
            lo, hi = wilson(k, n)
            w(f"| {lab} | {n} | {k/n:.4f} | {bp(k, n):.3g} |")
    ic_oi = te3.groupby("trade_date", group_keys=False).apply(
        lambda d: stats.spearmanr(d.doi_t, d.fwd).correlation if len(d) >= 20 else np.nan,
        include_groups=False)
    ic_oi = ic_oi.dropna()
    if len(ic_oi):
        t = ic_oi.mean() / (ic_oi.std() / math.sqrt(len(ic_oi)))
        w(f"- Δoi 截面 IC 均值={ic_oi.mean():.4f}，t={t:.2f}（{len(ic_oi)} 个交易日）")
    w()

    # ---------- 截面 IC（动量反转） TEST ----------
    w("## 4. 截面相对强弱（动量反转）IC（TEST 期）")
    w()
    wfwd = pan.pivot(index="trade_date", columns="symbol", values="fwd")
    wmap = {col: pan.pivot(index="trade_date", columns="symbol", values=col)
            for col, lab in [("ret_t", "过去1日"), ("ret5", "过去5日"),
                             ("ret20", "过去20日"), ("ret60", "过去60日")]}
    w("| 因子 | 截面 IC 均值 | t | 有效日 | 判定 |")
    w("|---|---|---|---|---|")
    for col, lab in [("ret_t", "过去1日"), ("ret5", "过去5日"), ("ret20", "过去20日"), ("ret60", "过去60日")]:
        wf = wmap[col]
        ics = []
        for d in wf.index:
            if d <= TRAIN_END:
                continue
            x = wf.loc[d]; y = wfwd.loc[d]
            m = x.notna() & y.notna()
            if m.sum() >= 20:
                ics.append(stats.spearmanr(x[m], y[m]).correlation)
        ics = np.array(ics)
        if len(ics):
            t = ics.mean() / (ics.std() / math.sqrt(len(ics)))
            sig = "有信息" if (abs(t) > 2 and ics.mean() < 0) else "弱/无"
            w(f"| {lab} | {ics.mean():.4f} | {t:.2f} | {len(ics)} | {sig} |")
    w()

    # ---------- (c) 净边际 TEST ----------
    w("## 5. 净边际估算（TEST 期，验证\"很薄\"）")
    w()
    sig = te[test_mask_te]
    if len(sig):
        mean_abs = sig.fwd.abs().mean()
        acc_sig = sig.rev_hit.mean()
        gross = (2 * acc_sig - 1) * mean_abs
        w(f"- 选定规则信号日：n={len(sig)}，方向准确率={acc_sig:.4f}，平均|次日收益|={mean_abs:.3f}%")
        w(f"- **毛边际 ≈ (2×acc−1)×|move| = (2×{acc_sig:.4f}−1)×{mean_abs:.3f}% = {gross:.3f}%**")
        w(f"- 双边成本参考：手续费+滑点 ≈ 0.05–0.08% → 估算净边际 ≈ {gross - 0.065:+.3f}% "
          f"（{'为正但薄' if gross > 0.065 else '可能为负，需严控成本'}）")
    w()

w()
w("---")
w("判定汇总见各节。1c 通过则条件反转信号 OOS 有效，可进入 PRD 实现路径。")
with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(L) + "\n")
print(f"\n[OK] 报告写入 {OUT}")
