"""60m 收盘即时链：采集 → L1 增量合成 → BA(60m) → 扫描推送。

CL-20261109-17（2026-11-09）

为什么需要单独一条链
--------------------
用户要求「每小时 K 线结束后 +10s 扫描并推送信号」。用**实测**数据核对后，
原来的链路在这三处都到不了：

1. **节奏不对**：``v5x_scan`` 固定 :05/:35，而 60m 真实收盘标签是
   10:00 / 11:15 / 14:15 / 15:00 / 22:00 / 23:00 / 00:00 / 01:00。
   :35 扫到 14:15 那根时已过 20 分钟，:05 扫时那根还没收盘。
2. **数据不对**：分钟采集 + L1 合成只在 :07/:37 跑（实测 14:07:00→14:10:05，
   其中 ``_extend_index_8888`` 占 128s）；后复权 ``l2_adj.bar_*_ba`` 一天只重建
   3 次（02:55/11:55/15:55，实测 15:00 那根要到 15:55 才进 BA）。收盘后
   一小时内扫描读到的是**旧表**。
3. **口径不对**：``synthesize_bars_incremental`` 会写**在途半截桶**（实测 14:10
   已写出标签 14:15 的半截 bar），而 ``scan_once`` 原来只按 ``bucket <= now``
   过滤 —— 等于拿半根 K 线出信号，且 ``dedupe_key`` 会把错误结果**永久固化**。

所以本模块把「收盘那一刻」需要的四步串成一条链，顺序不能换：
**先补 L0 → 再合成 L1 → 再刷 BA → 最后扫描**。任何一步缺，扫描读到的就不是
这根刚收盘的 K 线。

时序预算（2026-11-09 实测，53 个主连 6 路并发）
-----------------------------------------------
| 步骤            | 串行实测 | 本链做法 |
|-----------------|---------|---------|
| akshare 分钟采集 | 37s     | 6 路并发 ≈ 7~10s |
| L1 增量合成      | 9s      | 9s（含 purge，复用同一口径） |
| BA 增量(仅 60m)  | —       | 约 10~30s |
| V5X 扫描         | 13s     | 13s |
合计约 40~60s ⇒ 推送落在**收盘后 1 分钟内**。**做不到 10 秒**：数据源本身有秒级
延迟、53 个品种要逐个拉、还要等最后一分钟落库。10 秒只是**触发时刻**
（``live.SCAN_SECOND``），不是送达时刻 —— 这一点在台账和日志里都写明。
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import logger

#: 单次收盘链最多重试几轮采集（含首轮）。实测新浪 1 分钟在收盘后 5~20s 内补齐，
#: 2 次重试（间隔 15s）足够；再多就是在收盘后 1 分钟以后才开始扫描。
MAX_ATTEMPTS = 3
#: 重试间隔（秒）
RETRY_WAIT_S = 15
#: 覆盖率达到该比例即认为「这一轮的分钟数据已经到位」，不再重试。
#: 不要求 100%：个别品种停牌/无成交，收盘那一分钟本就没有 bar，等下去只会
#: 让整条链一直拖到重试上限。扫描侧的完备性门按品种单独判定，不会误用半截桶。
COVERAGE_MIN = 0.90
#: 并发抓取路数。6 路是权衡：再高容易被新浪限流（实测 8 路偶发空返回）。
FETCH_WORKERS = 6
#: 收盘后允许启动本链的最大滞后（秒）。超过说明是补跑/misfire，
#: 此时收盘那根早已被后续 bar 顶掉，重跑只会对错位的 bar 出信号 → 直接放弃，
#: 让兜底的 :05/:35 扫描与下一根收盘正常接管。
MAX_LAG_S = 180

#: 主链按池级覆盖率(90%)退出后，个别滞后品种(如 M888)仍可能因上游 akshare
#: 延迟未就绪，被完备性门丢弃 → 信号拖到固定 :35 兜底(实测 10:35)。
#: 扩展重试（CL-20261111-07）：对这类「个别滞后品种」每 RETRY_LAGGING_GAP_S 秒
#: 只重采它们 + 增量合成 + 刷 BA + 重扫推送，数据一到(akshare 上游补齐)即推；
#: 上限 RETRY_LAGGING_BUDGET_S(≈25min, 至 ~10:26)。极端坏日(>25min 才发布)仍由
#: :35 兜底扫接住，不比现状(10:35)差。完备性门本身不动（宁晚勿错）。
RETRY_LAGGING_GAP_S = 90
RETRY_LAGGING_BUDGET_S = 1500


def current_close_label(now: datetime, *, max_lag_s: int = MAX_LAG_S):
    """``now`` 是否正处在某根 60m 的「刚收盘」窗口内；是则返回该收盘标签。

    判据：标签 ``L`` 满足 ``L + SCAN_SECOND <= now <= L + max_lag_s``。
    用 ``CLOSE_HM``（live.py，日盘从合成器网格派生 + 夜盘并集）判定，
    不在这里另立一套时间表 —— 两处各写一份，下次改网格必然漂移。
    """
    from app.strategies.v5x.live import CLOSE_HM, SCAN_SECOND

    for hh, mm in CLOSE_HM:
        for off in (-1, 0, 1):
            label = (now + timedelta(days=off)).replace(
                hour=hh, minute=mm, second=0, microsecond=0)
            lag = (now - label).total_seconds()
            if SCAN_SECOND <= lag <= max_lag_s:
                return label
    return None


def _coverage(session: Session, symbols: list[str], label: datetime) -> tuple[int, int]:
    """这一轮采集后，「该有数据的品种」里有多少已经覆盖到 ``label``。

    **不能简单地数 ``ts = label``**：22:00 收盘时池里只有约 65/80 个品种有夜盘
    （农产品无夜盘、指数层不在池内），把「本来就不交易」算成「没采到」会让
    ``COVERAGE_MIN`` 永远达不到 → 每根收盘都白等两轮 15 秒。

    判据拆成三种：
      · ``max(ts) >= label``            → 就绪（桶尾那分钟已落库）；
      · ``max(ts) < label - 45min``     → 本时段不交易（休市/无夜盘），不算欠账；
      · 其余                            → 真·滞后（最近还有数据，就是没到收盘）。
    """
    if not symbols:
        return 0, 0
    rows = session.execute(
        text(
            "SELECT symbol, max(ts) FROM public.minute_bar "
            "WHERE symbol = ANY(:syms) AND ts >= :a GROUP BY symbol"
        ),
        {"syms": list(symbols), "a": label - timedelta(days=3)},
    ).all()
    stale_floor = label - timedelta(minutes=45)
    ready = idle = 0
    for _sym, ts in rows:
        if ts is None:
            continue
        if label.tzinfo is None and ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        elif label.tzinfo is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=label.tzinfo)
        if ts >= label:
            ready += 1
        elif ts < stale_floor:
            idle += 1
    # 缺 L0 的品种（rows 里没有）也算欠账：它们本来就该有数据。
    return ready, len(symbols) - idle


def lagging_symbols(session: Session, symbols: list[str], label: datetime) -> list[str]:
    """主链按池级覆盖率退出后，仍「分钟数据未覆盖到桶尾」的滞后品种（个别）。

    判据与 :func:`_coverage` 同源：``max(ts)`` 落在 ``[label-45min, label)`` 内才算
    真·滞后；``max(ts) < label-45min``（本时段不交易/休市→idle）或
    ``max(ts) >= label``（已就绪）都不算。无 L0 记录的品种视为 idle，不重试。
    """
    if not symbols:
        return []
    rows = session.execute(
        text(
            "SELECT symbol, max(ts) FROM public.minute_bar "
            "WHERE symbol = ANY(:syms) AND ts >= :a GROUP BY symbol"
        ),
        {"syms": list(symbols), "a": label - timedelta(days=3)},
    ).all()
    by_sym = {str(sym): ts for sym, ts in rows}
    stale_floor = label - timedelta(minutes=45)
    lag: list[str] = []
    for sym in symbols:
        ts = by_sym.get(sym)
        if ts is None:
            continue
        if label.tzinfo is None and ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        elif label.tzinfo is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=label.tzinfo)
        if ts < label and ts >= stale_floor:
            lag.append(sym)
    return lag


def collect_minutes(session: Session, symbols: list[str], workers: int = FETCH_WORKERS) -> dict:
    """补 L0：只采池内品种（收盘链不需要 82 个符号）。"""
    from app.ingest.minute_collector import MinuteCollector

    t0 = time.time()
    mc = MinuteCollector(session)
    stats = mc.collect_all(workers=workers, symbols=symbols)
    ok = sum(1 for r in stats if "error" not in r)
    return {"ok": ok, "n": len(stats), "elapsed_s": round(time.time() - t0, 1)}


def synth_l1(session: Session, *, purge: bool = True) -> dict:
    """补 L1：purge 最近 4 天旧桶 + 增量合成最近 3 天（与 ``_minute_and_bars_job``
    的 ``mode=python`` 分支**同一口径**）。

    ``purge`` 只在**一轮收盘链的第一次**尝试时置真：它的职责是清除旧网格残留，
    每根收盘做一次足够；重试轮再 purge 一遍纯属把同一批 2 万行删了又插，
    放大写入量却没有任何收益。

    刻意**不**调用 ``_extend_index_8888``：那一步实测要 128s（akshare 拉 82 条指数
    分钟），服务的是换月检测，与本链无关；收盘链里跳过它，换月检测仍由 :07/:37
    的正常管线负责。
    """
    from app.data.tradable_scope import load_scope
    from app.ingest.synthesizer import purge_recent_buckets, synthesize_bars_incremental

    scope = load_scope(session)
    if purge:
        purge_recent_buckets(session, days_back=4, scope=scope)
    stats = synthesize_bars_incremental(session, days_back=3, scope=scope)
    return {k: v for k, v in stats.items()}


def refresh_ba_min60(session: Session) -> float:
    """刷 L2：只重建 60m 后复权（V5X 唯一信号源）。

    ``sp_build_l2_back_adjusted`` 自带触发签名：roll_segment 没变时只增量补最新桶。
    ``SET LOCAL jit = off`` 沿用 CL-20261109-11 的实测结论（关 JIT 后单品种
    min5 75s→8.2s，结果逐位不变）。
    """
    t0 = time.time()
    session.execute(text("SET LOCAL jit = off"))
    session.execute(text("CALL l2_adj.sp_build_l2_back_adjusted('min60')"))
    session.flush()
    return round(time.time() - t0, 1)


def run_close_chain(
    label: datetime,
    *,
    do_push: bool = True,
    attempts: int = MAX_ATTEMPTS,
    wait_s: int = RETRY_WAIT_S,
    workers: int = FETCH_WORKERS,
    retry_lagging: bool = True,
) -> dict[str, Any]:
    """跑完一根 60m 的收盘链，返回摘要（供调度器记日志/判定失败）。

    返回字段：
      label       收盘标签（本地时间）
      attempts    实际采集轮数（主链）
      coverage    最后一轮的 (已覆盖品种数, 池内品种数)
      collect/l1/ba 各步耗时与行数
      scan        首轮 ``run_v5x_scan`` 的摘要
      lagging_initial / lagging_final   扩展重试前后的滞后品种数
      lagging_scan   末轮扩展重试扫描摘要

    扩展重试（CL-20261111-07）：主链按池级覆盖率(90%)退出，但个别品种(如 M888)
    可能因 akshare 上游延迟仍未就绪 → 信号拖到固定 :35 兜底。这里对滞后品种持续
    轮询刚收盘根，数据一到即推；上限 RETRY_LAGGING_BUDGET_S。每次重试独立开会话，
    间隔期间不持有任何 DB 会话（避免长事务）。
    """
    from app.core.db import session_scope
    from app.data.tradable_scope import load_tradable_pool
    from app.ingest.synthesizer import minute_pipeline_lock
    from app.strategies.v5x.live import run_v5x_scan

    # 品种池（独立短会话）
    with session_scope() as s:
        symbols = [f"{v}888" for v in sorted(load_tradable_pool(s))]
    if not symbols:
        raise RuntimeError("品种池为空（l3_ref.v_tradable_scope 无行）→ 收盘链不执行")

    summary: dict[str, Any] = {"label": label.isoformat(), "symbols": len(symbols)}
    cov: tuple[int, int] = (0, len(symbols))
    used = 0

    # ---- 主链：池级重试（覆盖率达标或达 attempt 上限即停）----
    for i in range(1, max(1, attempts) + 1):
        used = i
        # 采集 + 合成必须在分钟管线锁内完成：两者交错会让「DELETE 已删最近桶、
        # INSERT 读到未提交快照」重演（2026-09-30 静默漏桶的根因）。
        with session_scope() as s:
            with minute_pipeline_lock():
                summary["collect"] = collect_minutes(s, symbols, workers=workers)
                summary["l1"] = synth_l1(s, purge=(i == 1))
            cov = _coverage(s, symbols, label)
        summary["coverage"] = cov
        logger.info(
            f"[close_pipeline] {label:%m-%d %H:%M} 第 {i} 轮："
            f"覆盖 {cov[0]}/{cov[1]}（{cov[0] / max(1, cov[1]):.0%}），"
            f"采集 {summary['collect']}"
        )
        if cov[0] >= max(1, int(cov[1] * COVERAGE_MIN)):
            break
        if i < attempts:
            time.sleep(wait_s)

    summary["attempts"] = used

    # BA 必须在最后一轮 L1 之后刷，否则补进来的那根不进 BA，扫描仍看不到。
    # 扫描的 now 用**当前时刻**而不是 label（scan_once 内部用 now 做陈旧度判定，
    # MAX_BAR_AGE_MIN=90，传 label 会让刚收盘的 bar 天然带滞后）。
    with session_scope() as s:
        summary["ba60_s"] = refresh_ba_min60(s)
        res = run_v5x_scan(s, now=datetime.now(label.tzinfo), do_push=do_push)
    summary["scan"] = res
    logger.info(f"[close_pipeline] {label:%m-%d %H:%M} 首轮完成：{summary}")

    # ---- 扩展重试：个别滞后品种持续轮询刚收盘根（CL-20261111-07）----
    if retry_lagging:
        with session_scope() as s:
            lag = lagging_symbols(s, symbols, label)
        summary["lagging_initial"] = len(lag)
        if lag:
            logger.info(
                f"[close_pipeline] {label:%m-%d %H:%M} 进入扩展重试："
                f"{len(lag)} 个滞后品种 {lag}"
            )
            deadline = time.time() + RETRY_LAGGING_BUDGET_S
            while lag and time.time() < deadline:
                time.sleep(RETRY_LAGGING_GAP_S)
                with session_scope() as s:
                    with minute_pipeline_lock():
                        summary.setdefault("lagging_collect", []).append(
                            collect_minutes(
                                s, lag, workers=min(workers, max(1, len(lag)))
                            )
                        )
                        synth_l1(s, purge=False)
                    lag = lagging_symbols(s, symbols, label)
                    summary["ba60_s"] = refresh_ba_min60(s)
                    res2 = run_v5x_scan(
                        s, now=datetime.now(label.tzinfo), do_push=do_push
                    )
                summary["lagging_scan"] = res2
                logger.info(
                    f"[close_pipeline] {label:%m-%d %H:%M} 重试轮："
                    f"剩余滞后 {len(lag)} {lag}，扫描 {res2}"
                )
                if not lag:
                    break
            summary["lagging_final"] = len(lag)
            if lag:
                logger.warning(
                    f"[close_pipeline] {label:%m-%d %H:%M} 扩展重试超预算仍有滞后 "
                    f"{len(lag)} {lag} → 交 :35 兜底扫描"
                )
    return summary
