# -*- coding: utf-8 -*-
"""D:/学习资料 交易所分钟包 → minute_bar 缺口补录转换（本地跑，输出 gz csv 供服务器 COPY）

源 A（交易所分钟包按日按合约）: 2026 3-4月.zip → 只取 *9999(主连)/*8888(指数) 文件
    列: exchange,symbol,open,close,high,low,amount,volume,position,bob,eob,type,sequence
    映射: ts=bob(+08), symbol=品种码大写+888/8888, contract=原symbol, oi=position, amount=amount, src=csv_1min

源 B（交易所主连长历史包）: 六大交易所 zip → KQ.m@ 主连，只取 ts >= --since 段
    列: datetime,exchange,variety,symbol,datetime_nano,open,high,low,close,volume,open_interest,close_interest
    映射: ts=datetime(+08), symbol=KQ.m@EX.code→CODE大写+888, contract=KQ.m@原码, oi=open_interest 列(与既有 GFEX 导入一致), src=csv_1min

用法:
  python import_inbox_1m.py convert-a "D:/学习资料/2026 3-4月.zip" D:/qhyc_inbox_1m
  python import_inbox_1m.py convert-b "D:/学习资料" D:/qhyc_inbox_1m --since 2026-03-01
输出: out_*/part_NNN.csv.gz（无表头，云端 minute_bar 列序，tab 分隔 → COPY ... WITH (FORMAT text)）
"""
import zipfile, csv, gzip, io, os, re, sys, argparse, glob

COLS = ['symbol','ts','open','high','low','close','volume','amount','open_interest',
        'high_limit','low_limit','pre_close','settle_price','contract','src']
ROWS_PER_PART = 1_000_000


def new_writer(outdir, tag, idx):
    path = os.path.join(outdir, f"{tag}_part_{idx:03d}.csv.gz")
    f = gzip.open(path, 'wt', encoding='utf-8', newline='')
    return f, csv.writer(f, delimiter='\t')


def fmt_ts(s, has_tz=False):
    if has_tz:
        return s.strip()              # 已是 ...+08:00
    return s.strip() + '+08'          # 北京墙钟 → +08


def convert_a(zip_path, outdir):
    os.makedirs(outdir, exist_ok=True)
    tag = 'a_1min'
    idx, nrows = 1, 0
    f, w = new_writer(outdir, tag, idx)
    pat = re.compile(r'(?:^|/)(\d{8})/([A-Za-z]+)(9999|8888)\.csv$')
    with zipfile.ZipFile(zip_path) as zf:
        targets = [n for n in zf.namelist() if pat.search(n)]
        print(f"[A] 匹配 9999/8888 文件数: {len(targets)}")
        for n in targets:
            m = pat.search(n)
            var, kind = m.group(2), m.group(3)
            sym = var.upper() + ('888' if kind == '9999' else '8888')
            with zf.open(n) as fh:
                for line in io.TextIOWrapper(fh, encoding='utf-8'):
                    p = line.rstrip('\n').split(',')
                    if len(p) < 13 or p[0] == 'exchange':
                        continue
                    # exchange,symbol,open,close,high,low,amount,volume,position,bob,eob,type,sequence
                    row = [sym, fmt_ts(p[9], True), p[2], p[4], p[5], p[3],
                           p[7], p[6], p[8], '', '', '', '', p[1], 'csv_1min']
                    w.writerow(row)
                    nrows += 1
                    if nrows % ROWS_PER_PART == 0:
                        f.close(); idx += 1
                        f, w = new_writer(outdir, tag, idx)
    f.close()
    print(f"[A] 完成: {nrows} 行, {idx} 个分卷")


def convert_b(srcdir, outdir, since):
    os.makedirs(outdir, exist_ok=True)
    tag = 'b_kqm'
    idx, nrows = 1, 0
    f, w = new_writer(outdir, tag, idx)
    zips = sorted(glob.glob(os.path.join(srcdir, '*_*.zip')))
    zips = [z for z in zips if re.search(r'(SHFE|CZCE|DCE|INE|CFFEX|GFEX)_', os.path.basename(z))]
    print(f"[B] 交易所包: {[os.path.basename(z) for z in zips]}")
    kqm = re.compile(r'KQ\.m@([A-Z]+)\.([A-Za-z]+)')
    for z in zips:
        with zipfile.ZipFile(z) as zf:
            mains = [n for n in zf.namelist() if '/00_主力连续/' in n and n.endswith('.csv')]
            for n in mains:
                sym = None
                with zf.open(n) as fh:
                    for line in io.TextIOWrapper(fh, encoding='utf-8-sig'):
                        p = line.rstrip('\n').split(',')
                        if len(p) < 12 or p[0] == 'datetime':
                            if sym is None and len(p) >= 5:
                                m = kqm.match(p[3])
                                sym = (m.group(2).upper() + '888') if m else None
                            continue
                        if sym is None:
                            m = kqm.match(p[3])
                            if not m:
                                break
                            sym = m.group(2).upper() + '888'
                        if p[0] < since:
                            continue
                        # datetime,...,open,high,low,close,volume,open_interest,close_interest
                        row = [sym, fmt_ts(p[0]), p[5], p[6], p[7], p[8],
                               p[9], '', p[10], '', '', '', '', p[3], 'csv_1min']
                        w.writerow(row)
                        nrows += 1
                        if nrows % ROWS_PER_PART == 0:
                            f.close(); idx += 1
                            f, w = new_writer(outdir, tag, idx)
                print(f"    {os.path.basename(n)} -> {sym} 累计 {nrows}")
    f.close()
    print(f"[B] 完成: {nrows} 行, {idx} 个分卷")


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['convert-a', 'convert-b'])
    ap.add_argument('src')
    ap.add_argument('outdir')
    ap.add_argument('--since', default='2026-03-01')
    args = ap.parse_args()
    if args.cmd == 'convert-a':
        convert_a(args.src, args.outdir)
    else:
        convert_b(args.src, args.outdir, args.since)
