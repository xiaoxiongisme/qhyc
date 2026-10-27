# -*- coding: utf-8 -*-
"""GitHub ↔ 生产一致性守卫（只读，绝不改动工作树）。

背景
----
历史上出现过「生产代码与 GitHub 不一致」：生产机器上被手工热修（留下 .bak_*
残留），或本地/服务器各自领先对方若干提交，导致「以为跑的是 A 版本、实际是 B
版本」。这类偏差极难察觉——服务照常运行、日志正常，直到某个行为对不上才暴露。

本脚本把「一致性」变成**每日可断言的事实**，判定三条：

    C1 分支     ：当前检出分支 == 期望分支（默认 cb/dev）
    C2 提交对齐 ：HEAD == origin/<branch>（既不能领先也不能落后）
    C3 工作树   ：无已跟踪文件的改动（untracked 不算 drift，仅提示）

任一条不满足即 **exit 1** 并推送告警（pushplus → 备用 webhook），便于每日巡检。

跑法（生产机，systemd 同款环境）
--------------------------------
    set -a; . /home/ubuntu/qhyc_work/qhyc.env; set +a
    cd /Docker/qhyc && /home/ubuntu/qhyc_work/venv/bin/python scripts/check_git_prod_sync.py

    # 只打印不告警（人工核对时用）
    ... scripts/check_git_prod_sync.py --no-alert

建议 crontab（每日 09:00，早于盘前作业）
----------------------------------------
    0 9 * * * set -a && . /home/ubuntu/qhyc_work/qhyc.env && set +a && \
        cd /Docker/qhyc && /home/ubuntu/qhyc_work/venv/bin/python \
        scripts/check_git_prod_sync.py >> /home/ubuntu/qhyc_work/logs/git_sync_check.log 2>&1

退出码：0=一致，1=漂移（已告警），2=检查本身失败（如 git 不可用）。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

EXPECT_BRANCH_DEFAULT = "cb/dev"


def _git(repo: str, *args: str, check: bool = True) -> str:
    p = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, timeout=120,
    )
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败：{p.stderr.strip()}")
    return p.stdout.strip()


def _alert(title: str, content: str) -> None:
    try:
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from app.notify.pushplus import send_notify  # noqa: PLC0415

        ok, ch = send_notify(title, content)
        print(f"[alert] 已推送（{ch}）：ok={ok}")
    except Exception as e:  # noqa: BLE001
        print(f"[alert] 推送失败：{type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description="GitHub ↔ 生产一致性守卫")
    ap.add_argument("--repo", default=os.getenv("QHYC_REPO", "/Docker/qhyc"))
    ap.add_argument("--branch", default=os.getenv("QHYC_EXPECT_BRANCH", EXPECT_BRANCH_DEFAULT))
    ap.add_argument("--no-alert", action="store_true", help="只打印，不推送告警")
    args = ap.parse_args()

    print(f"[sync] repo={args.repo} expect_branch={args.branch}")

    # 0) fetch：把远端最新引用拉下来（只更新 refs，不动工作树）
    try:
        _git(args.repo, "fetch", "origin", args.branch, "--quiet")
    except Exception as e:  # noqa: BLE001
        msg = f"无法 fetch origin/{args.branch}：{e}"
        print(f"[sync] ERROR {msg}")
        if not args.no_alert:
            _alert("[QHYC] 一致性检查失败", msg)
        return 2

    try:
        branch = _git(args.repo, "branch", "--show-current")
        head = _git(args.repo, "rev-parse", "HEAD")
        remote = _git(args.repo, "rev-parse", f"origin/{args.branch}")
        ahead = int(_git(args.repo, "rev-list", "--count", f"origin/{args.branch}..HEAD"))
        behind = int(_git(args.repo, "rev-list", "--count", f"HEAD..origin/{args.branch}"))
        # 已跟踪文件的改动（M/A/D/R），不含 ?? untracked
        tracked_dirty = [
            ln for ln in _git(args.repo, "status", "--porcelain").splitlines()
            if ln.strip() and not ln.startswith("??")
        ]
        untracked = [
            ln[3:] for ln in _git(args.repo, "status", "--porcelain").splitlines()
            if ln.startswith("??")
        ]
    except Exception as e:  # noqa: BLE001
        msg = f"读取仓库状态失败：{e}"
        print(f"[sync] ERROR {msg}")
        if not args.no_alert:
            _alert("[QHYC] 一致性检查失败", msg)
        return 2

    print(f"[sync] branch={branch} HEAD={head[:12]} origin/{args.branch}={remote[:12]}")
    print(f"[sync] ahead={ahead} behind={behind} tracked_dirty={len(tracked_dirty)} "
          f"untracked={len(untracked)}")
    for ln in tracked_dirty[:20]:
        print(f"[sync]   DIRTY {ln}")
    for u in untracked[:10]:
        print(f"[sync]   UNTRACKED {u}")

    problems: list[str] = []
    if branch != args.branch:
        problems.append(f"C1 分支不符：当前 {branch!r} ≠ 期望 {args.branch!r}")
    if head != remote:
        problems.append(
            f"C2 提交漂移：HEAD {head[:12]} ≠ origin/{args.branch} {remote[:12]}"
            f"（领先 {ahead} / 落后 {behind}）")
    if tracked_dirty:
        problems.append(
            f"C3 工作树有 {len(tracked_dirty)} 处已跟踪改动（疑似生产手工热修）："
            + "; ".join(tracked_dirty[:5]))

    if not problems:
        print("[sync] OK：GitHub ↔ 生产一致")
        return 0

    content = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(problems))
    content += (
        f"\n\n仓库：{args.repo}\nHEAD：{head}\n远端：{remote}\n"
        f"处置：确认后在生产执行 git fetch origin {args.branch} && "
        f"git reset --hard origin/{args.branch}（先确认无未提交的有效改动）。"
    )
    print("[sync] DRIFT:\n" + content)
    if not args.no_alert:
        _alert("[QHYC] GitHub 与生产代码不一致", content)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
