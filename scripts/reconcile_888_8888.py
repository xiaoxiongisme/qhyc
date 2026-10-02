# -*- coding: utf-8 -*-
"""888 主连 vs 8888 指数连 双口径对拍 + contract_code_map JOIN 膨胀量化（整改 P2）。

覆盖两条整改项：
  - P2「8888 指数连 2026-03~09 补齐；bar_* 888 族 50→90；XX 占位 10 品种处置」
    → 本脚本先给出**可执行的事实基线**（各符号覆盖/区间/缺口），不臆造数据。
  - P2「contract_code_map JOIN 膨胀优化；8888/888 双口径对拍」
    → 量化 JOIN 膨胀倍数并提出/验证折叠方案（GROUP BY / DISTINCT）。

用法：
    docker exec -w /app -e PYTHONPATH=/app qhyc-api \
        python scripts/reconcile_888_8888.py --json runtime/reconcile.json
"""
from __future__ import annotations

import argparse
import json
import os

from sqlalchemy import text

from app.core.db import session_scope

BAR = "=" * 72


def _has(s, table: str) -> bool:
    return bool(s.execute(text(
        "SELECT 1 FROM information_schema.tables WHERE table_name=:t"
    ), {"t": table}).scalar())


def _coverage(s, pattern: str, src: str | None = None) -> list[dict]:
    """按符号族（LIKE 'xxx888' / 'xxx8888'）统计覆盖。"""
    where = "symbol LIKE :p"
    params = {"p": pattern}
    if src:
        where += " AND src = :s"
        params["s"] = src
    rows = s.execute(text(
        "SELECT symbol, count(*) n, min(trade_datetime) mn, max(trade_datetime) mx "
        "FROM hourly_bar WHERE " + where + " GROUP BY symbol ORDER BY symbol"
    ), params).all()
    return [{"symbol": r[0], "n": int(r[1]), "min": str(r[2]), "max": str(r[3])}
            for r in rows]


def _join_inflation(s) -> dict:
    """量化 contract_code_map JOIN 的膨胀倍数（未折叠 vs 折叠后）。"""
    if not (_has(s, "contract_code_map") and _has(s, "daily_bar")):
        return {"error": "缺少 contract_code_map 或 daily_bar"}
    # contract_code_map 是**合约级**字典（PK: exchange+std_symbol+version）。
    # 按品种码做 JOIN 会把每个日线行乘以该品种的合约数 —— 这就是"约 63 倍膨胀"。
    # 品种码 = 去掉 symbol 尾部的数字（FG888 → FG）。
    prod = "regexp_replace(d.symbol, '[0-9]+$', '')"
    raw = int(s.execute(text("""
        SELECT count(*) FROM daily_bar d
        JOIN contract_code_map m ON m.product = {0}
    """.format(prod))).scalar() or 0)
    folded = int(s.execute(text("""
        SELECT count(*) FROM (
          SELECT DISTINCT d.symbol, d.trade_date FROM daily_bar d
          JOIN contract_code_map m ON m.product = {0}
        ) t
    """.format(prod))).scalar() or 0)
    # 半连接基线：不产生行倍增（推荐写法）
    exists_n = int(s.execute(text("""
        SELECT count(*) FROM daily_bar d
        WHERE EXISTS (SELECT 1 FROM contract_code_map m WHERE m.product = {0})
    """.format(prod))).scalar() or 0)
    base = int(s.execute(text("SELECT count(*) FROM daily_bar")).scalar() or 0)
    return {
        "daily_bar_rows": base,
        "joined_rows": raw,
        "folded_rows": folded,
        "exists_rows": exists_n,
        "inflation_x": round(raw / exists_n, 2) if exists_n else None,
        "note": "JOIN(品种级) 把每行乘以合约数；EXISTS 半连接不倍增",
        "fix": "改写为 EXISTS 半连接；确需 JOIN 取字段时先对字典按 product 聚合"
               "（或 JOIN 后按 (symbol, trade_date) 折叠）再参与计算",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="akshare")
    ap.add_argument("--json", dest="json_path")
    a = ap.parse_args()

    with session_scope() as s:
        main888 = _coverage(s, "%888", a.src)
        idx8888 = _coverage(s, "%8888", a.src)
        inflation = _join_inflation(s)
        # XX 交易所占位品种（字典可解析但无行情）
        xx = s.execute(text(
            "SELECT symbol, count(*) FROM hourly_bar "
            "WHERE symbol LIKE 'XX%' GROUP BY symbol ORDER BY 2 DESC LIMIT 20"
        )).all() if _has(s, "hourly_bar") else []
        dim_variety_n = int(s.execute(text(
            "SELECT count(*) FROM dim_variety"
        )).scalar() or 0) if _has(s, "dim_variety") else 0

    print(BAR)
    print("888 / 8888 双口径对拍与 JOIN 膨胀量化")
    print(BAR)
    print("源：src={0}".format(a.src))
    print("\n888 主连：{0} 个符号".format(len(main888)))
    if main888:
        print("  区间跨度示例（前 5）：")
        for r in main888[:5]:
            print("    {0:<10} {1:>9} 行  {2} ~ {3}".format(
                r["symbol"], r["n"], r["min"][:10], r["max"][:10]))
    print("\n8888 指数连：{0} 个符号".format(len(idx8888)))
    if idx8888:
        for r in idx8888[:10]:
            print("   {0:<10} {1:>9} 行  {2} ~ {3}".format(
                r["symbol"], r["n"], r["min"][:10], r["max"][:10]))
    else:
        print("  ❌ 该源下无 8888 指数连数据（测试报告记载 2026-03~09 全缺）")

    print("\n{0}".format(BAR))
    print("contract_code_map JOIN 膨胀")
    print(BAR)
    for k, v in inflation.items():
        print("  {0:<16}: {1}".format(k, v))

    print("\nXX 占位符号（有字典无行情的疑似占位）：{0} 个".format(len(xx)))
    for r in xx[:10]:
        print("  {0:<12} {1} 行".format(r[0], r[1]))
    print("dim_variety 品种数：{0}".format(dim_variety_n))

    print("\n结论口径：8888 与 888 覆盖不一致时，跨口径 join 会静默丢样本；"
          "在补齐 8888 之前，任何同时引用两者的查询都必须先折叠并按单一口径取数。")

    if a.json_path:
        d = os.path.dirname(a.json_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.json_path, "w", encoding="utf-8") as f:
            json.dump({"main888": main888, "index8888": idx8888,
                       "join_inflation": inflation,
                       "xx_placeholders": [{"symbol": r[0], "n": r[1]} for r in xx]},
                      f, ensure_ascii=False, indent=2, default=str)
        print("\n已落盘：{0}".format(a.json_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
