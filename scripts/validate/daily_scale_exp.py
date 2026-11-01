# -*- coding: utf-8 -*-
"""日线尺度实验：验证「周期错配」假设是否是当前融合策略亏损的成因。

假设来源
--------
Moskowitz / Ooi / Pedersen (2012, JFE) 对 58 个期货合约 1985-2009 的研究：
时间序列动量在「12 个月回看 + 持有 1 个月」上 Sharpe 1.28；1-3 个月回看是
**噪音最大**的窗口。而生产口径 ema_k=140 作用在小时线上：

    hourly_bar 一年 1675 根 -> 每日 6.7 根
    140 / 6.7 ≈ 21 个交易日 ≈ 1 个月   <-- 正好落在最差窗口

本脚本把同一套引擎切换到日线，扫描 EMA 60/120/180/250 四档，做 IS/OOS 对照。

设计要点
--------
1. **只换数据源，不碰引擎**：复用 app.backtest.fusion_backtest 的
   _generate_states / _simulate_trades，保证单一真源口径一致（含真乘数、
   dim_trading_cost 真实费率）。
2. **EMA 需充分预热**：不能只读目标区间，否则 EMA250 在段首无历史。
   故读 2015 起全历史生成信号，再按 entry_dt 过滤到统计窗口。
3. **同号才算数**：IS / OOS 两段均为正才算通过（纪律：扫 N 档 t>2 假阳性极多，
   只看 OOS 是否同号）。

用法
----
python3 scripts/validate/daily_scale_exp.py --ema 60 120 180 250
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date

import numpy as np
import pandas as pd
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import session_scope
from app.backtest.fusion_backtest import (
    FusionBacktestParams,
    _generate_states,
    _simulate_trades,
    fusion_params_from_config,
    _read_hourly,
)

# 统计窗口：沿用品种选型实验的分段，便于横向对照
IS_WIN = (date(2021, 1, 1), date(2023, 12, 31))
OOS_WIN = (date(2024, 1, 1), date(2026, 10, 30))
PREHEAT_START = date(2015, 1, 1)  # 信号预热起点（不参与统计）


def _read_daily(session, symbol: str, src: str,
                start: date, end: date) -> pd.DataFrame | None:
    """读 l0_raw.daily_bar，返回与 _read_hourly **完全同格式**的 DataFrame。

    列名/顺序/类型必须与小时线一致（dt, open, high, low, close / float64），
    这样下游 _generate_states 无需任何适配，避免口径分叉。
    """
    rows = session.execute(
        text(
            "SELECT trade_date, open, high, low, close "
            "FROM l0_raw.daily_bar "
            "WHERE symbol = :sym AND src = :src "
            "  AND trade_date BETWEEN :s AND :e "
            "ORDER BY trade_date ASC"
        ),
        {"sym": symbol, "src": src, "s": start, "e": end},
    ).all()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["dt", "open", "high", "low", "close"])
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    # ⚠ 必须与 _read_hourly **同 dtype**：datetime64[ns, Asia/Shanghai]（tz-aware）。
    # 原因：_simulate_trades 里 dts = states_df["dt"].to_numpy()，
    #  tz-aware 列 -> to_numpy() 返回 object array of pd.Timestamp -> .date()
    #  得到 python datetime.date，cost 模块可正常查 main_contract_map；
    #  若留 naive datetime64 -> to_numpy() 返回 np.datetime64 -> .date() 仍是
    #  numpy 标量 -> psycopg "cannot adapt type 'datetime64'"（全品种静默失败）。
    df["dt"] = pd.to_datetime(df["dt"]).dt.tz_localize("Asia/Shanghai")
    return df


def _sym_metrics(trades: list[dict]) -> dict:
    """R 口径 + 金额口径的核心指标。

    ⚠ net_R 必须**逐笔相减再求均值**（mean(R_i - cost_R_i)）。
    早期版本写成 mean(R) - median(cost_R)，把均值与中位数混用，
    且忽略了 denom<=0 的样本，导致 net_R 与 pnl_yuan 反向互斥
    （实测同一批交易 net_R=-0.0107 但金额净利 +63.9 万），指标失真。
    """
    if not trades:
        return {}
    R = np.array([float(t.get("R") or 0.0) for t in trades], dtype=float)
    pnl = np.array([float(t.get("pnl") or 0.0) for t in trades], dtype=float)
    mult = np.array([float(t.get("mult") or 1.0) for t in trades], dtype=float)
    risk = np.array([float(t.get("risk") or 0.0) for t in trades], dtype=float)
    cost_y = np.array([float(t.get("cost_yuan") or 0.0) for t in trades], dtype=float)
    denom = risk * mult
    # 逐笔成本 R：分母无效（risk/mult 缺失）时按 0 计入，避免静默丢样本
    cost_R_arr = np.where(denom > 0, cost_y / np.where(denom > 0, denom, 1.0), 0.0)
    net_arr = R - cost_R_arr

    win = R > 0
    gross = float(R.mean())
    cost_R = float(cost_R_arr.mean())
    net = float(net_arr.mean())
    pf = (float(R[win].sum() / abs(R[~win].sum()))
          if (~win).any() and abs(R[~win].sum()) > 1e-12 else float("inf"))
    sd = float(net_arr.std(ddof=1)) if len(net_arr) > 1 else 0.0
    tstat = float(np.sqrt(len(net_arr)) * net / sd) if sd > 1e-12 else 0.0
    # 金额口径净利（含真乘数）
    yuan = float((pnl * mult).sum())
    return {
        "n": len(trades),
        "gross_R": round(gross, 4),
        "cost_R": round(cost_R, 4),
        "net_R": round(net, 4),
        "win_rate": round(float(win.mean()) * 100, 2),
        "pf": round(pf, 3),
        "t": round(tstat, 2),
        "pnl_yuan": round(yuan, 0),
    }


def run_one_segment(session, symbols: list[str], ema_k: int,
                    win: tuple[date, date], base: FusionBacktestParams,
                    tf: str = "daily", daily_src: str = "akshare") -> dict:
    """在全品种上跑日线单个 EMA 档位，统计落在 win 窗口内的成交。"""
    import dataclasses

    p = dataclasses.replace(
        base,
        ema_k=ema_k,
        # 日线不能沿用小时线 max_bars=800（= 3.2 年）否则样本被截断；
        # 小时线对照组保持生产原值不动。
        max_bars=4000 if tf == "daily" else base.max_bars,
    )
    all_tr: list[dict] = []
    per_sym: dict[str, dict] = {}
    skipped: list[str] = []

    for sym in symbols:
        if tf == "hourly":
            # 对照组：生产口径（小时线），走引擎原生读取器，零改造风险
            df = _read_hourly(session, sym, p.src, PREHEAT_START, win[1])
        else:
            # ⚠ 日线源不能用 p.src（=bar_60m，仅 60m 表有）；主力日线在
            # l0_raw.daily_bar 的 src=akshare（主连日线）。用 daily_src 显式指定。
            df = _read_daily(session, sym, daily_src, PREHEAT_START, win[1])
        if df is None or len(df) < p.min_bars:
            skipped.append(sym)
            continue
        try:
            st = _generate_states(df, p)
            tr = _simulate_trades(st, p, symbol=sym)
        except Exception as e:  # 单品种失败不中断整体，但要留痕
            skipped.append(sym)
            print(f"  [warn] {sym} 失败: {type(e).__name__}: {e}")
            continue
        # 按建仓日落入统计窗口过滤（信号本身已用全历史预热）
        # 注意 entry_dt 现在是 tz-aware，比较窗口必须同 tz，否则 TypeError
        lo = pd.Timestamp(win[0]).tz_localize("Asia/Shanghai")
        hi = pd.Timestamp(win[1]).tz_localize("Asia/Shanghai")
        keep = [t for t in tr
                if t.get("entry_dt") is not None
                and lo <= pd.Timestamp(t["entry_dt"]) <= hi]
        for t in keep:
            t["symbol"] = sym
        if keep:
            per_sym[sym] = _sym_metrics(keep)
        all_tr.extend(keep)

    agg = _sym_metrics(all_tr)
    agg["ema_k"] = ema_k
    agg["tf"] = tf
    agg["window"] = f"{win[0]}~{win[1]}"
    agg["n_symbols"] = len(per_sym)
    agg["skipped"] = len(skipped)
    agg["per_symbol"] = per_sym
    return agg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ema", nargs="+", type=int, default=[60, 120, 180, 250])
    ap.add_argument("--max-symbols", type=int, default=0, help="0=全部")
    ap.add_argument("--tf", default="daily,hourly",
                    help="要跑的时间尺度，逗号分隔；hourly 为生产对照")
    ap.add_argument("--hourly-ema", nargs="+", type=int, default=[140],
                    help="小时线对照组 EMA（生产=140）")
    ap.add_argument("--daily-src", default="akshare",
                    help="日线读取源（l0_raw.daily_bar 的 src；bar_60m 在日线表不存在）")
    ap.add_argument("--o", default="/tmp/daily_scale.json")
    args = ap.parse_args()

    st = get_settings()
    symbols = [s.symbol for s in st.main_contracts]
    if args.max_symbols:
        symbols = symbols[: args.max_symbols]

    base = fusion_params_from_config()
    print("=" * 78)
    print("日线尺度实验：周期错配假设验证")
    print(f"品种数={len(symbols)}  基线(小时线)=ema140  EMA档={args.ema}")
    print(f"信号预热起点={PREHEAT_START}（不参与统计，仅给 EMA 预热）")
    print("=" * 78)

    tfs = [x.strip() for x in args.tf.split(",") if x.strip()]
    # (tf, ema_k) 实验清单：先日线各档，后小时线对照
    plan: list[tuple[str, int]] = []
    for t in tfs:
        plan += [(t, k) for k in (args.ema if t == "daily" else args.hourly_ema)]

    print("=" * 78)
    print("日线尺度实验：周期错配假设验证")
    print(f"品种数={len(symbols)}  实验清单={plan}")
    print(f"信号预热起点={PREHEAT_START}（不参与统计，仅给 EMA 预热）")
    print("=" * 78)

    out: dict = {}
    with session_scope() as sess:
        for tf, k in plan:
            print(f"\n--- {tf.upper()} EMA{k} ---", flush=True)
            isagg = run_one_segment(sess, symbols, k, IS_WIN, base, tf=tf,
                                    daily_src=args.daily_src)
            print(f"  IS  {isagg.get('window')}: n={isagg.get('n')} "
                  f"毛={isagg.get('gross_R')} 成本={isagg.get('cost_R')} "
                  f"净={isagg.get('net_R')} PF={isagg.get('pf')} "
                  f"t={isagg.get('t')} 品种={isagg.get('n_symbols')}", flush=True)
            oosagg = run_one_segment(sess, symbols, k, OOS_WIN, base, tf=tf,
                                     daily_src=args.daily_src)
            print(f"  OOS {oosagg.get('window')}: n={oosagg.get('n')} "
                  f"毛={oosagg.get('gross_R')} 成本={oosagg.get('cost_R')} "
                  f"净={oosagg.get('net_R')} PF={oosagg.get('pf')} "
                  f"t={oosagg.get('t')} 品种={oosagg.get('n_symbols')}", flush=True)
            same = (isagg.get("net_R", 0) > 0) and (oosagg.get("net_R", 0) > 0)
            print(f"  同号通过: {'YES' if same else 'NO'}", flush=True)
            isagg["same_sign"] = same
            key = f"{tf}_{k}"
            out[key] = {"is": isagg, "oos": oosagg}

    print("\n" + "=" * 78)
    print("汇总（IS 2021-2023 / OOS 2024-2026）")
    print(f"{'尺度':>12} {'IS n':>6} {'IS净R':>8} {'OOS n':>6} {'OOS毛':>8} "
          f"{'OOS成本':>8} {'OOS净R':>8} {'PF':>6} {'t':>6} {'同号':>5}")
    for k, v in out.items():
        a, b = v["is"], v["oos"]
        print(f"{k:>12} {a.get('n',0):>6} {a.get('net_R',0):>8.4f} {b.get('n',0):>6} "
              f"{b.get('gross_R',0):>8.4f} {b.get('cost_R',0):>8.4f} "
              f"{b.get('net_R',0):>8.4f} {b.get('pf',0):>6.2f} {b.get('t',0):>6.2f} "
              f"{'YES' if a.get('same_sign') else 'NO':>5}")
    print("=" * 78)

    with open(args.o, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"结果已写: {args.o}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
