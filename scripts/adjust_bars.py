# -*- coding: utf-8 -*-
"""对 bar_15m/30m/60m 的 888 主连序列做加法平移前复权 → fut_kline(kind='cont_adj')。

生产脚本（原 `runtime/_adjust_bars.py`，因 .dockerignore 排除 runtime 导致云端
每天静默失败，2026-09-29 迁入 scripts/ 并改造连接为环境变量驱动）。

算法（不变）
-----------
换月检测双门，解决「误把隔夜跳空当换月」：
  1) is_large = |cc888| > (high-low) + (prev high-low)      [888 大跳空]
  2) roll     = is_large AND |cc8888| < RATIO*|cc888|       [指数连8888未同步跳→换月断层]
市场跳空（隔夜/周末）两端同跳 → 被 2) 排除，保留不动，不平移（否则扭曲收益率）。

前复权（锚定最新段不变）：
  cum[i] = Σ_{k>i, roll[k]} cc888[k]
  adjusted OHLC[i] = OHLC[i] + cum[i]
跨换月 bar i: cc'[i] = close[i]+cum[i] - (close[i-1]+cum[i-1]) = 0 → 断层消除，盈亏点数不变。

作业级保护（2026-09-29 新增）
----------------------------
  * pg_advisory_lock 串行锁：避免与 bars 合成作业撞车
  * 源尾部新鲜度校验：源 max(bucket) 不得早于目标已有 max，否则中止（防尾删竞态）

用法
----
  python scripts/adjust_bars.py --freq min15
  python scripts/adjust_bars.py --freq min30 --limit 5   # 调试
"""
import argparse
import io
import os
import sys
import time

import numpy as np
import pandas as pd
import psycopg2

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pgconn import (  # noqa: E402
    SourceRegressed, add_conn_args, advisory_lock, advisory_unlock,
    assert_source_not_regressed, conn_from_args,
)

TABLE = {'min15': 'bar_15m', 'min30': 'bar_30m', 'min60': 'bar_60m'}
RATIO_TH = 0.40   # 指数连移动 < 主连的 40% → 视为换月断层
LOCK_KEY = "adjust_bars"


def detect_rollover(g: pd.DataFrame, idx_close: np.ndarray):
    """888 OHLC(g, 升序) + 对齐的8888收盘(idx_close, 同长)。返回 (cc, roll)。"""
    cc = g['close'].diff().to_numpy(dtype=float)
    cc_idx = pd.Series(idx_close).diff().to_numpy(dtype=float)
    rng = (g['high'] - g['low']).to_numpy(dtype=float)
    rp = np.empty_like(rng)
    rp[0] = 0.0
    rp[1:] = rng[:-1]
    is_large = np.abs(cc) > (rng + rp + 1e-6)
    roll = is_large & (np.abs(cc_idx) < RATIO_TH * np.abs(cc))
    return cc, roll


def forward_adjust(g: pd.DataFrame, cc: np.ndarray, roll: np.ndarray):
    """前复权：锚定最新段，把每个换月断层平移到其之前所有 bar。返回 (g2, cum)。"""
    n = len(g)
    cum = np.zeros(n)
    run = 0.0
    for i in range(n - 1, -1, -1):
        cum[i] = run                       # 先取累计（跳空 bar 也带累计量）
        if roll[i]:
            run += cc[i]
    out = g.copy()
    for c in ('open', 'high', 'low', 'close'):
        out[c] = g[c].to_numpy(dtype=float) + cum
    return out, cum


def count_rollover_after(cur, freq, table, sym):
    """复权后验证：该品种 cont_adj 里残留的「换月型」大跳空应≈0。"""
    cur.execute("SELECT trade_datetime,open,high,low,close FROM fut_kline "
                "WHERE freq=%s AND kind='cont_adj' AND symbol=%s ORDER BY trade_datetime",
                (freq, sym))
    d = pd.DataFrame(cur.fetchall(),
                     columns=['trade_datetime', 'open', 'high', 'low', 'close'])
    if len(d) < 5:
        return -1
    for c in ('open', 'high', 'low', 'close'):
        d[c] = d[c].astype(float)
    s8888 = sym[:-3] + '8888'
    cur.execute(f"SELECT bucket AS trade_datetime,close FROM {table} "
                f"WHERE symbol=%s ORDER BY bucket", (s8888,))
    di = pd.DataFrame(cur.fetchall(), columns=['trade_datetime', 'close'])
    di['close'] = di['close'].astype(float)
    di['trade_datetime'] = pd.to_datetime(di['trade_datetime'])
    mm = d.merge(di, on='trade_datetime', how='left')
    cc = mm['close_x'].diff().to_numpy(dtype=float)
    cc_idx = mm['close_y'].diff().to_numpy(dtype=float)
    rng = (mm['high'] - mm['low']).to_numpy(dtype=float)
    rp = np.empty_like(rng)
    rp[0] = 0.0
    rp[1:] = rng[:-1]
    is_large = np.abs(cc) > (rng + rp + 1e-6)
    roll = is_large & (np.abs(cc_idx) < RATIO_TH * np.abs(cc))
    return int(roll.sum())


