# -*- coding: utf-8 -*-
"""仓单日报采集（PRD §4.4 → 新表 ``warehouse_receipt``）。

数据源：akshare（按交易日逐日取，接口只接受单日）
- ``futures_warehouse_receipt_czce(date)``   郑商所
- ``futures_warehouse_receipt_dce(date)``     大商所
- ``futures_shfe_warehouse_receipt(date)``    上期所
- ``futures_gfex_warehouse_receipt(date)``    广期所

落库表 ``warehouse_receipt``，主键 ``(report_date, exchange, symbol, warehouse)``。
解析按列名关键字启发式匹配（仓库 / 仓单数量 / 增减 / 单位 / 品种），部署后少量样本复核。
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

from app.core.db import get_engine
from app.core.logging import logger
from sqlalchemy import text

VERSION = "v1.0"
SRC = "akshare:warehouse_receipt"


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


def _str(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _col(row: dict, *keys: str) -> Any:
    low = {str(k).lower(): k for k in row.keys()}
    for key in keys:
        if key.lower() in low:
            return row[low[key.lower()]]
    return None


def _iter_frames(df):
    """把 akshare 的多种返回形态统一成 ``(品种|None, DataFrame)`` 迭代器。

    ★ 2026-10-07：akshare 1.18.94 起 ``futures_warehouse_receipt_czce`` 返回
    **dict{品种代码: DataFrame}**（每个品种一张表，且各品种列集不同：SR 有「品牌」、
    CY 有「仓库/厂库」「类别」且无「品牌」），不再是单一 DataFrame。
    旧代码只走 ``df.to_dict("records")`` 分支，普通 dict 无该方法 → 返回空列表 →
    **CZCE 仓单自 2026-09-30 起静默全失**（不抛异常、不记日志、调度照报成功）。
    实测 10-05/06/07 三个交易日 CZCE 零行。

    故这里显式支持三种形态：DataFrame / dict{品种: DataFrame} / dict{列: 序列}。
    品种名优先取 dict 的 key（CZCE 的表内**没有品种列**，只能靠 key）。
    """
    import pandas as pd  # 局部依赖，避免顶层硬绑

    if df is None:
        return
    if isinstance(df, pd.DataFrame):
        yield None, df
        return
    if isinstance(df, dict):
        for key, val in df.items():
            if isinstance(val, pd.DataFrame):
                yield (str(key), val)
            else:
                # dict{列名: 序列} —— 合成单表，品种留给行内字段
                try:
                    yield None, pd.DataFrame(val)
                except Exception:  # noqa: BLE001
                    continue
        return
    # 兜底：其它可迭代对象
    if hasattr(df, "to_dict"):
        try:
            yield None, pd.DataFrame(df.to_dict("records"))
        except Exception:  # noqa: BLE001
            return


_INSERT = text(
    """
    INSERT INTO warehouse_receipt
        (report_date, exchange, symbol, warehouse, receipt_qty, change_qty, unit, src, version)
    VALUES (:report_date, :exchange, :symbol, :warehouse, :receipt_qty, :change_qty, :unit, :src, :version)
    ON CONFLICT (report_date, exchange, symbol, warehouse) DO UPDATE SET
        receipt_qty = EXCLUDED.receipt_qty,
        change_qty  = EXCLUDED.change_qty,
        unit        = EXCLUDED.unit,
        src         = EXCLUDED.src
    """
)


def _parse_df(df, date: _dt.date, exchange: str) -> list[dict]:
    """解析仓单 DataFrame / dict{品种: DataFrame}（见 :func:`_iter_frames`）。"""
    if df is None:
        return []
    frames = list(_iter_frames(df))
    if not frames:
        return []
    out: list[dict] = []
    for key_sym, frame in frames:
        if frame is None or len(frame) == 0:
            continue
        for r in frame.to_dict("records"):
            wh = _str(_col(
                r, "warehouse", "交割仓库", "仓库", "仓库简称", "仓库/厂库",
                "warehouse_name", "仓库名称",
            ))
            if not wh:
                continue
            # 品种：行内字段优先；CZCE 的表内无品种列 → 用 dict key 兜底
            sym_raw = _str(_col(r, "symbol", "variety", "品种", "product"))
            symbol = f"{str(sym_raw).upper().strip('0')}888" if sym_raw else None
            if not symbol and key_sym:
                symbol = f"{str(key_sym).upper().strip('0')}888"
            if not symbol:
                continue
            out.append(
                {
                    "report_date": date,
                    "exchange": exchange,
                    "symbol": symbol,
                    "warehouse": wh,
                    "receipt_qty": _num(_col(
                        r, "receipt_qty", "仓单数量", "仓单", "qty", "receipt",
                        "仓单数量(手)", "数量",
                    )),
                    # ★ 「当日增减」是 CZCE/GFEX 的实际列名，旧别名表漏了它
                    "change_qty": _num(_col(
                        r, "change_qty", "增减", "当日增减", "变化", "change",
                        "delta", "日增减",
                    )),
                    "unit": _str(_col(r, "unit", "单位")),
                    "src": SRC,
                    "version": VERSION,
                }
            )
    return out


def _fetch_one(date: _dt.date, exchange: str) -> list[dict]:
    import akshare as ak  # type: ignore

    fn = {
        "CZCE": ak.futures_warehouse_receipt_czce,
        "DCE": ak.futures_warehouse_receipt_dce,
        "SHFE": ak.futures_shfe_warehouse_receipt,
        "GFEX": ak.futures_gfex_warehouse_receipt,
    }.get(exchange)
    if fn is None:
        return []
    try:
        df = fn(date=date.strftime("%Y%m%d"))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[warehouse_receipt] {exchange} {date} 接口失败: {e}")
        return []
    rows = _parse_df(df, date, exchange)
    # ★ fail-loud：源返回了内容却解析出 0 行 = **解析层已失效**（列名/返回形态变了）。
    #   这正是 2026-09-30→10-07 仓单静默全失的根因：旧代码在这里直接 return []，
    #   不报错、不告警，调度照报成功，数据空洞事后才发现。
    #   现在显式告警，让「采到了但没落库」不可能再隐身。
    if not rows and df is not None:
        try:
            n = len(df)
        except Exception:  # noqa: BLE001
            n = -1
        if n != 0:
            logger.error(
                f"[warehouse_receipt] {exchange} {date} 解析失败：源返回 "
                f"{n} 行但解析出 0 条（返回形态/列名可能已变更），"
                f"类型={type(df).__name__}，请检查 _iter_frames/_col 别名表")
    return rows


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


def run(date: _dt.date, exchanges: list[str] | None = None) -> dict:
    """单交易日批量抓取仓单。返回 {exchange: 行数/错误}。"""
    exchanges = exchanges or ["CZCE", "DCE", "SHFE", "GFEX"]
    summary: dict[str, Any] = {}
    for ex in exchanges:
        try:
            rows = _fetch_one(date, ex)
            n = save_rows(rows)
            summary[ex] = n
        except Exception as e:  # noqa: BLE001
            summary[ex] = f"失败: {type(e).__name__}: {e}"
    return summary


if __name__ == "__main__":
    import sys

    sys.stdout.reconfigure(encoding="utf-8")
    d = _dt.date.today()
    if len(sys.argv) > 1:
        d = _dt.datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    print(run(d))
