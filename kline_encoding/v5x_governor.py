# -*- coding: utf-8 -*-
"""v5x_governor.py —— V5X 自进化「Governor 编排 + 漂移监控」

闭环（见 v5x_self_evolution_design.html §2）：
  漂移监控(C) → 触发滚动重扫(B) → 候选参数 → 四道硬护栏(v5x_relock_guard) → 通过才重锁。

铁律：默认 dry-run，只产报告 + 变更日志，**绝不自动 overwrite 生产参数**。
落地需两层显式确认：--apply 且 --confirm，且 4 道护栏全过。

典型用法：
  # 仅监控 + 评估已存在的候选（来自 step74 reco 或 --candidate）
  python v5x_governor.py --candidate cand.json
  # 真要落地（必须 4 关全过）：
  python v5x_governor.py --candidate cand.json --apply --confirm
"""
from __future__ import annotations
import os, sys, json, argparse, datetime, shutil, re, subprocess
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np
import pandas as pd

import v5x_relock_guard as G
import fusion_v5x_engine as ENG
import step46_prod_bt as S46

CHANGELOG = os.path.join(HERE, 'v5x_changelog.json')
FROZEN = os.path.join(HERE, 'v5x_frozen_params.json')
SPEC = os.path.join(HERE, 'fusion_v5x_spec.html')
CUR_VERSION = 'V5X.1'   # 当前冻结版本号（重锁后基线）


# ---------------------------------------------------------------- 漂移监控
def drift_report(live_trades: pd.DataFrame | None = None, window: int = 60,
                 expected: dict | None = None):
    """比较实盘(滚动窗口) vs 回测预期。无 live 数据时返回 monitor-only。
    live_trades 需含列：net(单笔净利)、xdt(出场时间)。"""
    if live_trades is None or len(live_trades) == 0:
        return {'trigger': False, 'mode': 'monitor-only',
                'reason': '未提供实盘成交（live_trades），仅做监控，不触发重扫'}
    df = live_trades.sort_values('xdt').reset_index(drop=True)
    recent = df.tail(window)
    if len(recent) < 10:
        return {'trigger': False, 'mode': 'insufficient-live',
                'reason': f'实盘样本 {len(recent)} < 10，窗口不足'}
    eq = recent.net.cumsum()
    mdd = float((eq - eq.cummax()).min())
    net = float(recent.net.sum())
    mar = net / abs(mdd) if mdd < 0 else np.nan
    exp_mar = (expected or {}).get('mar')
    trigger = False
    note = ''
    if exp_mar and np.isfinite(exp_mar) and np.isfinite(mar):
        if mar < 0.6 * exp_mar:
            trigger = True
            note = f'实盘滚动 MAR {mar:.2f} < 0.6×回测预期 {exp_mar:.2f} → 触发重扫'
    return {'trigger': trigger, 'mode': 'live', 'window': window,
            'live_mar': mar, 'live_net': net, 'live_mdd': mdd,
            'expected_mar': exp_mar, 'note': note}


# ---------------------------------------------------------------- 候选来源
def propose_from_step74(path='step74_compare.json'):
    """从 step74 的 reco 读取候选参数（metrics 需另行评估，本函数只给参数）。"""
    try:
        d = json.load(open(os.path.join(HERE, path), encoding='utf-8'))
    except Exception:
        return None
    reco = d.get('reco')
    if not reco:
        return None
    # step74 reco 键名与 V5XConfig 一致：SL_ATR/TRAIL_ATR/BE_R/COOLDOWN/ADD_MAX_LOTS/ADD_THR
    return {k: reco[k] for k in ('SL_ATR', 'TRAIL_ATR', 'BE_R', 'COOLDOWN',
                                 'ADD_MAX_LOTS', 'ADD_THR') if k in reco}


# ---------------------------------------------------------------- 版本治理
def _load_changelog():
    try:
        return json.load(open(CHANGELOG, encoding='utf-8'))
    except Exception:
        return []


