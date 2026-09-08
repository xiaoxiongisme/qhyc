"""机器学习类模型（PRD §5.2 + §16.4 传导特征扩展）

共同特征构造（§4.4 根数窗口 + §16.3 传导特征 v1）：
- 基础：滞后收益 [1,2,3,5,10,20]
- 传导（extra，可选）：sector_ret_1d/5d/20d, sector_excess,
  upstream_ret_lag1/lag5, cost_gap, sector_corr_regime, te_topk
- §16.4 防共线性：仅"大类指数 + top-k 上游"，不把整个板块塞入特征
- σ = 训练残差 std
LSTM：torch 可选（Dockerfile CPU 层）；多变量输入（基础+传导）；权重周更（⑱）
"""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from app.features.transmission import FEATURE_COLS
from app.predictors.base import BaseModel, ModelOutput

LAGS = (1, 2, 3, 5, 10, 20)


def _extra_matrix(extra: pd.DataFrame | None, dates: pd.Index) -> np.ndarray | None:
    """按日期对齐传导特征矩阵（缺日期补 NaN，由模型容忍）"""
    if extra is None or extra.empty or dates is None:
        return None
    cols = [c for c in FEATURE_COLS if c in extra.columns]
    if not cols:
        return None
    sub = extra.reindex(dates)
    if sub.empty:
        return None
    return sub[cols].to_numpy(dtype=float)


