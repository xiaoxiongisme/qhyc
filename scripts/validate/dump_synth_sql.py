# -*- coding: utf-8 -*-
import sys
sys.path.insert(0, ".")
from app.ingest.synthesizer import _session_insert_sql


def main():
    sql = _session_insert_sql(60, "WHERE ts >= :since", "ON CONFLICT DO NOTHING", t="bar_60m")
    print(sql)


if __name__ == "__main__":
    main()
