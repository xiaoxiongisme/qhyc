# --- 2026-11-06 修：补仓库根到 sys.path，使 `python scripts/xxx.py` 可直接 import app.*
#     此前容器 Dockerfile 未设 PYTHONPATH、脚本自身也无引导 →
#     ModuleNotFoundError: No module named 'app'（tests/ 由根 conftest.py 兜底，不受影响）。
import sys
from pathlib import Path
sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents if (p / 'app').is_dir())))

from app.core.db import session_scope
from sqlalchemy import text

with session_scope() as s:
    runs = s.execute(text(
        "SELECT run_id, count(DISTINCT symbol) AS n, max(created_at) AS last "
        "FROM backtest_result GROUP BY run_id ORDER BY last DESC LIMIT 3"
    )).all()
    print("RECENT_RUNS=", [(r[0], r[1], str(r[2])) for r in runs])

    if runs:
        rid = runs[0][0]
        tot = s.execute(text("SELECT count(*) FROM backtest_result WHERE run_id=:x"), {"x": rid}).scalar()
        print(f"NEWEST_RUN={rid} total_rows={tot}")

    mw = s.execute(text(
        "SELECT model, weight, source, updated_at FROM model_weights ORDER BY model"
    )).all()
    print("MODEL_WEIGHTS=")
    for row in mw:
        print("  ", row[0], row[1], row[2], str(row[3]))
