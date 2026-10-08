"""风控空转 + 小时线标签判据 —— 回归守护（2026-10-08 修复）。

两个被修的缺陷都属于**「判据」类**：判据写错了不会抛异常，只会让数字悄悄变错。
这正是单测最该覆盖、也最容易漏掉的区域——修复前这两处**零覆盖**。

守护内容
--------
A. ``_valid_hourly_ts``：把「合法收盘标签」钉成显式契约。
   修复前判据是 ``minute == 0``，把合法的 11:15 / 14:15 误杀
   （实测 RB 丢弃率 512/1023 = 50%，每场丢 2 根日盘中间的 bar）。
B. ``brake_scalar`` / ``current_drawdown``：把「不可用」与「零回撤」钉成不同结果。
   修复前 ``peak <= 0``（全程亏损）静默返回 0.0 回撤 → 满仓放行。
C. ``brake_scalar`` 在 ``enabled=True`` 但无数据时**必须记 error**，
   避免「熔断关闭」与「开着但空转」压成同一个读数 1.0（虚假安全感）。
"""
from __future__ import annotations

import datetime as dt

import pytest
from loguru import logger as _loguru

from app.ingest.hourly_collector import _VALID_HOURLY_LABELS, _valid_hourly_ts
from app.risk.portfolio_brake import (
    BrakeConfig,
    brake_scalar,
    current_drawdown,
    equity_curve_status,
    load_equity_curve,
)


class _LogCapture:
    """收集 loguru 的 ERROR 及以上消息。

    ⚠ 不用 pytest 的 ``caplog``：项目统一走 loguru（``app/core/logging.py``
    只给 loguru 配 sink，全仓无 ``InterceptHandler``），标准库 ``caplog``
    **抓不到** loguru 记录 —— 用它会让"必须留 error 痕迹"的断言恒假，
    等于这道守护形同虚设（本次修复即踩到了：改用 loguru 后 caplog 全红）。
    """

    def __init__(self, level: str = "ERROR"):
        self._records = []
        self._level = level
        self._sid = None

    def __enter__(self):
        self._sid = _loguru.add(lambda m: self._records.append(str(m)), level=self._level)
        return self

    def __exit__(self, *exc):
        if self._sid is not None:
            _loguru.remove(self._sid)

    @property
    def text(self) -> str:
        return "\n".join(self._records)

    def has(self, needle: str) -> bool:
        return needle in self.text


def _ts(h: int, m: int = 0, day: int = 1) -> dt.datetime:
    """构造上海时区的收盘标签时刻（默认 2026-01-01，保证已收盘、不触发未来戳）。"""
    return dt.datetime(2026, 1, day, h, m, tzinfo=dt.timezone(dt.timedelta(hours=8)))


# ---------------------------------------------------------------------------
# A. 小时线「合法收盘标签」判据
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("h,m", [
    (10, 0), (11, 15), (14, 15), (15, 0),      # 商品日盘（实测口径）
    (21, 0), (22, 0), (23, 0),                # 夜盘至23:00
    (0, 0), (1, 0), (2, 0), (2, 30),          # 夜盘至02:30（SC/AU/AG）
    (9, 30), (10, 45), (13, 45), (14, 45), (15, 30),  # 黄金系专属
])
def test_valid_labels_accepted(h, m):
    """实测合法的收盘标签必须被接受（修复前 11:15/14:15 会被误杀）。"""
    assert _valid_hourly_ts(_ts(h, m)) is True, f"({h}:{m:02d}) 应为合法收盘标签"


@pytest.mark.parametrize("h,m", [
    (11, 0), (14, 0), (12, 0), (8, 0),        # 自然整点但非实际收盘口径
    (10, 30), (14, 30), (15, 15), (23, 30),   # 半点/异常
    (4, 0), (5, 0), (6, 0),                   # 凌晨无夜盘时段
])
def test_invalid_labels_rejected(h, m):
    """非实际收盘口径的标签必须被拒（纵深防御，防脏数据入库）。"""
    assert _valid_hourly_ts(_ts(h, m)) is False, f"({h}:{m:02d}) 不该被当作合法标签"


def test_future_timestamp_rejected():
    """未收盘 bar（收盘标签晚于当前时刻）必须丢弃 —— 用半成品 bar 会算错点位。"""
    future = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=30)
    assert _valid_hourly_ts(future) is False


