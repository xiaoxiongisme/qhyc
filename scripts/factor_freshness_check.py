#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""因子级新鲜度巡检 + **自动补算**（每日调用）。

为什么需要它（2026-11-07 实测）
------------------------------
`app/ops/alerting.py::_check_freshness` 是**表级**检查：只看 ``factor_value`` 整表的
``max(trade_date)`` ⇒ **发现不了「某个因子停更」**。实测事故：22 个已注册因子里，
16 个停在 **2026-09-30**，而整表 max 是 11-06（v1v6 的 4 个新因子在更新）⇒
**表级巡检一直显示"正常"，断更 40 天无人发现**。这正是本项目「开关看似生效、
实则静默空转」缺陷的又一例。

本脚本做三件事：
  1. **因子级**新鲜度：逐个 ``factor_registry.factor_id`` 查 ``factor_value`` 的
     ``max(trade_date)``，滞后 > 阈值（默认 3 天）→ 断供。
  2. **自动补算**：按因子归属调对应计算脚本（单一事实源 = 各脚本自己的因子清单），
     补算窗口取「滞后天数 + 缓冲」，**只补该断供因子所属的组**（不重跑全库）。
  3. **补后复查仍断供 → 硬告警**（``app.ops.alerting.send_alert``），
     并把结果写 ``ops.scheduled_task`` 口径的日志，便于次日对比。

用法::

    python scripts/factor_freshness_check.py                # 巡检 + 自动补算
    python scripts/factor_freshness_check.py --report-only   # 只报告不补算
    python scripts/factor_freshness_check.py --lag-days 5    # 自定义阈值

⚠ 口径说明
  * 补算是**幂等 UPSERT**（各 compute_* 脚本都用 ON CONFLICT DO UPDATE）→ 重复补算安全。
  * 只对**已注册**因子补算；``factor_registry`` 里没有的不管（避免给野因子发活）。
  * ``f_atr_pctile`` 需要 **250 交易日**历史（ATR 分位），v1v6 作业的 60 天窗口算不出它
    ⇒ 补算该因子时自动把 v1v6 的 since 放宽到 ``FACTOR_ATR_PCTILE_DAYS``（默认 420 自然日）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
_SCRIPTS = _ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import psycopg2  # noqa: E402

from pgconn import add_conn_args, conn_from_args  # noqa: E402

#: 因子归属 → 补算脚本。用各脚本**自己的**因子清单做单一事实源，避免硬编码漂移。
try:
    from compute_factor_value import A_FACTORS  # noqa: E402
except Exception:  # noqa: BLE001 —— 导入失败时退化为空集（仍能巡检，只是不自动补算）
    A_FACTORS = ()
try:
    from compute_factor_v1v6 import NEW_FACTORS  # noqa: E402
except Exception:  # noqa: BLE001
    NEW_FACTORS = ()

#: C 组（基差时序）因子：迁移 049 注册的 2 个
C_FACTORS = ("f_basis_mean60_z", "f_basis_chg5_z")

#: 需要长历史（>60 天）才能算出的因子 → 补算时放宽窗口
LONG_HISTORY_FACTORS = {"f_atr_pctile": 420}

#: 补算脚本 → 作业分组
GROUPS = ("A", "B", "C")


def _group_of(factor_id: str) -> str | None:
    """因子 → 组（A/B/C/v1v6）。B 组是"其余"（compute_b 无因子清单，按剩余归类）。"""
    if factor_id in A_FACTORS:
        return "A"
    if factor_id in C_FACTORS:
        return "C"
    if factor_id in NEW_FACTORS:
        return "v1v6"
    return "B"


def fetch_freshness(cur) -> list[tuple[str, object, int]]:
    """返回 [(factor_id, max_trade_date, rows)]：注册因子 × factor_value 实际新鲜度。"""
    cur.execute(
        "SELECT r.factor_id, v.max_td, v.n FROM factor_registry r "
        "LEFT JOIN (SELECT factor_id, max(trade_date) AS max_td, count(*) AS n "
        "           FROM factor_value GROUP BY factor_id) v "
        "  ON v.factor_id = r.factor_id ORDER BY r.factor_id"
    )
    return [(r[0], r[1], int(r[2] or 0)) for r in cur.fetchall()]


def stale_factors(rows, lag_days: int, today: date) -> list[dict]:
    """挑出断供因子（滞后 > lag_days 或完全无数据）。"""
    out = []
    for fid, max_td, n in rows:
        if max_td is None:
            out.append({"factor_id": fid, "max_td": None, "lag_days": 99999, "rows": n})
            continue
        d = max_td.date() if hasattr(max_td, "date") else max_td
        lag = (today - d).days
        if lag > lag_days:
            out.append({"factor_id": fid, "max_td": d, "lag_days": lag, "rows": n})
    return out


