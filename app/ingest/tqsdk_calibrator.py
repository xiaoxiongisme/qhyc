"""
tqsdk 校准器（M1，§4.1）
- 拉取 tqsdk 日线
- 与 akshare 入库结果比对
- 偏差超阈值（默认 0.5%）→ 生成异常工单 anomaly_ticket
- 缺失检测 → 用 tqsdk 补缺并入库（src=tqsdk）
- 异常处置：只生成工单，**不**自动覆盖（由人工裁决，⑰）

依赖：tqsdk
"""
from __future__ import annotations

import threading
from datetime import date, datetime
from decimal import Decimal
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import CalibrationError
from app.core.logging import logger
from app.ingest.utils import attach_returns, normalize_ak_daily
from app.models import AnomalyTicket, DailyBar, MainContinuous
from app.repositories._base import upsert_daily_bars


_TQSDK_LOCK = threading.Lock()
_TQSDK_API = None  # 单进程内共享一个 TqApi 实例


def _get_tqsdk():
    """懒加载 TqApi；账号密码从 .env 取（§11.2 ⑭）"""
    global _TQSDK_API
    if _TQSDK_API is not None:
        return _TQSDK_API
    with _TQSDK_LOCK:
        if _TQSDK_API is not None:
            return _TQSDK_API
        try:
            from tqsdk import TqApi, TqAuth  # type: ignore
        except ImportError as e:
            raise CalibrationError(f"tqsdk 未安装: {e}") from e

        settings = get_settings()
        phone = settings.env.TQSDK_PHONE
        pwd = settings.env.TQSDK_PASSWORD
        if not phone or not pwd:
            raise CalibrationError("TQSDK_PHONE / TQSDK_PASSWORD 未配置")

        logger.info("[tqsdk] 登录中…")
        auth = TqAuth(phone, pwd)
        _TQSDK_API = TqApi(auth=auth)
        logger.info("[tqsdk] 登录成功")
        return _TQSDK_API


def close_tqsdk() -> None:
    """显式关闭 tqsdk 会话，避免进程退出时 asyncio 事件循环报错"""
    global _TQSDK_API
    with _TQSDK_LOCK:
        if _TQSDK_API is not None:
            try:
                _TQSDK_API.close()
                logger.info("[tqsdk] 会话已关闭")
            except Exception as e:
                logger.warning(f"[tqsdk] 关闭异常: {e}")
            _TQSDK_API = None


