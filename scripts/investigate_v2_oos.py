#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""V2 样本外崩塌排查（2026-11-01）。

问题
----
生产 walk-forward 基线（FusionConfig 默认 SL2.0/TR2.0/BE0.5）在 28 窗 × 50 品种上出现
OOS 净 = −330,406（点）、decay = OOS/IS = −10.1 的深度衰减，IS 净却为 +32,709。
即「样本内盈利、样本外崩塌」。需要定位：**是设定 / 数据问题，还是策略真失效？**

诊断模式
--------
  (默认) core ：单遍基线 WF → 逐窗 + 逐品种分解（看形态：崩塌集中还是系统）。
  --cmp src   ：同源但 src=bar_60m vs src=hourly_bar(akshare) 对比整体 OOS 净 / decay
                → 若崩塌只在 bar_60m 出现，则属「数据源设定问题」。
  --cmp reest ：reestimate=False vs True 对比整体 OOS 净 / decay
                → 若逐窗重估可挽救 OOS，则属「参数衰减」；若仍崩，则边本身不存在。

成本说明（重要）
--------------
真实费率在 IS / OOS **同口径**施加（`_simulate_trades` 按建仓日取历史费率），不可能造成
IS/OOS 不对称，故**不单列成本对照**——成本不是本崩塌的候选成因。

输出
----
  --out <json> ：结构化结果（供报告直接读取）。
  文本：逐窗表、逐品种表、对照表。
  全部复用 run_walk_forward / fusion_params_from_config，不重复造回测逻辑。

判读框架（设定 vs 真失效）
--------------------------
  · 若 --cmp src 中 bar_60m 崩、akshare 不崩 → 数据源人为衰减（设定问题），优先修数据。
  · 若 --cmp reest 中重估可救回 OOS → 参数衰减（设定问题），需改为逐窗重估或重标定。
  · 若两者都不救、且逐品种 / 逐窗广泛为负 → 真失效（策略边不存在或被结构性破坏）。
  · 逐品种高度集中（少数品种贡献大部分 OOS 亏损）→ 指向这些品种的数据 / 合约映射问题。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.backtest.fusion_backtest import fusion_params_from_config
from app.backtest.walkforward import WalkForwardParams, run_walk_forward

# 与 validate_exit_candidate.py 完全一致的窗口设定
WF = WalkForwardParams(
    train_days=720, test_days=60, step_days=60, warmup_days=150,
    reestimate=False, min_oos_trades=5, decay_pass=0.50,
)


def _session():
    url = get_settings().db_url
    engine = create_engine(url, pool_size=4, max_overflow=8)
    return sessionmaker(bind=engine)()


def _symbols(settings, max_symbols: int):
    syms = [s.symbol for s in settings.main_contracts]
    if max_symbols and syms:
        syms = syms[:max_symbols]
    return syms


def _data_range_for_src(session, symbols, src: str):
    """按数据源取公共可用区间（修正 walkforward._data_range 只查 HourlyBar 的缺陷）。

    - src=bar_60m → l1_mkt.bar_60m（独立表，bucket 字段）
    - 其余 → hourly_bar WHERE src=:src
    """
    if src == "bar_60m":
        row = session.execute(
            text("SELECT min(bucket), max(bucket) FROM l1_mkt.bar_60m "
                 "WHERE symbol=ANY(:syms)"),
            {"syms": symbols},
        ).first()
    else:
        row = session.execute(
            text("SELECT min(trade_datetime), max(trade_datetime) FROM hourly_bar "
                 "WHERE src=:s AND symbol=ANY(:syms)"),
            {"s": src, "syms": symbols},
        ).first()
    if not row or row[0] is None:
        return None, None
    return row[0].date(), row[1].date()


