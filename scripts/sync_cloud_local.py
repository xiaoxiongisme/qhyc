# -*- coding: utf-8 -*-
"""
统一云地同步链路（双向、幂等、可复用；支持整表快照 + 增量）

连接：
  - 云端：经本机 SSH 隧道 127.0.0.1:15432 -> 云端 timescaledb:5432
          db/user 走 CLOUD_PG* 环境变量（口令无默认，缺失即报错，勿再硬编码）
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
  python scripts/sync_cloud_local.py --tables bar_60m --mode incremental --lookback-days 30

回看窗口（--lookback-days，增量模式默认 30 天）
--------------------------------------------
云端分钟线是「尾部重算 + 回补」语义：重建时会把过去 N 天整段重算，并把原本缺失的
**旧日期行**补进去。纯水位增量只拉 > wm 的行，云端回补到 wm 之前的行永远同步不到，
本地与云端会出现「永久历史分歧」（2026-09-29 实测 bar_60m 差 3,205 行，全在回补窗口内）。
因此增量每次额外重放 [now-N天, wm] 窗口（先删后拷，幂等）。
"""
import os
import argparse, sys, io, datetime as _dt, os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from factor_ic_scan import load_creds
import psycopg2

CLOUD = dict(host="127.0.0.1", port=15432, dbname="futures",
            user="futures", password=os.environ["POSTGRES_PASSWORD"])
TIME_CANDIDATES = ("bucket", "trade_date", "trade_datetime", "datetime",
                   "report_date", "date", "timestamp", "ts", "time")


def _env_dsn(base, prefix):
    """按 {prefix}PGHOST/PORT/DBNAME/USER/PASSWORD 覆盖 base。

    注意：docker-compose 里历史上写的是 `{prefix}PGPWD`（少 PASS 几个字母），
    只认 `{prefix}PGPASSWORD` 会静默回落到默认口令 —— 一旦 .env 改口令，
    云地同步会用错口令连库并失败。这里两个名字都认，PWD 优先于默认值。
    """
    out = dict(base)
    for k in ("host", "port", "dbname", "user", "password"):
        v = os.environ.get(f"{prefix}PG{k.upper()}")
        if v is None and k == "password":
            v = os.environ.get(f"{prefix}PGPWD")
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
    """列名列表。

    必须限定 ``table_schema='public'``：G4 分层后同名对象在两个 schema 并存
    （如 ``l0_raw.daily_bar`` 表 + ``public.daily_bar`` 兼容视图），只按
    ``table_name`` 过滤会让**每列返回 2 行**，本函数进而返回带重复项的列名列表，
    被用来拼 INSERT/SELECT 时会产生重复列。另加 DISTINCT 兜底。
    """
    cur.execute(
        "SELECT DISTINCT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s ORDER BY column_name",
        (table,))
    return [r[0] for r in cur.fetchall()]


def detect_time_col(cur, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s ORDER BY ordinal_position",
        (table,))
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


