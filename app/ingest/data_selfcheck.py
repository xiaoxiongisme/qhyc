# -*- coding: utf-8 -*-
"""V4 · 数据质量自检（数据层日终作业）。

来源：《PRD_借鉴落地验证.md》V4 + 《架构设计_CB执行版.md》T19（cron 17:35，
排在 rank_position 17:30 之后，避开复权/合成竞态）。

检查项
------
1. **缺失比例**：单合约单日 bar 数缺失 > 5% → 报警（lh3:176）
2. **零成交**：成交量连续为 0 但非停板 → 报警
3. **换月断层**：主连 888 大跳空 ∩ 指数连 8888 未同步跳 = 换月断层（正常）；
   若 888 与 8888 **同时**大跳 = 市场真实跳空（正常）；
   若 888 大跳但 8888 **反向/无数据** → 可疑，需人工确认
4. **尾部回退**：源表最新时间戳早于上一交易日 → 报警（应对"尾删重建"竞态）

产出：写入 `anomaly_ticket`（不阻塞决策链，仅留痕）。

配置（`config/*.yaml` 的 data_selfcheck 段，默认关闭）
----------------------------------------------------
    data_selfcheck:
      enabled: false
      missing_pct_alarm: 0.05
      zero_volume_days: 3
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import text

logger = logging.getLogger(__name__)


@dataclass
class SelfCheckConfig:
    enabled: bool = False
    missing_pct_alarm: float = 0.05
    zero_volume_days: int = 3
    #: 参与检查的周期（表 → 该表一个交易日的预期 bar 数）
    expected_bars: dict = field(default_factory=lambda: {
        "bar_15m": 32,    # 15m：日盘+夜盘约 32 根
        "bar_30m": 16,
        "bar_60m": 8,
        "daily_bar": 1,
    })


def _log_ticket(session, symbol: str, trade_date: date, fld: str,
                note: str, akshare_val=None, diff=None) -> None:
    """写一张异常工单（幂等：同 symbol+date+field 只写一次 pending）。"""
    try:
        session.execute(text("""
            INSERT INTO anomaly_ticket (symbol, trade_date, field, akshare_val,
                                        diff, status, note)
            VALUES (:s, :d, :f, :a, :diff, 'pending', :n)
            ON CONFLICT DO NOTHING
        """), {"s": symbol, "d": trade_date, "f": fld, "a": akshare_val,
               "diff": diff, "n": note})
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        logger.warning(f"[data_selfcheck] 写工单失败（可能缺唯一约束，降级为直接插入）：{e}")
        try:
            session.execute(text("""
                INSERT INTO anomaly_ticket (symbol, trade_date, field, status, note)
                SELECT :s, :d, :f, 'pending', :n
                WHERE NOT EXISTS (
                    SELECT 1 FROM anomaly_ticket
                    WHERE symbol=:s AND trade_date=:d AND field=:f AND status='pending')
            """), {"s": symbol, "d": trade_date, "f": fld, "n": note})
            session.commit()
        except Exception as e2:  # noqa: BLE001
            session.rollback()
            logger.error(f"[data_selfcheck] 写工单再次失败：{e2}")


#: 各候选表的时间列名（按优先级探测）。
#:
#: ★ 2026-10-09（PR review P1-2）：``check_missing`` 原硬编码 ``bucket::date``，
#:   但 ``cfg.expected_bars`` 里的表时间列**并不统一**：
#:     · ``l1_mkt.bar_5m/15m/30m/60m`` → ``bucket``
#:     · ``l0_raw.daily_bar``             → ``trade_date``（**没有 bucket 列**）
#:     · ``l0_raw.hourly_bar``            → ``trade_datetime``
#:     · ``l0_raw.minute_bar``            → ``ts``
#:   查 daily_bar 时 SQL 必然报 ``column "bucket" does not exist`` → 被
#:   ``except`` 吞成 ``continue`` → **该表永远检查不到**。
#:   而上层只看到「0 工单」，与「一切正常」无法区分——属典型静默失效
#:   （上层实测发现 daily_bar 一项从未生效，真实数据缺失也无人告警）。
_TIME_COL_CANDIDATES = ("bucket", "trade_date", "trade_datetime", "ts", "date")


def _time_col(session, table: str) -> str | None:
    """探测该表实际使用的时间列名；找不到返回 ``None``。"""
    try:
        rows = session.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t"
        ), {"t": table.split(".")[-1]}).fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    cols = {r[0] for r in rows}
    for c in _TIME_COL_CANDIDATES:
        if c in cols:
            return c
    return None


def check_missing(session, cfg: SelfCheckConfig, trade_date: date) -> int:
    """检查各周期表当日 bar 数缺失比例。返回写入工单数。

    ★ 2026-10-09：时间列改为**按表探测**（原先硬编码 ``bucket`` 导致 daily_bar
      这类表**整项检查从未执行**，且失败被 ``except`` 吞成 ``continue``，
      上层只看到"0 工单"）。现在探测不到列时记 ``error`` 并跳过，
      使「检查未执行」与「检查通过」在日志上可区分。
    """
    n = 0
    skipped: list[str] = []
    for table, expect in cfg.expected_bars.items():
        tc = _time_col(session, table)
        if tc is None:
            skipped.append(table)
            logger.error(
                f"[data_selfcheck] {table} **缺失检查未执行**：找不到可识别的时间列"
                f"（候选 {_TIME_COL_CANDIDATES}）—— 该表的 bar 缺失无人巡检")
            continue
        try:
            rows = session.execute(text(f"""
                SELECT symbol, count(*) AS c
                FROM {table}
                WHERE {tc}::date = :d
                GROUP BY symbol
            """), {"d": trade_date}).fetchall()
        except Exception as e:  # noqa: BLE001
            session.rollback()
            skipped.append(table)
            logger.error(f"[data_selfcheck] {table}（用列 {tc}）缺失检查**未执行**：{e}")
            continue
        for sym, c in rows:
            if c < expect * (1 - cfg.missing_pct_alarm):
                _log_ticket(session, sym, trade_date, f"{table}.missing_bars",
                            f"{table} {trade_date} 仅 {c} 根，预期 {expect} "
                            f"（缺失 {1 - c/expect:.1%}）",
                            diff=float(expect - c) / expect)
                n += 1
    if skipped:
        # fail-loud：让「有表没被检查」在结果里显式可见
        logger.error(f"[data_selfcheck] 本轮有 {len(skipped)} 张表**未被检查**：{skipped}")
    return n


def check_zero_volume(session, cfg: SelfCheckConfig, trade_date: date) -> int:
    """成交量连续为 0 的品种（非停板时属异常）。"""
    n = 0
    since = trade_date - timedelta(days=cfg.zero_volume_days)
    try:
        rows = session.execute(text("""
            SELECT symbol, count(*) FILTER (WHERE volume = 0) AS z, count(*) AS c
            FROM daily_bar
            WHERE trade_date > :s AND trade_date <= :d
            GROUP BY symbol
            HAVING count(*) > 0 AND count(*) FILTER (WHERE volume = 0) = count(*)
        """), {"s": since, "d": trade_date}).fetchall()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        # ★ 2026-10-09：升为error。原为 warning + return 0 —— 上层只看到
        # 「0 工单」，与「零成交检查通过」完全一样，属静默失效。
        logger.error(f"[data_selfcheck] 零成交检查**未执行**：{e}")
        return 0
    for sym, z, c in rows:
        _log_ticket(session, sym, trade_date, "daily_bar.zero_volume",
                    f"连续 {c} 个交易日成交量为 0（{since}~{trade_date}）")
        n += 1
    return n


def check_tail_regression(session, cfg: SelfCheckConfig, trade_date: date) -> int:
    """尾部回退：源表最新时间戳是否早于上一交易日（应对尾删重建竞态）。

    ★ 2026-10-09：改用 :func:`_time_col` 探测列名，替代原先"先试 bucket 再试 ts"
      的双层 try（后者对 ``bar_15m`` 能撞对，但表一多就容易漏；且失败仅
      ``warning``，与「检查通过」无法区分）。
    """
    n = 0
    for table in ("bar_15m", "bar_30m", "bar_60m", "minute_bar"):
        tc = _time_col(session, table)
        if tc is None:
            logger.error(f"[data_selfcheck] {table} **尾部回退检查未执行**：无时间列")
            continue
        try:
            mx = session.execute(text(f"SELECT max({tc}) FROM {table}")).scalar()
        except Exception as e:  # noqa: BLE001
            session.rollback()
            logger.error(f"[data_selfcheck] {table}（用列 {tc}）尾部检查**未执行**：{e}")
            continue
        if mx is None:
            logger.error(f"[data_selfcheck] {table} 全表为空，尾部回退检查无从判断")
            continue
        d = mx.date() if hasattr(mx, "date") else mx
        if d < trade_date - timedelta(days=3):
            _log_ticket(session, f"__{table}__", trade_date,
                        f"{table}.tail_regression",
                        f"{table} 最新时间戳 {mx}，早于 {trade_date} 3 天以上"
                        f"—— 疑似上游尾删重建未完成")
            n += 1
    return n


def run(session, cfg: SelfCheckConfig | None = None,
        trade_date: date | None = None) -> dict:
    """执行全部自检，返回 {检查项: 工单数}。cfg.enabled=False 时直接跳过。"""
    cfg = cfg or SelfCheckConfig()
    if not cfg.enabled:
        logger.info("[data_selfcheck] 已关闭（data_selfcheck.enabled=false）")
        return {"skipped": True}
    trade_date = trade_date or date.today()
    res = {
        "missing": check_missing(session, cfg, trade_date),
        "zero_volume": check_zero_volume(session, cfg, trade_date),
        "tail_regression": check_tail_regression(session, cfg, trade_date),
    }
    res["total"] = sum(v for v in res.values() if isinstance(v, int))
    logger.info(f"[data_selfcheck] {trade_date} 完成：{res}")
    return res
