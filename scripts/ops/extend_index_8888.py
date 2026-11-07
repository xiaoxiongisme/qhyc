#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 XXXX8888 指数层（``l0_raw.minute_bar`` ``layer='index'``）接进增量链路。

背景（CL-20261107-05 / -11）
---------------------------
8888 指数连原来**只**由 ``scripts/load_local_csv_1min.py`` 在全量 CSV 导入时合成，
源 ``local_csv_synth_8888`` 止于 2026-10-30 15:15。之后增量采集只采 ``*888``（主连），
没有任何环节再产出 8888 ⇒ L1 ``bar_15m`` 里的 8888 停更 ⇒
``sp_build_l2_roll_segment`` 的 ratio 门 ``coalesce(idx_delta,1e9) < 0.40*|cc|``
因 ``idx_delta`` 为 NULL 而**恒不成立** ⇒ 换月段静默不再追加
（实测 ``l2_adj.roll_segment`` 的 ``max(seg_start)`` 卡在 2026-10-30 15:00，
82 个品种全部如此）⇒ 10-30 之后发生的换月没有任何 offset ⇒ 后复权价格在换月处
保留原始跳空。

这是典型的**静默失效**：没有报错、没有告警，只是「换月检测从此不再触发」。

做法
----
复用全量导入脚本里已经调好的同一套算法（``fetch_front`` / ``synth_8888``），
只把数据源从「本地 CSV → stage_dedup」换成「L0 合约层 → stage_dedup」：

  1. 对每个品种建临时 ``stage_dedup``（该品种窗口内的 contract + continuous 行）；
  2. ``fetch_front`` 取「每交易日日末持仓量最大的合约」+ 换月 carry；
  3. ``synth_8888`` 合成 ``front - cumsum(carry)``，并按主连时间轴 reindex + ffill；
  4. **接缝对齐**：局部窗口算出的 cum 起点是 0，而库里已有的 8888 带着历史累计 cum。
     两者相差一个常数 ``delta``，可用「最后一个已覆盖交易日」上
     ``recon_close - stored_close`` 求得 —— 只要该日 front 选对，这个残差在当日
     **每分钟都相同**；残差不是常数即说明 front 选错（例如当日合约层缺主力），
     此时**拒绝写入**而不是写一条错的指数连。
  5. 只写 ``ts > 已有最大 ts`` 的行（幂等，可反复跑）。

为什么必须这么小心
------------------
8888 唯一的消费方是换月检测的 ratio 门。写错一条指数连，等于让换月 offset 全部错位，
后复权价、回测、实盘信号全部连带失真。所以本脚本对「合约层不完整」的日子
采取**跳过 + 大声记录**，绝不猜：

  * 当日该品种合约数 < 窗口内中位数的 60%  → 跳过该日
  * 当日 front 的日末持仓量 < 窗口内最大值 10% → 跳过该日（主力缺失时的典型特征：
    只剩远月小额持仓，如 RB 在 2026-11-02 只剩 OI=1724 的 RB2706）

已实测的这类日子：2026-11-02（全库 237 个合约，正常 708）、11-03（548）。
它们需要先从源端补齐合约分钟，本脚本不做假。

用法
----
    python scripts/ops/extend_index_8888.py --dry-run          # 只报告，不写库
    python scripts/ops/extend_index_8888.py                    # 全品种增量
    python scripts/ops/extend_index_8888.py --variety RB --variety I
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timedelta
from typing import Sequence

import numpy as np
import pandas as pd

sys.path.insert(0, "/app")
sys.path.insert(0, "./scripts")

logger = logging.getLogger("extend_index_8888")

SRC = "incr_synth_8888"
#: 接缝残差容差（价格单位）。真值应为 0；给浮点留 1e-6。
SEAM_EPS = 1e-6
#: 合约数门：当日合约数低于窗口内中位数的这个比例 → 视为合约层不完整
CONTRACT_RATIO_MIN = 0.6
#: 持仓量门：当日 front 日末 OI 低于窗口内最大值的这个比例 → 视为主力缺失
FRONT_OI_RATIO_MIN = 0.10


