# -*- coding: utf-8 -*-
"""分析 step73_sweep.json：用 net/MDD 自算子时段 MAR，对 A/B 做稳健排序。"""
import json

d = json.load(open('step73_sweep.json', encoding='utf-8'))
base = d['baseline']
A = d['A']; B = d['B']


def sub_mar(t):
    m = t.get('mdd_yuan', 0)
    n = t.get('net_yuan', 0)
    return (n / abs(m)) if (m is not None and m < 0 and n is not None) else 0.0


def score(v):
    tr = v.get('train', {}); ho = v.get('holdout', {})
    return sub_mar(tr) + sub_mar(ho)


print('=== BASELINE ===')
print('net', base['net_yuan'], 'pf', base['pf'], 'mar', base['mar'], 'mdd', base['mdd_yuan'])
print()
print('=== A TOP 15 (by train+holdout MAR, both net>0) ===')
rows = []
for k, v in A.items():
    if 'error' in v:
        continue
    tr = v.get('train', {}); ho = v.get('holdout', {})
    if (tr.get('net_yuan', 0) or 0) > 0 and (ho.get('net_yuan', 0) or 0) > 0:
        rows.append((score(v), k, v))
rows.sort(reverse=True)
for sc, k, v in rows[:15]:
    tr = v['train']; ho = v['holdout']
    print(f'{k:22s} totNet {v["net_yuan"]:>11.0f} pf {v["pf"]:.3f} mar {v["mar"]:.2f} '
          f'| trNet {tr["net_yuan"]:>10.0f} trMAR {sub_mar(tr):.2f} '
          f'| hoNet {ho["net_yuan"]:>9.0f} hoMAR {sub_mar(ho):.2f} '
          f'| mdd {v["mdd_yuan"]:.0f} win {v["win"]:.3f}')

print()
print('=== A best by TOTAL MAR only ===')
rows2 = [(v.get('mar', 0) or 0, k, v) for k, v in A.items() if 'error' not in v]
rows2.sort(reverse=True)
for sc, k, v in rows2[:8]:
    print(f'{k:22s} totNet {v["net_yuan"]:>11.0f} pf {v["pf"]:.3f} mar {v["mar"]:.2f} mdd {v["mdd_yuan"]:.0f}')

print()
print('=== A best by TOTAL NET only ===')
rows3 = [(v.get('net_yuan', 0) or 0, k, v) for k, v in A.items() if 'error' not in v]
rows3.sort(reverse=True)
for sc, k, v in rows3[:8]:
    print(f'{k:22s} totNet {v["net_yuan"]:>11.0f} pf {v["pf"]:.3f} mar {v["mar"]:.2f} mdd {v["mdd_yuan"]:.0f}')
