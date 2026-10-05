# -*- coding: utf-8 -*-
"""D:/学习资料 1 分钟 dump 校验器（2026-01-05~02-28 zip + GFEX 全历史 zip）。

两个已知布局（自动识别）：
  A) 逐日布局：2026/YYYYMM/YYYYMMDD/<code>.csv，列
     exchange,symbol,open,close,high,low,amount,volume,position,bob,eob,type,sequence
     bob/eob 为 ISO+08:00；bob=bar 起始（与 minute_bar.ts START 标签同口径）。
  B) GFEX 布局：<品种_中文名>/00_主力连续/<P>_main_1m.csv（月份连续暂不处理），
     列 datetime,exchange,variety,symbol,datetime_nano,open,high,low,close,volume,
     open_interest,close_interest（天勤 风格，datetime=bar 起始，ns 列冗余）。

只处理 9999(→888 主连) 与 8888(→8888 指数) 两类符号；合约级与 9998(次主力)
跳过并计数——库内 minute_bar 历来只存 888/8888 两族，合成器/复权链也只认这两族。

用法：
  python scripts/validate_1min_dump.py scan  --zip "D:/学习资料/2026.zip"
  python scripts/validate_1min_dump.py scan  --zip "D:/学习资料/GFEX_广州期货交易所.zip"
  python scripts/validate_1min_dump.py cross --zip "D:/学习资料/2026.zip" \
      --products RB,CU,A,M,TA,AP,LC,IF,AU,SC [--freqs 15m,30m,60m,5m]
  python scripts/validate_1min_dump.py cross --zip "D:/学习资料/GFEX_广州期货交易所.zip" --gfex

校验项：
  scan  —— 列完整 / 数值合法 / OHLC 关系 / bob<eob 且 60s / 单调 / 交易时段归属 /
           每产品行数与文件数 / 缺日统计。
  cross —— dump 1m 按 START 标签 floor 聚合为 5/15/30/60m，与库内 bar_*m 同窗对拍
           （matched/only_dump/only_db/最大价差/成交量差）。888 系列对拍证明 dump
           与 天勤 补采同源可信；8888 系列 2026 库内为空（即待补缺口）。
"""
from __future__ import annotations

import argparse
import collections
import csv
import io
import os
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

CONT_RE = re.compile(r"^([A-Za-z]+?)(9999|8888|9998)$")
DAY_RE = re.compile(r"2026/(\d{6})/(\d{8})/([A-Za-z0-9]+)\.csv$")
GFEX_MAIN_RE = re.compile(r"^[A-Za-z]+_[^/]+/00_主力连续/([A-Za-z]+)_main_1m\.csv$")

EXPECT_A = {"exchange", "symbol", "open", "close", "high", "low", "amount",
            "volume", "position", "bob", "eob", "type", "sequence"}
EXPECT_B = {"datetime", "exchange", "symbol", "open", "high", "low", "close",
            "volume", "open_interest"}

VIOL_SAMPLES = 5


def map_symbol(code: str) -> tuple[str, str] | None:
    """a9999→(A888,main) A8888→(A8888,index) a9998→(None,skip) 合约→(None,skip)。"""
    m = CONT_RE.match(code)
    if not m:
        return None
    prod, suf = m.group(1).upper(), m.group(2)
    if suf == "9999":
        return prod + "888", "main"
    if suf == "8888":
        return prod + "8888", "index"
    return None  # 9998 次主力：库内无此族，跳过


def _in_session(bob: datetime) -> bool:
    """宽松时段校验：日盘 08:30-15:16；夜盘 20:30-次日 02:59（允许 bob 落在次日凌晨）。"""
    t = bob.hour * 60 + bob.minute
    day = 8 * 60 + 30 <= t <= 15 * 60 + 16
    night = t >= 20 * 60 + 30 or t <= 2 * 60 + 59
    return day or night


