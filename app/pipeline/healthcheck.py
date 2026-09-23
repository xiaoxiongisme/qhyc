"""M8 pipeline 健康检查入口（compose healthcheck 用）：`python -m app.pipeline.healthcheck`。

检查项（任一失败 → 退出码 1）：
1. 源码只读挂载 `/app/pipeline_src` 可见
2. 启动快照目录存在且非空
3. PG 可连通
4. 产出根目录 `QH_BRIEF_BASE` 可写
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _checks() -> list[tuple[str, bool, str]]:
    from app.pipeline import config

    cfg = config.load()
    out: list[tuple[str, bool, str]] = []

    src = Path(cfg.src)
    out.append(("src_mount", src.exists() and any(src.iterdir()) if src.exists() else False,
                f"src={cfg.src}"))

    dest = Path(cfg.dest)
    ok_dest = dest.exists() and any(dest.iterdir()) if dest.exists() else False
    out.append(("snapshot", ok_dest, f"dest={cfg.dest}"))

    try:
        from sqlalchemy import text

        from app.core.db import session_scope

        with session_scope() as s:
            s.execute(text("SELECT 1"))
        out.append(("postgres", True, "SELECT 1 ok"))
    except Exception as e:  # noqa: BLE001
        out.append(("postgres", False, str(e)[:200]))

    # 产出目录：pipeline 容器需可写（rw），api 容器只是只读挂载 → 只要求可见/可读
    role = (os.environ.get("ROLE") or "").strip().lower()
    need_write = (role == "pipeline")
    base = Path(cfg.brief_base)
    try:
        ok_base = base.exists() and (os.access(base, os.W_OK) if need_write
                                     else os.access(base, os.R_OK))
    except Exception:  # noqa: BLE001
        ok_base = False
    out.append((f"brief_base_{'writable' if need_write else 'readable'}", ok_base,
                f"base={cfg.brief_base} role={role or '-'}"))

    return out


def main() -> int:
    results = _checks()
    for name, ok, note in results:
        print(f"{'OK ' if ok else 'FAIL'} {name}: {note}")
    bad = [n for n, ok, _ in results if not ok]
    if bad:
        print(f"UNHEALTHY: {', '.join(bad)}")
        return 1
    print("HEALTHY")
    return 0


if __name__ == "__main__":
    sys.exit(main())
