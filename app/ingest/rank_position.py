# -*- coding: utf-8 -*-
"""会员持仓排名（龙虎榜）每日入库。

数据源（决策 1：改用新浪财经期货成交持仓）：
- akshare `futures_hold_pos_sina(symbol, contract, date)` 按合约拉「成交量 / 多单持仓 / 空单持仓」三类排名
- akshare `match_main_contract(symbol=<交易所>)` 取该交易所全部商品主力合约代码（逗号串）

仅商品期货（CZCE/SHFE/DCE/GFEX/INE）；金融期货（CFFEX：IF/IH/IC/IM/T/TF/TS/TL）不纳入、不补（决策 3）。
逐合约→主力合约映射自 `config.main_contracts`，只入库本项目覆盖的品种。

单交易所失败不影响其他所；落库表 `member_position_rank`（与既有表结构/口径一致）。
"""
from __future__ import annotations

import datetime as _dt
from typing import Iterable

from app.core.db import get_engine
from app.core.logging import logger
from sqlalchemy import text

VERSION = "v1.0"
SRC = "sina_cot"

# akshare 交易所代码 ↔ 交易所缩写（match_main_contract 入参）
_AK_EXCHANGE = {
    "CZCE": "czce",
    "SHFE": "shfe",
    "DCE": "dce",
    "GFEX": "gfex",
    "INE": "ine",
    # CFFEX 不纳入（决策 3）：futures_hold_pos_sina 仅商品期货，且金融不补
}

# 三个持仓维度 → 对应列名（按返回 DataFrame 列顺序，避免依赖中文列名）
_CAT_VOLUME = "成交量"
_CAT_LONG = "多单持仓"
_CAT_SHORT = "空单持仓"

_INSERT = text(
    """
    INSERT INTO member_position_rank
        (trade_date, exchange, symbol, member, rank,
         long_pos, short_pos, long_chg, short_chg, vol_pos, src, version)
    VALUES (:trade_date, :exchange, :symbol, :member, :rank,
            :long_pos, :short_pos, :long_chg, :short_chg, :vol_pos, :src, :version)
    ON CONFLICT (trade_date, exchange, symbol, member, version) DO UPDATE SET
        long_pos  = EXCLUDED.long_pos,
        short_pos = EXCLUDED.short_pos,
        long_chg  = EXCLUDED.long_chg,
        short_chg = EXCLUDED.short_chg,
        vol_pos   = EXCLUDED.vol_pos,
        rank      = EXCLUDED.rank
    """
)


