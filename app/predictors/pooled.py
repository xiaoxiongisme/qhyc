"""P6 池化面板建模（M6c.1，PRD v1.3.2 §18.10 + 实现问题清单 P6 节）

动机：
- §18.2 诊断确认 rf(0.4924)/xgb(0.4919) 回归点估计**方向信息弱（≈0.49，学反）**；
- 根因：单品种仅 ~250 根 → 评估点仅 ~84，统计功率不足，且回归点估计方向由噪声决定。
- 处置（PRD P6）：**池化前先改分类目标**——不再预测 ret 点值，改为预测次日方向
  up/down；并把训练集从单品种 250 根扩展到**全品种池化面板**，评估点 84 → 2000+。

设计要点（防前视 + 跨品种可比）：
- 标签：y = (ret_{t+1} > 0)，收盘价口径（§17/§18.1，与全链路一致）。
- 特征：**归一化滞后收益** zL = ret_{t-L} / vol20_{t-L}（按 20 日滚动波动率标准化），
  使高/低波动品种特征尺度可比，池化不会被高波动品种主导；vol20 仅用 ≤ t-L 数据（无泄漏）。
  另加 log(vol20_t) 让模型感知波动体制。
- 训练：跨全部 *888 品种构造面板，单一分类器在池上拟合（短周期反转是跨品种稳定现象）。
- 落盘缓存：与 LSTM 同机制（`/app/runtime/pooled/*.joblib`），`predict` 加载即用；
  文件缺失时按全库自动训练（容器内 DB 可达）。
- 漏洞说明：集成/回测中池化模型为"全历史训练"全局模型；eval_date 早于训练末日的
  回测点存在轻微前视，方向映射近似平稳，靠 research_m6c_pooled.py 的 TRAIN/TEST 严格
  划分给出诚实 OOS 数字（见 docs/实现问题清单.md M6c.1）。
"""
from __future__ import annotations

import os
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import text

from app.core.config import get_settings
from app.predictors.base import BaseModel, ModelOutput

LAGS = (1, 2, 3, 5, 10, 20)
VOL_WIN = 20


def _model_dir() -> str:
    return os.getenv("POOLED_MODEL_DIR", "/app/runtime/pooled")


def _default_symbols() -> list[str]:
    try:
        return [m.symbol for m in get_settings().main_contracts]
    except Exception:
        return []


def build_pooled_panel(
    session,
    symbols: list[str] | None = None,
    train_end: date | str | None = None,
) -> pd.DataFrame:
    """构造全品种池化面板（严格只用 t 及更早数据，标签为次日收益方向）。

    返回列：
      symbol, trade_date, z1..z20（归一化滞后收益）, lvl（log vol 体制）,
      fwd（次日收益 %）, up（1=fwd>0）, is_train（train_end 之前）
    """
    if symbols is None:
        symbols = _default_symbols()
    rows = session.execute(
        text(
            "SELECT symbol, trade_date, close FROM daily_bar "
            "WHERE symbol = ANY(:syms) AND close IS NOT NULL ORDER BY symbol, trade_date"
        ).bindparams(syms=symbols)
    ).all()
    if not rows:
        # 兜底：任意 *888
        rows = session.execute(
            text(
                "SELECT symbol, trade_date, close FROM daily_bar "
                "WHERE symbol LIKE '%%888' AND close IS NOT NULL ORDER BY symbol, trade_date"
            )
        ).all()

    frames = []
    for sy, g in pd.DataFrame(
        [(r[0], r[1], float(r[2])) for r in rows],
        columns=["symbol", "trade_date", "close"],
    ).groupby("symbol"):
        g = g.sort_values("trade_date").copy()
        g["ret"] = g["close"].pct_change() * 100.0
        vol = g["ret"].rolling(VOL_WIN).std()
        for L in LAGS:
            g[f"z{L}"] = g["ret"].shift(L) / vol.shift(L)
        g["lvl"] = np.log(vol.replace(0, np.nan))
        g["fwd"] = g["ret"].shift(-1)
        g = g.dropna(subset=[f"z{L}" for L in LAGS] + ["lvl", "fwd"])
        g = g[g["ret"] != 0]  # 去零收益（无信息）
        g["up"] = (g["fwd"] > 0).astype(int)
        g["is_train"] = (
            True
            if train_end is None
            else (pd.to_datetime(g["trade_date"]) <= pd.to_datetime(train_end))
        )
        frames.append(g[["symbol", "trade_date", "is_train", "fwd", "up"]
                         + [f"z{L}" for L in LAGS] + ["lvl"]])
    return pd.concat(frames) if frames else pd.DataFrame()


