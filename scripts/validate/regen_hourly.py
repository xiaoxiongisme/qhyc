# -*- coding: utf-8 -*-
"""重建 hourly_bar(src=akshare) 全历史为正确的同花顺 60m 固定网格。

根因：生产回测 fusion_backtest 读 HourlyBar(src=akshare)。该表历史段（~2021 至
akshare 新浪窗口之前）由旧整点逻辑写入，之后定时采集只刷新近 ~8 个月，历史段永远
碰不到 → 回测看到的是错的整点棒。bar_60m/bar_30m 已正确，但回测不读它们。

做法：复用 rebuild_bars_session 的固定网格聚合（日盘标准边界 10:00/11:15/14:15/15:00，
夜盘从 21:00 起按 60m 递增、截断到自然收盘），把分钟表聚合结果写入 hourly_bar
(trade_datetime=bucket, oi=open_interest, src='akshare')。

PK 为 (symbol, trade_datetime)，不含 src；agg_1m 与 akshare 时间不重叠，INSERT 用
ON CONFLICT DO NOTHING 跳过边界极少数撞键（保留 agg_1m 的正确数据）。

⚠ 分块写：hourly_bar 是 TimescaleDB 压缩超表。对 SH/AG/LC 等大数据品种，单次 DELETE
跨全历史（~12 年、上百个压缩 chunk）会触发 "tuple decompression limit exceeded"。
因此按年分块：每年一次 DELETE（仅该年范围）+ 分批 INSERT，单次操作只碰 ~1 年的 chunk。

用法：
  set -a; . /home/ubuntu/qhyc_work/qhyc.env; set +a
  cd /Docker/qhyc; export PYTHONPATH=/Docker/qhyc
  /home/ubuntu/qhyc_work/venv/bin/python scripts/validate/regen_hourly.py --apply --symbols SH888,AG888,LC888
  /home/ubuntu/qhyc_work/venv/bin/python scripts/validate/regen_hourly.py --apply   # 全品种
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import session_scope
from scripts.validate.rebuild_bars_session import (
    DEFAULT_PRIORITY, END_LABELED_SRCS, _build_sql,
)


def _regen_symbol(sym: str, start: str, end: str, maxgap: int, apply: bool,
                  limit_days: int) -> dict:
    t0 = datetime.now(tz=timezone.utc)
    s_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    e_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
    params = {"s": s_dt, "e": e_dt, "maxgap": maxgap,
              "srcs": DEFAULT_PRIORITY, "end_srcs": END_LABELED_SRCS,
              "syms": [sym]}

    with session_scope() as sess:
        sql = _build_sql(60, "AND symbol = ANY(:syms)")
        rows = sess.execute(text(sql), params).fetchall()

    # 形态抽验（每天 bar 时刻）
    from zoneinfo import ZoneInfo
    SH = ZoneInfo("Asia/Shanghai")
    by_day: dict[str, list] = {}
    for r in rows:
        end_ = r[1]
        if end_ is None:
            continue
        day = end_.astimezone(SH).strftime("%Y-%m-%d")
        by_day.setdefault((sym, day), []).append(end_)
    print(f"\n  [{sym}] 聚合 {len(rows):,} 根 60m（耗时 "
          f"{(datetime.now(tz=timezone.utc)-t0).total_seconds():.1f}s）")
    shown = 0
    for (s, day), ts_list in sorted(by_day.items()):
        if limit_days and shown >= limit_days:
            break
        ts_list = sorted(ts_list)
        times = " ".join(t.astimezone(SH).strftime("%H:%M") for t in ts_list)
        print(f"    {day}  n={len(ts_list):>2}  {times}")
        shown += 1

    if not apply:
        print("  [dry-run] 未写库")
        return {"sym": sym, "n": len(rows), "applied": False}

    cols = ("symbol", "trade_datetime", "open", "high", "low", "close",
            "volume", "oi", "src")
    # 按年分块写，规避 hourly_bar 压缩超表单次 DELETE/INSERT 跨太多 chunk 触发
    # "tuple decompression limit exceeded"（SH/AG/LC 等大数据品种全程会越界）。
    # 每年独立事务：删除该 symbol 该年范围旧 akshare 行，再分批插入新聚合结果。
    years = list(range(s_dt.year, e_dt.year + 1))
    tot_written = 0
    tot_deleted = 0
    for y in years:
        y0 = datetime(y, 1, 1, tzinfo=timezone.utc)
        y1 = datetime(y + 1, 1, 1, tzinfo=timezone.utc)
        ys = max(s_dt, y0)
        ye = min(e_dt, y1)
        if ys >= ye:
            continue
        rows_y = [r for r in rows if r[1] is not None and ys <= r[1] < ye]
        if not rows_y:
            continue
        with session_scope() as sess:
            res = sess.execute(text(
                "DELETE FROM l0_raw.hourly_bar WHERE src='akshare' AND symbol=:sym "
                "AND trade_datetime >= :s AND trade_datetime < :e"
            ), {"sym": sym, "s": ys, "e": ye})
            d = res.rowcount
            tot_deleted += d
            batch = []
            n = 0
            for r in rows_y:
                dd = dict(zip(
                    ("symbol", "bucket", "open", "high", "low", "close",
                     "volume", "amount", "open_interest", "n_minutes"), r))
                if dd.get("bucket") is None:
                    continue
                batch.append({
                    "symbol": dd["symbol"], "trade_datetime": dd["bucket"],
                    "open": dd["open"], "high": dd["high"], "low": dd["low"],
                    "close": dd["close"], "volume": dd["volume"],
                    "oi": dd["open_interest"], "src": "akshare",
                })
                if len(batch) >= 2000:
                    sess.execute(text(
                        "INSERT INTO l0_raw.hourly_bar "
                        "(symbol, trade_datetime, open, high, low, close, volume, oi, src) "
                        "VALUES (:symbol, :trade_datetime, :open, :high, :low, :close, "
                        ":volume, :oi, :src) "
                        "ON CONFLICT (symbol, trade_datetime) DO NOTHING"
                    ), batch)
                    n += len(batch)
                    batch = []
            if batch:
                sess.execute(text(
                    "INSERT INTO l0_raw.hourly_bar "
                    "(symbol, trade_datetime, open, high, low, close, volume, oi, src) "
                    "VALUES (:symbol, :trade_datetime, :open, :high, :low, :close, "
                    ":volume, :oi, :src) "
                    "ON CONFLICT (symbol, trade_datetime) DO NOTHING"
                ), batch)
                n += len(batch)
            tot_written += n
        print(f"    [{y}] 删除 {d:,} / 写入 {n:,}")
    print(f"    总删除 {tot_deleted:,} / 写入 {tot_written:,} 行 (src=akshare)")
    return {"sym": sym, "n": len(rows), "applied": True}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="ALL")
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2026-12-31")
    ap.add_argument("--max-gap", type=int, default=150)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit-days", type=int, default=3)
    args = ap.parse_args()

    if args.symbols == "ALL":
        specs = get_settings().main_contracts
        symbols = [s.symbol for s in specs]
    else:
        symbols = [x.strip() for x in args.symbols.split(",")]

    print(f"[regen_hourly] 品种数={len(symbols)} apply={args.apply}")
    tot = 0
    failed = []
    for i, sym in enumerate(symbols, 1):
        print(f"\n{'='*60}\n[{i}/{len(symbols)}] {sym}")
        try:
            r = _regen_symbol(sym, args.start, args.end, args.max_gap,
                              args.apply, args.limit_days)
            if r["applied"]:
                tot += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ {type(e).__name__}: {str(e)[:300]}")
            failed.append(sym)
    print(f"\n完成（写库）{tot}/{len(symbols)}；失败：{failed}")


if __name__ == "__main__":
    main()
