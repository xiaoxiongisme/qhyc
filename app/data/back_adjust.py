# -*- coding: utf-8 -*-
"""等差后复权取数助手：给未复权 OHLC 套上 roll_segment 的累积偏移。

用法
----
    from app.data.back_adjust import apply_back_adjust

    df = read_raw(session, 'bar_15m', 'RB888')        # 未复权
    df2 = apply_back_adjust(df, 'RB888', 'min15', session)
    #   open/high/low/close 已是后复权价；df2['adj_offset'] 保留偏移量便于反解

为什么单独一个模块
------------------
BarStore 目前被上层引用极少（半迁移状态，上层多直接写 SQL），
与其改造 BarStore，不如提供一个**可直接调用的最小函数**，
让任何取数路径（含裸 SQL）都能一行套用复权。

口径要点
--------
* 后复权（锚定最早段）：最早段 cum_offset = 0，历史价即真实价。
* 使跨换月的**点数差**归零 —— 期货损益＝点数差×乘数，这是必须的。
* **不要用等比**：实测 84 品种，等比会让 87.8~90.2% 品种的 ATR 失真，
  直接摧毁 2×ATR 止损 / 0.5×ATR 保本。

负价提示
--------
即使后复权，长期 contango 品种的后期段仍可能把价格推低（实测 RU/FB/JD 三品种）。
因此本模块提供 `check_negative` 做上线前体检；回测侧应**只用点数、不做除法**。
"""
from __future__ import annotations

import pandas as pd
from sqlalchemy import text

#: freq → 表名（与 scripts/build_roll_segments.TABLE 保持一致）
TABLE = {'min5': 'bar_5m', 'min15': 'bar_15m',
         'min30': 'bar_30m', 'min60': 'bar_60m'}

PRICE_COLS = ('open', 'high', 'low', 'close')


def load_segments(session, symbol: str, freq: str) -> pd.DataFrame:
    """取该 symbol+freq 的分段表（seg_start 升序）。"""
    rows = session.execute(
        text("SELECT seg_no, seg_start, seg_end, roll_ts, roll_delta, cum_offset, "
             "price_shift, n_bars "
             "FROM roll_segment WHERE symbol=:s AND freq=:f ORDER BY seg_no"),
        {"s": symbol, "f": freq}).fetchall()
    return pd.DataFrame(rows, columns=['seg_no', 'seg_start', 'seg_end', 'roll_ts',
                                       'roll_delta', 'cum_offset', 'price_shift',
                                       'n_bars'])


def _level(segs: pd.DataFrame) -> list[float]:
    """每段的总位移 = cum_offset + price_shift。

    `price_shift` 是消除负价用的**整序列常量抬升**，见
    `build_roll_segments.apply_positivity`。不加该选项时该列为 0，这里自动兼容。
    """
    if 'price_shift' not in segs.columns:
        return segs['cum_offset'].astype(float).tolist()
    return (segs['cum_offset'].astype(float)
            + segs['price_shift'].astype(float).fillna(0.0)).tolist()


def map_offset(ts: pd.Series, segs: pd.DataFrame) -> pd.Series:
    """把每个时间戳映射到所属段的 cum_offset（as-of join）。

    段定义 [seg_start, seg_end)；最后一段 seg_end 为空 → 视为无穷大。
    落在所有段之前的时间戳（早于首段）取 0。
    """
    if segs.empty:
        return pd.Series(0.0, index=ts.index)
    starts = pd.to_datetime(segs['seg_start']).tolist()
    # 接入 price_shift 常量抬升（见 apply_positivity）：后复权价 = raw + cum_offset + shift
    # 修复前 map_offset 只用 cum_offset，导致 positivity 抬升形同虚设、长 backwardation 品种仍为负价
    offs = _level(segs)
    idx = pd.to_datetime(ts)
    # searchsorted(side='right') - 1 → 最后一个 seg_start <= ts 的段
    # 注意：Series.searchsorted 返回的就是 ndarray，不能再 .to_numpy()
    pos = pd.Series(starts).searchsorted(idx, side='right') - 1
    pos = pd.Series(pos, index=ts.index).clip(lower=0)
    return pd.Series([offs[i] for i in pos], index=ts.index)


