"""
§4.5 模板解析 dry-run 验证（不依赖 DB / Docker）

复用 LocalHistoryImporter 的文件发现逻辑（_find_product_files），
对真实数据目录逐品种解析 5 类文件并校验：
- 日期升序、OHLC 合法（high >= low，close/open 在 [low, high]）
- rolls delta 无缺失
- contracts attach_returns 正常产出 ret
- hourly 时间戳可解析
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.core.logging import setup_logging, logger
from app.ingest.local_importer import LocalHistoryImporter
from app.ingest.utils import attach_returns


def dec(v):
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def check_ohlc(rows: list[dict], label: str) -> int:
    bad = 0
    for r in rows:
        o, h, l, c = (dec(r.get("open")), dec(r.get("high")),
                      dec(r.get("low")), dec(r.get("close")))
        if None in (o, h, l, c):
            continue
        if h < l or c < l or c > h or o < l or o > h:
            bad += 1
            if bad <= 3:
                logger.warning(f"[dry-run] {label} OHLC 异常: {r}")
    return bad


def verify_product(files: dict[str, Path], product: str) -> bool:
    ok = True
    logger.info(f"===== {product} =====")

    # 1. daily.json
    if p := files.get("daily"):
        items = json.loads(p.read_text(encoding="utf-8-sig"))
        rows = [{"trade_date": date.fromisoformat(str(it["date"])[:10]), **it} for it in items]
        dates = [r["trade_date"] for r in rows]
        bad = check_ohlc(rows, "daily")
        mono = all(dates[i] < dates[i + 1] for i in range(len(dates) - 1))
        logger.info(f"[dry-run] {p.name}: rows={len(rows)} "
                    f"range={dates[0]}~{dates[-1]} asc={mono} ohlc_bad={bad}")
        ok &= mono and bad == 0
    else:
        logger.warning(f"[dry-run] {product} 缺 daily.json")
        ok = False

    # 2. cont_adj.json
    if p := files.get("cont_adj"):
        items = json.loads(p.read_text(encoding="utf-8-sig"))
        rows = [{"trade_date": date.fromisoformat(str(it["date"])[:10]), **it} for it in items]
        dates = [r["trade_date"] for r in rows]
        bad = check_ohlc(rows, "cont_adj")
        n_adj = sum(1 for it in items if "adj" in it)
        logger.info(f"[dry-run] {p.name}: rows={len(rows)} "
                    f"range={dates[0]}~{dates[-1]} adj_flag_rows={n_adj} ohlc_bad={bad}")
        ok &= bad == 0
    else:
        logger.warning(f"[dry-run] {product} 缺 cont_adj.json")
        ok = False

    # 3. rolls.csv
    if p := files.get("rolls"):
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        bad_delta = [r for r in rows if not (r.get("delta") or "").strip()]
        bad_oldnew = [r for r in rows if not (r.get("old") or "").strip() or not (r.get("new") or "").strip()]
        logger.info(f"[dry-run] {p.name}: rows={len(rows)} delta_missing={len(bad_delta)} "
                    f"oldnew_missing={len(bad_oldnew)} first={rows[0] if rows else None}")
        ok &= not bad_delta and not bad_oldnew
    else:
        logger.warning(f"[dry-run] {product} 缺 rolls.csv")
        ok = False

    # 4. contracts.json
    if p := files.get("contracts"):
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        rows = []
        symbols = []
        for full_code, items in data.items():
            sym = full_code.split(".")[-1]
            symbols.append(sym)
            for it in items:
                rows.append({
                    "symbol": sym,
                    "trade_date": date.fromisoformat(str(it["date"])[:10]),
                    **{k: v for k, v in it.items() if k != "date"},
                })
        rows = attach_returns(rows)
        bad = check_ohlc(rows, "contracts")
        rets = [r["ret_close"] for r in rows if r["ret_close"] is not None]
        logger.info(f"[dry-run] {p.name}: contracts={len(symbols)} rows={len(rows)} "
                    f"ohlc_bad={bad} ret_n={len(rets)} ret_sample={[str(x) for x in rets[:3]]}")
        ok &= bad == 0 and len(rets) > 0
    else:
        logger.warning(f"[dry-run] {product} 缺 contracts.json")
        ok = False

    # 5. hourly csv
    if p := files.get("hourly"):
        rows = []
        with p.open("r", encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                dt = datetime.strptime(str(r["date"]).strip(), "%Y-%m-%d %H:%M:%S")
                rows.append({"trade_datetime": dt, **{k: v for k, v in r.items() if k != "date"}})
        bad = check_ohlc(rows, "hourly")
        dts = [r["trade_datetime"] for r in rows]
        mono = all(dts[i] < dts[i + 1] for i in range(len(dts) - 1))
        logger.info(f"[dry-run] {p.name}: rows={len(rows)} "
                    f"range={dts[0]}~{dts[-1]} asc={mono} ohlc_bad={bad}")
        ok &= bad == 0 and mono
    else:
        logger.warning(f"[dry-run] {product} 缺 cont_adj_adjusted.csv")
        ok = False

    return ok


def main():
    setup_logging()
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=str, default=r"E:\QH")
    ap.add_argument("--product", type=str, default=None)
    args = ap.parse_args()

    # 构造一个不依赖 DB 的 importer 实例，仅用其文件发现能力
    imp = LocalHistoryImporter.__new__(LocalHistoryImporter)
    imp.root = Path(args.root)

    products = [args.product.upper()] if args.product else imp.discover_products()
    if not products:
        logger.error(f"[dry-run] {imp.root} 下未发现 §4.5 模板文件")
        sys.exit(1)

    all_ok = True
    for p in products:
        files = imp._find_product_files(p)
        all_ok &= verify_product(files, p)

    if all_ok:
        logger.info("[dry-run] §4.5 模板解析全部通过 ✔")
    else:
        logger.error("[dry-run] 存在校验失败项，见上方日志")
        sys.exit(1)


if __name__ == "__main__":
    main()