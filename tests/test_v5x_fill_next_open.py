# -*- coding: utf-8 -*-
"""C2b：V5X 成交层 next_open 口径（CL-20261109-05）。

三条必须钉死的性质
------------------
1. **零回归**：``fill_mode="close"`` 的重定价结果必须与 ``SymbolEngine.trades``
   逐笔逐字段一致（容忍 1e-9）。没有这条，历史归档数字就再也复现不出来。
2. **口径确实变了**：``next_open`` 的成交价必须是 ``open[i+1] ± 1tick``，
   而不是信号根收盘价——否则「接了 E1-C」只是文档里的说法。
3. **不双重计滑点**：1tick 已经进了成交价，成本里的 ``slip_yuan`` 必须置 0。
   fusion 回测在这上面踩过坑，这里用「成本恰好等于开平两笔手续费」来钉住。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.backtest.v5x_fill import gap_rejected, reprice_orders
from app.strategies.v5x.engine import CostCfg, DailyCtx, SymbolEngine


def _cost(fee=(0.0, 0.0), slip_yuan=0.0, mult=10.0, tick=1.0) -> CostCfg:
    return CostCfg(fee_row={"OPEN": fee, "CLOSE_TODAY": fee, "CLOSE_YEST": fee},
                   slip_yuan=slip_yuan, mult=mult, tick=tick)


def _engine_run(bars: pd.DataFrame, cost: CostCfg, sym="RB888"):
    """跑引擎并返回 (trades, orders)，orders 回填 bar_i（与回测器同一做法）。"""
    eng = SymbolEngine(sym, _cfg(), cost)
    eng.set_daily_ctx(DailyCtx(td=None))
    orders: list[dict] = []
    atr: list[float] = []
    for k, row in enumerate(bars.itertuples(index=False)):
        evs = eng.on_bar(row.bucket, float(row.open), float(row.high),
                         float(row.low), float(row.close), float(row.volume),
                         float(row.raw_c), allow=True)
        for ev in evs:
            ev["bar_i"] = k
        orders.extend(evs)
        atr.append(float(eng.atr.v) if eng.atr.v is not None else float("nan"))
    return eng.trades, orders, atr


def _cfg():
    from app.strategies.v5x.engine import V5XConfig
    # 关掉需要跨品种/日频数据的门，只留纯价格逻辑，便于合成数据触发
    return V5XConfig(M5_ON=False, M1A_ON=False)


def _bars(closes, opens=None, span=6.0, mult_spread=0.5) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    opens = closes - 0.15 if opens is None else np.asarray(opens, dtype=float)
    n = len(closes)
    return pd.DataFrame({
        "bucket": pd.date_range("2026-01-05 09:00", periods=n, freq="h"),
        "open": opens,
        "high": closes + mult_spread,
        "low": closes - mult_spread,
        "close": closes,
        "volume": 1000.0,
        "raw_c": closes,
    })


def _run_reprice(bars, orders, atr, *, cost, fill_mode, tick=1.0, gap=0.5, tds=None):
    n = len(bars)
    return reprice_orders(
        orders,
        opens=bars["open"].to_numpy(dtype=float),
        closes=bars["close"].to_numpy(dtype=float),
        raw_closes=bars["raw_c"].to_numpy(dtype=float),
        tds=tds if tds is not None else [0] * n,
        tss=bars["bucket"].tolist(),
        atr=atr,
        cost=cost,
        fill_mode=fill_mode,
        tick=tick,
        gap_lim=gap,
        sl_atr=1.75,
    )


# ------------------------------------------------------------------ 1. 零回归
def test_close_mode_reproduces_engine_trades_exactly():
    """close 口径必须逐笔等于引擎自己的 trades（历史锚点不可漂移）。"""
    closes = list(np.linspace(100, 130, 60)) + list(np.linspace(130, 104, 60))
    bars = _bars(closes)
    cost = _cost(fee=(3.0, 0.0001), slip_yuan=1.0)
    trades, orders, atr = _engine_run(bars, cost)
    assert trades, "合成数据应至少触发一次开平"

    repriced, stats = _run_reprice(bars, orders, atr, cost=cost, fill_mode="close")

    assert len(repriced) == len(trades)
    for a, b in zip(repriced, trades):
        assert a["dir"] == b["dir"]
        assert a["lots"] == pytest.approx(b["lots"])
        assert a["P"] == pytest.approx(b["P"], abs=1e-9)
        assert a["X"] == pytest.approx(b["X"], abs=1e-9)
        assert a["gross"] == pytest.approx(b["gross"], abs=1e-6)
        assert a["net"] == pytest.approx(b["net"], abs=1e-6)
        assert a["r"] == pytest.approx(b["r"], abs=1e-9)
        assert a["xdt"] == b["xdt"]
        assert a["reason"] == b["reason"]
    assert stats["n_open_gap_rejected"] == 0, "close 模式下跳空门必须失效"


# ------------------------------------------------------------ 2. 口径确实变了
def test_next_open_fills_at_next_bar_open_plus_one_tick():
    closes = list(np.linspace(100, 130, 60)) + list(np.linspace(130, 104, 60))
    bars = _bars(closes)
    cost = _cost(mult=10.0, tick=2.0)
    trades, orders, atr = _engine_run(bars, cost)
    repriced, _ = _run_reprice(bars, orders, atr, cost=cost, fill_mode="next_open",
                               tick=2.0, gap=0.0)
    assert repriced

    opens = bars["open"].to_numpy(dtype=float)
    for tr in repriced:
        i, j = tr["signal_i"], tr["fill_i"]
        assert j == i + 1, "next_open 必须成交在信号根的下一根"
        want = opens[j] + tr["dir"] * 2.0
        assert tr["P0"] == pytest.approx(want, abs=1e-9), "首仓价 = 下一根开盘 + dir*tick"
        # 平仓同理，反向加滑点
        k = tr["exit_i"]
        assert k == tr["fill_i"] + 1 or k > tr["signal_i"]


def test_next_open_exit_price_is_minus_tick():
    """平仓价必须是 open[exit信号根+1] - dir*tick（不是信号根收盘）。"""
    closes = list(np.linspace(100, 130, 60)) + list(np.linspace(130, 104, 60))
    bars = _bars(closes)
    cost = _cost(mult=10.0, tick=1.0)
    trades, orders, atr = _engine_run(bars, cost)
    closes_list = [o for o in orders if o["type"] == "CLOSE"]
    assert closes_list, "应至少有一次平仓事件"
    repriced, _ = _run_reprice(bars, orders, atr, cost=cost, fill_mode="next_open",
                               tick=1.0, gap=0.0)
    opens = bars["open"].to_numpy(dtype=float)
    for tr, od in zip(repriced, closes_list):
        want = opens[od["bar_i"] + 1] - od["dir"] * 1.0
        assert tr["X"] == pytest.approx(want, abs=1e-9)


# ----------------------------------------------------------------- 3. 跳空门
def test_gap_rejected_flags_adverse_gap_only():
    atr = np.array([10.0, 10.0])
    opens = np.array([100.0, 107.0])     # 相对 close[0]=100 高开 7 → 0.7 ATR
    closes = np.array([100.0, 107.0])
    # 多单追高：adverse = +7 → 0.7 > 0.5 → 拒
    assert gap_rejected(1.0, 0, opens, closes, atr, 0.5) is True
    # 空单在同一根：adverse = -7 → 不拒
    assert gap_rejected(-1.0, 0, opens, closes, atr, 0.5) is False
    # 阈值关闭
    assert gap_rejected(1.0, 0, opens, closes, atr, 0.0) is False


def test_next_open_rejects_chasing_entry_and_drops_episode():
    """开仓被跳空门拒绝 → 整段 episode 作废，不产生任何盈亏。"""
    n = 20
    closes = np.array([100.0] * n)
    opens = np.array([100.0] * n)
    opens[11] = 120.0        # 信号根 10 → 下一根 11 跳空 +20
    bars = pd.DataFrame({
        "bucket": pd.date_range("2026-01-05 09:00", periods=n, freq="h"),
        "open": opens, "high": closes + 0.5, "low": closes - 0.5,
        "close": closes, "volume": 1000.0, "raw_c": closes,
    })
    orders = [dict(type="OPEN", dir=1.0, price=100.0, lots=1.0, bar_i=10),
              dict(type="CLOSE", dir=1.0, price=100.0, lots=1.0, bar_i=13,
                   reason="stop")]
    atr = [1.0] * n          # ATR=1，跳空 20 倍 → 必拒
    trades, stats = _run_reprice(bars, orders, atr, cost=_cost(), fill_mode="next_open")
    assert trades == []
    assert stats["n_open_gap_rejected"] == 1
    assert stats["n_dropped_episode"] == 1


# --------------------------------------------------------- 4. 滑点不双计
def test_next_open_does_not_double_count_slippage():
    """成本 = 开仓费 + 平仓费，滑点只在成交价里出现一次。"""
    closes = np.array([100.0, 110.0, 120.0])
    bars = pd.DataFrame({
        "bucket": pd.date_range("2026-01-05 09:00", periods=3, freq="h"),
        "open": closes, "high": closes + 1, "low": closes - 1,
        "close": closes, "volume": 1000.0, "raw_c": closes,
    })
    fee = (2.0, 0.0)                       # 每边固定 2 元/手
    cost = _cost(fee=fee, slip_yuan=99.0, mult=10.0, tick=1.0)   # 故意给个大滑点
    orders = [dict(type="OPEN", dir=1.0, price=100.0, lots=1.0, bar_i=0),
              dict(type="CLOSE", dir=1.0, price=110.0, lots=1.0, bar_i=1,
                   reason="stop")]
    trades, _ = _run_reprice(bars, orders, [1.0, 1.0, 1.0], cost=cost,
                             fill_mode="next_open", tick=1.0, gap=0.0, tds=[0, 0, 0])
    t = trades[0]
    # 开仓 101（=open[1]+1tick）、平仓 119（=open[2]-1tick）→ 毛 18 点 × 10
    assert t["P"] == pytest.approx(101.0)
    assert t["X"] == pytest.approx(119.0)
    assert t["gross"] == pytest.approx(180.0)
    assert t["gross"] - t["net"] == pytest.approx(4.0), "成本只能是两笔手续费，不能含 99 滑点"


def test_close_mode_keeps_slippage_in_cost():
    """反过来：close 口径下滑点仍走成本（老基线逐位不变）。"""
    closes = np.array([100.0, 110.0, 120.0])
    bars = pd.DataFrame({
        "bucket": pd.date_range("2026-01-05 09:00", periods=3, freq="h"),
        "open": closes, "high": closes + 1, "low": closes - 1,
        "close": closes, "volume": 1000.0, "raw_c": closes,
    })
    cost = _cost(fee=(2.0, 0.0), slip_yuan=1.0, mult=10.0, tick=1.0)
    orders = [dict(type="OPEN", dir=1.0, price=100.0, lots=1.0, bar_i=0),
              dict(type="CLOSE", dir=1.0, price=100.0, lots=1.0, bar_i=1,
                   reason="stop")]
    trades, _ = _run_reprice(bars, orders, [1.0, 1.0, 1.0], cost=cost,
                             fill_mode="close", tick=1.0, gap=0.5, tds=[0, 0, 0])
    t = trades[0]
    assert t["P"] == pytest.approx(100.0) and t["X"] == pytest.approx(110.0)
    # 毛 100，成本 = 2 + 2 + 2*1*10 = 24
    assert t["gross"] - t["net"] == pytest.approx(24.0)


# ------------------------------------------------------- 5. 平今/平昨按成交根
def test_same_day_fee_uses_fill_bar_trading_day():
    """next_open 跨日成交时，平今/平昨必须按**成交根**的交易日判定。"""
    closes = np.array([100.0, 100.0, 100.0])
    bars = pd.DataFrame({
        "bucket": pd.date_range("2026-01-05 09:00", periods=3, freq="h"),
        "open": closes, "high": closes + 1, "low": closes - 1,
        "close": closes, "volume": 1000.0, "raw_c": closes,
    })
    # 开仓费率 0，平今 5 元，平昨 9 元 → 用差额反推用的是哪一档
    cost = CostCfg(fee_row={"OPEN": (0.0, 0.0), "CLOSE_TODAY": (5.0, 0.0),
                            "CLOSE_YEST": (9.0, 0.0)},
                   slip_yuan=0.0, mult=10.0, tick=1.0)
    orders = [dict(type="OPEN", dir=1.0, price=100.0, lots=1.0, bar_i=0),
              dict(type="CLOSE", dir=1.0, price=100.0, lots=1.0, bar_i=1,
                   reason="stop")]
    # tds: 根0/1 属第 1 个交易日（开仓成交在根1），根2 属第 2 个交易日（平仓成交在根2）
    trades, _ = _run_reprice(bars, orders, [1.0] * 3, cost=cost,
                             fill_mode="next_open", tick=0.0, gap=0.0,
                             tds=[1, 1, 2])
    t = trades[0]
    assert t["gross"] == pytest.approx(0.0)
    assert -t["net"] == pytest.approx(9.0), "跨日成交应按平昨 9 元计费"


# ------------------------------------------------------------ 6. 末尾兜底
def test_next_open_last_bar_falls_back_to_close():
    closes = np.array([100.0, 101.0])
    bars = pd.DataFrame({
        "bucket": pd.date_range("2026-01-05 09:00", periods=2, freq="h"),
        "open": closes, "high": closes + 1, "low": closes - 1,
        "close": closes, "volume": 1000.0, "raw_c": closes,
    })
    orders = [dict(type="OPEN", dir=1.0, price=101.0, lots=1.0, bar_i=1)]
    _, stats = _run_reprice(bars, orders, [1.0, 1.0], cost=_cost(),
                            fill_mode="next_open", tick=1.0, gap=0.0)
    assert stats["n_tail_fallback"] == 1
