# -*- coding: utf-8 -*-
"""等差后复权：构建 roll_segment 分段表（scripts/ 下，环境变量驱动连接）。

设计要点
--------
1) **换月检测统一用 15m 锚点**，再映射到目标 freq。
   实测（2026-09-30）：
     - 1m 直接检测会崩坏（A888 检出 200 处 vs 15m 真值 32 处）
     - 5m 直接检测可用，但 18.2% 落在 15m 换月窗口外（IF888 尤甚）
   统一锚点 ⇒ 各周期换月时刻完全一致，口径统一且避免低周期误检。

2) **后复权（锚定最早段）**：cum_offset(k) = -Σ_{j<=k} roll_delta(j)
   ⇒ 最早段偏移恒为 0（历史价 = 真实价），新换月只 INSERT 一段，**历史永不重算**。

3) 不同 freq 的 roll_delta 取该 freq 自身 bar 上的 cc，
   但换月**位置**由 15m 锚定，保证映射到同一个市场事件。

用法
----
  # 本地全量（15m/30m/60m；bar_5m 本地为空会自动 skip）
  python scripts/build_roll_segments.py --freqs min15,min30,min60
  # 试算不落库
  python scripts/build_roll_segments.py --freqs min15 --dry-run
  # 云端跑 5m
  python scripts/build_roll_segments.py --freqs min5 --symbol RB888
"""
import argparse
import os
import sys
import time

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pgconn import add_conn_args, conn_from_args  # noqa: E402

RATIO_TH = 0.40          # 指数连移动 < 主连的 40% → 视为换月断层（与 adjust_bars.py 一致）
ANCHOR_FREQ = 'min15'    # 换月检测锚点周期
ANCHOR_MIN = 15          # 锚点周期分钟数
TABLE = {'min5': 'bar_5m', 'min15': 'bar_15m', 'min30': 'bar_30m', 'min60': 'bar_60m'}
# BETA / AMP_WIN：原振幅地板门控参数，2026-09-30 经只读四门对比验证无效/更差，
#     已随 B/C/BC 三门一并移除，固定使用 A 门。保留仅为历史可追溯，不再被引用。


def detect_anchor_roll(g15, idx15):
    """15m 换月检测（双门 = 验证过的唯一正确门控）。

    返回 (换月事件 [(ts, gap)], cc, roll_mask)。

    ★ gap = open[i] - close[i-1]，而不是 cc = close[i] - close[i-1]
    --------------------------------------------------------------
    2026-09-30 实测：换月必然落在 bar 边界（新合约的第一根 bar 起点），所以
        cc[i] = (跳空) + (该 bar 自身涨跌)，而 gap[i] ≈ 纯跳空。
    用 cc 当 delta 会把该 bar 自身的真实涨跌一并抹成 0，且高阶周期 bar 更长、
    被抹掉的自然更多 ⇒ 各周期累计偏移发散（实测 SN888 15m 与 60m 差 1560 点）。

    双门判定（A 门，2026-09-30 经 252/252 验收 + 只读四门对比验证为唯一正确）：
        is_large = |cc| > (本bar振幅 + 前bar振幅)
        ratio    = |Δ指数连| < RATIO_TH · |cc|   # 换月时主连跳空、指数连连续 ⇒ Δ指数远小于主连
        roll     = is_large & ratio

    ⚠️ B/C/BC 三门（振幅地板 / 指数新鲜度 / 组合）于 2026-09-30 经只读四门对比
    验证无效或更差：fresh（指数新鲜度）对这些品种恒为真、无判别力；振幅地板与
    gap 法在零振幅品种上爆到几十~几百次/年。故固定 A 门，移除其余三门。
    """
    cc = g15['close'].diff().to_numpy(dtype=float)
    cc_idx = pd.Series(idx15).diff().to_numpy(dtype=float)
    rng = (g15['high'] - g15['low']).to_numpy(dtype=float)
    rp = np.empty_like(rng)
    rp[0] = 0.0
    rp[1:] = rng[:-1]
    amp = rng + rp                                       # 本bar振幅 + 前bar振幅
    is_large = np.abs(cc) > (amp + 1e-6)
    ratio = np.abs(cc_idx) < RATIO_TH * np.abs(cc)
    roll = is_large & ratio

    op = g15['open'].to_numpy(dtype=float)
    cl = g15['close'].to_numpy(dtype=float)
    gap = np.full(len(g15), np.nan)
    gap[1:] = op[1:] - cl[:-1]          # open[i] - close[i-1]
    ts = pd.to_datetime(g15['bucket'])
    events = [(ts[i], float(gap[i])) for i in np.where(roll)[0]]
    return events, cc, roll