def apply_back_adjust(df: pd.DataFrame, symbol: str, freq: str,
                      session=None, segs: pd.DataFrame | None = None,
                      time_col: str = 'bucket') -> pd.DataFrame:
    """返回套用后复权后的副本；额外写入 `adj_offset` 列便于反解回真实合约价。

    session 与 segs 至少给一个（给 segs 可复用、避免重复查库）。
    """
    out = df.copy()
    if segs is None:
        if session is None:
            raise ValueError('session 与 segs 需至少提供一个')
        segs = load_segments(session, symbol, freq)
    off = map_offset(out[time_col], segs)
    out['adj_offset'] = off.to_numpy()
    for c in PRICE_COLS:
        if c in out.columns:
            out[c] = out[c].astype(float) + out['adj_offset']
    return out


def unadjust_price(adj_price: float, adj_offset: float) -> float:
    """复权价反解回真实合约价：real = adj - offset。下单前必须做这一步。"""
    return adj_price - adj_offset


def check_negative(session, freq: str = 'min15',
                   symbols: list[str] | None = None) -> pd.DataFrame:
    """上线前体检：算出每个品种后复权后的**最低价**，标出会转负的品种。

    这些品种的复权序列禁止做任何除法/百分比/对数收益计算。

    注意：按 freq 选对应的 bar_* 表（5 分钟→bar_5m），不能写死 15m。
    """
    tbl = TABLE.get(freq)
    if tbl is None:
        raise ValueError(f"freq={freq!r} 无对应 bar_* 表，可选 {sorted(TABLE)}")
    sql = (f"SELECT symbol, MIN(m.minp + r.cum_offset) AS min_adj "
           f"FROM roll_segment r JOIN LATERAL ("
           # 判负价必须用 **low**（最低价），不能用 close：实测按 close 会漏判
           # （84 品种里 close 口径只检出 4 个，low 口径才是 7 个）
           f"  SELECT MIN(low) AS minp FROM {tbl} b "
           f"  WHERE b.symbol = r.symbol AND b.bucket >= r.seg_start "
           f"  AND (r.seg_end IS NULL OR b.bucket < r.seg_end)"
           # 剔除 low<=0 的脏 bar（2026-09-30 实测：6 品种共 9 根 Bar 的 low=0，
           # 全部落在 09:00/21:00 时段开盘第一根，是采集源缺陷不是复权造成，
           # 若不过滤会把这些品种误判成"负价"）
           f"  AND b.low > 0"
           f") m ON TRUE WHERE r.freq = :f")
    params: dict = {"f": freq}
    if symbols:
        sql += " AND r.symbol = ANY(:syms)"
        params['syms'] = symbols
    sql += " GROUP BY r.symbol ORDER BY 2"
    rows = session.execute(text(sql), params).fetchall()
    d = pd.DataFrame(rows, columns=['symbol', 'min_adj'])
    d['min_adj'] = d['min_adj'].astype(float)
    d['negative'] = d['min_adj'] <= 0
    d['forbid_pct'] = d['min_adj'] < 0.05 * d['min_adj'].abs().clip(lower=1)
    return d


# ---------------------------------------------------------------------------
# #8 物化落地读取：直接读 l2_adj.bar_*m_ba（已后复权），省去 pull-raw + Python as-of
# ---------------------------------------------------------------------------
BA_TABLE = {'min5': 'bar_5m_ba', 'min15': 'bar_15m_ba',
           'min30': 'bar_30m_ba', 'min60': 'bar_60m_ba'}


def read_back_adjusted(session, symbol, freq, start=None, end=None, with_raw=True):
    """读 #8 物化后复权表 l2_adj.bar_*m_ba。

    返回 DataFrame（按 bucket 升序）：
      bucket, open/high/low/close（后复权）, volume, amount, open_interest, adj_offset
      with_raw=True 时额外带 raw_open/high/low/close（= adj - adj_offset，用于需原始合约价处）。

    相比 apply_back_adjust（拉全量原始 + Python 逐根 as-of），本函数直接命中已物化结果，
    回测/信号取数免去两步开销。与 apply_back_adjust 逐位一致（建表时已验收 max|diff|=0）。
    """
    tbl = BA_TABLE.get(freq)
    if tbl is None:
        raise ValueError(f"freq={freq!r} 无对应 ba 表，可选 {sorted(BA_TABLE)}")
    sql = (f"SELECT bucket, open, high, low, close, volume, amount, "
           f"open_interest, adj_offset FROM l2_adj.{tbl} WHERE symbol=:s")
    p = {"s": symbol}
    if start is not None:
        sql += " AND bucket >= :start"; p["start"] = start
    if end is not None:
        sql += " AND bucket <= :end";   p["end"] = end
    sql += " ORDER BY bucket"
    rows = session.execute(text(sql), p).fetchall()
    df = pd.DataFrame(rows, columns=['bucket', 'open', 'high', 'low', 'close',
                                    'volume', 'amount', 'open_interest', 'adj_offset'])
    if with_raw:
        for c in ('open', 'high', 'low', 'close'):
            df['raw_' + c] = df[c].astype(float) - df['adj_offset'].astype(float)
    return df


