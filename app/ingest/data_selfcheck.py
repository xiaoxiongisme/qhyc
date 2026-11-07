# -*- coding: utf-8 -*-
"""V4 · 数据质量自检（数据层日终作业）。

来源：《PRD_借鉴落地验证.md》V4 + 《架构设计_CB执行版.md》T19（cron 17:35，
排在 rank_position 17:30 之后，避开复权/合成竞态）。

检查项
------
1. **缺失比例**：单合约单日 bar 数缺失 > 5% → 报警（lh3:176）
2. **零成交**：成交量连续为 0 但非停板 → 报警
3. **换月断层**：主连 888 大跳空 ∩ 指数连 8888 未同步跳 = 换月断层（正常）；
   若 888 与 8888 **同时**大跳 = 市场真实跳空（正常）；
   若 888 大跳但 8888 **反向/无数据** → 可疑，需人工确认
4. **尾部回退**：源表最新时间戳早于上一交易日 → 报警（应对"尾删重建"竞态）

产出：写入 `anomaly_ticket`（不阻塞决策链，仅留痕）。

配置（`config/*.yaml` 的 data_selfcheck 段，默认关闭）
----------------------------------------------------
    data_selfcheck:
      enabled: false
      missing_pct_alarm: 0.05
      zero_volume_days: 3
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import text

logger = logging.getLogger(__name__)


@dataclass
class SelfCheckConfig:
    enabled: bool = False
    missing_pct_alarm: float = 0.05
    zero_volume_days: int = 3
    #: 缺失判定的基线窗口（**最近 N 个有数据的交易日**，不是自然日）
    baseline_days: int = 5
    #: 参与检查的周期（表 → 该表一个交易日的预期 bar 数；仅作兜底，实际用自适应基线）
    expected_bars: dict = field(default_factory=lambda: {
        "bar_15m": 32,    # 15m：日盘+夜盘约 32 根
        "bar_30m": 16,
        "bar_60m": 8,
        "daily_bar": 1,
    })


def _log_ticket(session, symbol: str, trade_date: date, fld: str,
                note: str, akshare_val=None, diff=None) -> None:
    """写一张异常工单（幂等：同 symbol+date+field 只写一次 pending）。"""
    try:
        session.execute(text("""
            INSERT INTO anomaly_ticket (symbol, trade_date, field, akshare_val,
                                        diff, status, note)
            VALUES (:s, :d, :f, :a, :diff, 'pending', :n)
            ON CONFLICT DO NOTHING
        """), {"s": symbol, "d": trade_date, "f": fld, "a": akshare_val,
               "diff": diff, "n": note})
        session.commit()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        logger.warning(f"[data_selfcheck] 写工单失败（可能缺唯一约束，降级为直接插入）：{e}")
        try:
            session.execute(text("""
                INSERT INTO anomaly_ticket (symbol, trade_date, field, status, note)
                SELECT :s, :d, :f, 'pending', :n
                WHERE NOT EXISTS (
                    SELECT 1 FROM anomaly_ticket
                    WHERE symbol=:s AND trade_date=:d AND field=:f AND status='pending')
            """), {"s": symbol, "d": trade_date, "f": fld, "n": note})
            session.commit()
        except Exception as e2:  # noqa: BLE001
            session.rollback()
            logger.error(f"[data_selfcheck] 写工单再次失败：{e2}")


#: 各候选表的时间列名（按优先级探测）。
#:
#: ★ 2026-10-09（PR review P1-2）：``check_missing`` 原硬编码 ``bucket::date``，
#:   但 ``cfg.expected_bars`` 里的表时间列**并不统一**：
#:     · ``l1_mkt.bar_5m/15m/30m/60m`` → ``bucket``
#:     · ``l0_raw.daily_bar``             → ``trade_date``（**没有 bucket 列**）
#:     · ``l0_raw.hourly_bar``            → ``trade_datetime``
#:     · ``l0_raw.minute_bar``            → ``ts``
#:   查 daily_bar 时 SQL 必然报 ``column "bucket" does not exist`` → 被
#:   ``except`` 吞成 ``continue`` → **该表永远检查不到**。
#:   而上层只看到「0 工单」，与「一切正常」无法区分——属典型静默失效
#:   （上层实测发现 daily_bar 一项从未生效，真实数据缺失也无人告警）。
_TIME_COL_CANDIDATES = ("bucket", "trade_date", "trade_datetime", "ts", "date")


def _time_col(session, table: str) -> str | None:
    """探测该表实际使用的时间列名；找不到返回 ``None``。"""
    try:
        rows = session.execute(text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t"
        ), {"t": table.split(".")[-1]}).fetchall()
    except Exception:  # noqa: BLE001
        rows = []
    cols = {r[0] for r in rows}
    for c in _TIME_COL_CANDIDATES:
        if c in cols:
            return c
    return None


def _tradable_scope_vars(session) -> tuple[bool, list[str]]:
    """品种口径（CL-20261107-01）→ ``(是否过滤, 品种代码列表)``。

    * 开关关闭 / 视图为空 / 查询失败 → ``(False, [])``，即**不过滤**（fail-open）。
      与 ``app/data/tradable_scope.py`` 完全同一语义：口径读不到时按「全部品种」处理，
      不能因为口径表缺失就让日检整体静默停摆。
    * 口径里存的是 ``RB888`` 这类符号，判定时用 ``regexp_replace(symbol,'8888?$','')``
      归一到品种代码，这样 ``bar_*``（RB888）与 ``daily_bar``（RB）两种命名都能匹配。
    """
    try:
        arr = session.execute(
            text("SELECT l3_ref.tradable_scope_array()")).scalar()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        logger.info(f"[data_selfcheck] 读取品种口径失败（按不过滤处理）：{e}")
        return False, []
    if not arr:
        return False, []
    codes = sorted({re.sub(r"8888?$", "", s) for s in arr if s})
    return (True, codes) if codes else (False, [])


def _ref_trading_days(session, before: date, n: int) -> list[date]:
    """参考交易日 = ``daily_bar`` 里 before 之前最近 n 个交易日。

    为什么不能直接用目标表里出现过的日期：周五夜盘的 K 落在**周六** 00:00~02:30，
    ``bar_60m`` 因此有 10-31 这样的「残日」（只有 4 根）。把它算进基线会把中位数
    拉低、并让 ``max > 1.4×min`` 的稳定性门把正常品种全部跳过。
    ``daily_bar`` 是逐交易日的，天然没有残日，是现成的交易日历（5317 天）。
    """
    try:
        rows = session.execute(text(
            "SELECT DISTINCT trade_date FROM daily_bar "
            "WHERE trade_date < :d ORDER BY 1 DESC LIMIT :n"
        ), {"d": before, "n": n}).scalars().all()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        logger.info(f"[data_selfcheck] 读取交易日历失败（退回按表内日期取基线）：{e}")
        return []
    return sorted({r for r in rows if r is not None})


def check_missing(session, cfg: SelfCheckConfig, trade_date: date) -> int:
    """检查各周期表当日 bar 数缺失比例。返回写入工单数。

    ★ 2026-10-09：时间列改为**按表探测**（原先硬编码 ``bucket`` 导致 daily_bar
      这类表**整项检查从未执行**，且失败被 ``except`` 吞成 ``continue``，
      上层只看到"0 工单"）。现在探测不到列时记 ``error`` 并跳过，
      使「检查未执行」与「检查通过」在日志上可区分。

    ★ 2026-11-07（重开日检前修的**两处系统性误报**）
      ------------------------------------------------
      1) **期望值不能是全局硬编码**。原 ``cfg.expected_bars`` 对 bar_60m 统一期望 8 根，
         但无夜盘品种（农产品/部分化工）60m 只有 4~6 根、夜盘到 02:30 的品种又有 10+ 根
         ⇒ 前者**永远**被判缺失。现改为**该 symbol 近 20 个交易日 bar 数的中位数**做基线，
         自动适配品种交易时段；无历史基线的新 symbol 跳过（首日无可比对象，不报）。
      2) **非交易日必须跳过**。周五夜盘跨零点的 K 落在周六 00:00~02:30，
         若把周六当交易日检查，bar_60m 仅 4 根 vs 期望 8 ⇒ 全市场 82 品种齐刷
         「缺失」工单，首次开日检就会灌满 ``anomaly_ticket``，真缺失被噪音淹没。
         （交易日判断由 run() 统一做，见 :func:`run`。）

    ★ 2026-11-07 晚（**第三处系统性误报：基线窗口跨口径切换**）
      ------------------------------------------------------------
      上一版把基线取成「过去 40 个自然日内该 symbol 各日 bar 数的中位数」。
      60m 桶在 2026-10-30 从「自然整点」切成「交易分钟会话」（旧网格一天 12 根，
      其中 3 根是 11:15/14:15/23:01 这类碎片；新网格一天 6 根且无碎片）。
      40 天窗口里旧网格占多数 ⇒ 中位数停在 9，而 11-06 的**正确值**是 6
      ⇒ 38 个白名单品种 + 39 个非白名单品种**全部**被判「缺失 33%」。
      实测 11-06 白名单 38 品种的原始分钟数 12483 / 12507 / 12508（11-04/05/06），
      逐品种平均差 0.0 分钟 —— 数据是完整的，**工单是假的**。

      两处改动：

      * **窗口改短且按「有数据的交易日」取**（``baseline_days``，默认 5）。
        口径切换后 5 天就自适应完，不会像 40 天那样被旧网格长期污染。
      * **基线不稳定则不判定**：窗口内 ``max > 1.4 × min`` 说明样本横跨口径切换、
        或含半日市/长假前后 —— 此时中位数没有代表性，跳过并留痕（info），
        **宁可当天不判，也不许拿错基线刷工单**。真缺口由 ``check_layer_freshness``
        与 ``check_tail_regression`` 兜底，不会因为这条而失守。
      * **只检查口径内品种**：非白名单品种自 CL-20261107-01 起停止派生，
        它们的 bar 数不再增长属于设计行为，不该天天报缺失。口径为空（开关关闭）
        时不过滤（fail-open），与 ``app/data/tradable_scope.py`` 同一语义。
    """
    n = 0
    skipped: list[str] = []
    scope_on, vscope = _tradable_scope_vars(session)
    nb = max(3, int(cfg.baseline_days))
    ref_days = _ref_trading_days(session, trade_date, nb)
    for table, expect in cfg.expected_bars.items():
        tc = _time_col(session, table)
        if tc is None:
            skipped.append(table)
            logger.error(
                f"[data_selfcheck] {table} **缺失检查未执行**：找不到可识别的时间列"
                f"（候选 {_TIME_COL_CANDIDATES}）—— 该表的 bar 缺失无人巡检")
            continue
        days = ref_days
        if not days:
            # 交易日历不可用（daily_bar 还没铺到该日期）：退回该表自身的日期
            try:
                days = sorted(session.execute(text(
                    f"SELECT DISTINCT {tc}::date FROM {table} "
                    f"WHERE {tc} >= :d0 AND {tc} < :d "
                    f"ORDER BY 1 DESC LIMIT :nb"
                ), {"d": trade_date, "d0": trade_date - timedelta(days=40),
                    "nb": nb}).scalars().all())
            except Exception as e:  # noqa: BLE001
                session.rollback()
                logger.error(f"[data_selfcheck] {table} 取基线日期失败：{e}")
                continue
        try:
            rows = session.execute(text(f"""
                WITH daily AS (
                    SELECT symbol, {tc}::date AS d, count(*) AS c
                    FROM {table}
                    WHERE {tc} >= :d0 AND {tc} < :d1
                      AND (:scope_on = false
                           OR regexp_replace(symbol, '8888?$', '') = ANY(CAST(:vscope AS text[])))
                    GROUP BY 1, 2
                ), agg AS (
                    SELECT symbol,
                           percentile_cont(0.5) WITHIN GROUP (ORDER BY c) AS m,
                           min(c) AS lo, max(c) AS hi, count(*) AS ndays
                    FROM daily WHERE d = ANY(CAST(:ref_days AS date[]))
                    GROUP BY 1
                )
                SELECT t.symbol, t.c, agg.m, agg.lo, agg.hi, agg.ndays
                FROM daily t LEFT JOIN agg USING (symbol)
                WHERE t.d = :d
            """), {"d": trade_date,
                   "d0": trade_date - timedelta(days=40),
                   "d1": trade_date + timedelta(days=1),
                   "scope_on": scope_on,
                   "vscope": vscope,
                   "ref_days": days}).fetchall()
        except Exception as e:  # noqa: BLE001
            session.rollback()
            skipped.append(table)
            logger.error(f"[data_selfcheck] {table}（用列 {tc}）缺失检查**未执行**：{e}")
            continue
        unstable = 0
        for sym, c, m, lo, hi, ndays in rows:
            if m is None or m <= 0:
                # 新 symbol / 无历史基线：首日无可比对象，跳过并留痕（不报工单）
                logger.info(
                    f"[data_selfcheck] {table} {sym} 无历史基线（首个交易日？），跳过缺失判定")
                continue
            if ndays is not None and ndays < 3:
                logger.info(
                    f"[data_selfcheck] {table} {sym} 基线样本仅 {ndays} 天（<3），跳过判定")
                continue
            if lo is not None and hi is not None and float(hi) > float(lo) * 1.4:
                # 横跨口径切换 / 含半日市：中位数无代表性
                unstable += 1
                continue
            if c < float(m) * (1 - cfg.missing_pct_alarm):
                _log_ticket(session, sym, trade_date, f"{table}.missing_bars",
                            f"{table} {trade_date} 仅 {c} 根，近 {nb} 个交易日中位 {float(m):.0f} "
                            f"（缺失 {1 - c/float(m):.1%}）",
                            diff=float(float(m) - c) / float(m))
                n += 1
        if unstable:
            logger.info(
                f"[data_selfcheck] {table} 有 {unstable} 个品种基线不稳定"
                f"（窗口内 max>1.4×min，疑似跨口径切换/半日市），本轮不判定")
    if skipped:
        # fail-loud：让「有表没被检查」在结果里显式可见
        logger.error(f"[data_selfcheck] 本轮有 {len(skipped)} 张表**未被检查**：{skipped}")
    return n


def check_zero_volume(session, cfg: SelfCheckConfig, trade_date: date) -> int:
    """成交量连续为 0 的品种（非停板时属异常）。"""
    n = 0
    since = trade_date - timedelta(days=cfg.zero_volume_days)
    try:
        rows = session.execute(text("""
            SELECT symbol, count(*) FILTER (WHERE volume = 0) AS z, count(*) AS c
            FROM daily_bar
            WHERE trade_date > :s AND trade_date <= :d
            GROUP BY symbol
            HAVING count(*) > 0 AND count(*) FILTER (WHERE volume = 0) = count(*)
        """), {"s": since, "d": trade_date}).fetchall()
    except Exception as e:  # noqa: BLE001
        session.rollback()
        # ★ 2026-10-09：升为error。原为 warning + return 0 —— 上层只看到
        # 「0 工单」，与「零成交检查通过」完全一样，属静默失效。
        logger.error(f"[data_selfcheck] 零成交检查**未执行**：{e}")
        return 0
    for sym, z, c in rows:
        _log_ticket(session, sym, trade_date, "daily_bar.zero_volume",
                    f"连续 {c} 个交易日成交量为 0（{since}~{trade_date}）")
        n += 1
    return n


def check_tail_regression(session, cfg: SelfCheckConfig, trade_date: date) -> int:
    """尾部回退：源表最新时间戳是否早于上一交易日（应对尾删重建竞态）。

    ★ 2026-10-09：改用 :func:`_time_col` 探测列名，替代原先"先试 bucket 再试 ts"
      的双层 try（后者对 ``bar_15m`` 能撞对，但表一多就容易漏；且失败仅
      ``warning``，与「检查通过」无法区分）。
    """
    n = 0
    for table in ("bar_15m", "bar_30m", "bar_60m", "minute_bar"):
        tc = _time_col(session, table)
        if tc is None:
            logger.error(f"[data_selfcheck] {table} **尾部回退检查未执行**：无时间列")
            continue
        try:
            mx = session.execute(text(f"SELECT max({tc}) FROM {table}")).scalar()
        except Exception as e:  # noqa: BLE001
            session.rollback()
            logger.error(f"[data_selfcheck] {table}（用列 {tc}）尾部检查**未执行**：{e}")
            continue
        if mx is None:
            logger.error(f"[data_selfcheck] {table} 全表为空，尾部回退检查无从判断")
            continue
        d = mx.date() if hasattr(mx, "date") else mx
        if d < trade_date - timedelta(days=3):
            _log_ticket(session, f"__{table}__", trade_date,
                        f"{table}.tail_regression",
                        f"{table} 最新时间戳 {mx}，早于 {trade_date} 3 天以上"
                        f"—— 疑似上游尾删重建未完成")
            n += 1
    return n


#: L0 的 layer 数据契约（三层）。显式列出而非 `SELECT DISTINCT layer`——
#: 后者是全表扫描（6145 万行），2026-11-07 实测直接把 PostgreSQL 打到 OOM:
#: ``Memory cgroup out of memory: Killed process (postgres) anon-rss:2.0GB``。
_LAYERS = ("continuous", "contract", "index")

#: 分层新鲜度检查的回看窗口（天）。只扫窗口内的 chunk（Timescale 剪枝），
#: 不做全表聚合；某层窗口内**完全无数据**按"长期断更"报（比"落后 N 小时"更重）。
_LAYER_WINDOW_DAYS = 60


def check_layer_freshness(session, cfg: SelfCheckConfig, trade_date: date) -> int:
    """L0 各 layer / L1 / L2 的新鲜度断层检查（2026-11-07 新增）。

    为什么必须加（实测盲区）
    ------------------------
    :func:`check_missing` 只遍历「当日**有数据**的 symbol」——某层或某品种**彻底断更**
    时，它根本不出现在结果集里，于是永远检查不到。实测事故：``l0_raw.minute_bar``
    的 ``layer='index'``（XXXX8888 指数连，供 L2 换月检测的 ratio 门使用）自
    **2026-10-30 15:00 停更**，到 11-07 已 6 个交易日无数据，而全套日检/告警
    全程无任何提示（增量采集只采 ``*888``，8888 的合成步骤从未接入增量链路）。

    ⚠ 查询写法是**安全性要求**，不是风格问题
    ----------------------------------------
    本函数初版用 ``SELECT layer, max(ts), count(DISTINCT symbol) FROM
    l0_raw.minute_bar GROUP BY 1`` —— 全表聚合，2026-11-07 21:58 在生产把
    PostgreSQL 打成 OOM（容器内存触顶，内核 kill postgres，DB 进入 recovery ~10 秒，
    ``dmesg`` 有 ``Memory cgroup out of memory: Killed process (postgres)
    anon-rss:2.0GB``；该系统 11-06 16:54 / 11-07 00:47 已各发生一次同类 OOM）。
    故现在：
      · 基准取 **L1 主连 max(bucket)**（小表、走索引），不碰 L0 全表；
      · 分层查询**限定 ts 窗口**（``_LAYER_WINDOW_DAYS``），靠 Timescale chunk 剪枝；
      · **不做** ``count(DISTINCT symbol)``（hash/sort 吃内存且非必需）。

    判定基准用「L1 主连最新」而非 ``now()``：周末/长假期间所有层一起"旧"，
    用 now() 会集体误报；以最新落盘时间为基准，只抓**层间相对落后**。

    阈值 36 小时：覆盖「夜盘跨零点 + 次日上午无夜盘品种」的正常错位，同时能在
    「某一层整段停更」的第二个交易日就报出来（不必等一周）。

    Returns:
        异常层数（0 = 全部新鲜）。异常只记 error 并返回计数，由调度器侧决定告警口径
        ——本函数**不**写 ``anomaly_ticket``（该表 symbol 语义是"品种"，层名塞进去会
        污染工单列表与下游按品种的查询）。返回 -1 表示检查本身未执行。
    """
    bad = 0
    try:
        ref = session.execute(text(
            "SELECT max(bucket) FROM l1_mkt.bar_60m WHERE symbol LIKE '%888'")).scalar()
        if ref is None:
            logger.warning("[data_selfcheck] L1 无主连数据，跳过新鲜度检查（无法确定基准）")
            return 0
        lo = ref - timedelta(days=_LAYER_WINDOW_DAYS)

        for layer in _LAYERS:
            mx = session.execute(text(
                "SELECT max(ts) FROM l0_raw.minute_bar "
                "WHERE layer = :L AND ts >= :lo"), {"L": layer, "lo": lo}).scalar()
            if mx is None:
                logger.error(
                    f"[data_selfcheck] L0 layer={layer} **长时间无数据**："
                    f"近 {_LAYER_WINDOW_DAYS} 天窗口内零行（基准 {ref}）"
                    f" —— 该层已停止产出，依赖它的下游（换月检测/复权/因子）静默退化")
                bad += 1
                continue
            lag_h = (ref - mx).total_seconds() / 3600.0
            if lag_h > 36.0:
                logger.error(
                    f"[data_selfcheck] L0 layer={layer} 断更：最新 {mx}"
                    f"（落后 L1 主连 {ref} 共 {lag_h:.0f} 小时）"
                    f" —— 该层已停止产出，依赖它的下游（换月检测/复权/因子）静默退化")
                bad += 1

        # L1/L2 相对 L0 continuous 的断层（聚合/物化断链）。
        # 基准同样用小表的 max(bucket)，不用 L0 全表聚合。
        for tbl, tc in (("l1_mkt.bar_60m", "bucket"),
                        ("l2_adj.bar_60m_ba", "bucket"),
                        ("l1_mkt.bar_15m", "bucket")):
            try:
                mx = session.execute(text(f"SELECT max({tc}) FROM {tbl}")).scalar()
            except Exception:  # noqa: BLE001 —— 表不存在属迁移未到位，另行处理
                session.rollback()
                continue
            if mx is None:
                continue
            lag_h = (ref - mx).total_seconds() / 3600.0
            if lag_h > 36.0:
                logger.error(
                    f"[data_selfcheck] {tbl} 断层：最新 {mx} 落后 L1 主连"
                    f" {ref} 共 {lag_h:.0f} 小时 —— 聚合/物化链已断")
                bad += 1
    except Exception as e:  # noqa: BLE001
        session.rollback()
        logger.error(f"[data_selfcheck] 新鲜度检查**未执行**：{e}")
        return -1  # 显式区分「未执行」与「通过」
    return bad


def run(session, cfg: SelfCheckConfig | None = None,
        trade_date: date | None = None) -> dict:
    """执行全部自检，返回 {检查项: 工单数}。cfg.enabled=False 时直接跳过。

    ★ 2026-11-07：**非交易日只跑新鲜度检查**。周五夜盘跨零点的 K 落在周六，
      若把周六当交易日查缺失，会全市场齐刷工单（见 :func:`check_missing` 注释）。
      交易日判断由 ``trade_calendar.is_futures_trading_day`` 统一给出；日历不可用时
      记 warning 并**保守地照常检查**（宁可多查，不可漏查）。
    """
    cfg = cfg or SelfCheckConfig()
    if not cfg.enabled:
        logger.info("[data_selfcheck] 已关闭（data_selfcheck.enabled=false）")
        return {"skipped": True}
    trade_date = trade_date or date.today()

    is_td = True
    try:
        from app.data.trade_calendar import is_futures_trading_day

        is_td = bool(is_futures_trading_day(trade_date))
    except Exception as e:  # noqa: BLE001 —— 日历故障不阻断自检，保守照查
        logger.warning(f"[data_selfcheck] 交易日历不可用，按交易日处理（保守照查）：{e}")

    if not is_td:
        res = {
            "skipped_nontrading": True,
            # 新鲜度与交易日无关，必须照跑（它正是「周末也无人知」的那类盲区）
            "layer_freshness": check_layer_freshness(session, cfg, trade_date),
        }
        res["total"] = sum(v for v in res.values() if isinstance(v, int) and v > 0)
        logger.info(f"[data_selfcheck] {trade_date} 非交易日：跳过 bar 数检查，仅查层新鲜度 → {res}")
        return res

    res = {
        "missing": check_missing(session, cfg, trade_date),
        "zero_volume": check_zero_volume(session, cfg, trade_date),
        "tail_regression": check_tail_regression(session, cfg, trade_date),
        # ★ 2026-11-07：补「缺席者不在结果集里」的盲区（index 层停更 6 天无人知）
        "layer_freshness": check_layer_freshness(session, cfg, trade_date),
    }
    res["total"] = sum(v for v in res.values() if isinstance(v, int) and v > 0)
    logger.info(f"[data_selfcheck] {trade_date} 完成：{res}")
    return res
