#!/usr/bin/env python
"""合成 main_continuous 2026（R3 / 补采 2026 continuous）。

数据源（两源合并，重叠日优先 CSV）：
  A. D:/学习资料 各交易所 ZIP 的 00_主力连续 1 分钟主连 CSV
     —— 覆盖 2026-01-01 ~ 约 2026-06-13（无年份后缀文件为多年数据，已抽 2026 行）。
  B. daily_bar 888 主连日线（akshare 回填，已完整覆盖到当前）
     —— 补齐 2026-06-14 ~ 年底的缺口，并对 A 做交叉验证。

main_continuous.raw_* 权威口径 = daily_bar 主连（smooth_extender 注释明确），故 B 是天然兜底；
A 提供更细粒度的前半段且为天勤官方主连序列。两源在 6 月边界同为"当日主连"，应一致、合并连续。

adj_*：比率法复权（与项目 smooth_extender 同一约定）。换月检测：主连 OI
（CSV close_interest / daily_bar.oi）较前一日骤降 >40% 视为换月，消除拼接跳空。

用法（容器内，PGHOST=timescaledb）：
  python scripts/synthesize_main_continuous_2026.py --csv-dir /app/_mc_src --apply
"""
from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from datetime import date, datetime
from pathlib import Path

import psycopg2
from psycopg2.extras import execute_values


def _pg_conn():
    url = os.getenv("DATABASE_URL")
    if url and url.startswith("postgresql"):
        return psycopg2.connect(url)
    # 容器内凭证多为 POSTGRES_* 形式
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", os.getenv("PGHOST", "timescaledb")),
        port=int(os.getenv("POSTGRES_PORT", os.getenv("PGPORT", "5432"))),
        user=os.getenv("POSTGRES_USER", os.getenv("PGUSER", "futures")),
        password=os.getenv("POSTGRES_PASSWORD", os.getenv("PGPASSWORD", "")),
        dbname=os.getenv("POSTGRES_DB", os.getenv("PGDATABASE", "futures")),
    )


def _csv_product(symbol: str) -> str:
    """KQ.m@CZCE.ma -> MA ; KQ.m@SHFE.ag -> AG"""
    try:
        return symbol.split("@", 1)[1].split(".", 1)[1].upper()
    except Exception:
        return symbol.upper()


def _aggregate_csv(path: Path, start: date, end: date):
    """1 分钟主连 CSV -> (product, {date: raw_row})。"""
    rows: dict[date, dict] = {}
    prod = None
    with path.open(encoding="utf-8-sig") as f:
        rdr = csv.DictReader(f)
        for row in rdr:
            dt = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S")
            if dt.date() < start or dt.date() > end:
                continue
            if prod is None:
                prod = _csv_product(row.get("symbol", ""))
            o = float(row["open"]); h = float(row["high"]); l = float(row["low"]); c = float(row["close"])
            v = float(row["volume"]); oi = float(row.get("close_interest") or row.get("open_interest") or 0)
            d = dt.date()
            if d not in rows:
                rows[d] = {"open": o, "high": h, "low": l, "close": c, "volume": v, "oi": oi}
            else:
                cur = rows[d]
                cur["high"] = max(cur["high"], h)
                cur["low"] = min(cur["low"], l)
                cur["close"] = c
                cur["volume"] += v
                cur["oi"] = oi
    return prod, rows


def _load_daily_bar_888(conn, start: date, end: date) -> dict[str, dict[date, dict]]:
    """daily_bar 888 主连日线 -> {product: {date: raw_row}}。"""
    out: dict[str, dict[date, dict]] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT substring(symbol from 1 for length(symbol)-3), trade_date, open, high, low, close, volume, oi "
            "FROM daily_bar WHERE symbol LIKE '%%888' AND trade_date BETWEEN %s AND %s",
            (start, end),
        )
        for prod, td, o, h, l, c, v, oi in cur.fetchall():
            out.setdefault(prod, {})[td] = {
                "open": float(o), "high": float(h), "low": float(l), "close": float(c),
                "volume": int(v or 0), "oi": int(oi or 0),
            }
    return out


