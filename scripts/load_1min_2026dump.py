#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
load_1min_2026dump.py — D:/学习资料 dump（2026.zip 布局A / GFEX 布局B）1 分钟导入 + 窗口化合成

设计要点（合成口径与 app/ingest/synthesizer.py 完全一致）：
- minute_bar: ts=bob(START 标签), PK(symbol,ts)；COPY 进 TEMP stage → INSERT ... ON CONFLICT (symbol,ts) DO NOTHING
- 合成 SQL: time_bucket(INTERVAL 'N minutes', ts, 'Asia/Shanghai'),
            first(open,ts), max(high), min(low), last(close,ts),
            sum(volume)::bigint, sum(amount), last(open_interest,ts)
- 布局A（2026.zip 逐日 2026/YYYYMM/YYYYMMDD/code.csv, 列含 bob/eob）:
    9999 → 主连 888（仅 bar_5m：15/30/60 已由 天勤 补齐且桶键对拍 100% 一致，保持不动）
    8888 → 指数连 8888（bar_5m/15m/30m/60m 全四周期，2026 全缺）
    9998（次主力）与合约级 csv 跳过
    合成模式 = rebuild（窗口化 DELETE+INSERT，窗口 ⊆ dump 覆盖段且该段 raw 已导入）
- 布局B（GFEX 天勤导出 品种/00_主力连续/P_main_1m.csv）:
    仅归档 raw 的 2026-01-01 起段（2022-2025 段按保留策略#1 不入 raw；原 zip 留磁盘可重灌）
    bar_* 聚合补洞 = fill 模式（只 INSERT ON CONFLICT DO NOTHING，不 DELETE，保留 天勤 已填桶）
    GFEX csv 无 amount 列 → raw.amount=NULL, 合成 amount=NULL
- 幂等：全链路 ON CONFLICT DO NOTHING，可安全重跑
- 安全：绝不调用 synthesize_bars_full / synthesize_bars_incremental / sp_build_l1

用法:
  python load_1min_2026dump.py --zip "D:/学习资料/2026.zip" --targets local            # dry-run
  python load_1min_2026dump.py --zip "D:/学习资料/2026.zip" --targets local --apply
  python load_1min_2026dump.py --zip "D:/学习资料/GFEX_广州期货交易所.zip" --gfex --targets local --apply
  python load_1min_2026dump.py --zip "D:/学习资料/2026.zip" --targets cloud --apply
