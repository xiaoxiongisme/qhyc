"""Incremental loader: add 2025-05..12 1-min data to existing minute_bar.

Scans only the two NEW data folders placed by the user; every row is upserted
into minute_bar with ON CONFLICT (symbol, ts) DO NOTHING, so any overlap with
already-loaded 2025-01..04 data (Mar/Apr present in the new folder) is skipped
safely instead of aborting the COPY.
"""
import os, re, io, time, argparse
import numpy as np
import pandas as pd
import psycopg2

SH_FMT = "%Y-%m-%d %H:%M:%S%z"

NEW_ROOTS = [
    r"D:/学习资料/期货1min2025年3月-11月30",
    r"D:/学习资料/期货1min_2025年12月",
]

# permissive: capture code (before first '.') and exch (uppercase after it)
PAT = re.compile(r"^([A-Za-z0-9]+)\.([A-Z]+)")

OUT_COLS = ["symbol", "ts", "open", "high", "low", "close", "volume",
            "amount", "open_interest", "high_limit", "low_limit",
            "pre_close", "settle_price", "contract", "src"]


def _load_db_creds():
    creds = dict(host="127.0.0.1", port=5432, user="futures",
                 password="futures", dbname="futures")
    env_path = os.path.join(os.path.dirname(__file__), "..", ".env")
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k == "POSTGRES_PASSWORD":
                    creds["password"] = v
                elif k == "POSTGRES_USER":
                    creds["user"] = v
                elif k == "POSTGRES_DB":
                    creds["dbname"] = v
                elif k == "POSTGRES_PORT":
                    creds["port"] = int(v)
    except FileNotFoundError:
        pass
    return creds


def map_symbol(code, dataset="main"):
    if code.endswith("9999"):
        return code[:-4] + "888"
    if code.endswith("8888"):
        return code  # index kept literal
    return code


def parse_symbol(path):
    m = PAT.match(os.path.basename(path))
    if not m:
        return None
    return map_symbol(m.group(1))


def load_file(cur, path):
    symbol = parse_symbol(path)
    if symbol is None:
        return 0, "bad name"
    try:
        df = pd.read_csv(path, index_col=0)
        df.index = pd.to_datetime(df.index).tz_localize(None).tz_localize("Asia/Shanghai")
        contract = os.path.basename(path)[:-4]  # strip .csv
        out = pd.DataFrame({
            "symbol": symbol,
            "ts": df.index.strftime(SH_FMT),
            "open": df["open"], "high": df["high"], "low": df["low"], "close": df["close"],
            "volume": df["volume"],
            "amount": df["money"],
            "open_interest": df["open_interest"],
            "high_limit": df["high_limit"], "low_limit": df["low_limit"],
            "pre_close": df["pre_close"], "settle_price": df["settle_price"],
            "contract": contract, "src": "csv_1min",
        })[OUT_COLS]
        for c in ["open", "high", "low", "close", "amount", "open_interest",
                  "high_limit", "low_limit", "pre_close", "settle_price", "volume"]:
            out[c] = pd.to_numeric(out[c], errors="coerce")
        out = out.replace([np.inf, -np.inf], np.nan)
        out["volume"] = out["volume"].fillna(0).astype("int64")
        buf = io.StringIO()
        out.to_csv(buf, index=False, header=False)
        buf.seek(0)
        cur.copy_expert(
            "COPY minute_bar_stage (symbol, ts, open, high, low, close, volume, amount, "
            "open_interest, high_limit, low_limit, pre_close, settle_price, contract, src) "
            "FROM STDIN WITH (FORMAT CSV)", buf)
        cur.execute("INSERT INTO minute_bar SELECT * FROM minute_bar_stage "
                    "ON CONFLICT (symbol, ts) DO NOTHING")
        cur.execute("TRUNCATE TABLE minute_bar_stage")
        return len(out), None
    except Exception as e:
        raise e


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    args = ap.parse_args()

    files = []
    for root in NEW_ROOTS:
        for r, _, fs in os.walk(root):
            for f in fs:
                if f.endswith(".csv"):
                    files.append(os.path.join(r, f))
    if args.limit:
        files = files[:args.limit]
    print(f"files to process: {len(files)}")

    DB = _load_db_creds()
    conn = psycopg2.connect(**DB)
    cur = conn.cursor()
    cur.execute("CREATE TABLE IF NOT EXISTS minute_bar_stage (LIKE minute_bar)")
    conn.commit()

    total = 0
    skipped = 0
    t0 = time.time()
    for i, path in enumerate(files, 1):
        base = os.path.basename(path)
        try:
            n, err = load_file(cur, path)
            if err:
                print(f"SKIP [{i}] {base}: {err}")
                skipped += 1
            else:
                total += n
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"FAIL [{i}] {base}: {e}")
        if i % 25 == 0:
            print(f"[{i}/{len(files)}] rows={total} skipped={skipped} "
                  f"files/s={(i)/(time.time()-t0):.2f}")
    print(f"DONE rows={total} skipped={skipped} in {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
