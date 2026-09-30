# -*- coding: utf-8 -*-
"""等差后复权上线体检报告（只读 roll_segment + bar_*，不写业务库）。

产出三张清单，对应「你全部核算」里最该盯的东西：

1. **负价清单** —— 后复权后仍会转负的品种。
   这些序列**禁止做除法 / 百分比 / 对数收益**，回测只能用点数。
   （2026-09-30 实测：前复权 13 个品种转负 → 后复权降到 3 个）

2. **疑误检测清单** —— 年换月频次异常的品种。
   期货正常一年 2~3 次主力切换（1/5/9 月或逐月），>5 次/年基本是
   低流动性品种被振幅门误判（震荡+y 轴稀疏产品）。

3. **跨周期一致性** —— 同一品种 min15/min30/min60 的换月事件数必须一致。
   不一致 = 换月映射漏了（曾因 bucket 对齐差异静默丢 17% 的换月）。

用法
----
  python scripts/report_back_adj_quality.py                 # 打印报告
  python scripts/report_back_adj_quality.py --csv out.csv   # 另导出明细
"""
import argparse
import os
import sys

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_roll_segments import TABLE  # noqa: E402

CONN = dict(host=os.getenv("POSTGRES_HOST", "timescaledb"),
            port=int(os.getenv("POSTGRES_PORT", "5432")),
            user=os.getenv("POSTGRES_USER", "futures"),
            password=os.getenv("POSTGRES_PASSWORD", "qhyc_dev_pwd_2026"),
            dbname=os.getenv("POSTGRES_DB", "futures"))

FREQS = ('min15', 'min30', 'min60')
FREQ_MIN = {'min5': 5, 'min15': 15, 'min30': 30, 'min60': 60}


