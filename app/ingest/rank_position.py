# -*- coding: utf-8 -*-
"""会员持仓排名（龙虎榜）每日入库。

数据源（2026-09-20 用户拍板：**统一到交易所官方源**）：

| 交易所 | 源 | src |
|---|---|---|
| CZCE | `ak.get_rank_table_czce(date)` 官方全量（122 键，**只取合约级**） | `akshare:get_rank_table_czce` |
| SHFE | `ak.get_shfe_rank_table(date)` 官方全量（约 74 合约，**绝不能传 `vars_list`**） | `akshare:get_shfe_rank_table` |
| GFEX | `ak.futures_gfex_position_rank(date, vars_list)` 官方 | `akshare:futures_gfex_position_rank` |
| DCE  | **主源** `app.ingest.dce_scrapling`（真浏览器绕瑞数）＋**缺口自动补齐** `app.ingest.dce_sina`（新浪按单合约） | `scrapling:dce_memberDealPosi` / `sina:futures_hold_pos_sina` |
| INE  | **无免费源**，不纳入（如实标注，不做空跑） | — |
| CFFEX| 金融期货，决策 3 不纳入、不补 | — |

**统一原则**：官方源仍为唯一主源（交易所原始口径、覆盖全合约）。
2026-11-10 用户拍板 ①（CL-20261110-11）对 **DCE 单所**开了例外：官网全站瑞数
412、Scrapling 浏览器通道在三路环境实测不可用，2026-11-09 只采到 V/Y 共 12 合约；
允许 ``ak.futures_hold_pos_sina`` **按单个明确合约**查询作补齐/兜底。

⚠ 例外只到「单合约查询」为止，历史退役结论依然成立：
  · 绝不用 ``ak.match_main_contract('dce')`` 批量拉合约清单（会返回非 DCE 品种，
    历史 393 行 `src='sina_cot'` 污染即此因），候选合约一律从库内 DCE 真实合约取；
  · SHFE 新浪只给 25 个主力合约（官方 74 个）、CZCE 用 4 位月份（官方 3 位），
    这两个所仍只走官方接口；
  · 新浪补数只 INSERT 库内缺失合约，不删不改官网行，见 ``app.ingest.dce_sina``。

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

from app.core.db import get_engine
from app.core.logging import logger
from app.ingest.blocked_varieties import is_blocked_symbol
from sqlalchemy import text

# 旧 ``SRC = "sina_cot"`` 与 ``match_main_contract + 按会员合并`` 的新浪批量通道
# 已于 2026-09-20 退役，2026-11-10 清理删除（CL-20261110-11）：
# 它按会员名合并三表会写出 31 行/合约且 member 与 short_pos 错配，
# 批量拉合约清单还会返回非 DCE 品种（393 行污染）。DCE 现走 _run_dce 双通道。

# 走 Scrapling 主源 + 新浪缺口补齐的交易所
_SCRAPLING_EXCHANGES = ("DCE",)

# 走 akshare 官方直连接口的交易所
_OFFICIAL_RANK_EXCHANGES = ("SHFE", "CZCE", "GFEX")

# 无免费源、明确不纳入的交易所（如实标注，不做无谓空跑）
_NO_SOURCE_EXCHANGES = {
    "INE": "无免费源（新浪对 ine 无有效数据，交易所无公开排名接口）",
}

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


def _build_product_map() -> dict[str, str]:
    """品种码（大写）→ 交易所；用于把主力合约代码反查回本项目品种。

    例：RB2510 → 前缀 RB → SHFE。取最长匹配，避免 I（铁矿石）被 IF 之类前缀误吞。
    """
    from app.core.config import get_settings

    m: dict[str, str] = {}
    for spec in get_settings().main_contracts:
        m[spec.product.upper()] = spec.exchange
    return m


def save_rows(rows: list[dict]) -> int:
    if not rows:
        return 0
    eng = get_engine()
    n = 0
    for r in rows:
        if is_blocked_symbol(r.get("symbol")):
            continue
        with eng.begin() as conn:
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


def _run_dce(date: _dt.date) -> dict:
    """DCE 双通道：官网 Scrapling **主源** + 新浪单合约**缺口自动补齐**（CL-20261110-11）。

    官网全站瑞数 412，Scrapling 真浏览器通道在生产容器常不可用；即便可用也可能
    像 2026-11-09 那样只采到 V/Y 共 12 合约（2 品种），或像 11-06 那样个别远月
    合约写入残缺值。因此采集后一律以**主表聚合**核对品种覆盖：
    低于近 30 交易日中位数 → 调 ``dce_sina.backfill_missing`` 只补库内缺失合约。

    官网完全不可用时覆盖为 0，同样触发补齐（即新浪同时充当兜底主通道）。
    单所失败不抛异常打断其他交易所（§18.13 红线）。
    """
    try:
        from app.ingest import dce_sina
    except Exception as e:  # noqa: BLE001
        return {"error": f"dce_sina 导入失败 {type(e).__name__}: {e}",
                "official": _run_dce_scrapling(date)}

    official = _run_dce_scrapling(date)
    before = dce_sina.day_coverage(date)
    expected = dce_sina.expected_varieties()
    out: dict[str, object] = {
        "official": official,
        "varieties_before": before["varieties"],
        "expected_varieties": expected,
    }
    if not expected or before["varieties"] >= expected:
        out["sina_backfill"] = "skipped(官网覆盖完整)"
        return out
    try:
        st = dce_sina.backfill_missing(date, verify_existing=True, apply=True)
        out["sina_backfill"] = st
        out["varieties_after"] = st["after"]["varieties"]
    except Exception as e:  # noqa: BLE001
        out["sina_backfill"] = f"失败: {type(e).__name__}: {e}"
        logger.exception("[rank_position] DCE 新浪单合约补齐失败")
    return out


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
    product_map = _build_product_map()
    if exchanges is None:
        # 展开官方通道集合（DCE 另有新浪缺口补齐），只保留 config.main_contracts 覆盖的所
        exchanges = []
        covered = set(product_map.values())
        for special in (*_SCRAPLING_EXCHANGES, *_OFFICIAL_RANK_EXCHANGES):
            if special in covered and special not in exchanges:
                exchanges = list(exchanges) + [special]

    summary: dict[str, object] = {}
    for exch in exchanges:
        # ---- DCE：官网 Scrapling 主源 + 新浪单合约缺口补齐 ----
        if exch in _SCRAPLING_EXCHANGES:
            summary[exch] = _run_dce(date)
            continue
        # ---- SHFE / CZCE / GFEX：akshare 官方直连全量接口 ----
        if exch in _OFFICIAL_RANK_EXCHANGES:
            if exch == "SHFE":
                summary[exch] = _run_shfe_official(date)
            elif exch == "CZCE":
                summary[exch] = _run_czce_official(date)
            else:
                summary[exch] = _run_gfex_official(date)
            continue
        # ---- 无免费源 / 非本项目覆盖：如实标注，不做空跑 ----
        if exch in _NO_SOURCE_EXCHANGES:
            summary[exch] = f"跳过：{_NO_SOURCE_EXCHANGES[exch]}"
            continue
        summary[exch] = "跳过（非商品交易所，决策 3 不纳入）"
    return summary


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    d = _dt.date.today()
    if len(sys.argv) > 1:
        d = _dt.datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    print("抓取日期:", d)
    print(run(d))
