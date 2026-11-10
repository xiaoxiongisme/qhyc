"""用 akshare 低周期原生数据补齐 L1 分钟线缺口（CL-20261109-20）。

背景
----
1 分钟接口 ``futures_zh_minute_sina(period="1")`` 实测**硬上限 1023 根**，
按 BR0/PF0 的密度只够约 3 个交易日（2026-11-09 实测回溯至 11-05 09:13），
超出窗口的 1m 缺口**永久不可重建**（local_csv 快照亦止于 2026-10-30）。

但同一接口的低周期**回溯深得多**，2026-11-09 实测：

    period=5   1023 根 → 2026-10-20 10:05   约 15 个交易日
    period=15  1023 根 → 2026-08-28 22:15   约 50 个交易日
    period=30  1023 根 → 2026-07-03 10:00
    period=60  1023 根 → 2026-02-25 23:00   约 8.5 个月

故 5m/15m/30m/60m 的缺口**可以**用 5m 原生重建，只有 1m 不行。

为什么要从 5m 派生而不是各频直接用原生
--------------------------------------
四个周期的原生接口是**各自独立采样**的（实测 2026-11-06 BR888：
15m 有 5/23 根 OHLC 与 1m 聚合口径不同，60m 有 2/6 根）。若各频各用原生，
``check_cross_tf_consistency``（30m←15m、60m←30m）必然报不一致。
因此本脚本**只把 5m 原生当唯一源**，15/30/60 一律由 ``_build_sql`` 派生 ——
与生产重建脚本**同一份 SQL**，保证网格/会话边界/夜盘截断口径逐字一致。

时间戳口径
----------
新浪 5m 的时间戳是 bar 的**终点**（09:05 表示 09:00~09:05），与库内
``l1_mkt.bar_5m.bucket`` 语义相同。故写入 stage 表时 src 标为
``akshare_5m_sina`` 并登记进 ``end_srcs``，聚合 SQL 会自动 -1 分钟归一到
起点约定，无需在此手工平移。

用法
----
    # 先看每步会写多少根，不落库
    python scripts/ops/backfill_bars_from_5m.py \\
        --symbols BR888,PF888 --days 2026-10-30,2026-11-02,2026-11-03

    # 确认后落库
    python scripts/ops/backfill_bars_from_5m.py \\
        --symbols BR888,PF888 --days 2026-10-30,2026-11-02,2026-11-03 --apply

    # 自动探测缺口日（按「前夜盘有无」分组，真源 public.futures_rule）
    python scripts/ops/backfill_bars_from_5m.py --symbols BR888 --auto-days 30 --apply

写完 L1 后仍需重建 BA 层（后复权因子/换月段依赖 bar 序列）：

    python scripts/ops/rebuild_roll_all_freqs.py --symbols BR888,PF888 \\
        --freqs min5,min15,min30,min60 --force-ba

⚠ 自动探测的交易日形态判据见 ``scripts/ops/trading_day_caliber.py``
（CL-20261110-07）：节后首日与周一都**没有**前夜盘凌晨段，根数天然比
周二~周五少约 13 根，不能拿同星期几众数当基准，否则会把正常日误判成缺口。
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text

from app.core.db import session_scope
from scripts.ops.trading_day_caliber import (
    TRADING_DAY_SQL,
    has_prev_session,
    mode_of,
)
from scripts.validate.rebuild_bars_session import (
    END_LABELED_SRCS,
    _build_sql,
)

SH = ZoneInfo("Asia/Shanghai")
SRC = "akshare_5m_sina"
STAGE = "public.bar_backfill_stage"
TBL = {5: "bar_5m", 15: "bar_15m", 30: "bar_30m", 60: "bar_60m"}
COLS = ("symbol", "bucket", "open", "high", "low", "close",
        "volume", "amount", "open_interest")
#: 5m 相邻根间隔 5 分钟；日盘→夜盘 360 分钟必断；午休 20 分钟不断 ——
#: 与 1m 侧默认 150 的判据完全兼容。
MAXGAP = 150


def _stage_ddl() -> str:
    return f"""
    CREATE TABLE IF NOT EXISTS {STAGE} (
        symbol        text        NOT NULL,
        ts            timestamptz NOT NULL,
        open          numeric,
        high          numeric,
        low           numeric,
        close         numeric,
        volume        bigint,
        amount        numeric,
        open_interest numeric,
        src           text,
        PRIMARY KEY (symbol, ts)
    )"""


def fetch_5m(product: str) -> pd.DataFrame:
    """拉新浪主连 5 分钟全量（1023 根），返回带上海时区的 DataFrame。"""
    import akshare as ak  # type: ignore

    sina = f"{product.lower()}0"
    df = ak.futures_zh_minute_sina(symbol=sina, period="5")
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    df["dt"] = pd.to_datetime(df["datetime"]).dt.tz_localize(SH)
    for c in ("open", "high", "low", "close"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce")
    df["hold"] = pd.to_numeric(df.get("hold"), errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close"])


def day_window(day: str) -> tuple[datetime, datetime]:
    """自然日 [00:00, 次日 00:00) 上海时区。"""
    s = datetime.fromisoformat(day).replace(tzinfo=SH)
    return s, s + timedelta(days=1)


def load_stage(sess, sym: str, sub: pd.DataFrame) -> int:
    sess.execute(text(f"DELETE FROM {STAGE} WHERE symbol = :s"), {"s": sym})
    rows = [
        {
            "symbol": sym, "ts": r.dt.to_pydatetime(),
            "open": float(r.open), "high": float(r.high),
            "low": float(r.low), "close": float(r.close),
            "volume": int(r.volume) if pd.notna(r.volume) else None,
            "amount": None,
            "open_interest": int(r.hold) if pd.notna(r.hold) else None,
            "src": SRC,
        }
        for r in sub.itertuples()
    ]
    if rows:
        sess.execute(text(f"""
            INSERT INTO {STAGE} (symbol, ts, open, high, low, close, volume,
                                 amount, open_interest, src)
            VALUES (:symbol, :ts, :open, :high, :low, :close, :volume,
                    :amount, :open_interest, :src)"""), rows)
    return len(rows)


def aggregate(sess, sym: str, step: int, s_dt: datetime, e_dt: datetime) -> list:
    sql = _build_sql(step, "AND symbol = ANY(:syms)", src_table=STAGE)
    params = {
        "s": s_dt, "e": e_dt, "maxgap": MAXGAP,
        "srcs": [SRC], "end_srcs": END_LABELED_SRCS + [SRC],
        "syms": [sym],
    }
    return sess.execute(text(sql), params).fetchall()


def write_step(sess, sym: str, step: int, rows: list, s_dt: datetime,
               e_dt: datetime, apply: bool,
               missing_only: bool = False) -> tuple[int, int]:
    """按「聚合结果真实 min/max」收口删除窗口后重插。返回 (删除数, 插入数)。

    ``missing_only=True`` 时**不删**，只 INSERT 缺失桶（ON CONFLICT DO NOTHING）。
    用于「整天里只缺一小段」的场景（如 AO/SS 只缺 00:00~01:00 夜盘尾段）——
    此时整天重写会把本来由 1 分钟真实聚合出来的日盘也换成 5m 原生，
    平白降低与 l0 的自洽度，不划算。
    """
    buckets = [r[1] for r in rows if r[1] is not None]
    if not buckets:
        return 0, 0
    tbl = TBL[step]
    # 删除窗口收口到本自然日，且以真实 bucket 范围为界 —— 与 rebuild_bars_session
    # 的 CL-20261109-19 修复同一语义：绝不越过「本次真的会重写」的范围。
    dmin = max(min(buckets), s_dt)
    dmax = min(max(buckets) + timedelta(seconds=1), e_dt)
    if not apply:
        return 0, len(buckets)
    sess.execute(text("SET timescaledb.max_tuples_decompressed_per_dml_transaction = 0"))
    dl = 0
    if not missing_only:
        res = sess.execute(text(
            f"DELETE FROM l1_mkt.{tbl} WHERE symbol = :sym "
            f"AND bucket >= :a AND bucket < :b"),
            {"sym": sym, "a": dmin, "b": dmax})
        dl = res.rowcount
    conflict = ("ON CONFLICT (symbol, bucket) DO NOTHING" if missing_only else
                """ON CONFLICT (symbol, bucket) DO UPDATE SET
            open = EXCLUDED.open, high = EXCLUDED.high, low = EXCLUDED.low,
            close = EXCLUDED.close, volume = EXCLUDED.volume,
            amount = EXCLUDED.amount, open_interest = EXCLUDED.open_interest""")
    payload = [
        dict(zip(COLS, (r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8])))
        for r in rows if r[1] is not None
    ]
    sess.execute(text(f"""
        INSERT INTO l1_mkt.{tbl} (symbol, bucket, open, high, low, close,
                                  volume, amount, open_interest)
        VALUES (:symbol, :bucket, :open, :high, :low, :close,
                :volume, :amount, :open_interest)
        {conflict}
    """), payload)
    return dl, len(payload)


def auto_detect_days(sym: str, lookback: int) -> list[str]:
    """按「是否有前夜盘尾段」分组探测缺口日（CL-20261110-07）。

    ⚠ 分组键是**前夜盘有无**，不是星期几。这里踩过两次坑：

    1. 用全周众数（93）当基准 → 每个周一（80 根，无凌晨段）都被误判成缺口，
       把新浪周一凌晨吐出的合成脏 bar 当成真实数据补进 L1（AO/SS 事故）；
    2. 改成按星期几分组后，**节后首日**仍被误判：2026-10-08 是国庆后首个
       交易日（周四），前一日 10-07 休市、没有夜盘，实际只有 80 根，
       却拿去和「周四 93 根」比。

    真源 ``public.futures_rule``（``l3_ref.trade_calendar`` 实测停更到 10-08，
    不可使用）。判据与正反哨兵见 ``scripts/ops/trading_day_caliber.py``，
    由 ``BACKFILL-GAP-CALENDAR-WIRED`` 锁住真实引用。

    形态歧义保护：若某日根数低于本组众数、但与**另一组**众数吻合（例如
    「前一自然日是交易日但当天恰无夜盘」），按正常形态跳过而非报缺口 ——
    误补一根脏 bar 的代价远高于漏补一天。
    """
    with session_scope() as sess:
        daily = sess.execute(text("""
            SELECT bucket::date AS dd, count(*) AS n
            FROM l1_mkt.bar_5m
            WHERE symbol = :s AND bucket >= now() - (:lb || ' days')::interval
            GROUP BY 1 ORDER BY 1
        """), {"s": sym, "lb": lookback}).fetchall()
        if not daily:
            return []
        lo = min(r[0] for r in daily) - timedelta(days=2)
        hi = max(r[0] for r in daily)
        tdays = {r[0] for r in sess.execute(
            text(TRADING_DAY_SQL), {"lo": lo, "hi": hi}).fetchall()}

    if not tdays:
        raise RuntimeError(
            "public.futures_rule 在探测窗口内无交易日 —— 拒绝退回按星期几猜缺口"
            "（会重演 AO/SS 脏 bar 事故）。先跑 seed_futures_rule 补齐日历。")

    groups: dict[bool, list[tuple]] = {True: [], False: []}
    for dd, n in daily:
        groups[has_prev_session(dd, tdays)].append((dd, int(n)))

    baseline: dict[bool, int] = {}
    for g, items in groups.items():
        if len(items) < 3:
            print(f"    [跳过] 「{'有' if g else '无'}前夜盘」组仅 "
                  f"{len(items)} 天样本，不足以定基准")
            continue
        baseline[g] = mode_of([n for _, n in items])
    if not baseline:
        raise RuntimeError("两组样本均不足 3 天，无法求基准 —— 拒绝探测缺口")
    print(f"    基准：有前夜盘={baseline.get(True)} 根 / "
          f"无前夜盘={baseline.get(False)} 根"
          f"（真源 public.futures_rule，窗口内 {len(tdays)} 个交易日）")

    out = []
    for dd, n in sorted(groups[True] + groups[False]):
        # 周六凌晨的夜盘尾段（01:00/02:30 收盘的品种）只有零星几根，属正常
        if dd.weekday() == 5:
            continue
        g = has_prev_session(dd, tdays)
        mn = baseline.get(g)
        if mn is None or n >= mn * 0.95:
            continue
        other = baseline.get(not g)
        if other and n >= other * 0.95:
            print(f"    [形态] {sym} {dd} n={n} 与另一组合法形态 {other} 吻合"
                  f" → 不判缺口")
            continue
        out.append(str(dd))
        print(f"    [缺口] {sym} {dd}（周{'一二三四五六日'[dd.weekday()]}）"
              f" n={n} {'有' if g else '无'}前夜盘组众数={mn}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True, help="逗号分隔，如 BR888,PF888")
    ap.add_argument("--days", default="", help="逗号分隔的自然日，如 2026-11-02")
    ap.add_argument("--auto-days", type=int, default=0,
                    help="自动探测最近 N 天缺口（与 --days 二选一）")
    ap.add_argument("--steps", default="5,15,30,60")
    ap.add_argument("--missing-only", action="store_true",
                    help="只补库内缺失的桶，不删不改已有（适合整天只缺一小段）")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    symbols = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    steps = [int(x) for x in args.steps.split(",")]
    t0 = datetime.now(tz=timezone.utc)

    with session_scope() as sess:
        sess.execute(text(_stage_ddl()))

    total_w = 0
    for sym in symbols:
        prod = sym[:-3] if sym.endswith("888") else sym
        print(f"\n=== {sym}（新浪 {prod.lower()}0）===")
        df = fetch_5m(prod)
        if df.empty:
            print("  ❌ 源返回空，跳过")
            continue
        print(f"  5m 源：{len(df)} 根  {df.dt.iloc[0]:%Y-%m-%d %H:%M} ~ "
              f"{df.dt.iloc[-1]:%Y-%m-%d %H:%M}")

        days = ([x.strip() for x in args.days.split(",") if x.strip()]
                if args.days else [])
        if args.auto_days:
            days = auto_detect_days(sym, args.auto_days)
        if not days:
            print("  ⚠ 未指定 --days 且未探测到缺口，跳过")
            continue

        for day in days:
            sub = df[df.dt.dt.strftime("%Y-%m-%d") == day]
            if sub.empty:
                print(f"  ⚠ {day} 源中无数据（超出 5m 回溯窗口？），跳过")
                continue
            s_dt, e_dt = day_window(day)
            with session_scope() as sess:
                n_stage = load_stage(sess, sym, sub)
                print(f"\n  [{day}] stage 写入 {n_stage} 根 5m")
                for step in steps:
                    rows = aggregate(sess, sym, step, s_dt, e_dt)
                    dl, ins = write_step(sess, sym, step, rows, s_dt, e_dt,
                                         args.apply, args.missing_only)
                    times = " ".join(
                        r[1].astimezone(SH).strftime("%H:%M")
                        for r in rows if r[1] is not None)
                    flag = (f"删{dl}/写{ins}" if args.apply else f"待写{ins}")
                    print(f"    {step:>2}m: {len(rows):>3} 根  [{flag}]  {times}")
                    total_w += ins if args.apply else 0
            if not args.apply:
                continue

    el = (datetime.now(tz=timezone.utc) - t0).total_seconds()
    print(f"\n{'='*64}")
    if args.apply:
        print(f"✅ 已写入 {total_w:,} 根，耗时 {el:.1f}s")
        print("   下一步：scripts/ops/rebuild_roll_all_freqs.py "
              f"--symbols {args.symbols} --force-ba")
    else:
        print(f"[dry-run] 未落库，耗时 {el:.1f}s。确认形态后加 --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
