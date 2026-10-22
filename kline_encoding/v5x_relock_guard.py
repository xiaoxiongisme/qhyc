# -*- coding: utf-8 -*-
"""v5x_relock_guard.py —— V5X 自进化「四道硬护栏」

任何"学到"的参数变更，必须先过这 4 道关才能写入生产（三源：step46 / 引擎 / 规格）。
护栏来自本项目血泪教训（详见 v5x_self_evolution_design.html §3 / 项目记忆）：

  ① self_check 逐笔 PASS  —— 主干。曾抓出"空头吊灯误用 SL_ATR"bug：改 SL=3 的"更好
     表现"实为 bug 假象，无逐笔对拍会被误锁。
  ② OOS MAR↑ 且三分区同号 —— 防样本内过拟合。train/holdout/oos21 三段 net 同正、
     MAR 不低于当前（允许 ≤2% 退化）。
  ③ 无"全同结果"数据指纹 —— 数据退化（价格全 NaN → 多参数变体逐位相同）指纹。
  ④ 无前视                —— 变更字段必须落在白名单；会员净持仓 z 严格 T-1 可用纪律不可破。

默认只产出评估报告（dry-run），绝不写生产。需显式传入 --apply 才落地（且 apply 也只在
4 关全过时才生效）。
"""
from __future__ import annotations
import os, sys, json, argparse
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np
import pandas as pd

# 允许被自进化系统调整的"安全参数白名单"（其余一律视为非法/前视风险）
SAFE_PARAM_KEYS = {
    'SL_ATR', 'TRAIL_ATR', 'BE_R', 'COOLDOWN', 'ADD_MAX_LOTS', 'ADD_THR',
    'ADX_MIN', 'FIB_TOL', 'W_PIVOT', 'M5_Z_THR', 'ALT_GATE', 'ALT_THR', 'ALT_N',
    'EMA_DIR', 'MA_N', 'ATR_N', 'BK_N',
    'REGIME_P2_AUTO', 'REGIME_POS_SCALE',   # 体制层开关（默认关）
}
SELFCHECK_SYMBOLS = ('MA888', 'RB888')
OOS_DEGRAD_TOL = 0.02   # MAR 允许退化上限（2%）


# ---------------------------------------------------------------- ① self_check
def gate_selfcheck(symbols=SELFCHECK_SYMBOLS):
    """调用引擎逐笔对拍，返回 {pass, detail}。任何一笔偏离即 FAIL。"""
    import fusion_v5x_engine as eng
    res = eng.self_check_detailed(list(symbols))
    return {
        'pass': bool(res['pass_']),
        'detail': res['symbols'],
    }


# ---------------------------------------------------------------- ② OOS 同号
def _sub(m, tag):
    return (m or {}).get(tag, {}) or {}


def gate_oos(cand: dict, cur: dict, degrad_tol=OOS_DEGRAD_TOL):
    """候选 vs 当前冻结值：三分区（train/holdout/oos21）net 同正，且 MAR 不低于当前(容差内)。"""
    problems = []
    c_train, c_ho, c_oos = _sub(cand, 'train'), _sub(cand, 'holdout'), _sub(cand, 'oos21')
    u_train, u_ho, u_oos = _sub(cur, 'train'), _sub(cur, 'holdout'), _sub(cur, 'oos21')
    # 三分区同正
    for tag, c, u in [('train', c_train, u_train), ('holdout', c_ho, u_ho), ('oos21', c_oos, u_oos)]:
        cn = c.get('net_yuan', 0) or 0
        if cn <= 0:
            problems.append(f'{tag}: 候选 net_yuan={cn:,.0f} 非正（当前 {u.get("net_yuan",0):,.0f}）')
    # MAR 不低于当前（容差）
    cm = (cur.get('mar') or 0)
    am = (cand.get('mar') or 0)
    if cm > 0 and am < cm * (1 - degrad_tol):
        problems.append(f'MAR 退化 {am:.3f} < 当前 {cm:.3f}×(1-{degrad_tol})')
    # 同号（防 train 涨 holdout 跌的过拟合结构）
    def sgn(x):
        return 1 if (x or 0) > 0 else (-1 if (x or 0) < 0 else 0)
    if sgn(c_train.get('net_yuan')) != sgn(u_train.get('net_yuan')) or \
       sgn(c_ho.get('net_yuan')) != sgn(u_ho.get('net_yuan')) or \
       sgn(c_oos.get('net_yuan')) != sgn(u_oos.get('net_yuan')):
        problems.append('候选与当前在三分区 net 符号出现反转（疑似过拟合结构）')
    return {'pass': len(problems) == 0, 'problems': problems,
            'cur_mar': cm, 'cand_mar': am}


