# -*- coding: utf-8 -*-
"""统一运维告警（整改优先级 P1-2：作业失败 / OOM 告警）。

背景：2026-10-01 云端连续三次 OOM（cgroup CONSTRAINT_MEMCG 杀 postgres backend），
全部靠人工 `dmesg` 排查才发现；作业失败也无通知。本模块补齐三条检测线：

1. **资源类**（容器内可观测量）：cgroup 内存占用率、磁盘占用率、负载/CPU 比值；
2. **数据类**：关键表新鲜度（滞后期）、预测/推送静默、数据库进程uptime（<阈值=疑似
   崩溃重启，是越过容器边界观测 DB OOM 的**唯一可行代理指标**）；
3. **作业类**：APScheduler 事件监听，作业抛错或错过调度立即告警。

⚠ 能力边界（必读）：容器被 OOM kill 时本进程也会消失，无法自我告警——那条线必须由
宿主侧 `scripts/ops/ops_monitor.py`（宿主 crontab）兜底。本模块负责容器内可见的部分。
"""
from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core.logging import logger

CST = timezone(timedelta(hours=8))

# 冷却：同一告警键在窗口内只推送一次，避免刷屏
_LAST_SENT: dict[str, float] = {}

# 日配额计数（进程内；重启清零可接受——单次重启不会额外放大推送量）
_DAILY_BUDGET: dict = {"day": None, "used": 0}


def _budget_take(cfg: "AlertConfig") -> bool:
    """日推送配额闸门。返回 False 表示今日额度已用尽，本次不再推送。"""
    today = datetime.now(CST).date()
    if _DAILY_BUDGET.get("day") != today:
        _DAILY_BUDGET["day"] = today
        _DAILY_BUDGET["used"] = 0
    limit = int(getattr(cfg, "max_per_day", 0) or 0)
    if limit <= 0:
        return True
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
    job_heartbeat_days: int = 4        # 作业心跳阈值：近30天内有过成功的作业，若距上次成功超此天数→告警（覆盖"作业停跑但数据未滞后"盲区）
    push_silence_hours: int = 36       # 预测/推送静默告警阈值（小时）
    db_restart_alert_min: int = 10     # DB uptime < 该分钟数 → 疑似崩溃重启
    cooldown_sec: int = 1800
    # ★ 2026-11-08（CL-20261108-23）：单日推送上限。
    #   pushplus 免费额度约 200 条/日，一旦击穿返回 code=900「用户账号使用受限」，
    #   **与告警共用同一 token 的交易信号会一并静默**（11-06 实测 60 小时）。
    #   故给运维告警设硬闸门，余量留给信号推送。
    max_per_day: int = 80
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
        push_silence_hours=_i("ALERT_PUSH_SILENCE_HOURS", 36),
        db_restart_alert_min=_i("ALERT_DB_RESTART_MIN", 10),
        cooldown_sec=_i("ALERT_COOLDOWN_SEC", 1800),
        max_per_day=_i("ALERT_MAX_PER_DAY", 80),
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
    """
    out = []
    now = now or datetime.now(CST)
    today = now.date()
    days = _trading_days(session)
    expected_daily = _expected_trade_day(days, now, cfg.settle_cutoff_hour) if days else None
    expected_session = _expected_trade_day(days, now, cfg.session_open_hour) if days else None

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

        expected = expected_session if cadence == "session" else expected_daily
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
    # ★ 2026-11-10：频率单一真相是 ops.scheduled_task.schedule（scheduler_resync 每
    # 30 分钟用它覆盖代码 CronTrigger）。字典不可读/被移除/迁移中时全部回退固定阈值，
    # 既不静默，也不让巡检本身崩掉。
    schedule_map: dict[str, str] = {}
    try:
        sched_rows = session.execute(text(
            "SELECT task_id, schedule FROM ops.scheduled_task "
            "WHERE enabled AND executor = 'apscheduler' AND schedule IS NOT NULL"
        )).fetchall()
        schedule_map = {str(t): str(s) for t, s in sched_rows}
    except Exception:  # noqa: BLE001 —— 含测试用 FakeSession、字典缺失等
        schedule_map = {}
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
        last = last_map.get((tt, lb))
        if last is None:
            # 注册在跑但 task_run 从未成功过：可能是刚部署的新作业，或长期失败。
            # 与旧实现一致——旧逻辑只盯"近 30 天内有过成功"的作业，无历史即不报；
            # 真正的长期失败由调度器 EVENT_JOB_ERROR 监听 + 数据新鲜度巡检兜住。
            continue
        if last.tzinfo is None:
            last = last.replace(tzinfo=CST)
        lag = (now - last).total_seconds() / 86400.0
        overdue, detail = job_heartbeat_verdict(
            last, now, schedule=schedule_map.get(j.job_id),
            expected_gap_days=j.expected_gap_days)
        if overdue:
            out.append({
                "key": "jobhb_" + tt,
                "level": "WARN",
                "msg": ("作业 {0}（label={1}）已 {2:.1f} 天无成功运行（最近成功 {3}）；"
                        "判据：{4} —— 请核查该作业的 cron 注册与执行流水").format(
                            tt, lb, lag, last.date(), detail),
            })
    return out


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
        from app.notify.pushplus import send_notify
        ok, msg = send_notify(title, content)
        return bool(ok), msg
    except Exception as e:
        return False, "推送通道异常: {0}".format(str(e)[:160])


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
        findings += _check_push_silence(session, cfg)
    finally:
        if owns:
            try:
                ctx.__exit__(None, None, None)
            except Exception as exc:
                logger.opt(exception=True).warning(f"[silent-swallow] 异常被静默忽略（此处原本为 pass）: {exc}")

    alerts = []
    if findings:
        # ★ 2026-11-08（CL-20261108-23）：一轮巡检合并为**单条 digest**。
        #   原先每个 finding 单独发送：6 个数据源同时滞后 = 6 条/轮 × 48 轮/日 ≈ 288 条/日，
        #   2026-11-06 实测当天推送 206 条 [alert]，击穿 pushplus 额度后返回
        #   code=900「用户账号使用受限」，把共用同一 token 的**交易信号**一起打成静默
        #   （fusion_push_log 11-06 11:01 起 7 条全部未送达，直到 11-08 才恢复）。
        #   合并后单轮上限 1 条，信息不丢（逐项列出，key 仍在正文里可检索）。
        title, content = _render_digest(findings)
        ok, note = send_alert(title, content, cfg, key="digest")
        alerts = [{"key": f["key"], "level": f["level"], "sent": ok, "note": note}
                  for f in findings]
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
