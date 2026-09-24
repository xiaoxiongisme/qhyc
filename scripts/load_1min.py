"""Load 1-minute futures CSVs (D:/学习资料) into TimescaleDB and synthesize
5/15/30/60-minute bars via time_bucket.

Datasets:
  - 期货主力连续1min最新  (codes *9999.*) -> system symbol XXXX888
  - 期货商品指数1min最新  (codes *8888.*) -> symbol XXXX8888 (kept literal)

Filename formats:
  - OLD (2005-2024): CODE.EXCH_YYYY_1min.csv        (full year)
  - NEW (2025)     : CODE.EXCH_Y1_M1_D1_Y2_M2_D2_1min.csv (a quarter / range)

minute_bar.ts is an **END-time** label in Asia/Shanghai (ts=09:01 covers 09:00-09:01;
day session first bar 09:01, last bar 15:00). Multi-minute bars are labelled by START
time (bucket=09:00 covers 09:00-10:00 for 60m).
"""
import os, re, io, argparse, time, multiprocessing
import numpy as np
import pandas as pd
import psycopg2

SH_FMT = "%Y-%m-%d %H:%M:%S%z"  # -> 2025-01-02 09:01:00+0800 (parsed by COPY)


def _load_db_creds():
    """Read POSTGRES_* from project .env (never hardcode secrets).

    环境变量优先级最高（容器内运行 / 远端部署时用 POSTGRES_HOST 指向 timescaledb 服务名，
    不要依赖 127.0.0.1）。
    """
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
    env_map = {"POSTGRES_HOST": "host", "POSTGRES_PORT": "port",
               "POSTGRES_USER": "user", "POSTGRES_PASSWORD": "password",
               "POSTGRES_DB": "dbname"}
    for ek, ck in env_map.items():
        if os.environ.get(ek):
            creds[ck] = int(os.environ[ek]) if ck == "port" else os.environ[ek]
    return creds


DB = _load_db_creds()

MAIN = r"D:/学习资料/期货主力连续1min最新"
INDEX = r"D:/学习资料/期货商品指数1min最新"

PAT_OLD = re.compile(r"^(?P<code>[A-Za-z0-9]+)\.(?P<exch>[A-Z]+)_(?P<year>\d{4})_1min\.csv$")
PAT_NEW = re.compile(
    r"^(?P<code>[A-Za-z0-9]+)\.(?P<exch>[A-Z]+)_"
    r"\d+_\d+_\d+_\d+_\d+_\d+_1min\.csv$"
)

COLS = ["open", "high", "low", "close", "volume", "money",
        "high_limit", "low_limit", "pre_close", "open_interest", "settle_price"]

OUT_COLS = ["symbol", "ts", "open", "high", "low", "close", "volume",
            "amount", "open_interest", "high_limit", "low_limit",
            "pre_close", "settle_price", "contract", "src"]


def map_symbol(code, dataset):
    if dataset == "main" and code.endswith("9999"):
        return code[:-4] + "888"
    return code  # index *8888 kept literal; main fallback kept as-is


def gen_files():
    for root, _, fs in os.walk(MAIN):
        for f in fs:
            if f.endswith(".csv"):
                yield os.path.join(root, f), "main"
    for root, _, fs in os.walk(INDEX):
        for f in fs:
            if f.endswith(".csv"):
                yield os.path.join(root, f), "index"


def parse_symbol(path, dataset):
    name = os.path.basename(path)
    m = PAT_NEW.match(name) or PAT_OLD.match(name)
    if not m:
        return None
    return map_symbol(m.group("code"), dataset)


DDL = """
DROP TABLE IF EXISTS bar_60m;
DROP TABLE IF EXISTS bar_30m;
DROP TABLE IF EXISTS bar_15m;
DROP TABLE IF EXISTS bar_5m;
DROP TABLE IF EXISTS minute_bar;
CREATE TABLE minute_bar (
  symbol text NOT NULL,
  ts timestamptz NOT NULL,
  open numeric, high numeric, low numeric, close numeric,
  volume bigint, amount numeric, open_interest numeric,
  high_limit numeric, low_limit numeric, pre_close numeric, settle_price numeric,
  contract text, src text,
  PRIMARY KEY (symbol, ts)
);
-- plain table (no hypertable): 20y x 82 symbols under 7-day chunking would create
-- ~85k chunks and crash concurrent loads. PK index covers (symbol, ts) lookups.

CREATE TABLE bar_5m (
  symbol text NOT NULL, bucket timestamptz NOT NULL,
  open numeric, high numeric, low numeric, close numeric,
  volume bigint, amount numeric, open_interest numeric,
  PRIMARY KEY (symbol, bucket));
CREATE TABLE bar_15m (
  symbol text NOT NULL, bucket timestamptz NOT NULL,
  open numeric, high numeric, low numeric, close numeric,
  volume bigint, amount numeric, open_interest numeric,
  PRIMARY KEY (symbol, bucket));
CREATE TABLE bar_30m (
  symbol text NOT NULL, bucket timestamptz NOT NULL,
  open numeric, high numeric, low numeric, close numeric,
  volume bigint, amount numeric, open_interest numeric,
  PRIMARY KEY (symbol, bucket));
CREATE TABLE bar_60m (
  symbol text NOT NULL, bucket timestamptz NOT NULL,
  open numeric, high numeric, low numeric, close numeric,
  volume bigint, amount numeric, open_interest numeric,
  PRIMARY KEY (symbol, bucket));
-- bars also plain tables (PK on (symbol, bucket) suffices)
"""


