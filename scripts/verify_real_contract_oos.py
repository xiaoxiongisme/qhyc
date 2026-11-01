"""V2 崩塌排查 · 决定性验证：真实合约日线 vs 主连(复权)日线。

核心思想（隔离单一变量「数据源 / 主连复权」）：
  用**同一套**信号层(_generate_states)与组合层(_simulate_trades)，在同一套
  walk-forward 窗口/参数下，只替换「输入日线价格序列」：
    · 对照 A（主连·复权）：l1_mkt.bar_60m(SN888) 重采样日线
        —— 生产回测真用的序列，携带 l2_adj.roll_segment.cum_offset 累计复权；
           probe 证实它在每次换月处相对真实主力合约价出现数千~上万点的台阶跳变。
    · 实验 B（真实合约·未复权）：public.contract_daily 经 public.main_contract_map
        逐日取「当日主力真实合约」的日线 OHLC 拼接（绕过 cum_offset）。

  若 B 口径下 SN/NI 不再巨亏、整体 OOS/decay 显著改善 → 实锤「主连复权缺陷（设定问题）」；
  若 A/B 同崩 → 指向策略本身（真失效）。

  重要约束（probe 实测）：contract_daily 仅覆盖 2021+（SN 起 2021-01-18），
  故本实验窗口限定 2021-01-01 ~ end；A 额外跑全历史[2015,end]用于框架校验
  （验证「复权日线 A 能复现生产崩塌」）。

用法：
  python3 scripts/verify_real_contract_oos.py --symbols SN888,NI888
          [--src bar_60m] [--out /tmp/rc.json] [--start 2021-01-01] [--end 2026-12-31]
          [--focus SN,NI] [--full-history]  (默认开；关掉可省一半 A 计算)
"""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
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
)

# —— 交易所映射 ——
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


TZ = "Asia/Shanghai"


def _to_ts(d):
    # 必须返回 **tz-aware** pd.Timestamp，以匹配生产 _read_hourly 路径：
    # bar_60m 的 bucket 是 tz-aware，pandas 据此把 dt 列存为 tz-aware datetime64，
    # .to_numpy() 后得到 pd.Timestamp 对象（带 .date()），_simulate_trades 成本查库
    # 才能用 d.date() 取费率。若返回 tz-naive python datetime，pandas 会 coercion 成
    # datetime64[ns]，.to_numpy() 变 np.datetime64（无 .date()），psycopg 无法适配。
    if isinstance(d, pd.Timestamp):
        ts = d
    elif isinstance(d, datetime):
        ts = pd.Timestamp(d)
    elif isinstance(d, date):
        ts = pd.Timestamp(datetime(d.year, d.month, d.day, 15, 0, 0))
    elif isinstance(d, np.datetime64):
        ts = pd.Timestamp(d)
    else:
        ts = pd.Timestamp(str(d))
    if ts.tzinfo is None:
        ts = ts.tz_localize(TZ)
    return ts


# --------------------------------------------------------------------------
# A：主连复权日线（bar_60m 重采样）
# --------------------------------------------------------------------------
def build_main_daily(session, symbol: str, start, end):
    rows = session.execute(text(
        "SELECT bucket, open, high, low, close FROM l1_mkt.bar_60m "
        "WHERE symbol=:s AND bucket>=:a AND bucket<=:b ORDER BY bucket"
    ), {"s": symbol, "a": pd.Timestamp(start), "b": pd.Timestamp(end)}).all()
    if not rows:
        return None
    g = defaultdict(list)
    for b, o, h, l, c in rows:
        if None in (o, h, l, c):
            continue
        g[b.date()].append((b, float(o), float(h), float(l), float(c)))
    recs = []
    for d in sorted(g):
        bars = sorted(g[d], key=lambda x: x[0])
        o0 = bars[0][1]
        h0 = max(x[2] for x in bars)
        l0 = min(x[3] for x in bars)
        c0 = bars[-1][4]
        recs.append((_to_ts(d), o0, h0, l0, c0))
    return pd.DataFrame(recs, columns=["dt", "open", "high", "low", "close"])


# --------------------------------------------------------------------------
# B：真实合约日线（contract_daily 经 main_contract_map 逐日取主力真实合约）
# --------------------------------------------------------------------------
def build_continuous(session, product: str, ex: str, start, end):
    mcm = session.execute(text(
        "SELECT trade_date, underlying FROM public.main_contract_map "
        "WHERE product=:p ORDER BY trade_date"
    ), {"p": product}).all()
    if not mcm:
        return None
    date2u = {d: u for d, u in mcm}
    rows = session.execute(text(
        "SELECT trade_date, symbol, open, high, low, close FROM public.contract_daily "
        "WHERE product=:p AND trade_date>=:a AND trade_date<=:b ORDER BY trade_date"
    ), {"p": product, "a": start, "b": end}).all()
    recs = []
    for d, s, o, h, l, c in rows:
        if s == date2u.get(d):
            if None in (o, h, l, c):
                continue
            recs.append((_to_ts(d), float(o), float(h), float(l), float(c)))
    if len(recs) < 160:
        return None
    return pd.DataFrame(recs, columns=["dt", "open", "high", "low", "close"]).sort_values("dt").reset_index(drop=True)


