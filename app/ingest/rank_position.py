# -*- coding: utf-8 -*-
"""会员持仓排名（龙虎榜）每日入库。

数据源（2026-09-20 用户拍板：**统一到交易所官方源**）：

| 交易所 | 源 | src |
|---|---|---|
| CZCE | `ak.get_rank_table_czce(date)` 官方全量（122 键，**只取合约级**） | `akshare:get_rank_table_czce` |
| SHFE | `ak.get_shfe_rank_table(date)` 官方全量（约 74 合约，**绝不能传 `vars_list`**） | `akshare:get_shfe_rank_table` |
| GFEX | `ak.futures_gfex_position_rank(date, vars_list)` 官方 | `akshare:futures_gfex_position_rank` |
| DCE  | `app.ingest.dce_scrapling`（Scrapling 真浏览器绕大商所瑞数动态防护） | `scrapling:dce_memberDealPosi` |
| INE  | **无免费源**，不纳入（如实标注，不做空跑） | — |
| CFFEX| 金融期货，决策 3 不纳入、不补 | — |

**统一原则**：官方源为唯一主源（交易所原始口径、覆盖全合约）；**新浪通道已退役**——
新浪对 DCE 返回非大商所品种（历史 393 行 `src='sina_cot'` 错标即此因）、对 SHFE 只给 25 个主力合约
（官方给 74 个）、对 CZCE 用 4 位月份（官方是 3 位），同一合约两套 symbol，是库内 `src` 混杂的根源。

各所解析**复用** `app.ingest.member_position` 的同名解析器（同 src、同字段映射），保证矩阵链路
（手动 `POST /position/collect/{date}`）与本链路（每日 17:30 自动）写出的行完全同构、可互相幂等补齐。

symbol 口径（2026-09-20 统一）
-----------------------------
入库 `symbol` 一律为**标准码 = 品种大写 + YYMM 四位**（``AP2701`` / ``CU2611``）。
各所官方原生写法不统一（CZCE 3 位 ``AP701``、SHFE/DCE/GFEX 小写 ``cu2611``），
而新浪源统一 4 位（``AP2701``）——曾造成同一合约两套 symbol、跨源 join 全对不上。
归一在 ``member_position._std_symbol`` 收口（本模块复用其解析器，故自动继承）；
写法 ↔ 标准码的映射表见 ``contract_code_map``（``db/init/14_contract_code.sql``）。

历史回填走 ``app.ingest.backfill_rank``（同源同口径，可反复跑）。

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

# 新浪通道（match_main_contract + futures_hold_pos_sina）**已退役**。
# 保留空表与下方循环结构，仅为将来官方源不可用时能快速切回兜底。
# 退役理由（2026-09-20 用户拍板「官方源为准」）：
#   · DCE ：新浪对 dce 返回**非大商所品种** → 曾写入 393 行 `sina_cot` 错标；
#   · SHFE：新浪只给 **25 个主力合约**，官方给 74 个；
#   · CZCE：新浪用 **4 位月份**（AP2701），官方用 **3 位**（AP701），同合约两套 symbol。
_AK_EXCHANGE: dict[str, str] = {}

# 走 Scrapling 专用通道的交易所（不进新浪循环）
_SCRAPLING_EXCHANGES = ("DCE",)

# 走 akshare 官方直连接口的交易所（不进新浪循环）
_OFFICIAL_RANK_EXCHANGES = ("SHFE", "CZCE", "GFEX")

# 无免费源、明确不纳入的交易所（如实标注，不做无谓空跑）
_NO_SOURCE_EXCHANGES = {
    "INE": "无免费源（新浪对 ine 无有效数据，交易所无公开排名接口）",
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
        rank      = EXCLUDED.rank,
        src       = EXCLUDED.src
    """
)
# 注：src 一并更新——同一 PK 被不同源覆盖时 src 应如实反映最后写入者。
# 历史 DCE 污染的隐患之一正是「数值被新源覆盖、src 仍是旧源」，两边脱节。


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


