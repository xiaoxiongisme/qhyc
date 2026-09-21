# -*- coding: utf-8 -*-
"""合约代码对照表构建 & 全库 symbol 归一（统一到 4 位标准码）。

背景
----
五所官方合约代码的「月份位数 + 大小写」各不相同，而新浪源统一用「两位年 + 月、全大写」，
于是**同一合约在库内出现两套 symbol**（官方 ``AP701`` vs 新浪 ``AP2701``），
跨源/跨所 join 对不上。本模块：

1. ``build``      —— 扫描库内所有承载合约级代码的列，汇聚成 ``contract_code_map``
                     （标准码 ↔ 官方原生 ↔ 新浪 ↔ 天勤，见 ``db/init/14_contract_code.sql``）；
2. ``normalize``  —— 把业务表的合约级 symbol 物理改写为**标准码**
                     （``app.core.symbol_code.to_std``，即品种大写 + YYMM 四位）；
3. ``verify``     —— 归一后自检（残留 3 位码 / PK 重复 / 对照表覆盖度）。

用法（容器内执行；``./app`` 与 ``./scripts`` 均已挂载，无需重建镜像）
--------------------------------------------------------------------
    docker exec -w /app qhyc-api python -m app.ingest.contract_code build
    docker exec -w /app qhyc-api python -m app.ingest.contract_code verify
    docker exec -w /app qhyc-api python -m app.ingest.contract_code normalize --dry-run
    docker exec -w /app qhyc-api python -m app.ingest.contract_code normalize --apply --backup-dir /app/runtime

安全约束
--------
- ``normalize`` 默认 ``--dry-run``；``--apply`` 前会先**把受影响行导出 CSV 备份**，
  并在同一事务内做「PK 冲突预检」，任何冲突直接回滚（绝不静默丢行）。
- 只改写**合约码命名空间**；``<品种>888`` / ``KQ.m@...`` / ``IDX:...`` / 天勤码原样保留。
- ``fut_kline.symbol`` 存的是天勤**原生**码（郑商所 3 位），天勤按此订阅，
  **不参与物理归一** —— 只在对照表里登记 ``tqsdk_symbol``。
"""
from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json as _json
import os
from collections import defaultdict
from typing import Any, Optional

from sqlalchemy import text

from app.core import symbol_code as SC
from app.core.db import get_engine
from app.core.logging import logger

VERSION = "v1.0"

# ---------------------------------------------------------------------------
# 观测点：库内所有可能承载「合约级代码」的 (表, 列)
#   ex=None 表示该表没有交易所列，需从品种码/代码前缀反查
# ---------------------------------------------------------------------------
OBSERVATIONS: list[dict[str, Any]] = [
    # 会员持仓排名（核心：曾经两套 symbol 并存的地方）
    dict(table="member_position_rank", sym="symbol", ex="exchange",
         day="trade_date", src="src", where=None),
    # 合约级日线
    dict(table="contract_daily", sym="symbol", ex="exchange",
         day="trade_date", src="src", where=None),
    # 主连换月映射里的「当日实际合约」
    dict(table="main_contract_map", sym="underlying", ex="exchange",
         day="trade_date", src="src", where=None),
    # 日线主表（仅合约行，排除 888 连续码；库里 CZCE 历史合约是 3 位）
    dict(table="daily_bar", sym="symbol", ex=None,
         day="trade_date", src="src", where="symbol !~ '888$'"),
    # 基差表的近月 / 主力合约（原始来源大小写混杂）
    dict(table="spot_basis", sym="near_contract", ex=None,
         day="report_date", src="src", where="near_contract IS NOT NULL"),
    dict(table="spot_basis", sym="dominant_contract", ex=None,
         day="report_date", src="src", where="dominant_contract IS NOT NULL"),
    # 天勤 K 线（仅具体合约行；原生 3 位码，只登记不改写）
    dict(table="fut_kline", sym="symbol", ex=None,
         day="trade_datetime", src="src", where="kind = 'contract'"),
]

