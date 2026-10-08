# -*- coding: utf-8 -*-
"""分层 schema 感知的云地同步（修复 sync_cloud_local 落到 public 视图的问题）。

背景：G4 分层后真实表在 l0_raw/l1_mkt/l2_adj/l3_ref，public 仅同名视图。
sync_cloud_local.py 用裸表名 + table_schema='public' 取列，写操作会命中视图而失败。
本脚本一律解析物理 schema 并限定，支持：
  - cloud2local snapshot：清空本地物理表 + 从云端按月分块 COPY 重载（解本地缺表/落后）
  - local2cloud merge：把本地 contract_daily 全量按主键 (symbol,trade_date,version) dedup
    合并进云端（只插云端缺失行、不删云端已有、幂等），用于反向补 A1（含 2016-2018 段
    与 2019+ 段本地更全的部分）。

用法：
  python scripts/sync_layered.py --tables roll_segment,factor_ic_roll --direction cloud2local
  python scripts/sync_layered.py --tables fut_kline,main_continuous --direction cloud2local
  python scripts/sync_layered.py --tables contract_daily --direction local2cloud --mode merge
"""
import os, sys, io, argparse, datetime as _dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from factor_ic_scan import load_creds
import psycopg2

# ★ 2026-10-08：写入前必须解压目标 chunk。
#   TimescaleDB 对已压缩 chunk 的 COPY/INSERT 会被 ts_insert_blocker 触发器
#   **静默丢弃**（不报错、行数变 0）——本项目最典型的静默失效类型。
#   本脚本的所有写路径统一经 backfill_window：逐 chunk 解压 → 写 → 压回，
#   磁盘峰值只多一个 chunk，并在收尾做行数闭环断言。
from app.db.timescale_guard import backfill_chunks, backfill_window

TIME_CANDIDATES = ("bucket", "trade_date", "trade_datetime", "datetime",
                   "report_date", "date", "timestamp", "ts", "time")

LOCAL = load_creds()
CLOUD = dict(host="127.0.0.1", port=15432, user="futures", dbname="futures",
            password=LOCAL["password"])


def phys_schema(cur, table):
    cur.execute(
        "SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE c.relname=%s AND c.relkind IN ('r','p') ORDER BY n.nspname LIMIT 1", (table,))
    r = cur.fetchone()
    if not r:
        raise SystemExit(f"[ABORT] {table} 无物理表（仅视图或不存在）")
    return r[0]


def get_cols(cur, schema, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s ORDER BY ordinal_position", (schema, table))
    return [r[0] for r in cur.fetchall()]


def detect_time_col(cols):
    return next((c for c in cols if c in TIME_CANDIDATES), None)


def is_hypertable(cur, schema, table):
    cur.execute(
        "SELECT EXISTS(SELECT 1 FROM timescaledb_information.hypertables "
        "WHERE hypertable_schema=%s AND hypertable_name=%s)", (schema, table))
    return cur.fetchone()[0]


def clear_table(cur, conn, schema, table):
    if is_hypertable(cur, schema, table):
        cur.execute(
            "SELECT chunk_schema, chunk_name FROM timescaledb_information.chunks "
            "WHERE hypertable_schema=%s AND hypertable_name=%s", (schema, table))
        for sch, ch in cur.fetchall():
            cur.execute(f'DROP TABLE IF EXISTS "{sch}"."{ch}"')
        conn.commit()
        # 重建空超表（DROP CHUNK 只删数据块，表结构保留；若表本身被删则重建）
        cur.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relname=%s)", (schema, table))
        if not cur.fetchone()[0]:
            raise SystemExit(f"[ABORT] 超表 {schema}.{table} 结构丢失，需先重建")
    else:
        cur.execute(f'TRUNCATE {schema}."{table}"')
        conn.commit()


def add_months(d, n):
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    leap = (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0))
    days_in = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return _dt.datetime(y, m, min(d.day, days_in), d.hour, d.minute, d.second)


