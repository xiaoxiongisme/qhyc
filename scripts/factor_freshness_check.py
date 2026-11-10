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

★ 2026-11-10（CL-20261110-15 E2/E3）：补上**逐交易日覆盖**判据。
事故：``factor_freshness_check.py --report-only`` 11-10 只报 3 个断供，
但 ``f_atr_pctile`` 实际 11-09/11-10 **逐日 0 行**、11-07 只有 11 行（正常 80）。
两个原因叠加：① ``fetch_freshness`` 只看整表 ``max(trade_date)``，
11-07 残留的 11 行把 max 顶到 11-07，``lag=3`` 恰好不满足 ``lag > 3``；
② 因此 420 天长窗口补算（``LONG_HISTORY_FACTORS``）**永不触发**，
而 16:20 日常作业只给 60 天窗口，算不出 250 交易日 ATR 分位。
新增 ``fetch_daily_coverage`` + ``coverage_gaps``：对最近 N 个已结束交易日，
逐因子逐日数行；近期活跃（窗口内有数据）却在某日 0 行的因子照样进 ``stale``，
从而触发对应组的长窗口补算。``lag > 3`` 的边界本身保留（改 ``>=`` 会在周末误报），
由逐日判据兜住。

⚠ ``factor_registry.enabled`` 是**信号权重开关**，不是计算开关：实测 29 个注册因子
里 22 个 ``enabled=f`` 仍每天写入 ``factor_value``。故巡检不按 enabled 过滤，
也不改它的值（CL-20261110-15 初判 E3 的归因已纠正）。

用法::

    python scripts/factor_freshness_check.py                # 巡检 + 自动补算
    python scripts/factor_freshness_check.py --report-only   # 只报告不补算
    python scripts/factor_freshness_check.py --lag-days 5    # 自定义阈值
    python scripts/factor_freshness_check.py --coverage-days 7 --min-daily-rows 1

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


#: 逐日覆盖判据窗口（已结束的交易日数）与单日最小行数。
#: 不含「今天」——盘中因子层本来就没跑完，把今天算进去会全量误报。
COVERAGE_TRADE_DAYS = 5
MIN_DAILY_ROWS = 1


def fetch_daily_coverage(cur, days: int = COVERAGE_TRADE_DAYS) -> list[tuple[str, object, int]]:
    """返回 [(factor_id, trade_date, rows)]，覆盖最近 ``days`` 个**已结束**交易日。

    交易日真源与 ``extend_index_8888`` / ``trading_window`` 一致：``daily_bar``
    的 distinct trade_date。只查窗口内的 ``factor_value``（分区裁剪），
    不做全表 GROUP BY。
    """
    cur.execute(
        "WITH td AS ("
        "  SELECT DISTINCT trade_date FROM daily_bar "
        "  WHERE trade_date < CURRENT_DATE ORDER BY trade_date DESC LIMIT %s"
        ") SELECT f.factor_id, td.trade_date, "
        "       coalesce((SELECT count(*) FROM factor_value v "
        "                 WHERE v.factor_id=f.factor_id AND v.trade_date=td.trade_date), 0) AS n "
        "FROM factor_registry f CROSS JOIN td "
        "ORDER BY f.factor_id, td.trade_date", (int(days),))
    return [(r[0], r[1], int(r[2] or 0)) for r in cur.fetchall()]


def coverage_gaps(cov_rows, min_daily_rows: int = MIN_DAILY_ROWS) -> list[dict]:
    """近期活跃但在某个交易日行数不足的因子（含逐日 0 行）。

    「近期活跃」= 窗口内至少一天有 >= min_daily_rows 行；窗口内全空的因子
    属于早已停更，交给 ``stale_factors`` 的 max 判据处理，避免重复告警。
    """
    from collections import defaultdict  # noqa: PLC0415

    by_f: dict[str, dict] = defaultdict(dict)
    all_days: set = set()
    for fid, td, n in cov_rows:
        by_f[fid][td] = int(n)
        all_days.add(td)
    days = sorted(all_days)
    out: list[dict] = []
    for fid, per_day in by_f.items():
        if not any(n >= min_daily_rows for n in per_day.values()):
            continue
        missing = [d for d in days if per_day.get(d, 0) < min_daily_rows]
        if missing:
            out.append({
                "factor_id": fid,
                "max_td": max((d for d, n in per_day.items() if n >= min_daily_rows),
                              default=None),
                "lag_days": 0,
                "rows": sum(per_day.values()),
                "reason": "coverage_gap",
                "missing_days": [str(d) for d in missing],
            })
    return out


def needs_attention(cur, lag_days: int, today: date,
                    coverage_days: int = COVERAGE_TRADE_DAYS,
                    min_daily_rows: int = MIN_DAILY_ROWS) -> list[dict]:
    """表级新鲜度 + 逐日覆盖，按 factor_id 去重合并（覆盖判据补齐后者的盲区）。"""
    out: dict[str, dict] = {}
    for s in stale_factors(fetch_freshness(cur), lag_days, today):
        s.setdefault("reason", "max_trade_date_lag")
        out[s["factor_id"]] = s
    for g in coverage_gaps(fetch_daily_coverage(cur, coverage_days), min_daily_rows):
        if g["factor_id"] in out:
            out[g["factor_id"]]["reason"] = "max_lag+coverage_gap"
            out[g["factor_id"]]["missing_days"] = g["missing_days"]
        else:
            out[g["factor_id"]] = g
    return list(out.values())


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
    ap.add_argument("--coverage-days", type=int,
                    default=int(os.getenv("FACTOR_FRESH_COVERAGE_DAYS",
                                         str(COVERAGE_TRADE_DAYS))))
    ap.add_argument("--min-daily-rows", type=int,
                    default=int(os.getenv("FACTOR_FRESH_MIN_DAILY_ROWS",
                                         str(MIN_DAILY_ROWS))))
    ap.add_argument("--report-only", action="store_true", help="只报告不补算")
    ap.add_argument("--no-alert", action="store_true", help="不推送告警")
    add_conn_args(ap)
    a = ap.parse_args()

    today = date.today()
    conn = psycopg2.connect(**conn_from_args(a))
    try:
        cur = conn.cursor()
        rows = fetch_freshness(cur)
        stale = needs_attention(cur, a.lag_days, today, a.coverage_days, a.min_daily_rows)
        gaps = [s for s in stale if "coverage_gap" in s.get("reason", "")]
        print(f"[fresh] 注册因子 {len(rows)} 个；需补算 {len(stale)} 个 "
              f"(滞后 > {a.lag_days} 天 或 近 {a.coverage_days} 个交易日逐日缺行；逐日缺口 {len(gaps)} 个)")
        for s in stale:
            extra = (" 缺日=" + ",".join(s["missing_days"])) if s.get("missing_days") else ""
            print(f"[fresh]   {s['factor_id']}  最新={s['max_td']} 滞后={s['lag_days']}天 "
                  f"行数={s['rows']} 判据={s.get('reason')}{extra}")
        if not stale:
            print("[fresh] ✅ 全部因子新鲜且逐日覆盖完整")
            return 0

        if a.report_only:
            return 1

        results = backfill(stale, today)

        # 补后复查（同一连接可能看不到新提交 → 用新连接）
        conn.commit()
        conn2 = psycopg2.connect(**conn_from_args(a))
        try:
            cur2 = conn2.cursor()
            still = needs_attention(cur2, a.lag_days, today,
                                    a.coverage_days, a.min_daily_rows)
        finally:
            conn2.close()
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
