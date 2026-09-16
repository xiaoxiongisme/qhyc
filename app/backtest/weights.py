"""
⑳ 模型权重月更（按近 60 日回测准确率）

- 更新公式：w = clip((acc − 0.5) × 8 + 0.3, 0.1, 2.0)
  （acc=0.51 → 0.38、0.52 → 0.46、0.53 → 0.54；随机水平附近几乎无票权）
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
        # 复验同款修正：默认取"最新整批 run"（symbol 数 ≥ 门槛），而非"最新任意 run"
        # （手动单品种/子集验证 run 会污染权重来源）
        cand = session.execute(
            select(
                BacktestResult.run_id,
                func.count(func.distinct(BacktestResult.symbol)).label("n_syms"),
            )
            .group_by(BacktestResult.run_id)
            .having(
                func.count(func.distinct(BacktestResult.symbol))
                >= cfg.min_symbols_in_run
            )
            .order_by(func.max(BacktestResult.created_at).desc())
            .limit(1)
        ).first()
        if not cand:
            return {"updated": 0, "message": "无整批回测 run（全部低于品种门槛）"}
        run_id, cnt = cand[0], int(cand[1])

    if (cnt or 0) < cfg.min_symbols_in_run:
        logger.warning(
            f"[weights] run {run_id} 仅 {cnt} 个品种 < 门槛 {cfg.min_symbols_in_run}，"
            f"拒绝更新（P1-2 护栏：权重保持 config 等权）"
        )
        return {"updated": 0, "run_id": run_id, "rejected": f"symbols={cnt} < {cfg.min_symbols_in_run}"}

    rows = session.execute(
        select(BacktestResult).where(BacktestResult.run_id == run_id)
    ).scalars().all()

    # 跨品种聚合：每 model 收集 (acc, sample_n)
    by_model: dict[str, list[tuple[float, int]]] = {}
    skipped = 0
    for r in rows:
        if r.model == "ensemble":
            continue  # ensemble 是融合结果，不给投票权重
        # P1-2 样本门槛：新 run（250 日短窗口 step=3）每品种 ~84 评估点 > 60 通过；
        # 少数品种（LG/PR 等新品种）样本 < 10 → 跳过；[10,60) → 降权参与
        if (r.sample_n or 0) < 10:
            skipped += 1
            continue
        # §18.7（v1.3.2）：优先 recent60（月更用），兜底 dir_acc
        bs = r.by_state or {}
        recent60 = bs.get("recent60_dir_acc")
        acc = recent60 if recent60 is not None else r.dir_acc
        if acc is None:
            skipped += 1
            continue
        # 新 run 样本不足 60 时降权（仅计入聚合但不赋予高权重）
        by_model.setdefault(r.model, []).append((float(acc), int(r.sample_n)))

    updated = 0
    for model, lst in by_model.items():
        total_n = sum(n for _, n in lst)
        acc_agg = sum(a * n for a, n in lst) / total_n if total_n else 0.0
        # P1-1 LSTM 冻结：M4.1 泄漏根治后默认解除（null）；配置数值则冻结
        if model == "lstm" and cfg.lstm_weight_freeze is not None:
            w = cfg.lstm_weight_freeze
        else:
            # §18.7（v1.3.2）：新公式 w = clip((acc − 0.5) × 8 + 0.3, 0.1, 2.0)
            # 使 acc=0.51 → 0.38, 0.52 → 0.46, 0.53 → 0.54，拉开差距
            w = min(2.0, max(0.1, (acc_agg - 0.5) * 8 + 0.3))
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
                    # 可观测性修复（2026-09-10 复核）：ORM 的 onupdate 只在 ORM UPDATE
                    # 时触发，对 Core 的 on_conflict_do_update 无效 → 冲突路径不刷新
                    # updated_at（表内时间戳停在首次插入，会误判权重新鲜度）。显式写入。
                    "updated_at": func.now(),
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
