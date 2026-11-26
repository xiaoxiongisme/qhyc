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
* 批次新鲜度闸门（CL-20261126-02）：若绝大多数合约的 YYMM 到期月早于当月，
  ``refresh()`` 直接抛错并保留上一批 active，避免上游陈旧快照污染水位线。

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


def _contract_ym(symbol: str) -> int | None:
    """把 4 位 YYMM 合约码转成绝对月序号（2000 年起）；非 4 位返回 None。"""
    m = _SYMBOL_RE.match(str(symbol))
    if not m or len(m.group(2)) != 4:
        return None
    yy, mm = int(m.group(2)[:2]), int(m.group(2)[2:])
    if not 1 <= mm <= 12:
        return None
    return (2000 + yy) * 12 + mm


def plausible_universe(rows: list[dict[str, Any]], now: datetime | None = None,
                       min_rows: int = 100, min_ratio: float = 0.6) -> tuple[bool, str]:
    """拒绝「绝大多数合约到期月明显过期」的批次（CL-20261126-02）。

    实证：2026-11-25 22:15 上游 akshare 快照返回 558 个 2024/2025 合约，
    覆盖掉 10:00 正确的 676 个 2026 合约；symbol 与 minute_bar 近 8 天数据
    完全对不上，watermarked=0，单轮采集从 159s 涨到 940~1015s，超 15min
    调度间隔，flock 抢锁失败 11 次；contract 层 max(ts) 停在 22:03。

    判据：合约数 >= 100，且到期月落在 [当月-1, 当月+24] 的 4 位 YYMM 合约
    占比 >= 60%。拒绝时保留上一批 active，等下一次 TTL 刷新自愈；失败必须
    fail-loud，不允许静默污染。
    """
    now = now or datetime.now(timezone.utc)
    cur = now.year * 12 + now.month
    if len(rows) < min_rows:
        return False, f"合约数 {len(rows)} < 下限 {min_rows}"
    yms = [ym for r in rows if (ym := _contract_ym(r.get("symbol", ""))) is not None]
    if not yms:
        return False, "无可判定的 4 位 YYMM 合约码"
    good = [ym for ym in yms if cur - 1 <= ym <= cur + 24]
    ratio = len(good) / len(yms)
    if ratio < min_ratio:
        bad = sorted({ym for ym in yms if ym not in set(good)})[:8]
        fmt = lambda ym: f"{ym // 12 - 2000}-{ym % 12:02d}"  # noqa: E731
        return False, (f"有效月份占比 {ratio:.0%} < {min_ratio:.0%} "
                       f"(n={len(yms)})，过期样例 {[fmt(x) for x in bad]}")
    return True, f"有效月份占比 {ratio:.0%} (n={len(yms)})"

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


def refresh(session: Session, *, allow_stale: bool = False) -> int:
    """拉取 + 落库（跳过退市品种）。

    ``allow_stale=True`` 仅供人工修复历史清单时显式绕过；正常调度绝不允许
    把明显过期的批次写进 active（CL-20261126-02）。
    """
    rows = fetch_universe(exclude=_delisted(session))
    if not allow_stale:
        ok, why = plausible_universe(rows)
        if not ok:
            raise RuntimeError(f"[universe] 拒绝同步可疑合约清单：{why}；保留上一批 active")
    return sync_universe(session, rows)


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
