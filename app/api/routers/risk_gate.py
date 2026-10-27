# -*- coding: utf-8 -*-
"""风控外壳 REST（P0 盘前门 / P1 离场 / P2 品种减法）。

对外只暴露 8 个端点，按"用户一天里真正会点的动作"组织，不是按模块组织：

* 盘前：补录权益 → 跑一次门 → 看三色灯
* 盘中：查离场计划 → 报一次人工干预
* 盘后：看当日熔断状态 → 看接受标准

设计约束
--------
* 全部端点**不直接下单**。离场只产出动作建议（``exit_action``），下单仍归
  ``execution_runtime``——决策/执行分离是本项目既有铁律。
* 补录权益是唯一让门禁从 UNKNOWN 变可用的入口，故必须显式、留痕、可覆盖。
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.db import session_scope
from app.core.logging import logger
from app.execution import exit_guard
from app.risk import preopen_gate, trade_journal, variety_gate

router = APIRouter()


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------
class EquityReq(BaseModel):
    account: str = Field(..., description="资金账号")
    trade_date: date = Field(..., description="权益对应交易日")
    equity: float = Field(..., gt=0, description="动态权益（含浮盈浮亏），禁止填静态权益")
    available: Optional[float] = None
    margin_used: Optional[float] = None
    source: str = Field("MANUAL", description="BROKER / MANUAL / SETTLE")
    note: Optional[str] = None


class RunReq(BaseModel):
    account: str
    trade_date: Optional[date] = None
    session: str = Field("day", description="day / night")
    push: bool = Field(False, description="是否顺带推送到微信")


class PlanReq(BaseModel):
    account: str
    real_symbol: str
    direction: str = Field(..., description="LONG / SHORT")
    lots_open: int = Field(..., gt=0)
    entry_px: float = Field(..., gt=0)
    init_stop: float = Field(..., gt=0, description="开仓时定的止损，不是移动后的当前止损")
    atr: Optional[float] = None
    persist: bool = Field(True)


class EvalReq(BaseModel):
    plan: dict[str, Any]
    last_price: float = Field(..., gt=0)
    mult: float = Field(..., gt=0)
    atr_now: Optional[float] = None


class InterventionReq(BaseModel):
    account: str
    real_symbol: str
    kind: str = Field(..., description="MOVE_STOP/EARLY_EXIT/HOLD_PAST_STOP/ADD_LOTS/"
                                       "REVERSE/SKIP_SIGNAL/OTHER")
    planned_value: Optional[float] = None
    actual_value: Optional[float] = None
    lots: Optional[int] = None
    note: Optional[str] = None


# ---------------------------------------------------------------------------
# P0 · 盘前
# ---------------------------------------------------------------------------
@router.post("/preopen/equity", summary="补录账户权益（门禁的唯一输入缺口）")
def put_equity(req: EquityReq) -> dict[str, Any]:
    if req.source not in ("BROKER", "MANUAL", "SETTLE"):
        raise HTTPException(400, "source 必须是 BROKER / MANUAL / SETTLE")
    with session_scope() as s:
        s.execute(text("""
            INSERT INTO account_equity
                (trade_date, account, equity, available, margin_used, source, note)
            VALUES (:d, :a, :e, :av, :mu, :src, :n)
            ON CONFLICT (trade_date, account) DO UPDATE SET
                equity = EXCLUDED.equity, available = EXCLUDED.available,
                margin_used = EXCLUDED.margin_used, source = EXCLUDED.source,
                note = EXCLUDED.note
        """), {"d": req.trade_date, "a": req.account, "e": req.equity,
               "av": req.available, "mu": req.margin_used,
               "src": req.source, "n": req.note})
    return {"ok": True, "account": req.account, "trade_date": str(req.trade_date),
            "equity": req.equity}


@router.post("/preopen/run", summary="跑一次盘前门（不写交易数据，只留痕）")
def run_preopen(req: RunReq) -> dict[str, Any]:
    td = req.trade_date or date.today()
    with session_scope() as s:
        res = preopen_gate.run_gate(s, account=req.account, trade_date=td,
                                    session_kind=req.session)
        preopen_gate.persist(s, res)
    pushed = preopen_gate.push(res) if req.push else False
    return {
        "verdict": res["verdict"],
        "margin_usage_now": (res["now"].usage if res["now"] else None),
        "margin_usage_extreme": (res["extreme"].usage if res["extreme"] else None),
        "reasons": res["reasons"], "actions": res["actions"],
        "checks": res["checks"], "markdown": res["markdown"], "pushed": pushed,
    }


@router.get("/preopen/latest", summary="最近一次盘前门结果")
def latest_preopen(account: str, limit: int = 5) -> dict[str, Any]:
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT trade_date, session, verdict, margin_usage_now, margin_usage_extreme,
                   gross_notional, net_notional, equity, locked_symbols,
                   reasons, actions, markdown, created_at
            FROM preopen_snapshot WHERE account = :a
            ORDER BY created_at DESC LIMIT :n
        """), {"a": account, "n": limit}).all()
    return {"account": account, "items": [
        {"trade_date": str(r[0]), "session": r[1], "verdict": r[2],
         "margin_usage_now": float(r[3]) if r[3] is not None else None,
         "margin_usage_extreme": float(r[4]) if r[4] is not None else None,
         "gross_notional": float(r[5]) if r[5] is not None else None,
         "net_notional": float(r[6]) if r[6] is not None else None,
         "equity": float(r[7]) if r[7] is not None else None,
         "locked_symbols": r[8], "reasons": r[9], "actions": r[10],
         "created_at": str(r[12])}
        for r in rows]}


