"""
M4 回测引擎（PRD §6）

- 逐评估点（step 间隔）构造"截至当日"特征（防前视：①），跑门控内全部模型，
  与**次日真实涨跌**（ret_close，t→t+1）对比
- 指标 per model：方向准确率 dir_acc、幅度 MAE/RMSE、分位覆盖命中 quantile_hit、
  样本数、Hurst 状态分层（§6 by_state）
- 融合策略（ensemble）同步回测，作为策略基准
- 落库 backtest_result（run_id, model 主键；by_state JSON 含近 60 点准确率，⑳ 供权重月更）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import logger
from app.features.pipeline import (
    features_from_series,
    load_canonical_series,
    ret_series as ret_series_from,
)
from app.models import BacktestDetail, BacktestResult
from app.predictors import MODEL_REGISTRY, ModelOutput

TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class BacktestParams:
    window: int = 250          # ⑲ 模型输入窗口
    test_days: int = 250       # ⑲ 回测窗口（250 交易日）
    step: int = 3              # 评估点间隔（根）
    min_bars: int = 60
    lstm_retrain_every: int = 60   # M4.1：LSTM walk-forward 重训节奏（根，≈线上周训节奏的近似）


def _next_ret(session: Session, symbol: str, df: pd.DataFrame, t, metric: str) -> float | None:
    """t 的次交易日收益标签

    metric=close：t→t+1 收盘价收益（当前 canonical 口径，P2-1 待裁决）
    metric=settle：t+1 的 ret_settle（PRD-⑩ 结算价口径；当日无结算则该点跳过）
    """
    idx = df.index
    pos = idx.get_loc(t)
    if pos + 1 >= len(idx):
        return None
    if metric == "settle":
        from app.models import DailyBar

        next_d = idx[pos + 1]
        v = session.execute(
            select(DailyBar.ret_settle).where(
                DailyBar.symbol == f"{symbol[:-3]}888" if symbol.endswith("888") else DailyBar.symbol == symbol,
                DailyBar.trade_date == next_d,
            )
        ).scalar()
        return float(v) if v is not None else None
    c_t = float(df["close"].iloc[pos])
    c_n = float(df["close"].iloc[pos + 1])
    if c_t == 0:
        return None
    return (c_n - c_t) / c_t * 100.0


def backtest_symbol(
    session: Session,
    symbol: str,
    params: BacktestParams | None = None,
    run_id: str | None = None,
) -> dict:
    """单品种回测：滚动评估门控内全部模型 + ensemble"""
    p = params or BacktestParams()
    cfg = get_settings().yaml.predict
    cfg_bt = get_settings().yaml.backtest

    # P2 权重来源统一：与线上一致（model_weights 表优先，回落 config）
    weights = dict(cfg.weights)
    try:
        from app.backtest.weights import get_model_weights

        db_w = get_model_weights(session)
        if db_w:
            weights.update(db_w)
    except Exception as e:
        logger.debug(f"[backtest] DB 权重读取失败（回落 config）: {e}")

    df, source = load_canonical_series(session, symbol)
    if len(df) < p.test_days + p.min_bars:
        # 复验报告遗留 4：新品种（LG/PR/PS/LC 等）数据不足时自适应缩减评估期，
        # 而非直接跳过（仍保证 ≥60 根最小模型输入窗口）
        available = len(df) - p.min_bars
        if available < max(20, p.step):
            return {"symbol": symbol, "skipped": f"数据 {len(df)} 根，可评估期 {available} 根不足"}
        adjusted = available
        logger.info(
            f"[backtest] {symbol} 数据不足 {p.test_days} 根（仅 {len(df)}），"
            f"test_days 自适应缩减为 {adjusted}"
        )
        p = BacktestParams(
            window=p.window, test_days=adjusted, step=p.step,
            min_bars=p.min_bars, lstm_retrain_every=p.lstm_retrain_every,
        )

    # 传导特征一次构造（滞后口径，逐日切片无前视 §16.7 ①）
    try:
        from app.features.transmission import build_transmission_frame

        tframe = build_transmission_frame(
            session, symbol, end=df.index[-1], lookback=p.test_days + 140
        )
    except Exception as e:
        logger.warning(f"[backtest] {symbol} 传导特征构建失败（回退空 extra）: {e}")
        tframe = pd.DataFrame()

    # 评估点：最后 test_days+1 根内，每 step 根一个
    all_dates = df.index
    eval_dates = list(all_dates[-(p.test_days + 1) :][:: p.step])

    # 审计 P1 + M4.1：LSTM walk-forward —— 按线上重训节奏（lstm_retrain_every 根）
    # 把回测窗口分段，逐段生成"段起点前"无泄漏快照（train_until ≤ 段内全部评估日）
    if "lstm" in sum((v for v in cfg.gate_models.values()), []):
        try:
            from app.predictors.lstm_train import _ensure_torch, train_symbol as lstm_train

            retrain_every = max(p.lstm_retrain_every, p.step)
            # 重训边界：窗口内每 retrain_every 根的第一个评估点
            boundaries = eval_dates[:: max(1, retrain_every // p.step)]
            torch = _ensure_torch()
            snaps_made = 0
            for b in boundaries:
                sub0 = df[df.index < b]   # 训练数据严格早于段内首个评估日
                if len(sub0) < cfg.lstm.seq_len + 60:
                    continue
                rets_bt = ret_series_from(sub0["close"].astype(float))[-250:].dropna()
                if len(rets_bt) < 60:
                    continue
                tf_bt = (
                    tframe[tframe.index < b] if not tframe.empty else None
                )
                r = lstm_train(
                    torch, symbol, rets_bt.to_numpy(),
                    dates=rets_bt.index, extra=tf_bt,
                    seq_len=cfg.lstm.seq_len, epochs=cfg.lstm.epochs,
                    update_current=False,   # 不覆盖线上权重
                )
                snaps_made += 1
            logger.info(
                f"[backtest] {symbol} walk-forward 快照 {snaps_made} 个"
                f"（边界 {len(boundaries)} 个，节奏 {retrain_every} 根）"
            )
        except Exception as e:
            logger.warning(f"[backtest] {symbol} 回测快照生成失败（LSTM 将缺席）: {e}")

    # per-model 收集
    recs: dict[str, list[dict]] = {}
    gate_hist: list[str] = []
    errors_all: list[str] = []   # P1-4：失败点计数

    for t in eval_dates:
        sub = df[df.index <= t]
        if len(sub) < p.min_bars:
            continue
        snap = features_from_series(symbol, sub, source)
        extra = tframe[tframe.index <= t] if not tframe.empty else None

        # 次日真实收益/方向（P2-1 口径开关：close / settle）
        actual = _next_ret(session, symbol, df, t, cfg_bt.label_metric)
        if actual is None:
            continue
        actual_dir = actual > 0

        # 门控内模型（eval_date 传入：LSTM 只加载 train_until<=t 的快照，防泄漏）
        from app.engine.service import _run_models

        outputs, errors = _run_models(
            symbol, snap.state, snap.rets, weights, cfg.gate_models,
            dates=snap.dates, extra=extra, eval_date=t,
        )
        errors_all.extend(errors)
        # ensemble（同融合规则，权重与线上一致）
        if outputs:
            from app.engine.service import _ensemble_output

            ens = _ensemble_output(outputs, weights)
            if ens:
                outputs = outputs + [ens]

        gate_hist.append(snap.state)
        for o in outputs:
            # 复验报告遗留 5：point/high/low 出现 NaN 的点直接丢弃（不入库不污染聚合）
            vals = (float(o.ret_point), float(o.ret_low), float(o.ret_high))
            if any(not np.isfinite(v) for v in vals):
                continue
            recs.setdefault(o.name, []).append(
                {
                    "date": t,
                    "state": snap.state,
                    "pred_dir": o.direction,
                    "prob": float(o.prob),
                    "point": vals[0],
                    "low": vals[1],
                    "high": vals[2],
                    "actual": actual,
                    "actual_dir": actual_dir,
                }
            )

    # 指标
    def _metrics(rows: list[dict]) -> dict:
        # 复验报告遗留 5：只统计有限值行，杜绝 NaN 污染聚合
        rows = [
            r for r in rows
            if np.isfinite(r["point"]) and np.isfinite(r["actual"])
            and np.isfinite(r["low"]) and np.isfinite(r["high"])
        ]
        n = len(rows)
        if n == 0:
            return {}
        hits = [r["pred_dir"] == ("up" if r["actual_dir"] else "down") for r in rows]
        errs = [r["point"] - r["actual"] for r in rows]
        inrange = [r["low"] <= r["actual"] <= r["high"] for r in rows]
        out = {
            "sample_n": n,
            "dir_acc": round(sum(hits) / n, 4),
            "mae": round(float(np.mean(np.abs(errs))), 4),
            "rmse": round(float(np.sqrt(np.mean(np.square(errs)))), 4),
            "quantile_hit": round(sum(inrange) / n, 4),
        }
        # 近 60 评估点准确率（⑳ 权重月更输入）
        recent = hits[-60:]
        out["recent60_dir_acc"] = round(sum(recent) / len(recent), 4) if recent else None
        # Hurst 分层（§6）
        by_state: dict[str, dict] = {}
        for st in ("trend", "neutral", "mean_revert"):
            sub_rows = [r for r in rows if r["state"] == st]
            if sub_rows:
                sh = [r["pred_dir"] == ("up" if r["actual_dir"] else "down") for r in sub_rows]
                by_state[st] = {"n": len(sub_rows), "dir_acc": round(sum(sh) / len(sub_rows), 4)}
        out["by_state"] = by_state
        return out

    metrics = {name: _metrics(rows) for name, rows in recs.items()}
    gate_dist: dict[str, int] = {}
    for st in gate_hist:
        gate_dist[st] = gate_dist.get(st, 0) + 1

    # 落库（P0 修复：主键含 symbol，多品种不互相覆盖）
    run_id = run_id or f"{df.index[-1]:%Y%m%d}_{datetime.now(TZ):%H%M%S}_bt"

    # per-model 失败计数（P1-4：区分"不适用"与"崩溃"）
    error_counts: dict[str, int] = {}
    for err in errors_all:
        mname = err.split(":")[0]
        error_counts[mname] = error_counts.get(mname, 0) + 1

    from sqlalchemy.dialects.postgresql import insert as pg_insert

    # 审计 P1-3 遗留：rf/xgb/wavelet 区间未校准（M5 看板标注/隐藏依据）
    UNCALIBRATED = {"rf", "xgb", "wavelet"}

    def _clean(v):
        """NaN → None（复验遗留 5：杜绝 NaN 写入 numeric 列）"""
        if v is None:
            return None
        f = float(v)
        return round(f, 4) if np.isfinite(f) else None

    saved = 0
    detail_rows: list[dict] = []
    for name, m in metrics.items():
        if not m:
            continue
        stmt = pg_insert(BacktestResult).values(
            run_id=run_id,
            symbol=symbol,                      # P0：symbol 维度
            model=name,
            window_len=p.window,
            start_date=eval_dates[0],
            end_date=eval_dates[-1],
            dir_acc=_clean(m.get("dir_acc")),
            mae=_clean(m.get("mae")),
            rmse=_clean(m.get("rmse")),
            quantile_hit=_clean(m.get("quantile_hit")),
            sample_n=m.get("sample_n"),
            by_state={
                "states": m.get("by_state", {}),
                "recent60_dir_acc": _clean(m.get("recent60_dir_acc")),
                "gate_dist": gate_dist,
                "source": source,
                "error_counts": error_counts,   # P1-4：模型失败可见
                "interval_calibrated": name not in UNCALIBRATED,  # P1-3 遗留：看板标注
            },
        )
        # 同 run_id+symbol+model 幂等
        stmt = stmt.on_conflict_do_update(
            index_elements=["run_id", "symbol", "model"],
            set_={
                "dir_acc": stmt.excluded.dir_acc,
                "mae": stmt.excluded.mae,
                "rmse": stmt.excluded.rmse,
                "quantile_hit": stmt.excluded.quantile_hit,
                "sample_n": stmt.excluded.sample_n,
                "by_state": stmt.excluded.by_state,
            },
        )
        session.execute(stmt)
        saved += 1

        # P2-7：逐点明细
        for r in recs.get(name, []):
            detail_rows.append(
                {
                    "run_id": run_id,
                    "symbol": symbol,
                    "model": name,
                    "eval_date": r["date"],
                    "state": r["state"],
                    "pred_dir": r["pred_dir"],
                    "prob": round(r["prob"], 4),
                    "point": round(r["point"], 4),
                    "low": round(r["low"], 4),
                    "high": round(r["high"], 4),
                    "actual": round(r["actual"], 4),
                }
            )
    if detail_rows:
        session.execute(pg_insert(BacktestDetail).values(detail_rows))
    session.commit()

    summary = {
        "symbol": symbol,
        "run_id": run_id,
        "eval_points": len(eval_dates),
        "test_days_used": p.test_days,
        "source": source,
        "gate_dist": gate_dist,
        "metrics": metrics,
    }
    logger.info(
        f"[backtest] {symbol} {len(eval_dates)} 点 -> "
        + " ".join(f"{k}={v.get('dir_acc')}" for k, v in sorted(metrics.items()) if v)
    )
    return summary


def backtest_symbols(
    session: Session,
    symbols: list[str] | None = None,
    params: BacktestParams | None = None,
    run_id: str | None = None,
) -> list[dict]:
    """多品种回测"""
    settings = get_settings()
    if not symbols:
        symbols = [s.symbol for s in settings.main_contracts]
    run_id = run_id or f"{datetime.now(TZ):%Y%m%d_%H%M%S}_bt"
    results = []
    for sym in symbols:
        try:
            results.append(backtest_symbol(session, sym, params=params, run_id=run_id))
        except Exception as e:
            logger.warning(f"[backtest] {sym} 失败: {e}")
            results.append({"symbol": sym, "error": str(e)})
    return results