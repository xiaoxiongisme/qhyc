"""
融合策略回测与研究模块（报告 G1 / PRD §8 融合策略两层回测）

设计要点（满足报告 §14 #1「单一真源」铁律）：
- **信号层**：直接复用 `app.strategies.fusion_signal.walk_fusion_states`——
  与线上实时引擎 `fusion_state_detail` 是**同一套循环**（逐帧 yield），
  因此回测信号与实盘信号逐位一致，不存在口径分叉。
- **walk-forward 天然成立**：`walk_fusion_states` 在每根 bar i 上只用 `≤ i` 的数据，
  无前视；回测逐帧消费即可，无需额外切窗。
- **组合层**：单品种 → 逐笔成交（close-only，按信号翻转 / 止损 / 保本离场） →
  FIFO 顺序汇总成组合净值曲线与指标。

两层：
  ① 信号层 = 生成 per-bar 目标持仓（state 0/1/2）
  ② 组合层 = 把 state 序列重放成成交序列，扣除成本，统计收益/回撤/夏普

参数面板（§8.4）：sl_atr / trail_atr / be_r / W / ma_n / atr_n / ema_k /
entry_mode / cooldown_bars / max_positions / cost_bp / src / seed（多 seed 鲁棒性）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

import numpy as np
import pandas as pd
from sqlalchemy import select, text

from app.contracts.execution import fill_price as _contract_fill_price
from app.core.config import get_settings
from app.core.logging import logger
from app.models import HourlyBar
from app.strategies.fusion_signal import walk_fusion_states

TZ = "Asia/Shanghai"


@dataclass
class FusionBacktestParams:
    """融合策略回测参数（= 实盘参数 + 回测专用参数）。"""
    # —— 与 fusion_state_detail 同口径（单一真源需要逐位对齐）——
    ema_k: int = 140
    atr_n: int = 14
    ma_n: int = 20
    sl_atr: float = 2.0
    trail_atr: float = 2.0
    be_r: float = 0.5
    W: int = 60
    entry_mode: str = "both_nm"
    cooldown_bars: int = 3
    min_bars: int = 160
    max_bars: int = 800
    # —— V3.1~V3.4 门控/加码（与 FusionConfig 同名同义；默认 = V3.4 定稿口径）——
    # ⚠ 必须从 config 全量映射：walk_fusion_states 用 getattr(p, ...) 取值，
    #   参数对象缺字段会回退旧默认（adx_min=0 / use_sbull=True 等），
    #   造成回测信号层与线上引擎口径分叉（破坏单一真源）。
    adx_n: int = 14                  # V3.1 ADX 周期
    adx_min: float = 15.0            # V3.1 ADX 门控（上一根判定；0=关闭）
    use_sbull: bool = False          # V3.1 取消「收强/收弱」要求
    fib_confl: bool = True           # V3.2 斐波汇流（仅回踩支路）
    fib_ratios: tuple = (0.382, 0.5, 0.618)
    fib_tol_atr: float = 0.5         # 斐波容差 = 该值 × ATR
    add_max_lots: int = 2            # ★「总手数上限」= 加仓次数 + 1（历史语义，勿改）
                                     #   1=不加码 / 2=允许加一次 / 3=加两次 …
                                     #   ⚠ 名字易被误读为「加仓次数」，实为总手数：
                                     #     **加仓次数 = add_max_lots − 1**（开仓恒占 1 手）。
                                     #     要表达「加 N 次」用 add_on_times。
    add_on_times: int | None = None   # ★无歧义写法（2026-10-07 新增）= **加仓次数**
                                     #   None（默认）→ 沿用 add_max_lots 的历史语义；
                                     #   设了它则 _eff_cap = 1 + round(该值 × scalar)，
                                     #   即「开仓 1 手 + N 次加仓」，无 off-by-one 歧义。
    add_thr_atr: float = 1.0         # V3.4 加码浮盈门槛（lots × 该值 × ATR）
    add_guard_atr: float = 0.0       # V3.2 结构保本；V3.4 停用（保留兼容）
    # —— 成交口径（P2-3 / CL-20261108-11；E1-C / CL-20261108-15 用户拍板）——
    # "next_open"（默认）= 信号在 bar i 收盘产生 → bar i+1 开盘成交，开仓
    #                      +dirn×1tick / 平仓 -dirn×1tick（不利滑点）；
    #                      成本系数里的 slip_yuan 置 0，滑点已显式计入成交价，不双计。
    # "close"            = 信号同根收盘价成交，**历史基线**，显式传入可逐位复现；
    #                      此模式没有「下一根开盘」语义，故跳空过滤恒不生效。
    # ⚠ 2026-11-08 E1-C 起默认改为 next_open：旧 B0 基线数字一律标 pre-E1C。
    fill_mode: str = "next_open"
    tick_size: float = 0.0           # 无 symbol 时的 1tick 兜底；有 symbol 时取
                                     # cost_coefficients().tick_size（dim_variety）
    # —— E1-C 跳空不利过滤（用户 2026-11-08 19:00 拍板 5 条口径）——
    # gap_atr = dirn × (O[i+1] − C[i]) / ATR[i]，仅 >0（对开仓不利）才判定。
    # > gap_atr 阈值时：该段信号整体作废（blocked_dir 封锁到 tgt 归零或反向），
    # 平仓 / 反手的平仓腿 / 末尾强平不受影响；加仓被拒只取消该次、持仓保留。
    # 0 = 关闭（守卫合成对拍与 close 老基线用）。只在 next_open 生效。
    max_adverse_gap_atr: float = 0.5
    # —— 回测专用 ——
    src: str = "bar_60m"             # P0-3：唯一 K 线真源 l1_mkt.bar_60m；
                                     # 可切 "bar_60m_ba" 取同一条线的后复权维度
    cost_bp: float = 1.3             # 双边成本（基点）；pnl 中扣 2×单边 = 整段 cost_bp
    max_positions: int = 1           # 单品种同时持仓数（1=单仓）
    seed: int = 0                    # 多 seed 鲁棒性：扰动止损用
    seed_jitter: float = 0.0         # sl/trail 相对扰动幅度（0=关）；多 seed 时各 seed 取不同值
    lookback_bars: int = 0           # 0=全历史 walk-forward；>0 则仅用最后 N 根
    max_hold_bars: int = 0           # 持仓上限（根）：>0 时单笔持仓超过该根数强制平仓+再入场；
                                     # 0=关闭（默认关闭，避免改变研究基线；实盘/全历史模拟可开启
                                     # 以杜绝"强趋势下吊灯止损永不触发→持11年不平仓"的部署隐患）


def fusion_params_from_config(overrides: dict | None = None) -> FusionBacktestParams:
    """以 config.fusion 为基准，叠加回测覆盖参数。"""
    cfg = get_settings().fusion
    base = dict(
        ema_k=cfg.ema_k, atr_n=cfg.atr_n, ma_n=cfg.ma_n,
        sl_atr=cfg.sl_atr, trail_atr=cfg.trail_atr, be_r=cfg.be_r,
        W=cfg.W, entry_mode=cfg.entry_mode, cooldown_bars=cfg.cooldown_bars,
        min_bars=cfg.min_bars, max_bars=cfg.max_bars,
        # 2026-10-31：回测改用规范化、全量、正确固定网格的 bar_60m 作为唯一真源。
        # 原 hourly_bar(src=akshare) 历史段被 agg_1m 主键占位导致覆盖空洞，且
        # agg_1m 自身仍为旧整点网格，回测读它必然口径错误。实盘信号(fusion_signal)
        # P0-3（2026-11-07）：实盘已一并切到 l1_mkt.bar_60m（同一 kline_src、
        # 同一 app.data.fusion_kline 取数入口）；本行旧注释作废，两端不再分叉。
        src=getattr(cfg, "kline_src", "bar_60m"),
        # V3.1~V3.4 门控/加码全量映射（与线上引擎同一配置来源，保证信号层逐位一致）
        adx_n=cfg.adx_n, adx_min=cfg.adx_min, use_sbull=cfg.use_sbull,
        fib_confl=cfg.fib_confl, fib_ratios=tuple(cfg.fib_ratios),
        fib_tol_atr=cfg.fib_tol_atr,
        add_max_lots=cfg.add_max_lots, add_thr_atr=cfg.add_thr_atr,
        add_guard_atr=cfg.add_guard_atr,
        # 2026-10-07 接线（CB清单 #5）：add_on_times 此前是死参数——dataclass 里有、
        # 信号层也认，但配置里没有任何入口，线上无法调节「加仓次数」。
        # 传 None 时行为与接线前**完全一致**（沿用 add_max_lots 历史语义），零回归风险。
        add_on_times=cfg.add_on_times,
    )
    p = FusionBacktestParams(**base)
    if overrides:
        for k, v in overrides.items():
            if hasattr(p, k):
                setattr(p, k, v)
    return p


def _read_hourly(session, symbol: str, src: str, start: date | None, end: date | None):
    """单源读取小时线，按时间升序返回 ohlc DataFrame（含 dt）。

    2026-10-31 修正：生产回测原读 hourly_bar(src=akshare)。该表历史段与 agg_1m
    共享 (symbol, trade_datetime) 主键（不含 src），agg_1m 占位导致 akshare 无法写入
    ——覆盖存在空洞；且 agg_1m 自身仍是旧整点网格（11:00/12:00/14:00…）。故回测改用
    规范化、全量、正确固定网格的 l1_mkt.bar_60m 作为唯一真源（契合「单一真源」铁律）。
    """
    # 2026-11-07 P0-3：与实盘走完全相同的取数入口，只由 src（口径维度）区分。
    #   bar_60m     -> l1_mkt.bar_60m          原始维度（唯一 K 线真源）
    #   bar_60m_ba  -> l2_adj.bar_60m_ba       同一条 K 线的后复权维度
    # 二者恒等式 ba.close - l1.close = adj_offset 已在 118,646 行验证零误差。
    if src in ("bar_60m", "bar_60m_ba", "back_adj", "continuous"):
        from app.data.fusion_kline import read_fusion_kline
        return read_fusion_kline(session, symbol, None, src=src,
                                 start=start, end=end, context="backtest")
    logger.warning(
        f"[backtest] src={src!r} 为已废弃直拉源，与实盘不是同一条 K 线（P0-3）")
    q = (
        select(
            HourlyBar.trade_datetime, HourlyBar.open, HourlyBar.high,
            HourlyBar.low, HourlyBar.close,
        )
        .where(HourlyBar.symbol == symbol)
        .where(HourlyBar.src == src)
        .order_by(HourlyBar.trade_datetime.asc())
    )
    if start:
        q = q.where(HourlyBar.trade_datetime >= pd.Timestamp(start))
    if end:
        q = q.where(HourlyBar.trade_datetime <= pd.Timestamp(end))
    rows = session.execute(q).all()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["dt", "open", "high", "low", "close"])
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    return df


def _htf_direction(c: np.ndarray, ema_k: int) -> np.ndarray:
    """EMA140 方向门控（与 evaluate_symbol 一致：用前一根方向对齐，无前视）。"""
    if len(c) < ema_k:
        return np.zeros(len(c), dtype=int)
    ema140 = pd.Series(c).ewm(span=ema_k, adjust=False).mean().to_numpy()
    dir_raw = np.where(c > ema140, 1, -1)
    return np.concatenate([[0], dir_raw[:-1]])


def _atr(df: pd.DataFrame, n: int) -> pd.Series:
    """Wilder RMA 口径 ATR（与引擎 `atr_n` 同口径）。

    用途：给成交打 **R 单位**。融合 `_simulate_trades` 原本只产出价格单位的 pnl，
    无法喂 `app.backtest.robustness`（其 `run_six_checks` 依赖 `R`/`risk`）。
    """
    h = df["high"].astype(float)
    l = df["low"].astype(float)
    c = df["close"].astype(float)
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    if len(tr):
        tr.iloc[0] = float(h.iloc[0] - l.iloc[0])
    return tr.ewm(alpha=1.0 / max(1, int(n)), adjust=False).mean()


def _generate_states(df: pd.DataFrame, p: FusionBacktestParams) -> pd.DataFrame:
    """信号层：消费 walk_fusion_states，输出 per-bar (dt, close, state, atr)。

    注意对齐：生成器从 i=2 开始（前两根为预热），故 state[k] 对应 df 行 k+2。
    """
    o = df["open"].to_numpy(float)
    h = df["high"].to_numpy(float)
    l = df["low"].to_numpy(float)
    c = df["close"].to_numpy(float)
    htf = _htf_direction(c, p.ema_k)
    n = len(c)
    states = np.zeros(n, dtype=int)
    lots_arr = np.zeros(n, dtype=int)
    for k, d in enumerate(walk_fusion_states(o, h, l, c, htf, p)):
        states[k + 2] = d["state"]
        lots_arr[k + 2] = int(d.get("lots", 1) or 1)
    out = df.reset_index(drop=True).copy()
    out["state"] = states
    out["lots"] = lots_arr
    # 波动率基准：用于把成交归一化为 R（robustness 六项检查的硬要求）
    out["atr"] = _atr(out, getattr(p, "atr_n", 14)).to_numpy(dtype=float)
    return out


def _simulate_trades(states_df: pd.DataFrame, p: FusionBacktestParams,
                      symbol: str | None = None) -> list[dict]:
    """组合层：把 state + lots 序列重放成**逐手**成交（close-only，单品种单方向）。

    规则（V3.4 口径，PRD §16/§17；E1-C 起成交口径改为下一根开盘）：
    - bar i 收盘后策略目标持仓 = state_i；默认在 o[i+1] ± 1tick 开/平，
      显式 fill_mode="close" 时仍在 c[i] 开/平（历史基线逐位可复现）；
    - P1 阶梯加码：持仓期间引擎 lots 由 L→L+1（浮盈门槛门控），视为当根**加开 1 手**；
    - 离场/反手时把全部在手平掉（每手独立计 pnl，成本按各自开仓名义扣）；
    - 每手带 `addon` 标记（首仓=False / 加码=True），指标层单独统计 `n_addons`。

    ★ 成交口径 fill_mode（P2-3 / CL-20261108-11；E1-C / CL-20261108-15）：
      · "next_open"（默认）→ bar i 收盘出信号，bar i+1 开盘成交，开仓 +dirn×1tick /
        平仓 -dirn×1tick。信号层仍只用 ≤i 的数据，下一根开盘价是**成交时点**的
        价格，不构成前视。末尾无 i+1 时退回 closes[i] ± 1tick 强平。
      · "close"（历史基线）→ 完全走 close-only 路径，逐位不变；跳空过滤不生效。
      · ⚠ 成本系数已含 `slip_yuan = 2×slip_ticks×tick_size×mult`（开平各 1 跳）。
        next_open 模式把它置 0，滑点改由成交价显式体现，**绝不双计**。
    ★ E1-C 跳空不利过滤（默认 0.5 ATR，只在 next_open 生效）：
      · gap_atr = dirn × (O[i+1] − C[i]) / ATR[i]，>0.5 的整段信号作废，
        记 blocked_dir 封锁到 tgt 归零或反向才解锁（语义 B，非「延后一根」）；
      · 平仓 / 反手的平仓腿 / 末尾强平不判定；加仓被拒只取消该次、持仓保留；
      · 交易建议价 C[i] 只作展示，trade 的 entry_px/exit_px 恒为成交价（三价分离）。

    ⚠ 乘数与成本口径（2026-10-03 修正，用户拍板"真乘数口径"）
      此前 ``mult = getattr(p, "mult", 1.0)`` —— ``FusionBacktestParams`` **没有 mult
      字段**，故恒为 1.0；而 robust��ness 的 ``trade_pnl`` 按 ``R×risk×mult`` 折元，
      等于把「点数」直接当「元」。螺纹乘数 10 → 净利低估 10×、沪金 1000 → 1000×。
      现改为：从 ``dim_variety`` 取**真乘数**，并把每笔的**真实成本**（元）算好后
      以点数形式扣减（``cost_points = cost_yuan / mult``）。
      成本来源为 ``dim_trading_cost``（交易所费率 + 券商 1 分 + 滑点 1 跳/边），
      缺费率即抛错，**不再退回固定 bp 假设**（原 cost_bp=1.3 对螺纹低估约 8 倍）。
    """
    dts = states_df["dt"].to_numpy()
    closes = states_df["close"].to_numpy(float)
    fill_mode = getattr(p, "fill_mode", "next_open") or "next_open"
    if fill_mode not in ("close", "next_open"):
        raise ValueError(f"未知 fill_mode={fill_mode!r}（只支持 close / next_open）")
    # ⚠ next_open 必须有真实 open 列：旧代码缺列时静默 closes.copy()，
    # 会让跳空恒 0、过滤永不触发、成交价退化成 C[i]±1tick（E1-C 口径失效）。
    has_open_col = "open" in states_df.columns
    opens = (states_df["open"].to_numpy(float)
             if has_open_col else closes.copy())
    if fill_mode == "next_open" and not has_open_col:
        raise ValueError("next_open 需要 states_df 含 open 列；缺列会静默退回收盘价，"
                         "拒绝执行（E1-C 口径）")
    states = states_df["state"].to_numpy()
    lots_arr = states_df["lots"].to_numpy()
    n_bars = len(states)
    atr_arr = (states_df["atr"].to_numpy(dtype=float)
               if "atr" in states_df.columns else np.full(len(states), np.nan))
    with np.errstate(all="ignore"):
        _m = np.nanmean(atr_arr) if len(atr_arr) else float("nan")
        atr_mean = float(_m) if np.isfinite(_m) else 0.0
    cost_frac = p.cost_bp / 10000.0  # 旧：固定基点假设（仅当无 symbol 时兜底）
    # —— 真乘数 + 真成本（dict）——
    # ★ 2026-10-07 改为**按建仓日的历史费率**逐笔计价。
    #   原实现 `cost_coefficients(symbol, close_action="CLOSE_YEST")` 每品种只算一次
    #   且**未传 on_date** → 回测整段历史（可覆盖 2015~2026）都用「今天」的费率，
    #   与 fee_per_lot 文档「回测历史务必显式传 on_date」相悖，属静默用当前值。
    #   现按每笔的**建仓日**取费率（开仓费在建仓时收取；跨费率变动的交易按建仓日
    #   口径，简化且可复现）。
    #
    # 性能：先 resolve_rate_date 把建仓日**归约到费率变动区间起点**再查，
    #   于是「同一费率区间」共用一个 memo 键 + cost._FEE_CACHE 键，
    #   整段历史只有「费率变动次数」次真实查库（费率是低频变更数据），
    #   而不是逐笔查库（否则连接池会被当查询接口用）。
    _coef_memo: dict = {}
    if symbol:
        from app.data.cost import (cost_coefficients, cost_points_at,
                                   main_contract_at, resolve_rate_date,
                                   _variety as _vc)

        _vc_code = _vc(symbol)

        def _coef_for(d) -> dict:
            """按建仓日取该品种的开平成本系数。

            memo 键 = (费率区间起点, 当时主力合约码) —— 两个维度都归约：
              · 时间维度：同一费率变动区间共用一个键；
              · 合约维度：同一主力合约共用一个键（换月才变）。
            ★必须传 contract：交易所对特定合约给不同费率（RB 1/5/10 月主力
              1‱、其余 0.2‱），不传则只能命中 ALL 档 → **主力成本低估 5 倍**。
            """
            d0 = d.date() if hasattr(d, "date") else d
            ctr = main_contract_at(_vc_code, d0)
            key = (resolve_rate_date(_vc_code, d0), ctr)
            hit = _coef_memo.get(key)
            if hit is None:
                hit = cost_coefficients(symbol, contract=ctr,
                                        close_action="CLOSE_YEST", on_date=d0)
                _coef_memo[key] = hit
            return hit

        # 预热：取一次以获得乘数（逐笔仍按各自建仓日所在区间取费率）
        _first = _coef_for(dts[0]) if len(dts) else cost_coefficients(
            symbol, close_action="CLOSE_YEST")
        mult = float(_first["multiplier"])
        _tick_size = float(_first.get("tick_size") or 0.0)
    else:
        mult = float(getattr(p, "mult", 1.0) or 1.0)
        _tick_size = float(getattr(p, "tick_size", 0.0) or 0.0)
    # close 模式 tick 恒为 0，保证历史基线逐位一致
    tick = _tick_size if fill_mode == "next_open" else 0.0
    if fill_mode == "next_open" and symbol and tick <= 0:
        raise ValueError(f"{symbol}: next_open 需要有效 tick_size（实得 {tick!r}）；"
                         "为 0 会让 1tick 滑点静默消失（E1-C 口径失效）")
    # E1-C：跳空过滤只在 next_open 生效（close 老基线逐位零回归）
    gap_lim = (float(getattr(p, "max_adverse_gap_atr", 0.0) or 0.0)
               if fill_mode == "next_open" else 0.0)
    trades: list[dict] = []
    open_lots: list[dict] = []   # 在手各手：{entry_px, entry_i, addon, atr}
    pos = 0                      # 当前持仓方向 0/1/2
    blocked_dir = 0              # E1-C 语义 B：被拒后整段作废的方向（0=未封锁）

    def _atr_at(i: int) -> float:
        v = float(atr_arr[i]) if i < len(atr_arr) else float("nan")
        if not np.isfinite(v) or v <= 0:
            v = atr_mean
        return v

    def _gap_blocks(i: int, dirn: float) -> bool:
        """E1-C：对开仓不利的跳空是否 > 阈值（按信号根 ATR[i] 归一化）。

        adverse = dirn × (O[i+1] − C[i])，>0 才是「买贵/卖便宜」；
        ATR 不可用或末尾无 i+1 时一律不过滤，避免静默全拒。
        """
        if gap_lim <= 0:
            return False
        j = i + 1
        if j >= n_bars:
            return False
        a = _atr_at(i)
        if not np.isfinite(a) or a <= 0:
            return False
        adverse = dirn * (float(opens[j]) - float(closes[i]))
        return (adverse / a) > gap_lim

    def _fill_price(i: int, dirn: float, is_open: bool) -> tuple[float, int]:
        """薄封装：委托契约层纯函数 app.contracts.execution.fill_price（E1-B）。

        实现只有一份，守卫可直接对纯函数做行为级逐位对拍；未来 E1-D 实盘影子
        对账可复用同一函数。本闭包只注入本轮的 closes / opens / fill_mode / tick。
        ⚠ close 分支在纯函数里恒返回 (closes[i], i)，与历史基线逐位一致。
        """
        return _contract_fill_price(i, dirn, is_open, closes, opens,
                                    n_bars, fill_mode, tick)

    def _trade(lt: dict, exit_px: float, exit_i: int,
               open_pos: bool = False) -> dict:
        dirn = 1.0 if pos == 1 else -1.0
        ep = float(lt["entry_px"])
        xp = float(exit_px)
        gross = dirn * (xp - ep)
        # 成本：按**建仓日**的历史费率（交易所+券商1分+滑点1跳/边），折成点数扣减
        if symbol:
            _c = _coef_for(dts[lt["entry_i"]])
            if fill_mode == "next_open":
                # 滑点已由开/平两个成交价各 1tick 显式计入，成本系数里必须扣掉
                _c = {**_c, "slip_yuan": 0.0}   # 副本，不污染 _coef_memo
            cost_pts = cost_points_at(_c, ep)
        else:
            cost_pts = cost_frac * ep
        cost_yuan = cost_pts * mult
        pnl = gross - cost_pts
        _a = lt.get("atr")
        risk = float(p.sl_atr * _a) if (_a and np.isfinite(_a) and _a > 0) else 0.0
        r_unit = float(dirn * (xp - ep) / risk) if risk > 0 else 0.0
        t = {
            "symbol": None,
            "side": "LONG" if pos == 1 else "SHORT",
            "entry_dt": dts[lt["entry_i"]], "exit_dt": dts[exit_i],
            "entry_px": round(ep, 4),
            "exit_px": round(xp, 4),
            "pnl": round(float(pnl), 4),
            "pnl_pct": round(float(pnl / ep * 100.0), 4) if ep else 0.0,
            "addon": lt["addon"],
            # —— robustness 六项检查口径（R / risk / 进出场时价）——
            # ⚠ 这几个字段**禁止 round**：robustness.price_sign_symmetry 用
            #   abs_tol=1e-9 校验价格取负对称性，任何舍入都会被误判为不一致。
            "atr": float(_a or 0.0),
            "risk": risk, "R": r_unit,
            "ep": ep, "xp": xp,
            "edt": dts[lt["entry_i"]], "xdt": dts[exit_i],
            "dir": dirn, "mult": mult,
            # 真实成本（元/手，整段开平）：robustness 优先用此字段，
            # 避免退回 bp 假设（真实费率与 bp 假设可差数倍）
            "cost_yuan": cost_yuan,
            "cost_points": cost_pts,
        }
        if open_pos:
            t["open"] = True
        return t

    for i in range(len(states)):
        tgt = int(states[i])
        cur_lots = int(lots_arr[i])
        # 语义 B 解锁：只有该段信号结束（归零或反向）才算「信号再次发生」。
        # pos=0 时 tgt!=pos 每根都成立，若只在被拒那一根跳过，下一根又会开仓
        # —— 那正是实证过滤率 0% 的语义 A。
        if tgt == 0 or (blocked_dir and tgt != blocked_dir):
            blocked_dir = 0
        # P1 加码：持仓中引擎手数增加 → 当根加开对应手数。
        # 被跳空拒绝只取消该次加仓，已有持仓保留、不封锁整段（规约 D5）。
        if pos != 0 and tgt == pos and cur_lots > len(open_lots):
            d_add = 1.0 if pos == 1 else -1.0
            if not _gap_blocks(i, d_add):
                for _ in range(cur_lots - len(open_lots)):
                    px, fi = _fill_price(i, d_add, is_open=True)
                    open_lots.append({"entry_px": px, "entry_i": fi,
                                      "addon": True, "atr": _atr_at(i)})
        if tgt != pos:
            if open_lots:
                # 平仓腿：E1-C 第 5 条，不做跳空判定，一律按下一根开盘 ±1tick 成交
                d_exit = 1.0 if pos == 1 else -1.0
                xp, xi = _fill_price(i, d_exit, is_open=False)
                for lt in open_lots:
                    trades.append(_trade(lt, xp, xi))
                open_lots.clear()
            if tgt != 0:
                d_ent = 1.0 if tgt == 1 else -1.0
                if blocked_dir == tgt:
                    pos = 0                    # 该段仍在封锁期：不重试
                elif _gap_blocks(i, d_ent):
                    blocked_dir = tgt         # 整段信号作废
                    pos = 0
                else:
                    pos = tgt
                    px, fi = _fill_price(i, d_ent, is_open=True)
                    open_lots.append({"entry_px": px, "entry_i": fi,
                                      "addon": False, "atr": _atr_at(i)})
            else:
                pos = 0
    # 末尾仍持仓：close 模式按最后收盘价强平；next_open 无 i+1，同样退回收盘价+滑点
    if pos != 0 and open_lots:
        i = n_bars - 1
        d_exit = 1.0 if pos == 1 else -1.0
        xp, xi = _fill_price(i, d_exit, is_open=False)
        for lt in open_lots:
            trades.append(_trade(lt, xp, xi, open_pos=True))
        open_lots.clear()
    return trades


def to_robustness_trades(trades: list[dict], mult: float = 1.0) -> list[dict]:
    """把融合成交适配为 ``app.backtest.robustness`` 的输入口径。

    ``run_six_checks`` 依赖 ``R/risk/ep/xp/edt/xdt/dir``；融合成交原本只有价格单位
    pnl，直接喂入会缺字段。本函数**只补字段、不改任何既有数值**，并剔除未平仓
    （open=True）与 risk<=0 的成交——把检验作用域严格限定在已平仓样本。
    """
    out: list[dict] = []
    for t in trades:
        if t.get("open"):
            continue
        risk = float(t.get("risk") or 0.0)
        if not np.isfinite(risk) or risk <= 0:
            continue
        d = dict(t)
        d["mult"] = float(t.get("mult") or mult)
        d.setdefault("sym", t.get("symbol"))
        for k in ("edt", "xdt"):
            v = d.get(k)
            if v is not None and hasattr(v, "isoformat"):
                d[k] = str(v)
        out.append(d)
    return out


def _metrics_from_trades(trades: list[dict]) -> dict:
    closed = [t for t in trades if not t.get("open")]
    n = len(closed)
    if n == 0:
        return {"n_trades": 0, "win_rate": None, "total_return_pct": 0.0,
                "avg_pnl_pct": None, "max_drawdown_pct": 0.0, "sharpe": None}
    wins = [t for t in closed if t["pnl"] > 0]
    pcts = np.array([t["pnl_pct"] for t in closed], dtype=float)
    eq = np.cumprod(1.0 + pcts / 100.0)
    peak = np.maximum.accumulate(eq)
    dd = (peak - eq) / peak * 100.0
    mean = float(pcts.mean())
    std = float(pcts.std(ddof=1)) if n > 1 else 0.0
    sharpe = round(mean / std * np.sqrt(50), 3) if std > 0 else None  # ≈ 50 笔/年近似年化
    return {
        "n_trades": n,
        "n_wins": len(wins),
        "n_addons": sum(1 for t in closed if t.get("addon")),  # V3.4 加码手数
        "win_rate": round(len(wins) / n, 4),
        "avg_pnl_pct": round(mean, 4),
        "total_return_pct": round(float((eq[-1] - 1.0) * 100.0), 4),
        "max_drawdown_pct": round(float(dd.max()), 4),
        "sharpe": sharpe,
        "equity_curve": [round(float(x), 4) for x in eq.tolist()],
    }


def _jittered_params(p: FusionBacktestParams, seed: int, jitter: float) -> FusionBacktestParams:
    """多 seed：对 sl/trail 施加确定性扰动，评估参数鲁棒性。"""
    if jitter <= 0:
        return p
    # 用 seed 生成 [-jitter, +jitter] 的确定偏移
    rng = np.random.default_rng(seed)
    f = 1.0 + rng.uniform(-jitter, jitter)
    q = FusionBacktestParams(**{k: getattr(p, k) for k in p.__dataclass_fields__})
    q.sl_atr = round(p.sl_atr * f, 4)
    q.trail_atr = round(p.trail_atr * f, 4)
    q.seed = seed
    return q


def run_fusion_backtest(session, symbols: list[str] | None = None,
                        params: FusionBacktestParams | None = None,
                        start: date | None = None, end: date | None = None,
                        seeds: list[int] | None = None,
                        include_trades: bool = False) -> dict:
    """执行融合策略回测（信号层 walk-forward + 组合层 FIFO 重放）。

    seeds：非空则对每个 seed 跑一遍（用 seed_jitter 扰动），最终指标取均值。
    include_trades：True 时额外返回逐笔成交 `trades`（walk-forward / 六项检查需要）。
    默认 False —— 保持既有 API 返回体不变（避免逐笔数据放大响应体）。
    """
    settings = get_settings()
    if params is None:
        params = fusion_params_from_config()
    if symbols is None:
        symbols = [s.symbol for s in settings.main_contracts]
    all_trades: list[dict] = []
    per_symbol: list[dict] = []
    skipped: list[str] = []

    seed_list = seeds if seeds else [params.seed]
    for sym in symbols:
        sym_trades: list[dict] = []
        sym_metrics_acc: list[dict] = []
        for sd in seed_list:
            pp = _jittered_params(params, sd, params.seed_jitter) if len(seed_list) > 1 else params
            df = _read_hourly(session, sym, pp.src, start, end)
            if df is None or len(df) < pp.min_bars:
                continue
            if pp.lookback_bars and len(df) > pp.lookback_bars:
                df = df.iloc[-pp.lookback_bars:]
            states_df = _generate_states(df, pp)
            tr = _simulate_trades(states_df, pp, symbol=sym)
            for t in tr:
                t["symbol"] = sym
            sym_trades.extend(tr)
            sym_metrics_acc.append(_metrics_from_trades(tr))
        if not sym_trades:
            skipped.append(sym)
            continue
        m = _metrics_from_trades(sym_trades)
        m["symbol"] = sym
        per_symbol.append(m)
        all_trades.extend(sym_trades)

    agg = _metrics_from_trades(all_trades)
    agg["per_symbol"] = per_symbol
    agg["skipped"] = skipped
    if include_trades:
        agg["trades"] = all_trades
    agg["params"] = {k: getattr(params, k) for k in params.__dataclass_fields__}
    agg["seeds"] = seed_list
    return agg


def run_fusion_matrix(session, symbols: list[str] | None = None,
                     start: date | None = None, end: date | None = None,
                     grid: dict | None = None) -> dict:
    """四维测试矩阵（§8.3 简化版）：在 (src × sl_atr × trail_atr) 网格上跑回测，汇总净收益/胜率。

    grid 形如 {"src": [...], "sl_atr": [...], "trail_atr": [...]}
    """
    settings = get_settings()
    if symbols is None:
        symbols = [s.symbol for s in settings.main_contracts[:10]]  # 矩阵默认抽样 10 品种控时
    grid = grid or {
        "src": ["akshare"],
        "sl_atr": [1.5, 2.0, 2.5],
        "trail_atr": [1.5, 2.0, 2.5],
    }
    base = fusion_params_from_config()
    rows = []
    for src in grid["src"]:
        for sl in grid["sl_atr"]:
            for tr in grid["trail_atr"]:
                p = FusionBacktestParams(**{
                    k: getattr(base, k) for k in base.__dataclass_fields__
                })
                p.src = src
                p.sl_atr = sl
                p.trail_atr = tr
                res = run_fusion_backtest(session, symbols, p, start, end)
                rows.append({
                    "src": src, "sl_atr": sl, "trail_atr": tr,
                    "n_trades": res.get("n_trades", 0),
                    "win_rate": res.get("win_rate"),
                    "total_return_pct": res.get("total_return_pct"),
                    "max_drawdown_pct": res.get("max_drawdown_pct"),
                    "sharpe": res.get("sharpe"),
                })
    return {"grid": grid, "symbols": symbols, "rows": rows}


if __name__ == "__main__":
    from app.core.db import session_scope

    with session_scope() as s:
        r = run_fusion_backtest(s, symbols=["FG888", "RB888", "CU888"],
                                start=date(2025, 1, 1), end=date(2026, 9, 1))
        print("n_trades=", r["n_trades"], "win_rate=", r["win_rate"],
              "total_return_pct=", r["total_return_pct"], "max_dd=", r["max_drawdown_pct"])
