"""获取 1 分钟合约数据，并合成 5/15/30/60 分钟 K 线，支持获取日线数据。

能力
----
- 从 tqsdk 拉取 1 分钟 K 线（主连 ``KQ.m@交易所.品种`` 或具体合约 ``交易所.代码``），
  upsert 入 ``minute_bar``；
- 由 ``minute_bar`` 合成 5/15/30/60 分钟（``time_bucket``，起点标签，Asia/Shanghai），
  写入 ``bar_5m`` / ``bar_15m`` / ``bar_30m`` / ``bar_60m``；
- 从 akshare 拉取主连日线，upsert 入 ``daily_bar``；
- ``--ensure-schema`` 一键确保 5 张 K 线表存在并转为 TimescaleDB 超表（含压缩策略）；
- ``--prune-1m-5m`` 本地清理：清空 ``minute_bar`` / ``bar_5m``（仅删数据，保留表）；
- ``--smoke`` 用合成数据自测合成管线（无需网络）。

设计要点
--------
- ``minute_bar`` 是唯一事实来源；合成逐级 cascade（5←分钟, 15←5, 30←15, 60←30），
  桶边界天然对齐、等价且省扫描（见 scripts/load_1min.py 说明）。
- 时间戳口径：``minute_bar.ts`` 为 end-time 标记（与历史 CSV 一致），5m 用 ``ts-1min`` 分桶，
  15/30/60 在已对齐的 5m 桶上再 time_bucket。
- 入库幂等：``ON CONFLICT (symbol, ts/bucket) DO NOTHING``，可重复跑。
- 安全阀：``--synth`` 不带任何范围参数时，默认只重算最近 7 天（增量），
  不会清空既有 15/30/60 分钟数据；全量重算需显式 ``--full``。

运行（容器内 / 宿主机均可，需能连到 timescaledb）
------------------------------------------------
  python scripts/fetch_contract_1min.py --ensure-schema
  python scripts/fetch_contract_1min.py --prune-1m-5m --ensure-schema
  python scripts/fetch_contract_1min.py --symbols RB888,FG888 --fetch-min --synth
  python scripts/fetch_contract_1min.py --symbols SHFE.rb2510 --fetch-min --synth
  python scripts/fetch_contract_1min.py --symbols RB888 --fetch-daily --start 2020-01-01
  python scripts/fetch_contract_1min.py --smoke
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

_PROJ_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJ_ROOT not in sys.path:
    sys.path.insert(0, _PROJ_ROOT)

from sqlalchemy import (  # noqa: E402
    create_engine,
    text,
    table as sa_table,
    column as sa_column,
    insert as sa_insert,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402
from sqlalchemy.orm import sessionmaker, Session  # noqa: E402

from app.core.config import get_settings  # noqa: E402

_SH_TZ = ZoneInfo("Asia/Shanghai")

# ---------------------------------------------------------------------------
# 表 DDL（与 scripts/load_1min.py 保持一致）
# ---------------------------------------------------------------------------
_MINUTE_DDL = """
CREATE TABLE IF NOT EXISTS minute_bar (
  symbol text NOT NULL,
  ts timestamptz NOT NULL,
  open numeric, high numeric, low numeric, close numeric,
  volume bigint, amount numeric, open_interest numeric,
  high_limit numeric, low_limit numeric, pre_close numeric, settle_price numeric,
  contract text, src text,
  PRIMARY KEY (symbol, ts)
);
"""
_BAR_DDL = (
    "CREATE TABLE IF NOT EXISTS {t} (\n"
    "  symbol text NOT NULL, bucket timestamptz NOT NULL,\n"
    "  open numeric, high numeric, low numeric, close numeric,\n"
    "  volume bigint, amount numeric, open_interest numeric,\n"
    "  PRIMARY KEY (symbol, bucket));"
)
_BAR_TABLES = [
    ("bar_5m", "5 minutes"),
    ("bar_15m", "15 minutes"),
    ("bar_30m", "30 minutes"),
    ("bar_60m", "60 minutes"),
]
_CHUNK = {
    "minute_bar": "30 days",
    "bar_5m": "30 days",
    "bar_15m": "90 days",
    "bar_30m": "90 days",
    "bar_60m": "180 days",
}

# core 描述（用于 upsert）
_MINUTE_BAR = sa_table(
    "minute_bar",
    sa_column("symbol"), sa_column("ts"), sa_column("open"), sa_column("high"),
    sa_column("low"), sa_column("close"), sa_column("volume"), sa_column("amount"),
    sa_column("open_interest"), sa_column("high_limit"), sa_column("low_limit"),
    sa_column("pre_close"), sa_column("settle_price"), sa_column("contract"),
    sa_column("src"),
)
_DAILY_BAR = sa_table(
    "daily_bar",
    sa_column("symbol"), sa_column("trade_date"), sa_column("open"),
    sa_column("high"), sa_column("low"), sa_column("close"), sa_column("settle"),
    sa_column("volume"), sa_column("amount"), sa_column("oi"),
    sa_column("ret_close"), sa_column("ret_settle"), sa_column("ret5"),
    sa_column("ret20"), sa_column("src"),
)


# ---------------------------------------------------------------------------
# Schema / 超表
# ---------------------------------------------------------------------------
def ensure_schema(session: Session) -> None:
    """确保 5 张 K 线表存在，并转为超表（含压缩策略）。幂等。"""
    session.execute(text(_MINUTE_DDL))
    for t, _ in _BAR_TABLES:
        session.execute(text(_BAR_DDL.format(t=t)))
    session.commit()
    for name, iv in _CHUNK.items():
        time_col = "ts" if name == "minute_bar" else "bucket"
        session.execute(text(
            f"SELECT create_hypertable('{name}', '{time_col}', "
            f"chunk_time_interval => INTERVAL '{iv}', if_not_exists => TRUE)"
        ))
    session.commit()
    # 压缩：仅对大体量的分钟 / 5 分开启
    for name, order in [("minute_bar", "ts"), ("bar_5m", "bucket")]:
        session.execute(text(
            f"ALTER TABLE {name} SET (timescaledb.compress, "
            f"timescaledb.compress_segmentby='symbol', "
            f"timescaledb.compress_orderby='{order}')"
        ))
        session.execute(text(
            f"SELECT add_compression_policy('{name}', INTERVAL '30 days', if_not_exists => TRUE)"
        ))
    session.commit()
    print("[schema] 5 张 K 线表已确保为超表（含压缩策略）")


def prune_1m_5m(session: Session) -> None:
    """本地清理：清空 1 分钟 / 5 分钟数据（保留表，留待按需重建）。"""
    session.execute(text("TRUNCATE TABLE minute_bar; TRUNCATE TABLE bar_5m;"))
    session.commit()
    print("[prune] 已清空 minute_bar / bar_5m")


# ---------------------------------------------------------------------------
# 合成 5/15/30/60 分钟
# ---------------------------------------------------------------------------
def synthesize(session: Session, symbols: list[str] | None = None,
               since: datetime | None = None, full: bool = False) -> dict:
    """由 minute_bar 合成多周期。四种模式（安全优先）：

    - full=True                 : 全量 TRUNCATE 后重算（显式迁移用）
    - since 给定                : 仅重算该时刻之后的桶（增量）
    - symbols 给定              : 仅重算指定品种
    - 都不给（默认）            : 安全增量，仅重算最近 7 天（不会清空既有数据）
    """
    if full:
        for t, _ in _BAR_TABLES:
            session.execute(text(f"TRUNCATE TABLE {t}"))
    elif since is not None:
        for t, _ in _BAR_TABLES:
            session.execute(
                text(f"DELETE FROM {t} WHERE bucket >= :since"), {"since": since}
            )
    elif symbols is not None:
        for t, _ in _BAR_TABLES:
            session.execute(
                text(f"DELETE FROM {t} WHERE symbol = ANY(:syms)"), {"syms": symbols}
            )
    else:
        since = datetime.now(_SH_TZ) - timedelta(days=7)
        for t, _ in _BAR_TABLES:
            session.execute(
                text(f"DELETE FROM {t} WHERE bucket >= :since"), {"since": since}
            )
    session.commit()

    if symbols is not None:
        syms = symbols
    else:
        syms = [r[0] for r in session.execute(
            text("SELECT DISTINCT symbol FROM minute_bar")
        ).all()]
    if not syms:
        print("[synth] minute_bar 为空，跳过")
        return {}
    for s in syms:
        _synth_one(session, s, since)
    session.commit()
    stats: dict[str, int] = {}
    for t, _ in _BAR_TABLES:
        stats[t] = session.execute(text(f"SELECT count(*) FROM {t}")).scalar() or 0
    print(f"[synth] 完成 {len(syms)} 个品种 -> {stats}")
    return stats


def _synth_one(session: Session, symbol: str, since: datetime | None) -> None:
    p = {"sym": symbol}
    since_sql = ""
    if since:
        p["since"] = since
        since_sql = "AND ts >= :since"
    # 5m：由分钟线聚合（起点对齐 ts-1min）
    session.execute(text(
        f"""
        INSERT INTO bar_5m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
        SELECT :sym,
               time_bucket(INTERVAL '5 minutes', ts - INTERVAL '1 minute', 'Asia/Shanghai'),
               first(open, ts), max(high), min(low), last(close, ts),
               sum(volume)::bigint, sum(amount), last(open_interest, ts)
        FROM minute_bar WHERE symbol = :sym AND close IS NOT NULL {since_sql}
        GROUP BY 2
        ON CONFLICT (symbol, bucket) DO NOTHING
        """
    ), p)
    # 15m <- 5m
    session.execute(text(
        """
        INSERT INTO bar_15m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
        SELECT symbol, time_bucket(INTERVAL '15 minutes', bucket, 'Asia/Shanghai'),
               first(open, bucket), max(high), min(low), last(close, bucket),
               sum(volume)::bigint, sum(amount), last(open_interest, bucket)
        FROM bar_5m WHERE symbol = :sym GROUP BY 1, 2
        ON CONFLICT (symbol, bucket) DO NOTHING
        """
    ), p)
    # 30m <- 15m
    session.execute(text(
        """
        INSERT INTO bar_30m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
        SELECT symbol, time_bucket(INTERVAL '30 minutes', bucket, 'Asia/Shanghai'),
               first(open, bucket), max(high), min(low), last(close, bucket),
               sum(volume)::bigint, sum(amount), last(open_interest, bucket)
        FROM bar_15m WHERE symbol = :sym GROUP BY 1, 2
        ON CONFLICT (symbol, bucket) DO NOTHING
        """
    ), p)
    # 60m <- 30m
    session.execute(text(
        """
        INSERT INTO bar_60m (symbol, bucket, open, high, low, close, volume, amount, open_interest)
        SELECT symbol, time_bucket(INTERVAL '60 minutes', bucket, 'Asia/Shanghai'),
               first(open, bucket), max(high), min(low), last(close, bucket),
               sum(volume)::bigint, sum(amount), last(open_interest, bucket)
        FROM bar_30m WHERE symbol = :sym GROUP BY 1, 2
        ON CONFLICT (symbol, bucket) DO NOTHING
        """
    ), p)


# ---------------------------------------------------------------------------
# tqsdk 拉取 1 分钟
# ---------------------------------------------------------------------------
_TQ_CASE = {
    "CZCE": str.upper, "CFFEX": str.upper, "DCE": str.lower,
    "SHFE": str.lower, "INE": str.lower,
}


def _to_tq_symbol(sym: str) -> str:
    """主连代码(如 RB888) -> KQ.m@EXCH.PROD；含 '.' 视为完整 tqsdk 合约代码。"""
    if "." in sym:
        return sym
    if sym.endswith("888"):
        spec = next((m for m in get_settings().main_contracts if m.symbol == sym), None)
        if not spec:
            raise ValueError(f"未在主连配置中找到 {sym}，请改用完整代码如 SHFE.rb2510")
        prod = _TQ_CASE.get(spec.exchange, str.lower)(spec.product)
        return f"KQ.m@{spec.exchange}.{prod}"
    raise ValueError(f"无法解析 {sym}：请传完整 tqsdk 代码(含.)或主连代码(如 RB888)")


def _parse_tq_klines(klines, symbol: str) -> list[dict]:
    import numpy as np  # type: ignore

    rows: list[dict] = []
    n = len(klines)
    for i in range(n):
        rec = klines.iloc[i]
        dt_raw = rec.get("datetime")
        if dt_raw is None:
            continue
        # tqsdk datetime 为纳秒级 unix 时间戳
        if isinstance(dt_raw, (int, float)):
            if isinstance(dt_raw, float) and dt_raw != dt_raw:
                continue
            dt = datetime.fromtimestamp(float(dt_raw) / 1e9, tz=timezone.utc).astimezone(_SH_TZ)
        elif hasattr(dt_raw, "astimezone"):
            dt = dt_raw.astimezone(_SH_TZ) if dt_raw.tzinfo else dt_raw.replace(tzinfo=_SH_TZ)
        else:
            continue
        o, h, l, c = rec.get("open"), rec.get("high"), rec.get("low"), rec.get("close")
        if None in (o, h, l, c):
            continue
        try:
            vals = [float(v) for v in (o, h, l, c)]
        except (TypeError, ValueError):
            continue
        if any(v != v or abs(v) == float("inf") for v in vals):
            continue
        if dt.year <= 1970 or dt > datetime.now(_SH_TZ) + timedelta(minutes=2):
            continue
        vol = rec.get("volume")
        oi = rec.get("close_oi") or rec.get("open_oi")
        rows.append({
            "symbol": symbol, "ts": dt,
            "open": float(o), "high": float(h), "low": float(l), "close": float(c),
            "volume": int(vol) if vol is not None else 0,
            "amount": None, "open_interest": int(oi) if oi is not None else None,
            "high_limit": None, "low_limit": None, "pre_close": None,
            "settle_price": None, "contract": None, "src": "tqsdk_1min",
        })
    return rows


def fetch_min(session: Session, symbols: list[str], data_length: int = 8000) -> dict:
    from tqsdk import TqApi, TqAuth  # type: ignore

    s = get_settings().env
    if not s.TQSDK_PHONE or not s.TQSDK_PASSWORD:
        raise RuntimeError("TQSDK 凭证未配置（.env 中 TQSDK_PHONE/TQSDK_PASSWORD）")
    api = TqApi(auth=TqAuth(s.TQSDK_PHONE, s.TQSDK_PASSWORD))
    stats: dict[str, int] = {}
    try:
        for sym in symbols:
            tq = _to_tq_symbol(sym)
            print(f"[fetch-min] {sym} -> tqsdk {tq}")
            klines = api.get_kline_serial(tq, duration_seconds=60, data_length=data_length)
            # 等待序列完整下载：tqsdk 主连 1 分钟上限约 10000 根，需多轮 wait_update 才填满；
            # 早退会导致只抓到半截。超时才放行（合约历史不足 data_length 时也靠它收尾）。
            _deadline = time.monotonic() + min(300.0, max(30.0, data_length / 50.0))
            while klines is not None and len(klines) < data_length:
                try:
                    api.wait_update(timeout=20)
                except Exception:
                    break
                if time.monotonic() > _deadline:
                    break
            if klines is None or len(klines) == 0:
                print(f"[fetch-min] {sym} 无数据")
                stats[sym] = 0
                continue
            rows = _parse_tq_klines(klines, sym)
            if rows:
                stmt = pg_insert(_MINUTE_BAR).values(rows).on_conflict_do_nothing(
                    index_elements=["symbol", "ts"]
                )
                session.execute(stmt)
                session.commit()
            stats[sym] = len(rows)
            print(f"[fetch-min] {sym} upsert {len(rows)} 行")
    finally:
        api.close()
    return stats


# ---------------------------------------------------------------------------
# akshare 拉取日线
# ---------------------------------------------------------------------------
def fetch_daily(session: Session, symbols: list[str],
                start: date | None = None, end: date | None = None) -> dict:
    from app.ingest.akshare_source import AkShareSource

    start = start or date(2015, 1, 1)
    end = end or date.today()
    stats: dict[str, int] = {}
    for sym in symbols:
        if not sym.endswith("888"):
            print(f"[fetch-daily] 跳过 {sym}：akshare 日线仅支持主连代码（如 RB888）")
            continue
        src = AkShareSource(AkShareSource.detect_exchange(sym[:-3]))
        rows = src.fetch_daily(sym, start, end)
        if not rows:
            stats[sym] = 0
            continue
        stmt = pg_insert(_DAILY_BAR).values(rows)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "trade_date"],
            set_={c: stmt.excluded[c] for c in
                  ["open", "high", "low", "close", "settle", "volume",
                   "amount", "oi", "ret_close", "ret_settle", "ret5", "ret20", "src"]},
        )
        session.execute(stmt)
        session.commit()
        stats[sym] = len(rows)
        print(f"[fetch-daily] {sym} upsert {len(rows)} 行")
    return stats


# ---------------------------------------------------------------------------
# 自测（合成数据，无需网络）
# ---------------------------------------------------------------------------
def _smoke(session: Session) -> None:
    sym = "__SMOKE__"
    base = datetime(2025, 1, 2, 9, 1, tzinfo=_SH_TZ)
    rows = [{
        "symbol": sym, "ts": base + timedelta(minutes=i),
        "open": 1000 + i, "high": 1001 + i, "low": 999 + i, "close": 1000 + i,
        "volume": 10, "amount": (1000 + i) * 10, "open_interest": 5,
        "high_limit": None, "low_limit": None, "pre_close": None,
        "settle_price": None, "contract": None, "src": "smoke",
    } for i in range(300)]
    session.execute(pg_insert(_MINUTE_BAR).values(rows).on_conflict_do_nothing(
        index_elements=["symbol", "ts"]))
    session.commit()
    synthesize(session, symbols=[sym])
    for t, _ in _BAR_TABLES:
        n = session.execute(
            text(f"SELECT count(*) FROM {t} WHERE symbol=:s"), {"s": sym}
        ).scalar()
        print(f"[smoke] {t}: {n} 行")
    # 清理
    session.execute(text("DELETE FROM minute_bar WHERE symbol=:s"), {"s": sym})
    for t, _ in _BAR_TABLES:
        session.execute(text(f"DELETE FROM {t} WHERE symbol=:s"), {"s": sym})
    session.commit()
    print("[smoke] 合成管线通过，已清理测试数据")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _resolve_symbols(arg: str | None) -> list[str]:
    if not arg:
        return []
    return [s.strip() for s in arg.split(",") if s.strip()]


def _parse_date(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def main() -> None:
    ap = argparse.ArgumentParser(description="获取1分钟合约数据并合成多周期/日线")
    ap.add_argument("--symbols", help="逗号分隔：主连(结束888)或完整tqsdk代码(含.)，如 RB888,SHFE.rb2510")
    ap.add_argument("--start", help="YYYY-MM-DD（日线/增量起点）")
    ap.add_argument("--end", help="YYYY-MM-DD")
    ap.add_argument("--data-length", type=int, default=8000, help="tqsdk 单次拉取根数")
    ap.add_argument("--since", help="合成增量起点(含该天)，如 2025-05-01")
    ap.add_argument("--full", action="store_true", help="合成全量重算（TRUNCATE 后重建，谨慎）")
    ap.add_argument("--ensure-schema", action="store_true", help="确保5张表存在并转超表")
    ap.add_argument("--prune-1m-5m", action="store_true", help="清空 minute_bar/bar_5m（本地清理）")
    ap.add_argument("--fetch-min", action="store_true", help="tqsdk 拉取1分钟")
    ap.add_argument("--synth", action="store_true", help="合成5/15/30/60分钟（默认增量最近7天）")
    ap.add_argument("--fetch-daily", action="store_true", help="akshare 拉取主连日线")
    ap.add_argument("--all", action="store_true", help="fetch-min + synth")
    ap.add_argument("--smoke", action="store_true", help="合成管线自测（无需网络）")
    args = ap.parse_args()

    settings = get_settings()
    engine = create_engine(settings.db_url, future=True, pool_pre_ping=True)
    Session = sessionmaker(bind=engine, future=True)
    with Session() as session:
        if args.ensure_schema:
            ensure_schema(session)
        if args.prune_1m_5m:
            prune_1m_5m(session)
        if args.smoke:
            _smoke(session)
        if args.all or args.fetch_min:
            syms = _resolve_symbols(args.symbols) or [m.symbol for m in settings.main_contracts]
            fetch_min(session, syms, args.data_length)
        if args.all or args.synth:
            syms = _resolve_symbols(args.symbols) or None
            since = (datetime.fromisoformat(args.since).replace(tzinfo=_SH_TZ)
                     if args.since else None)
            synthesize(session, syms, since, full=args.full)
        if args.fetch_daily:
            syms = _resolve_symbols(args.symbols) or [m.symbol for m in settings.main_contracts]
            fetch_daily(session, syms, _parse_date(args.start), _parse_date(args.end))
    print("DONE")


if __name__ == "__main__":
    main()