def _run_dce_scrapling(date: _dt.date) -> object:
    """DCE 专用通道：Scrapling 真浏览器抓官方批量接口并入库。

    Scrapling 未就绪（如容器内未装依赖/浏览器）时**降级标注**并返回说明字符串，
    不抛异常打断其他交易所——与 member_position 的 §18.13 红线一致。
    """
    try:
        from app.ingest import dce_scrapling as DS
    except Exception as e:  # noqa: BLE001
        return f"跳过：dce_scrapling 导入失败（{type(e).__name__}）"

    if not DS.available():
        return ("跳过：scrapling 未就绪（容器内请构建时启用 WITH_SCRAPLING，"
                "或由宿主机 DCE_scrapling_crawler.py 采集）")
    try:
        st = DS.run(date)
    except Exception as e:  # noqa: BLE001
        return f"失败: {type(e).__name__}: {e}"
    if not isinstance(st, dict) or not st.get("rows"):
        return f"空数据（{st}）"
    return st.get("rows")


def _run_shfe_official(date: _dt.date) -> object:
    """SHFE 官方直连通道：一次取回当日**全部合约**的会员持仓排名。

    为什么不用新浪：新浪 ``match_main_contract('shfe')`` 只给 25 个主力合约，
    而本项目覆盖 SHFE 全合约；官方接口一次给约 74 个，且与库里既有
    ``src='akshare:get_shfe_rank_table'`` 的行**同源同口径**。

    **关键：绝不传 ``vars_list``** —— ``ak.get_shfe_rank_table(date, vars_list=None)``
    传了只回少量品种（历史 K1「静默空 dict」的假阴性根因即此），不传才返回全量。

    解析复用 ``member_position._parse_shfe_one``（同 src、同字段映射），保证矩阵链路
    与本链路写出的行完全同构、可互相幂等补齐。
    失败降级标注并返回说明字符串，不抛异常打断其他交易所。
    """
    try:
        import akshare as ak  # type: ignore
    except Exception as e:  # noqa: BLE001
        return f"跳过：akshare 导入失败（{type(e).__name__}）"
    try:
        from app.ingest.member_position import _parse_shfe_one
    except Exception as e:  # noqa: BLE001
        return f"跳过：SHFE 解析器导入失败（{type(e).__name__}）"

    try:
        table = ak.get_shfe_rank_table(date=date.strftime("%Y%m%d"))
    except Exception as e:  # noqa: BLE001
        return f"失败: {type(e).__name__}: {e}"
    if not isinstance(table, dict) or not table:
        return "空数据（SHFE 官方接口未返回合约，可能非交易日）"

    rows: list[dict] = []
    for contract, df in table.items():
        if df is None or len(df) == 0:
            continue
        rows.extend(_parse_shfe_one(df, date, str(contract)))
    if not rows:
        return "空数据（返回合约但解析后无有效行）"
    return save_rows(rows)


def _run_czce_official(date: _dt.date) -> object:
    """CZCE 官方直连通道：一次取回当日全部**合约级**会员持仓排名。

    ``ak.get_rank_table_czce(date)`` 实测返回 **122 个键**，含两个语义不同的层级：
      · 合约级 101 个（``AP701`` / ``TA705``，带 3 位月份）—— 单合约排名；
      · **品种级 21 个**（``AP`` / ``PTA``，无月份）—— 该品种**全合约合计**的会员排名。
    解析复用 ``member_position._parse_czce_one``，其 ``_is_variety_level`` 会**丢弃品种级行**，
    只入库合约级（本项目 ``symbol`` 语义即合约代码，见 ``app/features/position_factors.py``）；
    其 ``_std_symbol`` 再把官方 3 位（``AP701``）补成标准码 4 位（``AP2701``）。

    为什么不用新浪：新浪用 4 位月份（``AP2701``），与官方 3 位（``AP701``）同合约两套写法，
    且只覆盖 24 个主力合约（官方 101+ 个合约）。
    """
    try:
        import akshare as ak  # type: ignore
    except Exception as e:  # noqa: BLE001
        return f"跳过：akshare 导入失败（{type(e).__name__}）"
    try:
        from app.ingest.member_position import _parse_czce_one
    except Exception as e:  # noqa: BLE001
        return f"跳过：CZCE 解析器导入失败（{type(e).__name__}）"

    try:
        table = ak.get_rank_table_czce(date=date.strftime("%Y%m%d"))
    except Exception as e:  # noqa: BLE001
        return f"失败: {type(e).__name__}: {e}"
    if not isinstance(table, dict) or not table:
        return "空数据（CZCE 官方接口未返回，可能非交易日）"

    rows: list[dict] = []
    for contract, df in table.items():
        if df is None or len(df) == 0:
            continue
        rows.extend(_parse_czce_one(df, date, str(contract)))
    if not rows:
        return "空数据（返回键但解析后无有效行）"
    return save_rows(rows)


