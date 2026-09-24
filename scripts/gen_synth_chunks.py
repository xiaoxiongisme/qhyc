import os, psycopg2

def load_creds():
    c = dict(host="127.0.0.1", port=5432, user="futures", password="futures", dbname="futures")
    p = os.path.join(os.path.dirname(__file__), "..", ".env")
    try:
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k == "POSTGRES_PASSWORD": c["password"] = v
            elif k == "POSTGRES_USER": c["user"] = v
            elif k == "POSTGRES_DB": c["dbname"] = v
            elif k == "POSTGRES_PORT": c["port"] = int(v)
    except FileNotFoundError:
        pass
    return c

DB = load_creds()
con = psycopg2.connect(**DB)
cur = con.cursor()
cur.execute("SELECT DISTINCT symbol FROM minute_bar ORDER BY symbol")
symbols = [r[0] for r in cur.fetchall()]
con.close()
print("symbols:", len(symbols))

tmpl = """INSERT INTO {t} (symbol, bucket, open, high, low, close, volume, amount, open_interest)
SELECT symbol, time_bucket(INTERVAL '{iv}', ts, 'Asia/Shanghai') AS bucket,
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, sum(amount), last(open_interest, ts)
FROM minute_bar WHERE symbol = '{s}' GROUP BY symbol, bucket;"""

N = 6
chunks = [symbols[i::N] for i in range(N)]
d = os.path.dirname(__file__)
for i, syms in enumerate(chunks):
    lines = ["SET work_mem='512MB';"]
    for s in syms:
        for t, iv in [("bar_5m", "5 minutes"), ("bar_15m", "15 minutes"),
                      ("bar_30m", "30 minutes"), ("bar_60m", "60 minutes")]:
            lines.append(tmpl.format(t=t, iv=iv, s=s))
    with open(os.path.join(d, f"synth_chunk_{i}.sql"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"chunk {i}: {len(syms)} symbols")
