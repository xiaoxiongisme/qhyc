"""
M5 看板聚合 API（PRD §14）

- GET /dashboard/overview  品种总览：就绪状态 + 各品种最新预测（含 caliber/驱动因子）
- GET /dashboard/sectors   大类热力图数据：板块指数动量 + 板块内成员当日表现
- GET /dashboard/quality   数据质量：异常工单、回补进度、日历模式
- GET /backtest 已有；Wilson 区间在下钻接口补充
"""
from __future__ import annotations

from datetime import date, timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.api.routers.health import _readiness
from app.core.config import get_settings
from app.core.db import fastapi_db_dep
from app.models import (
    AnomalyTicket,
    BacktestDetail,
    BacktestResult,
    DailyBar,
    PredictionResult,
    SectorIndex,
    SectorMap,
    SpotBasis,
)

router = APIRouter()


def _wilson(p: float, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 置信区间（复验建议：dir_acc 展示必须带区间与样本量）"""
    if n <= 0:
        return (0.0, 1.0)
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, center - half), min(1.0, center + half))


@router.get("/overview")
def overview(db: Session = Depends(fastapi_db_dep)):
    """品种总览：就绪门控 + 每品种最新预测 + 数据新鲜度"""
    settings = get_settings()
    readiness = _readiness()

    # 每品种最新预测（单查询取每品种最大 as_of_ts 的行）
    latest_ts = (
        select(
            PredictionResult.symbol,
            func.max(PredictionResult.as_of_ts).label("mx"),
        )
        .where(PredictionResult.symbol.notlike("IDX:%"))
        .group_by(PredictionResult.symbol)
        .subquery()
    )
    preds = db.execute(
        select(PredictionResult)
        .join(
            latest_ts,
            (PredictionResult.symbol == latest_ts.c.symbol)
            & (PredictionResult.as_of_ts == latest_ts.c.mx),
        )
    ).scalars().all()
    pred_map = {
        p.symbol: {
            "direction": p.direction,
            "direction_prob": float(p.direction_prob) if p.direction_prob is not None else None,
            "ret_point": float(p.ret_point) if p.ret_point is not None else None,
            "target_date": p.target_date.isoformat(),
            "caliber": getattr(p, "caliber", "close"),
            "confidence": float(p.confidence) if p.confidence is not None else None,
        }
        for p in preds
    }

    # 数据新鲜度
    fresh = db.execute(
        select(
            DailyBar.symbol,
            func.max(DailyBar.trade_date),
            func.count(),
        )
        .where(DailyBar.symbol.like("%888"))
        .group_by(DailyBar.symbol)
    ).all()
    fresh_map = {r[0]: {"latest": r[1].isoformat(), "rows": int(r[2])} for r in fresh}

    # 行业映射
    sector_of = {
        r[0]: r[1]
        for r in db.execute(select(SectorMap.product, SectorMap.sector)).all()
    }

    symbols = []
    for s in settings.main_contracts:
        f = fresh_map.get(s.symbol, {})
        symbols.append(
            {
                "symbol": s.symbol,
                "name": s.name,
                "sector": sector_of.get(s.product),
                "data_latest": f.get("latest"),
                "rows": f.get("rows", 0),
                "prediction": pred_map.get(s.symbol),
            }
        )
    symbols.sort(key=lambda x: (x["sector"] or "zz", x["symbol"]))
    return {"readiness": readiness, "symbols": symbols}


@router.get("/sectors")
def sectors(db: Session = Depends(fastapi_db_dep)):
    """大类热力图 + 板块强弱排名（§16.6）"""
    # 板块指数（最近一天 + 动量）
    last_dates = db.execute(
        select(SectorIndex.sector, func.max(SectorIndex.trade_date)).group_by(
            SectorIndex.sector
        )
    ).all()
    sectors_out = []
    for sector, mx in last_dates:
        row = db.execute(
            select(SectorIndex).where(
                SectorIndex.sector == sector, SectorIndex.trade_date == mx
            )
        ).scalar()
        if not row:
            continue
        members = [
            r[0]
            for r in db.execute(
                select(SectorMap.product).where(SectorMap.sector == sector)
            ).all()
        ]
        # 成员当日表现（ret_close，收盘价口径）
        mem_rows = db.execute(
            select(DailyBar.symbol, DailyBar.ret_close, DailyBar.ret_settle)
            .where(
                DailyBar.symbol.in_([f"{m}888" for m in members]),
                DailyBar.trade_date == mx,
            )
        ).all()
        members_out = sorted(
            [
                {
                    "symbol": r[0],
                    "product": r[0][:-3],
                    "ret_1d": float(r[1]) if r[1] is not None else None,
                    "ret_settle_1d": float(r[2]) if r[2] is not None else None,
                }
                for r in mem_rows
            ],
            key=lambda m: (m["ret_1d"] if m["ret_1d"] is not None else -999),
            reverse=True,
        )
        sectors_out.append(
            {
                "sector": sector,
                "trade_date": mx.isoformat(),
                "ret_1d": float(row.ret_1d) if row.ret_1d is not None else None,
                "ret_5d": float(row.ret_5d) if row.ret_5d is not None else None,
                "ret_20d": float(row.ret_20d) if row.ret_20d is not None else None,
                "index_level": float(row.index_level) if row.index_level is not None else None,
                "weight_method": row.weight_method,
                "members": members_out,
            }
        )
    # 板块强弱排名（按 5 日动量）
    sectors_out.sort(key=lambda s: (s["ret_5d"] if s["ret_5d"] is not None else -999), reverse=True)
    return {"sectors": sectors_out}


@router.get("/backtest/{run_id}/detail")
def backtest_detail(
    run_id: str,
    symbol: str | None = None,
    model: str | None = None,
    limit: int = 500,
    db: Session = Depends(fastapi_db_dep),
):
    """回测下钻：逐品种/逐模型/逐评估点明细（backtest_detail，P2-7）"""
    stmt = select(BacktestDetail).where(BacktestDetail.run_id == run_id)
    if symbol:
        stmt = stmt.where(BacktestDetail.symbol == symbol)
    if model:
        stmt = stmt.where(BacktestDetail.model == model)
    stmt = stmt.order_by(BacktestDetail.symbol, BacktestDetail.model, BacktestDetail.eval_date).limit(limit)
    rows = db.execute(stmt).scalars().all()
    return {
        "run_id": run_id,
        "count": len(rows),
        "results": [
            {
                "symbol": r.symbol,
                "model": r.model,
                "eval_date": r.eval_date.isoformat(),
                "state": r.state,
                "pred_dir": r.pred_dir,
                "prob": float(r.prob) if r.prob is not None else None,
                "point": float(r.point) if r.point is not None else None,
                "low": float(r.low) if r.low is not None else None,
                "high": float(r.high) if r.high is not None else None,
                "actual": float(r.actual) if r.actual is not None else None,
                "caliber": getattr(r, "caliber", "close"),
            }
            for r in rows
        ],
    }


@router.get("/backtest-summary")
def backtest_summary(db: Session = Depends(fastapi_db_dep)):
    """回测面板聚合：最新整批 run 的 per-model 指标（跨品种聚合）+ Wilson 区间 + 未校准标注"""
    # 最新整批 run（与 update_model_weights 同门槛逻辑）
    cand = db.execute(
        select(
            BacktestResult.run_id,
            func.count(func.distinct(BacktestResult.symbol)).label("n"),
        )
        .group_by(BacktestResult.run_id)
        .having(func.count(func.distinct(BacktestResult.symbol)) >= 10)
        .order_by(func.max(BacktestResult.created_at).desc())
        .limit(1)
    ).first()
    if not cand:
        return {"run_id": None, "models": []}
    run_id = cand[0]
    rows = db.execute(
        select(BacktestResult).where(BacktestResult.run_id == run_id)
    ).scalars().all()

    # 跨品种聚合（与 update_model_weights 同口径：Σacc×n/Σn）
    by_model: dict[str, list] = {}
    for r in rows:
        by_model.setdefault(r.model, []).append(r)

    UNCALIBRATED = {"rf", "xgb", "wavelet"}
    models = []
    for model, lst in by_model.items():
        total_n = sum(r.sample_n or 0 for r in lst)
        if total_n == 0:
            continue

        def _wavg(getter):
            num = sum(getter(r) * (r.sample_n or 0) for r in lst if getter(r) is not None)
            den = sum(r.sample_n or 0 for r in lst if getter(r) is not None)
            return num / den if den else None

        def _bs_wavg(key):
            """by_state JSON 内指标的跨品种加权平均（§18.3 新指标）"""
            def getter(r):
                return (r.by_state or {}).get(key)
            return _wavg(getter)

        acc = _wavg(lambda r: float(r.dir_acc) if r.dir_acc is not None else None)
        wil_lo, wil_hi = _wilson(acc, total_n) if acc is not None else (None, None)

        # by_state 聚合（per state 合并 n 与 acc）
        states_agg: dict[str, dict] = {}
        for r in lst:
            for st, v in (r.by_state or {}).get("states", {}).items():
                agg = states_agg.setdefault(st, {"n": 0, "_hits": 0.0})
                agg["n"] += v.get("n", 0)
                agg["_hits"] += v.get("dir_acc", 0) * v.get("n", 0)
        for st, agg in states_agg.items():
            agg["dir_acc"] = round(agg["_hits"] / agg["n"], 4) if agg["n"] else None
            agg.pop("_hits", None)

        # 覆盖率-准确率曲线聚合（按 top_frac 平均；§18.3）
        curve_agg: dict[float, dict] = {}
        for r in lst:
            for c in ((r.by_state or {}).get("coverage_curve") or []):
                tf = c.get("top_frac")
                if tf is None:
                    continue
                agg = curve_agg.setdefault(tf, {"_acc": 0.0, "_w": 0, "n": 0})
                agg["_acc"] += (c.get("dir_acc") or 0) * (r.sample_n or 0)
                agg["_w"] += r.sample_n or 0
                agg["n"] += c.get("n", 0)
        coverage_curve = [
            {
                "top_frac": tf,
                "n": round(v["n"] / len(lst)),
                "dir_acc": round(v["_acc"] / v["_w"], 4) if v["_w"] else None,
            }
            for tf, v in sorted(curve_agg.items())
        ]

        # 未校准判定：任一品种标记未校准即视为未校准（保守展示）
        calibrated = all(
            (r.by_state or {}).get("interval_calibrated", model not in UNCALIBRATED)
            for r in lst
        )
        calibers = {getattr(r, "caliber", "close") for r in lst}
        models.append(
            {
                "model": model,
                "dir_acc": round(acc, 4) if acc is not None else None,
                "wilson_lo": round(wil_lo, 4) if wil_lo is not None else None,
                "wilson_hi": round(wil_hi, 4) if wil_hi is not None else None,
                "sample_n": total_n,
                "symbols": len(lst),
                "mae": round(_wavg(lambda r: float(r.mae) if r.mae is not None else None) or 0, 4),
                "rmse": round(_wavg(lambda r: float(r.rmse) if r.rmse is not None else None) or 0, 4),
                "quantile_hit": _wavg(lambda r: float(r.quantile_hit) if r.quantile_hit is not None else None),
                "recent60_dir_acc": _wavg(
                    lambda r: (r.by_state or {}).get("recent60_dir_acc")
                ),
                # §18.3（v1.3）指标升级
                "dir_acc_weighted": _bs_wavg("dir_acc_weighted"),
                "ic": _bs_wavg("ic"),
                "rank_ic": _bs_wavg("rank_ic"),
                "icir": _bs_wavg("icir"),
                "coverage": _bs_wavg("coverage"),
                "net_pnl_mean": _bs_wavg("net_pnl_mean"),
                "net_pnl_total": round(_bs_wavg("net_pnl_total") or 0, 2),
                "win_rate_pnl": _bs_wavg("win_rate_pnl"),
                "coverage_curve": coverage_curve,
                "interval_calibrated": calibrated,
                "by_state": states_agg,
                "caliber": "close" if calibers == {"close"} else sorted(calibers)[0],
            }
        )
    models.sort(key=lambda m: (m["dir_acc_weighted"] or m["dir_acc"] or 0), reverse=True)
    return {
        "run_id": run_id,
        "symbols": len({r.symbol for r in rows}),
        "caliber": "close",
        "note": "dir_acc 为收盘价口径；rf/xgb/wavelet 的区间未校准（见 interval_calibrated）",
        "models": models,
    }


@router.get("/volatility")
def volatility_dashboard(db: Session = Depends(fastapi_db_dep)):
    """§18.7 M6c：波动率预测准度 + 异常波动预警

    - 各品种最新 vol_point/vol 区间（prediction_result 最新一期）
    - 异常波动预警：vol_point 相对自身 60 日分位 > 0.8 时预警
    - vol_hit / vol_rmse 历史统计（backtest_result by_state）
    """
    from app.models import PredictionResult

    # 最新一期每品种 vol
    latest_ts = (
        select(
            PredictionResult.symbol,
            func.max(PredictionResult.as_of_ts).label("mx"),
        )
        .where(PredictionResult.symbol.notlike("IDX:%"))
        .group_by(PredictionResult.symbol)
        .subquery()
    )
    preds = db.execute(
        select(PredictionResult).join(
            latest_ts,
            (PredictionResult.symbol == latest_ts.c.symbol)
            & (PredictionResult.as_of_ts == latest_ts.c.mx),
        )
    ).scalars().all()
    rows = []
    for p in preds:
        if p.vol_point is None:
            continue
        rows.append(
            {
                "symbol": p.symbol,
                "target_date": p.target_date.isoformat(),
                "vol_point": float(p.vol_point),
                "vol_low": float(p.vol_low) if p.vol_low is not None else None,
                "vol_high": float(p.vol_high) if p.vol_high is not None else None,
            }
        )
    rows.sort(key=lambda r: r["vol_point"], reverse=True)
    # 高波动 top 20% 标预警（截面相对水平）
    n_warn = max(1, len(rows) // 5) if rows else 0
    for i, r in enumerate(rows):
        r["high_vol_alert"] = i < n_warn
    # 历史 vol_hit（最新整批 run 的 GARCH 行）
    hist = db.execute(
        select(BacktestResult).where(
            BacktestResult.run_id == _latest_full_run(db),
            BacktestResult.model == "garch",
        )
    ).scalars().all()
    vol_hits = [
        {
            "symbol": r.symbol,
            "vol_hit": (r.by_state or {}).get("vol_hit"),
            "vol_rmse": (r.by_state or {}).get("vol_rmse"),
        }
        for r in hist
        if (r.by_state or {}).get("vol_hit") is not None
    ]
    return {
        "latest": rows,
        "history": vol_hits,
        "note": "vol_hit 理论值 0.9（P5-P95 半正态区间）；vol_point 为 σ_t 预测（%）",
    }


def _latest_full_run(db: Session) -> str | None:
    """最新整批 run（与 backtest_summary 同门槛）"""
    cand = db.execute(
        select(
            BacktestResult.run_id,
        )
        .group_by(BacktestResult.run_id)
        .having(func.count(func.distinct(BacktestResult.symbol)) >= 10)
        .order_by(func.max(BacktestResult.created_at).desc())
        .limit(1)
    ).first()
    return cand[0] if cand else None


@router.get("/quality")
def quality(db: Session = Depends(fastapi_db_dep)):
    """数据质量页：异常工单 + 日历模式 + 回补进度"""
    from app.api.routers.ingest import ingest_progress
    from app.ingest.calendar_infer import calendar_mode

    tickets = db.execute(
        select(AnomalyTicket).order_by(AnomalyTicket.id.desc()).limit(50)
    ).scalars().all()
    open_n = db.execute(
        select(func.count()).select_from(AnomalyTicket).where(
            AnomalyTicket.status == "open"
        )
    ).scalar()
    return {
        "readiness": _readiness(),
        "calendar_mode": calendar_mode(db),
        "ingest_progress": ingest_progress(db),
        "anomaly_open": int(open_n or 0),
        "anomalies": [
            {
                "id": t.id,
                "symbol": t.symbol,
                "trade_date": t.trade_date.isoformat(),
                "field": t.field,
                "akshare_val": float(t.akshare_val) if t.akshare_val is not None else None,
                "tqsdk_val": float(t.tqsdk_val) if t.tqsdk_val is not None else None,
                "diff": float(t.diff) if t.diff is not None else None,
                "status": t.status,
            }
            for t in tickets
        ],
    }