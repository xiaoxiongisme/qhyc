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
from app.ingest.resilience import (  # G1 韧性层
    DEFAULT_MAX_ATTEMPTS, DegradedResult, bars_consistency_check,
    cache_get, resilient_fetch)
from app.ingest.utils import attach_returns, normalize_ak_daily


class AkShareSource:
    """akshare 拉取日线（经 G1 韧性层：退避重试 / 断路器 / 缓存降级 / 脏数据拦截）"""

    def __init__(self, exchange: str):
        self.exchange = exchange.upper()

    #: 上一成功批次（按 symbol），供跨批次一致性校验 —— 替代被删的 天勤 双源比对
    _last_good: dict[str, list] = {}
    #: 降级标记（本次结果非实时），调用方应据此告警
    degraded: DegradedResult | None = None

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

    # ---------- 交易日历（G4：改期货专属日历源） ----------
    def fetch_calendar(self, exchange: str, year: int) -> pd.DataFrame:
        """交易日历。
        ⚠ G4（评审 §2.5，P1 数据正确性）：``tool_trade_date_hist_sina`` 是**股票**日历，
        期货作息与之不同（中金所 09:15 开盘、国债/股指无夜盘、节假日提前收盘、各所夜盘
        时段不一），套用会系统性错，并污染护栏与门禁的时间边界。
        现优先用 **期货专属** ``futures_rule(trade_date)``（国泰君安，按日返回各所各品种
        日历 + 保证金比例），失败才回退股票日历并**明确告警**。
        """
        import akshare as ak  # type: ignore

        out = resilient_fetch(
            f"calendar:{exchange}:{year}",
            lambda: self._futures_calendar(ak, exchange, year),
            cache_ttl=7 * 86400.0,
            max_attempts=2,
        )
        if out.ok and out.value is not None and not out.value.empty:
            return out.value
        logger.warning(
            f"[calendar] 期货专属日历不可用（{out.error}），**回退股票日历** —— "
            f"该结果对中金所/国债/节假日前后可能有偏差（G4 已知遗留）")
        df = ak.tool_trade_date_hist_sina()
        df = df.copy()
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
        df = df[(df["trade_date"] >= date(year, 1, 1))
                & (df["trade_date"] <= date(year, 12, 31))]
        return df

    @staticmethod
    def _futures_calendar(ak, exchange: str, year: int) -> pd.DataFrame:
        """akshare 期货专属日历：按交易日查询各所各品种日历。

        ⚠ 接口参数名为 ``date=``（**不是** ``trade_date=``）；旧代码用错名会导致
        ``TypeError`` 被下方 except 静默吞掉 → 整个期货历取数空转。已修正。
        非交易日接口抛 ``ValueError: No tables found``（=该日无交易规则）→ 视为非交易日跳过；
        其余异常（网络/限流）**不再静默吞掉**，如实抛出由 resilient_fetch 重试/告警。
        """
        days = pd.date_range(date(year, 1, 1), date(year, 12, 31), freq="D")
        frames = []
        for d in days:
            try:
                f = ak.futures_rule(date=d.strftime("%Y%m%d"))
            except ValueError as e:  # 非交易日（接口无表）→ 跳过
                if "No tables found" in str(e):
                    continue
                raise
            except Exception:  # noqa: BLE001  其余（网络/限流）→ 交给上层重试，不静默
                raise
            if f is None or f.empty:
                continue
            f = f.copy()
            f["trade_date"] = d.date()
            frames.append(f)
        if not frames:
            return pd.DataFrame()
        out = pd.concat(frames, ignore_index=True)
        # 统一列名：仅保留日期与交易所/品种相关列（用真实中文列名过滤）
        keep = [c for c in out.columns
                if c in ("trade_date", "交易所", "品种", "交易保证金比例", "涨跌停板幅度")]
        return out[keep]

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
        """经 G1 韧性层拉取。

        与改造前的差异：
        * 原先**单次调用**，失败即抛、空结果静默返回 ``DataFrame()``（评审 §4.2 点名的
          静默失败模式）→ 现为退避重试 + 断路器 + 缓存降级；
        * 原先无脏数据拦截 → 现用「与上一成功批次一致性」校验替代被删的 天勤 比对层；
        * 降级结果通过 ``self.degraded`` **显式暴露**，调用方必须决定是否接受。
        """
        import akshare as ak  # type: ignore

        sina_symbol = self.to_sina_symbol(symbol)
        self.degraded = None
        out = resilient_fetch(
            f"daily:{sina_symbol}",
            lambda: ak.futures_main_sina(symbol=sina_symbol),
            cache_ttl=6 * 3600.0,
            max_attempts=DEFAULT_MAX_ATTEMPTS,
            validate=bars_consistency_check,
            previous=self._last_good.get(symbol),
        )

        if not out.ok:
            # 绝不静默：抛错由上层记 anomaly_ticket 并告警（评审 §4.2）
            raise IngestError(
                f"akshare.futures_main_sina({symbol}) 经 {out.attempts} 次重试仍失败: "
                f"{out.error}")
        if out.degraded:
            self.degraded = out.degraded
            logger.warning(
                f"[akshare] {symbol} **降级数据**（{out.degraded.reason}，陈旧 "
                f"{out.degraded.stale_seconds:.0f}s）—— 调用方需显式确认可用")

        df = out.value
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
        """由品种代码取交易所 —— **唯一真源 = ``dim_variety`` 字典**。

        ⚠ 改造前此处是 70+ 条硬编码品种表（与 ``local_importer._PRODUCT_EXCHANGE``
        ``inventory.PRODUCT_TO_EM_SYM`` ``scheduler._TICKS`` 构成四份重复真源），
        交易所/品种新增都会失效且**静默返回 UNKNOWN**。
        现改为读库；库内缺失时**抛错**（而非返回 UNKNOWN 让上层静默走错分支）。
        """
        p = str(product or "").upper().strip()
        if not p:
            raise IngestError("detect_exchange: 品种码为空")
        try:
            from app.data.barstore import variety_spec
            return variety_spec(p)["exchange"] or "UNKNOWN"
        except Exception as e:  # noqa: BLE001
            raise IngestError(
                f"detect_exchange({p}): dim_variety 字典查不到交易所（{e}）。"
                f"请先跑 migrations/007 + scripts/seed_variety_specs.py --apply；"
                f"**拒绝返回 UNKNOWN** —— 静默兜底会让后续采集走错分支。") from e


__all__ = ["AkShareSource"]