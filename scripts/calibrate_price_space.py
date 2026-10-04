# -*- coding: utf-8 -*-
"""P0-1 §3.1 口径判定：确定 888 信号价究竟是「原始连续价」还是「已后复权价」。

方法（以 contract_daily 真实合约日频价为真值）：
  对若干 888 样本，在参考交易日 t：
    c   = hourly_bar.<SYM>.close @ t            （信号价空间）
    off = roll_segment(SYM, min60).cum_offset @ t
    r   = contract_daily.<underlying>.close @ t  （真实合约价，真值）
    hyp_raw : 预测真实价 = c            （若信号价已是原始连续价）
    hyp_adj : 预测真实价 = c - off      （若信号价已后复权）
  选 |预测 - r|/r 更小者 → 裁决价空间。
同时校验 roll_segment 是否含 min60（反解 freq 是否对齐）。

用法（云端容器内）：
  docker exec -w /app -e PYTHONPATH=/app qhyc-api \\
    python scripts/calibrate_price_space.py [--date YYYY-MM-DD] [--symbols RB888 AG888 ...]
"""
from __future__ import annotations

import argparse
from datetime import date, datetime

import pandas as pd
from sqlalchemy import text

from app.core.db import session_scope
from app.core.symbol_code import product_of
from app.data.back_adjust import load_segments, map_offset


def _cum_offset(session, symbol, when):
    from app.execution.reverse_price import offset_at
    return offset_at(session, symbol, when)


def calibrate(session, symbols, trade_date):
    results = []
    for s in symbols:
        if not s.endswith("888"):
            continue
        when = datetime(trade_date.year, trade_date.month, trade_date.day, 15, 0, 0)
        # c 取该序列最新一根 K 线（数据可能早于参考日），并用其实际日期对齐 r，避免跨日偏差
        c_row = session.execute(text(
            "SELECT close, trade_datetime FROM hourly_bar "
            "WHERE symbol=:s AND trade_datetime <= :t ORDER BY trade_datetime DESC LIMIT 1"),
            {"s": s, "t": when}).fetchone()
        c = float(c_row[0]) if c_row else None
        c_dt = c_row[1] if c_row and c_row[1] is not None else when
        off = _cum_offset(session, s, c_dt)
        prod = product_of(s)
        row = session.execute(text(
            "SELECT underlying, exchange FROM main_contract_map "
            "WHERE product=:p AND trade_date <= :d ORDER BY trade_date DESC LIMIT 1"),
            {"p": prod, "d": trade_date}).fetchone()
        underlying = row[0] if row else None
        und_exch = row[1] if row else None
        # 2026-10-04 修正：真值合约优先 dim_main_contract_inferred（888 当日实际所跟，价格匹配
        # 反推），回退 main_contract_map.underlying（参考主力，可能≠实际所跟，如 MA888 恒跟近月）。
        inferred = session.execute(text(
            "SELECT inferred_symbol FROM dim_main_contract_inferred "
            "WHERE variety_code=:p AND trade_date <= :d ORDER BY trade_date DESC LIMIT 1"),
            {"p": prod, "d": trade_date}).scalar()
        truth = inferred or underlying
        r = None
        if truth:
            # 2026-10-04 修复：与 validator.fetch_market_price 同源（contract_code_map.observed_native
            # 字典解析 + 大小写兜底），替代 to_native——后者 CZCE 退化 3 位/SHFE 转小写，
            # 与 contract_daily 的 4 位大写存储不符，导致此前所有品种 r=None。
            from app.execution.validator import fetch_market_price
            r, _gran = fetch_market_price(session, truth, c_dt)
        if c is None or r is None:
            results.append((s, c, off, f"{underlying}/true:{truth}", r, None, "数据不足"))
            continue
        raw_dev = abs(c - r) / r
        adj_dev = abs((c - (off or 0)) - r) / r if off is not None else None
        verdict = "raw" if (adj_dev is None or raw_dev <= adj_dev) else "adj"
        results.append((s, c, off, f"{underlying}/true:{truth}", r, verdict,
                        f"c_date={c_dt.date()} raw_dev={raw_dev:.2%} adj_dev={adj_dev:.2%}"))
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=date.today().isoformat())
    ap.add_argument("--symbols", default="RB888 AG888 AU888 I888 FG888 MA888")
    args = ap.parse_args()
    td = date.fromisoformat(args.date)
    syms = args.symbols.split()
    with session_scope() as s:
        res = calibrate(s, syms, td)
    print(f"价格空间裁决（参考日 {td}，真值=contract_daily 真实合约收盘价）")
    print("-" * 78)
    raw_n = 0
    for s, c, off, und, r, v, note in res:
        print(f"{s}: c={c} off={off} underlying={und} r={r} -> {v} ({note})")
        if v == "raw":
            raw_n += 1
    valid = [v for v in (r[5] for r in res) if v in ("raw", "adj")]
    if len(valid) >= 2:
        rec = "raw" if raw_n >= len(valid) / 2 else "adj"
        note = ""
    else:
        # 有效样本不足（多因 hourly_bar 与 contract_daily 覆盖日期不一致）→ 维持默认 raw。
        # 独立推理 RB：c=3112/off=2400，若 888 已后复权则 real=712（螺纹不可能）→ 原始连续价。
        rec = "raw"
        note = "（有效样本不足，维持默认 raw；RB 推理：c=3112/off=2400，若已后复权则 real=712 不可能 → 原始连续价）"
    print("-" * 78)
    print(f"裁决：{rec}{note}")
    print(f"  样本明细：{raw_n}/{len(res)} 支持 raw，有效裁决样本 {len(valid)} 个；"
          f"reverse_price 默认 EXECUTION_PRICE_SPACE=raw。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
