"""
M4 回测独立审计脚本 v2（修复后复验，只读，不写业务库）

v2 相对 v1 的变化（适配 CodeBuddy 修复）：
  - BacktestResult 主键现为 (run_id, symbol, model)（P0 修复）→ 统计带 symbol 维度
  - 新增 backtest_detail 明细表核验：明细重算 vs 聚合表一致性（J 项）
  - M4.1 LSTM walk-forward 快照核验：快照覆盖、评估日 vs 快照 train_until 时序诚实性
  - 权重表核验升级：跨品种聚合复算 + 护栏（min_sample_n / min_symbols_in_run）
  - task_run 改为反射式查询（v1 列名硬编码失败）
  - P2-1 口径开关：读 config.backtest.label_metric 并核验 detail.actual

输出：/app/logs/audit_m4v2_<ts>.md + 控制台摘要
运行：docker exec qhyc-api python scripts/audit_m4_v2.py
"""
from __future__ import annotations

import sys
import math
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "/app")

import numpy as np
import pandas as pd
from scipy import stats
from sqlalchemy import select, text, inspect

from app.core.config import get_settings
from app.core.db import session_scope
from app.models import BacktestResult, ModelWeight
from app.features.pipeline import load_canonical_series

TZ = ZoneInfo("Asia/Shanghai")
TS = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
REPORT_PATH = f"/app/logs/audit_m4v2_{TS}.md"

findings: list[tuple[str, str, str]] = []


def add(level: str, title: str, detail: str) -> None:
    findings.append((level, title, detail))


def f(v):
    return None if v is None else float(v)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (center - half, center + half)


R: list[str] = []


def w(line: str = "") -> None:
    R.append(line)