def scan_zip(zpath: str) -> dict:
    zf = zipfile.ZipFile(zpath)
    names = [n for n in zf.namelist() if n.endswith(".csv")]
    layout = "A" if DAY_RE.match(names[0]) else ("B" if GFEX_MAIN_RE.match(names[0]) else "?")
    viol: dict[str, list] = collections.defaultdict(list)
    per_prod_rows: collections.Counter = collections.Counter()
    per_prod_files: collections.Counter = collections.Counter()
    total = 0
    bad_files = 0
    days = set()
    last_bob_by_file = None

    def add(kind: str, detail: str):
        if len(viol[kind]) < VIOL_SAMPLES:
            viol[kind].append(detail)

    it = names if layout == "A" else [n for n in names if GFEX_MAIN_RE.match(n)]
    for idx, name in enumerate(it):
        if layout == "A":
            m = DAY_RE.match(name)
            code = m.group(3)
            mapped = map_symbol(code)
            if mapped is None:
                continue
            sym, kind = mapped
            days.add(m.group(2))
        else:
            m = GFEX_MAIN_RE.match(name)
            sym, kind = m.group(1).upper() + "888", "main"
        per_prod_files[sym] += 1
        try:
            with zf.open(name) as f:
                text = io.TextIOWrapper(f, encoding="utf-8-sig", errors="replace")
                rdr = csv.reader(text)
                header = next(rdr, [])
                hset = set(header)
                need = EXPECT_A if layout == "A" else EXPECT_B
                if not need.issubset(hset):
                    add("missing_cols", f"{name}: 缺 {sorted(need - hset)}")
                    bad_files += 1
                    continue
                ix = {c: i for i, c in enumerate(header)}
                prev_bob = None
                nfile = 0
                for row in rdr:
                    if not row or len(row) < len(header):
                        add("short_row", f"{name}#{nfile+1}: {row[:3]}")
                        continue
                    try:
                        if layout == "A":
                            bob = datetime.fromisoformat(row[ix["bob"]])
                            eob = datetime.fromisoformat(row[ix["eob"]])
                            o, h, l, c = (float(row[ix[k]]) for k in ("open", "high", "low", "close"))
                            v = float(row[ix["volume"]])
                        else:
                            bob = datetime.fromisoformat(row[ix["datetime"]])
                            eob = bob + timedelta(minutes=1)
                            o, h, l, c = (float(row[ix[k]]) for k in ("open", "high", "low", "close"))
                            v = float(row[ix["volume"]])
                    except Exception as e:  # noqa: BLE001
                        add("parse_fail", f"{name}#{nfile+1}: {type(e).__name__} {row[:3]}")
                        continue
                    nfile += 1
                    total += 1
                    if not (o > 0 and h > 0 and l > 0 and c > 0):
                        add("nonpositive_price", f"{name}#{nfile}: O{o} H{h} L{l} C{c}")
                    if h < max(o, c) - 1e-9 or l > min(o, c) + 1e-9 or h < l - 1e-9:
                        add("ohlc_relation", f"{name}#{nfile}: O{o} H{h} L{l} C{c}")
                    if v < 0:
                        add("neg_volume", f"{name}#{nfile}: v={v}")
                    if eob <= bob or (eob - bob) != timedelta(minutes=1):
                        add("bad_bar_span", f"{name}#{nfile}: bob={bob} eob={eob}")
                    if prev_bob is not None and bob < prev_bob:
                        add("non_monotonic", f"{name}#{nfile}: {prev_bob} -> {bob}")
                    if not _in_session(bob):
                        add("out_of_session", f"{name}#{nfile}: bob={bob}")
                    prev_bob = bob
                per_prod_rows[sym] += nfile
        except Exception as e:  # noqa: BLE001
            bad_files += 1
            add("file_error", f"{name}: {type(e).__name__} {e}")
    rep = {
        "zip": zpath, "layout": layout, "csv_total": len(names),
        "rows_scanned": total, "bad_files": bad_files,
        "products": len(per_prod_files),
        "days": len(days), "day_first": min(days) if days else None,
        "day_last": max(days) if days else None,
        "violations": {k: v for k, v in viol.items()},
        "viol_total": sum(len(v) for v in viol.values()),
        "per_product_rows": dict(sorted(per_prod_rows.items())),
        "per_product_files": dict(sorted(per_prod_files.items())),
    }
    return rep


# ---------------------------------------------------------------- cross 对拍 --
def _db_creds():
    import psycopg2  # noqa: F401
    creds = dict(host="127.0.0.1", port=5432, user="futures",
                 password=os.environ["POSTGRES_PASSWORD"], dbname="futures")
    env_path = os.path.join(ROOT, ".env")
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"\'')
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


def load_zip_1m(zpath: str, symbols_wanted: set[str], gfex: bool = False) -> dict[str, list]:
    """读 zip 中 9999/8888（或 GFEX 主力）1m → {symbol: [(ts,o,h,l,c,v),...]}。"""
    zf = zipfile.ZipFile(zpath)
    out: dict[str, list] = collections.defaultdict(list)
    names = [n for n in zf.namelist() if n.endswith(".csv")]
    for name in names:
        if gfex:
            m = GFEX_MAIN_RE.match(name)
            if not m:
                continue
            sym = m.group(1).upper() + "888"
        else:
            m = DAY_RE.match(name)
            if not m:
                continue
            mapped = map_symbol(m.group(3))
            if mapped is None:
                continue
            sym, _kind = mapped
        if sym not in symbols_wanted:
            continue
        with zf.open(name) as f:
            text = io.TextIOWrapper(f, encoding="utf-8-sig", errors="replace")
            rdr = csv.reader(text)
            header = next(rdr, [])
            ix = {c: i for i, c in enumerate(header)}
            rows = out[sym]
            for row in rdr:
                if not row or len(row) < len(header):
                    continue
                try:
                    if gfex:
                        ts = datetime.fromisoformat(row[ix["datetime"]])
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone(timedelta(hours=8)))
                        o, h, l, c = (float(row[ix[k]]) for k in ("open", "high", "low", "close"))
                        v = float(row[ix["volume"]])
                    else:
                        ts = datetime.fromisoformat(row[ix["bob"]])
                        o, h, l, c = (float(row[ix[k]]) for k in ("open", "high", "low", "close"))
                        v = float(row[ix["volume"]])
                except Exception:  # noqa: BLE001
                    continue
                rows.append((ts, o, h, l, c, v))
    for k in out:
        out[k].sort(key=lambda r: r[0])
    return out


