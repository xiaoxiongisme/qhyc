# -*- coding: utf-8 -*-
"""迁移执行器：按序应用 migrations/*.sql 并记录到 schema_migrations 表。

设计
----
* 幂等：已应用过的迁移跳过（按文件名 + 内容 sha1 记录，内容变更会告警）
* 安全：默认 --dry-run 只打印将要执行的迁移，不落库
* 可追溯：每张表记录 applied_at / 内容哈希，便于云端与本地对齐

用法
----
  python scripts/db_apply_migrations.py --dry-run           # 只看计划
  python scripts/db_apply_migrations.py --apply             # 执行
  python scripts/db_apply_migrations.py --port 15432 --apply # 云端（经 SSH 隧道）
"""
import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pgconn import add_conn_args, conn_from_args  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parents[1] / "migrations"

_BOOTSTRAP = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   text PRIMARY KEY,
    sha1       text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);
"""


def sha1_of(p: Path) -> str:
    return hashlib.sha1(p.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(MIGRATIONS_DIR))
    ap.add_argument("--apply", action="store_true", help="实际执行（默认只 dry-run）")
    ap.add_argument("--force", action="store_true", help="内容已变更也强制重跑")
    ap.add_argument("--only", default=None, help="只跑指定文件（逗号分隔文件名）")
    add_conn_args(ap)
    a = ap.parse_args()

    d = Path(a.dir)
    files = sorted(d.glob("*.sql"))
    if a.only:
        want = set(a.only.split(","))
        files = [f for f in files if f.name in want]
    if not files:
        print(f"没有找到迁移文件（{d}）")
        return

    conn = conn_from_args(a)
    c = psycopg2.connect(**conn)
    c.autocommit = True
    cur = c.cursor()
    cur.execute(_BOOTSTRAP)
    cur.execute("SELECT filename, sha1 FROM schema_migrations")
    done = dict(cur.fetchall())

    mode = "APPLY" if a.apply else "DRY-RUN"
    print(f"=== 迁移目录 {d} | 模式 {mode} | 已应用 {len(done)} ===")
    for f in files:
        h = sha1_of(f)
        if f.name in done and done[f.name] == h and not a.force:
            print(f"  skip  {f.name}（已应用，sha 一致）")
            continue
        if f.name in done and done[f.name] != h:
            print(f"  !!    {f.name} 内容已变更（记录 {done[f.name][:8]} → 现在 {h[:8]}）")
            if not a.force:
                print("        需 --force 才会重跑（请确认迁移是幂等的）")
                continue
        print(f"  {'RUN ' if a.apply else 'PLAN'}  {f.name} ({f.stat().st_size}B)")
        if not a.apply:
            continue
        t0 = time.time()
        try:
            cur.execute(f.read_text(encoding="utf-8"))
            cur.execute(
                "INSERT INTO schema_migrations(filename, sha1) VALUES (%s,%s) "
                "ON CONFLICT (filename) DO UPDATE SET sha1=EXCLUDED.sha1, applied_at=now()",
                (f.name, h))
            print(f"        ✅ 完成 {round(time.time()-t0, 1)}s")
        except Exception as e:  # noqa: BLE001
            print(f"        ❌ 失败：{e}")
            print("        后续迁移已中止，请修复后重跑")
            break
    c.close()


if __name__ == "__main__":
    main()