def process(freq, verify=True, limit=None, conn=None):
    table = TABLE[freq]
    c = psycopg2.connect(**conn)
    cur = c.cursor()

    # —— 串行锁：同一时刻只允许一个复权作业（避免与 bars 合成撞车）——
    if not advisory_lock(cur, f"{LOCK_KEY}:{freq}"):
        print(f'[{freq}] 另一个复权作业正在运行，本次跳过', flush=True)
        c.close()
        return 0
    try:
        # —— 源尾部新鲜度校验：防「上游尾删重建未完成」导致残缺数据 ——
        try:
            src_max, dst_max = assert_source_not_regressed(
                cur,
                f"SELECT max(bucket) FROM {table} WHERE symbol ~ '[A-Za-z]888$'",
                "SELECT max(trade_datetime) FROM fut_kline "
                "WHERE freq=%s AND kind='cont_adj'",
                label=f"[{freq}]",
            )
            print(f'[{freq}] 源尾部={src_max} 目标已有尾部={dst_max}', flush=True)
        except SourceRegressed as e:
            print(f'[{freq}] 中止：{e}', flush=True)
            return 0

        cur.execute(f"SELECT DISTINCT symbol FROM {table} WHERE symbol ~ '[A-Za-z]888$'")
        syms = [r[0] for r in cur.fetchall()]
        print(f'[{freq}] {len(syms)} 个 888 序列来自 {table}', flush=True)

        # 幂等清理：仅删这些 symbol 的旧 cont_adj
        cur.execute("DELETE FROM fut_kline WHERE freq=%s AND kind='cont_adj' AND symbol=ANY(%s)",
                    (freq, syms))
        c.commit()
        print(f'  已清理旧 cont_adj {cur.rowcount} 行', flush=True)

        total = 0
        t0 = time.time()
        done = []
        for si, sym in enumerate(syms):
            if limit and si >= limit:
                break
            done.append(sym)
            s8888 = sym[:-3] + '8888'
            cur.execute(f"SELECT symbol,bucket,open,high,low,close,volume,open_interest "
                        f"FROM {table} WHERE symbol=%s ORDER BY bucket", (sym,))
            rows = cur.fetchall()
            if not rows:
                continue
            g = pd.DataFrame(rows, columns=['symbol', 'bucket', 'open', 'high', 'low',
                                            'close', 'volume', 'oi'])
            for col in ('open', 'high', 'low', 'close'):
                g[col] = g[col].astype(float)
            g = g.sort_values('bucket').reset_index(drop=True)
            # 8888 收盘（对齐）
            cur.execute(f"SELECT bucket,close FROM {table} WHERE symbol=%s ORDER BY bucket",
                        (s8888,))
            di = pd.DataFrame(cur.fetchall(), columns=['bucket', 'close'])
            di['close'] = di['close'].astype(float)
            di['bucket'] = pd.to_datetime(di['bucket'])
            m = g[['bucket']].merge(di, on='bucket', how='left')
            idx_close = m['close'].to_numpy(dtype=float)
            if np.isnan(idx_close).all():
                # 无 8888 参考 → 退化为不平移（保安全）
                idx_close = g['close'].to_numpy(dtype=float)
            cc, roll = detect_rollover(g, idx_close)
            n_roll = int(roll.sum())
            g2, _ = forward_adjust(g, cc, roll)
            # COPY 批量写入（超表原生分块，比 execute_values 快 10~50x）
            buf = io.StringIO()
            for r in g2.itertuples():
                o = 0 if r.open is None else float(r.open)
                h = 0 if r.high is None else float(r.high)
                lo = 0 if r.low is None else float(r.low)
                cl = 0 if r.close is None else float(r.close)
                v = 0 if r.volume is None else int(r.volume)
                oi = 0 if r.oi is None else int(r.oi)
                buf.write("\t".join([freq, "cont_adj", sym, str(r.bucket),
                                     f"{o:.4f}", f"{h:.4f}", f"{lo:.4f}", f"{cl:.4f}",
                                     str(v), str(oi), "1"]) + "\n")
            buf.seek(0)
            cur.copy_expert(
                "COPY fut_kline(freq,kind,symbol,trade_datetime,open,high,low,close,"
                "volume,oi,adj) FROM STDIN", buf)
            total += g2.shape[0]
            c.commit()
            if si % 5 == 0:
                print(f'  [{freq}] {si}/{len(syms)} {sym} roll={n_roll} rows={g2.shape[0]} '
                      f'cum={total} t={round(time.time()-t0,0)}s', flush=True)
        c.commit()

        if verify and done:
            print(f'\n=== [{freq}] 验证：复权后残留「换月型」大跳空应≈0 '
                  f'(市场跳空保留) ===', flush=True)
            for sym in done[:5]:
                n = count_rollover_after(cur, freq, table, sym)
                print(f'  {sym}: 复权后换月型大跳空={n}', flush=True)
        print(f'[{freq}] 完成：写入 {total} 行 cont_adj', flush=True)
        return total
    finally:
        advisory_unlock(cur, f"{LOCK_KEY}:{freq}")
        c.close()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--freq', required=True, choices=['min15', 'min30', 'min60'])
    ap.add_argument('--limit', type=int, default=None, help='仅前 N 个品种(调试)')
    ap.add_argument('--no-verify', action='store_true', help='跳过复权后校验')
    add_conn_args(ap)
    a = ap.parse_args()
    process(a.freq, verify=not a.no_verify, limit=a.limit, conn=conn_from_args(a))
