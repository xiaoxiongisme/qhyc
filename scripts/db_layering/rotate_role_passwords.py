# -*- coding: utf-8 -*-
"""G4 · 数据层分层角色口令轮换（幂等 + 参数化 + fail-loud）。

背景
----
分层迁移用占位口令 ``CHANGE_ME_*`` 建了 6 个角色。那是**可被任何人猜到**的口令，
即使角色当前未被应用使用，也属于必须清掉的欠账（真实资金前的硬要求）。

设计纪律
--------
* **密钥零入库**：口令只从环境变量读，代码与 git 中不出现任何明文。
  * ``APP_DB_PASSWORD``：统一口令（6 个角色共用）。
  * ``APP_DB_ROLE_PASSWORDS``：可选 JSON，按角色分别指定（推荐生产用，遵循最小权限）。
* **fail-loud**：环境变量缺失直接抛错，不静默跳过、不写空口令。
* **幂等**：重复执行即重新设置为当前 env 口令。
* **安全转义**：用 ``format('%I'/'%L')`` 构造 DDL，口令含特殊字符（引号/反斜杠/点）也安全。

用法
----
    export APP_DB_PASSWORD='真实口令'
    python scripts/db_layering/rotate_role_passwords.py            # 预演（只打印计划）
    python scripts/db_layering/rotate_role_passwords.py --apply    # 实际执行
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from sqlalchemy import text

from app.core.db import session_scope

#: 分层迁移创建的角色 → 职责（仅用于日志可读性）
ROLES: dict[str, str] = {
    "etl_ingest": "l0_raw 原始层写入（采集/回补）",
    "l1_builder": "l1_mkt 加工层写入（价差/主力推断）",
    "l2_builder": "l2_adj 复权层写入",
    "ref_maintainer": "l3_ref 维表维护（dim_* / contract_code_map）",
    "app_write": "app_state 业务写入",
    "app_read": "只读 access 视图（读路径最小权限）",
}


def resolve_passwords() -> dict[str, str]:
    """从环境变量解析每个角色的目标口令；缺失即抛错（fail-loud）。"""
    per_role_raw = os.getenv("APP_DB_ROLE_PASSWORDS", "").strip()
    if per_role_raw:
        try:
            per_role = json.loads(per_role_raw)
        except json.JSONDecodeError as e:
            raise SystemExit(f"[fail-loud] APP_DB_ROLE_PASSWORDS 不是合法 JSON：{e}")
        missing = [r for r in ROLES if r not in per_role]
        if missing:
            raise SystemExit(
                f"[fail-loud] APP_DB_ROLE_PASSWORDS 缺少角色：{missing}；"
                f"须为全部 {sorted(ROLES)} 指定"
            )
        return {r: str(per_role[r]) for r in ROLES}

    pw = os.getenv("APP_DB_PASSWORD", "")
    if not pw:
        raise SystemExit(
            "[fail-loud] 未提供口令：请设置环境变量 APP_DB_PASSWORD（6 角色统一），"
            "或 APP_DB_ROLE_PASSWORDS（JSON，按角色分别指定，推荐生产）。"
            "代码与仓库中不保存任何明文口令。"
        )
    return {r: pw for r in ROLES}


def existing_roles(session) -> set[str]:
    rows = session.execute(
        text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:names)"),
        {"names": list(ROLES)},
    ).scalars().all()
    return set(rows)


def rotate(apply: bool = False) -> int:
    pws = resolve_passwords()
    with session_scope() as s:
        present = existing_roles(s)
        absent = sorted(set(ROLES) - present)
        if absent:
            raise SystemExit(
                f"[fail-loud] 以下分层角色不存在，请先应用 db_layering_migrate.sql：{absent}"
            )
        for role, pw in pws.items():
            if not pw:
                raise SystemExit(f"[fail-loud] 角色 {role} 的口令为空字符串，拒绝设置")
            # 用**数据库自身的** quote_ident/quote_literal 做转义：
            # 口令含引号/反斜杠/点等特殊字符也安全，且不把明文拼进 SQL 文本。
            ident = s.execute(text("SELECT quote_ident(:r)"), {"r": role}).scalar()
            lit = s.execute(text("SELECT quote_literal(:p)"), {"p": pw}).scalar()
            if apply:
                s.execute(text(f"ALTER ROLE {ident} PASSWORD {lit}"))
            else:
                print(f"  [dry-run] ALTER ROLE {ident} PASSWORD '***'  ({ROLES[role]})")
        if apply:
            print(f"[rotate] 已轮换 {len(pws)} 个分层角色口令：{sorted(pws)}")
        else:
            print(f"[rotate] dry-run，未改动。计划轮换 {len(pws)} 个角色：{sorted(pws)}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际执行（缺省仅预演）")
    a = ap.parse_args()
    try:
        sys.exit(rotate(apply=a.apply))
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        print(f"[rotate] 失败：{type(e).__name__}: {e}")
        sys.exit(1)
