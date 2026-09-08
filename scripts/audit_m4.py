"""
M4 回测独立审计脚本（只读，不写业务库）

审计检查项：
  A. 覆盖与完整性     —— backtest_result 行分布 / run_id 数 / sample_n
  B. 标签口径核验     —— 重算次日收益（close 口径）并评估 settle 口径偏差
  C. 基线对比         —— 动量/反转/常涨基线 vs 模型 dir_acc、零预测 MAE
  D. 统计显著性       —— 每行 dir_acc 二项检验 + Wilson 95% CI
  E. 分位覆盖         —— quantile_hit vs 0.90 目标
  F. 分层与漂移       —— by_state 分层、recent60 vs 全样本
  G. 红旗扫描         —— dir_acc 过高 / coverage 异常 / MAE 过小
  H. LSTM 泄漏评估    —— runtime 权重文件、LSTM 与其他模型差异
  I. 权重表核验       —— model_weights 公式复算、与 config 权重对比

输出：/app/logs/audit_m4_<ts>.md + 控制台摘要
运行：docker exec qhyc-api python scripts/audit_m4.py
"""
from __future__ import annotations

import sys
import math
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "/app")

import numpy as np
import pandas as pd
from scipy import stats
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.db import session_scope
from app.models import BacktestResult, ModelWeight
from app.features.pipeline import load_canonical_series

TZ = ZoneInfo("Asia/Shanghai")
TS = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
REPORT_PATH = f"/app/logs/audit_m4_{TS}.md"

findings: list[tuple[str, str, str]] = []  # (级别, 标题, 详情)


def add(level: str, title: str, detail: str) -> None:
    findings.append((level, title, detail))


def f(v) -> float | None:
    return None if v is None else float(v)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    den = 1 + z * z / n
    center = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (center - half, center + half)


R: list[str] = []  # markdown 行缓冲


def w(line: str = "") -> None:
    R.append(line)


