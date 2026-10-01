# -*- coding: utf-8 -*-
"""1 分钟原始数据(minute_bar, 888 主连)加法平移前复权 → minute_bar_adj。

生产脚本（原 `runtime/_adjust_minute.py`，因 .dockerignore 排除 runtime 导致云端
每天静默失败，2026-09-29 迁入 scripts/ 并改造连接为环境变量驱动）。

换月检测**不直接在 1 分钟序列上跑**（1 分钟 bar 振幅极小，振幅门+比率门会被
主连/指数连基差噪声触发，实测 A888 在 1m 检出 200 处 vs 15m 真值 32 处，
每个真实换月事件会拖出 ~6 根连续误检 bar，误平移会抹掉真实行情）。
正确做法：**用已验证的 15 分钟双门检测**（scripts/adjust_bars.py 同款，
实测 84 品种真换月 2,802 处、复权后残留=0）定位换月 15m bucket，
再在 1 分钟序列上把每个 bucket 窗口 [bucket, bucket+15min) 内
|cc888| 最大且过振幅门的 bar 定为换月 bar。跨周期换月时刻完全一致，
回测混用 15m/1m 时口径统一。

前复权（锚定最新段）：cum[i] = Σ_{k>i, roll[k]} cc888[k]；跨换月 bar 收盘差→0。

模式
----
  --full            全量重算（一次性历史回填，云端 57M 行，约 4~5 小时）
  --since <ISO时间>  增量（夜间调度）：该品种 since 后出现过换月 bar → 全量重算；
                    否则只追加 since 之后新 bar（无换月时 cum=0，复权价=原价）。
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
from adjust_bars import detect_rollover, forward_adjust, RATIO_TH  # noqa: E402,F401
from pgconn import (  # noqa: E402
    SourceRegressed, add_conn_args, advisory_lock, advisory_unlock,
    assert_source_not_regressed, conn_from_args,
)

LOCK_KEY = "adjust_minute"


def _norm_since(since):
    """增量起点统一为带时区的 Timestamp。

    调度端传 naive ISO 串（datetime.now()，容器 TZ=Asia/Shanghai），而
    minute_bar.ts 是 timestamptz（aware）——直接比较会抛
    "can't subtract offset-naive and offset-aware datetimes"，且被 per-symbol
    try/except 吞掉：实测 2026-09-25 起云端每夜 04:30 静默失败、写入 0 行。
    naive 一律按上海时区本地化；aware 原样返回。
    """
    ts = pd.Timestamp(since)
    if ts.tzinfo is None:
        ts = ts.tz_localize('Asia/Shanghai')
    return ts


def load_15m_roll_buckets(cur, sym):
    """复用 15m 双门检测，返回该品种换月 15m bucket(Timestamp)列表。"""
    s8888 = sym[:-3] + '8888'
    cur.execute("SELECT bucket,open,high,low,close FROM bar_15m "
                "WHERE symbol=%s ORDER BY bucket", (sym,))
    rows = cur.fetchall()
    if len(rows) < 20:
        return []
    g = pd.DataFrame(rows, columns=['bucket', 'open', 'high', 'low', 'close'])
    for col in ('open', 'high', 'low', 'close'):
        g[col] = g[col].astype(float)
    cur.execute("SELECT bucket,close FROM bar_15m WHERE symbol=%s ORDER BY bucket", (s8888,))
    di = pd.DataFrame(cur.fetchall(), columns=['bucket', 'close'])
    if len(di) < 20:
        return []
    di['close'] = di['close'].astype(float)
    di['bucket'] = pd.to_datetime(di['bucket'])
    m = g[['bucket']].merge(di, on='bucket', how='left')
    idx = m['close'].to_numpy(dtype=float)
    if np.isnan(idx).all():
        return []
    cc, roll = detect_rollover(g, idx)
    return list(pd.to_datetime(g['bucket'])[roll])


def build_minute_roll_mask(g, roll_buckets):
    """在 1min 序列上定位换月 bar：每个 15m bucket 窗口内取 |cc| 最大且过振幅门的 bar。"""
    ts = pd.to_datetime(g['ts'])
    cc = g['close'].diff().to_numpy(dtype=float)
    rng = (g['high'] - g['low']).to_numpy(dtype=float)
    rp = np.empty_like(rng)
    rp[0] = 0.0
    rp[1:] = rng[:-1]
    amp_ok = np.abs(cc) > (rng + rp + 1e-6)
    roll = np.zeros(len(g), dtype=bool)
    for b in roll_buckets:
        w = ((ts >= b) & (ts < b + pd.Timedelta(minutes=15))).to_numpy()
        cand = np.where(w & amp_ok)[0]
        if len(cand):
            j = cand[int(np.argmax(np.abs(cc[cand])))]
            roll[j] = True
    return cc, roll


def load_symbol(cur, sym):
    cur.execute("SELECT symbol,ts,open,high,low,close,volume,open_interest "
                "FROM minute_bar WHERE symbol=%s ORDER BY ts", (sym,))
    rows = cur.fetchall()
    if not rows:
        return None
    g = pd.DataFrame(rows, columns=['symbol', 'ts', 'open', 'high', 'low',
                                    'close', 'volume', 'oi'])
    for col in ('open', 'high', 'low', 'close'):
        g[col] = g[col].astype(float)
    return g.sort_values('ts').reset_index(drop=True)


def process_symbol(cur, sym, since=None):
    """返回 (n_written, n_roll, mode)。"""
    g = load_symbol(cur, sym)
    if g is None or len(g) < 20:
        return 0, 0, 'skip'
    roll_buckets = load_15m_roll_buckets(cur, sym)
    cc, roll = build_minute_roll_mask(g, roll_buckets)
    n_roll = int(roll.sum())

    full = True
    since_ts = None
    if since is not None:
        since_ts = _norm_since(since)
        ts_series = pd.to_datetime(g['ts'])  # DB timestamptz → aware
        full = bool((roll & (ts_series >= since_ts).to_numpy()).any())

    if full:
        g2, _ = forward_adjust(g, cc, roll)
        mode = f'full(roll={n_roll})'
    else:
        mask = (pd.to_datetime(g['ts']) >= since_ts).to_numpy()
        g2 = g[mask].reset_index(drop=True)
        # since 后无换月 → 新 bar cum=0，复权价=原价
        g2, _ = forward_adjust(g2, np.zeros(len(g2)), np.zeros(len(g2), dtype=bool))
        mode = 'append'

    if len(g2) == 0:
        return 0, n_roll, mode
    if full:
        cur.execute("DELETE FROM minute_bar_adj WHERE symbol=%s", (sym,))
    else:
        cur.execute("DELETE FROM minute_bar_adj WHERE symbol=%s AND ts >= %s",
                    (sym, since_ts))
    buf = io.StringIO()
    for r in g2.itertuples():
        o = 0 if r.open is None else float(r.open)
        h = 0 if r.high is None else float(r.high)
        lo = 0 if r.low is None else float(r.low)
        cl = 0 if r.close is None else float(r.close)
        v = 0 if r.volume is None else int(r.volume)
        oi = 0 if r.oi is None else float(r.oi)
        buf.write("\t".join([sym, str(r.ts), f"{o:.4f}", f"{h:.4f}",
                             f"{lo:.4f}", f"{cl:.4f}", str(v), f"{oi:.4f}"]) + "\n")
    buf.seek(0)
    cur.copy_expert(
        "COPY minute_bar_adj(symbol,ts,open,high,low,close,volume,open_interest) FROM STDIN",
        buf)
    return len(g2), n_roll, mode


def run(full=True, since=None, limit=None, conn=None):
    c = psycopg2.connect(**conn)
    cur = c.cursor()

    if not advisory_lock(cur, LOCK_KEY):
        print('[minute] 另一个 1 分钟复权作业正在运行，本次跳过', flush=True)
        c.close()
        return 0
    try:
        # 源尾部校验：minute_bar 不得早于 minute_bar_adj 已有尾部（防上游尾删竞态）
        try:
            src_max, dst_max = assert_source_not_regressed(
                cur,
                "SELECT max(ts) FROM minute_bar WHERE symbol ~ '[A-Za-z]888$'",
                "SELECT max(ts) FROM minute_bar_adj",
                label="[minute]",
            )
            print(f'[minute] 源尾部={src_max} 目标已有尾部={dst_max}', flush=True)
        except SourceRegressed as e:
            print(f'[minute] 中止：{e}', flush=True)
            return 0

        cur.execute("SELECT DISTINCT symbol FROM minute_bar WHERE symbol ~ '[A-Za-z]888$'")
        syms = [r[0] for r in cur.fetchall()]
        tag = 'FULL(15m窗口驱动)' if full else f'SINCE {since}'
        print(f'[minute] {len(syms)} 个 888 序列 | 模式={tag}', flush=True)
        total = 0
        t0 = time.time()
        for si, sym in enumerate(syms):
            if limit and si >= limit:
                break
            try:
                n, nr, mode = process_symbol(cur, sym, since=None if full else since)
                c.commit()
                total += n
            except Exception as e:  # noqa: BLE001
                c.rollback()
                print(f'  !! {sym} 失败: {e}', flush=True)
                continue
            if si % 5 == 0:
                print(f'  [{si}/{len(syms)}] {sym} roll={nr} rows={n} mode={mode} '
                      f'cum={total} t={round(time.time()-t0,0)}s', flush=True)
        c.commit()
        print(f'[minute] 完成：写入 {total} 行 t={round(time.time()-t0,0)}s', flush=True)
        return total
    finally:
        advisory_unlock(cur, LOCK_KEY)
        c.close()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--full', action='store_true', help='全量重算(默认)')
    ap.add_argument('--since', default=None, help='增量模式：ISO 时间')
    ap.add_argument('--limit', type=int, default=None, help='仅前 N 个品种(调试)')
    add_conn_args(ap)
    a = ap.parse_args()
    run(full=a.full or not a.since, since=a.since, limit=a.limit, conn=conn_from_args(a))
