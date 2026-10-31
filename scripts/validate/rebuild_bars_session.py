# -*- coding: utf-8 -*-
"""按「交易小节 60/30 分钟」正确规则重建 60m / 30m bar（替换 time_bucket 整点切）。

## 为什么原来的做法是错的
`app/ingest/synthesizer.py` 用 TimescaleDB `time_bucket('60 minutes', ts)`，是按
**自然时间整点**切桶，于是 SC888 一天得到 09:00 10:00 11:00 13:00 14:00 15:00
21:00 22:00 23:00 这种"整点网格"，丢失了每个交易小节末尾不足 60 分钟的那一段，
EMA/ATR 等指标的 bar 含义被扭曲。

## 正确规则（用户口径：同花顺小时 K，一个交易日 ≥6 根）
用户给出的 SC 参照：`10:00 11:15 14:15 15:00 22:00 23:00`
→ 锚定到**交易所固定的交易时段网格**（不是"数分钟"，否则缺失分钟会让后续
bar 整体偏移 1 分钟，实测全库 ~7% 的 bar 因此偏离网格）：
1. 连续交易段（gap>--max-gap 或日盘↔夜盘 360 断开）内判定日盘/夜盘：
   段内出现任一 9~14 点分钟 = 日盘；否则（21~23/0~2 点）= 夜盘；
2. **日盘**：从 09:00 起，跳过两段休市（10:15-10:30 与 11:30-13:30），边界固定在
   60m → 10:00/11:15/14:15/15:00；30m → 上述各点加中点（避开休市）；
3. **夜盘**：从 21:00 起无休市，每 step 分钟一根，最后一段截断到该段自然收盘
   （可容纳 23:00/01:00/02:30 等不同夜盘长度）；
4. 锚点用**固定墙钟**（09:00/21:00 on seg_date），即使首根分钟缺失也不偏移；
   缺失分钟只让某根 bar 少 1 分钟，标签永远落在固定网格上。

## 多源分钟去重
l0_raw.minute_bar 有 kqm_1m / wh9999_1m / tqsdk_1min / akshare_1min 多个 src，
同一 (symbol, ts) 可能多行 → 按 --src-priority 取优先级最高的一行，避免重复计数。
分钟时间戳约定：kqm_1m / tqsdk_1min 记分钟【起点】；akshare_1min / wh9999_1m
记分钟【终点】（聚合前 -1min 归一）。

## 用法（安全：默认 dry-run）
    set -a; . /home/ubuntu/qhyc_work/qhyc.env; set +a
    cd /Docker/qhyc && PYTHONPATH=/Docker/qhyc venv/bin/python /tmp/rebuild_bars_session.py
        --symbols SC888,RB888 --start 2023-06-01 --end 2023-06-30        # 看形态
        --apply --symbols ALL --start 2015-01-01 --end 2026-10-31        # 真正写库
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from sqlalchemy import text, event as _sa_event

from app.core.db import session_scope, get_engine


def _patch_engine_guc() -> None:
    """在引擎每个新连接上解除压缩超表整块解压上限。

    仅依赖事务内 `SET` 会被连接池/事务边界重置而失效；挂在 connect 事件上，
    每次物理连接建立即生效，持久于该连接生命周期，彻底规避
    `max_tuples_decompressed_per_dml_transaction` 默认 100000 上限导致的重建中断。
    """
    eng = get_engine()
    if getattr(eng, "_qhyc_guc_patched", False):
        return

    def _on_connect(dbapi_conn, _record):
        # 原始 DBAPI 连接：用 cursor 执行，并以 autocommit 确保 SET 在连接建立时
        # 立即持久生效（否则会被后续 SQLAlchemy 事务边界回滚而失效）。
        old_iso = dbapi_conn.isolation_level
        try:
            dbapi_conn.set_isolation_level(0)  # autocommit
            cur = dbapi_conn.cursor()
            cur.execute("SET timescaledb.max_tuples_decompressed_per_dml_transaction = 0")
            cur.close()
        finally:
            dbapi_conn.set_isolation_level(old_iso)

    _sa_event.listen(eng, "connect", _on_connect)
    eng._qhyc_guc_patched = True  # type: ignore[attr-defined]

DEFAULT_PRIORITY = ["kqm_1m", "wh9999_1m", "tqsdk_1min", "akshare_1min"]

# =============================================================================
# ★ 正确做法：bar 边界锚定到「交易所固定的交易时段网格」，而非"数分钟"。
# -----------------------------------------------------------------------------
# 旧逻辑用 row_number()/step 数交易分钟，对**缺失分钟**脆弱：少一根就把后续所有
# bar 的计数边界整体偏移 1 分钟（SC 夜盘少 1 根 → 00:01/01:01/02:01），全库约 7%
# 的 bar 因此偏离网格。
#
# 新逻辑：边界固定在交易所时段上，缺失分钟只让某根 bar 少 1 分钟，标签不变。
#   · 日盘（seg 内出现 9~14 点的分钟 → is_day）：从 09:00 起，跳过两段休市
#       - 上午 10:15-10:30（15 分钟）
#       - 中午 11:30-13:30（120 分钟）
#     60m 固定边界（相对 09:00 的分钟偏移）：60, 135, 315, 360
#        → 10:00 / 11:15 / 14:15 / 15:00（与用户同花顺口径逐位吻合）
#     30m 固定边界：30, 60, 105, 135, 285, 315, 345, 360（14:45 为用户补充口径）
#   · 夜盘（其余 seg，从 21:00 起，无休市）：边界 = 21:00 + k*step，最后一段
#       残bar 截断到该 seg 自然收盘（max(ts_n)+1min），自然容纳 23:00/01:00/02:30
#       等不同夜盘长度。
# 锚点用**固定墙钟**（09:00 / 21:00 on seg_date），即使首根分钟缺失也不偏移。
# =============================================================================
#: 相对 09:00 的固定边界偏移（分钟），按 step 分。
#: 日盘边界每 step 一根；在两段休市（10:15-10:30、11:30-13:30）处"跳空"对应墙钟
#: 偏移即可自动跳过（休市区间本就无分钟数据，CASE 会把该段分钟映射到下一有效边界）。
#:   60m：10:00 / 11:15 / 14:15 / 15:00
#:   30m：09:30 / 10:00 / 10:45 / 11:15 / 13:45 / 14:15 / 14:45 / 15:00
#:         （14:45 为用户同花顺口径补充，旧逻辑漏掉）
#:   15m：晨 09:15..10:15(每15) → 10:45..11:30；午 13:45..15:00(每15)
#:   5m ：晨 09:05..10:15(每5)  → 10:35..11:30；午 13:35..15:00(每5)
#: 夜盘由 _build_sql 统一用 21:00 起每 step 一根 + 段尾截断处理（见文件头注释）。
DAY_BOUNDS = {
    60: [60, 135, 315, 360],
    30: [30, 60, 105, 135, 285, 315, 345, 360],
    15: [15, 30, 45, 60, 75, 105, 120, 135, 150, 285, 300, 315, 330, 345, 360],
    5:  [5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55, 60, 65, 70, 75,
         95, 100, 105, 110, 115, 120, 125, 130, 135, 140, 145, 150,
         275, 280, 285, 290, 295, 300, 305, 310, 315, 320, 325, 330, 335, 340, 345, 350, 355, 360],
}


def _build_sql(step: int, sym_filter: str) -> str:
    bounds = DAY_BOUNDS[step]
    # 日盘：rel_end = (ts_n+1min - day_start) 的分钟数；取 >= rel_end 的最小边界
    day_when = []
    prev = -1
    for b in bounds:
        if prev < 0:
            day_when.append(f"WHEN rel_end <= {b} THEN {b}")
        else:
            day_when.append(f"WHEN rel_end <= {b} THEN {b}")
        prev = b
    day_case = "CASE " + " ".join(day_when) + f" ELSE {bounds[-1]} END"

    return f"""
