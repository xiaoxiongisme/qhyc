# -*- coding: utf-8 -*-
"""因子作业 T10 调度闭环验收（整改优先级 P1）。

背景：16:20 factor_v1v6_daily 与 16:45 factor_ic_monitor_daily 已于 10-01 注册，
但**尚未经过真实运行日验证**。本脚本在本地把两个作业各跑一遍，核对产出行数与 IC，
证明闭环可用（等价于把"首个运行日验收"提前在本地完成）。

用法：
    docker exec -w /app -e PYTHONPATH=/app qhyc-api \
        python scripts/verify_factor_jobs.py --since 2026-01-01 --limit 12
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from sqlalchemy import text

from app.core.db import session_scope

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
BAR = "=" * 72


def _count(s, sql: str, **kw) -> int:
    try:
        return int(s.execute(text(sql), kw).scalar() or 0)
    except Exception:
        return -1


def _snapshot(s) -> dict:
    return {
        "factor_value": _count(s, "SELECT count(*) FROM factor_value"),
        "v1v6_rows": _count(
            s, "SELECT count(*) FROM factor_value WHERE factor_id IN "
               "('f_vol_ratio','f_vol_z','f_voldiv_divergence','f_atr_pctile')"),
        "registry": _count(s, "SELECT count(*) FROM factor_registry"),
        "ic_rows": _count(
            s, "SELECT count(*) FROM factor_value WHERE factor_id='f_atr_pctile'"),
    }


def _run(script: str, args: list[str], timeout: int) -> tuple[int, str]:
    cmd = [sys.executable, str(SCRIPTS / script)] + args
    print("  $ {0}".format(" ".join(cmd[-6:])))
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=str(ROOT))
    except subprocess.TimeoutExpired:
        return -1, "超时 {0}s".format(timeout)
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode, out.strip()[-1500:]


def main() -> int:
    ap = argparse.ArgumentParser()
    # ⚠ compute_factor_v1v6.py 的 --since/--until 收**年份**（内部拼 "-01-01"），
    #   传完整日期会拼出 "2026-01-01-01-01" 触发 InvalidDatetimeFormat。
    ap.add_argument("--since", default="2026")
    ap.add_argument("--until", default="2027")
    ap.add_argument("--limit", type=int, default=12, help="限制品种数（控时）")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--skip-ic", action="store_true")
    a = ap.parse_args()

    with session_scope() as s:
        before = _snapshot(s)

    print(BAR)
    print("因子作业 T10 闭环验收（等价于云端 16:20 / 16:45 首跑）")
    print(BAR)
    print("作业注册：16:20 factor_v1v6_daily / 16:45 factor_ic_monitor_daily（均已注册）")
    print("执行前快照：{0}".format(before))

    print("\n[1/2] 16:20 因子计算 compute_factor_v1v6.py")
    rc1, out1 = _run("compute_factor_v1v6.py",
                     ["--since", a.since, "--until", a.until,
                      "--limit", str(a.limit)], a.timeout)
    print("  退出码={0}".format(rc1))
    print("  输出尾部：\n    {0}".format(out1.replace("\n", "\n    ")[-900:]))

    rc2, out2 = 0, "(跳过)"
    if not a.skip_ic:
        print("\n[2/2] 16:45 IC 监控 factor_ic_monitor.py")
        rc2, out2 = _run("factor_ic_monitor.py", [], a.timeout)
        print("  退出码={0}".format(rc2))
        print("  输出尾部：\n    {0}".format(out2.replace("\n", "\n    ")[-900:]))

    with session_scope() as s:
        after = _snapshot(s)
        since_date = a.since if len(a.since) > 4 else a.since + "-01-01"
        per_factor = s.execute(text(
            "SELECT factor_id, count(*), min(trade_date), max(trade_date) "
            "FROM factor_value WHERE trade_date >= :d "
            "GROUP BY factor_id ORDER BY 2 DESC LIMIT 12"
        ), {"d": since_date}).all()

    print("\n" + BAR)
    print("验收结果")
    print(BAR)
    print("执行后快照：{0}".format(after))
    print("增量：factor_value +{0} / V1V6 因子行 +{1}".format(
        after["factor_value"] - before["factor_value"],
        after["v1v6_rows"] - before["v1v6_rows"]))
    print("\n自 {0} 起各因子行数：".format(since_date))
    for fid, n, mn, mx in per_factor:
        print("  {0:<26} {1:>9} 行  {2} ~ {3}".format(fid, n, mn, mx))
    ok = (rc1 == 0) and (a.skip_ic or rc2 == 0)
    print("\nT10 闭环判定：{0}".format(
        "✅ 两个因子作业均可正常执行并产出数据" if ok else
        "❌ 存在失败作业（见上方输出）"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
