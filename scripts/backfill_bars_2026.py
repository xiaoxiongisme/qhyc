# -*- coding: utf-8 -*-
"""2026-01-01 ~ 09-30 行情补采（云端执行）。

背景（用户 2026-09-30）
----------------------
当前 1 分钟数据缺失且无法补充；5/15/30/60m 与日线需在 2026-01-01~09-30 区间补齐。
本脚本按 品种 × 周期 循环调用采集器写入 L0/L1（bar_*m / daily_bar），再触发 L2 存储过程。

免费源历史深度（探针实测 2026-09-30）
-----------------------------------
tqsdk 连续合约 K 线硬上限 10000 根：
  15m 回溯到 2025-08-29 / 30m 2024-08 / 60m 2023-03 / daily(akshare) 全历史
  5m 仅回溯到 2026-03-02（10000 根 ≈ 6 个月）
→ 故 5m 实际可补区间为 2026-03-02~09-30；2026-01-01~03-01 免费源物理上拉不到，留缺口。

执行位置：云端（唯一真源，有 tqsdk/akshare 凭证与全量 bar_*）。

用法
----
    # 仅打印计划
    python scripts/backfill_bars_2026.py --dry-run --start 2026-01-01 --end 2026-09-30 \
        --freqs 15m --products RB

    # 先 1 品种 1 周期小窗口验证（不写库，只拉取打印）
    python scripts/backfill_bars_2026.py --verify-pull RB 15m 2026-03-01 2026-09-30

    # 实跑（先在小品种集验证）
    python scripts/backfill_bars_2026.py --start 2026-01-01 --end 2026-09-30 \
        --freqs 15m --products FG CU --apply
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone

# 使脚本在容器内以 scripts/xxx.py 直接运行时也能 import app.*
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("backfill_2026")

# 受补周期 → (bar 表 / roll_segment.freq / tqsdk 秒数)
FREQ_MAP = {
    "5m":   ("bar_5m",  "min5",  300),
    "15m":  ("bar_15m", "min15", 900),
    "30m":  ("bar_30m", "min30", 1800),
    "60m":  ("bar_60m", "min60", 3600),
    "daily":("daily_bar", None, None),
}

# tqsdk 主连「品种部分」大小写按交易所（错一个字母即合约不存在/超时）
_TQ_PROD_CASE = {
    "CZCE": str.upper,
    "CFFEX": str.upper,
    "DCE": str.lower,
    "SHFE": str.lower,
    "INE": str.lower,
}

# 免费源 5m 最深可回溯日期（探针实测 10000 根上限）。早于此时点 5m 拉不到。
_5M_FREE_FLOOR = date(2026, 3, 2)


def _resolve(symbol: str) -> dict | None:
    """品种码 → (exchange, main_symbol, product) via dim_variety。"""
    try:
        from app.core.db import get_engine
        from sqlalchemy import text
        with get_engine().connect() as conn:
            row = conn.execute(
                text("SELECT exchange, main_symbol FROM dim_variety "
                     "WHERE variety_code = :v AND is_active"),
                {"v": symbol},
            ).first()
        if not row:
            return None
        exchange, main = row[0], row[1]
        # 2026-10-01 修复：dim_variety.main_symbol 历史上长期为 NULL（1373 行中 1300 行为空），
        # 导致 upsert 写入 symbol=NULL 触发 NotNullViolation。见 migrations/011。
        # 此处做防御性回退：仍解析不出就返回 None，由调用方 skip，绝不写 NULL symbol。
        if not main or not exchange:
            # exchange 缺失同样无法拼 KQ 符号（KQ.m@{exchange}.{prod}），一并拒绝
            logger.warning(f"[resolve] {symbol} 字典缺 exchange/main_symbol，跳过")
            return None
        product = symbol  # variety_code 即产品大写码，如 RB / FG / CU
        return {"exchange": exchange, "main_symbol": main, "product": product}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[resolve] {symbol} 解析失败: {e}")
        return None


def _kq_symbol(exchange: str, product: str) -> str:
    prod = _TQ_PROD_CASE.get(exchange, str.lower)(product)
    return f"KQ.m@{exchange}.{prod}"


def _parse_tq_dt(ts) -> datetime | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts) / 1e9, tz=timezone.utc).astimezone(
            timezone(timedelta(hours=8)))
    except Exception:
        return None


def _to_dec(v):
    try:
        return None if v is None else round(float(v), 4)
    except Exception:
        return None


def _to_int(v):
    try:
        return None if v is None else int(v)
    except Exception:
        return None


def _pull_tqsdk(kq: str, dur: int, start: date, end: date, limit: int = 10000) -> list[dict]:
    """拉 tqsdk 主连 K 线，返回 [{'bucket':datetime,'open',...'oi'}]，已按 [start,end] 过滤。"""
    from tqsdk import TqApi, TqAuth
    from app.core.config import get_settings
    from app.ingest.fdf.tqhelper import get_auth
    api = TqApi(auth=get_auth())
    try:
        k = api.get_kline_serial(kq, duration_seconds=dur, data_length=limit)
        # 流式推送，等到根数稳定
        for _ in range(25):
            if k is not None and len(k) > 0:
                break
            try:
                api.wait_update(timeout=15)
            except Exception:
                break
        if k is None or len(k) == 0:
            return []
        rows = []
        for i in range(len(k)):
            dt = _parse_tq_dt(k.iloc[i].get("datetime"))
            if dt is None:
                continue
            # 现有 bar_Xm 为「起点标签」（实测 09:00/09:15…），tqsdk 多分钟 K 线同为起点标签，
            # 直接用作 bucket，不做 hourly 那种 +1h 偏移。
            if dt.date() < start or dt.date() > end:
                continue
            o, h, l, c = (_to_dec(k.iloc[i].get(x)) for x in ("open", "high", "low", "close"))
            vol = _to_int(k.iloc[i].get("volume"))
            oi = _to_int(k.iloc[i].get("close_oi") or k.iloc[i].get("open_oi"))
            # 成交额：tqsdk 无该字段，用 量×典型价 作近似（保持列非 NULL，避免下游断裂）
            typ = ((o or 0) + (h or 0) + (l or 0) + (c or 0)) / 4.0
            amount = round((vol or 0) * typ, 2) if vol else None
            rows.append({"bucket": dt, "open": o, "high": h, "low": l,
                         "close": c, "volume": vol, "amount": amount, "oi": oi})
        rows.sort(key=lambda r: r["bucket"])
        return rows
    finally:
        api.close()


def _pull_akshare_daily(product: str, start: date, end: date) -> list[dict]:
    """akshare 主连日线（futures_zh_daily_sina 用 RB0 这类主连码）。"""
    import akshare as ak
    sina = f"{product}0"
    df = ak.futures_zh_daily_sina(symbol=sina)
    if df is None or len(df) == 0:
        return []
    out = []
    for _, r in df.iterrows():
        d = str(r.get("date"))[:10]
        try:
            td = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            continue
        if td < start or td > end:
            continue
        out.append({
            "trade_date": td,
            "open": _to_dec(r.get("open")), "high": _to_dec(r.get("high")),
            "low": _to_dec(r.get("low")), "close": _to_dec(r.get("close")),
            "volume": _to_int(r.get("volume")), "oi": _to_int(r.get("hold")),
        })
    out.sort(key=lambda x: x["trade_date"])
    return out


def collect_frequency(symbol: str, freq: str, start: str, end: str,
                      session=None, verify: bool = False) -> int:
    """采集单品种单周期。

    - session=None 或 verify=True：仅拉取返回行数（不写库），用于验证。
    - 否则 upsert 入对应表（ON CONFLICT DO NOTHING，幂等）。
    返回写入/拉取行数。
    """
    if freq not in FREQ_MAP:
        raise ValueError(f"非法周期 {freq}")
    table, _, dur = FREQ_MAP[freq]
    s = _resolve(symbol)
    if not s:
        logger.error(f"[skip] {symbol} 在 dim_variety 无主连映射")
        return 0
    start_d = datetime.strptime(start, "%Y-%m-%d").date()
    end_d = datetime.strptime(end, "%Y-%m-%d").date()

    if freq == "daily":
        rows = _pull_akshare_daily(s["product"], start_d, end_d)
        if verify or session is None:
            logger.info(f"[verify] {symbol} daily 拉取 {len(rows)} 行 "
                        f"({rows[0]['trade_date'] if rows else '-'}~{rows[-1]['trade_date'] if rows else '-'})")
            return len(rows)
        _upsert_daily(s["main_symbol"], rows, session)
        return len(rows)

    # 5m 免费源最深只到 2026-03-02，早于该日的区间拉不到（留缺口）
    eff_start = max(start_d, _5M_FREE_FLOOR) if freq == "5m" else start_d
    if eff_start > end_d:
        logger.warning(f"[skip] {symbol} {freq}: 免费源不可达区间 {start_d}~{end_d}（5m 仅 ≥{_5M_FREE_FLOOR}）")
        return 0
    kq = _kq_symbol(s["exchange"], s["product"])
    rows = _pull_tqsdk(kq, dur, eff_start, end_d)
    if verify or session is None:
        first = rows[0]["bucket"] if rows else None
        last = rows[-1]["bucket"] if rows else None
        logger.info(f"[verify] {symbol} {freq} ({kq}) 拉取 {len(rows)} 行 {first}~{last}")
        return len(rows)
    _upsert_bar(table, s["main_symbol"], rows, session)
    return len(rows)


def _upsert_bar(table: str, symbol: str, rows: list[dict], session) -> None:
    from sqlalchemy import text
    if not rows:
        return
    sql = text(f"""
        INSERT INTO {table} (symbol, bucket, open, high, low, close, volume, amount, open_interest)
        VALUES (:symbol, :bucket, :open, :high, :low, :close, :volume, :amount, :oi)
        ON CONFLICT (symbol, bucket) DO NOTHING
    """)
    params = [{"symbol": symbol, "bucket": r["bucket"], "open": r["open"], "high": r["high"],
               "low": r["low"], "close": r["close"], "volume": r["volume"],
               "amount": r["amount"], "oi": r["oi"]} for r in rows]
    # executemany 逐批（避免单条游标膨胀）
    for i in range(0, len(params), 2000):
        session.execute(sql, params[i:i + 2000])
    session.commit()
    logger.info(f"  [ok] {symbol} {table} upsert {len(rows)} 行")


def _upsert_daily(symbol: str, rows: list[dict], session) -> None:
    from sqlalchemy import text
    if not rows:
        return
    # 2026-10-01 修复：daily_bar 的持仓列叫 oi（不是 open_interest，那是 bar_* 分钟表的列名）。
    # 原写法导致 82 品种 daily 全部 UndefinedColumn 失败（分钟线不受影响，故此前未暴露）。
    sql = text("""
        INSERT INTO daily_bar (symbol, trade_date, open, high, low, close, volume, oi, src)
        VALUES (:symbol, :trade_date, :open, :high, :low, :close, :volume, :oi, 'backfill')
        ON CONFLICT (symbol, trade_date) DO NOTHING
    """)
    params = [{"symbol": symbol, "trade_date": r["trade_date"], "open": r["open"],
               "high": r["high"], "low": r["low"], "close": r["close"],
               "volume": r["volume"], "oi": r["oi"]} for r in rows]
    for i in range(0, len(params), 2000):
        session.execute(sql, params[i:i + 2000])
    session.commit()
    logger.info(f"  [ok] {symbol} daily_bar upsert {len(rows)} 行")


def refresh_l1_l2(freq: str, session) -> None:
    """触发 L2 复权刷新（仅非日线/小时线）。"""
    from sqlalchemy import text
    if freq in ("5m", "15m", "30m", "60m"):
        rs_freq = FREQ_MAP[freq][1]
        logger.info(f"[refresh] sp_build_l2_roll_segment('{rs_freq}')")
        session.execute(text(f"CALL sp_build_l2_roll_segment('{rs_freq}')"))
        session.commit()


def run(args) -> int:
    freqs = [f.strip() for f in args.freqs.split(",") if f.strip() in FREQ_MAP]
    if not freqs:
        logger.error(f"--freqs 含非法周期，合法值：{','.join(FREQ_MAP)}")
        return 2

    products = [p.strip().upper() for p in args.products.split(",")] if args.products else None

    try:
        from app.core.db import get_engine
        from sqlalchemy import text
        with get_engine().connect() as conn:
            if products:
                # 显式 --products 视为用户意图，不做退市过滤
                syms = products
            else:
                # 排除已退市（source 由 011 标记为 *_delisted）：
                # 这类品种 tqsdk 查询会抛 "non-existent instrument"，实测 ME/TC 即如此。
                syms = [r[0] for r in conn.execute(
                    text("SELECT variety_code FROM dim_variety "
                         "WHERE is_active AND coalesce(source, '') NOT LIKE '%delisted%' "
                         "ORDER BY variety_code")
                ).all()]
    except Exception as e:  # noqa: BLE001
        logger.warning(f"取品种清单失败（用 --products 指定）：{e}")
        if not products:
            return 3
        syms = products

    logger.info(f"补采计划：{len(syms)} 品种 × {freqs} | {args.start}~{args.end} | "
                f"{'DRY-RUN' if args.dry_run else 'APPLY'}")
    failed: list[str] = []   # fail-soft：单品种失败不中断整轮（84 品种 × 5 频次代价太大）
    n_ok = 0
    for freq in freqs:
        for sym in syms:
            if args.dry_run:
                logger.info(f"  [dry] collect {sym} {freq} {args.start}~{args.end}")
                continue
            # 实跑：开 session 传入（upsert 幂等）
            from app.core.db import get_engine
            from sqlalchemy.orm import sessionmaker
            engine = get_engine()
            Session = sessionmaker(bind=engine)
            sess = Session()
            try:
                n = collect_frequency(sym, freq, args.start, args.end, session=sess)
                logger.info(f"  [ok] {sym} {freq} 写入 {n} 行")
                n_ok += 1
            except Exception as e:  # noqa: BLE001
                sess.rollback()
                logger.warning(f"  [fail] {sym} {freq}: {type(e).__name__}: {e}")
                failed.append(f"{sym}/{freq}")
            finally:
                sess.close()

    if failed:
        logger.warning(f"[汇总] 成功 {n_ok} 项，失败 {len(failed)} 项：{', '.join(failed)}")

    if args.skip_refresh:
        logger.info("[refresh] 已由 --skip-refresh 跳过，待外部统一做全量重生成")
        return 0

    if not args.dry_run:
        from app.core.db import get_engine
        from sqlalchemy.orm import sessionmaker
        engine = get_engine()
        Session = sessionmaker(bind=engine)
        sess = Session()
        try:
            for freq in freqs:
                refresh_l1_l2(freq, sess)
        finally:
            sess.close()
        logger.info("L1/L2 刷新完成；下一步：sync_cloud_local 反向灌入本地。")
    return 0


def verify_pull(args) -> int:
    """只拉取打印，不写库。用于上线前小窗口验证。"""
    sym, freq, start, end = args.verify_pull
    freq = freq.strip()
    if freq not in FREQ_MAP:
        logger.error(f"非法周期 {freq}")
        return 2
    n = collect_frequency(sym.strip().upper(), freq, start, end, session=None, verify=True)
    logger.info(f"verify-pull 结论：{sym} {freq} 在 {start}~{end} 可拉取 {n} 行")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="2026 行情补采（云端执行）")
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-09-30")
    ap.add_argument("--freqs", default="5m,15m,30m,60m,daily",
                    help="逗号分隔；合法值见 FREQ_MAP")
    ap.add_argument("--products", default=None,
                    help="逗号分隔品种码（默认全量 dim_variety）")
    ap.add_argument("--skip-refresh", action="store_true",
                    help="跳过收尾的增量 L1/L2 刷新（改由外部统一全量重生成）")
    ap.add_argument("--dry-run", action="store_true", help="只打印计划不写库")
    ap.add_argument("--apply", dest="dry_run", action="store_false",
                    help="实际写入（需先 verify-pull 验证）")
    ap.set_defaults(dry_run=True)
    # 验证子命令：python backfill_bars_2026.py --verify-pull RB 15m 2026-03-01 2026-09-30
    ap.add_argument("--verify-pull", nargs=4, default=None,
                    metavar=("SYMBOL", "FREQ", "START", "END"),
                    help="仅拉取打印不写库：--verify-pull RB 15m 2026-03-01 2026-09-30")
    args = ap.parse_args()
    if args.verify_pull:
        return verify_pull(args)
    try:
        date.fromisoformat(args.start); date.fromisoformat(args.end)
    except ValueError:
        logger.error("--start/--end 须为 YYYY-MM-DD"); return 2
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
