# -*- coding: utf-8 -*-
"""会员持仓排名（龙虎榜）历史回填 —— 走**交易所官方源**。

用途
----
把 ``member_position_rank`` 的历史补齐到各接口允许的最早日期，并修补缺口。
两条生产链路（``rank_position.run`` 每日 17:30 / ``member_position`` 手动）都只管当天，
历史只能靠本模块一次性回填；三者写出的行同源同口径，互相幂等。

各所回溯能力（2026-09-20 实测，回填到 901 个交易日）
----------------------------------------------------
============  ==========================================  ============================
交易所        接口                                        实测可回溯至
============  ==========================================  ============================
CZCE          ``ak.get_rank_table_czce(date)``            **2023-01-03** 起（901 天补齐）
SHFE          ``ak.get_shfe_rank_table(date)``            **2023-01-03** 起（901 天补齐）
GFEX          ``ak.futures_gfex_position_rank(date, vars)``2023-11-10 起（695 天补齐）
DCE           ``app.ingest.dce_scrapling``（Scrapling）   官方约一年；**仅宿主机可跑**
INE / CFFEX   —                                            不纳入（无源 / 决策 3）
============  ==========================================  ============================

> ⚠️ 「某所只能回溯到 X 月」的初判**不可信**：2026-09-20 曾误判 SHFE 只能到 2024-07，
> 实际它一路可取到 2023-01。原因是个别日期的请求在**数据库抖动/限流时返回空**，
> 被误当作「该日无数据」并记入缺口。**结论**：回溯边界要靠「按日续传 + 逐日验证」跑出来，
> 不要靠抽样几天就下结论。

⚠️ 回填会带来**库里从未见过的历史合约**，必须同步刷新代码对照表
--------------------------------------------------------------
``contract_code_map`` 是从库内实际出现过的 symbol 反推构建的，新增历史数据后
若不重建，会出现「成员持仓里有、对照表里没有」的覆盖缺口。
故回填收尾请跑 ``--rebuild-map``（或事后单独跑
``python -m app.ingest.contract_code build``）。

⚠️ 长回填会把数据库压到崩溃（Windows Docker 特有）
--------------------------------------------------
实测 2026-09-20 两次崩溃，日志均为
``PANIC: could not write to file "pg_wal/xlogtemp.NNN": Interrupted system call``
—— Windows 上 Docker 卷的 WAL 写入被 EINTR 打断。对策：
① **多所串行**回填（不要 3 个进程同时写）；② 适当加大 ``--sleep``；
③ 崩溃恢复通常 1~3 分钟，**恢复后重跑本命令即可续传**（``gap_only`` 会跳过已有天）。

用法
----
    # 补齐 2026-06-01 至今所有缺口（默认只补缺、不覆盖已有），并刷新代码表
    docker exec -w /app qhyc-api python -m app.ingest.backfill_rank \
        --start 2026-06-01 --rebuild-map

    # 打印当前覆盖情况，不取数
    docker exec -w /app qhyc-api python -m app.ingest.backfill_rank --coverage

    # 只跑某几个所 / 强制覆盖
    docker exec -w /app qhyc-api python -m app.ingest.backfill_rank \
        --start 2026-09-01 --exchanges CZCE,GFEX --force
"""
from __future__ import annotations

import argparse
import datetime as _dt
import time
from typing import Iterable, Optional

from sqlalchemy import text

from app.core.db import get_engine
from app.core.logging import logger

#: 走 akshare 官方直连接口、可在容器内直接回填的交易所
OFFICIAL_EXCHANGES = ("CZCE", "SHFE", "GFEX")

#: 仅宿主机可回填的（Scrapling 绕瑞数，容器内默认未装依赖）
HOST_ONLY_EXCHANGES = ("DCE",)


def _runner(exchange: str):
    """取该交易所的官方回填函数（复用 rank_position 的生产实现，保证同口径同 src）。"""
    from app.ingest import rank_position as RP

    return {
        "CZCE": RP._run_czce_official,
        "SHFE": RP._run_shfe_official,
        "GFEX": RP._run_gfex_official,
    }.get(exchange)


def trading_days(start: _dt.date, end: _dt.date) -> list[_dt.date]:
    """交易日列表：优先用 akshare 的交易日历，取不到时退化为「周一到周五」。

    期货与 A 股的节假日基本一致（都是国务院假日安排），用它做**预筛**足够；
    真正非交易日会在取数时返回空，被按「非交易日」跳过。
    """
    try:
        import akshare as ak  # type: ignore

        cal = ak.tool_trade_date_hist_sina()
        days = [
            _dt.date.fromisoformat(str(x)[:10])
            for x in (cal["trade_date"] if hasattr(cal, "columns") else cal)
        ]
        picked = [d for d in days if start <= d <= end]
        if picked:
            logger.info(f"[backfill] 交易日历命中 {len(picked)} 天（{start} ~ {end}）")
            return sorted(picked)
        logger.warning("[backfill] 交易日历区间为空，退化为工作日枚举")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[backfill] 交易日历不可用（{type(e).__name__}），退化为工作日枚举")
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += _dt.timedelta(days=1)
    return out


def _existing_days(exchange: str) -> set[_dt.date]:
    eng = get_engine()
    with eng.connect() as conn:
        return {
            r[0] for r in conn.execute(
                text("SELECT DISTINCT trade_date FROM member_position_rank WHERE exchange = :e"),
                {"e": exchange},
            ).all()
        }


