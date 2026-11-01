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

# 若 fut_kline 在某 schema，抽查 SN 类 symbol
for sch in ('l0_raw', 'public', 'raw', 'l1_mkt', 'l2_adj', 'l3_ref', 'app_state', 'l2_adj'):
    try:
        sample = sess.execute(text(
            f"SELECT DISTINCT symbol FROM {sch}.fut_kline "
            "WHERE symbol ILIKE '%sn%' LIMIT 5")).all()
        if sample:
            print(f"{sch}.fut_kline SN 样例:", [x[0] for x in sample])
    except Exception as e:
        pass
