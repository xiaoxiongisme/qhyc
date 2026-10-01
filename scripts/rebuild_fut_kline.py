"""fut_kline 夜间派生作业（方案 1：物化派生）。

背景：原本 fut_kline 由 3 个独立 tqsdk/复权写入（fetch_fdf→continuous+contract、
adjust_fdf→cont_adj daily/hourly、_adjust_bars→cont_adj min15/30/60）维护，来源分散、
与权威分钟源不一致。本作业改为**单一夜间派生**：从权威源重建 fut_kline：
  - cont_adj  (daily/hourly/min15/min30/min60) ← minute_bar_adj（已复权的 1 分钟主连，888 法）
  - continuous(daily/hourly/min15/min30/min60) ← bar_* （未复权主连，888 法，仅取 888 主连）
  - contract  (daily)                           ← contract_daily（UPSERT，保留历史逐合约序列）

关键约束：
  - cont_adj/continuous 做整表重建（DELETE+INSERT，生产模式）。contract 只能 UPSERT：逐合约
    历史只存在于 fut_kline 本身（contract_daily 仅 100 个活跃合约），不能整表重建，否则丢失
    历史 2868 个合约。故 contract 仅 upsert 活跃合约，历史行原样保留。
  - 符号口径严格对齐 archive：
      cont_adj  : 沿用分钟源 symbol（如 A888 / AP888，全大写 product+888），与 archive 一致。
      continuous: 翻译为天勤码 KQ.m@EX.PROD（DCE/SHFE/INE/GFEX 产品小写，CZCE 大写）。
      contract  : 翻译为 EX.原生（CZCE 4位→3位，如 CZCE.FG701；DCE 小写 a2611）。
  - 时区：全部 time_bucket 用 'Asia/Shanghai'，与 synthesizer/_adjust_bars 一致。
  - fut_kline 表无 src 列，INSERT 仅 11 列。
  - 性能：minute_bar_adj 达 57.8M 行，单次整表 time_bucket 聚合会打崩小实例（写物化 OOM）。
    故 cont_adj 按品种（symbol ~ '888$'）分块，每品种用一次 CTE 扫描 + 5 频率 UNION ALL，
    逐品种提交，既避免全表扫描也把单次写控制在千级行内。

用法：
  python scripts/rebuild_fut_kline.py                 # 真写 fut_kline（先 DELETE 后 INSERT）
  python scripts/rebuild_fut_kline.py --dry-run       # 落到 fut_kline_drv 临时表，不碰生产表
  python scripts/rebuild_fut_kline.py --limit 10      # 仅前 10 个品种（验证用，配合 --dry-run）
  python scripts/rebuild_fut_kline.py --dsn postgresql://...   # 指定库（测试用）
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

# scripts/ 下直接运行时自举仓库根（与容器内 /app 一致），使 `app` 包可导入
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import close_all_sessions

FN_TO_NATIVE = r"""
CREATE OR REPLACE FUNCTION to_native_symbol(std TEXT, ex TEXT) RETURNS TEXT AS $$
DECLARE
  s TEXT := upper(coalesce(std,''));
  exu TEXT := upper(coalesce(ex,''));
  m TEXT[]; prod TEXT; num TEXT;
BEGIN
  IF s ~ '^[A-Za-z]+888$' THEN RETURN s; END IF;
  SELECT regexp_match(s, '^([A-Za-z]+)(\d{2,6})$') INTO m;
  IF m IS NULL THEN RETURN s; END IF;
  prod := m[1]; num := m[2];
  IF exu = 'CZCE' AND length(num) = 4 THEN
    RETURN prod || substr(num, 2);
  ELSIF exu IN ('SHFE','DCE','GFEX','INE') THEN
    RETURN lower(prod) || num;
  ELSE
    RETURN s;
  END IF;
END;
$$ LANGUAGE plpgsql IMMUTABLE;
"""

CONT_ADJ_FREQS = [  # (freq_label, time_bucket interval)
    ("daily", "1 day"), ("hourly", "60 minutes"),
    ("min15", "15 minutes"), ("min30", "30 minutes"), ("min60", "60 minutes"),
]

# continuous：freq -> (源表, 是否按日聚合)
CONT_SOURCES = [
    ("min15", "bar_15m", False),
    ("min30", "bar_30m", False),
    ("min60", "bar_60m", False),
    ("hourly", "bar_60m", False),
    ("daily", "bar_60m", True),
]


BATCH = 8  # 每批品种数：3GB 容器内 20/批触发 cgroup OOM（2026-10-01 实测），降为 8


def _batch(iterable, n):
    it = list(iterable)
    for i in range(0, len(it), n):
        yield it[i:i + n]


def cont_adj_stmt_batch(target, freq, iv, batch):
    """单频率单批次：仅扫描本批品种（索引），一次聚合写入。"""
    ph = ",".join(f":s{i}" for i in range(len(batch)))
    binds = {f"s{i}": s for i, s in enumerate(batch)}
    stmt = text(f"""