# ---------------------------------------------------------------------------
# 归一目标：(表, 合约列, 交易所列 | None, 日期列, PK 列清单)
#   PK 列清单用于「改写后是否撞主键」的预检
# ---------------------------------------------------------------------------
NORMALIZE_TARGETS: list[dict[str, Any]] = [
    dict(table="member_position_rank", col="symbol", ex="exchange", day="trade_date",
         pk=["trade_date", "exchange", "symbol", "member", "version"]),
    dict(table="contract_daily", col="symbol", ex="exchange", day="trade_date",
         pk=["symbol", "trade_date", "version"]),
    dict(table="main_contract_map", col="underlying", ex="exchange", day="trade_date",
         pk=None),  # underlying 不在 PK，无冲突风险
    dict(table="daily_bar", col="symbol", ex=None, day="trade_date",
         pk=["symbol", "trade_date"]),
    dict(table="spot_basis", col="near_contract", ex=None, day="report_date",
         pk=None),  # near_contract 不在 PK
    dict(table="spot_basis", col="dominant_contract", ex=None, day="report_date",
         pk=None),
]


# ===========================================================================
# 工具
# ===========================================================================
def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        text("SELECT 1 FROM information_schema.tables WHERE table_name = :t"),
        {"t": name},
    ).first()
    return row is not None


def _column_exists(conn, table: str, col: str) -> bool:
    row = conn.execute(
        text("""SELECT 1 FROM information_schema.columns
                WHERE table_name = :t AND column_name = :c"""),
        {"t": table, "c": col},
    ).first()
    return row is not None


