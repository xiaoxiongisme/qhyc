#!/usr/bin/env python3
"""qhyc 运维：资源与服务监控告警（推送到微信 pushplus）

检查项：
  1. 系统：负载(5min) / 内存 / 根分区磁盘
  2. 容器：4 个 qhyc 容器是否 running
  3. 服务：API /health 是否 200；TimescaleDB 是否可查询
  4. 数据：daily_bar 最新交易日距今滞后天数（回迁期可放宽）
  5. 推送：fusion_push_log 最近一次推送是否成功（超时 24h 告警）

告警经 pushplus（与 qhyc 同一通道）发到微信；同一项在冷却期内不重复打扰。
每日 08:00 可发一份"体检日报"：python3 ops_monitor.py --daily

cron：
  */10 * * * * /usr/bin/python3 /opt/qhyc-ops/ops_monitor.py >> /var/log/qhyc_ops_monitor.log 2>&1
  0 8 * * *   /usr/bin/python3 /opt/qhyc-ops/ops_monitor.py --daily >> /var/log/qhyc_ops_monitor.log 2>&1
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SH = ZoneInfo("Asia/Shanghai")
STATE_DIR = "/opt/qhyc-ops/state"
STATE_FILE = os.path.join(STATE_DIR, "alert_state.json")
ENV_FILE = os.environ.get("QHYC_ENV_FILE", "/Docker/qhyc/.env")
CONTAINERS = ["qhyc-timescaledb", "qhyc-api", "qhyc-scheduler", "qhyc-pipeline"]

# 阈值（4 核 / 8G 机型标定；改机型请同步调 LOAD_WARN）
LOAD_WARN_RATIO = 1.5      # load5 > cores × 1.5
MEM_WARN = 85.0            # %
DISK_WARN = 85.0           # %
DISK_CRIT = 93.0           # %
DATA_LAG_WARN = 2          # 天
PUSH_SILENCE_WARN = 24     # 小时
COOLDOWN_SEC = 3600        # 同类告警冷却 1 小时
COOLDOWN_CRIT = 1800       # 严重告警冷却 30 分钟


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
    except FileNotFoundError:
        pass
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


def cooldown_ok(key: str, level: str) -> bool:
    """冷却期内不重复推送同一项。"""
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        st = json.load(open(STATE_FILE, encoding="utf-8")) if os.path.exists(STATE_FILE) else {}
    except Exception:  # noqa: BLE001
        st = {}
    last = float(st.get(key, 0))
    cd = COOLDOWN_CRIT if level == "crit" else COOLDOWN_SEC
    if time.time() - last < cd:
        return False
    st[key] = time.time()
    try:
        json.dump(st, open(STATE_FILE, "w", encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return True


def psql(sql: str, timeout: int = 30) -> tuple[bool, str]:
    rc, out = sh(["docker", "exec", "qhyc-timescaledb", "psql", "-U", "futures",
                  "-d", "futures", "-t", "-A", "-c", sql], timeout=timeout)
    return rc == 0, out


def collect() -> dict:
    """采集全部指标。返回 {指标: 值} 与问题列表。"""
    m: dict = {}
    issues: list[tuple[str, str, str]] = []   # (level, key, 描述)

    # 1. 系统资源
    cores = os.cpu_count() or 4
    load5 = os.getloadavg()[1]
    m["load5"] = round(load5, 2)
    m["cores"] = cores
    if load5 > cores * LOAD_WARN_RATIO:
        issues.append(("warn", "load", f"5 分钟负载 {load5:.2f} > {cores * LOAD_WARN_RATIO:.1f}（{cores} 核）"))

    total, used, free = shutil.disk_usage("/")
    disk_pct = used / total * 100
    m["disk_pct"] = round(disk_pct, 1)
    m["disk_free_gb"] = round(free / 1024 ** 3, 1)
    if disk_pct >= DISK_CRIT:
        issues.append(("crit", "disk", f"根分区已用 {disk_pct:.1f}%（剩余 {m['disk_free_gb']} GB）"))
    elif disk_pct >= DISK_WARN:
        issues.append(("warn", "disk", f"根分区已用 {disk_pct:.1f}%（剩余 {m['disk_free_gb']} GB）"))

    meminfo = {}
    try:
        for line in open("/proc/meminfo", encoding="utf-8"):
            k, v = line.split(":", 1)
            meminfo[k.strip()] = int(v.split()[0])
    except Exception:  # noqa: BLE001
        pass
    mem_total = meminfo.get("MemTotal", 1)
    mem_avail = meminfo.get("MemAvailable", mem_total)
    mem_pct = (mem_total - mem_avail) / mem_total * 100
    m["mem_pct"] = round(mem_pct, 1)
    m["mem_total_gb"] = round(mem_total / 1024 ** 2, 1)
    if mem_pct >= MEM_WARN:
        issues.append(("warn", "mem", f"内存已用 {mem_pct:.1f}%（总 {m['mem_total_gb']} GB）"))

    # swap（有使用说明在吃 swap，值得提示）
    swap_total = meminfo.get("SwapTotal", 0)
    swap_free = meminfo.get("SwapFree", swap_total)
    if swap_total:
        m["swap_pct"] = round((swap_total - swap_free) / swap_total * 100, 1)

    # 2. 容器
    down = []
    for c in CONTAINERS:
        rc, out = sh(["docker", "inspect", "-f", "{{.State.Running}}", c])
        if rc != 0 or out.strip() != "true":
            down.append(c)
    m["containers_down"] = down
    if down:
        issues.append(("crit", "container", "容器未运行：" + ", ".join(down)))

    # 3. 服务
    rc, out = sh(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                  "--max-time", "15", "http://127.0.0.1:8000/health"])
    m["api_code"] = out.strip() or str(rc)
    if out.strip() != "200":
        issues.append(("crit", "api", f"/health 返回 {m['api_code']}（期望 200）"))

    ok, _ = psql("SELECT 1")
    m["db_ok"] = ok
    if not ok:
        issues.append(("crit", "db", "TimescaleDB 不可查询"))

    # 4. 数据新鲜度（表不存在/空表时跳过，避免回迁期误报）
    ok, out = psql("SELECT max(trade_date)::text FROM daily_bar")
    m["latest_data_date"] = out.strip() if ok and out.strip() else None
    if m["latest_data_date"] and m["latest_data_date"] not in ("", "None"):
        try:
            d = datetime.strptime(m["latest_data_date"], "%Y-%m-%d").date()
            lag = (datetime.now(SH).date() - d).days
            m["data_lag_days"] = lag
            if lag > DATA_LAG_WARN:
                issues.append(("warn", "data_lag", f"日线数据滞后 {lag} 天（最新 {m['latest_data_date']}）"))
        except ValueError:
            pass

    # 5. 推送活性（仅交易日/有数据时检查：无数据时本就静默）
    ok, out = psql(
        "SELECT max(pushed_at)::text FROM fusion_push_log WHERE delivered")
    m["last_push_at"] = out.strip() if ok and out.strip() else None
    if m["last_push_at"] and m["last_push_at"] not in ("", "None"):
        try:
            t = datetime.fromisoformat(m["last_push_at"]).astimezone(timezone.utc)
            silence_h = (datetime.now(timezone.utc) - t).total_seconds() / 3600
            m["push_silence_h"] = round(silence_h, 1)
            if silence_h > PUSH_SILENCE_WARN:
                issues.append(("warn", "push_silence",
                               f"已 {silence_h:.0f} 小时无成功推送（信号链路可能停摆）"))
        except ValueError:
            pass

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
    if metrics.get("data_lag_days") is not None:
        lines.append(f"数据最新 {metrics.get('latest_data_date')}（滞后 {metrics.get('data_lag_days')} 天）")
    return "\n".join(lines)


def render_daily(metrics, issues) -> str:
    lines = [f"## 📊 qhyc 云端日报 {datetime.now(SH):%Y-%m-%d}", "",
             f"- 负载(5min)：**{metrics.get('load5')}** / {metrics.get('cores')} 核",
             f"- 内存：{metrics.get('mem_pct')}% （总 {metrics.get('mem_total_gb')} GB）",
             f"- 磁盘：{metrics.get('disk_pct')}% （剩余 {metrics.get('disk_free_gb')} GB）",
             f"- 容器：{'全部正常' if not metrics.get('containers_down') else '异常 ' + str(metrics.get('containers_down'))}",
             f"- API /health：{metrics.get('api_code')}｜DB：{'ok' if metrics.get('db_ok') else 'DOWN'}",
             ]
    if metrics.get("latest_data_date"):
        lines.append(f"- 数据最新：{metrics.get('latest_data_date')}"
                     f"（滞后 {metrics.get('data_lag_days')} 天）")
    if metrics.get("push_silence_h") is not None:
        lines.append(f"- 距上次成功推送：{metrics.get('push_silence_h')} 小时")
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
