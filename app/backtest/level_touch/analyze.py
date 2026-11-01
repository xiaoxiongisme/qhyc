# -*- coding: utf-8 -*-
"""level_touch · 编排入口。

遍历品种，对每个品种用未复权日线主连跑「点位触达统计 + 成本感知回测」，
输出：
  * <out>/level_stats_<PROD>.csv      逐价位触达频次 / 期望 / z
  * <out>/backtest_summary.csv        各品种回测聚合指标
  * <out>/backtest_report.md          可读报告（含方法学口径与结论）
  * <out>/trades_<PROD>.csv           （可选）逐笔成交

在容器内运行（有 DB 与 app 包）：
  docker exec qhyc-api PYTHONPATH=/app python -m app.backtest.level_touch.analyze
"""
from __future__ import annotations

import os
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import text

from .backtest import run_backtest
from .config import DEFAULT_CONFIG, LevelTouchConfig, resolve_freq


def list_products(cfg: LevelTouchConfig) -> list:
    if cfg.products:
        return list(cfg.products)
    from app.core.db import get_engine
    with get_engine().connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT product FROM main_continuous "
                 "WHERE trade_date >= '2015-01-01' ORDER BY product")
        ).fetchall()
    return [r[0] for r in rows]


def run_one(variety: str, cfg: LevelTouchConfig, out_dir: str) -> dict:
    from app.data import barstore
    from app.data.barstore import variety_spec

    fr = resolve_freq(cfg)
    bars = barstore.load(variety, fr["bs_freq"], "continuous", cfg.start, cfg.end)
    if bars.empty or len(bars) < fr["warmup_bars"] + 5:
        return {"variety": variety, "error": "数据不足/为空", "n_trades": 0}

    try:
        spec = variety_spec(variety, as_of=date.today())
        tick = float(spec["tick_size"])
        multiplier = float(spec["multiplier"])
    except Exception as e:
        return {"variety": variety, "error": f"规格缺失: {e}", "n_trades": 0}

    res = run_backtest(bars, variety, cfg, tick, multiplier)

    # 落盘逐价位触达统计
    if res.get("level_stats") is not None:
        os.makedirs(out_dir, exist_ok=True)
        res["level_stats"].to_csv(
            os.path.join(out_dir, f"level_stats_{variety}.csv"), index=False)

    row = {
        "variety": variety,
        "n_trades": res.get("n_trades", 0),
        "win_rate": round(res.get("win_rate", 0.0), 4),
        "pf": (round(res.get("pf", 0.0), 3)
               if np.isfinite(res.get("pf", 0.0)) else None),
        "total_net_yuan": round(res.get("total_net_yuan", 0.0), 1),
        "max_dd": round(res.get("max_dd", 0.0), 4),
        "sharpe": round(res.get("sharpe", 0.0), 3),
        "avg_hold": round(res.get("avg_hold", 0.0), 1),
        "ath": round(res.get("summary", {}).get("ath_price", float("nan")), 1),
        "atl": round(res.get("summary", {}).get("atl_price", float("nan")), 1),
        "n_singletons": res.get("summary", {}).get("n_singletons", 0),
    }
    return row


def analyze(cfg: LevelTouchConfig | None = None,
            out_dir: str = "/app/qh/level_touch_out") -> pd.DataFrame:
    cfg = cfg or DEFAULT_CONFIG
    products = list_products(cfg)
    print(f"[level_touch] 待跑品种 {len(products)} 个")
    rows = []
    for i, v in enumerate(products, 1):
        try:
            r = run_one(v, cfg, out_dir)
        except Exception as e:  # noqa: BLE001  单品种失败不影响整体
            r = {"variety": v, "error": str(e)[:80], "n_trades": 0}
        rows.append(r)
        if i % 10 == 0 or "error" in r:
            print(f"  [{i}/{len(products)}] {v}: "
                  f"trades={r.get('n_trades',0)} net="
                  f"{r.get('total_net_yuan',0):.0f} {r.get('error','')}")

    df = pd.DataFrame(rows)
    os.makedirs(out_dir, exist_ok=True)
    df.to_csv(os.path.join(out_dir, "backtest_summary.csv"), index=False)
    _write_report(df, out_dir, cfg)
    print(f"[level_touch] 完成 → {out_dir}/backtest_summary.csv")
    return df


def _write_report(df: pd.DataFrame, out_dir: str, cfg: LevelTouchConfig) -> None:
    lines = ["# level_touch 点位触达回测报告", ""]
    lines.append(f"周期={cfg.freq}  空头={'开' if cfg.allow_short else '关'}  "
                 f"warmup={cfg.warmup_days}日, stop={cfg.stop_atr_mult}ATR, "
                 f"target={cfg.target_atr_mult}ATR, limit_excl={cfg.limit_excl}")
    lines.append("")
    lines.append("## 口径铁律")
    lines.append("- 未复权日线主连（绝不用后复权做绝对点位）")
    lines.append("- 次根开盘成交 + bar 内止损止盈（保守、无前视）")
    lines.append("- 成本走 dim_trading_cost（历史费率/乘数/tick/滑点）")
    lines.append("- |日收益|>10% 的换月/涨跌停 bar 剔除")
    lines.append("")
    ok = df[df.get("n_trades", 0) > 0] if "n_trades" in df else df
    lines.append(f"## 回测汇总（{len(df)} 品种，有成交 {len(ok)} 个）")
    if "n_trades" in df:
        lines.append(df.to_markdown(index=False) if hasattr(df, "to_markdown")
                     else df.to_string(index=False))
    with open(os.path.join(out_dir, "backtest_report.md"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    analyze()
