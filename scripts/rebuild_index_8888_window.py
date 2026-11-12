#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CL-20261107-11：窗口化重建 8888 指数层的历史洞。

背景
----
2026-11-02 / 11-03 contract 层残缺（256/564 个合约 vs 正常 ~686），导致
``extend_index_8888.py`` 的日级门把这两天判为坏日跳过，8888 指数层在
11-02 只有 25 syms / 2308 行、11-03 只有 65 syms / 9772 行（健康日约
79 syms / 22386~25402 行），10-30 夜盘同样缺 120 分钟。

本轮已用 tqsdk 把 contract 层补齐（337960 行），但 ``extend_index_8888.py``
是**增量续跑**（只写 ``ts > last_ts``），永远不会回填历史洞；而
``repair_index_layer.py`` 会删光全部 ``incr_synth_8888``，删完后 82 个品种
的锚点退回 10-30（距今 >7 天），会被 ``DORMANT_DAYS`` 全部判休眠、一行不写，
另有 7 个品种（RB/NI/RU/SS/SP/PB/SN）锚点只到 10-23，直接永久失数据。
所以这里做**窗口化重建**：不改增量链路，只修历史窗口。

做法
----
1. 逐品种在 [10-29, 11-07) 窗口里重建 ``stage_dedup``，复用
   ``fetch_front`` / ``synth_8888``，并按 ``front.index`` 硬裁剪（禁 ffill 伪造）；
2. 用**缺口窗口之外**的重合分钟（10-30 15:00 之前 + 11-04 之后）求常数
   ``delta`` 做水平对齐；残差 spread > EPS 视为该品种接缝不一致，跳过并点名；
3. 只取 [10-30 15:01, 11-04 00:00) 的行 UPSERT 进 ``l0_raw.minute_bar``，
   ``src='incr_synth_8888'``，与增量链路同口径；
4. 默认干跑；``--apply`` 前自动备份被覆盖窗口的旧行到
   ``public.bak_cl20261107_11_index``。

用法（生产容器内，cwd=/app）::

    python scripts/rebuild_index_8888_window.py                    # 全品种干跑
    python scripts/rebuild_index_8888_window.py --variety RB --variety FU
    python scripts/rebuild_index_8888_window.py --apply            # 实际写库

⚠️ 一次性工具（CL-20261107-11，2026-11-12 由 CL-20261112-03 补入库）：
窗口写死在 2026-10-29..11-07，只为补齐 11-02/11-03 contract 层残缺造成的
8888 指数层历史洞。**勿重复执行**；后续同类修复须复制改窗口常量并另立 CL。
默认干跑，``--apply`` 前自动备份被覆盖窗口到 ``public.bak_cl20261107_11_index``；
口径内品种接缝不一致时整体 ABORT，绝不写一条错的指数连污染换月判据。
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/scripts")
sys.path.insert(0, "/app/scripts/ops")

import extend_index_8888 as E  # noqa: E402
from load_local_csv_1min import fetch_front, synth_8888  # noqa: E402

WIN_START = pd.Timestamp("2026-10-29 00:00:00+08")
WIN_END = pd.Timestamp("2026-11-07 00:00:00+08")
FIX_START = pd.Timestamp("2026-10-30 15:01:00+08")
FIX_END = pd.Timestamp("2026-11-04 00:00:00+08")
EPS = 1e-3
CONTRACT_RATIO_MIN = E.CONTRACT_RATIO_MIN
FRONT_OI_RATIO_MIN = E.FRONT_OI_RATIO_MIN
BAK = "public.bak_cl20261107_11_index"
STAGE = "public.stage_cl20261107_11_index"

COLS = ("symbol,ts,open,high,low,close,volume,amount,open_interest,high_limit,"
        "low_limit,pre_close,settle_price,contract,src,ts_end,layer,exchange,"
        "variety_code,caliber_vol,caliber_amt,amount_raw,raw_type")


def _variety_meta(cur, sym: str):
    cur.execute("""
        SELECT exchange, caliber_amt FROM l0_raw.minute_bar
        WHERE layer='index' AND symbol=%s AND ts >= %s
        ORDER BY ts DESC LIMIT 1
    """, (sym, WIN_START.to_pydatetime()))
    r = cur.fetchone()
    return (r[0], r[1]) if r else (None, "true")


def _stage_symbols(cur, v: str) -> list[str]:
    cur.execute("""
        SELECT DISTINCT symbol FROM l0_raw.minute_bar
        WHERE variety_code = %s AND layer IN ('contract','continuous')
          AND ts >= %s AND ts < %s
    """, (v, WIN_START.to_pydatetime(), WIN_END.to_pydatetime()))
    return [r[0] for r in cur.fetchall()]


