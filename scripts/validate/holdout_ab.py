# -*- coding: utf-8 -*-
"""决定性对照：在候选池**从未参与筛选**的独立区间上，比较"候选池 vs 其余品种"。

为什么这是决定性的
----------------
daily_pick.py 用 IS=2021-2023 / OOS=2024-2026 选出 6 个候选。要让结论可信，
必须在一个"选之前就已存在、且没被用来选"的区间上验证。
这里取 A 段 2016-2018（日线有数据的最早完整段）作为纯样本外。

同时给 C(2022-2024) / D(2025-2026) 作为补充（这两段样本也充足）。

产出：候选池 vs 其余品种 的加权净R、PF、t —— 差异若稳定，则品种减法有效。
"""
from __future__ import annotations

import argparse
import dataclasses
import io
import json
import sys
from datetime import date

import numpy as np
import pandas as pd

from app.backtest.fusion_backtest import (
    FusionBacktestParams, _generate_states, _simulate_trades,
    fusion_params_from_config,
)
from app.core.config import get_settings
from app.core.db import session_scope

sys.path.insert(0, "/app")
from scripts.validate.daily_scale_exp import _read_daily, _sym_metrics  # noqa: E402

SEGMENTS = [
    ("A 2016-2018", date(2016, 1, 1), date(2018, 12, 31)),
    ("C 2022-2024", date(2022, 1, 1), date(2024, 12, 31)),
    ("D 2025-2026", date(2025, 1, 1), date(2026, 10, 30)),
]
PREHEAT_START = date(2015, 1, 1)


def run_seg(session, symbols, ema_k, lo, hi, base, tf="daily"):
    from app.backtest.fusion_backtest import _read_hourly
    p = dataclasses.replace(
        base, ema_k=ema_k,
        max_bars=4000 if tf == "daily" else base.max_bars,
    )
    out = {}
    for sym in symbols:
        df = (_read_daily(session, sym, p.src, PREHEAT_START, hi) if tf == "daily"
              else _read_hourly(session, sym, p.src, PREHEAT_START, hi))
        if df is None or len(df) < p.min_bars:
            continue
        try:
            st = _generate_states(df, p)
            tr = _simulate_trades(st, p, symbol=sym)
        except Exception:
            continue
        a = pd.Timestamp(lo).tz_localize("Asia/Shanghai")
        b = pd.Timestamp(hi).tz_localize("Asia/Shanghai")
        keep = [t for t in tr if t.get("entry_dt") is not None
                and a <= pd.Timestamp(t["entry_dt"]) <= b]
        if keep:
            out[sym] = _sym_metrics(keep)
    return out


def pool_metrics(raw: dict, syms: set) -> dict:
    rows = [(s, m) for s, m in raw.items() if s in syms]
    if not rows:
        return {}
    tot_n = float(sum(m.get("n", 0) for _, m in rows))
    # 按笔加权合成 net_R（等价于每笔等风险的组合口径）
    w = float(sum(m["n"] * m["net_R"] for _, m in rows) / tot_n) if tot_n else 0.0
    pf = float(np.mean([m.get("pf", 0) or 0 for _, m in rows]))
    pos = sum(1 for _, m in rows if m.get("net_R", 0) > 0)
    return {"n_sym": len(rows), "n_trades": int(tot_n),
            "w_netR": round(w, 4), "avg_pf": round(pf, 2),
            "pos_ratio": f"{pos}/{len(rows)}"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cands", nargs="+",
                    default=["EB888", "CU888", "CF888", "SC888", "A888", "SM888"])
    ap.add_argument("--tf", default="daily")
    ap.add_argument("--ema", type=int, default=250)
    ap.add_argument("--o", default="/tmp/holdout_ab.json")
    args = ap.parse_args()

    st = get_settings()
    all_syms = [s.symbol for s in st.main_contracts]
    cands = set(args.cands)
    others = [s for s in all_syms if s not in cands]

    base = fusion_params_from_config()
    print(f"=== 独立区间验证：候选池 vs 其余品种（{args.tf} EMA{args.ema}）===")
    print(f"候选({len(cands)}): {sorted(cands)}")
    print(f"其余({len(others)} 个)")
    print()
    result = {}
    with session_scope() as sess:
        for name, lo, hi in SEGMENTS:
            raw = run_seg(sess, all_syms, args.ema, lo, hi, base, tf=args.tf)
            cp = pool_metrics(raw, cands)
            op = pool_metrics(raw, set(others))
            if not cp or not op:
                print(f"--- {name}（可用品种 {len(raw)}）：数据不足，跳过 ---")
                print()
                continue
            print(f"--- {name}（可用品种 {len(raw)}）---")
            print(f"  候选池: 品种={cp.get('n_sym')} 笔数={cp.get('n_trades'):>5} "
                  f"加权净R={cp.get('w_netR'):>+8.4f} 平均PF={cp.get('avg_pf')} "
                  f"正号品种={cp.get('pos_ratio')}")
            print(f"  其余种: 品种={op.get('n_sym')} 笔数={op.get('n_trades'):>5} "
                  f"加权净R={op.get('w_netR'):>+8.4f} 平均PF={op.get('avg_pf')} "
                  f"正号品种={op.get('pos_ratio')}")
            d = cp.get("w_netR", 0) - op.get("w_netR", 0)
            print(f"  >>> 差异 (候选 - 其余) = {d:+.4f}")
            print()
            result[name] = {"cand": cp, "other": op, "diff": round(d, 4)}

    with io.open(args.o, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print(f"结果已写: {args.o}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
