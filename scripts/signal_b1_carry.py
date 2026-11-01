#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""P0 B1 基差 carry 翻案信号（2026-11-02，研究分支 cb/v2-oos-investigation，未碰 main/cb/dev）。

§12 撤销「carry 不可行」误判：真实数据一直在 l0_raw.spot_basis / l2_adj.factor_value，
当初只扫了 l1_mkt 层。本信号用现成因子 basis_rate_z —— 探针 Q3 已证实其 raw_value
逐笔精确等于 spot_basis.dom_basis_rate =（主力价 − 现货价）/现货价。

做**横截面 carry**：同一交易日横比 45 个品种，贴水最深做多、升水最高做空。

★ 防前视（第一要务；§12.7 硬护栏 + 纪律1「高IC先查前视」）：
  探针 Q1 证实 l2_adj.factor_value.available_at 对全部 B 类因子恒 == trade_date
  （basis_rate_z 116471 行 lag 全 0）—— 平台**未编码** akshare 现货价/仓单/持仓的盘后发布
  延迟。若照 §12.7 原计划「用 available_at<=d 对齐」，d 日 open 会用到「d 日盘后才发布」
  的现货价 = 前视。故本信号**不依赖 available_at**，改为显式把因子滞后 lag 个交易日：
      决策日 i（i 日 open 成交）只用 trade_date <= i-lag 的因子值。
  默认 lag=1（当日盘后发布、次日开盘可用）；并跑 lag∈{1,2,3} 稳健性，确认边不依赖
  「刚好 1 天缓冲」这一假设，也不靠抢跑发布时点。

方向（经济先验固定，不事后挑选）：
  dom_basis_rate < 0 = 期货贴水 backwardation -> 持多到期向现货收敛获利 -> 做多
  dom_basis_rate > 0 = 期货升水 contango      -> 做空
  即 bottom-q(rank 最低/最负) 赋 +1，top-q(rank 最高/最正) 赋 -1。
  --direction 仅供方向核验诊断：若理论方向失败，记录反向表现但**不作准入依据**（避免挑方向）。

口径与 §9 底座逐位一致：WF-OOS(train720/test60/step60/warmup150)、真实成本、
次日 open 成交、R=(pnl−cost)/(3×日线ATR)。symbol 取主连∩因子 = 45/50
（AO/AP/B/CS/SC 主连无基差因子，缺失品种 target=0）。
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from signal_oos_backtest import run_signal_wf, print_report
from app.core.config import get_settings


def load_carry_matrix(symbols: list[str], start: date, end: date):
    """从 l2_adj.factor_value 取 basis_rate_z 的 raw(=dom_basis_rate)/z 矩阵。

    返回 (raw, z) 两个 DataFrame，index=DatetimeIndex(trade_date)，columns=symbol(888)。
    查询范围前推，覆盖底座 data_start=start-400 的 warmup。
    """
    settings = get_settings()
    engine = create_engine(settings.db_url, pool_size=4)
    session = sessionmaker(bind=engine)()
    rows = session.execute(text(
        "SELECT trade_date, symbol, raw_value, z_value, available_at "
        "FROM l2_adj.factor_value "
        "WHERE factor_id='basis_rate_z' AND trade_date >= :s AND trade_date <= :e"
    ), {"s": start - timedelta(days=450), "e": end}).all()
    session.close(); engine.dispose()

    df = pd.DataFrame(rows, columns=["date", "symbol", "raw", "z", "avail"])
    df["raw"] = pd.to_numeric(df["raw"], errors="coerce")
    df["z"] = pd.to_numeric(df["z"], errors="coerce")
    df["date"] = pd.to_datetime(df["date"])
    raw = df.pivot(index="date", columns="symbol", values="raw")
    z = df.pivot(index="date", columns="symbol", values="z")
    return raw, z