def snapshot(src, dst, table):
    sc = psycopg2.connect(**src); scur = sc.cursor()
    dc = psycopg2.connect(**dst); dcur = dc.cursor()
    ss = phys_schema(scur, table); ds = phys_schema(dcur, table)
    scols = get_cols(scur, ss, table); dcols = get_cols(dcur, ds, table)
    if scols != dcols:
        sc.close(); dc.close()
        raise SystemExit(f"[ABORT] schema mismatch {table}: src={scols} dst={dcols}")
    scur.execute(f"SELECT count(*) FROM {ss}.\"{table}\"")
    total = scur.fetchone()[0]
    if total == 0:
        sc.close(); dc.close()
        raise SystemExit(f"[ABORT] source {ss}.{table} empty -> would clear target, aborting")
    tcol = detect_time_col(scols)
    print(f"[snapshot {table}] src={ss} dst={ds} cols={len(scols)} src_rows={total:,} time_col={tcol}", flush=True)

    try:
        dcur.execute("SET timescaledb.max_tuples_decompressed_per_dml_transaction = 0")
        dc.commit()
    except Exception:
        dc.rollback()

    clear_table(dcur, dc, ds, table)

    done = 0
    if not tcol:
        buf = io.StringIO()
        scur.copy_expert(f"COPY (SELECT {','.join(scols)} FROM {ss}.\"{table}\") TO STDOUT", buf)
        buf.seek(0)
        dcur.copy_expert(f"COPY {ds}.\"{table}\" ({','.join(dcols)}) FROM STDIN", buf)
        dc.commit()
        done = buf.getvalue().count("\n")
        print(f"  full copy (no time col): {done:,}", flush=True)
    else:
        scur.execute(f"SELECT min(({tcol})::text), max(({tcol})::text) FROM {ss}.\"{table}\"")
        d0, d1 = scur.fetchone()
        cur = _dt.datetime.fromisoformat(d0).replace(tzinfo=None)
        end_full = _dt.datetime.fromisoformat(d1).replace(tzinfo=None)
        cm = 3
        while cur <= end_full:
            nxt = add_months(cur, cm)
            q = (f"COPY (SELECT {','.join(scols)} FROM {ss}.\"{table}\" "
                 f"WHERE ({tcol})::date >= DATE '{cur.date().isoformat()}' "
                 f"AND ({tcol})::date < DATE '{nxt.date().isoformat()}' "
                 f"ORDER BY {tcol}) TO STDOUT")
            buf = io.StringIO()
            scur.copy_expert(q, buf)
            buf.seek(0)
            # ★ 2026-10-08：写入前先解压目标 chunk。
            #   COPY INTO 已压缩 chunk 会被 ts_insert_blocker 触发器**静默丢弃**，
            #   且 COPY 不报错、行数看似正常 —— 本项目最典型的静默失效。
            #   这里按 chunk 逐个「解压 → COPY → 压回」，磁盘峰值只多一个 chunk。
            with backfill_window(dcur, table, cur, nxt) as ctx:
                dcur.copy_expert(f"COPY {ds}.\"{table}\" ({','.join(dcols)}) FROM STDIN", buf)
                dc.commit()
                n = buf.getvalue().count("\n")
            done += n
            print(f"  {cur.date()}..{nxt.date()}: {n:,} (cum {done:,})"
                  f"{' [chunk解压]' if ctx.chunks_decompressed else ''}", flush=True)
            cur = nxt
        # NULL 时间列行：分块 WHERE 会漏掉，单独补拷（目标已清，幂等）
        buf = io.StringIO()
        scur.copy_expert(
            f"COPY (SELECT {','.join(scols)} FROM {ss}.\"{table}\" WHERE \"{tcol}\" IS NULL) TO STDOUT", buf)
        if buf.getvalue():
            buf.seek(0)
            dcur.copy_expert(f"COPY {ds}.\"{table}\" ({','.join(dcols)}) FROM STDIN", buf)
            dc.commit()
            nn = buf.getvalue().count("\n")
            done += nn
            print(f"  null-{tcol}: {nn:,} (cum {done:,})", flush=True)
    dcur.execute(f"SELECT count(*) FROM {ds}.\"{table}\"")
    final = dcur.fetchone()[0]
    # ★ 闭环断言（2026-10-08）：COPY 没有 rowcount，且压缩 chunk 会静默丢数，
    #   所以唯一可靠的校验是「目标表实际行数 == 源表行数」。
    #   目标表已被 clear_table 清空重建，故这里应严格相等。
    src_total = total if not tcol else _count_src(scur, ss, table, tcol)
    if final != src_total:
        raise SystemExit(
            f"[ABORT] {ds}.{table} 行数不符：源 {src_total:,} != 目标 {final:,}"
            f"（差 {final - src_total:,}）。最可能原因：目标 chunk 处于压缩态，"
            f"COPY 被 ts_insert_blocker 静默丢弃。请确认已用 backfill_window 解压。"
        )
    print(f"[snapshot {table}] DONE loaded={done:,} dst_now={final:,} ✓校验通过", flush=True)
    sc.close(); dc.close()


def _count_src(cur, schema: str, table: str, tcol: str) -> int:
    """源表行数（含 NULL 时间列），用于 snapshot 的闭环断言。"""
    cur.execute(
        f'SELECT count(*) FILTER (WHERE "{tcol}" IS NOT NULL), '
        f'count(*) FILTER (WHERE "{tcol}" IS NULL) FROM {schema}."{table}"')
    a, b = cur.fetchone()
    return int(a) + int(b)


