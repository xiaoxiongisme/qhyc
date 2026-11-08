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
        elif str(got[f][1]).split(".")[0] != "320":
            bad.append(f"{f} cum_offset={got[f][1]}（应为 320）")
    if off is None or str(off).split(".")[0] != "320":
        bad.append(f"bar_60m_ba@22:00 adj_offset={off}（应为 320，断层未抹平）")
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
    try:
        px = [100.0] * (n - 1) + [130.0]
        d = pd.DataFrame({"symbol": SYM, "td": TD, "c": px,
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
                        f"hours={len(_live.SCAN_HOURS)}")
        if _live.GATE_LAG != 1:
            bad.append(f"扫描器 GATE_LAG={_live.GATE_LAG}（小时级实盘必须为 1）")
        if _live.REGIME_MODE != "strict":
            bad.append(f"扫描器 REGIME_MODE={_live.REGIME_MODE}（必须 strict 才无前视）")
        if not _live.SCAN_HOURS:
            bad.append("SCAN_HOURS 为空（等于永不扫描）")
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


INVARIANTS: list[tuple[str, str, str, Callable[[Ctx], tuple]]] = [
    # CL-20261108-23：2026-11-06 单日 206 条运维告警击穿 pushplus 额度，
    # 把共用 token 的交易信号一起打成 60 小时静默。防「恢复逐条发送」与「休市误报」复发。
    ("OPS-PUSH-QUOTA", "告警日配额与单轮 digest（不挤爆信号通道）",
     "block", _c_ops_push_quota),
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
