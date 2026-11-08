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
    C4 运行态   ：qhyc-api 进程启动时间 >= 磁盘 app/ 最新代码落盘时间
    C5 部署链路 ：部署标记 sha == HEAD、容器内代码 sha == 宿主、bind 源可重启

C1–C3 只看**仓库**，C4/C5 看**跑起来的进程**。这个区分是 2026-11-08 用一次
真实事故换来的：qhyc-api 自 11-06 14:32 起未重启、无 reload，期间 app/ 有 13 次
提交，宿主与容器内文件 sha1 完全一致 —— C1–C3 全绿，但常驻服务跑的是旧代码。
更隐蔽的是，宿主 ``conftest.py`` 被误建成空目录后连 ``docker restart`` 都会失败
（runc not a directory），即「想重启都重启不了」。故 C5 同时体检 bind 挂载源。

任一条不满足即 **exit 1** 并推送告警（pushplus → 备用 webhook），便于每日巡检。
M5 紧急通道：显式加 ``--allow-runtime-drift`` 可跳过 C4/C5（只打印不告警），
用于紧急修复；必须 24h 内补 ops.change_log 登记并安排重启。不设隐式豁免。

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
from datetime import datetime, timezone
from pathlib import Path

EXPECT_BRANCH_DEFAULT = "cb/dev"
API_CONTAINER = "qhyc-api"
DEPLOY_MARK = ".deployed_sha"
#: C4 允许的时钟容差（秒）：pull 与 restart 之间本就有先后，别把正常发布报成漂移。
START_TOLERANCE_SEC = 5.0
#: C5 抽查的进程内代码文件（相对仓库根 /app 挂载根同路径）
PROBE_FILE = "app/ingest/synthesizer.py"


def _sh(cmd: str, timeout: int = 60) -> str:
    p = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
    return (p.stdout or "").strip()


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


