"""V2 崩塌排查 · 决定性验证：真实合约回测路径。

核心思想（隔离单一变量「数据源 / 主连复权」）：
  用**同一套**已验证的信号层(walk_fusion_states)与组合层(_simulate_trades)，
  在同一套 walk-forward 窗口/参数下，只替换「输入价格序列」：
    · 对照（主连）：fusion_backtest._read_hourly —— 主连 SN888，经 roll_segment.cum_offset 复权
    · 实验（真实合约）：l0_raw.fut_kline 原始价拼接连续序列（绕过 cum_offset 复权 bug）

  若实验口径下 SN/NI 不再巨亏、整体 OOS/decay 显著改善 → 实锤「主连复权缺陷（设定问题）」；
  若两口径同崩 → 指向策略本身（真失效）。

实现：逐品种预计算全历史 trades（仅留存 trades，不囤 K，控内存），再按窗口切分 IS/OOS，
      复刻 walkforward._windows / _net 语义。主连(akshare)口径若复现 core 的
      IS≈-680k / OOS≈-479k，即证明本框架与 core 一致、rc 对比可信。

用法：
  python3 scripts/verify_real_contract_oos.py [--symbols SN,NI] [--src akshare]
          [--out /tmp/rc_verify.json] [--start 2015-01-01] [--end 2026-12-31]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.backtest.fusion_backtest import (
    FusionBacktestParams,
    fusion_params_from_config,
    _generate_states,
    _simulate_trades,
    _read_hourly,
)

# —— 交易所映射（源自分析侧 live_stage_a2_bars，SN/NI→SHFE 正确）——
EX_MAP = {
    "CF": "CZCE", "FG": "CZCE", "SR": "CZCE", "SA": "CZCE", "TA": "CZCE", "MA": "CZCE",
    "RM": "CZCE", "OI": "CZCE", "AP": "CZCE", "UR": "CZCE", "PF": "CZCE", "PK": "CZCE",
    "SH": "CZCE", "PX": "CZCE", "PR": "CZCE", "SF": "CZCE", "SM": "CZCE", "CY": "CZCE",
    "ZC": "CZCE", "RS": "CZCE", "JR": "CZCE", "LR": "CZCE", "PM": "CZCE", "WH": "CZCE",
    "RI": "CZCE", "BR": "CZCE", "CJ": "CZCE",
    "A": "DCE", "B": "DCE", "M": "DCE", "Y": "DCE", "P": "DCE", "C": "DCE", "CS": "DCE",
    "RR": "DCE", "L": "DCE", "PP": "DCE", "EB": "DCE", "EG": "DCE", "LG": "DCE", "LH": "DCE",
    "V": "DCE", "JD": "DCE", "JM": "DCE",
    "CU": "SHFE", "AL": "SHFE", "ZN": "SHFE", "PB": "SHFE", "RB": "SHFE", "HC": "SHFE",
    "SS": "SHFE", "FU": "SHFE", "BU": "SHFE", "AU": "SHFE", "AG": "SHFE", "SP": "SHFE",
    "AO": "SHFE", "RU": "SHFE", "NI": "SHFE", "SN": "SHFE",
    "SI": "GFEX", "LC": "GFEX", "PS": "GFEX",
    "SC": "INE", "LU": "INE", "NR": "INE",
}


def _to_naive(ts):
    ts = pd.to_datetime(ts)
    if ts.dt.tz is not None:
        ts = ts.dt.tz_convert(None)
    return ts


# --------------------------------------------------------------------------
# 真实合约连续序列构造（绕过 roll_segment.cum_offset 复权，用原始价拼接）
# --------------------------------------------------------------------------
def _fk_symbol_candidates(underlying: str, ex: str) -> list[str]:
    """underlying 可能带/不带交易所前缀、大小写不一 → 构造 fut_kline 候选 symbol。"""
    u = underlying.strip()
    cands: list[str] = []
    if "." in u:
        cands.append(u)
        base = u.split(".", 1)[1]
    else:
        base = u
    cands.append(f"{ex}.{base.lower()}")
    cands.append(f"{ex}.{base}")
    return cands


def build_continuous(session, product: str, ex: str, start: date | None, end: date | None):
    """用 l3_ref.main_contract_map + l0_raw.fut_kline 拼接「主力合约连续序列」。

    返回 df[dt,open,high,low,close]（dt 已转 naive），或 None（无数据）。
    """
    mcm = session.execute(text(
        "SELECT trade_date, underlying FROM l3_ref.main_contract_map "
        "WHERE product=:p ORDER BY trade_date"
    ), {"p": product}).all()
    if not mcm:
        return None
    underlyings = sorted({u for _, u in mcm}, key=lambda x: str(x))
    # 解析每个 underlying 实际存在的 fut_kline symbol
    fk_by_u: dict[str, str] = {}
    for u in underlyings:
        for c in _fk_symbol_candidates(u, ex):
            n = session.execute(text(
                "SELECT count(*) FROM l0_raw.fut_kline "
                "WHERE freq='hourly' AND symbol=:s"), {"s": c}).scalar()
            if n and n > 30:
                fk_by_u[u] = c
                break
    if not fk_by_u:
        return None
    # 批量取所有合约 K（一次查询）
    fks = list(fk_by_u.values())
    rows = session.execute(text(
        "SELECT symbol, trade_datetime, open, high, low, close FROM l0_raw.fut_kline "
        "WHERE freq='hourly' AND symbol=ANY(:s) ORDER BY symbol, trade_datetime"
    ), {"s": fks}).all()
    by_fk: dict[str, pd.DataFrame] = {}
    for s_, ts, o, h, l, c in rows:
        if None in (o, h, l, c):
            continue
        by_fk.setdefault(s_, []).append((ts, float(o), float(h), float(l), float(c)))
    # 按主力段切分拼接
    frames = []
    for i, (d, u) in enumerate(mcm):
        e = mcm[i + 1][0] if i + 1 < len(mcm) else None
        fk = fk_by_u.get(u)
        if not fk or fk not in by_fk:
            continue
        df = pd.DataFrame(by_fk[fk], columns=["dt", "open", "high", "low", "close"])
        d0 = pd.Timestamp(d)
        hi = pd.Timestamp(e) if e else pd.Timestamp.max
        seg = df[(_to_naive(df["dt"]).dt.date >= d0.date()) &
                 (_to_naive(df["dt"]).dt.date < hi.date())]
        frames.append(seg)
    if not frames:
        return None
    out = pd.concat(frames).drop_duplicates(subset=["dt"]).sort_values("dt").reset_index(drop=True)
    out["dt"] = _to_naive(out["dt"])
    # 范围过滤
    if start:
        out = out[out["dt"].dt.date >= start]
    if end:
        out = out[out["dt"].dt.date <= end]
    return out if len(out) >= 160 else None


# --------------------------------------------------------------------------
# 单品种全历史回测（双口径）
# --------------------------------------------------------------------------
def backtest_symbol_full(session, symbol: str, base: FusionBacktestParams,
                         mode: str, ex: str, start, end) -> list[dict]:
    if mode == "main":
        df = _read_hourly(session, symbol, base.src, start, end)
    else:
        product = symbol[:-3]
        df = build_continuous(session, product, ex, start, end)
    if df is None or len(df) < base.min_bars:
        return []
    states_df = _generate_states(df, base)
    trades = _simulate_trades(states_df, base, symbol=symbol)
    for t in trades:
        t["symbol"] = symbol
    return trades


def _dt(v):
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day)
    return None


def _pnl_yuan(t: dict) -> float:
    return float(t["pnl"]) * float(t.get("mult", 1.0) or 1.0)


# --------------------------------------------------------------------------
# walk-forward 窗口（复刻 walkforward._windows）
# --------------------------------------------------------------------------
def _windows(start: date, end: date, wf) -> list[tuple]:
    trd, ted, st = wf["train_days"], wf["test_days"], max(1, wf["step_days"])
    cur = start
    w = trd + ted
    out = []
    while cur + timedelta(days=w) <= end:
        te_s = cur + timedelta(days=trd)
        te_e = cur + timedelta(days=w) - timedelta(days=1)
        out.append((cur, te_s - timedelta(days=1), te_s, te_e))
        cur = cur + timedelta(days=st)
    return out


def _wf_for_mode(trades_by_sym: dict, symbols: list[str], wf: dict,
                 start: date, end: date, label: str) -> dict:
    is_net_total = 0.0
    oos_net_total = 0.0
    n_windows = 0
    n_oos_trades = 0
    windows = []
    for tr_s, tr_e, te_s, te_e in _windows(start, end, wf):
        is_net = 0.0
        oos_net = 0.0
        n_oos = 0
        for sym in symbols:
            for t in trades_by_sym.get(sym, {}).get(label, []):
                ed = _dt(t.get("entry_dt"))
                if ed is None:
                    continue
                edd = ed.date()
                if tr_s <= edd <= tr_e:
                    is_net += float(t["pnl"])
                if (not t.get("open")) and te_s <= edd <= te_e:
                    oos_net += float(t["pnl"])
                    n_oos += 1
        if n_oos < wf["min_oos_trades"]:
            continue
        is_net_total += is_net
        oos_net_total += oos_net
        n_oos_trades += n_oos
        n_windows += 1
        decay = (oos_net / is_net) if abs(is_net) > 1e-12 else None
        windows.append({"test": [str(te_s), str(te_e)], "n_oos": n_oos,
                        "is_net": round(is_net, 2), "oos_net": round(oos_net, 2),
                        "decay": round(decay, 4) if decay is not None else None})
    decay_total = (oos_net_total / is_net_total) if abs(is_net_total) > 1e-12 else None
    return {
        "label": label, "n_windows": n_windows, "n_oos_trades": n_oos_trades,
        "is_net": round(is_net_total, 2), "oos_net": round(oos_net_total, 2),
        "decay": round(decay_total, 4) if decay_total is not None else None,
        "windows": windows,
    }


# --------------------------------------------------------------------------
# 全量单跑汇总（IS/OOS 分界 + 全量）
# --------------------------------------------------------------------------
def _summarize(trades: list[dict], is_cut=pd.Timestamp("2020-12-31")):
    closed = [t for t in trades if not t.get("open")]
    def stat(rows):
        n = len(rows)
        if n == 0:
            return {"n": 0, "win_rate": None, "net_yuan": 0.0, "net_pct": 0.0}
        pnls = [_pnl_yuan(t) for t in rows]
        pcts = [float(t["pnl_pct"]) for t in rows]
        ws = [t for t in rows if _pnl_yuan(t) > 0]
        return {"n": n, "win_rate": round(len(ws) / n, 4),
                "net_yuan": round(float(sum(pnls)), 2),
                "net_pct": round(float(np.mean(pcts)), 4)}
    is_t = [t for t in closed if _dt(t.get("entry_dt")) is not None
            and _dt(t.get("entry_dt")) <= is_cut]
    oos_t = [t for t in closed if _dt(t.get("entry_dt")) is not None
             and _dt(t.get("entry_dt")) > is_cut]
    return {"all": stat(closed), "is": stat(is_t), "oos": stat(oos_t)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="", help="逗号分隔品种主连码，如 SN,NI；空=全部")
    ap.add_argument("--src", default="akshare", help="主连对照数据源(akshare/bar_60m)")
    ap.add_argument("--out", default="/tmp/rc_verify.json")
    ap.add_argument("--start", default="2015-01-01")
    ap.add_argument("--end", default="2026-12-31")
    ap.add_argument("--focus", default="SN,NI", help="重点对比品种(主连码)")
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    settings = get_settings()
    base = fusion_params_from_config()
    base.src = args.src  # 主连对照源

    if args.symbols:
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        syms = [s.symbol for s in settings.main_contracts]

    focus = [s.strip() for s in args.focus.split(",") if s.strip()]
    wf = {"train_days": 720, "test_days": 60, "step_days": 60,
          "warmup_days": 150, "min_oos_trades": 5}

    from app.core.db import session_scope
    with session_scope() as session:
        # 逐品种预计算双口径全历史 trades
        trades_by_sym: dict[str, dict] = {}
        per_symbol_full: dict[str, dict] = {}
        for sym in syms:
            ex = EX_MAP.get(sym[:-3])
            if not ex:
                continue
            rc = backtest_symbol_full(session, sym, base, "rc", ex, start, end)
            main = backtest_symbol_full(session, sym, base, "main", ex, start, end)
            trades_by_sym[sym] = {"rc": rc, "main": main}
            per_symbol_full[sym] = {
                "rc": _summarize(rc), "main": _summarize(main),
                "n_rc": len([t for t in rc if not t.get("open")]),
                "n_main": len([t for t in main if not t.get("open")]),
            }
            print(f"[preload] {sym}: rc_n={per_symbol_full[sym]['n_rc']} "
                  f"main_n={per_symbol_full[sym]['n_main']}", flush=True)

        # walk-forward 双口径
        rc_wf = _wf_for_mode(trades_by_sym, syms, wf, start, end, "rc")
        main_wf = _wf_for_mode(trades_by_sym, syms, wf, start, end, "main")

        # 全量单跑汇总
        all_rc = [t for sym in syms for t in trades_by_sym[sym]["rc"]]
        all_main = [t for sym in syms for t in trades_by_sym[sym]["main"]]
        full_rc = _summarize(all_rc)
        full_main = _summarize(all_main)

        # 重点品种（focus）
        focus_tbl = {}
        for f in focus:
            if f in trades_by_sym:
                focus_tbl[f] = {
                    "rc": _summarize(trades_by_sym[f]["rc"]),
                    "main": _summarize(trades_by_sym[f]["main"]),
                }

        result = {
            "meta": {
                "src": args.src, "start": args.start, "end": args.end,
                "n_symbols": len(syms), "params": {
                    "sl_atr": base.sl_atr, "trail_atr": base.trail_atr,
                    "be_r": base.be_r, "entry_mode": base.entry_mode,
                },
                "wf": wf,
            },
            "walk_forward": {"rc": rc_wf, "main": main_wf},
            "full_single_run": {"rc": full_rc, "main": full_main},
            "focus": focus_tbl,
            "per_symbol_full": per_symbol_full,
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, default=str)

    # 文本报告
    print("\n================ 决定性验证结果 ================")
    print(f"主连对照源: {args.src}  区间: {args.start}~{args.end}  品种数: {len(syms)}")
    print(f"参数: SL{base.sl_atr} TR{base.trail_atr} BE{base.be_r} mode={base.entry_mode}")
    print("\n--- walk-forward（复刻 core 窗口 720/60/60）---")
    for lab, w in (("主连(main)", main_wf), ("真实合约(rc)", rc_wf)):
        print(f"  [{lab}] 窗数={w['n_windows']} OOS笔={w['n_oos_trades']} "
              f"IS净={w['is_net']:>+.0f} OOS净={w['oos_net']:>+.0f} decay={w['decay']}")
    print("\n--- 全量单跑（IS/OOS 分界 2020-12-31，金额元）---")
    for lab, s in (("主连(main)", full_main), ("真实合约(rc)", full_rc)):
        print(f"  [{lab}] 全量净={s['all']['net_yuan']:>+.0f}元 "
              f"胜率={s['all']['win_rate']} | IS净={s['is']['net_yuan']:>+.0f} "
              f"OOS净={s['oos']['net_yuan']:>+.0f}")
    print("\n--- 重点品种 SN/NI（金额元，全量单跑）---")
    for f, d in focus_tbl.items():
        rc_n = d["rc"]["all"]; mn = d["main"]["all"]
        print(f"  {f}: 真实合约净={rc_n['net_yuan']:>+.0f}(胜{rc_n['win_rate']}) "
              f"| 主连净={mn['net_yuan']:>+.0f}(胜{mn['win_rate']}) "
              f"| Δ={rc_n['net_yuan']-mn['net_yuan']:>+.0f}")
    print(f"\nJSON -> {args.out}")


if __name__ == "__main__":
    main()
