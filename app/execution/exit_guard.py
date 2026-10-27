# -*- coding: utf-8 -*-
"""P1 · 离场自动化（Exit Guard）。

要解决的行为，是从 726 笔真实往返里量出来的，不是猜的
-------------------------------------------------------
口径：5 个资金账户、2023-08-23~2026-10-27、以「平仓事件」为一条往返
（金额取账单原值，可逐笔对平；开仓由 FIFO 重建）。源：``scripts/import_broker_bills.py``。

| 指标 | 实测 | 含义 |
|---|---|---|
| 往返笔数 | 726 笔（533 赢 / 193 亏） | — |
| 胜率 | 73.4% | 赢的次数很多 |
| 平均盈利 / 平均亏损 | 463 / 1,907 | **R:R = 0.24** |
| 期望值 | **−167 元/笔** | 赢 533 笔也救不回来 |
| 净额 | **−121,456** | — |
| 赢单持仓中位 | 0.37 小时 | 赚了就跑 |
| 亏单持仓中位 | 2.96 小时 | **亏了死扛（7.9 倍）** |
| 隔夜单净额 | −100,273（占全部**亏损** 83%） | 过夜就出事 |
| 连亏 2 笔后的下一笔 | 39 笔、胜率 64.1%、均净 −57 | 连亏后越做越差 |
| 连亏 ≥3 笔后的下一笔 | 29 笔、胜率 48.3%、均净 −742 | **合计 −21,527**，到第 3 笔基本就废了 |

持仓时长分桶（这是"时间止损"参数的直接依据）：

| 桶 | 笔数 | 净额 | 均净 | 胜率 |
|---|---|---|---|---|
| <0.5h | 348 | **+26,250** | +75 | 85.0% |
| 0.5–2h | 102 | −19,999 | −196 | 65.0% |
| 2–6h | 62 | −21,480 | −346 | 61.0% |
| 6–24h | 141 | −23,539 | −167 | 65.0% |
| 1–3d | 44 | +1,691 | +38 | 64.0% |
| **3–10d** | **20** | **−51,735** | −2,587 | 45.0% |
| **>10d** | **9** | **−32,644** | −3,627 | 44.4% |

**只有"半小时内了结"是赚钱的**；0.5–24h 三桶全线为负；3 天以上 29 笔合计
**−84,379（占全部净亏 69%）**——时间止损不是锦上添花，是主刀。

结论：不是"心态问题"，是**离场只有一个决策点，而这个点由情绪触发**。
本模块把它拆成三个机械触发点，每个都不需要临场判断：

1. **1R 出 1/3** —— 把"要不要卖"从 100% 决策降为 33% 决策，且先落袋一部分
2. **2R 出 1/3** —— 保证"赢的那笔至少赚到 2R"，直接对冲 R:R 0.24 的问题
3. **时间止损** —— 到点未达 1R 即减半（专治"扛"。实测 0.5–24h 三桶全线负，
   而 3 天以上 29 笔占全部净亏 69%）
4. **余量跟吊灯** —— 保留右尾（V5X 已验证 TRAIL_ATR 有效）

纪律
----
* 本模块**只产出动作建议**并落库 ``exit_plan`` / ``exit_action``，不下单——
  下单归 :mod:`app.execution.execution_runtime`（决策/执行分离，便于 dry-run）。
* R 的单位是**价格距离 × 乘数 × 手数**，不用百分比：百分比止损在高低价品种上
  风险完全不等（一手甲醇与一手螺纹的"3%"差 10 倍）。
* 单笔风险上限由 ``target_risk`` 决定，**与保证金占用双约束**，取更紧的一侧。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import logger

# ---------------------------------------------------------------------------
# 默认参数（可用环境变量覆盖；均来自实测或既有回测结论）
# ---------------------------------------------------------------------------
DEFAULT_TP1_R = 1.0          # 第一档止盈：1R
DEFAULT_TP2_R = 2.0          # 第二档止盈：2R（对应 V5X 的 TRAIL_ATR 结构）
DEFAULT_TIME_STOP_DAYS = 5   # 时间止损：5 个自然日（实测 3–7 天桶 −49,565，最差）
DEFAULT_TRAIL_ATR = 3.0      # 余量吊灯：3×ATR（影子扫描第 10 批实证优于 2.0）
DEFAULT_MAX_CONSEC_LOSS = 2  # 当日连亏 2 笔即停止开新仓
DEFAULT_DAILY_LOSS_LIMIT = 0.03  # 单日亏损 ≥ 权益 3% 即停止开新仓
DEFAULT_R_VIOLATION = 1.2    # 单笔亏损 > 1.2R 视为"扛过止损"


@dataclass(frozen=True)
class ExitConfig:
    tp1_r: float = DEFAULT_TP1_R
    tp2_r: float = DEFAULT_TP2_R
    time_stop_days: int = DEFAULT_TIME_STOP_DAYS
    trail_atr: float = DEFAULT_TRAIL_ATR
    max_consec_loss: int = DEFAULT_MAX_CONSEC_LOSS
    daily_loss_limit: float = DEFAULT_DAILY_LOSS_LIMIT
    r_violation: float = DEFAULT_R_VIOLATION


@dataclass
class ExitSignal:
    """一个待执行的离场动作建议。"""

    kind: str          # TP1 / TP2 / TIME_STOP / TRAIL / HARD_STOP
    lots: int
    price_ref: float
    reason: str

    def as_row(self) -> dict[str, Any]:
        return {"kind": self.kind, "lots": self.lots,
                "price_ref": round(self.price_ref, 4), "reason": self.reason}


def split_lots(lots: int) -> tuple[int, int, int]:
    """把持仓拆成 ``(第一档, 第二档, 余量)``。

    小持仓无法三等分，按可执行性降级（**不为凑比例而拆出 0 手**）：

    * ``1`` 手 → ``(0, 0, 1)``：只有一档，改为「到 2R 后止损上移到保本 + 吊灯」
    * ``2`` 手 → ``(1, 0, 1)``：1R 出 1 手，剩 1 手跟吊灯
    * ``≥3`` 手 → ``(n//3, n//3, 余)``
    """
    if lots <= 0:
        return 0, 0, 0
    if lots == 1:
        return 0, 0, 1
    if lots == 2:
        return 1, 0, 1
    a = max(1, lots // 3)
    b = max(1, lots // 3)
    rest = lots - a - b
    if rest <= 0:
        b = 0
        rest = lots - a
    return a, b, rest


def build_plan(*, account: str, real_symbol: str, direction: str, lots_open: int,
               entry_px: float, init_stop: float, atr: float | None = None,
               opened_at: datetime | None = None,
               cfg: ExitConfig | None = None) -> dict[str, Any]:
    """生成一份离场计划（纯计算，不落库）。

    ``init_stop`` 必须是**开仓时**定下的止损，不是移动后的当前止损——
    否则 R 会被压缩，1R/2R 目标失真。
    """
    cfg = cfg or ExitConfig()
    d = (direction or "").upper()
    if d not in ("LONG", "SHORT"):
        raise ValueError(f"[fail-loud] direction 必须是 LONG/SHORT，收到 {direction!r}")
    if lots_open <= 0:
        raise ValueError(f"[fail-loud] lots_open 必须为正，收到 {lots_open!r}")
    if entry_px <= 0 or init_stop <= 0:
        raise ValueError("[fail-loud] entry_px / init_stop 必须为正")

    r_unit = abs(entry_px - init_stop)
    if r_unit <= 0:
        raise ValueError(
            f"[fail-loud] 入场价 {entry_px} 与止损 {init_stop} 相同，R 为 0："
            f"这不是止损，是没设止损")

    sign = 1.0 if d == "LONG" else -1.0
    tp1_lots, tp2_lots, rest = split_lots(lots_open)
    opened = opened_at or datetime.now(timezone.utc)
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)

    plan = {
        "account": account,
        "real_symbol": real_symbol,
        "direction": d,
        "lots_open": lots_open,
        "entry_px": float(entry_px),
        "init_stop": float(init_stop),
        "r_unit": float(r_unit),
        "atr": float(atr) if atr else None,
        "opened_at": opened,
        "tp1_px": round(entry_px + sign * cfg.tp1_r * r_unit, 4) if tp1_lots else None,
        "tp1_lots": tp1_lots or None,
        "tp2_px": round(entry_px + sign * cfg.tp2_r * r_unit, 4) if tp2_lots else None,
        "tp2_lots": tp2_lots or None,
        "rest_lots": rest,
        "time_stop_at": opened + timedelta(days=cfg.time_stop_days),
        "trail_stop": (round(entry_px - sign * cfg.trail_atr * atr, 4)
                       if atr else None),
        "risk_1r": round(r_unit * lots_open, 2),   # 价格距离口径（乘数由调用方乘）
        "degraded": lots_open < 3,
    }
    if plan["degraded"]:
        logger.info(f"[exit_guard] {real_symbol} 仅 {lots_open} 手，无法三等分："
                    f"改为「到 {cfg.tp2_r:g}R 后止损上移保本 + 吊灯」")
    return plan


def evaluate(plan: dict[str, Any], *, last_price: float, mult: float,
             now: datetime | None = None, atr_now: float | None = None,
             cfg: ExitConfig | None = None) -> list[ExitSignal]:
    """按当前价判断哪些动作应当触发。

    ``mult`` 为合约乘数（用于把 R 换算成金额，仅用于日志与风险提示）。
    """
    cfg = cfg or ExitConfig()
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    d = plan["direction"]
    sign = 1.0 if d == "LONG" else -1.0
    entry = float(plan["entry_px"])
    r_unit = float(plan["r_unit"])
    stop = float(plan["init_stop"])
    out: list[ExitSignal] = []

    # ---- 1) 硬止损（最高优先级，先判）----
    hit_stop = (last_price <= stop) if d == "LONG" else (last_price >= stop)
    if hit_stop:
        out.append(ExitSignal(
            "HARD_STOP", int(plan["lots_open"]), stop,
            f"触及初始止损 {stop:g}（1R={r_unit:g} 点 ≈ "
            f"{r_unit * mult:,.0f} 元/手）"))
        return out

    # ---- 2) 分批止盈 ----
    tp1_done = plan.get("tp1_done_at")
    tp2_done = plan.get("tp2_done_at")
    tp1_px = plan.get("tp1_px")
    tp2_px = plan.get("tp2_px")
    if tp1_px and not tp1_done:
        reached = (last_price >= float(tp1_px)) if d == "LONG" else (last_price <= float(tp1_px))
        if reached:
            out.append(ExitSignal(
                "TP1", int(plan["tp1_lots"]), float(tp1_px),
                f"到 {cfg.tp1_r:g}R（{tp1_px:g}）先落袋 {plan['tp1_lots']} 手"))
    if tp2_px and not tp2_done:
        reached = (last_price >= float(tp2_px)) if d == "LONG" else (last_price <= float(tp2_px))
        if reached:
            out.append(ExitSignal(
                "TP2", int(plan["tp2_lots"]), float(tp2_px),
                f"到 {cfg.tp2_r:g}R（{tp2_px:g}）再落袋 {plan['tp2_lots']} 手，余量交吊灯"))

    # ---- 3) 时间止损（治"扛"）----
    ts_at = _as_utc(plan.get("time_stop_at"))
    if ts_at and not plan.get("time_stop_done") and now >= ts_at and not tp1_done:
        remain = int(plan["lots_open"]) - (int(plan["tp1_lots"] or 0) if tp1_done else 0)
        cut = max(1, remain // 2)
        held_days = (now - (_as_utc(plan.get("opened_at")) or now)).days
        out.append(ExitSignal(
            "TIME_STOP", cut, last_price,
            f"持仓 {held_days} 天仍未到 {cfg.tp1_r:g}R，按规则减半 {cut} 手"))

    # ---- 4) 吊灯（余量，到达 2R 后启用；1 手降级时到 2R 即启动）----
    rest = int(plan.get("rest_lots") or 0)
    old_trail = plan.get("trail_stop")
    armed = bool(tp2_done) or bool(plan.get("degraded") and _reached_r(
        plan, last_price, cfg.tp2_r))
    if rest > 0 and old_trail and armed:
        hit = (last_price <= float(old_trail)) if d == "LONG" else (
            last_price >= float(old_trail))
        if hit:
            out.append(ExitSignal(
                "TRAIL", rest, float(old_trail),
                f"吊灯止损 {float(old_trail):g} 被触及，余量 {rest} 手离场"))
    return out


def _reached_r(plan: dict[str, Any], last_price: float, r_mult: float) -> bool:
    d = plan["direction"]
    sign = 1.0 if d == "LONG" else -1.0
    target = float(plan["entry_px"]) + sign * r_mult * float(plan["r_unit"])
    return last_price >= target if d == "LONG" else last_price <= target


def update_trail(plan: dict[str, Any], *, last_price: float, atr_now: float,
                 cfg: ExitConfig | None = None) -> float | None:
    """计算吊灯止损的**新**位置（只在朝有利方向移动时更新）。

    返回 ``None`` 表示不该动（未启用 / 无 ATR / 方向不利）。
    吊灯只在"到达 2R 之后"启用——早启用会在正常回撤里把趋势单剪掉。
    """
    cfg = cfg or ExitConfig()
    if not atr_now or atr_now <= 0:
        return None
    if not (plan.get("tp2_done_at") or plan.get("degraded")):
        return None
    d = plan["direction"]
    sign = 1.0 if d == "LONG" else -1.0
    new_trail = last_price - sign * cfg.trail_atr * atr_now
    old = plan.get("trail_stop")
    if old is None:
        return round(new_trail, 4)
    old = float(old)
    better = new_trail > old if d == "LONG" else new_trail < old
    return round(new_trail, 4) if better else None


def _as_utc(x: Any) -> datetime | None:
    if x is None:
        return None
    if isinstance(x, str):
        try:
            x = datetime.fromisoformat(x)
        except ValueError:
            return None
    if isinstance(x, datetime) and x.tzinfo is None:
        x = x.replace(tzinfo=timezone.utc)
    return x


# ---------------------------------------------------------------------------
# 开仓前的双约束：单笔风险 + 保证金占用
# ---------------------------------------------------------------------------
def size_with_double_constraint(
    *,
    equity: float,
    per_lot_risk: float,
    per_lot_margin: float,
    target_risk_pct: float = 0.01,
    max_usage_pct: float = 0.40,
    current_margin: float = 0.0,
) -> dict[str, Any]:
    """手数 = min(风险约束, 保证金约束)。

    为什么必须双约束：只按 ATR 风险算手数（``execution/lot_sizing.py`` 现状）会在
    **低波动但高保证金**的品种上给出超额仓位——甲醇 2026-10 就是低波动建仓、
    高保证金收场。保证金占用才是硬约束，风险预算只是软约束。
    """
    if equity <= 0:
        raise ValueError("[fail-loud] equity 必须为正")
    if per_lot_risk <= 0 or per_lot_margin <= 0:
        return {"lots": 0, "binding": "invalid",
                "reason": "单手风险或单手保证金缺失，拒绝给出手数（fail-loud）"}

    by_risk = int(equity * target_risk_pct // per_lot_risk)
    budget = equity * max_usage_pct - current_margin
    by_margin = int(max(0.0, budget) // per_lot_margin)
    lots = max(0, min(by_risk, by_margin))
    binding = "risk" if by_risk <= by_margin else "margin"
    return {
        "lots": lots,
        "by_risk": by_risk,
        "by_margin": by_margin,
        "binding": binding,
        "headroom": round(max(0.0, budget), 2),
        "reason": (f"风险上限 {by_risk} 手 / 保证金上限 {by_margin} 手 → 取 {lots} 手"
                   f"（{binding} 为约束边）"),
    }


# ---------------------------------------------------------------------------
# 连亏熔断 / 当日亏损熔断
# ---------------------------------------------------------------------------
def daily_guard(session: Session, account: str, trade_date: date,
                equity: float | None = None,
                cfg: ExitConfig | None = None) -> dict[str, Any]:
    """当日开仓许可：连亏 N 笔 或 亏损达权益 X% 即禁止开新仓（允许平仓）。

    返回 ``{"allow_open": bool, "reasons": [...], ...}``。
    """
    cfg = cfg or ExitConfig()
    rows = session.execute(text(
        "SELECT net, close_at FROM trade_journal "
        "WHERE account = :a AND close_at::date = :d ORDER BY close_at DESC"),
        {"a": account, "d": trade_date}).all()
    if not rows:
        return {"allow_open": True, "reasons": [], "n_today": 0,
                "consec_loss": 0, "pnl_today": 0.0}

    consec = 0
    for net, _ in rows:
        if net <= 0:
            consec += 1
        else:
            break
    pnl_today = float(sum(r[0] for r in rows))
    reasons: list[str] = []
    allow = True
    if consec >= cfg.max_consec_loss:
        allow = False
        reasons.append(f"当日已连亏 {consec} 笔（阈值 {cfg.max_consec_loss}）：停止开新仓")
    if equity and equity > 0:
        loss_pct = -pnl_today / equity
        if loss_pct >= cfg.daily_loss_limit:
            allow = False
            reasons.append(
                f"当日亏损 {pnl_today:,.0f} 元 = 权益 {loss_pct:.1%}"
                f"（阈值 {cfg.daily_loss_limit:.0%}）：停止开新仓")
    return {"allow_open": allow, "reasons": reasons, "n_today": len(rows),
            "consec_loss": consec, "pnl_today": pnl_today}


def entry_allowed(preopen_verdict: str, daily: dict[str, Any]) -> tuple[bool, list[str]]:
    """把盘前门与当日熔断合并成一句结论。任何一格红灯则不许开新仓。"""
    reasons = list(daily.get("reasons") or [])
    ok = bool(daily.get("allow_open", True))
    if preopen_verdict in ("RED", "UNKNOWN"):
        ok = False
        reasons.insert(0, f"盘前门判定 {preopen_verdict}")
    elif preopen_verdict == "YELLOW":
        ok = False
        reasons.insert(0, "盘前门判定 YELLOW：只减不增")
    return ok, reasons


# ---------------------------------------------------------------------------
# 落库
# ---------------------------------------------------------------------------
UPSERT_PLAN = text("""
INSERT INTO exit_plan
    (account, real_symbol, direction, lots_open, entry_px, init_stop, r_unit, atr,
     tp1_px, tp1_lots, tp2_px, tp2_lots, time_stop_at, trail_stop, status)
VALUES
    (:account, :real_symbol, :direction, :lots_open, :entry_px, :init_stop, :r_unit, :atr,
     :tp1_px, :tp1_lots, :tp2_px, :tp2_lots, :time_stop_at, :trail_stop, 'OPEN')
ON CONFLICT (account, real_symbol, direction) DO UPDATE SET
    lots_open = EXCLUDED.lots_open,
    entry_px = EXCLUDED.entry_px,
    init_stop = EXCLUDED.init_stop,
    r_unit = EXCLUDED.r_unit,
    atr = EXCLUDED.atr,
    tp1_px = EXCLUDED.tp1_px,
    tp1_lots = EXCLUDED.tp1_lots,
    tp2_px = EXCLUDED.tp2_px,
    tp2_lots = EXCLUDED.tp2_lots,
    time_stop_at = EXCLUDED.time_stop_at,
    trail_stop = EXCLUDED.trail_stop,
    updated_at = now()
RETURNING id
""")


def persist_plan(session: Session, plan: dict[str, Any]) -> int:
    row = session.execute(UPSERT_PLAN, {
        "account": plan["account"], "real_symbol": plan["real_symbol"],
        "direction": plan["direction"], "lots_open": plan["lots_open"],
        "entry_px": plan["entry_px"], "init_stop": plan["init_stop"],
        "r_unit": plan["r_unit"], "atr": plan.get("atr"),
        "tp1_px": plan.get("tp1_px"), "tp1_lots": plan.get("tp1_lots"),
        "tp2_px": plan.get("tp2_px"), "tp2_lots": plan.get("tp2_lots"),
        "time_stop_at": plan.get("time_stop_at"), "trail_stop": plan.get("trail_stop"),
    }).first()
    return int(row[0])


def persist_actions(session: Session, plan_id: int, signals: Iterable[ExitSignal]) -> int:
    n = 0
    for s in signals:
        session.execute(text(
            "INSERT INTO exit_action (plan_id, kind, lots, price_ref, note) "
            "VALUES (:p, :k, :l, :px, :note)"),
            {"p": plan_id, "k": s.kind, "l": s.lots,
             "px": round(s.price_ref, 4), "note": s.reason})
        n += 1
    return n


def mark_done(session: Session, plan_id: int, kind: str) -> None:
    """标记 TP1/TP2/TIME_STOP 已完成（避免重复触发）。"""
    col = {"TP1": "tp1_done_at", "TP2": "tp2_done_at"}.get(kind)
    if col:
        session.execute(text(
            f"UPDATE exit_plan SET {col} = now(), updated_at = now() WHERE id = :i"),
            {"i": plan_id})
    elif kind == "TIME_STOP":
        session.execute(text(
            "UPDATE exit_plan SET time_stop_done = TRUE, updated_at = now() WHERE id = :i"),
            {"i": plan_id})


# ---------------------------------------------------------------------------
# 人工干预记账
# ---------------------------------------------------------------------------
def log_intervention(session: Session, *, account: str, real_symbol: str, kind: str,
                     planned_value: float | None = None, actual_value: float | None = None,
                     lots: int | None = None, note: str | None = None) -> None:
    """记录一次手动偏离系统的动作。kind 见 manual_intervention 表的 CHECK 约束。

    这不是道德记账，是**取证**：月末把 ``pnl_impact`` 汇总，才知道"手感"到底值多少钱。
    """
    valid = {"MOVE_STOP", "EARLY_EXIT", "HOLD_PAST_STOP", "ADD_LOTS",
             "REVERSE", "SKIP_SIGNAL", "OTHER"}
    if kind not in valid:
        raise ValueError(f"[fail-loud] kind 非法：{kind!r}，应为 {sorted(valid)}")
    session.execute(text(
        "INSERT INTO manual_intervention "
        "(account, real_symbol, kind, planned_value, actual_value, lots, note) "
        "VALUES (:a, :s, :k, :pv, :av, :l, :n)"),
        {"a": account, "s": real_symbol, "k": kind, "pv": planned_value,
         "av": actual_value, "l": lots, "n": note})
    logger.info(f"[exit_guard] 记录人工干预 {account}/{real_symbol} {kind} "
                f"{planned_value}→{actual_value}")


__all__ = [
    "ExitConfig", "ExitSignal", "split_lots", "build_plan", "evaluate",
    "size_with_double_constraint", "daily_guard", "entry_allowed",
    "persist_plan", "persist_actions", "mark_done", "log_intervention",
]