# ============================================================
def main() -> None:
    settings = get_settings()
    cfg = settings.yaml.predict

    w(f"# M4 回测独立审计报告")
    w()
    w(f"- 生成时间：{datetime.now(TZ):%Y-%m-%d %H:%M:%S %Z}")
    w(f"- 数据库：backtest_result / model_weights / task_run / daily_bar / main_continuous（只读）")
    w(f"- 回测参数（config）：window={cfg.history_bars}, test_days=250, step=3, min_bars=60")
    w(f"- 回测 config 权重：{cfg.weights}")
    w()

    # ---------- A. 覆盖与完整性 ----------
    w("## A. 覆盖与完整性")
    with session_scope() as s:
        rows = s.execute(select(BacktestResult)).scalars().all()
        n_rows = len(rows)
        run_ids = sorted({r.run_id for r in rows})
        models = sorted({r.model for r in rows})
        w(f"- backtest_result 总行数：{n_rows}，distinct run_id：{len(run_ids)}")
        w(f"- 模型清单（{len(models)}）：{', '.join(models)}")
        exp_models = len(models)
        # 每 run 行数
        per_run: dict[str, int] = {}
        for r in rows:
            per_run[r.run_id] = per_run.get(r.run_id, 0) + 1
        for rid in run_ids[-10:]:
            w(f"  - run `{rid}`：{per_run[rid]} 行（预期 ≈ {exp_models} 行 = 模型数×1 品种）")
        sample_ns = [r.sample_n for r in rows if r.sample_n is not None]
        if sample_ns:
            w(f"- sample_n：min={min(sample_ns)}, max={max(sample_ns)}, "
              f"median={int(np.median(sample_ns))}（预期 ≈ 84 = ceil(251/3) 附近）")
        # window / dates
        dates = [(r.start_date, r.end_date) for r in rows if r.start_date and r.end_date]
        if dates:
            w(f"- 回测区间：{min(d[0] for d in dates)} ~ {max(d[1] for d in dates)}")
        # task_run
        try:
            tr = s.execute(text(
                "SELECT task, status, label, finished_at, message FROM task_run "
                "WHERE task='backtest' ORDER BY started_at DESC LIMIT 8"
            )).all()
            w(f"- task_run 最近 backtest 任务 {len(tr)} 条：")
            for t in tr:
                w(f"  - {t[0]}/{t[1]}/{t[2]} @ {t[3]} — {t[4]}")
        except Exception as e:
            w(f"- task_run 查询失败（跳过）：{e}")
    w()

    # 覆盖 bug 判定
    if run_ids:
        max_rows = max(per_run.values())
        if max_rows <= exp_models + 2 and len(run_ids) >= 1 and n_rows / len(run_ids) <= exp_models + 2:
            add("P0", "backtest_result 主键缺 symbol 维度 → 多品种互相覆盖",
                f"backtest_symbols 对整批品种共用一个 run_id，而主键是 (run_id, model)。"
                f"每个 run 仅存 ≈{per_run[run_ids[-1]]} 行（模型数），"
                f"说明落库结果只保留最后一个成功品种，其余品种被 on_conflict_do_update 覆盖。"
                f"下游 update_model_weights 也只读到该品种的 recent60_dir_acc → 月更权重实际只由单一品种决定。")
        else:
            add("OK", "run_id 行数分布正常", "")

    # ---------- B. 标签口径核验 ----------
    w("## B. 标签口径核验（close vs settle）")
    with session_scope() as s:
        # 取最新 run 的区间做标签复算
        latest = s.execute(
            select(BacktestResult).order_by(BacktestResult.created_at.desc()).limit(1)
        ).scalar()
        src = (latest.by_state or {}).get("source") if latest else None
        w(f"- 最新 run：`{latest.run_id if latest else '—'}`，canonical source：`{src}`")
        # 用 daily_bar 对比 close 与 settle 收益的标签差异（⑩ 口径为 settle）
        diff_rows = s.execute(text(
            "SELECT symbol, count(*) AS n, "
            "sum(CASE WHEN sign(ret_close) <> sign(ret_settle) THEN 1 ELSE 0 END) AS mismatch, "
            "avg(abs(ret_close - ret_settle)) AS avg_gap "
            "FROM daily_bar WHERE ret_close IS NOT NULL AND ret_settle IS NOT NULL "
            "GROUP BY symbol ORDER BY mismatch DESC LIMIT 8"
        )).all()
        tot_n = tot_m = 0
        for d in diff_rows:
            w(f"  - {d[0]}：{d[1]} 天中 close/settle 方向不一致 {d[2]} 天（{d[2]/d[1]*100:.1f}%），平均幅度差 {f(d[3]):.3f}%")
            tot_n += d[1]; tot_m += d[2]
        if tot_n:
            w(f"- **抽样合计**：{tot_n} 天，方向标签不一致 {tot_m} 天（{tot_m/tot_n*100:.1f}%）")
        add("P2", "回测标签用平滑主连 close-to-close，非 PRD-⑩ settle 口径",
            f"_next_ret 取 canonical（csv_smooth）序列相邻收盘价收益。抽样显示 close 与 settle 日收益方向不一致率约 "
            f"{tot_m/tot_n*100:.1f}%（幅度差均值见上表）。这是口径偏差而非错误，但需明确承认并在看板标注，"
            f"或改为 settle 收益以对齐 PRD-⑩。")
    w()

    # ---------- C/D/E/F/G. 基于 DB 行的全量统计 + 品种基线 ----------
    w("## C~G. 模型指标统计检验 / 基线对比 / 覆盖 / 红旗")
    all_rows: list = []
    with session_scope() as s:
        all_rows = s.execute(select(BacktestResult)).scalars().all()

    # C. 基线：对每个品种在同一评估点框架下计算
    base_rows: dict[str, dict] = {}
    sym_universe: dict[str, dict] = {}
    with session_scope() as s:
        syms = [x.symbol for x in get_settings().main_contracts]
        for sym in syms:
            try:
                df, source = load_canonical_series(s, sym)
                if len(df) < 310:
                    continue
                idx = df.index
                eval_dates = list(idx[-251:][::3])
                c = df["close"].astype(float).reset_index(drop=True)
                pos = {d: i for i, d in enumerate(idx)}
                acts, mom1, mom5, rev = [], [], [], []
                for t in eval_dates:
                    i = pos.get(t)
                    if i is None or i + 1 >= len(idx):
                        continue
                    a = (c.iloc[i + 1] - c.iloc[i]) / c.iloc[i] * 100.0
                    if i < 5:
                        continue
                    acts.append(a)
                    r1 = (c.iloc[i] - c.iloc[i - 1]) / c.iloc[i - 1] * 100.0
                    r5 = (c.iloc[i] - c.iloc[i - 5]) / c.iloc[i - 5] * 100.0
                    mom1.append(r1 > 0)
                    mom5.append(r5 > 0)
                    rev.append(r1 < 0)
                if len(acts) < 30:
                    continue
                acts_a = np.array(acts)
                sym_universe[sym] = {
                    "n": len(acts), "up_rate": float((acts_a > 0).mean()),
                    "std": float(acts_a.std()), "mae0": float(np.abs(acts_a).mean()),
                    "mom1_acc": float(np.mean([m == (a > 0) for m, a in zip(mom1, acts_a)])),
                    "mom5_acc": float(np.mean([m == (a > 0) for m, a in zip(mom5, acts_a)])),
                    "rev_acc": float(np.mean([m == (a > 0) for m, a in zip(rev, acts_a)])),
                }
                base_rows[sym] = sym_universe[sym]
            except Exception:
                continue

    if sym_universe:
        bu = pd.DataFrame(sym_universe).T
        MAE0_REF = float(bu.mae0.mean())
        w(f"- 基线宇宙：{len(sym_universe)} 个品种（canonical 序列、同评估点框架 251 根/步长 3）")
        w(f"- 标签（次日收益）up-rate：均值 {bu.up_rate.mean():.3f}，"
          f"范围 [{bu.up_rate.min():.3f}, {bu.up_rate.max():.3f}]")
        w(f"- 零预测 MAE 基线（mean|ret|）：均值 {bu.mae0.mean():.3f}%，范围 [{bu.mae0.min():.3f}, {bu.mae0.max():.3f}]")
        w(f"- 动量1基线 dir_acc：均值 {bu.mom1_acc.mean():.3f}，最优 {bu.mom1_acc.max():.3f}；"
          f"动量5：均值 {bu.mom5_acc.mean():.3f}，最优 {bu.mom5_acc.max():.3f}；"
          f"反转1：均值 {bu.rev_acc.mean():.3f}")

    # D/E/G：全行统计
    w()
    w("| run_id | model | n | dir_acc | binom_p(0.5) | Wilson95 | mae | mae0同品种参考 | qhit | qhit_p(0.9) |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    mae0_ref = MAE0_REF if sym_universe else float("nan")
    stats_rows = []
    for r in all_rows:
        n = r.sample_n or 0
        acc = f(r.dir_acc)
        if not n or acc is None:
            continue
        k = round(acc * n)
        p = stats.binomtest(k, n, 0.5).pvalue
        lo, hi = wilson(k, n)
        qh = f(r.quantile_hit)
        qp = None
        if qh is not None and n:
            qp = stats.binomtest(round(qh * n), n, 0.9).pvalue
        stats_rows.append((r.run_id, r.model, n, acc, p, lo, hi, f(r.mae), qh, qp))
        w(f"| {r.run_id[:16]}… | {r.model} | {n} | {acc:.3f} | {p:.3g} | [{lo:.3f},{hi:.3f}] | "
          f"{f(r.mae):.3f} | ~{mae0_ref:.2f} | {qh:.2f} | {qp:.3g} |" if qh is not None else
          f"| {r.run_id[:16]}… | {r.model} | {n} | {acc:.3f} | {p:.3g} | [{lo:.3f},{hi:.3f}] | "
          f"{f(r.mae):.3f} | ~{mae0_ref:.2f} | — | — |")

    # 汇总显著性
    if stats_rows:
        ens = [x for x in stats_rows if x[1] == "ensemble"]
        sig = [x for x in stats_rows if x[4] < 0.05 and x[3] > 0.5]
        w()
        w(f"- 全部 {len(stats_rows)} 行中，dir_acc 显著 >50%（binom p<0.05）的行数：{len(sig)}")
        w(f"- ensemble 行 {len(ens)} 条："
          + "; ".join(f"acc={x[3]:.3f}(n={x[2]}, p={x[4]:.3g})" for x in ens) if ens else "- ensemble 行：无")
        # 红旗
        for x in stats_rows:
            if x[3] > 0.65:
                add("P1", f"{x[1]} dir_acc={x[3]:.3f} > 0.65 红旗（run {x[0]}）",
                    f"n={x[2]}。若非小样本波动（Wilson 下界 {x[5]:.3f}），需排查泄漏。"
                    f"LSTM 尤其注意 M4.1 已知问题。")
        for x in stats_rows:
            if x[8] is not None and (x[8] < 0.70 or x[8] > 0.98):
                lvl = "P1" if (x[8] < 0.70 or x[8] > 0.98) else "OK"
                add(lvl, f"{x[1]} 分位覆盖 {x[8]:.2f} 偏离 90% 目标（run {x[0]}）",
                    f"binom p(0.9)={x[9]:.3g}。覆盖 <70% 说明区间过窄（过自信），>98% 说明区间过宽。")
        # MAE 对比
        if sym_universe:
            mae0_mean = bu.mae0.mean()
            for x in stats_rows:
                if x[7] is not None and x[7] < mae0_mean * 0.4 and x[2] >= 60:
                    add("P1", f"{x[1]} MAE={x[7]:.3f} 仅为零预测基线（{mae0_mean:.3f}）的 40% 以下（run {x[0]}）",
                        "幅度预测好到不真实的程度，优先排查前视/泄漏（尤其 LSTM/ML 类）。")

    # F. 分层与漂移
    w()
    w("### F. by_state 分层与 recent60 漂移")
    state_acc: dict[str, list[float]] = {}
    drift_rows = []
    for r in all_rows:
        bs = r.by_state or {}
        for st, v in (bs.get("states") or {}).items():
            state_acc.setdefault(st, []).append(v.get("dir_acc"))
        r60, full = bs.get("recent60_dir_acc"), f(r.dir_acc)
        if r60 is not None and full is not None and r.sample_n:
            drift_rows.append((r.run_id, r.model, full, r60, abs(r60 - full)))
    for st, vals in state_acc.items():
        w(f"- 状态 {st}：{len(vals)} 行，dir_acc 均值 {np.mean(vals):.3f}，范围 [{min(vals):.3f}, {max(vals):.3f}]")
    if drift_rows:
        big = [d for d in drift_rows if d[4] > 0.08]
        w(f"- recent60 vs 全样本偏差 >8pp 的行：{len(big)}/{len(drift_rows)}")
        for d in big[:6]:
            w(f"  - {d[1]}（{d[0][:16]}…）：全样本 {d[2]:.3f} vs 近60 {d[3]:.3f}")
    w()

    # ---------- H. LSTM 泄漏 ----------
    w("## H. LSTM 泄漏评估（M4.1 已知问题）")
    try:
        import os
        lstm_dir = Path("/app/runtime/lstm")
        files = sorted(lstm_dir.glob("*")) if lstm_dir.exists() else []
        w(f"- runtime/lstm 权重文件数：{len(files)}")
        mtimes = []
        for fp in files[:5]:
            mt = datetime.fromtimestamp(fp.stat().st_mtime, TZ)
            mtimes.append(mt)
            w(f"  - {fp.name} mtime={mt:%Y-%m-%d %H:%M}")
        lstm_rows = [x for x in stats_rows if x[1] == "lstm"]
        nonlstm = [x for x in stats_rows if x[1] not in ("lstm", "ensemble")]
        if lstm_rows and nonlstm:
            lm = np.mean([x[3] for x in lstm_rows])
            nm = np.mean([x[3] for x in nonlstm])
            w(f"- LSTM 平均 dir_acc {lm:.3f} vs 其他模型均值 {nm:.3f}（差 {lm-nm:+.3f}）")
        add("P1", "LSTM 回测存在训练前视（M4.1，README 已承认）",
            "历史评估点的 LSTM 输出来自『训练至当前时刻』的权重，包含未来信息 → 回测 dir_acc 系统性偏乐观。"
            "缓解方式：按评估日动态加载历史权重快照（若 runtime 权重文件均晚于回测时间，说明历史快照缺失，"
            "回测实际仍在用『训练至现在』的同一权重 → 泄漏未真正消除）。"
            "修复后应重跑 M4 并观察 LSTM dir_acc 是否明显回落。")
    except Exception as e:
        w(f"- 检查失败：{e}")
    w()

    # ---------- I. 权重表核验 ----------
    w("## I. model_weights 核验")
    with session_scope() as s:
        mw = s.execute(select(ModelWeight)).scalars().all()
        w(f"- model_weights 行数：{len(mw)}")
        w("| model | weight | dir_acc | sample_n | source | 复算 w | 一致 |")
        w("|---|---|---|---|---|---|---|")
        for r in mw:
            acc = f(r.dir_acc)
            exp_w = min(2.0, max(0.1, (acc - 0.5) * 4)) if acc is not None else None
            ok = "✓" if exp_w is not None and abs(exp_w - float(r.weight)) < 0.01 else "✗"
            w(f"| {r.model} | {float(r.weight):.3f} | {acc:.3f} | {r.sample_n} | {r.source} | "
              f"{exp_w:.3f} | {ok} |" if exp_w is not None else
              f"| {r.model} | {float(r.weight):.3f} | — | {r.sample_n} | {r.source} | — | — |")
        # live vs config
        db_w = {r.model: float(r.weight) for r in mw}
        if db_w:
            cfg_w = dict(cfg.weights)
            diff = {k: (cfg_w.get(k), db_w[k]) for k in db_w
                    if k in cfg_w and abs(cfg_w[k] - db_w[k]) > 0.05}
            w(f"- DB 权重 vs config 权重差异 >0.05 的模型：{diff if diff else '无'}")
            add("P2", "回测 ensemble 用 config 固定权重，线上用 DB 权重 → 回测≠线上",
                f"backtest engine 传 cfg.weights，而 predict_symbol 加载 model_weights 表（fallback config）。"
                f"当前 DB 权重差异模型：{diff if diff else '无（尚未显著偏离，但机制性不一致存在）'}。"
                f"回测 ensemble 指标不代表线上 ensemble 行为，需统一权重来源。")
    w()

    # ---------- 结论 ----------
    w("## 审计结论")
    order = {"P0": 0, "P1": 1, "P2": 2, "OK": 3}
    for lv, t, d in sorted(findings, key=lambda x: order.get(x[0], 9)):
        icon = {"P0": "🔴", "P1": "🟠", "P2": "🟡", "OK": "🟢"}.get(lv, "•")
        w(f"### {icon} [{lv}] {t}")
        if d:
            w(f"{d}")
        w()

    report = "\n".join(R)
    Path(REPORT_PATH).write_text(report, encoding="utf-8")

    # 控制台摘要
    print("=" * 60)
    print(f"M4 审计完成 → {REPORT_PATH}")
    print("=" * 60)
    for lv, t, d in sorted(findings, key=lambda x: order.get(x[0], 9)):
        if lv == "OK":
            continue
        print(f"[{lv}] {t}")
    print("-" * 60)
    print(f"发现总数：P0={sum(1 for x in findings if x[0]=='P0')} "
          f"P1={sum(1 for x in findings if x[0]=='P1')} "
          f"P2={sum(1 for x in findings if x[0]=='P2')}")


if __name__ == "__main__":
    main()