"""
import os
from __future__ import annotations

import argparse
import csv
import io
import re
import zipfile
from datetime import datetime, timedelta, timezone

import psycopg2

TZ8 = timezone(timedelta(hours=8))
CONT_RE = re.compile(r"^([A-Za-z]+?)(9999|8888|9998)$")
GFEX_DIR_RE = re.compile(r"^([A-Za-z]+)_[^/]+/00_主力连续/[^/]+_main_1m\.csv$")
FREQS = {"5m": ("bar_5m", 5), "15m": ("bar_15m", 15),
         "30m": ("bar_30m", 30), "60m": ("bar_60m", 60)}
CREDS = {
    "local": dict(host="127.0.0.1", port=5432, user="futures",
                  password=os.environ["POSTGRES_PASSWORD"], dbname="futures"),
    "cloud": dict(host="127.0.0.1", port=15432, user="futures",
                  password=os.environ["POSTGRES_PASSWORD"], dbname="futures"),
}
SYNTH_SQL = (
    "INSERT INTO {t} (symbol, bucket, open, high, low, close, volume, amount, open_interest)\n"
    "SELECT symbol, time_bucket(INTERVAL '{iv} minutes', ts, 'Asia/Shanghai') AS bucket,\n"
    "       first(open, ts), max(high), min(low), last(close, ts),\n"
    "       sum(volume)::bigint, sum(amount), last(open_interest, ts)\n"
    "FROM minute_bar\n"
    "WHERE symbol = %s AND ts >= %s AND ts < %s\n"
    "GROUP BY symbol, bucket\n"
    "{conflict}"
)


def map_symbol(code: str) -> str | None:
    """a9999→A888 主连；A8888→A8888 指数连；9998/合约级 → None"""
    m = CONT_RE.match(code)
    if not m:
        return None
    prod, suf = m.group(1).upper(), m.group(2)
    if suf == "9999":
        return prod + "888"
    if suf == "8888":
        return prod + "8888"
    return None


def iter_layout_a(zf: zipfile.ZipFile, families: set[str]):
    """2026.zip：列 exchange,symbol,open,close,high,low,amount,volume,position,bob,eob,type,sequence"""
    for name in zf.namelist():
        if not name.endswith(".csv"):
            continue
        base = name.rsplit("/", 1)[-1]
        sym = map_symbol(base[:-4])
        if sym is None:
            continue
        if ("main" if sym.endswith("888") and not sym.endswith("8888") else "index") not in families:
            continue
        with zf.open(name) as f:
            reader = csv.reader(io.TextIOWrapper(f, encoding="utf-8"))
            header = next(reader)
            idx = {c: i for i, c in enumerate(header)}
            for row in reader:
                if len(row) < len(header):
                    continue
                try:
                    ts = datetime.fromisoformat(row[idx["bob"]])
                    o = float(row[idx["open"]]); h = float(row[idx["high"]])
                    l = float(row[idx["low"]]); c = float(row[idx["close"]])
                    v = int(float(row[idx["volume"]]))
                    amt = float(row[idx["amount"]])
                    oi = float(row[idx["position"]])
                except (ValueError, KeyError, IndexError):
                    continue
                yield (sym, ts, o, h, l, c, v, amt, oi, row[idx["symbol"]], "csv_1min")


def iter_layout_b(zf: zipfile.ZipFile, families: set[str], since: str):
    """GFEX zip：列 datetime,exchange,variety,symbol,datetime_nano,open,high,low,close,volume,open_interest,close_interest"""
    cut = datetime.fromisoformat(since).replace(tzinfo=TZ8)
    for name in zf.namelist():
        if not name.endswith(".csv"):
            continue
        m = GFEX_DIR_RE.match(name)
        if not m:
            continue
        sym = m.group(1).upper() + "888"   # GFEX 无指数连续导出，均为主力连续族
        if "main" not in families:
            continue
        with zf.open(name) as f:
            reader = csv.reader(io.TextIOWrapper(f, encoding="utf-8-sig"))
            header = next(reader)
            idx = {c: i for i, c in enumerate(header)}
            for row in reader:
                if len(row) < len(header):
                    continue
                try:
                    ts = datetime.strptime(row[idx["datetime"]],
                                           "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ8)
                    if ts < cut:
                        continue
                    o = float(row[idx["open"]]); h = float(row[idx["high"]])
                    l = float(row[idx["low"]]); c = float(row[idx["close"]])
                    v = int(float(row[idx["volume"]]))
                    oi = float(row[idx["open_interest"]])
                except (ValueError, KeyError, IndexError):
                    continue
                yield (sym, ts, o, h, l, c, v, None, oi, row[idx["symbol"]], "csv_gfex")


STAGE_DDL = (
    "CREATE TEMP TABLE mb_stage (symbol text, ts timestamptz, open numeric, high numeric, "
    "low numeric, close numeric, volume bigint, amount numeric, open_interest numeric, "
    "contract text, src text) ON COMMIT DROP"
)


def _fmt(x) -> str:
    return "" if x is None else repr(x)


def _copy_rows(cur, rows) -> int:
    buf = io.StringIO()
    n = 0
    for tup in rows:
        sym, ts, o, h, l, c, v, amt, oi, contract, src = tup
        buf.write(",".join([sym, ts.isoformat(), repr(o), repr(h), repr(l), repr(c),
                            str(v), _fmt(amt), repr(oi), contract, src]) + "\n")
        n += 1
        if n % 50000 == 0:
            buf.seek(0)
            cur.copy_expert("COPY mb_stage FROM STDIN WITH (FORMAT csv)", buf)
            buf = io.StringIO()
    if n % 50000:
        buf.seek(0)
        cur.copy_expert("COPY mb_stage FROM STDIN WITH (FORMAT csv)", buf)
    return n


def stage_copy(cur, rows) -> int:
    cur.execute(STAGE_DDL)
    return _copy_rows(cur, rows)


def stage_append(cur, rows) -> int:
    """向已存在的 mb_stage 追加（多 zip 混跑；TEMP 表随事务存续）"""
    return _copy_rows(cur, rows)


def window_of(cur, sym: str):
    cur.execute("SELECT min(ts), max(ts) FROM mb_stage WHERE symbol = %s", (sym,))
    mn, mx = cur.fetchone()
    if mn is None:
        return None, None
    lo = mn.astimezone(TZ8).replace(hour=0, minute=0, second=0, microsecond=0)
    hi = mx.astimezone(TZ8).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return lo, hi


def synthesize(cur, sym: str, lo, hi, freqs: list[str], mode: str) -> dict:
    out = {}
    for fq in freqs:
        t, iv = FREQS[fq]
        dele = 0
        if mode == "rebuild":
            cur.execute(f"DELETE FROM {t} WHERE symbol=%s AND bucket>=%s AND bucket<%s",
                        (sym, lo, hi))
            dele = cur.rowcount
            conflict = ""
        else:  # fill：只补缺桶
            conflict = "ON CONFLICT (symbol, bucket) DO NOTHING"
        cur.execute(SYNTH_SQL.format(t=t, iv=iv, conflict=conflict), (sym, lo, hi))
        out[fq] = (dele, cur.rowcount)
    return out


def detect_layout(zf: zipfile.ZipFile) -> str:
    """按第一个 csv 的表头自动检测布局：A=2026.zip（含 bob 列），B=GFEX（含 variety 列）"""
    for name in zf.namelist():
        if not name.endswith(".csv"):
            continue
        with zf.open(name) as f:
            head = f.read(600).decode("utf-8-sig", errors="replace").splitlines()
        cols = set(head[0].split(",")) if head else set()
        if "bob" in cols:
            return "A"
        if "variety" in cols:
            return "B"
    raise ValueError(f"无法识别布局: {zf.filename}")


def run_target(target: str, zips: list[str], families: set[str], apply: bool, gfex: bool,
               gfex_since: str):
    conn = psycopg2.connect(**CREDS[target])
    conn.autocommit = False
    cur = conn.cursor()
    print(f"\n########## target={target} {'APPLY' if apply else 'DRY-RUN'} ##########")
    stage_ready = False
    for zpath in zips:
        zf = zipfile.ZipFile(zpath)
        layout = detect_layout(zf)
        # 布局只决定解析器；--gfex 只决定合成模式（True=fill 只补缺，False=布局默认 rebuild/fill）
        rows = iter_layout_b(zf, families, gfex_since) if layout == "B" else iter_layout_a(zf, families)
        if not stage_ready:
            n_raw = stage_copy(cur, rows)
            stage_ready = True
        else:
            n_raw = stage_append(cur, rows)
        cur.execute("SELECT count(*), count(DISTINCT symbol) FROM mb_stage")
        total, nsym = cur.fetchone()
        cur.execute("SELECT min(ts), max(ts) FROM mb_stage")
        gmn, gmx = cur.fetchone()
        print(f"\n--- {zpath} [布局{layout}]")
        print(f"    stage: {total} 行 / {nsym} 符号 / 范围 {gmn} .. {gmx}")
        if total == 0:
            continue
        cur.execute("SELECT DISTINCT symbol FROM mb_stage ORDER BY 1")
        syms = [r[0] for r in cur.fetchall()]
        if not apply:
            # dry-run：报现有 raw 重叠量级（全库同窗口）；TEMP 表保留供后续 zip 追加
            cur.execute("SELECT count(*) FROM minute_bar WHERE ts >= %s AND ts < %s", (gmn, gmx))
            print(f"    [dry] 库内同窗口 raw 已有 {cur.fetchone()[0]} 行（将被 ON CONFLICT 跳过/共存）")
            for sym in syms:
                lo, hi = window_of(cur, sym)
                freqs = (["5m"] if (gfex is False and sym.endswith("888") and not sym.endswith("8888"))
                         else list(FREQS))
                est = {fq: None for fq in freqs}
                cur.execute("SELECT count(*) FROM mb_stage WHERE symbol=%s", (sym,))
                print(f"    [dry] {sym}: raw={cur.fetchone()[0]} 合成 freqs={freqs} 窗口 {lo:%F}..{hi:%F}")
                del est
            continue
        # ---- APPLY ----
        cur.execute("""INSERT INTO minute_bar
                       (symbol, ts, open, high, low, close, volume, amount, open_interest, contract, src)
                       SELECT symbol, ts, open, high, low, close, volume, amount, open_interest, contract, src
                       FROM mb_stage
                       ON CONFLICT (symbol, ts) DO NOTHING""")
        print(f"    raw INSERT: 插入 {cur.rowcount}（冲突跳过 {total - cur.rowcount}）")
        synth_total = {}
        for sym in syms:
            lo, hi = window_of(cur, sym)
            if gfex:
                freqs = list(FREQS)
                mode = "fill"
            else:
                is_main = sym.endswith("888") and not sym.endswith("8888")
                freqs = ["5m"] if is_main else list(FREQS)
                mode = "rebuild"
            res = synthesize(cur, sym, lo, hi, freqs, mode)
            for fq, (dele, ins) in res.items():
                d, i = synth_total.get(fq, (0, 0))
                synth_total[fq] = (d + dele, i + ins)
        for fq, (dele, ins) in sorted(synth_total.items()):
            print(f"    合成 {fq}: 删 {dele} / 插 {ins} 桶")
        zf.close()
    # TEMP 表 ON COMMIT DROP：所有 zip 必须在同一事务内处理，最后统一提交/回滚
    if apply:
        conn.commit()
    else:
        conn.rollback()
    conn.close()


def verify(target: str):
    conn = psycopg2.connect(**CREDS[target])
    cur = conn.cursor()
    print(f"\n########## verify({target}) ##########")
    checks = [
        ("minute_bar 2026-01-05..03-01 全符号",
         "SELECT count(*), count(DISTINCT symbol) FROM minute_bar WHERE ts>='2026-01-05' AND ts<'2026-03-01'"),
        ("bar_5m  A888  Jan-Feb",
         "SELECT count(*), min(bucket)::text, max(bucket)::text FROM bar_5m WHERE symbol='A888' AND bucket>='2026-01-05' AND bucket<'2026-03-01'"),
        ("bar_5m  A8888 2026 全期",
         "SELECT count(*), min(bucket)::text, max(bucket)::text FROM bar_5m WHERE symbol='A8888' AND bucket>='2026-01-01'"),
        ("bar_15m A8888 2026 全期",
         "SELECT count(*), min(bucket)::text, max(bucket)::text FROM bar_15m WHERE symbol='A8888' AND bucket>='2026-01-01'"),
        ("bar_60m A8888 2026 全期",
         "SELECT count(*), min(bucket)::text, max(bucket)::text FROM bar_60m WHERE symbol='A8888' AND bucket>='2026-01-01'"),
        ("bar_5m  LC888 2026 全期（GFEX 补洞后）",
         "SELECT count(*), min(bucket)::text, max(bucket)::text FROM bar_5m WHERE symbol='LC888' AND bucket>='2026-01-01'"),
        ("raw 新品种 AD/BZ/OP/PD/PL/PT",
         "SELECT count(DISTINCT symbol) FROM minute_bar WHERE symbol ~ '(AD|BZ|OP|PD|PL|PT)(888|8888)$'"),
        ("bar_* 符号总数",
         "SELECT (SELECT count(DISTINCT symbol) FROM bar_5m), (SELECT count(DISTINCT symbol) FROM bar_15m), (SELECT count(DISTINCT symbol) FROM bar_60m)"),
    ]
    for label, sql in checks:
        try:
            cur.execute(sql)
            print(f"  {label}: {cur.fetchone()}")
        except Exception as e:
            conn.rollback()
            print(f"  {label}: ERR {str(e)[:100]}")
    conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--zip", action="append", required=True)
    ap.add_argument("--targets", default="local")
    ap.add_argument("--symbols", default="main,index")
    ap.add_argument("--gfex", action="store_true")
    ap.add_argument("--gfex-since", default="2026-01-01")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    families = set(a.symbols.split(","))
    for target in a.targets.split(","):
        run_target(target, a.zip, families, a.apply, a.gfex, a.gfex_since)
    if a.apply:
        for target in a.targets.split(","):
            verify(target)


if __name__ == "__main__":
    main()
