#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日 bar 一致性巡检封装：串行跑两守卫，任一失败即 pushplus 告警。

为什么需要它
------------
2026-10-31 连续两次事故的根因，都是「代码层口径」与「数据层结果」分叉，而这类
分叉**不会报错、行数也看着正常**，只能靠守卫定期断言提前拦住：

* ``check_bar_grid_consistency.py`` —— 增量合成（synthesizer._SESSION_DAY_BOUNDS）
  与全量重建（rebuild_bars_session.DAY_BOUNDS）两侧的网格定义必须逐位相同；
  否则下一次增量又会把修好的 5m/15m 改回 13:30/10:30，两边无限拉锯。
* ``check_cross_tf_consistency.py`` —— 低周期 bar 两两聚合后必须精确等于高周期
  bar（30m==15m、60m==30m）。它抓到了「双源末根重复计入」那种纯看网格时刻
  发现不了的真 bug（volume 翻倍、min(low) 吃脏值、收盘价自相矛盾）。

两个守卫各自 exit 0/1，但**都不自带告警**。本封装把它们串起来跑一遍，任一失败
就通过 pushplus 推送告警（与 cron_git_sync_check 的自告警行为一致），并把两路
输出都落进日志，便于事后对账。

挂法（与 cron_git_sync_check 完全一致，走 task_runner 包装 + ops.scheduled_task 字典）::

    30 9 * * * /usr/bin/python3 /Docker/qhyc/scripts/ops/task_runner.py \
        --task-id cron_bar_consistency_check \
        --shell-cmd 'set -a; . /home/ubuntu/qhyc_work/qhyc.env; set +a; \
                     cd /Docker/qhyc && /home/ubuntu/qhyc_work/venv/bin/python \
                     scripts/validate/run_bar_consistency_daily.py \
                     >> /home/ubuntu/qhyc_work/logs/qhyc_bar_consistency.log 2>&1' \
        >> /home/ubuntu/qhyc_work/logs/qhyc_task_runner.log 2>&1

退出码：0=全绿；1=任一守卫失败（已告警）；2=封装自身异常。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
GUARD_GRID = REPO_ROOT / "scripts/validate/check_bar_grid_consistency.py"
GUARD_CROSS = REPO_ROOT / "scripts/validate/check_cross_tf_consistency.py"


def _run_guard(script: Path, extra_args: list[str], env: dict) -> tuple[int, str]:
    """运行单个守卫，返回 (退出码, 合并输出)。"""
    cmd = [sys.executable, str(script), *extra_args]
    try:
        p = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        return 2, f"[TIMEOUT] {script.name} 超过 600s"
    except Exception as e:  # noqa: BLE001
        return 2, f"[{type(e).__name__}] {script.name}: {e}"
    out = (p.stdout or "") + (p.stderr or "")
    return p.returncode, out.strip()


def _alert(title: str, content: str) -> None:
    try:
        sys.path.insert(0, str(REPO_ROOT))
        from app.notify.pushplus import send_notify  # noqa: PLC0415

        ok, ch = send_notify(title, content)
        print(f"[alert] 已推送（{ch}）：ok={ok}")
    except Exception as e:  # noqa: BLE001
        print(f"[alert] 推送失败：{e}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30,
                    help="跨周期校验回溯天数（默认 30）")
    ap.add_argument("--no-alert", action="store_true", help="只打印不告警（人工核对用）")
    args = ap.parse_args()

    # 继承当前环境（cron 已 source qhyc.env → 含 DATABASE_URL）；显式补 PYTHONPATH
    # 以让 cross_tf 守卫能 import app（守卫自身也会 sys.path.insert，这里双保险）。
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")

    failures: list[str] = []

    # ---- C1 网格定义两侧一致（纯 AST，无需 DB）----
    rc, out = _run_guard(GUARD_GRID, [], env)
    print("===== check_bar_grid_consistency =====")
    print(out)
    if rc != 0:
        failures.append(f"check_bar_grid_consistency 退出码={rc}\n{out}")

    # ---- C2 跨周期聚合自洽（需 DB）----
    rc, out = _run_guard(GUARD_CROSS, ["--days", str(args.days)], env)
    print("===== check_cross_tf_consistency =====")
    print(out)
    if rc != 0:
        failures.append(f"check_cross_tf_consistency 退出码={rc}\n{out}")

    if failures:
        msg = "\n\n".join(failures)
        print(f"\n[FAIL] bar 一致性巡检未通过（{len(failures)} 项）")
        if not args.no_alert:
            _alert("【qhyc 每日巡检】bar 一致性守卫告警", msg[:4000])
        return 1

    print("\n[OK] bar 一致性巡检全部通过：网格定义一致 + 跨周期聚合自洽")
    return 0


if __name__ == "__main__":
    sys.exit(main())
