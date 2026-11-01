"""探查：main_contract_map / contract_daily / bar_60m 的真实 schema 与覆盖。

只查不写，用于把 verify_real_contract_oos.py 的 build_continuous 从
（不存在的）l0_raw.fut_kline 重写为 contract_daily + main_contract_map。
"""
from sqlalchemy import create_engine, text
from app.core.db import session_scope


def dump_cols(session, schema, table):
    rows = session.execute(text(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_schema=:s AND table_name=:t ORDER BY ordinal_position"
    ), {"s": schema, "t": table}).all()
    return [(r[0], r[1]) for r in rows]


def main():
    with session_scope() as session:
        print("=== main_contract_map columns ===")
        print(dump_cols(session, "public", "main_contract_map"))
        print("=== main_contract_map SN sample ===")
        for r in session.execute(text(
            "SELECT * FROM public.main_contract_map WHERE product='SN' "
            "ORDER BY trade_date LIMIT 6")).all():
            print(r)

        print("\n=== contract_daily columns ===")
        print(dump_cols(session, "public", "contract_daily"))
        print("=== contract_daily distinct product sample ===")
        for r in session.execute(text(
            "SELECT product, count(*) FROM public.contract_daily "
            "GROUP BY product ORDER BY product LIMIT 10")).all():
            print(r)
        print("=== contract_daily SN2201 coverage ===")
        r = session.execute(text(
            "SELECT min(trade_date), max(trade_date), count(*) FROM public.contract_daily "
            "WHERE symbol='SN2201'")).one()
        print(r)
        print("=== contract_daily product='SN' coverage ===")
        r = session.execute(text(
            "SELECT min(trade_date), max(trade_date), count(*) FROM public.contract_daily "
            "WHERE product='SN'")).one()
        print(r)

        print("\n=== l1_mkt.bar_60m SN888 coverage ===")
        r = session.execute(text(
            "SELECT min(bucket), max(bucket), count(*) FROM l1_mkt.bar_60m "
            "WHERE symbol='SN888'")).one()
        print(r)
        print("=== l3_ref.main_contract_map columns (if exists) ===")
        try:
            print(dump_cols(session, "l3_ref", "main_contract_map"))
        except Exception as e:
            print("l3_ref.main_contract_map:", e)

        print("\n=== does main_contract_map have product col? check SN/NI row counts ===")
        for p in ("SN", "NI"):
            n = session.execute(text(
                "SELECT count(*) FROM public.main_contract_map WHERE product=:p"
            ), {"p": p}).scalar()
            print(f"  {p}: {n} rows")


if __name__ == "__main__":
    main()
