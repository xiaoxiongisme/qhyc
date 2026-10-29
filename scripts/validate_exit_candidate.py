#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离场规则候选验证（2026-10-29）：候选 R3 = SL1.0 / TRAIL6.0 / BE0.5。

背景
----
《实盘入场规则模拟报告_20261029.html》结论：当前生产规格（分析口径 SL1.0/TR2.0/BE0.5）
相对用户实际执行系统性劣化（削掉约 78% 收益），根因是 2×ATR 吊灯过紧、过早兑现浮盈。
候选 R3（SL1.0/TR6.0/BE0.5）在 500 次随机留出中 86~90% 优于实际执行。

但建议明确：候选「不能直接视为无条件生产规则，须经模拟盘/持续留出验证」。
本脚本即为该验证门控，做两件事：
  1) walk-forward OOS：在 V2 全品种 × 历史区间上滚动留出，比较
     基线（生产默认 FusionConfig：SL2.0/TR2.0/BE0.5）与候选（SL1.0/TR6.0/BE0.5）的
     IS/OOS 净利、衰减比 decay=OOS/IS，给出判据。
  2) 用户入场点复算：若可定位到此前分析产物 live_final.json，直接报告候选(R3) 与
     基线(R1) 在用户真实入场点上的单笔均收益/胜率/金额（与留出结论交叉印证）。

晋升判据（全部满足才建议把默认从 SL2.0/TR2.0 切换为候选 SL1.0/TR6.0）：
  A. 候选 OOS 净利 > 0；
  B. 候选 decay ≥ 50%（OOS 未深度衰减，沿用 walkforward 既定阈值）；
  C. 候选 OOS 净利 > 基线 OOS 净利；
  D. 用户入场点样本上候选优于基线（与已有留出结论一致）。

部署方式（不改默认，仅影子/模拟盘启用）：
  在单独进程/容器设环境变量后启动实时引擎或回测：
    QH_FUSION_PROFILE=candidate            # 套用 FUSION_EXIT_CANDIDATE
    # 或细粒度：QH_FUSION_SL_ATR=1.0 QH_FUSION_TRAIL_ATR=6.0 QH_FUSION_BE_R=0.5

用法（在能连 DB 的环境，如 qhyc Docker 容器或本地带 PG 的 venv）：
  python scripts/validate_exit_candidate.py
  python scripts/validate_exit_candidate.py --no-wf          # 仅用户入场点复算
  python scripts/validate_exit_candidate.py --start 2020-01-01 --end 2026-10-01
  python scripts/validate_exit_candidate.py --max-symbols 10 # 快速抽样
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

# 让脚本在仓库内直接运行（python scripts/validate_exit_candidate.py）
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings, FUSION_EXIT_CANDIDATE
from app.backtest.fusion_backtest import fusion_params_from_config
from app.backtest.walkforward import WalkForwardParams, run_walk_forward

# 基线 = 生产默认（不传 override）；候选 = FUSION_EXIT_CANDIDATE
BASELINE_OVERRIDE: dict = {}
CANDIDATE_OVERRIDE = dict(FUSION_EXIT_CANDIDATE)


def _session():
    url = get_settings().db_url
    engine = create_engine(url, pool_size=4, max_overflow=8)
    return sessionmaker(bind=engine)()


def _wf_one(session, override: dict, wf: WalkForwardParams,
            symbols, start, end) -> dict:
    base = fusion_params_from_config(overrides=override)
    return run_walk_forward(session, symbols=symbols, base=base,
                            params=wf, start=start, end=end, verbose=True)


def run_walkforward_check(args) -> dict:
    print("\n=== 1) walk-forward OOS（基线 vs 候选）===")
    wf = WalkForwardParams(
        train_days=720, test_days=60, step_days=60,
        warmup_days=150, reestimate=False, min_oos_trades=5, decay_pass=0.50,
    )
    settings = get_settings()
    symbols = [s.symbol for s in settings.main_contracts] or None
    if args.max_symbols and symbols:
        symbols = symbols[: args.max_symbols]
    print(f"品种数: {len(symbols) if symbols else '全部(main_contracts 为空)'}")
    print(f"候选覆盖: SL={CANDIDATE_OVERRIDE['sl_atr']} "
          f"TRAIL={CANDIDATE_OVERRIDE['trail_atr']} BE={CANDIDATE_OVERRIDE['be_r']}")

    with _session() as session:
        base_res = _wf_one(session, BASELINE_OVERRIDE, wf, symbols, args.start, args.end)
        cand_res = _wf_one(session, CANDIDATE_OVERRIDE, wf, symbols, args.start, args.end)

    def _row(tag, r):
        print(f"  [{tag}] n窗={r.get('n_windows')} OOS笔={r.get('n_oos_trades')} "
              f"IS净={r.get('is_net'):.4f} OOS净={r.get('oos_net'):.4f} "
              f"decay={r.get('decay')} 判据={r.get('verdict')}")
        return r

    _row("基线 SL2.0/TR2.0", base_res)
    _row("候选 SL1.0/TR6.0", cand_res)

    a = float(cand_res.get("oos_net", 0.0)) > 0
    decay = cand_res.get("decay")
    b = (decay is not None) and (decay >= wf.decay_pass)
    c = float(cand_res.get("oos_net", 0.0)) > float(base_res.get("oos_net", 0.0))
    verdict = {
        "A_oos_positive": a, "B_decay_ok": b, "C_better_than_baseline": c,
        "baseline_oos": base_res.get("oos_net"), "candidate_oos": cand_res.get("oos_net"),
        "candidate_decay": decay,
        "baseline_verdict": base_res.get("verdict"),
        "candidate_verdict": cand_res.get("verdict"),
    }
    return verdict


