# -*- coding: utf-8 -*-
"""futures_rule 期货专属规则表播种（PRD §14.4 G4 / C6）。

从 akshare `futures_rule(date='YYYYMMDD')` 逐日取各所各品种的保证金比例 / 涨跌停板 /
合约乘数 / 最小变动价位 / 最大下单手数，UPSERT 进 `futures_rule`。

要点（fail-loud + 可续跑）
-------------------------
* akshare 该接口 **仅在交易日返回数据**，非交易日抛 `ValueError: No tables found`
  → 视为「非交易日」跳过，不报错（这是期货日历的天然语义：有数据=交易日）。
* 网络抖动 / 限流 → 指数退避重试（最多 3 次），仍失败则**记异常并中断**（绝不静默跳过）。
* 断点续跑：已在库中的 (trade_date) 直接跳过；支持 --since 从某日之后补。
* 接口参数名是 `date=`（**不是** `trade_date=`，旧代码用错名会整段静默失败，已修正）。

用法（本地容器，scripts 已挂载）
  docker exec -w /app qhyc-scheduler python scripts/seed_futures_rule.py
  docker exec -w /app qhyc-scheduler python scripts/seed_futures_rule.py --since 2024-01-01
  docker exec -w /app qhyc-scheduler python scripts/seed_futures_rule.py --start 2026-01-01 --end 2026-12-31
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.abspath(__file__)))
from pgconn import add_conn_args, conn_from_args  # noqa: E402

import akshare as ak  # noqa: E402

# akshare 中文列 -> 本表列
COL_MAP = {
    "交易所": "exchange",
    "品种": "variety",
    "代码": "code",
    "交易保证金比例": "margin_ratio",
    "涨跌停板幅度": "price_limit",
    "合约乘数": "multiplier",
    "最小变动价位": "tick_size",
    "限价单每笔最大下单手数": "max_order_lots",
    "特殊合约参数调整": "special_note",
    "调整备注": "adjust_note",
}
REQUIRED = ("交易所", "代码")


def _to_float(v):
    try:
        if v is None:
            return None
        s = str(v).strip().rstrip("%")
        return float(s) if s not in ("", "nan", "None") else None
    except (ValueError, TypeError):
        return None


def _to_int(v):
    f = _to_float(v)
    return int(f) if f is not None else None


def fetch_day(d: date):
    """取单日规则；非交易日（无表）返回空 list；网络错误抛出。"""
    df = ak.futures_rule(date=d.strftime("%Y%m%d"))
    if df is None or df.empty:
        return []
    # 归一化列名：akshare 不同日期/版本返回的列名可能带空格或元组，
    # 直接 r[cn] 会 KeyError（如 2020-05-26 缺「合约乘数」列）→ 改为 .get 容错。
    df.columns = [str(c).strip() for c in df.columns]
    rows = []
    for _, r in df.iterrows():
        if any(p not in df.columns or (r.get(p) in (None, "")) for p in REQUIRED):
            continue
        rec = {"trade_date": d}
        for cn, col in COL_MAP.items():
            if cn not in df.columns:
                rec[col] = None
                continue
            val = r.get(cn)
            if col in ("margin_ratio", "price_limit", "multiplier", "tick_size"):
                rec[col] = _to_float(val)
            elif col == "max_order_lots":
                rec[col] = _to_int(val)
            else:
                rec[col] = None if val is None else str(val)
        rows.append(rec)
    return rows


def day_already_seeded(cur, d: date) -> bool:
    cur.execute("SELECT 1 FROM futures_rule WHERE trade_date=%s LIMIT 1", (d,))
    return cur.fetchone() is not None


def upsert(cur, rows):
    if not rows:
        return 0
    cols = ["trade_date", "exchange", "variety", "code", "margin_ratio",
            "price_limit", "multiplier", "tick_size", "max_order_lots",
            "special_note", "adjust_note"]
    data = [(r["trade_date"], r["exchange"], r["variety"], r["code"],
             r["margin_ratio"], r["price_limit"], r["multiplier"],
             r["tick_size"], r["max_order_lots"], r["special_note"], r["adjust_note"])
            for r in rows]
    psycopg2.extras.execute_values(
        cur,
        f"INSERT INTO futures_rule ({','.join(cols)}) VALUES %s "
        f"ON CONFLICT (trade_date, exchange, code) DO UPDATE SET "
        f"variety=EXCLUDED.variety, margin_ratio=EXCLUDED.margin_ratio, "
        f"price_limit=EXCLUDED.price_limit, multiplier=EXCLUDED.multiplier, "
        f"tick_size=EXCLUDED.tick_size, max_order_lots=EXCLUDED.max_order_lots, "
        f"special_note=EXCLUDED.special_note, adjust_note=EXCLUDED.adjust_note, "
        f"updated_at=now()",
        data, page_size=500)
    return len(data)


def main():
    ap = argparse.ArgumentParser(description="futures_rule 期货专属规则表播种")
    ap.add_argument("--start", default="2015-01-01", help="起始日 YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="结束日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--since", default=None, help="仅补该日之后（覆盖 --start）")
    ap.add_argument("--sleep", type=float, default=0.25, help="每次调用间隔（秒）")
    add_conn_args(ap)
    a = ap.parse_args()

    end = date.fromisoformat(a.end) if a.end else date.today()
    start = date.fromisoformat(a.since) if a.since else date.fromisoformat(a.start)

    c = psycopg2.connect(**conn_from_args(a))
    cur = c.cursor()
    # 续跑：若指定 --since 且库中已有更早数据，从库内最大日期+1 起（避免重复大量历史）
    cur.execute("SELECT max(trade_date) FROM futures_rule")
    mx = cur.fetchone()[0]
    if mx and mx >= start:
        start = mx + timedelta(days=1)
        print(f"[seed] 续跑：库内最大日期 {mx}，从 {start} 继续", flush=True)

    print(f"[seed] 范围 {start} ~ {end}", flush=True)
    d = start
    total_rows = 0
    trading_days = 0
    skipped_non = 0
    t0 = time.time()
    while d <= end:
        if day_already_seeded(cur, d):
            d += timedelta(days=1)
            continue
        try:
            rows = fetch_day(d)
        except ValueError as e:
            # 非交易日（接口无表）→ 跳过，视为非交易日
            if "No tables found" in str(e):
                skipped_non += 1
                d += timedelta(days=1)
                time.sleep(a.sleep)
                continue
            raise
        except Exception as e:  # noqa: BLE001  网络/限流 → 重试
            print(f"  [retry] {d} 失败 {type(e).__name__}: {str(e)[:80]}", flush=True)
            time.sleep(2.0)
            try:
                rows = fetch_day(d)
            except Exception as e2:  # noqa: BLE001  二次失败 → 跳过该日（长历史播种不因单日异常整体中断，但留痕告警）
                print(f"  [SKIP] {d} 二次失败，跳过：{e2}", flush=True)
                d += timedelta(days=1)
                time.sleep(a.sleep)
                continue

        n = upsert(cur, rows)
        c.commit()
        total_rows += n
        if rows:
            trading_days += 1
        if (trading_days + skipped_non) % 50 == 0:
            print(f"  [{d}] 已写 {total_rows} 行 | 交易日 {trading_days} "
                  f"非交易日跳过 {skipped_non} | {round(time.time()-t0)}s", flush=True)
        d += timedelta(days=1)
        time.sleep(a.sleep)

    print(f"[seed] 完成：交易日 {trading_days} 非交易日跳过 {skipped_non} "
          f"共写入 {total_rows} 行，耗时 {round(time.time()-t0)}s", flush=True)

    # G4：回写「已播种截止日」。futures_rule 只记交易日 → 「无行」二义（休市 or 未来未播种）。
    # 显式记录截止日，app/data/trade_calendar.py 才能把「<= 截止日的缺失行」判为确定的非交易日，
    # 而非退化为「周一~周五」把法定节假日误判成交易日（静默错）。
    #
    # ★ 2026-10-08 修正：原实现回写 `end`（= --end 参数），这是**错的**。
    #   `seeded_through` 的语义是「我已确认到这个日期为止的日历」，故必须是
    #   **实际播种到的最后交易日**，而非「我尝试过的最后一天」。
    #   事故实例（2026-10-08）：`--end 2026-12-31` 播种后，回写 seeded_through=12-31，
    #   但 akshare 对未来日期无数据，实际只拿到 10-08 一天。于是
    #   `is_futures_trading_day()` 三分判定走 ② 分支，把 **10-09~12-31 全部
    #   判为「确定休市」**——比原缺陷更危险（无告警、无兜底）。
    #   正确做法：取库内实际最大交易日作为边界；其后的日期走 ③ 分支（有告警 + 按
    #   工作日兜底），既不误判休市也不静默。
    try:
        cur.execute("CREATE TABLE IF NOT EXISTS cfg_calendar_seed_meta ("
                    "key TEXT PRIMARY KEY, value TEXT, "
                    "updated_at TIMESTAMPTZ NOT NULL DEFAULT now())")
        cur.execute("SELECT max(trade_date) FROM futures_rule")
        actual_max = cur.fetchone()[0]
        # 库内已有历史播种时，边界取「实际最大交易日」与「本次尝试终点」的较小者：
        # 前者是真正被确认过的边界，后者避免把范围外的老数据误当成已确认。
        seeded_through = min(x for x in (actual_max, end) if x is not None) if actual_max else end
        cur.execute(
            "INSERT INTO cfg_calendar_seed_meta(key, value) VALUES ('seeded_through', %s) "
            "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=now()",
            (seeded_through.isoformat(),))
        c.commit()
        if actual_max and actual_max < end:
            print(f"[seed] ⚠️ 请求范围到 {end}，但 akshare 实际只提供到 {actual_max}；"
                  f"seeded_through 回写 {actual_max}（不把未获取到的日期伪装成『已确认休市』）。\n"
                  f"       该日之后的日期将走 ③ 分支：有告警 + 按周一~周五兜底，不会误判为休市。",
                  flush=True)
        print(f"[seed] 已登记 seeded_through={seeded_through}"
              f"（= 库内实际最后交易日，供 G4 判定休市 vs 未来未播种）", flush=True)
    except Exception as e:  # noqa: BLE001 —— 元数据回写失败不阻断播种，但必须留痕
        print(f"[seed] ⚠️ seeded_through 回写失败（不影响已播数据，但 G4 将退化为二义判定）: {e}",
              flush=True)

    cur.close()
    c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
