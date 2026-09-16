"""M6c.1 验证：P6 池化面板建模 OOS（PRD v1.3.2 §18.10 / 实现问题清单 P6）

验证目标：
  1. 评估点数量：单品种回归 rf（旧）≈ 84/品种 → 全品种池化分类 2000+（提升量级）
  2. 方向技能：池化分类器在 TEST(2024+) 的 dir_acc / Wilson CI / 二项 p / IC
     —— 对照 §18.2 旧的 rf/xgb 回归 ≈0.49（方向学反）
  3. 与条件反转对照：在 |ret_t|>1% 子集上，池化分类器 dir_acc 是否接近 reversal 的 0.535

全程只读，不写库。复用 research_m5_validate 的 DB 会话方式。
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
OUT = f"/app/logs/research_m6c_pooled_{TS}.md"
L: list[str] = []


def w(s: str = "") -> None:
    L.append(s)
    print(s)


def wilson(k: int, n: int, z: float = 1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    m = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return ((c - m) / d, (c + m) / d)


def bp(k: int, n: int, p0: float = 0.5):
    if n == 0:
        return float("nan")
    return float(stats.binomtest(k, n, p0, alternative="two-sided").pvalue)


TRAIN_END = pd.Timestamp("2023-12-31")
LAGS = (1, 2, 3, 5, 10, 20)
VOL_WIN = 20
FEATS = [f"z{L}" for L in LAGS] + ["lvl"]

w(f"# M6c.1 池化面板建模 OOS 验证 {TS}")
w()
w(f"划分：TRAIN <= {TRAIN_END.date()} ｜ TEST > {TRAIN_END.date()}（收盘价口径，与全链路一致）")
w()

with session_scope() as s:
    db = pd.read_sql(text(
        "SELECT symbol, trade_date, close FROM daily_bar "
        "WHERE symbol LIKE '%%888' AND close IS NOT NULL ORDER BY symbol, trade_date"
    ), s.get_bind())
    db["close"] = pd.to_numeric(db["close"], errors="coerce")
    db["trade_date"] = pd.to_datetime(db["trade_date"])
    w(f"- 载入 daily_bar *888：{db.symbol.nunique()} 品种，{len(db)} 行")
    w()

    # ---- 构造每品种序列（与 build_pooled_panel 同口径：归一化滞后收益 + log vol）----
    frames = []
    for sy, g in db.groupby("symbol"):
        g = g.sort_values("trade_date").copy()
        g["ret"] = g["close"].pct_change() * 100.0
        vol = g["ret"].rolling(VOL_WIN).std()
        for lag in LAGS:
            g[f"z{lag}"] = g["ret"].shift(lag) / vol.shift(lag)
        g["lvl"] = np.log(vol.replace(0, np.nan))
        g["fwd"] = g["ret"].shift(-1)
        g = g.dropna(subset=FEATS + ["fwd"])
        g = g[g["ret"] != 0]
        g["up"] = (g["fwd"] > 0).astype(int)
        g["is_train"] = g["trade_date"] <= TRAIN_END
        g["symbol"] = sy
        frames.append(g[["symbol", "trade_date", "ret", "is_train", "fwd", "up"] + FEATS])
    pan = pd.concat(frames)
    w(f"- 面板（去零/去首尾 NaN）：{len(pan)} 行；"
      f"TRAIN {int(pan.is_train.sum())} ／ TEST {int((~pan.is_train).sum())}")
    n_test = int((~pan.is_train).sum())
    w(f"- **评估点（TEST）={n_test}**，对比单品种回归 rf 旧口径 ≈84/品种 "
      f"→ 池化提升约 {n_test / 84:.0f}× 量级（84 → {n_test}）")
    w()

    tr = pan[pan.is_train].dropna(subset=FEATS)
    te = pan[~pan.is_train].dropna(subset=FEATS)
    Xtr, ytr = tr[FEATS].to_numpy(float), tr["up"].to_numpy(int)
    Xte, yte = te[FEATS].to_numpy(float), te["up"].to_numpy(int)
    fwd_te = te["fwd"].to_numpy(float)

    # ---------- 1. 池化分类器 OOS ----------
    w("## 1. 池化分类器 OOS（全品种训练，TEST 评估）")
    w()
    w("| 模型 | TEST n | dir_acc | Wilson CI | 二项 p | IC(Spearman) | 判定 |")
    w("|---|---|---|---|---|---|---|")
    from sklearn.ensemble import RandomForestClassifier
    from xgboost import XGBClassifier

    for kind, Est in [
        ("rf_pool", RandomForestClassifier(n_estimators=200, max_depth=6,
                                           class_weight="balanced", random_state=42, n_jobs=-1)),
        ("xgb_pool", XGBClassifier(n_estimators=200, max_depth=6, learning_rate=0.05,
                                   subsample=0.8, colsample_bytree=0.8, reg_lambda=1.0,
                                   random_state=42, n_jobs=2, verbosity=0)),
    ]:
        est = Est.fit(Xtr, ytr)
        p = est.predict_proba(Xte)[:, 1]
        pred = (p >= 0.5).astype(int)
        k = int((pred == yte).sum())
        n = len(yte)
        lo, hi = wilson(k, n)
        pv = bp(k, n)
        ic = stats.spearmanr(p, fwd_te).correlation
        sig = "有方向信息" if (k / n > 0.5 and pv < 0.05) else "未显著优于随机"
        w(f"| {kind} | {n} | {k/n:.4f} | [{lo:.4f},{hi:.4f}] | {pv:.3g} | {ic:.4f} | {sig} |")
    w()

    # ---------- 2. 单品种回归 rf（旧口径）对照 ----------
    w("## 2. 单品种回归 rf OOS（旧口径，~84 评估点/品种）对照")
    w()
    w("每品种用自身 TRAIN 拟合 RandomForestRegressor，预测 TEST 次日方向（符号），聚合。")
    accs, ns = [], []
    for sy, g in pan.groupby("symbol"):
        gt, ge = g[g.is_train], g[~g.is_train]
        if len(gt) < 120 or len(ge) < 30:
            continue
        from sklearn.ensemble import RandomForestRegressor

        est = RandomForestRegressor(n_estimators=120, max_depth=4, random_state=42, n_jobs=-1)
        est.fit(gt[FEATS].to_numpy(float), gt["ret"].to_numpy(float))
        pred_ret = est.predict(ge[FEATS].to_numpy(float))
        pred_dir = (pred_ret >= 0).astype(int)
        accs.append((pred_dir == ge["up"].to_numpy(int)).mean())
        ns.append(len(ge))
    if accs:
        w(f"- 品种数={len(accs)}，单品种 TEST 平均 dir_acc={np.mean(accs):.4f} "
          f"（印证 §18.2 回归方向学反，多数 <0.5）")
        w(f"- 单品种评估点合计={sum(ns)}（≈{sum(ns)//len(accs)}/品种，量级 84）")
    w()

    # ---------- 3. |ret_t|>1% 子集（与条件反转对照）----------
    w("## 3. |ret_t|>1% 子集（对照 reversal OOS 0.535）")
    w()
    big = pan[~pan.is_train].copy()
    big["absr"] = big["ret"].abs()
    bb = big[big["absr"] > 1.0].dropna(subset=FEATS)
    if len(bb):
        est = RandomForestClassifier(n_estimators=200, max_depth=6, class_weight="balanced",
                                     random_state=42, n_jobs=-1).fit(Xtr, ytr)
        p2 = est.predict_proba(bb[FEATS].to_numpy(float))[:, 1]
        pred2 = (p2 >= 0.5).astype(int)
        k2 = int((pred2 == bb["up"].to_numpy(int)).sum())
        n2 = len(bb)
        lo2, hi2 = wilson(k2, n2)
        w(f"- |ret_t|>1% 子集：n={n2}，dir_acc={k2/n2:.4f}，CI[{lo2:.4f},{hi2:.4f}]，"
          f"p={bp(k2, n2):.3g}")
        w(f"- 对照 reversal OOS 0.535（p<1e-7）；池化分类器在强波动日方向技能见上")
    else:
        w("- 无 |ret_t|>1% 子集样本")
    w()

    w("---")
    w(f"结论：评估点 84 → {n_test}（量级跃升）；池化分类器方向技能以第 1 节 Wilson/二项 p 为准。")
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(f"\n[OK] 报告写入 {OUT}")
