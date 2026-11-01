# -*- coding: utf-8 -*-
"""探查 l0_raw.daily_bar 结构与覆盖度，确认能否支撑日线 EMA 回测。

背景：现有融合回测引擎只消费 hourly_bar。为验证「周期尺度错配」假设
（Moskowitz/Ooi/Pedersen 指出 6-12 个月回看才是趋势有效窗口，而小时线
ema_k=140 ≈ 21 交易日，落在噪音最大的 1-3 个月窗口），需要切到日线重跑。
本脚本只做数据源体检，不改任何引擎逻辑。
"""
import sys

from sqlalchemy import text
from app.core.db import session_scope
from app.core.config import get_settings


def main() -> int:
    with session_scope() as s:
        cols = s.execute(text(
            "SELECT column_name, data_type "
            "FROM information_schema.columns "
            "WHERE table_schema='l0_raw' AND table_name='daily_bar' "
            "ORDER BY ordinal_position"
        )).fetchall()
        print("=== daily_bar 列结构 ===")
        for c in cols:
            print(f"  {c[0]:<20} {c[1]}")

        total = s.execute(text("SELECT count(*) FROM l0_raw.daily_bar")).scalar()
        print(f"\n总行数: {total}")

        print("\n=== 按 symbol/src 聚合（前 10）===")
        rows = s.execute(text(
            "SELECT symbol, src, count(*) AS c, "
            "min(trade_date) AS mn, max(trade_date) AS mx "
            "FROM l0_raw.daily_bar GROUP BY 1,2 ORDER BY c DESC LIMIT 10"
        )).fetchall()
        for r in rows:
            print(f"  {r[0]:<12} src={r[1]:<10} n={r[2]:<6} {r[3]} ~ {r[4]}")

    # 生产主连符号 vs 日线可用符号：交集决定回测品种池
    st = get_settings()
    prod_syms = [x.symbol for x in st.main_contracts]
    with session_scope() as s:
        avail = s.execute(text(
            "SELECT DISTINCT symbol FROM l0_raw.daily_bar WHERE src='akshare'"
        )).scalars().all()
    avail = set(avail)
    hit = [x for x in prod_syms if x in avail]
    print(f"\n=== 品种池匹配（src=akshare）===")
    print(f"  生产主连数: {len(prod_syms)}")
    print(f"  日线可用数: {len(avail)}")
    print(f"  交集(可做): {len(hit)}")
    print(f"  示例: {hit[:10]}")
    missing = [x for x in prod_syms if x not in avail]
    print(f"  缺失: {len(missing)} 个 -> {missing[:10]}")

    # 样本长度体检：日线若某品种不足 800 根会被 max_bars 截断/或不足 min_bars
    print(f"\n=== 样本长度体检（akshare，看是否够日线 EMA250 预热）===")
    with session_scope() as s:
        rows = s.execute(text(
            "SELECT symbol, count(*) AS c FROM l0_raw.daily_bar "
            "WHERE src='akshare' AND symbol = ANY(:syms) "
            "GROUP BY 1 ORDER BY c ASC LIMIT 12"
        ), {"syms": hit}).fetchall()
    for r in rows:
        flag = "OK" if r[1] >= 1500 else ("薄" if r[1] >= 800 else "不足")
        print(f"  {r[0]:<12} n={r[1]:<6} [{flag}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
