#!/usr/bin/env python3
"""跨周期自洽校验：低周期 bar 两两聚合后必须精确等于高周期 bar。

为什么需要它
------------
2026-10-31 用它抓到了一个纯看「网格时刻」发现不了的真 bug：每个交易小节
**最后一分钟**同时存在 ``tqsdk_1min``（起点约定，末根 14:59）与
``akshare_1min``（终点约定，末根 15:00）两个源，归一化后 ts_n 相同，但去重
若按原始 ts 做则两条都保留 → 该分钟被双倍计入：

* volume/amount 翻倍（实测 bar_30m 某根多出 856）；
* ``min(low)`` 吃到另一源的脏值（实测 2940 vs 真实 3545，呈 -17% 假暴跌）；
* ``array_agg(close ORDER BY ts_n DESC)[1]`` 在 ts_n 并列时顺序未定义，
  导致同一份数据在 30m 与 60m 上给出**自相矛盾的收盘价**。

当时「网格违规」计数是 0，只有把相邻周期对拍才暴露出来。所以这份校验与
``check_bar_grid_consistency.py``（只管网格定义）互补，二者都该定期跑。

校验规则（由同花顺固定网格推导，不可想当然）
--------------------------------------------
30m 的 13:45 那根**跨越午休**（上午 11:15-11:30 的 15 分钟并入下午首根），
因此对拍映射不能按「相邻两根」机械推断：

    30m 09:30 = 15m 09:15 + 15m 09:30        30m 15:00 = 15m 15:00（残段，单根）
    30m 10:00 = 15m 09:45 + 15m 10:00        60m 10:00 = 30m 09:30 + 30m 10:00
    30m 10:45 = 15m 10:15 + 15m 10:45        60m 11:15 = 30m 10:45 + 30m 11:15
    30m 11:15 = 15m 11:00 + 15m 11:15        60m 14:15 = 30m 13:45 + 30m 14:15
    30m 13:45 = 15m 11:30 + 15m 13:45  ←★跨午休   60m 15:00 = 30m 14:45 + 30m 15:00
    30m 14:15 = 15m 14:00 + 15m 14:15
    30m 14:45 = 15m 14:30 + 15m 14:45

夜盘不参与：夜盘 bar 归属「交易日」涉及跨日历日，且各品种收盘时间不同，
一概用 ``bucket::date`` 会串味；这里只校验日盘（09:00~15:00），足以覆盖
绝大多数污染场景。

用法::

    python scripts/validate/check_cross_tf_consistency.py               # 默认近 30 天
    python scripts/validate/check_cross_tf_consistency.py --days 365
    python scripts/validate/check_cross_tf_consistency.py --symbol RB888
    python scripts/validate/check_cross_tf_consistency.py --days 365 --whitelist-only

``--whitelist-only`` 的由来（CL-20261109-19）
-------------------------------------------
2026-11-09 只把 **38 个白名单主连**迁到会话网格（用户决策：中金所 8 个与其余非
白名单品种**不动**）。这些品种的历史仍是旧网格（左沿标签），而本脚本的映射表按
新网格推导 —— 全库扫会把「设计如此」的差异算成不一致，于是每天 09:30 巡检必然
报红并推送。68,000 行里 9 行真问题会被几千行噪音埋掉，正是要防的「告警疲劳」。
故巡检默认只看白名单；**当下**的标签合法性仍由 ``check_db_bar_grid.py``（近 3 天，
全品种）覆盖，历史侧的排除是显式的、会打印排除品种数、且进台账。

退出码：0 = 全部自洽；1 = 发现不一致（会打印前若干条明细）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402

from app.core.db import get_engine  # noqa: E402

#: 日盘 30m 桶 → 构成它的 15m 桶墙钟时刻
_MAP_15_TO_30: dict[str, list[str]] = {
    "09:30": ["09:15", "09:30"],
    "10:00": ["09:45", "10:00"],
    "10:45": ["10:15", "10:45"],
    "11:15": ["11:00", "11:15"],
    "13:45": ["11:30", "13:45"],  # ★ 跨午休合并
    "14:15": ["14:00", "14:15"],
    "14:45": ["14:30", "14:45"],
    "15:00": ["15:00"],           # ★ 残段只有一根
}

#: 日盘 60m 桶 → 构成它的 30m 桶墙钟时刻
_MAP_30_TO_60: dict[str, list[str]] = {
    "10:00": ["09:30", "10:00"],
    "11:15": ["10:45", "11:15"],
    "14:15": ["13:45", "14:15"],
    "15:00": ["14:45", "15:00"],
}


def _values_clause(mapping: dict[str, list[str]]) -> tuple[str, dict]:
    """把映射拼成 VALUES 列表，返回 (SQL片段, 绑定参数)。"""
    frag, params = [], {}
    for i, (hi, parts) in enumerate(mapping.items()):
        frag.append(f"(:h{i}, ARRAY[{', '.join(f':p{i}_{j}' for j in range(len(parts)))}])")
        params[f"h{i}"] = hi
        for j, p in enumerate(parts):
            params[f"p{i}_{j}"] = p
    return ", ".join(frag), params


def _check_one(conn, *, hi_tbl: str, lo_tbl: str,
               mapping: dict[str, list[str]], days: int,
               symbol: str | None, limit: int,
               sym_set: list[str] | None = None) -> list[str]:
    frag, params = _values_clause(mapping)
    params["days"] = days
    params["lim"] = limit
    sym_filter = "AND b.symbol = :sym" if symbol else ""
    if symbol:
        params["sym"] = symbol
    elif sym_set is not None:
        # 白名单范围。必须用 ANY(:list) 而不是拼 IN 字面量：这个参数会进
        # Ctx 式的缓存键 / 查询计划，拼字面量会让每次调用生成新 plan。
        sym_filter = "AND b.symbol = ANY(:sym_set)"
        params["sym_set"] = list(sym_set)

    sql = f"""
        WITH m(hi_tm, parts) AS (VALUES {frag})
        -- 每一段都必须 COALESCE：低周期缺桶时 LATERAL 里的聚合值为 NULL（count=0），
        -- 而 PG 的 || 遇 NULL 会让**整条字符串**变 NULL → Python 侧 r[0].replace() 直接
        -- AttributeError。这个崩溃恰好只发生在「真的缺 bar」的场景，等于「越该报错越崩」。
        SELECT b.symbol || ' ' || to_char(b.bucket, 'YYYY-MM-DD HH24:MI')
                 || '  ' || '{{k}}=' || COALESCE(b.{{k}}::text, 'NULL')
                 || '  agg=' || COALESCE(a.{{k}}::text, 'NULL')
                 || '  n=' || a.n
        FROM l1_mkt.{hi_tbl} b
        JOIN m ON m.hi_tm = to_char(b.bucket, 'HH24:MI')
        JOIN LATERAL (
            SELECT {{agg}} AS {{k}}, count(*) AS n
            FROM l1_mkt.{lo_tbl} x
            WHERE x.symbol = b.symbol
              -- 可索引写法：直接算出低周期目标时刻戳（日期 + 墙钟分），而非对 bucket 套函数，
              -- 否则会对百万级低周期表做全表顺序扫描（每个高周期行扫一遍）。
              AND x.bucket = ANY(
                    ARRAY(SELECT b.bucket::date + p::time FROM unnest(m.parts) AS _(p))
                  )
        ) a ON true
        WHERE b.bucket::time BETWEEN '09:00' AND '15:00'
          AND b.bucket >= (now() - make_interval(days => :days))
          {sym_filter}
          AND (a.{{k}} IS NULL OR a.{{k}} <> b.{{k}} OR a.n = 0)
        LIMIT :lim
    """
    bad: list[str] = []
    # OHLC 用「首/最大/最小/末」逐项比对，volume 用 sum；任一项不等即视为不一致
    for kind, agg in (
        ("open", "(array_agg(x.open  ORDER BY x.bucket))[1]"),
        ("high", "max(x.high)"),
        ("low", "min(x.low)"),
        ("close", "(array_agg(x.close ORDER BY x.bucket DESC))[1]"),
        ("volume", "sum(x.volume)"),
    ):
        q = text(sql.format(k=kind, agg=agg))
        rows = conn.execute(q, params).fetchall()
        # SQL 侧已 COALESCE，这里再兜一层：校验脚本不该在「发现问题的行」上崩掉。
        bad.extend((r[0] or "<NULL row>").replace("{k}=", f"{kind}: ") for r in rows)
    return bad


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30, help="回溯天数，默认 30")
    ap.add_argument("--symbol", default=None, help="只校验某个品种")
    ap.add_argument("--limit", type=int, default=15, help="每种字段打印的明细上限")
    ap.add_argument("--whitelist-only", action="store_true",
                    help="只校验 l3_ref.tradable_variety 的 38 个白名单主连")
    args = ap.parse_args()

    # get_engine() 从 settings.db_url 取 DSN（容器内 / 已配置环境下可用），
    # 不再依赖 DATABASE_URL 环境变量（该变量 get_engine 本身并不读取）。
    try:
        eng = get_engine()
    except Exception as exc:  # noqa: BLE001
        print(f"[FAIL] 无法获取数据库引擎（settings.db_url 未配置或 DB 不可达）：{exc}")
        return 1
    problems: list[str] = []
    with eng.connect() as conn:
        sym_set = None
        if args.whitelist_only and not args.symbol:
            sym_set = [r[0] for r in conn.execute(text(
                "SELECT variety_code || '888' FROM l3_ref.tradable_variety "
                "WHERE effective_to IS NULL ORDER BY 1")).fetchall()]
            if not sym_set:
                print("[FAIL] --whitelist-only 拿到空白名单，拒绝在「零品种」上判绿")
                return 1
            total = conn.execute(text(
                "SELECT count(DISTINCT symbol) FROM l1_mkt.bar_30m "
                "WHERE bucket >= now() - make_interval(days => :d)"),
                {"d": args.days}).scalar() or 0
            print(f"[scope] 白名单 {len(sym_set)} 个品种；同窗口库内共 {total} 个品种，"
                  f"排除 {max(int(total) - len(sym_set), 0)} 个（非白名单历史仍为旧网格，"
                  f"见 CL-20261109-19）")
        problems += _check_one(conn, hi_tbl="bar_30m", lo_tbl="bar_15m",
                               mapping=_MAP_15_TO_30, days=args.days,
                               symbol=args.symbol, limit=args.limit,
                               sym_set=sym_set)
        problems += _check_one(conn, hi_tbl="bar_60m", lo_tbl="bar_30m",
                               mapping=_MAP_30_TO_60, days=args.days,
                               symbol=args.symbol, limit=args.limit,
                               sym_set=sym_set)

    if problems:
        print(f"[FAIL] 跨周期聚合不一致 {len(problems)} 处（前 {args.limit * 5} 条）：")
        for p in problems[: args.limit * 5]:
            print("  -", p)
        return 1

    scope = (f"symbol={args.symbol}" if args.symbol
             else f"白名单 {len(sym_set)} 个品种" if sym_set
             else "全部品种")
    print(f"[OK] 跨周期自洽：30m==15m 两两聚合、60m==30m 两两聚合均一致 "
          f"（近 {args.days} 天日盘，{scope}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
