#!/usr/bin/env python
"""以 roll_segment 为唯一真源重建 main_continuous 的 change_flag / adj_*。

背景（见 D:/过程 tasklog/2026/2026-10-10_1205_WB_复权三层矛盾_MA888_04-07.md）：
原 synthesize_main_continuous_2026.py 用「OI 骤降>40% 或 价格跳空>5%」独立启发式判定换月，
导致 main_continuous.change_flag 大量误报/漏报（普查 433/442=98% 为幽灵换月），并把真实行情
跳空（如 MA 04-08 的 -12%）吸收进 adj，使复权序列失真。

本脚本：
- change_flag：直接取自 roll_segment.roll_ts（真源信号），不再用 OI/跳空启发式。
- adj_*：保留项目既有的比率法（与 synthesize 同约定，锚定首行），但只在 roll_segment 真换月处
  才乘以连续性比率；非换月的真实价格跳空原样保留 → 修复「缺口被抹平」。
- underlying：补填 roll_segment 该日期所属 segment 的 contract_code（原 synth 全为 None）。
- 不依赖 roll_segment 的 cum_offset/roll_delta 数值，避免其近期段可疑 offset 传染。

用法（容器内，DATABASE_URL 指向 timescaledb）：
  python scripts/rebuild_main_continuous_from_roll_segment.py            # dry-run（仅统计/样例）
  python scripts/rebuild_main_continuous_from_roll_segment.py --apply    # 写入（当前覆盖 2026-01-01+）
  # (c) 往前补：把 --start 设到 2015-01-01 即可把 coverage 前推（daily_bar 888 与 roll_segment 均回溯到 2015）
  python scripts/rebuild_main_continuous_from_roll_segment.py --start 2015-01-01 --apply
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime

import psycopg2
from psycopg2.extras import execute_values


def _pg_conn():
    url = os.getenv("DATABASE_URL")
    if url and url.startswith("postgresql"):
        return psycopg2.connect(url)
    return psycopg2.connect(
        host=os.getenv("POSTGRES_HOST", os.getenv("PGHOST", "timescaledb")),
        port=int(os.getenv("POSTGRES_PORT", os.getenv("PGPORT", "5432"))),
        user=os.getenv("POSTGRES_USER", os.getenv("PGUSER", "futures")),
        password=os.getenv("POSTGRES_PASSWORD", os.getenv("PGPASSWORD", "")),
        dbname=os.getenv("POSTGRES_DB", os.getenv("PGDATABASE", "futures")),
    )


def _f(x):
    """安全转 float：NULL/None -> 0.0（保留行，至少修正 change_flag）。"""
    return float(x) if x is not None else 0.0


def load_daily_888(conn, start: date, end: date) -> dict[str, dict[date, dict]]:
    """daily_bar 的 888 主连日线 -> {symbol: {date: row}}。用于 (c) 往前补（main_continuous 未覆盖的日期）。"""
    out: dict[str, dict[date, dict]] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, trade_date, open, high, low, close, volume, oi "
            "FROM daily_bar WHERE symbol LIKE '%%888' AND trade_date BETWEEN %s AND %s ORDER BY symbol, trade_date",
            (start, end),
        )
        for sym, td, o, h, l, c, v, oi in cur.fetchall():
            out.setdefault(sym, {})[td] = {
                "open": _f(o), "high": _f(h), "low": _f(l), "close": _f(c),
                "volume": int(v or 0), "oi": int(oi or 0),
            }
    return out


def load_raw_from_mc(conn, start: date, end: date) -> dict[str, dict[date, dict]]:
    """以现有 main_continuous.raw_* 为权威原始序列（已合并 CSV+daily_bar888，且未被复权 bug 污染）。
    用于 (a) 原地修复：只重算 change_flag/adj/underlying，raw_* 原样保留。"""
    out: dict[str, dict[date, dict]] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT product, trade_date, raw_open, raw_high, raw_low, raw_close, raw_volume, raw_oi "
            "FROM l2_adj.main_continuous WHERE trade_date BETWEEN %s AND %s ORDER BY product, trade_date",
            (start, end),
        )
        for prod, td, o, h, l, c, v, oi in cur.fetchall():
            sym = prod + "888"
            out.setdefault(sym, {})[td] = {
                "open": _f(o), "high": _f(h), "low": _f(l), "close": _f(c),
                "volume": int(v or 0), "oi": int(oi or 0),
            }
    return out


def load_roll_segment(conn, symbols: list[str], freq: str = "min15") -> dict[str, dict]:
    """对每个 symbol：roll_ts 日期集合 + segment 列表（供 underlying 查表）。

    只取锚定周期 freq（默认 min15，与 build_roll_segments.ANCHOR_FREQ 一致）。
    理由：roll_segment 各周期把同一笔换月映射到相邻 bar（09-08/09-10 等），
    跨周期取并集会引入 ±几天的误报；锚定周期 min15 的 roll_ts 与
    main_contract_map.change_flag 日期严格对齐，不会多出幽灵换月。
    """
    out: dict[str, dict] = {}
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, seg_start, seg_end, contract_code, roll_ts "
            "FROM l2_adj.roll_segment WHERE symbol = ANY(%s) AND freq = %s ORDER BY symbol, seg_start",
            (symbols, freq),
        )
        for sym, seg_start, seg_end, contract_code, roll_ts in cur.fetchall():
            d = out.setdefault(sym, {"roll_dates": set(), "segments": []})
            if roll_ts is not None:
                d["roll_dates"].add(roll_ts.date())
            d["segments"].append((seg_start.date(), seg_end.date() if seg_end else None, contract_code))
    return out


def underlying_at(segments, d: date) -> str | None:
    for s_start, s_end, code in segments:
        if d >= s_start and (s_end is None or d <= s_end):
            return code
    # 落在最后一段之后（seg_end 为 null）
    if segments and (segments[-1][1] is None) and d >= segments[-1][0]:
        return segments[-1][2]
    return None


def rebuild(symbol: str, daily: dict[date, dict], roll: dict) -> list[dict]:
    """比率法复权，仅 roll_segment 真换月处中和跳空。锚定首行（与 synthesize 同约定）。"""
    roll_dates = roll.get("roll_dates", set())
    segments = roll.get("segments", [])
    seq = sorted(daily.items())  # [(date, row)]
    if not seq:
        return []
    out = []
    cum_ratio = 1.0
    prev_close = seq[0][1]["close"]
    for i, (td, r) in enumerate(seq):
        is_roll = td in roll_dates
        if i > 0 and is_roll and prev_close and r["close"]:
            cum_ratio *= (prev_close / r["close"])
        s = cum_ratio
        out.append({
            "trade_date": td,
            "raw_open": r["open"], "raw_high": r["high"], "raw_low": r["low"], "raw_close": r["close"],
            "raw_volume": r["volume"], "raw_oi": r["oi"],
            "adj_open": round(r["open"] * s, 4), "adj_high": round(r["high"] * s, 4),
            "adj_low": round(r["low"] * s, 4), "adj_close": round(r["close"] * s, 4),
            "adj_volume": r["volume"], "adj_oi": r["oi"],
            "underlying": underlying_at(segments, td),
            "change_flag": is_roll, "src": "rs_rebuild",
        })
        prev_close = r["close"]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="2026-12-31")
    ap.add_argument("--apply", action="store_true", help="写入 main_continuous（默认仅 dry-run）")
    ap.add_argument("--symbols", default=None, help="逗号分隔的 888 symbol，如 MA888,AG888；默认全部")
    ap.add_argument("--source", default="mc",
                    help="raw 源：mc=用现有 main_continuous.raw_*（覆盖最全，默认，用于(a)原地修复）；"
                         "daily=用 daily_bar 888（用于(c)往前补到无 main_continuous 覆盖的日期）")
    ap.add_argument("--roll-freq", default="min15",
                    help="取 roll_segment 中哪个 freq 的 roll_ts 作为换月信号（默认 min15，即锚定周期；"
                         "不要用跨周期并集，否则会引入 ±几天误报）")
    args = ap.parse_args()
    start = datetime.strptime(args.start, "%Y-%m-%d").date()
    end = datetime.strptime(args.end, "%Y-%m-%d").date()

    conn = _pg_conn()
    if args.source == "daily":
        daily = load_daily_888(conn, start, end)
    else:
        daily = load_raw_from_mc(conn, start, end)
    syms = args.symbols.split(",") if args.symbols else list(daily.keys())
    # 仅保留有 daily 数据的 symbol
    syms = [s for s in syms if s in daily]
    roll = load_roll_segment(conn, syms, freq=args.roll_freq)

    total = 0
    print(f"[rebuild] 窗口 {start}~{end} | symbol {len(syms)} | apply={args.apply}")
    for sym in sorted(syms):
        prod = sym[:-3] if sym.endswith("888") else sym
        rows = rebuild(sym, daily[sym], roll.get(sym, {}))
        if not rows:
            continue
        n_flag = sum(1 for r in rows if r["change_flag"])
        total += len(rows)
        # dry-run 报告
        if not args.apply:
            n_roll_in_window = sum(1 for d in roll.get(sym, {}).get("roll_dates", set()) if start <= d <= end)
            tag = ""
            if prod == "MA":  # 重点样例
                samp = [(str(r["trade_date"]), r["raw_close"], r["adj_close"], r["change_flag"],
                         r["underlying"]) for r in rows
                        if "2026-04-07" <= str(r["trade_date"]) <= "2026-04-08"]
                tag = "  <<MA 04-07/08 样例 " + "; ".join(
                    f"{d} raw={rc} adj={ac} flag={f} und={u}" for d, rc, ac, f, u in samp) + ">>"
            print(f"  {prod}: {len(rows)} 行 | 新change_flag={n_flag} | roll_segment窗口内roll={n_roll_in_window}{tag}")
        else:
            recs = [(prod, r["trade_date"], r["raw_open"], r["raw_high"], r["raw_low"], r["raw_close"],
                     r["raw_volume"], r["raw_oi"], r["adj_open"], r["adj_high"], r["adj_low"],
                     r["adj_close"], r["adj_volume"], r["adj_oi"], r["underlying"],
                     r["change_flag"], r["src"]) for r in rows]
            sql = """
                INSERT INTO l2_adj.main_continuous
                  (product, trade_date, raw_open, raw_high, raw_low, raw_close, raw_volume, raw_oi,
                   adj_open, adj_high, adj_low, adj_close, adj_volume, adj_oi, underlying, change_flag, src)
                VALUES %s
                ON CONFLICT (product, trade_date) DO UPDATE SET
                  raw_open=EXCLUDED.raw_open, raw_high=EXCLUDED.raw_high, raw_low=EXCLUDED.raw_low,
                  raw_close=EXCLUDED.raw_close, raw_volume=EXCLUDED.raw_volume, raw_oi=EXCLUDED.raw_oi,
                  adj_open=EXCLUDED.adj_open, adj_high=EXCLUDED.adj_high, adj_low=EXCLUDED.adj_low,
                  adj_close=EXCLUDED.adj_close, adj_volume=EXCLUDED.adj_volume, adj_oi=EXCLUDED.adj_oi,
                  underlying=EXCLUDED.underlying, change_flag=EXCLUDED.change_flag, src=EXCLUDED.src
            """
            with conn.cursor() as cur:
                execute_values(cur, sql, recs, page_size=2000)
            conn.commit()
            print(f"  {prod}: upsert {len(rows)} 行 (change_flag={n_flag})")

    print(f"[rebuild] 合计 {total} 行" + ("（已写入）" if args.apply else "（dry-run，未写入）"))
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
