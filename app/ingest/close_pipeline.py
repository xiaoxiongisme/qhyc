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
from collections import Counter
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

#: 兜底扫描（:05/:35）做「定稿判定」时，向前回看多少秒找最近的收盘标签。
#: 收盘后 35 分钟内的那次兜底仍应受安全阀约束（那正是残缺 bar 最容易
#: 被误用的窗口）；超过 35 分钟则视为「已过该收盘窗口」，不设限。
RETRY_FINALIZE_LOOKBACK_S = 2100

#: 批次标签（CL-20261111-10）。两批次是**给用户看的语义**：
#: 首批=收盘即已就绪的品种；次批=等上游数据到齐后补发的品种。
BATCH_FIRST = "首批·已就绪"
BATCH_LATE = "补发·滞后到齐"
BATCH_FALLBACK = "补发·兜底扫描"

#: 品种就绪状态（CL-20261111-10）。
READY = "ready"            # 定稿：引擎可以安全用它出信号
LAG_MINUTE = "lag_minute"  # 分钟未到桶尾（上游还没发布那一分钟）
LAG_DERIVED = "lag_derived"  # 分钟到了，但派生层(L1/BA)这根桶未定稿/不同步


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


#: 逐品种就绪判定的结果码（CL-20261111-10）。
#: ``ready``       —— 该品种这根 bar 已定稿，可以出信号
#: ``no_minute``   —— 分钟窗口有缺口（上游还没发全这根 bar 的分钟）
#: ``no_l1``       —— 分钟齐了，但 L1 的 60m 桶还没合成出来
#: ``no_ba``       —— L1 桶有了，但 BA（引擎真正读的表）还没有这根
#: ``ba_stale``    —— BA 这根存在，但与 L1 不同步（BA 是修正前的旧值）
READY_OK = "ready"
READY_NO_MINUTE = "no_minute"
READY_NO_L1 = "no_l1"
READY_NO_BA = "no_ba"
READY_BA_STALE = "ba_stale"
#: 该品种**本时段不交易**（历史同窗口根数恒为 0）—— 既不发也不重试。
READY_IDLE = "idle"

#: 期望根数取最近 N 天同窗口根数的众数。
EXPECT_LOOKBACK_DAYS = 6

#: 连续多少轮「窗口根数不再增长」后，认为上游已无更多分钟（次批放行）。
#: 取 3 轮 ≈ 4.5 分钟（RETRY_LAGGING_GAP_S=90s）：既给上游留足补发时间，
#: 又不至于让「永久缺根」的品种把这根 bar 的信号拖到超预算（= 永久丢失）。
STABLE_ROUNDS_TO_RELEASE = 3

#: 60m 桶的**分钟窗口** = (前一根桶标签, 本桶标签]。
#:
#: ★ 必须与合成器同源（``app/ingest/synthesizer.py`` 的会话网格），
#:   否则「就绪」判据与实际落库的桶对不上号 —— 那时判「就绪」也拿不到定稿值。
#:   日盘：``_SESSION_DAY_BOUNDS[60] = [60, 135, 315, 360]`` → 相对 09:00 的分钟偏移
#:   ⇒ 桶标签 10:00 / 11:15 / 14:15 / 15:00，窗口起点即前一根桶标签
#:   （首根 10:00 的窗口起点是 09:00）。
#:   夜盘：从 21:00 起逐小时一桶 ⇒ 22:00 / 23:00 / 00:00 / 01:00 / 02:00(+02:30)。
#:
#: 实测（11-11，``public.minute_bar``）：
#:   桶 10:00 → 窗口 (09:00, 10:00] 全池 53 品种**各 60 根**（09:01~10:00）
#:   桶 11:15 → 窗口 (10:00, 11:15] 多数 60 根（休市 10:16~10:30 无数据）
#:   桶 14:15 → 窗口 (11:15, 14:15] 多数 60 根（午休 11:31~13:30 无数据）
#:   桶 15:00 → 窗口 (14:15, 15:00] 全池**各 45 根**（14:16~15:00）
#:
#: 夜盘窗口跨日（00:00/01:00/02:00 属「前一日 21:00 起的那一段」），
#: 这里按「标签时刻」反推窗口起止，夜盘起点固定 21:00。
_DAY_BUCKET_STARTS = {
    (10, 0): (9, 0),      # (09:00, 10:00]
    (11, 15): (10, 0),    # (10:00, 11:15]
    (14, 15): (11, 15),   # (11:15, 14:15]
    (15, 0): (14, 15),    # (14:15, 15:00]
}
_NIGHT_BUCKET_STARTS = {
    (22, 0): (21, 0),     # (21:00, 22:00]
    (23, 0): (22, 0),     # (22:00, 23:00]
    (0, 0): (23, 0),      # (23:00, 00:00]（跨日）
    (1, 0): (0, 0),       # (00:00, 01:00]（跨日）
    (2, 0): (1, 0),       # (01:00, 02:00]（跨日）
    (2, 30): (2, 0),      # (02:00, 02:30]（跨日）
}