# ---------------------------------------------------------------------------
# P1 · 离场
# ---------------------------------------------------------------------------
@router.post("/exit/plan", summary="创建/覆盖离场计划（分批止盈 + 时间止损）")
def make_plan(req: PlanReq) -> dict[str, Any]:
    try:
        plan = exit_guard.build_plan(
            account=req.account, real_symbol=req.real_symbol,
            direction=req.direction, lots_open=req.lots_open,
            entry_px=req.entry_px, init_stop=req.init_stop, atr=req.atr)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    out = {k: (str(v) if isinstance(v, datetime) else v) for k, v in plan.items()}
    if req.persist:
        with session_scope() as s:
            out["plan_id"] = exit_guard.persist_plan(s, plan)
    return out


@router.post("/exit/evaluate", summary="按当前价判断该不该减仓（只出建议，不下单）")
def evaluate_plan(req: EvalReq) -> dict[str, Any]:
    try:
        sigs = exit_guard.evaluate(req.plan, last_price=req.last_price,
                                   mult=req.mult, atr_now=req.atr_now)
        trail = exit_guard.update_trail(req.plan, last_price=req.last_price,
                                        atr_now=req.atr_now or 0.0)
    except (KeyError, ValueError) as e:
        raise HTTPException(400, f"计划的字段不完整：{e}") from e

    # ⚠ 2026-10-28 修复（缺陷 K）：persist_actions / mark_done 此前**全仓无调用方**，
    # 于是动作不落 exit_action、tp1_done_at / time_stop_done 永不置位 ——
    # 同一动作每次调用都会重复建议，离场自动化实际只剩"返回建议"。
    # 现补上落库：动作写 exit_action；TP1/TP2/TIME_STOP 置位以避免重复触发。
    persisted = 0
    marked: list[str] = []
    plan_id = req.plan.get("id") if isinstance(req.plan, dict) else None
    if plan_id is not None and sigs:
        try:
            with session_scope() as sn:
                persisted = exit_guard.persist_actions(sn, int(plan_id), sigs)
                for sg in sigs:
                    if sg.kind in ("TP1", "TP2", "TIME_STOP"):
                        exit_guard.mark_done(sn, int(plan_id), sg.kind)
                        marked.append(sg.kind)
        except Exception as e:  # noqa: BLE001
            # 落库失败不阻断建议返回，但必须留痕——静默失败是本模块一贯的事故源。
            logger.warning(
                f"[risk] 离场动作落库失败（建议照常返回）plan_id={plan_id}: {e}")

    return {"actions": [sg.as_row() for sg in sigs],
            "new_trail_stop": trail,
            "persisted": persisted,
            "marked_done": marked,
            "note": "本接口只给建议；下单由 execution_runtime 负责"}


