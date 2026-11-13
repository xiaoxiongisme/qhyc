# -*- coding: utf-8 -*-
"""统一运维告警（整改优先级 P1-2：作业失败 / OOM 告警）。

背景：2026-10-01 云端连续三次 OOM（cgroup CONSTRAINT_MEMCG 杀 postgres backend），
全部靠人工 `dmesg` 排查才发现；作业失败也无通知。本模块补齐三条检测线：

1. **资源类**（容器内可观测量）：cgroup 内存占用率、磁盘占用率、负载/CPU 比值；
2. **数据类**：关键表新鲜度（滞后期）、预测/推送静默、数据库进程uptime（<阈值=疑似
   崩溃重启，是越过容器边界观测 DB OOM 的**唯一可行代理指标**）；
3. **作业类**：APScheduler 事件监听，作业抛错或错过调度立即告警；
4. **作业流水类**（CL-20261112-04 B-1/B-2）：集中扫描 ``ops.scheduled_task_run`` 的
   failed/aborted，并检查 ``cron_invariant_guard`` 自身存活性。补两个静默盲区：
   心跳只盯「距上次成功」，对「一直在跑、一直失败」无感；守卫一停，95+ 条断言
   集体失声而下游监控仍然全绿。

⚠ 能力边界（必读）：容器被 OOM kill 时本进程也会消失，无法自我告警——那条线必须由
宿主侧 `scripts/ops/ops_monitor.py`（宿主 crontab）兜底。本模块负责容器内可见的部分。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

from app.core.logging import logger

CST = timezone(timedelta(hours=8))

# 冷却：同一告警键在窗口内只推送一次，避免刷屏
_LAST_SENT: dict[str, float] = {}

# 日配额计数（进程内；重启清零可接受——单次重启不会额外放大推送量）
_DAILY_BUDGET: dict = {"day": None, "used": 0}

#: 2026-11-10（CL-20261110-20）：digest 状态去重的进程内兜底。
#: 主路径是 ops.notify_dedup（跨重启有效）；表不可用时才用它，避免巡检崩溃。
_DIGEST_FALLBACK: dict = {"fingerprint": None, "sent_at": 0.0}


def _budget_take(cfg: "AlertConfig") -> bool:
    """日推送配额闸门（CL-20261112-06 P1）：DB 原子计数，跨进程/重启有效。

    旧版是进程内 dict —— 调度器一重启就归零，且只统计 ``alerting.send_alert``，
    而 11-06 事故的根源是**多来源共用 pushplus 服务端额度**。现在：
      * ``ops.alert_push_quota`` 原子 UPSERT...RETURNING 计告警用量；
      * ``ops.push_send_ledger``（send_notify 全来源留痕）逼近服务端额度时，
        告警主动退让，把余量留给交易信号。
    表不可用时退回进程内计数并记日志（告警链路绝不因计数设施故障而中断）。
    """
    today = datetime.now(CST).date()
    limit = int(getattr(cfg, "max_per_day", 0) or 0)
    if limit <= 0:
        return True
    try:
        from app.core.db import get_engine

        with get_engine().begin() as conn:
            used = conn.execute(text("""
                INSERT INTO ops.alert_push_quota (day, used, updated_at)
                VALUES (:d, 1, now())
                ON CONFLICT (day) DO UPDATE
                    SET used = ops.alert_push_quota.used + 1,
                        updated_at = now()
                RETURNING used
            """), {"d": today}).scalar()
        if int(used or 0) > limit:
            return False
        total_max = int(getattr(cfg, "max_total_per_day", 0) or 0)
        if total_max > 0:
            with get_engine().connect() as conn:
                n = conn.execute(text("""
                    SELECT count(*) FROM ops.push_send_ledger
                    WHERE (sent_at AT TIME ZONE 'Asia/Shanghai')::date =
                          (now() AT TIME ZONE 'Asia/Shanghai')::date
                """)).scalar()
            if int(n or 0) >= total_max:
                logger.warning(
                    f"[alert] 今日全来源推送已达 {n} 条（上限 {total_max}），"
                    "告警主动退让，保留 pushplus 额度给交易信号")
                return False
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[alert] DB 配额不可用({type(exc).__name__})，退回进程内计数: {exc}"[:200])
        if _DAILY_BUDGET.get("day") != today:
            _DAILY_BUDGET["day"] = today
            _DAILY_BUDGET["used"] = 0
        if _DAILY_BUDGET["used"] >= limit:
            return False
        _DAILY_BUDGET["used"] += 1
        return True


@dataclass
class AlertConfig:
    enabled: bool = True
    interval_min: int = 10             # 定时检测间隔（分钟）
    mem_pct: float = 85.0              # 容器内存占用告警阈值（%）
    disk_pct: float = 85.0             # 磁盘占用告警阈值（%）
    disk_crit_pct: float = 93.0        # 磁盘严重阈值（%）
    load_ratio: float = 1.5            # 负载/核心数 比值告警阈值
    data_lag_days: int = 3             # 数据滞后期告警阈值（**仅 natural 类**：自然日）
    # ★ 2026-11-09（CL-20261109-02）：按交易日计滞后。
    #   旧实现一律用 (今日 - 最新日期).days 比自然日阈值，国庆/春节长假会让 15 张表
    #   同时越线（11-09 00:00「日线滞后 3 天」即周末两天顶出来的假告警，
    #   ops_monitor 侧已由 CL-20261109-01 修复，此处是同源残留）。
    #   trade_day / session 类改为「缺失的交易日个数」，默认 0 = 落后 1 个交易日即报。
    data_lag_trade_days: int = 0
    settle_cutoff_hour: int = 21       # 日更作业最晚 18:40(warehouse)/19:30(factor_freshness)，留余量到 21:00
    # 盘中类：交易日几点起要求「当日」必须有数据。取 10 点而非 09:00——
    # 首根 30 分钟桶要 09:37 才落库（minute_and_bars 在 :07/:37 跑），
    # 定在 09:00 会让 5 张行情表在每个交易日开盘瞬间各报一次假告警。
    session_open_hour: int = 10
    #: 2026-11-12（CL-20261112-10）：session 类表「当日数据应已就绪」的时刻按表覆盖。
    #:
    #: 背景：``session_open_hour=10`` 假设所有盘中类表 10:00 前都有当日数据
    #: （依据是首根 30m 桶 09:37 落库）。但 ``l2_adj.bar_{5,15,30}m_ba``
    #: 是**收盘后批量重建**的物化层，唯一生产者 ``back_adjusted_rebuild``
    #: 的 cron 为 ``55 2,11,15`` ⇒ 当日日盘数据首次在 **11:55** 才落库。
    #: 于是每个交易日 10:00~11:55 必然报「缺 1 个交易日」（11-10、11-12 实证），
    #: 而数据其实零滞后（11-12 三表 max(bucket) 当天已到 15:00）。
    #:
    #: 取 12 而非 11:55：给重建作业留执行余量（实测约 85s）。
    #: 12:00 之后仍要求当日数据 ⇒ **11:55 作业真失败时仍会告警**，
    #: 只是把「BA 真断更」的最早发现时刻从 10:00（本就是无效信息）推到 12:00。
    #:
    #: 未列出的表沿用 ``session_open_hour``，10 点灵敏度不变。
    #: 本配置只影响运维告警判定，不改变任何数据产出或交易信号。
    session_ready_hour: dict = field(default_factory=lambda: {
        "l2_adj.bar_5m_ba": 12,
        "l2_adj.bar_15m_ba": 12,
        "l2_adj.bar_30m_ba": 12,
    })
    job_heartbeat_days: int = 4        # 作业心跳阈值：近30天内有过成功的作业，若距上次成功超此天数→告警（覆盖"作业停跑但数据未滞后"盲区）
    # ★ CL-20261112-04 B-1：cron 作业失败的中央汇总（此前完全无人扫）。
    #   2026-11-12 实证：近 3 天 ops.scheduled_task_run 有 19 条真实 failed
    #   （cron_fetch_1min 从 11-11 22:30 到 11-12 10:15 连续 14 条、
    #    cron_git_sync_check 1 条…）却零告警。心跳盯的是「距上次成功」，
    #    对「一直在跑、一直失败」完全无感。
    job_failure_window_hours: int = 24
    job_failure_crit_count: int = 5    # 达到该条数升 CRIT（连续失败不是偶发抖动）
    # ★ CL-20261112-04 B-2：不变量守卫自身的存活性。
    #   cron_invariant_guard 每日 09:10 跑 --record --alert，但「守卫没跑」
    #   从来没人检查；它一停，95+ 条断言集体静默而下游监控仍全绿。
    #   注意它按设计在查出 block FAIL 时 sys.exit(1)，task_runner 因此记 failed
    #   —— 所以 B-1 的失败汇总刻意排除它，改由这里按 invariant_check 记录判定。
    guard_liveness_max_hours: int = 30
    push_silence_hours: int = 36       # 预测/推送静默告警阈值（小时）
    db_restart_alert_min: int = 10     # DB uptime < 该分钟数 → 疑似崩溃重启
    cooldown_sec: int = 1800
    #: 2026-11-10（CL-20261110-20）：状态未变化时，CRIT 最长静默多久重推一次。
    #: 去重不能把「严重告警长期存在」也静默掉；WARN 类完全按状态变化推。
    digest_crit_repush_hours: int = 6
    # ★ 2026-11-08（CL-20261108-23）：单日推送上限。
    #   pushplus 免费额度约 200 条/日，一旦击穿返回 code=900「用户账号使用受限」，
    #   **与告警共用同一 token 的交易信号会一并静默**（11-06 实测 60 小时）。
    #   故给运维告警设硬闸门，余量留给信号推送。
    max_per_day: int = 80
    #: CL-20261112-06 P1：全来源（告警 + 交易信号 + 简报）当日推送总量闸门。
    #: pushplus 服务端额度由所有来源共用，告警只限自己不够，逼近总额度须主动退让。
    max_total_per_day: int = 180
    #: 周全量 dump 容量红线（宿主 weekly_pgdump.sh 写 backup_health.json）。
    backup_max_total_gb: float = 25.0
    backup_min_free_gb: float = 20.0
    # ★ CL-20261113-02：部署窗口降级。publish.py 双重启会短暂杀掉在途 fetch，
    #   若把这类失败当严重误报，每次发布都刷严重。读 .deployed_sha（多行历史）
    #   全部部署时间戳，落在任一 [ts - before, ts + after] 宽窗内的 cron 失败
    #   视为部署预期，从严重计数剔除并降级为 INFO（不推送）。
    deploy_mark_path: str = "/Docker/qhyc/.deployed_sha"   # 宿主路径；容器内回退 /app/.deployed_sha
    deploy_grace_before_min: int = 10
    deploy_grace_after_min: int = 45
    notify: bool = True                # False = 只记日志不推送
    # 新鲜度巡检表（2026-10-08 大幅扩充）。
    #
    # ★ 移除 ``fut_kline``：其唯一生产者 `scripts/adjust_bars.py` 已随 fdf 退役停用，
    #   该表数据永久停在停用日 → `_check_freshness` 必然判 lag 超阈值 →
    #   **每 30 分钟推一条假告警**（cooldown 1800s）。这类噪音会让运维在几天内
    #   关闭通知，进而**淹没真告警**（备份失败 / DB 重启 / 采集断供）——
    #   比单点漏报更危险，故优先移除。
    #
    # ★ 扩充理由：原先只有 5 张表，**恰好漏掉了最容易静默失活的那些**——
    #   ``warehouse_receipt`` 2026-09-30→10-07 曾静默全失 7 天（当时无任何巡检）；
    #   ``spot_basis`` 是 pipeline readiness 的依赖；``contract_daily`` 是 carry 因子基础。
    #   原则：**凡是"停了会静默产出错误结果"的表，都要进巡检**。
    freshness_tables: dict = field(default_factory=lambda: {
        # L0 采集层
        "daily_bar": "trade_date",
        "hourly_bar": "trade_datetime",
        "minute_bar": "ts",
        "contract_daily": "trade_date",
        "member_position_rank": "trade_date",
        "warehouse_receipt": "report_date",
        "spot_basis": "report_date",
        "inventory": "report_date",
        # L1 行情层（fusion 与回测的实际数据源）
        "bar_60m": "bucket",
        # L2 后复权物化层（P1-1：bar_*_ba 须随 roll_segment + bar_* 同步；
        # 滞后 ⇒ back_adjusted_rebuild 作业未跑/roll_segment 缺失，会静默产出错误复权价）
        "l2_adj.bar_5m_ba": "bucket",
        "l2_adj.bar_15m_ba": "bucket",
        "l2_adj.bar_30m_ba": "bucket",
        "l2_adj.bar_60m_ba": "bucket",
        # L2 因子/参考层
        "factor_value": "trade_date",
        "main_contract_map": "trade_date",
    })
    # 每张表的**产出节奏**分类（CL-20261109-02）。滞后量的算法随节奏不同：
    #   · trade_day：值为 trade_date/report_date，只在交易日产出 → 滞后 = 缺失交易日数
    #   · session  ：值为盘中时间戳，交易日盘中持续产出；夜盘跨零点（次日 02:30 的桶
    #                属前一交易日）→ 先把时间戳映射到交易日，再按交易日数计
    #   · natural  ：每日计算、节假日也产出 → 只能按自然日计（当前无成员，
    #                保留给"真按日历跑"的表；真源不可用时全表降级到此口径）
    # 实测依据（2026-11-09）：
    #   · 国庆 10-01~10-07 **15 张表全部停产**，10-08 复产后各表 distinct 日期数
    #     = 交易日数 → 均为 trade_day/session 节奏；
    #   · factor_value 虽有 10-10(周六) 182 行、11-07(周六) 数据，但量级仅常规的
    #     1/10（1700+），是夜盘跨零点/补算碎片，不是"周末也产出"→ 仍归 trade_day，
    #     这些行会被映射回前一交易日。
    freshness_cadence: dict = field(default_factory=lambda: {
        "daily_bar": "trade_day",
        "contract_daily": "trade_day",
        "member_position_rank": "trade_day",
        "warehouse_receipt": "trade_day",
        "spot_basis": "trade_day",
        "inventory": "trade_day",
        "main_contract_map": "trade_day",
        "factor_value": "trade_day",
        "hourly_bar": "session",
        "minute_bar": "session",
        "bar_60m": "session",
        "l2_adj.bar_5m_ba": "session",
        "l2_adj.bar_15m_ba": "session",
        "l2_adj.bar_30m_ba": "session",
        "l2_adj.bar_60m_ba": "session",
    })
    # ★ 2026-10-13 整改：作业心跳巡检不再靠本类的「退役/一次性例外集」去堵误报——
    #   改为直接读调度器已注册周期作业清单 `app.scheduler_jobs.monitored_jobs()`
    #   （单一事实源）。清单里没有的作业（退役的 adjust、一次性手动回填 label 等）
    #   天然不会被监控，故以下三个手工例外集已移除。新增/退役作业只改
    #   `app/scheduler_jobs.py` + `app/scheduler.py::_build_scheduler` 即可，心跳自动同步。


def alert_config_from_env() -> AlertConfig:
    def _f(name, default):
        v = os.getenv(name)
        try:
            return float(v) if v not in (None, "") else default
        except ValueError:
            return default

    def _i(name, default):
        v = os.getenv(name)
        try:
            return int(v) if v not in (None, "") else default
        except ValueError:
            return default

    def _b(name, default):
        v = os.getenv(name)
        if v in (None, ""):
            return default
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    return AlertConfig(
        enabled=_b("ALERT_ENABLED", True),
        interval_min=_i("ALERT_INTERVAL_MIN", 10),
        mem_pct=_f("ALERT_MEM_PCT", 85.0),
        disk_pct=_f("ALERT_DISK_PCT", 85.0),
        disk_crit_pct=_f("ALERT_DISK_CRIT_PCT", 93.0),
        load_ratio=_f("ALERT_LOAD_RATIO", 1.5),
        data_lag_days=_i("ALERT_DATA_LAG_DAYS", 3),
        data_lag_trade_days=_i("ALERT_DATA_LAG_TRADE_DAYS", 0),
        settle_cutoff_hour=_i("ALERT_SETTLE_CUTOFF_HOUR", 21),
        session_open_hour=_i("ALERT_SESSION_OPEN_HOUR", 10),
        job_failure_window_hours=_i("ALERT_JOB_FAILURE_WINDOW_HOURS", 24),
        job_failure_crit_count=_i("ALERT_JOB_FAILURE_CRIT_COUNT", 5),
        guard_liveness_max_hours=_i("ALERT_GUARD_LIVENESS_MAX_HOURS", 30),
        push_silence_hours=_i("ALERT_PUSH_SILENCE_HOURS", 36),
        db_restart_alert_min=_i("ALERT_DB_RESTART_MIN", 10),
        cooldown_sec=_i("ALERT_COOLDOWN_SEC", 1800),
        digest_crit_repush_hours=_i("ALERT_DIGEST_CRIT_REPUSH_HOURS", 6),
        max_per_day=_i("ALERT_MAX_PER_DAY", 80),
        max_total_per_day=_i("ALERT_MAX_TOTAL_PER_DAY", 180),
        backup_max_total_gb=_f("ALERT_BACKUP_MAX_TOTAL_GB", 25.0),
        backup_min_free_gb=_f("ALERT_BACKUP_MIN_FREE_GB", 20.0),
        notify=_b("ALERT_NOTIFY", True),
    )


# ---------------------------------------------------------------------------
# 资源类检查（容器内可观测）
# ---------------------------------------------------------------------------

def _cgroup_candidates() -> list[tuple[str, str]]:
    """返回 (已用字节文件, 上限文件) 候选，按优先级。

    CL-20261108-01：qhyc 有两种运行形态，cgroup v2 文件位置不同——
      · 容器内：``/proc/self/cgroup = 0::/``，文件就在 /sys/fs/cgroup/ 根；
      · systemd 服务（qhyc-scheduler）：``0::/system.slice/qhyc-scheduler.service``，
        文件在 /sys/fs/cgroup/system.slice/qhyc-scheduler.service/ 下，
        **根目录根本没有 memory.current**。
    旧实现只试根路径与 v1，两者全失败后回退 /proc/meminfo（宿主全局内存），
    把「服务级 2.5G 限额」错量成「宿主机总内存」→ 内存告警实质失灵。
    """
    cands: list[tuple[str, str]] = [
        ("/sys/fs/cgroup/memory.current", "/sys/fs/cgroup/memory.max")]
    try:
        with open("/proc/self/cgroup") as f:
            for line in f:
                parts = line.strip().split(":", 2)
                if len(parts) == 3 and parts[1] == "" and parts[2].startswith("/"):
                    rel = parts[2].strip("/")
                    if rel:
                        base = "/sys/fs/cgroup/" + rel
                        cands.append((base + "/memory.current", base + "/memory.max"))
    except OSError:
        pass
    cands.append(("/sys/fs/cgroup/memory/memory.usage_in_bytes",
                  "/sys/fs/cgroup/memory/memory.limit_in_bytes"))   # cgroup v1
    return cands


def _cgroup_mem_source() -> tuple[int, int, str]:
    """返回 (已用字节, 上限字节, 来源)。``lim=0`` 表示 cgroup 未设上限（memory.max=max）。"""
    for used_path, lim_path in _cgroup_candidates():
        try:
            with open(used_path) as f:
                used = int(f.read().strip())
            with open(lim_path) as f:
                raw = f.read().strip()
            lim = 0 if raw in ("", "max") else int(raw)
            if lim > (1 << 60):         # cgroup v1 未设限时是个天文数字，等同无上限
                lim = 0
            if used > 0:
                return used, lim, used_path
        except (OSError, ValueError):
            # 预期性缺失：逐个候选降级即可，不打栈。
            # P0-4 把 pass 改成 exception=True 的 warning 后，每分钟刷两条 Traceback。
            continue
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        return total - avail, total, "proc-meminfo(宿主全局，口径降级)"
    except (OSError, ValueError):
        return 0, 0, "unavailable"


def _cgroup_mem() -> tuple[int, int]:
    """返回 (已用字节, 上限字节)。兼容容器 cgroup v2 / systemd 服务子路径 / v1。"""
    used, lim, src = _cgroup_mem_source()
    if not src.startswith("/sys/fs/cgroup/"):
        logger.warning("[alert] cgroup 内存限额不可读，内存告警降级为宿主全局口径；"
                       "请检查 OPS-MEM-CALIBER 守卫 (CL-20261108-01)")
    return used, lim


def _check_memory(cfg: AlertConfig) -> list[dict]:
    out = []
    used, lim = _cgroup_mem()
    if used and lim:
        pct = used / lim * 100.0
        level = "CRIT" if pct >= cfg.mem_pct + 10 else ("WARN" if pct >= cfg.mem_pct else None)
        if level:
            out.append({
                "key": "mem", "level": level,
                "msg": "cgroup 内存占用 {0:.1f}%（阈值 {1}%）- 接近 OOM 风险".format(
                    pct, cfg.mem_pct),
            })
    return out


def _check_disk(cfg: AlertConfig, path: str = "/app") -> list[dict]:
    out = []
    try:
        u = shutil.disk_usage(path)
    except Exception:
        return out
    pct = u.used / u.total * 100.0
    if pct >= cfg.disk_crit_pct:
        out.append({"key": "disk", "level": "CRIT",
                    "msg": "磁盘占用 {0:.1f}%（严重阈值 {1}%）".format(pct, cfg.disk_crit_pct)})
    elif pct >= cfg.disk_pct:
        out.append({"key": "disk", "level": "WARN",
                    "msg": "磁盘占用 {0:.1f}%（阈值 {1}%）".format(pct, cfg.disk_pct)})
    return out


def _check_load(cfg: AlertConfig) -> list[dict]:
    out = []
    try:
        with open("/proc/loadavg") as f:
            load1 = float(f.read().split()[0])
        ncpu = os.cpu_count() or 1
        ratio = load1 / ncpu
        if ratio >= cfg.load_ratio:
            out.append({"key": "load", "level": "WARN",
                        "msg": "负载1min/核心数 = {0:.2f}（阈值 {1}）".format(
                            ratio, cfg.load_ratio)})
    except Exception as exc:
        logger.opt(exception=True).warning(f"[silent-swallow] 异常被静默忽略（此处原本为 pass）: {exc}")
    return out


# ---------------------------------------------------------------------------
# 数据类检查
# ---------------------------------------------------------------------------

def _normalize_date(v):
    """把 timestamp / date 统一成 date（timestamptz 先转 CST 再取日）。"""
    if hasattr(v, "hour"):
        return (v.astimezone(CST) if getattr(v, "tzinfo", None) else v).date()
    return v


def _trading_days(session, lookback: int = 120):
    """最近交易日列表（升序）。真源 ``futures_rule``（akshare 每日 08:00 播种）。

    ⚠ 不用 ``l3_ref.trade_calendar``：那是止于 2026-10-08 的 R2 推断死表
    （CL-20261107-18），拿它判休市会把之后所有日期都当成休市。
    返回 None = 真源不可用，调用方退回自然日口径（宁可误报，不可漏报）。
    """
    try:
        rows = session.execute(
            text("SELECT DISTINCT trade_date FROM futures_rule "
                 "WHERE trade_date <= CURRENT_DATE "
                 "ORDER BY trade_date DESC LIMIT :n"),
            {"n": lookback},
        ).scalars().all()
    except Exception as e:
        logger.warning("[freshness] 交易日真源不可用，本次退回自然日口径: {0}".format(str(e)[:120]))
        return None
    days = sorted({_normalize_date(r) for r in rows if r is not None})
    return days or None


def _prev_trade_day(days, d):
    """≤ d 的最后一个交易日；早于整个窗口则 None。"""
    prev = None
    for x in days:
        if x <= d:
            prev = x
        else:
            break
    return prev


def _trade_gap(days, start, end) -> int:
    """(start, end] 区间内的交易日个数 = 缺失的交易日数。"""
    return sum(1 for x in days if start < x <= end)


def _expected_trade_day(days, now: datetime, deadline_hour: int):
    """截至 now 应已就绪的最后一个交易日。

    · 今天是交易日且已过 deadline_hour → 今天（当天缺当天报）
    · 否则 → 今天之前的最后一个交易日（周末/长假不额外要求，这是消除误报的关键）
    """
    today = now.date()
    if today in set(days) and now.hour >= deadline_hour:
        return today
    return _prev_trade_day(days, today - timedelta(days=1))


def _session_trade_day(days, mx):
    """盘中时间戳 → 所属交易日。夜盘 00:00-03:00 的桶属前一自然日的交易日。"""
    if hasattr(mx, "hour"):
        t = mx.astimezone(CST) if getattr(mx, "tzinfo", None) else mx
        d = t.date() - timedelta(days=1) if t.hour < 3 else t.date()
    else:
        d = mx
    # 周末/假日的空桶（synthesize 产物）映射回最近交易日，不当作"新数据"
    return _prev_trade_day(days, d)


def _check_freshness(session, cfg: AlertConfig, now: datetime | None = None) -> list[dict]:
    """关键表新鲜度。按表节奏分三类算滞后（CL-20261109-02）。

    trade_day：值为交易日/报告日，只在交易日产出 → 滞后 = 缺失交易日数
    session  ：值为盘中时间戳 → 先映射交易日，再按交易日数计
    natural  ：每日计算、节假日也产出 → 只能按自然日计（当前无成员；真源不可用时降级到此）

    session 类表的「当日应就绪时刻」可按表覆盖（``session_ready_hour``，
    CL-20261112-10）：BA 5/15/30m 由 11:55 的批量重建作业产出，12 点前不要求当日数据。
    """
    out = []
    now = now or datetime.now(CST)
    today = now.date()
    days = _trading_days(session)
    expected_daily = _expected_trade_day(days, now, cfg.settle_cutoff_hour) if days else None
    # 按「就绪时刻」缓存 expected：同一 hour 只算一次，避免逐表重复扫交易日历。
    # 默认 hour 预先放入，保证未被覆盖的表行为与改动前逐位一致。
    expected_by_hour: dict = {}
    if days:
        expected_by_hour[cfg.session_open_hour] = _expected_trade_day(
            days, now, cfg.session_open_hour)

    for tbl, col in cfg.freshness_tables.items():
        try:
            mx = session.execute(text("SELECT max({0}) FROM {1}".format(col, tbl))).scalar()
        except Exception as e:
            out.append({"key": "fresh_" + tbl, "level": "WARN",
                        "msg": "{0} 新鲜度检查失败: {1}".format(tbl, str(e)[:100])})
            continue
        if mx is None:
            out.append({"key": "fresh_" + tbl, "level": "WARN",
                        "msg": "{0} 无数据".format(tbl)})
            continue

        cadence = cfg.freshness_cadence.get(tbl, "natural")
        if cadence == "natural" or days is None:
            # 自然日口径：factor_value 每日计算；或交易日真源不可用时的降级
            d = _normalize_date(mx)
            lag = (today - d).days
            if lag > cfg.data_lag_days:
                out.append({"key": "fresh_" + tbl, "level": "WARN",
                            "msg": "{0} 数据滞后 {1} 天（最新 {2}，阈值 {3} 天）".format(
                                tbl, lag, d, cfg.data_lag_days)})
            continue

        if cadence == "session":
            # ★ 按表取就绪时刻（CL-20261112-10）：未覆盖的表走 session_open_hour，
            #   覆盖的表（BA 5/15/30m）走各自的 12 点。此前所有 session 表共用 10 点，
            #   与 BA 批量重建的 11:55 产出节奏错配，形成每交易日 1h55m 的假告警窗口。
            ready_hour = int(getattr(cfg, "session_ready_hour", {}).get(
                tbl, cfg.session_open_hour))
            if ready_hour not in expected_by_hour:
                expected_by_hour[ready_hour] = _expected_trade_day(
                    days, now, ready_hour)
            expected = expected_by_hour[ready_hour]
        else:
            expected = expected_daily
        if expected is None:
            continue
        own = _session_trade_day(days, mx) if cadence == "session" else \
            _prev_trade_day(days, _normalize_date(mx))
        if own is None:
            out.append({"key": "fresh_" + tbl, "level": "WARN",
                        "msg": "{0} 最新数据 {1} 早于近 {2} 个交易日，疑似长期停更".format(
                            tbl, mx, len(days))})
            continue
        gap = _trade_gap(days, own, expected)
        if gap > cfg.data_lag_trade_days:
            out.append({"key": "fresh_" + tbl, "level": "WARN",
                        "msg": "{0} 缺 {1} 个交易日数据（最新 {2}，应为 {3}）".format(
                            tbl, gap, mx, expected)})
    return out


# ---------------------------------------------------------------- 作业心跳判据
# ★ 2026-11-10（CL-20261110-10 A）：周/月频心跳改为 cron 感知。
# 旧判据只看「距上次成功 > 固定 expected_gap_days」。CL-20261107-03 把周频 cron
# 从错位的周日改回周六后，11-01（周日）最后一次成功，下一次正常触发是 11-14，
# 形成 13 天**合法**过渡空档，却被 9 天阈值写成「采集/调度链路可能停摆」；
# 当时日频链路全部 fresh，属于定性错误（CL-20261110-09 已实证）。
#
# 为何不读 ops.scheduled_task.updated_at 当「改期生效时刻」：实测三条周频字典的
# updated_at 仍停在 2026-10-14 22:23，改 schedule 时并未刷新 → 不可作真相源。
# 故用不依赖任何时间戳的等价判据：按**现行** cron 从上次成功起算，连续错过
# 两次应触发才判逾期。改期只跳过一次 → 合法空档不告警；真连续漏跑第二次后即报。
# 日频（<=4 天）周末/长假形态复杂且久经考验，保留原固定阈值，避免扩大改动面。
_JOBHB_CRON_MIN_GAP_DAYS = 7       # 仅周频/月频启用 cron 感知
_JOBHB_MISSED_FIRE_GRACE_H = 2.0   # 第二次应触发后的执行宽限
_JOBHB_HARD_CAP_MULT = 2.0         # cron 路径下超过 2× 登记间隔一律告警（防静默）


def _next_cron_fire(schedule: str, since: datetime) -> datetime | None:
    """按 5 段 crontab（apscheduler 语义）推算 **严格晚于** since 的下一次触发。

    实测 apscheduler 3.11 的 ``get_next_fire_time(None, now)`` 返回 >= now 的触发，
    正好落在触发点上时会把同一次触发再返回一遍（due1 == due2），连续漏跑判据会
    退化成「错过一次就报」，改期空档仍被误报。故基准统一 +1 微秒。
    """
    from apscheduler.triggers.cron import CronTrigger  # noqa: PLC0415

    trig = CronTrigger.from_crontab(schedule, timezone=CST)
    return trig.get_next_fire_time(None, since + timedelta(microseconds=1))


def job_heartbeat_verdict(last: datetime, now: datetime, *,
                          schedule: str | None = None,
                          expected_gap_days: float = 9.0,
                          use_cron: bool | None = None) -> tuple[bool, str]:
    """返回 (是否逾期, 判据说明)。纯函数，不查库，供守卫做行为级对拍。

    use_cron=None 时按 expected_gap_days 自动决定：>=7 天且有 schedule 走 cron 感知，
    否则沿用固定阈值。任何异常（cron 解析失败、字典不可读）都回退固定阈值，
    保证「不会静默」，但也不会把合法空档误判成链路停摆。
    """
    if last.tzinfo is None:
        last = last.replace(tzinfo=CST)
    if now.tzinfo is None:
        now = now.replace(tzinfo=CST)
    lag = (now - last).total_seconds() / 86400.0
    gap = float(expected_gap_days)
    cron_on = (use_cron if use_cron is not None
               else gap >= _JOBHB_CRON_MIN_GAP_DAYS) and bool(schedule)
    if not cron_on:
        return lag > gap, f"固定阈值 {gap:g} 天（lag={lag:.1f} 天）"
    hard_cap = _JOBHB_HARD_CAP_MULT * max(gap, 1.0)
    if lag > hard_cap:
        return True, f"距上次成功 {lag:.1f} 天 > 硬上限 {hard_cap:.1f} 天"
    try:
        due1 = _next_cron_fire(schedule, last)
        due2 = _next_cron_fire(schedule, due1) if due1 is not None else None
        if due1 is None or due2 is None:
            return lag > gap, "cron 无法推算应触发时刻，回退固定阈值"
        overdue = now > due2 + timedelta(hours=_JOBHB_MISSED_FIRE_GRACE_H)
        return overdue, (
            f"cron={schedule} 上次成功={last:%F %T} 应触发={due1:%F %T}/{due2:%F %T} "
            f"宽限={_JOBHB_MISSED_FIRE_GRACE_H:g}h lag={lag:.1f} 天")
    except Exception as exc:  # noqa: BLE001 —— 解析失败必须回退而非静默
        return lag > gap, f"cron 解析失败({type(exc).__name__})，回退固定阈值"


def _check_job_heartbeat(session, cfg: AlertConfig) -> list[dict]:
    """作业心跳巡检（2026-10-09 新增，2026-10-13 重构为清单驱动）。

    数据新鲜度巡检（_check_freshness）只校验"表最新日期"——当作业刚停 1~2 天、数据滞后
    还没越过 3 天阈值时它发现不了；本次 inventory 停摆 10 天靠数据滞后间接暴露，正是缺
    "作业本身是否还在跑"的直监控。

    ★ 2026-10-13 重构（根因修复，非补丁）：
    旧实现从 `task_run` 历史**反推**"哪些作业该在跑 + 该多久跑一次"，靠 task_type/label
    文本关键字猜频率 + 手工例外集堵误报，反复因「命名分类不全」误报（weights_update 月作业
    被 'weights' 误归、inventory 周频误判、一次性手动 label 被当独立作业、退役 adjust 被当停摆）。
    新实现直接读 `app.scheduler_jobs.monitored_jobs()`——即调度器 `app/scheduler.py` 真正
    注册的**周期作业清单**，每个作业带显式 `expected_gap_days`。清单里没有的作业（退役的、
    一次性手动的）天然不监控；频率不再靠猜。新增/退役作业只改清单，心跳自动同步。

    ★ 2026-11-10（CL-20261110-10 A）：周/月频不再用固定天数，改由 `ops.scheduled_task`
    的现行 cron 推算「连续两次应触发」判定（见 `job_heartbeat_verdict`）。日频沿用固定
    阈值。告警文案同时去掉「采集/调度链路可能停摆」这一定性——改期造成的合法空档
    与链路停摆是两回事，误报会让人忽略真正的值得查的告警。
    """
    days = getattr(cfg, "job_heartbeat_days", 4)
    if days <= 0:
        return []
    now = datetime.now(CST)
    out: list[dict] = []
    try:
        from app.scheduler_jobs import monitored_jobs
        jobs = monitored_jobs()
    except Exception as e:
        return [{"key": "jobhb", "level": "WARN",
                 "msg": "作业心跳清单加载失败: {0}".format(str(e)[:120])}]
    if not jobs:
        return []
    # ★ 2026-11-10（CL-20261110-20）：频率与状态源的**单一真相**是
    #   ops.scheduled_task（scheduler_resync 每 30 分钟用它覆盖代码 CronTrigger）。
    #   迁移 073 已把 backtest_weekly.expected_gap_days 从 9 改成 14，而
    #   scheduler_jobs._GAP_WEEKLY 仍是 9：若继续用代码硬编码值，两套阈值并存，
    #   合法周频空档会被固定 9 天阈值误判成停摆（11-10 15:00 实测两条 jobhb 误报）。
    #   **跳过判据用 expected_gap_days IS NULL**（CL-20261110-13 的 no_heartbeat
    #   口径），不是 meta.status_source：迁移 073 给 backtest_weekly 同时写了
    #   status_source 与 gap=14，它按设计「保留心跳」，不能被跳过。
    #   字典不可读/迁移中时全部回退代码值，既不静默也不让巡检本身崩掉。
    task_meta: dict[str, tuple[str | None, float | None, str, bool]] = {}
    try:
        sched_rows = session.execute(text(
            "SELECT task_id, schedule, expected_gap_days, "
            "coalesce(meta->>'status_source',''), executor "
            "FROM ops.scheduled_task WHERE enabled"
        )).fetchall()
        for row in sched_rows:
            # ★ CL-20261112-12：字典行按列弹性取值，不做固定元组解包。
            #   CL-20261110-20 之前这条查询只有 (task_id, schedule) 两列，之后
            #   扩成 5 列；固定解包遇到精简列会抛 ValueError，被下面的 except
            #   整块吞掉 → task_meta={} → schedule=None → cron 感知**静默退化**
            #   成固定天数阈值，11-10 改期空档就是这样被误报成「链路停摆」。
            #   缺列按安全默认补齐，不让一次列数差异把整套判据打回旧行为。
            cells = list(row)
            t = cells[0] if cells else None
            if t is None:
                continue
            sch = cells[1] if len(cells) > 1 else None
            gap = cells[2] if len(cells) > 2 else None
            src = cells[3] if len(cells) > 3 else ""
            exe = cells[4] if len(cells) > 4 else "apscheduler"
            try:
                gap_num = float(gap) if gap is not None else None
            except (TypeError, ValueError):
                gap_num = None  # 精简/异构查询里该列不是阈值 → 回退代码值
            # 只有**现行完整 5 列字典行**才有权用 expected_gap_days IS NULL
            # 声明 no_heartbeat；精简行的 None 只是「没取到这一列」，不能让
            # 下面的跳过分支把整个作业静默略过（否则改期空档会「假通过」、
            # 真停摆也报不出来，CL-20261112-12 实测踩过）。
            gap_known = len(cells) >= 5
            task_meta[str(t)] = (
                str(sch) if (sch and str(exe) == "apscheduler") else None,
                gap_num,
                str(src or ""),
                gap_known,
            )
    except Exception:  # noqa: BLE001 —— 含测试用 FakeSession、字典缺失等
        task_meta = {}
    # 只查"确实注册且写 task_run"的作业类型，避免扫到退役/一次性 label 产生误报。
    types = sorted({j.task_type for j in jobs})
    try:
        rows = session.execute(text(
            "SELECT task_type, label, max(finished_at) FROM task_run "
            "WHERE status='success' AND task_type = ANY(:types) "
            "GROUP BY task_type, label"
        ), {"types": types}).fetchall()
    except Exception as e:
        return [{"key": "jobhb", "level": "WARN",
                 "msg": "作业心跳检查失败: {0}".format(str(e)[:120])}]

    # 注册清单按 (task_type, label) 建索引；label 允许空串（单实例作业）。
    last_map: dict[tuple[str, str], object] = {}
    for task, label, last in rows:
        last_map[(str(task), str(label) if label is not None else "")] = last

    for j in jobs:
        tt, lb = j.task_type, j.label
        meta = task_meta.get(j.job_id)
        if meta is not None and meta[3] and meta[1] is None:
            # 字典**完整行**显式置 expected_gap_days=NULL（TSDB job_stats /
            # sync-only 实例 / python 合成等，真实状态源不在 task_run）→
            # 不用流水心跳判，避免「健康作业被判停摆」。精简行缺列不算声明，
            # backtest_weekly gap=14 也不在此列。
            continue
        last = last_map.get((tt, lb))
        if last is None:
            # 注册在跑但 task_run 从未成功过：可能是刚部署的新作业，或长期失败。
            # 与旧实现一致——旧逻辑只盯"近 30 天内有过成功"的作业，无历史即不报；
            # 真正的长期失败由调度器 EVENT_JOB_ERROR 监听 + 数据新鲜度巡检兜住。
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=CST)
        lag = (now - last).total_seconds() / 86400.0
        schedule, gap_db, _, _ = meta if meta else (None, None, "", False)
        gap = gap_db if gap_db is not None else float(j.expected_gap_days)
        overdue, detail = job_heartbeat_verdict(
            last, now, schedule=schedule, expected_gap_days=gap)
        if overdue:
            out.append({
                "key": "jobhb_" + tt,
                "level": "WARN",
                "msg": ("作业 {0}（label={1}）已 {2:.1f} 天无成功运行（最近成功 {3}）；"
                        "判据：{4} —— 请核查该作业的 cron 注册与执行流水").format(
                            tt, lb, lag, last.date(), detail),
            })
    return out


# ---------------------------------------------------------------- 作业失败 / 守卫自存活
def _deploy_ts_list(cfg: AlertConfig) -> "list[datetime]":
    """读部署标记 ``.deployed_sha`` 全部**历史**部署时刻（CL-20261113-02 修订）。

    标记允许多行：首行=当前部署（供 C5 守卫 ``read_text().split()[0]`` 取 HEAD），
    其后每行为更早的部署。每行形如 ``ac159ac 2026-11-12T16:30:20Z``，第二字段为
    部署 UTC 时刻。返回所有可解析的部署时刻（升序）。读不到 / 无有效行返回空列表
    （不降级）。多行历史让「被后续部署覆盖掉的早期部署窗口」仍能被降级逻辑匹配，
    避免 00:30 那类由旧部署引发的预期失败被重新算成严重。
    """
    out: list[datetime] = []
    for cand in (cfg.deploy_mark_path, "/app/.deployed_sha"):
        p = Path(cand)
        if not p.exists():
            continue
        try:
            txt = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for ln in txt.splitlines():
            parts = ln.split()
            if len(parts) >= 2:
                try:
                    out.append(datetime.fromisoformat(parts[1].replace("Z", "+00:00")))
                except Exception:
                    continue
        break  # 首个存在的候选即可，不再回退容器内
    return sorted(out)


def _check_job_failures(session, cfg: AlertConfig) -> list[dict]:
    """近窗口内 cron 作业真实失败的**中央汇总**（CL-20261112-04 B-1）。

    ``check_all`` 此前只查资源 / 新鲜度 / 心跳 / DB uptime / 推送静默。心跳判据
    （``_check_job_heartbeat``）盯的是「距上次成功多久」，一个作业可以每 30 分钟
    被调用、每次都失败，只要历史上成功过且间隔没超阈值就永远不报 ——
    2026-11-12 实测 ``cron_fetch_1min`` 连续失败 14 次就是这样静默过去的。

    两类刻意排除（不是漏报，是把已知噪声与真故障分开）：
      · ``aborted`` 且 message 含 ``[reaped:``：调度器重启自愈把僵尸 running
        置 aborted，属正常清理（近 3 天 78 条全是这种）；
      · ``cron_invariant_guard`` **按设计**在查出 block FAIL 时 ``sys.exit(1)``，
        task_runner 因此记 failed —— 但只在「该轮确实跑完并写了
        ``ops.invariant_run`` 完成标记」时才排除。

    ★ CL-20261112-06 P1 修正：旧版无条件 ``task_id <> 'cron_invariant_guard'``，
      把「按设计 exit=1」和 ImportError / OOM 崩溃**一起静默**了。现在守卫
      崩溃（无完成标记）会单独升级为 CRIT ``guardfail``：09:10 断言链一旦
      集体静默，其余监控仍全绿，这是最危险的盲区。

    聚合为**一条** finding 而非每条失败一条：digest 指纹只取 (key, level)，
    失败次数每轮变化不会触发重推，避免重蹈 11-10 刷爆 pushplus 额度的覆辙。
    """
    win = max(1, int(cfg.job_failure_window_hours))
    # ★ CL-20261113-02（修订）：部署窗口降级。读**全部历史**部署时刻，把落在
    #   任一部署宽窗内的 cron 失败视为发布双重启的预期副作用，从严重计数剔除。
    #   多行历史让被后续部署覆盖的早期部署窗口也能被匹配（如 00:30 那次误杀）。
    deploy_ts_list = _deploy_ts_list(cfg)
    params: dict = {"win": win}
    deploy_filter = ""
    if deploy_ts_list:
        conds = []
        for i, dt in enumerate(deploy_ts_list):
            lo = dt - timedelta(minutes=cfg.deploy_grace_before_min)
            hi = dt + timedelta(minutes=cfg.deploy_grace_after_min)
            params[f"dlo{i}"] = lo
            params[f"dhi{i}"] = hi
            conds.append(f"(started_at BETWEEN :dlo{i} AND :dhi{i})")
        deploy_filter = "AND NOT (" + " OR ".join(conds) + ")"
    try:
        rows = session.execute(text(f"""
            SELECT task_id, status, count(*) AS n
            FROM ops.scheduled_task_run
            WHERE started_at >= now() - make_interval(hours => :win)
              AND status IN ('failed', 'aborted')
              AND coalesce(message, '') NOT LIKE '%[reaped:%'
              AND task_id <> 'cron_invariant_guard'
              {deploy_filter}
            GROUP BY task_id, status
            ORDER BY n DESC, task_id
        """), params).fetchall()
        # 守卫 failed 单独判定：有 ops.invariant_run 完成标记 = 按设计 exit=1；
        # 没有 = ImportError/OOM/半途崩溃，必须 CRIT（旧版在这里静默）。
        guard_crash = session.execute(text("""
            SELECT count(*) FROM ops.scheduled_task_run r
            WHERE r.task_id = 'cron_invariant_guard'
              AND r.status = 'failed'
              AND r.started_at >= now() - make_interval(hours => :win)
              AND NOT EXISTS (
                  SELECT 1 FROM ops.invariant_run g
                  WHERE g.finished_at >= r.started_at - interval '2 minutes'
                    AND g.finished_at <= r.started_at + interval '30 minutes'
              )
        """), params).scalar()
        # 部署宽窗内的失败（预期）：跨所有历史部署窗口单独计数，供降级 INFO 使用。
        deploy_caused = 0
        for i in range(len(deploy_ts_list)):
            deploy_caused += int(session.execute(text(f"""
                SELECT count(*) FROM ops.scheduled_task_run
                WHERE started_at BETWEEN :dlo{i} AND :dhi{i}
                  AND status IN ('failed', 'aborted')
                  AND coalesce(message, '') NOT LIKE '%[reaped:%'
                  AND task_id <> 'cron_invariant_guard'
            """), params).scalar() or 0)
    except Exception as e:
        return [{"key": "jobfail", "level": "WARN",
                 "msg": "作业失败汇总查询失败（不静默）: {0}".format(str(e)[:120])}]
    out: list[dict] = []
    if rows:
        total = sum(int(r[2]) for r in rows)
        top = "、".join("{0}({1}×{2})".format(r[0], r[1], r[2]) for r in rows[:6])
        if len(rows) > 6:
            top += " 等 {0} 个作业".format(len(rows))
        out.append({
            "key": "jobfail",
            "level": "CRIT" if total >= int(cfg.job_failure_crit_count) else "WARN",
            "msg": ("近 {0}h 有 {1} 条 cron 作业失败/异常终止：{2}；"
                    "已排除 reaped 自愈、守卫设计内 exit=1 与部署窗口内预期失败").format(
                        win, total, top),
        })
    if deploy_caused > 0:
        latest = deploy_ts_list[-1]
        out.append({
            "key": "jobfail_deploy",
            "level": "INFO",
            "msg": ("近 {0}h 有 {1} 条 cron 失败落在部署窗口（最近部署于 {2}，"
                    "已纳入 {3} 次历史部署宽窗）内，为发布双重启的预期副作用，"
                    "已降级不报严重").format(
                        win, deploy_caused,
                        ((latest + timedelta(hours=8)).strftime("%m-%d %H:%M")
                         if latest else "?"),
                        len(deploy_ts_list)),
        })
    if int(guard_crash or 0) > 0:
        out.append({
            "key": "guardfail", "level": "CRIT",
            "msg": ("近 {0}h 有 {1} 次 cron_invariant_guard 异常退出且无"
                    " ops.invariant_run 完成标记（疑似 ImportError/OOM/半途崩溃）；"
                    "09:10 不变量断言链可能已集体静默，请立即查守卫日志").format(
                        win, int(guard_crash)),
        })
    return out


def _check_guard_liveness(session, cfg: AlertConfig) -> list[dict]:
    """不变量守卫自身是否还活着（CL-20261112-04 B-2）——「监控的监控」。

    ``cron_invariant_guard`` 每日 09:10 跑 ``--record --alert``，但从来没人检查
    「它今天到底跑了没有」。它一旦被摘掉，95+ 条断言集体静默，而所有下游
    监控仍显示全绿 —— 这是最典型的看不见的失效。

    三件套都是「此刻」可判定的状态，不靠事件流水凑数：
      ① DB 字典里 ``cron_invariant_guard`` 仍 enabled；
      ② ``ops.invariant_run`` 有**完成标记**，且标记条数 == 该轮
         ``ops.invariant_check`` 流水条数（防「写入 60 条后崩溃」被当成跑过）；
      ③ 最近完成轮次不早于「最近一个应完成的 09:10」（防「今天根本没跑，
         但昨天记录在 30h 窗口内」——旧版 30h 窗口正是被这样骗过去）。

    ★ CL-20261112-06 P1：旧判据是「30h 内有记录 + 最近一轮 ≥50 条」，
      守卫崩在第 60 条、或当天压根没跑，两者都仍 PASS。
    """
    max_h = max(1, int(cfg.guard_liveness_max_hours))
    try:
        enabled = session.execute(text(
            "SELECT enabled FROM ops.scheduled_task "
            "WHERE task_id='cron_invariant_guard'"
        )).scalar()
        row = session.execute(text("""
            SELECT r.run_id, r.finished_at, r.n_total,
                   (SELECT count(*) FROM ops.invariant_check i
                    WHERE i.run_id = r.run_id) AS n_rows
            FROM ops.invariant_run r
            ORDER BY r.finished_at DESC LIMIT 1
        """)).first()
        expected = session.execute(text("""
            SELECT CASE
              WHEN (now() AT TIME ZONE 'Asia/Shanghai')::time >= time '11:10'
              THEN ((now() AT TIME ZONE 'Asia/Shanghai')::date + time '09:10')
                   AT TIME ZONE 'Asia/Shanghai'
              ELSE ((now() AT TIME ZONE 'Asia/Shanghai')::date - 1 + time '09:10')
                   AT TIME ZONE 'Asia/Shanghai'
            END
        """)).scalar()
    except Exception as e:
        return [{"key": "guardlive", "level": "CRIT",
                 "msg": "守卫存活性检查失败（不静默）: {0}".format(str(e)[:120])}]
    problems = []
    if enabled is None:
        problems.append("ops.scheduled_task 缺 cron_invariant_guard 字典行")
    elif not enabled:
        problems.append("cron_invariant_guard 已被禁用")
    if row is None:
        problems.append("ops.invariant_run 无任何完成标记（守卫未跑完或未接线）")
    else:
        _rid, finished, n_total, n_rows = row
        if int(n_total or 0) != int(n_rows or 0):
            problems.append("最近轮标记 {0} 条但流水仅 {1} 条（半途崩溃）".format(
                n_total, n_rows))
        if finished is None:
            problems.append("最近完成轮缺 finished_at")
        else:
            now = datetime.now(timezone.utc)
            ref = finished.replace(tzinfo=timezone.utc) if finished.tzinfo is None else finished
            hours = (now - ref).total_seconds() / 3600.0
            if hours > max_h:
                problems.append("最近完成轮已是 {0:.1f}h 前（阈值 {1}h）".format(
                    hours, max_h))
            exp = expected.replace(tzinfo=timezone.utc) if (
                expected is not None and expected.tzinfo is None) else expected
            if exp is not None and ref < exp:
                problems.append("最近完成轮早于期望 09:10 轮次（今日守卫尚未跑完）")
    if not problems:
        return []
    return [{"key": "guardlive", "level": "CRIT",
             "msg": "不变量守卫存活性异常：" + "；".join(problems)}]


def _check_push_fallback(session, cfg: AlertConfig) -> list[dict]:
    """备用推送通道必须「可见」（CL-20261110-06 / CL-20261112-06 P1）。

    告警侧此前连 ``fusion.fallback_webhook`` 都不读 —— 用户即使配了 URL，
    运维告警也永远是单点。现已接线；这里只把「尚未配置」从无声变成一条
    稳定 WARN（状态指纹去重，不会每轮重推），等用户提供 webhook 即消失。
    """
    try:
        from app.core.config import get_settings

        fb = getattr(get_settings().fusion, "fallback_webhook", "") or ""
    except Exception as exc:  # noqa: BLE001
        return [{"key": "pushfallback", "level": "WARN",
                 "msg": "备用通道配置不可读({0})".format(type(exc).__name__)}]
    if fb:
        return []
    return [{"key": "pushfallback", "level": "WARN",
             "msg": ("fusion.fallback_webhook 未配置 → pushplus 是唯一推送通道，"
                     "且与运维告警共用服务端额度（11-06 曾致交易信号静默 60h）。"
                     "告警链路已接线，填入 URL 即自动降级生效；见 CL-20261110-06。")}]


def _check_backup_capacity(session, cfg: AlertConfig) -> list[dict]:
    """周全量备份容量与状态（CL-20261112-06 P1）——从「无声」改为可告警。

    宿主 ``weekly_pgdump.sh`` 每次运行后写 ``/app/runtime/backup_health.json``
    （容器内可见的 bind mount）。旧脚本固定 retain-7 × ~7.6G ≈ 53G，
    而 /dev/vda2 仅剩 34G；这里读实时总量/可用空间/校验结果并告警。
    """
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    p = Path("/app/runtime/backup_health.json")
    if not p.exists():
        return [{"key": "backupcap", "level": "WARN",
                 "msg": "无备份健康状态文件（周备份脚本未运行或未接线）"}]
    try:
        st = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [{"key": "backupcap", "level": "WARN",
                 "msg": "备份健康状态不可解析({0})".format(type(exc).__name__)}]
    problems: list[str] = []
    level = "WARN"
    total = float(st.get("total_gb") or 0)
    free = float(st.get("free_gb") or 0)
    if total > float(cfg.backup_max_total_gb):
        problems.append("dump 总量 {0:.1f}G 超上限 {1:.0f}G".format(
            total, cfg.backup_max_total_gb))
        level = "CRIT"
    if free < float(cfg.backup_min_free_gb):
        problems.append("磁盘可用 {0:.1f}G 低于安全线 {1:.0f}G".format(
            free, cfg.backup_min_free_gb))
        level = "CRIT"
    if not st.get("last_restore_list_ok"):
        problems.append("dump 未通过 pg_restore --list 完整性校验")
    if not st.get("remote_configured"):
        problems.append("无异地副本（BACKUP_REMOTE_DIR 未配置，待用户提供目标）")
    if not problems:
        return []
    return [{"key": "backupcap", "level": level,
             "msg": "备份健康：" + "；".join(problems)}]


def _check_db_uptime(session, cfg: AlertConfig) -> list[dict]:
    """DB 进程 uptime 过短 = 疑似崩溃/被 OOM kill 后重启（跨越容器边界的代理指标）。"""
    if cfg.db_restart_alert_min <= 0:
        return []
    try:
        start = session.execute(text("SELECT pg_postmaster_start_time()")).scalar()
    except Exception as e:
        return [{"key": "db_up", "level": "CRIT",
                 "msg": "无法获取数据库启动时间: {0}".format(str(e)[:100])}]
    if start is None:
        return []
    now = datetime.now(timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    mins = (now - start).total_seconds() / 60.0
    if mins < cfg.db_restart_alert_min:
        return [{"key": "db_up", "level": "CRIT",
                 "msg": "数据库 uptime 仅 {0:.1f} 分钟 < {1} —— 疑似崩溃/OOM 重启，请查 dmesg".format(
                     mins, cfg.db_restart_alert_min)}]
    return []


def _check_push_silence(session, cfg: AlertConfig) -> list[dict]:
    try:
        mx = session.execute(text("SELECT max(as_of_ts) FROM prediction_result")).scalar()
    except Exception:
        return []
    if mx is None:
        return [{"key": "silence", "level": "WARN", "msg": "prediction_result 为空（预测链路未产出）"}]
    now = datetime.now(timezone.utc)
    if mx.tzinfo is None:
        mx = mx.replace(tzinfo=timezone.utc)
    hours = (now - mx).total_seconds() / 3600.0
    if hours > cfg.push_silence_hours:
        return [{"key": "silence", "level": "WARN",
                 "msg": "预测产出静默 {0:.1f} 小时（最新 {1}，阈值 {2}h）".format(
                     hours, mx, cfg.push_silence_hours)}]
    return []


# ---------------------------------------------------------------------------
# 汇总与推送
# ---------------------------------------------------------------------------

def send_alert(title: str, content: str, cfg: AlertConfig | None = None,
               key: str = "") -> tuple[bool, str]:
    """发送告警（带同键冷却）。返回 (是否发出, 说明)。"""
    cfg = cfg or alert_config_from_env()
    if key:
        last = _LAST_SENT.get(key, 0.0)
        if time.time() - last < cfg.cooldown_sec:
            return False, "冷却中（{0}s 窗口）".format(cfg.cooldown_sec)
        _LAST_SENT[key] = time.time()
    logger.warning("[alert] {0} | {1}".format(title, content))
    if not cfg.notify or not cfg.enabled:
        return False, "告警推送已关闭或配置未启用"
    if not _budget_take(cfg):
        logger.warning("[alert] 今日推送配额已用尽（上限 {0} 条），本次仅记日志不推送: {1}"
                       .format(cfg.max_per_day, title))
        return False, "日推送配额已用尽（上限 {0} 条）".format(cfg.max_per_day)
    try:
        from app.core.config import get_settings
        from app.notify.pushplus import send_notify

        # CL-20261112-06 P1：此前 send_alert 从不传 fallback_webhook，
        # 于是即便用户配了 fusion.fallback_webhook，运维告警也仍是单点。
        # 其余链路（V5X live/brief、scheduler）都传了，只有这里漏了。
        fallback = getattr(get_settings().fusion, "fallback_webhook", "") or ""
        ok, msg = send_notify(title, content, fallback, source="alert")
        return bool(ok), msg
    except Exception as e:
        return False, "推送通道异常: {0}".format(str(e)[:160])


# ---------------------------------------------------------------------------
# 2026-11-10（CL-20261110-20）：告警汇总「状态变化才推」
# ---------------------------------------------------------------------------

def _digest_fingerprint(findings: list[dict]) -> str:
    """状态指纹 = 排序后的 ``(key, level)`` 集合。

    **刻意不含 msg 里的 lag/时刻数值**：周频作业从「9.4 天」变「9.5 天」不是
    状态变化；把 msg 纳入指纹会让每轮都算出新值，去重彻底失效（这正是旧版
    ``send_alert(key="digest")`` + 1800s 冷却每 30 分钟重推一条的根因）。
    """
    pairs = sorted((str(f.get("key", "?")), str(f.get("level", "WARN")))
                   for f in findings)
    raw = "|".join(f"{k}:{lv}" for k, lv in pairs) or "OK"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _dedup_conn():
    """独立连接读写去重表：不依赖调用方 session 的事务状态（check_all 也会被
    ops_monitor 用 FakeSession 调用），也不把网络 IO 夹在业务事务里。"""
    from app.core.db import get_engine
    return get_engine()


def _digest_last_state() -> tuple[str | None, datetime | None]:
    """最近一次实际推送的状态；表不可用/未建时回退进程内，不阻断告警。"""
    try:
        with _dedup_conn().connect() as conn:
            row = conn.execute(text(
                "SELECT fingerprint, sent_at FROM ops.notify_dedup "
                "WHERE kind = 'alert_digest' ORDER BY sent_at DESC LIMIT 1"
            )).first()
        if row is not None:
            return (str(row[0]) if row[0] else None), row[1]
    except Exception:  # noqa: BLE001
        pass
    fp = _DIGEST_FALLBACK.get("fingerprint")
    ts = float(_DIGEST_FALLBACK.get("sent_at") or 0.0)
    return fp, (datetime.fromtimestamp(ts, CST) if ts else None)


def _dedup_write(kind: str, dedup_key: str, fingerprint: str | None) -> bool:
    """写一条留痕；返回是否真的插入（去重命中时为 False）。

    **只在送达成功后调用**：绝不「先占位再发送」把失败固化成已送达
    （``_PUSH_GATE`` 与 ``V5X`` 信号都踩过这个坑）。
    """
    try:
        with _dedup_conn().begin() as conn:
            r = conn.execute(text(
                "INSERT INTO ops.notify_dedup(kind, dedup_key, fingerprint, sent_at) "
                "VALUES (:kind, :k, :fp, now()) "
                "ON CONFLICT (kind, dedup_key) DO NOTHING"
            ), {"kind": kind, "k": dedup_key, "fp": fingerprint})
        # 表不可用时靠它兜底：否则 _digest_last_state 永远返回 None，
        # 状态未变化也会每轮重推（刷屏原样复发）。
        _DIGEST_FALLBACK.update(fingerprint=fingerprint, sent_at=time.time())
        return bool(r.rowcount)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[alert] 去重留痕写入失败（不阻断告警）: {0}: {1}".format(
            type(exc).__name__, str(exc)[:100]))
        return False


def _should_send_digest(findings: list[dict], cfg: AlertConfig) -> tuple[bool, str, str]:
    """返回 (是否推送, 指纹, 判据说明)。

    状态变化才推；但 CRIT 长期存在时按 ``digest_crit_repush_hours`` 兜底重推，
    否则「去重」会把严重告警一并静默，比刷屏更危险。
    """
    fp = _digest_fingerprint(findings)
    last_fp, last_at = _digest_last_state()
    if last_fp == fp and last_at is not None:
        if last_at.tzinfo is None:
            last_at = last_at.replace(tzinfo=CST)
        hours = (datetime.now(CST) - last_at.astimezone(CST)).total_seconds() / 3600.0
        has_crit = any(f.get("level") == "CRIT" for f in findings)
        if has_crit and hours >= max(1, int(cfg.digest_crit_repush_hours)):
            return True, fp, f"CRIT 已静默 {hours:.1f}h → 超时重推"
        return False, fp, f"状态未变化（{fp}），距上次 {hours:.1f}h → 跳过"
    if last_fp is None:
        return bool(findings), fp, "首次巡检"
    return True, fp, f"状态变化 {last_fp} → {fp}"


def _render_digest(findings: list[dict]) -> tuple[str, str]:
    """把一轮巡检的全部发现合并成一条消息（标题, 正文）。"""
    crit = any(f.get("level") == "CRIT" for f in findings)
    level = "CRIT" if crit else "WARN"
    title = "[qhyc] {0} 告警汇总 {1} 项".format(level, len(findings))
    lines = []
    for f in findings:
        tag = "**严重**" if f.get("level") == "CRIT" else "警告"
        lines.append("- {0} `{1}`：{2}".format(tag, f.get("key", "?"), f.get("msg", "")))
    return title, "\n".join(lines)


def check_all(session=None, cfg: AlertConfig | None = None) -> dict:
    """跑一遍全部检查，返回 findings / alerts / summary。"""
    cfg = cfg or alert_config_from_env()
    if not cfg.enabled:
        return {"enabled": False, "findings": [], "alerts": [], "summary": "告警已停用"}

    findings: list[dict] = []
    findings += _check_memory(cfg)
    findings += _check_disk(cfg)
    findings += _check_load(cfg)

    owns = session is None
    if owns:
        from app.core.db import session_scope
        ctx = session_scope()
        session = ctx.__enter__()
    try:
        findings += _check_db_uptime(session, cfg)
        findings += _check_freshness(session, cfg)
        findings += _check_job_heartbeat(session, cfg)
        # CL-20261112-04 B-1/B-2：作业失败中央汇总 + 守卫自存活
        findings += _check_job_failures(session, cfg)
        findings += _check_guard_liveness(session, cfg)
        findings += _check_push_silence(session, cfg)
        # CL-20261112-06 P1：备用通道单点与备份容量炸弹，从无声改为可告警。
        findings += _check_push_fallback(session, cfg)
        findings += _check_backup_capacity(session, cfg)
    finally:
        if owns:
            try:
                ctx.__exit__(None, None, None)
            except Exception as exc:
                logger.opt(exception=True).warning(f"[silent-swallow] 异常被静默忽略（此处原本为 pass）: {exc}")

    alerts = []
    if findings:
        # ★ 2026-11-08（CL-20261108-23）：一轮巡检合并为**单条 digest**。
        #   原先每个 finding 单独发送：6 个数据源同时滞后 = 6 条/轮 × 48 轮/日 ≈ 288 条/日。
        # ★ 2026-11-10（CL-20261110-20）：合并还不够——旧版 send_alert(key="digest")
        #   的 1800s 冷却恰好等于 ALERT_INTERVAL_MIN=30，**状态没变化也每 30 分钟
        #   重推一条**；11-10 实测 20 条重复告警，11-06 曾刷爆 pushplus 额度
        #   导致交易信号静默 2.5 天。改为「状态指纹变化才推」+ CRIT 超时兜底。
        should, fp, reason = _should_send_digest(findings, cfg)
        logger.info(f"[alert] digest 判据：{reason}")
        if should:
            title, content = _render_digest(findings)
            # 不再传 key="digest"：冷却交给状态指纹，避免状态真变化时被 1800s
            # 冷却误挡（两套限流叠加会漏掉新出现的异常）。
            ok, note = send_alert(title, content, cfg)
            if ok:
                _dedup_write("alert_digest",
                             datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S.%f"), fp)
            alerts = [{"key": f["key"], "level": f["level"], "sent": ok, "note": note}
                      for f in findings]
        else:
            alerts = [{"key": f["key"], "level": f["level"], "sent": False,
                       "note": f"状态未变化，跳过：{reason}"} for f in findings]
    else:
        # 异常 → 正常只写一条 OK 留痕，**不推恢复通知**（本轮目标就是降噪，
        # 且 OPS-PUSH-QUOTA 锁死 check_all 内 send_alert 恰好 1 处）。
        # 不留痕的话，下次异常出现时会误以为状态从没变过。
        # 当前已正常且上次也正常时，一行都不写、一条都不推。
        last_fp, _ = _digest_last_state()
        ok_fp = _digest_fingerprint([])
        if last_fp not in (None, ok_fp):
            _dedup_write("alert_digest",
                         datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S.%f"), ok_fp)
    crit = sum(1 for f in findings if f["level"] == "CRIT")
    return {
        "enabled": True, "findings": findings, "alerts": alerts,
        "summary": "共 {0} 项异常（CRIT {1}）".format(len(findings), crit),
    }


def install_scheduler_listener(scheduler, cfg: AlertConfig | None = None) -> bool:
    """给 APScheduler 挂监听：作业抛错/错过调度 → 立即告警（P1-2「作业失败」）。"""
    cfg = cfg or alert_config_from_env()
    try:
        from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
    except Exception as e:
        logger.warning("[alert] 无法安装调度监听: {0}".format(e))
        return False

    def _on_error(ev):
        tb = getattr(ev, "traceback", "") or ""
        send_alert(
            "[qhyc] CRIT 调度作业失败: {0}".format(getattr(ev, "job_id", "?")),
            "异常：{0}\n{1}".format(getattr(ev, "exception", ""), tb[-800:]),
            cfg, key="job_error_" + str(getattr(ev, "job_id", "?")),
        )

    def _on_missed(ev):
        send_alert(
            "[qhyc] WARN 调度作业错过执行: {0}".format(getattr(ev, "job_id", "?")),
            "计划时间：{0}".format(getattr(ev, "scheduled_run_time", "")),
            cfg, key="job_missed_" + str(getattr(ev, "job_id", "?")),
        )

    try:
        scheduler.add_listener(_on_error, EVENT_JOB_ERROR)
        scheduler.add_listener(_on_missed, EVENT_JOB_MISSED)
        logger.info("[alert] 调度作业失败/错过监听已安装")
        return True
    except Exception as e:
        logger.warning("[alert] 安装调度监听失败: {0}".format(e))
        return False
