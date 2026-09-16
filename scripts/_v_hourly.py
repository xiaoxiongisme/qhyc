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
