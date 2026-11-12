# -*- coding: utf-8 -*-
"""L0 全天分钟校验与补缺（CL-20261112-09，每交易日 15:20）。

为什么需要它
------------
实时链路改为「只提交最近 N 根」（默认 120）之后，超过 N 根的长缺口不再由
实时重放自动补齐。这不是倒退：原先的补齐能力是**副作用**——每轮把 1024 根
历史窗口整个重提一遍，代价是 309 个跨源键被反复静默丢弃、每轮刷冲突留痕。
现在把「补缺」从实时链路里剥出来，交给一个**每天只跑一次、有明确窗口、
有结果表、有告警**的作业，行为可验证而不是碰运气。

口径
----
* 交易日 = 上一工作日夜盘 21:00 ~ 当日日盘 15:00（+20 分钟补尾）。
  周一交易日的夜盘是上周五 21:00，故窗口起点回退到上一个工作日，
  不能简单取「昨天 21:00」。见 :func:`l0_write_guard.trading_session_bounds`。
* 只检查 ``continuous`` 层的 53 个 888 主连（信号链口径）。``contract`` 层
  668 个合约由 ``cron_fetch_1min`` 的水位线增量 + 跨源占位过滤覆盖，
  全量日检代价过高且不影响决策链。
* **期望集合来自 akshare 接口本身**，不造交易日历、不推算「某品种今天应有
  几根」。不同品种夜盘长度/停牌差异极大，凭日历造期望必然误报。

「修正」的边界（用户 23:20 拍板 keep-first）
-------------------------------------------
只 **INSERT 缺失键**（``ON CONFLICT DO NOTHING`` 双保险）。
已被任何 src 占据的键一律跳过，异常值（volume<=0 / close<=0 / high<low）
**只统计并告警，绝不改写已有行**。要覆盖已有值需要另开决策，本模块不提供
这样的开关——避免「日终作业悄悄改写历史」这种更难发现的故障。

结果写 ``ops.l0_eod_check``（migrations/079），守卫 ``DATA-L0-EOD-RECONCILE``
据此判断「作业真跑过且缺口为零」，而不是只看作业流水 success。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.ingest.l0_write_guard import trading_session_bounds
from app.ingest.minute_collector import _MINUTE_BAR, MinuteCollector

_EXISTING_SQL = text(
    "SELECT ts, src, volume, close, high, low "
    "FROM l0_raw.minute_bar "
    "WHERE symbol = :sym AND layer = 'continuous' "
    "  AND ts >= :start AND ts <= :end"
)

_EOD_UPSERT = text(
    "INSERT INTO ops.l0_eod_check "
    " (trade_date, symbol, window_start, window_end, n_expected, n_existing, "
    "  n_missing, n_inserted, n_cross_src, n_anomaly, status) "
    "VALUES (:trade_date, :symbol, :start, :end, :n_expected, :n_existing, "
    "        :n_missing, :n_inserted, :n_cross_src, :n_anomaly, :status) "
    "ON CONFLICT (trade_date, symbol) DO UPDATE SET "
    "  window_start = EXCLUDED.window_start, window_end = EXCLUDED.window_end, "
    "  n_expected = EXCLUDED.n_expected, n_existing = EXCLUDED.n_existing, "
    "  n_missing = EXCLUDED.n_missing, n_inserted = EXCLUDED.n_inserted, "
    "  n_cross_src = EXCLUDED.n_cross_src, n_anomaly = EXCLUDED.n_anomaly, "
    "  status = EXCLUDED.status, checked_at = now()"
)


def _utc(dt: datetime) -> datetime:
    return dt.astimezone(timezone.utc)


def _has_bad_value(row: Any) -> bool:
    """异常值判据（只报告，不改写）：零/负成交量、非正收盘价、high < low。"""
    _ts, _src, vol, close, high, low = row
    if vol is None or vol <= 0:
        return True
    if close is None or close <= 0:
        return True
    if high is not None and low is not None and high < low:
        return True
    return False


def reconcile(
    session: Session,
    now: datetime | None = None,
    dry_run: bool = False,
    symbols: Iterable[str] | None = None,
    send_alerts: bool = True,
) -> dict[str, Any]:
    """校验一个完整交易日的 continuous 分钟并只补缺失键。

    ``dry_run=True`` 只计算不写库（用于灰度验收）；``symbols`` 可限定品种。
    返回汇总 dict；单品种异常不会中断整轮。
    """
    bounds = trading_session_bounds(now)
    if bounds is None:
        return {"skipped": "non_trading_day", "symbols": 0}
    start, end, trade_date = bounds

    from app.core.config import get_settings

    specs = list(get_settings().main_contracts)
    if symbols:
        want = {str(s) for s in symbols}
        specs = [s for s in specs if s.symbol in want]

    collector = MinuteCollector(session)
    total = {
        "n_expected": 0, "n_existing": 0, "n_missing": 0,
        "n_inserted": 0, "n_cross_src": 0, "n_anomaly": 0,
    }
    no_source: list[str] = []
    errors: list[str] = []

    for spec in specs:
        try:
            # 完整窗口（bars=0），不做实时裁切。
            rows = collector._fetch_akshare(spec, bars=0)
            in_window = [r for r in rows if start <= r["ts"] <= end]
            existing = session.execute(
                _EXISTING_SQL, {"sym": spec.symbol, "start": start, "end": end}
            ).all()
            existing_ts = {_utc(r[0]) for r in existing}
            n_cross = sum(
                1 for r in existing if (r[1] or "") != "akshare_1min"
            )
            n_bad = sum(1 for r in existing if _has_bad_value(r))
            missing = [
                r for r in in_window if _utc(r["ts"]) not in existing_ts
            ]

            n_inserted = 0
            remaining = missing
            if missing and not dry_run:
                for i in range(0, len(missing), 4000):
                    stmt = pg_insert(_MINUTE_BAR).values(
                        missing[i : i + 4000]
                    ).on_conflict_do_nothing(index_elements=["symbol", "ts"])
                    session.execute(stmt)
                session.commit()
                # 复查而不是拿「尝试插入行数」当成功数：并发/触发器/约束都可能
                # 让 INSERT 落地数少于候选数，守卫必须看到补后**真实**残留。
                after = session.execute(
                    _EXISTING_SQL,
                    {"sym": spec.symbol, "start": start, "end": end},
                ).all()
                after_ts = {_utc(r[0]) for r in after}
                remaining = [
                    r for r in in_window if _utc(r["ts"]) not in after_ts
                ]
                n_inserted = len(missing) - len(remaining)

            # n_missing 语义 = **补后仍缺**（守卫直接判 0），不是补前缺口。
            total["n_expected"] += len(in_window)
            total["n_existing"] += len(existing)
            total["n_missing"] += len(remaining)
            total["n_inserted"] += n_inserted
            total["n_cross_src"] += n_cross
            total["n_anomaly"] += n_bad
            if not in_window:
                no_source.append(spec.symbol)

            status = (
                "no_source" if not in_window
                else "gap_remaining" if remaining
                else "repaired" if n_inserted
                else "ok"
            )
            if not dry_run:
                session.execute(
                    _EOD_UPSERT,
                    {
                        "trade_date": trade_date,
                        "symbol": spec.symbol,
                        "start": start,
                        "end": end,
                        "n_expected": len(in_window),
                        "n_existing": len(existing),
                        "n_missing": len(remaining),
                        "n_inserted": n_inserted,
                        "n_cross_src": n_cross,
                        "n_anomaly": n_bad,
                        "status": status,
                    },
                )
                session.commit()
        except Exception as e:  # noqa: BLE001 —— 单品种失败不拖垮整轮
            logger.exception(f"[l0_eod] {spec.symbol} 校验失败: {e}")
            errors.append(f"{spec.symbol}: {type(e).__name__}: {e}")
            # 失败也要留一行 status='error'：否则守卫只能看到「结果表少几行」，
            # 无法区分「该品种无数据」与「校验本身抛异常」。
            if not dry_run:
                try:
                    session.rollback()
                    session.execute(
                        _EOD_UPSERT,
                        {
                            "trade_date": trade_date, "symbol": spec.symbol,
                            "start": start, "end": end,
                            "n_expected": 0, "n_existing": 0, "n_missing": 0,
                            "n_inserted": 0, "n_cross_src": 0, "n_anomaly": 0,
                            "status": "error",
                        },
                    )
                    session.commit()
                except Exception as inner:  # noqa: BLE001
                    logger.error(f"[l0_eod] 写失败标记也失败: {inner}")

    result: dict[str, Any] = {
        "trade_date": str(trade_date),
        "window": [start.isoformat(), end.isoformat()],
        "symbols": len(specs),
        "dry_run": bool(dry_run),
        **total,
        "no_source_symbols": no_source[:20],
        "n_no_source": len(no_source),
        "errors": errors[:20],
        "n_errors": len(errors),
    }
    logger.info(f"[l0_eod] reconcile done: {result}")

    # 节假日整轮都没有当日数据属于预期（作业按 weekday 跑，没有交易日历），
    # 只有「部分品种无数据」才值得告警，否则每个假期都推一条假告警。
    partial_no_source = bool(no_source) and len(no_source) < max(1, len(specs) // 2)
    if send_alerts and not dry_run and (
        total["n_missing"] or total["n_anomaly"] or errors or partial_no_source
    ):
        try:
            from app.ops.alerting import alert_config_from_env, send_alert

            send_alert(
                title="L0 日终校验：需人工关注",
                content=(
                    f"交易日 {trade_date}，窗口 {start:%m-%d %H:%M}~{end:%H:%M}。\n"
                    f"缺失键 {total['n_missing']}（已补 {total['n_inserted']}）、"
                    f"异常值 {total['n_anomaly']}（只报告未改写）、"
                    f"接口无当日数据 {len(no_source)} 个品种、"
                    f"校验异常 {len(errors)} 个品种。\n"
                    f"明细：SELECT * FROM ops.l0_eod_check "
                    f"WHERE trade_date = '{trade_date}'。"
                ),
                cfg=alert_config_from_env(),
                key="l0_eod_reconcile",
            )
        except Exception as e:  # noqa: BLE001 —— 告警失败不影响校验结果
            logger.error(f"[l0_eod] 告警异常（已忽略）: {e}")

    return result


__all__ = ["reconcile"]
