# -*- coding: utf-8 -*-
"""P0-1 组件④：点位校验（含盘口一致性安全闸）。

校验项（详见 PRD §3.4，已按 2026-10-02 实测数据模型修正）：
  1. 合约可交易：real_contract_metadata 可解析（futures_symbol 不含真实合约时回退连续 888）
  2. tick 对齐：price_tick 缺失则跳过并告警
  3. 盘口一致性（核心安全闸）：反解价 vs contract_daily 真实合约日频价偏差
  4. 涨跌停：数据源待接入，当前仅告警不阻断
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.core.symbol_code import to_std
from app.execution.roll_policy import real_contract_metadata

#: 实时盘口容差（默认 0.5%）
PRICE_TOL = float(__import__("os").getenv("PRICE_TOL", "0.005"))
#: 日频参照容差（默认 2%，因 contract_daily 为日线）
DAILY_TOL = float(__import__("os").getenv("DAILY_PRICE_TOL", "0.02"))
#: 硬阻断阈值（默认 5%，明显反解错误才阻断，避免日线噪声误杀）
BLOCK_TOL = float(__import__("os").getenv("PRICE_BLOCK_TOL", "0.05"))


def contract_daily_keys(session: Session, real_symbol: str) -> list[str]:
    """经 ``contract_code_map`` 字典解析 contract_daily 的真实键（单一真源）。

    contract_daily 内原生码形态不统一（CZCE 4 位大写 ``FG2701``、SHFE/DCE 小写 ``rb2510``），
    而 ``contract_code_map.observed_native`` 记录的就是该合约在库内**实际被存成**的形态
    （由 ``app.ingest.contract_code build`` 扫描 contract_daily 自身得到）。
    优先用字典解析；若字典缺失该行，再用 std / 大小写兜底，避免依赖 ``to_native`` 的
    3 位 CZCE 启发式（那只适用于天勤订阅码，不等于 contract_daily 存储键）。
    """
    std = to_std(real_symbol)
    cands: list[str] = [std, real_symbol]
    row = session.execute(
        text("SELECT observed_native FROM contract_code_map WHERE std_symbol=:s"),
        {"s": std},
    ).fetchone()
    if row and row[0]:
        for v in str(row[0]).split(","):
            v = v.strip()
            if v:
                cands.append(v)
    cands += [real_symbol.upper(), real_symbol.lower(), std.upper(), std.lower()]
    seen: set[str] = set()
    out: list[str] = []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def fetch_market_price(session: Session, real_symbol: str, when) -> tuple[Optional[float], Optional[str]]:
    """真实合约价取自 contract_daily（日频）。

    bar_*/hourly_bar 仅存 888 连续序列、无真实合约分钟/小时线，故一致性闸使用日频参照。
    返回 (price, granularity)；取不到返回 (None, None)。

    ⚠️ 2026-10-02 两次修正：
      1) 此前用 ``to_native`` 把 CZCE ``FG2701`` 退化成 3 位 ``FG701``，与 contract_daily
         的 4 位存储不符 → 永远取不到价；
      2) 改用 ``contract_code_map.observed_native``（字典单一真源）解析键，大小写仅兜底。
         更耐久的做法是跑 ``app.ingest.contract_code normalize --apply`` 把 contract_daily.symbol
         物理归一为 4 位标准码，届时此处可直接用 ``to_std(real_symbol)``。
    """
    d = when.date() if isinstance(when, datetime) else when
    try:
        from pandas import Timestamp
        if isinstance(when, Timestamp):
            d = when.date()
    except Exception:
        pass
    for form in contract_daily_keys(session, real_symbol):
        # ---- R10：优先**真实合约的分钟/小时级**价源（盘中防错，精度高）----
        # 库内当前 minute_bar/bar_5m/hourly_bar **全为 888 连续**、无真实合约行，
        # 故实际仍回退日频；但代码路径已就绪——真实合约分钟线一旦落地即自动收紧容差，
        # 无需再改校验器（check_consistency 按 granularity 选软阈值）。
        for table, tcol, gran in (("minute_bar", "ts", "minute"),
                                  ("hourly_bar", "trade_datetime", "hourly")):
            try:
                row = session.execute(
                    text(f"SELECT close FROM {table} WHERE symbol=:s AND {tcol} <= :t "
                         f"ORDER BY {tcol} DESC LIMIT 1"),
                    {"s": form, "t": when}).fetchone()
                if row and row[0] is not None:
                    return float(row[0]), gran
            except Exception:  # noqa: BLE001 —— 表/列缺失或无该合约行 → 继续降级
                session.rollback()
        # ---- 回退：真实合约日频（低精度参照）----
        row = session.execute(
            text("SELECT close FROM contract_daily WHERE symbol=:s AND trade_date <= :d "
                 "ORDER BY trade_date DESC LIMIT 1"),
            {"s": form, "d": d}).fetchone()
        if row is not None and row[0] is not None:
            return float(row[0]), "daily"
    return None, None


def check_tick(price: float, tick: Optional[float]) -> list[str]:
    warns: list[str] = []
    if tick is None or tick <= 0:
        return ["tick 未知，跳过对齐校验（futures_symbol.price_tick 待填充）"]
    aligned = abs(round(price / tick) * tick - price) <= 1e-6
    if not aligned:
        # 非阻断：下单前券商会拒绝非 tick 整数倍报单，这里仅提示
        warns.append(f"price={price} 非 tick={tick} 整数倍（下单前需取整）")
    return warns


def check_consistency(price: float, market: Optional[float], real_symbol: str,
                      granularity: Optional[str],
                      price_space: str = "raw") -> tuple[list[str], list[str]]:
    """盘口一致性安全闸：偏差 > 硬阈值 → 阻断；> 软阈值 → 告警。

    返回 (warnings, blocking_reasons)。
    ⚠️ 2026-10-02 修正：此前 validate 把本函数返回值一律并入 blocks，
    导致软偏差（应仅告警）被误判为阻断。现改为显式返回 (warns, blocks)。
    ⚠️ 2026-10-04 修正（space-aware）：盘口价源 contract_daily 为 **raw 空间** 日频价。
    当 entry 处于 adj 空间时，反解价已减 cum_offset，与 raw 盘口不可比 → 跳过阻断
    （仅记提示）。生产信号走 raw 空间，故不影响实盘；adj 仅用于研究/回测对照。
    """
    if (price_space or "raw").lower() == "adj":
        return [f"adj 空间跳过盘口一致性校验（盘口价源 contract_daily 为 raw 空间，不可比）"], []
    if market is None:
        return [], [f"取不到 {real_symbol} 盘口价（contract_daily 无数据），跳过一致性校验（建议人工核对）"]
    dev = abs(price - market) / max(market, 1e-9)
    if dev > BLOCK_TOL:
        return [], [f"反解价 {price:.2f} 与盘口 {market:.2f} 偏差 {dev:.2%} "
                    f"> 硬阈值 {BLOCK_TOL:.2%}：可能偏移/换月映射错误"]
    soft = DAILY_TOL if granularity == "daily" else PRICE_TOL
    warns: list[str] = []
    if granularity == "daily":
        # ⚠ 参照精度披露（R10）：日频参照只能给到 ±2% 软阈值，**做不了盘中防错**。
        #   根因：库内 minute_bar/bar_5m/hourly_bar 全为 888 连续，无真实合约行。
        #   这条告警长期存在是**如实反映能力边界**，不是可忽略的噪音——
        #   补齐真实合约分钟线后本告警自动消失（届时 granularity=minute/hourly）。
        warns.append(
            f"一致性闸参照为**日频**收盘价（软容差放宽至 {soft:.2%}）："
            f"库内无真实合约分钟/小时线，盘中防错能力受限（R10 待补数据源）")
    if dev > soft:
        warns.append(
            f"反解价 {price:.2f} 与盘口 {market:.2f} 偏差 {dev:.2%}"
            f"（>容差 {soft:.2%}，建议核对）")
    return warns, []


def check_limit(session: Session, real_symbol: str, price: float, when) -> list[str]:
    """涨跌停校验：[待办] 接入涨跌停源后，price 超出 [limit_down, limit_up] 应阻断。

    当前数据源（daily_bar.limit_up/limit_down 等）未确认存在，先探测；
    无数据源则静默跳过（不阻断、不告警，避免误报）。
    """
    try:
        # 必须限定 table_schema='public'：G4 分层后同名对象在两个 schema 并存
        # （l0_raw.daily_bar 表 + public.daily_bar 兼容视图），只按 table_name 过滤
        # 会命中 2 行，可能把"底层表新增了列"误判成"app 实际读的视图有该列"。
        has = session.execute(
            text("SELECT 1 FROM information_schema.columns "
                 "WHERE table_schema='public' AND table_name='daily_bar' "
                 "AND column_name='limit_up' LIMIT 1")
        ).fetchone()
        if has is None:
            return []
    except Exception as e:  # noqa: BLE001
        # 探测失败不能静默：显式告警，避免"以为在校验、其实没校验"
        logger.warning("[validator] 涨跌停列探测失败，本次跳过校验: %s", e)
        return []
    # 数据源已存在但未接入比对逻辑：提示待补全（不阻断）
    return [f"涨跌停数据源疑似存在但未接入比对（{real_symbol}）"]


def validate(session: Session, real_symbol: str, price: float,
             when, price_space: str = "raw") -> tuple[list[str], list[str]]:
    """返回 (warnings, blocking_reasons)。

    price_space：信号价空间（"raw" 默认 / "adj"）。仅 raw 空间执行盘口一致性闸，
    adj 空间由 check_consistency 内部跳过（盘口价源为 raw 空间，不可比）。
    """
    warns: list[str] = []
    blocks: list[str] = []

    meta = real_contract_metadata(session, real_symbol)
    if meta["source"] == "missing":
        blocks.append(f"{real_symbol} 无元数据（futures_symbol 与连续 888 回退均缺失）")
    elif meta["source"] == "futures_symbol" and not meta["active"]:
        blocks.append(f"{real_symbol} 已标记 inactive")
    elif meta["source"] == "continuous_fallback":
        warns.append(f"{real_symbol} 元数据回退自连续 888（futures_symbol 未含真实合约）")

    warns += check_tick(price, meta["price_tick"])

    market, gran = fetch_market_price(session, real_symbol, when)
    c_warns, c_blocks = check_consistency(price, market, real_symbol, gran, price_space)
    warns += c_warns
    blocks += c_blocks
    warns += check_limit(session, real_symbol, price, when)
    return warns, blocks