class TqSdkCalibrator:
    """tqsdk 拉取 + 校准 + 异常工单"""

    def __init__(self, session: Session):
        self.session = session
        settings = get_settings()
        self.threshold_pct = settings.calibration_threshold_pct
        self.timeout_sec = settings.env.TQSDK_TIMEOUT_SEC
        self.max_retries = settings.env.TQSDK_MAX_RETRIES

    # ---------- 公开 API ----------
    def fetch_daily(
        self,
        symbol: str,
        exchange: str,
        product: str,
        start: date,
        end: date,
    ) -> list[dict]:
        """从 tqsdk 拉日线；超时/会话失效时重建会话重试"""
        kq_symbol = self._to_kq_symbol(exchange, product)
        last_err: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                api = _get_tqsdk()
                klines = api.get_kline_serial(kq_symbol, duration_seconds=86400)
                rows = self._klines_to_rows(klines, symbol, start, end)
                rows = attach_returns(rows)
                logger.info(f"[tqsdk] {kq_symbol} -> {len(rows)} rows")
                return rows
            except Exception as e:
                last_err = e
                logger.warning(
                    f"[tqsdk] {kq_symbol} 第 {attempt + 1} 次拉取失败: {e}，重建会话重试"
                )
                close_tqsdk()  # 丢弃可能已失效的会话，下次循环重新登录
        raise CalibrationError(
            f"tqsdk 拉取 {kq_symbol} 失败（重试 {self.max_retries} 次后）: {last_err}"
        ) from last_err

    def fill_missing(
        self,
        symbol: str,
        exchange: str,
        product: str,
        start: date,
        end: date,
    ) -> tuple[int, list[date]]:
        """检测 akshare 入库缺失的部分并用 tqsdk 补齐

        返回 (补齐行数, 仍缺失日期列表)
        """
        from app.ingest.calendar_infer import INF_EXCHANGE
        from app.repositories.calendar_repo import CalendarRepository

        # 1. 取交易日历：官方（真实交易所）→ 推断（INF）→ 全量兜底
        cal_repo = CalendarRepository(self.session)
        open_dates = set(
            cal_repo.open_dates(exchange, start, end)
        )
        if not open_dates:
            # R2：官方日历缺 → 用推断日历（品种间交叉）
            open_dates = set(cal_repo.open_dates(INF_EXCHANGE, start, end))
            if open_dates:
                logger.info(f"[tqsdk] {symbol} 使用推断日历（INF）做缺失检测")

        have = {
            r[0]
            for r in self.session.execute(
                select(DailyBar.trade_date).where(DailyBar.symbol == symbol)
            ).all()
        }

        if not open_dates:
            # 2a. 无日历且无已有数据（如 CFFEX 新浪不覆盖）：tqsdk 全量拉取兜底
            rows = self.fetch_daily(symbol, exchange, product, start, end)
            if not rows:
                logger.warning(f"[tqsdk] {symbol} 无日历且 tqsdk 全量拉取为空")
                return 0, []
            upsert_daily_bars(self.session, rows)
            self.session.commit()
            logger.info(f"[tqsdk] {symbol} 无日历兜底：tqsdk 全量入库 {len(rows)} 行")
            return len(rows), []

        # 2c. 正常路径：按日历差集补缺
        missing = sorted(d for d in open_dates if d not in have and start <= d <= end)
        if not missing:
            logger.info(f"[tqsdk] {symbol} 无缺失，跳过补缺")
            return 0, []

        # 3. 仅拉缺失区间（按月份合并，减少 API 调用）
        rows = self.fetch_daily(symbol, exchange, product, start, end)
        keep = [r for r in rows if r["trade_date"] in set(missing)]
        if not keep:
            logger.warning(f"[tqsdk] {symbol} tqsdk 拉取结果也不覆盖缺失日期")
            return 0, missing

        # 4. 入库（src=tqsdk）
        upsert_daily_bars(self.session, keep)
        self.session.commit()
        logger.info(f"[tqsdk] {symbol} 补齐 {len(keep)} 行")
        still_missing = [d for d in missing if d not in {r["trade_date"] for r in keep}]
        return len(keep), still_missing

    def compare_and_ticket(
        self,
        symbol: str,
        exchange: str,
        product: str,
        start: date,
        end: date,
    ) -> list[AnomalyTicket]:
        """二次比对：tqsdk 拉全量 vs daily_bar（src=akshare/csv）的对应值

        - 收盘价偏差 > threshold → 生成工单
        - 成交量异常缺失/翻倍 → 生成工单
        - 状态：pending，由人工裁决（⑰）
        """
        tq_rows = self.fetch_daily(symbol, exchange, product, start, end)
        if not tq_rows:
            return []

        # 现有 daily_bar 数据
        db_rows = self.session.execute(
            select(DailyBar).where(
                DailyBar.symbol == symbol,
                DailyBar.trade_date >= start,
                DailyBar.trade_date <= end,
            )
        ).scalars().all()
        db_map = {r.trade_date: r for r in db_rows}

        tickets: list[AnomalyTicket] = []
        threshold = Decimal(str(self.threshold_pct))
        for tr in tq_rows:
            td = tr["trade_date"]
            db = db_map.get(td)
            if not db:
                continue  # 仅对比已入库行；缺失由 fill_missing 处理
            # 收盘价偏差
            if tr["close"] is not None and db.close is not None and db.close != 0:
                diff_pct = abs(tr["close"] - db.close) / db.close * Decimal(100)
                if diff_pct > threshold:
                    tickets.append(
                        AnomalyTicket(
                            symbol=symbol,
                            trade_date=td,
                            field="close",
                            akshare_val=db.close,
                            tqsdk_val=tr["close"],
                            diff=diff_pct.quantize(Decimal("0.000001")),
                            threshold=threshold,
                            status="pending",
                            note=f"close diff {float(diff_pct):.3f}% > {self.threshold_pct}%",
                        )
                    )
            # 成交量异常：缺失 / 翻倍
            if tr["volume"] is not None and db.volume:
                if abs(tr["volume"] - db.volume) > max(db.volume, 1):
                    ratio = (
                        Decimal(tr["volume"]) / Decimal(db.volume)
                        if db.volume
                        else Decimal(0)
                    )
                    if ratio < Decimal("0.5") or ratio > Decimal("2.0"):
                        tickets.append(
                            AnomalyTicket(
                                symbol=symbol,
                                trade_date=td,
                                field="volume",
                                akshare_val=Decimal(db.volume),
                                tqsdk_val=Decimal(tr["volume"]),
                                diff=(ratio - Decimal(1)).quantize(Decimal("0.000001")),
                                threshold=Decimal("1.0"),
                                status="pending",
                                note=f"volume ratio {float(ratio):.2f}",
                            )
                        )

        if tickets:
            self.session.add_all(tickets)
            self.session.commit()
            logger.warning(
                f"[tqsdk] {symbol} 生成 {len(tickets)} 条异常工单"
            )
        return tickets

    # ---------- 内部 ----------
    def _to_kq_symbol(self, exchange: str, product: str) -> str:
        """生成 tqsdk 代码：KQ.m@CZCE.FG（tqsdk 主连 = KQ.m@交易所.品种，无 888）"""
        return f"KQ.m@{exchange}.{product}"

    def _klines_to_rows(self, klines, symbol: str, start: date, end: date) -> list[dict]:
        """tqsdk kline DataFrame -> dict 列表

        tqsdk get_kline_serial 返回的 DataFrame 列：
        - datetime, open, high, low, close, volume, open_oi, close_oi
        """
        if klines is None or len(klines) == 0:
            return []
        rows: list[dict] = []
        for i in range(len(klines)):
            dt = klines.iloc[i].get("datetime")
            if dt is None:
                continue
            td = pd_date(dt)
            if td is None or td < start or td > end:
                continue
            rows.append(
                {
                    "symbol": symbol,
                    "trade_date": td,
                    "open": _dec(klines.iloc[i].get("open")),
                    "high": _dec(klines.iloc[i].get("high")),
                    "low": _dec(klines.iloc[i].get("low")),
                    "close": _dec(klines.iloc[i].get("close")),
                    "settle": None,
                    "volume": _int(klines.iloc[i].get("volume")),
                    "amount": None,
                    "oi": _int(klines.iloc[i].get("close_oi") or klines.iloc[i].get("open_oi")),
                    "src": "tqsdk",
                }
            )
        return rows


def _dec(v):
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _int(v):
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return None


def pd_date(dt):
    """tqsdk kline datetime 兼容解析：ns 时间戳 / Timestamp / str / datetime"""
    from datetime import datetime as _dt

    # tqsdk kline datetime 列为纳秒级 Unix 时间戳（numpy.float64 / int）
    if isinstance(dt, (int, float)):
        import numpy as np  # type: ignore

        if isinstance(dt, np.floating) and (dt != dt):  # NaN
            return None
        return _dt.fromtimestamp(float(dt) / 1e9).date()
    if isinstance(dt, datetime):
        return dt.date()
    if isinstance(dt, str):
        return _dt.fromisoformat(dt).date()
    import pandas as pd  # type: ignore

    if isinstance(dt, pd.Timestamp):
        return dt.date()
    raise ValueError(f"unknown datetime type: {type(dt)}")


__all__ = ["TqSdkCalibrator"]