def load_symbol(cur, table, sym):
    cur.execute(f"SELECT symbol,bucket,open,high,low,close FROM {table} "
                f"WHERE symbol=%s ORDER BY bucket", (sym,))
    rows = cur.fetchall()
    if not rows:
        return None
    g = pd.DataFrame(rows, columns=['symbol', 'bucket', 'open', 'high', 'low', 'close'])
    for c in ('open', 'high', 'low', 'close'):
        g[c] = g[c].astype(float)
    g['bucket'] = pd.to_datetime(g['bucket'])
    return g.reset_index(drop=True)


def load_index_close(cur, table, sym, g):
    """同品种 8888 指数连收盘，按 bucket 左对齐到 g。"""
    s8888 = sym[:-3] + '8888'
    cur.execute(f"SELECT bucket,close FROM {table} WHERE symbol=%s ORDER BY bucket", (s8888,))
    rows = cur.fetchall()
    if not rows:
        return np.full(len(g), np.nan)
    d = pd.DataFrame(rows, columns=['bucket', 'close'])
    d['close'] = d['close'].astype(float)
    d['bucket'] = pd.to_datetime(d['bucket'])
    return g[['bucket']].merge(d, on='bucket', how='left')['close'].to_numpy(dtype=float)


FREQ_MIN = {'min5': 5, 'min15': 15, 'min30': 30, 'min60': 60}


def map_roll_to_freq(g, events, freq_min):
    """把 15m 换月事件映射到本 freq：定位「包含该时刻」的那根 bar。

    返回 (cc, roll_mask, delta_arr)。
    **`delta_arr` 用的是锚点算出的 gap，不是本 freq 的 cc** —— 见 detect_anchor_roll
    注释：同一笔换月事件在所有周期上减去同一个常量，才能保证各周期价位一致。

    为什么不能用「[锚点, 锚点+ANCHOR_MIN) 窗口内取 bar」
    ------------------------------------------------
    2026-09-30 实测 BUG：各表 bucket 对齐方式不同——
        bar_15m :00/:15/:30/:45，bar_30m :00/:30，bar_60m 整点
    若锚点落在 10:15，窗口 [10:15,10:30) 里**一根 30m/60m bar 都没有**
    （10:00 那根早于窗口起点，10:30 那根不早于窗口终点）→ 该换月被静默丢弃。
    表现：min30/min60 段数比 min15 少 17%（2395 vs 2886），复权不完整。

    正确的做法是「包含关系」：取最后一个 ts <= 锚点时刻的 bar j，
    并要求锚点落在该 bar 的覆盖区间 [ts[j], 下一根) 内。这与 bucket 对齐无关。
    """
    tsv = (pd.to_datetime(g['bucket']).dt.tz_localize(None)
           .to_numpy(dtype='datetime64[ns]'))
    cc = g['close'].diff().to_numpy(dtype=float)
    cc = np.nan_to_num(cc, nan=0.0)      # 首根 bar 的 diff 为 NaN
    roll = np.zeros(len(g), dtype=bool)
    delta_ev = np.zeros(len(g), dtype=float)   # 落在该 bar 上的换月跳空（可叠加）
    drop = 0
    merge = 0
    fallback = np.timedelta64(int(freq_min), 'm')
    for b, gap_ev in events:
        bn = np.datetime64(pd.Timestamp(b).tz_localize(None), 'ns')
        j = int(np.searchsorted(tsv, bn, side='right')) - 1   # 最后一个 ts <= b
        if j < 0:
            drop += 1
            continue
        nxt = tsv[j + 1] if j + 1 < len(tsv) else tsv[j] + fallback
        if bn >= nxt:
            drop += 1                       # 锚点在该 bar 覆盖区间之外（停牌/无数据）
            continue
        if roll[j]:
            merge += 1                      # 两个换月落在同一根高阶 bar 内
        roll[j] = True
        delta_ev[j] += float(gap_ev)        # 用**锚点**的 gap，非本 freq 的 cc
    if drop or merge:
        print(f'    [warn] 锚点映射：丢弃 {drop}（落在 bar 覆盖外）、合并 {merge}（同 bar 多次）')
    return cc, roll, delta_ev


