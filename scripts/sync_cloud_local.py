# -*- coding: utf-8 -*-
"""
统一云地同步链路（双向、幂等、可复用；支持整表快照 + 增量）

连接：
  - 云端：经本机 SSH 隧道 127.0.0.1:15432 -> 云端 timescaledb:5432
          db=futures user=futures pwd=qhyc_dev_pwd_2026（可用环境变量 CLOUD_PG* 覆盖）
  - 本地：通过 factor_ic_scan.load_creds() 取本机连接（默认 127.0.0.1:5432）
          （可用环境变量 LOCAL_PG* 覆盖，供在云容器内运行时指向另一侧）

装载策略（针对超表与脏数据做了稳健处理）：
  - 清表：普通表 TRUNCATE；超表因 chunk 过多会触发 max_locks 上限，改为逐块 DROP CHUNK。
  - 装载：默认直接 COPY（超表原生分块、不撞锁，已验证）；仅当**探测到源端存在 PK 重复行**
    （如 daily_bar）才走「临时表 + DISTINCT ON(PK)」去重装载，保证目标 PK 不被撞破。
  - 增量：首次无水位自动整表快照并写入水位；之后只拉 src 中 time_col > 水位的行，
    写前先 DELETE 目标 tail(>水位) 保证幂等重放。

用法：
  python scripts/sync_cloud_local.py --tables bar_15m,bar_30m
  python scripts/sync_cloud_local.py --tables bar_15m,bar_30m --mode incremental
  python scripts/sync_cloud_local.py --tables factor_value --direction local2cloud
  python scripts/sync_cloud_local.py --tables bar_15m --dry-run
"""
import argparse, sys, io, datetime as _dt, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from factor_ic_scan import load_creds
import psycopg2

CLOUD = dict(host="127.0.0.1", port=15432, dbname="futures",
            user="futures", password="qhyc_dev_pwd_2026")
TIME_CANDIDATES = ("bucket", "trade_date", "trade_datetime", "datetime",
                   "report_date", "date", "timestamp", "ts", "time")


def _env_dsn(base, prefix):
    out = dict(base)
    for k in ("host", "port", "dbname", "user", "password"):
        v = os.environ.get(f"{prefix}PG{k.upper()}")
        if v is not None:
            out[k] = v if k != "port" else int(v)
    return out


