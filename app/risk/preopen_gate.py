# -*- coding: utf-8 -*-
"""P0 · 盘前作战门（PreOpen Gate）。

解决的具体事故
--------------
2026-10-26 夜盘，甲醇涨停触发交易所临时提保，账户保证金占用率打到 110%（盘中追保）。
复盘（726 笔真实往返 + 持仓重建）确认三个可计算、但此前没有任何程序在算的量：

1. **锁仓双倍保证金**：MA701 多 4 空 4、EG2701 多 2 空 2，净敞口 = 0，
   毛名义 46.3 万，郑商所不实行单向大边 → 保证金按 8 手收。
2. **提保是乘法不是加法**：8% → 15%（约 1.9×）叠加涨停价重算名义（约 1.06×），
   真实压力 ≈ 2 倍，而不是"多收几个点"。
3. **占用率的分母没有**：qhyc 至今没有账户实时权益，故"我还能开几手"从未被回答。

本模块的三个输出
----------------
* ``verdict``：GREEN（可开新仓）/ YELLOW（只减不增）/ RED（禁止开新仓）/ UNKNOWN（数据缺失）
* ``checks``：逐项证据（含阈值与实测值），可审计
* ``markdown``：一条能在手机上 10 秒读完的推送

纪律
----
* 数据缺失 ⇒ ``UNKNOWN``，**绝不退化为 GREEN**。不知道就不许开新仓。
* 只读不写交易数据；唯一写入是 ``preopen_snapshot`` 留痕。
* 任何异常只记日志、不抛给调度器（推送是旁路）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.risk import margin_model as mm
from app.risk.margin_model import PositionLeg, RuleRow

# ---------------------------------------------------------------------------
# 配置（全部可由环境变量覆盖，默认保守）
# ---------------------------------------------------------------------------
DEFAULT_WARN_NOW = 0.40        # 当前占用率警戒线：留 60% 缓冲 ≈ 抗 1.9 倍提保
DEFAULT_STOP_NOW = 0.55        # 当前占用率硬上限
DEFAULT_STOP_EXTREME = 0.60    # 极端情景（一板 + 提保）硬上限
DEFAULT_RATE_BUFFER = 1.5      # 再提保倍数（实测甲醇 15%→18% 为 1.2×，涨停后常见 1.5~1.9×）
#: 期货公司加收。**默认 0**：``futures_rule.margin_ratio`` 来自 akshare
#: ``futures_rule``（国泰君安口径），实测已是**含加收的实收率**
#: （2026-10-08 MA=18%、EG=20%，远高于交易所标准的 8%/10%）。
#: 若再统一 +3 个点等于双重计算，会把健康仓位误判成红灯。
#: 仅当确认数据源是"交易所标准"时才把 PREOPEN_FIRM_ADDON 设为 0.02~0.03。
DEFAULT_FIRM_ADDON = 0.0
DEFAULT_LIMIT_ROOM_WARN = 0.30  # 距停板剩余空间 < 30% 板幅 → 黄
DEFAULT_MARGIN_JUMP = 1.20     # 保证金率较 30 日中位上浮 20% → 黄
DEFAULT_DELIVERY_WARN_DAYS = 30  # 距交割月不足 30 天 → 黄（个人户不能进交割月）
DEFAULT_RULE_STALE_DAYS = 3    # 规则数据超过 N 天未更新 → 只能判 UNKNOWN


def _env_f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass
class GateConfig:
    enabled: bool = field(
        default_factory=lambda: os.getenv("PREOPEN_GATE_ENABLED", "0") == "1")
    warn_now: float = field(default_factory=lambda: _env_f("PREOPEN_WARN_NOW", DEFAULT_WARN_NOW))
    stop_now: float = field(default_factory=lambda: _env_f("PREOPEN_STOP_NOW", DEFAULT_STOP_NOW))
    stop_extreme: float = field(
        default_factory=lambda: _env_f("PREOPEN_STOP_EXTREME", DEFAULT_STOP_EXTREME))
    rate_buffer: float = field(
        default_factory=lambda: _env_f("PREOPEN_RATE_BUFFER", DEFAULT_RATE_BUFFER))
    firm_addon: float = field(
        default_factory=lambda: _env_f("PREOPEN_FIRM_ADDON", DEFAULT_FIRM_ADDON))
    limit_room_warn: float = field(
        default_factory=lambda: _env_f("PREOPEN_LIMIT_ROOM_WARN", DEFAULT_LIMIT_ROOM_WARN))
    margin_jump: float = field(
        default_factory=lambda: _env_f("PREOPEN_MARGIN_JUMP", DEFAULT_MARGIN_JUMP))
    delivery_warn_days: int = field(
        default_factory=lambda: int(_env_f("PREOPEN_DELIVERY_WARN_DAYS",
                                           DEFAULT_DELIVERY_WARN_DAYS)))
    rule_stale_days: int = field(
        default_factory=lambda: int(_env_f("PREOPEN_RULE_STALE_DAYS",
                                           DEFAULT_RULE_STALE_DAYS)))


# ---------------------------------------------------------------------------
# 数据读取
# ---------------------------------------------------------------------------
def _product_of(real_symbol: str) -> str:
    """``MA701`` → ``MA``；``EG2701`` → ``EG``。"""
    s = (real_symbol or "").strip().upper()
    i = 0
    while i < len(s) and not s[i].isdigit():
        i += 1
    return s[:i] or s


def delivery_ym(session: Session, real_symbol: str) -> tuple[int, int] | None:
    """交割年月 ``(年, 月)``。

    优先查 ``contract_code_map``（全库统一真源，含 deliv_year/deliv_month）；
    查不到时按合约码位数推断：
    * 4 位（``EG2701`` / ``RB2505``）→ ``20YY``
    * 3 位（郑商所 ``MA701`` / ``SR401``）→ ``2020 + 首位``
    推断结果会打日志，**不静默**。
    """
    row = session.execute(text(
        "SELECT deliv_year, deliv_month FROM contract_code_map "
        "WHERE std_symbol = :s LIMIT 1"), {"s": real_symbol}).first()
    if not (row and row[0]):
        # 郑商所原生 3 位码（MA701）在全库统一口径里写作 4 位（MA2701），补一次查询
        prod0 = _product_of(real_symbol)
        digits0 = real_symbol[len(prod0):]
        if digits0.isdigit() and len(digits0) == 3:
            row = session.execute(text(
                "SELECT deliv_year, deliv_month FROM contract_code_map "
                "WHERE std_symbol = :s LIMIT 1"),
                {"s": f"{prod0}2{digits0}"}).first()
    if row and row[0]:
        return int(row[0]), int(row[1])
    prod = _product_of(real_symbol)
    digits = real_symbol[len(prod):]
    if digits.isdigit() and len(digits) == 4:
        return 2000 + int(digits[:2]), int(digits[2:])
    if digits.isdigit() and len(digits) == 3:
        logger.warning(f"[preopen] {real_symbol} 无 contract_code_map 记录，按 3 位码推断交割月")
        return 2020 + int(digits[0]), int(digits[1:])
    return None


def load_equity(session: Session, account: str, trade_date: date) -> dict[str, Any] | None:
    """取 ≤ trade_date 的最新权益。返回 None 表示无数据（调用方必须判 UNKNOWN）。"""
    row = session.execute(text(
        "SELECT trade_date, equity, available, margin_used, source "
        "FROM account_equity WHERE account = :a AND trade_date <= :d "
        "ORDER BY trade_date DESC LIMIT 1"),
        {"a": account, "d": trade_date}).first()
    if not row:
        return None
    return {"trade_date": row[0], "equity": float(row[1]),
            "available": float(row[2]) if row[2] is not None else None,
            "margin_used": float(row[3]) if row[3] is not None else None,
            "source": row[4]}


def load_positions(session: Session, account: str, trade_date: date) -> list[PositionLeg]:
    """取 ≤ trade_date 的最新一张持仓快照，按合约聚合。

    数据源 ``execution_position``（已支持 long_lots/short_lots 分离与锁仓）。
    多空**不轧差**——这是本模块与所有"净持仓"口径的根本区别。
    """
    snap = session.execute(text(
        "SELECT MAX(snapshot_date) FROM execution_position WHERE account = :a "
        "AND snapshot_date <= :d"), {"a": account, "d": trade_date}).scalar()
    if not snap:
        return []
    rows = session.execute(text(
        "SELECT real_symbol, COALESCE(SUM(long_lots),0), COALESCE(SUM(short_lots),0), "
        "       MAX(avg_open_price) "
        "FROM execution_position "
        "WHERE account = :a AND snapshot_date = :s "
        "GROUP BY real_symbol"), {"a": account, "s": snap}).all()
    legs: list[PositionLeg] = []
    for sym, L, S, avg_px in rows:
        price, exch, mult = _ref_price_and_meta(session, sym, trade_date)
        if price is None and avg_px is not None:
            price = float(avg_px)
        legs.append(PositionLeg(real_symbol=sym, exchange=exch or "",
                                long_lots=int(L), short_lots=int(S),
                                price=float(price or 0.0),
                                multiplier=float(mult or 0.0)))
    return legs


def _ref_price_and_meta(session: Session, real_symbol: str,
                        trade_date: date) -> tuple[float | None, str | None, float | None]:
    """参考价（优先昨结算）与交易所、乘数。"""
    row = session.execute(text(
        "SELECT settle, close, exchange FROM contract_daily "
        "WHERE symbol = :s AND trade_date <= :d "
        "ORDER BY trade_date DESC LIMIT 1"),
        {"s": real_symbol, "d": trade_date}).first()
    settle = None
    exch = None
    if row:
        settle = float(row[0]) if row[0] is not None else (
            float(row[1]) if row[1] is not None else None)
        exch = row[2]
    sym = session.execute(text(
        "SELECT exchange, multiplier FROM futures_symbol WHERE symbol = :s LIMIT 1"),
        {"s": real_symbol}).first()
    if sym:
        exch = exch or sym[0]
        mult = float(sym[1]) if sym[1] is not None else None
    else:
        mult = None
    return settle, exch, mult


def load_rules(session: Session, trade_date: date,
               symbols: Iterable[str]) -> tuple[dict[str, RuleRow], date | None]:
    """取每个合约所属品种的当日规则（按 ``futures_rule.code`` 匹配品种码）。"""
    prods = sorted({_product_of(s) for s in symbols})
    if not prods:
        return {}, None
    day = session.execute(text(
        "SELECT MAX(trade_date) FROM futures_rule WHERE trade_date <= :d AND code = ANY(:c)"),
        {"d": trade_date, "c": prods}).scalar()
    if not day:
        return {}, None
    rows = session.execute(text(
        "SELECT code, margin_ratio, price_limit, multiplier, special_note, adjust_note "
        "FROM futures_rule WHERE trade_date = :d AND code = ANY(:c)"),
        {"d": day, "c": prods}).all()
    by_prod = {r[0]: RuleRow(margin_ratio=r[1], price_limit=r[2], multiplier=r[3],
                             special_note=r[4], adjust_note=r[5]) for r in rows}
    out: dict[str, RuleRow] = {}
    for s in symbols:
        r = by_prod.get(_product_of(s))
        if r:
            out[s] = r
    return out, day


def margin_rate_history(session: Session, trade_date: date,
                        prods: Iterable[str], lookback: int = 30) -> dict[str, float]:
    """各品种近 ``lookback`` 个交易日的保证金率中位数（百分数口径原样返回）。

    ⚠ 必须限定回看窗口：早期版本漏了 ``trade_date >= since``，中位数是在全部
    898 个交易日上算的——那样"提保"永远不会被检出（当前值淹没在两年历史里）。
    """
    since = trade_date - timedelta(days=int(max(lookback, 1) * 1.6) + 3)
    rows = session.execute(text(
        "SELECT code, percentile_cont(0.5) WITHIN GROUP (ORDER BY margin_ratio) "
        "FROM futures_rule WHERE trade_date <= :d AND trade_date >= :s "
        "AND code = ANY(:c) AND margin_ratio IS NOT NULL GROUP BY code"),
        {"d": trade_date, "s": since, "c": list(prods)}).all()
    return {r[0]: float(r[1]) for r in rows if r[1] is not None}


def notice_changes(session: Session, trade_date: date,
                   prods: Iterable[str]) -> list[str]:
    """对比最近两个有数据的交易日，找出 special_note / adjust_note 的变化。"""
    days = session.execute(text(
        "SELECT DISTINCT trade_date FROM futures_rule WHERE trade_date <= :d "
        "AND code = ANY(:c) ORDER BY trade_date DESC LIMIT 2"),
        {"d": trade_date, "c": list(prods)}).all()
    if len(days) < 2:
        return []
    cur, prev = days[0][0], days[1][0]

    def _snap(d: date) -> dict[str, tuple[str, str]]:
        rs = session.execute(text(
            "SELECT code, COALESCE(special_note,''), COALESCE(adjust_note,'') "
            "FROM futures_rule WHERE trade_date = :d AND code = ANY(:c)"),
            {"d": d, "c": list(prods)}).all()
        return {r[0]: (r[1], r[2]) for r in rs}

    a, b = _snap(cur), _snap(prev)
    out: list[str] = []
    for code, (sn, an) in a.items():
        psn, pan = b.get(code, ("", ""))
        if (sn, an) != (psn, pan):
            out.append(f"{code}: 规则变动 {prev}→{cur} | 特殊说明「{sn}」 调整备注「{an}」")
    return out


def main_contract_mismatch(session: Session, trade_date: date,
                           symbols: Iterable[str]) -> list[str]:
    """持仓合约与当日**真实**主力合约不一致 → 换月风险。

    必须用 ``main_contract_map.underlying`` 而非 ``main_symbol``：
    后者是主连符号（``MA888``），拿它和真实合约比会**每一行都报假警**
    （实测 ``main_symbol='MA888' / underlying='MA2701'``）。
    """
    prods = sorted({_product_of(s) for s in symbols})
    if not prods:
        return []
    rows = session.execute(text(
        "SELECT product, COALESCE(underlying, main_symbol) "
        "FROM main_contract_map "
        "WHERE trade_date = (SELECT MAX(trade_date) FROM main_contract_map "
        "                    WHERE trade_date <= :d) AND product = ANY(:p)"),
        {"d": trade_date, "p": prods}).all()
    main = {r[0]: r[1] for r in rows}
    out = []
    for s in symbols:
        m = main.get(_product_of(s))
        if m and m.upper() != s.upper():
            out.append(f"{s}: 已非主力（当日主力 {m}），注意流动性衰减与换月")
    return out


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def run_gate(session: Session, *, account: str, trade_date: date,
             session_kind: str = "day",
             cfg: GateConfig | None = None) -> dict[str, Any]:
    """执行一次盘前检查，返回结构化结果（不写库、不推送）。"""
    cfg = cfg or GateConfig()
    checks: list[dict[str, Any]] = []
    reasons: list[str] = []
    actions: list[str] = []
    verdict = "GREEN"

    def add(key: str, ok: bool, level: str, detail: str) -> None:
        checks.append({"key": key, "ok": ok, "level": level, "detail": detail})

    # ---- 0. 权益 ----
    eq = load_equity(session, account, trade_date)
    if not eq:
        add("equity", False, "RED", f"账户 {account} 在 {trade_date} 及之前无权益记录")
        reasons.append("缺账户权益：无法计算保证金占用率")
        actions.append(f"先补录权益：POST /risk/preopen/equity 或写 account_equity 表")
        return _finalize(verdict="UNKNOWN", checks=checks, reasons=reasons,
                         actions=actions, account=account, trade_date=trade_date,
                         session_kind=session_kind, cfg=cfg, equity=None, legs=[],
                         now=None, extreme=None, locked=[])
    equity = eq["equity"]
    stale_days = (trade_date - eq["trade_date"]).days
    if stale_days > 3:
        verdict = mm.worst(verdict, "YELLOW")
        add("equity_fresh", False, "YELLOW",
            f"权益数据已过期 {stale_days} 天（{eq['trade_date']}）")
        reasons.append(f"权益数据 {stale_days} 天未更新，占用率分母失真")
        actions.append("更新 account_equity 再做决策")
    else:
        add("equity_fresh", True, "OK", f"权益 {equity:,.0f} 元（{eq['trade_date']}，{eq['source']}）")

    # ---- 1. 持仓 ----
    legs = load_positions(session, account, trade_date)
    if not legs:
        add("positions", True, "OK", "无持仓（空仓过夜）")
    else:
        add("positions", True, "OK", f"{len(legs)} 个合约，毛手数 {sum(l.gross_lots for l in legs)}")

    # ---- 2. 规则 ----
    symbols = [l.real_symbol for l in legs]
    rules, rule_day = load_rules(session, trade_date, symbols)
    if legs and not rules:
        add("rules", False, "RED", "futures_rule 无可用记录")
        reasons.append("缺合约规则（保证金率/板幅）：所有保证金结论无效")
        actions.append("先跑 seed_futures_rule.py 补齐 futures_rule")
        verdict = mm.worst(verdict, "UNKNOWN")
    elif legs:
        miss = [s for s in symbols if s not in rules]
        add("rules", not miss, "YELLOW" if miss else "OK",
            f"规则日 {rule_day}，缺 {miss}" if miss else f"规则日 {rule_day}，{len(rules)} 合约齐备")
        if miss:
            reasons.append(f"缺规则的合约：{miss}（其保证金未计入）")
        # 规则新鲜度：futures_rule 一旦停更，所有保证金/板幅都用的是旧值。
        # 实测 2026-10-27 时该表最新只到 2026-10-08（停更 19 天）——
        # 这种「看起来有数据」的陈旧比缺数据更危险，必须显式降级。
        if rule_day is not None:
            lag = (trade_date - rule_day).days
            if lag > cfg.rule_stale_days:
                add("rules_fresh", False, "RED",
                    f"futures_rule 已停更 {lag} 天（最新 {rule_day}）："
                    f"保证金率/板幅可能已变")
                reasons.append(
                    f"合约规则数据停更 {lag} 天，提保与扩板信息缺失，判定不可信")
                actions.append("先跑 seed_futures_rule.py 补齐当日规则")
                verdict = mm.worst(verdict, "UNKNOWN")
            else:
                add("rules_fresh", True, "OK", f"规则数据新鲜（滞后 {lag} 天）")

    # ---- 3. 期货公司加收（把交易所率抬到实收率）----
    # 期货公司通常在交易所基础上加收 2~3 个点，且临时提保时同步加收。
    # 若只按交易所率算，会系统性低估 20%~40%。
    if legs:
        legs_adj = [
            PositionLeg(real_symbol=l.real_symbol, exchange=l.exchange,
                        long_lots=l.long_lots, short_lots=l.short_lots,
                        price=l.price, multiplier=l.multiplier)
            for l in legs
        ]
        now = mm.scenario_usage(legs_adj, rules, equity,
                                rate_buffer=1.0 + cfg.firm_addon)
        extreme_rates = cfg.rate_buffer
        extreme = mm.scenario_usage(legs_adj, rules, equity,
                                    rate_buffer=extreme_rates + cfg.firm_addon,
                                    limit_shock=1.0)
    else:
        now = mm.UsageResult(equity=equity)
        extreme = mm.UsageResult(equity=equity)

    # ---- 4. 占用率判定 ----
    v, why = mm.verdict_from_usage(
        now.usage, extreme.usage,
        warn_now=cfg.warn_now, stop_now=cfg.stop_now, stop_extreme=cfg.stop_extreme)
    verdict = mm.worst(verdict, v)
    reasons.extend(why)
    if v == "RED":
        actions.append("今日禁止开新仓；已有持仓只减不增")
    elif v == "YELLOW":
        actions.append("今日只允许减仓，不开新仓")

    add("margin_now", now.usage < cfg.warn_now,
        "OK" if now.usage < cfg.warn_now else ("RED" if now.usage >= cfg.stop_now else "YELLOW"),
        f"当前占用率 {now.usage:.1%}（保证金 {now.margin_required:,.0f} / 权益 {equity:,.0f}）")
    add("margin_extreme", extreme.usage < cfg.stop_extreme,
        "OK" if extreme.usage < cfg.stop_extreme else "RED",
        f"极端情景（一板+提保{cfg.rate_buffer:g}×+加收{cfg.firm_addon:.0%}）"
        f"占用率 {extreme.usage:.1%}")

    # ---- 5. 锁仓（本次事故的直接放大器）----
    locked = [l for l in legs if l.is_locked]
    if locked:
        detail = "；".join(
            f"{l.real_symbol} 多{l.long_lots}空{l.short_lots}"
            f"（{mm.norm_exchange(l.exchange) or '未知所'}，{mm.lock_policy(l.exchange)}）"
            for l in locked)
        if any(mm.lock_policy(l.exchange) == "BOTH_SIDES" for l in locked):
            add("locked", False, "RED", detail + " —— 该所锁仓收双边保证金")
            reasons.append("存在锁仓且交易所收双边：净敞口为 0 但保证金不减少")
            actions.append("解锁（平掉一边）或改做单向，勿用锁仓充当降风险工具")
            verdict = mm.worst(verdict, "YELLOW")
        else:
            add("locked", True, "OK", detail + " —— 该所按单向大边收取")
    else:
        add("locked", True, "OK", "无锁仓")

    # ---- 6. 涨跌停距离 ----
    for l in legs:
        rule = rules.get(l.real_symbol)
        if rule is None or l.price <= 0:
            continue
        room = mm.limit_room(l.price, l.price, rule)
        if not room:
            continue
        lim = mm.normalize_rate(rule.price_limit)
        if lim is None:
            continue
        # 对空头而言危险的是涨停，对多头是跌停
        danger = room["to_up_pct"] if l.short_lots >= l.long_lots else room["to_down_pct"]
        side = "涨停" if l.short_lots >= l.long_lots else "跌停"
        if danger < lim * cfg.limit_room_warn:
            add(f"limit_room:{l.real_symbol}", False, "RED",
                f"距{side}仅 {danger:.2%}（板幅 {lim:.2%}，已走"
                f"{1 - danger / lim:.0%}）")
            reasons.append(f"{l.real_symbol} 距{side}不足板幅 30%，跳空风险高")
            verdict = mm.worst(verdict, "YELLOW")
        else:
            add(f"limit_room:{l.real_symbol}", True, "OK",
                f"距{side} {danger:.2%}（板幅 {lim:.2%}）")

    # ---- 7. 保证金率异动（临时提保的唯一程序化信号）----
    prods = [_product_of(s) for s in symbols]
    hist = margin_rate_history(session, trade_date, prods)
    for p in sorted(set(prods)):
        rule = next((rules[s] for s in symbols if _product_of(s) == p and s in rules), None)
        if rule is None or p not in hist:
            continue
        cur = mm.normalize_rate(rule.margin_ratio)
        base = mm.normalize_rate(hist[p])
        if cur is None or base is None or base <= 0:
            continue
        ratio = cur / base
        if ratio >= cfg.margin_jump:
            add(f"margin_jump:{p}", False, "RED",
                f"{p} 保证金率 {base:.2%} → {cur:.2%}（{ratio:.2f}×，近 30 日中位）")
            reasons.append(f"{p} 保证金率异常上浮 {ratio:.1f} 倍，疑似临时提保")
            actions.append(f"{p} 按提保后比例重算可用手数，并预留下一档提保")
            verdict = mm.worst(verdict, "YELLOW")
        else:
            add(f"margin_jump:{p}", True, "OK", f"{p} 保证金率 {cur:.2%}（基准 {base:.2%}）")

    # ---- 8. 规则公告变动 ----
    notes = notice_changes(session, trade_date, prods)
    if notes:
        add("notice", False, "RED", "；".join(notes[:3]))
        reasons.append("交易所规则公告有变动")
        verdict = mm.worst(verdict, "YELLOW")
    else:
        add("notice", True, "OK", "近两交易日无规则公告变动")

    # ---- 9. 交割月临近 ----
    for l in legs:
        ym = delivery_ym(session, l.real_symbol)
        if not ym:
            add(f"delivery:{l.real_symbol}", False, "YELLOW", "无法解析交割月")
            continue
        y, m = ym
        deliv_first = date(y, m, 1)
        days_left = (deliv_first - trade_date).days
        if days_left <= 0:
            add(f"delivery:{l.real_symbol}", False, "RED",
                f"已进入/跨过交割月（{y}-{m:02d}）")
            reasons.append(f"{l.real_symbol} 已进交割月，个人户必须离场")
            verdict = mm.worst(verdict, "RED")
        elif days_left <= cfg.delivery_warn_days:
            add(f"delivery:{l.real_symbol}", False, "YELLOW",
                f"距交割月 {days_left} 天（{y}-{m:02d}）")
            reasons.append(f"{l.real_symbol} 距交割月 {days_left} 天，需提前移仓")
            verdict = mm.worst(verdict, "YELLOW")
        else:
            add(f"delivery:{l.real_symbol}", True, "OK", f"距交割月 {days_left} 天")

    # ---- 10. 换月 ----
    for msg in main_contract_mismatch(session, trade_date, symbols):
        add(f"roll:{msg.split(':')[0]}", False, "YELLOW", msg)
        verdict = mm.worst(verdict, "YELLOW")

    return _finalize(verdict=verdict, checks=checks, reasons=reasons, actions=actions,
                     account=account, trade_date=trade_date, session_kind=session_kind,
                     cfg=cfg, equity=equity, legs=legs, now=now, extreme=extreme,
                     locked=[l.real_symbol for l in locked])


def _finalize(**kw: Any) -> dict[str, Any]:
    verdict = kw["verdict"]
    if verdict == "GREEN" and not kw["reasons"]:
        kw["actions"].insert(0, "可正常开新仓（仍受单笔风险上限约束）")
    md = render_markdown(kw)
    kw["markdown"] = md
    return kw


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
_ICON = {"GREEN": "🟢", "YELLOW": "🟡", "RED": "🔴", "UNKNOWN": "⚪"}
_ACTION = {
    "GREEN": "可开新仓",
    "YELLOW": "只减不增",
    "RED": "禁止开新仓",
    "UNKNOWN": "数据缺失 · 视为禁止开新仓",
}


def render_markdown(res: dict[str, Any]) -> str:
    """一条手机上 10 秒读完的盘前简报。"""
    v = res["verdict"]
    cfg: GateConfig = res["cfg"]
    now: mm.UsageResult | None = res["now"]
    ext: mm.UsageResult | None = res["extreme"]
    lines = [
        f"## {_ICON.get(v, '')} 盘前门 {v} · {_ACTION.get(v, '')}",
        f"**{res['account']}** {res['trade_date']} {res['session_kind']}",
        "",
    ]
    if res["equity"] is not None:
        lines.append(f"权益 **{res['equity']:,.0f}** 元")
    if now is not None and res["legs"]:
        lines.append(
            f"保证金 {now.margin_required:,.0f} 元 · 占用 **{now.usage:.1%}**"
            f"（警戒 {cfg.warn_now:.0%} / 上限 {cfg.stop_now:.0%}）")
        lines.append(
            f"极端情景（一板+提保{cfg.rate_buffer:g}×）占用 **{ext.usage:.1%}**"
            f"（上限 {cfg.stop_extreme:.0%}）")
        lines.append(f"毛名义 {now.gross_notional:,.0f} / 净名义 {now.net_notional:,.0f}")
        if now.gross_notional > 0 and now.net_notional < now.gross_notional * 0.5:
            lines.append("> 毛/净差额大：锁仓占用了额外保证金")

    if res.get("locked"):
        lines.append("")
        lines.append(f"**锁仓**：{', '.join(res['locked'])}")

    if res["reasons"]:
        lines.append("")
        lines.append("**拦截理由**")
        for r in res["reasons"][:6]:
            lines.append(f"- {r}")

    if res["actions"]:
        lines.append("")
        lines.append("**今日动作**")
        for a in res["actions"][:5]:
            lines.append(f"- {a}")

    bad = [c for c in res["checks"] if not c["ok"]]
    if bad:
        lines.append("")
        lines.append(f"**未通过检查 {len(bad)} 项**")
        for c in bad[:6]:
            lines.append(f"- `{c['key']}` {c['detail']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 落库 + 推送
# ---------------------------------------------------------------------------
def persist(session: Session, res: dict[str, Any]) -> None:
    """写 preopen_snapshot（同 trade_date+session+account 覆盖）。"""
    import json

    now: mm.UsageResult | None = res["now"]
    ext: mm.UsageResult | None = res["extreme"]
    session.execute(text("""
        INSERT INTO preopen_snapshot
            (trade_date, session, account, verdict, margin_usage_now,
             margin_usage_extreme, gross_notional, net_notional, equity,
             locked_symbols, checks, reasons, actions, markdown)
        VALUES
            (:td, :se, :ac, :v, :un, :ue, :gn, :nn, :eq, :lk,
             CAST(:chk AS JSONB), :rs, :ac2, :md)
        ON CONFLICT (trade_date, session, account) DO UPDATE SET
            verdict = EXCLUDED.verdict,
            margin_usage_now = EXCLUDED.margin_usage_now,
            margin_usage_extreme = EXCLUDED.margin_usage_extreme,
            gross_notional = EXCLUDED.gross_notional,
            net_notional = EXCLUDED.net_notional,
            equity = EXCLUDED.equity,
            locked_symbols = EXCLUDED.locked_symbols,
            checks = EXCLUDED.checks,
            reasons = EXCLUDED.reasons,
            actions = EXCLUDED.actions,
            markdown = EXCLUDED.markdown,
            created_at = now()
    """), {
        "td": res["trade_date"], "se": res["session_kind"], "ac": res["account"],
        "v": res["verdict"],
        "un": now.usage if now else None,
        "ue": ext.usage if ext else None,
        "gn": now.gross_notional if now else None,
        "nn": now.net_notional if now else None,
        "eq": res["equity"],
        "lk": res.get("locked") or None,
        "chk": json.dumps(res["checks"], ensure_ascii=False, default=str),
        "rs": res["reasons"] or None,
        "ac2": res["actions"] or None,
        "md": res["markdown"],
    })


def push(res: dict[str, Any]) -> bool:
    """走既有 pushplus 通道（``app.notify.pushplus.send_notify``）；失败只记日志。"""
    title = f"[盘前门{res['verdict']}] {res['account']} {res['trade_date']}"
    try:
        from app.notify.pushplus import send_notify
        ok, via = send_notify(title, res["markdown"])
        if not ok:
            logger.warning(f"[preopen] 推送未送达（via={via}），正文已落 preopen_snapshot")
        return bool(ok)
    except Exception as e:  # noqa: BLE001 —— 推送是旁路，绝不能影响主流程
        logger.warning(f"[preopen] 推送异常（旁路）: {e}")
        return False


__all__ = [
    "GateConfig", "run_gate", "render_markdown", "persist", "push",
    "load_equity", "load_positions", "load_rules", "delivery_ym",
    "notice_changes", "main_contract_mismatch", "margin_rate_history",
]