@router.get("/exit/open", summary="当前所有未了结的离场计划")
def open_plans(account: str) -> dict[str, Any]:
    with session_scope() as s:
        rows = s.execute(text("""
            SELECT id, real_symbol, direction, lots_open, entry_px, init_stop, r_unit,
                   tp1_px, tp1_lots, tp2_px, tp2_lots, time_stop_at, trail_stop
            FROM exit_plan WHERE account = :a AND status = 'OPEN'
            ORDER BY updated_at DESC
        """), {"a": account}).all()
    return {"account": account, "items": [
        {"id": r[0], "real_symbol": r[1], "direction": r[2], "lots_open": r[3],
         "entry_px": float(r[4]), "init_stop": float(r[5]), "r_unit": float(r[6]),
         "tp1_px": float(r[7]) if r[7] else None, "tp1_lots": r[8],
         "tp2_px": float(r[9]) if r[9] else None, "tp2_lots": r[10],
         "time_stop_at": str(r[11]) if r[11] else None,
         "trail_stop": float(r[12]) if r[12] else None}
        for r in rows]}


@router.post("/exit/intervention", summary="记录一次人工干预（月末汇总干预净损益）")
def log_intervention(req: InterventionReq) -> dict[str, Any]:
    try:
        with session_scope() as s:
            exit_guard.log_intervention(
                s, account=req.account, real_symbol=req.real_symbol, kind=req.kind,
                planned_value=req.planned_value, actual_value=req.actual_value,
                lots=req.lots, note=req.note)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"ok": True}


@router.get("/daily-guard", summary="当日开仓许可（连亏 / 日亏损熔断）")
def daily_guard(account: str, equity: Optional[float] = None,
                trade_date: Optional[date] = None) -> dict[str, Any]:
    td = trade_date or date.today()
    with session_scope() as s:
        res = exit_guard.daily_guard(s, account, td, equity=equity)
        last = s.execute(text(
            "SELECT verdict FROM preopen_snapshot WHERE account = :a "
            "AND trade_date = :d ORDER BY created_at DESC LIMIT 1"),
            {"a": account, "d": td}).scalar()
    ok, reasons = exit_guard.entry_allowed(last or "UNKNOWN", res)
    return {**res, "preopen_verdict": last, "allow_open_final": ok,
            "final_reasons": reasons}


# ---------------------------------------------------------------------------
# P2 · 品种减法 / 接受标准
# ---------------------------------------------------------------------------
@router.get("/variety", summary="按当前权益筛出能做的 2–3 个品种")
def varieties(account: str, trade_date: Optional[date] = None,
              max_single_usage: float = variety_gate.MAX_SINGLE_USAGE,
              include_unvalidated: bool = False) -> dict[str, Any]:
    td = trade_date or date.today()
    with session_scope() as s:
        eq = preopen_gate.load_equity(s, account, td)
        if not eq:
            raise HTTPException(400, "缺账户权益，先 POST /risk/preopen/equity")
        return variety_gate.tradable_varieties(
            s, account=account, trade_date=td, equity=eq["equity"],
            max_single_usage=max_single_usage,
            include_unvalidated=include_unvalidated)


@router.get("/acceptance", summary="接受标准：全量 / 剔 TOP3 / 固定选品集 三列")
def acceptance(account: str, since: Optional[date] = None,
               fixed_symbols: Optional[str] = None) -> dict[str, Any]:
    fixed = [x.strip() for x in fixed_symbols.split(",")] if fixed_symbols else None
    with session_scope() as s:
        return variety_gate.check_trade_journal(s, account, since=since,
                                                fixed_symbols=fixed)


@router.get("/streak", summary="连亏串画像（该不该在连亏后继续做）")
def streak(account: Optional[str] = None) -> dict[str, Any]:
    with session_scope() as s:
        return {"streak": trade_journal.streak_profile(s, account),
                "violations": trade_journal.violations(s, account)}


@router.get("/health", summary="风控外壳自检")
def risk_health() -> dict[str, Any]:
    with session_scope() as s:
        t = s.execute(text("""
            SELECT to_regclass('account_equity')      IS NOT NULL,
                   to_regclass('preopen_snapshot')    IS NOT NULL,
                   to_regclass('exit_plan')           IS NOT NULL,
                   to_regclass('trade_journal')       IS NOT NULL,
                   to_regclass('manual_intervention') IS NOT NULL
        """)).first()
    missing = [n for n, ok in zip(
        ("account_equity", "preopen_snapshot", "exit_plan",
         "trade_journal", "manual_intervention"), t or (False,) * 5) if not ok]
    if missing:
        logger.warning(f"[risk] 缺表：{missing}（尚未应用 migrations/044_risk_gate.sql）")
    return {"tables_ok": not missing, "missing": missing,
            "gate_enabled": preopen_gate.GateConfig().enabled}
