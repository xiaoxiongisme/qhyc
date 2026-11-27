#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CL-20261127-01：修复 A8888/AP8888 因 11-24 换月残段接缝冲突导致的停写。

背景（2026-11-27 实证）
-----------------------
``DATA-INDEX-8888-LAG`` block FAIL：A8888 停在 11-24 15:00、AP8888 停在 11-24 11:30，
落后合约层 >2 交易日；其余 25+ 个 8888 品种正常更新至 11-27，非源端缺数。

只读干跑 ``extend_index_8888.py --variety A --variety AP --dry-run``：
``seam_mismatch(spread=2.0)`` / ``seam_mismatch(spread=64.0)``，锚点 11-24。
逐日锚点扫描确认 11-24 是换月残段日（A 当日 225 行 < 正常 345，AP 135 < 225），
其残段用旧 front 基线写入；下一轮重算用新 front 残差为非常数 ⇒ 拒写并永久冻结。
11-23 是残差恒定的完整交易日（spread=0.0，A delta=-255 / AP delta=-3345）。

修法与 CL-20261121-01 同构，锚点改为 11-23、窗口终点改为 11-28：
取锚点日重合分钟求 delta；用锚点日 23:59 之后的真实合约数据重算；
先备份到 BAK 表，再 DELETE + COPY 写入该品种锚点日之后的所有 index 行。

⚠️ 一次性工具（CL-20261127-01）。锚点日写死在 ANCHORS，勿重复执行；
   后续同类修复须复制改窗口常量并另立 CL。

用法（生产容器内 cwd=/app）::

    python scripts/ops/repair_index_8888_roll_20261127.py
    python scripts/ops/repair_index_8888_roll_20261127.py --apply
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, datetime, time as dtime, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/scripts")
sys.path.insert(0, "/app/scripts/ops")

import extend_index_8888 as E  # noqa: E402
from load_local_csv_1min import fetch_front, synth_8888  # noqa: E402

SRC = "incr_synth_8888"
EPS = 1e-3
#: 换月前最后一个残差恒定的完整交易日（由 2026-11-27 逐日残差扫描确定）
ANCHORS = {
    "A": date(2026, 11, 23),
    "AP": date(2026, 11, 23),
}
WIN_END = pd.Timestamp("2026-11-28 00:00:00+08:00")
BAK = "public.bak_cl20261127_01_index"
STAGE = "public.stage_cl20261127_01_index"

COLS = ("symbol,ts,open,high,low,close,volume,amount,open_interest,high_limit,"
        "low_limit,pre_close,settle_price,contract,src,ts_end,layer,exchange,"
        "variety_code,caliber_vol,caliber_amt,amount_raw,raw_type")


def _ts(d: date, h: int = 0) -> pd.Timestamp:
    return pd.Timestamp(datetime.combine(d, dtime(h)).replace(tzinfo=None)).tz_localize(
        "Asia/Shanghai")


def _meta(cur, sym: str):
    cur.execute("""
        SELECT exchange, caliber_amt FROM l0_raw.minute_bar
        WHERE layer='index' AND symbol=%s ORDER BY ts DESC LIMIT 1
    """, (sym,))
    r = cur.fetchone()
    return (r[0], r[1]) if r else (None, "true")


def _syms(cur, v: str, t0, t1) -> list[str]:
    cur.execute("""
        SELECT DISTINCT symbol FROM l0_raw.minute_bar
        WHERE variety_code=%s AND layer IN ('contract','continuous')
          AND ts >= %s AND ts < %s
    """, (v, t0, t1))
    return [r[0] for r in cur.fetchall()]


def _bad_days(cur, v: str, syms, t0, t1, anchor: date):
    q = E._daily_quality(cur, syms, t0, t1)
    med = float(q["ncontract"].median()) if len(q) else 0.0
    mx = float(q["front_oi"].max()) if len(q) else 0.0
    bad, why = set(), []
    for r in q.itertuples():
        d = r.d.date() if hasattr(r.d, "date") else r.d
        if d <= anchor:
            continue
        if med and r.ncontract < med * E.CONTRACT_RATIO_MIN:
            bad.add(d)
            why.append(f"{d}(contracts={r.ncontract})")
        elif mx and (pd.isna(r.front_oi) or float(r.front_oi) < mx * E.FRONT_OI_RATIO_MIN):
            bad.add(d)
            why.append(f"{d}(front_oi={r.front_oi})")
    return bad, why


def _build_stage(cur, syms, t0, t1, bad):
    cur.execute("DROP TABLE IF EXISTS pg_temp.stage_dedup")
    params = [syms, t0, t1]
    extra = ""
    if bad:
        extra = " AND ts::date <> ALL(%s)"
        params.append(sorted(bad))
    cur.execute("CREATE TEMP TABLE stage_dedup AS "
                "SELECT symbol, ts, open, high, low, close, volume, amount, "
                "open_interest, variety_code, layer FROM l0_raw.minute_bar "
                "WHERE symbol = ANY(%s) AND ts >= %s AND ts < %s AND close IS NOT NULL"
                + extra, params)
    cur.execute("CREATE UNIQUE INDEX ON stage_dedup (symbol, ts)")
    cur.execute("ANALYZE stage_dedup")