def coverage() -> dict:
    """当前各所覆盖情况（天数 / 日期范围 / 缺口提示）。"""
    eng = get_engine()
    out: dict = {}
    with eng.connect() as conn:
        rows = conn.execute(text("""
            SELECT exchange, COUNT(DISTINCT trade_date) nd,
                   MIN(trade_date) d0, MAX(trade_date) d1,
                   COUNT(*) rows, string_agg(DISTINCT src, ',') srcs
            FROM member_position_rank GROUP BY exchange ORDER BY exchange
        """)).all()
        for ex, nd, d0, d1, n, srcs in rows:
            out[ex] = {"days": int(nd), "first": str(d0), "last": str(d1),
                       "rows": int(n), "srcs": srcs}
        # 缺口：范围内理论交易日 - 已有日
        for ex in list(out):
            try:
                days = trading_days(_dt.date.fromisoformat(out[ex]["first"]),
                                    _dt.date.fromisoformat(out[ex]["last"]))
                have = _existing_days(ex)
                miss = [d for d in days if d not in have]
                out[ex]["missing_days"] = [str(d) for d in miss]
            except Exception as e:  # noqa: BLE001
                out[ex]["missing_days"] = f"（无法计算：{e}）"
        out["map_rows"] = int(conn.execute(
            text("SELECT COUNT(*) FROM contract_code_map")).scalar_one())
    return out


def backfill(
    start: _dt.date,
    end: _dt.date,
    exchanges: Optional[Iterable[str]] = None,
    gap_only: bool = True,
    sleep_s: float = 0.4,
) -> dict:
    """把 ``start ~ end`` 的会员持仓排名补齐。

    ``gap_only=True``（默认）：只补库里没有的天，已有数据不覆盖。
    ``gap_only=False``：全部重取（源未变时结果等价，属幂等覆盖）。
    """
    want = [e.upper() for e in (exchanges or OFFICIAL_EXCHANGES)]
    days = trading_days(start, end)
    stats: dict = {"range": [str(start), str(end)], "trading_days": len(days),
                   "per_exchange": {}, "non_trading": [], "errors": []}

    for ex in want:
        if ex in HOST_ONLY_EXCHANGES:
            stats["per_exchange"][ex] = "跳过：仅宿主机可回填（Scrapling）"
            continue
        fn = _runner(ex)
        if fn is None:
            stats["per_exchange"][ex] = f"跳过：无官方回填通道（{ex}）"
            continue

        have = _existing_days(ex) if gap_only else set()
        filled = failed = empty = 0
        for i, d in enumerate(days, 1):
            if d in have:
                continue
            try:
                res = fn(d)
            except Exception as e:  # noqa: BLE001
                failed += 1
                stats["errors"].append(f"{ex} {d}: {type(e).__name__}: {e}")
                logger.warning(f"[backfill] {ex} {d} 异常: {e}")
                continue
            if isinstance(res, int) and res > 0:
                filled += 1
                logger.info(f"[backfill] {ex} {d} +{res} 行（{i}/{len(days)}）")
            else:
                # 字符串说明：多为「非交易日」或「空数据」
                empty += 1
                if str(res).startswith("空数据"):
                    stats["non_trading"].append(f"{ex} {d}")
            time.sleep(sleep_s)
        stats["per_exchange"][ex] = {
            "filled_days": filled, "empty_days": empty, "failed_days": failed,
        }
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description="会员持仓排名官方源历史回填")
    ap.add_argument("--start", help="起始日 YYYY-MM-DD（含）")
    ap.add_argument("--end", help="结束日 YYYY-MM-DD（含），默认今天")
    ap.add_argument("--exchanges", default=None, help="逗号分隔，默认 CZCE,SHFE,GFEX")
    ap.add_argument("--force", action="store_true", help="不跳过已有日期（全量重取）")
    ap.add_argument("--coverage", action="store_true", help="只打印覆盖情况，不取数")
    ap.add_argument("--rebuild-map", action="store_true",
                    help="回填收尾刷新 contract_code_map（新增历史合约需登记）")
    ap.add_argument("--sleep", type=float, default=0.4, help="每次请求间隔秒")
    args = ap.parse_args()

    import json

    if args.coverage:
        print(json.dumps(coverage(), ensure_ascii=False, indent=2, default=str))
        return
    if not args.start:
        ap.error("--start 必填（或用 --coverage 只看覆盖）")

    start = _dt.date.fromisoformat(args.start)
    end = _dt.date.fromisoformat(args.end) if args.end else _dt.date.today()
    exs = [x.strip().upper() for x in args.exchanges.split(",")] if args.exchanges else None
    print(json.dumps(
        backfill(start, end, exchanges=exs, gap_only=not args.force, sleep_s=args.sleep),
        ensure_ascii=False, indent=2, default=str,
    ))
    if args.rebuild_map:
        # 回填进来的历史合约必须登记进对照表，否则出现「有数据、无映射」。
        # 注意：build_map 是**全量刷新**（会删掉不在新 payload 里的旧行），
        # 不能只传单表，否则会把别的表贡献的登记项清掉 —— 这里必须全量跑。
        try:
            from app.ingest import contract_code as CC

            st = CC.build_map()
            logger.info(f"[backfill] 代码对照表已刷新: {st}")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[backfill] 代码表刷新失败（不影响已回填数据）: {type(e).__name__}: {e}")
    print("== 回填后覆盖 ==")
    print(json.dumps(coverage(), ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