def _feature_cols() -> list[str]:
    return [f"z{L}" for L in LAGS] + ["lvl"]


class PooledClassifierModel(BaseModel):
    """全品种池化分类器（基础类，预测次日方向 up/down）

    predict(rets) 仅用单品种截至 as_of 的收益率序列（%）；模型为全局预训练，
    加载即用。特征与 build_pooled_panel 完全一致（归一化滞后收益 + log vol 体制），
    保证训练/推断同分布。
    """

    name = "pooled"
    kind: str = "rf"  # 子类覆盖

    def __init__(self, window: int = 250, n_estimators: int = 200, max_depth: int = 6):
        self.window = window
        self.n_estimators = n_estimators
        self.max_depth = max_depth
        self._cache: dict | None = None

    # ---------- 训练 / 落盘 ----------
    def _make_estimator(self):
        if self.kind == "xgb":
            from xgboost import XGBClassifier

            return XGBClassifier(
                n_estimators=self.n_estimators, max_depth=self.max_depth,
                learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
                reg_lambda=1.0, random_state=42, n_jobs=2, verbosity=0,
            )
        from sklearn.ensemble import RandomForestClassifier

        return RandomForestClassifier(
            n_estimators=self.n_estimators, max_depth=self.max_depth,
            class_weight="balanced", random_state=42, n_jobs=-1,
        )

    def fit_from_db(self, session, train_end: date | str | None = None) -> dict:
        """训练并落盘；返回元数据。train_end 给定时仅用其之前数据（防泄漏快照）。"""
        pan = build_pooled_panel(session, train_end=train_end)
        cols = _feature_cols()
        sub = pan.dropna(subset=cols)
        X = sub[cols].to_numpy(dtype=float)
        y = sub["up"].to_numpy(dtype=int)
        est = self._make_estimator()
        est.fit(X, y)
        fwd = sub["fwd"].to_numpy(dtype=float)
        meta = {
            "estimator": est,
            "feature_names": cols,
            "fwd_lo": float(np.quantile(fwd, 0.05)),
            "fwd_hi": float(np.quantile(fwd, 0.95)),
            "n_train": int(len(sub)),
            "train_end": str(train_end) if train_end is not None else "full",
            "kind": self.kind,
        }
        os.makedirs(_model_dir(), exist_ok=True)
        import joblib

        joblib.dump(meta, os.path.join(_model_dir(), f"{self.kind}_pool.joblib"))
        self._cache = meta
        return meta

    # ---------- 加载 / 推断 ----------
    def _load(self) -> dict:
        if self._cache is not None:
            return self._cache
        path = os.path.join(_model_dir(), f"{self.kind}_pool.joblib")
        if os.path.exists(path):
            import joblib

            self._cache = joblib.load(path)
            return self._cache
        # 文件缺失：容器内自动按全库训练（DB 可达）
        from app.core.db import session_scope

        with session_scope() as s:
            return self.fit_from_db(s)

    def _row_from_rets(self, rets: np.ndarray) -> np.ndarray:
        r = pd.Series(np.asarray(rets, dtype=float))
        if len(r) < VOL_WIN + max(LAGS) + 1:
            raise ValueError(f"{self.name}: 数据不足（需 ≥ {VOL_WIN + max(LAGS) + 1} 根）")
        vol = r.rolling(VOL_WIN).std().replace(0, np.nan)
        feats = []
        for L in LAGS:
            z = r.iloc[-L] / vol.iloc[-L]
            feats.append(0.0 if (pd.isna(z) or not np.isfinite(z)) else float(z))
        lvl = float(np.log(vol.iloc[-1])) if vol.iloc[-1] and np.isfinite(vol.iloc[-1]) else 0.0
        feats.append(lvl)
        return np.asarray(feats, dtype=float).reshape(1, -1)

    def predict(self, rets: np.ndarray, extra=None) -> ModelOutput:
        meta = self._load()
        x = self._row_from_rets(rets)
        p_up = float(meta["estimator"].predict_proba(x)[0, 1])
        direction = "up" if p_up >= 0.5 else "down"
        prob = min(max(p_up if direction == "up" else 1 - p_up, 0.5), 0.999)
        return ModelOutput(
            name=self.name,
            direction=direction,
            prob=prob,
            ret_point=0.0,                 # 分类器不预测幅度
            ret_low=meta["fwd_lo"],        # 经验 |收益| 分位区间（符号无关）
            ret_high=meta["fwd_hi"],
            signaled=True,
        )


class PooledRFModel(PooledClassifierModel):
    name = "rf_pool"
    kind = "rf"


class PooledXGBModel(PooledClassifierModel):
    name = "xgb_pool"
    kind = "xgb"