def build_segments(g, roll, delta_ev, positivity=False):
    """按换月点切段，算后复权累积偏移。

    `delta_ev`：**落在每个换月 bar 上的跳空点数**（由锚点 gap 算得，跨周期同值）。
    cum_offset(k) = -Σ_{j<=k} delta_j  ⇒ 跨换月的点数差被扣掉跳空、保留该 bar 自身涨跌。
    """
    ts = pd.to_datetime(g['bucket']).tolist()
    idx = np.where(roll)[0].tolist()
    bounds = [0] + idx + [len(g)]
    segs = []
    cum = 0.0
    for k in range(len(bounds) - 1):
        s, e = bounds[k], bounds[k + 1]
        delta = float(delta_ev[s]) if k > 0 else 0.0
        cum += delta
        segs.append(dict(
            seg_no=k,
            seg_start=ts[s].to_pydatetime(),
            seg_end=ts[e].to_pydatetime() if e < len(ts) else None,
            roll_ts=ts[s].to_pydatetime() if k > 0 else None,
            roll_delta=round(delta, 4),
            cum_offset=round(-cum, 4),      # 后复权：-Σ_{j<=k} delta_j
            n_bars=int(e - s),
        ))
    return segs


def apply_positivity(g, segs):
    """若后复权后最低价 <= 0，给整条序列加一个**常量抬升** price_shift。

    为什么能用：期货损益 = 点数差 × 乘数，**加减一个常量不改变任何点数差分**，
    ATR / 点数差 / tick 距离全部保持不变，只是绝对价位整体平移。
    → 拿它换「全程无负价」，让百分比/收益率类计算也安全。

    代价：失去「最早段价格 = 真实成交价」这一点（其实只剩下绝对水平的意义）。
    
    2026-09-30 实测：即使后复权，仍有 CJ/RU/FB/JD 四个长期 backwardation 品种
    的最低价为负（CJ888 low 达 −5690），加常量可彻底消除。
    """
    lowv = g['low'].to_numpy(dtype=float)
    ts = pd.to_datetime(g['bucket']).tolist()
    starts = [s['seg_start'] for s in segs]
    pos = np.array(pd.Series(starts).searchsorted(pd.Series(ts), side='right')) - 1
    off = np.array([segs[i]['cum_offset'] for i in np.clip(pos, 0, len(segs) - 1)])
    mn = float(np.nanmin(lowv + off))
    shift = 0.0
    if mn <= 0:
        # 抬到「最低价为 +1 个最小价位」以上，再取 100 的整数倍便于人工核对
        rng = float(np.nanmax(lowv) - np.nanmin(lowv))
        shift = float(np.ceil((-mn + 0.01 * max(rng, 1.0)) / 100.0) * 100.0)
    for s in segs:
        s['price_shift'] = shift
    return shift

def ensure_table(cur):
    cur.execute("""
        CREATE TABLE IF NOT EXISTS roll_segment (
            symbol      TEXT           NOT NULL,
            freq        TEXT           NOT NULL,
            seg_no      INT            NOT NULL,
            seg_start   TIMESTAMPTZ    NOT NULL,
            seg_end     TIMESTAMPTZ,
            roll_ts     TIMESTAMPTZ,
            roll_delta  NUMERIC(20,4)  NOT NULL DEFAULT 0,
            cum_offset  NUMERIC(20,4)  NOT NULL DEFAULT 0,
            n_bars      INT,
            src_freq    TEXT           NOT NULL DEFAULT 'min15',
            updated_at  TIMESTAMPTZ    NOT NULL DEFAULT now(),
            PRIMARY KEY (symbol, freq, seg_no)
        )""")
    cur.execute("CREATE INDEX IF NOT EXISTS ix_roll_segment_lookup "
                "ON roll_segment (symbol, freq, seg_start)")
    # 幂等补列：已存在的老表（本地就是脚本建的，云端走 migration）也能加上
    cur.execute("ALTER TABLE roll_segment ADD COLUMN IF NOT EXISTS "
                "price_shift NUMERIC(20,4) NOT NULL DEFAULT 0")


def upsert_segments(cur, sym, freq, segs):
    cur.execute("DELETE FROM roll_segment WHERE symbol=%s AND freq=%s", (sym, freq))
    if not segs:
        return 0
    rows = [(sym, freq, s['seg_no'], s['seg_start'], s['seg_end'], s['roll_ts'],
             s['roll_delta'], s['cum_offset'], s.get('price_shift', 0.0),
             s['n_bars'], ANCHOR_FREQ) for s in segs]
    psycopg2.extras.execute_values(
        cur,
        "INSERT INTO roll_segment (symbol,freq,seg_no,seg_start,seg_end,roll_ts,"
        "roll_delta,cum_offset,price_shift,n_bars,src_freq) VALUES %s", rows)
    return len(rows)