def _num(v) -> int | None:
    if v is None:
        return None
    s = str(v).replace(",", "").replace(" ", "").replace("\u3000", "")
    if s in ("", "-", "—", "nan", "NaN"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _build_product_map() -> dict[str, str]:
    """品种码（大写）→ 交易所；用于把主力合约代码反查回本项目品种。

    例：RB2510 → 前缀 RB → SHFE。取最长匹配，避免 I（铁矿石）被 IF 之类前缀误吞。
    """
    from app.core.config import get_settings

    m: dict[str, str] = {}
    for spec in get_settings().main_contracts:
        m[spec.product.upper()] = spec.exchange
    return m


def _product_of(contract: str, product_map: dict[str, str]) -> str | None:
    c = contract.upper()
    best: str | None = None
    for prod in product_map:
        if c.startswith(prod):
            if best is None or len(prod) > len(best):
                best = prod
    return best


def _main_contracts_of(exchange: str) -> list[str]:
    """新浪主力合约代码串 → 列表。返回该交易所全部商品主力合约。"""
    import akshare as ak  # type: ignore

    code = _AK_EXCHANGE[exchange]
    raw = ak.match_main_contract(symbol=code)
    if not raw:
        return []
    if isinstance(raw, str):
        return [x.strip() for x in raw.split(",") if x.strip()]
    # 防御：若未来返回 DataFrame，取首项列
    try:
        return [str(x).strip() for x in list(raw) if str(x).strip()]
    except Exception:  # noqa: BLE001
        return []


def _fetch_contract(contract: str, date_str: str) -> list[dict]:
    """拉单个主力合约的三类持仓排名，按会员合并成一行（long/short/vol 同列，避免 PK 冲突）。"""
    import akshare as ak  # type: ignore

    # 名次/会员为各维度共有；按会员名聚合，分别填入 long/short/vol
    merged: dict[str, dict] = {}

    def _ensure(member: str) -> dict:
        if member not in merged:
            merged[member] = {
                "member": member, "rank": None,
                "long_pos": None, "long_chg": None,
                "short_pos": None, "short_chg": None,
                "vol_pos": None,
            }
        return merged[member]

    def _rows(cat: str) -> Iterable[dict]:
        df = ak.futures_hold_pos_sina(symbol=cat, contract=contract, date=date_str)
        if df is None or len(df) == 0:
            return []
        cols = list(df.columns)
        # 列序：名次, 会员简称, 数值(成交量/多单/空单), 比上交易增减
        rank_i, member_i = 0, 1
        val_i, chg_i = 2, 3
        for _, r in df.iterrows():
            try:
                rank = _num(r.iloc[rank_i])
                member = str(r.iloc[member_i]).strip()
                val = _num(r.iloc[val_i])
                chg = _num(r.iloc[chg_i]) if len(cols) > chg_i else None
            except Exception:  # noqa: BLE001
                continue
            if not member:
                continue
            yield {"rank": rank, "member": member, "val": val, "chg": chg, "cat": cat}

    for cat in (_CAT_VOLUME, _CAT_LONG, _CAT_SHORT):
        for rec in _rows(cat):
            row = _ensure(rec["member"])
            if rec["rank"] is not None:
                row["rank"] = rec["rank"]
            if cat == _CAT_VOLUME:
                row["vol_pos"] = rec["val"]
            elif cat == _CAT_LONG:
                row["long_pos"], row["long_chg"] = rec["val"], rec["chg"]
            elif cat == _CAT_SHORT:
                row["short_pos"], row["short_chg"] = rec["val"], rec["chg"]

    return list(merged.values())


def save_rows(rows: list[dict]) -> int:
    if not rows:
        return 0
    eng = get_engine()
    n = 0
    with eng.begin() as conn:
        for r in rows:
            conn.execute(_INSERT, r)
            n += 1
    return n


def run(date: _dt.date | None = None, exchanges: list[str] | None = None) -> dict:
    """抓取并入库指定交易日的会员持仓排名。返回 {exchange: 入库行数/错误}。

    exchanges：走新浪 match_main_contract 的商品期货交易所列表（默认全部商品所）。
    """
    date = date or _dt.date.today()
    date_str = date.strftime("%Y%m%d")
    product_map = _build_product_map()
    if exchanges is None:
        exchanges = [e for e in _AK_EXCHANGE if e in product_map.values()] or list(_AK_EXCHANGE)

    summary: dict[str, object] = {}
    for exch in exchanges:
        if exch not in _AK_EXCHANGE:
            summary[exch] = "跳过（非商品交易所，决策 3 不纳入）"
            continue
        try:
            contracts = _main_contracts_of(exch)
            if not contracts:
                summary[exch] = "空数据"
                continue
            rows: list[dict] = []
            for c in contracts:
                prod = _product_of(c, product_map)
                if prod is None:
                    continue  # 非本项目覆盖品种，跳过
                ex = product_map[prod]
                try:
                    for rec in _fetch_contract(c, date_str):
                        rows.append({
                            "trade_date": date,
                            "exchange": ex,
                            "symbol": c,            # 主力合约代码（如 RB2510）
                            "member": rec["member"],
                            "rank": rec["rank"] or 0,
                            "long_pos": rec["long_pos"],
                            "short_pos": rec["short_pos"],
                            "long_chg": rec["long_chg"],
                            "short_chg": rec["short_chg"],
                            "vol_pos": rec["vol_pos"],
                            "src": SRC,
                            "version": VERSION,
                        })
                except Exception as ce:  # noqa: BLE001
                    logger.warning(f"[rank_position] {exch} {c} 失败: {ce}")
            n = save_rows(rows)
            summary[exch] = n
        except Exception as e:  # noqa: BLE001
            summary[exch] = f"失败: {type(e).__name__}: {e}"
    return summary


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    d = _dt.date.today()
    if len(sys.argv) > 1:
        d = _dt.datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    print("抓取日期:", d)
    print(run(d))