def compute_targets(closes: pd.DataFrame, opens=None, raw=None, z=None,
                    q: float = 0.2, lag: int = 1, direction: int = 1,
                    value_kind: str = "raw", min_valid: int = 10,
                    ffill_limit: int = 5) -> pd.DataFrame:
    """横截面 carry targets（date×symbol ∈ {-1,0,1}）。

    ★ 防前视：先把因子 reindex 到行情交易日、ffill 补缺，再整体 shift(lag)。
    shift 之后 target[i] 只含 trade_date <= i-lag 的信息，底座在 i 日 open 成交即安全。
    """
    mat = z if value_kind == "z" else raw
    ci = pd.to_datetime(closes.index)
    aligned = mat.reindex(ci).ffill(limit=ffill_limit)   # 对齐交易日 + 有限补缺
    aligned.index = closes.index
    lagged = aligned.shift(lag)                           # ★ 显式滞后，防前视
    pct = lagged.rank(axis=1, pct=True)
    valid = lagged.notna().sum(axis=1)
    targets = pd.DataFrame(0.0, index=closes.index, columns=closes.columns)
    notna = lagged.notna()
    lo = notna & (pct <= q)        # 贴水最深（rate 最负）
    hi = notna & (pct >= (1 - q))  # 升水最高（rate 最正）
    targets[lo] = float(direction) * 1.0
    targets[hi] = -float(direction) * 1.0
    targets[valid < min_valid] = 0.0
    return targets


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2021-01-01")
    ap.add_argument("--end", default="2026-10-30")
    ap.add_argument("--q", type=float, default=0.2)
    ap.add_argument("--lag", type=int, default=1,
                    help="因子滞后交易日数（防前视）；跑 1/2/3 做稳健性")
    ap.add_argument("--direction", type=int, default=1, choices=[1, -1],
                    help="+1=理论方向(贴水做多)；-1=方向核验诊断(不作准入依据)")
    ap.add_argument("--value-kind", default="raw", choices=["raw", "z"])
    ap.add_argument("--rebalance-n", type=int, default=5)
    ap.add_argument("--min-valid", type=int, default=10)
    ap.add_argument("--no-placebo", dest="do_placebo", action="store_false")
    ap.add_argument("--n-perm", type=int, default=300)
    ap.add_argument("--out", default="/tmp/b1_carry.json")
    args = ap.parse_args()

    symbols = [s.symbol for s in get_settings().main_contracts]
    s, e = date.fromisoformat(args.start), date.fromisoformat(args.end)
    raw, z = load_carry_matrix(symbols, s, e)
    inter = sorted(set(symbols) & set(raw.columns))
    print(f"[carry] factor symbols={len(raw.columns)} 主连={len(symbols)} "
          f"交集={len(inter)} raw范围=[{raw.index.min().date()},{raw.index.max().date()}]",
          flush=True)

    def sig(closes, opens=None, **kw):
        return compute_targets(closes, opens=opens, raw=raw, z=z, q=args.q,
                               lag=args.lag, direction=args.direction,
                               value_kind=args.value_kind, min_valid=args.min_valid)

    report = run_signal_wf(sig, "B1_basis_carry", symbols, s, e,
                           rebalance_n=args.rebalance_n, out=args.out,
                           do_placebo=args.do_placebo, n_perm=args.n_perm,
                           q=args.q, lag=args.lag, direction=args.direction,
                           value_kind=args.value_kind)
    report["signal_params"] = {
        "q": args.q, "lag": args.lag, "direction": args.direction,
        "value_kind": args.value_kind, "rebalance_n": args.rebalance_n,
        "min_valid": args.min_valid, "ffill_limit": 5,
        "n_intersection": len(inter),
        "avail_at_note": "available_at==trade_date(lag0)，故禁用其对齐，改用显式 shift(lag) 滞后",
    }
    if args.out:
        Path(args.out).write_text(json.dumps(report, ensure_ascii=False,
                                             indent=1, default=str), encoding="utf-8")
    print_report(report)


if __name__ == "__main__":
    main()