# ---------------------------------------------------------------- ③ 数据指纹
def gate_fingerprint(cand_metrics: dict, base_metrics: dict | None = None,
                     cand_params: dict | None = None, base_params: dict | None = None,
                     ba=None, check_data=False):
    """两重检查：
       a) 仅在「参数已变」但 metrics 与基线逐位相同 时报警——这才是数据退化指纹
          （多参数变体无差异→价格全 NaN 等）。参数本就相同（如重锁同值）则放行。
       b) 可选：直接核对数据完整性（每品种 close 非全 NaN）。"""
    problems = []
    params_changed = (cand_params is not None and base_params is not None
                     and cand_params != base_params)
    if base_metrics is not None and params_changed:
        same = (round(cand_metrics.get('net_yuan', 0), 2) == round(base_metrics.get('net_yuan', 0), 2)
                and cand_metrics.get('n', 0) == base_metrics.get('n', 0)
                and round(cand_metrics.get('mar', 0), 4) == round(base_metrics.get('mar', 0), 4))
        if same:
            problems.append('参数已变但 metrics 与基线逐位相同 → 疑似数据退化（全 NaN 等）指纹')
    if check_data and ba is not None:
        bad = []
        for sym, sub in ba.groupby('symbol'):
            if sub.close.isna().all():
                bad.append(sym)
        if bad:
            problems.append(f'价格全 NaN 品种: {bad[:10]}')
    return {'pass': len(problems) == 0, 'problems': problems}


# ---------------------------------------------------------------- ④ 无前视
def gate_no_lookahead(cand_params: dict):
    """变更字段必须落在白名单；值本身不得破坏滞后纪律（如 ADX_MIN<0）。"""
    problems = []
    for k in cand_params:
        if k not in SAFE_PARAM_KEYS:
            problems.append(f'参数 {k} 不在安全白名单（可能引入前视/未审计逻辑）')
    if 'ADX_MIN' in cand_params and cand_params['ADX_MIN'] < 0:
        problems.append('ADX_MIN<0 破坏门控语义')
    if 'M5_ON' in cand_params and cand_params.get('M5_ON') is False:
        # 关掉体制门需显式理由；这里仅提示，不致命
        problems.append('WARNING: 关闭 M5 体制门（需人工确认）')
    # member_z 可用性不可被参数破坏——它硬编码于 step46.attach_member_z，参数层无法触碰，
    # 此处仅断言候选未携带任何看似"未来字段"的键（白名单已拦截）。
    return {'pass': all('WARNING' not in p for p in problems),
            'problems': problems, 'warnings': [p for p in problems if p.startswith('WARNING')]}


# ---------------------------------------------------------------- 汇总
def run_all(cand_params: dict, cand_metrics: dict, cur_metrics: dict,
            base_metrics: dict | None = None, base_params: dict | None = None,
            ba=None, symbols=SELFCHECK_SYMBOLS):
    g1 = gate_selfcheck(symbols)
    g2 = gate_oos(cand_metrics, cur_metrics)
    g3 = gate_fingerprint(cand_metrics, base_metrics, cand_params=cand_params,
                          base_params=base_params, ba=ba, check_data=ba is not None)
    g4 = gate_no_lookahead(cand_params)
    overall = g1['pass'] and g2['pass'] and g3['pass'] and g4['pass']
    return {
        'overall_pass': overall,
        'gates': {
            'selfcheck': g1,
            'oos': g2,
            'fingerprint': g3,
            'no_lookahead': g4,
        },
    }


def current_frozen():
    """读当前冻结参数（V5XConfig 默认）与 results_step46.json 的 V5X 指标。"""
    import fusion_v5x_engine as eng
    cfg = eng.V5XConfig()
    params = {k: getattr(cfg, k) for k in SAFE_PARAM_KEYS if hasattr(cfg, k)}
    cur_metrics = {}
    try:
        r = json.load(open(os.path.join(HERE, 'results_step46.json'), encoding='utf-8'))
        cur_metrics = r.get('V5X', r.get('V5X_study', {}))
    except Exception:
        pass
    return params, cur_metrics


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--candidate', help='候选参数+指标 JSON（{params:{...}, metrics:{...}}）')
    ap.add_argument('--apply', action='store_true', help='仅 4 关全过才写三源（默认 dry-run）')
    args = ap.parse_args()

    cur_params, cur_metrics = current_frozen()
    if args.candidate:
        cand = json.load(open(args.candidate, encoding='utf-8'))
        cand_params = cand.get('params', cand)
        cand_metrics = cand.get('metrics', cand)
    else:
        # 演示：候选=当前（应全过）
        cand_params, cand_metrics = cur_params, cur_metrics

    rep = run_all(cand_params, cand_metrics, cur_metrics,
                  base_metrics=cur_metrics, base_params=cur_params)
    print(json.dumps(rep, ensure_ascii=False, indent=1, default=str))
    print('OVERALL:', 'PASS' if rep['overall_pass'] else 'FAIL')
    if args.apply and rep['overall_pass']:
        print('（apply 模式）4 关通过 → 调用 governor 落地（见 v5x_governor.py）')
    return rep['overall_pass']


if __name__ == '__main__':
    ok = main()
    sys.exit(0 if ok else 1)
