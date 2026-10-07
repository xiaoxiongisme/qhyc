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
from sqlalchemy import select

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
    max_bars: int = 400
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
    add_max_lots: int = 2            # V3.2 P1 加码最大手数（1=不加码）
    add_thr_atr: float = 1.0         # V3.4 加码浮盈门槛（lots × 该值 × ATR）
    add_guard_atr: float = 0.0       # V3.2 结构保本；V3.4 停用（保留兼容）
    # —— 回测专用 ——
    src: str = "akshare"             # 单一源（杜绝双源混读，§14 #2）
    cost_bp: float = 1.3             # 双边成本（基点）；pnl 中扣 2×单边 = 整段 cost_bp
    max_positions: int = 1           # 单品种同时持仓数（1=单仓）
    seed: int = 0                    # 多 seed 鲁棒性：扰动止损用
    seed_jitter: float = 0.0         # sl/trail 相对扰动幅度（0=关）；多 seed 时各 seed 取不同值
    lookback_bars: int = 0           # 0=全历史 walk-forward；>0 则仅用最后 N 根


def fusion_params_from_config(overrides: dict | None = None) -> FusionBacktestParams:
    """以 config.fusion 为基准，叠加回测覆盖参数。"""
    cfg = get_settings().fusion
    base = dict(
        ema_k=cfg.ema_k, atr_n=cfg.atr_n, ma_n=cfg.ma_n,
        sl_atr=cfg.sl_atr, trail_atr=cfg.trail_atr, be_r=cfg.be_r,
        W=cfg.W, entry_mode=cfg.entry_mode, cooldown_bars=cfg.cooldown_bars,
        min_bars=cfg.min_bars, max_bars=cfg.max_bars,
        src=cfg.hourly_src,
        # V3.1~V3.4 门控/加码全量映射（与线上引擎同一配置来源，保证信号层逐位一致）
        adx_n=cfg.adx_n, adx_min=cfg.adx_min, use_sbull=cfg.use_sbull,
        fib_confl=cfg.fib_confl, fib_ratios=tuple(cfg.fib_ratios),
        fib_tol_atr=cfg.fib_tol_atr,
        add_max_lots=cfg.add_max_lots, add_thr_atr=cfg.add_thr_atr,
        add_guard_atr=cfg.add_guard_atr,
    )
    p = FusionBacktestParams(**base)
    if overrides:
        for k, v in overrides.items():
            if hasattr(p, k):
                setattr(p, k, v)
    return p


def _read_hourly(session, symbol: str, src: str, start: date | None, end: date | None):
    """单源读取小时线，按时间升序返回 ohlc DataFrame（含 dt）。"""
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

    规则（V3.4 口径，PRD §16/§17）：
    - bar i 收盘后策略目标持仓 = state_i，在 c[i] 开/平；
    - P1 阶梯加码：持仓期间引擎 lots 由 L→L+1（浮盈门槛门控），视为当根收盘**加开 1 手**；
    - 离场/反手时把全部在手按当根收盘价平掉（每手独立计 pnl，成本按各自开仓名义扣）；
    - 每手带 `addon` 标记（首仓=False / 加码=True），指标层单独统计 `n_addons`。

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
    states = states_df["state"].to_numpy()
    lots_arr = states_df["lots"].to_numpy()
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
    else:
        mult = float(getattr(p, "mult", 1.0) or 1.0)
    trades: list[dict] = []
    open_lots: list[dict] = []   # 在手各手：{entry_px, entry_i, addon, atr}
    pos = 0                      # 当前持仓方向 0/1/2

    def _atr_at(i: int) -> float:
        v = float(atr_arr[i]) if i < len(atr_arr) else float("nan")
        if not np.isfinite(v) or v <= 0:
            v = atr_mean
        return v

    def _trade(lt: dict, i: int, open_pos: bool = False) -> dict:
        dirn = 1.0 if pos == 1 else -1.0
        ep = float(lt["entry_px"])
        xp = float(closes[i])
        gross = dirn * (xp - ep)
        # 成本：按**建仓日**的历史费率（交易所+券商1分+滑点1跳/边），折成点数扣减
        if symbol:
            _c = _coef_for(dts[lt["entry_i"]])
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
            "entry_dt": dts[lt["entry_i"]], "exit_dt": dts[i],
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
            "edt": dts[lt["entry_i"]], "xdt": dts[i],
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
        # P1 加码：持仓中引擎手数增加 → 当根收盘加开对应手数
        if pos != 0 and tgt == pos and cur_lots > len(open_lots):
            for _ in range(cur_lots - len(open_lots)):
                open_lots.append({"entry_px": float(closes[i]), "entry_i": i,
                                  "addon": True, "atr": _atr_at(i)})
        if tgt != pos:
            if open_lots:
                for lt in open_lots:
                    trades.append(_trade(lt, i))
                open_lots.clear()
            if tgt != 0:
                pos = tgt
                open_lots.append({"entry_px": float(closes[i]), "entry_i": i,
                                  "addon": False, "atr": _atr_at(i)})
            else:
                pos = 0
    # 末尾仍持仓：以最后收盘价强平（标记未实现，不计入已平仓统计）
    if pos != 0 and open_lots:
        i = len(states) - 1
        for lt in open_lots:
            trades.append(_trade(lt, i, open_pos=True))
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
