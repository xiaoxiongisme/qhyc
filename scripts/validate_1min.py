"""Survey + validate the 1-min futures CSVs under D:/学习资料.

Two datasets: 期货主力连续1min (main continuous) and 期货商品指数1min (index).
Each file: {CODE}.{EXCH}_{y1}_{m1}_{d1}_{y2}_{m2}_{d2}_1min.csv  (~one quarter).
Columns: <ts>,open,close,high,low,volume,money,avg,high_limit,low_limit,pre_close,paused,factor,open_interest,contract,settle_price
"""
import os, re, json, sys
from collections import Counter, defaultdict

BASE = r"D:/学习资料"
MAIN = os.path.join(BASE, "期货主力连续1min最新")
INDEX = os.path.join(BASE, "期货商品指数1min最新")

FNAME = re.compile(
    r"^(?P<code>[A-Z0-9]+)\.(?P<exch>[A-Z]+)_"
    r"(?P<y1>\d+)_(?P<m1>\d+)_(?P<d1>\d+)_"
    r"(?P<y2>\d+)_(?P<m2>\d+)_(?P<d2>\d+)_1min\.csv$"
)

def walk(root):
    out = []
    for dirpath, _, files in os.walk(root):
        for f in files:
            if not f.endswith(".csv"):
                continue
            m = FNAME.match(f)
            if not m:
                out.append(("BADNAME", f))
                continue
            g = m.groupdict()
            out.append((os.path.join(dirpath, f), g))
    return out

def survey(root, label):
    files = walk(root)
    bad = [x for x in files if x[0] == "BADNAME"]
    good = [x for x in files if x[0] != "BADNAME"]
    mcombos = Counter((int(x[1]["m1"]), int(x[1]["m2"])) for x in good)
    years = Counter(int(x[1]["y1"]) for x in good)
    codes = set()
    for x in good:
        codes.add(x[1]["code"])
    print(f"\n=== {label} ===")
    print(f"total csv: {len(files)}, bad-name: {len(bad)}")
    print(f"(start_month,end_month) distribution: {dict(sorted(mcombos.items()))}")
    print(f"year range: {min(years)}..{max(years)}, files/year: {dict(sorted(years.items()))}")
    print(f"distinct contract codes: {len(codes)} -> {sorted(codes)[:10]} ...")
    return good, codes

def validate_sample(good, n=24, seed=0):
    import pandas as pd, numpy as np
    # pick a spread of files across years
    good_sorted = sorted(good, key=lambda x: (x[1]["y1"], x[1]["code"]))
    step = max(1, len(good_sorted) // n)
    sample = good_sorted[::step][:n]
    print(f"\n=== validating {len(sample)} sample files ===")
    issues = []
    for path, g in sample:
        try:
            df = pd.read_csv(path, index_col=0, parse_dates=True)
        except Exception as e:
            issues.append((path, f"read error {e}"))
            continue
        df.index = pd.to_datetime(df.index)
        ts = df.index
        # dedupe check
        dup = int(ts.duplicated().sum())
        # sort & continuity
        s = ts.sort_values()
        d = s.diff().dropna()
        gap_counts = d.value_counts()
        big_gaps = d[d > pd.Timedelta(minutes=1)]
        # OHLC invariants
        o, h, l, c = df["open"], df["high"], df["low"], df["close"]
        bad_hl = int(((h < o) | (h < c) | (l > o) | (l > c)).sum())
        nonpos = int(((o <= 0) | (c <= 0) | (h <= 0) | (l <= 0)).sum())
        nan = int(df[["open","high","low","close"]].isna().sum().sum())
        neg_vol = int((df["volume"] < 0).sum())
        print(f"{g['code']} {g['y1']}-{g['m1']:0>2}: rows={len(df)} dup_ts={dup} "
              f"ts[{ts.min()} .. {ts.max()}] badHL={bad_hl} nonpos={nonpos} nan={nan} negVol={neg_vol} "
              f"maxgap={big_gaps.max() if len(big_gaps) else 'none'} ngaps={len(big_gaps)}")
        if dup or bad_hl or nonpos or nan or neg_vol:
            issues.append((path, f"dup={dup} badHL={bad_hl} nonpos={nonpos} nan={nan} negVol={neg_vol}"))
    print(f"\nSAMPLE ISSUES: {len(issues)}")
    for p, m in issues[:20]:
        print("  ", os.path.basename(p), m)
    return issues

if __name__ == "__main__":
    gm, cm = survey(MAIN, "MAIN CONTINUOUS")
    gi, ci = survey(INDEX, "COMMODITY INDEX")
    validate_sample(gm, n=30)
    validate_sample(gi, n=10)