def build(cur, freqs):
    rows = []
    cur.execute("SELECT symbol FROM roll_segment WHERE freq=%s GROUP BY 1 ORDER BY 1",
                (freqs[0],))
    syms = [r[0] for r in cur.fetchall()]
    for sym in syms:
        segs = {}
        for f in freqs:
            cur.execute("SELECT seg_no,seg_start,seg_end,cum_offset,n_bars "
                        "FROM roll_segment WHERE symbol=%s AND freq=%s ORDER BY seg_no",
                        (sym, f))
            segs[f] = cur.fetchall()
        # —— 后复权最低价：每段内 bar 的最低价 + 该段 cum_offset，取全局最小 ——
        # 只查 min15 足够判断负价（同一品种不同周期的最低价只差聚合粒度）
        tbl = TABLE[freqs[0]]
        mins = []
        for (no, st, en, off, _n) in segs[freqs[0]]:
            off = float(off)
            if en:
                cur.execute(f"SELECT MIN(low) FROM {tbl} WHERE symbol=%s "
                            f"AND bucket>=%s AND bucket<%s", (sym, st, en))
            else:
                cur.execute(f"SELECT MIN(low) FROM {tbl} WHERE symbol=%s "
                            f"AND bucket>=%s", (sym, st))
            v = cur.fetchone()[0]
            if v is not None:
                mins.append(float(v) + off)
        lo_adj = min(mins) if mins else np.nan
        cur.execute(f"SELECT MIN(low),MIN(bucket),MAX(bucket),count(*) "
                    f"FROM {tbl} WHERE symbol=%s", (sym,))
        lo_raw, t0, t1, nbar = cur.fetchone()
        span = (t1 - t0).days / 365.25 if t0 and t1 else 0.0
        nroll = len(segs[freqs[0]]) - 1
        rows.append(dict(symbol=sym, bars=nbar, years=round(span, 2),
                         n_roll=nroll, per_year=round(nroll / span, 2) if span else np.nan,
                         n_seg_15=len(segs['min15']) if 'min15' in segs else None,
                         n_seg_30=len(segs['min30']) if 'min30' in segs else None,
                         n_seg_60=len(segs['min60']) if 'min60' in segs else None,
                         min_raw=float(lo_raw) if lo_raw is not None else np.nan,
                         min_adj=round(lo_adj, 2) if mins else np.nan,
                         negative=bool(mins and lo_adj <= 0),
                         start=t0, end=t1))
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', default=None, help='导出明细 CSV')
    ap.add_argument('--freqs', default='min15,min30,min60')
    a = ap.parse_args()
    freqs = tuple(f.strip() for f in a.freqs.split(',') if f.strip())

    c = psycopg2.connect(**CONN)
    cur = c.cursor()
    d = build(cur, freqs)
    c.close()
    if d.empty:
        print('roll_segment 为空，先跑 scripts/build_roll_segments.py')
        return 2

    W = 96
    print('=' * W)
    print('等差后复权 · 上线体检报告')
    print('=' * W)
    print(f"样本：{len(d)} 个品种 | bar 总量 {int(d['bars'].sum()):,} 根 | "
          f"换月事件合计 {int(d['n_roll'].sum()):,} 次")
    print(f"时间跨度中位数 {d['years'].median():.1f} 年 "
          f"（{str(d['start'].min())[:10]} ~ {str(d['end'].max())[:10]}）")

    # ---------- 1. 负价 ----------
    print('\n' + '=' * W)
    print('【1】负价清单（后复权后最低价 ≤ 0）—— 这些序列禁止做除法/收益率')
    print('=' * W)
    neg = d[d['negative']].sort_values('min_adj')
    if neg.empty:
        print('  ✅ 无负价品种')
    else:
        print(f"  ⚠️  {len(neg)}/{len(d)} 个品种仍会转负：")
        print(f"  {'品种':<8}{'年份跨度':>10}{'换月数':>8}{'未复权最低':>12}"
              f"{'后复权最低':>12}{'处置':>26}")
        for _, r in neg.iterrows():
            print(f"  {r['symbol']:<8}{r['years']:>10.1f}{r['n_roll']:>8}"
                  f"{r['min_raw']:>12.1f}{r['min_adj']:>12.1f}"
                  f"{'仅用点数，禁除法/收益率':>26}")
        print('  注：负价是「以最早段为 0 点做加法平移」的必然结果——')
        print('      长期 contango 品种会把后续段一路推低。点数差分不受影响，')
        print('      但任何 pct_change / log return / 仓位百分比算法都会失效。')

    # ---------- 2. 疑误检测 ----------
    print('\n' + '=' * W)
    print('【2】疑似误检清单（年换月 > 5 次；正常 2~3 次/年）')
    print('=' * W)
    susp = d[d['per_year'] > 5].sort_values('per_year', ascending=False)
    if susp.empty:
        print('  ✅ 无异常品种')
    else:
        print(f"  ⚠️  {len(susp)}/{len(d)} 个品种：")
        print(f"  {'品种':<8}{'bar数':>10}{'年份':>7}{'换月':>7}{'次/年':>8}"
              f"{'未复权最低':>12}{'后复权最低':>12}")
        for _, r in susp.iterrows():
            print(f"  {r['symbol']:<8}{r['bars']:>10,}{r['years']:>7.1f}"
                  f"{r['n_roll']:>7}{r['per_year']:>8.1f}"
                  f"{r['min_raw']:>12.1f}{r['min_adj']:>12.1f}")
        print('  成因：低流动性（bar 数远少于同类）+ 价格低 → 最小变动价位占比大，')
        print('        振幅门 |Δclose| > range+prev_range 易被单跳 tick 触发。')
        print('  建议：这些品种的复权序列先不下发给策略，或单独调高 RATIO_TH。')

    # ---------- 3. 跨周期一致性 ----------
    print('\n' + '=' * W)
    print('【3】跨周期一致性（同品种各周期段数应完全相同）')
    print('=' * W)
    cols = [f'n_seg_{f[3:]}' for f in freqs]
    bad = d[d[cols].nunique(axis=1) > 1]
    if bad.empty:
        print(f"  ✅ {len(d)}/{len(d)} 品种的 {len(freqs)} 个周期段数完全一致")
    else:
        print(f"  ⚠️  {len(bad)}/{len(d)} 品种不一致：")
        print(f"  {'品种':<10}" + ''.join(f'{c:>10}' for c in cols))
        for _, r in bad.iterrows():
            print(f"  {r['symbol']:<10}" + ''.join(f'{r[c]:>10}' for c in cols))
        print('  不一致 = 换月映射漏事件（曾因 bar_30m/bar_60m 的 bucket 对齐不同，')
        print('  用 [锚点,锚点+15min) 窗口找不到 bar，静默丢 17% 换月）。')

    # ---------- 4. 整体健康度 ----------
    print('\n' + '=' * W)
    print('【4】整体健康度')
    print('=' * W)
    print(f"  年换月频次：中位 {d['per_year'].median():.2f}  "
          f"P90 {d['per_year'].quantile(0.9):.2f}  最大 {d['per_year'].max():.2f}")
    print(f"  单品种段数：中位 {d['n_seg_15'].median():.0f}  最大 {d['n_seg_15'].max()}")
    print(f"  未复权最低价 > 0 的品种：{int((d['min_raw'] > 0).sum())}/{len(d)}")
    print(f"  后复权最低价 > 0 的品种：{int((d['min_adj'] > 0).sum())}/{len(d)}")

    if a.csv:
        d.to_csv(a.csv, index=False, encoding='utf-8-sig')
        print(f"\n明细已导出 → {a.csv}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
