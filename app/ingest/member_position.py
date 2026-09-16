"""§18.4（v1.3.2）M6a：会员持仓排名（龙虎榜）采集器

数据源（§18.13 EXCHANGE_COVERAGE 矩阵驱动）：
- CZCE  : ak.get_rank_table_czce(date)         ✅ 2567 行/日（全品种）
- CFFEX : ak.get_cffex_rank_table(date, vars)  ✅ 496 行/日
- GFEX  : ak.futures_gfex_position_rank(date, vars) ✅ v1.3.2 新增（SI/LC/PS，2023-11-10 起）
- DCE   : ak.futures_dce_position_rank(...)    ❌ K1: BadZipFile（akshare 1.18.94，待修复）
- SHFE  : ak.get_shfe_rank_table(...)          ❌ K1: 静默空 dict（v1.18.94，待修复）
- INE   : 无持仓/库存接口                       ❌ K4: 硬上限 68/73

**§18.13 红线**（PRD v1.3.2 实现约束）：
- EXCHANGE_COVERAGE 矩阵化（active/func/symbols/known_issue/fallbacks）
- 发现接口坏时**降级到 active=False** + 标注 K1（不硬编码 try 跳过）
- akshare 修复后：仅需翻转 active=True + 补 symbols 即可一键扩展覆盖
- 不允许在采集器内对 SHFE/DCE 写硬编码 try 豁免逻辑

防前视（§18.4）：T 日盘后调度，as_of = T（latency 1 自然生效）。
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

import pandas as pd

from app.core.logging import logger
from app.models import MemberPositionRank


# §18.13 EXCHANGE_COVERAGE 矩阵（v1.3.2）
# - active=False 的所：仅记录到 errors，不调用 func（避免对坏接口硬编码 try）
# - symbols：active=False 时可省（即便提供也不调用）
# - known_issue：禁用原因（K1 接口坏 / K4 无接口）
# - fallbacks：可选项，akshare 修复后替代 func 的备选接口列表
def _build_exchange_coverage() -> dict[str, dict]:
    """延迟构造矩阵（避免 import 期 akshare 失败时模块加载）"""
    import akshare as ak  # type: ignore

    return {
        "CZCE": {
            "active": True,
            "func": ak.get_rank_table_czce,
            "args": lambda d: {"date": d.strftime("%Y%m%d")},
            "symbols": None,        # 接口一次性返回全品种
            "parser": "_parse_czce_one",
            "known_issue": None,
        },
        "CFFEX": {
            "active": True,
            "func": ak.get_cffex_rank_table,
            "args": lambda d, syms: {"date": d.strftime("%Y%m%d"), "vars_list": syms},
            "symbols": ["IF", "IC", "IM", "IH", "T", "TF", "TS", "TL"],
            "parser": "_parse_cffex_one",
            "known_issue": None,
        },
        "GFEX": {  # §18.13 v1.3.2 新增
            "active": True,
            "func": ak.futures_gfex_position_rank,
            "args": lambda d, syms: {"date": d.strftime("%Y%m%d"), "vars_list": syms},
            "symbols": ["SI", "LC", "PS"],  # PS 已上市但接口可能偶发空，按需过滤
            "parser": "_parse_gfex_one",
            "since": date(2023, 11, 10),  # GFEX 工业硅上市日
            "known_issue": None,
        },
        "DCE": {
            "active": False,        # K1: v1.18.94 BadZipFile
            "func": ak.futures_dce_position_rank,
            "symbols": list({"A", "B", "C", "CS", "FB", "BB", "I", "J", "JM", "L", "M", "P", "PP", "V", "Y", "EG"}),
            "fallbacks": [],         # akshare 修复后回填
            "known_issue": "K1",
        },
        "SHFE": {
            "active": False,        # K1: v1.18.94 静默空 dict
            "func": ak.get_shfe_rank_table,
            "symbols": list({"CU", "AU", "AG", "AL", "ZN", "PB", "NI", "SN", "RB", "RU", "BU", "FU", "HC", "SP", "SS", "NR", "SC"}),
            "fallbacks": [],
            "known_issue": "K1",
        },
        "INE": {  # K4: 持仓硬上限 68/73
            "active": False,
            "symbols": ["SC", "BC", "EC", "LU", "NR"],
            "known_issue": "K4",
            "reason": "akshare 无 INE 持仓/库存接口（§18.13 K4）",
        },
    }


EXCHANGE_COVERAGE: dict[str, dict] = _build_exchange_coverage()


def _coerce_int(v) -> int:
    """DataFrame 中 '1,001,351' 等字符串 → int（容忍 NaN→0）"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return 0
    if isinstance(v, (int,)):
        return int(v)
    s = str(v).replace(",", "").replace(" ", "").strip()
    if not s or s in ("-", "--"):
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _parse_czce_one(df: pd.DataFrame, trade_date: date, symbol_hint: str = "") -> list[dict]:
    """郑商所单品种 DataFrame → 标准化行（§18.4：v1.0 字段映射）"""
    rows = []
    for _, r in df.iterrows():
        try:
            rows.append(
                {
                    "trade_date": trade_date,
                    "exchange": "CZCE",
                    "symbol": str(r.get("symbol") or symbol_hint or "").upper(),
                    "member": str(r.get("long_party_name") or r.get("vol_party_name") or "").strip(),
                    "rank": _coerce_int(r.get("rank")),
                    "long_pos": _coerce_int(r.get("long_open_interest")),
                    "short_pos": _coerce_int(r.get("short_open_interest")),
                    "long_chg": _coerce_int(r.get("long_open_interest_chg")),
                    "short_chg": _coerce_int(r.get("short_open_interest_chg")),
                    "vol_pos": _coerce_int(r.get("vol")),
                    "src": "akshare:get_rank_table_czce",
                    "version": "v1.0",
                }
            )
        except Exception as e:
            logger.warning(f"[member_position] CZCE 解析行失败: {e}")
    return rows


