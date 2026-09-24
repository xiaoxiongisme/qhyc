"""
实时 1 分钟采集器（M?，#5 改造核心）
- 主源：tqsdk get_kline_serial(KQ.m@{exchange}.{product}, duration_seconds=60)
- 输出：标准化分钟行，upsert 入 minute_bar（主键 symbol, ts，冲突跳过）
- 设计目标：让「前向/实时」入库与历史 CSV 一致 —— 全部先落 minute_bar，
  再由 synthesizer 合成 5/15/30/60 分钟，做到「分钟是唯一事实来源」。

说明：
- tqsdk 主连代码的「品种部分」大小写按交易所而定（与 hourly_collector 一致）：
  CZCE/CFFEX 必须大写；DCE/SHFE/INE 小写。错一个字母就回合约不存在/超时。
- tqsdk kline 的 datetime 是纳秒级 Unix 时间戳（真实瞬时，即该分钟 K 的**起点**），
  统一转为 Asia/Shanghai 后写入 minute_bar，与历史 CSV（起点标签）口径一致。
- 实时 1 分钟不强行对齐交易时段整点（那会丢分钟），仅剔除 NaN 价 / 1970 脏戳 / 未来戳。
- 写入幂等：ON CONFLICT (symbol, ts) DO NOTHING，重复跑安全。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from sqlalchemy import column, insert, table
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


# tqsdk 主连品种大小写（与 hourly_collector._TQ_PROD_CASE 保持一致）
_TQ_PROD_CASE = {
    "CZCE": str.upper,
    "CFFEX": str.upper,
    "DCE": str.lower,
    "SHFE": str.lower,
    "INE": str.lower,
}


def _parse_tq_dt(dt) -> datetime | None:
    """tqsdk kline datetime 为纳秒级 Unix 时间戳（真实瞬时，即该分钟 K 起点）。转 UTC→上海。"""
    if dt is None:
        return None
    if isinstance(dt, (int, float)):
        import numpy as np  # type: ignore

        if isinstance(dt, np.floating) and dt != dt:  # NaN
            return None
        return datetime.fromtimestamp(float(dt) / 1e9, tz=timezone.utc).astimezone(_SH_TZ)
    if isinstance(dt, datetime):
        return dt if dt.tzinfo else dt.replace(tzinfo=_SH_TZ)
    import pandas as pd  # type: ignore

    if isinstance(dt, pd.Timestamp):
        return dt.tz_localize("UTC").astimezone(_SH_TZ) if dt.tzinfo is None \
            else dt.astimezone(_SH_TZ)
    if isinstance(dt, str):
        try:
            return datetime.fromisoformat(dt).astimezone(_SH_TZ)
        except Exception:
            return None
    return None


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
    """实时 1 分钟采集（单 session 内批量 upsert），与 HourlyCollector 同构。"""

    def __init__(self, session: Session, prefer: str = "tqsdk"):
        self.session = session
        self.prefer = prefer
        self._tq_api = None

    def close(self) -> None:
        if self._tq_api is not None:
            try:
                self._tq_api.close()
            except Exception:  # noqa: BLE001
                pass
            self._tq_api = None

    def _get_tq_api(self):
        if self._tq_api is not None:
            return self._tq_api
        from tqsdk import TqApi, TqAuth  # type: ignore

        e = get_settings().env
        self._tq_api = TqApi(auth=TqAuth(e.TQSDK_PHONE, e.TQSDK_PASSWORD))
        logger.info("[minute] tqsdk 连接已建立（全品种复用）")
        return self._tq_api

    # ---------- 公开 API ----------
    def collect_symbol(self, spec: MainContractSpec, data_length: int = 5000) -> int:
        rows = self._fetch_tqsdk(spec, data_length=data_length)
        if not rows:
            logger.warning(f"[minute] {spec.symbol} 无数据")
            return 0
        self._upsert(rows)
        logger.info(f"[minute] {spec.symbol} upserted {len(rows)}")
        return len(rows)

    def collect_all(
        self, only_products: Iterable[str] | None = None, data_length: int = 5000
    ) -> list[dict]:
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
                    logger.exception(f"[minute] {spec.symbol} 异常: {e}")
                    results.append({"symbol": spec.symbol, "error": str(e)})
        finally:
            self.close()
        ok = sum(1 for r in results if "error" not in r)
        logger.info(f"[minute] collect_all done: {ok}/{len(results)} symbols")
        return results

    # ---------- tqsdk 抓取 ----------
    def _fetch_tqsdk(self, spec: MainContractSpec, data_length: int = 5000) -> list[dict]:
        try:
            import tqsdk  # type: ignore  # noqa: F401
        except Exception as e:
            logger.warning(f"[minute] tqsdk 未安装: {e}")
            return []
        if not get_settings().env.TQSDK_PHONE or not get_settings().env.TQSDK_PASSWORD:
            logger.warning("[minute] TQSDK 凭证未配置，跳过")
            return []
        prod = _TQ_PROD_CASE.get(spec.exchange, str.lower)(spec.product)
        kq = f"KQ.m@{spec.exchange}.{prod}"
        try:
            api = self._get_tq_api()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[minute] tqsdk 登录失败: {e}")
            self.close()
            return []
        try:
            klines = api.get_kline_serial(kq, duration_seconds=60, data_length=data_length)
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
                o = klines.iloc[i].get("open")
                h = klines.iloc[i].get("high")
                l = klines.iloc[i].get("low")
                c = klines.iloc[i].get("close")
                if not _valid_minute_row(dt, o, h, l, c):
                    continue
                vol = klines.iloc[i].get("volume")
                oi = klines.iloc[i].get("close_oi") or klines.iloc[i].get("open_oi")
                rows.append(
                    {
                        "symbol": spec.symbol,
                        "ts": dt,
                        "open": float(o),
                        "high": float(h),
                        "low": float(l),
                        "close": float(c),
                        "volume": int(vol) if vol is not None else 0,
                        "amount": None,  # tqsdk 1 分钟 K 无成交额字段
                        "open_interest": int(oi) if oi is not None else None,
                        "high_limit": None,
                        "low_limit": None,
                        "pre_close": None,
                        "settle_price": None,
                        "contract": None,  # 主连无单一合约
                        "src": "tqsdk_1min",
                    }
                )
            return rows
        except Exception as e:
            logger.warning(f"[minute] tqsdk {spec.symbol} 失败: {e}")
            self.close()
            return []

    # ---------- upsert ----------
    def _upsert(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        stmt = insert(_MINUTE_BAR).values(rows).on_conflict_do_nothing(
            index_elements=["symbol", "ts"]
        )
        self.session.execute(stmt)
        self.session.commit()
        return len(rows)


__all__ = ["MinuteCollector"]