def _runtime_checks(repo: str, head: str) -> tuple[list[str], list[str]]:
    """C4/C5：进程运行态与部署链路体检。返回 (问题列表, 观测值列表)。

    断言只看「此刻」：进程什么时候启动、磁盘代码什么时候落盘、容器里那份
    文件与宿主是否同一个字节、部署标记是不是本次 HEAD。不读任何历史流水，
    收口后立刻能转 PASS（守卫铁律：不能用时间衰减凑通过）。
    """
    bad: list[str] = []
    notes: list[str] = []
    state = _sh(f"docker inspect {API_CONTAINER} --format '{{{{.State.Status}}}}'")
    if state != "running":
        return [f"C4/C5 {API_CONTAINER} 状态={state!r}（非 running，服务不可用）"], notes

    # ---------------- C4：进程启动 vs 代码落盘 ----------------
    started_s = _sh(
        f"docker inspect {API_CONTAINER} --format '{{{{.State.StartedAt}}}}'")
    started = None
    try:
        started = datetime.strptime(started_s[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc).timestamp()
    except (ValueError, TypeError):
        bad.append(f"C4 无法解析 StartedAt={started_s!r}")
    app_dir = Path(repo) / "app"
    py_files = [p for p in app_dir.rglob("*.py") if "__pycache__" not in str(p)]
    latest = max((p.stat().st_mtime for p in py_files), default=None)
    if not py_files:
        bad.append("C4 app/ 下找不到 .py（仓库结构异常）")
    if started is not None and latest is not None:
        lag = latest - started
        notes.append(
            f"C4 进程启动={started_s[:19]}Z / 代码落盘="
            f"{datetime.fromtimestamp(latest, timezone.utc):%Y-%m-%d %H:%M:%S}Z"
            f" / 差={lag:+.0f}s")
        if lag > START_TOLERANCE_SEC:
            bad.append(
                f"C4 运行态滞后：代码落盘比 {API_CONTAINER} 启动晚 {lag:.0f}s "
                "——发布未双重启或有人绕过通道改了生产文件，进程仍加载旧模块")

    # ---------------- C5：部署标记 / 容器内代码 / 挂载可重启 ----------------
    mark = Path(repo) / DEPLOY_MARK
    if not mark.is_file():
        bad.append(f"C5 缺部署标记 {DEPLOY_MARK}"
                   "（发布未走 publish.py；首次启用需按当前 HEAD 建基线）")
    else:
        txt = mark.read_text(encoding="utf-8", errors="replace").split()
        mark_sha = txt[0] if txt else ""
        if not mark_sha or not head.startswith(mark_sha):
            bad.append(f"C5 部署标记 sha={mark_sha!r} ≠ HEAD={head[:12]}")
        else:
            notes.append(f"C5 部署标记={mark_sha} 对齐 HEAD")

    probe = app_dir / Path(PROBE_FILE).relative_to("app")
    host_sha = _sh(f"sha1sum {probe}").split()[0] if probe.is_file() else ""
    cont_sha = _sh(f"docker exec {API_CONTAINER} sha1sum /app/{PROBE_FILE}").split()[0]
    if not host_sha or not cont_sha:
        bad.append(f"C5 无法读取 {PROBE_FILE} 的代码 sha（宿主={host_sha!r} "
                   f"容器={cont_sha!r}）")
    elif host_sha != cont_sha:
        bad.append(f"C5 容器内 {PROBE_FILE} 与宿主字节不一致")
    else:
        notes.append(f"C5 {PROBE_FILE} sha1={host_sha[:12]} 宿主/容器一致")

    fmt = '{{range .Mounts}}{{if eq .Type "bind"}}{{.Source}}{{println}}{{end}}{{end}}'
    srcs = [s.strip() for s in _sh(
        f"docker inspect {API_CONTAINER} --format '{fmt}'").splitlines() if s.strip()]
    for p in srcs:
        if not os.path.exists(p):
            bad.append(f"C5 bind 源缺失：{p}（重启会失败）")
        elif "." in os.path.basename(p) and not os.path.isfile(p):
            bad.append(f"C5 bind 源应为文件却是目录：{p}"
                       "（docker restart 报 not a directory）")
    notes.append(f"C5 检查 {len(srcs)} 个 bind 挂载源")
    return bad, notes


def main() -> int:
    ap = argparse.ArgumentParser(description="GitHub ↔ 生产一致性守卫")
    ap.add_argument("--repo", default=os.getenv("QHYC_REPO", "/Docker/qhyc"))
    ap.add_argument("--branch", default=os.getenv("QHYC_EXPECT_BRANCH", EXPECT_BRANCH_DEFAULT))
    ap.add_argument("--no-alert", action="store_true", help="只打印，不推送告警")
    ap.add_argument("--allow-runtime-drift", action="store_true",
                    help="M5 紧急通道：本轮跳过 C4/C5（须 24h 内补 change_log 登记）")
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

    # ---- C4/C5 运行态 ----
    if args.allow_runtime_drift:
        print("[sync] ⚠ M5 紧急通道：本轮跳过 C4/C5 —— 必须 24h 内在 "
              "ops.change_log 补登记并安排 qhyc-api 重启")
    else:
        try:
            rt_bad, rt_notes = _runtime_checks(args.repo, head)
        except Exception as e:  # noqa: BLE001 —— 守卫自身异常也要报出来，不能静默
            rt_bad, rt_notes = [f"C4/C5 检查本身失败：{type(e).__name__}: {e}"], []
        for n in rt_notes:
            print(f"[sync]   {n}")
        problems.extend(rt_bad)

    if not problems:
        print("[sync] OK：仓库 C1–C3 一致，运行态 C4/C5 与磁盘同步")
        return 0

    content = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(problems))
    content += (
        f"\n\n仓库：{args.repo}\nHEAD：{head}\n远端：{remote}\n"
        f"处置：确认后在生产执行 git fetch origin {args.branch} && "
        f"git reset --hard origin/{args.branch}（先确认无未提交的有效改动）。"
        "\nC4/C5 类漂移的处置不是 reset：检查 bind 挂载源是否为文件，"
        "再跑 scripts/ops/publish.py（会双重启 scheduler + api 并写部署标记）；"
        "紧急时用 --allow-runtime-drift 临时跳过，但须 24h 内补台账。"
    )
    print("[sync] DRIFT:\n" + content)
    if not args.no_alert:
        _alert("[QHYC] GitHub 与生产代码不一致", content)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
