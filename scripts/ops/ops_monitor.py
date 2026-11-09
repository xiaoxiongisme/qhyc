#!/usr/bin/env python3
"""qhyc 运维：资源与服务监控告警（推送到微信 pushplus）

检查项：
  1. 系统：负载(5min) / 内存 / 根分区磁盘
  2. 容器：2 个 qhyc 容器是否 running（qhyc-api / qhyc-timescaledb）
  2b. 系统服务：scheduler / pipeline 等 systemd 单元是否 active（迁云后存活直监控）
  3. 服务：API /health 是否 200；TimescaleDB 是否可查询
  4. 数据：daily_bar 最新交易日距今滞后天数（回迁期可放宽）
  5. 推送：v5x_signal_log 最近一次**送达成功**的时刻（交易时段内静默 > 2h 告警）

告警经 pushplus（与 qhyc 同一通道）发到微信；同一项在冷却期内不重复打扰。
每日 08:00 可发一份"体检日报"：python3 ops_monitor.py --daily

cron（★ 直接指向仓库路径：2026-11-08 发现 /opt/qhyc-ops 旧副本已与仓库分叉 212 行，
仓库里的修复跑不到生产，属「改了不生效」；副本已废弃，仅留 .bak_20261108 回滚）：
  */10 * * * * /usr/bin/python3 /Docker/qhyc/scripts/ops/ops_monitor.py >> /var/log/qhyc_ops_monitor.log 2>&1
  0 8 * * *   /usr/bin/python3 /Docker/qhyc/scripts/ops/ops_monitor.py --daily >> /var/log/qhyc_ops_monitor.log 2>&1
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import logging

logger = logging.getLogger(__name__)

SH = ZoneInfo("Asia/Shanghai")
#: 冷却状态目录。原先写死 ``/opt/qhyc-ops/state``（08 日已废弃的旧副本目录）：
#: 那里的 ``alert_state.json`` 属主是 **root:644**，而 cron 以 ubuntu 运行 →
#: 每次 ``json.dump`` 都 PermissionError，被 ``silent-swallow`` 吞掉后仍 ``return True``，
#: 冷却**恒失效**。2026-11-09 实测：``signal_stall`` / ``push_silence`` 两项误报
#: 每 10 分钟各推一次，日志覆盖范围内 **298 条**，把真正的交易信号在微信里淹没。
#: 现改为「env 覆盖 → 用户工作目录（cron 用户必然可写）→ 旧路径兜底」。
STATE_DIR = (os.environ.get("QH_OPS_STATE_DIR")
             or os.path.expanduser("~/qhyc_work/state"))
LEGACY_STATE_DIR = "/opt/qhyc-ops/state"
STATE_FILE = os.path.join(STATE_DIR, "alert_state.json")
#: 主路径不可写时的兜底状态文件（``/tmp`` 通常可写）。
#: 没有它的话，主路径一坏就只剩「按小时节流」这一档，连**首次**发现故障都推不出去
#: （实测 2026-11-09 16:57 模拟不可写：报了 1 项问题却推送 0 条，要等到下一个
#: 整点后 10 分钟窗口才放行 —— 对「监控自身故障」这种低频告警，等 50 分钟不可接受）。
FALLBACK_STATE_FILE = os.path.join(tempfile.gettempdir(), "qhyc_ops_alert_state.json")
ENV_FILE = os.environ.get("QHYC_ENV_FILE", "/Docker/qhyc/.env")
CONTAINERS = ["qhyc-timescaledb", "qhyc-api"]   # 2026-10-08 P3: scheduler/pipeline 已迁 systemd
# 迁云后生产调度不再是容器，改为检查 systemd 服务，否则会持续误报「容器未运行」
SYSTEMD_UNITS = ["qhyc-scheduler.service", "qhyc-pipeline.service"]

# 阈值（4 核 / 8G 机型标定；改机型请同步调 LOAD_WARN）
LOAD_WARN_RATIO = 1.5      # load5 > cores × 1.5
MEM_WARN = 85.0            # %
DISK_WARN = 85.0           # %
DISK_CRIT = 93.0           # %
DATA_LAG_WARN = 2          # 天（自然日；仅在交易日真源不可用时兜底）
DATA_LAG_TRADE_WARN = 0    # 个交易日；落后 >0 即报（期望值已扣除未收盘的今天）
SETTLE_HOUR = 19           # 日线入库截止小时（daily_futures_refresh 18:10 触发，留 50min 余量）
PUSH_SILENCE_WARN = 24     # 小时
MARKET_LIVE_H = 6          # 市场数据 6h 内有更新 ⇒ 视为交易时段（休市静默不算故障）
SIGNAL_STALL_WARN = 3      # 小时；交易时段内无任何推送记录超过该值 ⇒ 链路停摆
COOLDOWN_SEC = 3600        # 同类告警冷却 1 小时
COOLDOWN_CRIT = 1800       # 严重告警冷却 30 分钟

#: 冷却状态最近一次持久化失败的原因（None = 正常）。由 :func:`cooldown_ok` 写入，
#: 由 :func:`main` 读出来升级成一条**显式告警**——这套机制 2026-10-08~11-09 因
#: ``/opt/qhyc-ops/state`` 属主为 root 而整整静默失效一个月（每天多推 288 条），
#: 根因不是权限，是「写不进去却没人知道」。
_STATE_ERROR: str | None = None


def sh(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "").strip()
    except Exception as e:  # noqa: BLE001
        return -1, f"{type(e).__name__}: {e}"


def load_token() -> str:
    tok = os.environ.get("PUSHPLUS_TOKEN", "")
    if tok:
        return tok
    try:
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("PUSHPLUS_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except FileNotFoundError as exc:
        logger.debug("[silent-swallow] 异常被静默忽略（此处原本为 pass）: %s", exc, exc_info=True)
    return ""


def push(title: str, content: str, token: str) -> bool:
    if not token:
        print("[monitor] PUSHPLUS_TOKEN 未配置，跳过推送")
        return False
    payload = json.dumps({
        "token": token, "title": title, "content": content, "template": "markdown",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://www.pushplus.plus/send", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            body = r.read().decode("utf-8", "replace")
        ok = '"code":200' in body.replace(" ", "") or '"code": 200' in body
        print(f"[monitor] push {'OK' if ok else 'FAIL'}: {body[:120]}")
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"[monitor] push error: {e}")
        return False


def state_writable() -> str | None:
    """探测冷却状态目录可写性；返回错误描述，正常返回 None。

    每轮主动探一次，而不是等 ``cooldown_ok`` 写失败才发现 —— 后者只在「已经
    有告警要推」时才触发，一旦无事可报就永远暴露不出来（正是这一个月的状态）。
    """
    probe = os.path.join(STATE_DIR, ".write_probe")
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return None
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"


def _load_state() -> dict:
    """读冷却状态；新路径缺失时从旧路径迁移一次（保留历史冷却进度）。"""
    for path in (STATE_FILE, os.path.join(LEGACY_STATE_DIR, "alert_state.json")):
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    return json.load(f)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[monitor] 读取冷却状态失败 %s: %s", path, exc)
    return {}


def cooldown_ok(key: str, level: str) -> bool:
    """冷却期内不重复推送同一项。

    ⚠ 2026-11-09 重构：原实现只要写状态失败就 ``return True`` ⇒ 冷却恒失效 ⇒
    同一告警每轮（10 分钟）重推一次，一天 288 条。写失败本身是环境问题，不该
    让「不重复打扰」这个承诺悄悄作废。现在的约定是：
      · 能持久化 → 按 last 时间精确冷却（原语义）；
      · 不能持久化 → **确定性地按小时节流**（整点后首个 10 分钟窗口才允许推），
        宁可少报一次也不刷屏，并把 error 打到日志（不再 silent-swallow）。
    """
    st = _load_state()
    last = float(st.get(key, 0))
    cd = COOLDOWN_CRIT if level == "crit" else COOLDOWN_SEC
    now = time.time()
    if now - last < cd:
        return False

    st[key] = now
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f)
        globals()["_STATE_ERROR"] = None
        return True
    except Exception as exc:  # noqa: BLE001
        globals()["_STATE_ERROR"] = f"{type(exc).__name__}: {exc}"
        logger.error("[monitor] 冷却状态无法持久化 %s: %s —— 本轮降级为按小时节流",
                     STATE_FILE, exc)
    # 无持久化时的兜底：每小时只放行一次（10 分钟粒度下 minute<10 即整点那轮）
    return datetime.now(SH).minute < 10


def psql(sql: str, timeout: int = 30) -> tuple[bool, str]:
    rc, out = sh(["docker", "exec", "qhyc-timescaledb", "psql", "-U", "futures",
                  "-d", "futures", "-t", "-A", "-c", sql], timeout=timeout)
    return rc == 0, out


def sample_system() -> dict:
    """采集系统资源原始指标（load/disk/mem/swap）。

    抽成独立函数并可在 collect() 中注入，是为了打破依赖、让 ops_monitor 在
    无 /proc/meminfo、无 os.getloadavg 的开发/测试环境下也能被单元测试覆盖
    （Feathers《与遗留代码有效工作》：先引入 seam，再做 characterization）。
    """
    cores = os.cpu_count() or 4
    load5 = os.getloadavg()[1]
    total, used, free = shutil.disk_usage("/")
    disk_pct = used / total * 100
    disk_free_gb = round(free / 1024 ** 3, 1)
    meminfo: dict[str, int] = {}
    try:
        for line in open("/proc/meminfo", encoding="utf-8"):
            k, v = line.split(":", 1)
            meminfo[k.strip()] = int(v.split()[0])
    except Exception as exc:  # noqa: BLE001
        logger.warning("[silent-swallow] 异常被静默忽略（此处原本为 pass）: %s", exc, exc_info=True)
    mem_total = meminfo.get("MemTotal", 1)
    mem_avail = meminfo.get("MemAvailable", mem_total)
    mem_pct = (mem_total - mem_avail) / mem_total * 100
    mem_total_gb = round(mem_total / 1024 ** 2, 1)
    swap_pct = None
    swap_total = meminfo.get("SwapTotal", 0)
    swap_free = meminfo.get("SwapFree", swap_total)
    if swap_total:
        swap_pct = round((swap_total - swap_free) / swap_total * 100, 1)
    return {
        "load5": round(load5, 2),
        "cores": cores,
        "disk_pct": round(disk_pct, 1),
        "disk_free_gb": disk_free_gb,
        "mem_pct": round(mem_pct, 1),
        "mem_total_gb": mem_total_gb,
        "swap_pct": swap_pct,
    }


def collect(sh_fn=None, psql_fn=None, system_fn=None) -> dict:
    """采集全部指标。返回 {指标: 值} 与问题列表。

    sh_fn / psql_fn / system_fn 可注入，用于脱离 docker/systemctl/psql 做单元测试
    （锁定 2026-10-08 P3 迁云后 scheduler/pipeline 改 systemd 单元、不再被误报
    「容器未运行」的修复行为）。默认走真实实现，cron 调用 collect() 行为不变。
    """
    sh_fn = sh_fn or sh
    psql_fn = psql_fn or psql
    system_fn = system_fn or sample_system
    m: dict = {}
    issues: list[tuple[str, str, str]] = []   # (level, key, 描述)

    # 1. 系统资源（来自可注入的 system_fn）
    sm = system_fn()
    m.update(sm)
    load5 = sm["load5"]
    cores = sm["cores"]
    if load5 > cores * LOAD_WARN_RATIO:
        issues.append(("warn", "load", f"5 分钟负载 {load5:.2f} > {cores * LOAD_WARN_RATIO:.1f}（{cores} 核）"))

    disk_pct = sm["disk_pct"]
    disk_free_gb = sm["disk_free_gb"]
    if disk_pct >= DISK_CRIT:
        issues.append(("crit", "disk", f"根分区已用 {disk_pct:.1f}%（剩余 {disk_free_gb} GB）"))
    elif disk_pct >= DISK_WARN:
        issues.append(("warn", "disk", f"根分区已用 {disk_pct:.1f}%（剩余 {disk_free_gb} GB）"))

    mem_pct = sm["mem_pct"]
    mem_total_gb = sm["mem_total_gb"]
    if mem_pct >= MEM_WARN:
        issues.append(("warn", "mem", f"内存已用 {mem_pct:.1f}%（总 {mem_total_gb} GB）"))

    if sm.get("swap_pct") is not None:
        m["swap_pct"] = sm["swap_pct"]

    # 2. 容器
    down = []
    for c in CONTAINERS:
        rc, out = sh_fn(["docker", "inspect", "-f", "{{.State.Running}}", c])
        if rc != 0 or out.strip() != "true":
            down.append(c)
    m["containers_down"] = down
    if down:
        issues.append(("crit", "container", "容器未运行：" + ", ".join(down)))

    # 1b. systemd 服务（P3 迁云后 scheduler/pipeline 在此；存活直监控，弥补告警模块
    #     跑在调度器进程内、无法侦测自身死亡的能力盲区）
    units_down = []
    for u in SYSTEMD_UNITS:
        rc, out = sh_fn(["systemctl", "is-active", u])
        if out.strip() != "active":
            units_down.append(u)
    m["units_down"] = units_down
    if units_down:
        issues.append(("crit", "service", "systemd 服务未运行：" + ", ".join(units_down)))

    # 3. 服务
    rc, out = sh_fn(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     "--max-time", "15", "http://127.0.0.1:8000/health"])
    m["api_code"] = out.strip() or str(rc)
    if out.strip() != "200":
        issues.append(("crit", "api", f"/health 返回 {m['api_code']}（期望 200）"))

    ok, _ = psql_fn("SELECT 1")
    m["db_ok"] = ok
    if not ok:
        issues.append(("crit", "db", "TimescaleDB 不可查询"))

    # 4. 数据新鲜度（表不存在/空表时跳过，避免回迁期误报）
    #
    # ★ 2026-11-09（CL-20261109-01）：原判据 `(today - max(trade_date)).days > 2`
    #   按**自然日**算，周末两天必然把 lag 顶到 3 → 每逢周末/长假必误报。
    #   11-09 00:00 那条「日线数据滞后 3 天（最新 2026-11-06）」即假告警：
    #   daily_bar 的 max(trade_date) 与交易日真源 futures_rule 完全一致（都是 11-06），
    #   数据零滞后。改为「以最后一个已收盘交易日为期望，数缺失的交易日个数」。
    #   真源用 futures_rule + cfg_calendar_seed_meta.seeded_through（akshare 播种，
    #   每日 08:00 由 seed_futures_rule 刷新）；**不可用** ``l3_ref.trade_calendar``
    #   —— 那是止于 2026-10-08 的 R2 推断死表（CL-20261107-18）。
    ok, out = psql_fn("SELECT max(trade_date)::text FROM daily_bar")
    m["latest_data_date"] = out.strip() if ok and out.strip() else None
    if m["latest_data_date"] and m["latest_data_date"] not in ("", "None"):
        try:
            d = datetime.strptime(m["latest_data_date"], "%Y-%m-%d").date()
            m["data_lag_days"] = (datetime.now(SH).date() - d).days   # 自然日，仅展示
            # 期望最新交易日：今天若是交易日，需已过结算入库时刻(SETTLE_HOUR)才计入，
            # 否则取前一交易日 —— 这样「当天数据没到」能当天报，周末又不会误报。
            trade_sql = (
                "WITH exp AS ("
                "  SELECT max(trade_date) AS d FROM futures_rule"
                "  WHERE trade_date < CURRENT_DATE"
                "     OR (trade_date = CURRENT_DATE"
                "         AND EXTRACT(HOUR FROM now() AT TIME ZONE 'Asia/Shanghai') >= {h})"
                ") SELECT coalesce((SELECT d::text FROM exp), '') || '|' ||"
                "  (SELECT count(DISTINCT trade_date)::text FROM futures_rule, exp"
                "    WHERE futures_rule.trade_date > '{d}'"
                "      AND futures_rule.trade_date <= exp.d)"
            ).format(h=SETTLE_HOUR, d=d.isoformat())
            ok2, buf = psql_fn(trade_sql)
            exp_s, gap_s = "", ""
            if ok2 and "|" in buf:
                exp_s, gap_s = [x.strip() for x in buf.strip().split("|", 1)]
            if exp_s and gap_s.isdigit():
                m["expected_trade_date"] = exp_s
                m["data_lag_trade"] = int(gap_s)
                if int(gap_s) > DATA_LAG_TRADE_WARN:
                    issues.append((
                        "warn", "data_lag",
                        f"日线落后 {gap_s} 个交易日（最新 {d}，应为 {exp_s}；"
                        f"自然日 {m['data_lag_days']} 天）"))
            elif m["data_lag_days"] > DATA_LAG_WARN:
                # 交易日真源不可用 → 退回自然日判据（宁可误报，不可漏报）
                issues.append((
                    "warn", "data_lag",
                    f"日线数据滞后 {m['data_lag_days']} 天（最新 {d}，交易日真源不可用）"))
        except ValueError as exc:
            logger.debug("[silent-swallow] 异常被静默忽略（此处原本为 pass）: %s", exc, exc_info=True)

    # 5. 推送活性
    #
    # ★ 2026-11-08（CL-20261108-23）：原判据「delivered 静默 > 24h 即告警」不区分休市，
    #   每逢周末/长假必然误报——2026-11-08 22:30 这次「已 60 小时无成功推送」就是
    #   周五 11-06 10:03 后无交易日 + 当日推送被 pushplus 限流叠加的产物。
    #   改为「市场数据仍在更新时才算链路故障」：以 minute_bar 最新桶的年龄作休市代理，
    #   休市期间静默属正常，不下结论。
    now_utc = datetime.now(timezone.utc)
    market_age_h = None
    ok, out = psql_fn(
        "SELECT max(ts)::text FROM minute_bar WHERE ts > now() - interval '10 days'")
    if ok and out.strip() and out.strip() != "None":
        try:
            mt = datetime.fromisoformat(out.strip())
            if mt.tzinfo is None:
                mt = mt.replace(tzinfo=SH)
            market_age_h = (now_utc - mt.astimezone(timezone.utc)).total_seconds() / 3600
            m["market_age_h"] = round(market_age_h, 1)
        except ValueError as exc:
            logger.debug("[silent-swallow] market_age 解析失败: %s", exc, exc_info=True)
    # 市场 6 小时内有过新数据 ⇒ 处于交易时段（含夜盘），推送就应该在跑
    market_live = market_age_h is not None and market_age_h <= MARKET_LIVE_H

    # ★ 2026-11-09（CL-20261109-15）：判据**数据源过期**。
    #   11-08 起生产推送源唯一化为 V5X.3（cfg_feature_switch.fusion_v34_live_enabled=f），
    #   信号改落 ``v5x_signal_log``；而这两条判据仍在查 ``fusion_push_log`` ⇒ 该表自
    #   11-06 23:04 起不再增长 ⇒ 「已 79 小时无成功推送」+「信号链路 65.7 小时无记录」
    #   成为**永久误报**，每 10 分钟各推一次。
    # ★ 2026-11-09（CL-20261109-18）：该表已 **DROP**，判据里彻底去掉它（不是注释掉——
    #   查一张不存在的表会让 psql 整条语句失败 ⇒ cols 为空 ⇒ silence_h=None ⇒
    #   既不告警也不报错，等于这条判据悄悄消失）。现在：
    #     · 推送成功时间 = max(v5x_signal_log.created_at WHERE notify_ok)
    #     · 有信号却未送达 = v5x_signal_log 里 **source='live'** 且仍在补发窗口内、
    #       notify_ok 非真的记录（replay 补录行按设计不推送，混进来就是永久误报）
    #     · 链路活性       = ops.scheduled_task_run 里 v5x_scan 最近一次 success
    #   「无信号」与「推不出去」必须分开衡量：V5X 是事件驱动，一天没事件本来就不推。
    ok, out = psql_fn(
        "SELECT "
        " (SELECT max(created_at)::text FROM v5x_signal_log WHERE notify_ok),"
        " (SELECT max(created_at)::text FROM v5x_signal_log),"
        " (SELECT count(*) FROM v5x_signal_log"
        "   WHERE notify_ok IS NOT TRUE AND source='live'"
        "     AND bar_ts > now() - interval '2 hours'),"
        " (SELECT enabled::text FROM cfg_feature_switch WHERE switch_key='v5x_live_enabled'),"
        " (SELECT enabled::text FROM cfg_feature_switch"
        "   WHERE switch_key='fusion_v34_live_enabled')")
    cols = [c.strip() for c in out.strip().split("|")] if ok and out.strip() else []

    def _col(i: int) -> str:
        return cols[i] if len(cols) > i else ""

    def _age_h(s: str):
        if not s or s == "None":
            return None
        try:
            t = datetime.fromisoformat(s)
            if t.tzinfo is None:
                t = t.replace(tzinfo=SH)
            return (now_utc - t.astimezone(timezone.utc)).total_seconds() / 3600
        except ValueError:
            return None

    v5x_on = _col(3) == "true"
    v34_on = _col(4) == "true"
    silence_h = _age_h(_col(0))          # 唯一推送源 = V5X，不必再取 min
    last_signal_age = _age_h(_col(1))
    unsent = int(_col(2) or 0)
    if silence_h is not None:
        m["push_silence_h"] = round(silence_h, 1)
    if last_signal_age is not None:
        m["last_signal_age_h"] = round(last_signal_age, 1)
    m["v5x_unsent_live"] = unsent
    m["push_sources"] = f"v5x={'on' if v5x_on else 'off'}/v34={'on' if v34_on else 'off'}"

    # 通道故障：只在「确实有推送需求」时下结论 —— 近 24h 内有信号产出才算。
    # 无信号的自然静默（周末、无事件）不报，否则就是 11-08 那次 60 小时误报的翻版。
    signal_active = last_signal_age is not None and last_signal_age <= 24
    if (market_live and signal_active and silence_h is not None
            and silence_h > PUSH_SILENCE_WARN):
        issues.append(("warn", "push_silence",
                       f"已 {silence_h:.0f} 小时无成功推送（近 {last_signal_age:.1f} 小时"
                       f"内仍有信号产出 → 推送通道疑似故障）"))

    # 有信号但没送达：比「静默」更精确，且不依赖推送表是否还在增长。
    if unsent > 0:
        issues.append(("warn", "push_unsent",
                       f"{unsent} 条 V5X 实盘信号仍在补发窗口（2h）内却未送达"
                       f"——查 PUSHPLUS_TOKEN / fusion.fallback_webhook / 网络"))

    # 5b. 信号链路活性：交易时段内扫描作业压根没成功跑完，说明调度/计算停了。
    #     这与「推送通道失败」是两种故障，必须分开报——通道故障有留痕、链路停摆没有。
    #     判据从「推送表有无新行」改为「作业有无成功完成」：事件驱动策略没有事件时
    #     本来就不写推送表，用推送表衡量活性必然误报。
    if market_live and (v5x_on or v34_on):
        ok2, out2 = psql_fn(
            "SELECT max(finished_at)::text FROM ops.scheduled_task_run"
            # v5x_close_scan 是 CL-20261109-17 起的**主节奏**（60m 收盘 +10s），
            # 漏掉它会让「收盘链停摆」这个故障完全不可见 —— 只剩下兜底的
            # :05/:35 还能记 success，链路看起来一切正常。
            " WHERE task_id IN ('v5x_close_scan','v5x_scan','fusion_scan') AND status='success'"
            "   AND finished_at > now() - interval '2 days'")
        if ok2 and out2.strip() and out2.strip() != "None":
            stall_h = _age_h(out2.strip())
            if stall_h is not None:
                m["signal_scan_age_h"] = round(stall_h, 1)
                if stall_h > SIGNAL_STALL_WARN:
                    issues.append(("crit", "signal_stall",
                                   f"信号扫描作业已 {stall_h:.1f} 小时未成功完成"
                                   f"（市场数据仍在更新 → 调度/信号计算疑似停摆）"))

    return {"metrics": m, "issues": issues}


def render_alert(issues, metrics) -> str:
    lines = [f"## {'🚨' if any(i[0] == 'crit' for i in issues) else '⚠️'} qhyc 云端告警",
             f"时间：{datetime.now(SH):%Y-%m-%d %H:%M:%S}", ""]
    for level, _, desc in issues:
        lines.append(f"- {'**严重**' if level == 'crit' else '警告'}：{desc}")
    lines += ["", "---",
              f"负载 {metrics.get('load5')}（{metrics.get('cores')} 核）｜"
              f"内存 {metrics.get('mem_pct')}%｜磁盘 {metrics.get('disk_pct')}%｜"
              f"剩余 {metrics.get('disk_free_gb')} GB"]
    if metrics.get("data_lag_trade") is not None:
        lines.append(f"数据最新 {metrics.get('latest_data_date')}"
                     f"（落后 {metrics.get('data_lag_trade')} 个交易日，"
                     f"应为 {metrics.get('expected_trade_date')}）")
    elif metrics.get("data_lag_days") is not None:
        lines.append(f"数据最新 {metrics.get('latest_data_date')}"
                     f"（自然日滞后 {metrics.get('data_lag_days')} 天）")
    return "\n".join(lines)


def render_daily(metrics, issues) -> str:
    lines = [f"## 📊 qhyc 云端日报 {datetime.now(SH):%Y-%m-%d}", "",
             f"- 负载(5min)：**{metrics.get('load5')}** / {metrics.get('cores')} 核",
             f"- 内存：{metrics.get('mem_pct')}% （总 {metrics.get('mem_total_gb')} GB）",
             f"- 磁盘：{metrics.get('disk_pct')}% （剩余 {metrics.get('disk_free_gb')} GB）",
             f"- 容器：{'全部正常' if not metrics.get('containers_down') else '异常 ' + str(metrics.get('containers_down'))}",
             f"- 系统服务：{'全部正常' if not metrics.get('units_down') else '异常 ' + str(metrics.get('units_down'))}",
             f"- API /health：{metrics.get('api_code')}｜DB：{'ok' if metrics.get('db_ok') else 'DOWN'}",
             ]
    if metrics.get("latest_data_date"):
        if metrics.get("data_lag_trade") is not None:
            lines.append(f"- 数据最新：{metrics.get('latest_data_date')}"
                         f"（落后 {metrics.get('data_lag_trade')} 个交易日，"
                         f"应为 {metrics.get('expected_trade_date')}）")
        else:
            lines.append(f"- 数据最新：{metrics.get('latest_data_date')}"
                         f"（自然日滞后 {metrics.get('data_lag_days')} 天）")
    if metrics.get("push_silence_h") is not None:
        lines.append(f"- 距上次成功推送：{metrics.get('push_silence_h')} 小时")
    if metrics.get("market_age_h") is not None:
        lines.append(f"- 距最新行情数据：{metrics.get('market_age_h')} 小时"
                     f"（> {MARKET_LIVE_H}h 视为休市，静默不告警）")
    if issues:
        lines += ["", "**当前未恢复的问题**："] + [f"- {d}" for _, _, d in issues]
    else:
        lines += ["", "✅ 全部指标正常"]
    return "\n".join(lines)


def main() -> int:
    daily = "--daily" in sys.argv
    token = load_token()
    data = collect()
    metrics, issues = data["metrics"], data["issues"]

    # 监控自身的健康状况也算一项检查：冷却状态写不进去 ⇒ 「不重复打扰」这个承诺
    # 已经失效，必须让用户知道，而不是让它在日志里静默一个月。
    werr = state_writable()
    if werr:
        # 切到 /tmp 兜底：告警本身必须发得出去，冷却也继续精确生效。
        _orig_dir = STATE_DIR
        globals()["STATE_DIR"] = tempfile.gettempdir()
        globals()["STATE_FILE"] = FALLBACK_STATE_FILE
        issues.append(("warn", "state_unwritable",
                       f"监控冷却状态目录不可写 {_orig_dir}（{werr}）"
                       f"——已临时改用 {FALLBACK_STATE_FILE}，请修权限/属主"))
    if daily:
        push("qhyc 云端日报", render_daily(metrics, issues), token)
        return 0

    sent = 0
    for level, key, desc in issues:
        if cooldown_ok(key, level):
            icon = "🚨" if level == "crit" else "⚠️"
            if push(f"{icon} qhyc {key} 告警", render_alert([(level, key, desc)], metrics), token):
                sent += 1
    print(f"[monitor] 检查完成：{len(issues)} 项问题，推送 {sent} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
