# -*- coding: utf-8 -*-
"""多段独立验证：候选品种在四段互不重叠区间上的稳定性。

为什么必须做这一步
----------------
daily_pick.py 的入选池是用 IS=2021-2023 / OOS=2024-2026 两段筛出来的。
若再用这两段自证，属循环论证。故取四段**互不重叠**的独立区间：

    A 2016-2018 | B 2019-2021 | C 2022-2024 | D 2025-2026(截至10-30)

候选在四段中命中段数越多，越可能是品种自身属性而非参数/区间偶然。

同时给出「候选池合成口径」：按笔加权净R（等价于每笔等风险的组合口径）。
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

from app.core.config import get_settings
from app.core.db import session_scope
from app.backtest.fusion_backtest import (
    FusionBacktestParams, _generate_states, _simulate_trades,
    fusion_params_from_config,
)

try:
    from scripts.validate.daily_scale_exp import _read_daily, _sym_metrics
except ImportError:  # 直跑时的路径兜底
    sys.path.insert(0, "/app")
    from scripts.validate.daily_scale_exp import _read_daily, _sym_metrics

from app.backtest.fusion_backtest import _read_hourly

SEGMENTS = [
    ("A 2016-2018", date(2016, 1, 1), date(2018, 12, 31)),
    ("B 2019-2021", date(2019, 1, 1), date(2021, 12, 31)),
    ("C 2022-2024", date(2022, 1, 1), date(2024, 12, 31)),
    ("D 2025-2026", date(2025, 1, 1), date(2026, 10, 30)),
]
PREHEAT_START = date(2015, 1, 1)


def run_seg(session, symbols, ema_k, lo, hi, base, tf):
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cands", nargs="+",
                    default=["EB888", "CU888", "CF888", "SC888", "A888", "SM888"])
    ap.add_argument("--tf", default="daily,hourly")
    ap.add_argument("--ema", type=int, default=250)
    ap.add_argument("--hourly-ema", type=int, default=140)
    ap.add_argument("--o", default="/tmp/multi_seg.json")
    args = ap.parse_args()

    base = fusion_params_from_config()
    tfs = [x.strip() for x in args.tf.split(",") if x.strip()]
    plans = [(tf, args.ema if tf == "daily" else args.hourly_ema) for tf in tfs]

    print("=== 多段独立验证（四段互不重叠）===")
    print("候选:", args.cands)
    print()

    result = {}
    with session_scope() as sess:
        for tf, k in plans:
            print(f"########## {tf.upper()} EMA{k} ##########")
            labels = [s[0] for s in SEGMENTS]
            print(f"{'品种':<9}" + "".join(f"{l.split()[0]:>9}" for l in labels)
                  + f"{'命中':>7}   {'四段合成净R':>12}")
            pool_hits = {sym: 0 for sym in args.cands}
            pool_all = []
            per_seg_raw = {}
            for name, lo, hi in SEGMENTS:
                per_seg_raw[name] = run_seg(sess, args.cands, k, lo, hi, base, tf)
            for sym in args.cands:
                cells, pos = [], 0
                for name, lo, hi in SEGMENTS:
                    m = per_seg_raw[name].get(sym)
                    if not m or m.get("n", 0) < 10:
                        cells.append("     n/a")
                        continue
                    v = m["net_R"]
                    cells.append(f"{v:>+9.3f}")
                    if v > 0:
                        pos += 1
                    pool_all.append((sym, m["n"], v))
                pool_hits[sym] = pos
                print(f"{sym:<9}" + "".join(cells) + f"{pos}/4".rjust(7))
            # 候选池合成（按笔加权）
            tot_n = float(sum(n for _, n, _ in pool_all))
            wavg = float(sum(n * v for _, n, v in pool_all) / tot_n) if tot_n else 0.0
            pos_n = float(sum(n for _, n, v in pool_all if v > 0))
            print(f"{'[候选池合成]':<9}" + " " * (9 * len(labels)) + f"{'':>7}   "
                  f"笔数={int(tot_n):>5} 加权净R={wavg:+.4f} "
                  f"正号笔占比={pos_n / tot_n * 100 if tot_n else 0:.1f}%")
            result[f"{tf}_{k}"] = {
                "per_symbol_per_seg": per_seg_raw,
                "pool_hits": pool_hits,
                "pool_weighted_netR": round(wavg, 4),
                "pool_total_trades": int(tot_n),
            }
            print()

    with io.open(args.o, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print(f"结果已写: {args.o}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
