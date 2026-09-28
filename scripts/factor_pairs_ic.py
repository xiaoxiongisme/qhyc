#!/usr/bin/env python3
"""C 组：跨品种产业链比价因子（零成本配对）—— IC + 组合收益 + 成本压力

与 A/B 组不同，配对因子是**单一时间序列**而非横截面，不能按日分组算 IC
（每天只有 1 个样本）。这里用两套口径同时评估：

  1) 时序 IC：ic_h = spearman(z_t, -fwd_h)，fwd_h = ratio_{t+h}/ratio_t - 1
     —— 标签是「比价的反向变化」，所以高 z 预期比价回落，IC 应为正。
     t ≈ ic * sqrt(N)
  2) 组合口径（更贴近实盘）：
     pos_t    = -z_{t-1}           # 比价偏高 → 空 A 多 B
     pnl_t    = pos_t * (sp_t - sp_{t-1})   # sp = ln(P_A / P_B)
     t_pnl 与年化 Sharpe 直接由 pnl 序列给出
     同时给出换手率，并在 1/2/5 bp 三档成本下看是否还活

     sp 用对数比价，pnl 近似「等名义本金多 A 空 B」的组合回报（杠杆 1）。

数据：daily_bar（115 个品种，2015-2026），无需任何额外数据源。

用法：
    python scripts/factor_pairs_ic.py
    python scripts/factor_pairs_ic.py --since 2018
"""
from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

from factor_ic_scan import load_creds, _num  # 复用连接与 Decimal 转换

# (名称, 分子 A, 分母 B, 逻辑说明)
PAIRS = [
    ("螺矿比 RB/I", "RB888", "I888", "钢厂利润的盘面代理"),
    ("热卷螺差 HC-RB", "HC888", "RB888", "品种间价差/区域利润"),
    ("焦比 J/JM", "J888", "JM888", "焦炭/焦煤，黑色利润核心"),
    ("油粕比 Y/P", "Y888", "P888", "豆油/棕榈油，油脂内部分工"),
    ("粕油比 M/Y", "M888", "Y888", "同料不同产品，压榨利润代理"),
    ("糖棉比 SR/CF", "SR888", "CF888", "软商品替代"),
    ("玻璃纯碱 FG/SA", "FG888", "SA888", "联产，著名高相关对"),
    ("铜铝比 CU/AL", "CU888", "AL888", "宏观调控代理"),
    ("PT乙二醇 TA/EG", "TA888", "EG888", "能化，原油→下游传导"),
    ("甲醇PT MA/TA", "MA888", "TA888", "能化配对比价"),
    ("豆一豆油 A/Y", "A888", "Y888", "国产大豆 vs 进口大豆成本"),
    ("豆粕玉米 M/C", "M888", "C888", "饲料配方比价"),
    ("螺纹盘面利润 RB-(I*1.6+J*0.5)", "RB888", "I888", "近似钢厂利润（用螺矿比粗代理）"),
]

HORIZONS = [1, 3, 5, 10]
ZWIN = 60
COSTS_BP = [1, 2, 5]


def _f4(x) -> str:
    return f"{x:+.4f}" if x is not None and np.isfinite(x) else "  —   "


def _f1(x) -> str:
    return f"{x:+.1f}" if x is not None and np.isfinite(x) else " — "


def _f2(x) -> str:
    return f"{x:+.2f}" if x is not None and np.isfinite(x) else "  —  "


