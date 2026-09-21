# -*- coding: utf-8 -*-
"""期货行情 PostgreSQL 后端（替代技能原 db_dolphindb.py）。

接口与 db_dolphindb 对齐：ensure_schema / save_bars / save_adjusted /
load_bars / load_contract_frames / latest_date / list_symbols，
内部统一落到本项目 TimescaleDB 的 fut_kline 超表（freq, kind, symbol, trade_datetime）。

表设计（单一超表，按频率/类型/品种共用）：
  freq  : 'hourly' | 'min15' | 'daily' ...
  kind  : 'continuous'(主连 KQ.m@) | 'contract'(具体合约 EX.CODE) | 'cont_adj'(复权主连)
  symbol: 主连 'KQ.m@CZCE.FG' / 合约 'CZCE.FG609'
  trade_datetime: TIMESTAMPTZ
  open/high/low/close: NUMERIC(20,4)
  volume/oi: BIGINT
  adj : NUMERIC(20,4)  —— 仅 cont_adj 有意义（前复权因子，已复权区=1/True 区）

symbol 口径说明（2026-09-20）
----------------------------
``fut_kline.symbol`` 存的是**数据源原生码**，**刻意不参与**全库 4 位标准码归一：
  · 主连 ``KQ.m@CZCE.FG``（``m`` 大小写敏感）
  · 合约 ``CZCE.FG609`` —— **郑商所仍是 3 位**，天勤按此订阅，改了取数就断
各源原生码 ↔ 标准码（4 位）的对应关系登记在 ``contract_code_map``
（``tqsdk_symbol`` 列），需要跨表 join 时用 ``std_symbol``。
"""
from __future__ import annotations

import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.db import get_engine, get_session_factory
from app.models import FutKline

# 频率代号 -> (天勤 duration 秒, 人类可读名)，与技能 db_dolphindb.FREQS 一致
FREQS = {
    "daily":  (24 * 60 * 60, "日线"),
    "weekly": (7 * 24 * 60 * 60, "周线"),
    "hourly": (60 * 60, "1小时"),
    "h2":     (2 * 3600, "2小时"),
    "h4":     (4 * 3600, "4小时"),
    "min15":  (15 * 60, "15分钟"),
    "min5":   (5 * 60, "5分钟"),
    "min1":   (60, "1分钟"),
}
DURATION = {k: v[0] for k, v in FREQS.items()}


def ensure_schema(freqs=None):
    """确保 fut_kline 表/超表存在（幂等）。"""
    eng = get_engine()
    with eng.begin() as c:
        c.execute(text("""
        CREATE TABLE IF NOT EXISTS fut_kline (
            freq            TEXT        NOT NULL,
            kind            TEXT        NOT NULL,
            symbol          TEXT        NOT NULL,
            trade_datetime  TIMESTAMPTZ NOT NULL,
            open            NUMERIC(20,4),
            high            NUMERIC(20,4),
            low             NUMERIC(20,4),
            close           NUMERIC(20,4),
            volume          BIGINT,
            oi              BIGINT,
            adj             NUMERIC(20,4) DEFAULT 0,
            PRIMARY KEY (freq, kind, symbol, trade_datetime)
        );
        """))
    # 建 TimescaleDB 超表（若不存在）；扩展不可用则退化为普通表
    try:
        with eng.begin() as c:
            c.execute(text("""
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM timescaledb_information.hypertables
                WHERE hypertable_name = 'fut_kline'
              ) THEN
                PERFORM create_hypertable('fut_kline', 'trade_datetime');
              END IF;
            END $$;
            """))
            c.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_fut_kline_lookup "
                "ON fut_kline (freq, kind, symbol, trade_datetime);"
            ))
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(f"[fut_kline] 超表创建跳过（非 TimescaleDB？）：{e}")
    return True


def _rows_to_recs(freq, kind, symbol, rows):
    recs = []
    for r in rows:
        ts = pd.Timestamp(r["date"])
        recs.append({
            "freq": freq,
            "kind": kind,
            "symbol": symbol,
            "trade_datetime": ts,
            "open": _num(r.get("open")),
            "high": _num(r.get("high")),
            "low": _num(r.get("low")),
            "close": _num(r.get("close")),
            "volume": int(r.get("volume") or 0),
            "oi": int(r.get("oi") or 0),
            "adj": float(r.get("adj") or 0),
        })
    return recs


