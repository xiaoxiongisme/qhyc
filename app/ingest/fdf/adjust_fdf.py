# -*- coding: utf-8 -*-
"""入库后做复权，把复权主连写回 fut_kline（kind='cont_adj'）。

复用 build_continuous 的加法平移前复权：
  - 日线：BC.build_adjusted(frames, raw_main_df)
  - 子日线（1h/15min）：_build_adjusted_hourly（按交易日聚合 OI 选主力，再逐根拼主力序列）

用法：
  python -m app.ingest.fdf.adjust_fdf CZCE.FG --freq hourly
  python -m app.ingest.fdf.adjust_fdf CZCE.FG --freq min15
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from app.ingest.fdf import symbols as SYM
from app.ingest.fdf import db_pg as D
from app.ingest.fdf import build_continuous as BC


def _load_continuous(spec, freq):
    """读未复权主连（KQ.m@<品种>）。"""
    df = D.load_bars(freq, "continuous", spec["tq_cont"])
    if df is None or len(df) == 0:
        raise RuntimeError(f"continuous 表读不到 {spec['tq_cont']}（{freq}），请先跑 fetch_fdf 入库")
    out = df.reset_index(drop=True)
    out["date"] = pd.to_datetime(out["date"])
    fmt = "%Y-%m-%d" if freq == "daily" else "%Y-%m-%d %H:%M:%S"
    out["date"] = out["date"].dt.strftime(fmt)
    return out[["date", "open", "high", "low", "close", "volume"]]


def _build_adjusted_hourly(frames, raw_main_df, reliable_oi=BC.RELIABLE_OI):
    """子日线（小时/15min）复权：与日线相同的加法平移前复权，主力判定按交易日聚合 OI。

    reliable_oi: 按品种的主持仓量门槛（低流动性品种用更低值，见 build_continuous.reliable_oi）。
    """
    oi = pd.DataFrame({s: f["oi"] for s, f in frames.items()}).sort_index()
    oi_daily = oi.groupby(oi.index.floor("D")).last()
    dom = BC.pick_dominant(oi_daily).dropna()

    recs = []
    for d, s in dom.items():
        f = frames.get(s)
        if f is None:
            continue
        mask = f.index.floor("D") == d
        for dt, r in f[mask].iterrows():
            recs.append({"date": dt, "symbol": s,
                         "open": r["open"], "high": r["high"], "low": r["low"], "close": r["close"],
                         "volume": (0 if pd.isna(r["volume"]) else r["volume"]),
                         "oi": (0 if pd.isna(r["oi"]) else r["oi"])})
    C = pd.DataFrame(recs).set_index("date").sort_index()
    if len(C) == 0:
        raise RuntimeError("小时线主力序列为空，无法复权")

    c_dates = set(C.index.floor("D").unique())
    rolls = []
    dom_items = list(dom.items())
    for k in range(1, len(dom_items)):
        d_t, s_t = dom_items[k]
        d_prev, s_prev = dom_items[k - 1]
        if s_t != s_prev:
            if d_prev not in c_dates or d_t not in c_dates:
                continue
            old_last = C[C.index.floor("D") == d_prev]["close"].iloc[-1]
            new_first = C[C.index.floor("D") == d_t]["close"].iloc[0]
            if pd.notna(old_last) and pd.notna(new_first):
                rolls.append({"date": pd.Timestamp(d_t), "old": s_prev, "new": s_t,
                              "delta": float(new_first - old_last)})
            else:
                print(f"  警告：{d_t.date()} 换月（{s_prev}→{s_t}）缺少对照收盘价，该点未做复权")
                rolls.append({"date": pd.Timestamp(d_t), "old": s_prev, "new": s_t, "delta": 0.0})

    adj = C.copy()
    for r in reversed(rolls):
        m = adj.index < r["date"]
        for c in ("open", "high", "low", "close"):
            adj.loc[m, c] = adj.loc[m, c] + r["delta"]

    if len(adj) > 21:
        ref = adj["volume"].iloc[-21:-1].median()
        while len(adj) and adj["volume"].iloc[-1] < ref * 0.5:
            adj = adj.iloc[:-1]
            C = C.iloc[:-1]

    ok = adj["oi"] >= reliable_oi
    roll_ok = ok.rolling(BC.RELIABLE_HOLD).sum()
    good = roll_ok[roll_ok >= BC.RELIABLE_HOLD]
    if len(good) == 0:
        # 用 RuntimeError 而非 SystemExit：SystemExit 属 BaseException，
        # 编排器的 except Exception 抓不住，会把整个 run_fdf 进程干掉（死循环）。
        raise RuntimeError("没有任何区间主力持仓量达标，无法复权")
    start = adj.index[adj.index.get_loc(good.index[0]) - BC.RELIABLE_HOLD + 1]
    adj = adj.loc[start:]

    # 前复权只差一个加法常数：把整段平移到权威主连 KQ.m@ 末端对齐，
    # 使复权末值与真实当前主力一致（不改变复权正确性，仅统一参考水平）。
    if raw_main_df is not None and len(raw_main_df) and len(adj):
        shift = float(raw_main_df["close"].iloc[-1]) - float(adj["close"].iloc[-1])
        if abs(shift) > 1e-6:
            for c in ("open", "high", "low", "close"):
                adj[c] = adj[c] + shift

    if raw_main_df is not None and len(raw_main_df) > 0:
        raw = raw_main_df.copy()
        raw["date"] = pd.to_datetime(raw["date"])
        raw = raw.set_index("date").sort_index()
        raw = raw[~raw.index.duplicated(keep="last")]
        adj["symbol"] = adj["symbol"].astype(str)
        raw_code = spec_tq_cont()

        if start in raw.index:
            head = raw.loc[:start].iloc[:-1]
            if len(head):
                shift = float(adj.loc[start, "close"] - raw.loc[start, "close"])
                head = head.copy()
                for c in ("open", "high", "low", "close"):
                    head[c] = head[c] + shift
                head["oi"] = 0
                head["symbol"] = f"{raw_code}(未复权)"
                adj = pd.concat([head[["open", "high", "low", "close", "volume", "oi", "symbol"]], adj])

        tail_end = adj.index[-1]
        if tail_end in raw.index:
            rest = raw.loc[tail_end:].iloc[1:]
            if len(rest):
                shift = float(adj.loc[tail_end, "close"] - rest["close"].iloc[0])
                rest = rest.copy()
                for c in ("open", "high", "low", "close"):
                    rest[c] = rest[c] + shift
                rest["oi"] = rest.get("oi", 0)
                rest["symbol"] = f"{raw_code}(未复权)"
                adj = pd.concat([adj, rest[["open", "high", "low", "close", "volume", "oi", "symbol"]]])

    out_rows = []
    for d, r in adj.iterrows():
        is_adj = not str(r["symbol"]).startswith("KQ.m@")
        out_rows.append({
            "date": d.strftime("%Y-%m-%d %H:%M:%S"),
            "open": round(float(r["open"]), 2), "high": round(float(r["high"]), 2),
            "low": round(float(r["low"]), 2), "close": round(float(r["close"]), 2),
            "volume": int(r["volume"]), "oi": int(r["oi"]),
            "adj": bool(is_adj),
        })
    out = pd.DataFrame(out_rows)
    n_adj = int(out["adj"].sum())
    print(f"  写入复权主连 {len(out)} 根，区间 {out['date'].iloc[0]} ~ {out['date'].iloc[-1]}")
    print(f"    其中已复权 {n_adj} 根（{out[out['adj']]['date'].iloc[0]} 起），"
          f"未复权 {len(out) - n_adj} 根")
    return out


_spec_tq = None


def spec_tq_cont():
    return _spec_tq


def run(symbol, freq="hourly"):
    D.ensure_schema([freq])
    spec = SYM.get(symbol)
    global _spec_tq
    _spec_tq = spec["tq_cont"]
    print(f"[复权 {freq}] 品种 {spec['name']}（{spec['key']}）")
    cont = _load_continuous(spec, freq)
    print(f"  未复权主连：{len(cont)} 根，{cont['date'].iloc[0]} ~ {cont['date'].iloc[-1]}")
    frames = D.load_contract_frames(freq, spec["exchange"], spec["code"], spec["code_digits"])
    if not frames:
        raise RuntimeError(f"contract 表读不到 {spec['key']} 的具体合约（{freq}），请先跑 fetch_fdf")
    print(f"  具体合约 frames：{len(frames)} 个合约，"
          f"覆盖 {min(f.index[0] for f in frames.values()).date()} ~ "
          f"{max(f.index[-1] for f in frames.values()).date()}")

    if freq == "daily":
        adj = BC.build_adjusted(frames=frames, raw_main_df=cont, product=spec["code"])
    else:
        adj = _build_adjusted_hourly(frames, cont, reliable_oi=BC.reliable_oi(spec["code"]))
    n = D.save_adjusted(freq, spec["tq_cont"], adj)
    print(f"  写入 cont_adj 表：{n} 根，复权方式=forward（加法平移，对齐原始版）")
    return adj


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="PostgreSQL 复权主连入库")
    ap.add_argument("symbol", help="品种 tq 代码，如 CZCE.FG")
    ap.add_argument("--freq", default="hourly", help="周期 hourly/min15，默认 hourly")
    args = ap.parse_args()
    run(args.symbol, args.freq)
    print("完成。")
