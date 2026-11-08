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
  3. ``synth_8888`` 合成 ``front - cumsum(carry)``；它内部会 ``reindex(anchor).ffill()``
     到主连时间轴，**返回后必须再按 ``front.index`` 裁掉 ffill 出来的行** ——
     否则合约层缺数据的日子会被填成常数平线，把换月 ratio 门变成恒真判据
     （实测 2026-11-02 伪造 344 行平线 ⇒ FU 的周末跳空 -146 被判成换月）；
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
from datetime import date, datetime, timedelta
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


def _missing_trading_days(cur, d0, d1, have: set) -> list[str]:
    """窗口内「开市但该品种一行合约分钟都没有」的交易日。

    「坏日」（有行但残缺）由 :func:`_daily_quality` 的两个门拦下；这里补的是另一类：
    **整天一行都没有**，它压根不会出现在 ``q`` 里，因此两个门都拦不住它。
    实测 2026-11-02 的 FU 就是这一类（合约层 0 行，连续层却有 CSV 遗留的 bar），
    ``synth_8888`` 于是把它 ffill 成平线。必须单独点名，否则只是「静默留洞」。

    ⚠ 日历真源（CL-20261107-18，2026-11-08 修正）
    ----------------------------------------------
    原实现查 ``l3_ref.trade_calendar WHERE exchange = %s``。那张表是 R2 **推断**
    日历，唯一取值 ``exchange='INF'``（全市场、不分交易所）且止于 2026-10-08；
    而这里传入的是 L0 指数层的六家真实交易所之一（CFFEX/CZCE/DCE/GFEX/INE/SHFE）
    ⇒ 恒查不到任何行 ⇒ **这道门从上线起就是死代码**。
    全系统其它模块（``trading_window`` / ``data_selfcheck`` / ``akshare_source``）
    早已统一走 ``app/data/trade_calendar.py`` → ``futures_rule`` 的 distinct
    ``trade_date``（akshare futures_rule 播种的官方真源），本脚本是唯一漏网之鱼。
    ``trading_window.py:20`` 甚至已注明「l3_ref.trade_calendar 数据稀疏，不可依赖」。

    改读同一张真源，并用 ``cfg_calendar_seed_meta.seeded_through`` 做覆盖边界判断：
    日历只记交易日，「无行」既可能是休市也可能是尚未播种，绝不静默当成「不缺日」。
    窗口上界超出播种范围时（夜盘跨自然日常导致 d1 落在周六）只判已确认范围，
    并在 stderr 大声记一行，避免假绿。
    """
    cur.execute(
        "SELECT value FROM cfg_calendar_seed_meta WHERE key = 'seeded_through'")
    row = cur.fetchone()
    seeded_through = date.fromisoformat(str(row[0])[:10]) if row and row[0] else None
    if seeded_through is None:
        raise RuntimeError(
            "[calendar] cfg_calendar_seed_meta.seeded_through 缺失，无法判定交易日；"
            "先跑 scripts/seed_futures_rule.py（禁止退回 weekday 猜测）")
    upper = min(d1, seeded_through)
    if upper < d1:
        print(f"[calendar][WARN] 官方日历止于 {seeded_through}，窗口上界 {d1} "
              "超出部分不参与缺日判定（夜盘跨自然日属常见情形）", file=sys.stderr)
    cur.execute(
        "SELECT DISTINCT trade_date FROM futures_rule "
        "WHERE trade_date > %s AND trade_date <= %s ORDER BY 1",
        (d0, upper))
    return [str(r[0]) for r in cur.fetchall() if r[0] not in have]


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

    # 「整天一行都没有」的交易日：上面两个门都看不见它（它压根不出现在 q 里）。
    # 危害不是「少一天」那么轻——synth_8888 会把那天 ffill 成常数平线，直接污染
    # 换月判据（见 1b）。所以必须像坏日一样点名。
    have_days = {r.d for r in q.itertuples()}
    # 缺日必须同时进 bad_objs：只写进 bad_days 是「点名但不拦」——stage_dedup
    # 仍会把这些日子的 continuous 行喂给 fetch_front 选主力，与上面注释承诺的
    # 「像坏日一样拦下」不符（CL-20261107-18 一并修）。
    for ms in _missing_trading_days(cur, last_ts.date(), t_max.date(), have_days):
        bad_days.append(f"{ms}(no_contract_rows)")
        bad_objs.add(date.fromisoformat(ms))

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

    # ---- 1b) 禁止 ffill 伪造：只保留 front 合约真实有分钟的时刻 ----
    #
    # ``synth_8888`` 末尾有一句 ``cont.reindex(anchor).ffill()``，anchor 是 **888 主连
    # 时间轴**。全量 CSV 导入时 anchor 与 front 同源，ffill 无害；但增量链路的 anchor
    # 来自 ``layer='continuous'``（CSV 遗留），front 来自 ``layer='contract'``，两者
    # 可以不同步：
    #
    #   * 2026-11-02 合约层整天没有 FU 的任何行 → front 为空 → ffill 把 10-30 的收盘
    #     价铺满 11-02 全天 ⇒ 指数变成一条常数平线（实测 344 行同一价位）；
    #   * 2026-10-31 是周六，合约层无数据，同样被 anchor 拉出 865 行假 bar。
    #
    # 常数平线的后果不是「精度差一点」，而是**把换月检测变成随机判据**：ratio 门是
    # ``coalesce(idx_delta,1e9) < 0.40*|cc|``，平线的 ``idx_delta`` 恒为 0 ⇒ 门恒成立
    # ⇒ 任何跳空（含周末跳空）都被判成换月。实测 FU 在 11-02 09:15 的周末跳空
    # -146（10-30 收盘 5436 → 11-02 开盘 5290）被当成换月写进了
    # ``l2_adj.roll_segment``，并连带把另外 7 个品种各写了一段假 offset。
    #
    # 所以这里按「front 真实有分钟」硬裁剪：**缺就是缺**。宁可有洞（``idx_delta`` 为
    # NULL ⇒ 门关闭 ⇒ 保守地不判换月），也绝不写一条会误导判据的假曲线。
    n_filled = len(out)
    out = out.loc[out.index.intersection(front.index)]
    n_filled -= len(out)
    if len(out) == 0:
        return {"variety": variety, "status": "no_front_rows_in_window", "rows": 0,
                "skipped_days": bad_days}

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
                "ffill_dropped": n_filled, "skipped_days": bad_days,
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
            "ffill_dropped": n_filled, "skipped_days": bad_days,
            "delta": round(delta, 4)}


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