def load(conn, files):
    cur = conn.cursor()
    total = 0
    t0 = time.time()
    for i, (path, dset) in enumerate(files, 1):
        symbol = parse_symbol(path, dset)
        if symbol is None:
            print("SKIP (bad name)", os.path.basename(path))
            continue
        try:
            df = pd.read_csv(path, index_col=0)
            df.index = pd.to_datetime(df.index).tz_localize(None).tz_localize("Asia/Shanghai")
            contract = os.path.basename(path).split("_1min")[0]
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
            # sanitize: coerce numerics, drop inf, volume NaN->0 (int)
            for c in ["open", "high", "low", "close", "amount", "open_interest",
                      "high_limit", "low_limit", "pre_close", "settle_price", "volume"]:
                out[c] = pd.to_numeric(out[c], errors="coerce")
            out = out.replace([np.inf, -np.inf], np.nan)
            out["volume"] = out["volume"].fillna(0).astype("int64")
            buf = io.StringIO()
            out.to_csv(buf, index=False, header=False)
            buf.seek(0)
            cur.copy_expert(
                "COPY minute_bar (symbol, ts, open, high, low, close, volume, amount, "
                "open_interest, high_limit, low_limit, pre_close, settle_price, contract, src) "
                "FROM STDIN WITH (FORMAT CSV)", buf)
            conn.commit()
            total += len(out)
        except Exception as e:
            conn.rollback()
            print(f"FAIL [{i}] {symbol} {os.path.basename(path)}: {e}")
            continue
        if i % 50 == 0:
            print(f"[{i}/{len(files)}] loaded={total} files/s={ (i)/(time.time()-t0):.1f}")
    print(f"DONE loaded={total} in {time.time()-t0:.0f}s")


BAR_TABLES = [("bar_5m", "5 minutes"), ("bar_15m", "15 minutes"),
              ("bar_30m", "30 minutes"), ("bar_60m", "60 minutes")]


def _all_symbols(conn):
    """取 minute_bar 全部 symbol（loose index scan，避免全表扫描）。"""
    cur = conn.cursor()
    cur.execute("""
        WITH RECURSIVE t AS (
          SELECT (SELECT symbol FROM minute_bar ORDER BY symbol LIMIT 1) AS k
          UNION ALL
          SELECT (SELECT symbol FROM minute_bar WHERE symbol > t.k ORDER BY symbol LIMIT 1)
          FROM t WHERE t.k IS NOT NULL
        ) SELECT k FROM t WHERE k IS NOT NULL ORDER BY k""")
    return [r[0] for r in cur.fetchall()]