def add_months(d, n):
    m = d.month - 1 + n
    y = d.year + m // 12
    m = m % 12 + 1
    leap = (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0))
    days_in = [31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return _dt.datetime(y, m, min(d.day, days_in), d.hour, d.minute, d.second)


def get_cols(cur, table):
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name=%s ORDER BY ordinal_position", (table,))
    return [r[0] for r in cur.fetchall()]


def detect_time_col(cur, table):
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name=%s ORDER BY ordinal_position", (table,))
    cols = [r[0] for r in cur.fetchall()]
    return next((c for c in cols if c in TIME_CANDIDATES), None)


def detect_pk(cur, table):
    cur.execute(
        "SELECT a.attname FROM pg_index i "
        "JOIN pg_attribute a ON a.attrelid=i.indrelid AND a.attnum=ANY(i.indkey) "
        "WHERE i.indrelid=%s::regclass AND i.indisprimary", (table,))
    return [r[0] for r in cur.fetchall()]


def ensure_sync_state(dc):
    dc.cursor().execute(
        "CREATE TABLE IF NOT EXISTS sync_state ("
        "table_name text PRIMARY KEY, last_value text, rows_synced bigint, updated_at timestamptz DEFAULT now())"
    )
    dc.commit()


def get_watermark(dc, table):
    cur = dc.cursor()
    cur.execute("SELECT last_value FROM sync_state WHERE table_name=%s", (table,))
    r = cur.fetchone()
    return r[0] if r else None


def set_watermark(dc, table, value, rows):
    cur = dc.cursor()
    cur.execute(
        "INSERT INTO sync_state(table_name, last_value, rows_synced, updated_at) "
        "VALUES (%s,%s,%s,now()) ON CONFLICT (table_name) "
        "DO UPDATE SET last_value=EXCLUDED.last_value, rows_synced=EXCLUDED.rows_synced, updated_at=now()",
        (table, value, rows),
    )
    dc.commit()


def _clear_table(dcur, dc, table):
    """清表：超表逐块 DROP（避免 TRUNCATE 锁上限），普通表 TRUNCATE。"""
    dcur.execute("SELECT EXISTS(SELECT 1 FROM timescaledb_information.hypertables WHERE hypertable_name=%s)", (table,))
    if dcur.fetchone()[0]:
        dcur.execute(
            "SELECT chunk_schema, chunk_name FROM timescaledb_information.chunks WHERE hypertable_name=%s",
            (table,))
        for sch, ch in dcur.fetchall():
            dcur.execute(f'DROP TABLE IF EXISTS "{sch}"."{ch}"')
        dc.commit()
    else:
        dcur.execute(f"TRUNCATE {table}")
        dc.commit()


def _source_has_dups(scur, table, pk):
    if not pk:
        return False
    expr = ",".join(pk)
    scur.execute(
        f"SELECT 1 FROM (SELECT {expr} FROM {table} GROUP BY {expr} HAVING count(*)>1 LIMIT 1) s")
    return scur.fetchone() is not None


def _copy_direct(scur, dcur, dc, table, cols, select_sql):
    """源 SELECT -> 缓冲 -> 目标 COPY（超表原生分块，无锁风暴）。返回行数。"""
    buf = io.StringIO()
    scur.copy_expert(select_sql, buf)
    buf.seek(0)
    dcur.copy_expert(f"COPY {table} ({','.join(cols)}) FROM STDIN", buf)
    dc.commit()
    return buf.getvalue().count("\n")


def _copy_staging(scur, dcur, dc, table, cols, pk, tmp, select_sql):
    """源 SELECT -> 临时表(无约束) -> 目标 DISTINCT ON(PK) 去重插入。返回行数。"""
    buf = io.StringIO()
    scur.copy_expert(select_sql, buf)
    buf.seek(0)
    dcur.execute(f"TRUNCATE {tmp}")
    dcur.copy_expert(f"COPY {tmp} ({','.join(cols)}) FROM STDIN", buf)
    dc.commit()
    if pk:
        order = ",".join(pk)
        dcur.execute(
            f"INSERT INTO {table} ({','.join(cols)}) SELECT {','.join(cols)} FROM ("
            f"SELECT DISTINCT ON ({order}) * FROM {tmp} ORDER BY {order}) s")
    else:
        dcur.execute(f"INSERT INTO {table} ({','.join(cols)}) SELECT {','.join(cols)} FROM {tmp}")
    dc.commit()
    dcur.execute(f"SELECT count(*) FROM {tmp}")
    return dcur.fetchone()[0]


def _aligned(scur, dcur, table, tcol):
    """轻量一致性快检：行数相等且（有时间列时）按年分布一致，视为已对齐可跳过清表。"""
    scur.execute(f"SELECT count(*) FROM {table}")
    c1 = scur.fetchone()[0]
    dcur.execute(f"SELECT count(*) FROM {table}")
    c2 = dcur.fetchone()[0]
    if c1 != c2:
        return False
    if tcol:
        q = f"SELECT EXTRACT(year FROM {tcol})::int y, count(*) FROM {table} GROUP BY 1"
        scur.execute(q); cd = dict(scur.fetchall())
        dcur.execute(q); dd = dict(dcur.fetchall())
        if cd != dd:
            return False
    return True


def _snapshot(scur, dcur, dc, table, cols, tcol, chunk_months, dry_run, pk):
    """整表快照：清表 + 分块装载（自动选直接 COPY / 暂存去重）。返回行数。"""
    if dry_run:
        return 0
    if _aligned(scur, dcur, table, tcol):
        print("  already aligned (行数/按年一致), skip clear+reload", flush=True)
        scur.execute(f"SELECT count(*) FROM {table}")
        return scur.fetchone()[0]
    _clear_table(dcur, dc, table)
    need_dedup = _source_has_dups(scur, table, pk)
    tmp = f"tmp_sync_{table}"
    if need_dedup:
        dcur.execute(f"DROP TABLE IF EXISTS {tmp}")
        dcur.execute(f"CREATE TEMP TABLE {tmp} (LIKE {table})")
        dc.commit()
    if not tcol:
        q = f"COPY (SELECT {','.join(cols)} FROM {table}) TO STDOUT"
        if need_dedup:
            return _copy_staging(scur, dcur, dc, table, cols, pk, tmp, q)
        return _copy_direct(scur, dcur, dc, table, cols, q)

    scur.execute(f"SELECT min(({tcol})::text), max(({tcol})::text) FROM {table}")
    d0, d1 = scur.fetchone()
    cur = _dt.datetime.fromisoformat(d0).replace(tzinfo=None)
    end_month = _dt.datetime.fromisoformat(d1).replace(tzinfo=None).replace(day=1)
    done = 0
    while cur <= end_month:
        nxt = add_months(cur, chunk_months)
        q = (f"COPY (SELECT {','.join(cols)} FROM {table} "
             f"WHERE {tcol} >= '{cur.isoformat()}' AND {tcol} < '{nxt.isoformat()}' "
             f"ORDER BY {tcol}) TO STDOUT")
        if need_dedup:
            n = _copy_staging(scur, dcur, dc, table, cols, pk, tmp, q)
        else:
            n = _copy_direct(scur, dcur, dc, table, cols, q)
        done += n
        print(f"  {cur.date()}..{nxt.date()}: {n:,} (cum {done:,})", flush=True)
        cur = nxt
    return done


def sync_table(src, dst, table, *, chunk_months, mode, dry_run):
    sc = psycopg2.connect(**src); scur = sc.cursor()
    dc = psycopg2.connect(**dst); dcur = dc.cursor()

    scols = get_cols(scur, table)
    dcols = get_cols(dcur, table)
    if scols != dcols:
        sc.close(); dc.close()
        raise SystemExit(f"[ABORT] schema mismatch {table}: cloud={scols} local={dcols}")

    scur.execute(f"SELECT count(*) FROM {table}")
    total = scur.fetchone()[0]
    if total == 0:
        sc.close(); dc.close()
        raise SystemExit(f"[ABORT] source {table} empty -> would clear target, aborting")

    tcol = detect_time_col(scur, table)
    pk = detect_pk(dcur, table)
    print(f"[sync {table}] cols={len(scols)} src_rows={total:,} time_col={tcol} pk={pk} mode={mode}", flush=True)

    if dry_run:
        print(f"  (dry-run) would sync {table} from src", flush=True)
        sc.close(); dc.close()
        return

    ensure_sync_state(dc)

    if mode == "incremental" and tcol:
        wm = get_watermark(dc, table)
        if wm is None:
            print("  no watermark -> full snapshot first", flush=True)
            n = _snapshot(scur, dcur, dc, table, scols, tcol, chunk_months, False, pk)
            scur.execute(f"SELECT max(({tcol})::text) FROM {table}")
            mx = scur.fetchone()[0]
            set_watermark(dc, table, mx, n)
            print(f"[sync {table}] DONE snapshot n={n:,} wm={mx}", flush=True)
            sc.close(); dc.close()
            return
        scur.execute(f"SELECT max(({tcol})::text) FROM {table}")
        new_max = scur.fetchone()[0]
        if new_max is None or new_max <= wm:
            print(f"  no new data since wm={wm}, skip", flush=True)
            sc.close(); dc.close()
            return
        dcur.execute(f"DELETE FROM {table} WHERE ({tcol})::text > %s", (wm,))
        dc.commit()
        need_dedup = _source_has_dups(scur, table, pk)
        tmp = f"tmp_sync_{table}"
        if need_dedup:
            dcur.execute(f"DROP TABLE IF EXISTS {tmp}")
            dcur.execute(f"CREATE TEMP TABLE {tmp} (LIKE {table})")
            dc.commit()
            n = _copy_staging(
                scur, dcur, dc, table, scols, pk, tmp,
                f"COPY (SELECT {','.join(scols)} FROM {table} "
                f"WHERE ({tcol})::text > '{wm}' ORDER BY {tcol}) TO STDOUT")
        else:
            n = _copy_direct(
                scur, dcur, dc, table, scols,
                f"COPY (SELECT {','.join(scols)} FROM {table} "
                f"WHERE ({tcol})::text > '{wm}' ORDER BY {tcol}) TO STDOUT")
        set_watermark(dc, table, new_max, n)
        print(f"[sync {table}] DONE incremental +{n:,} (wm {wm} -> {new_max})", flush=True)
        sc.close(); dc.close()
        return

    n = _snapshot(scur, dcur, dc, table, scols, tcol, chunk_months, False, pk)
    if tcol:
        scur.execute(f"SELECT max(({tcol})::text) FROM {table}")
        mx = scur.fetchone()[0]
        set_watermark(dc, table, mx, n)
    print(f"[sync {table}] DONE snapshot n={n:,}", flush=True)
    sc.close(); dc.close()


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--tables", required=True, help="逗号分隔表名")
    ap.add_argument("--direction", default="cloud2local", choices=["cloud2local", "local2cloud"])
    ap.add_argument("--mode", default="snapshot", choices=["snapshot", "incremental"])
    ap.add_argument("--chunk-months", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    LOCAL = _env_dsn(load_creds(), "LOCAL")
    CURRENT_CLOUD = _env_dsn(CLOUD, "CLOUD")
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    for t in tables:
        src = CURRENT_CLOUD if args.direction == "cloud2local" else LOCAL
        dst = LOCAL if args.direction == "cloud2local" else CURRENT_CLOUD
        tag = "cloud->local" if args.direction == "cloud2local" else "local->cloud"
        print(f"=== {t} ({tag}, {args.mode}) ===", flush=True)
        sync_table(src, dst, t, chunk_months=args.chunk_months, mode=args.mode, dry_run=args.dry_run)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