def _connect():
    from load_local_csv_1min import _load_db_creds  # noqa: E402
    import psycopg  # noqa: E402

    return psycopg.connect(**_load_db_creds())


def _varieties(cur) -> list[str]:
    cur.execute(r"""
        SELECT DISTINCT regexp_replace(symbol, '8888$', '') AS v
        FROM l0_raw.minute_bar
        WHERE layer = 'index' AND symbol ~ '^[A-Za-z]+8888$'
        ORDER BY 1
    """)
    return [r[0] for r in cur.fetchall()]


def _last_index(cur, variety: str):
    cur.execute("""
        SELECT ts, close, exchange, caliber_amt
        FROM l0_raw.minute_bar
        WHERE layer = 'index' AND symbol = %s
        ORDER BY ts DESC LIMIT 1
    """, (variety + "8888",))
    return cur.fetchone()


def _daily_quality(cur, syms: Sequence[str], t0: datetime, t1: datetime) -> pd.DataFrame:
    """每（日历日）合约数与 front 日末 OI，用于「合约层是否完整」的判定。"""
    cur.execute("""
        WITH last_oi AS (
            SELECT DISTINCT ON (symbol, ts::date)
                   symbol, ts::date AS d, open_interest AS oi
            FROM l0_raw.minute_bar
            WHERE layer = 'contract' AND symbol = ANY(%s)
              AND ts >= %s AND ts < %s AND open_interest > 0
            ORDER BY symbol, ts::date, ts DESC
        ), dom AS (
            SELECT DISTINCT ON (d) d, symbol AS front, oi
            FROM last_oi ORDER BY d, oi DESC, symbol DESC
        ), cnt AS (
            SELECT ts::date AS d, count(DISTINCT symbol) AS ncontract
            FROM l0_raw.minute_bar
            WHERE layer = 'contract' AND symbol = ANY(%s) AND ts >= %s AND ts < %s
            GROUP BY 1
        )
        SELECT cnt.d, cnt.ncontract, dom.front, dom.oi
        FROM cnt LEFT JOIN dom USING (d) ORDER BY cnt.d
    """, (list(syms), t0, t1, list(syms), t0, t1))
    rows = cur.fetchall()
    return pd.DataFrame(rows, columns=["d", "ncontract", "front", "front_oi"])