def _synth_symbol(conn, symbol, since=None, cascade=True):
    """为单个品种重算全部 4 个周期（一个事务）。

    cascade=True（默认）：**级联聚合** —— 5m 由分钟线聚合，15m 由 5m 聚合，30m 由 15m，
    60m 由 30m。因为 5/15/30/60 都是日历对齐且逐级整除（15=3×5、30=2×15、60=2×30），
    桶边界天然对齐，first/max/min/last/sum 语义逐级等价。
    好处：总扫描量从「4 次读分钟线」降到「1 次读分钟线 + 3 次读逐级缩小的中间表」，
    约为原来的 1/3（每品种 460 万行 → 150 万行）。

    cascade=False：每个周期独立从分钟线聚合（等价但慢），用于校验级联结果。
    """
    cur = conn.cursor()
    if cascade and since is None:
        cur.execute("""
            INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
            SELECT %s,
                   time_bucket(INTERVAL '5 minutes', ts - INTERVAL '1 minute', 'Asia/Shanghai'),
                   first(open, ts), max(high), min(low), last(close, ts),
                   sum(volume)::bigint, sum(amount), last(open_interest, ts)
            FROM minute_bar WHERE symbol = %s AND close IS NOT NULL GROUP BY 2
        """, (symbol, symbol))
        cur.execute("""
            INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
            SELECT symbol, time_bucket(INTERVAL '15 minutes', bucket, 'Asia/Shanghai'),
                   first(open, bucket), max(high), min(low), last(close, bucket),
                   sum(volume)::bigint, sum(amount), last(open_interest, bucket)
            FROM bar_5m WHERE symbol = %s GROUP BY 1, 2
        """, (symbol,))
        cur.execute("""
            INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
            SELECT symbol, time_bucket(INTERVAL '30 minutes', bucket, 'Asia/Shanghai'),
                   first(open, bucket), max(high), min(low), last(close, bucket),
                   sum(volume)::bigint, sum(amount), last(open_interest, bucket)
            FROM bar_15m WHERE symbol = %s GROUP BY 1, 2
        """, (symbol,))
        cur.execute("""
            INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
            SELECT symbol, time_bucket(INTERVAL '60 minutes', bucket, 'Asia/Shanghai'),
                   first(open, bucket), max(high), min(low), last(close, bucket),
                   sum(volume)::bigint, sum(amount), last(open_interest, bucket)
            FROM bar_30m WHERE symbol = %s GROUP BY 1, 2
        """, (symbol,))
        conn.commit()
        return

    for tname, interval in BAR_TABLES:
        sql = f"""
            INSERT INTO {tname} (symbol, bucket, open, high, low, close, volume, amount, open_interest)
            SELECT %s,
                   time_bucket(INTERVAL %s, ts - INTERVAL '1 minute', 'Asia/Shanghai') AS bucket,
                   first(open, ts), max(high), min(low), last(close, ts),
                   sum(volume)::bigint, sum(amount), last(open_interest, ts)
            FROM minute_bar
            WHERE symbol = %s AND close IS NOT NULL
        """
        params = [symbol, interval, symbol]
        if since:
            sql += " AND ts >= %s"
            params.append(since)
        sql += " GROUP BY 2"
        cur.execute(sql, tuple(params))
    conn.commit()


def _synth_worker(shard):
    """多进程 worker：处理一批 symbol。"""
    syms, since = shard
    conn = psycopg2.connect(**DB)
    try:
        for s in syms:
            _synth_symbol(conn, s, since)
    finally:
        conn.close()
    return len(syms)


def synthesize_batched(conn, since=None, workers=4, only=None):
    """按品种分批合成（**默认路径**）。

    为什么不用单次全表聚合：全量写法是 `GROUP BY symbol, bucket` 一次聚合 1.37 亿行，
    bar_5m 要产出 ~2900 万组，哈希表远超 work_mem 只能 spill 到磁盘 —— 实测跑 41 分钟
    连第一个周期都没完成。按 symbol 分批后每批只有 ~17 万组，走 (symbol, ts) 主键索引
    顺序扫描，内存放得下、可多进程并行。
    """
    cur = conn.cursor()
    cur.execute("SET work_mem='256MB'")
    symbols = _all_symbols(conn)
    if not symbols:
        print("minute_bar 为空，跳过合成")
        return
    if only:
        want = {s.strip() for s in only.split(",") if s.strip()}
        miss = want - set(symbols)
        if miss:
            print(f"⚠ 以下品种在 minute_bar 中不存在，已忽略：{sorted(miss)}")
        symbols = [s for s in symbols if s in want]
        if not symbols:
            print("没有匹配的品种，跳过合成")
            return
    if since:
        # 增量：只删待重算区间
        for tname, _ in BAR_TABLES:
            cur.execute(f"DELETE FROM {tname} WHERE bucket >= %s", (since,))
            print(f"  {tname}: 清除 {cur.rowcount} 行待重算")
    elif only:
        # 指定品种：只删这些品种（⚠ 绝不能 TRUNCATE，会把其它品种一起清掉）
        for tname, _ in BAR_TABLES:
            cur.execute(f"DELETE FROM {tname} WHERE symbol = ANY(%s)", (symbols,))
            print(f"  {tname}: 清除 {cur.rowcount} 行（指定品种）")
    else:
        # 全量：整表重建（TRUNCATE 比逐 symbol DELETE 快得多）
        for tname, _ in BAR_TABLES:
            cur.execute(f"TRUNCATE TABLE {tname}")
    conn.commit()

    print(f"分批合成：{len(symbols)} 个品种 × {len(BAR_TABLES)} 个周期，workers={workers}")
    w = max(1, min(workers, os.cpu_count() or 4, len(symbols)))
    shards = [(symbols[i::w], since) for i in range(w)]
    t0 = time.time()
    if w == 1:
        _synth_worker(shards[0])
    else:
        with multiprocessing.Pool(w) as pool:
            pool.map(_synth_worker, shards)
    for tname, _ in BAR_TABLES:
        cur.execute(f"SELECT count(*) FROM {tname}")
        print(f"  {tname}: {cur.fetchone()[0]} 行")
    print(f"分批合成完成，用时 {time.time()-t0:.0f}s")