def rebuild_one(cur, v: str, rows_out: list) -> dict:
    anchor = ANCHORS[v]
    sym = v + "8888"
    t0 = _ts(anchor - timedelta(days=1))
    t1 = WIN_END.to_pydatetime()
    exchange, amt = _meta(cur, sym)
    syms = _syms(cur, v, t0, t1)
    if not syms:
        return {"v": v, "status": "no_symbols", "rows": 0}

    bad, why = _bad_days(cur, v, syms, t0, t1, anchor)
    _build_stage(cur, syms, t0, t1, bad)

    front, carry = fetch_front(cur)
    if front is None or len(front) == 0:
        return {"v": v, "status": "no_front", "rows": 0}
    out = synth_8888(cur, exchange, v, amt, front=front, carry=carry)
    cur.execute("DROP TABLE IF EXISTS pg_temp.stage_dedup")
    cur.execute("DROP TABLE IF EXISTS pg_temp.stage_daily_front")
    if out is None or len(out) == 0:
        return {"v": v, "status": "no_out", "rows": 0}

    out = out.set_index("ts").sort_index()
    n_all = len(out)
    out = out.loc[out.index.intersection(front.index)]
    n_ffill = n_all - len(out)

    cur.execute("""
        SELECT ts, close FROM l0_raw.minute_bar
        WHERE layer='index' AND symbol=%s AND ts >= %s AND ts < %s
    """, (sym, _ts(anchor).to_pydatetime(), _ts(anchor + timedelta(days=1)).to_pydatetime()))
    stored = pd.DataFrame(cur.fetchall(), columns=["ts", "close"])
    if stored.empty:
        return {"v": v, "status": "anchor_empty", "rows": 0}
    stored["close"] = stored["close"].astype("float64")
    stored["ts"] = pd.to_datetime(stored["ts"], utc=True)
    stored = stored.set_index("ts")["close"]
    common = out.index.intersection(stored.index)
    if len(common) < 10:
        return {"v": v, "status": f"anchor_overlap_small({len(common)})", "rows": 0}
    resid = (out.loc[common, "close"] - stored.loc[common]).astype("float64")
    spread = float(resid.max() - resid.min())
    if spread > EPS:
        return {"v": v, "status": f"seam_mismatch({spread:.4f})", "rows": 0,
                "anchor_n": len(common)}
    delta = float(resid.median())

    cut = _ts(anchor, 23) + timedelta(minutes=59)
    new = out[out.index > cut].copy()
    if new.empty:
        return {"v": v, "status": "no_rows_after_anchor", "rows": 0}
    for c in ("open", "high", "low", "close"):
        new[c] = new[c] - delta
    new = new.dropna(subset=["close"])

    for ts, r in new.iterrows():
        rows_out.append((
            sym, ts.to_pydatetime(),
            float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
            int(r["volume"]) if pd.notna(r["volume"]) else 0,
            float(r["amount"]) if pd.notna(r["amount"]) else None,
            float(r["open_interest"]) if pd.notna(r["open_interest"]) else None,
            None, None, None, None,
            sym, SRC, ts.to_pydatetime(), "index",
            exchange, v, "single_side", amt, None, None,
        ))
    return {"v": v, "status": "ok", "rows": len(new),
            "anchor": str(anchor), "anchor_n": len(common),
            "spread": round(spread, 8), "delta": round(delta, 4),
            "range": f"{new.index.min()} .. {new.index.max()}",
            "ffill_drop": n_ffill, "bad_days": ";".join(why[:4])}


def _write(cur, rows: list) -> tuple[int, int, int]:
    cur.execute(f"DROP TABLE IF EXISTS {BAK}")
    cur.execute(f"CREATE TABLE {BAK} AS SELECT * FROM l0_raw.minute_bar "
                "WHERE false")
    syms = sorted({r[0] for r in rows})
    cut = datetime.combine(max(ANCHORS.values()) + timedelta(days=1), dtime()).replace(
        tzinfo=None)
    # 逐品种按各自锚点备份 + 删除，避免误删其它品种
    n_bak = n_del = 0
    for v, anchor in ANCHORS.items():
        since = datetime.combine(anchor + timedelta(days=1), dtime())
        cur.execute("INSERT INTO " + BAK + " SELECT * FROM l0_raw.minute_bar "
                    "WHERE layer='index' AND symbol=%s AND ts >= %s",
                    (v + "8888", since))
        n_bak += cur.rowcount
        cur.execute("DELETE FROM l0_raw.minute_bar "
                    "WHERE layer='index' AND symbol=%s AND ts >= %s",
                    (v + "8888", since))
        n_del += cur.rowcount

    cur.execute(f"DROP TABLE IF EXISTS {STAGE}")
    cur.execute(f"CREATE TABLE {STAGE} (LIKE l0_raw.minute_bar INCLUDING DEFAULTS)")
    with cur.copy(f"COPY {STAGE} ({COLS}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)
    cur.execute(f"INSERT INTO l0_raw.minute_bar ({COLS}) SELECT {COLS} FROM {STAGE} "
                "ON CONFLICT (symbol, ts) DO NOTHING")
    return n_bak, n_del, cur.rowcount


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    conn = E._connect()
    conn.autocommit = True
    cur = conn.cursor()
    print(f"[repair] apply={a.apply} 窗口终点 {WIN_END} 锚点 {ANCHORS}", flush=True)
    rows: list = []
    t0 = time.time()
    for v in ANCHORS:
        r = rebuild_one(cur, v, rows)
        print(f"[repair] {v:3s} {r}", flush=True)
        if r["status"] != "ok":
            rows = [x for x in rows if x[0] != v + "8888"]
    print(f"[repair] 汇总 待写 {len(rows)} 行 耗时 {time.time()-t0:.1f}s", flush=True)
    if not a.apply:
        print("[repair] 干跑结束（--apply 才写库）", flush=True)
        conn.close()
        return 0
    if not rows:
        print("[repair] ABORT：无可写行", flush=True)
        conn.close()
        return 2
    n_bak, n_del, n_ins = _write(cur, rows)
    print(f"[repair] 备份 {BAK}={n_bak} 行；删除旧行={n_del}；写入={n_ins}", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
