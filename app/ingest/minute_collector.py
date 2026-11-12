"""
实时 1 分钟采集器（M?，#5 改造核心）—— C9 去天勤后 akshare 单一源
- 唯一源：akshare futures_zh_minute_sina(symbol, period="1")（新浪主连，免登录）
- 输出：标准化分钟行，upsert 入 minute_bar（主键 symbol, ts，冲突跳过）

说明：
- C9 去天勤：原为 天勤 get_kline_serial(...,60) 主源，现改为 akshare 新浪主连 1 分钟。
- akshare 1 分钟标签即该分钟棒的上海时间，直接按上海本地解析入库（不叠加任何 +1h）。
- ⚠ 已知限制：``futures_zh_minute_sina`` **全周期都是 1023 根硬上限**，周期越短回溯
  越浅。1 分钟只够约 **3 个交易日**（2026-11-09 实测 BR0 回到 11-05 09:13；
  此前注释写「约 6 个交易日」是按 172 根/日估的，实际 345 根/日，估错了）。
  故本采集器仅能刷新最近 ~3 个交易日的实时分钟；更长的 1 分钟历史
  **该源取不回**（local_csv 快照止于 2026-10-30），upsert 按 (symbol, ts)
  DO NOTHING 不会覆盖旧历史。
  ★ 但 5 分钟及以上能取回得多得多（period=5 约 15 个交易日、period=60 约 8.5 个月），
  缺口日用 ``scripts/ops/backfill_bars_from_5m.py`` 以 period=5 为唯一源重建即可，
  详见 _fetch_akshare 的实测表（CL-20261109-20）。
- 不强行对齐交易时段整点（那会丢分钟），仅剔除 NaN 价 / 1970 脏戳 / 未来戳，
  以及**时间戳落在非交易时段的合成 bar**（CL-20261109-04，见 _fetch_akshare 内注释）。
- 写入幂等：ON CONFLICT (symbol, ts) DO NOTHING，重复跑安全。
- ★ CL-20261112-09（2026-11-12）：上面「刷新最近 ~3 个交易日」正是 309 行
  跨源冲突的根因——实时任务每轮重放整段历史窗口，撞上先到的历史源占住的键。
  现由 ``app/ingest/l0_write_guard.py`` 做两件事：入库前把窗口裁成最近 N 根
  （默认 120，``QHYC_L0_REALTIME_BARS`` 可调），并显式剔除已被其它 src 占据的键。
  长缺口不再靠实时重放补，改由 15:20 的日终作业用完整窗口补缺。
  ``full_window=True`` 保留完整窗口，仅供日终校验/手工排查使用。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import column, table
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.config import MainContractSpec, get_settings
from app.core.logging import logger
from app.ingest.l0_write_guard import (
    bars_span,
    filter_cross_src_occupied,
    realtime_bars_limit,
    record_ingest_audit,
    tail_bars,
)
from app.ingest.trading_window import is_collection_window

_SH_TZ = ZoneInfo("Asia/Shanghai")


# minute_bar 无 ORM 模型（plain table），用 core table 描述以便 upsert
_MINUTE_BAR = table(
    "minute_bar",
    column("symbol"), column("ts"), column("open"), column("high"), column("low"),
    column("close"), column("volume"), column("amount"), column("open_interest"),
    column("high_limit"), column("low_limit"), column("pre_close"),
    column("settle_price"), column("contract"), column("src"),
)


def _parse_ak_minute_dt(s) -> datetime | None:
    """新浪 1 分钟 datetime 标签解析为 SH 本地时间 → UTC 感知。

    C9 去天勤后 akshare 为唯一源；T4 实测确认 akshare 标签即正确上海时间（offset≈0），
    故直接按上海本地解析，不叠加任何 +1h。
    """
    if not s:
        return None
    if isinstance(s, datetime):
        dt = s
    else:
        s = str(s).strip()
        dt = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except Exception:
                continue
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_SH_TZ)
    return dt.astimezone(timezone.utc)


def _valid_minute_row(dt: datetime | None, o, h, l, c) -> bool:
    """剔除脏行：空戳 / 1970 / 未来戳 / 非数 OHLC。"""
    if dt is None or dt.tzinfo is None:
        return False
    if dt.year <= 1970:
        return False
    if dt > datetime.now(_SH_TZ) + timedelta(minutes=2):
        return False
    for v in (o, h, l, c):
        if v is None:
            return False
        try:
            fv = float(v)
        except (TypeError, ValueError):
            return False
        if fv != fv or fv in (float("inf"), float("-inf")):
            return False
    return True


class MinuteCollector:
    """实时 1 分钟采集（单 session 内批量 upsert）—— C9 去天勤后 akshare 单一源"""

    def __init__(self, session: Session):
        self.session = session
        # CL-20261112-09：本轮护栏统计，collect_all 结束时写 ops.l0_ingest_audit。
        # 守卫据此验证「裁切与跨源跳过真在跑」，而不是只看配置文件里有没有参数。
        self._guard: dict = {
            "n_symbols": 0, "n_candidates": 0, "n_written": 0,
            "n_skipped_cross_src": 0, "max_lookback_bars": 0,
            "min_ts": None, "max_ts": None, "guard_failed": False,
        }

    def close(self) -> None:
        """释放资源（C9 后无外部连接需关闭，保留接口以兼容调用方）。"""
        return

    # ---------- 公开 API ----------
    def collect_symbol(
        self, spec: MainContractSpec, data_length: int = 5000,
        dry_run: bool = False, full_window: bool = False,
    ) -> int:
        # full_window=True 仅供日终校验 / 手工排查：保留接口返回的完整窗口。
        rows = self._fetch_akshare(spec, bars=0 if full_window else None)
        if not rows:
            logger.warning(f"[minute] {spec.symbol} akshare 无数据")
            return 0
        if dry_run:
            logger.info(f"[minute] {spec.symbol} DRY-RUN 拉取 {len(rows)} 行（未写库）")
            return len(rows)
        written = self._upsert(rows)
        logger.info(f"[minute] {spec.symbol} upserted {written}/{len(rows)}")
        return len(rows)

    def collect_all(
        self,
        only_products: Iterable[str] | None = None,
        data_length: int = 5000,
        dry_run: bool = False,
        workers: int = 1,
        symbols: Iterable[str] | None = None,
    ) -> list[dict]:
        """采集全部（或指定）主连的分钟线。

        ``workers > 1``（2026-11-09，CL-20261109-17）：**并发抓取、串行落库**。
        收盘即时扫描要在 K 线收盘后十几秒内拿到数据，串行 53 个品种约 37 秒
        （实测 14:07:00→14:07:37），并发 6 路约 7 秒。抓取是纯 HTTP（akshare 新浪
        接口），落库才需要单线程：``Session`` 非线程安全，并发 upsert 还会互相等锁。
        """
        specs = get_settings().main_contracts
        if only_products:
            specs = [s for s in specs if s.product in set(only_products)]
        if symbols:
            want = {str(s) for s in symbols}
            specs = [s for s in specs if s.symbol in want]
        results: list[dict] = []
        try:
            if workers and workers > 1:
                results = self._collect_parallel(specs, int(workers), dry_run)
            else:
                for spec in specs:
                    try:
                        n = self.collect_symbol(
                            spec, data_length=data_length, dry_run=dry_run
                        )
                        results.append({"symbol": spec.symbol, "rows": n})
                    except Exception as e:
                        logger.exception(f"[minute] {spec.symbol} 异常: {e}")
                        results.append({"symbol": spec.symbol, "error": str(e)})
        finally:
            self.close()
        ok = sum(1 for r in results if "error" not in r)
        logger.info(f"[minute] collect_all done: {ok}/{len(results)} symbols")
        # 独立事务写审计：即使本轮全部失败也要留痕，守卫才不会把「采集停了」
        # 误判成「跨源冲突已消失」。
        record_ingest_audit(
            "continuous",
            self._guard,
            {
                "ok": ok,
                "total": len(results),
                "bars_limit": realtime_bars_limit(),
                "guard_failed": bool(self._guard.get("guard_failed")),
            },
        )
        return results

    def _collect_parallel(
        self, specs: list, workers: int, dry_run: bool
    ) -> list[dict]:
        """并发抓取 + 串行落库（见 :meth:`collect_all` 的 workers 说明）。"""
        from concurrent.futures import ThreadPoolExecutor

        def _fetch(spec):
            try:
                return spec, self._fetch_akshare(spec), None
            except Exception as e:  # noqa: BLE001 —— 单品种失败不能拖垮整轮
                return spec, None, e

        with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
            fetched = list(ex.map(_fetch, specs))

        results: list[dict] = []
        for spec, rows, err in fetched:
            if err is not None:
                logger.warning(f"[minute] {spec.symbol} 抓取失败: {err}")
                results.append({"symbol": spec.symbol, "error": str(err)})
                continue
            if not rows:
                logger.warning(f"[minute] {spec.symbol} akshare 无数据")
                results.append({"symbol": spec.symbol, "rows": 0})
                continue
            if dry_run:
                results.append({"symbol": spec.symbol, "rows": len(rows)})
                continue
            try:
                self._upsert(rows)
                results.append({"symbol": spec.symbol, "rows": len(rows)})
            except Exception as e:  # noqa: BLE001
                logger.exception(f"[minute] {spec.symbol} 落库异常: {e}")
                results.append({"symbol": spec.symbol, "error": str(e)})
        return results


    # ---------- akshare 唯一源 ----------
    def _fetch_akshare(
        self, spec: MainContractSpec, bars: int | None = None
    ) -> list[dict]:
        """akshare 新浪主连 1 分钟（period=1）唯一源。

        ``bars``：``None`` = 按 ``realtime_bars_limit()`` 裁切（实时链路默认）；
        ``0`` 或负数 = 保留完整窗口（日终校验）；正数 = 指定根数（测试/灰度）。

        ⚠ 限制（2026-11-09 实测，不是猜的）：``futures_zh_minute_sina`` 全周期
        都是 **1023 根硬上限**，周期越短回溯越浅：

            period=1   1023 根 → 约 3 个交易日（BR0 实测回到 11-05 09:13）
            period=5   1023 根 → 约 15 个交易日（→ 10-20）
            period=15  1023 根 → 约 50 个交易日（→ 08-28）
            period=60  1023 根 → 约 8.5 个月（→ 02-25）

        ``datalen`` / ``num`` / ``date`` 参数均被服务端忽略（实测无效），
        故 1 分钟超过 3 个交易日的历史**无法从该源取回**。

        但 **5 分钟及以上可以**：缺口日用 ``scripts/ops/backfill_bars_from_5m.py``
        以 period=5 为唯一源重建（经生产同源聚合 SQL 派生 15/30/60，
        保证跨周期对拍自洽）。CL-20261109-20 即按此法补了 BR888/PF888 的
        10-30、11-02、11-03 三天。
        """
        import akshare as ak  # type: ignore

        sina_sym = f"{spec.product.lower()}0"  # 新浪主连 = 品种小写 + 0
        try:
            df = ak.futures_zh_minute_sina(symbol=sina_sym, period="1")
        except Exception as e:
            logger.warning(f"[minute] akshare {spec.symbol} ({sina_sym}) 失败: {e}")
            return []
        if df is None or df.empty:
            return []
        rows: list[dict] = []
        n_offhour = 0
        for _, r in df.iterrows():
            dt = _parse_ak_minute_dt(r.get("datetime"))
            o = r.get("open"); h = r.get("high"); l = r.get("low"); c = r.get("close")
            if not _valid_minute_row(dt, o, h, l, c):
                continue
            # ★ CL-20261109-04（2026-11-09）：休市期新浪会返回「时间戳落在非交易时段」的
            # 合成 bar。实测 ag0 收盘于 11-07 02:30，紧接着给出 2026-11-09 00:00
            # （volume=121，OHLC 齐全），而 11-09 是周一、周一凌晨无夜盘。
            # 这类 bar 进了 l0 → 60m 聚合 → bar_60m_ba（V5X 唯一信号源），会**占住状态机
            # 上一根的位置**参与 EMA/ATR/开平判定，且全程不报错。故在入库前逐根按
            # 交易时段判据（与 cron_fetch_1min 同一函数）拦掉。
            # 注意判的是「这根 bar 自己的时间戳」，不是 now()——历史窗口里的合法 bar
            # 必须放行，否则 1024 根历史会被整体误杀。
            if not is_collection_window(dt):
                n_offhour += 1
                continue
            vol = r.get("volume")
            oi = r.get("hold")
            rows.append(
                {
                    "symbol": spec.symbol,
                    "ts": dt,
                    "open": float(o),
                    "high": float(h),
                    "low": float(l),
                    "close": float(c),
                    "volume": int(vol) if vol is not None else 0,
                    "amount": None,  # akshare 1 分钟 K 无成交额字段
                    "open_interest": int(oi) if oi is not None else None,
                    "high_limit": None,
                    "low_limit": None,
                    "pre_close": None,
                    "settle_price": None,
                    "contract": None,  # 主连无单一合约
                    "src": "akshare_1min",
                }
            )
        if n_offhour:
            logger.warning(
                f"[minute] {spec.symbol} 丢弃 {n_offhour} 根非交易时段 bar"
                f"（休市期合成 bar，CL-20261109-04）")
        # ★ CL-20261112-09：在逐根过滤**之后**裁切。先裁切再过滤会被尾部的
        # 非交易时段合成 bar 吃掉窗口，导致有效根数不足。
        limit = realtime_bars_limit() if bars is None else bars
        return tail_bars(rows, limit)

    # ---------- upsert ----------
    def _upsert(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        # ★ CL-20261112-09：显式跳过已被其它 src 占据的历史键。
        # 此前每轮重放都会尝试 INSERT、被 DO NOTHING 吞掉、再被 078 触发器记一笔，
        # 于是 309 个键的 n_attempts 永久累加。现在源头不提交，冲突表自然安静。
        src = str(rows[0].get("src") or "akshare_1min")
        kept, n_skipped, guard_failed = filter_cross_src_occupied(
            self.session, rows, src
        )
        self._tally(rows, len(kept), n_skipped, guard_failed)
        if not kept:
            self.session.commit()
            return 0
        # PostgreSQL 单条 INSERT 参数上限 65535；minute_bar 每行 15 列，
        # 单品种 data_length=5000 行会超限，故按 ~4000 行分块执行。
        n = 0
        for i in range(0, len(kept), 4000):
            batch = kept[i:i + 4000]
            stmt = insert(_MINUTE_BAR).values(batch).on_conflict_do_nothing(
                index_elements=["symbol", "ts"]
            )
            self.session.execute(stmt)
            n += len(batch)
        self.session.commit()
        if n_skipped:
            logger.info(
                f"[minute] 跳过 {n_skipped} 个已被其它 src 占据的历史键（keep-first）"
            )
        return n

    def _tally(
        self, rows: list[dict], n_written: int, n_skipped: int, guard_failed: bool
    ) -> None:
        """累计本轮护栏指标，供 collect_all 写 ops.l0_ingest_audit。"""
        g = self._guard
        g["n_symbols"] += 1
        g["n_candidates"] += len(rows)
        g["n_written"] += n_written
        g["n_skipped_cross_src"] += n_skipped
        g["max_lookback_bars"] = max(int(g["max_lookback_bars"]), bars_span(rows))
        ts_all = [r["ts"] for r in rows]
        lo, hi = min(ts_all), max(ts_all)
        if g["min_ts"] is None or lo < g["min_ts"]:
            g["min_ts"] = lo
        if g["max_ts"] is None or hi > g["max_ts"]:
            g["max_ts"] = hi
        g["guard_failed"] = bool(g["guard_failed"] or guard_failed)


__all__ = ["MinuteCollector"]


def _main() -> None:
    """临时独立入口：akshare 拉 1 分钟 → upsert 入 minute_bar（幂等，重复跑安全）。

    用法（在 scheduler 容器内执行，本地 docker 即 qhyc 的「本地」部署）：
        # 1) 先 dry-run 验证：只拉取统计、不写库
        docker compose exec -T scheduler python -m app.ingest.minute_collector \\
            --products RB --dry-run

        # 2) 真实写入单品种最近 ~1024 根 1 分钟（akshare 硬上限）
        docker compose exec -T scheduler python -m app.ingest.minute_collector \\
            --products RB

        # 3) 全主连品种
        docker compose exec -T scheduler python -m app.ingest.minute_collector

    说明：
    - akshare futures_zh_minute_sina(period=1) 硬上限约 1024 根（约最近 6 个交易日）；
      1 分钟更长历史由历史 CSV 批量导入维护。
    - upsert 用 ON CONFLICT (symbol, ts) DO NOTHING，已存在行跳过，可反复跑。
    - 本入口仅供临时/手动补采；常规实时采集仍由 scheduler._minute_and_bars_job 负责。
    """
    import argparse

    from app.core.db import session_scope

    p = argparse.ArgumentParser(
        description="临时 1 分钟采集（akshare → minute_bar，幂等 upsert）"
    )
    p.add_argument(
        "--products",
        help="逗号分隔品种码，如 RB,FG；省略=全部主连",
        default=None,
    )
    p.add_argument(
        "--data-length",
        type=int,
        default=5000,
        help="预留参数（akshare period=1 实际返回约 1024 根，忽略此值）",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只拉取统计、不写库（用于先验证连通与行数）",
    )
    args = p.parse_args()

    only = (
        [x.strip().upper() for x in args.products.split(",") if x.strip()]
        if args.products
        else None
    )

    with session_scope() as s:
        mc = MinuteCollector(s)
        results = mc.collect_all(
            only_products=only, data_length=args.data_length, dry_run=args.dry_run
        )

    ok = [r for r in results if "error" not in r]
    err = [r for r in results if "error" in r]
    total = sum(r.get("rows", 0) for r in ok)
    tag = " [DRY-RUN 未写库]" if args.dry_run else ""
    print(
        f"[minute] 完成: 成功 {len(ok)}/{len(results)} 品种, 累计 {total} 行{tag}"
    )
    for r in err:
        print(f"  ! {r['symbol']}: {r['error']}")


if __name__ == "__main__":
    _main()
