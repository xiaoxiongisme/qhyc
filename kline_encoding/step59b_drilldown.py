# -*- coding: utf-8 -*-
"""step59b —— Route B (偏离回归) 在 EG/EB/MA 的逐项深挖 + 2026 实战交易清单。"""
import json
import numpy as np
import pandas as pd
from step46_prod_bt import load_all, build_cost_fn, build_sym, metrics
from step59_ab_engines import build_signals, run_engine

SYMS = ['EG888', 'EB888', 'MA888']
CFG_B = dict(DEV=2.5, SL_ATR=2.5, TRAIL_ATR=2.5)   # 2026 ho 最佳且交易最少
CFG_A = dict(BK_N=10, VOL_RATIO=1.3, SL_ATR=2.0, TRAIL_ATR=2.0)  # A 代表配置


def run_one(sym, b, vc, costmap, tick, kind, p):
    sig, atr, e140 = build_signals(b, kind, p)
    return run_engine(sym, b, costmap[vc], tick.get(vc, 1.0), sig, atr, e140, kind, p)


def main():
    ba, raw, dd, var, cost, fac = load_all()
    costmap = build_cost_fn(cost, var)
    tick = dict(zip(var.variety_code, var.tick_size.astype(float)))
    data = {}
    for s in SYMS:
        vc = s[:-3]
        if vc in costmap:
            b = build_sym(s, ba, raw, dd, var)
            if b is not None:
                b = b.dropna(subset=['open', 'high', 'low', 'close']).reset_index(drop=True)
                if len(b) >= 1500:
                    data[s] = (b, vc)

    out = {}
    for kind, p in [('B', CFG_B), ('A', CFG_A)]:
        out[kind] = {}
        for s, (b, vc) in data.items():
            tr = run_one(s, b, vc, costmap, tick, kind, p)
            df = pd.DataFrame(tr)
            # 详尽
            m = metrics(tr, s)
            # 2026 月度
            if len(df):
                df['ym'] = pd.to_datetime(df.xdt).dt.strftime('%Y-%m')
                mo = df[df.year >= 2026].groupby('ym').agg(
                    n=('net', 'size'), net=('net', 'sum'),
                    win=('net', lambda x: (x > 0).mean())).reset_index()
            else:
                mo = pd.DataFrame()
            # 2026 整笔清单（含 Sept-Oct）
            if len(df):
                tails = df[df.year >= 2026].copy()
                tails['edt_s'] = pd.to_datetime(tails.edt).dt.strftime('%Y-%m-%d %H:%M')
                tails['xdt_s'] = pd.to_datetime(tails.xdt).dt.strftime('%Y-%m-%d %H:%M')
                trades_2026 = tails[['edt_s', 'xdt_s', 'dir', 'P', 'X', 'lots', 'net', 'r', 'exit', 'e_atr']].to_dict('records')
            else:
                trades_2026 = []
            out[kind][s] = dict(m=m, monthly=mo.to_dict('records'), trades_2026=trades_2026)
            print(f'=== {kind} {s} ===')
            print(json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                              for k, v in m.items() if k not in ('per_sym',)}, ensure_ascii=False, default=str))
            print('  2026 月度:')
            for r in out[kind][s]['monthly']:
                print(f"    {r['ym']}  n={r['n']:>3}  net={r['net']:>8.0f}  win={r['win']:.2f}")
            print(f"  2026 共 {len(out[kind][s]['trades_2026'])} 笔")
    json.dump(out, open('step59b_drilldown.json', 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1, default=str)
    print('saved step59b_drilldown.json')


if __name__ == '__main__':
    main()