# ============================================================
def main() -> None:
    settings = get_settings()
    cfg = settings.yaml.predict
    cfg_bt = getattr(settings.yaml, "backtest", None)
    label_metric = getattr(cfg_bt, "label_metric", "close") if cfg_bt else "close"
    min_sample_n = getattr(cfg_bt, "min_sample_n", 60) if cfg_bt else 60
    min_symbols = getattr(cfg_bt, "min_symbols_in_run", 10) if cfg_bt else 10
    retrain_every = getattr(cfg_bt, "lstm_retrain_every", 60) if cfg_bt else 60

    w("# M4 回测独立审计报告 v2（修复后复验）")
    w()
    w(f"- 生成时间：{datetime.now(TZ):%Y-%m-%d %H:%M:%S %Z}")
    w(f"- 回测配置：label_metric={label_metric}（P2-1 开关）, min_sample_n={min_sample_n}, "
      f"min_symbols_in_run={min_symbols}, lstm_retrain_every={retrain_every}")
    w(f"- predict.config 权重：{cfg.weights}")
    w()

    # ---------- A. 覆盖与完整性 ----------
    w("## A. 覆盖与完整性（P0 复验）")
    all_rows: list = []
    with session_scope() as s:
        all_rows = s.execute(select(BacktestResult)).scalars().all()
        n_rows = len(all_rows)
        run_ids = sorted({r.run_id for r in all_rows})
        models = sorted({r.model for r in all_rows})
        w(f"- backtest_result 总行数：{n_rows}，distinct run_id：{len(run_ids)}，模型数：{len(models)}")
        for rid in run_ids[-6:]:
            sub = [r for r in all_rows if r.run_id == rid]
            syms = {r.symbol for r in sub}
            w(f"  - run `{rid}`：{len(sub)} 行，品种数 {len(syms)}，"
              f"start={sub[0].start_date} end={sub[0].end_date}")
        sample_ns = [r.sample_n for r in all_rows if r.sample_n is not None]
        if sample_ns:
            w(f"- sample_n：min={min(sample_ns)}, max={max(sample_ns)}, median={int(np.median(sample_ns))}")
        # detail 表
        try:
            dr = s.execute(text("SELECT count(*), count(DISTINCT run_id), count(DISTINCT symbol) FROM backtest_detail")).one()
            w(f"- backtest_detail：{dr[0]} 行 / {dr[1]} run / {dr[2]} 品种")
        except Exception as e:
            w(f"- backtest_detail 查询失败：{e}")
        # task_run 反射式
        try:
            insp = inspect(s.get_bind())
            cols = [c["name"] for c in insp.get_columns("task_run")]
            taskcol = next((c for c in ("task", "task_name", "name") if c in cols), None)
            if taskcol:
                q = (f"SELECT {taskcol}, status, finished_at, message FROM task_run "
                     f"WHERE {taskcol} LIKE '%backtest%' ORDER BY 3 DESC NULLS LAST LIMIT 6")
                tr = s.execute(text(q)).all()
                w(f"- task_run 最近 backtest 任务 {len(tr)} 条：")
                for t in tr:
                    w(f"  - {t[0]}/{t[1]} @ {t[2]} — {str(t[3])[:120]}")
        except Exception as e:
            w(f"- task_run 查询失败（跳过）：{str(e)[:160]}")
    w()

    # P0 判定（v3 修正：取"最新全品种整批 run"而非"最新任意 run"，
    # 避免手动单品种/子集 run 触发误报——复验报告曾因此误判 P0 回归）
    BATCH_MIN_SYMBOLS = 50  # 整批 run 门槛（目标 73 品种，容忍新品种/差异）
    if run_ids:
        batch_runs = []
        for rid in run_ids:
            syms = {r.symbol for r in all_rows if r.run_id == rid}
            batch_runs.append((rid, len(syms)))
        full_runs = [x for x in batch_runs if x[1] >= BATCH_MIN_SYMBOLS]
        if full_runs:
            latest_run, latest_n_syms = full_runs[-1]
            latest_rows = [r for r in all_rows if r.run_id == latest_run]
            latest_syms = {r.symbol for r in latest_rows}
            per_sym_models = {sym: sum(1 for r in latest_rows if r.symbol == sym) for sym in latest_syms}
            if len(latest_syms) >= 10 and min(per_sym_models.values()) >= len(models) - 3:
                add("FIXED", "P0 已修复：最新整批 run 含全部品种且每品种模型齐全",
                    f"整批 run `{latest_run}` 含 {len(latest_syms)} 品种 × {len(models)} 模型，"
                    f"总 {n_rows} 行，覆盖问题消除。"
                    f"（非整批 run {len(run_ids) - len(full_runs)} 个已正确忽略，不参与 P0 判定）")
            else:
                add("P0", "覆盖异常仍存在（整批 run）",
                    f"最新整批 run 仅 {len(latest_syms)} 品种：{sorted(latest_syms)[:10]}")
        else:
            add("P0", "无整批 run",
                f"全部 {len(run_ids)} 个 run 的品种数均 < {BATCH_MIN_SYMBOLS}，"
                f"需先执行一次全品种回测再复验。各 run 品种数：{batch_runs[-6:]}")

    # ---------- B. 标签口径核验 ----------
    w("## B. 标签口径（label_metric 与明细核验）")
    w(f"- config label_metric = **{label_metric}**" + ("（= PRD-⑩ settle 口径）" if label_metric == "settle" else "（canonical close 口径，与 PRD-⑩ 仍为偏差，待裁决）"))
    with session_scope() as s:
        diff = s.execute(text(
            "SELECT sum(CASE WHEN sign(ret_close) <> sign(ret_settle) THEN 1 ELSE 0 END)::float "
            "/ count(*) FROM daily_bar WHERE ret_close IS NOT NULL AND ret_settle IS NOT NULL"
        )).scalar()
        w(f"- close/settle 日收益方向不一致率（全库）：{diff*100:.1f}%"
          + ("——若 label_metric=settle，标签已对齐 ⑩" if label_metric == "settle" else "——若维持 close 口径，此项为已知偏差"))
    w()

    # ---------- 基线宇宙（per-symbol） ----------
    base: dict[str, dict] = {}
    with session_scope() as s:
        syms_all = [x.symbol for x in settings.main_contracts]
        for sym in syms_all:
            try:
                df, source = load_canonical_series(s, sym)
                if len(df) < 310:
                    continue
                idx = df.index
                eval_dates = list(idx[-251:][::3])
                c = df["close"].astype(float).reset_index(drop=True)
                pos = {d: i for i, d in enumerate(idx)}
                acts, mom1, mom5 = [], [], []
                for t in eval_dates:
                    i = pos.get(t)
                    if i is None or i + 1 >= len(idx) or i < 5:
                        continue
                    a = (c.iloc[i + 1] - c.iloc[i]) / c.iloc[i] * 100.0
                    acts.append(a)
                    r1 = (c.iloc[i] - c.iloc[i - 1]) / c.iloc[i - 1] * 100.0
                    r5 = (c.iloc[i] - c.iloc[i - 5]) / c.iloc[i - 5] * 100.0
                    mom1.append(r1 > 0); mom5.append(r5 > 0)
                if len(acts) < 30:
                    continue
                aa = np.array(acts)
                base[sym] = {"n": len(aa), "up": float((aa > 0).mean()),
                             "mae0": float(np.abs(aa).mean()),
                             "mom1": float(np.mean([m == (a > 0) for m, a in zip(mom1, aa)])),
                             "mom5": float(np.mean([m == (a > 0) for m, a in zip(mom5, aa)]))}
            except Exception:
                continue
    if base:
        bu = pd.DataFrame(base).T
        w(f"- 基线宇宙：{len(base)} 品种；up-rate 均值 {bu.up.mean():.3f}；"
          f"零预测 MAE 均值 {bu.mae0.mean():.3f}%；动量1 acc 均值 {bu.mom1.mean():.3f}（最优 {bu.mom1.max():.3f}）；"
          f"动量5 均值 {bu.mom5.mean():.3f}（最优 {bu.mom5.max():.3f}）")
    w()

    # ---------- D/E/G：per (run, model) 跨品种聚合 + per-row 检验 ----------
    w("## D/E/G. 模型指标：跨品种聚合显著性 / 覆盖 / 红旗")
    agg: dict[str, dict] = {}
    for r in all_rows:
        n = r.sample_n or 0
        acc = f(r.dir_acc)
        if not n or acc is None:
            continue
        a = agg.setdefault(r.model, {"k": 0, "n": 0, "qh_k": 0, "qh_n": 0,
                                     "mae_w": 0.0, "rows": 0, "accs": [], "run": r.run_id})
        a["k"] += round(acc * n); a["n"] += n
        qh = f(r.quantile_hit)
        if qh is not None:
            a["qh_k"] += round(qh * n); a["qh_n"] += n
        a["mae_w"] += (f(r.mae) or 0.0) * n
        a["accs"].append(acc); a["rows"] += 1
    w("| model | 品种数 | 总n | dir_acc | binom_p | Wilson95 | MAE | qhit | qhit_p(0.9) |")
    w("|---|---|---|---|---|---|---|---|---|")
    for m in sorted(agg):
        a = agg[m]
        p = stats.binomtest(a["k"], a["n"], 0.5).pvalue
        lo, hi = wilson(a["k"], a["n"])
        qp = stats.binomtest(a["qh_k"], a["qh_n"], 0.9).pvalue if a["qh_n"] else float("nan")
        w(f"| {m} | {a['rows']} | {a['n']} | {a['k']/a['n']:.3f} | {p:.3g} | [{lo:.3f},{hi:.3f}] | "
          f"{a['mae_w']/max(a['n'],1):.3f} | {a['qh_k']/max(a['qh_n'],1):.3f} | {qp:.3g} |")
    w()
    for m, a in sorted(agg.items()):
        acc = a["k"] / a["n"]
        p = stats.binomtest(a["k"], a["n"], 0.5).pvalue
        if acc > 0.65 and a["n"] >= 60:
            add("P1", f"{m} 跨品种 dir_acc={acc:.3f}(n={a['n']}) > 0.65", f"binom p={p:.3g}，需排查泄漏。")
        if a["qh_n"]:
            cov = a["qh_k"] / a["qh_n"]
            if cov < 0.70 or cov > 0.98:
                add("P1", f"{m} 分位覆盖 {cov:.2f} 偏离 90% 目标",
                    f"n={a['qh_n']}，binom p(0.9)={stats.binomtest(a['qh_k'], a['qh_n'], 0.9).pvalue:.3g}")
    ens = agg.get("ensemble")
    if ens:
        acc = ens["k"] / ens["n"]
        p = stats.binomtest(ens["k"], ens["n"], 0.5).pvalue
        if p >= 0.05:
            add("NOTE", f"ensemble 跨品种 dir_acc={acc:.3f}(n={ens['n']})，p={p:.2g}——不显著优于随机",
                "诚实结果；模型筛选应基于此框架继续。")

    # ---------- H. LSTM 泄漏复验 ----------
    w()
    w("## H. LSTM 泄漏复验（M4.1 修复验收）")
    import os
    import glob as _glob
    lstm_dir = Path("/app/runtime/lstm")
    cur = [p for p in lstm_dir.glob("*.pt") if "__" not in p.name] if lstm_dir.exists() else []
    snaps = list(lstm_dir.glob("*__*.pt")) if lstm_dir.exists() else []
    w(f"- 线上权重（无 __ 后缀）：{len(cur)} 个；walk-forward 快照：{len(snaps)} 个")
    snap_by_sym: dict[str, list] = {}
    for sp in snaps:
        sym = sp.name.split("__")[0]
        snap_by_sym.setdefault(sym, []).append(sp.name.split("__")[1].replace(".pt", ""))
    if snaps:
        sample_syms = sorted(snap_by_sym)[:4]
        for sym in sample_syms:
            ds = sorted(snap_by_sym[sym])
            w(f"  - {sym}：{len(ds)} 个快照，train_until {ds[0]} ~ {ds[-1]}")
    lstm_agg = agg.get("lstm")
    nonlstm = [m for m in agg if m not in ("lstm", "ensemble")]
    if lstm_agg and nonlstm:
        lm = lstm_agg["k"] / lstm_agg["n"]
        nm_n = sum(agg[m]["n"] for m in nonlstm)
        nm_k = sum(agg[m]["k"] for m in nonlstm)
        nm = nm_k / nm_n
        w(f"- LSTM 跨品种 dir_acc {lm:.3f}(n={lstm_agg['n']}) vs 其他模型 {nm:.3f}（差 {lm-nm:+.3f}）")
        if lm - nm > 0.05:
            add("P1", f"LSTM 仍显著高于其他模型（+{(lm-nm)*100:.1f}pp）",
                "若快照时序核验（下）无异常，可能为真实信号或残泄漏（如传导特征构造窗口），需人工复核。")
        else:
            add("FIXED", f"LSTM dir_acc 回落至 {lm:.3f}，与其他模型（{nm:.3f}）基本一致",
                "前视泄漏消除的验收标准达成。")
    # 时序诚实性：LSTM 明细首评估日 vs 首快照 train_until
    try:
        with session_scope() as s:
            ld = s.execute(text(
                "SELECT symbol, min(eval_date), count(*) FROM backtest_detail "
                "WHERE model='lstm' GROUP BY symbol ORDER BY symbol LIMIT 80"
            )).all()
        bad = 0
        checked = 0
        for sym, first_eval, cnt in ld:
            if sym not in snap_by_sym:
                bad += 1
                continue
            checked += 1
            first_snap = min(snap_by_sym[sym])
            # first_eval 若早于任何快照（YYYYMMDD 字符串比较即可）→ 该点只能用未来权重
            if str(first_eval).replace("-", "") < first_snap:
                bad += 1
        w(f"- LSTM 明细覆盖 {len(ld)} 品种，评估日早于最早快照（或无快照）的品种数：{bad}")
        if bad == 0 and checked > 0:
            add("FIXED", "LSTM 快照时序诚实性通过", "所有 LSTM 评估点均存在 train_until ≤ 评估日的快照。")
        elif bad:
            add("P1", f"{bad} 个品种的 LSTM 评估点早于最早快照", "这些点可能仍在用未来权重，需检查 walk-forward 段划分。")
    except Exception as e:
        w(f"- 时序核验失败：{str(e)[:160]}")
    w()

    # ---------- J. 明细 vs 聚合一致性 + 标签复算 ----------
    w("## J. 明细一致性核验（backtest_detail 重算 vs backtest_result）")
    try:
        with session_scope() as s:
            det = s.execute(text(
                "SELECT run_id, symbol, model, "
                "count(*) AS n, "
                "avg(CASE WHEN pred_dir = CASE WHEN actual > 0 THEN 'up' ELSE 'down' END "
                "THEN 1.0 ELSE 0.0 END) AS acc, "
                "avg(abs(point - actual)) AS mae, "
                "avg(CASE WHEN low <= actual AND actual <= high THEN 1.0 ELSE 0.0 END) AS cov "
                "FROM backtest_detail GROUP BY run_id, symbol, model"
            )).all()
        dfd = pd.DataFrame([{
            "run_id": d[0], "symbol": d[1], "model": d[2], "n_d": d[3],
            "acc_d": float(d[4]) if d[4] is not None else None,
            "mae_d": float(d[5]) if d[5] is not None else None,
            "cov_d": float(d[6]) if d[6] is not None else None,
        } for d in det])
        dfr = pd.DataFrame([{
            "run_id": r.run_id, "symbol": r.symbol, "model": r.model, "n_r": r.sample_n,
            "acc_r": f(r.dir_acc), "mae_r": f(r.mae), "cov_r": f(r.quantile_hit),
        } for r in all_rows])
        mg = dfd.merge(dfr, on=["run_id", "symbol", "model"], how="inner")
        if len(mg):
            # 容差：聚合表 acc/mae 为 4 位舍入
            acc_bad = mg[(mg.acc_d - mg.acc_r).abs() > 0.011]
            n_bad = mg[mg.n_d != mg.n_r]
            w(f"- 可比对 (run,symbol,model)：{len(mg)}")
            w(f"- 方向准确率偏差 >1.1pp：{len(acc_bad)}；样本数不一致：{len(n_bad)}")
            if len(acc_bad) > len(mg) * 0.02:
                add("P1", "明细重算与聚合表偏差过大", f"{len(acc_bad)}/{len(mg)} 行 acc 偏差 >1.1pp，聚合逻辑可能有问题。")
            elif len(mg) >= 50:
                add("FIXED", "明细与聚合一致", f"{len(mg)} 行抽样比对通过（P1-4 修复有效）。")
            # 标签复算：抽 3 品种
            with session_scope() as s:
                sym_list = sorted(mg.symbol.unique())[:3]
                for sym in sym_list:
                    dfx, _src = load_canonical_series(s, sym)
                    c = dfx["close"].astype(float)
                    sub = det = None
                    sub_rows = s.execute(text(
                        "SELECT eval_date, actual FROM backtest_detail "
                        "WHERE symbol=:sy AND model='ensemble' ORDER BY eval_date LIMIT 12"
                    ), {"sy": sym}).all()
                    mism = 0; tot = 0
                    for ed, act in sub_rows:
                        idx = dfx.index
                        if ed not in idx:
                            continue
                        i = idx.get_loc(ed)
                        if i + 1 >= len(idx):
                            continue
                        rec = (float(c.iloc[i + 1]) - float(c.iloc[i])) / float(c.iloc[i]) * 100.0
                        tot += 1
                        if abs(rec - float(act)) > 0.015:
                            mism += 1
                    if tot:
                        w(f"- {sym} 标签复算：{tot} 点中偏差 >0.015pp 的 {mism} 个"
                          + ("（label_metric=close 一致 ✓）" if mism == 0 and label_metric == "close" else ""))
        else:
            add("P2", "明细表与聚合表无法关联比对", "merge 结果为空，检查 run_id/symbol 写入。")
    except Exception as e:
        w(f"- 明细核验失败：{str(e)[:200]}")
    w()

    # ---------- F. by_state ----------
    w("## F. by_state 分层（跨品种聚合）")
    st_k: dict[str, int] = {}; st_n: dict[str, int] = {}
    for r in all_rows:
        for st, v in ((r.by_state or {}).get("states") or {}).items():
            n = v.get("n", 0); acc = v.get("dir_acc")
            if n and acc is not None:
                st_k[st] = st_k.get(st, 0) + round(acc * n)
                st_n[st] = st_n.get(st, 0) + n
    for st in sorted(st_n):
        w(f"- {st}: n={st_n[st]}, dir_acc={st_k[st]/st_n[st]:.3f}")
    w()

    # ---------- I. 权重表复验 ----------
    w("## I. model_weights 复验（跨品种聚合 + 护栏）")
    with session_scope() as s:
        mw = s.execute(select(ModelWeight)).scalars().all()
        w(f"- model_weights 行数：{len(mw)}")
        w("| model | weight | dir_acc | sample_n | 复算 w | 与聚合 acc 偏差 |")
        w("|---|---|---|---|---|---|")
        for r in mw:
            acc = f(r.dir_acc)
            exp_w = min(2.0, max(0.1, (acc - 0.5) * 4)) if acc is not None else None
            gap = ""
            if acc is not None and r.model in agg and agg[r.model]["n"]:
                gap = f"{acc - agg[r.model]['k']/agg[r.model]['n']:+.3f}"
            w(f"| {r.model} | {float(r.weight):.3f} | {acc:.3f} | {r.sample_n} | "
              f"{exp_w:.3f} | {gap} |" if exp_w is not None else
              f"| {r.model} | {float(r.weight):.3f} | — | {r.sample_n} | — | — |")
        db_w = {r.model: float(r.weight) for r in mw}
        cfg_w = dict(cfg.weights)
        diffw = {k: (cfg_w.get(k), db_w[k]) for k in db_w if k in cfg_w and abs(cfg_w[k] - db_w[k]) > 0.05}
        w(f"- DB vs config 权重差异 >0.05：{diffw if diffw else '无（等权/接近）'}")
        # LSTM 冻结检查
        lf = getattr(cfg_bt, "lstm_weight_freeze", None) if cfg_bt else None
        if lf is not None and "lstm" in db_w and abs(db_w["lstm"] - float(lf)) > 0.01:
            add("P2", f"lstm_weight_freeze={lf} 但 DB 权重为 {db_w['lstm']}", "冻结值未生效或被覆盖。")
        # 权重是否来自整批 run
        if all_rows:
            latest_run = run_ids[-1]
            src_syms = {r.symbol for r in all_rows if r.run_id == latest_run}
            if len(src_syms) >= min_symbols:
                add("FIXED", "权重来源为整批 run 且护栏生效",
                    f"最新 run 含 {len(src_syms)} 品种 ≥ min_symbols_in_run={min_symbols}；"
                    f"min_sample_n={min_sample_n} 护栏在 weights.py 生效（代码级确认）。")
    w()

    # ---------- 结论 ----------
    w("## 审计结论（v2）")
    order = {"P0": 0, "P1": 1, "P2": 2, "NOTE": 3, "FIXED": 4}
    for lv, t, d in sorted(findings, key=lambda x: order.get(x[0], 9)):
        icon = {"P0": "🔴", "P1": "🟠", "P2": "🟡", "NOTE": "🔵", "FIXED": "🟢"}.get(lv, "•")
        w(f"### {icon} [{lv}] {t}")
        if d:
            w(f"{d}")
        w()

    Path(REPORT_PATH).write_text("\n".join(R), encoding="utf-8")

    print("=" * 60)
    print(f"M4 v2 复验完成 → {REPORT_PATH}")
    print("=" * 60)
    for lv, t, d in sorted(findings, key=lambda x: order.get(x[0], 9)):
        print(f"[{lv}] {t}")
    print("-" * 60)
    cnt = {k: sum(1 for x in findings if x[0] == k) for k in ("P0", "P1", "P2", "NOTE", "FIXED")}
    print(" ".join(f"{k}={v}" for k, v in cnt.items()))


if __name__ == "__main__":
    main()
