"""§18.4（v1.3.2）M6a：会员持仓排名（龙虎榜）采集器

数据源（§18.13 EXCHANGE_COVERAGE 矩阵驱动；2026-09-20 起统一为**交易所官方源**）：
- CZCE  : ak.get_rank_table_czce(date)         ✅ 返回 122 键 = 101 合约级 + 21 品种级
          ⚠️ **只取合约级**：品种级键（`AP` / `PTA`，无月份 = 该品种全合约合计排名）
             由 `_is_variety_level` 丢弃；否则下游按合约聚合的持仓因子会被品种级主导
- CFFEX : ak.get_cffex_rank_table(date, vars)  ✅ 496 行/日
- GFEX  : ak.futures_gfex_position_rank(date, vars) ✅ v1.3.2 新增（SI/LC/PS，2023-11-10 起）
- DCE   : app.ingest.dce_scrapling（Scrapling 真浏览器过瑞数防护）✅ 全合约/日
          （原 K1「akshare futures_dce_position_rank → BadZipFile」根因是官网
           瑞数动态防护，非接口下线；详见该模块 docstring）
- SHFE  : ak.get_shfe_rank_table(date)         ✅ 一次全量（约 74 合约/日）
          （原 K1「静默空 dict」是**假阴性**：根因是调用时传了 `vars_list`，
           不是接口坏。**不传 vars_list** 即返回当日全部合约，详见矩阵注释）
- INE   : 无持仓/库存接口                       ❌ K4: 硬上限 68/73

**§18.13 红线**（PRD v1.3.2 实现约束）：
- EXCHANGE_COVERAGE 矩阵化（active/func/symbols/known_issue/fallbacks）
- 发现接口坏时**降级到 active=False** + 标注 K1（不硬编码 try 跳过）
- akshare 修复后：仅需翻转 active=True + 补 symbols 即可一键扩展覆盖
- 不允许在采集器内对 SHFE/DCE 写硬编码 try 豁免逻辑

防前视（§18.4）：T 日盘后调度，as_of = T（latency 1 自然生效）。

symbol 口径（2026-09-20 统一）
-----------------------------
入库的 ``symbol`` 一律为**标准码 = 品种大写 + YYMM 四位**（``AP2701`` / ``CU2611``），
出口统一走 ``_std_symbol`` → ``app.core.symbol_code.to_std``。

原因：各所官方原生写法不统一（CZCE 3 位 ``AP701``、SHFE/DCE/GFEX 小写 ``cu2611``），
而新浪源统一 4 位（``AP2701``）——曾导致同一合约两套 symbol 并存、跨源 join 全对不上。
各源写法 ↔ 标准码的映射登记在 ``contract_code_map``（见 ``db/init/14_contract_code.sql``）。
"""
from __future__ import annotations

from datetime import date
from typing import Any, Callable

import pandas as pd

from app.core import symbol_code as SC
from app.core.logging import logger
from app.models import MemberPositionRank


