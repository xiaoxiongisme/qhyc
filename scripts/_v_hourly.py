# --- 2026-11-06 修：补仓库根到 sys.path，使 `python scripts/xxx.py` 可直接 import app.*
#     此前容器 Dockerfile 未设 PYTHONPATH、脚本自身也无引导 →
#     ModuleNotFoundError: No module named 'app'（tests/ 由根 conftest.py 兜底，不受影响）。
import sys
from pathlib import Path
sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents if (p / 'app').is_dir())))

from app.core.db import session_scope
from sqlalchemy import text

with session_scope() as s:
    r = s.execute(
        text(
            "SELECT count(*), min(trade_datetime), max(trade_datetime) "
            "FROM hourly_bar WHERE symbol=:x"
        ),
        {"x": "FG888"},
    ).fetchone()
    print("FG888_HOURLY=", r)

    total = s.execute(text("SELECT count(*) FROM hourly_bar")).scalar()
    print("HOURLY_TOTAL_ROWS=", total)