def synthesize(conn, since=None):
    """由 minute_bar 合成 5/15/30/60 分钟线（**单次全表聚合，很慢**）。

    保留仅为对照/兜底；日常请用 synthesize_batched()。

    since: 增量模式，只重算该时间点之后的桶（**请传日期或更早，按天对齐**）。


    两处口径修正（2026-09-24）：

    1) 分桶基准：minute_bar.ts 是 **end-time 标记**（ts=09:01 代表 09:00–09:01 这一分钟；
       日盘首根 09:01、末根 15:00）。直接用 time_bucket(ts) 会把每根 K 线归到它结束时刻
       所在的桶，导致每个交易时段首尾各错分一根：15m 的 10:00 桶会把 10:00–10:15 与
       10:31–11:00 两段（跨越 15 分钟休市）合并成一根假 K 线，11:00 桶只有 31 分钟、
       13:00 桶只有 29 分钟；日线末根还会多出 15:00 / 23:00 各 1 根"碎片收盘 K"。
       正确做法：以 start-time = ts - 1 分钟 分桶，bucket 标签即该段起始时刻。

    2) 过滤上市前占位行：部分品种（股指/国债等）在上市年份之前存在全 NULL 占位分钟，
       约占 800 万行。不过滤会把 NULL 传播进合成表（TL888 的 15m 有 90% 为 NULL）。
    """
    cur = conn.cursor()
    # 1.43 亿行哈希聚合需要大 work_mem，否则全 spill 到磁盘会极慢
    cur.execute("SET work_mem='4GB'")
    for tname, interval in [("bar_5m", "5 minutes"), ("bar_15m", "15 minutes"),
                            ("bar_30m", "30 minutes"), ("bar_60m", "60 minutes")]:
        if since:
            cur.execute(f"DELETE FROM {tname} WHERE bucket >= %s", (since,))
            n_del = cur.rowcount
            print(f"  {tname}: 清除待重算桶 {n_del} 行")
        else:
            cur.execute(f"TRUNCATE TABLE {tname}")
        sql = f"""
            INSERT INTO {tname} (symbol, bucket, open, high, low, close, volume, amount, open_interest)
            SELECT symbol,
                   time_bucket(INTERVAL %s, ts - INTERVAL '1 minute', 'Asia/Shanghai') AS bucket,
                   first(open, ts), max(high), min(low), last(close, ts),
                   sum(volume)::bigint, sum(amount), last(open_interest, ts)
            FROM minute_bar
            WHERE close IS NOT NULL
        """
        params = [interval]
        if since:
            sql += " AND ts >= %s"
            params.append(since)
        sql += " GROUP BY symbol, bucket"
        cur.execute(sql, tuple(params))
        conn.commit()
        n = cur.rowcount
        print(f"synthesized {tname}: {n} rows")


def _worker(shard):
    conn = psycopg2.connect(**DB)
    try:
        load(conn, shard)
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None, help="process only first N files (smoke test)")
    ap.add_argument("--skip-load", action="store_true")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--since", default=None,
                    help="增量合成：只重算该时间点之后的桶（传日期，如 2025-05-01；"
                         "会自动按天对齐）。全量合成很慢，日常增量请用这个")
    ap.add_argument("--no-batch", action="store_true",
                    help="用单次全表聚合（很慢，实测 bar_5m 一轮 >41 分钟）；默认按品种分批")
    ap.add_argument("--symbols", default=None,
                    help="只合成指定品种，逗号分隔（如 FG888,RB888）；用于定点修复与冒烟测试")
    args = ap.parse_args()

    if not args.skip_load:
        print("== creating schema ==")
        conn = psycopg2.connect(**DB)
        cur = conn.cursor()
        cur.execute(DDL)
        conn.commit()
        conn.close()
        print("== loading 1-min (parallel) ==")
        files = list(gen_files())
        if args.limit:
            files = files[:args.limit]
        workers = max(1, min(args.workers, os.cpu_count() or 4, len(files) or 1))
        shards = [files[i::workers] for i in range(workers)]
        with multiprocessing.Pool(workers) as pool:
            pool.map(_worker, shards)

    print("== synthesizing 5/15/30/60m ==")
    conn = psycopg2.connect(**DB)
    if args.no_batch:
        synthesize(conn, since=args.since)          # 单次全表聚合（慢，仅兜底）
    else:
        synthesize_batched(conn, since=args.since, workers=args.workers,
                           only=args.symbols)
    conn.close()
    print("ALL DONE")


if __name__ == "__main__":
    main()
