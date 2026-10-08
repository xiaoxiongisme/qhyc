"""
在线小时线采集器（M?）—— C9 去天勤后 akshare 单一源
- 唯一源：akshare futures_zh_minute_sina(symbol, period="60")（新浪主连，免登录）
- 输出：hourly_bar 标准化行，复用 upsert_hourly_bars（主键 symbol, trade_datetime）

说明：
- T4 时区修复（2026-10-04）已核实：当前 akshare 版返回的 datetime 即正确 Asia/Shanghai
  收盘时刻（offset≈0，跨 RB/MA/J/AU/TA/FG 多所验证），故直接按上海本地时间解析
  （见 _parse_ak_dt），**不叠加**任何 +1h。每条棒经 _valid_hourly_ts 校验（收盘标签 ∈
  _VALID_HOURLY_LABELS 白名单 + 非未来戳）。⚠ 2026-10-08 更正：此处原写「合法整点 +
  非整点一律丢弃」，已被推翻——期货日盘非整点切分，11:15/14:15/10:45/15:30 本身合法，
  按「整点」过滤会误杀（实测丢弃率可达 50%）。另：白名单只拦「不在集合内」的标签，
  若源侧 +8h 偏移，(15,0)→(23,0)、(2,0)→(10,0) 会伪装通过，故不可依赖它兜底。
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
from app.data.quality import is_bad_row
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
    offset≈0）。故此处直接按上海本地时间解析、转 UTC 感知即可，**不做任何 ±h 平移**。

    ⚠ **切勿叠加** 天勤 路径的 +1h：那是 天勤 标签为「整点起点」才需重标为收盘口径；
    sina 标签本就是收盘口径，叠加 = 双重修正 = 错。PRD §6.6 描述的「+8h/+9h 偏移」是
    2026-09-23 旧版本/旧周期现象，现版本已不存在——若未来 akshare 回归偏移标签，解析出的
    收盘时刻会落在 {18,19,6,7,8,...} 等非法标签上，被 ``_valid_hourly_ts`` 整批丢弃
    （纵深防御，不会静默毒化），并由 ``_fetch_akshare`` 的丢弃率告警暴露，届时再据实测
    偏移修正，**严禁硬编码统一 −8h**（日盘/夜盘偏移量本就不一，统一平移仍错）。

    ★ 2026-10-08：所谓"时区偏移回归"的告警实为**误报**，真因是合法收盘标签
      （11:15/14:15）被"必须整点"的旧判据误杀，详见 :data:`_VALID_HOURLY_LABELS`。
      本函数解析口径本身**无需改动**——再次印证 T4 那个"offset≈0"的结论是对的。
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


# 中国期货小时线「合法收盘时刻」白名单（hour, minute 成对）。
#
# ★ 2026-10-08 重订：原判据是「必须整点 + 小时 ∈ {0,1,2,9,10,11,13,14,15,21,22,23}」，
#   但那**误杀了大量合法 bar**。根因：期货日盘 09:00~11:30 / 13:30~15:00 **不是整点切分**，
#   akshare 的 60 分钟 bar 按**交易时段**切，收盘标签本就是 11:15 / 14:15。
#
#   实测标签分布（快照 2026-10-08，akshare 1.18.x futures_zh_minute_sina period=60）：
#     RB / I  : 10:00, 11:15, 14:15, 15:00, 22:00, 23:00
#     JD / LH : 10:00, 11:15, 14:15, 15:00（**本次抽样未见夜盘棒**；
#                规则上 DCE 鸡蛋/生猪有 21:00~23:00 夜盘，故白名单仍保留 21/22/23）
#     CU      : 10:00, 11:15, 14:15, 15:00, 22:00, 23:00, 00:00, 01:00
#     AU      : 09:30, 10:45, 13:45, 14:45, 15:00, 22:00, 23:00, 00:00, 01:00, 02:00
#
#   原判据的丢弃率（**实测**，旧 `minute == 0` 会丢掉所有 minute≠0 的棒）：
#     JD888/LH888（无夜盘，1023 根全日盘）: 11:15(256) + 14:15(256) = **512 根 = 50.0%**
#         ↑ 这正是运行日志里那句「akshare JD888 丢弃率 512/1023」的数字来源
#     RB888   （1023 根，6 种标签）        : 11:15(172) + 14:15(172) = **344 根 = 33.6%**
#   ⚠ 更正一处此前的错误归因：曾把 512 写成「RB 的 345 + 22:00 的 167」。实际 512 全部
#     来自 **JD/LH 的 :15 标签**；RB 的 22:00(167) 当时并未被丢弃（未来戳判据每轮最多
#     命中 1 根「尚未收盘」的棒，不可能丢 167 根历史棒）。数字分属不同品种，不可拼接。
#
#   ⚠ 已知盲区（P0-1，勿依赖白名单兜底）：白名单是**封闭枚举**，只拦「不在集合内」的
#     标签。若源侧发生 **+8h 偏移**，`(15,0) → (23,0)`、`(2,0) → (10,0)` 会**伪装成
#     合法夜盘标签通过校验** —— 即"整批丢弃、不会静默毒化"是**错的**。
#     真正的信号只有 `_fetch_akshare` 的丢弃率告警与覆盖性核对。
#
# 教训（与 2026-09-23「数据质量整改」一并记录）：
#   当时把非整点棒当脏数据整批丢弃，而 akshare 的 60 分钟 bar **本就**按期货
#   交易时段切分，11:15/14:15 是**合法**收盘标签，不是脏数据。
#   **教训**：判"对齐"必须用数据实测得到的合法标签集合，不能想当然按自然小时推。
#   **配套教训**：写"实测数字 ⇒ 因果"的注释时，必须先核对数字取自**哪个品种**——
#   本次就曾把 RB 的 344 与 JD 的 512 拼接成一个错误等式，误导了归因。
_VALID_HOURLY_LABELS: frozenset = frozenset({
    # ── 商品日盘（09:00~11:30 / 13:30~15:00）──
    (10, 0), (11, 15), (14, 15), (15, 0),
    # ── 夜盘：至 23:00（CZCE/DCE/GFEX/SHFE 黑色系）──
    (21, 0), (22, 0), (23, 0),
    # ── 夜盘：至 01:00（SHFE 有色：CU/AL/ZN/PB/NI/SN/AO）──
    (0, 0), (1, 0),
    # ── 夜盘：至 02:30（AU/AG/SC，末段仅 30 分钟）──
    (2, 0), (2, 30),
    # ── 黄金系专属（AU/AG 日盘 09:00~11:30 / 13:30~15:30）──
    (9, 30), (10, 45), (13, 45), (14, 45), (15, 30),
})


def _valid_hourly_ts(dt) -> bool:
    """小时线对齐校验：收盘标签须落在实测合法集合内，且非未收盘的未来时间戳。

    ⚠ 关于「未来时间戳」：sina 的标签是**收盘时刻**，故夜盘进行中的 bar（如
    21:38 时 21:00~22:00 那根标 22:00）天然晚于当前时刻——这是**正常现象**，
    不能算脏数据。但它尚未收盘，用它会把不完整数据当完整数据入库，故仍丢弃，
    等收盘后自然被下一轮采集取回。这是"晚一小时入库"的正确代价。
    """
    if dt is None or dt.tzinfo is None:
        return False
    sh = dt.astimezone(_SH_TZ)
    if (sh.hour, sh.minute) not in _VALID_HOURLY_LABELS:
        return False
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
        **不叠加**任何 +1h。每条棒经 ``_valid_hourly_ts`` 校验：
        收盘标签 ∈ ``_VALID_HOURLY_LABELS``（(hour, minute) 成对白名单）且非未来戳。

        ⚠ 2026-10-08 更正：此处原写「合法交易**整点** + 非整点一律丢弃」，该描述
        **已被本模块推翻**——期货日盘非整点切分，11:15 / 14:15 / 10:45 / 15:30 等
        **本身合法**，按「整点」过滤会误杀（实测丢弃率达 50%）。判据已改为白名单，
        详见 :data:`_VALID_HOURLY_LABELS` 上方说明。
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
        dropped_ohlc = 0
        for _, r in df.iterrows():
            dt = _parse_ak_dt(r.get("datetime"))
            if dt is None or not _valid_hourly_ts(dt):
                dropped += 1
                continue
            # ★ 2026-10-08 补OHLC 校验（此前完全没有，是 bar_5m 里 9 行 low=0 的来源）
            #   为什么要：`_to_dec` 对无法解析的值返回 Decimal(0)，
            #   于是"上游缺 low"会变成 low=0 且**静默入库** —— 实测 l1_mkt.bar_5m
            #   就有 9 行 low=0（2022 年、6 个品种），一路传到 bar_60m，
            #   会污染 ATR / 吊灯止损 / 回撤，且**不报错**。
            #   姊妹模块 minute_collector._valid_minute_row 早有同类校验，
            #   此处是对齐（此前 hourly 侧是唯一缺口）。
            #   判据与 l0_raw.data_quality_flag 的 SQL 口径一致（见 app/data/quality.py）。
            o_, h_, l_, c_ = _to_dec(r.get("open")), _to_dec(r.get("high")), \
                _to_dec(r.get("low")), _to_dec(r.get("close"))
            if is_bad_row(o_, h_, l_, c_):
                dropped_ohlc += 1
                continue
            rows.append(
                {
                    "symbol": spec.symbol,
                    "trade_datetime": dt,
                    "open": o_,
                    "high": h_,
                    "low": l_,
                    "close": c_,
                    "volume": _to_int(r.get("volume")),
                    "oi": _to_int(r.get("hold")),
                    "src": "akshare",
                }
            )
        if dropped_ohlc:
            # fail-loud：脏 OHLC 是上游问题，需人工复核口径，故 error 而非 warning
            logger.error(
                f"[hourly] {spec.symbol} 丢弃 {dropped_ohlc} 根 OHLC 不自洽的棒"
                f"（high<max(o,c) / low>min(o,c) / 价格<=0 / 解析失败）—— "
                f"上游数据口径可能已变，请复核 akshare 返回；已拒入库（不入脏数据）"
            )
        # 丢弃率异常偏高 → **先怀疑白名单过时（合法标签被误杀），再怀疑源侧时区偏移**。
        # ⚠ 2026-10-08 更正：原注释与文案只写「疑似 sina 标签时区偏移回归」，而本次
        #   事故的真因恰是**前者**（合法 :15 标签被误杀），两者指向完全不同的排查方向。
        # ⚠ 阈值 0.5 是刀刃：JD888 实测 512/1023=0.5005 恰好触发，511/1023=0.4995
        #   则不触发 —— 且某品种只丢 40% 完全无声。别把它当可靠护栏。
        if raw and dropped / raw > 0.5:
            logger.warning(
                f"[hourly] akshare {spec.symbol} 丢弃率 {dropped}/{raw} 异常偏高 —— "
                f"先比对 _VALID_HOURLY_LABELS 是否已过时（合法收盘标签被误杀，"
                f"如 11:15/14:15/10:45/15:30 等非整点标签是合法的），"
                f"再怀疑 sina 标签时区偏移回归（PRD §6.6）；请复核 akshare 数据口径"
            )
        return rows


__all__ = ["HourlyCollector"]
