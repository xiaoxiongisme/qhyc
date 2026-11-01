# -*- coding: utf-8 -*-
"""探针：确认 _read_hourly 返回 dt 列的 dtype，解释为何小时线不踩 datetime64 坑。"""
import sys

from datetime import date
from app.core.db import session_scope
from app.backtest.fusion_backtest import _read_hourly


def main() -> int:
    with session_scope() as s:
        df = _read_hourly(s, "AU888", "akshare", date(2024, 1, 1), date(2024, 3, 31))
        print("HOURLY dt dtype :", df["dt"].dtype)
        arr = df["dt"].to_numpy()
        print("to_numpy dtype  :", arr.dtype)
        print("elem type       :", type(arr[0]))
        print("hasattr .date() :", hasattr(arr[0], "date"))
        if hasattr(arr[0], "date"):
            print("date() type     :", type(arr[0].date()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
