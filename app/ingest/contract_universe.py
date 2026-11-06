# -*- coding: utf-8 -*-
"""全市场合约清单（品种 → 全部上市合约）。

2026-11-06 新增：为满足「采集模块可采集各品种全部合约」的要求，把
「活跃品种 + 该品种全部上市合约」的枚举能力独立成模块。

数据源（akshare，实测 1.18.94）
------------------------------
* ``futures_symbol_mark()``   → 86 个品种的**官方命名表**（exchange / symbol / mark）。
  注意 ``futures_zh_realtime(symbol=...)`` 的入参必须与这张表的 ``symbol`` 列完全一致；
  用 ``futures_display_main_sina()`` 的 ``name`` 去掉「连续」会有 19 个品种 KeyError。
* ``futures_zh_realtime(symbol=品种名)`` → 该品种当前全部可交易合约（含 ``RB0`` 连续）。

过滤
----
* 排除 ``xxx0`` 连续合约（不是可交易合约，主连已由 MinuteCollector 单独采集）；
* 排除 ``l3_ref.delisted_variety`` 登记的退市品种（用户 2026-11-06 明确要求删除，
  不应被采集重新写回）；
* 其余品种**全部保留**——包括 ``blocked_varieties`` 里因低流动/金融分类被剔除的品种，
  因为本模块只写 ``l0_raw.minute_bar`` 的 contract 层，不触碰 dim_* 字典，
  且 L1 合成已加 ``layer='continuous'`` 过滤，不会影响主连/回测口径。

持久化
------
``l3_ref.contract_universe``：每次刷新 upsert，本轮未出现的合约置 ``is_active=false``，
采集器只读 active 行，避免每次调度都重复枚举 86 次接口。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import logger

_SYMBOL_RE = re.compile(r"^([A-Za-z]+)(\d{3,4})$")

DDL = (
    "CREATE TABLE IF NOT EXISTS l3_ref.contract_universe ("
    " symbol text PRIMARY KEY,"
    " variety_code text NOT NULL,"
    " exchange text,"
    " variety_name text,"
    " is_active boolean NOT NULL DEFAULT true,"
    " captured_at timestamptz NOT NULL DEFAULT now())"
)
DDL_IDX = (
    "CREATE INDEX IF NOT EXISTS ix_contract_universe_var "
    "ON l3_ref.contract_universe (variety_code)"
)


def ensure_table(session: Session) -> None:
    session.execute(text(DDL))
    session.execute(text(DDL_IDX))
    session.commit()


def _delisted(session: Session) -> set[str]:
    try:
        rows = session.execute(
            text("SELECT upper(variety_code) FROM l3_ref.delisted_variety")
        ).scalars().all()
        return {str(r) for r in rows}
    except Exception:  # 表不存在时按空集合处理（尚未执行退市登记）
        session.rollback()
        return set()


def fetch_universe(exclude: set[str] | None = None) -> list[dict[str, Any]]:
    """拉取全市场全部上市合约；不写库。"""
    import akshare as ak  # type: ignore

    exclude = {e.upper() for e in (exclude or set())}
    mark = ak.futures_symbol_mark()
    out: dict[str, dict[str, Any]] = {}
    n_variety, n_fail = 0, 0
    for _, r in mark.iterrows():
        name = str(r["symbol"]).strip()
        exchange = str(r.get("exchange", "") or "").strip()
        try:
            df = ak.futures_zh_realtime(symbol=name)
        except Exception as e:  # noqa: BLE001  单品种失败不阻塞全市场
            n_fail += 1
            logger.warning(f"[universe] 枚举品种 {name} 失败: {type(e).__name__}: {e}")
            continue
        n_variety += 1
        if df is None or df.empty or "symbol" not in df.columns:
            continue
        for raw in df["symbol"].tolist():
            sym = str(raw).upper().strip()
            m = _SYMBOL_RE.match(sym)
            if not m:
                continue
            variety = m.group(1).upper()
            if sym.endswith("0"):        # RB0 / V0 = 连续，不是可交易合约
                continue
            if variety in exclude:
                continue
            out[sym] = {
                "symbol": sym,
                "variety_code": variety,
                "exchange": exchange,
                "variety_name": name,
            }
    logger.info(
        f"[universe] akshare 枚举完成: 品种 {n_variety}/{len(mark)} "
        f"失败 {n_fail} 合约 {len(out)} 排除品种 {len(exclude)}"
    )
    return list(out.values())


def sync_universe(session: Session, rows: list[dict[str, Any]]) -> int:
    """upsert 合约清单，并把本轮未出现的旧合约置为 inactive。返回 active 数。"""
    ensure_table(session)
    now = datetime.now(timezone.utc)
    if rows:
        session.execute(
            text(
                "INSERT INTO l3_ref.contract_universe "
                "(symbol, variety_code, exchange, variety_name, is_active, captured_at) "
                "VALUES (:symbol, :variety_code, :exchange, :variety_name, true, :now) "
                "ON CONFLICT (symbol) DO UPDATE SET "
                " variety_code = EXCLUDED.variety_code,"
                " exchange = EXCLUDED.exchange,"
                " variety_name = EXCLUDED.variety_name,"
                " is_active = true, captured_at = EXCLUDED.captured_at"
            ),
            [{**r, "now": now} for r in rows],
        )
    session.execute(
        text(
            "UPDATE l3_ref.contract_universe SET is_active=false "
            "WHERE is_active AND captured_at < :now"
        ),
        {"now": now},
    )
    session.commit()
    n = session.execute(
        text("SELECT count(*) FROM l3_ref.contract_universe WHERE is_active")
    ).scalar_one()
    logger.info(f"[universe] 已同步 active={n}")
    return int(n)


def refresh(session: Session) -> int:
    """拉取 + 落库（跳过退市品种）。"""
    return sync_universe(session, fetch_universe(exclude=_delisted(session)))


def load_active(session: Session) -> list[dict[str, Any]]:
    """读取当前 active 合约清单供采集器使用。"""
    try:
        rows = session.execute(
            text(
                "SELECT symbol, variety_code, exchange FROM l3_ref.contract_universe "
                "WHERE is_active ORDER BY symbol"
            )
        ).mappings().all()
        return [dict(r) for r in rows]
    except Exception:
        session.rollback()
        return []


def universe_age_hours(session: Session) -> float | None:
    try:
        ts = session.execute(
            text("SELECT max(captured_at) FROM l3_ref.contract_universe")
        ).scalar()
        if ts is None:
            return None
        return (datetime.now(timezone.utc) - ts).total_seconds() / 3600.0
    except Exception:
        session.rollback()
        return None
