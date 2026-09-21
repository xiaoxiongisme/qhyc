"""§18.6（v1.3.2）M6b：合约级日线采集器（carry 数据基础）

数据源矩阵（§18.13 同思路——坏接口降级 + 兜底切换，不硬编码 try）：
- SINA   ak.futures_zh_daily_sina(合约)      DCE/SHFE/INE/GFEX（大写代码）✅
- TQSDK  api.get_kline_serial(合约, 86400)   CZCE 兜底（sina 对 CZCE Length mismatch）✅
- CFFEX  暂不采（金融期货 carry 信号意义弱）

活跃合约清单来源：spot_basis.near_contract / dominant_contract（M6a 每日维护）。

symbol 口径（2026-09-20 统一）
-----------------------------
本模块**入参与入库都用标准码**（品种大写 + YYMM 四位，如 ``FG2701``）。
调用外部接口前必须转回该源的原生写法：

    sina  : ``_sina_symbol``  → ``symbol_code.to_sina``（全大写 4 位，恒等）
    tqsdk : ``_tqsdk_symbol`` → ``symbol_code.to_tqsdk``（``CZCE.FG701``，**郑商所仍 3 位**）

⚠️ 天勤那一步曾是最容易踩的坑：库里统一成 4 位后，若直接拿 ``CZCE.FG2701`` 去订阅，
   天勤会报合约不存在 —— 必须转回 3 位。

因子（§18.6）：term_slope / roll_yield / term_curv —— 见 carry_factors()。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import pandas as pd

from app.core import symbol_code as SC
from app.core.logging import logger
from app.models import ContractDaily


def _coerce_float(v) -> float | None:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _coerce_int(v) -> int | None:
    f = _coerce_float(v)
    return int(f) if f is not None else None


def _to_date(v) -> date | None:
    try:
        return pd.to_datetime(v).date()
    except Exception:
        return None


def _sina_symbol(exchange: str, symbol: str) -> str:
    """**标准码** → 新浪行情接口用的合约代码。

    新浪对所有交易所都用「品种大写 + YYMM 四位」（``rb2609`` / ``FG2701``），
    与标准码同构，故这里基本是恒等映射；单独留一层是为了将来新浪换写法只改一处。
    """
    return SC.to_sina(symbol, exchange)


def _tqsdk_symbol(exchange: str, symbol: str) -> str:
    """**标准码** → 天勤合约代码 ``交易所.原生码``。

    ⚠️ **必须转回交易所原生写法**：天勤按原生码订阅，郑商所仍是 **3 位**
    （``CZCE.FG701``，不是 ``CZCE.FG2701``）。
    本模块的入参已统一为标准码（``spot_basis.near_contract`` 等），
    若不转换就会拿 ``CZCE.FG2701`` 去订阅 → 报「合约不存在」。
    """
    return SC.to_tqsdk(symbol, exchange)


# 数据源矩阵（§18.13 风格）
CARRY_SOURCES: dict[str, dict] = {
    "DCE":   {"sina": True,  "tqsdk": False},
    "SHFE":  {"sina": True,  "tqsdk": False},
    "INE":   {"sina": True,  "tqsdk": False},
    "GFEX":  {"sina": True,  "tqsdk": False},
    "CZCE":  {"sina": False, "tqsdk": True},   # sina Length mismatch → tqsdk 兜底
    "CFFEX": {"sina": False, "tqsdk": False},  # 暂不采（金融期货 carry 弱）
}


def fetch_sina_contract_daily(symbol: str, days: int = 90, exchange: str = "") -> list[dict]:
    """ak.futures_zh_daily_sina：单合约日线（近 days 自然日）

    ``symbol`` 入参为**标准码**（4 位）；入库同样写标准码。
    """
    import akshare as ak  # type: ignore

    try:
        df = ak.futures_zh_daily_sina(symbol=_sina_symbol(exchange, symbol))
    except Exception as e:
        logger.warning(f"[contract_bars] sina {symbol} 失败: {e}")
        return []
    if df is None or df.empty:
        return []
    cutoff = date.today() - timedelta(days=days)
    out_symbol = SC.to_std(symbol, exchange=exchange)
    rows: list[dict] = []
    for _, r in df.iterrows():
        d = _to_date(r.get("date"))
        if d is None or d < cutoff:
            continue
        rows.append(
            {
                "symbol": out_symbol,
                "trade_date": d,
                "open": _coerce_float(r.get("open")),
                "high": _coerce_float(r.get("high")),
                "low": _coerce_float(r.get("low")),
                "close": _coerce_float(r.get("close")),
                "settle": _coerce_float(r.get("settle")),
                "volume": _coerce_int(r.get("volume")),
                "oi": _coerce_int(r.get("hold")),
                "src": "akshare:sina",
                "version": "v1.0",
            }
        )
    return rows


def fetch_tqsdk_contract_daily(
    session,
    exchange: str,
    symbol: str,
    days: int = 90,
) -> list[dict]:
    """tqsdk 兜底：CZCE 合约级日线（N/A 时 sina 用不了）

    ``symbol`` 入参为**标准码**（4 位）；订阅前用 ``_tqsdk_symbol`` 转回原生 3 位。
    """
    from datetime import datetime, timedelta as td
    from zoneinfo import ZoneInfo

    from app.ingest.tqsdk_calibrator import _get_tqsdk, close_tqsdk

    try:
        api = _get_tqsdk()
        kq = _tqsdk_symbol(exchange, symbol)
        klines = api.get_kline_serial(kq, duration_seconds=86400, data_length=min(days, 250))
        if klines is None or klines.empty:
            return []
        tz = ZoneInfo("Asia/Shanghai")
        cutoff = date.today() - timedelta(days=days)
        out_symbol = SC.to_std(symbol, exchange=exchange)
        rows: list[dict] = []
        for _, r in klines.iterrows():
            dtv = r.get("datetime")
            if dtv is None or (isinstance(dtv, float) and dtv != dtv):
                continue
            d = datetime.fromtimestamp(float(dtv) / 1e9, tz=tz).date()
            if d < cutoff:
                continue
            rows.append(
                {
                    "symbol": out_symbol,
                    "trade_date": d,
                    "open": _coerce_float(r.get("open")),
                    "high": _coerce_float(r.get("high")),
                    "low": _coerce_float(r.get("low")),
                    "close": _coerce_float(r.get("close")),
                    "settle": _coerce_float(r.get("settle")),
                    "volume": _coerce_int(r.get("volume")),
                    "oi": _coerce_int(r.get("close_oi")),
                    "src": "tqsdk",
                    "version": "v1.0",
                }
            )
        return rows
    except Exception as e:
        logger.warning(f"[contract_bars] tqsdk {exchange}.{symbol} 失败: {e}")
        return []
    finally:
        close_tqsdk()


def upsert_contract_daily(session, rows: list[dict]) -> int:
    if not rows:
        return 0
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    seen = {}
    for r in rows:
        k = (r["symbol"], r["trade_date"], r["version"])
        seen[k] = r
    rows = list(seen.values())

    stmt = pg_insert(ContractDaily).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["symbol", "trade_date", "version"],
        set_={
            "open": stmt.excluded.open,
            "high": stmt.excluded.high,
            "low": stmt.excluded.low,
            "close": stmt.excluded.close,
            "settle": stmt.excluded.settle,
            "volume": stmt.excluded.volume,
            "oi": stmt.excluded.oi,
            "src": stmt.excluded.src,
        },
    )
    session.execute(stmt)
    return len(rows)


def collect_contract_bars(
    session,
    products: list[str] | None = None,
    days: int = 90,
) -> dict[str, Any]:
    """按品种采集主力+近月合约日线

    活跃合约清单取自 spot_basis 最新一日（M6a 每日维护）。
    数据源按 CARRY_SOURCES 矩阵：sina 主源，CZCE 走 tqsdk 兜底。
    """
    from sqlalchemy import text

    # 1. 取各品种活跃合约（近月 + 主力）
    sql = """
        SELECT DISTINCT ON (symbol) symbol, near_contract, dominant_contract
        FROM spot_basis
        ORDER BY symbol, report_date DESC
    """
    rows = session.execute(text(sql)).all()
    if products:
        want = {p.upper() for p in products}
        rows = [r for r in rows if r[0].upper() in want]

    # 2. exchange 归属（用 daily_bar 的 symbol 后缀映射）
    ex_map = {
        r[0]: r[1]
        for r in session.execute(
            text("SELECT DISTINCT ON (product) product, exchange FROM futures_symbol ORDER BY product, updated_at DESC")
        ).all()
    }

    stats: dict[str, Any] = {"contracts": 0, "rows": 0, "by_source": {}, "errors": []}
    for product, near_c, dom_c in rows:
        contracts = {c for c in (near_c, dom_c) if c}
        if not contracts:
            continue
        # exchange：优先 spot_basis 无 exchange 字段 → futures_symbol 映射 → 启发式
        exchange = ex_map.get(product.upper(), "")
        if not exchange:
            stats["errors"].append(f"{product}: exchange 未知，跳过")
            continue
        src_cfg = CARRY_SOURCES.get(exchange, {})
        for c in contracts:
            try:
                if src_cfg.get("sina"):
                    r = fetch_sina_contract_daily(c, days=days, exchange=exchange)
                elif src_cfg.get("tqsdk"):
                    r = fetch_tqsdk_contract_daily(session, exchange, c, days=days)
                else:
                    stats["errors"].append(f"{product}/{c}: {exchange} 未配置数据源")
                    continue
                # 补 product/exchange 字段
                for row in r:
                    row["product"] = product.upper()
                    row["exchange"] = exchange
                n = upsert_contract_daily(session, r)
                session.commit()
                stats["contracts"] += 1
                stats["rows"] += n
                src_key = "sina" if src_cfg.get("sina") else ("tqsdk" if src_cfg.get("tqsdk") else "none")
                stats["by_source"][src_key] = stats["by_source"].get(src_key, 0) + n
            except Exception as e:
                session.rollback()
                stats["errors"].append(f"{product}/{c}: {e}")
                logger.exception(f"[contract_bars] {product}/{c} 失败: {e}")
    return stats


def carry_factors(contract_rows: pd.DataFrame) -> dict:
    """§18.6 carry/期限结构因子

    contract_rows: 同品种同日多合约切片，列 [symbol, trade_date, close, oi]
    按品种时间序列计算：
    - term_slope:   (次主力 close - 主力 close) / 主力 close（%）
    - roll_yield:   (近月 - 主力)/主力 / 月份数（月化展期收益 %）
    - term_curv:    三合约曲率（若可得）
    """
    if contract_rows is None or contract_rows.empty:
        return {}
    out: dict = {}
    # 最新一日切片
    latest_d = contract_rows["trade_date"].max()
    cur = contract_rows[contract_rows["trade_date"] == latest_d].dropna(subset=["close"])
    if len(cur) < 2:
        return {}
    # 按 oi 排序：主力 = oi 最大；次主力 = oi 次大
    cur = cur.sort_values("oi", ascending=False).reset_index(drop=True)
    dom_close = float(cur.iloc[0]["close"])
    sub_close = float(cur.iloc[1]["close"])
    if dom_close <= 0:
        return {}
    out["dom_contract"] = cur.iloc[0]["symbol"]
    out["sub_contract"] = cur.iloc[1]["symbol"]
    out["term_slope_pct"] = round((sub_close - dom_close) / dom_close * 100, 4)
    # 展期收益（月化）：价差 / 主力价 / 月份数
    # 交割年月统一走 app.core.symbol_code（标准码是 4 位，年月解析只有一处）
    def _month(sym: str) -> int:
        try:
            y, m = SC.delivery_ym(sym)
            return 0 if y is None else y * 12 + m
        except Exception:  # noqa: BLE001
            return 0

    m_dom = _month(cur.iloc[0]["symbol"])
    m_sub = _month(cur.iloc[1]["symbol"])
    months = max(1, abs(m_sub - m_dom))
    out["roll_yield_monthly_pct"] = round((sub_close - dom_close) / dom_close / months * 100, 4)
    # 主力 oi（趋势参考）
    out["dom_oi"] = int(cur.iloc[0]["oi"] or 0)
    return out
