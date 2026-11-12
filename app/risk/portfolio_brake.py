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


#: 阈值字段 → env 变量名（env 作为库内配置缺失时的**部署兜底**）。
#:
#: ⚠ 这些 env 是既有部署实际在用的调参入口（PORTFOLIO_BRAKE_DD_WARN 等），
#:   接线时**必须保留**：直接换成"只读库内 JSON"会让线上失去 env 兜底。
_ENV_THRESHOLDS: dict[str, str] = {
    "dd_warn": "PORTFOLIO_BRAKE_DD_WARN",
    "dd_stop": "PORTFOLIO_BRAKE_DD_STOP",
    "lookback": "PORTFOLIO_BRAKE_LOOKBACK",
}

#: 开关的 env 兜底（与 scheduler._switch_on 的判定口径一致：'1'/'0'）。
_ENV_ENABLED = "PORTFOLIO_BRAKE_ENABLED"


def _read_switch_row(switch_key: str = "portfolio_brake_enabled"):
    """直读 ``cfg_feature_switch`` 一行，返回 ``(enabled, value)``；无行/异常返回 None。

    ⚠ **不走** :func:`app.core.feature_switch.is_enabled` / :func:`get_value`：
      那两个**无法区分**「库内明确 ``enabled=false``」与「库里根本没这一行」——
      而这正是 env 兜底该不该生效的分界：

        · 库内**有**这一行 → 以库内为准（运维显式关了就该关，env 不该翻案）；
        · 库内**没有**这一行（表缺失 / 未迁移） → 沿用 env 部署口径（保持既有行为）。

      若统一用 ``is_enabled(key, default=False)``，第二种情况会被当成
      「库内明确关闭」→ env 兜底永久失效 → 线上熔断再也开不起来（静默失效）。
    """
    from sqlalchemy import text

    try:
        from app.core.db import get_engine

        with get_engine().connect() as conn:
            row = conn.execute(
                text("SELECT enabled, value FROM cfg_feature_switch WHERE switch_key = :k"),
                {"k": switch_key},
            ).first()
        if row is None:
            return None
        return bool(row[0]), row[1]
    except Exception as exc:  # noqa: BLE001 —— 表缺失/连接失败 → 交由调用方回退 env
        logger.opt(exception=True).warning(
            f"[portfolio_brake] 读 cfg_feature_switch 失败（回退 env/默认）：{exc}")
        return None


def load_brake_config(*, env_fallback: bool = True) -> BrakeConfig:
    """读取组合熔断配置（库内 ``cfg_feature_switch`` 优先，env 兜底）。

    ★ 2026-11-06 接线（此前本函数**无生产调用方**）
      ------------------------------------------------
      旧实现在 :func:`scheduler._apply_portfolio_brake` 里**不被调用**：
      scheduler 自己用 ``_switch_on(...)`` 拼 ``BrakeConfig``，阈值一律取 env。
      于是本函数解析的 ``cfg_feature_switch.portfolio_brake_enabled`` 里那个
      JSON（``{"dd_warn":0.15,...}``）**永远不生效** —— 即"配置显示可调、
      实际不生效"，与本次体检同构的静默失效。现已由 scheduler 调用。

    取值优先级（**每项独立**，库内显式值 > env > 内置默认）：

    ==============  ==========================================================
    字段             优先级
    ==============  ==========================================================
    ``enabled``      库内 ``enabled`` 列（行存在时）→ 否则 env ``PORTFOLIO_BRAKE_ENABLED``
    ``dd_warn``      库内 JSON ``dd_warn`` → env ``PORTFOLIO_BRAKE_DD_WARN`` → 0.15
    ``dd_stop``      库内 JSON ``dd_stop`` → env ``PORTFOLIO_BRAKE_DD_STOP`` → 0.25
    ``lookback``     库内 JSON ``lookback`` → env ``PORTFOLIO_BRAKE_LOOKBACK`` → 250
    ==============  ==========================================================

    ⚠ env 兜底**不可省**：既有部署用这几个 env 调参；只读库会让线上失去兜底。
    ⚠ 库内 JSON 解析失败**不抛错**：熔断失效可以，误杀不行（记 warning 后继续）。
    """
    import json
    import os

    defaults = BrakeConfig()

    row = _read_switch_row()
    if row is None:
        # 库里没有这一行（表缺失/未迁移）→ 沿用 env 部署口径（保持既有行为）。
        enabled = False
        if env_fallback:
            enabled = os.getenv(_ENV_ENABLED, "0") == "1"
        kv: dict = {}
    else:
        enabled, raw = row
        kv = {}
        if raw:
            try:
                parsed = json.loads(raw) if str(raw).strip().startswith("{") else {}
                if isinstance(parsed, dict):
                    kv = parsed
            except (ValueError, TypeError):
                logger.warning(
                    "[portfolio_brake] cfg_feature_switch.value 不是合法 JSON，"
                    "本轮阈值改用 env/默认值")

    def _num(field: str, cast):
        v = kv.get(field)
        if v is not None:
            try:
                return cast(v)
            except (ValueError, TypeError):
                logger.warning(
                    f"[portfolio_brake] 库内 {field}={v!r} 类型非法，改用 env/默认值")
        if env_fallback:
            ev = os.getenv(_ENV_THRESHOLDS[field])
            if ev is not None and str(ev).strip() != "":
                try:
                    return cast(ev)
                except (ValueError, TypeError):
                    logger.warning(
                        f"[portfolio_brake] env {_ENV_THRESHOLDS[field]}={ev!r} 非法，改用默认值")
        return getattr(defaults, field)

    return BrakeConfig(
        enabled=enabled,
        dd_warn=_num("dd_warn", float),
        dd_stop=_num("dd_stop", float),
        lookback=_num("lookback", int),
    )


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