def _find_live_final() -> Path | None:
    # 优先：环境变量显式指定
    env_path = os.environ.get("LIVE_FINAL_JSON", "").strip()
    if env_path and Path(env_path).exists():
        return Path(env_path)
    # 常见位置：分析工作区、/tmp、仓库内 analysis
    roots = [
        Path(r"E:/抖音分析/2026-10-27-19-13-25/analysis"),
        Path("/tmp"),
        REPO_ROOT / "analysis",
    ]
    for root in roots:
        p = root / "live_final.json"
        if p.exists():
            return p
    return None


def run_live_check() -> dict:
    print("\n=== 2) 用户入场点复算（R3 候选 vs R1 基线）===")
    p = _find_live_final()
    if not p:
        print("  [跳过] 未找到 live_final.json（此前分析产物）。"
              "请先重跑 analysis/live_stage_*.py 生成，或设 LIVE_FINAL_JSON 指向它。")
        return {"available": False}
    data = json.loads(p.read_text(encoding="utf-8"))
    summary = data.get("summary", {})
    for tag in ("实际", "R1_V2原规格", "R3_紧止损宽吊灯"):
        v = summary.get(tag)
        if not v:
            continue
        print(f"  [{tag}] n={v.get('n')} 均收益%={v.get('mean'):+.4f} "
              f"胜率={v.get('wr'):.1f}% 金额={v.get('cny'):+.0f}元 "
              f"持bar中位={v.get('hold_med')}")
    r1 = summary.get("R1_V2原规格", {})
    r3 = summary.get("R3_紧止损宽吊灯", {})
    if r1 and r3:
        d = float(r3.get("mean", 0.0)) - float(r1.get("mean", 0.0))
        d_cny = float(r3.get("cny", 0.0)) - float(r1.get("cny", 0.0))
        print(f"  → R3 相对 R1：单笔均收益差 {d:+.4f}%  金额差 {d_cny:+.0f}元")
        return {"available": True, "delta_mean": d, "delta_cny": d_cny,
                "D_better": d > 0}
    return {"available": True, "D_better": None}


def main():
    ap = argparse.ArgumentParser(description="离场规则候选验证门控")
    ap.add_argument("--no-wf", action="store_true", help="跳过 walk-forward（仅用户入场点复算）")
    ap.add_argument("--start", default=None, help="walk-forward 起始日期 YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="walk-forward 结束日期 YYYY-MM-DD")
    ap.add_argument("--max-symbols", type=int, default=0, help="限制品种数（快速抽样）")
    args = ap.parse_args()

    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None

    wf_verdict = None
    if not args.no_wf:
        wf_verdict = run_walkforward_check(args)

    live = run_live_check()

    print("\n=== 晋升判据汇总 ===")
    checks = []
    if wf_verdict:
        checks += [
            ("A 候选OOS净>0", wf_verdict.get("A_oos_positive")),
            ("B 候选decay≥50%", wf_verdict.get("B_decay_ok")),
            ("C 候选OOS净>基线", wf_verdict.get("C_better_than_baseline")),
        ]
    if live.get("available") and live.get("D_better") is not None:
        checks.append(("D 用户入场点候选>基线", live.get("D_better")))

    all_pass = True
    for name, ok in checks:
        mark = "✅" if ok else "❌"
        if ok is False:
            all_pass = False
        print(f"  {mark} {name}")
    if not checks:
        print("  （无可用判据，请先生成数据后重跑）")
        all_pass = False

    print("\n=== 结论 ===")
    if all_pass:
        print("✅ 候选通过全部判据：建议把生产默认从 SL2.0/TR2.0/BE0.5 切换为 "
              "SL1.0/TR6.0/BE0.5（去掉 env 覆盖即生效）。")
    else:
        print("⚠ 候选尚未通过全部判据：维持生产默认 SL2.0/TR2.0/BE0.5 不变，"
              "继续以 QH_FUSION_PROFILE=candidate 做影子/模拟盘观察。")
        print("  （本结论仅基于本次样本；务必持续留出、跨多段行情复核后再定。）")


if __name__ == "__main__":
    main()