def _perse_symbol_from_oos(oos_trades: list[dict]) -> list[dict]:
    """从 oos_trades 聚合逐品种 OOS（pnl 为点单位）。IS 为整体口径，不拆分到品种。"""
    agg = defaultdict(lambda: {"n": 0, "oos_net": 0.0, "wins": 0})
    for t in oos_trades:
        sym = t.get("symbol")
        if not sym:
            continue
        a = agg[sym]
        a["n"] += 1
        a["oos_net"] += float(t.get("pnl", 0.0))
        if float(t.get("pnl", 0.0)) > 0:
            a["wins"] += 1
    rows = []
    for sym, a in agg.items():
        rows.append({
            "symbol": sym, "n_oos": a["n"],
            "oos_net": round(a["oos_net"], 2),
            "win_rate": round(a["wins"] / a["n"], 4) if a["n"] else 0.0,
        })
    rows.sort(key=lambda r: r["oos_net"])
    return rows


def _core(session, symbols, start, end, max_symbols, src="bar_60m") -> dict:
    print(f"\n=== [core] 基线 WF 逐窗 + 逐品种分解 (src={src}) ===")
    rng = _data_range_for_src(session, symbols, src)
    if rng[0] is None:
        print(f"  [跳过] src={src} 无数据区间")
        return {"mode": "core", "src": src, "error": "no_data_range"}
    base = fusion_params_from_config(overrides={"src": src})
    res = run_walk_forward(session, symbols=symbols, base=base,
                           params=WF, start=rng[0], end=rng[1], verbose=False)
    windows = res.get("windows", [])
    oos_trades = res.get("oos_trades", [])
    per_sym = _perse_symbol_from_oos(oos_trades)

    n_win = len(windows)
    n_win_neg = sum(1 for w in windows if w.get("oos_net", 0) < 0)
    total_oos = float(res.get("oos_net", 0.0))
    # 集中度：最亏的 5 个品种占整体 OOS 亏损的比例
    worst5 = per_sym[:5]
    worst5_share = (sum(r["oos_net"] for r in worst5) / total_oos
                    if total_oos else 0.0)
    n_sym_neg = sum(1 for r in per_sym if r["oos_net"] < 0)

    print(f"  品种数={len(symbols)} 有效窗={n_win} OOS<0 窗={n_win_neg} "
          f"({n_win_neg/max(n_win,1):.0%})")
    print(f"  OOS 净(总)={total_oos:.1f}  IS 净={res.get('is_net'):.1f}  "
          f"decay={res.get('decay')}")
    print(f"  逐品种：共 {len(per_sym)} 个有 OOS 成交，其中 OOS<0 的品种 = {n_sym_neg}")
    print(f"  最亏 5 品种 OOS 占比 = {worst5_share:.1%}")
    print("\n  --- 逐窗（test 区间 / n_oos / is_net / oos_net / decay）---")
    for w in windows:
        d = w.get("decay")
        print(f"    {w['test'][0]}~{w['test'][1]}  n={w.get('n_oos'):>4}  "
              f"IS={w.get('is_net'):>+10.1f}  OOS={w.get('oos_net'):>+10.1f}  "
              f"decay={d if d is not None else 'NA'}")
    print("\n  --- 逐品种 OOS 净（最亏在前，截断 15）---")
    for r in per_sym[:15]:
        print(f"    {r['symbol']:<10} n={r['n_oos']:>4}  "
              f"OOS净={r['oos_net']:>+10.1f}  胜率={r['win_rate']:.1%}")
    return {
        "mode": "core", "symbols": len(symbols), "windows": windows,
        "n_windows": n_win, "n_windows_oos_neg": n_win_neg,
        "is_net": res.get("is_net"), "oos_net": res.get("oos_net"),
        "decay": res.get("decay"), "n_oos_trades": res.get("n_oos_trades"),
        "per_symbol": per_sym, "worst5_share": round(worst5_share, 4),
        "n_symbols_oos_neg": n_sym_neg,
    }


def _run_wf(session, symbols, override: dict, wf: WalkForwardParams,
            start, end) -> dict:
    base = fusion_params_from_config(overrides=override)
    return run_walk_forward(session, symbols=symbols, base=base,
                            params=wf, start=start, end=end, verbose=False)