def test_whitelist_is_sorted_and_unique():
    """白名单不得有重复项（否则后续维护容易误以为覆盖更广）。"""
    assert len(_VALID_HOURLY_LABELS) == len(set(_VALID_HOURLY_LABELS))


# ---------------------------------------------------------------------------
# B/C. 风控：不可用 ≠ 零回撤
# ---------------------------------------------------------------------------
def test_drawdown_peak_nonpositive_is_not_silently_zero():
    """权益全程为负（peak<=0）时，回撤**不得**静默返回 0.0。

    修复前返回 0.0 → 上层判定「零回撤、无需减仓」→ 账户正在亏损却满仓放行。
    """
    eq = [-100.0, -200.0, -300.0]
    dd = current_drawdown(eq)
    assert dd == float("inf"), "peak<=0 必须返回 inf（不可计算），不能是 0.0"


def test_scalar_with_invalid_equity_does_not_cut_and_logs():
    """权益口径异常时：不做减仓动作（宁可没保护也不能胡乱保护），但必须记 error。"""
    cfg = BrakeConfig(enabled=True, dd_warn=0.15, dd_stop=0.25)
    with _LogCapture() as cap:
        sc = brake_scalar([-100.0, -200.0], cfg)
    assert sc == 1.0, "口径异常时不得据此清仓/减半仓"
    assert cap.has("回撤"), (
        "口径异常必须留 error 痕迹（否则又变成静默放行）")


def test_scalar_disabled_is_identity():
    """熔断显式关闭时恒等放行，且**不**报警（这是正常状态，不是缺陷）。"""
    cfg = BrakeConfig(enabled=False)
    with _LogCapture() as cap:
        assert brake_scalar([], cfg) == 1.0
        assert brake_scalar([100.0, 50.0], cfg) == 1.0
    assert not cap.has("熔断空转"), "熔断显式关闭属正常态，不该报『空转』"


def test_scalar_enabled_but_empty_logs_error():
    """enabled=True 但权益曲线不可用 → 恒等放行 + **必须记 error**。

    这是本次事故的核心读数：修复前 `if not cfg.enabled or not equity: return 1.0`
    把「熔断关闭」与「开着但空转」压成同一个 1.0，任何只读 scalar 的自检都会
    报「熔断正常」——虚假安全感在读数层面从未消除。
    """
    cfg = BrakeConfig(enabled=True)
    with _LogCapture() as cap:
        sc = brake_scalar([], cfg)
    assert sc == 1.0
    assert cap.has("熔断空转"), (
        "空转必须在读数层留 error，否则看板/自检仍会误报『熔断正常』")


def test_scalar_thresholds_work():
    """正常口径下阈值行为不变：>25% 清仓、>15% 半仓、其余不动。"""
    cfg = BrakeConfig(enabled=True, dd_warn=0.15, dd_stop=0.25)
    eq = [1000.0, 1000.0, 700.0]      # 回撤 30% → 清仓
    assert brake_scalar(eq, cfg) == 0.0
    eq = [1000.0, 1000.0, 800.0]      # 回撤 20% → 半仓
    assert brake_scalar(eq, cfg) == 0.5
    eq = [1000.0, 1000.0, 950.0]      # 回撤 5% → 不动
    assert brake_scalar(eq, cfg) == 1.0


def test_equity_status_shape():
    """诊断字典必须始终暴露 ok/reason/n_days/need_days 四个键（告警文案依赖）。"""
    st = equity_curve_status()
    for k in ("ok", "reason", "n_days", "need_days"):
        assert k in st, f"缺少诊断键 {k}"

# ---------------------------------------------------------------------------
# D. load_equity_curve：样本门槛 + reason 分类（修复 A 的核心机制，此前零覆盖）
# ---------------------------------------------------------------------------
def _fake_session(rows):
    """构造一个只提供 portfolio_equity 查询结果的假 session。

    rows 为 ``[(trade_date, equity|None, realized_pnl|None), ...]``，
    按真实实现的 ``ORDER BY trade_date DESC`` 返回（即倒序），函数内部会 ``[::-1]``。
    """
    class _Res:
        def fetchall(self_inner):
            return list(rows)
    class _Sess:
        rolled = 0
        def execute(self_inner, *a, **k):
            return _Res()
        def rollback(self_inner):
            type(self_inner).rolled += 1
    return _Sess()