def bucket_window(label: datetime) -> tuple[datetime, datetime] | None:
    """这根 60m 桶标签对应的分钟窗口 ``(起, 止]``（闭区间含止点）。

    与 :mod:`app.ingest.synthesizer` 的会话网格同源（见 ``_DAY_BUCKET_STARTS``）。
    非 60m 网格标签（如 21:01 这种残段）返回 ``None`` —— 那不是一根真实 K 线。
    """
    hm = (label.hour, label.minute)
    if hm in _DAY_BUCKET_STARTS:
        sh, sm = _DAY_BUCKET_STARTS[hm]
        start = label.replace(hour=sh, minute=sm, second=0, microsecond=0)
        return start, label
    if hm in _NIGHT_BUCKET_STARTS:
        sh, sm = _NIGHT_BUCKET_STARTS[hm]
        start = label.replace(hour=sh, minute=sm, second=0, microsecond=0)
        if hm == (0, 0):
            # 只有 00:00 这一根跨日：它属于「**前一日** 21:00 起」的那一段夜盘。
            # 01:00 / 02:00 / 02:30 的分钟就在 label 当天（夜盘过了午夜仍在当天），
            # 若也减一天，窗口会拉成 25 小时 ⇒ 根数虚高、判据失效。
            start -= timedelta(days=1)
        return start, label
    return None


def expected_minute_counts(session: Session, symbols: list[str], label: datetime,
                           lookback: int = EXPECT_LOOKBACK_DAYS) -> dict[str, int]:
    """每个品种「这根桶的窗口」应有的分钟根数 = 最近若干天同窗口根数的**众数**。

    为什么用历史众数而不是写死常量
    ------------------------------
    * 各品种交易时段不同（农产品无夜盘、金属到 02:30、个别品种 23:00 就收）；
      写死「夜盘 60 根」会把「本就不交易」的品种永远判成滞后。
    * 同一品种的窗口根数在历史里高度稳定（实测 11-11：38 个白名单品种在
      10:00/14:15/15:00 标签下最近 4 天全是 60/60/45，**方差 0**）。
    * 个别品种偶发少 2~3 根（实测 BR888/FU888 在 11:15 窗口 57 vs 众数 60）
      ⇒ 众数是「正常值」，今天低于它就是**还没到齐**，该等。

    返回 ``{symbol: 期望根数}``。**期望为 0 ⇒ 该品种本时段不交易（idle）**，
    调用方必须把它排除在两批之外，否则夜盘每根收盘都会空转整个重试预算。
    """
    if not symbols:
        return {}
    window = bucket_window(label)
    if window is None:
        return {}
    wa, wb = window
    # ★ 性能（实测）：CROSS JOIN generate_series 把「窗口 × 天数」做成交叉连接，
    #   在 38 品种 × 6 天 × 全池 minute_bar 上跑满 280s 超时。
    #   改成「逐日扫窄区间」：每天一次 (窗口起, 窗口止] 的范围查询，
    #   走 ``minute_bar_ts_idx``（ts 索引），每次只碰 ~38×60 行。
    #   ★ 不能改成「按日期分组一次扫」—— 那样数到的是**整日**的分钟数，
    #     不是窗口内的根数（窗口只占一天的 1/4~1/2），判据会失效。
    hist: dict[str, list[int]] = {}
    for k in range(1, max(2, lookback) + 1):
        a = wa - timedelta(days=k)
        b = wb - timedelta(days=k)
        rows = session.execute(
            text(
                "SELECT symbol, count(*) n FROM public.minute_bar "
                " WHERE symbol = ANY(:syms) AND ts > :a AND ts <= :b "
                " GROUP BY symbol"
            ),
            {"syms": list(symbols), "a": a, "b": b},
        ).all()
        for sym, n in rows:
            hist.setdefault(str(sym), []).append(int(n))

    out: dict[str, int] = {}
    for sym in symbols:
        vals = hist.get(sym, [])
        if not vals:
            out[sym] = 0          # 没有历史 ⇒ 该品种本时段不交易
            continue
        cnt = Counter(vals)
        # 众数；并列时取**较大**者（保守：宁可判定「还没到齐」也别提前发）
        out[sym] = max(cnt.items(), key=lambda kv: (kv[1], kv[0]))[0]
    return out