def _run(cmd: list[str], timeout: int) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, ((p.stdout or "") + (p.stderr or ""))[-1500:]
    except subprocess.TimeoutExpired:
        return -1, "timeout>%ds" % timeout
    except Exception as e:  # noqa: BLE001
        return -2, str(e)[:300]


def backfill(stale: list[dict], today: date) -> list[dict]:
    """按组补算。只补真正断供的组，且每组只跑一次。"""
    results = []
    groups_needed = sorted({_group_of(s["factor_id"]) for s in stale} - {None})
    v1v6_long = any(f in LONG_HISTORY_FACTORS for f in (s["factor_id"] for s in stale))

    for g in groups_needed:
        if g == "A":
            since = (today - timedelta(days=int(os.getenv("FACTOR_FRESH_LOOKBACK_DAYS", "60")))).isoformat()
            cmd = [sys.executable, str(_SCRIPTS / "compute_factor_value.py"),
                   "--groups", "A", "--since", since]
        elif g == "B":
            since = (today - timedelta(days=int(os.getenv("FACTOR_FRESH_LOOKBACK_DAYS", "60")))).isoformat()
            cmd = [sys.executable, str(_SCRIPTS / "compute_factor_value.py"),
                   "--groups", "B", "--since", since]
        elif g == "C":
            since = (today - timedelta(days=int(os.getenv("FACTOR_FRESH_LOOKBACK_DAYS", "60")))).isoformat()
            cmd = [sys.executable, str(_SCRIPTS / "compute_factor_value.py"),
                   "--groups", "C", "--since", since]
        elif g == "v1v6":
            days = int(os.getenv("FACTOR_ATR_PCTILE_DAYS", "420")) if v1v6_long else \
                int(os.getenv("FACTOR_V1V6_LOOKBACK_DAYS", "60"))
            since = (today - timedelta(days=days)).isoformat()
            cmd = [sys.executable, str(_SCRIPTS / "compute_factor_v1v6.py"), "--since", since]
        else:
            continue
        print(f"[fresh] 补算组 {g}: {' '.join(cmd[1:])}", flush=True)
        rc, out = _run(cmd, timeout=int(os.getenv("FACTOR_FRESH_TIMEOUT", "3600")))
        print(f"[fresh]   exit={rc} tail={out[-400:]}", flush=True)
        results.append({"group": g, "rc": rc, "cmd": " ".join(cmd[1:]), "tail": out[-400:]})
    return results


def main() -> int:
    ap = argparse.ArgumentParser(description="因子级新鲜度巡检 + 自动补算")
    ap.add_argument("--lag-days", type=int,
                    default=int(os.getenv("FACTOR_FRESH_LAG_DAYS", "3")))
    ap.add_argument("--report-only", action="store_true", help="只报告不补算")
    ap.add_argument("--no-alert", action="store_true", help="不推送告警")
    add_conn_args(ap)
    a = ap.parse_args()

    today = date.today()
    conn = psycopg2.connect(**conn_from_args(a))
    try:
        cur = conn.cursor()
        rows = fetch_freshness(cur)
        stale = stale_factors(rows, a.lag_days, today)
        print(f"[fresh] 注册因子 {len(rows)} 个；断供（滞后 > {a.lag_days} 天）{len(stale)} 个")
        for s in stale:
            print(f"[fresh]   断供 {s['factor_id']}  最新={s['max_td']} 滞后={s['lag_days']}天 行数={s['rows']}")
        if not stale:
            print("[fresh] ✅ 全部因子新鲜")
            return 0

        if a.report_only:
            return 1

        results = backfill(stale, today)

        # 补后复查（同一连接可能看不到新提交 → 用新连接）
        conn.commit()
        conn2 = psycopg2.connect(**conn_from_args(a))
        try:
            cur2 = conn2.cursor()
            rows2 = fetch_freshness(cur2)
        finally:
            conn2.close()
        still = stale_factors(rows2, a.lag_days, today)
        print(f"[fresh] 补算后仍断供 {len(still)} 个: {[s['factor_id'] for s in still]}")

        if still and not a.no_alert:
            try:
                from app.ops.alerting import send_alert

                body = "\n".join(
                    "- {factor_id} 最新={max_td} 滞后={lag_days}天".format(**s) for s in still)
                ok, note = send_alert(
                    "[qhyc] CRIT 因子断供（补算后仍无数据）: {0} 个".format(len(still)),
                    body, key="factor_stale")
                print(f"[fresh] 告警已发={ok} note={note}")
            except Exception as e:  # noqa: BLE001
                print(f"[fresh] 告警发送失败（不阻断）: {e}")
        return 1 if still else 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
