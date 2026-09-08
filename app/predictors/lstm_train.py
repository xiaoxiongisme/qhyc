"""LSTM 网络与训练/推理工具（M3 + §16.4 多变量传导特征，⑱ 每周重训）

- 输入：seq_len=20 × (1 + n_extra) 特征序列（基础收益归一 + 传导特征 v1 各列归一）
- 权重目录：/app/runtime/lstm/{symbol}.pt（api/scheduler 共享卷）
- 防前视：extra 各列已是滞后口径（§16.3）
"""
from __future__ import annotations

import os
from datetime import date

import numpy as np
import pandas as pd


def _ensure_torch():
    try:
        import torch  # type: ignore

        return torch
    except ImportError as e:
        raise RuntimeError("torch 未安装（镜像构建可选层），LSTM 不可用") from e


def LSTMNet(torch, seq_len: int = 20, n_features: int = 1, hidden: int = 32):
    """多变量 LSTM（input_size = n_features）"""
    from torch import nn  # type: ignore

    class _Net(nn.Module):
        def __init__(self):
            super().__init__()
            self.lstm = nn.LSTM(input_size=n_features, hidden_size=hidden, num_layers=2, batch_first=True)
            self.head = nn.Linear(hidden, 1)

        def forward(self, xb):  # xb: [B, seq_len, n_features]
            out, _ = self.lstm(xb)
            return self.head(out[:, -1, :]).ravel()

    return _Net()


def _make_multivariate(
    rets: np.ndarray,
    dates: pd.Index | None,
    extra: pd.DataFrame | None,
    seq_len: int,
    extra_cols: list[str] | None = None,
) -> tuple[np.ndarray, np.ndarray, dict, list[str]]:
    """构造多变量序列：ch0 = 归一收益；ch1..k = 归一传导特征列"""
    rets = np.asarray(rets, dtype=float)
    mu, sd = float(np.mean(rets)), float(np.std(rets, ddof=1) or 1.0)
    channels = [(rets - mu) / sd]
    used_cols: list[str] = []

    if extra is not None and dates is not None and len(dates) == len(rets):
        cols = extra_cols or [c for c in extra.columns]
        sub = extra.reindex(dates)
        if not sub.empty:
            for c in cols:
                if c not in sub.columns:
                    continue
                v = sub[c].to_numpy(dtype=float)
                if np.all(np.isnan(v)):
                    continue
                s = float(np.nanstd(v, ddof=1)) if np.isfinite(v).sum() > 2 else 1.0
                m = float(np.nanmean(v)) if np.isfinite(v).any() else 0.0
                sdv = s if s > 1e-9 else 1.0
                zn = (v - m) / sdv
                zn = np.nan_to_num(zn, nan=0.0)
                channels.append(zn)
                used_cols.append(c)

    mat = np.vstack(channels).T.astype(np.float32)  # [T, n_features]
    X, y = [], []
    for t in range(seq_len, len(rets)):
        X.append(mat[t - seq_len : t])
        y.append((rets[t] - mu) / sd)
    X = np.asarray(X, dtype=np.float32)
    y = np.asarray(y, dtype=np.float32)
    norm = {"ret_mu": mu, "ret_sd": sd, "extras": {c: {"col_i": i + 1} for i, c in enumerate(used_cols)}}
    return X, y, norm, used_cols


