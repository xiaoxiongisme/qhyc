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


@dataclass
class AlertConfig:
    enabled: bool = True
    interval_min: int = 10             # 定时检测间隔（分钟）
    mem_pct: float = 85.0              # 容器内存占用告警阈值（%）
    disk_pct: float = 85.0             # 磁盘占用告警阈值（%）
    disk_crit_pct: float = 93.0        # 磁盘严重阈值（%）
    load_ratio: float = 1.5            # 负载/核心数 比值告警阈值
    data_lag_days: int = 3             # 数据滞后期告警阈值（自然日，含周末）
    push_silence_hours: int = 36       # 预测/推送静默告警阈值（小时）
    db_restart_alert_min: int = 10     # DB uptime < 该分钟数 → 疑似崩溃重启
    cooldown_sec: int = 1800
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
        # L2 因子/参考层
        "factor_value": "trade_date",
        "main_contract_map": "trade_date",
    })


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
        push_silence_hours=_i("ALERT_PUSH_SILENCE_HOURS", 36),
        db_restart_alert_min=_i("ALERT_DB_RESTART_MIN", 10),
        cooldown_sec=_i("ALERT_COOLDOWN_SEC", 1800),
        notify=_b("ALERT_NOTIFY", True),
    )


# ---------------------------------------------------------------------------
# 资源类检查（容器内可观测）
# ---------------------------------------------------------------------------

def _cgroup_mem() -> tuple[int, int]:
    """返回 (已用字节, 上限字节)。cgroup v2 优先，回退 v1，再回退 /proc/meminfo。"""
    try:
        with open("/sys/fs/cgroup/memory.current") as f:
            used = int(f.read().strip())
        lim = 0
        try:
            with open("/sys/fs/cgroup/memory.max") as f:
                v = f.read().strip()
                lim = 0 if v == "max" else int(v)
        except Exception:
            lim = 0
        if used:
            return used, lim
    except Exception:
        pass
    try:                                    # cgroup v1
        with open("/sys/fs/cgroup/memory/memory.usage_in_bytes") as f:
            used = int(f.read().strip())
        with open("/sys/fs/cgroup/memory/memory.limit_in_bytes") as f:
            lim = int(f.read().strip())
        return used, lim
    except Exception:
        pass
    # 回退：读取宿主机 /proc/meminfo（容器内看到的通常是宿主机内存）
    try:
        info = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
        total = info.get("MemTotal", 0)
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        return total - avail, total
    except Exception:
        return 0, 0


def _check_memory(cfg: AlertConfig) -> list[dict]:
    out = []
    used, lim = _cgroup_mem()
    if used and lim:
        pct = used / lim * 100.0
        level = "CRIT" if pct >= cfg.mem_pct + 10 else ("WARN" if pct >= cfg.mem_pct else None)
        if level:
            out.append({
                "key": "mem", "level": level,
                "msg": "容器内存占用 {0:.1f}%（阈值 {1}%）- 接近 OOM 风险".format(
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
    except Exception:
        pass
    return out


# ---------------------------------------------------------------------------
# 数据类检查
# ---------------------------------------------------------------------------

def _check_freshness(session, cfg: AlertConfig) -> list[dict]:
    out = []
    now = datetime.now(CST).date()
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
        d = mx.date() if isinstance(mx, datetime) else mx
        lag = (now - d).days
        if lag > cfg.data_lag_days:
            out.append({"key": "fresh_" + tbl, "level": "WARN",
                        "msg": "{0} 数据滞后 {1} 天（最新 {2}，阈值 {3} 天）".format(
                            tbl, lag, d, cfg.data_lag_days)})
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
    try:
        from app.notify.pushplus import send_notify
        ok, msg = send_notify(title, content)
        return bool(ok), msg
    except Exception as e:
        return False, "推送通道异常: {0}".format(str(e)[:160])


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
        findings += _check_push_silence(session, cfg)
    finally:
        if owns:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass

    alerts = []
    for f in findings:
        ok, note = send_alert("[qhyc] {0} {1}".format(f["level"], f["key"]),
                              f["msg"], cfg, key=f["key"])
        alerts.append({"key": f["key"], "level": f["level"], "sent": ok, "note": note})
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
