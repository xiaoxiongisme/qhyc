# -*- coding: utf-8 -*-
"""step49 胜率结构实验：V5X 保本胜率拆解 + 「提高胜率」各路径的实证代价

用 fusion_v5x_engine 回放 56 品种，对比离场层变体：
  V5X(基准) / TP1.0 / TP1.5 / TP2.0 / TP3.0 / TRAIL1.5 / BE0.3
另从已有 CSV 补 M5_15（更严门控）的胜率口径。
输出 results_step49.json + winrate_report.html
"""
import json
import numpy as np
import pandas as pd
import step46_prod_bt as s46
from fusion_v5x_engine import V5XConfig, CostCfg, DailyCtx, SymbolEngine, CLUSTER

DATA = 'prod50'


def replay(sym, b, cfg, costmap, tick, blocked):
    vc = sym[:-3]
    fee_row, slip, mult = costmap[vc]
    eng = SymbolEngine(sym, cfg, CostCfg(fee_row, slip, mult, tick.get(vc, 1.0)))
    sl_hours = frozenset(int(x) for x in b.loc[b.sess_last, 'hour'].unique())
    prev_td = [None]
    for row in b.itertuples():
        td = row.td
        if td != prev_td[0]:
            eng.set_daily_ctx(DailyCtx(
                td=td, hvol=bool(row.hvol),
                member_z=float(row.member_z) if not pd.isna(row.member_z) else np.nan,
                blocked=td in blocked, sess_last_hours=sl_hours))
            prev_td[0] = td
        eng.on_bar(row.ts, float(row.open), float(row.high), float(row.low),
                   float(row.close), float(row.volume), float(row.raw_c),
                   allow=bool(row.allow))
    # 数据末端强平（对齐回测口径）
    if eng.state != 0:
        row = b.iloc[-1]
        eng.ctx.td = row.td
        eng._exit(row.ts, float(row.close), float(row.raw_c),
                  1 if eng.state == 1 else -1, 'eod')
    for t in eng.trades:
        t['year'] = pd.Timestamp(t['xdt']).year
    return eng.trades


def metrics(trades, label):
    if not trades:
        return dict(label=label, n=0)
    df = pd.DataFrame(trades).sort_values('xdt').reset_index(drop=True)
    eq = df.net.cumsum()
    mdd = float((eq - eq.cummax()).min())
    w = df[df.net > 0]; l = df[df.net < 0]
    aw = float(w.net.mean()) if len(w) else 0.0
    al = float(-l.net.mean()) if len(l) else 0.0
    gp = float(w.net.sum()); gl = float(-l.net.sum())
    out = dict(label=label, n=len(df), win=float(len(w) / len(df)),
               avg_win=aw, avg_loss=al,
               winloss=float(aw / al) if al > 0 else np.nan,
               p_be=float(al / (aw + al)) if (aw + al) > 0 else np.nan,
               pf=float(gp / gl) if gl > 0 else np.nan,
               exp_per_trade=float(df.net.mean()),
               net_yuan=float(df.net.sum()), mdd_yuan=mdd,
               mar=float(df.net.sum() / abs(mdd)) if mdd < 0 else np.nan)
    sub = df[df.year >= 2026]
    if len(sub):
        out['holdout'] = dict(n=int(len(sub)), win=float((sub.net > 0).mean()),
                              net_yuan=float(sub.net.sum()))
    return out


def main():
    ba, raw, dd, var, cost, fac = s46.load_all()
    costmap = s46.build_cost_fn(cost, var)
    tick = dict(zip(var.variety_code, var.tick_size.astype(float)))
    syms = sorted(s for s in ba.symbol.unique() if s[:-3] in costmap and s[:-3] not in s46.EXCLUDE)
    data = {}
    for s in syms:
        b = s46.build_sym(s, ba, raw, dd, var)
        if b is not None:
            data[s] = b
    data = s46.attach_member_z(data, fac)
    shock = s46.cluster_shock_days(data)
    blocked_map = {s: shock.get(CLUSTER.get(s[:-3]), set()) for s in data}
    print(f'loaded {len(data)} syms', flush=True)

    variants = {
        'V5X(基准)':    {},
        'TP1.0':        {'TP_R': 1.0},
        'TP1.5':        {'TP_R': 1.5},
        'TP2.0':        {'TP_R': 2.0},
        'TP3.0':        {'TP_R': 3.0},
        'TRAIL1.5':     {'TRAIL_ATR': 1.5},
        'BE0.3':        {'BE_R': 0.3},
    }
    results = {}
    for name, ov in variants.items():
        cfg = V5XConfig(**ov)
        allt = []
        for s, b in data.items():
            allt += replay(s, b, cfg, costmap, tick, blocked_map[s])
        m = metrics(allt, name)
        results[name] = m
        print(name, json.dumps({k: (round(v, 4) if isinstance(v, float) else v)
                                for k, v in m.items() if k != 'holdout'}, ensure_ascii=False),
              m.get('holdout'), flush=True)
        pd.DataFrame(allt).to_csv(f'trades49_{name.split("(")[0]}.csv', index=False, encoding='utf-8-sig')

    # 已有 CSV 补充：更严门控 M5_15 的胜率口径（基于 V4X 骨架，仅作参考）
    try:
        m515 = pd.read_csv('trades46_M5_15.csv')
        w = m515[m515.net > 0]; l = m515[m515.net < 0]
        results['M5_15(参考)'] = dict(label='M5_15(参考)', n=len(m515),
                                      win=float(len(w) / len(m515)),
                                      net_yuan=float(m515.net.sum()),
                                      note='V4X+M5|z|>=1.5 骨架，非 V5X')
    except Exception:
        pass

    json.dump(results, open('results_step49.json', 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1, default=str)
    print('saved results_step49.json', flush=True)


if __name__ == '__main__':
    main()
