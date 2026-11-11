#!/usr/bin/env python
"""qhyc 不变量守卫（Invariant Guard）。

为什么要有这个脚本
==================
2026-11-07 审计反复出现同一类失败模式：**「系统应该是什么样」只存在于人的脑子里**
或一份讨论过但没落地的方案里，于是偏差只能靠人偶然发现。

实测案例（每条都真实发生过）：
  · 白名单 38 vs cloud.yaml 50 vs L1 主连 81 vs 融合信号 41 —— 四套品种口径并存，
    白名单表已落库但下游过滤从未实施，无人察觉；
  · portfolio_equity 建表后写入方从未实现（0 行），组合熔断恒放行；
  · data_selfcheck 双开关打架（switch=t 而 task.enabled=f），日检从未执行；
  · 周度作业 DB 写 `* * 6`（apscheduler 语义=周日），与代码 `day_of_week="sat"` 相反；
  · l0_raw.minute_bar 的 layer='index' 停更 6 个交易日，全套日检零提示。

机制
====
1. 每条**不变量**是一个可执行断言：expected（承诺）vs observed（实测）。
2. 违反时不是一律报红：若 `ops.change_log` 里存在状态为
   proposed/approved/in_progress/deferred 的台账项声明了该 key，
   则降级为 **WAIVED（已知缺口，附台账 ID）**——诚实但不刷屏。
3. 台账项一旦改成 implemented，豁免自动失效，不变量必须真通过。
   → 「欠债可以，但必须挂账；挂账不许假装已还」。
4. 只有 severity=block 且真实 FAIL 才让退出码非零（可接 cron 告警/部署门禁）。

用法
====
    python scripts/ops/guard_invariants.py                # 全部检查，人读表格
    python scripts/ops/guard_invariants.py --json          # 机器读
    python scripts/ops/guard_invariants.py --record        # 结果落库 ops.invariant_check
    python scripts/ops/guard_invariants.py --alert         # FAIL 时推送（带冷却）
    python scripts/ops/guard_invariants.py --only VAR-    # 只跑匹配前缀
    python scripts/ops/guard_invariants.py --no-waiver     # 验收模式：豁免失效

退出码：0 = 无 block 级 FAIL（WAIVED 视为通过但会列出）；1 = 有 block 级 FAIL；2 = 脚本自身异常。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402

from app.core.db import session_scope  # noqa: E402

# 未完成状态 = 允许豁免（欠债已挂账）
OPEN_STATUS = ("proposed", "approved", "in_progress", "deferred")
WHITELIST_SQL = """
    SELECT variety_code FROM l3_ref.tradable_variety
    WHERE effective_from <= CURRENT_DATE
      AND (effective_to IS NULL OR effective_to >= CURRENT_DATE)
"""


@dataclass
class Result:
    key: str
    title: str
    severity: str
    ok: bool
    expected: str
    observed: str
    detail: str = ""
    status: str = "PASS"
    waiver_ref: str = ""


def _hkey(v):
    """把 SQL 参数值归一成可哈希形式（缓存键用）。

    断言里大量出现 ``= ANY(:syms)`` 这类 **list** 参数；直接塞进 dict key 会抛
    ``unhashable type: 'list'``。这不是理论问题：``L1-SESSION-GRID`` 因此整整一天
    报的是「查询失败 → 无法判定」，既不红也不绿，等于这条断言不存在。
    """
    if isinstance(v, (list, set, frozenset)):
        return tuple(sorted(_hkey(x) for x in v))
    if isinstance(v, dict):
        return tuple(sorted((k, _hkey(x)) for k, x in v.items()))
    return v


@dataclass
class Ctx:
    session: object
    whitelist: set[str] = field(default_factory=set)
    cfg_products: set[str] = field(default_factory=set)
    _cache: dict = field(default_factory=dict)

    def scalar(self, sql: str, **params):
        # 缓存键必须含参数：按 layer 循环查同一句 SQL 时，只按 SQL 文本做键
        # 会让三个 layer 全部命中第一次的结果（曾导致 index 断更被误判为 PASS）。
        # 参数里常见 ``ANY(:labs)`` 的 **list**，而 list 不可哈希 → 直接放进 key
        # 会抛 ``unhashable type: 'list'``（L1-SESSION-GRID 就这么「查询失败 →
        # 无法判定」了一整天，而不是红也不是绿）。统一归一成 tuple。
        key = (sql, tuple(sorted((k, _hkey(v)) for k, v in params.items())))
        if key not in self._cache:
            self._cache[key] = self.session.execute(text(sql), params).scalar()
        return self._cache[key]

    def rows(self, sql: str, **params):
        return self.session.execute(text(sql), params).fetchall()


# ---------------------------------------------------------------- 品种口径
def _c_whitelist_nonempty(c: Ctx) -> tuple[bool, str, str, str]:
    n = len(c.whitelist)
    return n > 0, "白名单有效品种 >= 1", f"{n} 个", (
        "" if n else "l3_ref.tradable_variety 为空或全部过期——所有依赖白名单的断言将失效")


def _c_signal_in_whitelist(c: Ctx) -> tuple[bool, str, str, str]:
    """策略产出的信号品种必须 ⊆ 白名单 ∪ 持仓豁免。

    ★ 口径演进（2026-11-09，CL-20261107-01 收口）
    ------------------------------------------------
    旧版只看 ``fusion_signal_log`` **近 7 天**。V3.4 于 2026-11-08 23:58 停用后，
    这个窗口里剩下的 6 个越界品种（AU/CS/LC/PB/RU/ZN）全是停用前写的**历史流水**，
    要从 11-14 才自然滚出去。用「等时间衰减」来让断言转绿，正是本 skill 第 6 条
    铁律禁止的形状（断言必须衡量「现在的行为」）。

    改法：
      1. V3.4 若已停用 → 起点取开关的 ``updated_at``（停用那一刻），而不是 7 天窗口。
         停用之后还冒出新行 = 有人偷偷绕开关跑 V3.4，这比越界更严重。
      2. V3.4 若在跑 → 仍用 7 天窗口（此时的窗口是「当前写入行为」的合理代理）。
      3. **同时检查 ``v5x_signal_log``**：V5X 是 2026-11-08 起的唯一生产推送源，
         老断言只盯 V3.4 的表，等于盯着一台已停机的机器。

    ★ 持仓豁免（CL-20261107-01）：扫描池允许「非白名单但有持仓」的品种继续扫描，
      否则它既不会产出平仓/止损信号，持仓会被永久冻结。所以这类品种出现在信号表里
      是**正确行为**，不是越界。不加这一项，断言会在口径正确时永远 FAIL。
    """
    sw = c.rows("SELECT enabled, updated_at FROM cfg_feature_switch "
                "WHERE switch_key = 'fusion_v34_live_enabled'")
    v34_on = bool(sw[0][0]) if sw else True
    since = (sw[0][1] if sw else None)

    v5x_part = """
        UNION
        SELECT DISTINCT regexp_replace(symbol, '888$', '') AS v, 'v5x' AS src
        FROM v5x_signal_log
        WHERE symbol ~ '^[A-Za-z]+888$' AND bar_ts >= now() - interval '7 days'
    """
    if v34_on or since is None:
        sql = ("SELECT DISTINCT regexp_replace(symbol, '888$', '') AS v, "
               "'fusion' AS src FROM fusion_signal_log "
               "WHERE symbol ~ '^[A-Za-z]+888$' "
               "AND signal_at >= now() - interval '7 days'") + v5x_part
        rows = c.rows(sql)
        scope = "近 7 天"
    else:
        sql = ("SELECT DISTINCT regexp_replace(symbol, '888$', '') AS v, "
               "'fusion' AS src FROM fusion_signal_log "
               "WHERE symbol ~ '^[A-Za-z]+888$' AND signal_at >= :since") + v5x_part
        rows = c.rows(sql, since=since)
        scope = f"V3.4 停用后（{since:%m-%d %H:%M}）"

    got = {r[0] for r in rows}
    held = {r[0] for r in c.rows("""
        SELECT DISTINCT regexp_replace(symbol, '8888?$', '')
        FROM fusion_position WHERE COALESCE(position, 'FLAT') <> 'FLAT'
    """)}
    label = f"策略信号品种 ⊆ 白名单 ∪ 持仓豁免（{scope} + V5X 近 7 天）"
    if not got:
        return True, label, "窗口内无信号（真空通过）", ""
    extra = sorted(got - c.whitelist - held)
    return not extra, label, (
        f"{len(got)} 个品种（持仓豁免 {len(held)}），越界 {len(extra)}"), (
        f"越界品种：{','.join(extra)}" if extra else "")


def _c_config_in_whitelist(c: Ctx) -> tuple[bool, str, str, str]:
    extra = sorted(c.cfg_products - c.whitelist)
    return not extra, "cloud.yaml 主连 ⊆ 白名单", (
        f"{len(c.cfg_products)} 个配置，越界 {len(extra)}"), (
        f"越界品种：{','.join(extra)}" if extra else "")


def _c_whitelist_in_config(c: Ctx) -> tuple[bool, str, str, str]:
    miss = sorted(c.whitelist - c.cfg_products)
    return not miss, "白名单 ⊆ cloud.yaml 主连（不漏扫）", (
        f"缺失 {len(miss)}"), (f"未进配置、永远不会被扫描：{','.join(miss)}" if miss else "")


def _c_l1_in_whitelist(c: Ctx) -> tuple[bool, str, str, str]:
    """L1 主连**新数据**不得来自「非白名单 且 无持仓」。

    ★ 为什么不用「近 N 天窗口」（2026-11-07 修正）
    ------------------------------------------------
    停派生只阻止**新增**，历史行仍在表里；而 ``purge_recent_buckets`` 现在也
    跟着口径收窄（不删非白名单，避免「停派生」偷偷变成「分批删除」）。于是
    「近 3 天存在非白名单行」在停派生后会继续为真好几天 —— 那是**时间衰减**，
    不是「现在的行为」。用窗口断言的后果是：明明已经收口，却要等一周才能
    转 PASS，等的这段时间只能靠豁免遮住。

    改为与**口径生效时刻**比较：非白名单品种的最新桶必须不晚于该时刻。
    生效之后任何一天又给它派生了数据 → 立刻 FAIL。此刻就成立，不依赖衰减。

    ★ 持仓豁免（CL-20261107-01）：非白名单但有持仓的品种必须继续派生，
      否则它不再有新 bar，止损/平仓判定拿不到最新价，持仓被永久冻结。
      不加这一项，断言会在「口径正确」时永远 FAIL。
    """
    eff = c.scalar(
        "SELECT COALESCE(verified_at, updated_at) FROM ops.change_log "
        "WHERE change_id = 'CL-20261107-01'")
    if eff is None:
        # 台账缺失（新环境）→ 退化为保守窗口，宁可晚一点转 PASS 也不误判 PASS
        eff = c.scalar("SELECT now() - interval '3 days'")

    held = {r[0] for r in c.rows("""
        SELECT DISTINCT regexp_replace(symbol, '8888?$', '')
        FROM fusion_position WHERE COALESCE(position, 'FLAT') <> 'FLAT'
    """)}
    rows = c.rows("""
        SELECT regexp_replace(symbol, '888$', '') AS v, max(bucket) AS mx
        FROM l1_mkt.bar_60m
        WHERE symbol ~ '^[A-Za-z]+888$'
        GROUP BY 1
    """)
    late = sorted(
        f"{v}@{mx:%Y-%m-%d %H:%M}" for v, mx in rows
        if v not in c.whitelist and v not in held and mx is not None and mx > eff)
    return not late, "L1 主连新数据 ⊆ 白名单 ∪ 持仓豁免", (
        f"{len(rows)} 个品种，口径生效({eff:%m-%d %H:%M})后仍有新数据的 {len(late)} 个"), (
        f"口径生效后仍在派生：{','.join(late[:15])}" if late else "")


def _c_index_in_scope(c: Ctx) -> tuple[bool, str, str, str]:
    """L1 的 ``*8888`` 指数层新数据必须 ⊆ 口径（CL-20261110-03）。

    为什么单列一条：``VAR-L1-IN-WHITELIST`` 的 SQL 写死 ``symbol ~ '...888$'``，
    **只覆盖主连**，指数层 8888 完全在视野之外。11-10 数据体检实测
    ``l1_mkt.bar_60m`` 基表近 3 天 103 个 symbol、兼容视图 80 个，
    其中 15 个口径外 8888（AG/AL/AU/B/CS/CU/J/LC/LH/NI/PB/RU/SC/SN/ZN）
    在口径生效（11-09 07:43）后一直派生到 11-10 10:00。

    ⚠ 更正体检报告的一处误判：基表 103 / 视图 80 **不是**「断言查了被过滤的
    视图所以假绿」—— ``public.bar_*`` 本来就是按口径过滤的兼容视图，基表保留
    全量是 D1「只导出不删」的要求。真正的盲区是断言只看 ``888$``，
    以及 ``synthesizer._synth_one(layer='index')`` 在 CL-20261107-13 被有意
    设为「不过口径」。CL-20261110-03 已把指数层派生/purge/追平校验统一收窄。

    判据与口径生效时刻比较，衡量「现在是否还在派生」，不靠历史窗口衰减。
    """
    eff = c.scalar(
        "SELECT COALESCE(verified_at, updated_at) FROM ops.change_log "
        "WHERE change_id = 'CL-20261107-01'")
    if eff is None:
        eff = c.scalar("SELECT now() - interval '3 days'")
    n_scope = c.scalar(
        "SELECT count(*) FROM l3_ref.v_tradable_scope WHERE symbol LIKE '%8888'")
    if not n_scope:
        return True, "L1 指数层新数据 ⊆ 口径", (
            "口径视图无 8888 符号（fail-open），跳过本断言"), ""
    rows = c.rows("""
        SELECT symbol, max(bucket) AS mx
        FROM l1_mkt.bar_60m
        WHERE symbol LIKE '%8888' AND bucket >= :eff
          AND symbol NOT IN (SELECT symbol FROM l3_ref.v_tradable_scope)
        GROUP BY 1 ORDER BY 1
    """, eff=eff)
    bad = [f"{s}@{mx:%m-%d %H:%M}" for s, mx in rows]
    return not bad, "L1 指数层新数据 ⊆ 口径", (
        f"口径内 8888 {n_scope} 个，生效({eff:%m-%d %H:%M})后口径外仍在派生的 "
        f"{len(bad)} 个"), (
        f"口径外指数连仍在派生：{','.join(bad[:15])}" if bad else "")


def _c_scope_wired(c: Ctx) -> tuple[bool, str, str, str]:
    """口径**真的被引用**了吗？——直接针对本次事故的根因断言。

    2026-11-07 之前 ``l3_ref.tradable_variety`` 已落库 38 行，但 L1 派生、
    L2 复权、7 个兼容视图**没有一处引用它**。结果「白名单已生效」是一句
    没有任何证据支撑的话，四套口径继续并存。

    本断言检查关键路径的定义文本里是否出现口径来源标识：
      · L2 复权存储过程  → tradable_scope_array
      · public.bar_* 视图 → tradable_variety / tradable_filter_on
    （L1 派生自 2026-11-09 / 067 起已无库层生产者，改由 Python 侧接入，见下方注释）
    任何一处丢失引用即 block FAIL ——「表建了却没人读」不许再静默通过。
    """
    checks = [
        # L1 派生：2026-11-09 CL-20261109-14 / 067 起库层不再有 L1 生产者
        # （sp_build_l1_from_minute 已弃用为「调用即报错」空壳），口径接入点转移到
        # app/ingest/synthesizer.py（scope= 传参，由 tests/ 覆盖）。故此处只剩 L2 复权。
        # 弃用态本身由 MIG-PROC-BASELINE 的 SESSION_GRID_RETIRED token 兜住。
        ("L2 复权 sp_build_l2_back_adjusted",
         "SELECT pg_get_functiondef('l2_adj.sp_build_l2_back_adjusted(text,text,boolean)'::regprocedure)",
         "tradable_scope_array"),
    ]
    bad, seen = [], []
    for label, sql, token in checks:
        try:
            d = c.scalar(sql) or ""
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{label} 读取失败({type(exc).__name__})")
            continue
        if token in d:
            seen.append(label)
        else:
            bad.append(f"{label} 未引用 {token}")

    views = ["public.bar_60m", "public.bar_15m", "access.v_bar_60m_main"]
    for v in views:
        try:
            d = c.scalar(f"SELECT pg_get_viewdef('{v}'::regclass, true)") or ""
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{v} 读取失败({type(exc).__name__})")
            continue
        if "tradable_variety" in d or "tradable_filter_on" in d:
            seen.append(v)
        else:
            bad.append(f"{v} 未引用白名单")

    return not bad, "口径被 L1/L2/视图真实引用", (
        f"{len(seen)}/{len(seen) + len(bad)} 处已接入口径"), (
        "口径未接入（表建了没人读）：" + "; ".join(bad) if bad else "")


def _c_scope_view_healthy(c: Ctx) -> tuple[bool, str, str, str]:
    """口径视图必须存在、非空，且覆盖全部有效白名单品种。

    视图为空 = 所有消费方 fail-open = 收口静默失效。这种「不报错、只是不生效」
    的退化最难发现，所以单独一条 block 断言。
    """
    try:
        n = c.scalar("SELECT count(*) FROM l3_ref.v_tradable_scope") or 0
    except Exception as exc:  # noqa: BLE001
        return False, "口径视图存在且非空", f"读取失败 {type(exc).__name__}", str(exc)[:200]
    if n == 0:
        return False, "口径视图存在且非空", "0 行", (
            "口径视图为空 → 所有消费方 fail-open → 收口静默失效")
    miss = c.rows("""
        SELECT v.variety_code FROM l3_ref.tradable_variety v
        WHERE v.effective_from <= CURRENT_DATE
          AND (v.effective_to IS NULL OR v.effective_to >= CURRENT_DATE)
          AND (v.variety_code || '888') NOT IN (SELECT symbol FROM l3_ref.v_tradable_scope)
    """)
    miss = [r[0] for r in miss]
    return not miss, "口径视图存在且非空", (
        f"{n} 个 symbol，白名单覆盖缺失 {len(miss)}"), (
        f"白名单品种未进口径视图：{','.join(miss)}" if miss else "")


# ---------------------------------------------------------------- 数据新鲜度
def _c_l0_layers_fresh(c: Ctx) -> tuple[bool, str, str, str]:
    """各 layer 是否与 L1 主连落在同一交易日（CL-20261109-03 改口径）。

    ★ 只用窗口查询 + 单列聚合：2026-11-07 21:58 一次
      `SELECT layer,max(ts),count(DISTINCT symbol) FROM l0_raw.minute_bar GROUP BY 1`
      （6145 万行全表聚合）曾把生产 PostgreSQL 打到 OOM 被内核 kill。

    原实现比较「落后小时数 > 36h」，会被休市期的零星桶骗到：2026-11-09 00:00
    `l1_mkt.bar_60m` 有 3 品种/125 手（正常时段 11 品种）的残留桶，把基准拽到
    周一凌晨，而 contract/index 正常停在周五夜盘 02:30 → 判 «落后 46h» 的假红。

    改为交易日口径：两侧时间戳先映射到所属交易日（夜盘 00:00-03:00 归前一交易日，
    周末/节假日归最近交易日），再看中间缺了几个交易日。同一交易日内不再按小时卡——
    36h 阈值本来就容忍盘中滞后，同一交易日最多跨 ~15h，不可能越线。
    盘中停采由 ops_monitor 的 market_age_h 负责，不在这里重复。
    """
    base = c.scalar("SELECT max(bucket) FROM l1_mkt.bar_60m WHERE symbol ~ '^[A-Za-z]+888$'")
    if base is None:
        return False, "L1 主连有数据", "max(bucket)=NULL", "L1 空表，无法建立新鲜度基准"

    days = [r[0] for r in c.rows(
        "SELECT DISTINCT trade_date FROM futures_rule "
        "WHERE trade_date <= CURRENT_DATE ORDER BY trade_date DESC LIMIT 120")]

    def _trade_day(ts):
        d = ts.date() if hasattr(ts, "date") else ts
        if hasattr(ts, "hour") and ts.hour < 3:      # 夜盘跨零点归前一自然日
            d = d - timedelta(days=1)
        prev = None
        for x in sorted(days):
            if x <= d:
                prev = x
            else:
                break
        return prev

    base_day = _trade_day(base) if days else None
    bad, seen = [], []
    for layer in ("continuous", "contract", "index"):
        mx = c.scalar(
            "SELECT max(ts) FROM l0_raw.minute_bar "
            "WHERE layer = :ly AND ts >= now() - interval '14 days'", ly=layer)
        if mx is None:
            bad.append(f"{layer}=窗口内无数据")
            seen.append(f"{layer}=none")
            continue
        if base_day is None:
            # 交易日真源不可用 → 退回旧的 36h 口径（宁可误报，不可漏报）
            lag = (base - mx).total_seconds() / 3600.0
            seen.append(f"{layer}={lag:.1f}h(降级)")
            if lag > 36.0:
                bad.append(f"{layer} 落后 {lag:.0f}h")
            continue
        own_day = _trade_day(mx)
        if own_day is None:
            bad.append(f"{layer} 最新 {mx} 早于交易日窗口")
            seen.append(f"{layer}=out-of-window")
            continue
        gap = sum(1 for x in days if own_day < x <= base_day)
        seen.append(f"{layer}=gap{gap}d")
        if gap > 0:
            bad.append(f"{layer} 缺 {gap} 个交易日（最新 {mx} vs 基准 {base}）")
    return not bad, "各 layer 与 L1 主连同一交易日（交易日缺口 0）", "; ".join(seen), (
        "断层：" + "; ".join(bad) if bad else "")


def _c_l0_index_coverage(c: Ctx) -> tuple[bool, str, str, str]:
    """index 层「最近一个完整交易日」的活跃品种数不得腰斩（CL-20261110-01）。

    ``DATA-L0-LAYERS-FRESH`` 只看各 layer 的 ``max(ts)``：2026-11-09 有 8 个
    8888 品种跑到 23:00 就让整层 PASS，而同期另外 **45 个品种全天只有 1~2 根**
    （``extend_index_8888`` 接缝锚点死锁，见该脚本 ``_seam_anchor`` 文档）。
    这里补上「覆盖广度」维度：**口径内**（``l3_ref.v_tradable_scope`` 里的 38 个
    ``*8888``）品种在最近一个已收盘交易日必须各有 ≥ 30 根。

    为什么不用「全库活跃品种数 / 历史峰值」：峰值 78 里含 25 个早已停更在
    10-30 / 11-06 的口径外老品种（FB/WR/AD/BC…），它们不该被算进分母——
    修复后口径内 38/38 全齐，按全库比却只有 53/78=68%，会永远假红。
    只选「已收盘交易日」是为了不把盘中未走完的今天误判成断更。
    ⚠ 查询窗口化（14 天）+ 按 layer 与本地自然日过滤，禁止对
    ``l0_raw.minute_bar`` 全表 GROUP BY（2026-11-07 21:58 曾把生产 PG 打到 OOM）。
    """
    tdays = [r[0] for r in c.rows(
        "SELECT DISTINCT trade_date FROM futures_rule "
        "WHERE trade_date <= CURRENT_DATE ORDER BY trade_date DESC LIMIT 20")]
    now_sh = c.scalar("SELECT now() AT TIME ZONE 'Asia/Shanghai'")
    today = now_sh.date() if hasattr(now_sh, "date") else date.today()
    intraday = (now_sh.hour, now_sh.minute) < (15, 30)
    if tdays and tdays[0] == today and intraday and len(tdays) > 1:
        target = tdays[1]
    elif tdays:
        target = tdays[0]
    else:
        return False, "官方交易日历可用", "futures_rule 无交易日", "无法选定基准交易日"

    row = c.rows("""
        WITH sc AS (
          SELECT symbol FROM l3_ref.v_tradable_scope WHERE symbol LIKE '%8888'
        ), per AS (
          SELECT b.symbol, count(*) AS n
          FROM l0_raw.minute_bar b JOIN sc USING (symbol)
          WHERE b.layer = 'index'
            AND b.ts >= now() - interval '14 days'
            AND (b.ts AT TIME ZONE 'Asia/Shanghai')::date = CAST(:target AS date)
          GROUP BY 1
        )
        SELECT (SELECT count(*) FROM sc),
               (SELECT count(*) FROM per WHERE n >= 30),
               (SELECT coalesce(array_agg(symbol ORDER BY symbol), ARRAY[]::text[])
                FROM sc WHERE symbol NOT IN (SELECT symbol FROM per WHERE n >= 30))
    """, target=target)
    scope_n, active_n, missing = (row[0] if row else (0, 0, []))
    missing = list(missing or [])
    ratio = active_n / scope_n if scope_n else 0.0
    ok = scope_n >= 20 and ratio >= 0.90
    obs = (f"基准日 {target}: 口径内 8888 活跃 {active_n}/{scope_n}（{ratio:.0%}）"
           + (f"；缺席 {', '.join(missing[:12])}" if missing else ""))
    return ok, "口径内 8888 在最近完整交易日覆盖率 ≥ 90%", obs, (
        "" if ok else f"覆盖塌陷：{obs}；8888 是换月 ratio 门唯一输入，"
                      "断更会让换月检测静默停摆")


def _c_l0_index_hist_coverage(c: Ctx) -> tuple[bool, str, str, str]:
    """index 层近 6 个已收盘交易日不得留历史洞（CL-20261107-11）。

    ``DATA-L0-INDEX-COVERAGE`` 只盯「最近一个完整交易日」。2026-11-02 / 11-03
    因 contract 层残缺只合成出 25 / 65 个 8888 品种；洞一旦变成历史、基准日
    往后挪一天，就再没有任何断言看得见它 —— 换月 ratio 门在那两天静默关闭，
    实测漏检 7 个真实换月（EB/L/MA/PF/PP/PX/TA），直到本轮从 contract 层
    补数并窗口化重建指数后才重算出来。

    所以这里把窗口拉到最近 6 个**官方**已收盘交易日，逐日要求口径内覆盖率
    ≥ 90%。交易日真源 ``futures_rule``（禁止 weekday 猜测）；排除今天是为了
    不把盘中未走完的交易日误判成断更；查询固定 ``ts >= now() - 14 days``
    窗口化，不对 6000 万行基表做全表 GROUP BY。
    """
    days = [r[0] for r in c.rows(
        "SELECT DISTINCT trade_date FROM futures_rule "
        "WHERE trade_date < CURRENT_DATE ORDER BY trade_date DESC LIMIT 6")]
    if not days:
        return False, "官方交易日历可用", "futures_rule 无已收盘交易日", (
            "先跑 scripts/seed_futures_rule.py（禁止退回 weekday 猜测）")
    rows = c.rows("""
        WITH sc AS (
          SELECT symbol FROM l3_ref.v_tradable_scope WHERE symbol LIKE '%8888'
        ), per AS (
          SELECT (b.ts AT TIME ZONE 'Asia/Shanghai')::date AS d, b.symbol,
                 count(*) AS n
          FROM l0_raw.minute_bar b JOIN sc USING (symbol)
          WHERE b.layer = 'index' AND b.ts >= now() - interval '14 days'
          GROUP BY 1, 2
        )
        SELECT d, count(*) FILTER (WHERE n >= 30) AS active
        FROM per GROUP BY 1 ORDER BY 1
    """)
    have = {r[0]: int(r[1]) for r in rows}
    scope_n = int(c.scalar(
        "SELECT count(*) FROM l3_ref.v_tradable_scope "
        "WHERE symbol LIKE '%8888'") or 0)
    bad: list[str] = []
    seen: list[str] = []
    for d in days:
        a = have.get(d, 0)
        seen.append(f"{d:%m-%d} {a}/{scope_n}")
        if scope_n and a / scope_n < 0.90:
            bad.append(f"{d} {a}/{scope_n}")
    ok = scope_n >= 20 and not bad
    obs = f"近 {len(days)} 个已收盘交易日：" + " ".join(reversed(seen))
    return ok, "近 6 个已收盘交易日每日口径内 8888 覆盖率 ≥ 90%", obs, (
        "" if ok else f"历史指数洞：{', '.join(bad)}；8888 是换月 ratio 门唯一输入，"
                      "洞不会自愈，必须从 contract 层补数后窗口化重建")


def _c_bar_grid_valid(c: Ctx) -> tuple[bool, str, str, str]:
    """L1/L2 近 14 天不得有非网格「在途半截桶」，BA 桶集必须是 L1 的子集。

    CL-20261110-02：``synthesizer._session_insert_sql`` 的夜盘分支用
    ``LEAST(网格上界, seg_last + 1min)`` 截断残段。残段若由**数据缺失**（而非
    自然收盘）造成，桶标签会落在 21:01 / 22:31 / 22:59 / 00:38 这类位置：
    实测 11-09 的 8888 指数层品种写出 32 根，另有 57 根 BA 幽灵桶在 L1 里
    已不存在（L1 被 purge 重建后没同步清 BA）。V5X 读的是 BA，而
    ``check_cross_tf_consistency.py`` 只查 L1 → 这层污染完全没人看见。

    合法会话网格标签的分钟数一律是 5 的倍数，故 ``%5<>0`` 能精准抓残段、
    不误杀 10:00/11:15/02:30 等合法桶。BA ⊆ L1 单独判，防「L1 修好了但
    BA 还留着幽灵」。窗口 14 天覆盖 V5X 回看，且靠 Timescale chunk 剪枝。
    """
    bad = {tf: int(n) for tf, n in c.rows("""
        SELECT tf, count(*) FROM (
          SELECT 'l1_5m'  tf FROM l1_mkt.bar_5m    WHERE bucket >= now() - interval '14 days' AND (extract(minute FROM (bucket AT TIME ZONE 'Asia/Shanghai'))::int % 5) <> 0
          UNION ALL SELECT 'l1_15m' FROM l1_mkt.bar_15m   WHERE bucket >= now() - interval '14 days' AND (extract(minute FROM (bucket AT TIME ZONE 'Asia/Shanghai'))::int % 5) <> 0
          UNION ALL SELECT 'l1_30m' FROM l1_mkt.bar_30m   WHERE bucket >= now() - interval '14 days' AND (extract(minute FROM (bucket AT TIME ZONE 'Asia/Shanghai'))::int % 5) <> 0
          UNION ALL SELECT 'l1_60m' FROM l1_mkt.bar_60m   WHERE bucket >= now() - interval '14 days' AND (extract(minute FROM (bucket AT TIME ZONE 'Asia/Shanghai'))::int % 5) <> 0
          UNION ALL SELECT 'ba_5m'  FROM l2_adj.bar_5m_ba  WHERE bucket >= now() - interval '14 days' AND (extract(minute FROM (bucket AT TIME ZONE 'Asia/Shanghai'))::int % 5) <> 0
          UNION ALL SELECT 'ba_15m' FROM l2_adj.bar_15m_ba WHERE bucket >= now() - interval '14 days' AND (extract(minute FROM (bucket AT TIME ZONE 'Asia/Shanghai'))::int % 5) <> 0
          UNION ALL SELECT 'ba_30m' FROM l2_adj.bar_30m_ba WHERE bucket >= now() - interval '14 days' AND (extract(minute FROM (bucket AT TIME ZONE 'Asia/Shanghai'))::int % 5) <> 0
          UNION ALL SELECT 'ba_60m' FROM l2_adj.bar_60m_ba WHERE bucket >= now() - interval '14 days' AND (extract(minute FROM (bucket AT TIME ZONE 'Asia/Shanghai'))::int % 5) <> 0
        ) x GROUP BY 1
    """) if n}
    orphan = int(c.scalar("""
        SELECT count(*) FROM l2_adj.bar_60m_ba a
        WHERE a.bucket >= now() - interval '14 days'
          AND NOT EXISTS (SELECT 1 FROM l1_mkt.bar_60m l
                          WHERE l.symbol = a.symbol AND l.bucket = a.bucket)
    """) or 0)
    ok = not bad and orphan == 0
    detail = ", ".join(f"{k}:{v}" for k, v in sorted(bad.items()))
    obs = (f"非网格桶 {sum(bad.values())} 根"
           + (f"（{detail}）" if detail else "")
           + f"；BA_60m 孤儿 {orphan} 根")
    return ok, "L1/L2 近 14 天桶标签合法且 BA_60m ⊆ L1_60m", obs, (
        "" if ok else f"网格污染：{obs}；V5X 直接读 BA，幽灵桶会占状态机的桶位")


def _c_ba_nonpositive_guarded(c: Ctx) -> tuple[bool, str, str, str]:
    """非正价只能是加法后复权的产物，且必须可被 V5X 反解回真实价。

    CL-20261110-04 核实结论：**不修数据**。``sp_build_l2_back_adjusted`` 用
    ``ba = raw + adj_offset``（等差后复权），2013 年以来多次换月的累计偏移
    可达 -9465 点，于是 BA 出现负价（近月 600 根，白名单 JD888 连续 23 个
    交易日每日 4 根、最低 -820）；``synth_8888`` 同样产出负的 8888 指数连
    （L1 近 30 天 496 根，全部是 ``*8888``）。把它们「改成正数」会破坏
    后复权连续性与全部回测基准，是错的修复。

    真正要防的是「在 BA 空间做比例运算」：CL-20261108-25 已让
    ``v5x/context._daily_frame`` 先用 ``raw_c`` 反解真实价再算
    ``amp=(hh-ll)/c`` 与 ``lret=log(c/c.shift(1))``。本断言锁住三件事：
      · 白名单**主连 888** 的 L1 原始价不得有非正价（负价只允许在复权/指数层）；
      · 白名单 BA 非正价行的 ``adj_offset`` 必须非空（否则反解不出真实价）；
      · ``_daily_frame`` 仍在用 ``raw_c``（防未来有人改回 BA 空间）。
    """
    root = Path(__file__).resolve().parents[2]
    ctx_src = (root / "app" / "strategies" / "v5x" / "context.py").read_text(
        encoding="utf-8", errors="ignore")
    guard = ('"close": bars["raw_c"]' in ctx_src
             and "np.log(" in ctx_src)
    raw_bad = int(c.scalar("""
        SELECT count(*) FROM l1_mkt.bar_60m
        WHERE bucket >= now() - interval '30 days'
          AND symbol ~ '^[A-Za-z]+888$'
          AND (high < low OR open <= 0 OR close <= 0 OR low <= 0)
    """) or 0)
    no_off = int(c.scalar("""
        SELECT count(*) FROM l2_adj.bar_60m_ba b
        WHERE b.bucket >= now() - interval '30 days'
          AND (b.high < b.low OR b.open <= 0 OR b.close <= 0 OR b.low <= 0)
          AND b.symbol IN (SELECT symbol FROM l3_ref.v_tradable_scope)
          AND b.adj_offset IS NULL
    """) or 0)
    ok = guard and raw_bad == 0 and no_off == 0
    obs = (f"V5X 真实价反解={'在' if guard else '丢失'}；"
           f"主连 L1 非正价 {raw_bad} 根；白名单 BA 负价缺 adj_offset {no_off} 根")
    return ok, "非正价仅在复权层且 V5X 已反解真实价", obs, (
        "" if ok else f"比例运算风险：{obs}；log/除法类因子会因负价符号翻转或除零")


def _c_equity_fresh(c: Ctx) -> tuple[bool, str, str, str]:
    """权益流必须够新、且**够长**——否则组合熔断是「开着但空转」。

    ★ 2026-11-09 加严（CL-20261107-07 / CL-20261107-20）
    ------------------------------------------------
    旧版只看「7 天内有没有更新」。但 `portfolio_brake.load_equity_curve` 要求
    至少 ``EQUITY_MIN_DAYS``（20）个交易日样本，否则返回 ``[]`` → 上层**恒等放行**：
    >15% 降半仓 / >25% 清仓两条规则形同不存在，而开关显示 `=t`。
    实测 2026-11-09：`portfolio_equity` 只有 **1 行，停在 2026-10-27**（13 天前），
    样本 1/20 → 熔断完全空转。这正是 position_sync 那次同型的「声明 vs 实际」背离。

    所以一条断言同时判两件事：新鲜度 + 样本量。细节里直接写明「熔断是否真的生效」，
    不让运维从「权益流不新鲜」自己推导出「风控没在跑」。
    """
    n = c.scalar("SELECT count(*) FROM portfolio_equity")
    if not n:
        return False, "portfolio_equity 有记录且 7 天内更新", "0 行", (
            "权益流为空 → 组合熔断恒放行（>15% 半仓 / >25% 清仓全部失效）。"
            "写入方：scripts/ops/upsert_equity.py")
    last = c.scalar("SELECT max(trade_date) FROM portfolio_equity")
    days = c.scalar("SELECT CURRENT_DATE - max(trade_date) FROM portfolio_equity")

    try:
        from app.risk.portfolio_brake import EQUITY_MIN_DAYS
        need = int(EQUITY_MIN_DAYS)
    except Exception:                                     # noqa: BLE001
        need = 20
    brake_on = bool(c.scalar(
        "SELECT enabled FROM cfg_feature_switch "
        "WHERE switch_key = 'portfolio_brake_enabled'"))

    fresh = days <= 7
    enough = int(n) >= need
    eff = ("生效" if (fresh and enough) else
           ("空转：样本 %d/%d" % (int(n), need) if not enough else "读过期曲线"))
    detail = []
    if not fresh:
        detail.append(f"权益流停更 {days} 天 → 熔断读的是过期曲线，回撤判断失真")
    if not enough:
        detail.append(
            f"样本 {int(n)}/{need} 个交易日 < 下限 → `load_equity_curve` 返回 [] → "
            f"熔断**恒等放行**（开关 {'=t 但不会触发任何降仓' if brake_on else '=f'}）")
    detail.append(f"熔断当前：{eff}；写入方 scripts/ops/upsert_equity.py")
    return fresh and enough, "portfolio_equity 新鲜且样本 ≥ 熔断下限", (
        f"{n} 行/{need} 天，最新 {last}（{days} 天前）"), "；".join(detail)


# ---------------------------------------------------------------- 交易日历
def _c_calendar_fresh(c: Ctx) -> tuple[bool, str, str, str]:
    """官方交易日历必须覆盖到「已有数据的最新日」。

    CL-20261107-18：``l3_ref.trade_calendar`` 是 R2 推断表，只有 ``exchange='INF'``
    且止于 2026-10-08；``extend_index_8888.py`` 却按六家真实交易所查它，导致
    「整天零合约行」的坏日门恒空、从上线起就是死代码。真源是 ``futures_rule``
    的 distinct trade_date（akshare futures_rule 播种）+
    ``cfg_calendar_seed_meta.seeded_through``，由 ``seed_futures_rule`` 每日 08:00 刷新。

    断言只看此刻：日历覆盖上界 vs **真实成交日**。基准取 ``daily_bar.max(trade_date)``
    而不是 ``l0_raw.minute_bar.max(ts)::date``——后者会被休市期的零星桶骗到：
    2026-11-09 00:00 有 3 品种/125 手的残留 tick，把「最新数据日」拽到周一凌晨，
    日历明明正确覆盖到 11-06，却被判「落后 3 天」（CL-20261109-03）。
    容差 0：日线日期本身就是交易日，不需要为跨夜盘留 1 天。
    """
    seeded = c.scalar(
        "SELECT value FROM cfg_calendar_seed_meta WHERE key='seeded_through'")
    cal_max = c.scalar("SELECT max(trade_date) FROM futures_rule")
    data_day = c.scalar("SELECT max(trade_date) FROM daily_bar")
    degraded = False
    if data_day is None:
        # daily_bar 空表/迁移期 → 退回 L0，但只认窗口内、且映射回最近交易日
        degraded = True
        raw = c.scalar(
            "SELECT max(ts)::date FROM l0_raw.minute_bar "
            "WHERE layer='continuous' AND ts >= now() - interval '14 days'")
        if raw is not None and cal_max is not None:
            data_day = min(raw, cal_max)
        else:
            data_day = raw
    try:
        st = date.fromisoformat(str(seeded)[:10]) if seeded else None
    except ValueError:
        st = None
    if st is None or cal_max is None or data_day is None:
        return False, "官方日历覆盖到真实成交日", (
            f"seeded={seeded} cal_max={cal_max} data_day={data_day}"), (
            "日历真源缺失 → extend_index_8888 的坏日门无法判定，"
            "先跑 scripts/seed_futures_rule.py")
    cover = max(st, cal_max)
    lag = (data_day - cover).days
    ok = lag <= 0
    src = "l0(降级)" if degraded else "daily_bar"
    return ok, "官方日历覆盖到真实成交日（容差 0）", (
        f"seeded={st} cal_max={cal_max} data_day={data_day}({src}) lag={lag}d"), (
        "" if ok else
        f"官方日历落后 {lag} 天 → 指数扩展的坏日门会静默失效（CL-20261107-18）")


# ---------------------------------------------------------------- 数据层缺陷修复 (CL-20261111-03)
def _c_futures_rule_fresh(c: Ctx) -> tuple[bool, str, str, str]:
    """官方交易日历真源 futures_rule 必须新鲜（CL-20261111-03 / P0-1）。

    诊断实证：遗留的 l3_ref.trade_calendar(INF) 止于 2026-10-08，但生产逻辑
    （data_selfcheck / extend_index_8888 / trading_window）早已改读 futures_rule 的
    distinct trade_date（akshare 每日 08:00 由 seed_futures_rule.py 播种，并回写
    cfg_calendar_seed_meta.seeded_through）。真源若停止播种，extend_index_8888 的坏日门
    会静默失能、换月检测停摆。本断言锁住「真源新鲜度」——这比修那张孤儿表更有价值。
    """
    cal_max = c.scalar("SELECT max(trade_date) FROM futures_rule")
    if cal_max is None:
        return (False, "futures_rule 已播种且覆盖到近 3 个自然日",
                "futures_rule 无数据", "先跑 scripts/seed_futures_rule.py")
    try:
        cal_max_d = date.fromisoformat(str(cal_max)[:10])
    except ValueError:
        return (False, "futures_rule 最新交易日可解析", str(cal_max), "日期格式异常")
    today = c.scalar("SELECT (now() AT TIME ZONE 'Asia/Shanghai')::date")
    if today is None:
        today = date.today()
    lag = (today - cal_max_d).days
    ok = lag <= 3
    return (ok, "futures_rule 覆盖到近 3 个自然日（容差含周末）",
            f"max(trade_date)={cal_max_d} lag={lag}d",
            "" if ok else
            f"官方日历真源落后 {lag} 天 → 指数扩展坏日门静默失能（CL-20261111-03）")


def _c_l2_no_index(c: Ctx) -> tuple[bool, str, str, str]:
    """L2 后复权层不得含任何指数(8888)符号（CL-20261111-03 / P1）。

    sp_build_l2_back_adjusted 的正则是 ^[A-Za-z]+888$（只处理主连 888），8888 指数连
    本就不进 BA（fusion_kline.py:17 注释明确）。L2 由「888 主连 + roll_segment」构成，
    roll_segment 才以 8888 当换月 ratio 门的输入。故「L2 含 8888」是设计违反：任何读 BA
    的策略会把指数当主连复权，污染实盘/回测。本断言把这条设计契约钉死。
    """
    n = c.scalar("""
        SELECT count(*) FROM (
          SELECT symbol FROM l2_adj.bar_5m_ba  WHERE symbol LIKE '%8888' UNION ALL
          SELECT symbol FROM l2_adj.bar_15m_ba WHERE symbol LIKE '%8888' UNION ALL
          SELECT symbol FROM l2_adj.bar_30m_ba WHERE symbol LIKE '%8888' UNION ALL
          SELECT symbol FROM l2_adj.bar_60m_ba WHERE symbol LIKE '%8888'
        ) x
    """)
    n = int(n or 0)
    ok = n == 0
    return (ok, "l2_adj.bar_*m_ba 不含任何 *8888 指数符号",
            f"命中 {n} 行",
            "" if ok else f"L2 后复权层出现 {n} 个 8888 指数符号 → 设计违反（P1）")


def _c_index_8888_seam(c: Ctx) -> tuple[bool, str, str, str]:
    """8888 指数层(后复权)历史(local_csv)与增量(incr)接缝必须连续（CL-20261111-03 / P0-2）。

    8888 指数连是后复权连续合约：close = 主力价 − 累计换月偏移，老品种（JD 2013 上市、
    RU 1990s 等）累计偏移达数千点 → 当下价可为负（设计产物，非损坏）。唯一消费者是换月
    ratio 门（用价格变化量 idx_delta，不受绝对值符号影响）。真正的故障模式是
    extend_index_8888 的 seam delta 算错 → 增量段相对历史整体平移数千点 → 接缝处出现
    远超常态的单步跳变。本断言捕捉该跳变（阈值 2000 点，远高于这类品种的正常单步波动
    <500，不会误报）。

    ★ 2026-11-11 修正（CL-20261111-03）：原实现把「历史段末端 vs 增量段首端」的
      任意两点价差都当接缝跳变，会误伤「跨多日采集缺口后的真实行情变动」。实测 BC8888
      历史止于 2026-10-30、增量起于 2026-11-04（4.9 日缺口），价差 2630 点恰是 BC 主连
      同期真实跌幅（BC888 自 96690→94640，约 −2.1%；8888 含 carry 基线差约 580，远小于
      2000 阈值）。故改为**仅当两端时间相邻（缺口 ≤ 2 日）时**才判接缝跳变；多日缺口下
      的价差是真实行情，不是 seam delta 算错。BC8888 本身的真实缺陷是「增量段冻结于
      11-06 11:30（_extend_index_8888 因 contract 层缺 BC 主连参照而每 30 分钟 abort）」，
      属数据鲜度/断层类缺陷，由 CL-20261111-03 另立条目跟踪修复，不在本接缝断言口径内。
    """
    rows = c.rows("""
        WITH h AS (
          SELECT DISTINCT ON (symbol) symbol, close AS hc, ts AS hts
          FROM l0_raw.minute_bar
          WHERE layer='index' AND src='local_csv_synth_8888'
            AND ts >= now() - interval '400 days'
          ORDER BY symbol, ts DESC
        ), i AS (
          SELECT DISTINCT ON (symbol) symbol, close AS ic, ts AS its
          FROM l0_raw.minute_bar
          WHERE layer='index' AND src='incr_synth_8888'
            AND ts >= now() - interval '60 days'
          ORDER BY symbol, ts ASC
        )
        SELECT count(*) FROM h JOIN i USING (symbol)
        WHERE abs(h.hc - i.ic) > 2000
          -- 仅当历史段末端与增量段首端时间相邻时才判接缝跳变：
          -- 多日采集缺口下的价差是真实行情变动（如 BC8888 4.9 日缺口内跌 2630，
          -- 与主连 BC888 跌幅一致），不是 seam delta 算错。
          AND extract(epoch from (i.its - h.hts)) <= 2 * 86400
    """)
    n = int(rows[0][0]) if rows else 0
    ok = n == 0
    return (ok, "8888 指数层相邻接缝跳变 ≤ 2000 点",
            f"异常品种数 {n}",
            "" if ok else
            f"{n} 个 8888 品种相邻接缝跳变 >2000 点 → seam delta 疑似失效（P0-2）")


def _c_l0_no_offhours_bar(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261109-04：l0 不得出现时间戳落在**非交易时段**的 bar。

    休市期新浪 ``futures_zh_minute_sina`` 会返回一根「时间戳 = 当前时刻」的合成 bar
    （实测 ag0 末尾 11-07 02:30 后紧跟 2026-11-09 00:00，volume=121，OHLC 齐全），
    而 11-09 是周一、**周一凌晨无夜盘**。这根 bar 一路进 l0 → bar_60m → bar_60m_ba，
    占住 V5X 状态机「上一根」的位置参与 EMA/ATR/开平判定，且全程不报错。

    只看近 7 天：固定触发点是「每周一凌晨」，7 天足以覆盖一次；
    不让远古 CSV 导入残留常红（历史脏数据由清理脚本处理，不该挡住当前发布）。
    """
    label = "l0 无时间戳落在非交易时段的 bar（CL-20261109-04）"
    try:
        n = c.scalar("""
            SELECT count(*) FROM (
              SELECT (ts AT TIME ZONE 'Asia/Shanghai') AS lts
              FROM l0_raw.minute_bar
              WHERE layer='continuous' AND ts >= now() - interval '7 days'
            ) b
            WHERE extract(dow FROM lts) = 0                                  -- 周日全天
               OR (extract(dow FROM lts) = 1 AND lts::time < TIME '08:50')   -- 周一开盘前
               OR (lts::time > TIME '02:40' AND lts::time < TIME '08:50')
               OR (lts::time > TIME '15:20' AND lts::time < TIME '20:50')
        """)
        n = int(n or 0)
        ok = n == 0
        return ok, label, f"offhours_bars={n}", (
            "" if ok else
            f"{n} 根 bar 落在非交易时段 → 会污染 60m 与 V5X 状态机。"
            "先确认 _minute_and_bars_job 的 is_collection_window 门没被绕过，"
            "再清理 l0_raw.minute_bar 与 public.bar_60m 的对应行")
    except Exception as exc:                                    # noqa: BLE001
        return False, label, "", f"查询失败 {type(exc).__name__}: {exc}"


def _c_minute_collect_window_gated(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261109-04：两个采集入口都必须真实调用时段门（AST 级，防被删/被注释）。

    为什么要断言源码而不是只看数据：幽灵 bar 只在**每周一凌晨**出现，
    数据侧断言要等到下周一才会转红；若期间有人把门删了，中间几天全是假绿。
    这里直接钉住「作业层 + 采集器层」两处调用点，改坏了当场 FAIL。
    """
    label = "分钟采集作业与采集器都调用 is_collection_window（CL-20261109-04）"
    bad: list[str] = []
    observed: list[str] = []
    try:
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]

        def _calls_gate(path: Path, func: str) -> tuple[bool, int]:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                        and node.name == func:
                    hits = 0
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call):
                            fn = sub.func
                            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                            if name == "is_collection_window":
                                hits += 1
                    return hits > 0, hits
            return False, -1            # 函数本身不存在

        ok_job, n_job = _calls_gate(root / "app" / "scheduler.py", "_minute_and_bars_job")
        ok_col, n_col = _calls_gate(root / "app" / "ingest" / "minute_collector.py",
                                    "_fetch_akshare")
        observed.append(f"job_calls={n_job} collector_calls={n_col}")
        if n_job == -1:
            bad.append("scheduler.py 找不到 _minute_and_bars_job（改名了？断言需同步）")
        elif not ok_job:
            bad.append("_minute_and_bars_job 未调用 is_collection_window → "
                       "休市期会再写幽灵 bar")
        if n_col == -1:
            bad.append("minute_collector.py 找不到 _fetch_akshare（改名了？）")
        elif not ok_col:
            bad.append("MinuteCollector._fetch_akshare 未逐根过滤非交易时段 bar → "
                       "其他调用方可绕过作业层门")
    except Exception as exc:                                    # noqa: BLE001
        bad.append(f"扫描异常 {type(exc).__name__}: {exc}")
    return (not bad), label, " ".join(observed), (
        "" if not bad else "；".join(bad))


def _c_v5x_push_channel(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261109-06：V5X 推送必须①带备用通道 ②保留补发队列 ③返回 n_unsent。

    V5X 已是**唯一**推送源，推送可靠性等价于策略可用性。三处退化全部发生在
    「从 V3.4 移植时漏抄」，而不是设计取舍：

      · **备用通道**：`scheduler.py:771` 的 V3.4 推送传了
        `settings.fusion.fallback_webhook`，V5X 只传了 (title, content)
        → pushplus 限流/故障时没有降级路径。
      · **补发队列**：`_persist` 用 `ON CONFLICT (dedupe_key) DO NOTHING`，
        推送失败时那一行**已经落库**，下一轮 `_persist` 返回空。若只推「本轮新增」，
        这条信号**永远不会再发** —— pushplus 抖一下 = 信号永久丢失，而用户侧
        看到的是「这个小时没信号」。
      · **失败可观测**：见 `_c_v5x_push_failure_loud`。

    为什么断言源码而不是查数据：数据侧只有在真出故障后才有 `notify_ok=false`
    的痕迹，而那时候信号已经丢了。改坏当场红才有意义。
    """
    label = "V5X 推送带备用通道 + 失败可补发（CL-20261109-06）"
    bad: list[str] = []
    observed: list[str] = []
    try:
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        p = root / "app" / "strategies" / "v5x" / "live.py"
        src = p.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(src)

        n_call = n_ok = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
            if name != "send_notify":
                continue
            n_call += 1
            has_pos = len(node.args) >= 3
            has_kw = any(k.arg == "fallback_webhook" for k in node.keywords)
            if has_pos or has_kw:
                n_ok += 1
            else:
                bad.append(
                    f"live.py:{node.lineno} send_notify 未传 fallback_webhook → "
                    f"pushplus 故障时无降级通道")
        observed.append(f"send_notify_calls={n_call} with_fallback={n_ok}")
        if n_call == 0:
            bad.append("live.py 找不到 send_notify 调用（推送被删掉了？）")

        has_requeue = ("notify_ok IS NOT TRUE" in src) and ("source = 'live'" in src)
        observed.append(f"requeue={'yes' if has_requeue else 'no'}")
        if not has_requeue:
            bad.append(
                "live.py 缺少未送达补发查询（notify_ok IS NOT TRUE 且 source='live'）"
                " → 推送失败的信号永远不会再发")

        if '"n_unsent"' not in src and "'n_unsent'" not in src:
            bad.append("run_v5x_scan 未返回 n_unsent → 调度层无法区分"
                       "「有信号但没送达」与「本轮没信号」")

        # 回写标记必须用库里的 key：dedupe_key 里嵌着 bar_ts，落库时是 pandas
        # Timestamp、读回来是 datetime，重算不保证一致 → UPDATE 匹配 0 行 →
        # 信号永远标不成「已送达」，窗口内每轮重推（2026-11-09 实测抓到的 bug）。
        if "stored_key" not in src:
            bad.append("live.py 缺少 stored_key → 从库读回的信号会重算 dedupe_key，"
                       "标记匹配 0 行导致无限重推")
    except Exception as exc:                                    # noqa: BLE001
        bad.append(f"扫描异常 {type(exc).__name__}: {exc}")
    return (not bad), label, " ".join(observed), ("" if not bad else "；".join(bad))


def _c_v5x_push_failure_loud(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261109-06：V5X 作业失败必须外抛，不能 `logger.exception` 后吞掉。

    `ops.scheduled_task_run` 的 success 判据是 apscheduler 的
    `EVENT_JOB_EXECUTED` —— **只要作业函数没抛异常就记 success**。
    2026-11-07 的 pgconn.py 误删事故里，9 个脚本连续 ImportError、流水仍
    11 次全绿，就是这么来的（代码里 9 处 `logger.warning` 写得明明白白，
    监控一条都没报）。2026-11-08 的 V5X 作业又写了一模一样的结尾：
    `except Exception: logger.exception(...)`。

    断言两件事：
      · 至少有 2 处 raise —— 扫描异常外抛 + 推送失败升级；
      · 函数体里出现 `pushed` —— 否则推送失败会被记成 success。
    """
    label = "V5X 扫描作业失败必须外抛（否则流水全绿而信号全丢）"
    bad: list[str] = []
    observed: list[str] = []
    try:
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        p = root / "app" / "scheduler.py"
        src = p.read_text(encoding="utf-8", errors="ignore")
        tree = ast.parse(src)

        seg = ""
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == "_v5x_scan_job":
                seg = ast.get_source_segment(src, node) or ""
                break

        n_raise = 0
        if seg:
            try:
                # 用 AST 数 ast.Raise 节点，不能用字符串 count：
                # 裸 `raise`（重新抛出）后面紧跟换行，count("raise ") 会漏掉它。
                n_raise = sum(1 for x in ast.walk(ast.parse(seg))
                              if isinstance(x, ast.Raise))
            except SyntaxError:
                n_raise = seg.count("raise ")
        observed.append(f"found={'yes' if seg else 'no'} raise={n_raise}")
        if not seg:
            bad.append("scheduler.py 找不到 _v5x_scan_job（改名了？断言需同步）")
        else:
            if n_raise < 2:
                bad.append(
                    f"_v5x_scan_job 里 raise 只有 {n_raise} 处，应 ≥2："
                    f"扫描异常外抛 + 推送失败升级为任务失败")
            if "pushed" not in seg:
                bad.append("_v5x_scan_job 未判断 pushed → 推送失败会被 "
                           "EVENT_JOB_EXECUTED 记成 success")
    except Exception as exc:                                    # noqa: BLE001
        bad.append(f"扫描异常 {type(exc).__name__}: {exc}")
    return (not bad), label, " ".join(observed), ("" if not bad else "；".join(bad))


def _c_v5x_push_single_channel(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261109-06：`fusion.fallback_webhook` 为空 = 推送单通道，是已知单点。

    不是假想风险，2026-11-06 真实发生过：当天 `[alert]` 逐条推送 206 条，
    击穿 pushplus 账号额度后返回 `code=900 用户账号使用受限`，把**共用同一
    token 的交易信号**一起打成静默 —— `fusion_push_log` 从 11-06 11:01 起
    连续 7 条 `delivered=f via=none`，直到 11-08 才恢复。用户侧看到的是
    「这两天没信号」，实际是「信号发不出去」。

    告警刷屏已在 CL-20261108-23 修掉（单条 digest + max_per_day=80），
    但「信号与告警共用额度、且只有一条通道」这个结构没变。V5X 现在是**唯一**
    推送源，配一个企业微信/钉钉/飞书群机器人 URL 就能消掉这个单点。

    定 warn 而非 block：这是配置选择，不是代码缺陷；空值不阻断发布，
    但要在每轮守卫报告里持续可见，直到配上为止。
    """
    label = "推送有降级通道（fusion.fallback_webhook 已配置）"
    bad: list[str] = []
    observed: list[str] = []
    try:
        from app.core.config import get_settings

        fb = getattr(get_settings().fusion, "fallback_webhook", "") or ""
        observed.append(f"fallback={'set' if fb else 'empty'}")
        if not fb:
            bad.append(
                "fusion.fallback_webhook 为空 → pushplus 是唯一通道，且与告警共用额度。"
                "11-06 就因告警刷爆额度导致交易信号静默 2.5 天。"
                "配一个企业微信/钉钉/飞书群机器人 URL 即可消除")
    except Exception as exc:                                    # noqa: BLE001
        bad.append(f"读取配置失败 {type(exc).__name__}: {exc}")
    return (not bad), label, " ".join(observed), ("" if not bad else "；".join(bad))


# ---------------------------------------------------------------- 运行态一致性
def _c_switch_job_consistency(c: Ctx) -> tuple[bool, str, str, str]:
    """代码开关（cfg_feature_switch）与字典开关（ops.scheduled_task）必须同向。

    实测事故：data_selfcheck_enabled=t 但 data_selfcheck_daily.enabled=f，
    后者把作业从调度器移除，日检从未执行。
    """
    rows = c.rows("""
        SELECT s.switch_key, s.enabled AS sw, t.task_id, t.enabled AS tk
        FROM cfg_feature_switch s
        JOIN ops.scheduled_task t
          ON t.task_id = replace(replace(s.switch_key, '_enabled', ''), 'data_selfcheck', 'data_selfcheck_daily')
        WHERE s.switch_key LIKE '%\\_enabled'
    """)
    bad = [f"{r[0]}={r[1]} vs {r[2]}={r[3]}" for r in rows if bool(r[1]) != bool(r[3])]
    return not bad, "开关表与任务字典同向", f"比对 {len(rows)} 对，冲突 {len(bad)}", (
        "双开关打架：" + "; ".join(bad) if bad else "")


def _c_signal_writer_unique(c: Ctx) -> tuple[bool, str, str, str]:
    rows = c.rows("SELECT source, count(*) FROM fusion_signal_log GROUP BY 1")
    got = {r[0]: r[1] for r in rows}
    return len(got) <= 1, "融合信号单一写入方", (
        f"{len(got)} 个 source：{got}"), (
        "出现多个写入方 → 无法证明信号纯由策略产生" if len(got) > 1 else "")


def _fire_weekdays(trigger, n: int = 14) -> set[int]:
    """用真实 next_fire_time 反推触发星期集合（0=周一…6=周日）。

    不看 cron 字符串、不看日志里硬编码的 "sat" 字样 —— 那是
    CL-20261107 周度作业事故和 CL-20261109-21 inventory 停更的共同盲点。
    """
    from datetime import datetime, timedelta  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415
    tz = ZoneInfo("Asia/Shanghai")
    cur = datetime.now(tz).replace(second=0, microsecond=0)
    out: set[int] = set()
    for _ in range(n):
        # now 必须随上次触发推进；若每次传 cur+20 天，20 天窗口内第一次触发
        # 恒落在同一个星期，集合只有 1 个元素 → 断言失去判别力（实测踩过）。
        nxt = trigger.get_next_fire_time(None, cur)
        if nxt is None:
            break
        out.add(nxt.weekday())
        cur = nxt + timedelta(minutes=1)
    return out


#: 每个带星期位的 apscheduler 作业，代码侧声明的**意图**（APScheduler 名称语义）。
#: 真源：cloud.yaml inventory.run_day="mon-fri"、app/pipeline/config.py check_day="mon"、
#: app/scheduler.py 三个 weekly 的 day_of_week="sat"。DB 的 5 段 cron 由
#: scheduler_resync 覆盖代码注册，故必须校验两者真实触发星期一致。
EXPECTED_CRON_DOW: dict[str, str] = {
    "inventory_weekly": "mon-fri",
    "pipeline_check": "mon",
    "lstm_weekly_retrain": "sat",
    "transmission_weekly_recalc": "sat",
    "backtest_weekly": "sat",
}


def _c_weekly_dow(c: Ctx) -> tuple[bool, str, str, str]:
    """DB cron 的真实触发星期必须与代码声明的意图一致（CL-20261109-21 扩展）。

    旧实现只查「三个周度作业星期位 != 6」，挡不住 `1-5` 这种范围错位：
    `0 17 * * 1-5` 本意工作日，在 CronTrigger.from_crontab 里是周二~周六，
    于是每个周一 inventory 都不跑（11-02、11-09 无流水），数据静默断供。
    现在对**全部**带星期位的 apscheduler 作业，直接算真实触发星期集合比对。
    """
    from apscheduler.triggers.cron import CronTrigger  # noqa: PLC0415
    from zoneinfo import ZoneInfo  # noqa: PLC0415
    tz = ZoneInfo("Asia/Shanghai")
    rows = c.rows("""
        SELECT task_id, schedule FROM ops.scheduled_task
        WHERE enabled AND executor = 'apscheduler'
          AND split_part(schedule, ' ', 5) <> '*'
    """)
    bad: list[str] = []
    checked = 0
    for tid, sch in rows:
        want_name = EXPECTED_CRON_DOW.get(tid)
        if want_name is None:
            bad.append(f"{tid}={sch} 未登记意图（新增作业须补 EXPECTED_CRON_DOW）")
            continue
        try:
            got = _fire_weekdays(CronTrigger.from_crontab(sch, timezone=tz))
            want = _fire_weekdays(CronTrigger(
                day_of_week=want_name, hour=0, minute=0, timezone=tz))
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{tid}={sch} 解析失败 {type(exc).__name__}: {exc}")
            continue
        checked += 1
        if got != want:
            bad.append(f"{tid}={sch} 实得星期{sorted(got)} 期望{sorted(want)}({want_name})")
    return not bad, "DB cron 真实触发星期 == 代码意图（0=周一…6=周日）", (
        f"检查 {len(rows)} 个 / 有效比对 {checked} 个 / 违规 {len(bad)}"), (
        "星期语义错位：" + "; ".join(bad) if bad else "")


def _c_jobhb_cron_aware(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261110-10 A：周/月频心跳判据必须真的按现行 cron 判定，且被接线。

    只查源码字符串会重演「定义了没人用」（skill 坑 #7），所以这里做行为级对拍：
    让生产真实判据跑改期过渡空档 / 连续漏跑 / 无字典回退 / 硬上限 / 日频固定阈值
    六个场景，再叠加一次源码接线检查与字典 cron 可解析性检查。
    """
    label = "周/月频作业心跳按现行 cron 推算应触发，且判据被真实接线"
    try:
        import inspect  # noqa: PLC0415
        from datetime import timezone  # noqa: PLC0415
        from app.ops import alerting  # noqa: PLC0415
        fn = alerting.job_heartbeat_verdict
        src = inspect.getsource(alerting._check_job_heartbeat)
        wired = "job_heartbeat_verdict" in src and "ops.scheduled_task" in src
    except Exception as exc:  # noqa: BLE001
        return False, label, f"判据导入失败 {type(exc).__name__}", str(exc)[:200]

    tz = timezone(timedelta(hours=8))

    def d(s: str) -> datetime:
        return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=tz)

    cases = [
        ("改期过渡空档不误报", "2026-11-01 06:06", "2026-11-10 18:00", "0 6 * * 5", 9, False),
        ("连续错过两次应触发告警", "2026-11-01 06:06", "2026-11-15 12:00", "0 6 * * 5", 9, True),
        ("字典无 cron 回退固定阈值", "2026-11-01 06:06", "2026-11-11 12:00", None, 9, True),
        ("硬上限防长期静默", "2026-11-01 06:06", "2026-11-25 12:00", "0 6 * * 5", 9, True),
        ("日频沿用固定阈值", "2026-11-08 16:20", "2026-11-12 16:20", "0 16 * * *", 3, True),
        ("月频正常周期内不误报", "2026-11-01 06:30", "2026-11-10 18:00", "30 6 1 * *", 40, False),
    ]
    bad: list[str] = []
    for name, last_s, now_s, sch, gap, want in cases:
        try:
            got, detail = fn(d(last_s), d(now_s), schedule=sch, expected_gap_days=gap)
        except Exception as exc:  # noqa: BLE001
            bad.append(f"{name}: 抛异常 {type(exc).__name__}: {exc}")
            continue
        if got != want:
            bad.append(f"{name}: got={got} want={want} ({detail})")
    if not wired:
        bad.append("_check_job_heartbeat 未引用新判据或调度字典（定义了没人用）")

    # 字典侧：周/月频作业的 cron 必须能解析，否则判据会全量回退成旧行为。
    try:
        from apscheduler.triggers.cron import CronTrigger  # noqa: PLC0415
        rows = c.rows(
            "SELECT task_id, schedule FROM ops.scheduled_task "
            "WHERE enabled AND executor='apscheduler' AND expected_gap_days >= 7")
        for tid, sch in rows:
            if not sch or CronTrigger.from_crontab(sch, timezone=tz) is None:
                bad.append(f"{tid} cron 不可解析: {sch}")
    except Exception as exc:  # noqa: BLE001
        bad.append(f"字典 cron 校验失败 {type(exc).__name__}: {exc}")

    return not bad, label, (
        f"行为对拍 {len(cases)} 例 / 违规 {len(bad)} / wired={wired}"), (
        "；".join(bad) if bad else "")


def _c_compress_log_path(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261110-14：压缩脚本日志不得依赖 /var/log，且失败不得假绿。

    /var/log 默认仅 root 可写，旧版靠 sudo 预创建 + chown 才能跑，主机重装即复发；
    而 psql -f 不带 ON_ERROR_STOP 时报错仍退 0，task_runner 按 returncode 记 success。
    这里不只查字符串：默认日志目录交给 sh 真实展开、整份脚本跑 sh -n，
    再查作业字典仍启用（防「脚本改了但没人调用」）。
    """
    import os  # noqa: PLC0415
    import re  # noqa: PLC0415
    import subprocess  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    label = "压缩脚本日志落在项目可写目录且失败可冒泡"
    candidates = [
        "/app/scripts/db/run_compress.sh",  # 生产容器：/Docker/qhyc ro bind 到 /app
        str(Path(__file__).resolve().parents[2] / "scripts/db/run_compress.sh"),
    ]
    path = next((p for p in candidates if os.path.isfile(p)), None)
    if not path:
        return False, label, "run_compress.sh 可读", "两处候选路径均不存在"

    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    # 注释里会提到 /var/log 作为历史说明，只检查有效代码行。
    code = "\n".join(ln for ln in src.splitlines()
                      if ln.strip() and not ln.lstrip().startswith("#"))
    bad: list[str] = []
    if "/var/log" in code:
        bad.append("有效代码仍引用 /var/log（root-only，ubuntu 不可写）")

    m = re.search(r'LOG_DIR="\$\{QHYC_LOG_DIR:-([^}]+)\}"', code)
    if not m:
        bad.append("未找到 QHYC_LOG_DIR 带默认值的定义")
    else:
        default = m.group(1)
        if default != "/home/ubuntu/qhyc_work/logs":
            bad.append(f"默认日志目录漂移: {default}")
        try:
            got = subprocess.run(
                ["sh", "-c",
                 f'LOG_DIR="${{QHYC_LOG_DIR:-{default}}}"; printf "%s" "$LOG_DIR"'],
                capture_output=True, text=True, timeout=5).stdout.strip()
            if got != default:
                bad.append(f"shell 实际展开 {got} != {default}")
        except Exception as exc:  # noqa: BLE001
            bad.append(f"shell 展开失败 {type(exc).__name__}: {exc}")

    if "ON_ERROR_STOP=1" not in code:
        bad.append("psql 未启用 ON_ERROR_STOP，SQL 报错仍退 0 假绿")
    if 'exit "$rc"' not in code and "exit $rc" not in code:
        bad.append("失败分支未把非 0 退出码冒泡给 task_runner")
    try:
        syn = subprocess.run(["sh", "-n", path], capture_output=True, text=True, timeout=5)
        if syn.returncode != 0:
            bad.append(f"sh -n 失败: {syn.stderr.strip()[:120]}")
    except Exception as exc:  # noqa: BLE001
        bad.append(f"sh -n 异常 {type(exc).__name__}: {exc}")
    try:
        row = c.rows(
            "SELECT enabled::text FROM ops.scheduled_task "
            "WHERE task_id='cron_compress_chunks'")
        if not row or str(row[0][0]).lower() not in ("true", "t"):
            bad.append("cron_compress_chunks 字典未登记或已禁用（定义了没人用）")
    except Exception as exc:  # noqa: BLE001
        bad.append(f"字典接线检查失败 {type(exc).__name__}: {exc}")

    return not bad, label, f"6 项检查 / 违规 {len(bad)} / path={path}", "；".join(bad)


def _load_script_module(name: str, relpath: str):
    """从仓库路径加载脚本模块（守卫运行时 scripts/ 未必在 sys.path）。"""
    import importlib.util  # noqa: PLC0415
    import sys  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    root = Path(__file__).resolve().parents[2]
    for p in (str(root), str(root / "scripts")):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(name, root / relpath)
    mod = importlib.util.module_from_spec(spec)
    # 动态加载模块需注册进 sys.modules，否则 Python 3.13 的 dataclasses
    # 解析 string annotation 时查不到 cls.__module__，@dataclass 会抛异常。
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)
    return mod


def _c_roll_yield_zero_loud(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261110-15 E1：roll_yield 整日 0 行不得再记 success。

    11-09 上游 contract_daily 晚到，``save_rows([]) -> 0``，旧脚本只认
    字符串「失败」→ fails=0 → 退出码 0 → ``_require_subprocess_success`` 放行
    → ``scheduled_task_run`` 假绿。数据次日才靠续跑补回。这里做行为对拍
    （真实 ``day_written_rows``）+ 判据接线 + 调度传参三查。
    """
    label = "roll_yield 整交易日零写入必须非零退出且调度已接线"
    bad: list[str] = []
    try:
        import inspect  # noqa: PLC0415

        m = _load_script_module("_g_roll_yield_check", "scripts/collect_roll_yield.py")
        fn = m.day_written_rows
        cases = [
            ("全零", {"RB": 0, "CU": 0}, 0),
            ("正常", {"RB": 47, "CU": 0}, 47),
            ("db_fallback 字符串", {"RB": "47(db_fallback)"}, 47),
            ("双失败字符串", {"RB": "失败: Timeout"}, 0),
            ("空字典", {}, 0),
        ]
        for name, res, want in cases:
            got = fn(res)
            if got != want:
                bad.append(f"{name}: got={got} want={want}")
        src = inspect.getsource(m.main)
        if not all(k in src for k in ("--require-rows", "written == 0", "fails += 1")):
            bad.append("main() 未把 0 行接入失败计数")
        from app import scheduler  # noqa: PLC0415

        if "--require-rows" not in inspect.getsource(scheduler._collect_roll_yield_job):
            bad.append("_collect_roll_yield_job 未传 --require-rows（定义了没人用）")
    except Exception as exc:  # noqa: BLE001
        bad.append(f"导入/对拍失败 {type(exc).__name__}: {str(exc)[:150]}")
    return not bad, label, f"5 例对拍 / 违规 {len(bad)}", "；".join(bad)


def _c_factor_freshness_coverage(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261110-15 E2/E3：因子巡检必须看逐交易日覆盖，且断供能触发长窗口补算。

    只看 ``max(trade_date)`` 时，``f_atr_pctile`` 11-07 残留 11 行把 max 顶住，
    lag=3 不满足 ``lag > 3``，420 天补算永不触发，11-09/10 逐日 0 行无人发现。
    """
    label = "因子新鲜度巡检按交易日出覆盖判据且补算链路真实接线"
    bad: list[str] = []
    try:
        import inspect  # noqa: PLC0415
        from datetime import date, timedelta  # noqa: PLC0415

        m = _load_script_module("_g_factor_fresh_check", "scripts/factor_freshness_check.py")
        d0 = date(2026, 11, 3)
        days = [d0 + timedelta(days=i) for i in range(5)]

        def cov(vals):
            return [("f_atr_pctile", d, n) for d, n in zip(days, vals)]

        cases = [
            ("正常逐日完整", cov([80, 81, 80, 80, 79]), 1, 0),
            ("某日 0 行必报", cov([80, 81, 80, 80, 0]), 1, 1),
            ("多日 0 行必报", cov([80, 0, 80, 0, 0]), 1, 1),
            ("全窗口 0 行交表级判据", cov([0, 0, 0, 0, 0]), 1, 0),
            ("部分行可按阈值报", cov([80, 11, 80, 80, 80]), 20, 1),
            ("部分行不低于阈值不报", cov([80, 11, 80, 80, 80]), 1, 0),
        ]
        for name, rows, mn, want in cases:
            got = len(m.coverage_gaps(rows, mn))
            if got != want:
                bad.append(f"{name}: got={got} want={want}")
        if m._group_of("f_atr_pctile") != "v1v6" or "f_atr_pctile" not in m.LONG_HISTORY_FACTORS:
            bad.append("f_atr_pctile 未归入 v1v6 长窗口组，补算窗口不会放宽")

        # 集成对拍：lag=3 恰好不报，但逐日 0 行必须把它送进需补算列表。
        class _Cur:
            def __init__(self): self.sql = ""
            def execute(self, sql, params=None): self.sql = sql
            def fetchall(self):
                if "CROSS JOIN td" in self.sql:
                    return cov([80, 81, 80, 80, 0])
                return [("f_atr_pctile", date(2026, 11, 7), 137647)]
        need = m.needs_attention(_Cur(), 3, date(2026, 11, 10))
        if "f_atr_pctile" not in [x["factor_id"] for x in need]:
            bad.append("needs_attention 漏掉逐日 0 行因子（巡检盲区未修）")
        src = inspect.getsource(m.main)
        if "needs_attention" not in src or "backfill(stale" not in src:
            bad.append("main() 未接入逐日覆盖判据或补算（定义了没人用）")
    except Exception as exc:  # noqa: BLE001
        bad.append(f"导入/对拍失败 {type(exc).__name__}: {str(exc)[:150]}")
    return not bad, label, f"6 例对拍 + 集成对拍 / 违规 {len(bad)}", "；".join(bad)


def _c_mpr_dce_contract_continuity(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261110-12：DCE 会员持仓必须**逐合约**检查规模连续性。

    ``OPS-MPR-EXCHANGE-COVERAGE`` / ``OPS-MPR-DCE-COVERAGE`` 只数品种数，
    2026-11-06 官网 87 个 DCE 合约里 11 个数值腰斩/行数不足（P2705
    long 220849→11955、PP2701 447868→26249），品种覆盖照样 16/16 PASS。
    判据：每个合约逐日 ``sum(long_pos)`` 相对**上一个有数据交易日**，
    腰斩 <0.3× 或暴涨 >3× 即违规；11-06 的坏值还会在 11-09 制造
    「暴涨 17×」的二次假象，修复后两者一并消失。

    窗口 = 近 10 天 + 事故区间 11-05~11-09（永久锚点，防靠时间衰减转 PASS）。
    只查小表 member_position_rank，不做 minute_bar 全表聚合。
    """
    label = "DCE 会员持仓逐合约规模连续（无腰斩/暴涨）"
    try:
        rows = c.rows("""
            WITH daily AS (
              SELECT trade_date, symbol, sum(long_pos) sl
              FROM l0_raw.member_position_rank
              WHERE exchange='DCE'
                AND (trade_date >= CURRENT_DATE - INTERVAL '10 days'
                     OR trade_date BETWEEN DATE '2026-11-05' AND DATE '2026-11-09')
              GROUP BY 1,2
            ), lagd AS (
              SELECT trade_date, symbol, sl,
                     lag(sl) OVER (PARTITION BY symbol ORDER BY trade_date) prev_sl
              FROM daily
            )
            SELECT trade_date, symbol, sl, prev_sl,
                   round(sl::numeric / nullif(prev_sl, 0), 3) ratio
            FROM lagd
            WHERE prev_sl > 10000 AND (sl < prev_sl * 0.3 OR sl > prev_sl * 3.0)
            ORDER BY 1,2 LIMIT 40
        """)
    except Exception as exc:  # noqa: BLE001
        return False, label, f"查询失败 {type(exc).__name__}", str(exc)[:200]
    bad = [f"{r[0]} {r[1]} long={r[2]} prev={r[3]} ratio={r[4]}" for r in rows]
    return not bad, label, f"逐合约腰斩/暴涨 {len(bad)} 处", "；".join(bad[:10])


def _c_daily_refresh_wired(c: Ctx) -> tuple[bool, str, str, str]:
    """日频刷新链必须真的接通，且失败必须冒泡（CL-20261109-21）。

    `235711a` 按文件名清理「未使用脚本」时删掉了仍被
    `scripts/daily_futures_refresh.py` import 的 `refresh_main_contract_map.py`，
    而编排脚本的 main() 只 logger.error 后 return 0 → 调度器按「作业函数有没有
    抛异常」记 success，流水全绿、main_contract_map 从 11-06 静默停更。
    三项都衡量「此刻」：模块在不在、代码会不会吞异常、两表最新交易日对不对。
    """
    root = Path(__file__).resolve().parents[2]
    bad: list[str] = []
    mcm = root / "scripts" / "refresh_main_contract_map.py"
    if not mcm.is_file():
        bad.append("scripts/refresh_main_contract_map.py 不存在（再次被清理？）")
    refresh_src = (root / "scripts" / "daily_futures_refresh.py").read_text(
        encoding="utf-8", errors="ignore")
    if "import refresh_main_contract_map" not in refresh_src:
        bad.append("daily_futures_refresh.py 未引用 main_contract_map 步骤")
    if "raise RuntimeError" not in refresh_src:
        bad.append("daily_futures_refresh.main() 失败不抛异常 → 流水会假绿")

    exp = c.scalar("""
        SELECT max(trade_date) FROM futures_rule WHERE trade_date <= CURRENT_DATE
    """)
    # 日频刷新作业在 17:00（inventory）/ 18:10（daily_futures_refresh）跑。
    # 白天把「今天」当期望日会每天上午误报：两表本来就该等收盘后才有数。
    # 19:00 之前期望前一交易日；19:00 之后作业该跑完，仍缺就是真断更。
    # （CL-20261110-01 验收时实测 09:54 全天守卫被这条判据误判为 FAIL。）
    now_sh = c.scalar("SELECT now() AT TIME ZONE 'Asia/Shanghai'")
    if exp is not None and now_sh.hour < 19:
        prev = c.scalar(
            "SELECT max(trade_date) FROM futures_rule WHERE trade_date < :d", d=exp)
        if prev is not None:
            exp = prev
    for tbl, col in (("main_contract_map", "trade_date"), ("inventory", "report_date")):
        got = c.scalar(f"SELECT max({col}) FROM {tbl}")
        if exp is None or got is None or got < exp:
            bad.append(f"{tbl} 最新 {got} < 期望交易日 {exp}")
    return not bad, "日频刷新链接通 / 失败冒泡 / 两表当日有数", (
        f"期望交易日 {exp}，模块存在={mcm.is_file()}，失败冒泡="
        f"{'raise RuntimeError' in refresh_src}，违规 {len(bad)} 项"), (
        "; ".join(bad) if bad else "")


# ---------------------------------------------------------------- 告警口径
def _c_mem_caliber(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261108-01：内存告警必须读 cgroup，禁止回退宿主全局 /proc/meminfo。

    旧实现的三条候选路径全都读不到——scheduler 是 systemd 服务，cgroup v2 文件
    在 ``/sys/fs/cgroup/system.slice/qhyc-scheduler.service/`` 下，而不是根目录；
    于是回退 /proc/meminfo，把服务级 2.5G 限额错量成宿主机总内存，
    「接近 OOM」告警永远不可能触发。断言只看此刻调用的返回值，不看历史流水。
    """
    try:
        from app.ops.alerting import _cgroup_mem_source  # noqa: PLC0415
        used, lim, src = _cgroup_mem_source()
    except Exception as exc:                              # noqa: BLE001
        return False, "内存告警从 cgroup 读取（非宿主全局口径）", (
            f"调用失败：{type(exc).__name__}: {exc}"), (
            "app.ops.alerting._cgroup_mem_source 不可用，检查 CL-20261108-01")
    ok = used > 0 and src.startswith("/sys/fs/cgroup/")
    return ok, "内存告警从 cgroup 读取（非宿主全局口径）", (
        f"used={used} lim={lim} src={src}"), (
        "" if ok else
        "回退到 /proc/meminfo → 分母变成宿主机总内存，服务/容器级内存上限永远告警不到")


# ------------------------------------------------------------ 回测批次完整性
def _c_bt_run_complete(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261108-02：最新回测 run 必须是完整整批，或仍处于活跃写入中。

    2026-11-08 事故：B0 全量回测跑在 qhyc-api 容器内，被一次「消除 C4 漂移」
    的容器重启误杀，停在 16/50 品种。系统里没有任何断言或告警能发现，
    全靠人工挂的只读轮询 65 分钟才定位。更关键的是
    ``app/backtest/weights.py::update_model_weights`` 默认取
    「n_sym >= min_symbols_in_run(=10) 且 created_at 最新」的 run ——
    16 个品种的半成品足以冒充整批，直接污染模型权重月更。

    判据只衡量此刻，不看历史流水：
      · 参考规模 = 近 90 天所有 run 的最大 symbol 数（自适应，不硬编码 50）；
      · 最新 run 的 n_sym >= 0.8 × 参考规模 → 完整，PASS；
      · 否则若最后一笔写入距今 < 2 小时 → 仍在跑，PASS（不打扰进行中的回测）；
      · 否则 → 停滞的半成品，FAIL。
    代价：手工单品种验证 run 若成为最新 run 且超过 2 小时，会报 warn。
    这是刻意保留的——它同样会让权重月更取错 run，属于真实风险。
    只查 backtest_result（小表），不触碰 l0_raw 全表。
    """
    rows = c.rows("""
        WITH runs AS (
            SELECT run_id, count(DISTINCT symbol) AS n_sym,
                   max(created_at) AS last_at
            FROM public.backtest_result
            WHERE created_at >= now() - interval '90 days'
            GROUP BY run_id
        ), ref AS (SELECT max(n_sym) AS n_max FROM runs)
        SELECT r.run_id, r.n_sym, r.last_at, f.n_max,
               extract(epoch FROM now() - r.last_at) / 3600.0 AS idle_h
        FROM runs r CROSS JOIN ref f
        ORDER BY r.last_at DESC LIMIT 1
    """)
    expected = "最新回测 run 为完整整批，或仍在活跃写入（<2h）"
    if not rows:
        return False, expected, "近 90 天无任何回测 run", (
            "周度回测链路从未产出结果，检查 _backtest_weekly_job")
    run_id, n_sym, last_at, n_max, idle_h = rows[0]
    n_sym = int(n_sym or 0)
    threshold = int((n_max or 0) * 0.8)
    idle_h = float(idle_h or 0)
    complete = threshold > 0 and n_sym >= threshold
    active = idle_h < 2.0
    ok = complete or active
    state = "完整整批" if complete else ("写入中" if active else "停滞半成品")
    return ok, expected, (
        f"run={run_id} n_sym={n_sym}/{n_max} (门槛{threshold}) "
        f"last={last_at} idle={idle_h:.2f}h state={state}"), (
        "" if ok else
        "最新 run 是停滞的半成品 → update_model_weights 会把它当整批用于权重月更；"
        "确认回测进程是否被中断，必要时整批重跑并清理半成品批次")


def _c_lstm_snapshot_write(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261108-03：LSTM 快照写入必须能覆盖跨 UID 的只读目标文件。

    09:07 容器内 B0(appuser uid=10001) 留下 85 个 644 快照，宿主
    ubuntu(uid=1000) 重跑时 torch.save 覆盖报 Permission denied，
    被 engine.py 的 warning 吞掉，17/53 品种快照悄悄不生成。
    共享卷天然混合 UID，所以判据不是「文件当前可写」（从容器视角看
    uid=1000/644 的历史快照也不可写，会误报），而是**实测原子写能力**：
    让目标文件 mode=0444，仍须能被替换。同时静态检查 train_symbol
    未回退到直接 torch.save。
    """
    import glob                                           # noqa: PLC0415
    import os                                             # noqa: PLC0415
    import shutil                                         # noqa: PLC0415
    import tempfile                                       # noqa: PLC0415

    label = "LSTM 快照写入可跨 UID 覆盖只读目标"
    d = os.getenv("LSTM_WEIGHT_DIR", "/app/runtime/lstm")
    if not os.path.isdir(d):
        return True, label, f"{d} 不存在（非生产环境）", ""
    files = glob.glob(os.path.join(d, "*.pt"))
    uid_set = sorted({os.stat(f).st_uid for f in files})
    try:
        import ast                                        # noqa: PLC0415
        from app.predictors import lstm_train as m        # noqa: PLC0415
        with open(m.__file__, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        defs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

        def _calls_torch_save(node):
            # 用 AST 而非字符串：docstring 里出现 torch.save(...) 不应误判。
            return any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "save" and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "torch"
                for n in ast.walk(node))

        atomic = ("train_symbol" in defs and "_atomic_torch_save" in defs
                  and hasattr(m, "_atomic_torch_save")
                  and not _calls_torch_save(defs["train_symbol"])
                  and _calls_torch_save(defs["_atomic_torch_save"]))

        class _FakeTorch:
            @staticmethod
            def save(state, path):
                with open(path, "wb") as fh:
                    fh.write(b"guard-probe")

        probe_dir = tempfile.mkdtemp(prefix="lstm_guard_")
        try:
            target = os.path.join(probe_dir, "probe.pt")
            with open(target, "wb") as fh:
                fh.write(b"old")
            os.chmod(target, 0o444)
            m._atomic_torch_save(_FakeTorch(), {"k": 1}, target)
            with open(target, "rb") as fh:
                cap_ok = fh.read() == b"guard-probe"
        finally:
            shutil.rmtree(probe_dir, ignore_errors=True)
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"{type(exc).__name__}: {exc}", (
            "检查 CL-20261108-03：app.predictors.lstm_train 不可用")
    ok = atomic and cap_ok
    return ok, label, (
        f"files={len(files)} uids={uid_set} atomic={atomic} "
        f"overwrite_0444={cap_ok} dir={d}"), (
        "" if ok else
        "train_symbol 未走原子写，或原子写无法覆盖 0444 目标 → "
        "跨 UID 共享卷会让回测快照静默失败（CL-20261108-03）")


def _c_roll_gap_aware(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261107-15 / CL-20261108-04：换月映射必须跨休市感知，且 SP 不能再漏 n_bars。

    054 用「事件落在 [bucket, next_bucket)」判归属，60m 网格上 15:00→22:00
    的休市空隙被算进 15:00 那根，把 21:15 的夜盘换月归到日盘收盘桶。
    055 把上界 clamp 到 min(next_bucket, bucket+名义周期)，并补一个归
    next_bucket 的分支；同时补上漏写的 n_bars 列。

    这里直接读**当前生效的 SP 定义**做防回退检查（与 VAR-SCOPE-WIRED 同思路）：
    以前 CL-15 挂 high 却没有任何断言覆盖，重跑一次错归就会静默复发。
    """
    label = "L2 换月映射跨休市感知（055 未回退）"
    try:
        d = c.scalar(
            "SELECT pg_get_functiondef("
            "'public.sp_build_l2_roll_segment(text,boolean,text)'::regprocedure)") or ""
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"{type(exc).__name__}: {exc}", (
            "055 三参签名不存在 → SP 已回退到 054，跨休市错归会静默复发")
    tokens = {
        "上界clamp": "least(t.bucket +",
        "空隙分支": "SELECT t.next_bucket AS bar_ts",
        "单品种参数": "p_symbol",
        "n_bars补齐": "fs.n_bars",
    }
    missing = [k for k, t in tokens.items() if t not in d]
    return not missing, label, (
        f"sig=(text,boolean,text) ddl_len={len(d)} 缺失标记={missing or '无'}"), (
        "" if not missing else
        "SP 定义缺少 " + ", ".join(missing) +
        " → 055 被回退或部分覆盖，跨休市错归 / INSERT 漏列会静默复发"
        "（CL-20261107-15 / CL-20261108-04）")


def _c_roll_gap_data(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-15 回归哨兵：2026-11-04 PF888 21:15 夜盘换月必须归到夜盘首桶。

    近 120 天 min60/min30 各 17,933 个跨休市空隙里只有这 1 件换月事件落入，
    故固定作为回归样本。判的是**当前主表状态**（不靠时间窗口衰减）：
    只要有人重跑出 15:00 桶或后复权没重建，立刻 block FAIL。
    """
    label = "PF888 夜盘换月段落在夜盘首桶且后复权已抹平"
    try:
        segs = c.rows("""
            SELECT freq, seg_start::text, cum_offset::text
            FROM l2_adj.roll_segment
            WHERE symbol = 'PF888' AND freq IN ('min30', 'min60')
              AND seg_start >= '2026-11-04 00:00+08'::timestamptz
              AND seg_start <  '2026-11-05 00:00+08'::timestamptz
            ORDER BY freq
        """)
        off = c.scalar("""
            SELECT adj_offset::text FROM l2_adj.bar_60m_ba
            WHERE symbol = 'PF888'
              AND bucket = '2026-11-04 22:00+08'::timestamptz
        """)
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"{type(exc).__name__}: {exc}", "主表查询失败"
    got = {r[0]: (r[1], r[2]) for r in segs}
    want_start = {"min30": "2026-11-04 21:30", "min60": "2026-11-04 22:00"}
    bad: list[str] = []
    for f, want in want_start.items():
        if f not in got:
            bad.append(f"{f} 缺段")
        elif not got[f][0].startswith(want):
            bad.append(f"{f} seg_start={got[f][0]}（应为 {want} 之后，不得是 15:00）")
        elif not got[f][1] or float(got[f][1]) <= 0:
            # ★ 不再硬编码 320（2026-11-09，CL-20261109-19）。换会话网格后，换月检测
            #   的锚点 15m 桶价格变了一档，检出的缺口随之 320→340（PF tick=2，20=10 跳）。
            #   硬编码的数字一改网格就失效，而本断言的意图只有两条：
            #   ① 换月必须落在夜盘首桶（上面已断言）；② 后复权按该段 cum_offset 抹平。
            #   所以这里只要求「累计缺口为正」——具体数值由 ROLL-MIN5-NO-FAKE-JUMP 的
            #   恒等式（ba 与 raw 的价差恰等于 -roll_delta）来保证自洽。
            bad.append(f"{f} cum_offset={got[f][1]}（应为正数：该品种的累计换月缺口）")
    # ② 后复权已抹平：22:00 那根的 adj_offset 必须等于 min60 段的 cum_offset。
    want_off = float(got["min60"][1]) if "min60" in got and got["min60"][1] else None
    if off is None or want_off is None or abs(float(off) - want_off) > 1e-6:
        bad.append(f"bar_60m_ba@22:00 adj_offset={off}"
                   f"（应与 min60 段 cum_offset={want_off} 相等，断层未抹平）")
    return not bad, label, f"segs={got} ba_adj_offset={off}", (
        "" if not bad else
        "；".join(bad) + " → 换月又归到日盘收盘桶或后复权未重建（CL-20261107-15）")


def _c_roll_positivity_shortcircuit(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261107-16 / C2：默认路径必须短路 shift 计算，否则 min5 会再 OOM。

    055 及以前 `low_off` 对每根 bar 跑相关子查询、`shift_calc` 再做一次
    `max(low)-min(low)` 全表聚合；结果却只在 p_positivity=true 才被使用，
    生产所有调用都走 false。bar_5m = 3477 MB / 82 品种，PG 容器只有 3 GiB，
    全量 min5 重跑两次 OOM（42/71 分钟）、堵 `ALTER TABLE fusion_position` 58 分钟。
    056 用 `AND 'f'::boolean` 让优化器常数折叠成空集，并把 true 分支改成区间 JOIN。
    """
    label = "min5 后复权 shift 默认路径短路（056 未回退）"
    try:
        d = c.scalar(
            "SELECT pg_get_functiondef("
            "'public.sp_build_l2_roll_segment(text,boolean,text)'::regprocedure)") or ""
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"{type(exc).__name__}: {exc}", "读取 SP 定义失败"
    must = {
        "常数短路": "AND %4$L::boolean",
        "区间JOIN": "LEFT JOIN full_segs s",
        "取消二次全表聚合": "greatest(max(low) - min(low), 1.0)",
    }
    missing = [k for k, t in must.items() if t not in d]
    legacy = "ORDER BY s.seg_start DESC LIMIT 1" in d
    ok = not missing and not legacy
    return ok, label, (
        f"ddl_len={len(d)} 缺失={missing or '无'} 旧相关子查询残留={legacy}"), (
        "" if ok else
        "056 被回退或只改了一半 → p_positivity=false 仍会全表扫 bar_5m，"
        "min5 重跑可能再次打满 3 GiB 容器内存并堵锁（CL-20261107-16）")


#: CL-20261108-07 / P2-1：2026-11-08 14:26 生产实测的「实际生效」参数指纹。
#: 覆盖 29 个信号/离场/仓位/取数字段（推送与展示字段故意不计入，避免运维调参误报）。
#: 改代码默认值、给 cloud.yaml 加 fusion 段、注入 QH_FUSION_* env、或让策略注册表
#: 覆盖真正生效，都会改变它 → 必须走台账重新登记 / 拍板，不许静默漂移。
PINNED_FUSION_SIGNAL_HASH = (
    "32bbd9eed3596d5c12491898d303deb8e0c726ddd0203ac820e0c186bc5af48a"
)


def _c_fusion_profile_pinned(c: Ctx) -> tuple[bool, str, str, str]:
    """P2-1：生产实跑的 fusion 参数必须与已登记 V3.4 基线逐位一致。

    审计报告 §4④ 的隐患是「回测报告的参数」与「生产在跑的参数」可能不是同一套，
    而此前没有任何机器断言在盯这件事。本断言直接解析**当前磁盘 + 当前进程 env**
    的完整覆盖链（复用 scripts/ops/dump_fusion_profile.py，避免两处实现分叉），
    衡量的是「此刻的行为」而非历史流水。

    注：常驻服务的配置缓存由 P0-1 的 C4 运行态断言负责，两者互补。
    """
    label = "实际生效 fusion 参数 = 已登记 V3.4 基线"
    try:
        import contextlib
        import importlib.util
        import io

        path = Path(__file__).with_name("dump_fusion_profile.py")
        spec = importlib.util.spec_from_file_location("_qhyc_fusion_profile", path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"无法加载 {path}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # app 首次 import 会打一行 INFO 到 stdout，别污染守卫的人读表格
        with contextlib.redirect_stdout(io.StringIO()):
            prof = mod.build_profile()
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"{type(exc).__name__}: {exc}", (
            "生效参数 profile 无法解析 → 参数口径处于不可验证状态（CL-20261108-07）")

    h = prof["hashes"]["signal_params_hash"]
    env = prof["layers"]["L3_env"]
    reg = prof["layers"]["L4_registry"]
    bad: list[str] = []
    if h != PINNED_FUSION_SIGNAL_HASH:
        bad.append(f"signal_params_hash={h[:12]}… != 基线 {PINNED_FUSION_SIGNAL_HASH[:12]}…")
    if not prof["equals_code_default"]:
        bad.append("生效值不等于代码默认（L2/L3/L4 有覆盖）")
    if env.get("profile_candidate_active") or env.get("granular"):
        bad.append(f"env 覆盖未登记：profile={env.get('QH_FUSION_PROFILE')} "
                   f"granular={env.get('granular')}")
    if reg.get("overrides"):
        bad.append(f"注册表覆盖非空：{reg['overrides']} → 两个消费者可能分叉")
    if not prof["profile_label"].startswith("V3.4"):
        bad.append(f"profile_label={prof['profile_label']}")

    observed = (
        f"hash={h[:12]}… label={prof['profile_label']} "
        f"equals_default={prof['equals_code_default']} "
        f"env={env.get('QH_FUSION_PROFILE') or 'none'} "
        f"registry_overrides={len(reg.get('overrides') or {})}"
    )
    return not bad, label, observed, (
        "" if not bad else
        "；".join(bad) + "。参数口径发生漂移：须在 ops.change_log 登记、重新过回测闸门，"
        "并由人显式更新 PINNED_FUSION_SIGNAL_HASH 后才允许放行（CL-20261108-07 / M4）"
    )


def _c_config_snapshot_fresh(c: Ctx) -> tuple[bool, str, str, str]:
    """P2-2：启动快照 / 信号流水必须绑定**当前生效**的 params_hash。

    与 OPS-FUSION-PROFILE-PINNED 的分工：那条钉住「参数不许漂移」，
    这条钉住「每次运行、每条信号都能追溯到参数」。衡量的都是此刻可判的状态：
      · 每个 role 的**最新**快照 hash == 当前生效 hash（不查时间新鲜度，
        避免「靠历史衰减才能转 PASS」——进程没重启时旧快照依然有效）；
      · 信号表列存在、写入方源码确实在写（防「加了列没人填」）；
      · 近 30 天所有非 NULL 的 signal params_hash 都属于当前 hash。
    """
    label = "启动快照与信号流水绑定当前生效 params_hash"
    try:
        from app.core.config_snapshot import current_params_hash
        cur = current_params_hash()
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"{type(exc).__name__}: {exc}", (
            "无法解析当前生效 params_hash → 参数追溯链不可用（CL-20261108-10）")
    if not cur:
        return False, label, "current_params_hash=None", (
            "当前生效参数 hash 为空，快照/信号无法绑定（CL-20261108-10）")

    try:
        snaps = c.rows("""
            SELECT DISTINCT ON (role) role, signal_params_hash::text, captured_at::text
            FROM ops.config_snapshot ORDER BY role, captured_at DESC
        """)
        col = c.scalar("""
            SELECT count(*) FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'fusion_signal_log'
              AND column_name = 'params_hash'
        """) or 0
        signal_hashes = c.rows("""
            SELECT DISTINCT params_hash FROM fusion_signal_log
            WHERE params_hash IS NOT NULL
              AND signal_at > now() - interval '30 days'
        """)
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"{type(exc).__name__}: {exc}", (
            "ops.config_snapshot / fusion_signal_log 查询失败 → 057 可能未应用"
            "（CL-20261108-10）")

    try:
        src = (Path(__file__).resolve().parents[2]
               / "app" / "strategies" / "fusion_signal.py").read_text(encoding="utf-8")
        writer_wired = "params_hash=_params_hash" in src
    except OSError:
        writer_wired = False

    by_role = {r[0]: str(r[1]) for r in snaps}
    bad: list[str] = []
    for role in ("api", "scheduler"):
        if role not in by_role:
            bad.append(f"缺 {role} 启动快照")
        elif by_role[role] != cur:
            bad.append(f"{role} 最新快照 hash={by_role[role][:12]}… != 当前 {cur[:12]}…")
    if col == 0:
        bad.append("fusion_signal_log 缺 params_hash 列（057 未应用）")
    if not writer_wired:
        bad.append("persist_signals 未写 params_hash（写入方被回退）")
    foreign = sorted({str(r[0]) for r in signal_hashes if str(r[0]) != cur})
    if foreign:
        bad.append(f"近 30 天信号含 {len(foreign)} 个异参数 hash："
                   + ", ".join(x[:12] + "…" for x in foreign[:3]))

    observed = (
        f"cur={cur[:12]}… roles={{{','.join(sorted(by_role))}}} "
        f"col={col} writer_wired={writer_wired} "
        f"signal_hash_kinds={len(signal_hashes)}"
    )
    return not bad, label, observed, (
        "" if not bad else
        "；".join(bad) + "。参数追溯链断裂：确认 057 已应用、api/scheduler 已重启，"
        "并检查 ops.config_snapshot 最新行（CL-20261108-10）"
    )


def _c_bt_fill_mode_wired(c: Ctx) -> tuple[bool, str, str, str]:
    """P2-3 / CL-20261108-11：回测成交口径必须**真实**按 fill_mode 分叉。

    衡量的都是「此刻的行为」，不看历史流水、不靠时间衰减：
      · 默认 fill_mode = next_open（E1-C / CL-20261108-15 起），close 须可显式复现；
      · 合成对拍显式关闭跳空过滤，专测成交价分叉（过滤由
        BT-GAP-FILTER-WIRED 单独验）；
      · 合成 8 根 K 直接调 _simulate_trades，逐笔核对成交价与成交 bar 索引，
        多空两侧都验（防只改了一边）；
      · next_open 必须是「下一根开盘 ± 1tick」且成交 dt 落在下一根；
      · 未知 fill_mode 必须拒绝（防静默回退到 close）；
      · 源码里 next_open 分支必须把成本系数 slip_yuan 置 0（防滑点双计）。
        用 AST 找 Dict 节点，避免 docstring/注释里的字面量造成假阳性
        （OPS-LSTM-SNAPSHOT-WRITE 曾踩过字符串检查的坑）。
    """
    label = "回测成交口径 fill_mode 真实接线（默认 next_open +1tick / close 可复现）"
    try:
        import ast
        import pandas as pd
        from app.backtest import fusion_backtest as fb
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"import 失败 {type(exc).__name__}: {exc}", (
            "融合回测模块不可导入 → 成交口径断言不可用（CL-20261108-11）")

    bad: list[str] = []
    observed: list[str] = []

    bp = fb.FusionBacktestParams()
    default_mode = getattr(bp, "fill_mode", None)
    default_gap = getattr(bp, "max_adverse_gap_atr", None)
    observed.append(f"default={default_mode!r} gap={default_gap!r}")
    if default_mode != "next_open":
        bad.append(f"默认 fill_mode={default_mode!r}，E1-C 起必须 'next_open'")
    if default_gap is None or abs(float(default_gap) - 0.5) > 1e-9:
        bad.append(f"默认 max_adverse_gap_atr={default_gap!r}，用户拍板 0.5 ATR")

    n = 8
    opens = [100.0 + 10.0 * i for i in range(n)]
    closes = [103.0 + 10.0 * i for i in range(n)]
    dts = pd.date_range("2026-01-01", periods=n, freq="h")

    def frame(side: int):
        return pd.DataFrame({
            "dt": dts,
            "open": opens,
            "close": closes,
            "state": [0, side, side, side, 0, 0, 0, 0],
            "lots": [0, 1, 1, 1, 0, 0, 0, 0],
            # 给有效 ATR：全 NaN 会让 _simulate_trades 的 np.nanmean 打
            # "Mean of empty slice" 到 stderr，污染每日 09:10 守卫日志。
            # 只影响 risk/R 字段，不影响成交价与成交 bar 的判据。
            "atr": [1.0] * n,
        })

    def run(mode: str, side: int, tick_size: float = 0.0):
        # 显式关闭跳空过滤：本断言只负责成交价分叉，过滤由
        # BT-GAP-FILTER-WIRED 单独验（否则合成网格的大跳空会拦掉多头开仓）。
        p = fb.FusionBacktestParams(fill_mode=mode, tick_size=tick_size,
                                    max_adverse_gap_atr=0.0)
        return fb._simulate_trades(frame(side), p, symbol=None)

    def hour(x) -> int:
        return int(pd.Timestamp(x).hour)

    try:
        # close：成交价 = 信号根收盘；成交 bar = 信号根
        for side in (1, 2):
            tr = run("close", side)
            if len(tr) != 1:
                bad.append(f"close side={side} 期望 1 笔，实得 {len(tr)}")
                continue
            t = tr[0]
            if abs(t["entry_px"] - 113.0) > 1e-9 or abs(t["exit_px"] - 143.0) > 1e-9:
                bad.append(f"close side={side} 成交价 "
                           f"{t['entry_px']}/{t['exit_px']} != 113/143")
            if (hour(t["entry_dt"]), hour(t["exit_dt"])) != (1, 4):
                bad.append(f"close side={side} 成交 bar "
                           f"{hour(t['entry_dt'])}/{hour(t['exit_dt'])} != 1/4")
        observed.append("close=113/143@bar1/4")

        # next_open：下一根开盘 ± 1tick；成交 bar = 下一根；多空分别验
        tick = 2.0
        for side, exp in ((1, (122.0, 148.0)), (2, (118.0, 152.0))):
            tr = run("next_open", side, tick_size=tick)
            if len(tr) != 1:
                bad.append(f"next_open side={side} 期望 1 笔，实得 {len(tr)}")
                continue
            t = tr[0]
            if (abs(t["entry_px"] - exp[0]) > 1e-9
                    or abs(t["exit_px"] - exp[1]) > 1e-9):
                bad.append(f"next_open side={side} 成交价 "
                           f"{t['entry_px']}/{t['exit_px']} != {exp[0]}/{exp[1]}")
            if (hour(t["entry_dt"]), hour(t["exit_dt"])) != (2, 5):
                bad.append(f"next_open side={side} 成交 bar "
                           f"{hour(t['entry_dt'])}/{hour(t['exit_dt'])} != 2/5")
        observed.append("next_open=L122/148,S118/152@bar2/5")

        # 未知 mode 必须拒绝，而不是静默回退
        try:
            run("bogus", 1)
            bad.append("未知 fill_mode 未拒绝（会静默回退 close）")
        except ValueError:
            observed.append("unknown_mode=rejected")
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"行为级对拍异常 {type(exc).__name__}: {exc}", (
            "_simulate_trades 未按预期工作（CL-20261108-11）")

    # 源码：next_open 必须把成本系数 slip_yuan 置 0（AST 防 docstring 假阳性）
    try:
        src = (Path(__file__).resolve().parents[2] / "app" / "backtest"
               / "fusion_backtest.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        no_double = any(
            isinstance(node, ast.Dict)
            and any(isinstance(k, ast.Constant) and k.value == "slip_yuan"
                    for k in node.keys)
            and any(isinstance(v, ast.Constant)
                    and isinstance(v.value, (int, float)) and float(v.value) == 0.0
                    for v in node.values)
            for node in ast.walk(tree)
        )
        observed.append(f"slip_yuan_zero={no_double}")
        if not no_double:
            bad.append("next_open 未把成本系数 slip_yuan 置 0 → 滑点双计")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"源码 AST 检查失败：{type(exc).__name__}: {exc}")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad)
        + "。成交口径接线被回退或改坏，回测结论不可信（CL-20261108-11）")


def _c_bt_v5x_fill_mode(c: Ctx) -> tuple[bool, str, str, str]:
    """C2b / CL-20261109-05：V5X 回测必须与 fusion 同口径（E1-C next_open）。

    为什么必须断言而不是只写文档

    改造前 ``v5x_backtest`` 的 docstring 明明写着「E1-C 未实现、偏乐观、
    不得并列引用」——文档是对的，但**没有任何东西阻止别人拿 +273,705 去和
    V3.4 比**。这正是本项目反覆出现的形状：口径只存在于文字里。

    所以这条断言同时量三件事（都是「此刻的行为」，零 DB、零时间衰减）：

      1. **默认声明**：``V5XBacktestParams().fill_mode == "next_open"``，
         跳空阈值 0.5 ATR —— 与 fusion 默认一致，两份回测才第一次可比；
      2. **真实接线**：AST 找到对 ``reprice_orders`` 的调用（防「字段加了没人读」），
         且 close 分支直接返回 ``eng.trades``（零回归锚点必须还在）；
      3. **行为级**：合成趋势序列跑真引擎 →
         · close 重定价 == 引擎自己的 trades（逐笔 P/X/net/R）；
         · next_open 首仓价 == ``open[i+1] + dir×tick``（不是信号根收盘）。
         两条同时成立，才算「同一份事件、两种成交口径」真的通了。
    """
    label = "V5X 回测成交口径接 E1-C（默认 next_open 且 close 零回归）"
    try:
        import ast

        import numpy as np
        import pandas as pd

        from app.backtest import v5x_backtest as vb
        from app.backtest.v5x_fill import reprice_orders
        from app.strategies.v5x.engine import CostCfg, DailyCtx, SymbolEngine, V5XConfig
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"import 失败 {type(exc).__name__}: {exc}", (
            "V5X 回测或成交层不可导入 → C2b 断言不可用（CL-20261109-05）")

    bad: list[str] = []
    observed: list[str] = []

    p0 = vb.V5XBacktestParams()
    observed.append(f"default={p0.fill_mode!r} gap={p0.max_adverse_gap_atr!r}")
    if p0.fill_mode != "next_open":
        bad.append(f"默认 fill_mode={p0.fill_mode!r}，C2b 起必须 'next_open'")
    if abs(float(p0.max_adverse_gap_atr) - 0.5) > 1e-9:
        bad.append(f"默认 max_adverse_gap_atr={p0.max_adverse_gap_atr!r}，须与 E1-C 一致取 0.5")

    # ---------- 2) 源码接线（AST，防 docstring 假阳性）----------
    try:
        src = (Path(__file__).resolve().parents[2] / "app" / "backtest"
               / "v5x_backtest.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        calls_reprice = any(
            isinstance(n, ast.Call) and (
                (isinstance(n.func, ast.Name) and n.func.id == "reprice_orders")
                or (isinstance(n.func, ast.Attribute) and n.func.attr == "reprice_orders"))
            for n in ast.walk(tree))
        observed.append(f"calls_reprice={calls_reprice}")
        if not calls_reprice:
            bad.append("v5x_backtest 未调用 reprice_orders（成交口径只声明未接线）")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"源码 AST 检查失败：{type(exc).__name__}: {exc}")

    # ---------- 3) 行为级：同一份事件、两种口径 ----------
    n = 120
    closes = np.linspace(100.0, 130.0, 60).tolist() + np.linspace(130.0, 104.0, 60).tolist()
    closes = np.asarray(closes, dtype=float)
    opens = closes - 0.15
    bars = pd.DataFrame({
        "bucket": pd.date_range("2026-01-05 09:00", periods=n, freq="h"),
        "open": opens, "high": closes + 0.5, "low": closes - 0.5,
        "close": closes, "volume": 1000.0, "raw_c": closes,
    })
    cost = CostCfg(fee_row={"OPEN": (3.0, 0.0001), "CLOSE_TODAY": (3.0, 0.0001),
                            "CLOSE_YEST": (3.0, 0.0001)},
                   slip_yuan=1.0, mult=10.0, tick=1.0)

    def _run(mode: str, tick: float, gap: float):
        eng = SymbolEngine("RB888", V5XConfig(M5_ON=False, M1A_ON=False), cost)
        eng.set_daily_ctx(DailyCtx(td=None))
        orders: list[dict] = []
        atr: list[float] = []
        for k, row in enumerate(bars.itertuples(index=False)):
            evs = eng.on_bar(row.bucket, float(row.open), float(row.high),
                             float(row.low), float(row.close), float(row.volume),
                             float(row.raw_c), allow=True)
            for ev in evs:
                ev["bar_i"] = k
            orders.extend(evs)
            atr.append(float(eng.atr.v) if eng.atr.v is not None else float("nan"))
        tr, _ = reprice_orders(
            orders, opens=bars["open"].to_numpy(dtype=float),
            closes=closes, raw_closes=closes, tds=[0] * n,
            tss=bars["bucket"].tolist(), atr=atr, cost=cost,
            fill_mode=mode, tick=tick, gap_lim=gap, sl_atr=1.75)
        return tr, eng.trades

    try:
        tr_close, eng_tr = _run("close", 1.0, 0.0)
        if not eng_tr:
            bad.append("合成趋势序列未触发开仓（信号层被改坏）")
        elif len(tr_close) != len(eng_tr):
            bad.append(f"close 重定价 {len(tr_close)} 笔 != 引擎 {len(eng_tr)} 笔（零回归破了）")
        else:
            diffs = [abs(a["net"] - b["net"]) for a, b in zip(tr_close, eng_tr)]
            observed.append(f"close_parity_maxdiff={max(diffs):.2e}")
            if max(diffs) > 1e-6:
                bad.append(f"close 重定价与引擎 trades 不一致（maxdiff={max(diffs):.3g}）")

        tr_no, _ = _run("next_open", 1.0, 0.0)
        if not tr_no:
            bad.append("next_open 未产出成交（成交层未接线）")
        else:
            fill_ok = all(
                abs(t["P0"] - (opens[t["fill_i"]] + t["dir"] * 1.0)) < 1e-9
                and t["fill_i"] == t["signal_i"] + 1
                for t in tr_no)
            observed.append(f"next_open_fill={fill_ok} n={len(tr_no)}")
            if not fill_ok:
                bad.append("next_open 首仓价不是「下一根开盘 ± 1tick」（仍是同根收盘）")
            if len(tr_no) > 0 and abs(tr_no[0]["net"] - eng_tr[0]["net"]) < 1e-9:
                bad.append("next_open 净盈亏与 close 完全相同 → 口径没真的换")
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"行为级对拍异常 {type(exc).__name__}: {exc}", (
            "V5X 成交层未按预期工作（CL-20261109-05）")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad)
        + "。V5X 与 fusion 成交口径分叉，两份回测不可比（CL-20261109-05）")


def _c_v5x_p6_sessions(c: Ctx) -> tuple[bool, str, str, str]:
    """P6 会话末根集合必须是「常态末根」，不能被残缺历史污染（CL-20261109-10）。

    为什么这条值得 block

    旧实现取「每个交易日最后一根 bar 的小时」再跨日**取并集**，于是历史上任何
    一天出现过的末根小时都被永久视为会话末根。实测 38 池里有 30+ 品种的并集
    混进了 ``9/10/11/13/14`` —— **9:00 与 13:00 是日盘开盘第一根**。后果：

      · ``SF888 / SM888`` 十年**一笔未成交**（并集覆盖整个日盘）；
      · 全池 net +259,729 → 修正后 +342,152（+32%）；
      · 更阴的是**窗口依赖**：同一品种，实盘只载近几个月历史（并集干净）、
        回测载 12 年（并集脏），两边跑出的信号天然不一致，而代码看起来一模一样。

    断言用合成 K 直接量行为（零 DB、零时间衰减）：
      1. 某日缺日盘/夜盘末根 → 结果仍是常态 ``{15, 23}``（众数而非并集）；
      2. 会话**开始**小时（9/13/21）绝不出现在集合里；
      3. 集合大小 ≤ 2（一个交易日最多两个会话段）。
    """
    label = "P6 会话末根集合取常态末根（众数，非历史并集）"
    try:
        import numpy as np
        import pandas as pd

        from app.strategies.v5x.context import _sess_last_hours
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"import 失败 {type(exc).__name__}: {exc}", (
            "V5X 上下文模块不可导入（CL-20261109-10）")

    bad: list[str] = []
    observed: list[str] = []

    def _f(spec: dict) -> pd.DataFrame:
        rows = [{"symbol": "RB888", "bucket": pd.Timestamp(f"{d} {h:02d}:00")}
                for d, hours in spec.items() for h in hours]
        return pd.DataFrame(rows)

    def _days(spec: dict):
        ks = list(spec)
        return np.array([np.datetime64(d, "D") for d in ks]
                        + [np.datetime64(pd.Timestamp(ks[-1]) + pd.Timedelta(days=3), "D")])

    full = [9, 10, 11, 13, 14, 15, 22, 23]
    spec = {
        "2026-01-05": full,
        "2026-01-06": [9, 10, 11, 22, 23],            # 缺日盘末根
        "2026-01-07": [9, 10, 11, 13, 14, 15, 22],    # 缺夜盘末根
        "2026-01-08": [9, 10],                        # 早年残缺：只有开盘两根
        "2026-01-09": full,
    }
    try:
        got = _sess_last_hours(_f(spec), _days(spec))["RB888"]
        observed.append(f"hours={sorted(got)}")
        if got != frozenset({15, 23}):
            bad.append(f"末根集合={sorted(got)}，应为 [15, 23]（被残缺日污染）")
        if got & {9, 13, 21}:
            bad.append(f"会话开始小时出现在禁开仓集合：{sorted(got & {9, 13, 21})}")
        if len(got) > 2:
            bad.append(f"末根集合大小 {len(got)} > 2（一个交易日最多两段）")
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"行为级对拍异常 {type(exc).__name__}: {exc}", (
            "_sess_last_hours 未按预期工作（CL-20261109-10）")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad)
        + "。P6 会把整个日盘禁开仓，且实盘/回测随窗口长度不同而分叉（CL-20261109-10）")


def _c_v5x_snapshot_wired(c: Ctx) -> tuple[bool, str, str, str]:
    """理论持仓快照必须「真的读引擎状态」且方向标签不反（CL-20261109-08）。

    为什么这条值得 block

    2026-11-09 首次实盘前的清单是**人工从重放里抄的**，结果 5 个仓里 4 个方向抄反
    （实际 1 多 4 空，被写成 5 个多头）。用户若照那份清单核对，会以为手上该有
    5 个多单，然后对每一条 ``CLOSE`` 都无仓可平 —— 而这正是「只推事件不推持仓」
    这个设计缺口的全部危害。

    所以断言量三件事（零 DB、零时间衰减）：
      1. **同一条重放路径**：snapshot 模块必须调用 ``live._replay_orders``，
         不允许自己再写一套循环（两套重放必然分叉）；
      2. **真的注册了**：``scheduler.py`` 里有 ``id="v5x_snapshot"`` 且调用
         ``push_snapshot``（防「模块写好了没人调」）；
      3. **方向标签**：合成一条多头 + 一条空头，渲染结果里多/空字面量必须与
         ``dir`` 一致（防 ``state`` 与 ``LONG/SHORT`` 的映射被写反）。
    """
    label = "V5X 理论持仓快照真实接线且方向标签正确"
    try:
        import ast

        from app.strategies.v5x import snapshot as snap
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"import 失败 {type(exc).__name__}: {exc}", (
            "持仓快照模块不可导入（CL-20261109-08）")

    bad: list[str] = []
    observed: list[str] = []
    root = Path(__file__).resolve().parents[2]

    for rel, need in (("app/strategies/v5x/snapshot.py", "_replay_orders"),
                      ("app/scheduler.py", "push_snapshot")):
        try:
            tree = ast.parse((root / rel).read_text(encoding="utf-8"))
            names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
            attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
            found = need in names or need in attrs
            observed.append(f"{Path(rel).name}:{need}={found}")
            if not found:
                bad.append(f"{rel} 未引用 {need}")
        except Exception as exc:                          # noqa: BLE001
            bad.append(f"{rel} AST 检查失败：{type(exc).__name__}: {exc}")

    # 调度注册：id="v5x_snapshot" 必须出现（否则快照永远不会被定时调用）
    try:
        sched_src = (root / "app/scheduler.py").read_text(encoding="utf-8")
        registered = 'id="v5x_snapshot"' in sched_src or "id='v5x_snapshot'" in sched_src
        observed.append(f"registered={registered}")
        if not registered:
            bad.append("scheduler.py 未注册 v5x_snapshot 作业")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"scheduler 源码读取失败：{type(exc).__name__}: {exc}")

    # 方向标签：合成 1 多 1 空，渲染后逐条核对
    fake = {
        "last_bar": "2026-11-07 15:00", "stale_days": 0,
        "n_long": 1, "n_short": 1,
        "rows": [
            {"symbol": "AA888", "dir": "LONG", "lots": 1, "avg_px_real": 100.0,
             "last_price": 110.0, "stop_real": 95.0, "unrealized_yuan": 100.0,
             "be_done": False},
            {"symbol": "BB888", "dir": "SHORT", "lots": 2, "avg_px_real": 200.0,
             "last_price": 190.0, "stop_real": 210.0, "unrealized_yuan": 200.0,
             "be_done": True},
        ],
    }
    try:
        text = snap.format_snapshot(fake)
        ok_long = "AA888 多 1手" in text
        ok_short = "BB888 空 2手" in text
        observed.append(f"long_label={ok_long} short_label={ok_short}")
        if not (ok_long and ok_short):
            bad.append("方向标签渲染错误（LONG/SHORT 映射反了）")
        if "存量不追" not in text:
            bad.append("缺少「存量不追，只跟新开仓」纪律提示")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"format_snapshot 渲染异常：{type(exc).__name__}: {exc}")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad)
        + "。持仓快照失效 = 用户无法核对理论持仓，统计与实盘永久错位（CL-20261109-08）")


def _c_v5x_close_scan_wired(c: Ctx) -> tuple[bool, str, str, str]:
    """V5X 收盘即时扫描链真实接线（CL-20261109-17）。

    为什么这条值得 block

    用户要求「每小时 K 线结束后 +10s 扫描推送」。这件事有四个**都可以悄悄错**的
    接缝，任何一处断掉，表现都是「照常运行、就是不推」：

      1. 收盘标签与合成器网格不同源 → 扫的时点不对（历史上 30m 漏 14:45、
         5m/15m 退回自然整点桶都是「两边各写一份」造成的）；
      2. 触发器没注册 → 作业永远不跑（`v5x_snapshot` 就踩过「模块写好了没人调」）；
      3. 字典里被写成 5 段 cron → `scheduler_resync` 每 30 分钟用整分触发覆盖，
         second=10 静默消失（skill 坑 #2，062 已栽过一次）；
      4. 完备性门丢了 → 对**在途半截桶**出信号，且 `dedupe_key` 把错误结果永久固化。

    断言五面（零 DB 全表扫，时间窗 ≤ 20 天）：
      A. `live.CLOSE_HM` 的日盘部分 == 由 `_SESSION_DAY_BOUNDS[60]` 推出的标签；
      B. 生产近 20 天池内实际出现的 60m 标签 ⊆ `CLOSE_HM`（网格变了标签没跟会红）；
      C. `live.SCAN_SECOND` 的语义：对每个标签，`label+10s` 必须命中、
         `label+5s` 与 `label+4min` 必须不命中（触发器窗口的唯一定义）；
      D. `scheduler.py` 里 `id="v5x_close_scan"` 的 add_job 用 OrTrigger，
         且 trigger 子树引用 `_V5X_CLOSE_HM` / `_V5X_SCAN_SECOND`（同源，不抄常量）；
      E. 字典行存在、executor≠apscheduler、schedule 不是 5 段 cron、meta.code_cron 非空。
    """
    label = "V5X 收盘即时扫描链真实接线（+10s → 采集/L1/BA/扫描）"
    try:
        import ast

        from app.strategies.v5x import live
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"import 失败 {type(exc).__name__}: {exc}", (
            "live 模块不可导入（CL-20261109-17）")

    bad: list[str] = []
    observed: list[str] = []
    root = Path(__file__).resolve().parents[2]

    # A. 日盘标签与合成器网格同源
    try:
        from app.ingest.synthesizer import _SESSION_DAY_BOUNDS as _grid
        want_day = tuple((9 + b // 60, b % 60) for b in _grid[60])
        have_day = tuple(sorted(live._DAY_CLOSE_HM))
        observed.append(f"day={have_day}")
        if have_day != tuple(sorted(want_day)):
            bad.append(f"日盘收盘标签 {have_day} != 合成器网格推出 {tuple(sorted(want_day))}")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"日盘标签同源检查失败：{type(exc).__name__}: {exc}")

    # B. 生产实际出现的 60m 标签必须在 CLOSE_HM 里
    #    只查**白名单 ×888 的最近 10 天**：那正是 V5X 扫描覆盖的范围。
    #    扩到全 scope（含 8888 指数层与持仓豁免品种）会把「本轮重建范围之外的历史
    #    残留」算进来 —— 那不是收盘链的缺口，而是一条永远转不绿的断言。
    #    ★ 必须排除「还没结算」的在途桶（2026-11-09，CL-20261109-19）：
    #      合成器夜盘分支把在途桶标签钳到 ``seg_last + 1min``，夜盘实跑会写出
    #      22:07/22:08/21:31 这类标签（日盘分支不会，恒落在网格边界）。这不是
    #      「CLOSE_HM 没跟上网格」，而是「这根 K 线还没收完」—— 桶完成后
    #      下一轮 purge+re-synth 会把它改写成 23:00。不排除的话，本断言在每个
    #      夜盘时段都会假红（白天 09:10 的 cron 反而是绿的，最难查的那种）。
    try:
        rows = c.rows(
            "SELECT DISTINCT to_char(b.bucket, 'HH24:MI') FROM l1_mkt.bar_60m b "
            "WHERE b.symbol = ANY(:syms) AND b.bucket > now() - interval '10 days' "
            "  AND b.bucket < now() - interval '2 hours'",
            syms=sorted({w + "888" for w in c.whitelist}))
        seen = {str(r[0]) for r in rows}
        known = {f"{hh:02d}:{mm:02d}" for hh, mm in live.CLOSE_HM}
        extra = sorted(seen - known)
        observed.append(f"seen={len(seen)} extra={extra[:3]}")
        if extra:
            bad.append(f"生产出现未登记的 60m 收盘标签 {extra}（CLOSE_HM 没跟上网格）")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"生产标签核对失败：{type(exc).__name__}: {exc}")

    # C. 触发器窗口语义：label+10s 命中，label+5s / label+4min 不命中
    try:
        from datetime import timedelta
        from zoneinfo import ZoneInfo

        from app.ingest.close_pipeline import current_close_label

        tz = ZoneInfo("Asia/Shanghai")
        base = datetime(2026, 11, 9, 0, 0, tzinfo=tz)   # 周一，避免周末门干扰
        miss = []
        for hh, mm in live.CLOSE_HM:
            lab = base.replace(hour=hh, minute=mm)
            ok_hit = current_close_label(lab + timedelta(seconds=live.SCAN_SECOND)) == lab
            ok_early = current_close_label(lab + timedelta(seconds=5)) is None
            ok_late = current_close_label(lab + timedelta(minutes=4)) is None
            if not (ok_hit and ok_early and ok_late):
                miss.append(f"{hh:02d}:{mm:02d}(hit={ok_hit},early={ok_early},late={ok_late})")
        observed.append(f"window_miss={len(miss)}")
        if miss:
            bad.append(f"收盘窗口语义不符：{miss[:3]}")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"窗口语义检查失败：{type(exc).__name__}: {exc}")

    # D. scheduler 注册：OrTrigger + 同源常量引用
    try:
        tree = ast.parse((root / "app/scheduler.py").read_text(encoding="utf-8"))
        node = None
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            name = getattr(n.func, "attr", None) or getattr(n.func, "id", None)
            if name != "add_job":
                continue
            idv = dict((kw.arg, kw.value) for kw in n.keywords if kw.arg).get("id")
            if isinstance(idv, ast.Constant) and idv.value == "v5x_close_scan":
                node = n
                break
        if node is None:
            bad.append("scheduler.py 未注册 v5x_close_scan 作业")
        else:
            trig = dict((kw.arg, kw.value) for kw in node.keywords if kw.arg).get("trigger")
            is_or = (isinstance(trig, ast.Call)
                     and (getattr(trig.func, "id", None) or
                          getattr(trig.func, "attr", None)) == "OrTrigger")
            names = ({x.id for x in ast.walk(trig) if isinstance(x, ast.Name)}
                     if trig is not None else set())
            same_src = {"_V5X_CLOSE_HM", "_V5X_SCAN_SECOND"} <= names
            observed.append(f"OrTrigger={is_or} same_src={same_src}")
            if not is_or:
                bad.append("v5x_close_scan 的 trigger 不是 OrTrigger（多标签无法表达）")
            if not same_src:
                bad.append("trigger 未引用 CLOSE_HM/SCAN_SECOND（另抄常量会漂移）")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"scheduler 注册检查失败：{type(exc).__name__}: {exc}")

    # E. 字典行：必须存在且保持惰性
    try:
        rows = c.rows(
            "SELECT executor, coalesce(schedule,''), coalesce(meta->>'code_cron','') "
            "FROM ops.scheduled_task WHERE task_id = 'v5x_close_scan'")
        if not rows:
            bad.append("ops.scheduled_task 缺 v5x_close_scan 行（migrations/068）")
        else:
            executor, schedule, code_cron = rows[0][0], rows[0][1], rows[0][2]
            observed.append(f"dict={executor}/{schedule!r}")
            if executor == "apscheduler":
                bad.append("executor=apscheduler → resync 会用 5 段 cron 覆盖 second=10")
            if len(schedule.split()) == 5:
                bad.append(f"schedule={schedule!r} 是合法 5 段 cron，会被 resync 覆盖")
            if not code_cron:
                bad.append("meta.code_cron 为空（真实节奏意图丢失）")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"字典核对失败：{type(exc).__name__}: {exc}")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad)
        + "。收盘链断 = 信号晚到甚至永远不到（CL-20261109-17）")


def _c_schedule_parity(c: Ctx) -> tuple[bool, str, str, str]:
    """调度频率只有一个真相：字典 schedule 必须等于代码节奏（CL-20261109-07）。

    `_reregister_schedules_from_db()` 在启动时用 `ops.scheduled_task.schedule`
    （**executor='apscheduler' 的行**）覆盖代码里的 `CronTrigger`，字典赢。实测：

      · `fusion_scan` 字典 `1,31 * * * *` vs 代码 `5,35` → 回切 V3.4 会在 :01
        触发，早于 :00 的 hourly_collect 落盘（读到的是上一根）；
      · `v5x_scan` 字典 `unknown` → **靠两条巧合幸存**：executor='manual' 不被
        读取，且 'unknown' 不是 5 段 cron。谁"顺手补一条合法 cron"，双跑就静默
        变单跑，11:15 / 14:15 两根非整点 bar 永远不被评估（CL-20261108-28 栽过）。

    断言三面（都是「此刻可判」）：
      1. 两个扫描作业的字典值 == 由 `SCAN_MINUTES` 推出的 crontab（同源，不另抄）；
      2. `scheduler.py` 里 fusion 的 `minute="5,35"` 字面量必须与 `SCAN_MINUTES`
         一致 —— 否则有人改了常量却没改代码，两边会再次分叉；
      3. `v5x_snapshot` 必须登记、executor 非 apscheduler、schedule 保持惰性
         （非 5 段 cron）、且 `meta.code_cron` 非空 —— 意图不能丢。
         刻意不写 `5 15 * * 1-5`：`from_crontab` 的星期位 0=周一，`1-5` 会被
         解析成周二~周六。
    """
    label = "调度频率单一真相（字典 == 代码）"
    try:
        import ast

        from app.strategies.v5x.live import SCAN_MINUTES
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"import 失败 {type(exc).__name__}: {exc}", (
            "SCAN_MINUTES 不可导入（CL-20261109-07）")

    bad: list[str] = []
    observed: list[str] = []
    scan_cron = ",".join(str(m) for m in SCAN_MINUTES) + " * * * *"
    expected = {"fusion_scan": scan_cron, "v5x_scan": scan_cron}

    rows = c.rows(
        "SELECT task_id, coalesce(schedule,''), executor, coalesce(meta->>'code_cron','') "
        "FROM ops.scheduled_task WHERE task_id = ANY(:ids)",
        ids=list(expected) + ["v5x_snapshot"])
    got = {r[0]: (r[1], r[2], r[3]) for r in rows}

    for tid, want in expected.items():
        if tid not in got:
            bad.append(f"{tid} 未登记到 ops.scheduled_task")
            continue
        have, executor, _ = got[tid]
        observed.append(f"{tid}={have!r}/{executor}")
        if have.strip() != want:
            bad.append(f"{tid} 字典 schedule={have!r} != 代码期望 {want!r}")

    # 代码侧：fusion_scan 那个 add_job 的 minute 字面量必须与 SCAN_MINUTES 同源
    # （按 id 精确定位，不能扫全文件 —— 文件里有几十个 CronTrigger，取到别的作业
    #   的 minute='35' 会得到假 FAIL）
    try:
        src = (Path(__file__).resolve().parents[2] / "app" / "scheduler.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        fusion_minutes = None
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            fn = n.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name != "add_job":
                continue
            kws = {kw.arg: kw.value for kw in n.keywords if kw.arg}
            idv = kws.get("id")
            if not (isinstance(idv, ast.Constant) and idv.value == "fusion_scan"):
                continue
            trig = kws.get("trigger")
            if isinstance(trig, ast.Call):
                for kw in trig.keywords:
                    if kw.arg == "minute":
                        fusion_minutes = (kw.value.value
                                          if isinstance(kw.value, ast.Constant) else "<expr>")
        want_minutes = ",".join(str(m) for m in SCAN_MINUTES)
        observed.append(f"code_minute={fusion_minutes!r}")
        if fusion_minutes is None:
            bad.append("scheduler.py 未找到 id='fusion_scan' 的 add_job(minute=...)")
        elif fusion_minutes != want_minutes:
            bad.append(f"scheduler.py 里 fusion_scan 的 CronTrigger(minute={fusion_minutes!r}) "
                       f"与 SCAN_MINUTES={want_minutes!r} 不一致")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"scheduler.py AST 检查失败：{type(exc).__name__}: {exc}")

    # 新作业：必须登记，但 schedule 保持惰性（不给 from_crontab 误读的机会）
    if "v5x_snapshot" not in got:
        bad.append("v5x_snapshot 未登记到 ops.scheduled_task")
    else:
        have, executor, code_cron = got["v5x_snapshot"]
        parsed = len(have.split()) == 5
        observed.append(f"v5x_snapshot={have!r}/{executor} code_cron={bool(code_cron)}")
        if parsed:
            bad.append(f"v5x_snapshot.schedule={have!r} 是合法 5 段 cron —— "
                       "from_crontab 的星期位 0=周一，极易跑错一天；应保持惰性")
        if not code_cron:
            bad.append("v5x_snapshot 缺少 meta.code_cron（真实节奏的意图丢了）")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad)
        + "。字典与代码分叉 = 作业在没人预期的时间跑（CL-20261109-07）")


def _c_cfg_strategy_override(c: Ctx) -> tuple[bool, str, str, str]:
    """`strategies.<key>.params` 覆盖层必须真的生效（CL-20261108-09）。

    这条防的是「文档说能改、代码里没人读」：`YamlConfig` 一度**没有**
    `strategies` 字段，而 `registry.params_for` 用
    `getattr(get_settings(), "strategies", None)` 取覆盖 —— 恒为 None，
    于是 scheduler 里「只有 config 显式写了才会覆盖」的注释是反的。

    三面同时判（零 DB、零时间衰减）：
      1. **字段存在**：`YamlConfig.model_fields` 有 `strategies`，
         `Settings` 有同名属性；
      2. **返回纯 dict**：给 pydantic 模型会让 `registry` 的 `isinstance(ov, dict)`
         判假 → 覆盖再次静默失效（这比死代码更隐蔽）；
      3. **端到端合并**：临时替换 `get_settings`，验证 `params_for` 真的合并了
         覆盖值，且 `_fusion_params` 把它落到 FusionConfig。
    """
    label = "策略 config 覆盖层真实生效（strategies.<key>.params）"
    try:
        import app.core.config as cfgmod
        from app.core.config import EnvSettings, Settings, YamlConfig
        from app.strategies.registry import params_for
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"import 失败 {type(exc).__name__}: {exc}", (
            "配置/注册表不可导入（CL-20261108-09）")

    bad: list[str] = []
    observed: list[str] = []

    if "strategies" not in YamlConfig.model_fields:
        bad.append("YamlConfig 缺 strategies 字段 → 覆盖层永远是 None")
    if not hasattr(Settings, "strategies"):
        bad.append("Settings 未暴露 strategies 属性")

    def _mk(strategies: dict | None) -> Settings:
        y = YamlConfig.model_validate({"strategies": strategies} if strategies else {})
        return Settings(env=EnvSettings(), yaml=y)

    try:
        s = _mk({"fusion_v34": {"params": {"sl_atr": 1.5}}})
        st = s.strategies
        plain = isinstance(st.get("fusion_v34"), dict)
        observed.append(f"plain_dict={plain}")
        if not plain:
            bad.append("Settings.strategies 返回的不是原生 dict → registry 会静默跳过")

        orig = cfgmod.get_settings
        try:
            cfgmod.get_settings = lambda: s
            merged = params_for("fusion_v34")
        finally:
            cfgmod.get_settings = orig
        observed.append(f"params_for={merged}")
        if merged.get("sl_atr") != 1.5:
            bad.append(f"params_for 未合并覆盖值：{merged!r}")

        # 无覆盖时必须为空（生产现状：cloud.yaml 没有 strategies 段）
        orig = cfgmod.get_settings
        try:
            cfgmod.get_settings = lambda: _mk(None)
            empty = params_for("fusion_v34")
        finally:
            cfgmod.get_settings = orig
        if empty:
            bad.append(f"无 strategies 段时 params_for 应为空，实得 {empty!r}")
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"端到端合并异常 {type(exc).__name__}: {exc}", (
            "覆盖层接线不完整（CL-20261108-09）")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad)
        + "。按注释改配置调参会静默无效（CL-20261108-09）")


def _c_factor_z_ts_distinct(c: Ctx) -> tuple[bool, str, str, str]:
    """A1 / CL-20261108-20：横截面 z 与时序 z 必须是两个因子、两种语义。

    为什么这条值得一条 block 断言
    ----------------------------
    `member_net_z`（横截面 z，groupby trade_date）长期被当成「单品种择时门」使用，
    拿错语义不报错、只静默失效。实测 38 白名单近 365 日：
    **19/38 个品种 |z|>=1 占比为 0%**（avg|z| 0.150~5.858），门控 100% 误杀。
    这种错配属于「同名不同义」——与 `dim_variety.is_active` 同一类坑，
    只靠 code review 挡不住，必须机器判定「两种 z 真的不是同一个东西」。

    断言分两层，全部衡量「此刻的行为」：
      · 行为级（零 DB）：AST 抽出 `_zscore` 真身，用确定性序列验证
        ① 横截面模式：每个 trade_date 的 z 均值≈0、std≈1；
        ② 时序模式：前 min_periods-1 期必须无值（不许拿不足样本硬算）；
        ③ 两模式在同一 (symbol, date) 上 >=90% 不相等（防又合并回一套）；
        ④ **无前视**：只用前 60 期算出的 z 与用全量算出的前 60 期逐位相同
           （防「用全样本均值/标准差」这种最隐蔽的前视）；
      · 数据级：`member_net_ts_z` 已注册（version=1.1）、近 90 日有值，
        且逐品种 |z|>=1 占比为 0 的品种数 < 一半（与横截面 19/38 形成对照）。
    """
    label = "因子 z 语义可辨：member_net_z=横截面 / member_net_ts_z=时序"
    bad: list[str] = []
    observed: list[str] = []

    # ---------- ① 行为级：AST 抽出 _zscore 真身（不 import 整个脚本）----------
    try:
        import ast
        from datetime import date as _date, timedelta as _td

        import numpy as np
        import pandas as pd

        path = (Path(__file__).resolve().parents[2] / "scripts"
                / "compute_factor_value.py")
        tree = ast.parse(path.read_text(encoding="utf-8"))
        node = next((nd for nd in tree.body
                     if isinstance(nd, ast.FunctionDef) and nd.name == "_zscore"), None)
        if node is None:
            return (False, label, "compute_factor_value.py 找不到 _zscore",
                    "_zscore 缺失说明 z 口径实现被删/改名（CL-20261108-20）")
        consts = {"TS_Z_WINDOW": 60, "TS_Z_MIN_PERIODS": 20}
        for nd in tree.body:
            if (isinstance(nd, ast.Assign) and len(nd.targets) == 1
                    and isinstance(nd.targets[0], ast.Name)
                    and nd.targets[0].id in consts):
                try:
                    consts[nd.targets[0].id] = int(ast.literal_eval(nd.value))
                except Exception:                            # noqa: BLE001
                    pass
        ns: dict = {"pd": pd, "np": np, **consts}
        exec(compile(ast.Module(body=[node], type_ignores=[]),
                     "<_zscore>", "exec"), ns)               # noqa: S102
        _zscore = ns["_zscore"]
        min_p = int(consts["TS_Z_MIN_PERIODS"])
        observed.append(f"ts_window={consts['TS_Z_WINDOW']} min_periods={min_p}")

        n_days = 90
        rows = []
        for k, sym in enumerate(("A888", "B888", "C888")):
            base = 100.0 * (k + 1)
            for d in range(n_days):
                # A 逐日 +1 陡升，B/C 近乎横盘 -> 两种 z 必然给出不同结论
                v = base + d * (1.0 if k == 0 else 0.1) + (d % 7) * 0.5
                rows.append({"symbol": sym,
                             "trade_date": _date(2026, 1, 1) + _td(days=d),
                             "raw": float(v)})
        df = pd.DataFrame(rows)

        cs = _zscore(df, "raw", mode="cross_section")
        ts = _zscore(df, "raw", mode="time_series")
        d0 = _date(2026, 1, 1)
        d59 = d0 + _td(days=59)
        _df = df[(df["trade_date"] == d0)]
        d0_cs = _df.merge(cs[["symbol", "trade_date", "z"]],
                          on=["symbol", "trade_date"])["z"]
        if abs(float(d0_cs.mean())) > 1e-9 or abs(float(d0_cs.std(ddof=0)) - 1.0) > 1e-6:
            bad.append("横截面模式未按 trade_date 标准化（day0 mean/std 偏离 0/1）")
        else:
            observed.append("cs_day0_mean0_std1")

        early = ts[ts["trade_date"] < d0 + _td(days=min_p - 1)]
        if early["z"].notna().any():
            bad.append(f"时序模式在 min_periods={min_p} 之前就给了 z（样本不足硬算）")
        else:
            observed.append(f"ts_first_{min_p - 1}_null")

        a = cs.set_index(["symbol", "trade_date"])["z"]
        b = ts.set_index(["symbol", "trade_date"])["z"]
        both = a.notna() & b.notna()
        if not bool(both.any()):
            bad.append("两模式无可比样本")
        else:
            diff_ratio = float((abs(a[both] - b[both]) > 1e-9).sum()) / int(both.sum())
            observed.append(f"cs_vs_ts_diff={diff_ratio:.3f}")
            if diff_ratio < 0.9:
                bad.append(f"横截面 z 与时序 z 几乎相同（差异比 {diff_ratio:.2f} < 0.9）"
                           "——疑似又合并回同一套实现")

        part_ts = _zscore(df[df["trade_date"] <= d59], "raw", mode="time_series")
        j = part_ts.set_index(["symbol", "trade_date"])["z"]
        k2 = ts.set_index(["symbol", "trade_date"])["z"]
        idx = j.index.intersection(k2.index)
        idx = [t for t in idx if t[1] <= d59]
        if not idx:
            bad.append("无前视校验无样本")
        else:
            m = float(np.nanmax(np.abs(j.loc[idx].to_numpy(float)
                                      - k2.loc[idx].to_numpy(float))))
            observed.append(f"no_lookahead_maxdiff={m:.2e}")
            if not np.isfinite(m) or m > 1e-9:
                bad.append(f"时序模式存在前视（截断重算 maxdiff={m:.2e}）"
                           "——窗口不得依赖未来样本")
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"行为级对拍异常 {type(exc).__name__}: {exc}")

    # ---------- ② 数据级：注册 + 落库 + 分布对照 ----------
    try:
        reg = {r[0]: (r[1], r[2]) for r in c.rows(
            "SELECT factor_id, version, enabled FROM l2_adj.factor_registry "
            "WHERE factor_id IN ('member_net_z','member_net_ts_z')")}
        observed.append(f"registry={sorted(reg)}")
        if set(reg) != {"member_net_z", "member_net_ts_z"}:
            bad.append(f"factor_registry 缺注册项：实得 {sorted(reg)}（migrations/058）")
        elif str(reg["member_net_ts_z"][0]) != "1.1":
            bad.append(f"member_net_ts_z version={reg['member_net_ts_z'][0]!r} != '1.1'")
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"注册表检查失败：{type(exc).__name__}: {exc}")

    try:
        n_ts = int(c.scalar(
            "SELECT count(*) FROM l2_adj.factor_value "
            "WHERE factor_id='member_net_ts_z' AND trade_date >= CURRENT_DATE - 90") or 0)
        observed.append(f"ts_rows_90d={n_ts}")
        if n_ts <= 0:
            bad.append("member_net_ts_z 近 90 日 0 行（口径注册了但从未计算）")
        else:
            row = c.rows("""
                WITH w AS (SELECT variety_code||'888' AS s FROM l3_ref.tradable_variety
                           WHERE effective_from <= CURRENT_DATE
                             AND (effective_to IS NULL OR effective_to >= CURRENT_DATE)),
                v AS (SELECT f.symbol, f.z_value FROM l2_adj.factor_value f
                        JOIN w ON w.s = f.symbol
                       WHERE f.factor_id='member_net_ts_z'
                         AND f.trade_date >= CURRENT_DATE - 90),
                g AS (SELECT symbol, count(*) FILTER (WHERE abs(z_value) >= 1) AS k
                        FROM v GROUP BY symbol)
                SELECT count(*), count(*) FILTER (WHERE k = 0) FROM g
            """)[0]
            n_sym, n_zero = int(row[0]), int(row[1])
            observed.append(f"ts_syms={n_sym} zero_ratio_syms={n_zero}")
            if n_sym == 0:
                bad.append("member_net_ts_z 未覆盖白名单任何品种")
            elif n_zero * 2 >= n_sym:
                bad.append(f"{n_zero}/{n_sym} 个品种 |z|>=1 占比为 0 —— 时序 z 退化"
                           "（横截面 z 的历史症状是 19/38）")
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"分布检查失败：{type(exc).__name__}: {exc}")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad)
        + "。横截面 z / 时序 z 语义再次混用，单品种择时门会静默失效（CL-20261108-20）")


def _c_bt_ba_price_space(c: Ctx) -> tuple[bool, str, str, str]:
    """A2 / CL-20261108-21：后复权维度的「信号空间 / 真实价空间」必须分离。

    为什么这条值一条 block 断言
    --------------------------
    `l2_adj.bar_60m_ba` 的价格是 `ba = raw + adj_offset`，**不是可成交价**：
    实测 JD888 2026-11-06 15:00 `ba.close = -462.0`（offset -3923，真实价 3461）。
    切到该维度后，若组合层直接用 ba 价算成交与 pnl，成本、保证金、涨跌停比例、
    R 单位全部失真，甚至出现负价 —— 而且**不会报错**，只是数字变好看/变难看。

    断言全是「此刻的行为」（合成 K 逐位对拍，零 DB）：
      · `_generate_states` 输出 `close == close_sig − adj_offset`；
      · adj_offset 缺失（NaN）必须 fail loud，不许静默退化成原始价；
      · 换月假跳空：raw 空间 O−C=20（>0.5 ATR）会被拦，adj 空间为 0 不拦；
      · 成交价取**真实价**（121），不得取信号空间价（100）。
    """
    label = "后复权维度价格空间分离（信号=adj / 成交=真实价）"
    bad: list[str] = []
    observed: list[str] = []
    try:
        import numpy as np
        import pandas as pd

        from app.backtest import fusion_backtest as fb
    except Exception as exc:                                 # noqa: BLE001
        return False, label, f"import 失败 {type(exc).__name__}: {exc}", (
            "融合回测不可导入 → 价格空间断言不可用（CL-20261108-21）")

    n = 60
    k = 50                       # 换月根：真实价 100 → 120，后复权价保持 100
    dts = pd.date_range("2026-01-01", periods=n, freq="h")
    raws = np.array([100.0] * k + [120.0] * (n - k))
    offs = np.array([0.0] * k + [-20.0] * (n - k))
    bas = raws + offs

    # ---------- ① _generate_states 的空间契约 ----------
    try:
        df = pd.DataFrame({"dt": dts, "open": bas, "high": bas, "low": bas,
                           "close": bas, "adj_offset": offs})
        p = fb.FusionBacktestParams()
        st = fb._generate_states(df, p)
        observed.append(f"cols_sig={'close_sig' in st.columns}/"
                        f"{'atr_sig' in st.columns}")
        if "close_sig" not in st.columns or "atr_sig" not in st.columns:
            bad.append("_generate_states 未产出 close_sig/atr_sig（无法区分两个空间）")
        else:
            drift = float(np.max(np.abs(
                st["close"].to_numpy(float)
                - (st["close_sig"].to_numpy(float) - offs))))
            observed.append(f"close_eq_sig_minus_off={drift:.2e}")
            if drift > 1e-9:
                bad.append(f"真实价 != 信号价-偏移（maxdiff {drift:.2e}）")
            if st.attrs.get("price_space") != "adj":
                bad.append(f"attrs.price_space={st.attrs.get('price_space')!r} != 'adj'")
        # 缺偏移必须 fail loud（静默按 0 = 假装后复权）
        df_bad = df.copy()
        df_bad.loc[3, "adj_offset"] = np.nan
        try:
            fb._generate_states(df_bad, p)
            bad.append("adj_offset 缺失未拒绝（会静默退化成原始价）")
        except ValueError:
            observed.append("nan_offset=rejected")
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"_generate_states 空间对拍异常 {type(exc).__name__}: {exc}")

    # ---------- ② 跳空判定用 sig 空间 / 成交价用真实价 ----------
    def _states_frame(with_sig: bool) -> pd.DataFrame:
        # 信号必须落在换月**前一根**（i=k-1）：这样成交根 j=k 的开盘价与信号根
        # 收盘价之间才存在那 20 点「合约价差」，才谈得上「真跳空 vs 假跳空」。
        # 若信号落在 i=k，O[k] 与 C[k] 同属新合约，两边都是 0 跳空 → 对照组失效
        # （这正是本断言第一版写成 1 笔的 bug）。
        states = [0] * (k - 1) + [1] * 6 + [0] * (n - k - 5)
        d = {
            "dt": dts,
            "open": raws, "high": raws, "low": raws, "close": raws,
            "state": states,
            "lots": [1 if s == 1 else 0 for s in states],
            "atr": np.full(n, 1.0),
            "cur_stop": np.full(n, np.nan),
        }
        if with_sig:
            d["open_sig"] = bas
            d["close_sig"] = bas
            d["atr_sig"] = np.full(n, 1.0)
        return pd.DataFrame(d)

    try:
        p = fb.FusionBacktestParams(tick_size=1.0)
        tr = fb._simulate_trades(_states_frame(True), p, symbol=None)
        observed.append(f"adj_trades={len(tr)}"
                        + (f" entry={tr[0]['entry_px']}" if tr else ""))
        if len(tr) != 1:
            bad.append(f"后复权口径下换月不应拦信号（实得 {len(tr)} 笔，应 1 笔）")
        else:
            if abs(float(tr[0]["entry_px"]) - 121.0) > 1e-6:
                bad.append(f"成交价未用真实价（实得 {tr[0]['entry_px']}，应 121=120+1tick）")
            if abs(float(tr[0]["exit_px"]) - 119.0) > 1e-6:
                bad.append(f"平仓价未用真实价（实得 {tr[0]['exit_px']}，应 119=120-1tick）")
            if float(tr[0]["pnl"]) >= 0:
                bad.append(f"pnl 应为负（真实价 121→119），实得 {tr[0]['pnl']}")
        # control：不给 sig 列 = 旧行为，换月 20 点假跳空必须被拦
        tr0 = fb._simulate_trades(_states_frame(False), p, symbol=None)
        observed.append(f"raw_control_trades={len(tr0)}")
        if len(tr0) != 0:
            bad.append("raw 空间 20 点假跳空未被拦（0.5 ATR 过滤失效）")
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"_simulate_trades 对拍异常 {type(exc).__name__}: {exc}")

    # ---------- ③ AST：防「字段建了没人读」 ----------
    try:
        import inspect
        src_gap = inspect.getsource(fb._simulate_trades_ex)
        for token in ("opens_sig", "closes_sig", "atr_sig"):
            if token not in src_gap:
                bad.append(f"_simulate_trades_ex 未引用 {token}（跳空判定疑似回退到真实价）")
        src_gen = inspect.getsource(fb._generate_states)
        if "adj_offset" not in src_gen:
            bad.append("_generate_states 未读取 adj_offset（价格空间分离被移除）")
        observed.append("ast_tokens=ok")
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"AST 检查失败 {type(exc).__name__}: {exc}")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad)
        + "。后复权维度价格空间混用，回测 pnl/成本/保证金会失真（CL-20261108-21）")


def _c_v5x_engine_parity(c: Ctx) -> tuple[bool, str, str, str]:
    """C1 / CL-20261108-22：V5X.3 引擎移植后必须与冻结参数一致、门控仍生效。

    为什么这条必须存在
    ------------------
    V5X.3 的移植价值全部建立在「逐位复现冻结版」上：参数默认值改一个、门控顺序
    挪一下，回测数字（+244,742 / 2022 占 88%）就不可比了，而且**不报错**。
    历史教训（CL-20261108-08）就是「参数口径只存在于人脑子里」。

    断言（全是「此刻的行为」，零 DB）：
      · ``check_defaults()`` 为空 —— ``V5XConfig`` 默认值逐项等于 ``FROZEN_V5X3``；
      · 合成趋势序列必须触发突破开仓（信号层没被改坏）；
      · ``hvol=True`` / ``blocked=True`` → 不开仓（P2 静态门与簇冲击门生效）；
      · ``regime=2 且 vol_z>=2.5`` → 不开仓，``vol_z=1.0`` → 仍开仓
        （V5X.3 的核心增量就是这条「只削极端高波尾」）；
      · ``sess_last_hours`` 命中当根小时 → 不开仓（P6）。
    """
    label = "V5X.3 引擎移植：默认值=冻结参数且五道门控仍生效"
    bad: list[str] = []
    observed: list[str] = []
    try:
        import numpy as np
        import pandas as pd

        from app.strategies.v5x.engine import (
            CostCfg,
            DailyCtx,
            SymbolEngine,
            V5XConfig,
        )
        from app.strategies.v5x.params import check_defaults
    except Exception as exc:                                 # noqa: BLE001
        return False, label, f"import 失败 {type(exc).__name__}: {exc}", (
            "V5X 包不可导入 → 移植断言不可用（CL-20261108-22）")

    drift = check_defaults()
    observed.append(f"param_drift={len(drift)}")
    if drift:
        bad.append(f"V5XConfig 默认值偏离冻结参数：{drift[:4]}")

    # ---------- 合成趋势序列（必然触发突破支路）----------
    n = 200
    c_arr = 100.0 + 0.2 * np.arange(n)
    o_arr = c_arr - 0.15
    h_arr = c_arr + 0.1
    l_arr = c_arr - 0.2
    ts_list = pd.date_range("2026-01-05 09:00", periods=n, freq="h")

    cost = CostCfg(fee_row={"OPEN": (0.0, 0.0), "CLOSE_TODAY": (0.0, 0.0),
                            "CLOSE_YEST": (0.0, 0.0)},
                   slip_yuan=0.0, mult=10.0, tick=1.0)

    def _run(cfg: V5XConfig, ctx: DailyCtx) -> int:
        eng = SymbolEngine("RB888", cfg, cost)
        eng.set_daily_ctx(ctx)
        n_open = 0
        for i in range(n):
            orders = eng.on_bar(ts_list[i], float(o_arr[i]), float(h_arr[i]),
                                float(l_arr[i]), float(c_arr[i]), 1000.0,
                                float(c_arr[i]), allow=True)
            n_open += sum(1 for o in orders if o.get("type") == "OPEN")
        return n_open

    base_ctx = dict(td=None, hvol=False, member_z=float("nan"), blocked=False,
                    sess_last_hours=frozenset(), regime=None, vol_z=None,
                    pos_scale=1.0)
    try:
        cfg = V5XConfig()
        n_base = _run(cfg, DailyCtx(**base_ctx))
        observed.append(f"base_open={n_base}")
        if n_base < 1:
            bad.append("合成趋势序列未触发开仓（信号层被改坏）")

        for name, kw in (("hvol", {"hvol": True}), ("blocked", {"blocked": True})):
            n_g = _run(cfg, DailyCtx(**{**base_ctx, **kw}))
            observed.append(f"{name}_open={n_g}")
            if n_g != 0:
                bad.append(f"{name}=True 仍开仓（门控失效）")

        n_p2 = _run(cfg, DailyCtx(**{**base_ctx, "regime": 2, "vol_z": 3.0}))
        n_p2_lo = _run(cfg, DailyCtx(**{**base_ctx, "regime": 2, "vol_z": 1.0}))
        observed.append(f"p2_hi={n_p2} p2_lo={n_p2_lo}")
        if n_p2 != 0:
            bad.append("regime=2 且 vol_z=3.0 未禁开仓（REGIME_P2_AUTO 失效）")
        if n_p2_lo < 1:
            bad.append("regime=2 但 vol_z=1.0 也被禁（P2 应只削极端高波尾）")

        # P6：命中会话末根小时 → 当根不开仓。
        # 合成序列的小时是 9,10,…,23,0,1,… 连续递增，因此必须把 24 个小时
        # 全部纳入集合才等价于「每根都是会话末根」；只写 {9} 会漏掉大部分 bar
        # （本断言第一版就是这么写成 FAIL 的）。
        n_p6 = _run(cfg, DailyCtx(**{**base_ctx,
                                     "sess_last_hours": frozenset(range(24))}))
        observed.append(f"p6_open={n_p6}")
        if n_p6 != 0:
            bad.append("会话末根小时全覆盖仍开仓（P6 失效）")
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"引擎行为对拍异常 {type(exc).__name__}: {exc}")

    # ---------- 供数层：三种 td 入参必须都能查到上下文 ----------
    # 2026-11-08 实测踩过：dict 用 Timestamp 作 key、查表用 datetime64，
    # 结果 hvol / blocked / regime 三组配置跑出**完全相同**的数字（8,916 笔
    # / +249,726），三道门全部静默 miss。这种错不报错，只能靠断言抓。
    try:
        from datetime import date as _date

        from app.strategies.v5x.context import V5XMarketData, build_ctx

        md = V5XMarketData(src="bar_60m_ba")
        d = _date(2026, 1, 5)
        md.hvol = {"RB888": {d: True}}
        md.blocked = {"黑色": {d}}
        md.regime = {("RB888", d): (2, 3.1)}
        md.sess_last_hours = {"RB888": frozenset({15})}
        probes = ((d, "date"), (np.datetime64("2026-01-05"), "datetime64"),
                  (pd.Timestamp("2026-01-05 00:00"), "Timestamp"))
        miss = []
        for probe, tag in probes:
            ctx = build_ctx(md, "RB888", probe)
            if not (ctx.hvol and ctx.blocked and ctx.regime == 2
                    and abs(float(ctx.vol_z) - 3.1) < 1e-9):
                miss.append(tag)
        observed.append(f"ctx_key_types={3 - len(miss)}/3")
        if miss:
            bad.append(f"build_ctx 在 {miss} 入参下查不到上下文"
                       "（dict key 类型与查询类型不一致 → 门控静默失效）")
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"供数层 key 类型检查异常 {type(exc).__name__}: {exc}")

    # ---------- 供数层不得偷偷用横截面 z ----------
    try:
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / "app" / "strategies"
               / "v5x" / "context.py").read_text(encoding="utf-8")
        if "member_net_z" in src and "member_net_ts_z" not in src:
            bad.append("context.py 引用了横截面 member_net_z（应为 top20 时序 z）")
        if "top20" not in src:
            bad.append("context.py 未用 top20 净持仓（V5X M5 门口径）")
        observed.append("ctx_member_z=top20_ts")
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"context 源码检查失败 {type(exc).__name__}: {exc}")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad)
        + "。V5X.3 移植已偏离冻结口径，历史回测数字不再可比（CL-20261108-22）")


def _c_v5x_t1_gate_no_leak(c: Ctx) -> tuple[bool, str, str, str]:
    """C3 / CL-20261108-08：``gate_lag=1``（t-1 口径）三道门不得看到当日数据。

    为什么这条必须存在
    ------------------
    小时级扫描要在盘中任意整点算信号，而回测口径（``gate_lag=0``）的三道日频门
    都用到**当日**日线聚合（振幅 / 簇收益 / 已实现波动），收盘前算不出来。
    于是切 t-1。但「切了参数」≠「真不偷看」：``shift`` 加错位置（例如只挪阈值
    不挪被比较的值）会让代码看起来是 t-1、实际仍在用当日 —— 这类错误不报错，
    只会让回测好看起来。本断言用合成序列把方向钉死。

    断言（纯合成数据，零 DB）：
      · 只有**当日**振幅极端时，``lag=0`` 判禁开（True），``lag=1`` **不**判禁开；
      · 只有**当日**簇收益极端时，``lag=0`` 封禁，``lag=1`` **不**封禁；
      · 只有**当日**波动极端时，``lag=0`` 判高波体制 2，``lag=1`` **不**判 2；
      · 极端值出现在**前一天**时，``lag=1`` 必须能判出来（否则门等于关掉）。
    """
    label = "V5X t-1 门控不得使用当日日线（小时级实盘前提）"
    bad: list[str] = []
    observed: list[str] = []
    try:
        import pandas as pd

        from app.strategies.v5x.context import (
            _cluster_shock,
            _hvol_flags,
            _regime_table,
        )
    except Exception as exc:                                 # noqa: BLE001
        return False, label, f"import 失败 {type(exc).__name__}: {exc}", (
            "V5X 供数层不可导入 → t-1 口径断言不可用（CL-20261108-08）")

    n = 320
    TD = list(range(n))
    SYM = "RB888"        # 必须在 CLUSTER 里：簇冲击只对登记品种生效
    CL = "黑色"

    # ---------- hvol：极端振幅只出现在最后一天 ----------
    try:
        amp = [0.01] * (n - 1) + [0.99]
        d = pd.DataFrame({"symbol": SYM, "td": TD, "amp": amp,
                          "c": 100.0, "vol": 0.01})
        hv0 = bool(_hvol_flags(d, 0)[SYM][n - 1])
        hv1 = bool(_hvol_flags(d, 1)[SYM][n - 1])
        observed.append(f"hvol_lastday lag0={hv0} lag1={hv1}")
        if not hv0:
            bad.append("hvol lag=0 未用当日振幅（判定口径与预期不符）")
        if hv1:
            bad.append("hvol lag=1 仍用当日振幅（前视未消除）")

        # 极端值挪到倒数第二天 → lag=1 必须判出最后一天禁开
        amp2 = [0.01] * (n - 2) + [0.99, 0.01]
        d2 = d.assign(amp=amp2)
        hv1b = bool(_hvol_flags(d2, 1)[SYM][n - 1])
        observed.append(f"hvol_prevday lag1={hv1b}")
        if not hv1b:
            bad.append("hvol lag=1 对「昨日极端」不生效（门被关掉了）")
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"hvol 合成检查异常 {type(exc).__name__}: {exc}")

    # ---------- 簇冲击：极端日收益只出现在最后一天 ----------
    # 注意基线必须是 0 收益：若用常数非零收益，(r.abs() > 3*sd) 在 sd=0 时恒真，
    # 整段历史会被判封禁，测试就失去区分力。
    try:
        lret = [0.0] * (n - 1) + [0.5]
        d = pd.DataFrame({"symbol": SYM, "td": TD, "c": 100.0, "lret": lret,
                          "amp": 0.01, "vol": 0.01})
        b0 = _cluster_shock(d, 0).get(CL, set())
        b1 = _cluster_shock(d, 1).get(CL, set())
        observed.append(f"blocked_lastday lag0={n - 1 in b0} lag1={n - 1 in b1}")
        if not b0:
            bad.append("簇冲击 lag=0 未用当日收益（判定口径与预期不符）")
        if b1:
            bad.append("簇冲击 lag=1 仍用当日收益（前视未消除）")
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"簇冲击合成检查异常 {type(exc).__name__}: {exc}")

    # ---------- 体制：最后一天波动极端 ----------
    try:
        vol = [0.01] * (n - 1) + [9.9]
        d = pd.DataFrame({"symbol": SYM, "td": TD, "amp": 0.01,
                          "c": 100.0, "vol": vol})
        r0 = _regime_table(d, "strict", 0).get((SYM, n - 1), (None, None))[0]
        r1 = _regime_table(d, "strict", 1).get((SYM, n - 1), (None, None))[0]
        observed.append(f"regime_lastday lag0={r0} lag1={r1}")
        if r0 != 2:
            bad.append("体制 lag=0 未判高波（判定口径与预期不符）")
        if r1 == 2:
            bad.append("体制 lag=1 仍用当日波动（前视未消除）")
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"体制合成检查异常 {type(exc).__name__}: {exc}")

    # ---------- frozen + lag 必须 fail loud ----------
    try:
        from app.strategies.v5x.context import _regime_table as _rt
        try:
            _rt(pd.DataFrame({"symbol": SYM, "td": [0], "amp": [0.1],
                              "c": [1.0], "vol": [0.1]}), "frozen", 1)
            bad.append("frozen + gate_lag=1 未抛错（会产生既不复现也不可实盘的第三口径）")
        except ValueError:
            pass
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"frozen+lag 组合检查异常 {type(exc).__name__}: {exc}")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad)
        + "。t-1 口径失守 → 小时级扫描可能在偷看当日收盘（CL-20261108-08）")


def _c_v5x_live_wired(c: Ctx) -> tuple[bool, str, str, str]:
    """C3 / CL-20261108-08：V5X 小时扫描链路必须真接线，且**不得对外给 BA 价**。

    为什么这条必须存在
    ------------------
    1. 「表建了没人读」与「注册表加了没人跑」在本项目已各犯过一次。
       本断言检查的是**接线**：注册表有 v5x3、SIGNAL_WIRED_KEYS 认它、
       调度里存在 v5x_scan 作业、扫描器常量仍为 t-1 口径。
    2. V5X 信号价活在 BA 后复权空间（JD888 实测 ba.close = -462），
       推送必须已反解。若某次改动把反解漏掉，人收到的是负价格，
       而系统不会报错。故用真实表数据校验 ``price_real = price_ba - adj_offset``。
    """
    label = "V5X 小时扫描接线 + 出口价格反解"
    bad: list[str] = []
    observed: list[str] = []

    # ---------- 1) 注册表接线 ----------
    try:
        from app.strategies.registry import REGISTRY, SIGNAL_WIRED_KEYS

        observed.append(f"registry={'v5x3' in REGISTRY}")
        if "v5x3" not in REGISTRY:
            bad.append("registry 未登记 v5x3")
        if "v5x3" not in SIGNAL_WIRED_KEYS:
            bad.append("v5x3 未在 SIGNAL_WIRED_KEYS（会被当成静默空转）")
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"注册表检查异常 {type(exc).__name__}: {exc}")

    # ---------- 2) 扫描器常量仍为 t-1 ----------
    try:
        from app.strategies.v5x import live as _live

        observed.append(f"gate_lag={_live.GATE_LAG} regime={_live.REGIME_MODE} "
                        f"scan_minutes={_live.SCAN_MINUTES}")
        if _live.GATE_LAG != 1:
            bad.append(f"扫描器 GATE_LAG={_live.GATE_LAG}（小时级实盘必须为 1）")
        if _live.REGIME_MODE != "strict":
            bad.append(f"扫描器 REGIME_MODE={_live.REGIME_MODE}（必须 strict 才无前视）")
        # CL-20261108-28：节奏必须是 :05/:35 双时刻。只跑 :05 会漏掉 11:15 / 14:15
        # 这两根非整点收盘的日盘 bar（实测 2026-11-06 14:15 的 FU888 平仓信号从未产出）。
        mins = tuple(_live.SCAN_MINUTES)
        if len(mins) < 2:
            bad.append(f"SCAN_MINUTES={mins}（必须至少 :05/:35 两个时刻，"
                       "单时刻会漏掉非整点收盘的 bar）")
        if 5 not in mins or 35 not in mins:
            bad.append(f"SCAN_MINUTES={mins} 缺 :05 或 :35")
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"扫描器导入异常 {type(exc).__name__}: {exc}")

    # ---------- 3) 调度作业存在 ----------
    try:
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2] / "app" / "scheduler.py").read_text(
            encoding="utf-8")
        has_job = "def _v5x_scan_job" in src and 'id="v5x_scan"' in src
        observed.append(f"sched_job={has_job}")
        if not has_job:
            bad.append("scheduler 缺少 v5x_scan 作业注册（扫描永不触发）")
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"scheduler 源码检查失败 {type(exc).__name__}: {exc}")

    # ---------- 4) 表与开关 ----------
    try:
        n_tbl = c.scalar("""SELECT count(*) FROM information_schema.tables
                            WHERE table_schema='public' AND table_name='v5x_signal_log'""")
        observed.append(f"signal_log_table={bool(n_tbl)}")
        if not n_tbl:
            bad.append("缺少 public.v5x_signal_log（去重与留痕无处落）")
        sw = c.scalar("SELECT enabled FROM cfg_feature_switch "
                      "WHERE switch_key='v5x_live_enabled'")
        observed.append(f"switch={'missing' if sw is None else bool(sw)}")
        if sw is None:
            bad.append("cfg_feature_switch 缺少 v5x_live_enabled（扫描无法开关）")
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"表/开关检查异常 {type(exc).__name__}: {exc}")

    # ---------- 5) 已推信号必须已反解 ----------
    try:
        row = c.session.execute(text("""
            SELECT count(*) AS n,
                   count(*) FILTER (WHERE abs(price_real - (price_ba - adj_offset)) > 1e-6) AS bad,
                   count(*) FILTER (WHERE abs(adj_offset) > 1e-9
                                      AND abs(price_real - price_ba) < 1e-9) AS not_inverted
            FROM public.v5x_signal_log
        """)).one()
        n, n_bad, n_not_inv = int(row[0]), int(row[1]), int(row[2] or 0)
        observed.append(f"sig_rows={n} invert_mismatch={n_bad} ba_leak={n_not_inv}")
        if n_bad:
            bad.append(f"{n_bad} 条信号的 price_real 与 ba-offset 不一致")
        if n_not_inv:
            bad.append(f"{n_not_inv} 条信号 offset≠0 却 price_real==price_ba（BA 价泄漏）")
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"信号表反解校验异常 {type(exc).__name__}: {exc}")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad)
        + "。V5X 只推送链路已断或价格未反解（CL-20261108-08 C3-min）")


def _c_bt_gap_filter_wired(c: Ctx) -> tuple[bool, str, str, str]:
    """E1-C / CL-20261108-15：0.5 ATR 不利跳空过滤必须真实接线。

    全部是「此刻的行为」，不看历史流水、不靠时间衰减：
      · 默认口径 = next_open + 0.5 ATR；
      · 1.0 ATR 不利跳空 → 整段作废（0 笔）；关掉过滤 → 1 笔且成交价含 1tick；
      · 封锁期内即便跳空恢复也不重试（防退化成语义 A「延后一根」），
        归零解锁后新信号段才重新开仓；
      · 平仓腿不受过滤影响；close 老基线忽略过滤、成交价仍为收盘价；
      · 缺 open 列必须 fail loud；活跃品种 tick_size 必须 >0；
      · AST 确认 _gap_blocks 被真实调用 ≥2 处（防「定义了没人用」）。
    """
    label = "E1-C 跳空不利过滤（0.5 ATR / 语义 B）真实接线"
    try:
        import ast
        import pandas as pd
        from app.backtest import fusion_backtest as fb
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"import 失败 {type(exc).__name__}: {exc}", (
            "融合回测模块不可导入 → 跳空过滤断言不可用（CL-20261108-15）")

    bad: list[str] = []
    observed: list[str] = []
    bp = fb.FusionBacktestParams()
    observed.append(f"defaults={bp.fill_mode!r}/{bp.max_adverse_gap_atr!r}")
    if bp.fill_mode != "next_open":
        bad.append(f"默认 fill_mode={bp.fill_mode!r} != next_open")
    if abs(float(getattr(bp, "max_adverse_gap_atr", 0.0)) - 0.5) > 1e-9:
        bad.append("默认 max_adverse_gap_atr != 0.5（用户拍板阈值）")

    n = 8

    def frame(states, opens, lots=None, with_open=True):
        d = {
            "dt": pd.date_range("2026-01-01", periods=n, freq="h"),
            "close": [100.0] * n,
            "state": states,
            "lots": lots if lots is not None else [0 if s == 0 else 1 for s in states],
            "atr": [1.0] * n,
        }
        if with_open:
            d["open"] = opens
        return pd.DataFrame(d)

    def run(states, overrides=None, lots=None, with_open=True, **kw):
        o = [100.0] * n
        for k, v in (overrides or {}).items():
            o[k] = v
        p = fb.FusionBacktestParams(tick_size=1.0, **kw)
        return fb._simulate_trades(
            frame(states, o, lots, with_open), p, symbol=None)

    seg = [0, 1, 1, 1, 0, 0, 0, 0]
    try:
        tr = run(seg, {2: 101.0})
        if tr:
            bad.append(f"1.0 ATR 不利跳空未拦截（实得 {len(tr)} 笔）")
        else:
            observed.append("gap_1atr=blocked")
        tr = run(seg, {2: 101.0}, max_adverse_gap_atr=0.0)
        if len(tr) != 1 or abs(tr[0]["entry_px"] - 102.0) > 1e-9:
            bad.append("关掉过滤后应 1 笔 @102（O[2]=101 +1tick）")
        else:
            observed.append("no_filter=1@102")
        tr = run([0, 1, 1, 1, 0, 1, 1, 0], {2: 101.0})
        if len(tr) != 1 or abs(tr[0]["entry_px"] - 101.0) > 1e-9:
            bad.append("整段作废/解锁语义不成立（疑似退化成『延后一根』）")
        else:
            observed.append("unlock_after_new_segment=1@101")
        tr = run(seg, {5: 101.0})
        if len(tr) != 1 or abs(tr[0]["exit_px"] - 100.0) > 1e-9:
            bad.append("平仓腿被误过滤（第 5 条口径要求直接成交）")
        else:
            observed.append("exit_not_filtered=100")
        tr = run(seg, {2: 101.0}, fill_mode="close")
        if (len(tr) != 1 or abs(tr[0]["entry_px"] - 100.0) > 1e-9
                or abs(tr[0]["exit_px"] - 100.0) > 1e-9):
            bad.append("close 老基线被跳空过滤污染（应逐位零回归）")
        else:
            observed.append("close_baseline=100/100")
        tr = run(seg, {3: 101.0, 4: 101.0}, lots=[0, 1, 2, 2, 0, 0, 0, 0])
        if len(tr) != 1 or any(t.get("addon") for t in tr):
            bad.append("加仓被拒后持仓状态异常（应只取消该次加仓）")
        else:
            observed.append("addon_rejected_keeps_pos")
        try:
            run(seg, {2: 101.0}, with_open=False)
            bad.append("next_open 缺 open 列未拒绝（会静默退回收盘价）")
        except ValueError:
            observed.append("missing_open=rejected")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"行为级对拍异常 {type(exc).__name__}: {exc}")

    try:
        row = c.rows(
            "SELECT count(*) FILTER (WHERE is_active), "
            "count(*) FILTER (WHERE is_active AND "
            "(tick_size IS NULL OR tick_size <= 0)) FROM public.dim_variety")[0]
        observed.append(f"active_varieties={row[0]} bad_tick={row[1]}")
        if row[1]:
            bad.append("活跃品种存在 tick_size<=0 → 1tick 滑点会静默消失")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"tick_size 覆盖检查失败：{type(exc).__name__}: {exc}")

    try:
        src = (Path(__file__).resolve().parents[2] / "app" / "backtest"
               / "fusion_backtest.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        defined = any(isinstance(nd, ast.FunctionDef) and nd.name == "_gap_blocks"
                      for nd in ast.walk(tree))
        calls = sum(1 for nd in ast.walk(tree)
                    if isinstance(nd, ast.Call) and isinstance(nd.func, ast.Name)
                    and nd.func.id == "_gap_blocks")
        observed.append(f"gap_defined={defined} gap_calls={calls}")
        if not defined or calls < 2:
            bad.append("_gap_blocks 未定义或未被开仓/加仓两处真实引用")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"源码 AST 检查失败：{type(exc).__name__}: {exc}")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad)
        + "。E1-C 跳空过滤被回退或改坏，融合回测口径不可信（CL-20261108-15）")


def _c_bt_pool_in_whitelist(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261108-12（用户 19:48 拍板）：回测池严格 38 白名单，其余不放行。

    三层都是「此刻的行为」，不看历史流水：
      · 行为级：白名单 symbol 放行，B888 / CU888 / J888 / 合约码一律拒绝；
      · AST 真实引用：run_fusion_backtest 必须调用 load_tradable_pool +
        _validate_backtest_symbols，且不得再读 settings.main_contracts
        （防「表建了没人读」——qhyc 最隐蔽的失效模式）；
      · 生产口径：有效白名单 30~45 个且不含持仓豁免品种；同时
        v_tradable_scope 必须仍含 B888/J888/LH888/NI888 四个持仓豁免，
        防有人为「严格不放行」把存量持仓的 bar 与止损价一起停掉。
    """
    label = "回测品种池严格 38 白名单（持仓豁免只留数据链路）"
    try:
        import ast
        from pathlib import Path
        from app.backtest import fusion_backtest as fb
        from app.data.tradable_scope import load_tradable_pool
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"import 失败 {type(exc).__name__}: {exc}", (
            "回测/口径模块不可导入 → 品种池断言不可用（CL-20261108-12）")

    bad: list[str] = []
    observed: list[str] = []
    pool = frozenset({"RB", "FG", "MA"})

    try:
        got = fb._validate_backtest_symbols(["RB888", "FG8888"], pool)
        observed.append(f"accepted={len(got)}")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"白名单 symbol 被误拒：{type(exc).__name__}: {exc}")
    for sym in ("B888", "CU888", "J888", "RB2601"):
        try:
            fb._validate_backtest_symbols([sym], pool)
            bad.append(f"非白名单 {sym} 未被拒绝（会静默跑错品种池）")
        except ValueError:
            observed.append(f"rejected={sym}")

    try:
        src = (Path(__file__).resolve().parents[2] / "app" / "backtest"
               / "fusion_backtest.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(nd for nd in tree.body
                  if isinstance(nd, ast.FunctionDef)
                  and nd.name == "run_fusion_backtest")
        called = {nd.func.id for nd in ast.walk(fn)
                  if isinstance(nd, ast.Call) and isinstance(nd.func, ast.Name)}
        attrs = {nd.attr for nd in ast.walk(fn) if isinstance(nd, ast.Attribute)}
        observed.append(f"wired={'load_tradable_pool' in called}"
                        f"/{'_validate_backtest_symbols' in called}")
        if "load_tradable_pool" not in called or "_validate_backtest_symbols" not in called:
            bad.append("回测入口未真实引用白名单加载/校验函数（表建了没人读）")
        if "main_contracts" in attrs:
            bad.append("回测入口仍读 settings.main_contracts（50+ 含非白名单）")
        # OOS 与旧多品种引擎同样不得绕过：否则 B1 / V5X.3 的 OOS 会跑错池
        root = Path(__file__).resolve().parents[2]
        for rel in ("app/backtest/walkforward.py", "app/backtest/engine.py"):
            t2 = ast.parse((root / rel).read_text(encoding="utf-8"))
            a2 = {nd.attr for nd in ast.walk(t2) if isinstance(nd, ast.Attribute)}
            if "main_contracts" in a2:
                bad.append(f"{rel} 仍读 settings.main_contracts")
        observed.append("other_bt_entries=clean")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"源码 AST 检查失败：{type(exc).__name__}: {exc}")

    try:
        live = load_tradable_pool(c.session)
        observed.append(f"live_pool={len(live)}")
        if not 30 <= len(live) <= 45:
            bad.append(f"有效白名单 {len(live)} 个，偏离 38 口径")
        leaked = sorted({"B", "CS", "J", "LH", "NI"} & set(live))
        if leaked:
            bad.append(f"封禁/持仓豁免品种混入白名单池：{leaked}")
        rows = c.rows(
            "SELECT symbol FROM l3_ref.v_tradable_scope "
            "WHERE symbol IN ('B888','J888','LH888','NI888')")
        exempt = {r[0] for r in rows}
        observed.append(f"scope_exempt={len(exempt)}")
        if len(exempt) < 4:
            bad.append("持仓豁免被一刀切停派生 → 存量持仓拿不到 bar 与止损价")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"生产口径查询失败：{type(exc).__name__}: {exc}")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad)
        + "。回测品种池偏离 38 白名单或持仓豁免被误停，结论不可信（CL-20261108-12）")


def _c_bt_b1_penalty_wired(c: Ctx) -> tuple[bool, str, str, str]:
    """P2-5 / CL-20261108-19：B1 执行惩罚必须真实接线。

    路线图 §3.3 把回测的乐观写成 4 项，本断言覆盖其中 3 项（第 1 项同根收盘
    已由 E1-C 修，见 BT-GAP-FILTER-WIRED）。全部衡量「此刻的行为」：

      · 默认 ``exec_penalty_mode="off"``，且 off 下即使给了 high/low/cur_stop
        也必须与旧基线**逐位一致**（否则每次重跑都在偷偷改历史数字）；
      · 盘中击穿：``cur_stop[i-1]`` 被 bar i 的 low/high 击穿 → **当根**离场，
        并只用 i-1 的止损（用 cur_stop[i] 撞 bar i 是前视）；
      · 一字 K（O==H==L==C **且** 跳空 ≥ limit_move_pct）拒绝开仓但**不拒平仓**；
        低波动 O==H==L==C（无跳空）不得被误判为停板；
      · 资金不足拒开仓；充足则记录该手保证金（margin 字段 > 0）；
      · 缺 high/low/cur_stop 必须 fail loud（否则盘中击穿静默失效）；
      · AST：``_generate_states`` 真的输出 cur_stop、``_simulate_trades_ex``
        真的引用 exec_penalty_mode / _one_price / _margin_per_lot / flat_lock
        —— 专治「字段建了没人读」。

    ⚠ 已知不建模：流动性（回测取数层 ``_BASE_COLS`` 只含 OHLC，无 volume）
    与真实涨跌停板价（库内 high_limit/low_limit 近 30 天 0 行有效值）。
    报告口径不得写成「已建模」。
    """
    label = "B1 执行惩罚真实接线（盘中击穿 / 一字 K / 保证金 / 未收盘 K）"
    try:
        import inspect

        import pandas as pd

        from app.backtest import fusion_backtest as fb
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"import 失败 {type(exc).__name__}: {exc}", (
            "融合回测模块不可导入 → B1 断言不可用（CL-20261108-19）")

    bad: list[str] = []
    observed: list[str] = []
    bp = fb.FusionBacktestParams()
    observed.append(f"default={bp.exec_penalty_mode!r}")
    if bp.exec_penalty_mode != "off":
        bad.append(f"默认 exec_penalty_mode={bp.exec_penalty_mode!r}，必须 'off'"
                   "（否则旧基线不可复现）")

    n = 8
    seg = [0, 1, 1, 1, 0, 0, 0, 0]
    nan = float("nan")

    def frame(states, opens=None, highs=None, lows=None, closes=None,
              stops=None, lots=None, with_hl=True):
        c_list = list(closes) if closes is not None else [100.0] * len(states)
        o_list = list(opens) if opens is not None else [100.0] * len(states)
        d = {
            "dt": pd.date_range("2026-01-01", periods=len(states), freq="h"),
            "open": o_list, "close": c_list,
            "state": states,
            "lots": lots if lots is not None else
            [0 if s == 0 else 1 for s in states],
            "atr": [1.0] * len(states),
        }
        if with_hl:
            d["high"] = list(highs) if highs is not None else list(c_list)
            d["low"] = list(lows) if lows is not None else list(c_list)
            d["cur_stop"] = stops if stops is not None else [nan] * len(states)
        return pd.DataFrame(d)

    def run(fr, **kw):
        p = fb.FusionBacktestParams(tick_size=1.0,
                                    max_adverse_gap_atr=0.0, **kw)
        return fb._simulate_trades(fr, p, symbol=None)

    try:
        stops_hit = [nan, 95.0, 95.0, 95.0] + [nan] * (n - 4)
        low_hit = [100.0] * n
        low_hit[2] = 94.0
        # 1) off 模式逐位一致（即便多了 high/low/cur_stop 列）
        a = run(frame(seg, lows=low_hit, stops=stops_hit))
        b = run(frame(seg, with_hl=False))
        ka = [(t["entry_px"], t["exit_px"], t["pnl"], str(t["exit_dt"])) for t in a]
        kb = [(t["entry_px"], t["exit_px"], t["pnl"], str(t["exit_dt"])) for t in b]
        if ka != kb:
            bad.append("off 模式下新增列改变了成交（旧基线不再逐位可复现）")
        else:
            observed.append(f"off_bitwise={len(ka)}")
        # 2) 盘中击穿当根离场，成交价含 1tick
        tr = run(frame(seg, lows=low_hit, stops=stops_hit),
                 exec_penalty_mode="b1")
        if (len(tr) != 1
                or pd.Timestamp(tr[0]["exit_dt"]) != pd.Timestamp("2026-01-01 02:00")
                or abs(tr[0]["exit_px"] - 94.0) > 1e-9):
            bad.append(f"盘中击穿未在当根离场（实得 {len(tr)} 笔 / "
                       f"exit={tr[0]['exit_dt'] if tr else None}）")
        else:
            observed.append("intrabar_exit=bar2@94")
        # 3) 时点纪律：不得用 cur_stop[i]
        stops_lag = [nan, 95.0, 90.0, 90.0] + [nan] * (n - 4)
        low_lag = [100.0] * n
        low_lag[2] = 92.0
        tr = run(frame(seg, lows=low_lag, stops=stops_lag),
                 exec_penalty_mode="b1")
        if not tr or abs(tr[0]["exit_px"] - 94.0) > 1e-9:
            bad.append("盘中击穿用了 cur_stop[i]（前视），应按 cur_stop[i-1]=95")
        else:
            observed.append("stop_lag_ok")
        # 4) 一字 K 拒开仓、但延后一根仍可成交
        o1 = [100.0] * n
        c1 = [100.0] * n
        h1 = [100.5] * n
        l1 = [99.5] * n
        for arr in (o1, c1, h1, l1):
            arr[2] = 104.0
        tr = run(frame(seg, opens=o1, closes=c1, highs=h1, lows=l1),
                 exec_penalty_mode="b1")
        if (len(tr) != 1
                or pd.Timestamp(tr[0]["entry_dt"]) != pd.Timestamp("2026-01-01 03:00")):
            bad.append("一字 K 未拒绝开仓（或未在下一根恢复）")
        else:
            observed.append("one_price_entry_deferred")
        # 5) 低波动 O==H==L==C 不得误判成停板
        tr = run(frame(seg), exec_penalty_mode="b1")
        if len(tr) != 1:
            bad.append("低波动 O==H==L==C 被误判成停板 → 该品种几乎无法开仓")
        else:
            observed.append("low_vol_not_limit")
        # 6) 一字不阻止平仓
        o2 = [100.0] * n
        c2 = [100.0] * n
        h2 = [100.5] * n
        l2 = [99.5] * n
        for arr in (o2, c2, h2, l2):
            arr[5] = 104.0
        tr = run(frame(seg, opens=o2, closes=c2, highs=h2, lows=l2),
                 exec_penalty_mode="b1")
        if not tr or abs(tr[0]["exit_px"] - 103.0) > 1e-9:
            bad.append("一字 K 误阻止平仓（会让持仓无限期推迟出场）")
        else:
            observed.append("exit_not_blocked")
        # 7) 保证金约束
        if run(frame(seg), exec_penalty_mode="b1", margin_rate=0.1,
               initial_capital=5.0):
            bad.append("资金不足时仍开仓（保证金约束未接线）")
        else:
            observed.append("margin_cap_blocks")
        tr = run(frame(seg), exec_penalty_mode="b1", margin_rate=0.1,
                 initial_capital=1000.0)
        if not tr or abs(float(tr[0].get("margin") or 0.0)
                         - 101.0 * 0.1 * 1.03) > 1e-6:
            bad.append("开仓未记录该手保证金（margin 字段缺失或口径错）")
        else:
            observed.append("margin_per_lot_ok")
        # 8) 缺列 fail loud
        try:
            run(frame(seg, with_hl=False), exec_penalty_mode="b1")
            bad.append("b1 缺 high/low/cur_stop 未拒绝（盘中击穿会静默失效）")
        except ValueError:
            observed.append("missing_cols=rejected")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"行为级对拍异常 {type(exc).__name__}: {exc}")

    # 9) AST / 源码引用：字段建了必须有人读
    try:
        src_gen = inspect.getsource(fb._generate_states)
        if "cur_stop" not in src_gen:
            bad.append("_generate_states 未输出 cur_stop（盘中击穿无数据可用）")
        src_sim = inspect.getsource(fb._simulate_trades_ex)
        for token in ("exec_penalty_mode", "_one_price", "_margin_per_lot",
                      "flat_lock"):
            if token not in src_sim:
                bad.append(f"_simulate_trades_ex 未引用 {token}（疑似死代码）")
        if "cur_stop" not in src_sim:
            bad.append("_simulate_trades_ex 未读取 cur_stop")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"源码引用检查失败：{type(exc).__name__}: {exc}")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad)
        + "。B1 执行惩罚未真实落地，G4 净利判据不可信（CL-20261108-19）")


def _c_bt_walkforward_same_source(c: Ctx) -> tuple[bool, str, str, str]:
    """P2-3 E2 / CL-20261108-13：walk-forward 的日期范围必须来自唯一 K 线真源。

    行为级，不看历史流水、不靠时间衰减：
      · 调 _data_range(RB888/PF888/FG888, 'bar_60m')，必须与直接查
        l1_mkt.bar_60m 的 min/max bucket 日期**逐位相等**（同源对拍）；
      · 结果必须非空 —— 旧实现恒返回 (None,None)，令 run_walk_forward
        默认调用静默「无数据」，这正是本次修掉的失效；
      · 废弃维度 src='akshare' 必须被 normalize_src 拒绝（防绕过口径）；
      · AST 检查 walkforward 不再 import HourlyBar、确实引用 fusion_kline
        （防 docstring/注释里的字面量造成假阳性）。
    """
    label = "walk-forward 日期范围取自唯一 K 线真源（E2 残留已消除）"
    try:
        import ast
        from app.backtest.walkforward import _data_range
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"import 失败 {type(exc).__name__}: {exc}", (
            "walkforward 不可导入 → E2 同源断言不可用（CL-20261108-13）")

    syms = ["RB888", "PF888", "FG888"]
    bad: list[str] = []
    observed: list[str] = []
    try:
        got = _data_range(c.session, syms, "bar_60m")
        ref = c.rows(
            "SELECT min(bucket), max(bucket) FROM l1_mkt.bar_60m "
            "WHERE symbol IN ('RB888','PF888','FG888')"
        )[0]
        expected = (ref[0].date(), ref[1].date())
        observed.append(f"range={got[0]}~{got[1]}")
        if not got or got[0] is None:
            bad.append("_data_range 返回空 → run_walk_forward 默认调用仍静默无数据")
        elif tuple(got) != expected:
            bad.append(f"_data_range={got} != 真源 {expected}（又出现第二条读取路径）")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"同源对拍异常：{type(exc).__name__}: {exc}")

    try:
        _data_range(c.session, syms, "akshare")
        bad.append("废弃维度 src=akshare 未被拒绝（normalize_src 被绕过）")
    except ValueError:
        observed.append("akshare=rejected")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"废弃维度校验抛了非 ValueError：{type(exc).__name__}: {exc}")

    try:
        src = (Path(__file__).resolve().parents[2] / "app" / "backtest"
               / "walkforward.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        imports = [n for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
        legacy = any((n.module or "") == "app.models"
                     and any(a.name == "HourlyBar" for a in n.names)
                     for n in imports)
        wired = any("fusion_kline" in (n.module or "") for n in imports)
        observed.append(f"legacy_import={legacy} fusion_kline_wired={wired}")
        if legacy:
            bad.append("walkforward 仍 import HourlyBar（残留未清）")
        if not wired:
            bad.append("walkforward 未引用 fusion_kline（同源函数被绕过）")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"源码 AST 检查失败：{type(exc).__name__}: {exc}")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad)
        + "。walk-forward 会静默返回无数据或读到废弃链（CL-20261108-13）")


def _c_bt_exec_fork_declared(c: Ctx) -> tuple[bool, str, str, str]:
    """P2-3 E1-A/E1-B / CL-20261108-14：执行分叉声明必须与当前行为一致。

    衡量的都是「此刻的行为」，防声明表沦为过期文档：
      · 声明表 6 条齐全、状态合法、每条有证据 / 处置 / 关联键（防空声明）；
      · 6 个默认值任一被改动都会让实测证据失效，必须逐项核实；
      · 直接对契约层纯函数 fill_price 做多空逐位对拍（close / next_open /
        末尾边界），并与 _simulate_trades 的合成 K 结果比对；
      · AST 确认 _simulate_trades 确实 import 并委托契约层（防「抽了没人用」）；
      · 白名单条目 is_allowed=True，延后条目归因到 CL-20261108-08。
    """
    label = "执行分叉声明契约与当前行为一致（E1-A/E1-B）"
    try:
        import ast
        import pandas as pd
        from app.backtest import fusion_backtest as fb
        from app.contracts import execution as ec
        from app.core.config import get_settings
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"import 失败 {type(exc).__name__}: {exc}", (
            "契约层或回测模块不可导入 → 分叉声明断言不可用（CL-20261108-14）")

    bad: list[str] = []
    observed: list[str] = []

    # ① 声明表自身完整性
    ok, problems = ec.validate_fork_declaration()
    observed.append(f"declaration_ok={ok} forks={len(ec.EXECUTION_FORKS)}")
    bad.extend(problems)

    # ② 默认值：任一变化都会让声明表里的实测证据失效
    bp = fb.FusionBacktestParams()
    try:
        cfg = get_settings().fusion
    except Exception as exc:                              # noqa: BLE001
        cfg = None
        bad.append(f"FusionConfig 不可读：{type(exc).__name__}: {exc}")
    checks = (
        # E1-C / CL-20261108-15：默认成交口径已切 next_open + 0.5 ATR 过滤，
        # 任一漂移都会让 E1 的 6 条分叉实测证据失效。
        ("fill_mode", getattr(bp, "fill_mode", None), "next_open"),
        ("max_adverse_gap_atr", getattr(bp, "max_adverse_gap_atr", None), 0.5),
        ("max_hold_bars", getattr(bp, "max_hold_bars", None), 0),
        ("lookback_bars", getattr(bp, "lookback_bars", None), 0),
        ("max_bars", getattr(bp, "max_bars", None), 800),
        ("max_hold_days", getattr(cfg, "max_hold_days", None) if cfg else None, 365),
        ("entry_gate_profile",
         getattr(cfg, "entry_gate_profile", None) if cfg else None, "off"),
    )
    for name, got, want in checks:
        observed.append(f"{name}={got!r}")
        if got != want:
            bad.append(f"{name}={got!r} != 声明基线 {want!r} → 分叉证据需重新量化")

    # ③ 纯函数行为级对拍（合成 8 根 K，与 BT-FILL-MODE-WIRED 同网格）
    n = 8
    opens = [100.0 + 10.0 * i for i in range(n)]
    closes = [103.0 + 10.0 * i for i in range(n)]
    cases = (
        ("close", 0.0, 1.0, True, 1, (113.0, 1)),
        ("close", 0.0, 1.0, False, 4, (143.0, 4)),
        ("next_open", 2.0, 1.0, True, 1, (122.0, 2)),
        ("next_open", 2.0, 1.0, False, 4, (148.0, 5)),
        ("next_open", 2.0, -1.0, True, 1, (118.0, 2)),
        ("next_open", 2.0, -1.0, False, 4, (152.0, 5)),
        ("next_open", 2.0, 1.0, False, n - 1, (171.0, n - 1)),  # 末尾无 i+1
    )
    try:
        for mode, tick, dirn, is_open, i, want in cases:
            got = ec.fill_price(i, dirn, is_open, closes, opens, n, mode, tick)
            if abs(got[0] - want[0]) > 1e-9 or got[1] != want[1]:
                bad.append(f"fill_price({mode},dirn={dirn},open={is_open},i={i})"
                           f"={got} != {want}")
        observed.append(f"pure_fn_cases={len(cases)}")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"纯函数对拍异常：{type(exc).__name__}: {exc}")

    # ④ 接线：_simulate_trades 的合成 K 结果必须与纯函数一致，且源码确实委托
    try:
        dts = pd.date_range("2026-01-01", periods=n, freq="h")

        def frame(side: int):
            return pd.DataFrame({
                "dt": dts, "open": opens, "close": closes,
                "state": [0, side, side, side, 0, 0, 0, 0],
                "lots": [0, 1, 1, 1, 0, 0, 0, 0],
                "atr": [1.0] * n,
            })

        tr = fb._simulate_trades(
            frame(1),
            fb.FusionBacktestParams(fill_mode="next_open", tick_size=2.0,
                                    max_adverse_gap_atr=0.0),
            symbol=None)
        if len(tr) != 1:
            bad.append(f"接线对拍期望 1 笔，实得 {len(tr)}")
        else:
            t = tr[0]
            if (abs(t["entry_px"] - 122.0) > 1e-9
                    or abs(t["exit_px"] - 148.0) > 1e-9):
                bad.append(f"_simulate_trades 成交价 {t['entry_px']}/{t['exit_px']} "
                           f"!= 契约纯函数 122/148")
            else:
                observed.append("wired=122/148")
        src = (Path(__file__).resolve().parents[2] / "app" / "backtest"
               / "fusion_backtest.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        imported = any(
            isinstance(nd, ast.ImportFrom)
            and (nd.module or "") == "app.contracts.execution"
            for nd in ast.walk(tree))
        called = any(
            isinstance(nd, ast.Call) and isinstance(nd.func, ast.Name)
            and nd.func.id == "_contract_fill_price"
            for nd in ast.walk(tree))
        observed.append(f"import_contract={imported} delegate={called}")
        if not imported:
            bad.append("fusion_backtest 未从契约层 import（纯函数被绕过）")
        if not called:
            bad.append("_fill_price 未委托契约层纯函数（抽了但没人用）")
    except Exception as exc:                              # noqa: BLE001
        bad.append(f"接线检查异常：{type(exc).__name__}: {exc}")

    # ⑤ 白名单 / 延后条目归因
    for key in ("max_hold", "window", "entry_gates", "ops_risk"):
        if not ec.ExecutionContract.is_allowed(key):
            bad.append(f"白名单条目 {key} 缺理由或状态不对")
    unres = [f.key for f in ec.ExecutionContract.unresolved()]
    observed.append(f"unresolved={unres}")
    if unres != ["stop_loss"]:
        bad.append(f"延后条目应为 [stop_loss]，实得 {unres}（B1 归因丢失）")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad)
        + "。执行分叉声明已与当前行为脱节，E1 结论不可信（CL-20261108-14）")


def _c_ci_same_source_wired(c: Ctx) -> tuple[bool, str, str, str]:
    """P2-4 / E3 / CL-20261108-17：CI 同源断言必须真接线且**此刻真通过**。

    衡量「现在的行为」，不查历史流水、也不只查字符串：
      · 两个 E3 测试文件存在，且 AST 里确有关键用例（防文件被掏空）；
      · 根 ``conftest.py`` 确实把 ``scripts/`` 加进 ``sys.path``
        （``235711a`` 误删它导致 GitHub 侧 pytest 整轮 0 用例）；
      · ``compute_factor_v1v6`` 顶层 import 的 ``factor_ic_scan`` / ``pgconn``
        在磁盘上可解析（防「清理未使用脚本」再次打断收集阶段）；
      · **直接执行 pytest 跑这两个文件**：rc=0、passed≥20、零 skipped。

    ⚠ 容器 ``/app`` 没有 ``.github/``，``ci.yml`` 的接线由 CI 自身的显式
      E3 step 自检，并由 C1–C5 三方同 sha 保证 GitHub/生产/本地配置一致。
    """
    import ast
    import subprocess

    label = "CI 同源断言已接线且真实通过（E3 / P2-4）"
    root = Path(__file__).resolve().parents[2]
    bad: list[str] = []
    observed: list[str] = []
    tests = root / "tests"

    # ① 文件 + 关键用例（AST，防注释字符串假阳性）
    required = {
        "test_data_same_source.py": (
            "test_backtest_and_live_read_identical_bars",
            "test_source_drift_sentinel_fires",
            "test_both_chains_wired_to_single_entry",
        ),
        "test_signal_parity.py": (
            "test_same_params_hash_same_direction",
            "test_params_drift_is_detected",
            "test_execution_fork_contract_declared",
        ),
    }
    for name, funcs in required.items():
        p = tests / name
        if not p.is_file():
            bad.append(f"{name} 缺失")
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
            defined = {n.name for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef)}
            miss = [f for f in funcs if f not in defined]
            observed.append(f"{name}:{len(defined)}funcs")
            if miss:
                bad.append(f"{name} 关键用例缺失 {miss}")
        except Exception as exc:                              # noqa: BLE001
            bad.append(f"{name} 解析失败：{type(exc).__name__}: {exc}")

    # ② 根 conftest.py：scripts/ 必须在 sys.path 上
    cf = root / "conftest.py"
    if not cf.is_file():
        bad.append("根 conftest.py 缺失（235711a 误删事故复发）")
    else:
        try:
            src = cf.read_text(encoding="utf-8")
            has_insert = any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "insert"
                for n in ast.walk(ast.parse(src)))
            if not (has_insert and "scripts" in src):
                bad.append("conftest.py 未把 scripts/ 加进 sys.path")
            else:
                observed.append("conftest=sys.path+scripts")
        except Exception as exc:                              # noqa: BLE001
            bad.append(f"conftest.py 解析失败：{type(exc).__name__}: {exc}")

    # ③ 顶层 import 可达性：被误删一个就会让 pytest 收集整轮中断
    cf2 = root / "scripts" / "compute_factor_v1v6.py"
    if cf2.is_file():
        try:
            mods = {
                n.module for n in ast.walk(ast.parse(cf2.read_text(encoding="utf-8")))
                if isinstance(n, ast.ImportFrom)
                and n.module in ("factor_ic_scan", "pgconn")
            }
            for m in sorted(mods):
                ok = (root / "scripts" / f"{m}.py").is_file()
                observed.append(f"{m}.py={'ok' if ok else 'MISSING'}")
                if not ok:
                    bad.append(f"scripts/{m}.py 缺失 → pytest 收集会整轮中断")
            if not mods:
                bad.append("compute_factor_v1v6 顶层依赖清单为空，检查逻辑需复核")
        except Exception as exc:                              # noqa: BLE001
            bad.append(f"compute_factor_v1v6 解析失败：{type(exc).__name__}: {exc}")
    else:
        observed.append("compute_factor_v1v6=absent")

    # ④ 行为级：此刻真的执行 E3 两个测试文件
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest",
             str(tests / "test_data_same_source.py"),
             str(tests / "test_signal_parity.py"),
             "-q", "-rs", "-p", "no:cacheprovider"],
            cwd=str(root), capture_output=True, text=True, timeout=180)
        lines = [x for x in proc.stdout.strip().splitlines() if x.strip()]
        tail = lines[-1] if lines else "(no stdout)"
        observed.append(f"pytest_rc={proc.returncode} {tail[:90]}")
        if proc.returncode != 0:
            bad.append(f"E3 pytest rc={proc.returncode}：{tail[:160]}")
        m = re.search(r"(\d+) passed", proc.stdout)
        if not m or int(m.group(1)) < 20:
            bad.append("E3 passed 用例数 <20（疑似 0 用例假绿）")
        if re.search(r"\d+ skipped", proc.stdout):
            bad.append("E3 出现 skipped（CI/容器都不该跳过同源断言）")
    except Exception as exc:                                  # noqa: BLE001
        bad.append(f"E3 pytest 执行失败：{type(exc).__name__}: {exc}")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad)
        + "。CI 同源断言未真实生效，E3 结论不可信（CL-20261108-17）")


def _c_ops_data_lag_trade_day(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261109-01：日线滞后必须按**交易日**计，不得回退到自然日差。

    历史误报（2026-11-09 00:00）：「日线数据滞后 3 天（最新 2026-11-06）」，
    而 daily_bar 与交易日真源 futures_rule 的 max(trade_date) 同为 2026-11-06，
    数据零滞后 —— 纯粹是 (today - db_max).days 在周末被顶到 3。

    衡量此刻的行为（不查历史流水）：
      · ops_monitor.collect 的 AST 中存在以 ``DATA_LAG_TRADE_WARN`` 为比较对象的
        If 判据（证明交易日阈值真的接进了判定，而不只是定义了常量）；
      · 期望值来源为 ``futures_rule`` + ``expected_trade_date``（不用已死的
        ``l3_ref.trade_calendar``，见 CL-20261107-18）。
    """
    import ast

    label = "日线滞后按交易日计（CL-20261109-01）"
    root = Path(__file__).resolve().parents[2]
    mon = root / "scripts" / "ops" / "ops_monitor.py"
    bad: list[str] = []
    observed: list[str] = []
    if not mon.is_file():
        return False, label, "ops_monitor.py 缺失", "文件不存在"
    src = mon.read_text(encoding="utf-8")
    observed.append("futures_rule={0}".format("futures_rule" in src))
    if "futures_rule" not in src:
        bad.append("真源未使用 futures_rule（可能回退到已死的 l3_ref.trade_calendar）")
    if "expected_trade_date" not in src:
        bad.append("缺 expected_trade_date（未输出期望交易日，无法解释滞后口径）")
    try:
        tree = ast.parse(src)
        used = False
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                names = {x.id for x in ast.walk(node.test) if isinstance(x, ast.Name)}
                if "DATA_LAG_TRADE_WARN" in names:
                    used = True
                    break
        observed.append("判据引用 DATA_LAG_TRADE_WARN={0}".format(used))
        if not used:
            bad.append("交易日阈值未接进判定（仅定义常量不算生效）")
    except SyntaxError as e:
        bad.append("ops_monitor 语法错误: {0}".format(str(e)[:60]))
    return (not bad), label, " · ".join(observed), "；".join(bad)


def _c_ops_fresh_trade_day(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261109-02：新鲜度巡检必须按表节奏分类，不得回退「一律自然日」。

    历史误报：15 张表共用 ``(today - max).days > 3`` 的自然日判据，国庆/春节
    长假必然让全部表同时越线（2026-11-09 00:00 那条「日线滞后 3 天」是周末两天
    顶出来的，同源缺陷）。

    衡量此刻的行为（不查历史流水）：
      · ``freshness_tables`` 每张表都在 ``freshness_cadence`` 里有分类
        （漏分类会静默落到 natural → 长假立刻误报，必须显式）；
      · 分类取值 ⊆ {trade_day, session, natural}，且 trade_day 与 session 都有成员；
      · AST 中存在以 ``data_lag_trade_days`` 为比较对象的判定（阈值真接进判定）；
      · 交易日真源为 ``futures_rule``（不用已死的 l3_ref.trade_calendar，
        CL-20261107-18），且存在夜盘映射 ``_session_trade_day``。
    """
    import ast

    label = "新鲜度按交易日/时间戳分类（CL-20261109-02）"
    root = Path(__file__).resolve().parents[2]
    bad: list[str] = []
    observed: list[str] = []

    try:
        from app.ops.alerting import AlertConfig
        cfg = AlertConfig()
        tables = set(cfg.freshness_tables)
        cad = dict(cfg.freshness_cadence)
    except Exception as e:
        return False, label, "AlertConfig 不可导入", str(e)[:120]

    missing = sorted(tables - set(cad))
    extra = sorted(set(cad) - tables)
    kinds = sorted(set(cad.values()))
    observed.append("表={0} 已分类={1}".format(len(tables), len(tables) - len(missing)))
    observed.append("kinds={0}".format(",".join(kinds)))
    if missing:
        bad.append("未分类表（会静默落 natural，长假必误报）: {0}".format(",".join(missing)))
    if extra:
        bad.append("分类里有不存在的表: {0}".format(",".join(extra)))
    if not set(kinds) <= {"trade_day", "session", "natural"}:
        bad.append("非法节奏取值: {0}".format(",".join(kinds)))
    if "trade_day" not in kinds or "session" not in kinds:
        bad.append("trade_day/session 两类必须都有成员（否则退化为单一口径）")

    src_path = root / "app" / "ops" / "alerting.py"
    if not src_path.is_file():
        return False, label, " · ".join(observed), "alerting.py 缺失"
    src = src_path.read_text(encoding="utf-8")
    if "futures_rule" not in src:
        bad.append("交易日真源未使用 futures_rule（可能回退到已死的 l3_ref.trade_calendar）")
    if "_session_trade_day" not in src:
        bad.append("缺 _session_trade_day（夜盘 02:30 的桶会被算成新交易日）")
    try:
        tree = ast.parse(src)
        wired = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                # 可能是 data_lag_trade_days（Name）或 cfg.data_lag_trade_days（Attribute）
                names = {x.id for x in ast.walk(node) if isinstance(x, ast.Name)}
                attrs = {x.attr for x in ast.walk(node) if isinstance(x, ast.Attribute)}
                if "data_lag_trade_days" in names | attrs:
                    wired = True
                    break
        observed.append("判据引用 data_lag_trade_days={0}".format(wired))
        if not wired:
            bad.append("交易日阈值未接进判定（仅定义字段不算生效）")
    except SyntaxError as e:
        bad.append("alerting.py 语法错误: {0}".format(str(e)[:60]))
    return (not bad), label, " · ".join(observed), "；".join(bad)


def _c_ops_push_quota(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261108-23：运维告警不得挤爆与交易信号共用的推送通道。

    历史事故（2026-11-06）：6 个数据源同时滞后，``check_all`` 逐条发送 →
    单日 206 条 ``[alert]``，击穿 pushplus 免费额度（约 200 条/日），10:50 起
    返回 ``code=900 用户账号使用受限``；共用同一 token 的融合信号自 11:01 起
    7 条全部未送达，直到 11-08 19:30 才恢复。**通道被自家告警打死**。

    衡量此刻的行为（不查历史流水）：
      · ``AlertConfig.max_per_day`` 存在且 ∈ (0,150]（200 条的 75%，余量留给信号）；
      · ``check_all`` 内 ``send_alert`` 调用**恰好 1 处**（单轮 digest；
        恢复成逐条发送会立刻回到 288 条/日）；
      · ``ops_monitor`` 的静默判据引用了 ``market_live``（休市代理），
        否则每逢周末必报「无成功推送」——2026-11-08 那条 60 小时告警即此类误报。
    """
    import ast

    label = "告警日配额与单轮 digest 生效（CL-20261108-23）"
    root = Path(__file__).resolve().parents[2]
    bad: list[str] = []
    observed: list[str] = []

    # 1) 日配额存在且合理
    try:
        from app.ops.alerting import AlertConfig
        limit = int(getattr(AlertConfig(), "max_per_day", 0) or 0)
    except Exception as e:  # noqa: BLE001
        limit = -1
        bad.append("AlertConfig 导入失败: {0}".format(str(e)[:60]))
    observed.append("max_per_day={0}".format(limit))
    if not (0 < limit <= 150):
        bad.append("max_per_day 须 ∈ (0,150]，实测 {0}".format(limit))

    # 2) check_all 单轮只调一次 send_alert（digest）
    alerts_py = root / "app" / "ops" / "alerting.py"
    if not alerts_py.is_file():
        bad.append("app/ops/alerting.py 缺失")
    else:
        try:
            tree = ast.parse(alerts_py.read_text(encoding="utf-8"))
            n_send = 0
            found_fn = False
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "check_all":
                    found_fn = True
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call):
                            fn = sub.func
                            name = getattr(fn, "id", None) or getattr(fn, "attr", None)
                            if name == "send_alert":
                                n_send += 1
                    break
            observed.append("check_all.send_alert={0}".format(n_send))
            if not found_fn:
                bad.append("alerting.check_all 未找到")
            elif n_send != 1:
                bad.append("check_all 内 send_alert 应恰好 1 处（digest），实测 {0}".format(n_send))
        except SyntaxError as e:
            bad.append("alerting.py 语法错误: {0}".format(str(e)[:60]))

    # 3) ops_monitor 静默判据引用市场活跃代理
    mon = root / "scripts" / "ops" / "ops_monitor.py"
    if not mon.is_file():
        bad.append("scripts/ops/ops_monitor.py 缺失")
    else:
        try:
            mt = ast.parse(mon.read_text(encoding="utf-8"))
            has_proxy = any(
                isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
                and any(getattr(t, "id", "") == "MARKET_LIVE_H" for t in n.targets)
                for n in ast.walk(mt)
            )
            wired = any(
                isinstance(n, ast.If)
                and any(isinstance(x, ast.Name) and x.id == "market_live"
                        for x in ast.walk(n.test))
                for n in ast.walk(mt)
            )
            observed.append("MARKET_LIVE_H={0} 判据引用={1}".format(has_proxy, wired))
            if not has_proxy:
                bad.append("ops_monitor 缺 MARKET_LIVE_H 常量")
            if not wired:
                bad.append("ops_monitor 静默判据未引用 market_live（休市会误报）")
        except SyntaxError as e:
            bad.append("ops_monitor.py 语法错误: {0}".format(str(e)[:60]))

    return (not bad), label, " · ".join(observed), "；".join(bad)


def _c_ba_real_price_derived(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261108-25：日频派生必须回到真实价，不得在 BA 偏移空间做**比例**运算。

    为什么这条必须存在
    ------------------
    ``l2_adj.bar_60m_ba`` 是**等差**后复权（BA = 真实 + offset）。等差偏移的
    平移不变量只有**差值**（EMA / ATR / 枢轴 / 点数）；振幅率 ``(hh-ll)/c`` 与
    对数收益 ``log(c/c_prev)`` 都**不是**平移不变的。而全库有 23,940 根 BA 价为
    非正（FB888 35.2% / CJ888 33.2% / RU888 32.5%），于是：
      · ``amp`` 在 ``c < 0`` 时**符号翻转** → hvol 判据整体反向（实测 3,336 天）；
      · ``log`` 遇 0/负价 → ``-inf`` / NaN，污染簇冲击与日已实现波动。
    这些都不抛异常，只会让回测数字悄悄变形。故用合成序列把方向钉死。
    """
    label = "BA 空间的日频派生已回真实价（振幅不翻转 / 换月收益不冒充）"
    bad: list[str] = []
    observed: list[str] = []
    try:
        import numpy as np
        import pandas as pd

        from app.strategies.v5x.context import _cluster_shock, _daily_frame
    except Exception as exc:                                 # noqa: BLE001
        return False, label, f"import 失败 {type(exc).__name__}: {exc}", (
            "V5X 供数层不可导入 → BA 价格空间断言不可用（CL-20261108-25）")

    # 构造：真实价 100→98，偏移 -3300 让 BA 价为负；最后一根换月（偏移变化）
    try:
        real = np.array([100.0, 99.0, 98.0, 97.0])
        off = np.array([-3300.0, -3300.0, -3300.0, -3400.0])
        bars = pd.DataFrame({
            "symbol": ["FB888"] * 4,
            "bucket": pd.to_datetime(["2024-01-02 10:00", "2024-01-02 11:00",
                                      "2024-01-02 14:00", "2024-01-03 10:00"]),
            "open": real + off,
            "high": real + 1.0 + off,
            "low": real - 1.0 + off,
            "close": real + off,
            "volume": np.ones(4),
            "raw_c": real,
        })
        open_days = np.array([np.datetime64("2024-01-02"),
                              np.datetime64("2024-01-03")])
        d = _daily_frame(bars, open_days)
        d1 = d.iloc[0]
        d2 = d.iloc[1]

        observed.append(f"ba_min={float(bars['close'].min()):.0f}")
        observed.append(f"amp={float(d1['amp']):.6f} expect={4 / 98:.6f}")
        observed.append(f"close={float(d1['c']):.1f}")
        observed.append(f"roll_lret_nan={bool(pd.isna(d2['lret']))}")
        observed.append(f"neg_amp_days={int((d['amp'] < 0).sum())}")

        if not (bars["close"] <= 0).all():
            bad.append("合成数据未构造出负 BA 价，本断言失去意义")
        if abs(float(d1["amp"]) - 4.0 / 98.0) > 1e-9:
            bad.append(f"振幅未回真实价：{float(d1['amp']):.6f} != {4 / 98:.6f}"
                       "（在 BA 空间算 (hh-ll)/c 会符号翻转）")
        if float(d1["c"]) != 98.0:
            bad.append(f"日收盘不是真实价：{float(d1['c'])}")
        if int((d["amp"] < 0).sum()) != 0:
            bad.append("出现负振幅 → 振幅仍在 BA 空间计算")
        if not pd.isna(d2["lret"]):
            bad.append("换月日对数收益未置 NaN → 换月跳空会冒充真实收益")
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"合成检查异常 {type(exc).__name__}: {exc}")

    # 防止回退：_cluster_shock 不得再自己用 BA 收盘取 log
    try:
        fake = pd.DataFrame({"symbol": ["RB888"], "td": [1], "c": [-3200.0],
                             "amp": [0.01], "vol": [0.01]})
        try:
            _cluster_shock(fake, 0)
        except ValueError:
            observed.append("cluster_shock_requires_lret=ok")
        else:
            bad.append("_cluster_shock 在缺 lret 列时未报错 → 可能已回退为 BA 空间取 log")
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"簇冲击契约检查异常 {type(exc).__name__}: {exc}")

    return (not bad), label, " ".join(observed), (
        "" if not bad else "；".join(bad)
        + "。日频门控将读取失真的振幅/波动，历史回测数字不可信（CL-20261108-25）")


def _c_loguru_fmt_braces(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261108-24：loguru 只认 ``{}``，写成 ``%s`` 会原样打印占位符并丢参数。

    关键前提：**只检查 loguru 的调用方**。本项目同时存在标准库 ``logging``
    （``logger = logging.getLogger(__name__)``），那里 ``%s`` 是正确写法 ——
    盲改会把对的改坏（首版台账把 23 处标准库用法误计为缺陷，已订正）。

    ★ 2026-11-08 修守卫本身的漏检（CL-20261108-26 收尾时发现）：
      旧实现逐行跑正则，**跨行拼接的调用全部漏掉**——``logger.warning(\\n
      "第一段"\\n "（候选=%s）..."\\n, keys)`` 在这类写法下匹配不到，于是
      上一轮「已修复 4 处」其实还剩 6 处没修，而守卫一直 PASS。
      现改用 **AST**：隐式字符串拼接在 AST 里已合并成一个 Constant，跨行天然覆盖；
      并且只在**存在额外位置参数**时才判为「丢值」（单条消息里的 ``%`` 只是文本）。
    """
    import ast

    label = "loguru 调用不得使用 % 占位符（标准库 logging 不在此列）"
    bad: list[str] = []
    observed: list[str] = []
    _METHODS = ("info", "warning", "error", "debug", "exception", "critical", "trace")
    try:
        import re as _re

        root = Path(__file__).resolve().parents[2]
        pct = _re.compile(r"%[-+ 0#]*\d*(?:\.\d+)?[sdfr]")
        marks = ("from app.core.logging import", "from loguru import")
        n_loguru = n_std = 0
        for p in list((root / "app").rglob("*.py")) + list((root / "scripts").rglob("*.py")):
            try:
                txt = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            is_loguru = any(m in txt for m in marks) or \
                p.as_posix().endswith("app/core/logging.py")
            try:
                tree = ast.parse(txt)
            except SyntaxError:
                continue
            rel = p.relative_to(root).as_posix()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not (isinstance(fn, ast.Attribute) and fn.attr in _METHODS):
                    continue
                if not (isinstance(fn.value, ast.Name) and fn.value.id == "logger"):
                    continue
                if not node.args:
                    continue
                first = node.args[0]
                if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                    continue
                fmt = first.value
                if not pct.search(fmt) or "%(" in fmt:
                    continue
                if is_loguru:
                    # 只有「既有 % 占位符、又传了额外参数」才会真的丢值
                    if len(node.args) > 1:
                        n_loguru += 1
                        bad.append(f"{rel}:{node.lineno} 用 % 占位符并丢了 "
                                   f"{len(node.args) - 1} 个参数")
                else:
                    n_std += 1
        observed.append(f"loguru_bad={n_loguru}")
        observed.append(f"stdlib_ok={n_std}")
        if n_loguru:
            bad.insert(0, f"loguru 误用 {n_loguru} 处")
    except Exception as exc:                                 # noqa: BLE001
        bad.append(f"扫描异常 {type(exc).__name__}: {exc}")
    return (not bad), label, " ".join(observed), (
        "" if not bad else "；".join(bad[:6])
        + "。日志会原样打印 %s 并丢掉变量值，事故排查时最需要的信息恰好丢失")


def _c_ops_push_pnl_wired(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261108-18：推送盈亏金额必须真实接线，且契约测试不得再被 skip。

    历史事故：``68bfe82`` 把功能整体下线却留下契约测试，CI 常年 3 errors；
    P2-4 曾只能整组 ``pytest.mark.skip``。2026-11-08 用户拍板恢复后，
    用「AST 真实引用 + 直接执行 pytest」钉住，防止再次静默退化。

    衡量此刻的行为，不查历史流水：
      · scheduler 定义了 _multiplier / _pnl_yuan / _fmt_money 三个符号；
      · _pnl_yuan 在源码中被**真实调用** ≥2 处（防「定义了没人用」）；
      · 契约测试文件不含 pytest.mark.skip（防僵尸化）；
      · 直接执行该测试文件：rc=0、3 passed、零 skipped。
    """
    import ast
    import subprocess

    label = "推送盈亏金额已接线且契约测试真实通过（CL-20261108-18）"
    root = Path(__file__).resolve().parents[2]
    bad: list[str] = []
    observed: list[str] = []

    sched = root / "app" / "scheduler.py"
    if not sched.is_file():
        bad.append("app/scheduler.py 缺失")
    else:
        try:
            src = sched.read_text(encoding="utf-8")
            tree = ast.parse(src)
            defined = {n.name for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef)}
            need = ("_multiplier", "_pnl_yuan", "_fmt_money")
            miss = [n for n in need if n not in defined]
            observed.append("symbols=" + ("ok" if not miss else f"missing{miss}"))
            if miss:
                bad.append(f"推送盈亏符号缺失 {miss}")
            calls = sum(1 for n in ast.walk(tree)
                        if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Name)
                        and n.func.id == "_pnl_yuan")
            observed.append(f"_pnl_yuan_calls={calls}")
            if calls < 2:
                bad.append(f"_pnl_yuan 真实调用仅 {calls} 处（应覆盖持仓行与信号两处）")
        except Exception as exc:                              # noqa: BLE001
            bad.append(f"scheduler.py 解析失败：{type(exc).__name__}: {exc}")

    t = root / "tests" / "test_fusion_push_pnl.py"
    if not t.is_file():
        bad.append("tests/test_fusion_push_pnl.py 缺失")
    else:
        tsrc = t.read_text(encoding="utf-8")
        if "pytest.mark.skip" in tsrc or "pytestmark" in tsrc:
            bad.append("契约测试又被整组 skip（僵尸测试复发）")
        else:
            observed.append("test_skip=none")

    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", str(t), "-q", "-rs",
             "-p", "no:cacheprovider"],
            cwd=str(root), capture_output=True, text=True, timeout=120)
        lines = [x for x in proc.stdout.strip().splitlines() if x.strip()]
        tail = lines[-1] if lines else "(no stdout)"
        observed.append(f"pytest_rc={proc.returncode} {tail[:90]}")
        if proc.returncode != 0:
            bad.append(f"契约测试 rc={proc.returncode}：{tail[:160]}")
        m = re.search(r"(\d+) passed", proc.stdout)
        if not m or int(m.group(1)) < 3:
            bad.append("契约测试 passed <3（疑似被掏空或 0 用例假绿）")
        if re.search(r"\d+ skipped", proc.stdout):
            bad.append("契约测试出现 skipped")
    except Exception as exc:                                  # noqa: BLE001
        bad.append(f"契约测试执行失败：{type(exc).__name__}: {exc}")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad)
        + "。推送盈亏金额接线不完整或契约被跳过（CL-20261108-18）")


def _c_position_sync_single_entry(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261108-26：写实盘持仓只允许有一条路。

    2026-11-08 事故形状：``upsert_position`` 有两个同名实现（一个写
    ``execution_position``、一个写 ``fusion_position``），``apply_fill`` 零调用方，
    ``bridge_report`` 的注释却声称持仓已被维护。**没有一处报错**，因为「没接线」
    本身不会抛异常 —— 只有断言能发现。

    断言（此刻可判，不查历史流水）：
      · 写 ``execution_position`` 的调用（``persistence.upsert_position(``，或
        文件内已 import ``app.execution.persistence`` 后的裸调用）只能出现在
        ``app/execution/position_manager.py`` 与 ``app/execution/position_sync.py``；
      · ``apply_fill(`` 的调用点只能出现在 ``position_sync.py``；
      · 两者都不许出现在 ``app/api/`` / ``app/scheduler.py`` / ``app/strategies/``
        —— 接口层和策略层直接写持仓正是要收口掉的东西。

    ⚠ 必须区分两个同名函数（这是本断言最容易写错的地方）：
      ``app.execution.persistence.upsert_position`` 写 **execution_position**（实盘持仓），
      ``app.strategies.fusion_signal.upsert_position`` 写 **fusion_position**（V3.4
      策略意图台账）。后者是策略内部状态机，不属本次收口范围 —— 首版断言没区分，
      把 scheduler 里两处 fusion 台账写入误判成违规。
    """
    label = "写实盘持仓只有 position_sync 一个业务入口（CL-20261108-26）"
    root = Path(__file__).resolve().parents[2]
    bad: list[str] = []
    observed: list[str] = []

    allow_upsert = {"app/execution/position_manager.py",
                    "app/execution/position_sync.py"}
    allow_apply = {"app/execution/position_sync.py"}
    hits_upsert: list[str] = []
    hits_apply: list[str] = []

    for p in list((root / "app").rglob("*.py")):
        rel = p.relative_to(root).as_posix()
        txt = p.read_text(encoding="utf-8", errors="ignore")
        # 该文件是否把 execution 层持仓写函数带进了命名空间
        touches_exec_pos = (
            "from app.execution import persistence" in txt
            or "from app.execution.persistence import" in txt
            or "app.execution.position_manager" in txt
        )
        for i, line in enumerate(txt.splitlines(), 1):
            s = line.strip()
            if s.startswith("def ") or s.startswith("#"):
                continue
            if "upsert_position(" in s and "def upsert_position" not in s:
                is_exec_pos = ("persistence.upsert_position(" in s
                               or "position_manager.upsert_position(" in s
                               or touches_exec_pos)
                if not is_exec_pos:
                    continue                     # fusion_signal 的台账写入，不在此列
                hits_upsert.append(f"{rel}:{i}")
                if rel not in allow_upsert:
                    bad.append(f"{rel}:{i} 直接写 execution_position（应经 position_sync）")
            if "apply_fill(" in s and "def apply_fill" not in s:
                hits_apply.append(f"{rel}:{i}")
                if rel not in allow_apply:
                    bad.append(f"{rel}:{i} 绕过 position_sync 直接调 apply_fill")

    observed.append(f"upsert_calls={len(hits_upsert)} apply_fill_calls={len(hits_apply)}")
    if not hits_apply:
        bad.append("position_sync 里没有 apply_fill 调用 —— 收口了但没人用，等于没做")
    if (root / "app/execution/position_sync.py").is_file():
        observed.append("module=ok")
    else:
        bad.append("app/execution/position_sync.py 缺失（收口模块被删）")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad[:6])
        + "。多入口写同一张持仓表，迟早出现「两个数都自称是真的」")


def _c_position_sync_switch_declared(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261108-26/27：两个新开关必须**在表里存在**，不能只靠代码默认值。

    区分两件事：①「故意关着」= 表里 enabled=false，是设计；②「行都不存在」=
    迁移没跑，此时 :func:`app.core.feature_switch.is_enabled` 回退默认值 false，
    **行为看起来一模一样**，但没人知道到底是哪一种。所以要断言行必须在。
    """
    label = "持仓同步/策略单源两个开关已在 cfg_feature_switch 登记"
    need = ("position_sync_enabled", "fusion_v34_live_enabled")
    rows = c.rows(
        "SELECT switch_key, enabled FROM cfg_feature_switch "
        "WHERE switch_key = ANY(:ks)", ks=list(need))
    got = {r[0]: bool(r[1]) for r in rows}
    miss = [k for k in need if k not in got]
    observed = " ".join(f"{k}={'on' if v else 'off'}" for k, v in sorted(got.items())) \
        or "(none)"
    bad = [f"开关缺失 {miss}（migrations/060 未应用？）"] if miss else []
    return not bad, label, observed, "" if not bad else "；".join(bad)


def _c_strategy_single_push_source(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261108-27：同一时刻只能有一个生产推送源。

    V3.4 与 V5X.3 的持仓假设互不相容（前者维护 ``fusion_position`` 意图台账，
    后者无状态回放、不认任何台账）。两个开关同时开 = 微信里会同时收到
    「开多 RB」和「平空 RB」，而且两边都自认为是对的。这比只推一个更糟。
    """
    label = "V3.4 与 V5X.3 不得同时作为生产推送源"
    rows = c.rows(
        "SELECT switch_key, enabled FROM cfg_feature_switch "
        "WHERE switch_key IN ('fusion_v34_live_enabled', 'v5x_live_enabled')")
    got = {r[0]: bool(r[1]) for r in rows}
    v34 = got.get("fusion_v34_live_enabled", False)
    v5x = got.get("v5x_live_enabled", False)
    observed = f"v34={'on' if v34 else 'off'} v5x={'on' if v5x else 'off'}"
    ok = not (v34 and v5x)
    detail = "" if ok else (
        "两个开关同时为 true：V3.4 与 V5X.3 会各推一套互相矛盾的开平信号，"
        "且 V3.4 的 fusion_position 台账与 V5X 的无状态回放无法互相校验")
    return ok, label, observed, detail


def _c_v5x_replay_seeded(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261108-28：V5 的统计起点必须真的落在库里，而不是只写在文档里。

    用户拍板起点 = 2026-11-05 夜盘。回放结果以 ``source='replay'`` 落
    ``v5x_signal_log``。若这列/这些行不存在，「V5 从哪天开始算」就只是记忆，
    下次统计仍然要从零解释一遍。
    """
    label = "V5X 回放补录已落库（source 列 + replay 行）"
    col = c.scalar(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name='v5x_signal_log' "
        "AND column_name='source'")
    if not col:
        return False, label, "source列缺失", \
            "v5x_signal_log 无 source 列（migrations/060 未应用），回放与实盘不可区分"
    n = c.scalar("SELECT count(*) FROM public.v5x_signal_log WHERE source='replay'") or 0
    first = c.scalar("SELECT min(bar_ts) FROM public.v5x_signal_log WHERE source='replay'")
    observed = f"replay={int(n)} first={first}"
    if not n:
        return False, label, observed, \
            "尚无 source='replay' 的行（回放未执行），V5 的统计起点未落地"
    return True, label, observed, ""


def _c_roll_finer_anchor_wired(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261108-06：min5 锚点必须取「15m 桶内第一根」，且 SQL/Python 双真源一致。

    为什么这条必须存在
    ------------------
    换月映射有两份实现：生产走 ``sp_build_l2_roll_segment``（SQL），
    人工/研究走 ``scripts/build_roll_segments.py::map_roll_to_freq``（Python）。
    历史上两份各写各的，044/055/056 三次都是「改了一边忘另一边」。
    更糟的是 063 首次实施时**从 055 的文本** CREATE OR REPLACE，把 056 的
    ``%4$L::boolean`` 常数折叠一起覆盖掉——性能从秒级退化到 166s/品种，
    且把 min5 全量重建重新推回 OOM 路径。这种事必须有断言盯。

    判据（都是「此刻的行为」）：
      1. pg_proc 里过程体同时含 ① 新分支 ``interval '15 minutes'``/``%5$L``
         和 ② 056 的短路 ``%4$L::boolean``（缺任何一个都是回归）；
      2. Python 侧 ``map_roll_to_freq`` 含 finer 分支（``freq_min < ANCHOR_MIN``）。
    """
    label = "min5 锚点取桶内第一根（SQL 含 056 短路 + Python 双真源一致）"
    bad: list[str] = []
    observed: list[str] = []

    src = c.scalar(
        "SELECT prosrc FROM pg_proc WHERE proname = 'sp_build_l2_roll_segment'") or ""
    sql_finer = "interval '15 minutes'" in src and "%5$L" in src
    sql_short = "%4$L::boolean" in src
    observed.append(f"sql_finer={sql_finer} sql_shortcircuit={sql_short}")
    if not sql_finer:
        bad.append("存储过程缺 CL-20261108-06 的「桶内第一根」分支")
    if not sql_short:
        bad.append("存储过程缺 056 的 shift 常数折叠（%4$L::boolean）→ 性能/内存回归")

    try:
        from pathlib import Path
        py = (Path(__file__).resolve().parents[2] / "scripts"
              / "build_roll_segments.py").read_text(encoding="utf-8")
        py_finer = "freq_min < ANCHOR_MIN" in py
    except Exception as exc:                              # noqa: BLE001
        py_finer = False
        bad.append(f"读 build_roll_segments.py 失败：{type(exc).__name__}")
    observed.append(f"py_finer={py_finer}")
    if not py_finer:
        bad.append("Python 侧 map_roll_to_freq 缺 finer 分支（双真源已分叉）")

    return not bad, label, " ".join(observed), (
        "" if not bad else "；".join(bad) + " → 见 migrations/063（CL-20261108-06）")


def _c_roll_min5_no_fake_jump(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261108-06：5m 后复权序列的换月 delta 必须**精确**落在段首那根 bar 上。

    为什么从「阈值启发式」换成「精确恒等式」（2026-11-09 修正）：

    原判据是「``|ba_cc|>50`` 且 ``raw_cc < 10% * ba_cc``」——拿 close-to-close 比。
    它把 **116 根**旧映射（取桶末根）的假跳空抓了出来，但留下 2 根疑似：
    OI888@2012-11-02 09:50（ba 10052−10110=−58，raw 10024−10020=+4，delta=62）、
    SF888@2016-09-28 14:40（−154 / +6 / 160）。查下去发现**它们都是对的**：
    换月缺口落在段首那根 bar 的**开盘**里（open 从 4640 跳到 4800），close 只动 +6，
    于是 close-to-close 看着像大跳，实际上 delta 正好在段首兑现 —— 这是 063 的目标形态。
    阈值法无法区分「delta 在该兑现的 bar 兑现」与「delta 晚一根兑现」，只会误伤。

    新判据（精确、无阈值、无尺度依赖）：
        对每个 min5 段边界 T：
        ``(ba.close(T) - ba.close(T-)) - (raw.close(T) - raw.close(T-)) == -roll_delta``
    后复权 = raw + offset，且 offset 走 ``seg_start <= bucket`` 的 as-of join ——
    **只要 ba 是按当前段表重建的，delta 就必然正好在 T 兑现**，上式恒等成立。
    所以它真正衡量的是「ba 与当前 roll_segment 是否自洽」，抓的是
    **段表已改 / ba 没重建**（价格序列整体错位但 adj_offset 看着没事，正是
    CL-20261109-12 的静默形态），以及「offset 被算到别的 bar 上」。
    旧判据只在 |Δ|>50 时才看得见，且分不清「合法的大缺口」与「错位」。

    ⚠ 它**不**能替代 ROLL-FINER-ANCHOR-WIRED：段若被记错到桶末根，delta 照样
    在「错误的 T」上自洽，本断言看不出来——两者是互补的，缺一不可。

    ⚠ 只统计**已存在**的边界。边界整条丢失（上游门控把换月事件丢了）时这根 bar
    根本不在样本里，本断言看不见——那是 CL-20261108-05 的另一半（min5 与 min15
    段数差），根因是 ``idx_delta`` 门控依赖的 8888 指数在早年缺失，重跑只会
    得到更少事件，需要单独拍板，暂不并入这条断言。
    """
    label = "5m 换月 delta 精确落在段首（无假跳空）"
    if not c.whitelist:
        return False, label, "白名单为空", "l3_ref.tradable_variety 无有效品种"
    syms = sorted({w if w.endswith("888") else w + "888" for w in c.whitelist})
    sql = """
        SELECT bd.symbol,
               count(*) AS n,
               count(*) FILTER (
                 WHERE abs((b.close - p.close) - (r.close - q.close)
                           + bd.roll_delta) > 1e-6) AS bad,
               max(abs((b.close - p.close) - (r.close - q.close)
                       + bd.roll_delta))::float AS mx,
               count(*) FILTER (WHERE abs(b.close - p.close) > 50) AS big
          FROM (SELECT symbol, seg_start, roll_delta FROM l2_adj.roll_segment
                 WHERE freq = 'min5' AND symbol = ANY(:syms)) bd
          JOIN l2_adj.bar_5m_ba b ON b.symbol = bd.symbol AND b.bucket = bd.seg_start
          JOIN l1_mkt.bar_5m   r ON r.symbol = bd.symbol AND r.bucket = bd.seg_start
          JOIN LATERAL (SELECT close FROM l2_adj.bar_5m_ba x
                         WHERE x.symbol = bd.symbol AND x.bucket < bd.seg_start
                         ORDER BY x.bucket DESC LIMIT 1) p ON true
          JOIN LATERAL (SELECT close FROM l1_mkt.bar_5m y
                         WHERE y.symbol = bd.symbol AND y.bucket < bd.seg_start
                         ORDER BY y.bucket DESC LIMIT 1) q ON true
         GROUP BY bd.symbol
    """
    try:
        # 64MB /dev/shm 装不下并行 worker 段（实测 DiskFull），关并行再跑
        c.session.execute(text("SET LOCAL max_parallel_workers_per_gather = 0"))
        rows = c.rows(sql, syms=syms)
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"{type(exc).__name__}: {str(exc).splitlines()[0][:90]}", (
            "查询失败 → 无法判定（不要当成通过）")
    tot = sum(int(r[2]) for r in rows)
    mx = max((float(r[3] or 0) for r in rows), default=0.0)
    n_bd = sum(int(r[1]) for r in rows)
    n_big = sum(int(r[4]) for r in rows)
    worst = [f"{r[0]}={int(r[2])}" for r in rows if int(r[2])]
    return tot == 0, label, (
        f"边界 {n_bd} 根 / 残差违约 {tot} 根 / 最大 {mx:.3f}"
        f"（|Δba|>50 的段首 {n_big} 根，均已由 roll_delta 解释）"
        + (f"（{','.join(worst)}）" if worst else "")
    ), ("" if tot == 0 else
        "delta 没在段首 bar 兑现（晚一根或记在桶末根）→ 见 migrations/063 + "
        "CL-20261108-06，需重算该品种 roll_segment 并重建 ba")


def _c_roll_freq_parity(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261108-05：四个频率的换月段必须由**同一组事件**派生。

    换月事件只在 15m 锚点上检测一次（``roll_events``），再映射到各目标频率；
    映射只能把落进同一根 bar 的多个事件**合并**（``sum(gap)``），不能凭空增删。
    因此两条恒等式必须同时成立：

    1. **``Σ roll_delta`` 跨频相等**。合并是求和运算，所以总量守恒；某一频偏小 =
       事件在映射中被静默丢弃（锚点落在目标表覆盖之外 / 门控多杀一行），偏大 =
       同一事件被重复计入。这是唯一能抓住"丢事件"的廉价聚合。
       ⚠ 注意不能用 ``Σ|roll_delta|``：相邻两笔符号相反的事件合并后会被抵消，
       绝对值之和必然变小，那是合法行为（38 品种里 5 个就是这么不同的）。
    2. **段数单调**：``min5 / min30 / min60 ≤ min15``。分辨率只会合并、不可能
       拆出更多段；违反说明某个频率混进了不属于当前口径的旧段。

    2026-11-09 四频统一重算后实测：38 品种两条全部成立。BU/FU/OI/SF/SM 段数在
    四频间不同（如 FU 138/144/142/139）是**合法的分辨率合并**，Σ 总量仍然相等。
    """
    label = "四频换月段同源（Σroll_delta 跨频相等 + 段数单调）"
    if not c.whitelist:
        return False, label, "白名单为空", "l3_ref.tradable_variety 无有效品种"
    syms = sorted({w if w.endswith("888") else w + "888" for w in c.whitelist})
    sql = """
        SELECT symbol,
               coalesce(sum(roll_delta) FILTER (WHERE freq = 'min5'),  0),
               coalesce(sum(roll_delta) FILTER (WHERE freq = 'min15'), 0),
               coalesce(sum(roll_delta) FILTER (WHERE freq = 'min30'), 0),
               coalesce(sum(roll_delta) FILTER (WHERE freq = 'min60'), 0),
               count(*) FILTER (WHERE freq = 'min5'),
               count(*) FILTER (WHERE freq = 'min15'),
               count(*) FILTER (WHERE freq = 'min30'),
               count(*) FILTER (WHERE freq = 'min60')
          FROM l2_adj.roll_segment
         WHERE symbol = ANY(:syms)
         GROUP BY symbol
         ORDER BY symbol
    """
    try:
        rows = c.rows(sql, syms=syms)
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"{type(exc).__name__}: {str(exc).splitlines()[0][:90]}", (
            "查询失败 → 无法判定（不要当成通过）")
    if not rows:
        return False, label, "无段表数据", "l2_adj.roll_segment 为空"

    bad_sum, bad_mono = [], []
    for r in rows:
        vals = [float(x or 0) for x in r[1:5]]
        if max(vals) - min(vals) > 1e-6:
            bad_sum.append(f"{r[0]}({'/'.join(f'{v:.1f}' for v in vals)})")
        n5, n15, n30, n60 = (int(r[5]), int(r[6]), int(r[7]), int(r[8]))
        if n15 and (n5 > n15 or n30 > n15 or n60 > n15):
            bad_mono.append(f"{r[0]}({n5}/{n15}/{n30}/{n60})")

    ok = not bad_sum and not bad_mono
    detail = (f"{len(rows)} 品种 / 总量不符 {len(bad_sum)} / 段数单调违反 {len(bad_mono)}"
              + (f"；总量异常：{','.join(bad_sum[:4])}" if bad_sum else "")
              + (f"；段数异常：{','.join(bad_mono[:4])}" if bad_mono else ""))
    return ok, label, detail, ("" if ok else
                               "某频率的事件被丢/重复计入，或混入旧口径段 → "
                               "跑 scripts/ops/rebuild_roll_all_freqs.py 统一重算")


def _c_ba_price_identity(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261109-12：后复权行必须满足 ``ba.close - adj_offset == raw.close``。

    为什么这条不平凡：``sp_build_l2_back_adjusted`` 的**增量**模式只补
    ``bucket > max_bucket`` 的新桶。l1 侧历史一旦被修订（补数、888 连重算、
    口径回补），旧行会静默保留**错误的 close**，而 ``adj_offset`` 仍是对的——
    于是价格序列整体错位几十到几百点，整条链路不报任何错。

    2026-11-09 实测（252 交易日 A/B 对拍时发现）：**37/38 品种**在
    ``2026-10-21 09:15 ~ 2026-10-28 01:00`` 区间违约，60m 最大偏差 215 点
    （BR888）。修法是全量重建（``--force-ba``），不是改口径。

    窗口取 45 天：够抓"上个月刚被静默改坏"的情况，又不至于每天扫 12 年数据。
    """
    label = "后复权行与原始价自洽（ba.close - adj_offset == raw.close）"
    if not c.whitelist:
        return False, label, "白名单为空", "l3_ref.tradable_variety 无有效品种"
    syms = sorted({w if w.endswith("888") else w + "888" for w in c.whitelist})
    parts = []
    for freq, ba, raw in (("min5", "bar_5m_ba", "bar_5m"),
                          ("min15", "bar_15m_ba", "bar_15m"),
                          ("min30", "bar_30m_ba", "bar_30m"),
                          ("min60", "bar_60m_ba", "bar_60m")):
        parts.append(f"""
            SELECT '{freq}' AS freq,
                   count(*) FILTER (
                     WHERE abs(r.close - (a.close - a.adj_offset)) > 1e-6) AS n_bad,
                   max(abs(r.close - (a.close - a.adj_offset)))::float AS mx
              FROM l2_adj.{ba} a
              JOIN l1_mkt.{raw} r ON r.symbol = a.symbol AND r.bucket = a.bucket
             WHERE a.symbol = ANY(:syms)
               AND a.bucket >= now() - interval '45 days'
               -- 排除当日：盘中/收盘前生成的桶本身会随 l1 推进（半成品），
               -- 那是正常状态，不该在日常巡检里报红。历史桶必须自洽。
               AND a.bucket < date_trunc('day', now())""")
    sql = " UNION ALL ".join(parts)
    try:
        rows = c.rows(sql, syms=syms)
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"{type(exc).__name__}: {str(exc).splitlines()[0][:90]}", (
            "查询失败 → 无法判定（不要当成通过）")
    tot = sum(int(r[1]) for r in rows)
    worst = max((float(r[2] or 0) for r in rows), default=0.0)
    per = ", ".join(f"{r[0]}={int(r[1])}" for r in rows if int(r[1]))
    return tot == 0, label, (
        f"45 天内违约定价 {tot} 根 / 最大偏差 {worst:.1f} 点"
        + (f"（{per}）" if per else "")
    ), ("" if tot == 0 else
        "l1 历史被修订而后复权只增量补新桶 → 跑 "
        "rebuild_roll_all_freqs.py --force-ba 全量重建")


#: CL-20261109-13：关键存储过程 → {必须存在的 token: 引入它的迁移}。
#:
#: 治的是**「拿旧基线迁移覆盖新迁移」**这一类事故。plpgsql 的
#: `CREATE OR REPLACE PROCEDURE` 是全量替换：想改一行，就得把整段过程体重新写进去，
#: 于是「从哪个迁移拷基线」变成了一个静默的、没人检查的选择。
#:
#: 2026-11-09 **同一天犯两次**：
#:   ① `063` 基于 055 而非 056 重建 roll_segment → 056 的 positivity 短路消失
#:      （p_positivity=false 也会全表扫 bar_5m，恢复 min5 OOM 的触发条件）；
#:   ② `065` 基于 040 而非 053 重建 back_adjusted → 053 的 `tradable_scope_array`
#:      口径 + 042 的四维签名一起消失（非白名单品种重新被派生，VAR-SCOPE-WIRED 判红）。
#:
#: 共同点：每次只消失**一个** token，而原有断言各自只盯自己那一个——
#: ROLL-FINER-ANCHOR-WIRED 盯 063、VAR-SCOPE-WIRED 盯 053，恰好都不覆盖「被覆盖的那一方」。
#: 所以这条断言不按「结论」而是按「迁移来源」组织：一次读取过程体，逐 token 追责到迁移号，
#: 缺哪个就直接报「哪个迁移被覆盖」。口径是「当前生效的定义」，不依赖任何历史流水。
MIG_PROC_BASELINE: dict[str, dict[str, str]] = {
    "l2_adj.sp_build_l2_back_adjusted(text,text,boolean)": {
        "tradable_scope_array": "053-S3 品种口径（白名单∪持仓豁免）",
        "cum_offset_baseline": "042 四维触发签名 Σcum_offset",
        "nseg_baseline": "042 四维触发签名 段数",
        "b.bucket >= %L::timestamptz": "065/066 增量下界刷新最后一个桶",
    },
    "public.sp_build_l2_roll_segment(text,boolean,text)": {
        "least(t.bucket +": "055 跨休市上界 clamp",
        "SELECT t.next_bucket AS bar_ts": "055 空隙归 next_bucket 分支",
        "fs.n_bars": "055 补 n_bars 列",
        "AND %4$L::boolean": "056 positivity 常数短路",
        "LEFT JOIN full_segs s": "056 区间 JOIN 替代相关子查询",
    },
    "public.sp_build_l1_from_minute(text,timestamptz,timestamptz)": {
        # 2026-11-09 CL-20261109-14 / migrations/067：本过程已**弃用**（调用即 RAISE），
        # 库层不再有第二个 L1 生产者。原来的两个 token（tradable_scope_array /
        # amount=EXCLUDED.amount）随过程体一起作废，改为只认「弃用态标记」——
        # 若有人拿 053/009 的旧过程体覆盖回来，token 消失，本条立刻报「067 被回退」。
        "SESSION_GRID_RETIRED": "067 弃用旧 time_bucket 网格派生",
    },
}


def _c_mig_proc_baseline(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261109-13：关键过程的多迁移 token 必须同时存在（防旧基线覆盖新迁移）。"""
    label = "关键过程未被旧基线覆盖（迁移 token 集合完整）"
    dead: list[str] = []
    checked = 0
    for sig, tokens in MIG_PROC_BASELINE.items():
        try:
            ddl = c.scalar(
                "SELECT pg_get_functiondef(CAST(:sig AS regprocedure))", sig=sig) or ""
        except Exception:                                 # noqa: BLE001
            dead.append(f"{sig.split('(')[0]} 不存在")
            continue
        checked += 1
        miss = [f"{src}" for tok, src in tokens.items() if tok not in ddl]
        if miss:
            short = sig.split("(")[0].split(".")[-1]
            dead.append(f"{short} 缺[{', '.join(miss)}]")
    return not dead, label, (
        f"校验 {checked}/{len(MIG_PROC_BASELINE)} 个过程，缺失 {len(dead)} 处"
        + (f"：{'；'.join(dead)}" if dead else "")
    ), ("" if not dead else
        "有人用旧迁移作基线 CREATE OR REPLACE 了过程体 → 上面列出的迁移改动已被静默回退。"
        "修法：以**最新**一份重定义该过程的迁移为基线重写（不是最早那份），"
        "并核对本条断言全绿（CL-20261109-13 / 同型前例 20261107-16、20261109-12）")


def _c_l1_session_grid(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261109-14：白名单品种 2015 起的 L1 bucket 必须全部落在**会话网格**上。

    背景：库内 2004-12-22~2026-10-20 共 5,742 个交易日是旧网格
    （``time_bucket`` 左沿：60m = 09:00/10:00/11:00/13:00/14:00），
    2026-10-21~10-28 新旧两套并存（一天 15 根 30m），只有 10-29 之后才是会话网格。
    旧网格「内部自洽」——``30m 09:00 = 15m 09:00 + 09:15`` 同样精确成立 ——
    所以 ``check_cross_tf_consistency`` 只在**两套并存**时才报红，纯旧网格日抓不到。
    要把「口径已统一」变成可验证的事实，只能直接检 bucket 的墙钟时刻。

    判据：按频率列出**只可能出现在旧网格**的墙钟标签，命中即判红。
    不用「必须等于白名单标签集」，因为夜盘收盘时刻随品种而变（23:00/01:00/02:30），
    白名单会把合法的截断尾段也算成违规。

    | 频率 | 旧网格独有标签（日盘 + 夜盘起点） |
    |---|---|
    | 60m | 09:00 11:00 13:00 14:00 21:00 |
    | 30m | 09:00 10:30 11:00 11:30 13:30 14:00 21:00 |
    | 15m | 09:00 10:30 13:30 21:00 |
    | 5m  | 09:00 10:20 10:25 10:30 13:30 21:00 |

    ⚠ 只查 2015-01-01 起：那是本轮重建的边界，2015 年前**有意**保留旧网格
    （CL-20261109-14 决策 A/B/C 中选了 B：只统一回测窗口）。
    ⚠ 只查白名单 38 品种（×888 与 ×8888）：非白名单品种不在重建范围内。
    """
    label = "白名单 2015+ L1 bucket 全落在会话网格（无旧网格残留）"
    if not c.whitelist:
        return False, label, "白名单为空", "l3_ref.tradable_variety 无有效品种"
    base = sorted({w[:-3] if w.endswith("888") else w for w in c.whitelist})
    syms = sorted({v + suf for v in base for suf in ("888", "8888")})

    bad_labels: dict[str, list[str]] = {
        "bar_60m": ["09:00", "11:00", "13:00", "14:00", "21:00"],
        "bar_30m": ["09:00", "10:30", "11:00", "11:30", "13:30", "14:00", "21:00"],
        "bar_15m": ["09:00", "10:30", "13:30", "21:00"],
        "bar_5m":  ["09:00", "10:20", "10:25", "10:30", "13:30", "21:00"],
    }
    hits: list[str] = []
    total = 0
    for tbl, labs in bad_labels.items():
        try:
            n = c.scalar(
                f"SELECT count(*) FROM l1_mkt.{tbl} "
                " WHERE symbol = ANY(:syms) "
                "   AND bucket >= timestamptz '2015-01-01 00:00+08' "
                "   AND to_char(bucket, 'HH24:MI') = ANY(:labs)",
                syms=syms, labs=labs) or 0
        except Exception as exc:                          # noqa: BLE001
            return False, label, f"{tbl} 查询失败 {type(exc).__name__}: {str(exc)[:70]}", (
                "查询失败 → 无法判定（不要当成通过）")
        total += int(n)
        if n:
            hits.append(f"{tbl}={int(n):,}")
    return total == 0, label, (
        f"{len(syms)} 个 symbol / 2015-01-01 起 / 旧网格残留 {total:,} 根"
        + (f"（{', '.join(hits)}）" if hits else "")
    ), ("" if total == 0 else
        "仍有旧网格 bucket（time_bucket 左沿）→ 该区间未按会话网格重建，"
        "或重建脚本的 DELETE 窗口漏了边界。见 CL-20261109-14 / "
        "scripts/ops/rebuild_l1_grid.py")


def _c_push_log_retired(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261109-18：已退役的 `fusion_push_log` 必须「真的没了」，且无人再查它。

    三段衡量的都是**现在的行为**，不是流水账：
      A 库：任何 schema 下都不存在名为 fusion_push_log 的表/视图；
      B 码：``app/`` ``scripts/`` 下没有以 SQL 形式引用它的语句（FROM/INTO/JOIN/UPDATE/
        TABLE 前缀）—— 迁移目录豁免：迁移是历史记录，不回改；
      C 判据：ops_monitor 的推送活性语句仍必须有 v5x_signal_log。

    B 的后果不是「报错」而是「静默」：查一张不存在的表会让整条多列 SELECT 失败 →
    ``cols`` 为空 → ``silence_h=None`` → push_silence 既不告警也不报错，判据凭空消失。

    C 单独断言的道理同上：删表时最省事的做法是把整段判据一起注释掉，那会把
    「信号发不出去」从「每 10 分钟误报」变成「永远沉默」——而沉默没人会抱怨，
    也就永远不会被发现。
    """
    label = "已退役的 fusion_push_log 无残留（库对象/代码引用/监控判据仍在）"
    bad: list[str] = []
    observed: list[str] = []

    # A. 库对象（表或视图，任何 schema）
    n = c.scalar(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        " WHERE c.relname = 'fusion_push_log'") or 0
    observed.append(f"db_objects={int(n)}")
    if n:
        bad.append(f"库里仍有 {int(n)} 个 fusion_push_log 对象（表/视图）"
                   "→ 069 未执行，或有人从归档恢复了它")

    # B. 代码引用（SQL 形式；注释与文档提及不算）
    pat = re.compile(
        r"\b(?:FROM|INTO|JOIN|UPDATE|TABLE)\s+(?:public\.)?fusion_push_log\b",
        re.IGNORECASE)
    root = Path(__file__).resolve().parents[2]
    hits: list[str] = []
    for sub in ("app", "scripts"):
        d = root / sub
        if not d.exists():
            continue
        for p in d.rglob("*.py"):
            if "migrations" in p.parts:
                continue
            try:
                txt = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if pat.search(txt):
                hits.append(str(p.relative_to(root)))
    observed.append(f"code_refs={len(hits)}")
    if hits:
        bad.append(f"{len(hits)} 个文件仍以 SQL 引用该表（查不存在的表 → 判据静默消失）："
                   f"{hits[:3]}")

    # C. 监控判据不许被顺手删掉
    mon = root / "scripts" / "ops" / "ops_monitor.py"
    src = ""
    try:
        src = mon.read_text(encoding="utf-8")
    except OSError as exc:                                   # noqa: BLE001
        bad.append(f"读不到 ops_monitor.py：{type(exc).__name__}: {exc}")
    if src:
        has_v5x = "FROM v5x_signal_log" in src
        observed.append(f"monitor_v5x_source={'yes' if has_v5x else 'no'}")
        if not has_v5x:
            bad.append("ops_monitor 的推送活性判据不再查 v5x_signal_log → "
                       "「信号发不出去」将永久沉默（比误报更危险）")

    return (not bad), label, " ".join(observed), ("" if not bad else "；".join(bad))


def _c_mpr_exchange_coverage(c: Ctx) -> tuple[bool, str, str, str]:
    """会员持仓排名：每所品种覆盖不得相对**自身**近期中位数腰斩（CL-20261110-05）。

    2026-11-10 体检发现 11-09 总行数 -26%，逐所拆开才看清：CZCE/SHFE/GFEX
    全部正常，只有 DCE 从 11-06 的 1710 行/87 品种塌到 240 行/12 品种
    （只剩 V/Y）。``OPS-FRESH-TRADE-DAY`` 只看「最新交易日有没有数」，
    四个所里只要有一个写进了行整条就 PASS —— 典型的分母掩盖。

    按交易所各与自身近 30 天品种数中位数比（不能用全局中位数：GFEX 天生
    只有 9 个品种，拿全局比会永远报它）。定 warn 而非 block：DCE 官网受
    瑞数防护、只能经 Scrapling 真浏览器采集，是**已知可能失败的外部依赖**，
    不该阻断发布，但必须每轮可见。
    """
    label = "各交易所会员持仓排名覆盖未塌陷（≥ 自身中位数 60%）"
    try:
        latest = c.scalar(
            "SELECT max(trade_date) FROM public.member_position_rank "
            "WHERE trade_date >= now() - interval '30 days'")
        rows = c.rows("""
            WITH d AS (
              SELECT trade_date, exchange, count(DISTINCT symbol) AS syms
              FROM public.member_position_rank
              WHERE trade_date >= now() - interval '30 days'
              GROUP BY 1, 2
            ), base AS (
              SELECT exchange,
                     percentile_cont(0.5) WITHIN GROUP (ORDER BY syms) AS med
              FROM d WHERE trade_date < CAST(:latest AS date)
              GROUP BY 1
            )
            SELECT b.exchange, coalesce(x.syms, 0), b.med
            FROM base b
            LEFT JOIN (SELECT exchange, syms FROM d
                       WHERE trade_date = CAST(:latest AS date)) x USING (exchange)
            ORDER BY 1
        """, latest=latest)
    except Exception as e:  # noqa: BLE001
        return False, label, f"查询失败 {type(e).__name__}", str(e)[:200]
    bad, obs = [], []
    for exch, syms, med in rows:
        med = float(med or 0)
        ratio = (syms / med) if med else 0
        obs.append(f"{exch}={syms}/{med:.0f}({ratio:.0%})")
        if med and ratio < 0.60:
            bad.append(f"{exch} {syms} 品种仅为中位数 {med:.0f} 的 {ratio:.0%}")
    return (not bad), label, f"基准日 {latest}: " + " ".join(obs), (
        "" if not bad else "；".join(bad)
        + " → 见 CL-20261110-05/11：17:30 rank_position 已自动用新浪单合约补齐；"
          "若仍缺可跑 scripts/ops/backfill_dce_rank_sina.py --start ... --apply")


def _c_backfill_gap_calendar_wired(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261110-07：5m 缺口探测必须接交易日历、按「前后夜盘形态」分组。

    旧判据按星期几求众数，把节后首日（2026-10-08）80 根误判成缺口，
    导致 AO/SS 被补进新浪凌晨合成脏 bar。断言做三件事：
      1) 动态加载判据模块，用真实历史交易日跑**正反哨兵**；
      2) 检查探测脚本真实 import 了该模块、且已不再用 ``extract(dow``；
      3) 检查真源 ``public.futures_rule`` 近 30 天确实有交易日
         （``l3_ref.trade_calendar`` 实测停更到 10-08，不可用）。
    """
    label = "5m 缺口探测接 futures_rule 日历（防节后首日假阳性）"
    bad, obs = [], []
    root = Path(__file__).resolve().parents[2]
    try:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "trading_day_caliber", root / "scripts/ops/trading_day_caliber.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception as e:  # noqa: BLE001
        return False, label, f"判据模块加载失败 {type(e).__name__}", str(e)[:200]

    # 用 2026-09-28~11-10 的**真实**交易日集合（futures_rule 实测），稀疏集合
    # 会让「下一交易日」距离失真，把普通周五也算成长假。
    td = {date.fromisoformat(s) for s in (
        "2026-09-28 2026-09-29 2026-09-30 2026-10-08 2026-10-09 "
        "2026-10-12 2026-10-13 2026-10-14 2026-10-15 2026-10-16 "
        "2026-10-19 2026-10-20 2026-10-21 2026-10-22 2026-10-23 "
        "2026-10-26 2026-10-27 2026-10-28 2026-10-29 2026-10-30 "
        "2026-11-02 2026-11-03 2026-11-04 2026-11-05 2026-11-06 "
        "2026-11-09 2026-11-10").split()}
    for d, exp, tag in ((date(2026, 10, 8), False, "节后首日"),
                        (date(2026, 10, 9), True, "节后次日"),
                        (date(2026, 10, 12), False, "周一"),
                        (date(2026, 11, 10), True, "周二")):
        got = bool(mod.has_prev_session(d, td))
        obs.append(f"{tag}={got}")
        if got != exp:
            bad.append(f"{tag} {d} 期望 {exp} 实得 {got}")
    for d, exp, tag in ((date(2026, 9, 30), (True, False), "节前末日"),
                        (date(2026, 10, 8), (False, True), "节后首日"),
                        (date(2026, 11, 6), (True, True), "周五"),
                        (date(2026, 11, 10), (True, False), "窗口末端")):
        got = tuple(mod.session_shape(d, td))
        obs.append(f"shape_{tag}={got}")
        if got != exp:
            bad.append(f"{tag} {d} 期望形态 {exp} 实得 {got}")
    obs.append(f"src={mod.TRADING_DAY_TABLE}")
    if mod.TRADING_DAY_TABLE != "public.futures_rule":
        bad.append("交易日真源不是 public.futures_rule（停更的 trade_calendar 不可用）")
    try:
        src = (root / "scripts/ops/backfill_bars_from_5m.py").read_text(
            encoding="utf-8", errors="ignore")
    except OSError as e:
        return False, label, "读探测脚本失败", str(e)[:200]
    # 四形态判据：只有 has_prev_session（二分）仍会把节前末日误判成缺口
    wired = "trading_day_caliber" in src and "session_shape(" in src
    legacy = "extract(dow" in src or "GROUP BY dow" in src
    obs.append(f"wired={wired} legacy_dow={legacy}")
    if not wired:
        bad.append("探测脚本未真实引用交易日历判据模块")
    if legacy:
        bad.append("探测脚本仍按星期几分组（节后首日会复发假阳性）")
    n = c.scalar("SELECT count(DISTINCT trade_date) FROM public.futures_rule "
                 "WHERE trade_date >= CURRENT_DATE - interval '30 days'") or 0
    obs.append(f"calendar_30d={n}")
    if n < 5:
        bad.append(f"futures_rule 近 30 天仅 {n} 个交易日 → 日历真源可能停更")
    return (not bad), label, " ".join(obs), (
        "" if not bad else "；".join(bad) + " → 见 CL-20261110-07")


def _c_dce_sina_wired(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261110-11：DCE 新浪单合约通道必须**真实接线**且保持正确对齐口径。

    防四种复发（都是本项目踩过的坑）：
      1) 改回「按会员名合并三表」→ 31 行/合约，member 与 short_pos 错配；
      2) 改回 ``match_main_contract('dce')`` 批量清单 → 返回非 DCE 品种，
         历史 393 行 ``src='sina_cot'`` 污染即此因；
      3) 只把模块放进仓库、``rank_position`` 没人调用 → 「表建了没人读」；
      4) 新浪接口改为忽略 ``date`` 恒返回最新一天 → 把未来数据写进历史。
    为此额外锁住门 0（date 敏感性）与门 2（跨日增减自洽）在补数主流程里
    被真实调用，而不只是「函数定义存在」。
    """
    label = "DCE 新浪单合约通道真实接线（按名次对齐 + 三道质量门）"
    bad, obs = [], []
    root = Path(__file__).resolve().parents[2]
    try:
        mod = (root / "app/ingest/dce_sina.py").read_text(
            encoding="utf-8", errors="ignore")
        rp = (root / "app/ingest/rank_position.py").read_text(
            encoding="utf-8", errors="ignore")
        cli = (root / "scripts/ops/backfill_dce_rank_sina.py").read_text(
            encoding="utf-8", errors="ignore")
    except OSError as e:
        return False, label, "读源码失败", str(e)[:200]

    body = mod.split("def backfill_missing", 1)[-1]

    # 用 AST 判「有没有真的调用」而不是字符串匹配：docstring 里必须能写明
    # 「禁止使用 match_main_contract」而不被误判成违规。
    import ast

    def has_call(src: str, name: str) -> bool:
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return True  # 解析不了就按违规处理，保守
        return any(
            isinstance(n, ast.Call) and (
                (isinstance(n.func, ast.Attribute) and n.func.attr == name)
                or (isinstance(n.func, ast.Name) and n.func.id == name))
            for n in ast.walk(tree))

    def has_sina_cot_src(src: str) -> bool:
        """只看真实的 ``SRC = "sina_cot"`` 赋值；注释里说明旧标记不算违规。"""
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return True
        return any(
            isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "SRC" for t in n.targets)
            and isinstance(n.value, ast.Constant)
            and n.value.value == "sina_cot"
            for n in ast.walk(tree))

    checks = [
        ("futures_hold_pos_sina" in mod, "调用 futures_hold_pos_sina"),
        ('SRC_TAG = "sina:futures_hold_pos_sina"' in mod, "src 为新标记（非 sina_cot）"),
        ("range(1, 21)" in mod and "for i in range(20)" in mod, "按名次 1..20 横向拼三表"),
        (not has_call(mod, "match_main_contract"), "模块无批量合约清单调用"),
        ("verify_date_sensitive(" in body, "门0 date 敏感性在主流程被调用"),
        ("cross_day_selfcheck(" in body, "门2 跨日增减自洽在主流程被调用"),
        ("l0_raw.member_position_rank" in mod, "候选合约来自库内 DCE 真实合约"),
        ("ON CONFLICT" in mod, "幂等写入（不覆盖官网行）"),
    ]
    for ok, tag in checks:
        obs.append(f"{tag}={'yes' if ok else 'NO'}")
        if not ok:
            bad.append(tag)
    rp_checks = [
        ("from app.ingest import dce_sina" in rp, "rank_position 真实 import dce_sina"),
        ("_run_dce(date)" in rp, "DCE 分支调用 _run_dce"),
        ("merged[member]" not in rp, "已删除按会员合并逻辑"),
        (not has_call(rp, "match_main_contract"), "已删除批量合约清单调用"),
        (not has_sina_cot_src(rp), "不再写旧污染 src"),
    ]
    for ok, tag in rp_checks:
        obs.append(f"{tag}={'yes' if ok else 'NO'}")
        if not ok:
            bad.append(tag)
    obs.append(f"cli_backfill={'yes' if 'backfill_missing' in cli else 'NO'}")
    if "backfill_missing" not in cli:
        bad.append("补数 CLI 未引用 backfill_missing")
    return (not bad), label, " ".join(obs), (
        "" if not bad else "；".join(bad) + " → 见 CL-20261110-11")


def _c_mpr_dce_coverage(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261110-11：近 30 天**每个** DCE 交易日的品种覆盖都不得塌陷。

    旧的 ``OPS-MPR-EXCHANGE-COVERAGE`` 只比「最新交易日 vs 自身中位数」，
    一旦断档日被后续正常日盖过就永远看不见（11-09 正是这样只靠人工体检发现）。
    这条逐日比对，把「补完即通过」变成可判定的当前状态，不依赖时间衰减。
    排除 ``CURRENT_DATE``：当日 17:30 采集尚未执行时不该算违约。
    """
    label = "DCE 会员持仓排名逐日品种覆盖完整（≥ 中位数 90%）"
    try:
        rows = c.rows("""
            WITH d AS (
              SELECT trade_date,
                     count(DISTINCT upper(regexp_replace(symbol,'[0-9]+$',''))) v
              FROM public.member_position_rank
              WHERE exchange = 'DCE'
                AND trade_date >= now() - interval '30 days'
                AND trade_date < CURRENT_DATE
              GROUP BY 1
            ), m AS (
              SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY v) med FROM d
            )
            SELECT d.trade_date, d.v, m.med FROM d CROSS JOIN m ORDER BY 1
        """)
    except Exception as e:  # noqa: BLE001
        return False, label, f"查询失败 {type(e).__name__}", str(e)[:200]
    bad, obs = [], []
    for d, v, med in rows:
        med = float(med or 0)
        if med and v < med * 0.9:
            bad.append(f"{d} 仅 {v} 品种（中位数 {med:.0f} 的 {v / med:.0%}）")
    mins = min((int(v) for _, v, _ in rows), default=0)
    obs.append(f"近30天 {len(rows)} 个交易日，最低 {mins} 品种")
    return (not bad), label, " ".join(obs), (
        "" if not bad else "；".join(bad[:6]) + " → 见 CL-20261110-05/11")


def _c_factor_mpr_coverage(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261110-08：上游补数后，会员类因子必须按 trade_date 同步重算。

    事故形状：DCE 补数把 ``member_position_rank_summary`` 11-09 从 41 → 55 品种，
    但当日 18:00 的 B 组作业已经跑完；因子层停在 41 品种，而因子巡检只看
    整表 ``max(trade_date)``（=11-10，判为新鲜）→ 永远不会自动重算。
    判据衡量「此刻」而非历史流水：近 7 个已完成交易日里，5 个会员类因子
    当日 distinct symbol 数均须 ≥ 该日 summary 品种数的 90%，且 5 个因子
    不得整体缺席。排除 ``CURRENT_DATE``（当日 18:00 作业尚未执行）。
    只检查 summary 有数据的交易日，避免把休市日误判成违约。
    """
    label = "会员类因子逐日品种覆盖与 summary 一致（补数后须重算）"
    try:
        rows = c.rows("""
            WITH d AS (
              SELECT DISTINCT trade_date FROM public.futures_rule
              WHERE trade_date < CURRENT_DATE
                AND trade_date >= CURRENT_DATE - interval '14 days'
              ORDER BY trade_date DESC LIMIT 7
            ), s AS (
              SELECT report_date AS trade_date, count(DISTINCT symbol) AS n
              FROM l1_mkt.member_position_rank_summary
              WHERE report_date >= CURRENT_DATE - interval '14 days'
              GROUP BY 1
            ), f AS (
              SELECT trade_date, count(DISTINCT factor_id) AS fac_n, min(n) AS min_n
              FROM (
                SELECT trade_date, factor_id, count(DISTINCT symbol) AS n
                FROM l2_adj.factor_value
                WHERE factor_id IN ('member_net_z','member_net_ts_z','member_ls_z',
                                    'member_trend_z','vol_concentration_z')
                  AND trade_date >= CURRENT_DATE - interval '14 days'
                GROUP BY 1,2
              ) x GROUP BY 1
            )
            SELECT d.trade_date, s.n, coalesce(f.fac_n, 0), coalesce(f.min_n, 0)
            FROM d JOIN s ON s.trade_date = d.trade_date
            LEFT JOIN f ON f.trade_date = d.trade_date
            ORDER BY 1 DESC
        """)
    except Exception as e:  # noqa: BLE001
        return False, label, f"查询失败 {type(e).__name__}", str(e)[:200]
    bad, obs = [], []
    for d, n_summary, fac_n, min_n in rows:
        n_summary, fac_n, min_n = int(n_summary), int(fac_n), int(min_n)
        obs.append(f"{d}:{min_n}/{n_summary}({fac_n}因子)")
        if fac_n < 5 or min_n < n_summary * 0.9:
            bad.append(f"{d} summary {n_summary} 品种 / 因子仅 {fac_n} 个、最少 {min_n} 品种")
    return (not bad), label, f"近 {len(rows)} 个交易日 " + " ".join(obs[:4]), (
        "" if not bad else "；".join(bad[:5]) + " → 见 CL-20261110-08，按 trade_date 重算 B 组")


def _c_factor_retired_consist(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261110-16：因子退役必须「代码真接线 + 库里有标记 + 历史不删」。

    防的是反复出现的「改一半」：退役只写进注释/台账，巡检照样每天告警、
    补算照样每天白跑；或者干脆删掉历史行，让断供看起来消失。
    用 AST 判真实赋值与真实引用（字符串 count 会被注释骗过，见 OPS-DCE-SINA-WIRED）。
    """
    import ast  # noqa: PLC0415

    label = "退役因子 f_amt_conc 在巡检中真接线且历史数据保留"
    p = Path(__file__).resolve().parents[2] / "scripts" / "factor_freshness_check.py"
    bad, obs = [], []
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
    except Exception as e:  # noqa: BLE001
        return False, label, f"源码读取失败 {type(e).__name__}", str(e)[:200]

    # 1) RETIRED_FACTORS 必须是真实赋值（不是注释），且真包含 f_amt_conc
    retired_vals: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not any(isinstance(t, ast.Name) and t.id == "RETIRED_FACTORS" for t in targets):
            continue
        val = node.value
        if isinstance(val, ast.Call) and isinstance(val.func, ast.Name) \
                and val.func.id in ("frozenset", "set", "tuple", "list"):
            containers = [a for a in val.args if isinstance(a, (ast.Set, ast.List, ast.Tuple))]
        else:
            containers = [val] if isinstance(val, (ast.Set, ast.List, ast.Tuple)) else []
        for cont in containers:
            for el in cont.elts:
                if isinstance(el, ast.Constant) and isinstance(el.value, str):
                    retired_vals.add(el.value)
    if "f_amt_conc" not in retired_vals:
        bad.append("RETIRED_FACTORS 未真实包含 f_amt_conc")
    else:
        obs.append("常量含 f_amt_conc")

    # 2) needs_attention 两个判据入口必须各有一次真实过滤
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
               and n.name == "needs_attention"), None)
    n_ref = 0
    if fn is not None:
        for n in ast.walk(fn):
            if isinstance(n, ast.Compare) and any(
                    isinstance(x, ast.Name) and x.id == "RETIRED_FACTORS"
                    for x in [n.left] + list(n.comparators)):
                n_ref += 1
    if n_ref < 2:
        bad.append(f"needs_attention 的 RETIRED_FACTORS 过滤仅 {n_ref} 处（应 2 处）")
    else:
        obs.append(f"过滤 {n_ref} 处")

    # 3) registry 有退役标记；4) 历史行不得被删
    try:
        rows = c.rows("""
            SELECT (SELECT count(*) FROM factor_registry
                     WHERE factor_id='f_amt_conc' AND description LIKE '%CL-20261110-16%'),
                   (SELECT count(*) FROM l2_adj.factor_value WHERE factor_id='f_amt_conc')
        """)
        n_mark, n_hist = int(rows[0][0]), int(rows[0][1])
        if n_mark < 1:
            bad.append("factor_registry 缺退役标记")
        else:
            obs.append("registry 已标记")
        if n_hist < 120000:
            bad.append(f"历史行仅 {n_hist}（疑似误删）")
        else:
            obs.append(f"历史 {n_hist:,} 行保留")
    except Exception as e:  # noqa: BLE001
        return False, label, f"库内核查失败 {type(e).__name__}", str(e)[:200]

    return (not bad), label, " ".join(obs), (
        "" if not bad else "；".join(bad) + " → 见 CL-20261110-16")


def _c_vwap_caliber_wired(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261110-17：f_vwap_dev 必须真用成交量加权典型价，且数值不再退化。

    旧式 ``(cl - amt/vol)/atr*100`` 在 X888 的 amount 全 NULL 时静默变成
    ``cl/atr*100``：有值、有逐日覆盖，所有新鲜度巡检都看不出语义失真。
    所以这里同时锁「源码真实接线（AST，防注释蒙混）」和「库内数值量级」，
    并要求历史行仍在（防删数据让断言转绿）。
    """
    import ast  # noqa: PLC0415

    label = "f_vwap_dev 采用成交量加权典型价口径且数值未退化"
    p = Path(__file__).resolve().parents[2] / "scripts" / "factor_ic_scan.py"
    bad, obs = [], []
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
    except Exception as e:  # noqa: BLE001
        return False, label, f"源码读取失败 {type(e).__name__}", str(e)[:200]

    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
               and n.name == "build_factors"), None)
    if fn is None:
        return False, label, "build_factors 不存在", "factor_ic_scan.py 结构已变"
    expr = None
    has_tpv = False
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) \
                        and t.value.id == "out" and isinstance(t.slice, ast.Constant) \
                        and t.slice.value == "f_vwap_dev":
                    expr = ast.unparse(n.value)
                if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) \
                        and t.value.id == "ds" and isinstance(t.slice, ast.Constant) \
                        and t.slice.value == "tpv":
                    has_tpv = True
    if expr is None or "vwap_vol" not in expr:
        bad.append("f_vwap_dev 未真实引用 vwap_vol（旧 amt/vol 口径可能回来了）")
    elif "amt" in expr:
        bad.append("f_vwap_dev 仍引用 amt")
    else:
        obs.append("AST: 赋值仅用 vwap_vol")
    if not has_tpv:
        bad.append("build_factors 未构造逐根 tpv（典型价×成交量）")
    else:
        obs.append("tpv 已构造")

    try:
        rows = c.rows("""
            SELECT count(*) AS n,
                   coalesce(max(abs(raw_value)), 0) AS mx,
                   coalesce(percentile_cont(0.99) WITHIN GROUP
                            (ORDER BY abs(raw_value)), 0) AS p99
            FROM l2_adj.factor_value
            WHERE factor_id = 'f_vwap_dev'
              AND trade_date >= current_date - interval '30 days'
              AND symbol IN (SELECT symbol FROM l3_ref.v_tradable_scope)
        """)
        n, mx, p99 = int(rows[0][0]), float(rows[0][1]), float(rows[0][2])
        # 旧口径近 30 天均值 >5,000、最大 >30,000；新口径是「收盘价偏离 VWAP
        # 几个 ATR」，p99 应远小于 1,000。阈值留了一个数量级余量。
        if n < 100:
            bad.append(f"近 30 天仅 {n} 行（疑似删数据或因子断供）")
        elif p99 >= 1000 or mx >= 5000:
            bad.append(f"数值量级异常 p99={p99:.1f} max={mx:.1f}（疑似退化成 cl/atr*100）")
        else:
            obs.append(f"近30天 {n:,} 行 p99={p99:.1f} max={mx:.1f}")
        mark = c.rows("""SELECT count(*) FROM factor_registry
                         WHERE factor_id='f_vwap_dev'
                           AND description LIKE '%CL-20261110-17%'""")
        if int(mark[0][0]) < 1:
            bad.append("factor_registry 缺口径变更标记")
        else:
            obs.append("registry 已声明口径")
    except Exception as e:  # noqa: BLE001
        return False, label, f"库内核查失败 {type(e).__name__}", str(e)[:200]

    return (not bad), label, " ".join(obs), (
        "" if not bad else "；".join(bad) + " → 见 CL-20261110-17")


def _c_task_status_source(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261110-13：不写宿主流水的作业必须声明真实状态源，且该来源当前健康。

    防两种复发：① 把「作业在跑但流水没写」误判成停摆，或反过来靠置 NULL
    掩盖真停摆；② 只改字典不改断言，下次漂移又只能靠人发现。
    状态结论一律取自主表/系统视图聚合，不取事件表推断（skill 铁律 #4）。
    """
    label = "非流水监控作业声明真实状态源且来源健康"
    bad, obs = [], []
    # 本轮（migration 073）显式声明状态来源的作业。锁死这个集合，防有人
    # 随便给别的作业贴个 status_source 就把心跳关掉。
    declared = {
        "cloud_local_sync", "sp_build_l1_from_minute", "backtest_weekly",
        "tsdb_job_1003", "tsdb_job_1", "tsdb_job_3", "tsdb_job_1000",
        "tsdb_job_1001", "tsdb_job_1002", "tsdb_job_1004",
        "tsdb_job_1005", "tsdb_job_1007",
    }
    try:
        rows = c.rows("""
            SELECT task_id, coalesce(meta->>'status_source', '')
            FROM ops.scheduled_task WHERE meta->>'status_source' IS NOT NULL
        """)
        unknown = sorted(t for t, _ in rows if t not in declared)
        empty = sorted(t for t, s in rows if not s.strip())
        if unknown:
            bad.append("未授权的 status_source: " + ",".join(unknown[:6]))
        if empty:
            bad.append("status_source 为空: " + ",".join(empty[:6]))
        if not unknown and not empty:
            obs.append(f"声明来源 {len(rows)} 个全部合规")

        # 真正防「置 NULL 掩盖停摆」：apscheduler/crontab 里近 24h 有 >=20 次
        # success 流水、又没声明状态源的作业，绝不能 expected_gap_days=NULL。
        # manual / stored_proc / systemd / timescaledb_job 既有 no_heartbeat 共 19 个
        # （含高频 v5x_scan，其存活由 V5X-LIVE-WIRED 等断言覆盖）不受影响，
        # 避免本轮改造误伤历史设计。
        masked = c.rows("""
            SELECT t.task_id FROM ops.scheduled_task t
            WHERE t.enabled AND t.expected_gap_days IS NULL
              AND t.executor IN ('apscheduler', 'crontab')
              AND coalesce(t.meta->>'status_source', '') = ''
              AND (SELECT count(*) FROM ops.scheduled_task_run r
                    WHERE r.task_id = t.task_id AND r.status = 'success'
                      AND r.started_at >= now() - interval '24 hours') >= 20
        """)
        if masked:
            bad.append("高频流水作业被置 NULL: " + ",".join(r[0] for r in masked[:6]))
        else:
            obs.append("高频流水作业均保留心跳")

        # TSDB 后台作业：字典 enabled 项必须在 jobs 表存在且 scheduled，
        # 且 job_stats 最近一次运行成功、下次触发在未来。
        orphan = c.rows("""
            SELECT t.task_id FROM ops.scheduled_task t
            WHERE t.enabled AND t.executor = 'timescaledb_job'
              AND regexp_replace(t.task_id, '^tsdb_job_', '')::int
                  NOT IN (SELECT job_id FROM timescaledb_information.jobs)
        """)
        if orphan:
            bad.append("字典有陈旧 tsdb_job: " + ",".join(r[0] for r in orphan[:6]))
        js = c.rows("""
            SELECT count(*) FILTER (WHERE s.last_run_status <> 'Success') AS n_fail,
                   count(*) FILTER (WHERE s.next_start <= now()) AS n_overdue,
                   count(*) AS n_total
            FROM timescaledb_information.jobs j
            JOIN timescaledb_information.job_stats s USING (job_id)
            WHERE j.scheduled
        """)
        n_fail, n_overdue, n_total = (int(x) for x in js[0])
        if n_fail or n_overdue:
            bad.append(f"TSDB job_stats 失败 {n_fail} / 逾期 {n_overdue}（共 {n_total}）")
        else:
            obs.append(f"TSDB {n_total} job 全 Success")

        bt = c.rows("""SELECT count(*) FROM public.backtest_result
                       WHERE created_at >= now() - interval '14 days'""")
        if int(bt[0][0]) < 1:
            bad.append("backtest_result 近 14 天无产出（周度回测状态源断供）")
        else:
            obs.append(f"backtest_result 近14天 {int(bt[0][0]):,} 行")
    except Exception as e:  # noqa: BLE001
        return False, label, f"库内核查失败 {type(e).__name__}", str(e)[:200]
    return (not bad), label, " ".join(obs), (
        "" if not bad else "；".join(bad) + " → 见 CL-20261110-13")


def _c_reaped_abort_only(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261110-13 F：aborted 只能是进程重启自愈遗留，不能成为真故障的垃圾桶。

    ``_reap_stale_running`` 每次启动把上一进程遗留的 running 置 aborted 并写明原因；
    11-09/10 因密集发布重启产生 127 条，全部带 reaped 标记，journal 明确写
    「非任务故障」。若哪天真挂死被塞进这个状态、或自愈失效留下僵尸 running，
    这条断言立刻 FAIL。
    """
    label = "近 24h aborted 全为重启自愈遗留且无僵尸 running"
    try:
        rows = c.rows("""
            SELECT count(*) FILTER (WHERE status='aborted') AS n_ab,
                   count(*) FILTER (WHERE status='aborted'
                     AND (message IS NULL OR message NOT LIKE '%reaped%')) AS n_bad,
                   (SELECT count(*) FROM ops.scheduled_task_run
                     WHERE status='running'
                       AND started_at < now() - interval '2 hours') AS n_zombie
            FROM ops.scheduled_task_run
            WHERE started_at >= now() - interval '24 hours'
        """)
        n_ab, n_bad, n_zombie = (int(x) for x in rows[0])
    except Exception as e:  # noqa: BLE001
        return False, label, f"库内核查失败 {type(e).__name__}", str(e)[:200]
    bad = []
    if n_bad:
        bad.append(f"{n_bad} 条 aborted 非 reaped（真故障被误归类）")
    if n_zombie:
        bad.append(f"{n_zombie} 条 running 超 2 小时（自愈未生效）")
    if n_ab > 80:
        bad.append(f"近 24h aborted {n_ab} 条（重启过于频繁，查部署动作）")
    return (not bad), label, (
        f"近24h aborted={n_ab} / 非reaped={n_bad} / 僵尸running={n_zombie}"), (
        "" if not bad else "；".join(bad) + " → 见 CL-20261110-13")


def _c_alert_digest_dedup(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261110-20：告警汇总必须「状态变化才推」，且持久化去重真接线。

    旧版 ``send_alert(key="digest")`` 冷却 1800s == ALERT_INTERVAL_MIN=30，
    状态没变化也每 30 分钟重推一条（11-10 实测 20 条），曾刷爆 pushplus
    额度让交易信号静默 2.5 天。只改注释/只加冷却都挡不住，必须 AST 判真实门控。
    """
    import ast  # noqa: PLC0415

    label = "告警汇总按状态指纹去重（不是每 30 分钟重推）"
    p = Path(__file__).resolve().parents[2] / "app" / "ops" / "alerting.py"
    bad, obs = [], []
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
    except Exception as e:  # noqa: BLE001
        return False, label, f"源码读取失败 {type(e).__name__}", str(e)[:200]
    if not any(isinstance(n, ast.FunctionDef) and n.name == "_digest_fingerprint"
               for n in tree.body):
        bad.append("缺 _digest_fingerprint 状态指纹")
    else:
        obs.append("状态指纹存在")

    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
               and n.name == "check_all"), None)
    if fn is None:
        bad.append("check_all 不存在")
    else:
        calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Name)]
        if not any(n.func.id == "_should_send_digest" for n in calls):
            bad.append("check_all 未真实调用 _should_send_digest")
        else:
            obs.append("状态门真接线")
        legacy = [n for n in calls if n.func.id == "send_alert" and any(
            kw.arg == "key" and isinstance(kw.value, ast.Constant)
            and kw.value.value == "digest" for kw in n.keywords)]
        if legacy:
            bad.append("仍使用 send_alert(key='digest') 旧 1800s 冷却")
        else:
            obs.append("旧冷却已摘除")
        guarded = any(
            isinstance(x, ast.If) and any(
                isinstance(y, ast.Call) and isinstance(y.func, ast.Name)
                and y.func.id == "send_alert" for z in x.body for y in ast.walk(z))
            for x in ast.walk(fn))
        if not guarded:
            bad.append("send_alert 未受条件门控制")
        else:
            obs.append("推送受条件门控制")

    # 持久化去重：表存在 + 近 24h 实际推送留痕 ≤ 8（旧行为约 48 条/日）
    try:
        tbl = c.rows("SELECT to_regclass('ops.notify_dedup')")[0][0]
        if not tbl:
            bad.append("ops.notify_dedup 未建")
        else:
            n = int(c.rows("""
                SELECT count(*) FROM ops.notify_dedup
                WHERE kind='alert_digest' AND sent_at >= now() - interval '24 hours'
            """)[0][0])
            if n > 8:
                bad.append(f"近 24h digest 推送 {n} 条（去重失效/刷屏复发）")
            else:
                obs.append(f"近24h digest 留痕 {n} 行")
    except Exception as e:  # noqa: BLE001
        return False, label, f"库内核查失败 {type(e).__name__}", str(e)[:200]
    return (not bad), label, " ".join(obs), (
        "" if not bad else "；".join(bad) + " → 见 CL-20261110-20")


def _c_jobhb_status_source(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261110-20：心跳频率/阈值以 ops.scheduled_task 为单一真相。

    迁移 073 把 backtest_weekly 的 expected_gap_days 改成 14，代码
    scheduler_jobs._GAP_WEEKLY 仍是 9；若巡检继续用代码值，合法周频空档
    会被固定 9 天阈值误判成停摆（11-10 15:00 两条 jobhb 误报）。
    """
    import ast  # noqa: PLC0415

    label = "作业心跳以 DB 字典 cron/阈值为准且尊重 status_source"
    p = Path(__file__).resolve().parents[2] / "app" / "ops" / "alerting.py"
    bad, obs = [], []
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
    except Exception as e:  # noqa: BLE001
        return False, label, f"源码读取失败 {type(e).__name__}", str(e)[:200]
    fn = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
               and n.name == "_check_job_heartbeat"), None)
    if fn is None:
        bad.append("_check_job_heartbeat 不存在")
    else:
        consts = [n.value for n in ast.walk(fn) if isinstance(n, ast.Constant)
                  and isinstance(n.value, str)]
        if not any("expected_gap_days" in s and "scheduled_task" in s for s in consts):
            bad.append("SQL 未从 ops.scheduled_task 读取 expected_gap_days")
        else:
            obs.append("阈值取自 DB 字典")
        vc = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Name)
              and n.func.id == "job_heartbeat_verdict"]
        if not vc or not any(any(
                kw.arg == "expected_gap_days" and isinstance(kw.value, ast.Name)
                and kw.value.id == "gap" for kw in n.keywords) for n in vc):
            bad.append("verdict 仍用代码硬编码阈值")
        else:
            obs.append("verdict 用 DB gap")
        if not any(isinstance(n, ast.Continue) for n in ast.walk(fn)):
            bad.append("缺 no_heartbeat（gap IS NULL）跳过分支")
        else:
            obs.append("非流水作业跳过心跳")
    try:
        rows = c.rows("""
            SELECT (SELECT expected_gap_days FROM ops.scheduled_task
                     WHERE task_id='backtest_weekly'),
                   (SELECT count(*) FROM ops.scheduled_task
                     WHERE task_id IN ('lstm_weekly_retrain',
                                       'transmission_weekly_recalc','backtest_weekly')
                       AND schedule IS NOT NULL),
                   (SELECT count(*) FROM ops.scheduled_task
                     WHERE meta->>'status_source' IS NOT NULL
                       AND expected_gap_days IS NULL)
        """)
        gap, n_cron, n_no_hb = (int(x or 0) for x in rows[0])
        if gap != 14:
            bad.append(f"backtest_weekly 字典阈值 {gap} ≠ 14（DB 非单一真相）")
        else:
            obs.append("DB 阈值=14")
        if n_cron < 3:
            bad.append(f"仅 {n_cron}/3 条周频作业有 cron")
        # backtest_weekly 按迁移 073 设计「保留心跳」，不能要求 status_source 与
        # gap 互斥；这里只确认 -13 的 no_heartbeat 声明作业仍然存在。
        if n_no_hb < 1:
            bad.append("CL-20261110-13 的 no_heartbeat 声明作业已消失")
        else:
            obs.append(f"no_heartbeat 声明 {n_no_hb} 条")
    except Exception as e:  # noqa: BLE001
        return False, label, f"库内核查失败 {type(e).__name__}", str(e)[:200]
    return (not bad), label, " ".join(obs), (
        "" if not bad else "；".join(bad) + " → 见 CL-20261110-20")


def _c_git_sync_env_guard(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261110-20：一致性检查必须先判宿主环境。

    qhyc-api 容器内无 git/docker，误跑会抛 Errno 2 并推「一致性检查失败」
    假告警；宿主 09:00 crontab 本身 success。环境不适用 ≠ 仓库漂移。
    """
    import ast  # noqa: PLC0415

    label = "一致性检查有宿主环境自检，容器内误跑不推假告警"
    p = Path(__file__).resolve().parents[2] / "scripts" / "check_git_prod_sync.py"
    bad, obs = [], []
    try:
        tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
    except Exception as e:  # noqa: BLE001
        return False, label, f"源码读取失败 {type(e).__name__}", str(e)[:200]
    main = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
                 and n.name == "main"), None)
    envfn = next((n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == "_host_env_precheck"), None)
    if envfn is None or not any(
            isinstance(n, ast.Constant) and isinstance(n.value, str)
            and "/.dockerenv" in n.value for n in ast.walk(envfn)):
        bad.append("环境自检未判断 /.dockerenv")
    else:
        obs.append("容器判据存在")
    if main is None:
        bad.append("main 不存在")
    else:
        if not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                   and n.func.id == "_host_env_precheck" for n in ast.walk(main)):
            bad.append("main 未调用环境自检")
        else:
            obs.append("main 真接线")
        skip_ok = False
        for n in ast.walk(main):
            if not isinstance(n, ast.If):
                continue
            body_calls = [y.func.id for x in n.body for y in ast.walk(x)
                          if isinstance(y, ast.Call) and isinstance(y.func, ast.Name)]
            has_ret0 = any(isinstance(y, ast.Return)
                           and isinstance(y.value, ast.Constant)
                           and y.value.value == 0
                           for x in n.body for y in ast.walk(x))
            if has_ret0 and "_alert" not in body_calls:
                skip_ok = True
                break
        if not skip_ok:
            bad.append("容器 SKIP 分支仍会推告警或不存在")
        else:
            obs.append("SKIP 分支不推告警")
    try:
        n = int(c.rows("""
            SELECT count(*) FROM ops.scheduled_task_run
            WHERE task_id='cron_git_sync_check'
              AND started_at >= now() - interval '24 hours'
              AND status <> 'success'
              AND (message ILIKE '%无法 fetch%' OR message ILIKE '%Errno 2%')
        """)[0][0])
        if n:
            bad.append(f"近 24h 仍有 {n} 条 Errno 2 假告警流水")
        else:
            obs.append("近24h 无 Errno2 失败")
    except Exception as e:  # noqa: BLE001
        return False, label, f"库内核查失败 {type(e).__name__}", str(e)[:200]
    return (not bad), label, " ".join(obs), (
        "" if not bad else "；".join(bad) + " → 见 CL-20261110-20/22")


def _c_v5x_brief_wired(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261110-21：收盘链必须真接线状态简报，且去重/静默/持仓复用都真实。

    防「简报写了但从没被调用」与「去重失效每根 K 线刷屏」两种复发形态。
    """
    import ast  # noqa: PLC0415

    label = "收盘链后真接线 V5X 状态简报（去重/静默/持仓复用）"
    root = Path(__file__).resolve().parents[2]
    bad, obs = [], []
    try:
        sch = ast.parse((root / "app" / "scheduler.py").read_text(
            encoding="utf-8", errors="ignore"))
        job = next((n for n in ast.walk(sch) if isinstance(n, ast.FunctionDef)
                    and n.name == "_v5x_close_scan_job"), None)
        if job is None or not any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "push_close_brief" for n in ast.walk(job)):
            bad.append("_v5x_close_scan_job 未真实调用 push_close_brief")
        else:
            obs.append("收盘链真接线")
        bp = root / "app" / "strategies" / "v5x" / "brief.py"
        br = ast.parse(bp.read_text(encoding="utf-8", errors="ignore"))
        br_calls = {n.func.id for n in ast.walk(br)
                    if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        for need in ("send_notify", "theoretical_positions",
                     "is_hourly_collection_allowed"):
            if need not in br_calls:
                bad.append(f"brief.py 缺真实调用 {need}")
        if all(x in br_calls for x in ("send_notify", "theoretical_positions",
                                       "is_hourly_collection_allowed")):
            obs.append("推送/重放/时段门齐全")
        consts = [n.value for n in ast.walk(br) if isinstance(n, ast.Constant)
                  and isinstance(n.value, str)]
        if not any("ops.notify_dedup" in s for s in consts) or not any(
                "ON CONFLICT" in s for s in consts):
            bad.append("brief.py 缺持久化去重 SQL")
        else:
            obs.append("去重 SQL 存在")
        if not any(isinstance(n, ast.Raise) for n in ast.walk(br)):
            bad.append("推送失败未外抛（会被记成 success）")
        else:
            obs.append("失败外抛")
    except Exception as e:  # noqa: BLE001
        return False, label, f"源码读取失败 {type(e).__name__}", str(e)[:200]
    try:
        tbl = c.rows("SELECT to_regclass('ops.notify_dedup')")[0][0]
        if not tbl:
            bad.append("ops.notify_dedup 未建")
        else:
            n = int(c.rows("""
                SELECT count(*) FROM ops.notify_dedup
                WHERE kind='v5x_brief' AND sent_at >= now() - interval '24 hours'
            """)[0][0])
            if n > 12:
                bad.append(f"近 24h 简报 {n} 条 > 收盘标签上限（去重失效）")
            else:
                obs.append(f"近24h 简报 {n} 条")
    except Exception as e:  # noqa: BLE001
        return False, label, f"库内核查失败 {type(e).__name__}", str(e)[:200]
    return (not bad), label, " ".join(obs), (
        "" if not bad else "；".join(bad) + " → 见 CL-20261110-21")


def _c_v5x_hourlybrief_wired(c: Ctx) -> tuple[bool, str, str, str]:
    """CL-20261111-01：每小时状态简报必须真接线（函数定义 + scheduler 调用 + DB 行）。

    防「函数写了但从没被调度」「scheduler 没注册作业」「DB 字典缺行导致 resync 不认」三类复发。
    """
    import ast  # noqa: PLC0415

    label = "每小时状态简报真接线（函数/scheduler/DB 行齐全）"
    root = Path(__file__).resolve().parents[2]
    bad, obs = [], []
    try:
        sch = ast.parse((root / "app" / "scheduler.py").read_text(
            encoding="utf-8", errors="ignore"))
        job = next((n for n in ast.walk(sch) if isinstance(n, ast.FunctionDef)
                    and n.name == "_v5x_hourly_brief_job"), None)
        if job is None or not any(
                isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "push_periodic_brief" for n in ast.walk(job)):
            bad.append("_v5x_hourly_brief_job 未真实调用 push_periodic_brief")
        else:
            obs.append("scheduler 真接线")
        def _addjob_has_id(node):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "add_job"):
                return False
            for a in node.args:
                if isinstance(a, ast.Constant) and a.value == "v5x_hourly_brief":
                    return True
            for kw in node.keywords:
                if kw.arg == "id" and isinstance(kw.value, ast.Constant) \
                        and kw.value.value == "v5x_hourly_brief":
                    return True
            return False
        if not any(_addjob_has_id(n) for n in ast.walk(sch)):
            bad.append("sched.add_job 未注册 id=v5x_hourly_brief")
        else:
            obs.append("作业已注册")
        bp = root / "app" / "strategies" / "v5x" / "brief.py"
        br = ast.parse(bp.read_text(encoding="utf-8", errors="ignore"))
        br_defs = {n.name for n in ast.walk(br) if isinstance(n, ast.FunctionDef)}
        if "push_periodic_brief" not in br_defs:
            bad.append("brief.py 未定义 push_periodic_brief")
        else:
            obs.append("brief.py 已定义")
    except Exception as e:  # noqa: BLE001
        return False, label, f"源码读取失败 {type(e).__name__}", str(e)[:200]
    try:
        row = c.rows("SELECT enabled FROM ops.scheduled_task "
                     "WHERE task_id='v5x_hourly_brief'")
        if not row:
            bad.append("ops.scheduled_task 无 v5x_hourly_brief 行")
        elif not row[0][0]:
            bad.append("v5x_hourly_brief 行 enabled=f")
        else:
            obs.append("DB 行存在且 enabled")
    except Exception as e:  # noqa: BLE001
        return False, label, f"库内核查失败 {type(e).__name__}", str(e)[:200]
    return (not bad), label, " ".join(obs), (
        "" if not bad else "；".join(bad) + " → 见 CL-20261111-01")


INVARIANTS: list[tuple[str, str, str, Callable[[Ctx], tuple]]] = [
    # CL-20261108-23：2026-11-06 单日 206 条运维告警击穿 pushplus 额度，
    # 把共用 token 的交易信号一起打成 60 小时静默。防「恢复逐条发送」与「休市误报」复发。
    ("OPS-PUSH-QUOTA", "告警日配额与单轮 digest（不挤爆信号通道）",
     "block", _c_ops_push_quota),
    # CL-20261109-18：fusion_push_log 于 2026-11-09 DROP（推送源唯一化为 V5X 后它只剩
    # 「监控误报源」这一个作用）。删表本身很容易，难的是「不许有人再查它」和
    # 「不许顺手把推送活性判据一起删掉」——两条后果都是沉默。
    ("PUSH-LOG-RETIRED", "已退役的 fusion_push_log 无残留（库/代码/监控判据）",
     "block", _c_push_log_retired),
    # CL-20261109-01：11-09 00:00 那条「日线滞后 3 天」是自然日差在周末被顶到阈值，
    # 数据实际零滞后。防「判据回退成自然日差」与「真源退回已死的 l3_ref.trade_calendar」。
    ("OPS-DATA-LAG-TRADE-DAY", "日线滞后按交易日计且真源为 futures_rule",
     "block", _c_ops_data_lag_trade_day),
    # CL-20261109-02：15 张表共用自然日判据时，国庆/春节长假会让它们同时越线。
    # 防「每张表都显式分类」被漏掉（漏分类会静默落 natural，长假立刻误报）。
    ("OPS-FRESH-TRADE-DAY", "新鲜度按交易日/时间戳分类且阈值真接进判定",
     "block", _c_ops_fresh_trade_day),
    # CL-20261108-18：恢复推送盈亏金额后，防止功能再下线 / 契约测试再僵尸化。
    ("OPS-PUSH-PNL-WIRED", "推送盈亏金额已接线且契约测试真实通过（CL-20261108-18）",
     "block", _c_ops_push_pnl_wired),
    # P2-4 E3：CI 门禁复活 + data_same_source / signal_parity 真实接线并执行。
    ("CI-SAME-SOURCE-WIRED", "CI 同源断言已接线且真实通过（E3 / P2-4）",
     "block", _c_ci_same_source_wired),
    # P2-3 E1-A/E1-B：6 处分叉用声明式白名单钉住，且断言逐项核实「声明仍成立」。
    # 特别防「参数默认值被改了但声明表没更新」——E1 结论全部建立在实测数值上。
    ("BT-EXEC-FORK-DECLARED", "执行分叉声明契约与当前行为一致（E1-A/E1-B）",
     "block", _c_bt_exec_fork_declared),
    ("VAR-WHITELIST-NONEMPTY", "白名单表非空且有效", "block", _c_whitelist_nonempty),
    ("VAR-SIGNAL-IN-WHITELIST", "融合信号品种 ⊆ 白名单", "block", _c_signal_in_whitelist),
    # info 级：cloud.yaml 是**采集口径**（归档定案：L0/日线对所有品种生效，
    # 不因不交易而停采），所以允许是超集。信号池收口由 VAR-SIGNAL-IN-WHITELIST 保证。
    ("VAR-CONFIG-IN-WHITELIST", "cloud.yaml 主连（采集口径，允许超集）", "info", _c_config_in_whitelist),
    ("VAR-WHITELIST-IN-CONFIG", "白名单 ⊆ cloud.yaml 主连", "block", _c_whitelist_in_config),
    ("VAR-L1-IN-WHITELIST", "L1 主连新数据 ⊆ 白名单 ∪ 持仓豁免", "warn", _c_l1_in_whitelist),
    # CL-20261110-03：上面那条只匹配 '888$'，15 个口径外 8888 指数连
    # 在口径生效后持续派生却完全看不见。指数层收窄后单列 block 断言锁住。
    ("VAR-INDEX-IN-SCOPE", "L1 指数层新数据 ⊆ 口径", "block", _c_index_in_scope),
    # 直接针对 2026-11-07 事故根因：白名单落库 38 行，但 L1/L2/视图**零引用**。
    # 「建了表却没人读」= 口径永远不生效，且不报错，所以必须 block。
    ("VAR-SCOPE-WIRED", "口径被 L1/L2/视图真实引用", "block", _c_scope_wired),
    ("VAR-SCOPE-VIEW-OK", "口径视图存在、非空、覆盖白名单", "block", _c_scope_view_healthy),
    ("DATA-L0-LAYERS-FRESH", "L0 各 layer 未断层", "block", _c_l0_layers_fresh),
    # CL-20261110-01：上面那条只看 max(ts)，45 个 8888 品种全天 1 根也会被 8 个
    # 跑到 23:00 的品种「代表」成 PASS。补上覆盖广度维度，专治「只看有数据的 symbol」。
    ("DATA-L0-INDEX-COVERAGE", "index 层覆盖广度未塌陷", "block", _c_l0_index_coverage),
    # CL-20261107-11：11-02/11-03 的历史指数洞（25/65 品种）在基准日滚动后
    # 逃出上面那条断言的视野，换月 ratio 门静默关闭、漏检 7 个真实换月。
    # 洞不会自愈，必须逐交易日盯住最近 6 个已收盘日。
    ("DATA-L0-INDEX-HIST-COVERAGE", "近 6 个已收盘交易日指数层无历史洞",
     "block", _c_l0_index_hist_coverage),
    # CL-20261110-02：跨周期对拍只查 L1，而 V5X 读 BA；非网格在途桶与
    # BA 孤儿桶两层都没人看。必须同时断言标签合法性与 BA ⊆ L1。
    ("BAR-GRID-VALID", "L1/L2 桶标签合法且 BA ⊆ L1", "block", _c_bar_grid_valid),
    # CL-20261110-04：负价是加法后复权的数学必然，不该「修数据」；
    # 要锁的是 V5X 真实价反解别被改回去，以及负价不渗进主连原始层。
    ("BA-NONPOSITIVE-GUARDED", "非正价仅在复权层且 V5X 已反解", "block",
     _c_ba_nonpositive_guarded),
    ("DATA-CALENDAR-FRESH", "官方交易日历覆盖到最新数据日", "block", _c_calendar_fresh),
    # CL-20261111-03 / P0-1：遗留 l3_ref.trade_calendar(INF) 止于 2026-10-08，但生产逻辑
    # 早已改读 futures_rule 真源。锁住真源新鲜度（若 seed_futures_rule 停跑会静默失能）。
    ("CAL-FUTURES-RULE-FRESH", "官方日历真源 futures_rule 覆盖到近 3 日",
     "block", _c_futures_rule_fresh),
    # CL-20261111-03 / P1：L2 后复权只处理主连 888，8888 指数连本就不进 BA（设计）。
    # 钉死「L2 不得含 8888」，防未来有人把指数误塞进后复权层。
    ("VAR-L2-NO-INDEX", "L2 后复权层不含任何 8888 指数符号", "block", _c_l2_no_index),
    # CL-20261111-03 / P0-2：8888 指数负价是后复权设计产物（接缝连续、仅 delta 消费），
    # 不可重算为正。真正故障模式是 seam delta 算错导致接缝跳变数千点。钉死接缝连续性。
    ("DATA-INDEX-8888-SEAM", "8888 指数层相邻接缝连续（缺口≤2日且跳变≤2000点）",
     "block", _c_index_8888_seam),
    # CL-20261109-04：休市期 akshare 会给「时间戳=当前时刻」的合成 bar，
    # 采集作业无时段门 → 周一凌晨写入 11 根幽灵 bar，一路进 V5X 信号源且不报错。
    ("L0-NO-OFFHOURS-BAR", "l0 无时间戳落在非交易时段的 bar", "block",
     _c_l0_no_offhours_bar),
    ("MINUTE-COLLECT-WINDOW-GATED", "采集作业与采集器都走交易时段门", "block",
     _c_minute_collect_window_gated),
    # warn 而非 block：录权益是运营动作，且调度器已有每日一条 pushplus 提醒，
    # 守卫再报 block 只会重复轰炸。挂账后变 WAIVED，录满 20 天自动转 PASS。
    ("DATA-EQUITY-FRESH", "组合权益流新鲜", "warn", _c_equity_fresh),
    ("OPS-SWITCH-JOB-CONSIST", "开关表与任务字典同向", "block", _c_switch_job_consistency),
    ("OPS-SIGNAL-WRITER-UNIQUE", "融合信号单一写入方", "warn", _c_signal_writer_unique),
    # CL-20261109-21：从「三个周度作业 != 6」扩展到全部带星期位的 apscheduler
    # 作业真实触发星期比对；`1-5` 这类范围错位此前完全没被覆盖，直接让 inventory
    # 每个周一停跑。升级 block：这类错误必然静默断供，不能只当告警。
    ("OPS-WEEKLY-DOW", "DB cron 真实触发星期与代码意图一致", "block", _c_weekly_dow),
    # CL-20261110-10 A：周频改期造成 13 天合法空档被固定 9 天阈值误报成「链路停摆」。
    # 判据换成 cron 感知后，必须用行为对拍锁住「改期不误报 + 连续漏跑仍告警 + 真实接线」。
    ("OPS-JOBHB-CRON-AWARE", "周/月频心跳按 cron 推算应触发且判据真实接线", "block",
     _c_jobhb_cron_aware),
    # CL-20261110-14 C：/var/log 硬编码靠 sudo 维持，且 psql 无 ON_ERROR_STOP 会假绿。
    ("OPS-COMPRESS-LOG-PATH", "压缩脚本日志落在项目可写目录且失败可冒泡", "block",
     _c_compress_log_path),
    # CL-20261110-15：11-09 roll_yield 0 行假绿 + f_atr_pctile 逐日 0 行巡检盲区。
    ("OPS-ROLL-YIELD-ZERO-LOUD", "roll_yield 整交易日零写入必须非零退出且调度已接线",
     "block", _c_roll_yield_zero_loud),
    ("OPS-FACTOR-FRESHNESS-COVERAGE", "因子巡检按交易日出覆盖判据且补算链路真实接线",
     "block", _c_factor_freshness_coverage),
    # CL-20261110-12：11-06 DCE 11 个合约数值腰斩，品种数型监控全 PASS。
    ("OPS-MPR-DCE-CONTRACT-CONTINUITY", "DCE 会员持仓逐合约规模连续（无腰斩/暴涨）",
     "block", _c_mpr_dce_contract_continuity),
    ("OPS-DAILY-REFRESH-WIRED", "日频刷新链接通且失败冒泡", "block",
     _c_daily_refresh_wired),
    ("OPS-MEM-CALIBER", "内存告警读 cgroup 限额（非宿主全局口径）", "block", _c_mem_caliber),
    ("OPS-LSTM-SNAPSHOT-WRITE", "LSTM 快照共享卷可写且原子写已启用", "warn", _c_lstm_snapshot_write),
    # warn 而非 block：重跑期间最新 run 天然是半成品，靠「活跃写入 <2h」放行；
    # 只有停滞超过 2h 才报，正好抓住 CL-20261108-02 的容器重启误杀。
    ("OPS-BT-RUN-COMPLETE", "最新回测 run 完整或仍在活跃写入", "warn", _c_bt_run_complete),
    # CL-15 之前挂 high 却零断言覆盖：算法一旦回退或重跑错归，没有任何告警。
    # 一条防 SP 回退、一条盯当前主表状态，都是「此刻可判」，不靠时间衰减。
    ("L2-ROLL-GAP-AWARE", "L2 换月映射跨休市感知（055 未回退）", "block", _c_roll_gap_aware),
    ("L2-ROLL-GAP-DATA", "PF888 夜盘换月段位置与后复权偏移", "block", _c_roll_gap_data),
    ("L2-POSITIVITY-SHORTCIRCUIT", "min5 shift 默认路径短路（056 未回退）", "block",
     _c_roll_positivity_shortcircuit),
    # P2-1：审计报告 §4④「三套参数口径并存」的防漂移断言。此前无任何断言在盯
    # 「回测的参数是不是生产在跑的参数」，静默调参 / 静默覆盖都无人察觉。
    ("OPS-FUSION-PROFILE-PINNED", "实际生效 fusion 参数与已登记 V3.4 基线一致",
     "block", _c_fusion_profile_pinned),
    # P2-2：把「哪次启动 / 哪条信号用的是哪套参数」变成机器可判。
    # 特别防「加了列没人填」——qhyc 历史上最隐蔽的失效模式。
    ("OPS-CONFIG-SNAPSHOT-FRESH", "启动快照与信号流水绑定当前生效 params_hash",
     "block", _c_config_snapshot_fresh),
    # P2-3 前置：成交口径是回测结论的地基。此前 close 模式硬编码、无任何断言盯它，
    # 「改成下一根开盘」或「默认被悄悄改掉」都不会有人发现。行为级对拍而非只查源码。
    ("BT-FILL-MODE-WIRED", "回测成交口径 fill_mode 真实接线（默认 next_open +1tick）",
     "block", _c_bt_fill_mode_wired),
    # E1-C / CL-20261108-15：0.5 ATR 不利跳空过滤（语义 B 整段作废）。
    # 行为级合成 K 对拍 + AST 真实引用 + 活跃品种 tick_size 覆盖，
    # 防「定义了没人用」「静默丢滑点」「退化成延后一根」三种假绿。
    ("BT-GAP-FILTER-WIRED", "E1-C 跳空不利过滤（0.5 ATR / 语义 B）真实接线",
     "block", _c_bt_gap_filter_wired),
    # C2b / CL-20261109-05：V5X 回测原为「同根收盘」，与 fusion 的 E1-C 差一整个
    # 成交假设，却没有任何东西阻止别人拿 +273,705 去和 V3.4 比。断言同时钉
    # 「默认口径」「真实接线」「close 零回归」「next_open 确实落在下一根开盘」。
    ("BT-V5X-FILL-MODE-WIRED", "V5X 回测成交口径接 E1-C（默认 next_open / close 零回归）",
     "block", _c_bt_v5x_fill_mode),
    # CL-20261109-10：P6 会话末根集合被历史并集污染，9:00/13:00 被当成末根，
    # SF/SM 十年零成交，且实盘/回测随载入窗口长度分叉。
    ("V5X-P6-SESSIONS", "P6 会话末根取常态末根（非历史并集）", "block",
     _c_v5x_p6_sessions),
    # CL-20261109-08：V5X 只推事件不推持仓，用户漏看一条 CLOSE 就彻底失联；
    # 首份人工清单 5 个仓抄反了 4 个方向，所以「方向标签」也进断言。
    ("V5X-SNAPSHOT-WIRED", "理论持仓快照真实接线且方向标签正确", "block",
     _c_v5x_snapshot_wired),
    # CL-20261109-17：用户要求「60m 收盘 +10s 扫描推送」。四个接缝都可静默断掉
    # （标签不同源 / 没注册 / 字典被写成 5 段 cron / 半截桶门失效），且表现都是
    # 「照常运行就是不推」。这条把它们钉在一处。
    ("V5X-CLOSE-SCAN-WIRED", "V5X 收盘即时扫描链接线（标签同源/注册/字典惰性/窗口语义）",
     "block", _c_v5x_close_scan_wired),
    # CL-20261109-07：ops.scheduled_task.schedule 会覆盖代码 CronTrigger（字典赢）。
    # fusion_scan 字典 1,31 vs 代码 5,35；v5x_scan 靠 executor='manual'+'unknown'
    # 两条巧合才保住节奏 —— 纯属幸存，必须变成断言。
    ("OPS-SCHEDULE-PARITY", "调度频率单一真相（字典 == 代码）", "block",
     _c_schedule_parity),
    # CL-20261108-09：YamlConfig 一度没有 strategies 字段，params_for 恒返回 {}，
    # 而 scheduler 的注释写着「写了就会覆盖」——又一次「口径只存在注释里」。
    ("CFG-STRATEGY-OVERRIDE-WIRED", "策略 config 覆盖层真实生效（strategies.<key>.params）",
     "block", _c_cfg_strategy_override),
    # CL-20261108-06：换月映射有 SQL / Python 两份实现，历史上三次「改一边忘另一边」。
    # 063 首次实施还从 055 文本重建、把 056 的 shift 短路一起覆盖掉（166s/品种、
    # 重新踩 OOM 路径）。两条断言：双真源一致 + 数据层无假跳空。
    ("ROLL-FINER-ANCHOR-WIRED", "min5 锚点取桶内第一根（SQL+056 短路 / Python 双真源）",
     "block", _c_roll_finer_anchor_wired),
    # 2026-11-09 起判据换成精确恒等式：段首那根的 (Δba − Δraw) 必须正好等于 −roll_delta。
    # 原阈值法（|Δba|>50 且 raw 未跳）会把「缺口落在段首 bar 的 open 里」的
    # 正确形态误判成假跳空（OI888@2012-11-02、SF888@2016-09-28 两根即此类）。
    ("ROLL-MIN5-NO-FAKE-JUMP", "5m 换月 delta 精确落在段首（无假跳空）",
     "block", _c_roll_min5_no_fake_jump),
    # CL-20261108-05：四个频率各自建段，历史上不同频率是在不同版本的检测代码下
    # 生成的，"min5 比 min15 少几段"到底是合法的分辨率合并还是丢事件，肉眼分不出。
    # 用 Σroll_delta 守恒 + 段数单调两条恒等式把"同源"钉住（廉价的聚合，不是重算）。
    # CL-20261109-14：旧网格（time_bucket 左沿）内部自洽，跨周期对拍抓不到，
    # 只能直接检 bucket 墙钟。这是「口径已统一」唯一可验证的判据。
    ("L1-SESSION-GRID", "白名单 2015+ L1 bucket 全落在会话网格（旧网格残留=0）",
     "block", _c_l1_session_grid),
    ("ROLL-FREQ-PARITY", "四频换月段同源（Σroll_delta 跨频相等 + 段数单调）",
     "block", _c_roll_freq_parity),
    # CL-20261109-12：增量重建只补新桶，l1 历史被修订后 ba 会静默保留错的 close
    # （adj_offset 却仍正确）。2026-11-09 252/252 对拍才发现 37/38 品种在
    # 2026-10-21~10-28 违约、60m 最大 215 点。这条断言把"静默错位"变成红灯。
    ("BA-PRICE-IDENTITY", "后复权行与原始价自洽（ba.close - adj_offset == raw.close）",
     "block", _c_ba_price_identity),
    # CL-20261109-13：2026-11-09 一天之内两次「拿旧迁移作基线 CREATE OR REPLACE」——
    # 063 覆盖 056、065 覆盖 053。每次只丢一个 token，而既有断言各自只盯自己那一个，
    # 正好漏掉被覆盖的那一方。这条按「迁移来源」逐 token 追责，缺了直接报哪个迁移被回退。
    ("MIG-PROC-BASELINE", "关键过程未被旧基线覆盖（迁移 token 集合完整）",
     "block", _c_mig_proc_baseline),
    # P2-3 E2：walkforward 此前用 HourlyBar 查范围，src='bar_60m' 时 0 行 →
    # run_walk_forward 默认调用静默「无数据」。行为级同源对拍，防第二条读取路径复活。
    ("BT-WALKFORWARD-SAME-SOURCE", "walk-forward 日期范围取自唯一 K 线真源",
     "block", _c_bt_walkforward_same_source),
    # CL-20261108-12 / 用户 19:48 拍板：38 白名单是回测与交易池唯一口径。
    # 行为级拒绝非白名单 + AST 真实引用 + 持仓豁免仍在数据链路，三面防漂移。
    ("BT-POOL-IN-WHITELIST", "回测品种池严格 38 白名单（持仓豁免只留数据链路）",
     "block", _c_bt_pool_in_whitelist),
    # P2-5 / CL-20261108-19：B1 执行惩罚（路线图 §3.3 第 2/3/4 项）。
    # 行为级对拍（盘中击穿/一字K/保证金/未收盘K）+ off 逐位零回归 + AST 引用，
    # 防「字段建了没人读」「盘中击穿静默失效」「低波动被误判成停板」三种假绿。
    ("BT-B1-PENALTY-WIRED", "B1 执行惩罚真实接线（盘中击穿/一字K/保证金/未收盘K）",
     "block", _c_bt_b1_penalty_wired),
    # A1 / CL-20261108-20：`member_net_z` 是**横截面** z（品种间排序），却被当
    # 单品种择时门用，实测 19/38 品种 |z|>=1 占比为 0%（静默 100% 误杀）。
    # 断言同时验「两种 z 真不是一回事」（含无前视）与「时序 z 已注册并有数据」。
    ("FACTOR-Z-TS-DISTINCT", "横截面 z 与时序 z 语义可辨且时序 z 已落库",
     "block", _c_factor_z_ts_distinct),
    # A2 / CL-20261108-21：后复权价不是可成交价（JD888 实测 ba.close=-462）。
    # 断言用合成 K 对拍「换月假跳空被消除 + 成交价取真实价」，防静默失真。
    ("BT-BA-PRICE-SPACE", "后复权维度的信号空间与真实价空间已分离",
     "block", _c_bt_ba_price_space),
    # C1 / CL-20261108-22：V5X.3 引擎移植。价值全在「逐位复现冻结版」，
    # 参数或门控一次静默改动就让历史数字不可比，故用行为级门控对拍钉住。
    ("V5X-ENGINE-PARITY", "V5X.3 引擎默认值=冻结参数且五道门控仍生效",
     "block", _c_v5x_engine_parity),
    # C3 / CL-20261108-08：小时级推送链路。两条断言分别钉「口径」与「接线+反解」：
    # t-1 偷看当日会让回测好看但实盘做不到；接线断了或忘反解则推的是负价。
    ("V5X-T1-GATE-NO-LEAK", "V5X t-1 门控不得使用当日日线",
     "block", _c_v5x_t1_gate_no_leak),
    ("V5X-LIVE-WIRED", "V5X 小时扫描已接线且出口价已反解",
     "block", _c_v5x_live_wired),
    # CL-20261108-25：BA 是等差后复权，只对差值平移不变。日频派生（振幅率、
    # 对数收益）若留在 BA 空间，负价处会符号翻转 / 除零，且全程不报错。
    ("BA-REAL-PRICE-DERIVED", "BA 空间的日频派生已回真实价",
     "block", _c_ba_real_price_derived),
    # CL-20261108-24：loguru 只认 {}。写成 %s 会原样打印占位符并丢参数，
    # 事故排查时最需要的信息恰好丢失。只查 loguru 调用方（标准库 %s 是对的）。
    ("LOGURU-FMT-BRACES", "loguru 调用不得使用 % 占位符",
     "block", _c_loguru_fmt_braces),
    # CL-20261108-26：持仓同步此前是「断链 + 一句谎话」——apply_fill 零调用方，
    # bridge_report 的注释却声称持仓已被维护。收口成唯一入口并用开关关掉；
    # 这里钉住「只能有一个入口」与「开关行真的在表里」。
    ("POSITION-SYNC-SINGLE-ENTRY", "写实盘持仓只有 position_sync 一个业务入口",
     "block", _c_position_sync_single_entry),
    ("POSITION-SYNC-SWITCH-DECLARED", "持仓同步/策略单源开关已登记（区分『故意关』与『行不存在』）",
     "block", _c_position_sync_switch_declared),
    # CL-20261108-27：V3.4 停用、V5X.3 接唯一推送源。两个开关同时开是最危险状态：
    # 微信里会同时出现「开多」和「平空」，而两边都自认为对。
    ("STRATEGY-SINGLE-PUSH-SOURCE", "V3.4 与 V5X.3 不得同时作为生产推送源",
     "block", _c_strategy_single_push_source),
    # CL-20261108-28：V5 统计起点（2026-11-05 夜盘）必须落在库里。
    # warn 而非 block：回放是一次性数据动作，清库重来不该把守卫打成永久红。
    ("V5X-REPLAY-SEEDED", "V5X 回放补录已落库（source 列 + replay 行）",
     "warn", _c_v5x_replay_seeded),
    # CL-20261109-06：V5X 是唯一推送源，通道退化 = 策略不可用。
    ("V5X-PUSH-FALLBACK-WIRED", "V5X 推送带备用通道且失败可补发",
     "block", _c_v5x_push_channel),
    ("V5X-PUSH-FAILURE-LOUD", "V5X 推送失败必须升级为任务失败",
     "block", _c_v5x_push_failure_loud),
    # 配置态：空值不阻断发布，但要让「单通道」在每轮报告里持续可见。
    ("V5X-PUSH-SINGLE-CHANNEL", "推送有降级通道（fallback_webhook 非空）",
     "warn", _c_v5x_push_single_channel),
    # CL-20261110-05：11-09 总行数 -26% 只有 DCE 一个所塌陷，旧新鲜度断言
    # 被「其它三所有数据」掩盖。按交易所各自中位数比，才看得见单所静默断档。
    ("OPS-MPR-EXCHANGE-COVERAGE", "各交易所持仓排名覆盖未塌陷",
     "warn", _c_mpr_exchange_coverage),
    # CL-20261110-07：节后首日（10-08）被星期几众数误判成缺口 → 补进合成脏 bar。
    ("BACKFILL-GAP-CALENDAR-WIRED", "5m 缺口探测接交易日历（防节后首日假阳性）",
     "block", _c_backfill_gap_calendar_wired),
    # CL-20261110-11 / 用户 18:16 拍板①：DCE 官网瑞数 412 时改用新浪单合约补齐。
    # 源码级锁「按名次对齐 + 禁批量清单 + 三道质量门 + 真实接线」，
    # 数据级锁「近 30 天逐日品种覆盖」，防止再出现 11-09 那种单所静默断档。
    ("OPS-DCE-SINA-WIRED", "DCE 新浪单合约通道真实接线且对齐口径正确",
     "block", _c_dce_sina_wired),
    ("OPS-MPR-DCE-COVERAGE", "DCE 会员持仓排名逐日品种覆盖未塌陷",
     "block", _c_mpr_dce_coverage),
    # CL-20261110-08：明细补数 → summary 55 品种，但 18:00 B 组作业已跑完，
    # 因子层停在 41 品种；因子巡检只看整表 max(trade_date)，永远等不到重算。
    # 逐日比对 summary 与 5 个会员类因子的品种数，把「补数后忘记重算」变成可判定的当前状态。
    ("OPS-FACTOR-MPR-COVERAGE", "会员类因子逐日品种覆盖与 summary 一致",
     "block", _c_factor_mpr_coverage),
    # CL-20261110-16：f_amt_conc 因 X888 主力连续 amount 全 NULL 而结构性不可算，
    # 正式退役。锁「代码真接线 + registry 标记 + 历史不删」，防只改注释或删数据蒙混。
    ("OPS-FACTOR-RETIRED-CONSIST", "退役因子在巡检中真接线且历史数据保留",
     "block", _c_factor_retired_consist),
    # CL-20261110-17：f_vwap_dev 有值但语义失真（amount=0 退化成 cl/atr*100），
    # 逐日覆盖类巡检看不见 → 必须锁源码口径 + 数值量级。
    ("OPS-VWAP-CALIBER-WIRED", "f_vwap_dev 成交量加权典型价口径真接线且数值未退化",
     "block", _c_vwap_caliber_wired),
    # CL-20261110-13：作业在跑、流水没写 ≠ 停摆。置 NULL 必须声明真实状态源，
    # 且该来源（job_stats / backtest_result）当前真的健康。
    ("OPS-TASK-STATUS-SOURCE", "非流水监控作业声明真实状态源且来源健康",
     "block", _c_task_status_source),
    ("OPS-REAPED-ABORT-ONLY", "aborted 仅为重启自愈遗留且无僵尸 running",
     "block", _c_reaped_abort_only),
    # CL-20261110-20：告警刷屏（20 条/日重复 digest）与 jobhb 周频误报，
    # 两条都让交易信号被埋；git 一致性检查在容器内误跑还会推假告警。
    ("OPS-ALERT-DIGEST-DEDUP", "告警汇总按状态指纹去重，不是每 30 分钟重推",
     "block", _c_alert_digest_dedup),
    ("OPS-JOBHB-STATUS-SOURCE", "心跳阈值/状态源以 ops.scheduled_task 为单一真相",
     "block", _c_jobhb_status_source),
    ("OPS-GIT-SYNC-ENV-GUARD", "一致性检查有宿主环境自检，容器内误跑不推假告警",
     "block", _c_git_sync_env_guard),
    # CL-20261110-21：无信号的收盘根原本完全静默，用户看不到任何推送。
    ("OPS-V5X-BRIEF-WIRED", "收盘链真接线 V5X 状态简报且去重/静默齐全",
     "block", _c_v5x_brief_wired),
    # CL-20261111-01：每小时状态简报须真接线（函数/scheduler 注册/DB 字典行齐全）。
    ("OPS-V5X-HOURLYBRIEF-WIRED", "每小时状态简报真接线（函数/scheduler/DB 行齐全）",
     "block", _c_v5x_hourlybrief_wired),
]


def _load_waivers(session) -> dict[str, str]:
    """key -> change_id（同一 key 多条时取最新）。"""
    rows = session.execute(text(f"""
        SELECT change_id, invariant_keys FROM ops.change_log
        WHERE status IN ({','.join("'%s'" % s for s in OPEN_STATUS)})
          AND invariant_keys IS NOT NULL AND invariant_keys <> ''
        ORDER BY created_at
    """)).fetchall()
    out: dict[str, str] = {}
    for cid, keys in rows:
        for k in re.split(r"[,\s]+", keys.strip()):
            if k:
                out[k] = cid
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="qhyc 不变量守卫")
    ap.add_argument("--json", action="store_true", help="JSON 输出")
    ap.add_argument("--record", action="store_true", help="结果写入 ops.invariant_check")
    ap.add_argument("--alert", action="store_true", help="block 级 FAIL 时推送")
    ap.add_argument("--only", default="", help="只跑 key 前缀匹配")
    ap.add_argument("--no-waiver", action="store_true", help="验收模式：忽略台账豁免")
    args = ap.parse_args()

    run_id = f"{datetime.now():%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:6]}"
    results: list[Result] = []
    try:
        with session_scope() as s:
            ctx = Ctx(session=s)
            ctx.whitelist = {r[0] for r in ctx.rows(WHITELIST_SQL)}
            try:
                from app.core.config import get_settings
                ctx.cfg_products = {
                    re.sub(r"888$", "", m.symbol)
                    for m in get_settings().main_contracts
                }
            except Exception as e:  # noqa: BLE001
                results.append(Result(
                    "VAR-CONFIG-LOAD", "读取 cloud.yaml 主连", "block", False,
                    "配置可读", f"异常 {type(e).__name__}", str(e)[:200]))
            waivers = {} if args.no_waiver else _load_waivers(s)

            for key, title, severity, fn in INVARIANTS:
                if args.only and not key.startswith(args.only):
                    continue
                try:
                    ok, exp, obs, detail = fn(ctx)
                except Exception as e:  # noqa: BLE001
                    s.rollback()
                    ok, exp, obs, detail = False, "执行不报错", f"{type(e).__name__}: {e}"[:200], ""
                    severity = "block"
                r = Result(key, title, severity, ok, exp, obs, detail)
                if not ok and key in waivers:
                    r.status = "WAIVED"
                    r.waiver_ref = waivers[key]
                elif ok:
                    r.status = "PASS"
                else:
                    r.status = "FAIL"
                results.append(r)

            if args.record:
                for r in results:
                    s.execute(text("""
                        INSERT INTO ops.invariant_check
                            (run_id, check_key, title, severity, status,
                             expected, observed, detail, waiver_ref)
                        VALUES (:rid, :k, :t, :sev, :st, :exp, :obs, :det, :wr)
                    """), {"rid": run_id, "k": r.key, "t": r.title, "sev": r.severity,
                           "st": r.status, "exp": r.expected, "obs": r.observed,
                           "det": r.detail[:1000], "wr": r.waiver_ref or None})
                s.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[guard] 运行失败：{type(e).__name__}: {e}", file=sys.stderr)
        return 2

    real_fail = [r for r in results if r.status == "FAIL"]
    blocked = [r for r in real_fail if r.severity == "block"]
    waived = [r for r in results if r.status == "WAIVED"]

    if args.json:
        print(json.dumps({
            "run_id": run_id,
            "checked_at": datetime.now().isoformat(timespec="seconds"),
            "total": len(results),
            "pass": sum(1 for r in results if r.status == "PASS"),
            "fail": len(real_fail), "waived": len(waived),
            "exit_code": 1 if blocked else 0,
            "results": [r.__dict__ for r in results],
        }, ensure_ascii=False, indent=2))
    else:
        icon = {"PASS": "✅", "FAIL": "❌", "WAIVED": "🟡", "SKIP": "⏭"}
        print(f"\nqhyc 不变量守卫  run={run_id}")
        print("=" * 96)
        print(f"{'状态':<4} {'断言':<34} {'实测':<28} 依据/说明")
        print("-" * 96)
        for r in results:
            # 分级显示（2026-11-09）：
            #   block 级 FAIL → ❌（拦发布）
            #   warn  级 FAIL → ⚠️（要处理但不拦）
            #   info  级 FAIL → ℹ️（设计如此，例如「配置允许是白名单超集」）
            # 三者混成一个 ❌ 会让人以为守卫坏了，也会让「FAIL 数」永远降不到 0。
            if r.status == "FAIL":
                ic = {"block": "❌", "info": "ℹ️"}.get(r.severity, "⚠️")
            else:
                ic = icon.get(r.status, "?")
            print(f"{ic} {r.key:<32} {r.observed[:26]:<28} "
                  f"{r.waiver_ref or ''}")
            if r.detail and r.status != "PASS":
                print(f"     └ {r.detail[:150]}")
        print("-" * 96)
        n_info = sum(1 for r in real_fail if r.severity == "info")
        n_warn_fail = sum(1 for r in real_fail
                          if r.severity not in ("block", "info"))
        # ★ FAIL 只数 block（2026-11-09）：原先用 len(real_fail)，把 warn/info 也算进
        #   FAIL，于是「58 PASS · FAIL 3 · 仅告警 2 · INFO 1」——3 就是那 2+1 重复计数，
        #   三项相加还等于 61，看起来自洽但含义是错的：全绿的一轮会显示 FAIL 3，读到的人
        #   只会记住「有 3 个失败」。这正是要防的「监控骗人」的镜像版本（狼来了）。
        n_block_fail = sum(1 for r in real_fail if r.severity == "block")
        tail = ""
        if n_warn_fail:
            tail += f" · 仅告警 {n_warn_fail}"
        if n_info:
            tail += f" · INFO {n_info}"
        print(f"合计 {len(results)}：PASS {sum(1 for r in results if r.status == 'PASS')}"
              f" · FAIL {n_block_fail}{tail} · WAIVED {len(waived)}")
        if waived:
            print("\n已挂账缺口（不算违约，但必须有人推进）：")
            for r in waived:
                print(f"  {r.waiver_ref} ← {r.key}：{r.detail[:110] or r.observed}")
        if blocked:
            print("\n❌ 未挂账的阻断项：" + ", ".join(r.key for r in blocked))

    if args.alert and (blocked or waived):
        try:
            from app.ops.alerting import send_alert
            lines = [f"❌ {r.key}: {r.detail or r.observed}" for r in blocked]
            lines += [f"🟡 {r.waiver_ref} {r.key}: {r.detail[:90]}" for r in waived]
            send_alert(
                f"[qhyc守卫] {'FAIL' if blocked else '欠账'} {len(blocked)}块/{len(waived)}挂账",
                "\n".join(lines)[:1500],
                key="invariant_guard",
            )
        except Exception as e:  # noqa: BLE001
            print(f"[guard] 告警失败：{e}", file=sys.stderr)

    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
