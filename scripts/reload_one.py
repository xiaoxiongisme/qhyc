import os, psycopg2
import load_1min_incremental as L

DB = L._load_db_creds()
conn = psycopg2.connect(**DB)
cur = conn.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS minute_bar_stage (LIKE minute_bar)")
conn.commit()

target = None
for root in L.NEW_ROOTS:
    for r, _, fs in os.walk(root):
        for f in fs:
            if f.startswith("NI8888.XSGE") and "2025_12" in f and f.endswith(".csv"):
                target = os.path.join(r, f)
                break
        if target:
            break
    if target:
        break

print("target:", target)
n, err = L.load_file(cur, target)
print("loaded rows =", n, "err =", err)
conn.commit()
cur.execute("SELECT count(*) FROM minute_bar WHERE symbol='NI8888' AND ts >= '2025-12-01'")
print("NI8888 Dec rows now in minute_bar:", cur.fetchone()[0])
conn.close()
