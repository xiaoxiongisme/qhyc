# -*- coding: utf-8 -*-
"""按「交易小节 60/30 分钟」正确规则重建 60m / 30m bar（替换 time_bucket 整点切）。

## 为什么原来的做法是错的
`app/ingest/synthesizer.py` 用 TimescaleDB `time_bucket('60 minutes', ts)`，是按
**自然时间整点**切桶，于是 SC888 一天得到 09:00 10:00 11:00 13:00 14:00 15:00
21:00 22:00 23:00 这种"整点网格"，丢失了每个交易小节末尾不足 60 分钟的那一段，
EMA/ATR 等指标的 bar 含义被扭曲。

## 正确规则（用户口径：同花顺小时 K，一个交易日 ≥6 根）
用户给出的 SC 参照：`10:00 11:15 14:15 15:00 22:00 23:00`
反推得到的**唯一**能精确复现该序列的规则（已用 SC888 2023-06-15 实测吻合）：
1. 按自然日（Asia/Shanghai）分组，一个交易日 = 日盘 + 当日夜盘；
2. 在该组内把分钟按 ts 升序串起来，**跳过午休等小节间休市继续累计**
   （即只看有没有分钟数据，不看墙钟），每 60 个交易分钟成一根 bar；
3. 相邻分钟间隔 > --max-gap 分钟（默认 150，> 商品最长午休 120，
   < 日盘到夜盘的 360）或跨自然日 → 强制收尾，剩余部分单独成一根；
4. bar 时间标签 = 该 bar 内最后一根分钟的 ts + 1 分钟（即 bar 的结束边界）。
   例：09:00-10:00 → 10:00；10:00~11:15（含 10:15-10:30 休市）→ 11:15；
       11:15~14:15（含午休）→ 14:15；余 14:15-15:00 → 15:00。

## 多源分钟去重
l0_raw.minute_bar 有 kqm_1m / wh9999_1m / tqsdk_1min / akshare_1min 多个 src，
同一 (symbol, ts) 可能多行 → 按 --src-priority 取优先级最高的一行，避免重复计数。

## 用法（安全：默认 dry-run）
    set -a; . /home/ubuntu/qhyc_work/qhyc.env; set +a
    cd /Docker/qhyc && PYTHONPATH=/Docker/qhyc venv/bin/python /tmp/rebuild_bars_session.py
        --symbols SC888,RB888 --start 2023-06-01 --end 2023-06-30        # 看形态
        --apply --symbols ALL --start 2015-01-01 --end 2026-10-31        # 真正写库
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from sqlalchemy import text

from app.core.db import session_scope

DEFAULT_PRIORITY = ["kqm_1m", "wh9999_1m", "tqsdk_1min", "akshare_1min"]

SQL_AGG = """
WITH m AS (
    SELECT symbol, ts, open, high, low, close, volume, amount, open_interest, src,
           (ts AT TIME ZONE 'Asia/Shanghai')::date d,
           lag(ts) OVER (PARTITION BY symbol ORDER BY ts) prev
    FROM l0_raw.minute_bar
    WHERE ts >= :s AND ts < :e
      {sym_filter}
      AND src = ANY(:srcs)
), dedup AS (
    SELECT DISTINCT ON (symbol, ts) *
    FROM m
    ORDER BY symbol, ts, array_position(:srcs, src)
), gapd AS (
    -- 只在「相邻分钟间隔超过阈值」处切断（日盘↔夜盘间隔 360 分钟必然切断，
    -- 午休 120 分钟 / 小节休市 15 分钟不切断）。
    -- ⚠ 不再按自然日切断：夜盘跨午夜是连续的，切断会产生 23:59 / 01:01 这类碎片 bar。
    SELECT *,
           CASE WHEN prev IS NULL
                     OR extract(epoch FROM (ts - prev)) / 60 > :maxgap
                THEN 1 ELSE 0 END AS brk
    FROM dedup
), segs AS (
    SELECT *, sum(brk) OVER (PARTITION BY symbol ORDER BY ts) grp FROM gapd
), ranked AS (
    SELECT *, (row_number() OVER (PARTITION BY symbol, grp ORDER BY ts) - 1) / :step bucket,
           min(ts) OVER (PARTITION BY symbol, grp) grp_first
    FROM segs
)
SELECT symbol,
       -- ★ 标签＝该 bar 最后一根分钟的 ts，再按该源的「分钟时间戳约定」补偏移：
       --   kqm_1m / tqsdk_1min 记分钟【起点】(09:00…14:59) → 需 +1min 才是 bar 收边界
       --   akshare_1min / wh9999_1m 记分钟【终点】(09:01…15:00) → 本身就是收边界，+0
       -- 判定依据：本连续段首根分钟的分钟数能否被 30 整除（整点/半点=起点约定）
       max(ts) + CASE WHEN mod(extract(minute FROM (min(grp_first)
                          AT TIME ZONE 'Asia/Shanghai'))::int, 30) = 0
                      THEN interval '1 minute' ELSE interval '0 minute' END
           AS bucket_end,
       (array_agg(open  ORDER BY ts))[1] AS open,
       max(high)  AS high,
       min(low)   AS low,
       (array_agg(close ORDER BY ts DESC))[1] AS close,
       sum(volume) AS volume,
       sum(amount) AS amount,
       (array_agg(open_interest ORDER BY ts DESC))[1] AS open_interest,
       count(*) AS n_minutes