def append_changelog(entry: dict):
    log = _load_changelog()
    log.append(entry)
    json.dump(log, open(CHANGELOG, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    return entry


def next_version():
    log = _load_changelog()
    nums = [int(e['version'].split('.')[-1]) for e in log if e.get('version', '').startswith('V5X.')]
    return f'V5X.{max(nums + [1]) + 1}'


def bump_spec_corrigendum(version, params, guard_ok, note):
    """向 fusion_v5x_spec.html 追加一段勘误（仅在 apply 且 gates 通过时调用）。
    插入到 </body> 之前，安全且不改动既有结构。"""
    with open(SPEC, encoding='utf-8') as f:
        html = f.read()
    stamp = datetime.date.today().isoformat()
    rows = ''.join(f'<tr><td>{k}</td><td>{v}</td></tr>' for k, v in params.items())
    block = f'''
  <div class="risk" style="margin-top:10px"><b>勘误（{stamp}，{version} 自进化重锁）：</b>
    四道硬护栏结果：{"全过" if guard_ok else "未通过(保留上一版本)"}。{note}
    <table class="mini"><tbody>{rows}</tbody></table></div>
'''
    if '</body>' in html:
        html = html.replace('</body>', block + '\n</body>', 1)
    else:
        html += block
    with open(SPEC, 'w', encoding='utf-8') as f:
        f.write(html)


# ---------------------------------------------------------------- 落地（高危，需双层确认）
# step46 全局量与引擎 V5XConfig 的精确替换规则：只改数值字面，保留既有注释尾。
# 每条规则必须命中恰好 1 次，否则视为结构漂移 → 中止并回滚。
_STEP46_RULES = [
    ('SL_ATR/TRAIL_ATR/BE_R',
     re.compile(r'(SL_ATR, TRAIL_ATR, BE_R = )[\d.]+, [\d.]+, [\d.]+'),
     lambda m, p: m.group(1) + f"{p['SL_ATR']}, {p['TRAIL_ATR']}, {p['BE_R']}"),
    ('W_PIVOT/COOLDOWN',
     re.compile(r'(W_PIVOT, COOLDOWN = )\d+, \d+'),
     lambda m, p: m.group(1) + f"60, {p['COOLDOWN']}"),
    ('ADD_MAX_LOTS/ADD_THR',
     re.compile(r'(ADD_MAX_LOTS, ADD_THR = )\d+, [\d.]+'),
     lambda m, p: m.group(1) + f"{p['ADD_MAX_LOTS']}, {p['ADD_THR']}"),
]
_ENG_RULES = [
    ('SL_ATR', re.compile(r'(SL_ATR: float = )[\d.]+'),
     lambda m, p: m.group(1) + f"{p['SL_ATR']}"),
    ('TRAIL_ATR', re.compile(r'(TRAIL_ATR: float = )[\d.]+'),
     lambda m, p: m.group(1) + f"{p['TRAIL_ATR']}"),
    ('BE_R', re.compile(r'(BE_R: float = )[\d.]+'),
     lambda m, p: m.group(1) + f"{p['BE_R']}"),
    ('COOLDOWN', re.compile(r'(COOLDOWN: int = )\d+'),
     lambda m, p: m.group(1) + f"{p['COOLDOWN']}"),
    ('ADD_MAX_LOTS', re.compile(r'(ADD_MAX_LOTS: int = )\d+'),
     lambda m, p: m.group(1) + f"{p['ADD_MAX_LOTS']}"),
    ('ADD_THR', re.compile(r'(ADD_THR: float = )[\d.]+'),
     lambda m, p: m.group(1) + f"{p['ADD_THR']}"),
]


def _patch_file(path, rules, params):
    """对单文件逐条 regex 替换；返回每条规则的命中计数（须 ==1）。
    用 newline='' 保留原始换行符（不污染 git diff）；无变化时绝不落盘。"""
    with open(path, 'r', encoding='utf-8', newline='') as f:
        text = f.read()
    orig = text
    counts = {}
    for name, rx, fn in rules:
        text, n = rx.subn(lambda m, fn=fn: fn(m, params), text)
        counts[name] = n
    if text != orig:
        with open(path, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
    return counts


def apply_to_sources(params: dict):
    """把候选参数写回三源：step46 全局量 + 引擎 V5XConfig + 重跑回测 + 三源对拍。
    每次先备份四件产物（两个源 + trades CSV + results JSON）。
    仅 4 关全过 + --confirm 才调用。
    终极护栏：改写后重跑 step46 V5X 再生 trades46_V5X.csv，再跑引擎 self_check；
    若对拍失败，回滚全部备份并抛错。"""
    ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    artifacts = ['step46_prod_bt.py', 'fusion_v5x_engine.py',
                 'trades46_V5X.csv', 'results_step46.json']
    backups = {}
    for f in artifacts:
        src = os.path.join(HERE, f)
        if os.path.exists(src):
            dst = os.path.join(HERE, f'.bak_apply_{ts}_{f}')
            shutil.copy(src, dst)
            backups[f] = dst

    def _restore():
        for f, dst in backups.items():
            shutil.copy(dst, os.path.join(HERE, f))

    step46_py = os.path.join(HERE, 'step46_prod_bt.py')
    eng_py = os.path.join(HERE, 'fusion_v5x_engine.py')
    try:
        c46 = _patch_file(step46_py, _STEP46_RULES, params)
        cEng = _patch_file(eng_py, _ENG_RULES, params)
    except Exception as e:
        _restore()
        raise RuntimeError(f'patch 异常 → 已回滚: {e}')

    bad = {k: v for k, v in {**c46, **cEng}.items() if v != 1}
    if bad:
        _restore()
        raise RuntimeError(f'替换命中数非 1（结构漂移）→ 已回滚: {bad}')

    # 重跑 step46 V5X 再生 trades46_V5X.csv / results_step46.json（与引擎新参数一致）
    try:
        subprocess.run([sys.executable, 'step46_prod_bt.py', 'V5X'],
                       cwd=HERE, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        _restore()
        raise RuntimeError(f'step46 重跑失败 → 已回滚: {e.stderr[:500]}')

    # 终极护栏：引擎 self_check 必须 PASS（逐笔对拍）
    import importlib
    importlib.reload(ENG)
    ok = ENG.self_check()
    if not ok:
        _restore()
        # 回滚后还需把 CSV/JSON 也还原（已在 _restore 覆盖）
        raise RuntimeError('self_check FAIL 于落地后 → 已全量回滚，三源仍对齐于上一版本')

    return {'backup_ts': ts, 'step46_counts': c46, 'engine_counts': cEng,
            'self_check': 'PASS', 'backups': backups}


def restore_from_backup(ts: str):
    """应急：用指定 bak_apply_{ts}_* 还原三源（手动恢复用）。"""
    for f in ['step46_prod_bt.py', 'fusion_v5x_engine.py',
              'trades46_V5X.csv', 'results_step46.json']:
        bak = os.path.join(HERE, f'.bak_apply_{ts}_{f}')
        if os.path.exists(bak):
            shutil.copy(bak, os.path.join(HERE, f))
    print(f'已从 {ts} 还原三源。')


# ---------------------------------------------------------------- 主流程
def run(candidate: dict | None = None, live: pd.DataFrame | None = None,
        apply: bool = False, confirm: bool = False, sweep: bool = False):
    cur_params, cur_metrics = G.current_frozen()
    drift = drift_report(live, expected=cur_metrics)
    print(f'[drift] mode={drift["mode"]} trigger={drift["trigger"]} :: {drift.get("reason","")}')

    cand_params = candidate.get('params') if candidate else propose_from_step74()
    cand_metrics = candidate.get('metrics') if candidate else None

    report = {
        'ts': datetime.datetime.now().isoformat(timespec='seconds'),
        'version_before': CUR_VERSION,
        'drift': drift,
        'candidate_params': cand_params,
        'guard': None,
        'decision': 'monitor-only',
    }

    if cand_params is None:
        print('[governor] 无候选参数 → 维持当前冻结版本，仅记录监控。')
        append_changelog({**report, 'version': CUR_VERSION, 'action': 'monitor-only'})
        return report

    # 候选与当前冻结值完全一致 → 非重锁，直接短路，避免污染规格/变更日志
    if cand_params == cur_params:
        report['decision'] = 'ALREADY-CURRENT (no-op)'
        print('[governor] 候选参数与当前冻结版完全一致 → 无需重锁。')
        append_changelog({**report, 'version': CUR_VERSION, 'action': 'noop-same'})
        return report

    # 跑四道护栏；候选无 metrics 时 OOS 关会因缺数据 FAIL（正确：未评估不得落地）
    rep = G.run_all(cand_params, cand_metrics or {}, cur_metrics,
                    base_metrics=cur_metrics, base_params=cur_params)
    report['guard'] = rep
    report['overall_pass'] = rep['overall_pass']

    if not rep['overall_pass']:
        report['decision'] = 'REJECTED (guard fail)'
        print('[governor] 候选被护栏拦截，保留当前版本。')
        append_changelog({**report, 'version': CUR_VERSION, 'action': 'rejected'})
        return report

    # 4 关全过
    if apply and confirm:
        ver = next_version()
        report['version_after'] = ver
        report['decision'] = f'APPLIED {ver}'
        # 先落地源文件 + 重跑 + 三源对拍（内部备份+回滚护栏）；成功后才写权威记录
        patch = apply_to_sources(cand_params)
        report['patch'] = patch
        # 写冻结快照（权威记录）
        json.dump({'version': ver, 'params': cand_params, 'metrics': cand_metrics,
                   'ts': report['ts']}, open(FROZEN, 'w', encoding='utf-8'),
                  ensure_ascii=False, indent=1)
        bump_spec_corrigendum(ver, cand_params, True, '自进化 Governor 自动重锁（4 关全过）。')
        print(f'[governor] 已落地 {ver}，源备份 {patch["backup_ts"]}，规格勘误已写入。')
        append_changelog({**report, 'version': ver, 'action': 'applied'})
    else:
        report['decision'] = 'PROPOSED (dry-run)'
        print('[governor] 4 关全过 → 建议重锁（dry-run，未落地）。'
              '用 --apply --confirm 确认落地。')
        append_changelog({**report, 'version': CUR_VERSION, 'action': 'proposed'})
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--candidate', help='候选 JSON {params:{...}, metrics:{...}}')
    ap.add_argument('--live', help='实盘成交 CSV（含 net,xdt 列）')
    ap.add_argument('--apply', action='store_true')
    ap.add_argument('--confirm', action='store_true', help='与 --apply 同用，才真正改写三源')
    ap.add_argument('--sweep', action='store_true', help='占位：触发 step73/74（需 prod50 数据，另调度）')
    a = ap.parse_args()
    cand = json.load(open(a.candidate, encoding='utf-8')) if a.candidate else None
    live = pd.read_csv(a.live) if a.live else None
    rep = run(candidate=cand, live=live, apply=a.apply, confirm=a.confirm, sweep=a.sweep)
    out = os.path.join(HERE, 'v5x_governor_report.json')
    json.dump(rep, open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1, default=str)
    print('report ->', out)
    print('DECISION:', rep['decision'])


if __name__ == '__main__':
    main()
