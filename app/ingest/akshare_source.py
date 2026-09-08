"""
akshare 主源采集器（M1）
- 输入：symbol (主连代码，如 FG888)、起止日期
- 输出：标准化后的日线 dict 列表（不含入库）
- 依赖：akshare
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Sequence

import pandas as pd

from app.core.exceptions import IngestError
from app.core.logging import logger
from app.ingest.utils import attach_returns, normalize_ak_daily


class AkShareSource:
    """akshare 拉取日线"""

    def __init__(self, exchange: str):
        self.exchange = exchange.upper()

    # ---------- 公开 API ----------
    def fetch_daily(
        self,
        symbol: str,
        start: date | None = None,
        end: date | None = None,
    ) -> list[dict]:
        """拉取 [start, end] 日线；start/end 为 None 则按历史/今日兜底"""
        start = start or date(2015, 1, 1)
        end = end or date.today()
        df = self._call_akshare(symbol, start, end)
        rows = normalize_ak_daily(df, symbol)
        rows = attach_returns(rows)
        logger.info(
            f"[akshare] {symbol} exchange={self.exchange} start={start} end={end} -> {len(rows)} rows"
        )
        return rows

    def fetch_calendar(self, exchange: str, year: int) -> pd.DataFrame:
        """拉取某年某交易所交易日历（用于缺失检测 §4.3）"""
        import akshare as ak  # type: ignore

        try:
            df = ak.tool_trade_date_hist_sina()  # 通用 sina 日历
            # sina 接口返回全市场日期；过滤 is_open
            df = df.copy()
            df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
            df = df[(df["trade_date"] >= date(year, 1, 1)) & (df["trade_date"] <= date(year, 12, 31))]
            return df
        except Exception as e:
            logger.warning(f"[akshare] trade_date_hist_sina 失败 {e}，使用降级方案")
            # 降级：调用新浪原始数据
            raise

    # ---------- 内部 ----------
    @staticmethod
    def to_sina_symbol(symbol: str) -> str:
        """库内主连代码 → 新浪代码：FG888 → FG0（新浪主连 = 品种 + 0）"""
        p = symbol.upper()
        if p.endswith("888"):
            return p[:-3] + "0"
        return p

    def _call_akshare(
        self, symbol: str, start: date, end: date
    ) -> pd.DataFrame:
        import akshare as ak  # type: ignore

        # 新浪主连代码：FG0/SA0（库内统一用 FG888，调用前转换）
        sina_symbol = self.to_sina_symbol(symbol)
        try:
            df = ak.futures_main_sina(symbol=sina_symbol)
        except Exception as e:
            raise IngestError(f"akshare.futures_main_sina({symbol}) 失败: {e}") from e

        if df is None or df.empty:
            return pd.DataFrame()

        # 过滤日期
        if "日期" in df.columns:
            df["日期"] = pd.to_datetime(df["日期"]).dt.date
            df = df[(df["日期"] >= start) & (df["日期"] <= end)]
        elif "date" in df.columns:
            df["date"] = pd.to_datetime(df["date"]).dt.date
            df = df[(df["date"] >= start) & (df["date"] <= end)]

        df = df.reset_index(drop=True)
        return df

    # ---------- 工具：识别交易所对应代码 ----------
    @staticmethod
    def detect_exchange(product: str) -> str:
        """由品种代码推断交易所（内置规则）"""
        czce = {"FG", "SA", "SR", "CF", "TA", "MA", "RM", "OI", "AP", "PK"}
        shfe = {"RB", "CU", "AU", "AG", "AL", "ZN", "PB", "SN", "NI", "SS", "FU", "BU"}
        dce = {"M", "Y", "I", "JM", "J", "P", "C", "A", "B", "L", "V", "PP", "EG", "EB"}
        cffex = {"IF", "IC", "IH", "T", "TF", "TS"}
        ine = {"SC", "NR", "LU", "BC"}
        p = product.upper()
        if p in czce:
            return "CZCE"
        if p in shfe:
            return "SHFE"
        if p in dce:
            return "DCE"
        if p in cffex:
            return "CFFEX"
        if p in ine:
            return "INE"
        return "UNKNOWN"


__all__ = ["AkShareSource"]