INSERT INTO {target} (freq,kind,symbol,trade_datetime,open,high,low,close,volume,oi,adj)
SELECT :f, 'cont_adj', symbol,
       time_bucket(INTERVAL '{iv}', ts, 'Asia/Shanghai'),
       first(open, ts), max(high), min(low), last(close, ts),
       sum(volume)::bigint, last(open_interest, ts)::bigint, 0
FROM minute_bar_adj
WHERE symbol IN ({ph})
GROUP BY symbol, time_bucket(INTERVAL '{iv}', ts, 'Asia/Shanghai')
""").bindparams(f=freq, **binds)
    return stmt


def continuous_stmt(target, freq, tbl, daily):
    bucket = ("time_bucket(INTERVAL '1 day', b.bucket, 'Asia/Shanghai')"
              if daily else "b.bucket")
    return text(f"""
INSERT INTO {target} (freq,kind,symbol,trade_datetime,open,high,low,close,volume,oi,adj)
SELECT :f, 'continuous', g.sym, g.dt, g.open, g.high, g.low, g.close, g.volume, g.oi, 0
FROM (
  SELECT
    'KQ.m@' || COALESCE(m.exchange,'XX') || '.' ||
      CASE WHEN m.exchange IN ('SHFE','DCE','GFEX','INE')
           THEN lower(left(b.symbol,-3)) ELSE left(b.symbol,-3) END AS sym,
    {bucket} AS dt,
    first(b.open, b.bucket) AS open, max(b.high) AS high,
    min(b.low) AS low, last(b.close, b.bucket) AS close,
    sum(b.volume)::bigint AS volume, last(b.open_interest, b.bucket)::bigint AS oi
  FROM {tbl} b
  LEFT JOIN contract_code_map m ON m.product = left(b.symbol,-3)
  WHERE b.symbol ~ '^[A-Za-z]+888$'
  GROUP BY
    'KQ.m@' || COALESCE(m.exchange,'XX') || '.' ||
      CASE WHEN m.exchange IN ('SHFE','DCE','GFEX','INE')
           THEN lower(left(b.symbol,-3)) ELSE left(b.symbol,-3) END,
    {bucket}
) g
""").bindparams(f=freq)


def contract_stmt(target):
    return text(f"""
INSERT INTO {target} (freq,kind,symbol,trade_datetime,open,high,low,close,volume,oi,adj)
SELECT 'daily','contract',
       cd.exchange || '.' || to_native_symbol(cd.symbol, cd.exchange),
       cd.trade_date::timestamptz,
       cd.open, cd.high, cd.low, cd.close, cd.volume, cd.oi, 0
FROM contract_daily cd
ON CONFLICT (freq,kind,symbol,trade_datetime) DO UPDATE SET
  open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low, close=EXCLUDED.close,
  volume=EXCLUDED.volume, oi=EXCLUDED.oi, adj=EXCLUDED.adj
