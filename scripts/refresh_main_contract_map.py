# -*- coding: utf-8 -*-
"""主力合约映射每日刷新：由 spot_basis.dominant_contract 数据驱动派生 main_contract_map。

为什么不是 smooth_extender（CSV 平滑锚点）？
  旧路径只在 MainContinuous 有 csv_smooth 锚点的品种上跑过（仅 FG/SA 试点），
  main_contract_map 因此只有 2 个品种。本脚本改用「每日维护的 spot_basis.dominant_contract」
  作为主力合约真值信号，覆盖全部品种（spot_basis 有 56 品种、dominant 100% 有值），
  且天然随 spot_basis 每日更新。

口径
----
  - underlying = spot_basis.dominant_contract（已 4 位标准码，如 PS2611）
  - main_symbol = {product}888
  - exchange 取自 futures_symbol（缺则跳过并告警）
  - change_flag = 与同品种上一交易日 underlying 不同
  - src = 'spot_basis'
  - delta 置 0（执行层偏移走 roll_segment，不依赖此列）

幂等：按 (trade_date, exchange, product) UPSERT；支持 --since 增量（默认取 main_contract_map
当前最大日之后，首跑全量）。

用法（容器内，scripts/ 已挂载）
  docker exec -w /app -e PYTHONPATH=/app qhyc-scheduler python scripts/refresh_main_contract_map.py
  docker exec -w /app -e PYTHONPATH=/app qhyc-scheduler python scripts/refresh_main_contract_map.py --since 2026-09-01
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date

from sqlalchemy import text

from app.core.db import session_scope
from app.core.logging import logger
from app.ingest.blocked_varieties import is_blocked
from app.repositories.main_contract_repo import MainContractRepository


def _max_existing_date(session) -> date | None:
    return session.execute(text("SELECT MAX(trade_date) FROM main_contract_map")).scalar()


def _delivery_ord(code: str | None) -> int | None:
    """合约码(如 'MA2609'/'PS2611') → 可比较的月度序数 (year*12+month)，用于换月前后向判定。"""
    if not code:
        return None
    m = re.search(r"(\d{2})(\d{2})$", code)
    if not m:
        return None
    yy, mm = int(m.group(1)), int(m.group(2))
    return (2000 + yy) * 12 + mm


# 连续 N 个交易日出现同一“更高”合约才确认换月，过滤 akshare dominant_contract 的临时抖动/回退。
HOLD_DAYS = 2

# exchange 兜底：确知品种但参考表(futures_symbol/contract_code_map)缺映射时补（自文档、无副作用）。
# PM=普麦(CZCE)、BB=胶合板(DCE)、FB=纤维板(DCE)。其余缺映射品种保持跳过（留 DUAL_GATE 兜底）。
_EXCHANGE_FALLBACK = {"PM": "CZCE", "BB": "DCE", "FB": "DCE"}


def smooth_dominant(series: list[tuple]) -> list[tuple]:
    """series: [(report_date, dominant_contract), ...] 按日期升序。
    返回 [(report_date, smoothed_contract), ...]：
      · 严格前向（ord 只能增大，禁止回退到更早月份）—— 消除 A→B→A→B 来回翻转；
      · 新合约须连续出现 >= HOLD_DAYS 个交易日才确认换月，防止单日毛刺。
    月度合约(PB/CU 等)本就逐月前进，平滑后行为不变；仅去掉虚假回退/毛刺。
    """
    out = []
    cur_ord = None
    cur_code = None
    cand_code = None
    cand_ord = None
    cand_streak = 0
    for d, code in series:
        o = _delivery_ord(code)
        if o is None:
            out.append((d, cur_code))
            continue
        if cur_ord is None:
            cur_ord, cur_code = o, code
        elif o > cur_ord:
            if cand_code == code:
                cand_streak += 1
            else:
                cand_code, cand_ord, cand_streak = code, o, 1
            if cand_streak >= HOLD_DAYS:
                cur_ord, cur_code = cand_ord, cand_code
                cand_code, cand_streak = None, 0
        # o <= cur_ord：忽略（sticky 前向）
        out.append((d, cur_code))
    return out


def parse_contract_yymm(symbol: str) -> tuple[int, int] | None:
    """合约码(如 'b2511'/'RS2507'/'sc2601') 末尾 4 位 YYMM → (year, month)。
    兼容旧 CZCE 3 位（'RS507'=2015-07）。用于 OI 推断主力的代码解析。"""
    m = re.search(r"(\d{2})(\d{2})$", symbol)
    if m:
        yy, mm = int(m.group(1)), int(m.group(2))
        if 1 <= mm <= 12:
            return (2000 + yy, mm)
    m3 = re.search(r"(\d)(\d{2})$", symbol)
    if m3:
        y, mo = int(m3.group(1)), int(m3.group(2))
        if 1 <= mo <= 12 and 2010 <= 2010 + y <= 2099:
            return (2010 + y, mo)
    return None


def derive_oi_rows(session, covered: set[str], ex_map: dict, since=None, output_all=False):
    """第二真源：对 spot_basis 未覆盖（缺 dominant_contract）的品种，
    用 contract_daily 持仓量(OI)最大合约推断每日主力，平滑后并入 main_contract_map。

    仅作用于：有 exchange 映射、且不在 covered（spot_basis 已覆盖或已存在）的品种；
    收敛 DUAL_GATE_FALLBACK 段。src='oi_inferred' 以示区分。
    注意：OI 最大合约在深度 contango 时可能落在远月，偶发误判；但对缺真源品种远优于价格双门兜底。
    """
    candidates = [p for p in ex_map if p not in covered]
    if not candidates:
        return [], set()
    out = []
    used = set()
    for product in candidates:
        if is_blocked(product):
            continue
        pfx = product.lower() + "%"
        rows = session.execute(text("""
            SELECT symbol, trade_date, oi FROM contract_daily
            WHERE lower(symbol) LIKE :pfx AND oi IS NOT NULL AND oi > 0
            ORDER BY trade_date
        """), {"pfx": pfx}).fetchall()
        if not rows:
            continue
        daily = {}
        for sym, td, oi in rows:
            cur = daily.get(td)
            if cur is None or oi > cur[1]:
                daily[td] = (sym, oi)
        series = []
        for td in sorted(daily):
            ym = parse_contract_yymm(daily[td][0])
            if not ym:
                continue
            yy, mm = ym
            series.append((td, f"{product}{yy % 100:02d}{mm:02d}"))
        if not series:
            continue
        smoothed = smooth_dominant(series)
        exchange = ex_map[product][0]
        prev = None
        for td, und in smoothed:
            if und is None:
                prev = und
                continue
            change_flag = (prev is not None) and (und != prev)
            prev = und
            if (not output_all) and since is not None and td < since:
                continue
            out.append({
                "trade_date": td, "exchange": exchange, "product": product,
                "main_symbol": f"{product}888", "underlying": str(und),
                "change_flag": bool(change_flag), "delta": 0, "src": "oi_inferred",
            })
        used.add(product)
    return out, used


def derive_rows(session, since: date | None, output_all: bool = False) -> list[dict]:
    """派生 main_contract_map：
      · 真源1 spot_basis.dominant_contract（平滑去噪）；
      · 真源2 contract_daily 持仓量最大合约（OI 推断），仅补 spot_basis 未覆盖品种；
      · change_flag = 与同品种上一交易日 smoothed underlying 不同；
      · exchange 取自 futures_symbol(优先)/contract_code_map。
    """
    raw = session.execute(text("""
        SELECT s.symbol AS product, s.report_date AS trade_date, s.dominant_contract AS underlying
        FROM spot_basis s
        WHERE s.dominant_contract IS NOT NULL AND s.dominant_contract <> ''
        ORDER BY s.symbol, s.report_date
    """)).fetchall()

    by_prod: dict[str, list] = {}
    for product, trade_date, underlying in raw:
        by_prod.setdefault(product, []).append((trade_date, underlying))

    ex_rows = session.execute(text("""
        SELECT product, exchange, 1 AS pri FROM futures_symbol
            WHERE product IS NOT NULL AND product <> '' AND exchange IS NOT NULL
        UNION ALL
        SELECT product, exchange, 2 AS pri FROM contract_code_map
            WHERE product IS NOT NULL AND product <> '' AND exchange IS NOT NULL
    """)).fetchall()
    ex_map: dict[str, tuple] = {}
    for product, exchange, pri in ex_rows:
        p = str(product).upper()
        if p not in ex_map or pri < ex_map[p][1]:
            ex_map[p] = (str(exchange).upper(), pri)
    for p, ex in _EXCHANGE_FALLBACK.items():
        if p not in ex_map:
            ex_map[p] = (ex, 9)

    # 已被 spot_basis 覆盖、或已存在于 main_contract_map 的品种不再走 OI 重算（增量省成本）
    existing = {str(r[0]).upper() for r in session.execute(
        text("SELECT DISTINCT product FROM main_contract_map")).fetchall()}
    covered = set(str(p).upper() for p in by_prod) | existing

    out: list[dict] = []
    skipped_exchange = set()
    for product, series in by_prod.items():
        if is_blocked(product):
            continue
        series.sort(key=lambda x: x[0])
        smoothed = smooth_dominant(series)
        ex = ex_map.get(str(product).upper())
        if not ex:
            skipped_exchange.add(product)
            continue
        exchange = ex[0]
        prev = None
        for td, und in smoothed:
            if und is None:
                prev = und
                continue
            change_flag = (prev is not None) and (und != prev)
            prev = und
            if (not output_all) and since is not None and td < since:
                continue
            out.append({
                "trade_date": td,
                "exchange": exchange,
                "product": str(product).upper(),
                "main_symbol": f"{str(product).upper()}888",
                "underlying": str(und),
                "change_flag": bool(change_flag),
                "delta": 0,
                "src": "spot_basis",
            })

    oi_rows, oi_used = derive_oi_rows(session, covered, ex_map, since=since, output_all=output_all)
    out.extend(oi_rows)
    if oi_used:
        logger.info(f"[refresh_mcm] OI 推断主力覆盖品种: {sorted(oi_used)}")

    if skipped_exchange:
        logger.warning(f"[refresh_mcm] 缺 exchange 跳过品种: {sorted(skipped_exchange)}")
    return out


def refresh(since: date | None = None, reset: bool = False, chunk: int = 5000) -> dict:
    with session_scope() as session:
        output_all = False
        if reset:
            # public.main_contract_map 是透传视图，TRUNCATE 不支持视图；改用 DELETE（与 repo.upsert 经同一视图写基表，路径一致）
            session.execute(text("DELETE FROM main_contract_map"))
            session.commit()
            since = None
            output_all = True
            logger.info("[refresh_mcm] --reset：清空并全量重算（带平滑去噪）")
        else:
            if since is None:
                since = _max_existing_date(session)
                if since is not None:
                    logger.info(f"[refresh_mcm] 增量模式，since={since}（仅新日期）")
                else:
                    logger.info("[refresh_mcm] 首跑全量")
        rows = derive_rows(session, since, output_all=output_all)
        repo = MainContractRepository(session)
        n = 0
        products: set[str] = set()
        for i in range(0, len(rows), chunk):
            batch = rows[i:i + chunk]
            n += repo.upsert(batch)
            products.update(r["product"] for r in batch)
        session.commit()
        logger.info(f"[refresh_mcm] 写入 {n} 行，覆盖品种 {len(products)} 个")
        return {"rows": n, "products": len(products)}


def main() -> None:
    ap = argparse.ArgumentParser(description="由 spot_basis 派生 main_contract_map（每日刷新，带换月去噪）")
    ap.add_argument("--since", default=None, help="仅处理该日之后的 report_date（YYYY-MM-DD）")
    ap.add_argument("--reset", action="store_true", help="清空 main_contract_map 并全量重算（带平滑去噪）")
    args = ap.parse_args()
    since = date.fromisoformat(args.since) if args.since else None
    r = refresh(since, reset=args.reset)
    print(f"OK rows={r['rows']} products={r['products']}")


if __name__ == "__main__":
    sys.exit(main())