def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--freqs', default='min15,min30,min60',
                    help='目标周期，逗号分隔，或 all。可用 min5,min15,min30,min60')
    ap.add_argument('--limit', type=int, default=None, help='仅前 N 个品种')
    ap.add_argument('--symbol', default=None, help='仅处理指定品种')
    ap.add_argument('--dry-run', action='store_true', help='试算不落库')
    ap.add_argument('--positivity', action='store_true',
                    help='给整条序列加常量抬升，保证后复权价恒 > 0（不改变任何点数差分）')
    add_conn_args(ap)
    a = ap.parse_args()

    freqs = (['min5', 'min15', 'min30', 'min60']
             if a.freqs.strip().lower() == 'all'
             else [f.strip() for f in a.freqs.split(',') if f.strip()])
    bad = [f for f in freqs if f not in TABLE]
    if bad:
        print(f'[abort] 未知周期 {bad}，可用 {sorted(TABLE)}')
        return 2

    c = psycopg2.connect(**conn_from_args(a))
    cur = c.cursor()
    if not a.dry_run:
        ensure_table(cur)
        c.commit()

    if a.symbol:
        syms = [a.symbol]
    else:
        cur.execute("SELECT symbol FROM bar_15m WHERE symbol ~ '[A-Za-z]888$' "
                    "GROUP BY 1 ORDER BY 1")
        syms = [r[0] for r in cur.fetchall()]
        if a.limit:
            syms = syms[:a.limit]

    mode = 'DRY-RUN' if a.dry_run else '写库'
    print(f'[build] 品种 {len(syms)} × 周期 {freqs} | 锚点 {ANCHOR_FREQ} | {mode}')

    t0 = time.time()
    report = {f: {'sym': 0, 'seg': 0, 'skip': 0} for f in freqs}
    quality = []

    for si, sym in enumerate(syms):
        g15 = load_symbol(cur, TABLE[ANCHOR_FREQ], sym)
        if g15 is None or len(g15) < 100:
            for f in freqs:
                report[f]['skip'] += 1
            continue
        idx15 = load_index_close(cur, TABLE[ANCHOR_FREQ], sym, g15)
        if np.isnan(idx15).all():
            print(f'  !! {sym} 无 8888 指数连，跳过')
            for f in freqs:
                report[f]['skip'] += 1
            continue
        events, cc15, roll15 = detect_anchor_roll(g15, idx15)

        for freq in freqs:
            if freq == ANCHOR_FREQ:
                g = g15
            else:
                g = load_symbol(cur, TABLE[freq], sym)
                if g is None or len(g) < 100:
                    report[freq]['skip'] += 1
                    continue
            cc, roll, delta_ev = map_roll_to_freq(g, events, FREQ_MIN[freq])
            segs = build_segments(g, roll, delta_ev)
            if a.positivity:
                apply_positivity(g, segs)
            if not a.dry_run:
                upsert_segments(cur, sym, freq, segs)
            report[freq]['sym'] += 1
            report[freq]['seg'] += len(segs)
        if not a.dry_run:
            c.commit()

        b = pd.to_datetime(g15['bucket'])
        span = (b.iloc[-1] - b.iloc[0]).days / 365.25
        quality.append((sym, len(events), len(events) / max(span, 1e-9),
                        float(np.nanmin(g15['close'].to_numpy(dtype=float)))))
        if si % 20 == 0:
            print(f'  [{si}/{len(syms)}] {sym} 15m换月={len(events)} '
                  f'({quality[-1][2]:.1f}/年) t={round(time.time()-t0)}s', flush=True)

    print('\n' + '=' * 92)
    print('【构建结果】')
    print('=' * 92)
    print(f"{'周期':<8}{'处理品种':>10}{'生成段数':>10}{'跳过':>8}{'平均每品种段数':>16}")
    for f in freqs:
        r = report[f]
        print(f"{f:<8}{r['sym']:>10}{r['seg']:>10}{r['skip']:>8}"
              f"{r['seg']/max(r['sym'],1):>16.1f}")

    susp = [q for q in quality if q[2] > 5]
    print('\n' + '=' * 92)
    print('【数据质量】15m 年换月频次异常（正常 2~3 次/年，>5 疑似误检）')
    print('=' * 92)
    print(f"{'品种':<10}{'换月数':>8}{'次/年':>9}{'未复权最低价':>14}{'判定':>12}")
    for sym, n, py, lo in sorted(susp, key=lambda x: -x[2]):
        print(f"{sym:<10}{n:>8}{py:>9.1f}{lo:>14.1f}{'疑似误检':>12}")
    print(f"\n  异常 {len(susp)}/{len(quality)} 品种 | 总耗时 {round(time.time()-t0)}s")
    c.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