@dataclass
class BrakeState:
    """一轮扫描应生效的组合熔断状态（CL-20261111-13 / A 接线 S1）。

    刻意做成**值对象 + fail-open**：任何异常都返回 ``scalar=1.0``，
    「熔断失效可以，误杀不行」（与 ``scheduler._apply_portfolio_brake`` 同纪律）。
    ``idle=True`` = 开关开着却没真正保护，这是**必须被看见**的状态，不是正常降级。
    """

    scalar: float = 1.0
    enabled: bool = False
    idle: bool = False
    reason: str = "not_called"
    n_days: int = 0
    need_days: int = EQUITY_MIN_DAYS


def brake_state_for_session(session) -> BrakeState:
    """读取开关 + 权益曲线，返回本轮应生效的熔断标量（V5X 消费点入口）。

    ★ 为什么放在 ``app/risk`` 而不是 scheduler（CL-20261111-13 / A 接线）
      原唯一消费点 ``scheduler._apply_portfolio_brake`` 位于 ``_fusion_scan_job``
      里 ``fusion_v34`` 未启用时的 early return **之后**；生产
      ``fusion_v34_live_enabled=f``（推送源唯一化为 V5X）⇒ 该分支永不可达，
      组合熔断在投产链路上从未生效。修法不是把 V3.4 打开（那会造成双源冲突），
      而是给 V5X 加独立消费点；放在 ``app/risk`` 可避免 live.py 反向 import
      scheduler 造成循环依赖。

    判据：开关关 → scalar=1.0（``enabled=False``，非 idle）；开关开但曲线不可用
    → scalar=1.0 + ``idle=True``（空转，上层必须留痕）；曲线可用 → :func:`brake_scalar`。
    """
    try:
        cfg = load_brake_config(env_fallback=True)
        if not cfg.enabled:
            return BrakeState(reason="disabled")
        equity = load_equity_curve(session=session, lookback=cfg.lookback)
        if not equity:
            st = equity_curve_status()
            return BrakeState(
                enabled=True, idle=True,
                reason=str(st.get("reason") or "no_equity"),
                n_days=int(st.get("n_days") or 0),
                need_days=int(st.get("need_days") or EQUITY_MIN_DAYS),
            )
        return BrakeState(
            scalar=brake_scalar(equity, cfg), enabled=True, idle=False,
            reason="ok", n_days=len(equity), need_days=EQUITY_MIN_DAYS,
        )
    except Exception as exc:  # noqa: BLE001 —— fail-open：熔断失效可以，误杀不行
        logger.exception(
            f"[portfolio_brake] brake_state_for_session 异常，恒等放行：{exc}")
        return BrakeState(enabled=True, idle=True,
                          reason=f"exception:{type(exc).__name__}")


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
        except Exception as exc:  # noqa: BLE001 —— rollback 自身失败也别掩盖上面的诊断
            logger.opt(exception=True).warning(f"[silent-swallow] 异常被静默忽略（此处原本为 pass）: {exc}")
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