WITH m AS (
    SELECT symbol, ts, open, high, low, close, volume, amount, open_interest, src
    FROM l0_raw.minute_bar
    WHERE ts >= :s AND ts < :e
      {sym_filter}
      AND src = ANY(:srcs)
), dedup AS (
    SELECT DISTINCT ON (symbol, ts) *
    FROM m
    ORDER BY symbol, ts, array_position(:srcs, src)
), norm AS (
    -- 终点约定源（ts 记分钟终点）减 1 分钟归一到起点约定，后续只需一套公式
    SELECT *, ts - CASE WHEN src = ANY(:end_srcs)
                        THEN interval '1 minute' ELSE interval '0 minute' END AS ts_n
    FROM dedup
), gapd AS (
    -- 只在「相邻分钟间隔超过阈值」处切断：日盘↔夜盘 360 必断，午休/小节休市不切断
    SELECT *, CASE WHEN prev IS NULL
                     OR extract(epoch FROM (ts_n - prev)) / 60 > :maxgap
                THEN 1 ELSE 0 END AS brk
    FROM (SELECT *, lag(ts_n) OVER (PARTITION BY symbol ORDER BY ts_n) prev FROM norm) g
), segs AS (
    SELECT *, sum(brk) OVER (PARTITION BY symbol ORDER BY ts_n) AS grp FROM gapd
), segmeta AS (
    SELECT *, min(ts_n) OVER (PARTITION BY symbol, grp) AS seg_first,
             max(ts_n) OVER (PARTITION BY symbol, grp) AS seg_last
    FROM segs
), typed AS (
    -- 日盘判定：该连续段内出现任一 9~14 点（上午/下午）的分钟即判为日盘；
    -- 夜盘（21~23、0~2 点）永不会出现这些小时，必判为夜盘。
    SELECT *, (seg_first AT TIME ZONE 'Asia/Shanghai')::date AS seg_date,
           bool_or(EXTRACT(hour FROM ts_n AT TIME ZONE 'Asia/Shanghai') BETWEEN 9 AND 14)
               OVER (PARTITION BY symbol, grp) AS is_day
    FROM segmeta
), bucketed AS (
    SELECT symbol, open, high, low, close, volume, amount, open_interest, ts_n, is_day,
           CASE
             WHEN is_day THEN
               -- 日盘：固定墙钟网格（09:00 起），rel_end=本分钟收盘相对 09:00 的分钟数
               (seg_date + TIME '09:00') AT TIME ZONE 'Asia/Shanghai'
                 + ( {day_case} ) * interval '1 minute'
             ELSE
               -- 夜盘：21:00 起每 step 分钟一根，残段截断到 seg 自然收盘
               LEAST(
                 (seg_date + TIME '21:00') AT TIME ZONE 'Asia/Shanghai'
                   + ( floor( EXTRACT(epoch FROM (ts_n
                       - ((seg_date + TIME '21:00') AT TIME ZONE 'Asia/Shanghai')) ) / 60
                       / {step} ) + 1 ) * ({step} * interval '1 minute'),
                 seg_last + interval '1 minute'
               )
           END AS bucket_end
    FROM (
        SELECT *, EXTRACT(epoch FROM ((ts_n + interval '1 minute')
                      - ((seg_date + TIME '09:00') AT TIME ZONE 'Asia/Shanghai')))/60 AS rel_end
        FROM typed
    ) t
)
SELECT symbol, bucket_end AS bucket,
       (array_agg(open  ORDER BY ts_n))[1] AS open,
       max(high)  AS high,
       min(low)   AS low,
       (array_agg(close ORDER BY ts_n DESC))[1] AS close,
       sum(volume) AS volume,
       sum(amount) AS amount,
       (array_agg(open_interest ORDER BY ts_n DESC))[1] AS open_interest,
       count(*) AS n_minutes