def extend_one(cur, variety: str, dry_run: bool) -> dict:
    """返回 {'status': ..., 'rows': n, 'skipped_days': [...]}。"""
    last = _last_index(cur, variety)
    if last is None:
        return {"variety": variety, "status": "no_index_series", "rows": 0}
    last_ts, last_close, exchange, amt_caliber = last

    # 窗口：从「最后一个已覆盖交易日的前 1 天」开始，保证接缝日能被完整重建
    t0 = datetime.combine((last_ts - timedelta(days=1)).date(), datetime.min.time()).replace(
        tzinfo=last_ts.tzinfo)
    cur.execute("SELECT max(ts) FROM l0_raw.minute_bar WHERE layer='contract' AND ts >= %s",
                (t0,))
    t_max = cur.fetchone()[0]
    if t_max is None or t_max <= last_ts:
        return {"variety": variety, "status": "no_new_contract_data", "rows": 0}
    t1 = t_max + timedelta(minutes=1)

    cur.execute("""
        SELECT DISTINCT symbol FROM l0_raw.minute_bar
        WHERE variety_code = %s AND layer IN ('contract', 'continuous')
          AND ts >= %s AND ts < %s
    """, (variety, t0, t1))
    syms = [r[0] for r in cur.fetchall()]
    if not syms:
        return {"variety": variety, "status": "no_contract_symbols", "rows": 0}

    # ---- 1) 先做「合约层是否完整」的日级体检，**坏日必须挡在 stage 之外** ----
    #
    # 为什么要在建 stage 之前判：坏日若只是「产出后丢弃」，它仍会参与
    # fetch_front 的「日末持仓量选主力」。RB 实测 2026-11-02 只剩 OI=1732 的
    # RB2706（全库当日 237 个合约 vs 正常 708）→ 主力被选错 → 合成出
    # 2701→2706→2701 两次伪换月，carry 被灌进 cum，**11-03 之后整条指数被平移**。
    # 不参与选主力，才不会污染后面的日子。
    q = _daily_quality(cur, syms, t0, t1)
    med_ctr = float(q["ncontract"].median()) if len(q) else 0.0
    max_oi = float(q["front_oi"].max()) if len(q) else 0.0
    bad_days: list[str] = []
    bad_objs: set = set()
    for r in q.itertuples():
        d = r.d
        if d <= last_ts.date():
            continue
        why = None
        if med_ctr and r.ncontract < med_ctr * CONTRACT_RATIO_MIN:
            why = f"contracts={r.ncontract}<{med_ctr * CONTRACT_RATIO_MIN:.0f}"
        elif max_oi and (pd.isna(r.front_oi) or float(r.front_oi) < max_oi * FRONT_OI_RATIO_MIN):
            why = f"front_oi={r.front_oi} < {max_oi * FRONT_OI_RATIO_MIN:.0f}"
        if why:
            bad_days.append(f"{d}({why})")
            bad_objs.add(d)

    # ---- 2) 临时 stage_dedup（与全量导入脚本同名同列，直接复用其算法）----
    cur.execute("DROP TABLE IF EXISTS pg_temp.stage_dedup")
    # ⚠ 不要用 `AND NOT (ts::date = ANY(%s))` 配空列表：`= ANY(ARRAY[NULL])` 得到 NULL，
    #   `NOT NULL` 仍是 NULL → 所有行都被滤掉，stage 直接空表。空列表时必须整段不加条件。
    extra = ""
    params = [syms, t0, t1]
    if bad_objs:
        extra = " AND ts::date <> ALL(%s)"
        params.append(sorted(bad_objs))
    cur.execute("CREATE TEMP TABLE stage_dedup AS "
                "SELECT symbol, ts, open, high, low, close, volume, amount, open_interest, "
                "       variety_code, layer "
                "FROM l0_raw.minute_bar "
                "WHERE symbol = ANY(%s) AND ts >= %s AND ts < %s AND close IS NOT NULL"
                + extra, params)
    cur.execute("CREATE UNIQUE INDEX ON stage_dedup (symbol, ts)")
    cur.execute("ANALYZE stage_dedup")

    from load_local_csv_1min import fetch_front, synth_8888  # noqa: E402

    front, carry = fetch_front(cur)
    if front is None or len(front) == 0:
        cur.execute("DROP TABLE IF EXISTS pg_temp.stage_dedup")
        return {"variety": variety, "status": "no_front", "rows": 0}

    out = synth_8888(cur, exchange, variety, amt_caliber, front=front, carry=carry)
    cur.execute("DROP TABLE IF EXISTS pg_temp.stage_dedup")
    cur.execute("DROP TABLE IF EXISTS pg_temp.stage_daily_front")
    if out is None or len(out) == 0:
        return {"variety": variety, "status": "no_out", "rows": 0}

    out = out.set_index("ts").sort_index()

    # ---- 2) 接缝对齐：用「最后一个已覆盖交易日」求常数 delta ----
    seam_day = last_ts.date()
    cur.execute("SELECT ts, close FROM l0_raw.minute_bar "
                "WHERE layer='index' AND symbol=%s AND ts::date=%s",
                (variety + "8888", seam_day))
    stored = pd.DataFrame(cur.fetchall(), columns=["ts", "close"])
    # numeric 列经 psycopg 回来是 Decimal，直接与 float 相减会 TypeError
    stored["close"] = stored["close"].astype("float64")
    if stored.empty:
        return {"variety": variety, "status": "seam_day_empty", "rows": 0}
    stored["ts"] = pd.to_datetime(stored["ts"], utc=True)
    stored = stored.set_index("ts")["close"]
    common = out.index.intersection(stored.index)
    if len(common) < 10:
        return {"variety": variety, "status": f"seam_overlap_too_small({len(common)})", "rows": 0}
    resid = (out.loc[common, "close"] - stored.loc[common]).astype("float64")
    spread = float(resid.max() - resid.min())
    if spread > SEAM_EPS:
        return {"variety": variety, "status": f"seam_mismatch(spread={spread:.6f})", "rows": 0}
    delta = float(resid.median())

    # ---- 3) 只取新增行（坏日已在 stage 层剔除，不会出现在 out 里）----
    new = out[out.index > last_ts].copy()
    if new.empty:
        return {"variety": variety, "status": "up_to_date", "rows": 0,
                "skipped_days": bad_days}
    new["open"] = new["open"] - delta
    new["high"] = new["high"] - delta
    new["low"] = new["low"] - delta
    new["close"] = new["close"] - delta
    new = new.dropna(subset=["close"])
    if new.empty:
        return {"variety": variety, "status": "all_days_skipped", "rows": 0,
                "skipped_days": bad_days}

    if dry_run:
        return {"variety": variety, "status": "dry-run", "rows": len(new),
                "skipped_days": bad_days,
                "range": f"{new.index.min()} .. {new.index.max()}",
                "delta": round(delta, 4)}

    rows = []
    for ts, r in new.iterrows():
        rows.append((
            variety + "8888", ts.to_pydatetime(),
            float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]),
            int(r["volume"]) if pd.notna(r["volume"]) else 0,
            float(r["amount"]) if pd.notna(r["amount"]) else None,
            float(r["open_interest"]) if pd.notna(r["open_interest"]) else None,
            None, None, None, None,
            variety + "8888", SRC, ts.to_pydatetime(), "index",
            exchange, variety, "single_side", amt_caliber, None, None,
        ))
    cols = ("symbol,ts,open,high,low,close,volume,amount,open_interest,high_limit,"
            "low_limit,pre_close,settle_price,contract,src,ts_end,layer,exchange,"
            "variety_code,caliber_vol,caliber_amt,amount_raw,raw_type")
    ph = ",".join(["%s"] * 23)
    with cur.copy(f"COPY l0_raw.minute_bar ({cols}) FROM STDIN", ) as cp:
        for r in rows:
            cp.write_row(r)
    return {"variety": variety, "status": "written", "rows": len(rows),
            "skipped_days": bad_days, "delta": round(delta, 4)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variety", action="append", default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")

    conn = _connect()
    conn.autocommit = True
    cur = conn.cursor()
    target = a.variety or _varieties(cur)
    logger.info(f"[8888] 目标 {len(target)} 个品种，干跑={a.dry_run}")

    stat: dict[str, int] = {}
    written = 0
    problems = []
    for v in target:
        try:
            r = extend_one(cur, v, a.dry_run)
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            logger.exception(f"[8888] {v} 失败：{e}")
            problems.append((v, f"exception:{e}"))
            continue
        st = r["status"]
        stat[st] = stat.get(st, 0) + 1
        if st in ("written", "dry-run"):
            written += r["rows"]
        if r.get("skipped_days"):
            problems.append((v, "skip:" + ",".join(r["skipped_days"])))
        if st != "up_to_date":
            logger.info(f"[8888] {v}: {r}")

    logger.info(f"[8888] 汇总 {stat}；新增 {written} 行")
    for v, msg in problems:
        logger.warning(f"[8888] ⚠ {v} {msg}")
    bad = [s for s in stat if s.startswith(("seam_mismatch", "seam_day_empty",
                                           "seam_overlap_too_small"))]
    if bad:
        logger.error(f"[8888] 接缝校验失败 {bad} —— 这些品种的合约层在 {stat} 对应的日子"
                     f"可能不含主力，未写入任何数据")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