def _parse_shfe_one(df: pd.DataFrame, trade_date: date, symbol_hint: str) -> list[dict]:
    """上期所单合约 DataFrame（列: rank, long_party_name, long_open_interest, ...）"""
    rows = []
    for _, r in df.iterrows():
        try:
            rows.append(
                {
                    "trade_date": trade_date,
                    "exchange": "SHFE",
                    "symbol": str(r.get("symbol") or symbol_hint).upper(),
                    "member": str(r.get("long_party_name") or r.get("vol_party_name") or "").strip(),
                    "rank": _coerce_int(r.get("rank")),
                    "long_pos": _coerce_int(r.get("long_open_interest")),
                    "short_pos": _coerce_int(r.get("short_open_interest")),
                    "long_chg": _coerce_int(r.get("long_open_interest_chg")),
                    "short_chg": _coerce_int(r.get("short_open_interest_chg")),
                    "vol_pos": _coerce_int(r.get("vol")),
                    "src": "akshare:get_shfe_rank_table",
                    "version": "v1.0",
                }
            )
        except Exception as e:
            logger.warning(f"[member_position] SHFE 解析行失败: {e}")
    return rows


def _parse_cffex_one(df: pd.DataFrame, trade_date: date, symbol_hint: str) -> list[dict]:
    """中金所（CFFEX 字段与 SHFE 同构，按需适配）"""
    rows = []
    for _, r in df.iterrows():
        try:
            rows.append(
                {
                    "trade_date": trade_date,
                    "exchange": "CFFEX",
                    "symbol": str(r.get("symbol") or symbol_hint).upper(),
                    "member": str(r.get("long_party_name") or r.get("vol_party_name") or "").strip(),
                    "rank": _coerce_int(r.get("rank")),
                    "long_pos": _coerce_int(r.get("long_open_interest")),
                    "short_pos": _coerce_int(r.get("short_open_interest")),
                    "long_chg": _coerce_int(r.get("long_open_interest_chg")),
                    "short_chg": _coerce_int(r.get("short_open_interest_chg")),
                    "vol_pos": _coerce_int(r.get("vol")),
                    "src": "akshare:get_cffex_rank_table",
                    "version": "v1.0",
                }
            )
        except Exception as e:
            logger.warning(f"[member_position] CFFEX 解析行失败: {e}")
    return rows


def _parse_gfex_one(df: pd.DataFrame, trade_date: date, symbol_hint: str) -> list[dict]:
    """§18.13 v1.3.2 新增：广期所（GFEX，列与 CZCE/CFFEX 同构）"""
    rows = []
    for _, r in df.iterrows():
        try:
            rows.append(
                {
                    "trade_date": trade_date,
                    "exchange": "GFEX",
                    "symbol": str(r.get("symbol") or symbol_hint).upper(),
                    "member": str(r.get("long_party_name") or r.get("vol_party_name") or "").strip(),
                    "rank": _coerce_int(r.get("rank")),
                    "long_pos": _coerce_int(r.get("long_open_interest")),
                    "short_pos": _coerce_int(r.get("short_open_interest")),
                    "long_chg": _coerce_int(r.get("long_open_interest_chg")),
                    "short_chg": _coerce_int(r.get("short_open_interest_chg")),
                    "vol_pos": _coerce_int(r.get("vol")),
                    "src": "akshare:futures_gfex_position_rank",
                    "version": "v1.0",
                }
            )
        except Exception as e:
            logger.warning(f"[member_position] GFEX 解析行失败: {e}")
    return rows