FROM bucketed
GROUP BY symbol, bucket_end
"""


#: 分钟时间戳约定登记（实测）：这些源把 ts 记为分钟的【终点】
END_LABELED_SRCS = ["akshare_1min", "wh9999_1m"]


def _agg_sql(symbols: list[str] | None, step: int = 60) -> str:
    sym_filter = "AND symbol = ANY(:syms)" if symbols else ""
    return _build_sql(step, sym_filter)


def run(symbols: list[str] | None, start: str, end: str, step: int,
        maxgap: int, priority: list[str], apply: bool, target: str,
        limit_days: int | None) -> int:
    t0 = datetime.now(tz=timezone.utc)
    s_dt = datetime.fromisoformat(start).replace(tzinfo=timezone.utc)
    e_dt = datetime.fromisoformat(end).replace(tzinfo=timezone.utc)
    params = {"s": s_dt, "e": e_dt, "maxgap": maxgap, "srcs": priority,
              "end_srcs": END_LABELED_SRCS}
    if symbols:
        params["syms"] = symbols

    with session_scope() as sess:
        # bar_5m/bar_30m 等是压缩超表，整范围 DELETE+INSERT 会触发整块解压，
        # 默认 max_tuples_decompressed_per_dml_transaction=100000 上限会被突破 → 报错中断。
        # 一次性重建时解除该上限（本会话生效，不影响全局配置）。
        sess.execute(text("SET timescaledb.max_tuples_decompressed_per_dml_transaction = 0"))
        rows = sess.execute(text(_agg_sql(symbols, step)), params).fetchall()
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

    print(f"\n[apply] 写入 {target}（按完整时间范围删除旧数据后重插，杜绝跨日残留/空洞）…")
    tbl = {60: "bar_60m", 30: "bar_30m", 15: "bar_15m", 5: "bar_5m"}[step]
    if not rows:
        print("  无产出，跳过")
        return 0
    print(f"  时间范围 {start} ~ {end}（覆盖全部能重建的分钟区间）")

    cols = ("symbol", "bucket", "open", "high", "low", "close",
            "volume", "amount", "open_interest")
    ins = text(f"""
        INSERT INTO l1_mkt.{tbl} (symbol, bucket, open, high, low, close,
                                  volume, amount, open_interest)
        VALUES (:symbol, :bucket, :open, :high, :low, :close,
                :volume, :amount, :open_interest)
    """)
    with session_scope() as sess:
        # 解除压缩超表整块解压上限（仅本会话），避免 DELETE/INSERT 触发的整块
        # 解压超过默认 100000 上限而报错中断（一次性全量重建场景）。
        sess.execute(text("SET timescaledb.max_tuples_decompressed_per_dml_transaction = 0"))
        dp = dict(params)
        # ★ 按 symbol 整品种删除（不限定 bucket 范围）：全量重建本就替换该品种全部
        # 历史，去掉范围过滤可彻底避免「残留旧行未被删 → INSERT 主键冲突」。
        # （范围过滤在历史时区边界上会漏删部分旧行，已实测触发 duplicate key。）
        del_sql = (
            f"DELETE FROM l1_mkt.{tbl} WHERE symbol = ANY(:syms)"
            if symbols else
            f"DELETE FROM l1_mkt.{tbl} WHERE bucket >= :s AND bucket < :e"
        )
        res = sess.execute(text(del_sql), dp)
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
    ap.add_argument("--step", type=int, default=60, choices=[5, 15, 30, 60])
    ap.add_argument("--max-gap", type=int, default=150)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--limit-days", type=int, default=12)
    args = ap.parse_args()

    # 解除压缩超表整块解压上限（挂在引擎 connect 事件，对所有连接生效）
    _patch_engine_guc()

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
