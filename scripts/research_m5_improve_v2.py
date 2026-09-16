"""M5 改进方向实证研究 v2（只读）— 修正数据源

v1 的错误：
  - 用 main_continuous 做长历史 → 该表只有 FG/SA 两个品种（用户 CSV 快照），样本被低估
  - 把 daily_bar.symbol（实为主连代码 A888/AG888）当成合约代码解析月份 → carry 因子无效
v2 修正：
  - 长历史面板改用 daily_bar 的 *888 主连代码（73 品种全历史）
  - carry 因子如实标注"库内无合约级数据，需新接入"
新增：
  - H=1 反转效应的全样本检验 + 品种一致性 + 幅度分层（v1 在 2 品种上已见显著反转）
  - 持仓量量仓配合因子（全品种）
  - 截面动量/反转 IC（全品种）
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
OUT = f"/app/logs/research_m5v2_{TS}.md"
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


w(f"# M5 改进方向实证研究 v2（修正数据源） — {TS}")
w()

with session_scope() as s:
    syms = [r[0] for r in s.execute(text(
        "SELECT DISTINCT symbol FROM daily_bar ORDER BY symbol")).all()]
    m888 = [x for x in syms if x.upper().endswith("888")]
    w(f"- `daily_bar` 共 {len(syms)} 个代码，其中主连代码（*888）{len(m888)} 个；"
      f"非主连代码示例：{[x for x in syms if x not in m888][:6]}")
    w("- **库内无单合约（如 FG2601）数据** → 期限结构/carry 因子需新接入数据源，本轮无法实测")
    w()

    db = pd.read_sql(text(
        "SELECT symbol, trade_date, close, volume, oi, ret_close FROM daily_bar "
        "WHERE symbol LIKE '%%888' AND close IS NOT NULL ORDER BY symbol, trade_date"
    ), s.get_bind())
    for c in ("close", "volume", "oi", "ret_close"):
        db[c] = pd.to_numeric(db[c], errors="coerce")
    db["trade_date"] = pd.to_datetime(db.trade_date)
    w(f"- 长历史面板：{db.symbol.nunique()} 品种 / {len(db)} 根日线 "
      f"（{db.trade_date.min().date()} → {db.trade_date.max().date()}）")
    w()

    frames = {sy: g.set_index("trade_date") for sy, g in db.groupby("symbol") if len(g) >= 250}

    # ---------- 1. 多周期动量/反转 ----------
    w("## 1. 建议1 实测（全品种）— 各周期"
      "「动量延续 vs 反转」可预测性")
    w()
    w("| H | 上涨基准 | 动量延续acc | n(重叠) | p | 非重叠acc | n | p | 反转acc(=1−延续) | VR(H) |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    hz_rows = []
    for H in (1, 2, 3, 5, 10, 20):
        k_o = n_o = k_n = n_n = up_c = tot_c = 0
        vrs = []
        for sy, g in frames.items():
            r = g.close.pct_change().dropna() * 100
            if len(r) < 80 + H:
                continue
            trail = r.rolling(H).sum()
            fwd = r.rolling(H).sum().shift(-H)
            d = pd.DataFrame({"trail": trail, "fwd": fwd}).dropna()
            d = d[(d.trail != 0) & (d.fwd != 0)]
            if d.empty:
                continue
            hit = np.sign(d.trail) == np.sign(d.fwd)
            k_o += int(hit.sum()); n_o += len(d)
            nol = d.iloc[::H]
            k_n += int(hit.loc[nol.index].sum()); n_n += len(nol)
            up_c += int((d.fwd > 0).sum()); tot_c += len(d)
            v1 = r.var()
            vh = (r.rolling(H).sum().dropna().var() / H) if H > 1 else v1
            if v1 > 0:
                vrs.append(vh / v1)
        if n_o == 0:
            continue
        acc_o, acc_n = k_o / n_o, k_n / max(n_n, 1)
        hz_rows.append({"H": H, "acc_o": acc_o, "n_o": n_o, "p_o": binom_p(k_o, n_o),
                        "acc_n": acc_n, "n_n": n_n, "p_n": binom_p(k_n, max(n_n, 1)),
                        "vr": float(np.mean(vrs))})
        w(f"| {H} | {up_c/max(tot_c,1):.4f} | {acc_o:.4f} | {n_o} | {binom_p(k_o,n_o):.3g} | "
          f"{acc_n:.4f} | {n_n} | {binom_p(k_n,max(n_n,1)):.3g} | {1-acc_o:.4f} | "
          f"{np.mean(vrs):.3f} |")
    w()
    w("> 「非重叠」为独立样本，是统计上更严格的口径。反转acc = 用「与过去 H 期反向」规则的准确率。")
    w()

    # ---------- 2. H=1 反转效应细究 ----------
    w("## 2. 关键发现细究 — H=1 短期反转效应")
    w()
    per_sym = []
    for sy, g in frames.items():
        r = g.close.pct_change().dropna() * 100
        d = pd.DataFrame({"t": r, "f": r.shift(-1)}).dropna()
        d = d[(d.t != 0) & (d.f != 0)]
        if len(d) < 200:
            continue
        rev = int((np.sign(d.t) != np.sign(d.f)).sum())
        per_sym.append({"symbol": sy, "n": len(d), "rev_acc": rev / len(d),
                        "p": binom_p(rev, len(d))})
    ps = pd.DataFrame(per_sym)
    tot_k = int((ps.rev_acc * ps.n).sum()); tot_n = int(ps.n.sum())
    lo, hi = wilson(tot_k, tot_n)
    w(f"- **全样本反转准确率：{tot_k/tot_n:.4f}**（n={tot_n}，Wilson95=[{lo:.4f},{hi:.4f}]，"
      f"p={binom_p(tot_k, tot_n):.3g}）")
    w(f"- 品种一致性：{int((ps.rev_acc > 0.5).sum())}/{len(ps)} 个品种反转准确率 >50%；"
      f"其中单品种显著（p<0.05）{int(((ps.rev_acc>0.5)&(ps.p<0.05)).sum())} 个")
    w(f"- 反转最强 5 品种：" + "、".join(
        f"{r.symbol} {r.rev_acc:.3f}" for _, r in ps.nlargest(5, 'rev_acc').iterrows()))
    w(f"- 反转最弱 5 品种：" + "、".join(
        f"{r.symbol} {r.rev_acc:.3f}" for _, r in ps.nsmallest(5, 'rev_acc').iterrows()))
    w()
    # 幅度分层
    allr = []
    for sy, g in frames.items():
        r = g.close.pct_change().dropna() * 100
        d = pd.DataFrame({"t": r, "f": r.shift(-1)}).dropna()
        d["symbol"] = sy
        allr.append(d[(d.t != 0) & (d.f != 0)])
    ar = pd.concat(allr)
    ar["absr"] = ar.t.abs()
    ar["rev"] = (np.sign(ar.t) != np.sign(ar.f)).astype(int)
    ar["q"] = pd.qcut(ar.absr, 5, labels=False, duplicates="drop")
    w("### 2.1 按当日涨跌幅绝对值分层（反转是否在大波动日更强）")
    w()
    w("| 分层 | |当日涨跌%| 区间 | n | 反转acc | Wilson95 | p |")
    w("|---|---|---|---|---|---|---|")
    for q, g in ar.groupby("q"):
        k, n = int(g.rev.sum()), len(g)
        l_, h_ = wilson(k, n)
        w(f"| Q{int(q)+1} | [{g.absr.min():.2f}, {g.absr.max():.2f}] | {n} | {k/n:.4f} | "
          f"[{l_:.4f},{h_:.4f}] | {binom_p(k,n):.3g} |")
    w()
    # 分年度稳定性
    ar["year"] = ar.index.year
    w("### 2.2 分年度稳定性（是否只是某一年的偶然）")
    w()
    w("| 年份 | n | 反转acc | Wilson95 | p |")
    w("|---|---|---|---|---|")
    for y, g in ar.groupby("year"):
        k, n = int(g.rev.sum()), len(g)
        if n < 500:
            continue
        l_, h_ = wilson(k, n)
        w(f"| {y} | {n} | {k/n:.4f} | [{l_:.4f},{h_:.4f}] | {binom_p(k,n):.3g} |")
    w()

    # ---------- 3. 持仓量因子 ----------
    w("## 3. 补充方向 — 持仓量（oi）量仓配合因子（全品种）")
    w()
    oi_rows = []
    for sy, g in frames.items():
        g = g.sort_index()
        d = pd.DataFrame({
            "ret": g.close.pct_change() * 100,
            "doi": g.oi.pct_change() * 100,
            "dvol": g.volume.pct_change() * 100,
        })
        d["fwd"] = d.ret.shift(-1)
        d["symbol"] = sy
        oi_rows.append(d.dropna(subset=["ret", "doi", "fwd"]))
    oif = pd.concat(oi_rows)
    oif = oif[(oif.ret != 0) & (oif.fwd != 0)]
    w(f"- 样本：{len(oif)} 行 / {oif.symbol.nunique()} 品种")
    w()
    w("| 当日量仓形态 | 市场含义 | n | 次日上涨占比 | 次日反转占比 | Wilson95(反转) | p |")
    w("|---|---|---|---|---|---|---|")
    for mask, lab, mean_ in (
        ((oif.ret > 0) & (oif.doi > 0), "价涨仓增", "多头主动增仓（教科书=延续）"),
        ((oif.ret > 0) & (oif.doi < 0), "价涨仓减", "空头平仓推动（教科书=衰竭）"),
        ((oif.ret < 0) & (oif.doi > 0), "价跌仓增", "空头主动增仓（教科书=延续）"),
        ((oif.ret < 0) & (oif.doi < 0), "价跌仓减", "多头平仓推动（教科书=衰竭）"),
    ):
        g = oif[mask]
        if len(g) < 100:
            continue
        up = int((g.fwd > 0).sum())
        rev = int((np.sign(g.ret) != np.sign(g.fwd)).sum())
        l_, h_ = wilson(rev, len(g))
        w(f"| {lab} | {mean_} | {len(g)} | {up/len(g):.4f} | {rev/len(g):.4f} | "
          f"[{l_:.4f},{h_:.4f}] | {binom_p(rev,len(g)):.3g} |")
    w()
    ic = oif.groupby(oif.index).apply(
        lambda g: g.doi.corr(g.fwd, method="spearman") if len(g) >= 10 else np.nan).dropna()
    if len(ic) > 50:
        t = ic.mean() / (ic.std() / math.sqrt(len(ic)))
        w(f"- **Δoi 截面 IC**：均值 {ic.mean():+.4f}，t={t:+.2f}，{len(ic)} 个交易日"
          f"（|t|>2 视为有信息）→ {'有信息' if abs(t)>2 else '无信息'}")
    ic2 = oif.groupby(oif.index).apply(
        lambda g: g.dvol.corr(g.fwd, method="spearman") if len(g) >= 10 else np.nan).dropna()
    if len(ic2) > 50:
        t2 = ic2.mean() / (ic2.std() / math.sqrt(len(ic2)))
        w(f"- **Δvolume 截面 IC**：均值 {ic2.mean():+.4f}，t={t2:+.2f} → "
          f"{'有信息' if abs(t2)>2 else '无信息'}")
    w()

    # ---------- 4. 截面 IC ----------
    w("## 4. 补充方向 — 截面（相对强弱）视角的信息含量")
    w()
    panel = db.pivot_table(index="trade_date", columns="symbol", values="close").sort_index()
    ret1 = panel.pct_change() * 100
    w("| 因子 | 截面 IC 均值 | t 值 | IC>0 占比 | 有效日数 | 判定 |")
    w("|---|---|---|---|---|---|")
    ic_res = {}
    for lb_, lab in ((1, "过去1日涨跌（反转）"), (5, "过去5日动量"), (20, "过去20日动量"),
                     (60, "过去60日动量")):
        sig_ = panel.pct_change(lb_) * 100
        fwd_ = ret1.shift(-1)
        ics = []
        for d_ in panel.index:
            a, b = sig_.loc[d_], fwd_.loc[d_]
            m_ = a.notna() & b.notna()
            if m_.sum() >= 10:
                c = stats.spearmanr(a[m_], b[m_]).correlation
                if not np.isnan(c):
                    ics.append(c)
        ics = pd.Series(ics)
        if len(ics) > 50:
            t = ics.mean() / (ics.std() / math.sqrt(len(ics)))
            ic_res[lab] = (ics.mean(), t)
            w(f"| {lab} | {ics.mean():+.4f} | {t:+.2f} | {(ics>0).mean():.1%} | {len(ics)} | "
              f"{'**有信息**' if abs(t) > 2 else '无信息'} |")
    w()
    w("> 截面 IC：每日在全品种间做「因子值 vs 次日涨跌」的 Spearman 相关，再对时间序列做 t 检验。"
      "|t|>2 表示该因子在横截面上稳定携带信息（可用于相对强弱排序 / 多空对冲）。")
    w()

    # ---------- 5. 波动率可预测性对照 ----------
    w("## 5. 补充方向 — 「方向 vs 波动率」哪个更可预测（换预测标的）")
    w()
    rows = []
    for sy, g in frames.items():
        r = g.close.pct_change().dropna() * 100
        if len(r) < 300:
            continue
        av = r.abs()
        rows.append({
            "symbol": sy,
            "ac1_ret": r.autocorr(1),
            "ac1_absret": av.autocorr(1),
            "ac5_absret": av.autocorr(5),
            "r2_vol": (av.rolling(5).mean().shift(1).corr(av)) ** 2,
        })
    vr = pd.DataFrame(rows)
    w(f"| 指标 | 全品种均值 | 中位 | 解读 |")
    w("|---|---|---|---|")
    w(f"| 收益率 lag1 自相关 | {vr.ac1_ret.mean():+.4f} | {vr.ac1_ret.median():+.4f} | "
      f"接近 0/微负 → 方向几乎不可预测（微弱反转） |")
    w(f"| |收益率| lag1 自相关 | {vr.ac1_absret.mean():+.4f} | {vr.ac1_absret.median():+.4f} | "
      f"显著正 → 波动率高度可预测（波动聚集） |")
    w(f"| |收益率| lag5 自相关 | {vr.ac5_absret.mean():+.4f} | {vr.ac5_absret.median():+.4f} | "
      f"仍为正 → 波动记忆长 |")
    w(f"| 5日均|收益率| 预测当日|收益率| R² | {vr.r2_vol.mean():.4f} | {vr.r2_vol.median():.4f} | "
      f"波动率预测的可解释方差 |")
    w()
    w(f"- 对照：方向预测的「可解释方差」量级 ≈ 自相关² ≈ {vr.ac1_ret.mean()**2:.5f}，"
      f"波动率预测 ≈ {vr.r2_vol.mean():.4f} → **相差约 {vr.r2_vol.mean()/max(vr.ac1_ret.mean()**2,1e-9):.0f} 倍**")
    w()

with open(OUT, "w", encoding="utf-8") as f:
    f.write("\n".join(LINES) + "\n")
print(f"\n[OK] 报告写入 {OUT}")