def _window_counts(session: Session, symbols: list[str], label: datetime
                   ) -> dict[str, int]:
    """每个品种在「这根桶的窗口」内当前有多少根分钟。"""
    window = bucket_window(label)
    if window is None or not symbols:
        return {}
    rows = session.execute(
        text(
            "SELECT symbol, count(*) n FROM public.minute_bar "
            " WHERE symbol = ANY(:syms) AND ts > :a AND ts <= :b "
            " GROUP BY symbol"
        ),
        {"syms": list(symbols), "a": window[0], "b": window[1]},
    ).all()
    return {str(s_): int(n) for s_, n in rows}


def symbol_readiness(
    session: Session, symbols: list[str], label: datetime,
    expected: dict[str, int] | None = None,
) -> dict[str, tuple[str, str]]:
    """逐品种判定「这根 60m bar 能不能现在就用」。

    ★ 主判据是**窗口根数**，不是「桶尾那一分钟」
    ---------------------------------------------
    这是 CL-20261111-10 的核心修复，也是本轮实测推翻先前认知的地方。

    先前的判据（``_coverage`` 与引擎的完备性门）都只看 ``max(ts) >= label``，
    即「桶尾那一分钟到没到」。实测反例（11-11 10:00 收盘，生产 journal）：

    * 收盘链 ``_coverage`` 报 **38/38（100%）** —— 每���品种的桶尾分钟都到了；
    * M888 的信号直到 **10:35** 才出，且落库价（ba 8003 / offset 4846 / real 3157）
      与事后最终 BA **逐位一致** ⇒ 那条是**迟到但正确**，不是错价；
    * 事后用分钟重算 11-11 上午窗口：**每个品种窗口内都缺根**（桶 10:00 应 60 根，
      实测部分品种只有 57 根），而 ``max(ts)`` 仍然是 10:00 ⇒ 老判据完全看不见。

    机理：分钟是**陆续**到的。若窗口中间缺了几根（09:31~09:59 之类），
    ``max(ts)`` 照样是 10:00，但这一根 bar 是用**残缺分钟**合成的；
    引擎读 BA 拿到这根残缺 bar → 重放出的信号与定稿值不同 →
    要么这轮不出信号（10:01 那轮 M888 就是如此），要么出**错价信号**并被
    ``dedupe_key`` 永久固化（同一根 bar 的重算再也进不来）。后者不可逆。

    所以判据必须是「**这个窗口的分钟齐不齐**」，即：

    1. 窗口内分钟根数 == 期望根数（按会话网格标定）→ 否则 ``no_minute``；
    2. L1 有这根桶 → 否则 ``no_l1``；
    3. BA（引擎真正读的表）有这根桶 → 否则 ``no_ba``；
    4. BA 与 L1 逐位一致（``BA-PRICE-IDENTITY`` 的同一条恒等式）→ 否则 ``ba_stale``。

    「重采有意义」与「只需重刷 BA」被明确区分：前两类重采可治好，
    ``ba_stale`` 是分钟齐了但 BA 是旧值 —— 增量分支的 WHERE 是
    ``bucket >= meta.max_bucket``（**含水位桶本身**，实测自存储过程源码），
    所以水位桶会被重写，但**更早的桶**不会 —— 那类需要按品种全量重建。
    """
    if not symbols:
        return {}
    syms = list(symbols)

    window = bucket_window(label)
    if expected is None:
        expected = expected_minute_counts(session, syms, label)

    # 一次查询拿齐三层事实 + 窗口根数，避免逐品种往返。
    rows = session.execute(
        text(
            """
            WITH m AS (
                SELECT symbol, max(ts) mx FROM public.minute_bar
                 WHERE symbol = ANY(:syms) AND ts >= :a GROUP BY symbol
            ), w AS (
                SELECT symbol, count(*) n FROM public.minute_bar
                 WHERE symbol = ANY(:syms) AND ts > :wa AND ts <= :wb
                 GROUP BY symbol
            ), l1 AS (
                SELECT symbol, close FROM l1_mkt.bar_60m
                 WHERE symbol = ANY(:syms) AND bucket = :bk
            ), ba AS (
                SELECT symbol, close, adj_offset FROM l2_adj.bar_60m_ba
                 WHERE symbol = ANY(:syms) AND bucket = :bk
            )
            SELECT coalesce(m.symbol, w.symbol, l1.symbol, ba.symbol) AS sym,
                   m.mx, w.n, l1.close AS l1_close, ba.close AS ba_close,
                   ba.adj_offset AS off
              FROM m FULL OUTER JOIN w ON w.symbol = m.symbol
                     FULL OUTER JOIN l1 ON l1.symbol = coalesce(m.symbol, w.symbol)
                     FULL OUTER JOIN ba ON ba.symbol = coalesce(m.symbol, w.symbol, l1.symbol)
             ORDER BY 1
            """
        ),
        {
            "syms": syms,
            "a": label - timedelta(days=3),
            "wa": window[0] if window else label,
            "wb": window[1] if window else label,
            "bk": label,
        },
    ).all()

    facts: dict[str, tuple] = {}
    for r in rows:
        facts[str(r[0])] = (r[1], r[2], r[3], r[4], r[5])

    out: dict[str, tuple[str, str]] = {}
    for sym in syms:
        mx, n_win, l1_close, ba_close, off = facts.get(sym, (None, None, None, None, None))
        exp = int(expected.get(sym, 0))
        if exp == 0:
            # 该品种本时段不交易（历史同窗口恒 0 根）—— 不是「滞后」，不重试。
            out[sym] = (READY_IDLE, "本时段不交易")
            continue
        if mx is None:
            # 从未有 L0 记录 → 该品种不参与本池（idle），不是「滞后」。
            out[sym] = (READY_NO_MINUTE, "无 L0 记录")
            continue
        if label.tzinfo is None and mx.tzinfo is not None:
            mx = mx.replace(tzinfo=None)
        elif label.tzinfo is not None and mx.tzinfo is None:
            mx = mx.replace(tzinfo=label.tzinfo)
        # ① 窗口根数：老判据只看桶尾那一分钟，看不见窗口中间的洞。
        got = int(n_win or 0)
        if got < exp:
            out[sym] = (
                READY_NO_MINUTE,
                f"窗口 {got}/{exp} 根分钟（止于 {mx:%m-%d %H:%M}）",
            )
            continue
        if l1_close is None:
            out[sym] = (READY_NO_L1, "L1 60m 桶缺失")
            continue
        if ba_close is None:
            out[sym] = (READY_NO_BA, "BA 桶缺失")
            continue
        # ② BA 与 L1 必须逐位一致（BA-PRICE-IDENTITY 的同一条恒等式）。
        try:
            drift = abs(float(ba_close) - float(off or 0) - float(l1_close))
        except (TypeError, ValueError):
            drift = 1.0
        if drift >= 0.01:
            out[sym] = (READY_BA_STALE, f"BA 与 L1 偏差 {drift:.4f}")
            continue
        out[sym] = (READY_OK, "已定稿")
    return out