def apply_back_adjust_materialized(session, symbol, freq, start=None, end=None):
    """apply_back_adjust 的物化等价版（drop-in）：直接返回后复权 OHLC DataFrame。

    与 apply_back_adjust(df, symbol, freq, segs=...) 的返回列一致
    （bucket, open/high/low/close 后复权, volume, amount, open_interest, adj_offset），
    但数据源是已物化表，不再做 Python as-of。调用方只需把
    `apply_back_adjust(b, sym, freq, segs=segs)` 换成
    `apply_back_adjust_materialized(c, sym, freq)` 即可获得性能收益。
    """
    return read_back_adjusted(session, symbol, freq, start=start, end=end, with_raw=False)


def apply_back_adjust_from_materialized(df, symbol, freq, session):
    """apply_back_adjust 的物化等价 drop-in（**保留 df 原形状**，消除 Python 逐根 as-of）。

    等价地改写 df 的 open/high/low/close 为后复权价并写入 adj_offset，但不再用 Python
    逐根 as-of，而是按 bucket 从已物化表 l2_adj.bar_*m_ba 取算好的后复权值做 left merge。
    输入 df 的索引与其余列（如 dt/d）原样保留 —— 可直接替换如下两行：

        segs = load_segments(session, symbol, freq)
        sig  = apply_back_adjust(df, symbol, freq, segs=segs)

    为：

        sig  = apply_back_adjust_from_materialized(df, symbol, freq, session)

    前置：l2_adj.bar_*m_ba 已用 040 物化（含全部 888 主连历史）。
    与 apply_back_adjust 逐位一致（建表时已验收 max|diff|=0；ba 是 bar_*m(888) 的超集）。
    """
    tbl = BA_TABLE.get(freq)
    if tbl is None:
        raise ValueError(f"freq={freq!r} 无对应 ba 表，可选 {sorted(BA_TABLE)}")
    if 'bucket' not in df.columns:
        raise ValueError("apply_back_adjust_from_materialized: df 需含 'bucket' 列")

    ba = read_back_adjusted(session, symbol, freq, with_raw=False)
    if ba.empty:
        raise ValueError(f"{symbol}/{freq} 在 l2_adj.{tbl} 无数据，无法物化复权")

    # 以 UTC 归一化做 merge key，规避 Asia/Shanghai vs UTC 表示差异
    def _utc(s):
        s = pd.to_datetime(s)
        return s.dt.tz_convert('UTC') if s.dt.tz is not None else s.dt.tz_localize('UTC')
    ba_k = _utc(ba['bucket'])
    sub = ba.set_index(ba_k)[['open', 'high', 'low', 'close', 'adj_offset']]
    bk = _utc(df['bucket']).rename('_bk')

    out = df.copy()
    out['_bk'] = bk  # 保持 tz-aware Series，避免 .values 丢失时区
    merged = out.merge(sub, left_on='_bk', right_index=True, how='left',
                      suffixes=('', '_ba'))

    miss = int(merged['adj_offset'].isna().sum())
    if miss:
        raise ValueError(
            f"{symbol}/{freq}: {miss} 根 bar 在 l2_adj.{tbl} 缺失 "
            f"（ba 覆盖不完整，请先跑 040 build）")
    for c in ('open', 'high', 'low', 'close'):
        out[c] = merged[c + '_ba'].to_numpy()
    out['adj_offset'] = merged['adj_offset'].to_numpy()
    out.drop(columns=['_bk'], inplace=True)
    return out