# §18.13 EXCHANGE_COVERAGE 矩阵（v1.3.2）
# - active=False 的所：仅记录到 errors，不调用 func（避免对坏接口硬编码 try）
# - symbols：active=False 时可省（即便提供也不调用）
# - known_issue：禁用原因（K1 接口坏 / K4 无接口）
# - fallbacks：可选项，akshare 修复后替代 func 的备选接口列表
def _dce_fetch(trade_date: date) -> dict:
    """DCE 矩阵入口：延迟导入 Scrapling 采集器（可选依赖，勿在模块顶层 import）

    依赖缺失/无浏览器时抛 ``DceUnavailable``，由 collect_member_position 记入 errors
    ——符合 §18.13「降级 + 标注而非硬编码 try」的红线。
    """
    from app.ingest.dce_scrapling import fetch_dce_rank

    return fetch_dce_rank(trade_date)


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
            # 决策 3：金融期货不纳入、CFFEX 不补 → 关闭（active=False + 标注 K5）
            "active": False,
            "func": ak.get_cffex_rank_table,
            "args": lambda d, syms: {"date": d.strftime("%Y%m%d"), "vars_list": syms},
            "symbols": ["IF", "IC", "IM", "IH", "T", "TF", "TS", "TL"],
            "parser": "_parse_cffex_one",
            "known_issue": "K5",
            "reason": "金融期货（CFFEX）不纳入本项目（决策 3），不补龙虎榜",
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
            # 原 K1（akshare futures_dce_position_rank → BadZipFile）根因已定位：
            # 大商所官网套了**瑞数动态防护**，纯 HTTP 客户端一律 412。
            # 改由 app.ingest.dce_scrapling（Scrapling 真浏览器过挑战）供给。
            # 依赖缺失时抛 DceUnavailable → 走下面的 errors 分支（不硬编码 try 豁免）。
            "active": True,
            "func": _dce_fetch,     # 延迟导入的 Scrapling 采集器
            "args": lambda d: {"trade_date": d},
            "symbols": None,        # 接口一次性返回当日全部合约（约 94~110 个）
            "parser": "_parse_dce_one",
            "known_issue": None,
            "src": "scrapling:dce_memberDealPosi",
            "fallbacks": ["app.ingest.dce_scrapling", "宿主机 DCE_scrapling_crawler.py"],
        },
        "SHFE": {
            # 原 K1「v1.18.94 静默空 dict」经实证是**假阴性**，根因是**调用姿势**而非接口坏：
            #   ak.get_shfe_rank_table(date, vars_list=None)
            #   · 传 vars_list  → 只回少量品种（且按品种码猜合约，语义不稳）
            #   · **不传 vars_list → 当日全部合约（约 74 键，键为合约代码 cu2611/cu2610/…）**
            # 返回 DataFrame 列：symbol, rank, long_party_name, short_party_name,
            # vol_party_name, long_open_interest, short_open_interest, long_open_interest_chg,
            # short_open_interest_chg, vol, vol_chg, variety —— 与 _parse_shfe_one 完全对齐。
            # 故此处 args **只传 date**，绝不下传 vars_list（matrix 的 symbols 保持 None）。
            "active": True,
            "func": ak.get_shfe_rank_table,
            "args": lambda d: {"date": d.strftime("%Y%m%d")},
            "symbols": None,        # 一次返回当日全部合约（约 74 个）
            "parser": "_parse_shfe_one",
            "known_issue": None,
            "src": "akshare:get_shfe_rank_table",
            "fallbacks": [],
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


def _member_name(*cands) -> str:
    """从候选列里取第一个**有效**会员名。

    ⚠️ 不能用 ``a or b or ""``：``float('nan')`` 在 Python 里是 **truthy**，
    ``str(nan)`` 会得到字符串 ``'nan'`` 并被当成会员名入库（幽灵行）。
    """
    for v in cands:
        if v is None:
            continue
        if isinstance(v, float) and pd.isna(v):
            continue
        s = str(v).strip()
        if s and s.lower() not in ("nan", "none", "null"):
            return s
    return ""


def _is_summary_row(rank: int, member: str) -> bool:
    """识别「合约合计」行（20 名明细后追加的汇总行）。

    交易所官方表在 20 名明细后**追加一行合计**：``rank=999``（哨兵）、会员名空白。
    （SHFE/CFFEX 官方接口实测如此；CZCE 无此形态 —— 但 CZCE 另有**品种级键**，
     见 ``_is_variety_level``，那是另一种同样必须过滤的行。）
    不得入库——否则下游按 member 聚合会多出一条幽灵行。
    """
    return rank >= 999 or not str(member).strip()


def _is_variety_level(symbol: str) -> bool:
    """识别「品种级」行：symbol 是**纯品种代码、无合约月份**。

    ``ak.get_rank_table_czce`` 一次返回 **122 个键**，含两个语义不同的层级：
      · **合约级** 101 个 —— ``AP701`` / ``TA705``（带 3 位月份），单合约的会员排名；
      · **品种级**  21 个 —— ``AP`` / ``PTA``（**无月份**），该品种**全部合约合计**的会员排名。

    两者混进同一 ``symbol`` 列会让下游按合约聚合的持仓因子被品种级主导
    （品种级量级远大于单合约：如 FG 品种级 top20 多单和约 130 万手）。
    本项目 ``member_position_rank`` 的语义是**合约级**（见 ``app/features/position_factors.py``），
    故品种级行**必须丢弃**。

    判据：合约代码必然含月份数字；纯字母即品种级。
    （实测：CZCE 官方 122 键中有 21 个此形态；DCE/SHFE/GFEX/CFFEX 均无。）
    """
    s = str(symbol or "").strip()
    return bool(s) and not any(ch.isdigit() for ch in s)


def _std_symbol(raw: str, exchange: str, trade_date: date, drop_variety: bool = True) -> str:
    """交易所原生合约码 → **标准码**（品种大写 + YYMM 四位，见 ``app.core.symbol_code``）。

    ⚠️ 这是本项目 symbol 口径的**唯一收口点**，五个解析器都必须过这里。

    为什么必须归一：五所官方原生写法不统一，而新浪源统一用 4 位 ——
      官方 CZCE ``AP701``（3 位）vs 新浪 ``AP2701``（4 位）→ **同一合约两套 symbol**，
      跨源 join / 跨所聚合全部对不上（2026-09-20 实测确认）。
    归一后全库只有一种写法，映射关系登记在 ``contract_code_map``。

    ``ref_date`` 必须传**交易日**：郑商所 3 位码的「年」只有个位（十年一循环），
    要靠当天的年月才能补全（``AP701`` + 2026-09 → ``AP2701``）。
    """
    if drop_variety and _is_variety_level(raw):
        return ""
    return SC.to_std(raw, exchange=exchange, ref_date=trade_date)


def _parse_czce_one(df: pd.DataFrame, trade_date: date, symbol_hint: str = "") -> list[dict]:
    """郑商所单 DataFrame → 标准化行（§18.4：v1.0 字段映射）

    ⚠️ 只保留**合约级**：``get_rank_table_czce`` 的品种级键（``AP``/``PTA``）必须过滤，
    详见 ``_is_variety_level`` 的说明。

    ⚠️ symbol 出口统一为标准码（``_std_symbol``）：官方原生是 3 位（``AP701``），
    入库为 4 位（``AP2701``），与新浪/其他四所对齐。
    """
    rows = []
    for _, r in df.iterrows():
        try:
            symbol = _std_symbol(_member_name(r.get("symbol"), symbol_hint), "CZCE", trade_date)
            if not symbol:
                continue
            rank = _coerce_int(r.get("rank"))
            member = _member_name(r.get("long_party_name"), r.get("vol_party_name"))
            if _is_summary_row(rank, member):
                continue
            rows.append(
                {
                    "trade_date": trade_date,
                    "exchange": "CZCE",
                    "symbol": symbol,
                    "member": member,
                    "rank": rank,
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


def _parse_shfe_one(df: pd.DataFrame, trade_date: date, symbol_hint: str = "") -> list[dict]:
    """上期所单合约 DataFrame（列: rank, long_party_name, long_open_interest, ...）

    契约与 `app.ingest.rank_position._run_shfe_official` 共用（同 args 姿势、同 src），
    两条链路写出的行完全同构，可互相幂等补齐。
    """
    rows = []
    for _, r in df.iterrows():
        try:
            symbol = _std_symbol(_member_name(r.get("symbol"), symbol_hint), "SHFE", trade_date)
            if not symbol:
                continue
            rank = _coerce_int(r.get("rank"))
            member = _member_name(r.get("long_party_name"), r.get("vol_party_name"))
            if _is_summary_row(rank, member):
                continue
            rows.append(
                {
                    "trade_date": trade_date,
                    "exchange": "SHFE",
                    "symbol": symbol,
                    "member": member,
                    "rank": rank,
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
    """中金所（CFFEX 字段与 SHFE 同构，按需适配）

    注：CFFEX 经 ``vars_list`` 按合约拉取，返回即合约级、实测无品种级形态，
    故此处不做 ``_is_variety_level`` 过滤（避免 symbol_hint 为品种代码时误杀）。
    """
    rows = []
    for _, r in df.iterrows():
        try:
            rank = _coerce_int(r.get("rank"))
            member = _member_name(r.get("long_party_name"), r.get("vol_party_name"))
            if _is_summary_row(rank, member):
                continue
            rows.append(
                {
                    "trade_date": trade_date,
                    "exchange": "CFFEX",
                    # drop_variety=False：CFFEX 经 vars_list 按合约拉取，symbol_hint 是
                    # 品种码（IF/T…），走品种级过滤会把整批误杀（详见上方 docstring）。
                    "symbol": _std_symbol(_member_name(r.get("symbol"), symbol_hint),
                                          "CFFEX", trade_date, drop_variety=False),
                    "member": member,
                    "rank": rank,
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
            symbol = _std_symbol(_member_name(r.get("symbol"), symbol_hint), "GFEX", trade_date)
            if not symbol:
                continue
            rank = _coerce_int(r.get("rank"))
            member = _member_name(r.get("long_party_name"), r.get("vol_party_name"))
            if _is_summary_row(rank, member):
                continue
            rows.append(
                {
                    "trade_date": trade_date,
                    "exchange": "GFEX",
                    "symbol": symbol,
                    "member": member,
                    "rank": rank,
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


def _parse_dce_one(df: pd.DataFrame, trade_date: date, symbol_hint: str = "") -> list[dict]:
    """大商所（DCE）单合约 DataFrame → 标准化行

    列由 ``app.ingest.dce_scrapling.rows_to_frames`` 产出：
    ``symbol, rank, member, long_pos, long_chg, short_pos, short_chg, vol_pos``
    （名次对齐口径，见该模块 docstring）。``src`` 与外部采集器一致，
    便于两条链路互相幂等补齐。
    """
    rows = []
    for _, r in df.iterrows():
        try:
            symbol = _std_symbol(_member_name(r.get("symbol"), symbol_hint), "DCE", trade_date)
            if not symbol:
                continue
            rank = _coerce_int(r.get("rank"))
            member = _member_name(r.get("member"))
            if _is_summary_row(rank, member):
                continue
            rows.append(
                {
                    "trade_date": trade_date,
                    "exchange": "DCE",
                    "symbol": symbol,
                    "member": member,
                    "rank": rank,
                    "long_pos": _coerce_int(r.get("long_pos")),
                    "short_pos": _coerce_int(r.get("short_pos")),
                    "long_chg": _coerce_int(r.get("long_chg")),
                    "short_chg": _coerce_int(r.get("short_chg")),
                    "vol_pos": _coerce_int(r.get("vol_pos")),
                    "src": "scrapling:dce_memberDealPosi",
                    "version": "v1.0",
                }
            )
        except Exception as e:
            logger.warning(f"[member_position] DCE 解析行失败: {e}")
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
            # SHFE 现走官方全量接口（args 只接 date、symbols=None），
            # 故下面的 shfe_contracts override 事实上**已不生效**（保留仅为签名兼容）。
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