def train_symbol(
    torch,
    symbol: str,
    rets: np.ndarray,
    dates: pd.Index | None = None,
    extra: pd.DataFrame | None = None,
    seq_len: int = 20,
    epochs: int = 30,
    update_current: bool = True,
) -> dict:
    """训练单品种多变量 LSTM 并保存权重

    update_current=False：仅写带 train_until 的快照（回测用），不覆盖线上权重。
    """
    from torch.utils.data import DataLoader, TensorDataset  # type: ignore

    rets = np.asarray(rets, dtype=float)[-250:]
    if rets.size < 60:
        return {"symbol": symbol, "skipped": f"数据不足 {rets.size}"}
    if dates is not None:
        dates = dates[-len(rets):]
    rets_dates = dates  # train_until 依据（审计 P1：快照带训练截止日）

    X, y, norm, used_cols = _make_multivariate(rets, dates, extra, seq_len)
    Xt = torch.tensor(X)
    yt = torch.tensor(y).ravel()
    loader = DataLoader(TensorDataset(Xt, yt), batch_size=32, shuffle=True)

    net = LSTMNet(torch, seq_len, n_features=X.shape[2])
    opt = torch.optim.Adam(net.parameters(), lr=1e-3)
    loss_fn = torch.nn.MSELoss()
    net.train()
    for _ in range(epochs):
        for xb, yb in loader:
            opt.zero_grad()
            loss = loss_fn(net(xb), yb)
            loss.backward()
            opt.step()

    net.eval()
    with torch.no_grad():
        pred_z = net(Xt).numpy()
    resid = (y - pred_z) * norm["ret_sd"]

    out_dir = os.getenv("LSTM_WEIGHT_DIR", "/app/runtime/lstm")
    os.makedirs(out_dir, exist_ok=True)
    state = {
        "model_state": net.state_dict(),
        "norm": norm,
        "extra_cols": used_cols,
        "seq_len": seq_len,
        "resid_std": float(np.std(resid, ddof=1)),
        "trained_at": str(date.today()),
        "train_until": str(rets_dates[-1]) if (rets_dates is not None and len(rets_dates) > 0) else str(date.today()),
        "bars": int(rets.size),
    }
    # 当前权重（线上预测用；回测快照模式 update_current=False 不覆盖）
    path = os.path.join(out_dir, f"{symbol}.pt")
    if update_current:
        torch.save(state, path)
    # 快照（审计 P1：回测按 train_until <= 评估日 加载，消除前视泄漏）
    snap_path = None
    if rets_dates is not None and len(rets_dates) > 0:
        snap_path = os.path.join(out_dir, f"{symbol}__{rets_dates[-1]:%Y%m%d}.pt")
        torch.save(state, snap_path)
    return {
        "symbol": symbol,
        "weights": path if update_current else snap_path,
        "snapshot": snap_path if (rets_dates is not None and len(rets_dates) > 0) else None,
        "train_until": state["train_until"],
        "bars": int(rets.size),
        "n_features": int(X.shape[2]),
        "resid_std": float(np.std(resid, ddof=1)),
    }


def predict_with_weights(
    model_state,
    rets: np.ndarray,
    seq_len: int,
    norm: dict,
    extra_cols: list[str] | None,
    dates: pd.Index | None = None,
    extra: pd.DataFrame | None = None,
) -> float:
    """用已保存权重预测下一期收益（原量纲 %）"""
    torch = _ensure_torch()
    rets = np.asarray(rets, dtype=float)
    mu, sd = norm.get("ret_mu", 0.0), norm.get("ret_sd", 1.0) or 1.0

    channels = [(rets[-seq_len:] - mu) / sd]
    if extra_cols and extra is not None and dates is not None and len(dates) == len(rets):
        sub = extra.reindex(dates)
        if not sub.empty:
            for c in extra_cols:
                if c in sub.columns:
                    v = sub[c].to_numpy(dtype=float)[-seq_len:]
                    channels.append(np.nan_to_num(v, nan=0.0))
                else:
                    channels.append(np.zeros(seq_len))
    mat = np.vstack(channels).T.astype(np.float32)[-seq_len:]

    X = torch.tensor(mat.reshape(1, seq_len, mat.shape[1]))
    net = LSTMNet(torch, seq_len, n_features=mat.shape[1])
    net.load_state_dict(model_state)
    net.eval()
    with torch.no_grad():
        pred_z = float(net(X).ravel()[0])
    return pred_z * sd + mu