def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def save_bars(freq, kind, symbol, rows, _batch=1000):
    """把某品种/合约一批 K 线 upsert 进 fut_kline（幂等：同 PK 覆盖）。"""
    if not rows:
        return 0
    delete_bars(freq, "cont_adj", symbol)  # 复权先清后插，避免脏数据累积
    recs = _rows_to_recs(freq, kind, symbol, rows)
    sess = get_session_factory()()
    try:
        for i in range(0, len(recs), _batch):
            chunk = recs[i:i + _batch]
            stmt = pg_insert(FutKline).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["freq", "kind", "symbol", "trade_datetime"],
                set_={
                    "open": stmt.excluded.open,
                    "high": stmt.excluded.high,
                    "low": stmt.excluded.low,
                    "close": stmt.excluded.close,
                    "volume": stmt.excluded.volume,
                    "oi": stmt.excluded.oi,
                    "adj": stmt.excluded.adj,
                },
            )
            sess.execute(stmt)
        sess.commit()
    finally:
        sess.close()
    return len(recs)


def delete_bars(freq, kind, symbol):
    """删除某 (freq,kind,symbol) 的全部行（用于复权重跑时清掉旧数据）。"""
    sess = get_session_factory()()
    try:
        from sqlalchemy import text as _t
        sess.execute(_t(
            "DELETE FROM fut_kline WHERE freq=:f AND kind=:k AND symbol=:s"
        ), {"f": freq, "k": kind, "s": symbol})
        sess.commit()
    finally:
        sess.close()


def filter_contract_rows(rows, code_full, product, code_digits):
    """取数入库前剔除合约代码窗口外的占位 phantom 行情。"""
    code = code_full.split(".", 1)[1]
    start, end = _contract_window(code, product, code_digits)
    out = []
    for r in rows:
        ts = pd.Timestamp(r["date"]).tz_localize("UTC")
        if start <= ts <= end:
            out.append(r)
    return out


def save_adjusted(freq, symbol, df, _batch=1000):
    """把 build_adjusted 的复权主连 DataFrame 写回 fut_kline（kind='cont_adj'）。

    df 列：date, open, high, low, close, volume, oi, adj。
    """
    rows = []
    for _, r in df.iterrows():
        d = str(r.get("date"))
        if len(d) > 10 and " " in d:
            d = d[:19]
        elif len(d) > 10:
            d = d[:10]
        rows.append({
            "date": d,
            "open": float(r.get("open")),
            "high": float(r.get("high")),
            "low": float(r.get("low")),
            "close": float(r.get("close")),
            "volume": int(r.get("volume") or 0),
            "oi": int(r.get("oi") or 0),
            "adj": float(r.get("adj")),
        })
    return save_bars(freq, "cont_adj", symbol, rows, _batch=_batch)


def load_bars(freq, kind, symbol, start=None, end=None):
    """读某 (freq,kind,symbol) 的 K 线，返回以 date 为列的升序 DataFrame；无则 None。"""
    sess = get_session_factory()()
    try:
        q = select(FutKline).where(
            FutKline.freq == freq, FutKline.kind == kind, FutKline.symbol == symbol
        )
        if start is not None:
            q = q.where(FutKline.trade_datetime >= pd.Timestamp(start))
        if end is not None:
            q = q.where(FutKline.trade_datetime <= pd.Timestamp(end))
        q = q.order_by(FutKline.trade_datetime)
        rows = sess.execute(q).scalars().all()
    finally:
        sess.close()
    if not rows:
        return None
    df = pd.DataFrame([{
        "date": r.trade_datetime,
        "open": float(r.open) if r.open is not None else None,
        "high": float(r.high) if r.high is not None else None,
        "low": float(r.low) if r.low is not None else None,
        "close": float(r.close) if r.close is not None else None,
        "volume": int(r.volume) if r.volume is not None else 0,
        "oi": int(r.oi) if r.oi is not None else 0,
        "adj": float(r.adj) if r.adj is not None else 0.0,
    } for r in rows])
    df["date"] = pd.to_datetime(df["date"])
    return df