def fetch_czce(trade_date: date) -> list[dict]:
    """§18.13 兼容层：直接调用矩阵中的 func + parser（CZCE 一次返回全品种）"""
    rows: list[dict] = []
    meta = EXCHANGE_COVERAGE["CZCE"]
    if not meta["active"]:
        return rows
    result = meta["func"](**(meta["args"](trade_date)))
    if not isinstance(result, dict):
        return rows
    for sym, df in result.items():
        if isinstance(df, pd.DataFrame) and not df.empty:
            for r in _parse_czce_one(df, trade_date):
                if not r["symbol"]:
                    r["symbol"] = str(sym).upper()
                rows.append(r)
    return rows


def upsert_rank(session, rows: list[dict]) -> int:
    """批量 upsert（PK: trade_date+exchange+symbol+member+version）"""
    if not rows:
        return 0
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    # 去重（同一 key 内取最后一条）
    seen = {}
    for r in rows:
        k = (r["trade_date"], r["exchange"], r["symbol"], r["member"], r["version"])
        seen[k] = r
    rows = list(seen.values())

    stmt = pg_insert(MemberPositionRank).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=["trade_date", "exchange", "symbol", "member", "version"],
        set_={
            "rank": stmt.excluded.rank,
            "long_pos": stmt.excluded.long_pos,
            "short_pos": stmt.excluded.short_pos,
            "long_chg": stmt.excluded.long_chg,
            "short_chg": stmt.excluded.short_chg,
            "vol_pos": stmt.excluded.vol_pos,
            "src": stmt.excluded.src,
        },
    )
    session.execute(stmt)
    return len(rows)


def collect_member_position(
    session,
    trade_date: date,
    shfe_contracts: list[str] | None = None,  # 已废弃（§18.13 矩阵驱动）；保留兼容
    cffex_contracts: list[str] | None = None,
) -> dict[str, Any]:
    """§18.13 矩阵驱动采集（v1.3.2）

    遍历 EXCHANGE_COVERAGE：对 active=True 的所调用对应 func；
    active=False 的所记入 errors 并跳过（**禁止在采集器内对坏接口硬编码 try**）。
    合约列表从矩阵 symbols 自动取；调用方可传 override（CFFEX/GFEX 限定品种）。

    返回：{ trade_date, per-exchange 入库行数, errors（含 K1/K4 标记） }
    """
    stats: dict[str, Any] = {
        "trade_date": trade_date.isoformat(),
        "coverage": {ex: meta.get("active", False) for ex, meta in EXCHANGE_COVERAGE.items()},
        "errors": [],
    }
    per_ex_rows: dict[str, int] = {}
    for ex, meta in EXCHANGE_COVERAGE.items():
        if not meta.get("active"):
            # §18.13：active=False 跳过；标注 known_issue 而非硬编码 try
            ki = meta.get("known_issue", "Unknown")
            reason = meta.get("reason", "接口未激活")
            stats["errors"].append(f"{ex.lower()}: skipped ({ki} {reason})")
            per_ex_rows[ex] = 0
            continue
        # 上市日判断（GFEX 2023-11-10 之后才有数据）
        since = meta.get("since")
        if since and trade_date < since:
            stats["errors"].append(f"{ex.lower()}: skipped (pre-listing, since={since})")
            per_ex_rows[ex] = 0
            continue
        # 调 func（每个所可独立 args 生成）
        try:
            func = meta["func"]
            args_fn = meta["args"]
            parser = meta.get("parser")
            symbols = meta.get("symbols")
            # CFFEX/GFEX symbols 可被调用方 override
            if ex == "CFFEX" and cffex_contracts:
                symbols = cffex_contracts
            if ex == "SHFE" and shfe_contracts:
                symbols = shfe_contracts
            # args_fn 签名：1 参 (d) 或 2 参 (d, syms)
            import inspect

            nargs = len(inspect.signature(args_fn).parameters)
            args = args_fn(trade_date) if nargs == 1 else args_fn(trade_date, symbols)
            # 适配 CZCE 单参无 symbols
            result = func(**args)
            # 解析 dict → 标准化行
            rows: list[dict] = []
            parse_fn = globals().get(parser) if parser else None
            if isinstance(result, dict):
                for sym, df in result.items():
                    if isinstance(df, pd.DataFrame) and not df.empty:
                        if parse_fn:
                            rows.extend(parse_fn(df, trade_date, sym))
                        else:
                            logger.warning(f"[member_position] {ex} parser 缺失: {parser}")
            elif isinstance(result, pd.DataFrame) and not result.empty and parse_fn:
                rows.extend(parse_fn(result, trade_date, ""))
            n = upsert_rank(session, rows)
            session.commit()
            per_ex_rows[ex] = n
            logger.info(f"[member_position] {ex} {trade_date} 入库 {n} 行")
        except Exception as e:
            session.rollback()
            stats["errors"].append(f"{ex.lower()}: {e}")
            per_ex_rows[ex] = 0
            logger.exception(f"[member_position] {ex} 失败: {e}")
    stats["rows"] = per_ex_rows
    stats["total"] = sum(per_ex_rows.values())
    return stats