@pytest.mark.parametrize("n_days,expect_ok", [(21, True), (20, True), (19, False), (3, False)])
def test_min_days_boundary_on_equity_path(n_days, expect_ok):
    """样本门槛边界：恰好 ``min_days`` 应可用，少一天即不可用。

    若被改成 ``<=``，恰好 20 天会被判不可用 → **生产上熔断永久空转**（原缺陷复现）。
    """
    rows = [(dt.date(2026, 1, i + 1), 1000.0 - i, None) for i in range(n_days)]
    eq = load_equity_curve(session=_fake_session(rows), min_days=20)
    st = equity_curve_status()
    assert bool(eq) is expect_ok, f"{n_days} 天：期望可用={expect_ok}，实际 {len(eq)} 条"
    if not expect_ok:
        assert st["reason"] == "insufficient_days_equity"
        assert st["n_days"] == n_days, "诊断必须报告真实样本数，否则告警文案会误导"


def test_realized_pnl_path_reason_and_cumulative_order():
    """realized_pnl 回退路径：equity 全None 时应走该路，且**按时间正序累加**。

    顺序若被弄反（``rows[::-1]`` 被删），权益曲线会时间倒序 → 回撤方向算错
    → 可能误触发 25% 清仓。
    """
    pnl = [10.0, -3.0, 5.0] * 10          # 30 个交易日，混合正负
    rows = [(dt.date(2026, 1, i + 1), None, pnl[i]) for i in range(30)]
    # 真实实现按 DESC 取，故这里给倒序
    rows_desc = list(reversed(rows))
    eq = load_equity_curve(session=_fake_session(rows_desc), min_days=20)
    assert eq, "30 个交易日应可用"
    assert eq[0] == pytest.approx(pnl[0]), "累加曲线首项应等于最早一日的 pnl"
    assert eq[-1] == pytest.approx(sum(pnl)), "末项应等于全期 pnl 之和（验证正序累加）"
    assert equity_curve_status()["reason"] == "realized_pnl"


def test_table_empty_reason():
    """表为空（0 行）→ reason 指向数据缺失，便于与 query_error 区分。"""
    eq = load_equity_curve(session=_fake_session([]), min_days=20)
    assert eq == []
    assert equity_curve_status()["reason"] == "portfolio_equity_empty_or_unavailable"


def test_query_error_reason_not_overwritten():
    """★ DB 异常时 reason 必须是 ``query_error:*``，不能被"表为空"覆盖。

    这是一条**真实缺陷**的回归：修复前 except 块里 ``_mark(query_error:*)`` 没有
    ``return``，会继续落到下面 ``_mark(portfolio_equity_empty_or_unavailable)``
    把 reason 覆盖掉。后果是 DB 挂掉时告警正文变成「请补 portfolio_equity」，
    让运维去**填数据表** —— 填完告警照旧响。
    """
    class _Boom:
        def execute(self, *a, **k):
            raise RuntimeError("connection refused")
        def rollback(self):
            pass
    eq = load_equity_curve(session=_Boom(), min_days=20)
    assert eq == []
    assert equity_curve_status()["reason"] == "query_error:RuntimeError", (
        "query_error 未被保留 —— 告警会把 DB 故障误导成『请补数据表』")


# ---------------------------------------------------------------------------
# E. inf / NaN 不得触发清仓（放行方向的静默失效）
# ---------------------------------------------------------------------------
def test_nan_equity_does_not_trigger_liquidation():
    """equity 含 NaN → dd 为 NaN；NaN 与任何阈值比较恒为 False。

    若守卫是 ``dd == float("inf")``（精确等值），NaN 会**静默穿过**并落到
    ``return 1.0``，且一条日志都不记 —— 防线失效。故必须用 ``isfinite``。
    """
    cfg = BrakeConfig(enabled=True, dd_warn=0.15, dd_stop=0.25)
    with _LogCapture() as cap:
        sc = brake_scalar([1000.0, 900.0, float("nan")], cfg)
    assert sc == 1.0, "NaN 绝不能被当成巨量回撤而清仓"
    assert cap.has("不可计算"), "NaN 必须留 error 痕迹（否则静默失效）"


def test_inf_drawdown_never_reaches_liquidation_branch():
    """``inf > dd_stop`` 在 Python 中为 True —— 必须确认inf 被守卫拦下。

    删除 brake_scalar 里的 isfinite 分支会让本测试失败（直接返回 0.0 清仓），
    那正是修复反复强调要避免的「胡乱保护」。
    """
    cfg = BrakeConfig(enabled=True, dd_warn=0.15, dd_stop=0.25)
    assert brake_scalar([-1.0, -2.0, -3.0], cfg) == 1.0