def _load_ddl() -> str:
    """读 db/init/14_contract_code.sql（容器内 /app/db/init 未挂载 → 用内置 DDL 兜底）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (
        os.path.join(here, "..", "..", "db", "init", "14_contract_code.sql"),
        "/app/db/init/14_contract_code.sql",
    ):
        p = os.path.abspath(cand)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                return f.read()
    return _INLINE_DDL


_INLINE_DDL = """
CREATE TABLE IF NOT EXISTS contract_code_map (
    exchange        TEXT        NOT NULL,
    std_symbol      TEXT        NOT NULL,
    product         TEXT        NOT NULL,
    month_code      TEXT        NOT NULL,
    deliv_year      INT         NOT NULL,
    deliv_month     INT         NOT NULL,
    official_symbol TEXT,
    sina_symbol     TEXT,
    tqsdk_symbol    TEXT,
    observed_native TEXT,
    sources         TEXT,
    name            TEXT,
    first_seen      DATE,
    last_seen       DATE,
    version         TEXT        NOT NULL DEFAULT 'v1.0',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (exchange, std_symbol, version)
);
CREATE INDEX IF NOT EXISTS idx_ccm_std      ON contract_code_map (std_symbol);
CREATE INDEX IF NOT EXISTS idx_ccm_official ON contract_code_map (official_symbol);
CREATE INDEX IF NOT EXISTS idx_ccm_sina     ON contract_code_map (sina_symbol);
CREATE INDEX IF NOT EXISTS idx_ccm_product  ON contract_code_map (exchange, product);
"""


def ensure_table(conn) -> None:
    """建表（幂等）——容器内 /app/db 未挂载时必须走内联 DDL。"""
    conn.execute(text(_INLINE_DDL))
    # 若容器里能读到完整 DDL，也执行一遍（含注释版），无副作用
    try:
        ddl = _load_ddl()
        if ddl.strip() and ddl.strip() != _INLINE_DDL.strip():
            conn.execute(text(ddl))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[contract_code] 读取完整 DDL 失败（用内联 DDL 兜底）：{e}")


def product_exchange_map(conn) -> dict[str, str]:
    """品种码（大写）→ 交易所。取自 ``futures_symbol``（product 列可能为空）。"""
    m: dict[str, str] = {}
    if not _table_exists(conn, "futures_symbol"):
        return m
    rows = conn.execute(
        text("""SELECT DISTINCT UPPER(product), exchange FROM futures_symbol
                WHERE product IS NOT NULL AND product <> ''""")
    ).all()
    for prod, ex in rows:
        if prod and ex:
            m.setdefault(str(prod).upper(), str(ex).upper())
    return m


def _derive_exchange(raw: str, ex_col: Optional[str], pe_map: dict[str, str]) -> Optional[str]:
    """没有交易所列时反推：先看天勤前缀（``CZCE.AP610``），再按品种码查表。"""
    if ex_col:
        return str(ex_col).upper()
    s = str(raw or "").strip()
    if "." in s:
        head = s.split(".", 1)[0].strip().upper()
        if head.isalpha():
            return head
    prod = SC.product_of(s)
    return pe_map.get(prod) if prod else None


# ===========================================================================
# 1. build —— 汇聚对照表
# ===========================================================================
def _resolve(raw: str, ex_col: Optional[str], ref: _dt.date, pe_map: dict[str, str]):
    """一条库内观测 → ``(exchange, std_symbol, native_code)``；非合约码返回 ``None``。

    处理三种形态：
      · 裸原生码 ``AP701`` / ``cu2611`` / ``AP2701``
      · 天勤码   ``CZCE.AP610``           → 交易所取前缀，合约码取 ``.`` 之后
      · 非合约码 ``FG888`` / ``KQ.m@CZCE.FG`` / ``IDX:black`` → 跳过
    """
    s = str(raw or "").strip()
    if not s or "@" in s or ":" in s:
        return None
    ex = ex_col
    code = s
    if "." in s:
        head, tail = s.split(".", 1)
        if not head.isalpha() or not tail:
            return None
        ex = head.upper()
        code = tail
    std = SC.to_std(code, exchange=ex, ref_date=ref)
    if not SC.is_std(std):
        return None                      # 888 连续码 / 品种级 / 无法识别
    exchange = str(ex).upper() if ex else (_derive_exchange(code, None, pe_map) or "UNKNOWN")
    return exchange, std, code.upper()


def _scan(conn, obs: dict, pe_map: dict[str, str],
          agg: dict, stats: dict, names: dict) -> None:
    """扫一个观测点，把解析结果并入 ``agg``；顺带用「品种码 + 交易所」回填 ``pe_map``。

    ``pe_map`` 回填很关键：``futures_symbol`` 只登记 config 里的 50 个品种，
    ``BZ``（大商所丁二烯）/ ``PL``（郑商所丙烯）/ ``LG``（大商所原木）等不在其中，
    而 ``spot_basis`` / ``daily_bar`` / ``fut_kline`` 又没有交易所列 ——
    靠本函数从 ``member_position_rank`` 之类**带交易所列的表**里学到对应关系，
    第二遍才能把它们归到正确的所。
    """
    tbl, sym = obs["table"], obs["sym"]
    where = f"WHERE {obs['where']}" if obs.get("where") else ""
    ex_sel = obs["ex"] if obs["ex"] and _column_exists(conn, tbl, obs["ex"]) else "NULL"
    src_sel = obs["src"] if obs["src"] and _column_exists(conn, tbl, obs["src"]) else "NULL"
    sql = f"""
        SELECT {ex_sel} AS ex,
               {sym}    AS sym,
               MIN({obs['day']})::date AS first_seen,
               MAX({obs['day']})::date AS last_seen,
               string_agg(DISTINCT COALESCE({src_sel}, ''), ',') AS srcs
        FROM {tbl}
        {where}
        GROUP BY 1, 2
    """
    rows = conn.execute(text(sql)).all()
    stats["per_source"][f"{tbl}.{sym}"] = len(rows)
    for ex_c, raw, first_seen, last_seen, srcs in rows:
        raw_s = str(raw or "").strip()
        if not raw_s:
            continue
        ref = first_seen or _dt.date.today()
        resolved = _resolve(raw_s, ex_c, ref, pe_map)
        if resolved is None:
            # 888 连续码 / 主连码 / 指数码 / 品种级 → 不属于合约码命名空间
            stats["skipped"].append(f"{tbl}.{sym}={raw_s}")
            continue
        ex, std, native_code = resolved
        # 学习「品种码 → 交易所」（供第二遍无交易所列的表使用）
        prod = SC.product_of(std)
        if prod and ex != "UNKNOWN":
            pe_map.setdefault(prod, ex)
        key = (ex, std)
        a = agg[key]
        a["native"].add(native_code)
        for s in str(srcs or "").split(","):
            if s.strip():
                a["sources"].add(s.strip())
        if first_seen and (a["first"] is None or first_seen < a["first"]):
            a["first"] = first_seen
        if last_seen and (a["last"] is None or last_seen > a["last"]):
            a["last"] = last_seen
        stats["observed"] += 1


def build_map(only: Optional[list[str]] = None) -> dict[str, Any]:
    """扫描库内所有合约级 symbol，写 ``contract_code_map``（幂等）。

    对每个原生码，参考日取**该码在库内最早出现的日期** —— 这样 3 位码补年
    一定落在「合约尚未到期」的区间，不会因跨年而补成十年后。
    （``fut_kline`` 横跨 2020→2026 十年边界，``CF001`` 必须靠最早日期才能定成
      2020-01 而不是 2030-01，故该表的时间聚合不能省。）

    ``only``：表名白名单（只扫这些表）。``fut_kline`` 有 700 万+ 行，
    全量扫描是耗时大头，迭代调试时可先跳过它。
    """
    eng = get_engine()
    stats: dict[str, Any] = {"observed": 0, "mapped": 0, "skipped": [], "per_source": {}}
    # (exchange, std_symbol) -> 聚合
    agg: dict[tuple[str, str], dict[str, Any]] = defaultdict(
        lambda: {"native": set(), "sources": set(), "first": None, "last": None}
    )

    with eng.begin() as conn:
        ensure_table(conn)
        pe_map = product_exchange_map(conn)
        names = {}
        if _table_exists(conn, "futures_symbol"):
            for p, n in conn.execute(text(
                "SELECT DISTINCT UPPER(product), name FROM futures_symbol WHERE product IS NOT NULL"
            )).all():
                if p:
                    names.setdefault(str(p).upper(), n)

        pending: list[dict] = []
        for obs in OBSERVATIONS:
            tbl, sym = obs["table"], obs["sym"]
            if only and tbl not in only:
                continue
            if not _table_exists(conn, tbl) or not _column_exists(conn, tbl, sym):
                stats["skipped"].append(f"{tbl}.{sym}（表/列不存在）")
                continue
            if not obs["ex"] or not _column_exists(conn, tbl, obs["ex"]):
                pending.append(obs)
                continue
            _scan(conn, obs, pe_map, agg, stats, names)

        # 第二遍：扫没有交易所列的表 —— 此时 pe_map 已被上一遍补全
        # （``futures_symbol`` 只覆盖 config 里的 50 个品种，BZ / LG / PL 等不在其中）
        for obs in pending:
            _scan(conn, obs, pe_map, agg, stats, names)

        payload = []
        for (ex, std), a in sorted(agg.items()):
            prod = SC.product_of(std)
            yr, mo = SC.delivery_ym(std, ref_date=a["first"] or _dt.date.today())
            if yr is None or mo is None:
                continue
            payload.append({
                "exchange": ex,
                "std_symbol": std,
                "product": prod,
                "month_code": f"{yr % 100:02d}{mo:02d}",
                "deliv_year": yr,
                "deliv_month": mo,
                "official_symbol": SC.to_native(std, ex),
                "sina_symbol": SC.to_sina(std, ex),
                "tqsdk_symbol": SC.to_tqsdk(std, ex),
                "observed_native": ",".join(sorted(a["native"])),
                "sources": ",".join(sorted(a["sources"])),
                "name": names.get(prod),
                "first_seen": a["first"],
                "last_seen": a["last"],
                "version": VERSION,
            })

        if payload:
            conn.execute(
                text("""
                    INSERT INTO contract_code_map
                        (exchange, std_symbol, product, month_code, deliv_year, deliv_month,
                         official_symbol, sina_symbol, tqsdk_symbol, observed_native,
                         sources, name, first_seen, last_seen, version)
                    VALUES
                        (:exchange, :std_symbol, :product, :month_code, :deliv_year, :deliv_month,
                         :official_symbol, :sina_symbol, :tqsdk_symbol, :observed_native,
                         :sources, :name, :first_seen, :last_seen, :version)
                    ON CONFLICT (exchange, std_symbol, version) DO UPDATE SET
                        product         = EXCLUDED.product,
                        month_code      = EXCLUDED.month_code,
                        deliv_year      = EXCLUDED.deliv_year,
                        deliv_month     = EXCLUDED.deliv_month,
                        official_symbol = EXCLUDED.official_symbol,
                        sina_symbol     = EXCLUDED.sina_symbol,
                        tqsdk_symbol    = EXCLUDED.tqsdk_symbol,
                        observed_native = EXCLUDED.observed_native,
                        sources         = EXCLUDED.sources,
                        name            = COALESCE(EXCLUDED.name, contract_code_map.name),
                        first_seen      = LEAST(contract_code_map.first_seen, EXCLUDED.first_seen),
                        last_seen       = GREATEST(contract_code_map.last_seen, EXCLUDED.last_seen),
                        updated_at      = NOW()
                """),
                payload,
            )
            # 清掉本轮不再产生的历史行（本表是**派生**参考数据，全量刷新；
            # 否则早期误判交易所的 ``UNKNOWN`` 行会作为残留一直留着）
            conn.execute(
                text("""
                    DELETE FROM contract_code_map
                    WHERE version = :v
                      AND (exchange, std_symbol) NOT IN (
                          SELECT (x->>'exchange')::text, (x->>'std_symbol')::text
                          FROM jsonb_array_elements(CAST(:j AS jsonb)) AS x
                      )
                """),
                {"v": VERSION, "j": _json.dumps(payload, ensure_ascii=False, default=str)},
            )
        stats["mapped"] = len(payload)

    logger.info(f"[contract_code] build 完成：观测 {stats['observed']} 条 → 对照 {stats['mapped']} 行")
    return stats


# ===========================================================================
# 2. normalize —— 业务表物理改写为标准码
# ===========================================================================
def _build_runs(conn, tbl: str, col: str, day: str, ex: Optional[str]) -> list[tuple[str, str, _dt.date, _dt.date]]:
    """按**逐行交易日**算出「原生码 → 标准码」的连续日期段。

    返回 ``[(native, std, date_from, date_to), ...]``。

    ⚠️ 为什么必须逐行、不能按 symbol 取一个参考日：
    郑商所 3 位码十年一循环，库里 ``daily_bar`` 的 ``FG601`` **同时装着
    2016-01 与 2026-01 两个真实合约**（历史 CSV 导入时丢了年代）。
    若按 symbol 取「最早日期」当参考日，会把整批统一成 ``FG1601``，
    2026 年的行就被标成 2016 年的合约 —— 数据静默错乱。
    逐行取该行自己的交易日当参考日，才能把两段正确劈成 ``FG1601`` / ``FG2601``。
    """
    ex_sel = ex if ex and _column_exists(conn, tbl, ex) else "NULL"
    rows = conn.execute(text(f"""
        SELECT DISTINCT {ex_sel} AS ex, {col} AS raw, {day}::date AS d
        FROM {tbl} WHERE {col} IS NOT NULL AND {col} <> ''
        ORDER BY 2, 3
    """)).all()
    runs: list[list] = []
    for ex_c, raw, d in rows:
        raw_s = str(raw).strip()
        std = SC.to_std(raw_s, exchange=ex_c, ref_date=d)
        if std == raw_s:
            continue
        if runs and runs[-1][0] == raw_s and runs[-1][1] == std:
            runs[-1][3] = d            # 同一 (native, std) 且日期连续 → 延长
        else:
            runs.append([raw_s, std, d, d])
    return [tuple(r) for r in runs]  # type: ignore[return-value]


def _backup_rows(conn, tbl: str, col: str, natives: list[str], out_dir: str) -> str:
    """把将被改写的行导成 CSV 备份。"""
    os.makedirs(out_dir, exist_ok=True)
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"symbol_norm_backup_{tbl}_{col}_{ts}.csv")
    rows = conn.execute(
        text(f"SELECT * FROM {tbl} WHERE {col} = ANY(:n)"),
        {"n": natives},
    )
    cols = list(rows.keys())
    data = rows.fetchall()
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(cols)
        w.writerows(data)
    logger.info(f"[contract_code] 备份 {len(data)} 行 → {path}")
    return path


def _load_runs(conn, runs: list) -> None:
    """把改写方案装进临时表 ``_sym_runs``，供冲突预检与批量 UPDATE 共用。"""
    conn.execute(text("""
        CREATE TEMP TABLE IF NOT EXISTS _sym_runs (
            native TEXT, std TEXT, d0 DATE, d1 DATE
        ) ON COMMIT DROP
    """))
    conn.execute(text("TRUNCATE _sym_runs"))
    if runs:
        conn.execute(
            text("INSERT INTO _sym_runs (native, std, d0, d1) VALUES (:n, :s, :d0, :d1)"),
            [{"n": n, "s": s, "d0": a, "d1": b} for n, s, a, b in runs],
        )


def _pk_collision_count(conn, tbl: str, col: str, pk: list[str], day: str) -> int:
    """改写后是否会出现 PK 重复（同一 PK 下两组数据被合并成一组）。"""
    other = [c for c in pk if c != col]
    if not other:
        return 0
    sql = f"""
        SELECT 1 FROM {tbl} s
        LEFT JOIN _sym_runs m
               ON s.{col} = m.native AND s.{day}::date BETWEEN m.d0 AND m.d1
        GROUP BY {', '.join('s.' + c for c in other)}, COALESCE(m.std, s.{col})
        HAVING COUNT(*) > 1
    """
    return len(conn.execute(text(sql)).all())


def normalize(apply: bool = False, backup_dir: str = "./runtime") -> dict[str, Any]:
    """把业务表的合约级 symbol 改写为标准码。

    ``apply=False``（默认）只做 dry-run：算出待改写行数与 PK 冲突数，不落任何变更。
    """
    eng = get_engine()
    report: dict[str, Any] = {"plan": [], "applied": [], "backups": [], "dry_run": not apply}

    with eng.begin() as conn:
        for tgt in NORMALIZE_TARGETS:
            tbl, col, day = tgt["table"], tgt["col"], tgt["day"]
            if not _table_exists(conn, tbl) or not _column_exists(conn, tbl, col):
                continue
            runs = _build_runs(conn, tbl, col, day, tgt.get("ex"))
            if not runs:
                report["plan"].append({"table": f"{tbl}.{col}", "rows": 0, "note": "已达标"})
                continue
            _load_runs(conn, runs)

            n_rows = conn.execute(text(f"""
                SELECT COUNT(*) FROM {tbl} s JOIN _sym_runs m
                  ON s.{col} = m.native AND s.{day}::date BETWEEN m.d0 AND m.d1
            """)).scalar_one()
            coll = _pk_collision_count(conn, tbl, col, tgt.get("pk") or [], day)

            entry = {
                "table": f"{tbl}.{col}",
                "runs": len(runs),
                "distinct_native": len({r[0] for r in runs}),
                "rows": int(n_rows),
                "pk_collisions": coll,
                # 拆分出来的段（同一原生码落到多个标准码 = 十年撞车被正确劈开）
                "split_natives": sorted({
                    r[0] for r in runs
                    if len([x for x in runs if x[0] == r[0]]) > 1
                }),
                "sample": [f"{r[0]}[{r[2]}~{r[3]}]→{r[1]}" for r in runs[:5]],
            }
            report["plan"].append(entry)
            if coll:
                report.setdefault("aborted", []).append(
                    f"{tbl}.{col}：{coll} 组 PK 冲突，已跳过（请人工核对后处理）"
                )
                continue
            if not apply:
                continue

            # ---- 备份 ----
            report["backups"].append(
                _backup_rows(conn, tbl, col, sorted({r[0] for r in runs}), backup_dir)
            )
            # ---- 按日期段批量改写 ----
            conn.execute(text(f"""
                UPDATE {tbl} s SET {col} = m.std
                FROM _sym_runs m
                WHERE s.{col} = m.native AND s.{day}::date BETWEEN m.d0 AND m.d1
            """))
            report["applied"].append(entry)

    return report


# ===========================================================================
# 3. verify —— 归一后自检
# ===========================================================================
def verify() -> dict[str, Any]:
    eng = get_engine()
    out: dict[str, Any] = {}
    with eng.begin() as conn:
        ensure_table(conn)
        out["map_rows"] = int(conn.execute(
            text("SELECT COUNT(*) FROM contract_code_map")).scalar_one())
        out["map_by_exchange"] = [
            {"exchange": r[0], "n": int(r[1])}
            for r in conn.execute(text(
                "SELECT exchange, COUNT(*) FROM contract_code_map GROUP BY 1 ORDER BY 1")).all()
        ]

        checks = []
        for tgt in NORMALIZE_TARGETS:
            tbl, col = tgt["table"], tgt["col"]
            if not _table_exists(conn, tbl) or not _column_exists(conn, tbl, col):
                continue
            n3 = int(conn.execute(text(
                f"""SELECT COUNT(DISTINCT {col}) FROM {tbl}
                    WHERE {col} ~ '^[A-Za-z]+[0-9]{{3}}$' AND {col} !~ '888$'"""
            )).scalar_one())
            nlow = int(conn.execute(text(
                f"""SELECT COUNT(DISTINCT {col}) FROM {tbl}
                    WHERE {col} ~ '[a-z]'"""
            )).scalar_one())
            nbad = int(conn.execute(text(
                f"""SELECT COUNT(*) FROM {tbl}
                    WHERE {col} ~ '^[A-Za-z]+[0-9]{{3}}$' AND {col} !~ '888$'"""
            )).scalar_one())
            checks.append({"table": f"{tbl}.{col}", "residual_3digit_syms": n3,
                           "residual_3digit_rows": nbad, "lowercase_syms": nlow})
        out["residual"] = checks

        # 对照表覆盖度：库内出现但表里没有的标准码
        missing = []
        for tgt in NORMALIZE_TARGETS:
            tbl, col = tgt["table"], tgt["col"]
            if not _table_exists(conn, tbl) or not _column_exists(conn, tbl, col):
                continue
            r = conn.execute(text(f"""
                SELECT COUNT(*) FROM (
                    SELECT DISTINCT {col} AS s FROM {tbl}
                    WHERE {col} ~ '^[A-Za-z]+[0-9]{{4}}$'
                ) x
                WHERE NOT EXISTS (
                    SELECT 1 FROM contract_code_map m WHERE m.std_symbol = x.s
                )
            """)).scalar_one()
            if int(r):
                missing.append({"table": f"{tbl}.{col}", "unmapped_syms": int(r)})
        out["unmapped"] = missing

        # ---- 交割年月合理性：3 位码补年的**正确性证据** ----
        # 不变式：合约首次出现**不得晚于**交割月（合约在交割月内仍在交易），
        # 且领先期不超过 40 个月（挂 1~3 年上市）。
        # ``lead = 0`` 属正常（首次观测即在交割月，临近交割的合约就是如此）。
        # 若 3 位码补错年份（如 CF001 被补成 2030-01），lead 会变成 -120 或 +120，立刻暴露。
        bad = conn.execute(text("""
            SELECT exchange, std_symbol, official_symbol, first_seen,
                   (deliv_year * 12 + deliv_month)
                     - (EXTRACT(YEAR FROM first_seen) * 12 + EXTRACT(MONTH FROM first_seen)) AS lead_m
            FROM contract_code_map
            WHERE first_seen IS NOT NULL
              AND ((deliv_year * 12 + deliv_month)
                     - (EXTRACT(YEAR FROM first_seen) * 12
                        + EXTRACT(MONTH FROM first_seen))) NOT BETWEEN 0 AND 40
            ORDER BY 5
        """)).all()
        out["implausible_delivery"] = [
            {"exchange": r[0], "std_symbol": r[1], "official": r[2],
             "first_seen": str(r[3]), "lead_months": int(r[4])}
            for r in bad
        ]
        out["checks"] = {
            "residual_3digit_total": sum(c["residual_3digit_rows"] for c in checks),
            "implausible_total": len(out["implausible_delivery"]),
            "unmapped_total": sum(m["unmapped_syms"] for m in missing),
        }
    return out


# ===========================================================================
# CLI
# ===========================================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="合约代码对照表构建 / 全库 symbol 归一")
    ap.add_argument("action", choices=["build", "normalize", "verify", "all"])
    ap.add_argument("--apply", action="store_true", help="normalize 时真正落库（默认 dry-run）")
    ap.add_argument("--backup-dir", default="./runtime", help="改写前的 CSV 备份目录")
    ap.add_argument("--tables", default=None,
                    help="build 时只扫这些表（逗号分隔），例如 member_position_rank,daily_bar")
    args = ap.parse_args()

    import json

    if args.action in ("build", "all"):
        print("== build ==")
        only = [t.strip() for t in args.tables.split(",")] if args.tables else None
        print(json.dumps(build_map(only=only), ensure_ascii=False, indent=2, default=str))
    if args.action in ("normalize", "all"):
        print("== normalize ==")
        print(json.dumps(normalize(apply=args.apply, backup_dir=args.backup_dir),
                         ensure_ascii=False, indent=2, default=str))
    if args.action in ("verify", "all"):
        print("== verify ==")
        print(json.dumps(verify(), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
