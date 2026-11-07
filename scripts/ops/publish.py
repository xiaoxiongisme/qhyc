#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""唯一发布通道：本地 → GitHub(cb/dev) → 生产 pull → 按需重启 → 三方同 sha 校验。

为什么要把它做成**唯一**通道
----------------------------
2026-11-07 出过一次典型事故：改了 `app/ingest/synthesizer.py` 后为了「快点生效」
直接 ``scp`` 到生产，随后在生产 ``git pull`` 时因为**那个文件本地已被改过**而
fast-forward 失败，只能 `git checkout --` 丢弃再拉。表面上补救了，实际代价是：

* 生产短暂跑的是「scp 过去的内容」而不是任何提交 —— 排查时按 sha 对不上；
* pull 失败后如果用了 `git merge` 或 `pull --rebase`，会留下没人 review 过的合并；
* scp 过去的内容**不在 GitHub 上**，回滚 / 追溯全部失效。

所以规则不是「不要 scp」，而是**让正确路径成为最省事的那条**。本脚本就是那条路径：

    python scripts/ops/publish.py -m "fix(xxx): ..."        # 提交+推送+生产拉取+校验
    python scripts/ops/publish.py --check                    # 只看状态，不动任何东西

它做的事（顺序固定，任一步失败即中止）
--------------------------------------
  1. 本地：不允许脏工作树（要么先提交，要么显式 --allow-dirty）
  2. 本地：确认 HEAD 的祖先包含 origin/<branch>（保证是 fast-forward，不制造合并）
  3. push origin HEAD:<branch>
  4. 生产：``git status --porcelain -uno`` **必须为空** —— 这是本次事故的直接拦网。
     生产上有已跟踪改动 = 有人绕过了本通道（scp/手工编辑），此时**拒绝 pull**，
     因为 pull 只会有两种结局：失败，或者把改动悄悄带进下一次提交。
  5. 生产：``git pull --ff-only``
  6. 比对两端 ``rev-parse HEAD`` 完全一致
  7. 若本次改动命中 ``app/`` → 重启 ``qhyc-scheduler``；命中 ``migrations/`` → 提示
     （迁移**不自动执行**：DDL 必须人看一眼再 apply，这是刻意的）

退出码：0=已一致；1=检查未通过/中止；2=执行失败。
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BRANCH = "cb/dev"
PROD_HOST = "qhyc"
PROD_REPO = "/Docker/qhyc"
SCHED_SVC = "qhyc-scheduler"


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True,
         timeout: int = 120) -> str:
    # 必须带超时：本仓库 `git fetch origin` 实测会卡住（SSH 侧 early EOF / 挂起），
    # 无超时会让整条发布流程静默吊死，比失败更糟。
    p = subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                       capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} 失败：\n{p.stdout}\n{p.stderr}")
    return (p.stdout or "") + (p.stderr or "")


def _ssh(script: str, check: bool = True) -> str:
    return _run(["ssh", PROD_HOST, script], check=check)


