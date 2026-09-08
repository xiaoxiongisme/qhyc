"""
平滑主连自动延伸（M2.1，R1 完整落地）

背景：用户 CSV 平滑主连（⑪ 权威口径）是静态快照，会逐渐滞后于 daily_bar。
本模块用「比例平滑法」把平滑序列延伸到最新：

- 换月检测：候选合约日 kline 的收盘持仓量（close_oi）最大者为主力（实测与用户
  rolls 口径差 2~4 天，用户为确认延迟移仓；延伸段只要求收益率无跳空，无需对齐）
- 平滑收益率：换月日 ret = 新合约自身两日收益（消除拼接伪跳空）；其余日 = raw ret
- 水平锚定：adj(t) = adj(t-1) × (1 + smooth_ret/100)，从 CSV 末日 adj_close 链接
- adj_OHLC = raw_OHLC × (adj_close/raw_close)，volume 用 raw
- 新换月记录入 main_contract_map（src='inferred'）；延伸行 src='csv_smooth_ext'
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.logging import logger
from app.ingest.tqsdk_calibrator import _get_tqsdk, close_tqsdk, pd_date
from app.models import DailyBar, MainContractMap, MainContinuous
from app.repositories._base import upsert_main_continuous
from app.repositories.main_contract_repo import MainContractRepository


def _yymm_key(code: str) -> int | None:
    """合约代码 → 可排序的年月键。支持郑商所 3 位（609=2026-09）与 4 位（2609）"""
    digits = "".join(ch for ch in code if ch.isdigit())
    if len(digits) == 3:
        decade, month = int(digits[0]), int(digits[1:3])
        if not 1 <= month <= 12:
            return None
        return decade * 100 + month
    if len(digits) == 4:
        yy, month = int(digits[:2]), int(digits[2:4])
        if not 1 <= month <= 12:
            return None
        return yy * 100 + month
    return None


def _kline_to_df(klines, code: str) -> pd.DataFrame:
    """tqsdk 合约日线 → DataFrame[date, close, close_oi, volume]"""
    if klines is None or len(klines) == 0:
        return pd.DataFrame(columns=["date", "close", "close_oi", "volume"])
    recs = []
    for i in range(len(klines)):
        dt = klines.iloc[i].get("datetime")
        try:
            d = pd_date(dt)
        except Exception:
            continue
        if d is None:
            continue
        close = klines.iloc[i].get("close")
        if close is None or close != close or close <= 0:  # NaN/无效
            continue
        recs.append(
            {
                "date": d,
                "close": float(close),
                "close_oi": float(klines.iloc[i].get("close_oi") or 0),
                "volume": float(klines.iloc[i].get("volume") or 0),
                "code": code,
            }
        )
    return pd.DataFrame(recs)


def _code_len_from(known_codes: list[str]) -> int:
    """由已知合约推断代码数字位数（CZCE=3, 多数其他=4）"""
    for c in known_codes:
        digits = "".join(ch for ch in c if ch.isdigit())
        if digits:
            return len(digits)
    return 3


def _gen_next_codes(last_code: str, product: str, n: int = 3) -> list[str]:
    """按已知主力月码循环生成后续合约代码

    例：FG 已知主力月 = {1,5,9}，last=609 → 生成 701, 705, 709
    """
    digits = "".join(ch for ch in last_code if ch.isdigit())
    if len(digits) == 3:
        decade, month = int(digits[0]), int(digits[1:3])
    elif len(digits) == 4:
        decade, month = int(digits[:2]), int(digits[2:4])
    else:
        return []
    # 月码循环：从历史合约集合无法在此函数获得，由调用方传入 cycle
    return []


def _candidate_contracts(
    product: str, known_codes: list[str], last_underlying: str, n_future: int = 3
) -> list[str]:
    """候选合约 = 上次主力 + 按月码循环生成的后续合约

    known_codes: 用户 contracts.json 中该品种全部合约代码（去交易所前缀）
    """
    # 主力月码循环（如 FG/SA = 1,5,9）：按出现频次过滤，
    # 剔除移仓途中偶经的月份（如 FG010/011/012 各仅出现 1 次）
    from collections import Counter

    month_counts = Counter(_yymm_key(c) % 100 for c in known_codes if _yymm_key(c))
    if not month_counts:
        return []
    max_cnt = max(month_counts.values())
    months = sorted(m for m, n in month_counts.items() if n >= max_cnt * 0.5)
    last_key = _yymm_key(last_underlying)
    if last_key is None or not months:
        return []
    code_len = _code_len_from(known_codes)

    codes = [last_underlying]
    decade, month = last_key // 100, last_key % 100
    for _ in range(n_future):
        nxt = next((m for m in months if m > month), None)
        if nxt is None:
            nxt = months[0]
            decade += 1
        month = nxt
        if code_len == 3:
            codes.append(f"{product.upper()}{decade}{month:02d}")
        else:
            codes.append(f"{product.upper()}{decade:02d}{month:02d}")
    # 去重保序
    seen: set[str] = set()
    return [c for c in codes if not (c in seen or seen.add(c))]


def extend_smooth_series(session: Session, product: str) -> dict:
    """延伸某品种的 CSV 平滑主连序列；返回统计"""
    # 1. CSV 平滑段末锚点
    anchor = session.execute(
        select(MainContinuous.trade_date, MainContinuous.adj_close)
        .where(MainContinuous.product == product, MainContinuous.adj_close.is_not(None))
        .order_by(MainContinuous.trade_date.desc())
        .limit(1)
    ).first()
    if not anchor:
        return {"product": product, "skipped": "no csv_smooth anchor"}
    t0, v0 = anchor[0], float(anchor[1])

    # 2. raw 段（daily_bar 主连）
    raw_rows = session.execute(
        select(
            DailyBar.trade_date,
            DailyBar.open,
            DailyBar.high,
            DailyBar.low,
            DailyBar.close,
            DailyBar.volume,
        )
        .where(DailyBar.symbol == f"{product}888", DailyBar.trade_date > t0)
        .order_by(DailyBar.trade_date)
    ).all()
    if not raw_rows:
        return {"product": product, "skipped": f"raw 段无新数据（csv 止于 {t0}）"}

    # 前一交易日 raw close（计算 t0+1 的收益率）
    prev_raw = session.execute(
        select(DailyBar.close)
        .where(DailyBar.symbol == f"{product}888", DailyBar.trade_date == t0)
        .limit(1)
    ).scalar()

    # 3. 换月检测：候选合约 tqsdk 日线，close_oi 主力判定
    last_map = session.execute(
        select(MainContractMap.underlying)
        .where(MainContractMap.product == product)
        .order_by(MainContractMap.trade_date.desc())
        .limit(1)
    ).scalar()
    last_underlying = last_map or f"{product}999"
    since_key = _yymm_key(last_underlying) or 0
    exchange = session.execute(
        select(MainContractMap.exchange)
        .where(MainContractMap.product == product)
        .limit(1)
    ).scalar() or "UNKNOWN"

    api = _get_tqsdk()
    # 候选合约：从用户 contracts.json 已知合约 + 月码循环生成（不依赖 query_quotes）
    known_codes: list[str] = []
    try:
        from app.ingest.local_importer import LocalHistoryImporter

        imp = LocalHistoryImporter.__new__(LocalHistoryImporter)
        imp.root = Path(get_settings().yaml.imports.container_root or "/app/imports")
        files = imp._find_product_files(product)
        if "contracts" in files:
            data = json.loads(files["contracts"].read_text(encoding="utf-8-sig"))
            known_codes = [k.split(".")[-1] for k in data.keys()]
    except Exception as e:
        logger.warning(f"[smooth] {product} 读取 contracts.json 失败: {e}")
    candidates = _candidate_contracts(product, known_codes, last_underlying)
    if not candidates:
        return {"product": product, "skipped": "无候选合约（月码生成失败）"}

    frames = []
    for code in candidates:
        df_c = None
        for attempt in range(2):  # 超时重试 1 次
            try:
                kl = api.get_kline_serial(f"{exchange}.{code}", duration_seconds=86400, data_length=80)
                df_c = _kline_to_df(kl, code)
                break
            except Exception as e:
                logger.warning(f"[smooth] {product} 拉取 {code} 第 {attempt + 1} 次失败: {e}")
                close_tqsdk()
                api = _get_tqsdk()
        if df_c is not None and not df_c.empty:
            frames.append(df_c)
    if not frames:
        return {"product": product, "skipped": "候选合约 kline 全部失败"}

    oi_table = pd.concat(frames, ignore_index=True)
    # 主力判定：close_oi 最大（平手用 volume）
    oi_table = oi_table.sort_values(["date", "close_oi", "volume"], ascending=[True, False, False])
    main_by_day = oi_table.drop_duplicates("date", keep="first").set_index("date")

    # 换月日序列（延伸段内）
    raw_dates = [r[0] for r in raw_rows]
    switches: dict[date, tuple[str, str]] = {}  # date -> (old, new)
    prev_main: str | None = None
    for d in raw_dates:
        if d in main_by_day.index:
            cur = str(main_by_day.loc[d, "code"])
            if prev_main is not None and cur != prev_main:
                switches[d] = (prev_main, cur)
            prev_main = cur

    # 合约收盘表（换月日修正收益用）
    close_by_code: dict[str, dict[date, float]] = {}
    for f in frames:
        for _, r in f.iterrows():
            close_by_code.setdefault(r["code"], {})[r["date"]] = r["close"]

    # 4. 平滑收益率 → adj 链
    ext_rows: list[dict] = []
    switches_in_ext: list[dict] = []
    prev_adj = v0
    prev_close_raw = float(prev_raw) if prev_raw else None
    for r in raw_rows:
        d, o, h, l, c, vol = r[0], r[1], r[2], r[3], r[4], r[5]
        c = float(c)
        if prev_close_raw in (None, 0) or c == 0:
            ret = 0.0
        else:
            ret = (c - prev_close_raw) / prev_close_raw * 100.0
        change_flag = False
        underlying = None
        if d in switches:
            old_c, new_c = switches[d]
            change_flag = True
            underlying = new_c
            n_close = close_by_code.get(new_c, {}).get(d)
            n_prev = close_by_code.get(new_c, {}).get(raw_dates[raw_dates.index(d) - 1]) if d in raw_dates else None
            if n_close and n_prev:
                # 换月日：用新合约自身两日收益（消除拼接伪跳空）
                ret = (n_close - n_prev) / n_prev * 100.0
            switches_in_ext.append(
                {
                    "trade_date": d,
                    "exchange": exchange,
                    "product": product,
                    "main_symbol": f"{product}888",
                    "underlying": new_c,
                    "change_flag": True,
                    "delta": (float(n_close) - float(close_by_code.get(old_c, {}).get(d, 0)))
                    if (n_close and close_by_code.get(old_c, {}).get(d))
                    else 0,
                    "src": "inferred",
                }
            )

        prev_adj = prev_adj * (1 + ret / 100.0)
        scale = prev_adj / c if c else 1.0
        ext_rows.append(
            {
                "product": product,
                "trade_date": d,
                "raw_open": o,
                "raw_high": h,
                "raw_low": l,
                "raw_close": c,
                "raw_volume": vol,
                "adj_open": (float(o) * scale) if o is not None else None,
                "adj_high": (float(h) * scale) if h is not None else None,
                "adj_low": (float(l) * scale) if l is not None else None,
                "adj_close": round(prev_adj, 4),
                "adj_volume": vol,
                "underlying": underlying,
                "change_flag": change_flag,
                "src": "csv_smooth_ext",
            }
        )
        prev_close_raw = c

    # 5. 入库
    n_ext = upsert_main_continuous(session, ext_rows)
    if switches_in_ext:
        MainContractRepository(session).upsert(switches_in_ext)
    session.commit()
    logger.info(
        f"[smooth] {product} 延伸 {n_ext} 行（{raw_dates[0]} ~ {raw_dates[-1]}），"
        f"检出换月 {len(switches_in_ext)} 次: "
        f"{[(str(d), f'{o}->{n}') for d, (o, n) in sorted(switches.items())]}"
    )
    return {
        "product": product,
        "extended_rows": n_ext,
        "range": [raw_dates[0].isoformat(), raw_dates[-1].isoformat()],
        "rolls_detected": [
            {"date": d.isoformat(), "from": o, "to": n} for d, (o, n) in sorted(switches.items())
        ],
    }


def extend_all(session: Session, products: list[str] | None = None) -> list[dict]:
    """延伸全部有 CSV 口径的品种（缺省自动发现）"""
    if products is None:
        rows = session.execute(
            select(MainContinuous.product).distinct().order_by(MainContinuous.product)
        ).all()
        products = [r[0] for r in rows]
    results = []
    try:
        for p in products:
            try:
                results.append(extend_smooth_series(session, p))
            except Exception as e:
                logger.exception(f"[smooth] {p} 延伸失败: {e}")
                results.append({"product": p, "error": str(e)})
    finally:
        close_tqsdk()
    return results
