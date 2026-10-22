# -*- coding: utf-8 -*-
"""step76 演示用：从 step75 扩展扫描结果中挑选一个「真实、且优于/不低于当前冻结值容差」的
候选配置，构造 Governor 所需的候选 JSON {params, metrics}（指标与 results_step46.json
V5X 同构），供 v5x_governor.py 走 PROPOSED → --apply --confirm 完整闭环。

挑选规则（与 v5x_relock_guard 四道护栏前提一致）：
  - 排除当前冻结值本身；
  - 三分区（train/holdout/oos21）net 全正（与当前同号）；
  - MAR >= 当前 MAR × (1 - 0.02)（护栏②容差内）；
  - 取其中 MAR 最高者。
若无满足容差者，退而取「三分区正且 MAR 最高」者，仅供演示护栏②拒绝路径。
"""
import os, sys, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import v5x_relock_guard as G

HERE = os.path.dirname(os.path.abspath(__file__))
SWEEP = os.path.join(HERE, 'step75_sweep.json')
OUT = os.path.join(HERE, 'cand_demo.json')
TOL = 0.02


def cur_key(params):
    return (params['SL_ATR'], params['TRAIL_ATR'], params['BE_R'],
            params['COOLDOWN'], params['ADD_MAX_LOTS'], params['ADD_THR'])


def three_pos(m):
    return (m.get('train', {}).get('net_yuan', 0) > 0 and
            m.get('holdout', {}).get('net_yuan', 0) > 0 and
            m.get('oos21', {}).get('net_yuan', 0) > 0)


def main():
    cur_p, cur_m = G.current_frozen()
    cur_mar = cur_m.get('mar', 0) or 0
    print(f'当前冻结 MAR={cur_mar:.4f} 容差下限={cur_mar*(1-TOL):.4f}')

    d = json.load(open(SWEEP, encoding='utf-8'))
    results = d['results']
    ck = cur_key(cur_p)

    cands = []
    for k, v in results.items():
        if cur_key(v['params']) == ck:
            continue
        if not three_pos(v):
            continue
        if v['mar'] >= cur_mar * (1 - TOL):
            cands.append((v['mar'], k, v))
    cands.sort(reverse=True)

    if not cands:
        allc = [(v['mar'], k, v) for k, v in results.items()
                if cur_key(v['params']) != ck and three_pos(v)]
        allc.sort(reverse=True)
        if not allc:
            print('ERROR: 扫描中无任何三分区正配置'); return
        mar, k, v = allc[0]
        print(f'[演示] 无满足容差候选；最高三分区正配置 MAR={mar:.4f}（将触发护栏②拒绝）')
    else:
        mar, k, v = cands[0]
        tag = '优于当前' if mar > cur_mar else '容差内(不低于2%)'
        print(f'[选定] {k}  MAR={mar:.4f} net={v["net_yuan"]:,.0f}  ({tag})')

    p = v['params']
    m = {kk: v[kk] for kk in ('n', 'net_yuan', 'pf', 'mdd_yuan', 'mar', 'train', 'holdout', 'oos21')}
    cand = {'params': p, 'metrics': m}
    json.dump(cand, open(OUT, 'w', encoding='utf-8'), ensure_ascii=False, indent=1, default=str)
    print('params:', p)
    print(f"  train={m['train']['net_yuan']:,.0f}  holdout={m['holdout']['net_yuan']:,.0f}  "
          f"oos21={m['oos21']['net_yuan']:,.0f}")
    print('written', OUT)


if __name__ == '__main__':
    main()