# --------------------------------------------------------------------------
# 单品种全历史回测（双口径）
# --------------------------------------------------------------------------
def backtest_symbol_full(session, symbol: str, base: FusionBacktestParams,
                         mode: str, ex: str, start, end) -> list[dict]:
    if mode == "main":
        df = build_main_daily(session, symbol, start, end)
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
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    return None


def _pnl_yuan(t: dict) -> float:
    return float(t["pnl"]) * float(t.get("mult", 1.0) or 1.0)


# --------------------------------------------------------------------------
# walk-forward 窗口（复刻 walkforward._windows 720/60/60）
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
                if te_s <= edd <= te_e:
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


def _summarize(trades: list[dict], is_cut):
    closed = [t for t in trades if not t.get("open")]
    def stat(rows):
        n = len(rows)
        if n == 0:
            return {"n": 0, "win_rate": None, "net_yuan": 0.0, "net_pct": 0.0, "net_pts": 0.0}
        pnls = [_pnl_yuan(t) for t in rows]
        pcts = [float(t["pnl_pct"]) for t in rows]
        pts = [float(t["pnl"]) for t in rows]
        ws = [t for t in rows if _pnl_yuan(t) > 0]
        return {"n": n, "win_rate": round(len(ws) / n, 4),
                "net_yuan": round(float(sum(pnls)), 2),
                "net_pts": round(float(sum(pts)), 2),
                "net_pct": round(float(np.mean(pcts)), 4)}
    is_t = [t for t in closed if _dt(t.get("entry_dt")) is not None
            and _dt(t.get("entry_dt")).date() <= is_cut]
    oos_t = [t for t in closed if _dt(t.get("entry_dt")) is not None
             and _dt(t.get("entry_dt")).date() > is_cut]
    return {"all": stat(closed), "is": stat(is_t), "oos": stat(oos_t)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default="", help="逗号分隔主连码，如 SN888,NI888；空=全部")
    ap.add_argument("--out", default="/tmp/rc_verify.json")
    ap.add_argument("--start", default="2021-01-01", help="真实合约对照起点(contract_daily 仅2021+)")
    ap.add_argument("--end", default="2026-12-31")
    ap.add_argument("--focus", default="SN,NI", help="重点对比品种(主连码)")
    ap.add_argument("--full-history", action="store_true", default=True,
                    help="额外跑 A 全历史[2015,end]做框架校验(复现生产崩塌)")
    ap.add_argument("--no-full-history", dest="full_history", action="store_false")
    args = ap.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    settings = get_settings()
    base = fusion_params_from_config()

    if args.symbols:
        syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    else:
        syms = [s.symbol for s in settings.main_contracts]

    focus = [s.strip() for s in args.focus.split(",") if s.strip()]
    wf = {"train_days": 720, "test_days": 60, "step_days": 60,
          "warmup_days": 150, "min_oos_trades": 3}

    from app.core.db import session_scope
    with session_scope() as session:
        trades_by_sym: dict[str, dict] = {}
        per_symbol_full: dict[str, dict] = {}
        for sym in syms:
            ex = EX_MAP.get(sym[:-3])
            if not ex:
                continue
            # B：真实合约日线（仅 start~end 有数据）
            rc = backtest_symbol_full(session, sym, base, "rc", ex, start, end)
            # A：主连复权日线（start~end，与 B 同窗对照）
            main_c = backtest_symbol_full(session, sym, base, "main", ex, start, end)
            trades_by_sym[sym] = {"rc": rc, "main_c": main_c}
            per_symbol_full[sym] = {
                "rc": _summarize(rc, is_cut=date(2024, 1, 1)),
                "main_c": _summarize(main_c, is_cut=date(2024, 1, 1)),
                "n_rc": len([t for t in rc if not t.get("open")]),
                "n_main_c": len([t for t in main_c if not t.get("open")]),
            }
            print(f"[preload] {sym}: rc_n={per_symbol_full[sym]['n_rc']} "
                  f"main_n={per_symbol_full[sym]['n_main_c']}", flush=True)

        # A 全历史（框架校验）
        main_full = {}
        if args.full_history:
            for sym in syms:
                ex = EX_MAP.get(sym[:-3])
                if not ex:
                    continue
                mf = backtest_symbol_full(session, sym, base, "main", ex,
                                         date(2015, 1, 1), end)
                main_full[sym] = mf

        # 仅保留实际产出交易的品种（EX_MAP 缺省或数据缺失的品种会被跳过）
        valid_syms = [s for s in syms if s in trades_by_sym
                      and trades_by_sym[s].get("main_c") is not None]

        # walk-forward 双口径（同窗 start~end）
        rc_wf = _wf_for_mode(trades_by_sym, valid_syms, wf, start, end, "rc")
        main_wf = _wf_for_mode(trades_by_sym, valid_syms, wf, start, end, "main_c")
        main_full_wf = None
        if args.full_history:
            mfull = {sym: {"main_full": mf} for sym, mf in main_full.items()}
            main_full_wf = _wf_for_mode(mfull, valid_syms, wf, date(2015, 1, 1), end, "main_full")

        # 全量单跑汇总
        all_rc = [t for sym in valid_syms for t in trades_by_sym[sym].get("rc", [])]
        all_main_c = [t for sym in valid_syms for t in trades_by_sym[sym].get("main_c", [])]
        full_rc = _summarize(all_rc, is_cut=date(2024, 1, 1))
        full_main_c = _summarize(all_main_c, is_cut=date(2024, 1, 1))

        focus_tbl = {}
        for f in focus:
            sym = next((s for s in syms if s[:-3] == f), None)
            if sym and sym in trades_by_sym:
                focus_tbl[f] = {
                    "rc": _summarize(trades_by_sym[sym]["rc"], is_cut=date(2024, 1, 1)),
                    "main_c": _summarize(trades_by_sym[sym]["main_c"], is_cut=date(2024, 1, 1)),
                    "rc_wf": _wf_for_mode({sym: trades_by_sym[sym]}, [sym], wf, start, end, "rc"),
                    "main_wf": _wf_for_mode({sym: trades_by_sym[sym]}, [sym], wf, start, end, "main_c"),
                }

        result = {
            "meta": {
                "start": args.start, "end": args.end, "n_symbols": len(syms),
                "params": {"sl_atr": base.sl_atr, "trail_atr": base.trail_atr,
                           "be_r": base.be_r, "entry_mode": base.entry_mode,
                           "ema_k": base.ema_k, "ma_n": base.ma_n, "W": base.W},
                "wf": wf,
                "note": "A=bar_60m复权日线(生产口径)；B=contract_daily真实主力日线(未复权)；"
                        "合约日线仅2021+，故对照窗=2021~end；A另跑全历史[2015,end]校验。",
            },
            "walk_forward": {"rc": rc_wf, "main_c": main_wf, "main_full": main_full_wf},
            "full_single_run": {"rc": full_rc, "main_c": full_main_c},
            "focus": focus_tbl,
            "per_symbol_full": per_symbol_full,
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, default=str)

    # 文本报告
    print("\n================ 决定性验证结果（日线对照）================")
    print(f"对照窗: {args.start}~{args.end}  品种数: {len(syms)}")
    print(f"参数: SL{base.sl_atr} TR{base.trail_atr} BE{base.be_r} mode={base.entry_mode} "
          f"ema_k={base.ema_k} ma_n={base.ma_n} W={base.W}")
    print("\n--- walk-forward（720/60/60，同窗 2021+）---")
    for lab, w in (("主连复权A(main_c)", main_wf), ("真实合约B(rc)", rc_wf)):
        print(f"  [{lab}] 窗数={w['n_windows']} OOS笔={w['n_oos_trades']} "
              f"IS净={w['is_net']:>+.0f} OOS净={w['oos_net']:>+.0f} decay={w['decay']}")
    if main_full_wf:
        print(f"  [主连复权A全历史 main_full] 窗数={main_full_wf['n_windows']} "
              f"OOS笔={main_full_wf['n_oos_trades']} IS净={main_full_wf['is_net']:>+.0f} "
              f"OOS净={main_full_wf['oos_net']:>+.0f} decay={main_full_wf['decay']}")
    print("\n--- 全量单跑（按 2024-01-01 切 IS/OOS，金额元）---")
    for lab, s in (("主连复权A(main_c)", full_main_c), ("真实合约B(rc)", full_rc)):
        print(f"  [{lab}] 全量净={s['all']['net_yuan']:>+.0f}元(胜{s['all']['win_rate']}) "
              f"IS净={s['is']['net_yuan']:>+.0f} OOS净={s['oos']['net_yuan']:>+.0f}")
    print("\n--- 重点品种 SN/NI（金额元，全量单跑 + 各自 walk-forward OOS）---")
    for f, d in focus_tbl.items():
        rc_n = d["rc"]["all"]; mn = d["main_c"]["all"]
        print(f"  {f}: 真实合约B净={rc_n['net_yuan']:>+.0f}(胜{rc_n['win_rate']}) "
              f"| 主连A净={mn['net_yuan']:>+.0f}(胜{mn['win_rate']}) "
              f"| Δ(B-A)={rc_n['net_yuan']-mn['net_yuan']:>+.0f}")
        print(f"     B_wf: OOS净={d['rc_wf']['oos_net']:>+.0f}(窗{d['rc_wf']['n_windows']},"
              f"笔{d['rc_wf']['n_oos_trades']})  A_wf: OOS净={d['main_wf']['oos_net']:>+.0f}"
              f"(窗{d['main_wf']['n_windows']},笔{d['main_wf']['n_oos_trades']})")
    print(f"\nJSON -> {args.out}")


if __name__ == "__main__":
    main()