""")


def build_engine(dsn: str | None):
    """2026-10-01 云端 OOM 实测：timescaledb 容器 mem_limit=3GB，20 品种/批的
    全历史聚合 INSERT 使 backend 峰值内存打满 cgroup（16:15 OOM kill postgres
    backend，生产重建在 min30 中途崩退）。会话 work_mem 24MB（hash/sort 溢盘换
    稳定）+ BATCH 20→8 双管齐下。

    注意 get_engine() 是全局共享 engine：借用前先关掉本进程已占用的池连接，
    避免与调度器进程内其它会话争抢内存。"""
    if not dsn:
        close_all_sessions()
        from app.core.db import get_engine
        eng = get_engine()
    else:
        eng = create_engine(dsn)

    @event.listens_for(eng, "connect")
    def _low_mem(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("SET work_mem = '24MB'")
        # 2026-10-01 云端实测：minute_bar_adj ~4170 个日 chunk，8 品种批的查询计划
        # 生成 5402 个 JIT 函数、大部分 chunk actual rows=0，EXPLAIN ANALYZE 单批
        # SELECT 185s 中 JIT 编译占大头 —— 高 chunk 数场景 jit 纯属开销，off。
        cur.execute("SET jit = off")
        cur.close()

    return eng


def main() -> int:
    ap = argparse.ArgumentParser(description="fut_kline 夜间派生重建")
    ap.add_argument("--dry-run", action="store_true",
                    help="落到 fut_kline_drv 临时表，不碰生产 fut_kline")
    ap.add_argument("--limit", type=int, default=0,
                    help="仅处理前 N 个品种（验证用，0=全部）")
    ap.add_argument("--dsn", default=None, help="指定数据库连接（测试用）")
    a = ap.parse_args()

    eng = build_engine(a.dsn)
    target = "fut_kline_drv" if a.dry_run else "fut_kline"
    print(f"[rebuild] target={target} dry_run={a.dry_run} limit={a.limit}", flush=True)

    t0 = time.time()
    with eng.begin() as c:
        c.execute(text(FN_TO_NATIVE))
        if a.dry_run:
            c.execute(text(f"DROP TABLE IF EXISTS {target}"))
            c.execute(text(f"""
            CREATE TABLE {target} (
                freq TEXT NOT NULL, kind TEXT NOT NULL, symbol TEXT NOT NULL,
                trade_datetime TIMESTAMPTZ NOT NULL, open NUMERIC(20,4), high NUMERIC(20,4),
                low NUMERIC(20,4), close NUMERIC(20,4), volume BIGINT, oi BIGINT,
                adj NUMERIC(20,4) DEFAULT 0,
                PRIMARY KEY (freq, kind, symbol, trade_datetime)
            )"""))
            print(f"  [dry] created temp {target}", flush=True)

    # ⚠ 安全前置（2026-09-30 实测事故路径）：生产模式是 **先 DELETE 后取源**，
    # 源表一旦缺失/为空，就会先把 fut_kline 的 cont_adj+continuous 删光再失败
    # —— 云端这两个 kind 合计 1,992 万行，删了就回不来。必须在 DELETE 之前拦死。
    # 实测：本地无 minute_bar_adj 表（云端有 5,765 万行），本地跑生产模式即触发此路径。
    REQUIRED_SOURCES = ("minute_bar_adj",)
    with eng.connect() as c:
        for src in REQUIRED_SOURCES:
            if not c.execute(text(f"select to_regclass('{src}') is not null")).scalar():
                print(f"  [abort] 源表 {src} 不存在 —— 拒绝执行"
                      f"（生产模式会先 DELETE fut_kline 再重建，源缺失=删空后失败）", flush=True)
                return 2
            if not c.execute(text(f"select exists (select 1 from {src} limit 1)")).scalar():
                print(f"  [abort] 源表 {src} 为空 —— 拒绝执行", flush=True)
                return 2
            print(f"  [preflight] {src} 存在且非空", flush=True)

    # 生产模式：清空待重建的 cont_adj / continuous（contract 不删，仅 upsert）
    if not a.dry_run:
        with eng.begin() as c:
            c.execute(text(
                "DELETE FROM fut_kline WHERE kind IN ('cont_adj','continuous')"))
            print("  [prod] cleared cont_adj/continuous for rebuild", flush=True)

    # 品种清单
    with eng.connect() as c:
        syms = [r[0] for r in c.execute(text(
            "SELECT distinct symbol FROM minute_bar_adj WHERE symbol ~ '^[A-Za-z]+888$'"
        )).all()]
    if a.limit:
        syms = syms[:a.limit]
    print(f"  [info] cont_adj 处理品种数 = {len(syms)}", flush=True)

    # cont_adj：按频率分块（每频率每批 BATCH 个品种，索引扫描），逐批提交
    conn = eng.connect()
    try:
        for freq, iv in CONT_ADJ_FREQS:
            cnt = 0
            for bi, batch in enumerate(_batch(syms, BATCH), 1):
                conn.execute(cont_adj_stmt_batch(target, freq, iv, batch))
                conn.commit()
                cnt += len(batch)
                if bi % 2 == 0 or bi == (len(syms) + BATCH - 1) // BATCH:
                    n = conn.execute(text(
                        f"SELECT count(*) FROM {target} WHERE kind='cont_adj' AND freq=:x"
                    ), {"x": freq}).scalar()
                    print(f"  [cont_adj/{freq}] {cnt}/{len(syms)} 累计 {n} rows", flush=True)
    finally:
        conn.close()

    # continuous：bar_* 为小表，单次聚合安全
    with eng.begin() as c:
        for freq, tbl, daily in CONT_SOURCES:
            c.execute(continuous_stmt(target, freq, tbl, daily))
            n = c.execute(text(
                f"SELECT count(*) FROM {target} WHERE kind='continuous' AND freq=:x"
            ), {"x": freq}).scalar()
            print(f"  [continuous/{freq}] -> {n} rows", flush=True)

        # contract：UPSERT（生产表历史行保留）
        c.execute(contract_stmt(target))
        n_con = c.execute(text(
            f"SELECT count(*) FROM {target} WHERE kind='contract'")).scalar()
        print(f"  [contract/daily] -> {n_con} rows (upsert, history preserved)", flush=True)

    print(f"[rebuild] done in {time.time()-t0:.1f}s target={target}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
