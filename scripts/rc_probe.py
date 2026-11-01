"""探查：定位生产容器内真实合约 K 线 / 主力合约映射 / 换月段表的真实 schema 与覆盖。"""
from app.core.config import get_settings
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

s = get_settings()
eng = create_engine(s.db_url)
sess = sessionmaker(bind=eng)()

print("=== 相关表（kline/fut/contract/main_contract_map/roll）===")
rows = sess.execute(text(
    "SELECT table_schema, table_name FROM information_schema.tables "
    "ORDER BY 1,2")).all()
for r in rows:
    n = r[1].lower()
    if ('kline' in n or 'fut' in n or 'contract' in n
            or n == 'main_contract_map' or 'roll' in n):
        print(r)

print("=== main_contract_map 精确位置 ===")
for r in sess.execute(text(
    "SELECT table_schema, table_name FROM information_schema.tables "
    "WHERE table_name='main_contract_map'")).all():
    print(r)

# contract_daily（真实合约日线）schema + SN 样例
print("=== contract_daily schema + SN 样例 ===")
for sch in ('public', 'l0_raw'):
    try:
        cnt = sess.execute(text(f"SELECT count(*) FROM {sch}.contract_daily")).scalar()
        print(f"{sch}.contract_daily count={cnt}")
        samp = sess.execute(text(
            f"SELECT DISTINCT symbol FROM {sch}.contract_daily "
            "WHERE symbol ILIKE '%sn%' LIMIT 8")).all()
        print(f"  SN 样例:", [x[0] for x in samp])
    except Exception as e:
        print(f"{sch}.contract_daily err {e}")

print("=== main_contract_map SN underlying 格式 ===")
for r in sess.execute(text(
    "SELECT DISTINCT underlying FROM public.main_contract_map "
    "WHERE product='SN' LIMIT 8")).all():
    print(r)

print("=== contract_daily 列(SN2501 若存在) ===")
for sch in ('public', 'l0_raw'):
    try:
        cols = sess.execute(text(
            f"SELECT column_name FROM information_schema.columns "
            f"WHERE table_schema='{sch}' AND table_name='contract_daily' "
            "ORDER BY ordinal_position")).all()
        print(f"{sch}.contract_daily cols:", [c[0] for c in cols])
        break
    except Exception:
        pass