FROM ranked
GROUP BY symbol, grp, bucket
"""


def _agg_sql(symbols: list[str] | None) -> str:
    return SQL_AGG.format(sym_filter="AND symbol = ANY(:syms)" if symbols else "")


def run(symbols: list[str] | None, start: str, end: str, step: int,
        maxgap: int, priority: list[str], apply: bool, target: str,
        limit_days: int | None) -> int:
    t0 = datetime.now(tz=timezone.utc)
    s_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    e_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
    params = {"s": s_dt, "e": e_dt, "step": step, "maxgap": maxgap, "srcs": priority}
    if symbols:
        params["syms"] = symbols

    with session_scope() as sess:
        rows = sess.execute(text(_agg_sql(symbols)), params).fetchall()
    print(f"聚合结果：{len(rows):,} 根 {step}m bar   "
          f"（耗时 {(datetime.now(tz=timezone.utc)-t0).total_seconds():.1f}s）")

    # 形态对照：抽若干 symbol × 自然日
    by_day: dict[tuple[str, str], list] = {}
    for r in rows:
        end_ = r[1]
        if end_ is None:
            continue
        day = (end_.astimezone(timezone.utc).astimezone(
            __import__("zoneinfo").ZoneInfo("Asia/Shanghai"))).strftime("%Y-%m-%d")
        by_day.setdefault((r[0], day), []).append(end_)
    print("\n=== 形态抽验（每天 bar 时刻）===")
    shown = 0
    for (sym, day), ts_list in sorted(by_day.items()):
        if limit_days and shown >= limit_days:
            break
        ts_list = sorted(ts_list)
        times = " ".join(t.astimezone(
            __import__("zoneinfo").ZoneInfo("Asia/Shanghai")).strftime("%H:%M")
            for t in ts_list)
        print(f"  {sym:<8}{day}  n={len(ts_list):>2}  {times}")
        shown += 1

    total_days = len(by_day)
    avg = len(rows) / total_days if total_days else 0
    print(f"\n  交易日数={total_days:,}  日均 {step}m bar = {avg:.2f} 根")

    if not apply:
        print("\n[dry-run] 未写库。确认形态正确后加 --apply")
        return 0

    print(f"\n[apply] 写入 {target}（仅替换本次能重建出来的自然日，杜绝空洞）…")
    tbl = {60: "bar_60m", 30: "bar_30m"}[step]
    days = sorted({d for _, d in by_day})
    if not days:
        print("  无产出，跳过")
        return 0
    print(f"  覆盖自然日 {len(days)} 天：{days[0]} ~ {days[-1]}")

    cols = ("symbol", "bucket", "open", "high", "low", "close",
            "volume", "amount", "open_interest")
    ins = text(f"""
        INSERT INTO l1_mkt.{tbl} (symbol, bucket, open, high, low, close,
                                  volume, amount, open_interest)
        VALUES (:symbol, :bucket, :open, :high, :low, :close,
                :volume, :amount, :open_interest)
    """)
    with session_scope() as sess:
        dp = dict(params)
        dp["days"] = [datetime.fromisoformat(d).date() for d in days]
        res = sess.execute(text(
            f"DELETE FROM l1_mkt.{tbl} WHERE symbol = ANY(:syms) "
            f"AND (bucket AT TIME ZONE 'Asia/Shanghai')::date = ANY(:days)"
        ) if symbols else text(
            f"DELETE FROM l1_mkt.{tbl} "
            f"WHERE (bucket AT TIME ZONE 'Asia/Shanghai')::date = ANY(:days)"
        ), dp)
        print(f"  已删除旧数据 {res.rowcount:,} 行")

        batch: list[dict] = []
        n = 0
        for r in rows:
            d = dict(zip(cols + ("n_minutes",), r))
            if d.get("bucket") is None:
                continue
            batch.append({k: d.get(k) for k in cols})
            if len(batch) >= 5000:
                sess.execute(ins, batch)
                n += len(batch)
                batch = []
        if batch:
            sess.execute(ins, batch)
            n += len(batch)
        print(f"  已写入 {n:,} 行到 l1_mkt.{tbl}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="SC888", help="逗号分隔，或 ALL 表示全部")
    ap.add_argument("--start", default="2023-06-01")
    ap.add_argument("--end", default="2023-06-30")
    ap.add_argument("--step", type=int, default=60, choices=[30, 60])
    ap.add_argument("--max-gap", type=int, default=150)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit-days", type=int, default=12)
    args = ap.parse_args()

    symbols = None if args.symbols == "ALL" else [x.strip() for x in args.symbols.split(",")]
    if args.symbols == "ALL":
        with session_scope() as sess:
            symbols = [r[0] for r in sess.execute(text(
                "SELECT DISTINCT symbol FROM l0_raw.minute_bar ORDER BY symbol"))]
        print(f"[ALL] 分钟表品种数 = {len(symbols)}")

    if symbols is None or len(symbols) <= 1:
        return run(symbols, args.start, args.end, args.step, args.max_gap,
                   DEFAULT_PRIORITY, args.apply, f"l1_mkt.bar_{args.step}m",
                   args.limit_days)

    print(f"[逐个品种] 共 {len(symbols)} 个")
    tot = 0
    failed: list[str] = []
    for i, sym in enumerate(symbols, 1):
        print(f"\n{'='*70}\n[{i}/{len(symbols)}] {sym}")
        try:
            run([sym], args.start, args.end, args.step, args.max_gap,
                DEFAULT_PRIORITY, args.apply, f"l1_mkt.bar_{args.step}m", 0)
            tot += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ❌ {type(e).__name__}: {str(e)[:200]}")
            failed.append(sym)
    print(f"\n完成 {tot}/{len(symbols)}；失败：{failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