def _cmp_src(session, symbols, start, end, max_symbols) -> dict:
    print("\n=== [cmp src] bar_60m vs akshare(HourlyBar) ===")
    rng60 = _data_range_for_src(session, symbols, "bar_60m")
    rng_ak = _data_range_for_src(session, symbols, "akshare")
    r60 = (_run_wf(session, symbols, {"src": "bar_60m"}, WF, rng60[0], rng60[1])
           if rng60[0] else None)
    rak = (_run_wf(session, symbols, {"src": "akshare"}, WF, rng_ak[0], rng_ak[1])
           if rng_ak[0] else None)
    if r60 and rak:
        print(f"  bar_60m : OOS净={r60['oos_net']:>+12.1f}  IS净={r60['is_net']:>+10.1f}  "
              f"decay={r60['decay']}  窗={r60['n_windows']}  [{rng60[0]}~{rng60[1]}]")
        print(f"  akshare : OOS净={rak['oos_net']:>+12.1f}  IS净={rak['is_net']:>+10.1f}  "
              f"decay={rak['decay']}  窗={rak['n_windows']}  [{rng_ak[0]}~{rng_ak[1]}]")
    else:
        print(f"  bar_60m : {'OK' if r60 else '无数据'}")
        print(f"  akshare : {'OK' if rak else '无数据'}")
    return {
        "mode": "cmp_src", "symbols": len(symbols),
        "bar_60m": ({"oos_net": r60["oos_net"], "is_net": r60["is_net"],
                     "decay": r60["decay"], "n_windows": r60["n_windows"],
                     "range": [str(rng60[0]), str(rng60[1])]} if r60 else None),
        "akshare": ({"oos_net": rak["oos_net"], "is_net": rak["is_net"],
                     "decay": rak["decay"], "n_windows": rak["n_windows"],
                     "range": [str(rng_ak[0]), str(rng_ak[1])]} if rak else None),
    }


def _cmp_reest(session, symbols, start, end, max_symbols) -> dict:
    print("\n=== [cmp reest] reestimate=False vs True ===")
    wf_false = WF
    wf_true = WalkForwardParams(
        train_days=720, test_days=60, step_days=60, warmup_days=150,
        reestimate=True, reestimate_grid=[{}, {"sl_atr": 1.5}, {"sl_atr": 2.5}],
        min_oos_trades=5, decay_pass=0.50,
    )
    r_false = _run_wf(session, symbols, {}, wf_false, start, end)
    r_true = _run_wf(session, symbols, {}, wf_true, start, end)
    print(f"  reest=False: OOS净={r_false['oos_net']:>+12.1f}  IS净={r_false['is_net']:>+10.1f}  "
          f"decay={r_false['decay']}")
    print(f"  reest=True : OOS净={r_true['oos_net']:>+12.1f}  IS净={r_true['is_net']:>+10.1f}  "
          f"decay={r_true['decay']}")
    return {
        "mode": "cmp_reest", "symbols": len(symbols),
        "reest_false": {"oos_net": r_false["oos_net"], "is_net": r_false["is_net"],
                        "decay": r_false["decay"]},
        "reest_true": {"oos_net": r_true["oos_net"], "is_net": r_true["is_net"],
                       "decay": r_true["decay"]},
    }


def main():
    ap = argparse.ArgumentParser(description="V2 样本外崩塌排查")
    ap.add_argument("--cmp", choices=["src", "reest"], default=None,
                    help="对照模式：src=数据源敏感性；reest=参数重估敏感性")
    ap.add_argument("--start", default=None, help="起始 YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="结束 YYYY-MM-DD")
    ap.add_argument("--max-symbols", type=int, default=0,
                    help="限制品种数（对照模式默认 25，控时）")
    ap.add_argument("--src", default="bar_60m",
                    help="core 模式数据源：bar_60m / akshare（复现 validate 用 akshare）")
    ap.add_argument("--out", default=None, help="结果 JSON 输出路径")
    args = ap.parse_args()

    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None

    settings = get_settings()
    max_sym = args.max_symbols or (25 if args.cmp else 0)
    symbols = _symbols(settings, max_sym)

    with _session() as session:
        if args.cmp == "src":
            result = _cmp_src(session, symbols, start, end, max_sym)
        elif args.cmp == "reest":
            result = _cmp_reest(session, symbols, start, end, max_sym)
        else:
            result = _core(session, symbols, start, end, max_sym, src=args.src)

    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        print(f"\n[JSON] 已写入 {args.out}")
    print("\n=== 完成 ===")


if __name__ == "__main__":
    main()
