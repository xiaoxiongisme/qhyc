# -*- coding: utf-8 -*-
"""全市场日线参数扫描入口（可复现）。

网格：日线广度 × 焦点超参 = 162 组合 × 全市场品种。
重点维度：
  - allow_short [False, True]   -> 直接支撑「选择性空头」结论
  - support_z_thr / stop / target / s3_p_thr -> 稳健参数区间
输出：/app/qh/level_touch_sweep_out/{sweep_results.csv, sweep_summary.md}

在 qhyc-api 容器内运行：
  docker exec -it qhyc-api env PYTHONPATH=/app \\
      python app/backtest/level_touch/run_full_sweep.py
"""
from __future__ import annotations

import time

from .analyze import list_products
from .config import DEFAULT_CONFIG
from .sweep import sweep

GRID = {
    "freq": ["daily"],
    "allow_short": [False, True],
    "support_z_thr": [1.5, 2.0, 2.5],
    "stop_atr_mult": [1.5, 2.0, 3.0],
    "target_atr_mult": [2.0, 3.0, 4.0],
    "s3_no_return_p_thr": [0.5, 0.6, 0.7],
}

OUT = "/app/qh/level_touch_sweep_out"


def main() -> None:
    t0 = time.time()
    products = list_products(DEFAULT_CONFIG)
    import itertools
    n_combo = 1
    for v in GRID.values():
        n_combo *= len(v)
    print(f"[run_full_sweep] n_products={len(products)} "
          f"combos/variety={n_combo} total={len(products)*n_combo}")
    df = sweep(products, DEFAULT_CONFIG, GRID, OUT)
    print(f"[run_full_sweep] done in {time.time()-t0:.0f}s rows={len(df)} "
          f"-> {OUT}/sweep_results.csv")


if __name__ == "__main__":
    main()
