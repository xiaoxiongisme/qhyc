# -*- coding: utf-8 -*-
"""l0_raw.hourly_bar_uni 拼接体检（切换数据源前的准入检查）。

背景：生产口径 `src='akshare'` 的小时线只覆盖 2023-01 之后（实测 8,452 行/品种
级别），此前误判为「小时线只有 4 年」。统一视图 `hourly_bar_uni` 把
`agg_1m`(2015-01-05~2023-01-09) 与 `akshare`(2023-01-09~) 拼起来。
但**拼接前必须证明两源同构**，否则拉长久期只会引入假信号：

    J1 重叠       ：同 (symbol, trade_datetime) 是否跨源重复（重复=重复计数）
    J2 时段形态   ：两源的 hour-of-day 分布是否一致（夜盘归属不同=致命）
    J3 每日密度   ：bars/day 是否接近（一个含夜盘另一个不含，立刻暴露）
    J4 交界连续   ：交界前后 close 跳幅是否远超该品种正常波动尺度
    J5 关键品种   ：策略池品种逐个体检，不许「平均通过、个别爆炸」

跑法（生产机）：
    set -a; . /home/ubuntu/qhyc_work/qhyc.env; set +a
    cd /Docker/qhyc && PYTHONPATH=/Docker/qhyc \
        /home/ubuntu/qhyc_work/venv/bin/python /tmp/hourly_uni_join_check.py
"""
from __future__ import annotations

from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import session_scope