def _run_gfex_official(date: _dt.date) -> object:
    """GFEX 官方直连通道：广期所（SI 工业硅 / LC 碳酸锂 / PS 多晶硅）会员持仓排名。

    ``ak.futures_gfex_position_rank(date, vars_list)`` 需显式传品种列表；
    实测返回均为**合约级**键（``si2611`` 等 4 位月份），无品种级形态。

    为什么不用新浪：新浪对本所同样只覆盖少量主力合约，且与官方 symbol 口径不统一。
    解析复用 ``member_position._parse_gfex_one``（同 src、同字段映射）。
    """
    try:
        import akshare as ak  # type: ignore
    except Exception as e:  # noqa: BLE001
        return f"跳过：akshare 导入失败（{type(e).__name__}）"
    try:
        from app.ingest.member_position import EXCHANGE_COVERAGE, _parse_gfex_one
    except Exception as e:  # noqa: BLE001
        return f"跳过：GFEX 解析器导入失败（{type(e).__name__}）"

    syms = list(EXCHANGE_COVERAGE.get("GFEX", {}).get("symbols") or ["SI", "LC", "PS"])
    try:
        table = ak.futures_gfex_position_rank(
            date=date.strftime("%Y%m%d"), vars_list=syms
        )
    except Exception as e:  # noqa: BLE001
        return f"失败: {type(e).__name__}: {e}"
    if not isinstance(table, dict) or not table:
        return "空数据（GFEX 官方接口未返回，可能非交易日或品种未上市）"

    rows: list[dict] = []
    for contract, df in table.items():
        if df is None or len(df) == 0:
            continue
        rows.extend(_parse_gfex_one(df, date, str(contract)))
    if not rows:
        return "空数据（返回键但解析后无有效行）"
    return save_rows(rows)


def run(date: _dt.date | None = None, exchanges: list[str] | None = None) -> dict:
    """抓取并入库指定交易日的会员持仓排名。返回 {exchange: 入库行数/错误}。

    exchanges：交易所缩写列表。**默认按官方通道全量拉**：
      DCE → Scrapling 官网；SHFE / CZCE / GFEX → akshare 官方直连。
      INE 无免费源、CFFEX 属金融期货（决策 3），均不纳入、不补。
    """
    date = date or _dt.date.today()
    date_str = date.strftime("%Y%m%d")
    product_map = _build_product_map()
    if exchanges is None:
        # 新浪通道已退役（_AK_EXCHANGE 为空），这里直接展开官方通道集合，
        # 只保留本项目 config.main_contracts 覆盖的交易所。
        exchanges = []
        covered = set(product_map.values())
        for special in (*_SCRAPLING_EXCHANGES, *_OFFICIAL_RANK_EXCHANGES):
            if special in covered and special not in exchanges:
                exchanges = list(exchanges) + [special]

    summary: dict[str, object] = {}
    for exch in exchanges:
        # ---- DCE：Scrapling 专用通道（不进新浪循环）----
        if exch in _SCRAPLING_EXCHANGES:
            summary[exch] = _run_dce_scrapling(date)
            continue
        # ---- SHFE / CZCE / GFEX：akshare 官方直连全量接口（不进新浪循环）----
        if exch in _OFFICIAL_RANK_EXCHANGES:
            if exch == "SHFE":
                summary[exch] = _run_shfe_official(date)
            elif exch == "CZCE":
                summary[exch] = _run_czce_official(date)
            else:
                summary[exch] = _run_gfex_official(date)
            continue
        # ---- 无免费源的交易所：如实标注，不做空跑 ----
        if exch in _NO_SOURCE_EXCHANGES:
            summary[exch] = f"跳过：{_NO_SOURCE_EXCHANGES[exch]}"
            continue
        # ---- 以下为已退役的新浪通道（_AK_EXCHANGE 恒空 → 实际不可达）----
        # 保留结构，供将来官方源不可用时切回兜底。
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