def _back_adjust(rows: list[dict]) -> list[dict]:
    """比率法复权：换月日（OI 骤降 >40% 或 价格跳空 >5%）消除拼接跳空，adj 连续。

    两源合并时（CSV 上半年 → daily_bar888 下半年），跨源边界常因两家"主力合约"认定不同
    出现 >5% 跳空，按换月平滑可使整段 adj 连续，raw_* 仍保留真实日值。
    """
    if not rows:
        return []
    cum_ratio = 1.0
    prev_oi = rows[0]["oi"]
    prev_close = rows[0]["close"]
    out = []
    for i, r in enumerate(rows):
        is_roll = False
        if i > 0:
            gap = (abs(r["close"] - prev_close) / prev_close) > 0.05 if prev_close else False
            oi_drop = bool(prev_oi) and r["oi"] < 0.6 * prev_oi
            if gap or oi_drop:
                is_roll = True
                if prev_close and r["close"]:
                    cum_ratio *= (prev_close / r["close"])
        s = cum_ratio
        out.append({
            "trade_date": r["trade_date"], "raw_open": r["open"], "raw_high": r["high"],
            "raw_low": r["low"], "raw_close": r["close"], "raw_volume": r["volume"], "raw_oi": r["oi"],
            "adj_open": round(r["open"] * s, 4), "adj_high": round(r["high"] * s, 4),
            "adj_low": round(r["low"] * s, 4), "adj_close": round(r["close"] * s, 4),
            "adj_volume": r["volume"], "adj_oi": r["oi"], "underlying": None,
            "change_flag": is_roll, "src": "synth_2026",
        })
        prev_oi = r["oi"]
        prev_close = r["close"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv-dir", default="/app/_mc_src")
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-12-31")
    ap.add_argument("--apply", action="store_true", help="写入 main_continuous（默认仅预览）")
    args = ap.parse_args()
    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()

    conn = _pg_conn()
    csv_dir = Path(args.csv_dir)

    # 1) CSV 源（上半年）
    csv_data: dict[str, dict[date, dict]] = {}
    csv_files = sorted(csv_dir.glob("*.csv")) if csv_dir.exists() else []
    for p in csv_files:
        prod, rows = _aggregate_csv(p, start, end)
        if prod and rows:
            csv_data[prod] = rows

    # 2) daily_bar 888 源（兜底 + 下半年）
    db_data = _load_daily_bar_888(conn, start, end)

    products = sorted(set(csv_data) | set(db_data))
    print(f"[synth] 目标品种 {len(products)}（CSV {len(csv_data)} / daily_bar888 {len(db_data)}）")

    total = 0
    for prod in products:
        merged: dict[date, dict] = {}
        if prod in db_data:
            merged.update(db_data[prod])          # 888 打底
        if prod in csv_data:
            merged.update(csv_data[prod])         # CSV 覆盖重叠日（优先）
        if not merged:
            continue
        seq = [{"trade_date": d, **v} for d, v in sorted(merged.items())]
        adj = _back_adjust(seq)

        # 交叉验证：CSV 与 daily_bar888 重叠日的 raw_close 一致性
        if prod in csv_data and prod in db_data:
            diffs = [abs(a["raw_close"] - db_data[prod][a["trade_date"]]["close"])
                     for a in adj if a["trade_date"] in db_data[prod]]
            mx = max(diffs) if diffs else 0.0
            print(f"  + {prod}: {len(adj)} 行 | CSV∩888 最大价差 {mx:.2f}")
        else:
            src = "csv_only" if prod in csv_data else "db888_only"
            print(f"  + {prod}: {len(adj)} 行 [{src}]")
        total += len(adj)

        if args.apply:
            recs = [(prod, a["trade_date"], a["raw_open"], a["raw_high"], a["raw_low"], a["raw_close"],
                     a["raw_volume"], a["raw_oi"], a["adj_open"], a["adj_high"], a["adj_low"],
                     a["adj_close"], a["adj_volume"], a["adj_oi"], a["underlying"],
                     a["change_flag"], a["src"]) for a in adj]
            sql = """
                INSERT INTO main_continuous
                  (product, trade_date, raw_open, raw_high, raw_low, raw_close, raw_volume, raw_oi,
                   adj_open, adj_high, adj_low, adj_close, adj_volume, adj_oi, underlying, change_flag, src)
                VALUES %s
                ON CONFLICT (product, trade_date) DO UPDATE SET
                  raw_open=EXCLUDED.raw_open, raw_high=EXCLUDED.raw_high, raw_low=EXCLUDED.raw_low,
                  raw_close=EXCLUDED.raw_close, raw_volume=EXCLUDED.raw_volume, raw_oi=EXCLUDED.raw_oi,
                  adj_open=EXCLUDED.adj_open, adj_high=EXCLUDED.adj_high, adj_low=EXCLUDED.adj_low,
                  adj_close=EXCLUDED.adj_close, adj_volume=EXCLUDED.adj_volume, adj_oi=EXCLUDED.adj_oi,
                  change_flag=EXCLUDED.change_flag, src=EXCLUDED.src
            """
            with conn.cursor() as cur:
                execute_values(cur, sql, recs, page_size=2000)
            conn.commit()

    print(f"[synth] 合计 {total} 行" + ("（已写入）" if args.apply else "（预览，未写入）"))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
