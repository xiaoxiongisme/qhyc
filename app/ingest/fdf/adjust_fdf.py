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


def _frames_stale(frames, threshold_days=400):
    """contract 帧是否过旧（最新一根距今天数超过阈值）。

    免费档取不到近期合约时，某品种逐合约帧只覆盖历史某段（如橡胶 RU、或其他低活跃/远月品种），
    据此复权只得到一段废数据；此时应回退到连续主连兜底（见决策 6：本兜底对所有品种通用，不限于 RU）。
    """
    if not frames:
        return True
    latest = max(f.index[-1] for f in frames.values())
    now = pd.Timestamp.now("UTC") if latest.tzinfo else pd.Timestamp.now()
    return (now - latest).days > threshold_days


def _clear_adjusted(freq, symbol):
    """兜底写入前，清掉该 symbol 已有 cont_adj，避免新旧两段时间共存。"""
    from app.core.db import get_engine
    from sqlalchemy import text
    eng = get_engine()
    with eng.begin() as c:
        c.execute(text(
            "DELETE FROM fut_kline WHERE freq=:f AND kind='cont_adj' AND symbol=:s"
        ), {"f": freq, "s": symbol})


def _fallback_from_continuous(cont, raw_code):
    """兜底：无逐合约帧时，直接用连续主连作为复权主连（通用机制，决策 6）。

    场景：免费档 tqsdk 取不到该品种近期逐合约历史（任何商品期货都可能，如 SHFE.ru 橡胶
    仅是其中一例），无法做换月复权。天勤连续主连 KQ.m@ 本身已是前复权主连，直接落库即可
    （oi 置 0，全部标记为未复权区间）。本兜底对所有品种通用，不限 RU。
    """
    rows = []
    for _, r in cont.iterrows():
        rows.append({
            "date": r["date"],
            "open": round(float(r["open"]), 2),
            "high": round(float(r["high"]), 2),
            "low": round(float(r["low"]), 2),
            "close": round(float(r["close"]), 2),
            "volume": int(r["volume"]) if not pd.isna(r["volume"]) else 0,
            "oi": 0,
            "adj": False,
        })
    out = pd.DataFrame(rows)
    print(f"  [兜底] 连续主连 {raw_code} 直接作为复权主连：{len(out)} 根（未做换月平移）")
    return out


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
    if _frames_stale(frames, 400):
        # 兜底（通用，决策 6）：免费档取不到该品种近期逐合约历史（如 SHFE.ru，亦适用于其他
        # 低活跃/远月品种），帧只覆盖陈旧段，据此复权只得到一段废数据。
        # 直接用连续主连作为复权主连（天勤连续主连本身已前复权），与逐合约复权无缝共存于 cont_adj。
        print(f"  [兜底] {spec['key']} 逐合约帧缺失/过旧（免费档取不到），用连续主连 {spec['tq_cont']} 直接作为复权主连")
        _clear_adjusted(freq, spec["tq_cont"])
        adj = _fallback_from_continuous(cont, spec["tq_cont"])
        n = D.save_adjusted(freq, spec["tq_cont"], adj)
        print(f"  写入 cont_adj 表：{n} 根（兜底，未做换月平移）")
        return adj
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
