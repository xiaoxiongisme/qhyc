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
from datetime import date, datetime
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


@dataclass
class Ctx:
    session: object
    whitelist: set[str] = field(default_factory=set)
    cfg_products: set[str] = field(default_factory=set)
    _cache: dict = field(default_factory=dict)

    def scalar(self, sql: str, **params):
        # 缓存键必须含参数：按 layer 循环查同一句 SQL 时，只按 SQL 文本做键
        # 会让三个 layer 全部命中第一次的结果（曾导致 index 断更被误判为 PASS）。
        key = (sql, tuple(sorted(params.items())))
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
    """只看**近 7 天新增**信号——历史行的存在不代表当前行为。

    这是本套断言的一条通用纪律：不变量必须衡量「现在的系统怎么运行」，
    不能拿历史流水当现状（否则收口之后永远无法转 PASS，豁免就变成了永久遮羞布）。

    ★ 持仓豁免（CL-20261107-01）：扫描池允许「非白名单但有持仓」的品种继续扫描，
      否则它既不会产出平仓/止损信号，持仓会被永久冻结。所以这类品种出现在信号表里
      是**正确行为**，不是越界。不加这一项，断言会在口径正确时永远 FAIL。
    """
    rows = c.rows("""
        SELECT DISTINCT regexp_replace(symbol, '888$', '') AS v
        FROM fusion_signal_log
        WHERE symbol ~ '^[A-Za-z]+888$'
          AND signal_at >= now() - interval '7 days'
    """)
    got = {r[0] for r in rows}
    held = {r[0] for r in c.rows("""
        SELECT DISTINCT regexp_replace(symbol, '8888?$', '')
        FROM fusion_position WHERE COALESCE(position, 'FLAT') <> 'FLAT'
    """)}
    if not got:
        return True, "近 7 天信号 ⊆ 白名单 ∪ 持仓豁免", "近 7 天无新信号（真空通过）", ""
    extra = sorted(got - c.whitelist - held)
    return not extra, "近 7 天信号 ⊆ 白名单 ∪ 持仓豁免", (
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


def _c_scope_wired(c: Ctx) -> tuple[bool, str, str, str]:
    """口径**真的被引用**了吗？——直接针对本次事故的根因断言。

    2026-11-07 之前 ``l3_ref.tradable_variety`` 已落库 38 行，但 L1 派生、
    L2 复权、7 个兼容视图**没有一处引用它**。结果「白名单已生效」是一句
    没有任何证据支撑的话，四套口径继续并存。

    本断言检查三处关键路径的定义文本里是否出现口径来源标识：
      · L1 派生存储过程  → tradable_scope_array
      · L2 复权存储过程  → tradable_scope_array
      · public.bar_* 视图 → tradable_variety / tradable_filter_on
    任何一处丢失引用即 block FAIL ——「表建了却没人读」不许再静默通过。
    """
    checks = [
        ("L1 派生 sp_build_l1_from_minute",
         "SELECT pg_get_functiondef('public.sp_build_l1_from_minute(text,timestamptz,timestamptz)'::regprocedure)",
         "tradable_scope_array"),
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
    """各 layer 相对 L1 主连最新桶的落后小时数。

    ★ 只用窗口查询 + 单列聚合：2026-11-07 21:58 一次
      `SELECT layer,max(ts),count(DISTINCT symbol) FROM l0_raw.minute_bar GROUP BY 1`
      （6145 万行全表聚合）曾把生产 PostgreSQL 打到 OOM 被内核 kill。
    """
    base = c.scalar("SELECT max(bucket) FROM l1_mkt.bar_60m WHERE symbol ~ '^[A-Za-z]+888$'")
    if base is None:
        return False, "L1 主连有数据", "max(bucket)=NULL", "L1 空表，无法建立新鲜度基准"
    bad, seen = [], []
    for layer in ("continuous", "contract", "index"):
        mx = c.scalar(
            "SELECT max(ts) FROM l0_raw.minute_bar "
            "WHERE layer = :ly AND ts >= now() - interval '14 days'", ly=layer)
        if mx is None:
            bad.append(f"{layer}=窗口内无数据")
            seen.append(f"{layer}=none")
            continue
        lag = (base - mx).total_seconds() / 3600.0
        seen.append(f"{layer}={lag:.1f}h")
        if lag > 36.0:
            bad.append(f"{layer} 落后 {lag:.0f}h")
    return not bad, "各 layer 落后 L1 <= 36h", "; ".join(seen), (
        "断层：" + "; ".join(bad) if bad else "")


def _c_equity_fresh(c: Ctx) -> tuple[bool, str, str, str]:
    n = c.scalar("SELECT count(*) FROM portfolio_equity")
    if not n:
        return False, "portfolio_equity 有记录且 7 天内更新", "0 行", (
            "权益流为空 → 组合熔断恒放行（>15% 半仓 / >25% 清仓全部失效）。"
            "写入方：scripts/ops/upsert_equity.py")
    last = c.scalar("SELECT max(trade_date) FROM portfolio_equity")
    days = c.scalar("SELECT CURRENT_DATE - max(trade_date) FROM portfolio_equity")
    return days <= 7, "portfolio_equity 7 天内更新", (
        f"{n} 行，最新 {last}（{days} 天前）"), (
        "" if days <= 7 else "权益流停更 → 熔断读的是过期曲线，回撤判断失真")


# ---------------------------------------------------------------- 交易日历
def _c_calendar_fresh(c: Ctx) -> tuple[bool, str, str, str]:
    """官方交易日历必须覆盖到「已有数据的最新日」。

    CL-20261107-18：``l3_ref.trade_calendar`` 是 R2 推断表，只有 ``exchange='INF'``
    且止于 2026-10-08；``extend_index_8888.py`` 却按六家真实交易所查它，导致
    「整天零合约行」的坏日门恒空、从上线起就是死代码。真源是 ``futures_rule``
    的 distinct trade_date（akshare futures_rule 播种）+
    ``cfg_calendar_seed_meta.seeded_through``，由 ``seed_futures_rule`` 每日 08:00 刷新。

    断言只看此刻：日历覆盖上界 vs L0 continuous 层最新数据日。容差 1 天是因为
    周五夜盘会跨到周六凌晨（``ts::date`` 落在周六，但日历里周六不是交易日），
    不留这 1 天会每个周一都误报。窗口化查询，禁止对 l0_raw 全表聚合。
    """
    seeded = c.scalar(
        "SELECT value FROM cfg_calendar_seed_meta WHERE key='seeded_through'")
    cal_max = c.scalar("SELECT max(trade_date) FROM futures_rule")
    l0_day = c.scalar(
        "SELECT max(ts)::date FROM l0_raw.minute_bar "
        "WHERE layer='continuous' AND ts >= now() - interval '14 days'")
    try:
        st = date.fromisoformat(str(seeded)[:10]) if seeded else None
    except ValueError:
        st = None
    if st is None or cal_max is None or l0_day is None:
        return False, "官方日历覆盖到 L0 最新数据日", (
            f"seeded={seeded} cal_max={cal_max} l0={l0_day}"), (
            "日历真源缺失 → extend_index_8888 的坏日门无法判定，"
            "先跑 scripts/seed_futures_rule.py")
    cover = max(st, cal_max)
    lag = (l0_day - cover).days
    ok = lag <= 1
    return ok, "官方日历覆盖到 L0 最新数据日（容差 1 天跨夜盘）", (
        f"seeded={st} cal_max={cal_max} l0={l0_day} lag={lag}d"), (
        "" if ok else
        f"官方日历落后 {lag} 天 → 指数扩展的坏日门会静默失效（CL-20261107-18）")


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


def _c_weekly_dow(c: Ctx) -> tuple[bool, str, str, str]:
    """周度作业的 DB 5 段 cron 星期位不能是 6。

    apscheduler 的 CronTrigger.from_crontab 里 0=周一 … 6=周日，与标准 crontab
    错位一天。实测「周六」写成 6 会静默跑在周日（10-18/10-25/11-01 三条成功
    记录全是周日，而 11-07 周六一条没有）。
    """
    rows = c.rows("""
        SELECT task_id, schedule FROM ops.scheduled_task
        WHERE enabled AND task_id IN (
            'lstm_weekly_retrain','transmission_weekly_recalc','backtest_weekly')
    """)
    bad = []
    for tid, sch in rows:
        parts = (sch or "").split()
        if len(parts) == 5 and "6" in parts[4].split(","):
            bad.append(f"{tid}={sch}")
    return not bad, "周度作业星期位 != 6（apscheduler 里 6=周日）", (
        f"检查 {len(rows)} 个，违规 {len(bad)}"), (
        "星期语义错位：" + "; ".join(bad) if bad else "")


INVARIANTS: list[tuple[str, str, str, Callable[[Ctx], tuple]]] = [
    ("VAR-WHITELIST-NONEMPTY", "白名单表非空且有效", "block", _c_whitelist_nonempty),
    ("VAR-SIGNAL-IN-WHITELIST", "融合信号品种 ⊆ 白名单", "block", _c_signal_in_whitelist),
    # info 级：cloud.yaml 是**采集口径**（归档定案：L0/日线对所有品种生效，
    # 不因不交易而停采），所以允许是超集。信号池收口由 VAR-SIGNAL-IN-WHITELIST 保证。
    ("VAR-CONFIG-IN-WHITELIST", "cloud.yaml 主连（采集口径，允许超集）", "info", _c_config_in_whitelist),
    ("VAR-WHITELIST-IN-CONFIG", "白名单 ⊆ cloud.yaml 主连", "block", _c_whitelist_in_config),
    ("VAR-L1-IN-WHITELIST", "L1 主连新数据 ⊆ 白名单 ∪ 持仓豁免", "warn", _c_l1_in_whitelist),
    # 直接针对 2026-11-07 事故根因：白名单落库 38 行，但 L1/L2/视图**零引用**。
    # 「建了表却没人读」= 口径永远不生效，且不报错，所以必须 block。
    ("VAR-SCOPE-WIRED", "口径被 L1/L2/视图真实引用", "block", _c_scope_wired),
    ("VAR-SCOPE-VIEW-OK", "口径视图存在、非空、覆盖白名单", "block", _c_scope_view_healthy),
    ("DATA-L0-LAYERS-FRESH", "L0 各 layer 未断层", "block", _c_l0_layers_fresh),
    ("DATA-CALENDAR-FRESH", "官方交易日历覆盖到最新数据日", "block", _c_calendar_fresh),
    # warn 而非 block：录权益是运营动作，且调度器已有每日一条 pushplus 提醒，
    # 守卫再报 block 只会重复轰炸。挂账后变 WAIVED，录满 20 天自动转 PASS。
    ("DATA-EQUITY-FRESH", "组合权益流新鲜", "warn", _c_equity_fresh),
    ("OPS-SWITCH-JOB-CONSIST", "开关表与任务字典同向", "block", _c_switch_job_consistency),
    ("OPS-SIGNAL-WRITER-UNIQUE", "融合信号单一写入方", "warn", _c_signal_writer_unique),
    ("OPS-WEEKLY-DOW", "周度作业星期语义正确", "warn", _c_weekly_dow),
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
            print(f"{icon.get(r.status, '?')} {r.key:<32} {r.observed[:26]:<28} "
                  f"{r.waiver_ref or ''}")
            if r.detail and r.status != "PASS":
                print(f"     └ {r.detail[:150]}")
        print("-" * 96)
        print(f"合计 {len(results)}：PASS {sum(1 for r in results if r.status == 'PASS')}"
              f" · FAIL {len(real_fail)} · WAIVED {len(waived)}")
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