def _make_dataset(
    rets: np.ndarray,
    dates: pd.Index | None = None,
    extra: pd.DataFrame | None = None,
    lags: tuple[int, ...] = LAGS,
    extra_cols: list[str] | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """监督数据集：X[t] = [滞后收益..., 传导特征...]，y[t] = ret_t"""
    max_lag = max(lags)
    xs, ys = [], []
    n = len(rets)
    for t in range(max_lag, n):
        row = [rets[t - lag] for lag in lags]
        xs.append(row)
        ys.append(rets[t])
    X = np.asarray(xs, dtype=float)
    y = np.asarray(ys, dtype=float)
    if extra is not None and dates is not None and len(dates) == n:
        cols = extra_cols or [c for c in FEATURE_COLS if c in extra.columns]
        if cols:
            sub = extra.reindex(dates)
            if not sub.empty:
                ev = sub[cols].to_numpy(dtype=float)
                # 对齐 y 的行：y[t]=ret_t，t 从 max_lag 起 → extra 行从 max_lag 截取
                ev = ev[max_lag : max_lag + len(y)]
                X = np.hstack([X, ev])
    # NaN 填 0（传导特征早期/缺数据时）
    return np.nan_to_num(X, nan=0.0), y


class _SklearnTreeBase(BaseModel):
    """sklearn 风格回归器基类（fit/predict 接口，§16.4 传导特征统一接入）"""

    def _make_estimator(self):
        raise NotImplementedError

    def predict(self, rets: np.ndarray, extra=None) -> ModelOutput:
        x = np.asarray(rets, dtype=float)[-self.window :]
        x = x[~np.isnan(x)]
        if x.size < 60:
            raise ValueError(f"{self.name}: 数据不足 {x.size}")

        dates = getattr(self, "_dates", None)
        extra_df = extra
        X, y = _make_dataset(x, dates, extra_df)
        # 审计 P1-3：树模型训练残差过拟合（区间过窄）→ 时间留出残差代表泛化误差
        n = len(y)
        n_hold = max(10, int(n * 0.2))
        est_hold = self._make_estimator()
        est_hold.fit(X[: n - n_hold], y[: n - n_hold])
        resid = y[n - n_hold :] - est_hold.predict(X[n - n_hold :])

        est = self._make_estimator()
        est.fit(X, y)
        x_next = np.asarray([[x[-lag] for lag in LAGS]])
        if extra_df is not None and not extra_df.empty:
            cols = [c for c in FEATURE_COLS if c in extra_df.columns]
            if cols:
                last_row = extra_df[cols].iloc[-1].to_numpy(dtype=float)
                x_next = np.concatenate([x_next, last_row.reshape(1, -1)], axis=1)
        x_next = np.nan_to_num(x_next, nan=0.0)
        point = float(est.predict(x_next)[0])
        # 审计 P1-3：留出残差经验分位区间
        return self._from_empirical_resid(self.name, point, resid)


class RandomForestModel(_SklearnTreeBase):
    name = "rf"

    def __init__(self, window: int = 250, n_estimators: int = 120, max_depth: int = 4):
        self.window = window
        self.n_estimators = n_estimators
        self.max_depth = max_depth

    def _make_estimator(self):
        from sklearn.ensemble import RandomForestRegressor

        return RandomForestRegressor(
            n_estimators=self.n_estimators, max_depth=self.max_depth,
            random_state=42, n_jobs=-1,
        )


class GPRModel(BaseModel):
    """高斯过程回归（PRD §5.2：预测 + 不确定性区间）——窗口收缩控制 O(n³) 成本"""

    name = "gpr"

    def __init__(self, window: int = 60):
        self.window = window

    def predict(self, rets: np.ndarray, extra=None) -> ModelOutput:
        from sklearn.gaussian_process import GaussianProcessRegressor
        from sklearn.gaussian_process.kernels import RBF, ConstantKernel, WhiteKernel

        x = np.asarray(rets, dtype=float)[-self.window :]
        x = x[~np.isnan(x)]
        if x.size < 60:
            raise ValueError(f"gpr: 数据不足 {x.size}")

        dates = getattr(self, "_dates", None)
        X, y = _make_dataset(x, dates, extra)
        kernel = ConstantKernel(1.0) * RBF(length_scale=5.0) + WhiteKernel(noise_level=1.0)
        gpr = GaussianProcessRegressor(kernel=kernel, normalize_y=True, random_state=42)
        gpr.fit(X, y)
        x_next = np.asarray([[x[-lag] for lag in LAGS]])
        if extra is not None and not extra.empty:
            cols = [c for c in FEATURE_COLS if c in extra.columns]
            if cols:
                last_row = extra[cols].iloc[-1].to_numpy(dtype=float)
                x_next = np.concatenate([x_next, last_row.reshape(1, -1)], axis=1)
        x_next = np.nan_to_num(x_next, nan=0.0)
        point, std = gpr.predict(x_next, return_std=True)
        return self._from_point_dist(self.name, float(point[0]), float(std[0]))


class XGBoostModel(_SklearnTreeBase):
    name = "xgb"

    def __init__(self, window: int = 250, n_estimators: int = 150, max_depth: int = 3):
        self.window = window
        self.n_estimators = n_estimators
        self.max_depth = max_depth

    def _make_estimator(self):
        from xgboost import XGBRegressor

        return XGBRegressor(
            n_estimators=self.n_estimators, max_depth=self.max_depth,
            learning_rate=0.05, subsample=0.8, colsample_bytree=0.8,
            random_state=42, n_jobs=2,  # 容器内 -1 会线程争用（实测 27s→<1s）
            verbosity=0,
        )


class LSTMModel(BaseModel):
    """LSTM（torch，可选）：⑱ 每周重训一次，多变量输入（基础+传导特征）

    审计 P1 泄漏根治：权重快照带 train_until，回测点只加载 train_until <= 评估日
    的最新快照；无合格快照 → 该点 LSTM 诚实缺席（不再用"未来训练"的权重）。
    engine 层通过 m.symbol / m._dates / m._extra / m._eval_date 注入。
    """

    name = "lstm"

    def __init__(self, window: int = 250, seq_len: int = 20):
        self.window = window
        self.seq_len = seq_len
        self.symbol: str | None = None
        self._dates = None
        self._extra = None
        self._eval_date = None   # 回测评估日（None=线上用最新权重）

    _STATE_CACHE: dict[str, tuple] = {}  # path -> (mtime, state)  回测提速

    def _select_weight_path(self, weight_dir: str) -> str | None:
        """选择权重文件：回测模式选 train_until <= 评估日 的最新快照"""
        import glob

        if self._eval_date is None:
            cur = os.path.join(weight_dir, f"{self.symbol}.pt")
            return cur if os.path.exists(cur) else None
        eval_d = (
            self._eval_date
            if not isinstance(self._eval_date, str)
            else pd.Timestamp(self._eval_date).date()
        )
        best: tuple | None = None
        for f in glob.glob(os.path.join(weight_dir, f"{self.symbol}__*.pt")):
            stem = os.path.basename(f)[: -len(".pt")].split("__")[-1]
            try:
                tu = pd.Timestamp(stem).date()
            except Exception:
                continue
            if tu <= eval_d and (best is None or tu > best[0]):
                best = (tu, f)
        return best[1] if best else None

    def predict(self, rets: np.ndarray, extra=None) -> ModelOutput:
        import os

        x = np.asarray(rets, dtype=float)[-self.window :]
        x = x[~np.isnan(x)]
        if x.size < 60:
            raise ValueError(f"lstm: 数据不足 {x.size}")
        if not self.symbol:
            raise ValueError("lstm: 未注入 symbol")

        weight_dir = os.getenv("LSTM_WEIGHT_DIR", "/app/runtime/lstm")
        weight_path = self._select_weight_path(weight_dir)
        if not weight_path:
            if self._eval_date is not None:
                raise ValueError(
                    f"lstm: 评估日 {self._eval_date} 之前无合格权重快照（诚实缺席，防泄漏）"
                )
            raise ValueError(f"lstm: 权重文件缺失（等待周更训练）")

        try:
            import torch  # type: ignore
        except ImportError as e:
            raise ValueError("lstm: torch 未安装") from e

        from app.predictors.lstm_train import predict_with_weights

        mtime = os.path.getmtime(weight_path)
        cached = LSTMModel._STATE_CACHE.get(weight_path)
        if cached is None or cached[0] != mtime:
            state = torch.load(weight_path, map_location="cpu", weights_only=True)
            LSTMModel._STATE_CACHE[weight_path] = (mtime, state)
        else:
            state = cached[1]

        point = predict_with_weights(
            state["model_state"], x, self.seq_len,
            state.get("norm"), state.get("extra_cols"),
            self._dates, extra,
        )
        sigma = float(state.get("resid_std", 1.0))
        return self._from_point_dist(self.name, point, sigma)