def eval_pair(sp: pd.Series, zwin: int = ZWIN) -> dict:
    rec: dict = {"days": len(sp)}
    sp = sp.astype(float)
    ma = sp.rolling(zwin, min_periods=max(15, zwin // 2)).mean()
    sd = sp.rolling(zwin, min_periods=max(15, zwin // 2)).std(ddof=0)
    z = (sp - ma) / sd.replace(0, np.nan)

    # ---- 时序 IC 与衰减
    for h in HORIZONS:
        fwd = sp.shift(-h) - sp                 # 比价的对数变化（未来 h 日）
        j = pd.concat([z.rename("f"), fwd.rename("y")], axis=1).dropna()
        n = len(j)
        if n < 60:
            rec[f"ic{h}"] = np.nan
            rec[f"t{h}"] = np.nan
            rec[f"n{h}"] = n
            continue
        ic_mr = stats.spearmanr(j["f"], -j["y"]).statistic   # 均值回归方向
        ic_mo = stats.spearmanr(j["f"], j["y"]).statistic    # 趋势方向
        rec[f"ic{h}"] = ic_mr
        rec[f"t{h}"] = ic_mr * np.sqrt(n)
        rec[f"ic_mo{h}"] = ic_mo
        rec[f"n{h}"] = n

    # ---- 比价自身诊断：漂移 vs 均值回归
    ds = sp.diff()
    sd0 = ds.std(ddof=1)
    if sd0 > 0:
        rec["acf5"] = float(sp.autocorr(lag=5))
        rec["acf20"] = float(sp.autocorr(lag=20))

    # ---- 组合口径：权重归一到 [-1, 1]（名义本金比例），并剔除换月假跳空
    d = pd.concat([z.rename("z"), sp.rename("sp")], axis=1).dropna()
    ret = d["sp"].diff()
    if sd0 > 0:
        keep = (ret.abs() < 8 * sd0)          # 主力连续换月的 10σ 跳空
        d = d[keep]
    if len(d) < 60:
        for k in ("t_pnl", "sharpe", "pnl_bp", "turnover", "is_sharpe", "oos_sharpe"):
            rec[k] = np.nan
        return rec

    w = (-d["z"] / 2.0).clip(-1.0, 1.0)       # 高 z → 空 A 多 B，满仓对应 |z|=2
    pnl = (w.shift(1) * d["sp"].diff()).dropna()
    turn = float(w.diff().abs().mean())
    mu, sd_p = pnl.mean(), pnl.std(ddof=1)
    rec["pnl_bp"] = mu * 1e4                   # 日均收益（bp 名义本金）
    rec["t_pnl"] = mu / sd_p * np.sqrt(len(pnl)) if sd_p > 0 else np.nan
    rec["sharpe"] = mu / sd_p * np.sqrt(252) if sd_p > 0 else np.nan
    rec["turnover"] = turn
    rec["costbp1"] = turn * 1.0                # 单边 1bp 下的日均消耗（bp）
    rec["costbp2"] = turn * 2.0
    rec["costbp5"] = turn * 5.0
    # IS/OOS（2021 年为界，与 A 组 2023 界不同，这里多给一段）
    cut = d.index[len(d) // 2]
    for tag, sub in (("is", d[d.index < cut]), ("oos", d[d.index >= cut])):
        ww = (-sub["z"] / 2.0).clip(-1.0, 1.0)
        p = (ww.shift(1) * sub["sp"].diff()).dropna()
        if len(p) < 30 or p.std(ddof=1) <= 0:
            rec[f"{tag}_sharpe"] = np.nan
        else:
            rec[f"{tag}_sharpe"] = p.mean() / p.std(ddof=1) * np.sqrt(252)
    return rec


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2015")
    ap.add_argument("--zwin", type=int, default=ZWIN)
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    out_dir = Path(a.out) if a.out else Path(__file__).resolve().parent / "factor_pairs_out"
    out_dir.mkdir(parents=True, exist_ok=True)

    import psycopg2
    conn = psycopg2.connect(**load_creds())
    cur = conn.cursor()
    cur.execute("SELECT symbol, trade_date, close FROM daily_bar "
                "WHERE trade_date >= %s AND close > 0 ORDER BY symbol, trade_date",
                (f"{a.since}-01-01",))
    db = _num(pd.DataFrame(cur.fetchall(), columns=["symbol", "trade_date", "close"]))
    db["trade_date"] = pd.to_datetime(db["trade_date"])
    print(f"载入 {len(db):,} 行 / {db['symbol'].nunique()} 品种\n")

    rows = []
    for name, na, nb, why in PAIRS:
        a_ = db[db["symbol"] == na].set_index("trade_date")["close"]
        b_ = db[db["symbol"] == nb].set_index("trade_date")["close"]
        idx = a_.index.intersection(b_.index)
        if len(idx) < 300:
            print(f"  {name:28s} 数据不足（{len(idx)}）")
            continue
        sp = np.log(a_[idx] / b_[idx])
        rec = eval_pair(sp, a.zwin)
        rec.update({"pair": name, "why": why, "na": na, "nb": nb})
        rows.append(rec)
        head = "  " + name.ljust(28)
        if not np.isfinite(rec.get("t_pnl", np.nan)):
            print(head + "—")
        else:
            print(head + "  ".join(f"h{h}: IC={_f4(rec.get(f'ic{h}'))}/t={_f1(rec.get(f't{h}'))}"
                                   for h in HORIZONS)
                  + f"   | 多空 t={_f1(rec.get('t_pnl'))} SR={rec.get('sharpe'):+.2f}")

    res = pd.DataFrame(rows)
    res.to_csv(out_dir / "factor_pairs_ic.csv", index=False, encoding="utf-8-sig")

    # ---------------- 报告
    best_ic_col = max((f"ic{h}" for h in (1, 3, 5)), key=lambda c: abs(res[c]).fillna(0).max())
    lines = [
        "# C 组：跨品种产业链比价因子（零成本配对）\n",
        f"> 区间 `{a.since}+`　数据源：`daily_bar`（无需爬虫）　因子 = ln(P_A/P_B) 的 "
        f"`{a.zwin}` 日 z-score；方向 = 均值回归（高 z → 空 A 多 B）\n",
        "> 单条比价是**时间序列**不是横截面，故 IC 用时序口径 `spearman(z_t, -fwd_h)`，"
        "显著性用 `t = IC·√N`；组合口径给出多空 t 与年化 SR。\n",
        "## 一、时序 IC 与衰减\n",
        "| 比价 | 逻辑 | " + " | ".join(f"h{h}d IC / t" for h in HORIZONS)
        + " | 最佳 IC | 判定 |",
        "|---" * (len(HORIZONS) + 3) + "|",
    ]
    for _, r in res.iterrows():
        cells, best = [], (None, 0.0)
        for h in HORIZONS:
            ic, t = r.get(f"ic{h}"), r.get(f"t{h}")
            cells.append(_f4(ic) + " / " + _f1(t))
            sc = abs(ic) if ic is not None and np.isfinite(ic) else 0.0
            if sc > best[1]:
                best = (h, sc)
        verdict = "**保留**" if (best[1] > 0.03 and abs(r.get(f"t{best[0]}", 0) or 0) > 2.5) else "淘汰"
        lines.append(f"| {r['pair']} | {r['why']} | " + " | ".join(cells)
                     + f" | h{best[0]} {_f4(r.get(f'ic{best[0]}'))} | {verdict} |")

    lines += ["\n## 二、组合口径（权重归一到 ±1，剔除 8σ 换月跳空）\n",
              "| 比价 | 日均收益 bp | 多空 t | 年化 SR | IS SR | OOS SR | 日换手 | "
              + " | ".join(f"扣 {c}bp 后 SR" for c in COSTS_BP) + " | 判定 |",
              "|---" * (len(COSTS_BP) + 7) + "|"]
    for _, r in res.iterrows():
        alive = []
        for c in COSTS_BP:
            s = r.get("sharpe")
            cost = r.get(f"costbp{c}", np.nan)
            alive.append("—" if not (np.isfinite(s) and np.isfinite(cost))
                         else f"{s - cost:+.2f}")
        keep = (np.isfinite(r.get("sharpe", np.nan)) and r["sharpe"] > 0
                and abs(r.get("t_pnl", 0) or 0) > 2
                and (r.get("sharpe", 0) - r.get("costbp2", 9)) > 0)
        lines.append(
            f"| {r['pair']} | {_f2(r.get('pnl_bp'))} | {_f1(r.get('t_pnl'))} | "
            f"{_f2(r.get('sharpe'))} | {_f1(r.get('is_sharpe'))} | {_f1(r.get('oos_sharpe'))} | "
            f"{r['turnover']:.2f} | " + " | ".join(alive)
            + f" | {'**保留**' if keep else '淘汰'} |")

    lines += ["\n## 三、诊断：是均值回归还是漂移？\n",
              "> `acf5/acf20` = 对数比价序列的 5/20 日自相关。自相关显著为正 = 比价在**趋势**，"
              "此时「z 高 → 做空」赚的是漂移不是回归，短周期无效且长期暴露很大。\n",
              "| 比价 | acf5 | acf20 | 解读 |", "|---|---|---|---|"]
    for _, r in res.iterrows():
        a5, a20 = r.get("acf5"), r.get("acf20")
        if not (np.isfinite(a5) and np.isfinite(a20)):
            continue
        verdict = ("趋势型（自相关高）" if a20 > 0.3 else
                   "弱趋势" if a20 > 0.1 else
                   "均值回归为主" if a20 < 0 else "震荡为主")
        lines.append(f"| {r['pair']} | {a5:+.3f} | {a20:+.3f} | {verdict} |")

    p = out_dir / "factor_pairs_report.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n报告 -> {p}")
    conn.close()


if __name__ == "__main__":
    main()
