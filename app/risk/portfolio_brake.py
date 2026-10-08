# -*- coding: utf-8 -*-
"""V5 · 组合级回撤熔断（执行层风控标量）。

来源：《PRD_借鉴落地验证.md》V5 + 《架构设计_CB执行版.md》T20（D2 阈值 15% / 25%）。

定位（架构铁律）
----------------
* **不动** `walk_fusion_states` 状态机 —— 本模块只在其**下游执行层**叠加一个
  全局标量，缩放"期望手数"。故不触发 Qi Analisy skill 双源同步（该铁律只约束
  状态机真源）。
* 与因子层的 `position_cap_scalar` **乘法合成**，互不干扰。
* `enabled=false`（默认）时 `scalar ≡ 1.0`，与"从未上线"逐位等价（T9 回归保证）。

阈值（D2 初设，可由历史净值回放反推微调）
----------------------------------------
    回撤 > 15% → scalar = 0.5（降半仓）
    回撤 > 25% → scalar = 0.0（清仓观望）
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# ★ 2026-10-08：原为 `logging.getLogger(__name__)`（标准库），**日志进不了项目日志文件**。
#   `app/core/logging.py` 只给 loguru 配了 sink（控制台 + qhyc.log + qhyc.err.log），
#   全仓**没有** InterceptHandler / basicConfig / dictConfig —— loguru 默认不拦截
#   标准库 logging，故本模块此前的 error 只落到 stderr，运维翻 `qhyc.err.log`
#   根本看不到。而本模块的日志正是"风控为何空转"的唯一诊断出处（区分
#   insufficient_days / query_error / 表为空），必须进文件，故改用项目 logger。
from app.core.logging import logger

#: 权益曲线可用的**最小交易日数**（2026-10-08 新增）。
#:
#: 为什么要门槛：回撤 = 峰值到谷底。样本只有 2~3 天时，"峰值"很可能只是某一天
#: 的运气，DD 会剧烈抖动 → 一根假回撤就可能触发 dd_stop(25%) 把仓位清零。
#: 那比"熔断空转"更糟：空转只是没保护，误触发是**胡乱保护**（在无回撤时清仓）。
#:
#: ⚠ **20 是经验值，未经净值回放校准**（与本文件顶部对 dd_warn/dd_stop 的
#:   "D2 初设，可由历史净值回放反推微调"规格不一致——那两个明确标了待校准，
#:   这个却像既成结论）。待办：用真实净值做「样本量-误报率」回放，定出触发
#:   dd_stop 误报的最小样本量。
#:
#: ⚠ **本门槛只管天数，不管曲线起点符号**：realized_pnl 回退路径构造的是
#:   **从 0 起累加**的曲线（``cum[0] == pnl[0]``），故即使满 20 天也可能全程为负
#:   → ``peak <= 0`` → :func:`current_drawdown` 返回 ``inf`` → 恒等放行 1.0。
#:   即**调大 min_days 会提高该分支的命中率**，不是纯粹的"更安全"。
#:
#: ⚠ 生产调用方 ``scheduler._apply_portfolio_brake`` **未传 min_days**，
#:   当前恒为 20，无调节入口（需改 scheduler 才可调）。
EQUITY_MIN_DAYS = 20


@dataclass
class BrakeConfig:
    enabled: bool = False      # 默认关闭 = 旧行为
    dd_warn: float = 0.15      # 回撤 > 15% → 半仓
    dd_stop: float = 0.25      # 回撤 > 25% → 清仓
    lookback: int = 250        # 峰值回看窗口（交易日）


def load_brake_config() -> BrakeConfig:
    """从 cfg_feature_switch（迁移 008）读取熔断配置；表缺失/异常时退回默认（关闭）。

    ⚠ 当前**无生产调用方**（2026-10-08 核查）：`scheduler._apply_portfolio_brake`
      并**不**调用本函数，而是
        · 开关走 `_switch_on("portfolio_brake_enabled", "PORTFOLIO_BRAKE_ENABLED")`
          （优先读 DB，表缺失才回退 env）；
        · 阈值一律 `os.getenv(PORTFOLIO_BRAKE_DD_WARN / _DD_STOP / _LOOKBACK)`。

      故本函数解析的 ``cfg_feature_switch.portfolio_brake_enabled`` 里那个 JSON
      （``{"dd_warn":0.15,...}``）**不会生效**。原 docstring 声称"改库内 JSON 即可调阈值"
      是错的——照做会得到"配置显示可调、实际不生效"，与本次事故同构
      （开关显示开启、实际空转）。若要真正做到库内可调，需先改 scheduler 改用本函数。
    """
    from app.core.feature_switch import is_enabled, get_value

    enabled = is_enabled("portfolio_brake_enabled", default=False)
    cfg = BrakeConfig(enabled=enabled)
    raw = get_value("portfolio_brake_enabled")
    if raw:
        try:
            import json
            kv = json.loads(raw) if raw.strip().startswith("{") else {}
            cfg.dd_warn = float(kv.get("dd_warn", cfg.dd_warn))
            cfg.dd_stop = float(kv.get("dd_stop", cfg.dd_stop))
            cfg.lookback = int(kv.get("lookback", cfg.lookback))
        except (ValueError, TypeError):
            logger.warning("[portfolio_brake] value 解析失败，用默认值")
    return cfg


def max_drawdown(equity: list[float]) -> float:
    """权益序列的最大回撤（正数，如 0.18 表示 -18%）。"""
    if not equity:
        return 0.0
    peak = equity[0]
    mdd = 0.0
    for v in equity:
        if v > peak:
            peak = v
        if peak > 0:
            d = (peak - v) / peak
            if d > mdd:
                mdd = d
    return mdd


def current_drawdown(equity: list[float], lookback: int = 250) -> float:
    """当前回撤（相对回看窗口内的峰值）。

    ⚠ ``peak <= 0`` 的处置（2026-10-08 修正）
    ------------------------------------------
    ``load_equity_curve`` 的 realized_pnl 回退构造的是**从 0 起累加**的曲线
    （``cum[0] == pnl[0]``），故窗口内净亏损时 ``max(win) <= 0``，回撤在数学上
    **无定义**。原实现直接 ``return 0.0`` —— 等于告诉上层"零回撤、无需减仓"，
    即**账户正在亏钱时熔断反而满仓放行**，且毫无告警。这是最坏的放行方向静默失效，
    且恰好会在用户按告警建议①"去补 portfolio_equity"、改用净值口径之前被激活。

    现改为：口径异常时**大声告警并返回 ``inf``**，由 :func:`brake_scalar` 统一
    降级为"不可用"处理（记 error + 不做减仓动作）。之所以**不**直接清仓：
    权益口径本身不可信时，任何回撤数字都不可信，据此清仓同样是错的 ——
    宁可"没保护"（并让原因可见），也不要"胡乱保护"。

    ⚠ ``brake_scalar`` 的守卫是 ``not math.isfinite(dd)``（2026-10-08 由
      ``dd == float("inf")`` 修正）。若 equity 含 **NaN**，``max(win)`` 会传播 NaN
      → dd 为 NaN，而 ``nan > 阈值`` 恒为 False —— **精确等值比较会直接漏过**，
      防线静默失效。故必须用 ``isfinite`` 把 NaN 一并纳入"不可计算"分支。
    """
    if not equity:
        return 0.0
    win = equity[-lookback:] if lookback > 0 else equity
    peak = max(win)
    if peak <= 0:
        logger.error(
            "[portfolio_brake] 权益曲线峰值={:.2f} ≤ 0（窗口内无正权益/全程亏损），"
            "回撤在数学上无定义 → **不得静默当作零回撤**（那会让『正在亏损』被判定为"
            "『无需减仓』→ 满仓放行）。请核对 equity 口径：应为账户净值（含初始资金），"
            "而非从 0 起的累计盈亏。".format(peak)
        )
        return float("inf")
    return (peak - win[-1]) / peak


def brake_scalar(equity: list[float], cfg: BrakeConfig) -> float:
    """按当前回撤计算组合刹车标量。

    Returns:
        1.0（不动）/ 0.5（半仓）/ 0.0（清仓）。cfg.enabled=False 时恒为 1.0。

    ★ 2026-10-08：``enabled=True`` 但权益曲线不可用时，此处**记 error**。
      此前 ``if not cfg.enabled or not equity: return 1.0`` 把两种截然不同的状态
      （"熔断被显式关闭" 与 "开着但拿不到数据"）压成同一个读数 1.0，导致任何
      只读 scalar 的自检/看板都会报"熔断正常"——**虚假安全感在读数层面从未消除**。
    """
    if not cfg.enabled:
        return 1.0
    if not equity:
        logger.error(
            "[portfolio_brake] cfg.enabled=True 但权益曲线不可用 → 恒等放行 scalar=1.0，"
            "**熔断空转**（无保护）。诊断：{}".format(equity_curve_status())
        )
        return 1.0
    dd = current_drawdown(equity, cfg.lookback)
    # ★ 2026-10-08 修正（代码审查 P0-6）：原守卫是 `dd == float("inf")` —— **精确等值比较**。
    #   若 equity 含 NaN，`max(win)` 会传播 NaN → dd 为 NaN，而
    #     `nan == inf` → False、`nan > dd_stop` → False
    #   ⇒ 直接落到末尾 `return 1.0`，**连一条日志都不记**，防线静默失效。
    #   equity 来自 DB 的 float(r[1])，列里出现 NaN 完全可能，故改为 `isfinite` 判据，
    #   把 NaN 一并纳入「回撤不可计算」分支（与 inf 同等处置：不做减仓、但留痕）。
    if not math.isfinite(dd):
        # 权益口径异常（峰值 ≤ 0）或数据含 NaN：回撤不可计算。刻意**不**据此减仓/清仓，
        # 因为口径不可信时任何动作都是错的；已在 current_drawdown / 此处记 error。
        logger.error("[portfolio_brake] 回撤不可计算（dd={}，权益口径异常或含 NaN）"
                     "→ 恒等放行，**本次未做任何减仓**".format(dd))
        return 1.0
    if dd > cfg.dd_stop:
        logger.warning(f"[portfolio_brake] 回撤 {dd:.2%} > {cfg.dd_stop:.0%} → 清仓观望")
        return 0.0
    if dd > cfg.dd_warn:
        logger.warning(f"[portfolio_brake] 回撤 {dd:.2%} > {cfg.dd_warn:.0%} → 降半仓")
        return 0.5
    return 1.0


def apply(expected_lots: float, equity: list[float], cfg: BrakeConfig) -> float:
    """把刹车标量应用到期望手数。

    ⚠ 当前**无生产调用方**（2026-10-08 核查）：`scheduler._apply_portfolio_brake`
      直接用 `brake_scalar()` 再**自行 ceil 取整**（``max(1, ceil(lots*sc - 1e-9))``），
      并在 ``sc <= 0`` 时直接把 lots 置 0。本函数**不取整**，两条路径口径不同，
      切勿混用或"照着改"——改了不会有任何效果（scheduler 根本不调它）。
      原 docstring 称此处为"执行层唯一入口"是失真的。
    """
    s = brake_scalar(equity, cfg)
    if s >= 1.0:
        return expected_lots
    return expected_lots * s


#: ``load_equity_curve`` 最近一次调用的诊断信息（供上层告警与自检读取）。
#:
#: ★ 2026-10-08 新增。存在的理由：此前该函数在无数据时只 ``logger.warning`` 并返回
#: ``[]``，上层据此「恒等放行」——于是**开关显示开启、风控实际空转**，而这件事
#: 只有翻日志才知道（生产事故：``cfg_feature_switch.portfolio_brake_enabled=True``
#: 而 ``portfolio_equity`` 0 行，运行期一直打 warning，无人察觉）。
#: 现在把「为什么不可用」显式暴露出来，让上层能据此告警。
LAST_EQUITY_STATUS: dict = {
    "ok": False,
    "reason": "not_called",
    "n_days": 0,
    "need_days": EQUITY_MIN_DAYS,
}


def equity_curve_status() -> dict:
    """返回 :data:`LAST_EQUITY_STATUS` 的拷贝（诊断用，不影响业务）。"""
    return dict(LAST_EQUITY_STATUS)


def load_equity_curve(session, symbol: str | None = None,
                      lookback: int = 250,
                      min_days: int = EQUITY_MIN_DAYS) -> list[float]:
    """从已平仓盈亏派生组合权益曲线（单位：元，起点 0 的累计盈亏）。

    数据源优先级：
      1. `portfolio_equity` 的 ``equity``（累计权益，口径正确）
      2. 回退：同表 ``realized_pnl`` 逐日累加成权益曲线
         （⚠ 不能直接用 realized_pnl 序列算回撤：那是**单日盈亏**，
           其峰值是"赚最多的那一天"，算出来的是假回撤）

    ★ min_days（2026-10-08 新增）：权益曲线**至少要有 ``min_days`` 个交易日**
    才有统计意义。样本不足时返回 ``[]``（= 不可用），**而不是**拿 2~3 天数据去算
    回撤——那种回撤纯属噪声，却会触发 dd_stop 把仓位清零，比"熔断空转"更危险：
    空转只是"没保护"，误触发是"胡乱保护"。

    实测（2026-10-08）：``portfolio_equity`` 0 行；唯一可派生的
    ``fusion_signal_log.pnl`` 只有 **3 个交易日**（09-29/09-30/10-08），且
    pnl 是**收益率**而非金额，无法直接当权益累加。故当前**只能诚实判定为不可用**。
    """
    from sqlalchemy import text

    def _mark(ok: bool, reason: str, n_days: int = 0) -> None:
        LAST_EQUITY_STATUS.update(
            ok=ok, reason=reason, n_days=n_days, need_days=min_days)

    try:
        rows = session.execute(text(
            "SELECT trade_date, equity, realized_pnl FROM portfolio_equity "
            "ORDER BY trade_date DESC LIMIT :n"), {"n": lookback}).fetchall()
        rows = rows[::-1]
        eq = [float(r[1]) for r in rows if r[1] is not None]
        if eq:
            if len(eq) < min_days:
                _mark(False, "insufficient_days_equity", len(eq))
                logger.error(
                    f"[portfolio_brake] 权益曲线样本不足：{len(eq)} 个交易日 < "
                    f"下限 {min_days} → 判定为**不可用**（不据此计算回撤）。"
                    f"样本过少时回撤纯属噪声，误触发 dd_stop 会把仓位清零，"
                    f"比空转更危险。")
                return []
            _mark(True, "equity", len(eq))
            return eq
        pnl = [float(r[2]) for r in rows if r[2] is not None]
        if pnl:
            if len(pnl) < min_days:
                _mark(False, "insufficient_days_realized_pnl", len(pnl))
                logger.error(
                    f"[portfolio_brake] 已实现盈亏样本不足：{len(pnl)} 个交易日 < "
                    f"下限 {min_days} → 判定为**不可用**")
                return []
            cum, acc = [], 0.0
            for v in pnl:
                acc += v
                cum.append(acc)
            _mark(True, "realized_pnl", len(pnl))
            return cum
    except Exception as e:  # noqa: BLE001  表不存在或字段缺失 → 走回退
        # ★ 2026-10-08 修正（代码审查 pr-test-analyzer 抓到）：原实现 `_mark(query_error:*)`
        #   **没有 return**，会继续落到下面 `_mark(portfolio_equity_empty_or_unavailable)`
        #   把 reason **覆盖掉** ⇒ `query_error:*` 是死代码，调用方永远看不到。
        #   后果很实际：DB 挂掉时推给用户的告警正文是「请补 portfolio_equity」，
        #   让运维去**填数据表**；填完告警照旧响 —— 正是本次修复要防的"照旧方向
        #   排查错"。且节流键含 reason 的设计也因此失去意义。
        # 修法：query 异常时**立即返回**，并让告警文案明确指向"数据库/查询故障"。
        _mark(False, f"query_error:{type(e).__name__}")
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 —— rollback 自身失败也别掩盖上面的诊断
            pass
        logger.error(
            "[portfolio_brake] 权益曲线查询失败（{}），无法计算回撤。"
            "**熔断实际未生效**（恒等放行）。这是**数据库/查询故障**，"
            "不是数据缺失 —— 请先查连接与表结构，勿盲目补 portfolio_equity 造数据。".format(
                type(e).__name__)
        )
        return []

    _mark(False, "portfolio_equity_empty_or_unavailable")
    logger.error(
        "[portfolio_brake] portfolio_equity 不可用（0 行 / 表缺失），无法计算回撤。"
        "**熔断实际未生效**（恒等放行）。请补权益曲线或关闭 cfg_feature_switch."
        "portfolio_brake_enabled —— 不要让它显示为'已开启'。")
    return []