def finalized_symbols(session: Session, label: datetime,
                      symbols: list[str] | None = None) -> set[str]:
    """这根收盘标签下，**已定稿**（可以安全出信号）的品种集合。

    供兜底扫描（:05/:35）使用：那条路是「收盘链没赶上时的补救」，
    但它同样会落库并被 ``dedupe_key`` **永久固化** ⇒ 未定稿的 bar
    一律不许用它出信号（宁可漏推，也不把错信号钉死）。

    与 :func:`split_ready` 的区别：这里只要「已定稿」那一份，
    且判定失败时**返回空集**（= 全池都不许出信号）—— 对兜底路径来说，
    「不确定」时应保守；而收盘链内部调用时失败会走它自己的重试循环。
    """
    from app.data.tradable_scope import load_tradable_pool

    if symbols is None:
        symbols = [f"{v}888" for v in sorted(load_tradable_pool(session))]
    if not symbols:
        return set()
    expected = expected_minute_counts(session, symbols, label)
    ready, _pending, _detail = split_ready(
        session, symbols, label, expected=expected)
    return set(ready)


def split_ready(session: Session, symbols: list[str], label: datetime,
                expected: dict[str, int] | None = None,
                ) -> tuple[list[str], list[str], dict[str, str]]:
    """把品种池拆成「已定稿」「未定稿」「本时段不交易」三组。

    返回 ``(ready, pending, detail)``：

    * ``ready``   —— 已定稿，进首批，立即出信号、立即推送；
    * ``pending`` —— 未定稿，进次批，持续重试到齐后补发；
    * ``detail``  —— 每个品种的状态码与说明（供日志/简报）。

    ★ ``idle``（本时段不交易）品种**既不进 ready 也不进 pending**。
      它们在历史上同窗口恒 0 根（农产品无夜盘、金属 23:00 收等），
      若混进 pending，重试循环会为它们空转整个预算
      （实测夜盘 22:00 时 38 个白名单品种里有 7 个根本不开夜盘）。
    """
    st = symbol_readiness(session, symbols, label, expected=expected)
    detail = {s: f"{st.get(s, (READY_NO_MINUTE, ''))[0]}:{st.get(s, ('', ''))[1]}"
              for s in symbols}
    ready = [s for s in symbols if st.get(s, (READY_NO_MINUTE, ""))[0] == READY_OK]
    ready_set = set(ready)
    pending = [
        s for s in symbols
        if s not in ready_set and st.get(s, (READY_NO_MINUTE, ""))[0] != READY_IDLE
    ]
    return ready, pending, detail


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

    ★ 两批次推送（CL-20261111-10）
    ------------------------------
    用户诉求：「分两次发，第一次发已经完成的，第二次发滞后的」。
    本函数把「就绪」判据从**池级分钟覆盖率**推进到**逐品种的派生层定稿**，
    再据此把一次收盘拆成两批推送：

    * **首批·已就绪**：这根 bar 已在派生层（``bar_60m_ba``）定稿的品种 →
      收盘后约 1 分钟内立即出信号、立即推送。
    * **次批·滞后到齐**：这根 bar 还没定稿的品种 → 不落库、不推送，
      持续轮询（重采 + 重合成 + 重刷 BA），数据到齐后单独成批补发。

    为什么必须逐品种判「定稿」（而不是看池级覆盖率）
    ----------------------------------------------
    实测反例（11-11 10:00 收盘，生产 journal）：

    * 收盘链 ``_coverage`` 报 **38/38（100%）** —— 分钟确实全部到齐；
    * 同一轮扫描器却报「**26 品种降级**」，M888 的信号直到 **10:35** 才出；
    * 而 10:35 那条信号落库的价格（ba 8003 / offset 4846 / real 3157）
      与事后最终 BA **逐位一致** ⇒ 是**迟到但正确**，不是错价。

    根因：引擎读的是 ``l2_adj.bar_60m_ba``，而池级覆盖率只看 minute_bar。
    分钟齐 ≠ 引擎能读到这根 bar 的定稿值；两者之间隔着一层「分钟→L1→BA」的物化，
    而 ``sp_build_l2_back_adjusted`` 的**增量分支只写 ``bucket > meta.max_bucket``**
    —— 已经写过的桶永远不会被增量分支重写。于是 L1 修正后 BA 可能仍停留在旧值。

    返回字段：
      label       收盘标签（本地时间）
      attempts    实际采集轮数（主链）
      coverage    最后一轮的 (已覆盖品种数, 池内品种数)
      collect/l1/ba 各步耗时与行数
      ready_n / pending_n      首批（已定稿）/ 次批（未定稿）品种数
      pending_detail           次批逐品种的状态码与说明
      scan        首批 ``run_v5x_scan`` 的摘要
      lagging_initial / lagging_final   次批轮询前后的剩余未定稿品种数
      lagging_scan   末轮次批扫描摘要
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

    # ---- 就绪判定（逐品种）----
    #
    # ★ 判据是「**这根桶的分钟窗口齐不齐**」，不是「桶尾那一分钟到没到」。
    #   实测（11-11 10:00，生产 journal）：池级覆盖报 38/38（100%），
    #   同一轮扫描器却报「26 品种降级」，M888 的信号直到 10:35 才出，
    #   且那条落库价与事后最终 BA **逐位一致**（迟到但正确，不是错价）。
    #   真因：分钟是陆续到的 —— 桶尾那分钟到了，但窗口中间还缺根，
    #   于是这一根 bar 是用**残缺分钟**合成的。``max(ts)`` 看不见这种洞。
    #   若用它出信号，错的 bar 会被 ``dedupe_key`` **永久固化**（不可逆）。
    #
    # 扫描的 now 用**当前时刻**而不是 label（scan_once 内部用 now 做陈旧度判定，
    # MAX_BAR_AGE_MIN=90，传 label 会让刚收盘的 bar 天然带滞后）。
    with session_scope() as s:
        summary["ba60_s"] = refresh_ba_min60(s)
        expected = expected_minute_counts(s, symbols, label)
        ready, pending, detail = split_ready(s, symbols, label, expected=expected)
    summary["ready_n"] = len(ready)
    summary["pending_n"] = len(pending)
    summary["pending_detail"] = {sym: detail.get(sym, "") for sym in pending}
    summary["idle_n"] = sum(
        1 for sym in symbols if detail.get(sym, "").startswith(READY_IDLE)
    )
    logger.info(
        f"[close_pipeline] {label:%m-%d %H:%M} 就绪判定：首批 {len(ready)} 个 / "
        f"次批 {len(pending)} 个未定稿 / {summary['idle_n']} 个本时段不交易"
        + (f"（{summary['pending_detail']}）" if pending else "")
    )

    # ---- 首批：只扫「已定稿」的品种 → 立即推送 ----
    # emit_symbols 只在**出信号**这一步过滤，取数仍是全池 ⇒
    # 三道日频门（簇冲击是横截面的）在两批之间口径一致。
    # 首批为空时也要扫（emit_symbols=空集 ⇒ 一条都不出），但**不推**：
    # 否则会把「还没定稿」的 bar 当成信号推出去。
    with session_scope() as s:
        res = run_v5x_scan(
            s,
            now=datetime.now(label.tzinfo),
            do_push=do_push,
            emit_symbols=set(ready),
            batch=BATCH_FIRST if do_push else "",
        )
    summary["scan"] = res
    logger.info(f"[close_pipeline] {label:%m-%d %H:%M} 首批完成：{summary}")

    # ---- 次批：未定稿品种持续轮询，数据到齐后单独成批补发 ----
    if retry_lagging and pending:
        with session_scope() as s:
            lag = list(pending)
            # 初始窗口根数（用于「上游不再补发」的稳定期判定）
            cur_counts = _window_counts(s, symbols, label)
        summary["lagging_initial"] = len(lag)
        logger.info(
            f"[close_pipeline] {label:%m-%d %H:%M} 进入次批轮询："
            f"{len(lag)} 个未定稿品种 {lag}"
        )
        deadline = time.time() + RETRY_LAGGING_BUDGET_S
        # 首批已发出的品种不再重发（避免同一根 bar 反复扫描）
        sent: set[str] = set(ready)
        # 连续几轮根数不再增长 ⇒ 认为上游已无更多数据（见 _stable_ok 说明）
        stable_rounds: dict[str, int] = {}
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
                # ★ BA 必须在这之后重刷：L1 刚被重合成，BA 才有新值可读。
                #   增量分支的 WHERE 是 ``bucket >= meta.max_bucket``（**含水位桶
                #   本身**，实测自存储过程源码）⇒ 水位桶会被重写，无需全量重建。
                summary["ba60_s"] = refresh_ba_min60(s)
                ready2, lag, _d2 = split_ready(s, symbols, label, expected=expected)
                new_counts = _window_counts(s, symbols, label)

                # 稳定期判定：某品种的窗口根数连续 N 轮不再增长
                # ⇒ 上游确实没有更多分钟了（永久缺根，如实测 BR888 11-11
                #    窗口缺 10:47~10:49 且上游从未补发）。
                #    此时继续等下去也等不到 ⇒ 按「已尽可能完整」放行次批，
                #    否则该品种这根 bar 的信号会**永久丢失**（比用残缺 bar
                #    发信号更糟）。首批不受影响：首批只放「根数达标」的。
                for sym in list(lag):
                    if new_counts.get(sym, 0) == cur_counts.get(sym, 0):
                        stable_rounds[sym] = stable_rounds.get(sym, 0) + 1
                    else:
                        stable_rounds[sym] = 0
                cur_counts = new_counts

                newly = [x for x in ready2 if x not in sent]
                settled = [
                    x for x in lag
                    if x not in newly
                    and stable_rounds.get(x, 0) >= STABLE_ROUNDS_TO_RELEASE
                    and new_counts.get(x, 0) > 0
                ]
                batch_symbols = newly + settled
                if batch_symbols:
                    res2 = run_v5x_scan(
                        s,
                        now=datetime.now(label.tzinfo),
                        do_push=do_push,
                        emit_symbols=set(batch_symbols),
                        batch=BATCH_LATE if do_push else "",
                    )
                    sent.update(batch_symbols)
                    summary["lagging_scan"] = res2
                    summary.setdefault("late_batches", []).append(
                        {"symbols": batch_symbols,
                         "settled": settled,
                         "scan": res2}
                    )
                    if settled:
                        logger.warning(
                            f"[close_pipeline] {label:%m-%d %H:%M} 次批按「已尽可能完整」"
                            f"放行 {len(settled)} 个品种（窗口根数连续 "
                            f"{STABLE_ROUNDS_TO_RELEASE} 轮未增长，上游已无更多分钟）"
                            f"：{settled}"
                        )
                    lag = [x for x in lag if x not in sent]
            logger.info(
                f"[close_pipeline] {label:%m-%d %H:%M} 次批轮："
                f"剩余未定稿 {len(lag)} {lag}"
            )
        summary["lagging_final"] = len(lag)
        if lag:
            logger.warning(
                f"[close_pipeline] {label:%m-%d %H:%M} 次批超预算仍有 {len(lag)} 个"
                f"未定稿品种 {lag} → 交 :35 兜底扫描"
            )
    return summary