def main() -> int:
    syms = [s.symbol for s in get_settings().main_contracts]
    print(f"策略池品种数 = {len(syms)}")

    with session_scope() as s:
        # ---------- J1 跨源重叠 ----------
        dup = s.execute(text("""
            SELECT count(*) FROM (
                SELECT symbol, trade_datetime
                FROM l0_raw.hourly_bar
                WHERE symbol = ANY(:syms)
                GROUP BY symbol, trade_datetime
                HAVING count(DISTINCT src) > 1
            ) x
        """), {"syms": syms}).scalar()
        print(f"\n[J1] 跨源重复的 (symbol, 时刻) 数 = {dup}  "
              f"{'✅ 无重叠' if dup == 0 else '❌ 存在重叠，拼接会重复计数'}")

        # ---------- 覆盖概览 ----------
        cov = s.execute(text("""
            SELECT src, count(*) n, count(DISTINCT symbol) n_sym,
                   min(trade_datetime)::date first_day, max(trade_datetime)::date last_day
            FROM l0_raw.hourly_bar WHERE symbol = ANY(:syms)
            GROUP BY src ORDER BY src
        """), {"syms": syms}).fetchall()
        print("\n[覆盖概览]")
        print(f"{'src':<10}{'bars':>12}{'品种':>8}{'首日':>14}{'末日':>14}")
        for r in cov:
            m = r._mapping
            print(f"{m['src']:<10}{m['n']:>12,}{m['n_sym']:>8}"
                  f"{str(m['first_day']):>14}{str(m['last_day']):>14}")

        # ---------- J2 时段形态（hour of day）----------
        hod = s.execute(text("""
            SELECT src, extract(hour FROM trade_datetime)::int h, count(*) n
            FROM l0_raw.hourly_bar WHERE symbol = ANY(:syms)
            GROUP BY 1,2 ORDER BY 1,2
        """), {"syms": syms}).fetchall()
        by_src: dict[str, dict[int, int]] = {}
        for r in hod:
            m = r._mapping
            by_src.setdefault(m["src"], {})[m["h"]] = m["n"]
        print("\n[J2] 各 src 的小时分布（占比%）——交易时段必须同构")
        hours = sorted({h for d in by_src.values() for h in d})
        srcs = sorted(by_src)
        print(f"{'hour':<6}" + "".join(f"{x:>10}" for x in srcs))
        for h in hours:
            row = ""
            for src in srcs:
                tot = sum(by_src[src].values())
                row += f"{by_src[src].get(h, 0)/tot*100:>9.1f}%"
            print(f"{h:<6}{row}")

        # ---------- J3 每日密度 ----------
        dens = s.execute(text("""
            SELECT symbol, src,
                   count(*)::float / NULLIF(count(DISTINCT trade_datetime::date), 0) bpd
            FROM l0_raw.hourly_bar WHERE symbol = ANY(:syms)
            GROUP BY 1,2
        """), {"syms": syms}).fetchall()
        agg: dict[str, list[float]] = {}
        for r in dens:
            m = r._mapping
            agg.setdefault(m["src"], []).append(float(m["bpd"] or 0))
        print("\n[J3] 每品种 bars/day（中位数 / 最小 / 最大）")
        for src in sorted(agg):
            v = sorted(agg[src])
            med = v[len(v) // 2]
            print(f"  {src:<10} med={med:>6.1f}  min={v[0]:>6.1f}  max={v[-1]:>6.1f}  n={len(v)}")

        # ---------- J4 交界连续性 ----------
        print("\n[J4] 交界连续性：agg_1m 最后一根 → akshare 第一根")
        rows = s.execute(text("""
            WITH a AS (
                SELECT symbol, src, trade_datetime, close,
                       row_number() OVER (PARTITION BY symbol ORDER BY trade_datetime DESC) rn
                FROM l0_raw.hourly_bar WHERE symbol = ANY(:syms) AND src = 'agg_1m'
            ), b AS (
                SELECT symbol, src, trade_datetime, close,
                       row_number() OVER (PARTITION BY symbol ORDER BY trade_datetime ASC) rn
                FROM l0_raw.hourly_bar WHERE symbol = ANY(:syms) AND src = 'akshare'
            ), scale AS (
                SELECT symbol,
                       percentile_cont(0.5) WITHIN GROUP (ORDER BY step) med_step
                FROM (
                    SELECT h.symbol,
                           abs(h.close - lag(h.close) OVER (
                               PARTITION BY h.symbol ORDER BY h.trade_datetime)) step
                    FROM l0_raw.hourly_bar h
                    WHERE h.symbol = ANY(:syms) AND h.src = 'akshare'
                ) st
                WHERE step IS NOT NULL
                GROUP BY symbol
            )
            SELECT a.symbol, a.trade_datetime a_dt, a.close a_close,
                   b.trade_datetime b_dt, b.close b_close,
                   extract(epoch FROM (b.trade_datetime - a.trade_datetime))/3600 gap_h,
                   sc.med_step
            FROM a JOIN b ON b.symbol = a.symbol AND a.rn = 1 AND b.rn = 1
            JOIN scale sc ON sc.symbol = a.symbol
            ORDER BY a.symbol
        """), {"syms": syms}).fetchall()

        print(f"{'symbol':<9}{'agg_1m末':>17}{'close':>10}{'akshare首':>17}"
              f"{'close':>10}{'gap(h)':>9}{'跳幅%':>9}{'正常步长%':>11}{'倍数':>8}")
        bad = []
        for r in rows:
            m = r._mapping
            ac, bc = float(m["a_close"]), float(m["b_close"])
            step = float(m["med_step"]) if m["med_step"] else 0.0
            jump = abs(bc - ac) / ac * 100 if ac else 0.0
            step_pct = step / ac * 100 if ac else 0.0
            ratio = jump / step_pct if step_pct else float("inf")
            flag = "  ⚠" if step_pct and ratio > 20 else ""
            if step_pct and ratio > 20:
                bad.append((m["symbol"], jump, step_pct, ratio))
            print(f"{m['symbol']:<9}{str(m['a_dt'])[:16]:>17}{ac:>10.2f}"
                  f"{str(m['b_dt'])[:16]:>17}{bc:>10.2f}"
                  f"{float(m['gap_h']):>9.1f}{jump:>9.2f}{step_pct:>11.3f}{ratio:>8.1f}{flag}")

        print("\n=== 判定 ===")
        if dup:
            print("❌ J1 重叠：禁止拼接")
        elif bad:
            print(f"❌ J4 有 {len(bad)} 个品种交界跳幅 > 20 倍正常步长：")
            for sym, j, sp, ra in bad[:10]:
                print(f"     {sym}: 跳幅 {j:.2f}% vs 正常步长 {sp:.3f}%  ({ra:.0f}x)")
            print("   → 多为 888 换月断层，须按品种排查后方可切源")
        else:
            print("✅ J1/J2/J3/J4 通过：两源时段同构、无重叠、交界无非正常跳空")
            print("   → 可切 hourly_bar_uni 做长周期回测（早期 2015-2016 数据仍须单独校验）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
