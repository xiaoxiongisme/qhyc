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
      · 默认 fill_mode 仍是 close（旧回测基线可复现）；
      · 合成 8 根 K 直接调 _simulate_trades，逐笔核对成交价与成交 bar 索引，
        多空两侧都验（防只改了一边）；
      · next_open 必须是「下一根开盘 ± 1tick」且成交 dt 落在下一根；
      · 未知 fill_mode 必须拒绝（防静默回退到 close）；
      · 源码里 next_open 分支必须把成本系数 slip_yuan 置 0（防滑点双计）。
        用 AST 找 Dict 节点，避免 docstring/注释里的字面量造成假阳性
        （OPS-LSTM-SNAPSHOT-WRITE 曾踩过字符串检查的坑）。
    """
    label = "回测成交口径 fill_mode 真实接线（默认 close / next_open +1tick）"
    try:
        import ast
        import pandas as pd
        from app.backtest import fusion_backtest as fb
    except Exception as exc:                              # noqa: BLE001
        return False, label, f"import 失败 {type(exc).__name__}: {exc}", (
            "融合回测模块不可导入 → 成交口径断言不可用（CL-20261108-11）")

    bad: list[str] = []
    observed: list[str] = []

    default_mode = getattr(fb.FusionBacktestParams(), "fill_mode", None)
    observed.append(f"default={default_mode!r}")
    if default_mode != "close":
        bad.append(f"默认 fill_mode={default_mode!r}，必须 'close' 否则历史基线作废")

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
        p = fb.FusionBacktestParams(fill_mode=mode, tick_size=tick_size)
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
    ("BT-FILL-MODE-WIRED", "回测成交口径 fill_mode 真实接线（默认 close / next_open +1tick）",
     "block", _c_bt_fill_mode_wired),
    # P2-3 E2：walkforward 此前用 HourlyBar 查范围，src='bar_60m' 时 0 行 →
    # run_walk_forward 默认调用静默「无数据」。行为级同源对拍，防第二条读取路径复活。
    ("BT-WALKFORWARD-SAME-SOURCE", "walk-forward 日期范围取自唯一 K 线真源",
     "block", _c_bt_walkforward_same_source),
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