def _bad_days(cur, syms, have_check: bool = True) -> tuple[set, list[str]]:
    """复刻 extend_one 的日级门：合约数 / front OI / 整天无行。"""
    q = E._daily_quality(cur, syms, WIN_START.to_pydatetime(),
                         WIN_END.to_pydatetime())
    med_ctr = float(q["ncontract"].median()) if len(q) else 0.0
    max_oi = float(q["front_oi"].max()) if len(q) else 0.0
    bad: set = set()
    why: list[str] = []
    for r in q.itertuples():
        if med_ctr and r.ncontract < med_ctr * CONTRACT_RATIO_MIN:
            bad.add(r.d)
            why.append(f"{r.d}(contracts={r.ncontract}<{med_ctr*CONTRACT_RATIO_MIN:.0f})")
        elif max_oi and (pd.isna(r.front_oi)
                         or float(r.front_oi) < max_oi * FRONT_OI_RATIO_MIN):
            bad.add(r.d)
            why.append(f"{r.d}(front_oi={r.front_oi})")
    if have_check:
        have = {r.d for r in q.itertuples()}
        try:
            for ms in E._missing_trading_days(cur, WIN_START.date(),
                                              (WIN_END - timedelta(days=1)).date(),
                                              have):
                bad.add(pd.Timestamp(ms).date())
                why.append(f"{ms}(no_contract_rows)")
        except Exception as e:  # noqa: BLE001
            why.append(f"calendar_fail:{e}")
    return bad, why


def _build_stage(cur, syms, bad: set):
    cur.execute("DROP TABLE IF EXISTS pg_temp.stage_dedup")
    extra = ""
    params = [syms, WIN_START.to_pydatetime(), WIN_END.to_pydatetime()]
    if bad:
        extra = " AND ts::date <> ALL(%s)"
        params.append(sorted(bad))
    cur.execute(
        "CREATE TEMP TABLE stage_dedup AS "
        "SELECT symbol, ts, open, high, low, close, volume, amount, open_interest, "
        "       variety_code, layer "
        "FROM l0_raw.minute_bar "
        "WHERE symbol = ANY(%s) AND ts >= %s AND ts < %s AND close IS NOT NULL"
        + extra, params)
    cur.execute("CREATE UNIQUE INDEX ON stage_dedup (symbol, ts)")
    cur.execute("ANALYZE stage_dedup")


def _stored(cur, sym: str) -> pd.Series:
    cur.execute("""
        SELECT ts, close FROM l0_raw.minute_bar
        WHERE layer='index' AND symbol=%s AND ts >= %s AND ts < %s
    """, (sym, WIN_START.to_pydatetime(), WIN_END.to_pydatetime()))
    df = pd.DataFrame(cur.fetchall(), columns=["ts", "close"])
    if df.empty:
        return pd.Series(dtype="float64")
    df["close"] = df["close"].astype("float64")
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df.set_index("ts")["close"]


def rebuild_one(cur, v: str, rows_out: list) -> dict:
    sym = v + "8888"
    exchange, amt = _variety_meta(cur, sym)
    syms = _stage_symbols(cur, v)
    if not syms:
        return {"v": v, "status": "no_symbols", "rows": 0}
    bad, why = _bad_days(cur, syms)
    _build_stage(cur, syms, bad)

    front, carry = fetch_front(cur)
    if front is None or len(front) == 0:
        return {"v": v, "status": "no_front", "rows": 0}
    out = synth_8888(cur, exchange, v, amt, front=front, carry=carry)
    cur.execute("DROP TABLE IF EXISTS pg_temp.stage_dedup")
    cur.execute("DROP TABLE IF EXISTS pg_temp.stage_daily_front")
    if out is None or len(out) == 0:
        return {"v": v, "status": "no_out", "rows": 0}
    out = out.set_index("ts").sort_index()
    n_fill = len(out)
    out = out.loc[out.index.intersection(front.index)]
    n_fill -= len(out)

    stored = _stored(cur, sym)
    common = out.index.intersection(stored.index)
    # 只用缺口窗口之外的重合分钟定水平，缺口内的旧行本身可能就是错的
    align = common[(common < FIX_START) | (common >= FIX_END)]
    if len(align) < 10:
        return {"v": v, "status": f"align_overlap_too_small({len(align)})", "rows": 0}
    resid = (out.loc[align, "close"] - stored.loc[align]).astype("float64")
    spread = float(resid.max() - resid.min())
    if spread > EPS:
        return {"v": v, "status": f"seam_mismatch({spread:.4f})", "rows": 0,
                "align_n": len(align), "delta": float(resid.median())}
    delta = float(resid.median())

    new = out[(out.index >= FIX_START) & (out.index < FIX_END)].copy()
    if new.empty:
        return {"v": v, "status": "no_rows_in_fix_window", "rows": 0,
                "delta": round(delta, 4), "align_n": len(align)}
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
            sym, "incr_synth_8888", ts.to_pydatetime(), "index",
            exchange, v, "single_side", amt, None, None,
        ))
    return {"v": v, "status": "ok", "rows": len(new),
            "ffill_dropped": n_fill, "align_n": len(align),
            "spread": round(spread, 8), "delta": round(delta, 4),
            "range": f"{new.index.min()} .. {new.index.max()}",
            "bad_days": ";".join(why[:4])}


