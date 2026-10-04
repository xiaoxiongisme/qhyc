"""
实时 1 分钟采集器（M?，#5 改造核心）—— C9 去天勤后 akshare 单一源
- 唯一源：akshare futures_zh_minute_sina(symbol, period="1")（新浪主连，免登录）
- 输出：标准化分钟行，upsert 入 minute_bar（主键 symbol, ts，冲突跳过）

说明：
- C9 去天勤：原为 天勤 get_kline_serial(...,60) 主源，现改为 akshare 新浪主连 1 分钟。
- akshare 1 分钟标签即该分钟棒的上海时间，直接按上海本地解析入库（不叠加任何 +1h）。
- ⚠ 已知限制：akshare futures_zh_minute_sina(period="1") 每次仅返回约 1024 根、跨约 6 个
  交易日（2026-10-04 实测 RB 为 09-24~09-30）。故本采集器仅能刷新「最近 ~6 天」实时分钟；
  minute_bar 的更长历史由 CSV 导入（fetch_contract_1min.py）维护，upsert 按 (symbol, ts)
  DO NOTHING 不会覆盖旧历史。这是 C9 收敛到 akshare 单一源后的已知降韧性取舍——
  天勤 原可拉 ~9000 根（约 21 日），切换后实时回溯窗口缩短，须确保 CSV 导入定期补历史。
- 不强行对齐交易时段整点（那会丢分钟），仅剔除 NaN 价 / 1970 脏戳 / 未来戳。
- 写入幂等：ON CONFLICT (symbol, ts) DO NOTHING，重复跑安全。
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

    def close(self) -> None:
        """释放资源（C9 后无外部连接需关闭，保留接口以兼容调用方）。"""
        return

    # ---------- 公开 API ----------
    def collect_symbol(
        self, spec: MainContractSpec, data_length: int = 5000, dry_run: bool = False
    ) -> int:
        rows = self._fetch_akshare(spec)
        if not rows:
            logger.warning(f"[minute] {spec.symbol} akshare 无数据")
            return 0
        if dry_run:
            logger.info(f"[minute] {spec.symbol} DRY-RUN 拉取 {len(rows)} 行（未写库）")
            return len(rows)
        self._upsert(rows)
        logger.info(f"[minute] {spec.symbol} upserted {len(rows)}")
        return len(rows)

    def collect_all(
        self,
        only_products: Iterable[str] | None = None,
        data_length: int = 5000,
        dry_run: bool = False,
    ) -> list[dict]:
        specs = get_settings().main_contracts
        if only_products:
            specs = [s for s in specs if s.product in set(only_products)]
        results: list[dict] = []
        try:
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
        return results

    # ---------- akshare 唯一源 ----------
    def _fetch_akshare(self, spec: MainContractSpec) -> list[dict]:
        """akshare 新浪主连 1 分钟（period=1）唯一源。

        ⚠ 限制：akshare 每次仅返回约 1024 根（跨约 6 个交易日），仅刷新最近 ~6 天实时分钟；
        minute_bar 更长历史由 CSV 导入维护。见模块文档。
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
        for _, r in df.iterrows():
            dt = _parse_ak_minute_dt(r.get("datetime"))
            o = r.get("open"); h = r.get("high"); l = r.get("low"); c = r.get("close")
            if not _valid_minute_row(dt, o, h, l, c):
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
        return rows

    # ---------- upsert ----------
    def _upsert(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        # PostgreSQL 单条 INSERT 参数上限 65535；minute_bar 每行 15 列，
        # 单品种 data_length=5000 行会超限，故按 ~4000 行分块执行。
        n = 0
        for i in range(0, len(rows), 4000):
            batch = rows[i:i + 4000]
            stmt = insert(_MINUTE_BAR).values(batch).on_conflict_do_nothing(
                index_elements=["symbol", "ts"]
            )
            self.session.execute(stmt)
            n += len(batch)
        self.session.commit()
        return n


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
      1 分钟更长历史由 CSV 导入（fetch_contract_1min.py）维护。
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