def _contract_window(code, product, code_digits, ref=None):
    """由合约代码解析其真实交易窗口，剔除行情源填入的占位 phantom 行情。

    CZCE 用 3 位码（``FG609`` → 2026-09），其他所用 4 位（``rb2605`` → 2026-05）。

    ⚠️ 3 位码的「年」只有**个位**（十年一循环），必须锚定参考日才能定出完整年份。
    原实现硬编码 ``2020 + 个位``，跨到 2030 年代会整体错十年 ——
    现改为由 ``ref`` 推出「不早于 ref 的最小匹配年份」，``ref`` 取
    该合约行情自身的最后一天（见 ``_filter_contract``）。
    4 位码无歧义（两位年 + 两位月），不受影响。
    """
    suffix = code[len(product):]
    mo = int(suffix[-2:])
    if code_digits == 3:
        d = int(suffix[0])
        r = pd.Timestamp(ref) if ref is not None else pd.Timestamp.now(tz="UTC")
        ref_ym = r.year * 100 + r.month
        yr = r.year - 1
        while not (yr % 10 == d and yr * 100 + mo >= ref_ym):
            yr += 1
    else:
        yr = 2000 + int(suffix[:-2])
    cd = pd.Timestamp(yr, mo, 1, tz="UTC")
    start = cd - pd.DateOffset(years=2)
    end = cd + pd.DateOffset(years=1, months=1)
    return start, end


def _filter_contract(df, code, product, code_digits):
    if len(df) == 0:
        return df
    # 参考日取该合约行情自身的最后一天：只用来定 3 位码所属的十年，不影响筛选语义
    start, end = _contract_window(code, product, code_digits, ref=df.index.max())
    return df[(df.index >= start) & (df.index <= end)]


def load_contract_frames(freq, exchange, product, code_digits=4):
    """读某品种全部具体合约（kind='contract'，symbol LIKE 'EX.PRODUCT%'），
    返回 {合约代码(去前缀): DataFrame[date,open,high,low,close,volume,oi]（按时间索引）}。
    """
    like = f"{exchange}.{product}%"
    sess = get_session_factory()()
    try:
        q = select(FutKline).where(
            FutKline.freq == freq,
            FutKline.kind == "contract",
            FutKline.symbol.like(like),
        ).order_by(FutKline.trade_datetime)
        rows = sess.execute(q).scalars().all()
    finally:
        sess.close()
    if not rows:
        return {}
    by_code: dict[str, list] = {}
    for r in rows:
        code = r.symbol.split(".", 1)[1]
        # 防前缀碰撞：仅保留「品种代码 + 纯数字月份」的合约。
        # 例如品种 j 不应吞掉 jd/jm 的合约（否则 _contract_window 解析
        # suffix='d2012' 时 int('d20') 崩溃）；p 也不应吞掉 pp。
        suffix = code[len(product):]
        if not (code.startswith(product) and suffix.isdigit()
                and len(suffix) == code_digits):
            continue
        by_code.setdefault(code, []).append(
            (r.trade_datetime, r.open, r.high, r.low, r.close, r.volume, r.oi)
        )
    frames = {}
    for code, lst in by_code.items():
        df = pd.DataFrame(
            lst, columns=["date", "open", "high", "low", "close", "volume", "oi"]
        ).sort_values("date")
        for col in ("open", "high", "low", "close"):
            df[col] = df[col].astype(float)
        df["volume"] = df["volume"].astype("int64")
        df["oi"] = df["oi"].astype("int64")
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        df = _filter_contract(df, code, product, code_digits)  # 剔除占位 phantom 行情
        frames[code] = df
    return frames


def latest_date(freq, kind, symbol):
    """该 (freq,kind,symbol) 最新时间；无则 None。"""
    sess = get_session_factory()()
    try:
        from sqlalchemy import func
        v = sess.execute(
            select(func.max(FutKline.trade_datetime)).where(
                FutKline.freq == freq, FutKline.kind == kind, FutKline.symbol == symbol
            )
        ).scalar()
    finally:
        sess.close()
    if v is None:
        return None
    return pd.Timestamp(v).date()


def list_symbols(freq, kind):
    """列出库里已有的 (freq,kind) 下 symbol。"""
    sess = get_session_factory()()
    try:
        from sqlalchemy import distinct
        rows = sess.execute(
            select(distinct(FutKline.symbol)).where(
                FutKline.freq == freq, FutKline.kind == kind
            )
        ).all()
    finally:
        sess.close()
    return [r[0] for r in rows]
