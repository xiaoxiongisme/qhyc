# -*- coding: utf-8 -*-
"""step75 扩展扫描：在最优解周边（含原 step73 网格未覆盖区 SL<1.5 / TR>3.0）寻找
V5X 真实更优配置。复用 step74.run_v5x + step46.metrics，输出与 results_step46.json
V5X 完全同构的指标（train/holdout/oos21 三段），供 Governor 四道硬护栏评估。

目的：构造一个真实（非伪造）的候选参数，演示自进化系统「扫描→对照→重锁」闭环。
"""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import step46_prod_bt as S
import step74_compare as S74

HERE = os.path.dirname(os.path.abspath(__file__))

# 网格：围绕当前冻结值 SL1.5/TR3.0/BE1.0/CD7/AL2/AT1.5 扩展
SL_LIST = [1.0, 1.25, 1.5, 1.75, 2.0]
TR_LIST = [2.0, 2.5, 3.0, 3.5, 4.0]
CD_LIST = [5, 7, 10]
AT_LIST = [1.0, 1.5, 2.0]
BE = 1.0
AL = 2


def main():
    t0 = time.time()
    data, costmap, tick, shock = S74.build_data()
    print(f'build_data {time.time()-t0:.1f}s syms={len(data)}', flush=True)

    results = {}
    total = len(SL_LIST) * len(TR_LIST) * len(CD_LIST) * len(AT_LIST)
    done = 0
    for sl in SL_LIST:
        for tr in TR_LIST:
            for cd in CD_LIST:
                for at in AT_LIST:
                    p = dict(SL_ATR=sl, TRAIL_ATR=tr, BE_R=BE,
                             COOLDOWN=cd, ADD_MAX_LOTS=AL, ADD_THR=at)
                    trs = S74.run_v5x(data, p, costmap, tick, shock)
                    m = S.metrics(trs, 'V5X')
                    key = f'SL{sl}_TR{tr}_CD{cd}_AT{at}'
                    results[key] = dict(
                        params=p, n=m['n'], net_yuan=m['net_yuan'],
                        pf=m['pf'], mdd_yuan=m['mdd_yuan'], mar=m['mar'],
                        train=m.get('train'), holdout=m.get('holdout'),
                        oos21=m.get('oos21'))
                    done += 1
                    if done % 25 == 0:
                        print(f'{done}/{total}  cur best mar={max(v["mar"] for v in results.values()):.3f}',
                              flush=True)

    # 排序（MAR 降序）
    ranked = sorted(results.items(), key=lambda kv: kv[1]['mar'], reverse=True)
    out = {'n_configs': total, 'ranked': [(k, v['mar'], v['net_yuan']) for k, v in ranked],
           'results': results}
    json.dump(out, open(os.path.join(HERE, 'step75_sweep.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1, default=str)
    print(f'\nDONE {total} configs in {time.time()-t0:.1f}s. Top 8 by MAR:')
    for k, v in ranked[:8]:
        m = v['mar']
        t = v['train']['net_yuan']; h = v['holdout']['net_yuan']; o = v['oos21']['net_yuan']
        print(f'  {k:24s} MAR={m:7.3f} net={v["net_yuan"]:,.0f}  '
              f'train={t:,.0f} ho={h:,.0f} oos21={o:,.0f}  '
              f'3zone={"+" if (t>0 and h>0 and o>0) else "-"}')
    print('saved step75_sweep.json')


if __name__ == '__main__':
    main()