def agg_bars(rows, freq_min: int) -> dict:
    """START 标签 ts → floor 聚合（与 synthesizer time_bucket 同口径，纯 Python）。"""
    out: dict = {}
    for ts, o, h, l, c, v in rows:  # rows 已按 ts 升序
        b = ts.replace(second=0, microsecond=0)
        b = b - timedelta(minutes=b.minute % freq_min, hours=(b.hour % 24) * 0)
        # 按日对齐的 floor 已由分钟取模覆盖；freq≥60 时按小时取模（60m 与日历小时对齐）
        if freq_min >= 60:
            hh = (b.hour // (freq_min // 60)) * (freq_min // 60)
            b = b.replace(minute=0, hour=hh)
        g = out.get(b)
        if g is None:
            out[b] = {"o": o, "h": h, "l": l, "c": c, "v": v, "n": 1}
        else:
            g["h"] = max(g["h"], h)
            g["l"] = min(g["l"], l)
            g["c"] = c
            g["v"] += v
            g["n"] += 1
    return out


def cross_zip(zpath: str, products: list[str], freqs: list[str], gfex: bool = False) -> None:
    import psycopg2
    creds = _db_creds()
    if gfex:
        syms = [p.upper() + "888" for p in products]
    else:
        syms = []
        for p in products:
            syms += [p.upper() + "888", p.upper() + "8888"]
    dump = load_zip_1m(zpath, set(syms), gfex=gfex)
    conn = psycopg2.connect(**creds)
    cur = conn.cursor()
    freq_iv = {"5m": 5, "15m": 15, "30m": 30, "60m": 60}
    print(f"=== cross {os.path.basename(zpath)} products={products} freqs={freqs} ===")
    for sym in syms:
        rows = dump.get(sym, [])
        if not rows:
            print(f"[{sym}] dump 无数据")
            continue
        lo, hi = rows[0][0], rows[-1][0]
        for fq in freqs:
            table = f"bar_{fq}"
            agg = agg_bars(rows, freq_iv[fq])
            cur.execute(
                f"select bucket::text, open, high, low, close, volume from {table} "
                f"where symbol=%s and bucket>=%s and bucket<%s order by bucket",
                (sym, lo.isoformat(), (hi + timedelta(minutes=freq_iv[fq])).isoformat()))
            dbrows = {datetime.fromisoformat(r[0].replace("+08", "+08:00")): r[1:] for r in cur.fetchall()}
            both = set(agg) & set(dbrows)
            only_d = set(agg) - set(dbrows)
            only_b = set(dbrows) - set(agg)
            maxd = 0.0
            vd = 0
            vd_n = 0
            for b in both:
                a, d = agg[b], dbrows[b]
                dv = [float(x) if x is not None else None for x in d[:4]]
                pairs = [(a["o"], dv[0]), (a["h"], dv[1]), (a["l"], dv[2]), (a["c"], dv[3])]
                diffs = [abs(x - y) for x, y in pairs if y is not None]
                if diffs:
                    maxd = max(maxd, max(diffs))
                if d[4] is not None and abs(a["v"] - float(d[4])) > 1e-6:
                    vd += 1
                    vd_n = max(vd_n, abs(a["v"] - float(d[4])))
            exact = sum(1 for b in both
                        if all(y is not None and abs(x - float(y)) < 1e-6
                               for x, y in [(agg[b]["o"], dbrows[b][0]), (agg[b]["h"], dbrows[b][1]),
                                            (agg[b]["l"], dbrows[b][2]), (agg[b]["c"], dbrows[b][3])]))
            print(f"[{sym} {fq}] db={len(dbrows)} dump={len(agg)} both={len(both)} "
                  f"only_dump={len(only_d)} only_db={len(only_b)} "
                  f"价格全等={exact}/{len(both)} 最大价差={maxd:.4f} 量不等={vd}(最大差{vd_n})")
    conn.commit()
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p1 = sub.add_parser("scan")
    p1.add_argument("--zip", required=True)
    p2 = sub.add_parser("cross")
    p2.add_argument("--zip", required=True)
    p2.add_argument("--products", default="RB,CU,A,M,TA,AP,LC,IF,AU,SC")
    p2.add_argument("--freqs", default="15m,30m,60m,5m")
    p2.add_argument("--gfex", action="store_true")
    a = ap.parse_args()
    if a.cmd == "scan":
        rep = scan_zip(a.zip)
        import json
        print(json.dumps(rep, ensure_ascii=False, indent=1, default=str)[:6000])
    else:
        prods = [x for x in a.products.split(",") if x]
        freqs = [x for x in a.freqs.split(",") if x]
        cross_zip(a.zip, prods, freqs, gfex=a.gfex)


if __name__ == "__main__":
    main()
