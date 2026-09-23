"""
在线小时线采集器（M?）
- 主源：akshare futures_zh_minute_sina(symbol, period="60")（新浪主连，免登录）
- 兜底：tqsdk get_kline_serial(KQ.m@{exchange}.{product}, duration_seconds=3600)
- 输出：hourly_bar 标准化行，复用 upsert_hourly_bars（主键 symbol, trade_datetime）

说明：
- akshare 新浪分钟线通常只返回近期窗口（数月 ~ 一年量级），适合每日增量刷新；
- tqsdk 可拉更长历史（data_length 控制），用于一次性回填（CLI --prefer tqsdk）；
- 两种来源均按 (symbol, trade_datetime) 幂等 upsert，重复跑安全。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Iterable

from decimal import Decimal

from app.core.config import MainContractSpec, get_settings
from app.core.logging import logger
from app.ingest.akshare_source import AkShareSource
from app.repositories._base import upsert_hourly_bars


def _to_dec(v) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def _to_int(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return None


def _parse_ak_dt(s) -> datetime | None:
    """新浪分钟线 datetime 列为字符串 '2025-12-26 11:15:00'"""
    if not s:
        return None
    if isinstance(s, datetime):
        return s
    s = str(s).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def _parse_tq_dt(dt) -> datetime | None:
    """tqsdk kline datetime 列为纳秒级 Unix 时间戳（numpy.float64 / int）/ Timestamp / str"""
    from datetime import datetime as _dt

    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        import numpy as np  # type: ignore

        if isinstance(dt, np.floating) and dt != dt:  # NaN
            return None
        return _dt.fromtimestamp(float(dt) / 1e9)
    if isinstance(dt, datetime):
        return dt
    import pandas as pd  # type: ignore

    if isinstance(dt, pd.Timestamp):
        return dt.to_pydatetime()
    if isinstance(dt, str):
        try:
            return _dt.fromisoformat(dt)
        except Exception:
            return None
    return None


# 中国期货小时线「合法收盘整点」集合（覆盖日盘+夜盘，含各交易所差异）。
# 实测 akshare 固定网格分钟线会吐出 11:15/14:15/09:30/10:45/13:45/14:45/22:15
# 等非对齐棒（全表 24,659 行脏数据，2026-09-23 数据质量整改）；另有少量未来时间戳。
# 凡不在本集合、非整点、或晚于当前时刻的棒一律丢弃。
_VALID_HOURLY_HOURS = frozenset({0, 1, 2, 9, 10, 11, 13, 14, 15, 21, 22, 23})


def _valid_hourly_ts(dt) -> bool:
    """小时线对齐校验：必须是交易时段整点、且非未来时间戳。"""
    if dt is None:
        return False
    if dt.minute != 0:
        return False
    if dt.hour not in _VALID_HOURLY_HOURS:
        return False
    # 未来时间戳：akshare 偶发返回尚未形成的棒（如当前 00:19 却带当日 10:00）
    if dt > datetime.now() + timedelta(minutes=2):
        return False
    return True


class HourlyCollector:
    """在线小时线采集（单 session 内批量 upsert）"""

    def __init__(self, session, prefer: str = "akshare"):
        self.session = session
        self.prefer = prefer  # 'akshare'（默认，免费免登录）| 'tqsdk'（长历史回填）
        self._tq_api = None   # 共享的 tqsdk 连接（惰性创建，全品种复用）

    # ---------- 资源释放 ----------
    def close(self) -> None:
        """释放共享的 tqsdk 连接。TqApi 登录一次即可服务多个合约。"""
        if self._tq_api is not None:
            try:
                self._tq_api.close()
            except Exception:  # noqa: BLE001
                pass
            self._tq_api = None

    def _get_tq_api(self):
        """惰性创建并复用单个 TqApi。

        原实现里每个品种都 new 一次 TqApi + 登录：50 个品种 = 50 次登录（每次数秒），
        在 15 分钟的扫描周期内根本跑不完 —— 兜底等于没有。
        """
        if self._tq_api is not None:
            return self._tq_api
        from tqsdk import TqApi, TqAuth  # type: ignore

        e = get_settings().env
        self._tq_api = TqApi(auth=TqAuth(e.TQSDK_PHONE, e.TQSDK_PASSWORD))
        logger.info("[hourly] tqsdk 连接已建立（全品种复用）")
        return self._tq_api

    # ---------- 公开 API ----------
    def collect_symbol(self, spec: MainContractSpec, data_length: int = 8000) -> int:
        """采集单个品种小时线并 upsert，返回写入行数"""
        rows: list[dict] = []
        if self.prefer != "tqsdk":
            rows = self._fetch_akshare(spec)
        if not rows:
            rows = self._fetch_tqsdk(spec, data_length=data_length)
        if not rows:
            logger.warning(f"[hourly] {spec.symbol} 无数据（akshare/tqsdk 均空）")
            return 0
        n = upsert_hourly_bars(self.session, rows)
        self.session.commit()
        logger.info(f"[hourly] {spec.symbol} upserted {n}")
        return n

    def collect_all(
        self, only_products: Iterable[str] | None = None, data_length: int = 8000
    ) -> list[dict]:
        """采集全部（或指定）主连品种；逐项容错不中断"""
        specs = get_settings().main_contracts
        if only_products:
            specs = [s for s in specs if s.product in set(only_products)]
        results: list[dict] = []
        try:
            for spec in specs:
                try:
                    n = self.collect_symbol(spec, data_length=data_length)
                    results.append({"symbol": spec.symbol, "rows": n})
                except Exception as e:
                    logger.exception(f"[hourly] {spec.symbol} 异常: {e}")
                    results.append({"symbol": spec.symbol, "error": str(e)})
        finally:
            self.close()          # 释放共享 tqsdk 连接（若本轮用到）
        ok = sum(1 for r in results if "error" not in r)
        logger.info(f"[hourly] collect_all done: {ok}/{len(results)} symbols")
        return results

    # ---------- akshare 主源 ----------
    def _fetch_akshare(self, spec: MainContractSpec) -> list[dict]:
        import akshare as ak  # type: ignore

        # 新浪主连代码：FG888 -> FG0（与日线 AkShareSource 同映射）
        sina = AkShareSource.to_sina_symbol(spec.symbol)
        try:
            df = ak.futures_zh_minute_sina(symbol=sina, period="60")
        except Exception as e:
            logger.warning(f"[hourly] akshare {spec.symbol} 失败: {e}")
            return []
        if df is None or len(df) == 0:
            return []
        rows: list[dict] = []
        for i in range(len(df)):
            dt = _parse_ak_dt(df.iloc[i].get("datetime"))
            if dt is None or not _valid_hourly_ts(dt):
                continue
            rows.append(
                {
                    "symbol": spec.symbol,
                    "trade_datetime": dt,
                    "open": _to_dec(df.iloc[i].get("open")),
                    "high": _to_dec(df.iloc[i].get("high")),
                    "low": _to_dec(df.iloc[i].get("low")),
                    "close": _to_dec(df.iloc[i].get("close")),
                    "volume": _to_int(df.iloc[i].get("volume")),
                    "oi": _to_int(df.iloc[i].get("hold")),  # 新浪分钟线持仓列名 hold
                    "src": "akshare",
                }
            )
        return rows

    # ---------- tqsdk 兜底 / 回填 ----------
    def _fetch_tqsdk(self, spec: MainContractSpec, data_length: int = 8000) -> list[dict]:
        try:
            import tqsdk  # type: ignore  # noqa: F401
        except Exception as e:
            logger.warning(f"[hourly] tqsdk 未安装: {e}")
            return []
        if not get_settings().env.TQSDK_PHONE or not get_settings().env.TQSDK_PASSWORD:
            logger.warning("[hourly] TQSDK 凭证未配置，跳过 tqsdk 兜底")
            return []
        # tqsdk 主连代码的「品种部分」大小写按交易所而定，错一个字母就回合约不存在/超时：
        #   CZCE 必须大写 (FG/SA/SR/TA/...)；CFFEX 大写 (IF/IH/...)；
        #   DCE/SHFE/INE 小写 (jd/rb/sc/...)。
        # 旧代码一律 .lower() → 22 个郑商所品种(玻璃/纯碱/白糖/PTA/甲醇…)兜底全部失效，
        # 主源(新浪)一旦限流/异常就彻底无数据，信号静默漏推。
        _TQ_PROD_CASE = {
            "CZCE": str.upper,
            "CFFEX": str.upper,
            "DCE": str.lower,
            "SHFE": str.lower,
            "INE": str.lower,
        }
        prod = _TQ_PROD_CASE.get(spec.exchange, str.lower)(spec.product)
        kq = f"KQ.m@{spec.exchange}.{prod}"
        try:
            api = self._get_tq_api()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[hourly] tqsdk 登录失败: {e}")
            self.close()
            return []
        try:
            klines = api.get_kline_serial(kq, duration_seconds=3600, data_length=data_length)
            # 同步等待数据落地（get_kline_serial 初始为空，需 wait_update 填充）
            for _ in range(5):
                if klines is not None and len(klines) > 0:
                    break
                try:
                    api.wait_update(timeout=20)
                except Exception:
                    break
            if klines is None or len(klines) == 0:
                return []
            rows: list[dict] = []
            for i in range(len(klines)):
                dt = _parse_tq_dt(klines.iloc[i].get("datetime"))
                if dt is None or not _valid_hourly_ts(dt):
                    continue
                rows.append(
                    {
                        "symbol": spec.symbol,
                        "trade_datetime": dt,
                        "open": _to_dec(klines.iloc[i].get("open")),
                        "high": _to_dec(klines.iloc[i].get("high")),
                        "low": _to_dec(klines.iloc[i].get("low")),
                        "close": _to_dec(klines.iloc[i].get("close")),
                        "volume": _to_int(klines.iloc[i].get("volume")),
                        "oi": _to_int(
                            klines.iloc[i].get("close_oi") or klines.iloc[i].get("open_oi")
                        ),
                        "src": "tqsdk",
                    }
                )
            return rows
        except Exception as e:
            logger.warning(f"[hourly] tqsdk {spec.symbol} 失败: {e}")
            self.close()          # 连接可能已失效，丢弃以便下个品种重建
            return []


__all__ = ["HourlyCollector"]