def _banner(t: str) -> None:
    print(f"\n=== {t} ===")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-m", "--message", help="提交信息（给出则执行 git add -A + commit）")
    ap.add_argument("--check", action="store_true", help="只检查，不做任何改动")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="允许本地有未提交改动（只推送已有提交；不推荐）")
    ap.add_argument("--no-restart", action="store_true", help="即使改了 app/ 也不重启")
    ap.add_argument("--branch", default=BRANCH)
    a = ap.parse_args()

    # ---- 1) 本地状态 ----
    _banner("1/6 本地状态")
    dirty = _run(["git", "-C", str(REPO), "status", "--porcelain", "-uno"]).strip()
    if a.message:
        if a.check:
            print("--check 与 -m 不能同时用")
            return 1
        _run(["git", "-C", str(REPO), "add", "-A"])
        _run(["git", "-C", str(REPO), "commit", "-m", a.message], check=False)
    dirty = _run(["git", "-C", str(REPO), "status", "--porcelain", "-uno"]).strip()
    if dirty and not a.allow_dirty:
        print("⚠ 本地有未提交的已跟踪改动，先提交（或用 -m 自动提交）：")
        print(dirty)
        return 1
    head = _run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"]).strip()
    print(f"本地 HEAD = {head}")

    # ---- 2) 远端状态（**不 fetch**）----
    #
    # 原来这里 `git fetch origin` 判 fast-forward。实测本仓库 fetch 会长时间挂起
    # （`fetch-pack: unexpected disconnect / early EOF`，或干脆不动），无超时会把
    # 整条发布流程吊死。改用 `ls-remote`：只取远端那个 sha，不发包不值钱，
    # 还顺便避免为了「判关系」把几十万对象拉一遍。
    # 「能不能 FF」交给 push 自己判 —— 非 FF 的 push 会被远端拒绝，判据一样硬。
    _banner("2/6 远端状态")
    remote_sha = ""
    try:
        out = _run(["git", "-C", str(REPO), "ls-remote", "origin",
                    f"refs/heads/{a.branch}"], timeout=60)
        remote_sha = out.split()[0][:7] if out.split() else ""
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        print(f"⚠ ls-remote 失败（{e}）—— 跳过远端比对，直接尝试 push")
    if remote_sha and remote_sha == head:
        print(f"远端已是 {head}，无新提交")
    else:
        print(f"远端 {remote_sha or '?'} → 本地 {head}")

    if a.check:
        print("\n--check：仅检查，未做任何改动")
        return 0

    # ---- 3) 推送 ----
    _banner("3/6 推送 GitHub")
    out = _run(["git", "-C", str(REPO), "push", "origin", f"HEAD:{a.branch}"],
               check=False, timeout=300)
    print(out.strip() or "(已是最新)")
    if "rejected" in out or "non-fast-forward" in out:
        print("✗ 远端拒绝（非 fast-forward）—— 先 pull/rebase 你的分支再发布")
        return 1

    # ---- 4) 生产工作树必须干净（本次事故的拦网）----
    _banner("4/6 生产工作树")
    _ssh(f"cd {PROD_REPO} && git fetch --quiet")
    prod_dirty = _ssh(f"cd {PROD_REPO} && git status --porcelain -uno").strip()
    if prod_dirty:
        # 文件名用 `git diff --name-only` 取，**不要**去切 porcelain 的前缀：
        # porcelain 的状态位宽度（" M "/"M  "/"MM"）不是固定的，切 3 个字符会把
        # 首字母吃掉（实测把 app/ingest/... 变成 pp/ingest/...）。
        dirty_files = [x for x in _ssh(
            f"cd {PROD_REPO} && git diff --name-only").splitlines() if x.strip()]
        if not dirty_files:
            print("✗ 生产工作树脏，但 diff --name-only 为空（可能有未完成的操作），人工检查")
            print(prod_dirty)
            return 1
        # 只有一种情况可以自动收拾：脏文件的内容**与要拉取的提交相同**。
        # 两次判定：先逐字节；再忽略行尾 CR（本地 core.autocrlf 会把 LF 检出成 CRLF，
        # 一旦用 scp 把「工作副本文本」搬过去，生产看到的就是纯行尾差异）。
        # 忽略 CR 后仍不同 = 内容真的不一样，必须人看。
        flist = " ".join(dirty_files)
        same = _ssh(
            f"cd {PROD_REPO} && git diff --quiet origin/{a.branch} -- {flist} "
            f"&& echo SAME || (git diff --quiet --ignore-cr-at-eol origin/{a.branch} "
            f"-- {flist} && echo SAME_CRLF || echo DIFF)").strip()
        if same.endswith(("SAME", "SAME_CRLF")):
            why = "（差异仅为 CRLF 行尾，scp 工作副本所致）" if "CRLF" in same else ""
            print(f"⚠ 生产有 {len(dirty_files)} 个文件被绕过通道改动，"
                  f"内容与 origin/{a.branch} 一致{why} → 自动 checkout 归位")
            _ssh(f"cd {PROD_REPO} && git checkout -- {flist}")
        else:
            print("✗ 生产存在已跟踪文件的改动，且内容与 origin/%s **不同** —— 拒绝 pull。"
                  % a.branch)
            print(prod_dirty)
            print("\n这说明有人绕过了本通道（scp / 手工编辑）。处置（按顺序选一条）：")
            print("  · 内容已经合进某个提交 → 先确认："
                  f"git -C {PROD_REPO} diff origin/{a.branch} -- <file>，无差异再 checkout --")
            print("  · 内容还没进仓库 → 把它取回本地提交，再重跑本脚本")
            print("  · **不要**用 git merge / pull --rebase 硬拉（会留下没人 review 的合并）")
            return 1
    print("干净")

    # ---- 5) 拉取 + 比对 ----
    _banner("5/6 生产 pull")
    changed = _ssh(f"cd {PROD_REPO} && "
                   f"git diff --name-only HEAD origin/{a.branch}").strip()
    _ssh(f"cd {PROD_REPO} && git pull --ff-only --quiet")
    prod_head = _ssh(f"cd {PROD_REPO} && git rev-parse --short HEAD").strip()
    print(f"生产 HEAD = {prod_head}")
    if prod_head != head:
        print(f"✗ 两端 sha 不一致（本地 {head} / 生产 {prod_head}）")
        return 1

    # ---- 6) 按改动范围处理部署动作 ----
    _banner("6/6 部署动作")
    files = [f for f in changed.splitlines() if f.strip()]
    touched_app = [f for f in files if f.startswith("app/")]
    touched_mig = [f for f in files if f.startswith("migrations/")]
    if touched_app and not a.no_restart:
        print(f"app/ 有 {len(touched_app)} 个文件变更 → 重启 {SCHED_SVC}")
        _ssh(f"sudo systemctl restart {SCHED_SVC} && sleep 6 && "
             f"systemctl is-active {SCHED_SVC}")
    elif touched_app:
        print("app/ 有变更，但 --no-restart：**生产仍在跑旧代码**，记得手动重启")
    else:
        print("未触及 app/，无需重启调度器")
    if touched_mig:
        print(f"⚠ migrations/ 有 {len(touched_mig)} 个新迁移，**未自动执行**：")
        for f in touched_mig:
            print(f"    docker exec -w /app -e PYTHONPATH=/app qhyc-api "
                  f"python scripts/db_apply_migrations.py --only {Path(f).name} --apply")

    print(f"\n✓ 发布完成：本地 = 生产 = GitHub = {head}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
