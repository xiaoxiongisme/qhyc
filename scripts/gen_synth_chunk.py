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

lines = ["SET work_mem='4GB';",
         "TRUNCATE bar_5m;", "TRUNCATE bar_15m;",
         "TRUNCATE bar_30m;", "TRUNCATE bar_60m;"]
for s in symbols:
    lines.append(tmpl.format(t="bar_5m", iv="5 minutes", s=s))
    lines.append(tmpl.format(t="bar_15m", iv="15 minutes", s=s))
    lines.append(tmpl.format(t="bar_30m", iv="30 minutes", s=s))
    lines.append(tmpl.format(t="bar_60m", iv="60 minutes", s=s))

out = os.path.join(os.path.dirname(__file__), "synth_chunk.sql")
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(lines))
print("wrote", out, "with", len(symbols) * 2, "INSERT statements")
