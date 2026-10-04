# -*- coding: utf-8 -*-
"""将 D:/学习资料 下 2026 逐挂牌合约 1 分钟线（2026.zip / 2026 3-4月.zip）
重采样为日线，灌入 contract_daily（append-only，version='v1.0' 与现有采集器一致，
避免重复版本行）。

目录结构：2026/<yyyymm>/<yyyymmdd>/<symbol>.csv
  - symbol 形如 a2601 / ag2603 / AP2601（小写所=DCE/SHFE 等，大写=CZCE）
  - 列：exchange,symbol,open,close,high,low,amount,volume,position,bob,eob,type,sequence
日线聚合：open=首根, high=max, low=min, close=末根, volume=sum, oi=末根 position
过滤：连续/指数代码（8888/9998/9999）与不符合 字母+4位YYMM 的 symbol 不入。

用法（容器内，已 docker cp 两个 zip 到 /app/data）：
  python scripts/import_local_contract_daily.py /app/data/2026.zip "/app/data/2026 3-4月.zip"
"""
from __future__ import annotations

import os
import re
import zipfile
import sys

import pandas as pd
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.db import session_scope
from app.models import ContractDaily

VERSION = "v1.0"
CONTRACT_RE = re.compile(r"^([A-Za-z]{1,3})(\d{4})$")
EXCLUDE = {"8888", "9998", "9999"}


def is_contract(sym: str) -> bool:
    m = CONTRACT_RE.match(sym)
    return bool(m) and m.group(2) not in EXCLUDE


def aggregate_day(df: pd.DataFrame) -> dict | None:
    o = pd.to_numeric(df["open"], errors="coerce").dropna()
    h = pd.to_numeric(df["high"], errors="coerce").dropna()
    l = pd.to_numeric(df["low"], errors="coerce").dropna()
    c = pd.to_numeric(df["close"], errors="coerce").dropna()
    v = pd.to_numeric(df["volume"], errors="coerce").dropna()
    p = pd.to_numeric(df["position"], errors="coerce").dropna()
    if o.empty or c.empty:
        return None
    return {
        "open": float(o.iloc[0]),
        "high": float(h.max()),
        "low": float(l.min()),
        "close": float(c.iloc[-1]),
        "volume": float(v.sum()),
        "oi": float(p.iloc[-1]) if not p.empty else None,
    }


def process_zip(zippath: str, batch: list, flush_fn) -> tuple[int, int, int]:
    n_files = 0
    n_rows = 0
    n_skip = 0
    with zipfile.ZipFile(zippath) as zf:
        for name in zf.namelist():
            if not name.endswith(".csv"):
                continue
            parts = name.split("/")
            # 期望 .../<yyyymmdd>/<symbol>.csv  （倒数第二段为 8 位日期）
            if len(parts) < 2:
                continue
            datefolder = parts[-2]
            if not (len(datefolder) == 8 and datefolder.isdigit()):
                continue
            sym_file = os.path.splitext(parts[-1])[0]
            if not is_contract(sym_file):
                n_skip += 1
                continue
            try:
                with zf.open(name) as f:
                    df = pd.read_csv(
                        f,
                        usecols=["exchange", "open", "high", "low", "close", "volume", "position"],
                    )
            except Exception:
                n_skip += 1
                continue
            if df.empty:
                n_skip += 1
                continue
            agg = aggregate_day(df)
            if agg is None:
                n_skip += 1
                continue
            sym = sym_file.upper()
            exchange = str(df["exchange"].iloc[0]).upper()
            product = sym[:-4]
            batch.append(
                {
                    "symbol": sym,
                    "product": product,
                    "exchange": exchange,
                    "trade_date": datefolder[:4] + "-" + datefolder[4:6] + "-" + datefolder[6:8],
                    "settle": None,
                    "src": "local_2026_1m",
                    "version": VERSION,
                    **agg,
                }
            )
            n_files += 1
            n_rows += 1
            if len(batch) >= 5000:
                flush_fn(batch)
                batch.clear()
    return n_files, n_rows, n_skip


def main() -> None:
    zips = sys.argv[1:] or [r"D:/学习资料/2026.zip", r"D:/学习资料/2026 3-4月.zip"]
    print(f"[import] 处理 zip: {zips}", flush=True)

    def flush(batch: list):
        if not batch:
            return
        stmt = pg_insert(ContractDaily).values(batch)
        stmt = stmt.on_conflict_do_update(
            index_elements=["symbol", "trade_date", "version"],
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "settle": stmt.excluded.settle,
                "volume": stmt.excluded.volume,
                "oi": stmt.excluded.oi,
                "src": stmt.excluded.src,
                "product": stmt.excluded.product,
                "exchange": stmt.excluded.exchange,
            },
        )
        with session_scope() as s:
            s.execute(stmt)
            s.commit()

    total_files = total_rows = total_skip = 0
    batch: list = []
    for zp in zips:
        if not os.path.exists(zp):
            print(f"[import] 跳过不存在: {zp}", flush=True)
            continue
        print(f"[import] <<< {zp}", flush=True)
        nf, nr, ns = process_zip(zp, batch, flush)
        total_files += nf
        total_rows += nr
        total_skip += ns
        print(f"[import]   {zp}: files={nf} rows={nr} skip={ns}", flush=True)
    flush(batch)
    print(f"[import] 完成：files={total_files} rows={total_rows} skip={total_skip}", flush=True)


if __name__ == "__main__":
    main()