def _backup_and_upsert(cur, rows: list) -> tuple[int, int]:
    cur.execute(f"DROP TABLE IF EXISTS {BAK}")
    cur.execute(
        f"CREATE TABLE {BAK} AS SELECT * FROM l0_raw.minute_bar "
        "WHERE layer='index' AND ts >= %s AND ts < %s",
        (FIX_START.to_pydatetime(), FIX_END.to_pydatetime()))
    cur.execute(f"SELECT count(*) FROM {BAK}")
    n_bak = cur.fetchone()[0]

    cur.execute(f"DROP TABLE IF EXISTS {STAGE}")
    cur.execute(f"CREATE TABLE {STAGE} (LIKE l0_raw.minute_bar INCLUDING DEFAULTS)")
    with cur.copy(f"COPY {STAGE} ({COLS}) FROM STDIN") as cp:
        for r in rows:
            cp.write_row(r)

    cur.execute(f"""
        INSERT INTO l0_raw.minute_bar ({COLS})
        SELECT {COLS} FROM {STAGE}
        ON CONFLICT (symbol, ts) DO UPDATE SET
            open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
            close=EXCLUDED.close, volume=EXCLUDED.volume,
            amount=EXCLUDED.amount, open_interest=EXCLUDED.open_interest,
            contract=EXCLUDED.contract, src=EXCLUDED.src,
            ts_end=EXCLUDED.ts_end, layer=EXCLUDED.layer,
            exchange=EXCLUDED.exchange, variety_code=EXCLUDED.variety_code,
            caliber_vol=EXCLUDED.caliber_vol, caliber_amt=EXCLUDED.caliber_amt
        WHERE l0_raw.minute_bar.layer='index'
    """)
    return n_bak, cur.rowcount


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variety", action="append", default=None)
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    conn = E._connect()
    conn.autocommit = True
    cur = conn.cursor()
    target = a.variety or E._varieties(cur)
    print(f"[rebuild] 目标 {len(target)} 品种 / apply={a.apply} / "
          f"窗口 {FIX_START} .. {FIX_END}", flush=True)

    rows: list = []
    stat: dict[str, int] = {}
    problems = []
    t0 = time.time()
    for i, v in enumerate(target, 1):
        try:
            r = rebuild_one(cur, v, rows)
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            print(f"[rebuild] {v} EXCEPTION {e}", flush=True)
            r = {"v": v, "status": "exception", "rows": 0}
        st = r["status"].split("(")[0]
        stat[st] = stat.get(st, 0) + 1
        if st != "ok":
            problems.append((v, r["status"]))
        if st in ("exception", "seam_mismatch", "align_overlap_too_small"):
            # 失败品种不能有半成品行进入待写集合
            rows = [x for x in rows if x[0] != v + "8888"]
        if r.get("rows"):
            print(f"[rebuild] {i:3d}/{len(target)} {v:4s} rows={r['rows']:5d} "
                  f"delta={r.get('delta')} spread={r.get('spread')} "
                  f"align={r.get('align_n')} ffill_drop={r.get('ffill_dropped')}",
                  flush=True)

    print(f"[rebuild] 汇总 {stat}；待写 {len(rows)} 行；耗时 {time.time()-t0:.1f}s",
          flush=True)
    if problems:
        print("[rebuild] 非 ok 品种：" + ", ".join(f"{v}:{s}" for v, s in problems),
              flush=True)

    if not a.apply:
        print("[rebuild] 干跑结束（--apply 才写库）", flush=True)
        conn.close()
        return 0

    # 只要**口径内**品种出现接缝不一致就整体拒绝写入，避免写一条错的指数连
    # 污染换月判据；口径外老品种（FB/WR/PB/T…）允许跳过并点名挂账。
    cur.execute("SELECT symbol FROM l3_ref.v_tradable_scope WHERE symbol LIKE '%8888'")
    scope = {r[0] for r in cur.fetchall()}
    bad_seam = [v for v, s in problems if s.startswith(("seam_mismatch",
                                                        "align_overlap_too_small"))]
    bad_in_scope = [v for v in bad_seam if v + "8888" in scope]
    if bad_in_scope:
        print(f"[rebuild] ABORT：口径内 {len(bad_in_scope)} 个品种接缝不一致 "
              f"{bad_in_scope}，拒绝写入", flush=True)
        conn.close()
        return 2
    if bad_seam:
        print(f"[rebuild] 跳过口径外接缝不一致 {len(bad_seam)} 个品种："
              + ", ".join(bad_seam), flush=True)
    if not rows:
        print("[rebuild] ABORT：无可写行", flush=True)
        conn.close()
        return 2

    n_bak, n_ups = _backup_and_upsert(cur, rows)
    print(f"[rebuild] 备份 {BAK} = {n_bak} 行；UPSERT 影响 {n_ups} 行", flush=True)
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
