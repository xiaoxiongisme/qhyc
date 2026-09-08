"""
采集编排（M1）
- 一个品种一次的完整流程：
  1. akshare 拉取日线（主源）
  2. upsert 入 daily_bar（src=akshare）
  3. tqsdk 补缺（src=tqsdk）
  4. tqsdk 二次比对 → 异常工单
- 一次手动 / 自动更新触发一个或多个品种
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Iterable

from app.core.config import MainContractSpec, get_settings
from app.core.bootstrap import release_symbol_lock, try_symbol_lock
from app.core.logging import logger
from app.ingest.akshare_source import AkShareSource
from app.ingest.tqsdk_calibrator import TqSdkCalibrator, close_tqsdk
from app.repositories._base import upsert_daily_bars
from app.repositories.symbol_repo import SymbolRepository


@dataclass
class IngestReport:
    symbol: str
    exchange: str
    product: str
    akshare_rows: int = 0
    tqsdk_filled: int = 0
    still_missing: list[str] = field(default_factory=list)
    anomalies: int = 0
    error: str | None = None             # 主源（akshare）失败才置位
    calibration_error: str | None = None  # 校准（tqsdk）失败单独记录，不阻塞主源

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "exchange": self.exchange,
            "product": self.product,
            "akshare_rows": self.akshare_rows,
            "tqsdk_filled": self.tqsdk_filled,
            "still_missing": self.still_missing,
            "anomalies": self.anomalies,
            "error": self.error,
            "calibration_error": self.calibration_error,
        }


class IngestOrchestrator:
    """采集 + 校准 主流程"""

    def __init__(self, session):
        self.session = session

    def ingest_symbol(
        self,
        spec: MainContractSpec,
        start: date | None = None,
        end: date | None = None,
        skip_calibration: bool = False,
        close_tqsdk_after: bool = True,
    ) -> IngestReport:
        """一个品种一次完整 ingest（close_tqsdk_after=False 用于批量流程）"""
        settings = get_settings()
        start = start or self._parse_history_start(settings.history_start)
        end = end or date.today()

        report = IngestReport(
            symbol=spec.symbol,
            exchange=spec.exchange,
            product=spec.product,
        )
        # R3③ 品种级锁：boot 与定时任务并发时，同品种只允许一个执行者
        if not try_symbol_lock(self.session, spec.symbol):
            report.error = "skipped: symbol locked by another ingest task"
            logger.warning(f"[ingest] {spec.symbol} 被其他任务锁定，跳过")
            return report
        try:
            # 1. akshare 拉取
            src = AkShareSource(spec.exchange)
            rows = src.fetch_daily(spec.symbol, start, end)
            if rows:
                n = upsert_daily_bars(self.session, rows)
                self.session.commit()
                report.akshare_rows = n
                logger.info(
                    f"[ingest] {spec.symbol} akshare upserted {n}"
                )

            if skip_calibration:
                return report

            # 2. tqsdk 补缺 + 比对（校准失败不阻塞主源数据，单独记录）
            try:
                cal = TqSdkCalibrator(self.session)
                filled, still_missing = cal.fill_missing(
                    spec.symbol, spec.exchange, spec.product, start, end
                )
                report.tqsdk_filled = filled
                report.still_missing = [d.isoformat() for d in still_missing]

                # 3. tqsdk 二次比对 + 工单
                tickets = cal.compare_and_ticket(
                    spec.symbol, spec.exchange, spec.product, start, end
                )
                report.anomalies = len(tickets)
            except Exception as ce:
                self.session.rollback()
                report.calibration_error = str(ce)
                logger.warning(f"[ingest] {spec.symbol} 校准失败（主源数据已入库）: {ce}")
            return report
        except Exception as e:
            self.session.rollback()
            report.error = str(e)
            logger.exception(f"[ingest] {spec.symbol} 失败: {e}")
            return report
        finally:
            release_symbol_lock(self.session, spec.symbol)
            # 单品种调用结束时释放 tqsdk 会话（批量流程由 ingest_all 统一关闭）
            if close_tqsdk_after:
                close_tqsdk()

    def ingest_all(
        self,
        start: date | None = None,
        end: date | None = None,
        only_products: Iterable[str] | None = None,
    ) -> list[IngestReport]:
        settings = get_settings()
        specs = settings.main_contracts
        if only_products:
            specs = [s for s in specs if s.product in only_products]
        results: list[IngestReport] = []
        try:
            for spec in specs:
                results.append(
                    self.ingest_symbol(spec, start, end, close_tqsdk_after=False)
                )
        finally:
            close_tqsdk()
        return results

    @staticmethod
    def _parse_history_start(s: str) -> date:
        try:
            return date.fromisoformat(s)
        except Exception:
            return date(2015, 1, 1)


__all__ = ["IngestOrchestrator", "IngestReport"]