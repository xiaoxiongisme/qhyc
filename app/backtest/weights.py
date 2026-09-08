"""
⑳ 模型权重月更（按近 60 日回测准确率）

- 更新公式：w = clip((acc − 0.5) × 4, 0.1, 2.0)
  （acc=0.5 → 0.1 弱化；acc=1.0 → 2.0 强化；随机水平附近几乎无票权）
- 来源：backtest_result 最新 run 的 by_state.recent60_dir_acc
- engine 权重优先级：model_weights 表 → config.predict.weights 回落
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import logger
from app.models import BacktestResult, ModelWeight


def update_model_weights(session: Session, run_id: str | None = None) -> dict:
    """⑳ 按最新回测 run 的近 60 日准确率更新 model_weights（月更任务调用）

    审计护栏（P1-2）：
    - run 内 symbol 数 ≥ min_symbols_in_run（整批 run 才够格）
    - 每 symbol sample_n ≥ min_sample_n
    - LSTM 在泄漏修复观察期内冻结为 config.lstm_weight_freeze
    - 不满足条件的行直接跳过（回退 config 权重）
    """
    from sqlalchemy import func

    cfg = get_settings().yaml.backtest

    if run_id:
        cnt = session.execute(
            select(func.count(func.distinct(BacktestResult.symbol))).where(
                BacktestResult.run_id == run_id
            )
        ).scalar()
    else:
        latest = session.execute(
            select(BacktestResult).order_by(BacktestResult.created_at.desc()).limit(1)
        ).scalar()
        if not latest:
            return {"updated": 0, "message": "无回测记录"}
        run_id = latest.run_id
        cnt = session.execute(
            select(func.count(func.distinct(BacktestResult.symbol))).where(
                BacktestResult.run_id == run_id
            )
        ).scalar()

    if (cnt or 0) < cfg.min_symbols_in_run:
        logger.warning(
            f"[weights] run {run_id} 仅 {cnt} 个品种 < 门槛 {cfg.min_symbols_in_run}，"
            f"拒绝更新（P1-2 护栏：权重保持 config 等权）"
        )
        return {"updated": 0, "run_id": run_id, "rejected": f"symbols={cnt} < {cfg.min_symbols_in_run}"}

    rows = session.execute(
        select(BacktestResult).where(BacktestResult.run_id == run_id)
    ).scalars().all()

    # 跨品种聚合：每 model 收集 (recent60_acc, sample_n)
    by_model: dict[str, list[tuple[float, int]]] = {}
    skipped = 0
    for r in rows:
        if r.model == "ensemble":
            continue  # ensemble 是融合结果，不给投票权重
        # P1-2 样本门槛（per symbol per model）
        if (r.sample_n or 0) < cfg.min_sample_n:
            skipped += 1
            continue
        acc = (r.by_state or {}).get("recent60_dir_acc")
        if acc is None:
            skipped += 1
            continue
        by_model.setdefault(r.model, []).append((float(acc), int(r.sample_n)))

    updated = 0
    for model, lst in by_model.items():
        total_n = sum(n for _, n in lst)
        acc_agg = sum(a * n for a, n in lst) / total_n if total_n else 0.0
        # P1-1 LSTM 冻结：M4.1 泄漏根治后默认解除（null）；配置数值则冻结
        if model == "lstm" and cfg.lstm_weight_freeze is not None:
            w = cfg.lstm_weight_freeze
        else:
            w = min(2.0, max(0.1, (acc_agg - 0.5) * 4))
        stmt = (
            pg_insert(ModelWeight)
            .values(
                model=model,
                weight=round(w, 4),
                dir_acc=round(acc_agg, 4),
                sample_n=total_n,
                source="backtest",
            )
            .on_conflict_do_update(
                index_elements=["model"],
                set_={
                    "weight": round(w, 4),
                    "dir_acc": round(acc_agg, 4),
                    "sample_n": total_n,
                    "source": "backtest",
                },
            )
        )
        session.execute(stmt)
        updated += 1
    session.commit()
    logger.info(f"[weights] model_weights updated {updated} (skipped {skipped}) from run {run_id}")
    return {"updated": updated, "skipped": skipped, "run_id": run_id, "symbols_in_run": cnt}


def get_model_weights(session: Session) -> dict[str, float] | None:
    """engine 权重来源：DB 优先，无记录返回 None（回落 config 等权）"""
    rows = session.execute(select(ModelWeight)).scalars().all()
    if not rows:
        return None
    return {r.model: float(r.weight) for r in rows}
