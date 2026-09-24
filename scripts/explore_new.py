import os, re

base = r"D:/学习资料"
folders = [
    "期货1min2025年3月-11月30",
    "期货1min_2025年12月",
    "期货主力连续1min最新",
    "期货商品指数1min最新",
]

pat_new = re.compile(r"^(?P<code>[A-Za-z0-9]+)\.(?P<exch>[A-Z]+)_"
                     r"(?P<y1>\d{4})(?P<m1>\d{2})(?P<d1>\d{2})_"
                     r"(?P<y2>\d{4})(?P<m2>\d{2})(?P<d2>\d{2})_1min\.csv$")
pat_old = re.compile(r"^(?P<code>[A-Za-z0-9]+)\.(?P<exch>[A-Z]+)_(?P<year>\d{4})_1min\.csv$")

for d in folders:
    p = os.path.join(base, d)
    files = []
    for root, _, fs in os.walk(p):
        for f in fs:
            if f.endswith(".csv"):
                files.append(f)
    files.sort()
    main = sum(1 for f in files if "9999" in f)
    idx = sum(1 for f in files if "8888" in f)
    # date range from NEW-format names
    dates = []
    for f in files:
        m = pat_new.match(f)
        if m:
            dates.append(int(m.group("y1") + m.group("m1") + m.group("d1")))
    dmin = min(dates) if dates else None
    dmax = max(dates) if dates else None
    print(f"=== {d} ===")
    print(f"  total={len(files)}  main(9999)={main}  index(8888)={idx}")
    print(f"  NEW-fmt date range: {dmin} .. {dmax}  (count={len(dates)})")
    print(f"  first2: {files[:2]}")
    print(f"  last2 : {files[-2:]}")
