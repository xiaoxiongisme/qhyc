"""
在线小时线采集器（M?）—— C9 去天勤后 akshare 单一源
- 唯一源：akshare futures_zh_minute_sina(symbol, period="60")（新浪主连，免登录）
- 输出：hourly_bar 标准化行，复用 upsert_hourly_bars（主键 symbol, trade_datetime）

说明：
- T4 时区修复（2026-10-04）已核实：当前 akshare 版返回的 datetime 即正确 Asia/Shanghai
  收盘时刻（offset≈0，跨 RB/MA/J/AU/TA/FG 多所验证），故直接按上海本地时间解析
  （见 _parse_ak_dt），**不叠加**任何 +1h。每条棒经 _valid_hourly_ts 校验（合法整点 +
  非未来戳），非法/偏移棒一律丢弃（纵深防御，防止任何残留时区偏移毒化信号状态机）。
- 按 (symbol, trade_datetime) 幂等 upsert，重复跑安全。
- 注：akshare 新浪分钟线仅返回近期窗口（约 8 个月量级，~680–1024 行），适合每日增量
  刷新；历史回填由既有 hourly_bar 覆盖，不再依赖 天勤（C9 已去天勤）。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo

from decimal import Decimal

from app.core.config import MainContractSpec, get_settings
from app.core.logging import logger
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


_SH_TZ = ZoneInfo("Asia/Shanghai")


def _parse_ak_dt(s) -> datetime | None:
    """新浪分钟线 datetime 列为字符串 '2025-12-26 11:15:00'。

    T4 时区修复（2026-10-04 实测）：当前 akshare 版 ``futures_zh_minute_sina`` 返回的
    标签**已是正确 Asia/Shanghai 收盘时刻**（RB/MA/J 跨所、period=60/1 对齐 天勤 实测
    offset≈0；小时分布恰为合法交易整点 {10,11,14,15,22,23}）。故此处直接按上海本地时间
    解析定位、转 UTC 感知即可。

    ⚠ **切勿叠加** 天勤 路径的 +1h：那是 天勤 标签为「整点起点」才需重标为收盘口径；
    sina 标签本就是收盘口径，叠加 = 双重修正 = 错。PRD §6.6 描述的「+8h/+9h 偏移」是
    2026-09-23 旧版本/旧周期现象，现版本已不存在——若未来 akshare 回归偏移标签，解析出的
    小时会落在 {18,19,6,7,8,...} 而非合法整点，被 ``_valid_hourly_ts`` 整批丢弃（纵深防御，
    不会静默毒化），并由 ``_fetch_akshare`` 的丢弃率告警暴露，届时再据实测偏移修正，
    **严禁硬编码统一 −8h**（日盘/夜盘偏移量本就不一，统一平移仍错）。
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


# 中国期货小时线「合法收盘整点」集合（覆盖日盘+夜盘，含各交易所差异）。
# 实测 akshare 固定网格分钟线会吐出 11:15/14:15/09:30/10:45/13:45/14:45/22:15
# 等非对齐棒（全表 24,659 行脏数据，2026-09-23 数据质量整改）；另有少量未来时间戳。
# 凡不在本集合、非整点、或晚于当前时刻的棒一律丢弃。
_VALID_HOURLY_HOURS = frozenset({0, 1, 2, 9, 10, 11, 13, 14, 15, 21, 22, 23})


def _valid_hourly_ts(dt) -> bool:
    """小时线对齐校验：按上海时区判定必须是交易时段整点，且非未来时间戳。"""
    if dt is None or dt.tzinfo is None:
        return False
    sh = dt.astimezone(_SH_TZ)
    if sh.minute != 0:
        return False
    if sh.hour not in _VALID_HOURLY_HOURS:
        return False
    # 未来时间戳：akshare 偶发返回尚未形成的棒（如当前 00:19 却带当日 10:00）
    if dt > datetime.now(timezone.utc) + timedelta(minutes=2):
        return False
    return True


class HourlyCollector:
    """在线小时线采集（单 session 内批量 upsert）—— C9 去天勤后 akshare 单一源"""

    def __init__(self, session):
        # C9 去天勤：akshare 单一源，无 天勤 兜底/连接。
        self.session = session

    # ---------- 资源释放 ----------
    def close(self) -> None:
        """释放资源（C9 后无外部连接需关闭，保留接口以兼容调用方）。"""
        return

    # ---------- 公开 API ----------
    def collect_symbol(self, spec: MainContractSpec, data_length: int = 8000) -> int:
        """采集单个品种小时线（akshare 单一源）并 upsert，返回写入行数"""
        rows = self._fetch_akshare(spec)
        if not rows:
            logger.warning(f"[hourly] {spec.symbol} akshare 无数据")
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
            self.close()          # 资源释放（C9 后为 no-op）
        ok = sum(1 for r in results if "error" not in r)
        logger.info(f"[hourly] collect_all done: {ok}/{len(results)} symbols")
        return results

    # ---------- akshare 唯一源（T4 已修：sina 标签实测为正确上海时间，offset≈0） ----------
    def _fetch_akshare(self, spec: MainContractSpec) -> list[dict]:
        """akshare 新浪主连小时线（period=60）唯一源。

        T4 时区修复（2026-10-04 实测）：``futures_zh_minute_sina`` 返回的 datetime 已是
        正确 Asia/Shanghai 收盘时刻，``_parse_ak_dt`` 直接按上海本地时间解析即可，
        **不叠加** 天勤 路径的 +1h。每条棒经 ``_valid_hourly_ts`` 校验（合法交易整点 +
        非未来戳），非法棒（含未来偏差/非整点）一律丢弃——这是防止任何残留时区偏移
        毒化信号状态机的纵深防御。
        """
        import akshare as ak  # type: ignore

        sina_sym = f"{spec.product.lower()}0"  # 新浪主连 = 品种小写 + 0（rb0/ma0/j0）
        try:
            df = ak.futures_zh_minute_sina(symbol=sina_sym, period="60")
        except Exception as e:
            logger.warning(f"[hourly] akshare {spec.symbol} ({sina_sym}) 失败: {e}")
            return []
        if df is None or df.empty:
            return []
        raw = len(df)
        rows: list[dict] = []
        dropped = 0
        for _, r in df.iterrows():
            dt = _parse_ak_dt(r.get("datetime"))
            if dt is None or not _valid_hourly_ts(dt):
                dropped += 1
                continue
            rows.append(
                {
                    "symbol": spec.symbol,
                    "trade_datetime": dt,
                    "open": _to_dec(r.get("open")),
                    "high": _to_dec(r.get("high")),
                    "low": _to_dec(r.get("low")),
                    "close": _to_dec(r.get("close")),
                    "volume": _to_int(r.get("volume")),
                    "oi": _to_int(r.get("hold")),
                    "src": "akshare",
                }
            )
        # 丢弃率异常偏高 → 疑似 sina 标签时区偏移回归（PRD §6.6），告警暴露
        # （C9 已无 天勤 兜底，须人工复核 akshare 数据口径）。
        if raw and dropped / raw > 0.5:
            logger.warning(
                f"[hourly] akshare {spec.symbol} 丢弃率 {dropped}/{raw} 异常偏高 —— "
                f"疑似 sina 标签时区偏移回归，请复核 akshare 数据口径"
            )
        return rows


__all__ = ["HourlyCollector"]