def merge_contract_daily(local, cloud):
    """local2cloud：全量按主键 (symbol,trade_date,version) dedup 合并到云端。
    只插入云端缺失的行，不删除云端已有数据、幂等、可重跑。
    用于补齐云端 contract_daily（2016-2018 段 + 2019+ 段本地更全的部分）。"""
    table = "contract_daily"
    sc = psycopg2.connect(**local); scur = sc.cursor()
    dc = psycopg2.connect(**cloud); dcur = dc.cursor()
    ss = phys_schema(scur, table); ds = phys_schema(dcur, table)
    cols = get_cols(scur, ss, table)
    print(f"[merge {table}] src={ss} dst={ds} cols={len(cols)} (full dedup by PK)", flush=True)

    try:
        dcur.execute("SET timescaledb.max_tuples_decompressed_per_dml_transaction = 0")
        dc.commit()
    except Exception:
        dc.rollback()

    # 本地全量导出 -> 云端临时表
    buf = io.StringIO()
    scur.copy_expert(f"COPY (SELECT {','.join(cols)} FROM {ss}.\"{table}\") TO STDOUT", buf)
    buf.seek(0)
    dcur.execute(f'CREATE TEMP TABLE "_stg_{table}" (LIKE {ds}."{table}" INCLUDING ALL) ON COMMIT DROP')
    dcur.copy_expert(f'COPY "_stg_{table}" ({",".join(cols)}) FROM STDIN', buf)
    # 按主键 dedup 插入云端（冲突即跳过，幂等）
    # ★ 2026-10-08：先解压目标 chunk —— INSERT INTO 已压缩 chunk 会被
    #   ts_insert_blocker 静默丢弃（rowcount 变 0、不报错）。这正是本次修的静默失效。
    dcur.execute(f'SELECT min(trade_date), max(trade_date) FROM "_stg_{table}"')
    lo, hi = dcur.fetchone()
    dcur.execute(f'SELECT count(*) FROM "_stg_{table}"')
    staged = dcur.fetchone()[0]

    with backfill_window(dcur, table, lo, hi, compress_back=True) as ctx:
        dcur.execute(
            f'INSERT INTO {ds}."{table}" SELECT * FROM "_stg_{table}" '
            f'ON CONFLICT (symbol, trade_date, version) DO NOTHING')
        n = dcur.rowcount
        dc.commit()  # 提交后临时表自动 DROP
        # 断言：不能出现「staged>0 但一条都没插进去」——那正是压缩静默拦截的特征。
        # 分母用 staged 是**保守下界**：已存在的行会因 ON CONFLICT 跳过，
        # 故只在「一行都没插进去」时才判定为异常。
        if staged > 0 and n == 0:
            dcur.execute(f'SELECT count(*) FROM {ds}."{table}"')
            before = dcur.fetchone()[0]
            raise SystemExit(
                f"[ABORT] {ds}.{table} 暂存 {staged:,} 行但插入 0 行。"
                f"若源与目标本就有差异（全部冲突）可忽略；否则极可能是目标 chunk "
                f"仍处压缩态被静默拦截（解压前目标 {before:,} 行）。"
            )

    dcur.execute(f'SELECT count(*) FROM {ds}."{table}"')
    tot = dcur.fetchone()[0]
    print(f"[merge {table}] staged={staged:,} inserted={n:,} "
          f"cloud_total={tot:,} min={lo} max={hi} "
          f"(chunks 解压{ctx.chunks_decompressed}/压回{ctx.chunks_recompressed})", flush=True)
    sc.close(); dc.close()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", required=True)
    ap.add_argument("--direction", default="cloud2local", choices=["cloud2local", "local2cloud"])
    ap.add_argument("--mode", default="snapshot", choices=["snapshot", "merge"])
    args = ap.parse_args()
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    for t in tables:
        print(f"=== {t} ({args.direction}/{args.mode}) ===", flush=True)
        try:
            if args.direction == "cloud2local":
                snapshot(LOCAL, CLOUD, t) if False else snapshot(CLOUD, LOCAL, t)
            else:
                if t == "contract_daily" and args.mode == "merge":
                    merge_contract_daily(LOCAL, CLOUD)
                else:
                    raise SystemExit(f"[ABORT] local2cloud 仅支持 contract_daily merge，收到 {t}/{args.mode}")
        except SystemExit as e:
            print(f"[skip {t}] {e}", flush=True)
        except Exception as e:
            print(f"[ERROR {t}] {type(e).__name__}: {e}", flush=True)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
