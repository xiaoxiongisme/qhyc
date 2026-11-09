# -*- coding: utf-8 -*-
"""V5X.3 小时级信号扫描（C3-min：只推送，不建单、不回写持仓）。

设计要点
========
1. **无状态回放**：每次扫描重放最近 ``LOOKBACK_DAYS`` 天的 60m bar，取**最后一根
   已收盘 bar** 的订单事件。不持久化 ``SymbolEngine`` 内部状态 —— 于是既没有
   「状态表与真实脱节」的第二份真相，也不会因进程重启/漏跑而漂移。
   代价是每轮重算 ~3 千根 bar × 38 品种，实测可接受。

2. **t-1 门控**（``gate_lag=1`` + ``regime_mode="strict"``）：三道日频门
   （hvol / 簇冲击 / 体制）只用 ≤ T-1 的日线聚合，全天恒定。这是小时级扫描
   唯一可复现的口径；``gate_lag=0`` 的回测口径要等当日收盘，盘中算不出来。

3. **出口价格反解**：V5X 信号活在 BA 后复权空间（``ba = raw + adj_offset``，
   强品种会转负，如 JD888 实测 ``ba.close = -462``）。推送**必须**给真实可成交价：
   ``real = ba - adj_offset``，其中 ``adj_offset = ba_close - raw_c``（取信号那根
   bar 自带的两列相减，无需再查表）。BA 价同时保留在日志里便于对账。

4. **只读**：本模块**不**写 ``execution_order``、**不**写 ``fusion_position``，
   也不写 ``execution_position``。下单与持仓同步都还没接：
   * 下单需要影子对账通过后才谈；
   * 持仓回写已于 2026-11-08 统一收口到 :mod:`app.execution.position_sync` 并
     **默认关闭**（CL-20261108-26，用户拍板「暂时不做到时候」）。
   所以本模块的产出只有两样：``v5x_signal_log`` 一行流水 + 一条微信推送。

5. **2026-11-08 起它是唯一生产推送源**（CL-20261108-27）：V3.4 的
   ``fusion_scan`` 已由开关 ``fusion_v34_live_enabled=false`` 停用。
   两条链物理并存但只有一个在推，避免出现「一边开多、一边平空」。

回放补录
========
:func:`scan_range` 用**同一套重放逻辑**（:func:`_replay_orders`）一次扫出区间内
全部信号，供「以 2026-11-05 夜盘为统计起点」的回放补录用（CL-20261108-28）。
与 :func:`scan_once` 逐小时各取最后一根**完全等价**，但只重放一遍；
信号落库时标 ``source='replay'``，不推送。

去重
====
``dedupe_key = symbol|bar_ts|action|side``，落在 ``public.v5x_signal_log``
（UNIQUE）。同一根 bar 的同一动作只推一次；重跑扫描不会重复轰炸。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import numpy as np
import pandas as pd
from sqlalchemy import text

from app.core.logging import logger
from app.data.tradable_scope import load_tradable_pool
from app.ingest.synthesizer import _SESSION_DAY_BOUNDS
from app.strategies.v5x.context import assign_td, build_ctx, build_cost, load_market_data
from app.strategies.v5x.engine import SymbolEngine
from app.strategies.v5x.params import config_from

__all__ = [
    "V5XSignal",
    "SCAN_MINUTES",
    "SCAN_SECOND",
    "CLOSE_HM",
    "scan_once",
    "scan_range",
    "format_message",
    "run_v5x_scan",
]

#: 回放窗口（自然日）。日频门控要 250 个交易日（≈360 自然日）的振幅/波动，
#: 再加 60m 指标预热（EMA140 ≈ 25 个交易日），540 天足够且留余量。
LOOKBACK_DAYS = 540
#: 单品种最少 bar 数：低于此值视为预热不足，跳过（不产信号）
MIN_BARS = 200
#: 最后一根 bar 的最大年龄（分钟）。超过即视为数据陈旧，**本轮该品种不产信号**。
#: 来源：生产 V3.4 在 2026-09-23 因陈旧 K 线一轮误推 18 条信号，此后加了同类门控。
#: 取 90 分钟：**真实 60m bar 的收盘标签不是整点**（见 SCAN_MINUTES 注释），
#: 最长一段是日盘 10:00→11:15 的 75 分钟，留 15 分钟采集延迟余量。
MAX_BAR_AGE_MIN = 90
#: 扫描触发分钟（每小时的 :05 / :35）。
#:
#: ★★ 为什么不能只跑 :05（2026-11-08 实测修正，CL-20261108-28）
#: ---------------------------------------------------------------
#: 真实 60m bar 的收盘标签**不是整点**，实测生产分布：
#:   日盘 10:00 / 11:15 / 14:15 / 15:00；夜盘 22:00 / 23:00 / 00:00 / 01:00 / 02:00 / 02:30。
#: 于是「每小时只在 :05 扫一次」会这样漏：
#:   · 11:15 那根 → 11:05 时还没收盘，13:05 时已是 110 分钟前（> MAX_BAR_AGE_MIN）→ 跳过；
#:   · 14:15 那根 → 14:05 时还没收盘，15:05 时已被 15:00 那根顶掉 → 永远不被评估。
#: 实证（2026-11-06）：14:15 的 FU888 平仓信号在 :05-only 节奏下**从未被评估**，
#: 而 :35 时刻一跑就出来了。这不是「少推一条」的小事——回放对账会把这条算进去，
#: 于是「回放有、实盘没有」，对账永远对不平。
#: V3.4 一直就是 :05/:35 双跑，V5X 抄成单跑是移植时的疏漏。
#:
#: ★ 2026-11-09（CL-20261109-17）：:05/:35 是**兜底**，不再是主节奏。主节奏改由
#:   ``CLOSE_HM`` + :data:`SCAN_SECOND` 表达（每根 60m 收盘后 10 秒），见下。
SCAN_MINUTES = (5, 35)
#: 收盘后多少秒触发「收盘即时扫描」。
#:
#: 为什么不是 0 秒：``bucket`` 是**收盘时刻**，收盘那一瞬 L0 还没把最后一分钟
#: 落库；留 10 秒给采集/写入的抖动，再由 :func:`app.ingest.close_pipeline.run_close_chain`
#: 做完备性判定与重试，绝不拿半截桶出信号。
SCAN_SECOND = 10
#: 60m 日盘收盘标签（时, 分）—— **从合成器会话网格派生，不另抄一份**。
#: ``_SESSION_DAY_BOUNDS[60] = [60, 135, 315, 360]`` 是相对 09:00 的分钟偏移，
#: 展开即 10:00 / 11:15 / 14:15 / 15:00。两处若各写一遍，下一次改网格必然漂移
#: （历史上 30m 漏 14:45、5m/15m 退回自然整点桶都是这么来的）。
_DAY_CLOSE_HM = tuple((9 + b // 60, b % 60) for b in _SESSION_DAY_BOUNDS[60])
#: 60m 夜盘收盘标签：夜盘从 21:00 起、无休市，段尾随品种不同
#: （23:00 / 01:00 / 02:30 三类）。这里是**全品种并集**——多扫一个不存在的标签
#: 只多一次空转（完备性门会挡掉），少一个就是漏推。
#: 守卫 ``V5X-CLOSE-SCAN-WIRED`` 会拿生产实际出现的夜盘标签反向验证本集合。
NIGHT_CLOSE_HM = ((22, 0), (23, 0), (0, 0), (1, 0), (2, 0), (2, 30))
#: 全部 60m 收盘标签（日盘 + 夜盘并集，按日内时间升序）。
CLOSE_HM: tuple[tuple[int, int], ...] = tuple(sorted(_DAY_CLOSE_HM + NIGHT_CLOSE_HM))
#: V5X 信号价格源（与回测 v5x_backtest.py 默认一致）
SRC = "bar_60m_ba"
#: 实盘口径：t-1 门控 + 无前视体制
GATE_LAG = 1
REGIME_MODE = "strict"
#: 推送失败的重发窗口（小时）。
#:
#: ★ 为什么必须有重发窗口（2026-11-09，CL-20261109-06）
#: ---------------------------------------------------
#: ``_persist`` 用 ``ON CONFLICT (dedupe_key) DO NOTHING`` 去重，推送失败时
#: 那一行**已经落库**，下一轮 ``_persist`` 返回空 —— 若只推「本轮新增」，
#: 这条信号就**永远不会再发**。pushplus 抖一下 = 信号永久丢失，且用户侧
#: 看到的表现是「这个小时没信号」，与「行情没信号」不可区分。这是比「没有
#: 备用通道」更严重的一层退化。
#:
#: 取 2 小时（约 4 个扫描周期）：短故障能在窗口内补上；超过 2 小时的信号
#: 价格早已不可用，补推反而误导下单，宁可让它留在表里靠 ``notify_ok=false``
#: 和 task_run 的 failed 留痕。
RETRY_WINDOW_HOURS = 2.0

#: 「备用通道未配置」的告警只打一次（进程级），避免每小时刷屏。
_NO_FALLBACK_WARNED = False


@dataclass
class V5XSignal:
    """一条待推送信号（价格已反解到真实合约价）。"""

    symbol: str
    action: str                 # OPEN / ADD / CLOSE
    side: str                   # LONG / SHORT
    bar_ts: Any                 # bar 收盘时刻
    adj_offset: float           # BA 与真实价的加性偏移
    price_ba: float
    price_real: float
    lots: float
    stop_ba: float | None = None
    stop_real: float | None = None
    etype: str = ""             # pb=回踩 / bk=突破
    reason: str | None = None   # stop / tp
    net_yuan: float | None = None
    source: str = "live"        # live=实盘推送；replay=历史回放补录（不推送）
    #: 这一条是「上一轮推送失败、本轮补发」而非本轮新产出。
    #: 只用于推送文案打标记，**不参与 dedupe_key**（同一根 bar 的同一动作全局唯一）。
    from_retry: bool = False
    #: 从 ``v5x_signal_log`` 读回来的原始 key。
    #:
    #: ★ 为什么必须存它（2026-11-09 端到端验证抓到的真 bug）
    #:   ``dedupe_key`` 是 ``f"{symbol}|{bar_ts}|{action}|{side}"``，其中 ``bar_ts``
    #:   在落库时是 pandas ``Timestamp``、读回来是 ``datetime``。两者 ``str()``
    #:   在时区/微秒表示上并不保证一致（``Timestamp`` 可能带 ``.000000``）。
    #:   一旦不一致，``_mark_notify`` 的 ``WHERE dedupe_key=:k`` 就匹配 0 行 ——
    #:   信号永远标不成「已送达」，于是在 2 小时窗口内**每轮都重推一次**
    #:   （:05/:35 各一轮 = 连收 4 条重复消息，且都标「补推」）。
    #:   回写一律用表里那个值，不做重算。
    stored_key: str | None = None

    @property
    def dedupe_key(self) -> str:
        # ★ 刻意不含 source：同一根 bar 的同一动作全局只允许一条。
        #   否则「先回放补录、后实盘跑到同一根 bar」会各记一条，统计翻倍。
        # 从库里读回的行必须用原始 key（见 stored_key 的注释），不能重算。
        if self.stored_key:
            return self.stored_key
        return f"{self.symbol}|{self.bar_ts}|{self.action}|{self.side}"


def _td_series(buckets: pd.Series, open_days: np.ndarray) -> np.ndarray:
    """bar 收盘时刻 → 所属交易日（夜盘归次一交易日）。"""
    return assign_td(buckets, open_days)


def _next_scan_time(t: datetime) -> datetime | None:
    """``t`` 之后（含）最近的一个扫描触发时刻。

    两类触发点取最早者（2026-11-09，CL-20261109-17）：

      * **主节奏**：每根 60m 收盘后 :data:`SCAN_SECOND` 秒（``CLOSE_HM``），
        由 ``v5x_close_scan`` 作业驱动，数据链同时刷新；
      * **兜底**：:data:`SCAN_MINUTES` 整分（:05/:35），旧节奏保留 —— 收盘作业
        失败/数据未就绪时，它仍会在下一轮把漏掉的 bar 补上（dedupe 保证不重复推）。

    返回的是**带秒**的时刻：收盘即时扫描是 :10 秒，不能截断到整分，否则
    ``_coverable`` 会把「14:15:10 才扫」误算成「14:15:00 扫」，进而把收盘那根
    错误地判成不可评估。
    """
    cands: list[datetime] = []

    # 兜底节奏：整分
    cur = t.replace(second=0, microsecond=0)
    if cur < t:
        cur += timedelta(minutes=1)
    for _ in range(90):
        if cur.minute in SCAN_MINUTES:
            cands.append(cur)
            break
        cur += timedelta(minutes=1)

    # 主节奏：收盘 + SCAN_SECOND。跨零点/夜盘要用「日期 ±1」构造，
    # 不能直接 replace(hour=...) —— 那样 00:00 与 23:00 会落到错误的日期上。
    for hh, mm in CLOSE_HM:
        for off in (-1, 0, 1):
            c = (t + timedelta(days=off)).replace(
                hour=hh, minute=mm, second=SCAN_SECOND, microsecond=0)
            if c >= t:
                cands.append(c)
    return min(cands) if cands else None


def _complete_max(session, symbols: list[str], now: datetime) -> dict[str, datetime]:
    """每个品种「已收完」的最新分钟标签。

    判据：``l0_raw.minute_bar`` 的 ``ts`` 是**分钟终点**标签（实测 14:15 那根覆盖
    14:14:00→14:15:00），60m 桶 ``bucket`` 也是终点标签，两者可直接比大小 ——
    桶 ``B`` 已收完 ⟺ 存在分钟 ``ts = B``（等价于 ``max(ts) >= B``）。

    为什么必须有这道门（CL-20261109-17）：``synthesize_bars_incremental`` 会写
    **在途半截桶**（docstring 原文「覆盖在途桶」）。实测 2026-11-09 14:07 那一轮
    就把 13:30→14:07 的 60m 半截桶写成了标签 14:15 的 bar。``scan_once`` 原来只按
    ``bucket <= now`` 过滤，于是 **14:35 的扫描会把半截 14:15 当成已收盘 K 线**，
    出的信号还会被 ``dedupe_key`` 永久固化（同一根 bar 的重算结果再也进不来）。
    """
    since = now - timedelta(days=3)
    rows = session.execute(
        text(
            "SELECT symbol, max(ts) FROM public.minute_bar "
            "WHERE symbol = ANY(:syms) AND ts >= :a GROUP BY symbol"
        ),
        {"syms": list(symbols), "a": since},
    ).all()
    out: dict[str, datetime] = {}
    for sym, ts in rows:
        if ts is None:
            continue
        # 统一时区身份：``now`` 可能是 naive（默认 ``datetime.now()``），
        # 而 timestamptz 取回来是 aware —— 直接比大小会 TypeError。
        if now.tzinfo is None and ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        elif now.tzinfo is not None and ts.tzinfo is None:
            ts = ts.replace(tzinfo=now.tzinfo)
        out[str(sym)] = ts
    return out


def _coverable(t: datetime, nxt: datetime | None,
               max_age_min: int = MAX_BAR_AGE_MIN) -> bool:
    """这根 bar 在真实调度节奏下**是否真的会被评估到**。

    判据：存在扫描时刻 ``T`` 满足 ``t <= T < min(下一根 bar 的收盘时刻, t+90min)``，
    且 ``T`` 落在采集窗口内（:mod:`app.ingest.trading_window` 已处理周五夜盘顺延到
    周六凌晨、周日休市这些边界）。

    为什么回放要这么吹毛求疵：回放的意义是「复现当时会推的东西」。若回放把
    实盘永远评估不到的 bar 也算进去，对账报告就会长期多出几条幽灵信号，
    而实盘的缺失反而被这份「更全」的报告掩盖掉。
    """
    from app.ingest.trading_window import is_collection_window

    limit = t + timedelta(minutes=max_age_min)
    if nxt is not None and nxt < limit:
        limit = nxt
    nt = _next_scan_time(t)
    return nt is not None and nt <= limit and is_collection_window(nt)


def _load(session, symbols: list[str] | None, end: date):
    """取数（scan_once / scan_range 共用，避免两处口径漂移）。"""
    if symbols is None:
        symbols = [f"{v}888" for v in sorted(load_tradable_pool(session))]
    if not symbols:
        return None, []
    start = end - timedelta(days=LOOKBACK_DAYS)
    md = load_market_data(session, symbols, start, end, src=SRC,
                          regime_mode=REGIME_MODE, gate_lag=GATE_LAG)
    return md, symbols


def _replay_orders(md, sym: str, df: pd.DataFrame, cfg, holder: dict | None = None):
    """逐根 bar 重放状态机，yield ``(idx, bar_ts, adj_offset, orders)``。

    **实时扫描与历史回放的唯一重放实现**。两处若各写一遍，迟早出现
    「回放能对账、实盘对不上」这种最难查的分歧（参数/预热/换月处理任一细节都会漂）。

    关于成本参数 ``build_cost(sym, 最后一个交易日)``：取 ``df`` 末根 bar 的日期，
    即「按当前费率」算，**不按被回放那根 bar 的历史费率**。这是刻意的 —— 回放是为了
    复现「如果今天用这套参数跑到那根 bar 会出什么信号」，不是复刻当年的真实成本。

    ``holder``（可选）：传入 dict 时会把 ``engine`` 塞进去，供调用方在重放结束后
    读取 ``state / lots / avg_px``。持仓快照（:mod:`app.strategies.v5x.snapshot`）
    靠它复用同一条重放路径，而不是再写一遍循环（CL-20261109-08）。
    """
    tds = _td_series(df["bucket"], md.open_days)
    # ★ 每根 bar 用**自己那根**的偏移量（BA 是加性偏移，随时间变化）。
    #   取最后一根去反解所有历史 bar 会把早年价格算成负几万 —— 这是 BA 空间最容易踩的坑。
    offs = (df["close"].astype(float) - df["raw_c"].astype(float)).to_numpy()
    eng = SymbolEngine(sym, cfg, build_cost(sym, df["bucket"].iloc[-1].date()))
    if holder is not None:
        holder["engine"] = eng
    prev_td = None
    for i in range(len(df)):
        td = tds[i]
        if td != prev_td:
            eng.set_daily_ctx(build_ctx(md, sym, td))
            prev_td = td
        r = df.iloc[i]
        orders = eng.on_bar(r["bucket"], float(r["open"]), float(r["high"]),
                            float(r["low"]), float(r["close"]),
                            float(r["volume"]), float(r["raw_c"]))
        yield i, r["bucket"], float(offs[i]), orders


def _to_signals(sym: str, ts, off: float, orders: list[dict],
                price_off: float | None = None,
                source: str = "live") -> list[V5XSignal]:
    """把引擎订单事件转成待推送信号（价格反解到真实合约价）。

    ``price_off`` 为 None 时用 ``off`` 本身；分开传是为了让调用方明确
    「入场价用哪根 bar 的偏移」与「止损用哪根 bar 的偏移」是同一个量。
    """
    o_off = off if price_off is None else price_off
    out: list[V5XSignal] = []
    for o in orders:
        act = str(o.get("type"))
        d = int(o.get("dir") or 0)
        side = "LONG" if d > 0 else "SHORT"
        p_ba = float(o.get("price"))
        stop_ba = o.get("stop")
        out.append(V5XSignal(
            symbol=sym,
            action=act,
            side=side,
            bar_ts=ts,
            adj_offset=o_off,
            price_ba=p_ba,
            price_real=float(p_ba - o_off),
            lots=float(o.get("lots") or 1.0),
            stop_ba=float(stop_ba) if stop_ba is not None else None,
            stop_real=float(stop_ba - o_off) if stop_ba is not None else None,
            etype=str(o.get("etype") or ""),
            reason=o.get("reason"),
            net_yuan=float(o["net"]) if "net" in o else None,
            source=source,
        ))
    return out


def scan_once(session, symbols: list[str] | None = None, *,
              now: datetime | None = None,
              overrides: dict | None = None) -> list[V5XSignal]:
    """扫描一轮，返回**最后一根已收盘 bar** 产生的信号（未去重）。

    Args:
        symbols: 品种池；``None`` 时取 ``l3_ref.tradable_variety`` 的 38 个白名单主连。
        now: 判定「已收盘」的基准时刻，默认当前时间。``bucket <= now`` 才参与。
    """
    now = now or datetime.now()
    md, symbols = _load(session, symbols, (now + timedelta(days=1)).date())
    if not md or not md.bars:
        logger.warning(f"[v5x.live] 未取到任何 bar（src={SRC}）→ 本轮不扫描")
        return []

    cfg = config_from(overrides or {})
    sigs: list[V5XSignal] = []
    skipped: list[str] = []
    n_partial = 0
    # 完备性门（CL-20261109-17）：只有分钟数据覆盖到桶尾的 bar 才算「已收盘」。
    # 拿不到某品种的 L0 记录时退化为 now（不额外限制），但会把次数记进 diagnostics ——
    # 静默退化会让整池「看起来都有信号」或「看起来都没信号」，两种情况都查不出原因。
    complete_max = _complete_max(session, symbols, now)

    for sym in symbols:
        df = md.bars.get(sym)
        if df is None or df.empty:
            skipped.append(sym)
            continue
        # 只吃已收盘的 bar：bucket 是收盘时刻，未收盘的那根不进状态机。
        # cap = min(now, 分钟数据已覆盖到的位置) —— 后者挡掉在途半截桶。
        cap = now
        cm = complete_max.get(sym)
        if cm is not None and cm < cap:
            cap = cm
        n_dropped = int((df["bucket"] > cap).sum())
        if n_dropped:
            n_partial += 1
        df = df[df["bucket"] <= cap].reset_index(drop=True)
        # ★ 只吃「合法 60m 网格标签」（2026-11-09，CL-20261109-19）。
        #   合成器的夜盘分支把**在途半截桶**的标签钳到 ``seg_last + 1min``，实测夜盘
        #   会出现 21:01 / 21:31 / 21:38 / 22:01 这类标签（白天分支不会，恒落在网格
        #   边界上）。这类 bar 的 bucket 远小于 cap（22:01 ≤ 23:00），完备性门挡不住，
        #   状态机会把它当成一根真实的 1 分钟 K 线 → 指标与信号全被污染，且
        #   dedupe_key 会把错误结果永久固化。标签白名单是最省事又最硬的过滤：
        #   合法的 60m 收盘标签只有 CLOSE_HM 这些，其余一律不是「一根 K 线」。
        legal = set(CLOSE_HM)
        mask = df["bucket"].map(lambda b: (b.hour, b.minute) in legal)
        n_offgrid = int((~mask).sum())
        if n_offgrid:
            n_partial += 1
            logger.debug(f"[v5x.live] {sym} 丢弃 {n_offgrid} 根非网格标签 bar"
                         f"（在途半截桶）")
        df = df[mask].reset_index(drop=True)
        if len(df) < MIN_BARS:
            skipped.append(sym)
            continue

        # 新鲜度门：陈旧 K 线一律不产信号（宁可漏推，不可用过期数据误导下单）
        last_ts = df["bucket"].iloc[-1]
        age_min = (now - pd.Timestamp(last_ts).to_pydatetime()).total_seconds() / 60.0
        if age_min > MAX_BAR_AGE_MIN:
            logger.warning(f"[v5x.live] {sym} 最后一根 bar 陈旧 {age_min:.0f} 分钟"
                           f"（>{MAX_BAR_AGE_MIN}）→ 跳过")
            skipped.append(sym)
            continue

        last = None
        for i, ts, off, orders in _replay_orders(md, sym, df, cfg):
            last = (ts, off, orders)
        if last is None:
            continue
        ts, off, orders = last
        if orders:
            sigs.extend(_to_signals(sym, ts, off, orders))

    logger.info(f"[v5x.live] 扫描完成：{len({s.symbol for s in sigs})} 品种出信号，"
                f"{len(skipped)} 品种跳过（预热不足/无 bar/数据陈旧）；"
                f"{n_partial} 品种因分钟数据未覆盖到桶尾而降级到最后已收完的桶")
    return sigs


def scan_range(session, symbols: list[str] | None = None, *,
               start: datetime, end: datetime | None = None,
               overrides: dict | None = None,
               coverable_only: bool = True) -> list[V5XSignal]:
    """**一次重放**返回 ``[start, end]`` 区间内全部信号（历史回放补录用）。

    为什么不「对每个小时点各调一次 :func:`scan_once`」：那要重放 N 次 540 天数据，
    38 品种 × 12 小时点 ≈ 456 次全量重放，纯浪费。状态机从同一份数据横扫一遍，
    产生的订单事件序列与「逐时刻扫描各取最后一根」**完全等价**——因为 scan_once
    每轮也都是从头重放、只取最后一根。

    Args:
        coverable_only: 只保留「真实调度节奏下会被评估到」的 bar
            （见 :func:`_coverable`）。**默认 True 且不要关**：
            关掉会把实盘永远评估不到的 bar 也算进来，回放报告长期多出幽灵信号，
            反而掩盖实盘的缺失。

    预热门与实时一致：该 bar 之前不足 :data:`MIN_BARS` 根的一律不产信号
    （实时在那些时点同样会被 `len(df) < MIN_BARS` 拦掉）。
    """
    end = end or datetime.now()
    md, symbols = _load(session, symbols, (end + timedelta(days=1)).date())
    if not md or not md.bars:
        logger.warning(f"[v5x.live] 回放未取到任何 bar（src={SRC}）")
        return []

    cfg = config_from(overrides or {})
    sigs: list[V5XSignal] = []
    n_bar = 0
    n_skip_cover = 0

    for sym in symbols:
        df = md.bars.get(sym)
        if df is None or df.empty:
            continue
        df = df[df["bucket"] <= end].reset_index(drop=True)
        if len(df) < MIN_BARS:
            continue

        for i, ts, off, orders in _replay_orders(md, sym, df, cfg):
            if not orders:
                continue
            if i + 1 < MIN_BARS:            # 预热不足：实时同样不会出信号
                continue
            t = pd.Timestamp(ts).to_pydatetime()
            if t < start or t > end:
                continue
            if coverable_only:
                nxt = (pd.Timestamp(df["bucket"].iloc[i + 1]).to_pydatetime()
                       if i + 1 < len(df) else None)
                if not _coverable(t, nxt):
                    n_skip_cover += 1
                    continue
            n_bar += 1
            sigs.extend(_to_signals(sym, ts, off, orders, source="replay"))

    logger.info(f"[v5x.live] 回放完成 {start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}："
                f"{n_bar} 根 bar 出信号，共 {len(sigs)} 条"
                + (f"（另有 {n_skip_cover} 条因真实调度节奏下不可覆盖而排除）"
                   if n_skip_cover else ""))
    return sigs


def _persist(session, sigs: Iterable[V5XSignal],
             source: str = "live") -> list[V5XSignal]:
    """写信号日志并返回**本次新增**的那些（已推过的自动过滤）。

    ``source``：``live`` = 实盘扫描（会推送）；``replay`` = 历史回放补录
    （只落库 + 出对账报告，不推送）。去重键不含 source —— 同一根 bar 的同一动作
    全局只允许一条，这样「先回放、后实盘跑到同一根 bar」不会重复记账
    （CL-20261108-28）。
    """
    new: list[V5XSignal] = []
    for s in sigs:
        row = session.execute(text("""
            INSERT INTO public.v5x_signal_log
                (dedupe_key, symbol, bar_ts, action, side, price_ba, adj_offset,
                 price_real, stop_ba, stop_real, lots, etype, reason, net_yuan, source)
            VALUES (:k, :sym, :ts, :act, :side, :pba, :off, :preal,
                    :sba, :sreal, :lots, :etype, :reason, :net, :src)
            ON CONFLICT (dedupe_key) DO NOTHING
            RETURNING id
        """), {
            "k": s.dedupe_key, "sym": s.symbol, "ts": s.bar_ts, "act": s.action,
            "side": s.side, "pba": s.price_ba, "off": s.adj_offset,
            "preal": s.price_real, "sba": s.stop_ba, "sreal": s.stop_real,
            "lots": s.lots, "etype": s.etype, "reason": s.reason,
            "net": s.net_yuan, "src": source,
        }).first()
        if row is not None:
            new.append(s)
    return new


def _load_unsent(session, now: datetime,
                 *, hours: float = RETRY_WINDOW_HOURS) -> list[V5XSignal]:
    """取「已落库但尚未确认送达」的信号 = 本轮新增 + 之前推送失败的（重发队列）。

    判据 ``notify_ok IS NOT TRUE`` 覆盖两种状态：
      · 刚由 ``_persist`` 落库 → ``notify_ok`` 为 NULL（还没试过）；
      · 上一轮推送失败 → ``notify_ok = false``（还没送达）。

    ★ 必须排除 ``source <> 'live'``：回放补录的行 ``notify_ok`` 同样是 NULL
      （回放按设计不推送），若不过滤，某天重跑回放会把一堆历史信号当「未送达」
      一次性补推到微信。

    ★ 必须限制窗口：超过 :data:`RETRY_WINDOW_HOURS` 的信号价格已不可用，
      补推等于误导下单。宁可留痕（``notify_ok=false`` + task_run failed）。
    """
    since = now - timedelta(hours=hours)
    rows = session.execute(text("""
        SELECT dedupe_key, symbol, action, side, bar_ts, adj_offset, price_ba,
               price_real, lots, stop_ba, stop_real, etype, reason, net_yuan,
               (notify_ok IS FALSE) AS was_failed
          FROM public.v5x_signal_log
         WHERE notify_ok IS NOT TRUE
           AND source = 'live'
           AND bar_ts >= :since
           AND bar_ts <= :now
         ORDER BY bar_ts, symbol, action
    """), {"since": since, "now": now}).mappings().all()

    def _f(v):
        return None if v is None else float(v)

    return [V5XSignal(
        symbol=r["symbol"],
        action=r["action"],
        side=r["side"],
        bar_ts=r["bar_ts"],
        adj_offset=float(r["adj_offset"] or 0.0),
        price_ba=float(r["price_ba"]),
        price_real=float(r["price_real"]),
        lots=float(r["lots"] or 1.0),
        stop_ba=_f(r["stop_ba"]),
        stop_real=_f(r["stop_real"]),
        etype=r["etype"] or "",
        reason=r["reason"],
        net_yuan=_f(r["net_yuan"]),
        source="live",
        # notify_ok 为 NULL = 本轮刚落库；为 FALSE = 上轮推送失败，本轮补发
        from_retry=bool(r["was_failed"]),
        # 回写标记必须用库里那个 key，不能拿 bar_ts 重算（见 stored_key 注释）
        stored_key=r["dedupe_key"],
    ) for r in rows]


def _mark_notify(session, sigs: Iterable[V5XSignal], ok: bool, via: str) -> None:
    for s in sigs:
        session.execute(text("""
            UPDATE public.v5x_signal_log
               SET notify_ok = :ok, notify_via = :via
             WHERE dedupe_key = :k
        """), {"ok": ok, "via": via, "k": s.dedupe_key})


def format_message(sigs: list[V5XSignal], *, retry: bool = False) -> tuple[str, str]:
    """生成推送标题与正文（价格一律真实合约价，BA 价附在括号里备查）。

    Args:
        retry: 这是「上一轮推送失败后的补发」而非本轮新信号。标题加标记、
            正文加警示 —— 补发的是旧 bar，价格可能已经不可用，
            不能让他当成刚出的信号去下单。
    """
    n_open = sum(1 for s in sigs if s.action == "OPEN")
    n_close = sum(1 for s in sigs if s.action == "CLOSE")
    n_add = sum(1 for s in sigs if s.action == "ADD")
    tag = " 补推" if retry else ""
    title = f"[V5X{tag}] {len(sigs)} 条信号（开{n_open}/加{n_add}/平{n_close}）"

    lines: list[str] = []
    icon = {"OPEN": "🚨", "ADD": "➕", "CLOSE": "✅"}
    for s in sorted(sigs, key=lambda x: (x.action != "OPEN", x.symbol)):
        tag_line = f"{icon.get(s.action, '•')} {s.action} {s.side}"
        lines.append(f"{tag_line}  {s.symbol} @ {s.price_real:,.2f}"
                     f"（BA {s.price_ba:,.2f}，偏移 {s.adj_offset:+,.2f}）")
        if s.stop_real is not None:
            lines.append(f"   ⛔止损 {s.stop_real:,.2f}")
        if s.action == "CLOSE":
            lines.append(f"   原因 {s.reason or '-'}，净 {s.net_yuan:+,.0f} 元")
        elif s.action == "OPEN":
            lines.append(f"   路径 {'回踩' if s.etype == 'pb' else '突破'}，手数 {s.lots:g}")
        lines.append(f"   bar {s.bar_ts}")
    if retry:
        lines.append("")
        lines.append("⚠️ 补推：以上 bar 的收盘时刻早于本次推送（前一轮推送失败，已自动补发）。"
                     "价格可能已不可用，请勿直接照此下单。")
    lines.append("")
    lines.append("口径：V5X.3 · t-1 门控（hvol/簇冲击/体制均只用 ≤T-1 日线），"
                 "BA 后复权信号 + 真实价反解；同根收盘成交假设，偏乐观。")
    lines.append("⚠️ 本策略只出信号，不下单、不维护持仓；是否交易由你决定。")
    return title, "\n".join(lines)


def run_v5x_scan(session, *, now: datetime | None = None,
                 do_push: bool = True, overrides: dict | None = None) -> dict[str, Any]:
    """扫描 + 去重落库 + 推送（含失败补发）。返回摘要（供调度器写 task_run / 日志）。

    ★ 顺序刻意是「先落库 → 再推 → 最后标记」，而不是「先推、成功才落库」：
      落库是事实记录，推送是可失败的外设。反过来的话，推送失败就什么都不留，
      事后既无法统计信号、也无法补发（CL-20261109-06）。

    返回字段：
      n_signals  本轮扫描产出的信号数（含已被去重的历史 bar）
      n_new      本轮**新增**落库的条数（0 = 扫到的全是以前落过的旧 bar）
      n_unsent   实际进入推送的条数（新增 + 窗口内未送达的补发）
      pushed     是否送达（``False`` 且 ``n_unsent > 0`` = 需要告警）
      via        实际通道：pushplus / webhook / none
      retry      本次推送里是否含补发内容
    """
    now = now or datetime.now()
    sigs = scan_once(session, now=now, overrides=overrides)
    new = _persist(session, sigs) if sigs else []
    session.commit()

    if not do_push:
        return {"n_signals": len(sigs), "n_new": len(new), "n_unsent": 0,
                "pushed": False, "retry": False}

    # 不能只推 ``new``：上一轮推送失败的行**已经在表里**，``_persist`` 会因
    # dedupe_key 冲突返回空 → 只推 new 的话它永远发不出去（详见 _load_unsent）。
    unsent = _load_unsent(session, now)
    if not unsent:
        logger.info(f"[v5x.live] 本轮 {len(sigs)} 条信号均已送达 → 不重复推送")
        return {"n_signals": len(sigs), "n_new": len(new), "n_unsent": 0,
                "pushed": False, "retry": False}

    # 补推按**每行自己的状态**判（notify_ok IS FALSE），不是「队列条数 > 新增数」：
    # 后者在「本轮新增也被去重」时算不准（new=0 而队列=1，会误判成整批都是补发）。
    n_retry = sum(1 for s in unsent if s.from_retry)
    retry = n_retry > 0
    if retry:
        logger.warning(f"[v5x.live] 补发 {n_retry} 条上轮未送达的信号"
                       f"（{RETRY_WINDOW_HOURS:g}h 窗口内）")

    from app.core.config import get_settings
    from app.notify import send_notify

    # 备用通道：与 V3.4（scheduler._fusion_scan_job）共用同一个配置项。
    # V5X 接管唯一推送源后若漏传这一项，pushplus 一旦限流/故障就没有降级路径
    # —— 移植时正是漏了这处（CL-20261109-06）。
    fallback = getattr(get_settings().fusion, "fallback_webhook", "") or ""

    # 配了参数 ≠ 有降级通道：`fallback_webhook` 留空时 send_notify 直接跳过降级。
    # 生产实测为空（2026-11-09），所以要显式告警一次 —— 否则「代码看起来修好了」
    # 会让人误以为有双通道。只打一次，避免每小时刷屏。
    global _NO_FALLBACK_WARNED
    if not fallback and not _NO_FALLBACK_WARNED:
        logger.warning(
            f"[v5x.live] fusion.fallback_webhook 未配置 → pushplus 是唯一通道。"
            f"推送失败时靠下一轮补发（窗口 {RETRY_WINDOW_HOURS:g}h），无降级通道。"
            f"如需双通道，在 config 里填企业微信/钉钉/飞书群机器人 URL。")
        _NO_FALLBACK_WARNED = True

    title, content = format_message(unsent, retry=retry)
    ok, via = send_notify(title, content, fallback)
    _mark_notify(session, unsent, ok, via)
    session.commit()
    logger.info(f"[v5x.live] 推送 {len(unsent)} 条（{via}"
                f"{'，含补发' if retry else ''}）：ok={ok}")
    return {"n_signals": len(sigs), "n_new": len(new), "n_unsent": len(unsent),
            "pushed": bool(ok), "via": via, "retry": retry}