def sync_table(src, dst, table, *, chunk_months, mode, dry_run, lookback_days=0,
               force_regressed=False):
    sc = psycopg2.connect(**src); scur = sc.cursor()
    dc = psycopg2.connect(**dst); dcur = dc.cursor()

    # 2026-09-29 实测：目标库的表若是**已启用压缩的超表**，增量模式先 DELETE 水位之后的
    # 行会触发解压，默认上限 100,000 元组 → 直接报错：
    #   ConfigurationLimitExceeded: tuple decompression limit exceeded by operation
    #   HINT: increase timescaledb.max_tuples_decompressed_per_dml_transaction or set to 0
    # 0 = 不限制。增量 DELETE 的窗口很小（只删水位之后的行），不会失控；
    # 本会话内设置，不影响实例全局。不修的话 03:15 的定时同步每晚都会失败。
    try:
        dcur.execute("SET timescaledb.max_tuples_decompressed_per_dml_transaction = 0")
        dc.commit()
    except Exception:  # noqa: BLE001  非 TimescaleDB 或无该 GUC → 忽略
        dc.rollback()

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

        # ------------------------------------------------------------------
        # 源端回退守卫（2026-09-29 实测踩出来的坑，比历史分歧更危险）
        # 云端分钟线重建（adjust_minute）是「先删尾部再重算」：作业跑的过程中
        # 源表 max(bucket) 会**先变小后变大**。若此刻同步，会把本地整段尾部
        # 删成云端的中间态 —— 实测 bar_30m 被回滚 7 天（4,883,649 → 4,880,860，
        # max 09-29 → 09-22），本地凭空少了 7 天数据且不报错。
        # 因此：源端尾部早于已有水位 = 源在重建/异常 → 直接中止，不动本地。
        # 确需强制覆盖时用 --force-regressed-src（例如确认云端重建已完成）。
        # ------------------------------------------------------------------
        if new_max is not None and new_max < wm and not force_regressed:
            sc.close(); dc.close()
            raise SystemExit(
                f"[ABORT] {table}: src max {new_max} < watermark {wm} — "
                f"源端尾部回退（多半是云端重建作业正在跑），放弃本次同步以免回滚本地。"
                f"如需强制：加 --force-regressed-src")

        # ------------------------------------------------------------------
        # 回看窗口（2026-09-29 实测发现的结构性缺陷）
        # 云端分钟线/日线是「尾部重算 + 回补」语义：adjust_minute 每次重建会把
        # 过去 N 天的历史窗口整段重算并**补进旧日期里原本缺失的行**。
        # 而纯水位增量只拉 > wm 的行 —— 云端往 wm 之前回补的旧行永远同步不到，
        # 本地与云端会在历史段产生「永久分歧」（实测 bar_60m 差 3,205 行，
        # 全部落在 2026-09-01..09-22，即云端回补窗口内）。
        # 解决：每次增量都把 [now - lookback_days, wm] 这段一起重放（先删后拷，幂等）。
        # 窗口默认 30 天，覆盖云端回补半径；代价只有几万行，可忽略。
        # ------------------------------------------------------------------
        start = wm
        if lookback_days and lookback_days > 0:
            lb = (_dt.datetime.now() - _dt.timedelta(days=int(lookback_days))).isoformat()
            if lb < start:
                start = lb
            print(f"  lookback {lookback_days}d -> replay from {start}", flush=True)

        if new_max is not None and new_max <= start:
            print(f"  no new data since {start}, skip", flush=True)
            sc.close(); dc.close()
            return
        if new_max is None:
            new_max = start
        dcur.execute(f"DELETE FROM {table} WHERE ({tcol})::text > %s", (start,))
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
                f"WHERE ({tcol})::text > '{start}' ORDER BY {tcol}) TO STDOUT")
        else:
            n = _copy_direct(
                scur, dcur, dc, table, scols,
                f"COPY (SELECT {','.join(scols)} FROM {table} "
                f"WHERE ({tcol})::text > '{start}' ORDER BY {tcol}) TO STDOUT")
        set_watermark(dc, table, new_max, n)
        print(f"[sync {table}] DONE incremental +{n:,} (wm {wm} -> {new_max}, replay from {start})", flush=True)
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
    ap.add_argument("--lookback-days", type=int, default=30,
                    help="增量模式下额外重放的回看窗口天数（修云端往历史回补导致的永久分歧）；0=关闭")
    ap.add_argument("--force-regressed-src", action="store_true",
                    help="源端尾部早于水位时仍强制同步（默认中止，防回滚本地）")
    args = ap.parse_args()

    LOCAL = _env_dsn(load_creds(), "LOCAL")
    CURRENT_CLOUD = _env_dsn(CLOUD, "CLOUD")
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    for t in tables:
        src = CURRENT_CLOUD if args.direction == "cloud2local" else LOCAL
        dst = LOCAL if args.direction == "cloud2local" else CURRENT_CLOUD
        tag = "cloud->local" if args.direction == "cloud2local" else "local->cloud"
        print(f"=== {t} ({tag}, {args.mode}) ===", flush=True)
        try:
            sync_table(src, dst, t, chunk_months=args.chunk_months, mode=args.mode,
                       dry_run=args.dry_run, lookback_days=args.lookback_days,
                       force_regressed=args.force_regressed_src)
        except SystemExit as e:
            # 单表失败（如源端回退中止）不应中断其余表的同步
            print(f"[sync {t}] SKIP: {e}", flush=True)
    print("ALL DONE", flush=True)


if __name__ == "__main__":
    main()
