"""
预测服务（M2，PRD §5.3 / §5.4）

- Hurst 状态门控选择模型子集
- 方向加权投票（M2 等权起步；M3 起按 60 日回测准确率月更，⑳）
- 幅度分位融合（中位数点估计 + [P5, P95] 区间）
- 输出 §5.3 JSON 契约 v1，落库 prediction_result（同 as_of 多 run 可追溯）
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import logger
from app.features.pipeline import build_features
from app.ingest.calendar_infer import INF_EXCHANGE
from app.models import PredictionResult, TradeCalendar
from app.predictors import MODEL_REGISTRY, ModelOutput

TZ = ZoneInfo("Asia/Shanghai")


def next_trade_date(session: Session, after: date) -> date:
    """下一交易日（⑨）：官方/推断日历 → 兜底跳过周末"""
    row = session.execute(
        select(TradeCalendar.trade_date)
        .where(
            TradeCalendar.is_open.is_(True),
            TradeCalendar.trade_date > after,
        )
        .order_by(TradeCalendar.trade_date)
        .limit(1)
    ).first()
    if row:
        return row[0]
    d = after + timedelta(days=1)
    while d.weekday() >= 5:  # Sat/Sun
        d += timedelta(days=1)
    return d


def _ensemble(
    symbol: str,
    state: str,
    outputs: list[ModelOutput],
    weights: dict[str, float],
    run_id: str,
    target_date: date,
    as_of_ts: datetime,
    source: str,
    features: dict,
    hurst: float | None,
    sampen: float | None,
    model_errors: list[str],
    save: bool,
    session: Session | None,
) -> dict:
    """加权投票 + 分位融合 → §5.3 契约"""
    # §18.2（v1.3）：未发信号模型（如 reversal 门控内）不参与投票与幅度融合
    active = [o for o in outputs if o.signaled]
    if not active:
        raise ValueError(f"{symbol} 无可用模型输出: {model_errors}")

    # 方向加权投票（⑫ 涨/跌二分类）
    up_w = 0.0
    total_w = 0.0
    for o in active:
        w = float(weights.get(o.name, 1.0))
        total_w += w
        up_w += w * (o.prob if o.direction == "up" else 1 - o.prob)
    p_up = up_w / total_w if total_w > 0 else 0.5
    direction = "up" if p_up >= 0.5 else "down"
    direction_prob = p_up if direction == "up" else 1 - p_up

    # 幅度分位融合：中位数点估计 + 中位数 P5/P95（仅 signaled 模型）
    points = sorted(o.ret_point for o in active)
    lows = sorted(o.ret_low for o in active)
    highs = sorted(o.ret_high for o in active)
    ret_point = points[len(points) // 2]
    ret_low = lows[len(lows) // 2]
    ret_high = highs[len(highs) // 2]

    # §18.7（v1.3.2）M6c：波动率三件套——取有 vol_forecast 的模型中位数
    vols = [o for o in active if o.vol_forecast is not None]
    vol_point = vol_low = vol_high = None
    if vols:
        vol_point = float(sorted(o.vol_forecast for o in vols)[len(vols) // 2])
        vol_low = float(sorted(o.vol_low for o in vols if o.vol_low is not None)[
            max(0, len([o for o in vols if o.vol_low is not None]) // 2)
        ]) if any(o.vol_low is not None for o in vols) else None
        vol_high = float(sorted(o.vol_high for o in vols if o.vol_high is not None)[
            max(0, len([o for o in vols if o.vol_high is not None]) // 2)
        ]) if any(o.vol_high is not None for o in vols) else None

    # 置信度：方向概率 + 样本熵修正（低熵更可信，M3 完整化）
    base_conf = (direction_prob - 0.5) * 2  # [0,1]
    conf = base_conf
    if sampen is not None:
        # 样本熵高（不可预测）→ 折减；样本熵范围经验 [0.2, 2.5]
        entropy_factor = max(0.5, 1.0 - max(0.0, sampen - 1.0) * 0.3)
        conf *= entropy_factor
    confidence = round(min(max(conf, 0.0), 0.99), 4)

    out = {
        "schema_version": 1,
        "symbol": symbol,
        "target_date": target_date.isoformat(),
        "as_of": as_of_ts.isoformat(),
        "run_id": run_id,
        "caliber": get_settings().yaml.backtest.label_metric,  # §17 工单①：口径声明
        "direction": direction,
        "direction_prob": round(float(direction_prob), 4),
        "ret_point": round(float(ret_point), 4),
        "ret_low": round(float(ret_low), 4),
        "ret_high": round(float(ret_high), 4),
        "confidence": confidence,
        # §18.7（v1.3.2）M6c：波动率预测（σ_t + |ret| P5/P95）；无 GARCH 时为 null
        "vol_point": round(vol_point, 4) if vol_point is not None else None,
        "vol_low": round(vol_low, 4) if vol_low is not None else None,
        "vol_high": round(vol_high, 4) if vol_high is not None else None,
        "participated_models": [o.name for o in active],
        "signaled": {o.name: o.signaled for o in outputs if not o.signaled},  # §18.2 门控留痕
        "state": state,
        # 附加诊断（非契约字段，便于看板/排查）
        "_meta": {
            "source": source,
            "hurst": round(hurst, 4) if hurst is not None else None,
            "sample_entropy": round(sampen, 4) if sampen is not None else None,
            "model_errors": model_errors,
            "features": features,
        },
    }

    if save and session is not None:
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        values = {
            "run_id": run_id,
            "symbol": symbol,
            "target_date": target_date,
            "as_of_date": as_of_ts.date(),
            "as_of_ts": as_of_ts,
            "model_set": get_settings().yaml.predict.model_set,
            "direction": direction,
            "direction_prob": round(direction_prob, 4),
            "ret_point": round(ret_point, 4),
            "ret_low": round(ret_low, 4),
            "ret_high": round(ret_high, 4),
            "confidence": confidence,
            "vol_point": round(vol_point, 4) if vol_point is not None else None,   # §18.7
            "vol_low": round(vol_low, 4) if vol_low is not None else None,
            "vol_high": round(vol_high, 4) if vol_high is not None else None,
            "state": state,
            "participated_models": [o.name for o in outputs],
            "schema_version": 1,
            "caliber": get_settings().yaml.backtest.label_metric,  # §17 工单①
        }
        stmt = pg_insert(PredictionResult).values(**values)
        # run_id 含秒级时间戳：同 as_of 多次运行生成新 run_id，天然可追溯不冲突
        session.execute(stmt)
        session.commit()
    return out


def _run_models(
    symbol: str,
    state: str,
    rets,
    weights: dict,
    gate_models: dict[str, list[str]],
    dates=None,
    extra=None,
    eval_date=None,
    doi: float | None = None,
) -> tuple[list[ModelOutput], list[str]]:
    """按门控启用子集跑模型；单模型失败降级跳过"""
    enabled = gate_models.get(state) or list(MODEL_REGISTRY.keys())
    outputs: list[ModelOutput] = []
    errors: list[str] = []
    for name in enabled:
        cls = MODEL_REGISTRY.get(name)
        if cls is None:
            errors.append(f"unknown model {name}")
            continue
        try:
            m = cls()
            if name == "lstm":
                m.symbol = symbol       # LSTM 按品种加载周更权重
                m._dates = dates        # 多变量输入（§16.4 传导特征）
                m._extra = extra
                m._eval_date = eval_date  # 审计 P1：回测按 train_until<=评估日 选快照
            elif name in ("rf", "xgb", "gpr"):
                m._dates = dates        # sklearn 系特征集统一加入传导特征（§16.4）
            elif name == "reversal":
                m._doi = doi  # §18.2 Δoi 软上调（可 None）
            outputs.append(m.predict(rets, extra=extra))
        except Exception as e:
            errors.append(f"{name}: {e}")
            logger.warning(f"[predict] 模型 {name} 失败: {e}")
    return outputs, errors


def _ensemble_output(outputs: list[ModelOutput], weights: dict[str, float]) -> ModelOutput | None:
    """融合策略输出（与 §5.3 集成同规则），供回测作为 ensemble 基准"""
    # §18.2（v1.3）：仅 signaled 模型参与（reversal 门控内"无观点"被排除）
    active = [o for o in outputs if o.signaled]
    if not active:
        return None
    up_w = total_w = 0.0
    for o in active:
        w = float(weights.get(o.name, 1.0))
        total_w += w
        up_w += w * (o.prob if o.direction == "up" else 1 - o.prob)
    p_up = up_w / total_w if total_w > 0 else 0.5
    direction = "up" if p_up >= 0.5 else "down"
    prob = p_up if direction == "up" else 1 - p_up
    points = sorted(o.ret_point for o in active)
    lows = sorted(o.ret_low for o in active)
    highs = sorted(o.ret_high for o in active)
    return ModelOutput(
        name="ensemble",
        direction=direction,
        prob=float(prob),
        ret_point=float(points[len(points) // 2]),
        ret_low=float(lows[len(lows) // 2]),
        ret_high=float(highs[len(highs) // 2]),
    )


def predict_symbol(
    session: Session,
    symbol: str,
    as_of_date: date | None = None,
    save: bool = True,
) -> dict:
    """单品种预测（§5.4 POST /predict + §16.3 传导特征注入）"""
    settings = get_settings()
    cfg = settings.yaml.predict

    snap = build_features(session, symbol)
    as_of_date = as_of_date or snap.last_date
    as_of_ts = datetime.now(TZ)
    run_id = f"{as_of_date:%Y%m%d}_{as_of_ts:%H%M%S}_{cfg.model_set}"
    target_date = next_trade_date(session, as_of_date)

    # ⑳ 权重来源：model_weights 表（月更）优先，回落 config 等权
    weights = dict(cfg.weights)
    try:
        from app.backtest.weights import get_model_weights

        db_w = get_model_weights(session)
        if db_w:
            weights.update(db_w)
    except Exception as e:
        logger.debug(f"[predict] DB 权重读取失败（回落 config）: {e}")

    # §16.3 传导特征 v1（全部滞后数据，防前视）
    tframe = pd.DataFrame()
    tmeta: dict = {}
    try:
        from app.features.transmission import build_transmission_frame, transmission_snapshot

        tframe = build_transmission_frame(session, symbol, end=as_of_date)
        tmeta = transmission_snapshot(session, symbol, end=as_of_date)
    except Exception as e:
        logger.warning(f"[predict] {symbol} 传导特征构建失败（跳过，仅用单品种特征）: {e}")

    outputs, errors = _run_models(
        symbol, snap.state, snap.rets, weights, cfg.gate_models,
        dates=snap.dates, extra=tframe, doi=snap.doi,
    )
    result = _ensemble(
        symbol=symbol,
        state=snap.state,
        outputs=outputs,
        weights=weights,
        run_id=run_id,
        target_date=target_date,
        as_of_ts=as_of_ts,
        source=snap.source,
        features=snap.features,
        hurst=snap.hurst,
        sampen=snap.sample_entropy,
        model_errors=errors,
        save=save,
        session=session,
    )
    # §16.6 v2 预留 drivers 字段（契约 v1 不变，向后兼容）
    result["drivers"] = tmeta
    result["_meta"]["transmission"] = tmeta
    logger.info(
        f"[predict] {symbol} -> {result['direction']} p={result['direction_prob']} "
        f"point={result['ret_point']} [{result['ret_low']},{result['ret_high']}] "
        f"models={result['participated_models']} state={snap.state} run={run_id}"
    )
    return result


def predict_symbols(
    session: Session,
    symbols: list[str] | None = None,
    as_of_date: date | None = None,
    save: bool = True,
) -> list[dict]:
    """批量预测（§5.4 POST /predict/batch，用于看板热力图）

    §16.1：金融期货（sector_map.active=false）默认不纳入预测范围。
    §16.2 第 3 层：先层级预测大类指数，结果作为品种级特征（sector_pred_prob）。
    """
    settings = get_settings()
    if not symbols:
        symbols = [s.symbol for s in settings.main_contracts]

    # 金融期货排除（§16.1 开关）
    from app.sectors.builder import predict_enabled_products

    allowed = set(predict_enabled_products(session))
    excluded = [s for s in symbols if s not in allowed]
    symbols = [s for s in symbols if s not in excluded]
    if excluded:
        logger.info(f"[predict] 排除非预测范围品种（§16.1）: {excluded}")

    # §16.2 第 3 层：层级预测（大类指数先行，落库 IDX:*，品种预测读取）
    try:
        predict_sector_indices(session, as_of_date=as_of_date)
    except Exception as e:
        logger.warning(f"[predict] 层级预测（大类指数）失败，sector_pred_prob 置空: {e}")

    results: list[dict] = []
    for sym in symbols:
        try:
            results.append(
                predict_symbol(session, sym, as_of_date=as_of_date, save=save)
            )
        except Exception as e:
            logger.warning(f"[predict] {sym} 失败: {e}")
            results.append({"symbol": sym, "error": str(e)})
    return results


def predict_sector_indices(session: Session, as_of_date: date | None = None) -> list[dict]:
    """§16.2 第 3 层：用同一模型库预测各大类指数

    - 输入：sector_index.ret_1d 序列（合成指数收益，天然平滑无换月跳空）
    - 输出：§5.3 结构，symbol='IDX:{sector}' 落库（同 as_of 可追溯）
    - 品种级预测通过 drivers.sector_pred_prob 引用（防前视：仅用 as_of 之前数据）
    """
    from app.models import SectorIndex

    cfg = get_settings().yaml.predict
    as_of_ts = datetime.now(TZ)

    sectors = session.execute(
        select(SectorIndex.sector).distinct().order_by(SectorIndex.sector)
    ).scalars().all()

    outputs_all: list[dict] = []
    for sector in sectors:
        rows = session.execute(
            select(SectorIndex.trade_date, SectorIndex.ret_1d)
            .where(SectorIndex.sector == sector, SectorIndex.ret_1d.is_not(None))
            .order_by(SectorIndex.trade_date.desc())
            .limit(cfg.history_bars)
        ).all()
        if len(rows) < cfg.min_bars:
            logger.info(f"[sector-predict] {sector} 指数样本 {len(rows)} 不足，跳过")
            continue
        rows = rows[::-1]
        dates = pd.Index([r[0] for r in rows])
        rets = np.asarray([float(r[1]) for r in rows])

        # Hurst 门控（与品种同一套规则）
        from app.features.complexity import hurst_exponent, market_state, sample_entropy

        h = hurst_exponent(rets[-cfg.hurst.window:])
        sampen = sample_entropy(rets[-cfg.hurst.window:])
        state = market_state(h, cfg.hurst.trend_threshold, cfg.hurst.mean_revert_threshold)

        symbol = f"IDX:{sector}"
        as_of_d = as_of_date or dates[-1]
        run_id = f"{as_of_d:%Y%m%d}_{as_of_ts:%H%M%S}_{cfg.model_set}_idx"
        target_date = next_trade_date(session, dates[-1])

        outputs, errors = _run_models(
            symbol, state, rets, cfg.weights, cfg.gate_models,
            dates=dates, extra=None,   # 指数层无自身上游（跨板块传导 M3+ 扩展）
        )
        if not outputs:
            logger.warning(f"[sector-predict] {sector} 无可用模型输出: {errors}")
            continue

        result = _ensemble(
            symbol=symbol,
            state=state,
            outputs=outputs,
            weights=cfg.weights,
            run_id=run_id,
            target_date=target_date,
            as_of_ts=as_of_ts,
            source="sector_index",
            features={"hurst": h, "sample_entropy": sampen},
            hurst=h,
            sampen=sampen,
            model_errors=errors,
            save=True,
            session=session,
        )
        outputs_all.append(result)
        logger.info(
            f"[sector-predict] {symbol} -> {result['direction']} p={result['direction_prob']} "
            f"state={state} target={target_date}"
        )
    return outputs_all


def latest_sector_prediction(session: Session, sector: str) -> dict | None:
    """读取某板块最新的指数预测结果（品种级 sector_pred_prob 特征来源）"""
    symbol = f"IDX:{sector}"
    row = session.execute(
        select(PredictionResult)
        .where(PredictionResult.symbol == symbol)
        .order_by(PredictionResult.as_of_ts.desc())
        .limit(1)
    ).scalar()
    if not row:
        return None
    return {
        "direction": row.direction,
        "direction_prob": float(row.direction_prob) if row.direction_prob is not None else None,
        "target_date": row.target_date.isoformat(),
        "run_id": row.run_id,
        "state": row.state,
    }
