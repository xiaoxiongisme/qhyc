#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组合回撤熔断演练（G2 验收：16% 回撤必须被拦住）。

背景（2026-11-07 审计 P0-1）
----------------
``portfolio_equity`` 无写入方 → 熔断无输入。修复 = upsert_equity.py（权益流）
+ 本脚本（证明熔断**在有输入时真的会动作**）。

设计纪律
--------
* **零残留**：所有写入都在一个事务里，结束必 ROLLBACK（含异常路径）。
* **零推送**：本脚本直接调用 ``scheduler._apply_portfolio_brake``，不触发
  fusion_scan 主链；同时 monkeypatch 掉 ``_alert_portfolio_brake_idle``，
  避免"曲线不可用"分支把演练告警推到微信（节流键被演练污染）。
  monkeypatch 只在本进程内生效，不影响正在运行的调度器进程。
* **开关走 monkeypatch 而非事务内 UPDATE**：``load_brake_config`` 内部用
  ``get_engine().connect()`` **独立新连接**读库，READ COMMITTED 下看不到
  本事务未提交的 UPDATE。故"开启熔断"用进程内 ``BrakeConfig(enabled=True)``
  替换；而**数据注入**走传入 session（``load_equity_curve`` 用 session 参数），
  事务内可见 → 权益曲线读取、回撤计算、手数缩放全部走生产真实代码。

场景（对应 app/risk/portfolio_brake.py 的全部分支）
------------------------------------------------
  S0 负例·开关关     enabled=False → 深回撤也不拦（当前生产行为基线）
  S1 正例·16% 回撤   dd=16% ∈ (15%, 25%] → scalar=0.5，lots=4→2
  S2 正例·26% 回撤   dd=26% > 25% → scalar=0.0，lots=0 + state=0 + brake_liquidated
  S3 负例·样本不足   10 天 < min_days=20 → 曲线不可用 → 恒等放行
  S4 负例·口径异常   realized_pnl 全负（从 0 累加全程亏损）→ peak≤0 → dd=inf →
                     放行但**不误杀**（口径不可信时不动作）
  收尾               ROLLBACK → 验证 portfolio_equity 0 行、开关未被改动

用法（生产容器内）
----------------
    docker exec -w /app -e PYTHONPATH=/app qhyc-api \
        python scripts/ops/drill_portfolio_brake.py
"""
from __future__ import annotations

import math
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents if (p / "app").is_dir())))

from sqlalchemy import text  # noqa: E402

#: 演练用合成曲线参数（净值口径，含"初始资金"）
_START = 100_000.0   # 起点 10 万
_PEAK = 120_000.0    # 峰值 12 万
_DD_WARN_SCALAR = 100_800.0   # 12万×(1-16%) → 半仓
_DD_STOP_SCALAR = 88_800.0    # 12万×(1-26%) → 清仓

_RESULTS_PASS: list[str] = []


def _check(name: str, ok: bool, detail: str) -> None:
    tag = "PASS" if ok else "FAIL"
    _RESULTS_PASS.append(ok)
    print(f"  [{tag}] {name} — {detail}")


def _trade_dates(n: int, end: date) -> list[date]:
    """从 end 往回取 n 个周一~周五（近似交易日，load_equity_curve 只按日期排序）。"""
    days, d = [], end
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return sorted(days)


def _inject_equity(s, dates: list[date], final_equity: float) -> None:
    """写入合成净值曲线：前段线性涨到峰值，末行 = final_equity。"""
    s.execute(text("DELETE FROM portfolio_equity"))
    k = len(dates)
    for i, d in enumerate(dates):
        eq = _START + (_PEAK - _START) * i / (k - 2) if i < k - 1 else final_equity
        s.execute(text(
            "INSERT INTO portfolio_equity (trade_date, equity, realized_pnl, note, updated_at)"
            " VALUES (:d, :eq, NULL, 'DRILL', now())"),
            {"d": d, "eq": round(eq, 2)})
    s.flush()


def _inject_realized_pnl_loss(s, dates: list[date]) -> None:
    """S4：equity 全空、realized_pnl 逐日为负 → 累加曲线全程 < 0 → peak ≤ 0。"""
    s.execute(text("DELETE FROM portfolio_equity"))
    for i, d in enumerate(dates):
        s.execute(text(
            "INSERT INTO portfolio_equity (trade_date, equity, realized_pnl, note, updated_at)"
            " VALUES (:d, NULL, :pnl, 'DRILL', now())"),
            {"d": d, "pnl": -1000.0 - i})
    s.flush()


def _fake_results() -> list[dict]:
    return [{"symbol": "RB888", "state": 2, "lots": 4},
            {"symbol": "SC888", "state": 1, "lots": 3}]


def _patch_brake(enabled: bool):
    """进程内替换 load_brake_config：规避独立连接读不到本事务的问题。"""
    import app.risk.portfolio_brake as pb
    from app.risk.portfolio_brake import BrakeConfig

    real = pb.load_brake_config
    pb.load_brake_config = lambda **kw: BrakeConfig(
        enabled=enabled, dd_warn=0.15, dd_stop=0.25, lookback=250)

    import app.scheduler as sched
    sched._alert_portfolio_brake_idle = lambda *a, **k: print(
        "    (演练：空转告警已拦截，不推送)")
    return real, pb


def _restore(real, pb) -> None:
    pb.load_brake_config = real


def main() -> int:
    from app.core.db import get_session_factory

    dates = _trade_dates(30, date.today())
    s = get_session_factory()()
    # 演练事务：与 session 默认事务合一，最后统一 ROLLBACK
    try:
        from app.risk.portfolio_brake import (
            EQUITY_MIN_DAYS, equity_curve_status,
        )
        from app.scheduler import _apply_portfolio_brake

        print("== 熔断演练开始（事务内注入，结束 ROLLBACK，零残留零推送）==")
        print(f"[生产现状] 开关读取（真实路径，只读）：")
        from app.risk.portfolio_brake import load_brake_config
        prod_cfg = load_brake_config()
        print(f"  enabled={prod_cfg.enabled} dd_warn={prod_cfg.dd_warn} "
              f"dd_stop={prod_cfg.dd_stop} lookback={prod_cfg.lookback}")

        # --- S0 负例：开关关 ---
        print("\n[S0] 开关关（enabled=False）：深回撤也不拦（当前生产基线）")
        _inject_equity(s, dates, _DD_STOP_SCALAR)
        real, pb = _patch_brake(enabled=False)
        out = _apply_portfolio_brake(s, _fake_results())
        _restore(real, pb)
        _check("S0 lots 不变", all(r["lots"] in (4, 3) for r in out),
               f"out={[(r['symbol'], r['lots']) for r in out]}")

        # --- S1 正例：16% 回撤 → 半仓 ---
        print("\n[S1] 回撤 16%（> dd_warn 15%，≤ dd_stop 25%）→ scalar=0.5 半仓")
        _inject_equity(s, dates, _DD_WARN_SCALAR)
        real, pb = _patch_brake(enabled=True)
        out = _apply_portfolio_brake(s, _fake_results())
        _restore(real, pb)
        _check("S1 RB lots 4→2", out[0]["lots"] == 2, f"RB lots={out[0]['lots']}")
        _check("S1 SC lots 3→2", out[1]["lots"] == 2,
               f"SC 3×0.5=1.5 → ceil→2 lots={out[1]['lots']}")
        _check("S1 state 未动（未清仓）", out[0]["state"] == 2,
               f"state={out[0]['state']}")

        # --- S2 正例：26% 回撤 → 清仓 ---
        print("\n[S2] 回撤 26%（> dd_stop 25%）→ scalar=0.0 清仓观望")
        _inject_equity(s, dates, _DD_STOP_SCALAR)
        real, pb = _patch_brake(enabled=True)
        out = _apply_portfolio_brake(s, _fake_results())
        _restore(real, pb)
        _check("S2 lots=0", all(r["lots"] == 0 for r in out),
               f"lots={[r['lots'] for r in out]}")
        _check("S2 state=0（引擎将产生真实平仓）",
               all(r["state"] == 0 for r in out), f"state={[r['state'] for r in out]}")
        _check("S2 brake_liquidated 标记",
               all(r.get("brake_liquidated") for r in out), "")

        # --- S3 负例：样本不足 ---
        print(f"\n[S3] 样本 10 天 < min_days={EQUITY_MIN_DAYS} → 不可用 → 放行（防误杀）")
        _inject_equity(s, dates[:10], _DD_STOP_SCALAR)
        real, pb = _patch_brake(enabled=True)
        out = _apply_portfolio_brake(s, _fake_results())
        _restore(real, pb)
        st = equity_curve_status()
        _check("S3 lots 不变", all(r["lots"] in (4, 3) for r in out),
               f"reason={st['reason']} n={st['n_days']}/{st['need_days']}")

        # --- S4 负例：口径异常（realized_pnl 累加全程亏损） ---
        print("\n[S4] equity 缺失、realized_pnl 全负 → 累加曲线 peak≤0 → dd=inf → 放行不误杀")
        _inject_realized_pnl_loss(s, dates)
        real, pb = _patch_brake(enabled=True)
        out = _apply_portfolio_brake(s, _fake_results())
        _restore(real, pb)
        _check("S4 lots 不变（口径不可信时不动作）",
               all(r["lots"] in (4, 3) for r in out), "")

        # --- 收尾：ROLLBACK + 残留验证 ---
        s.rollback()
        n = s.execute(text("SELECT count(*) FROM portfolio_equity")).scalar()
        cfg_after = load_brake_config()
        print("\n== 残留验证（真实连接重读）==")
        _check("portfolio_equity 回到 0 行", n == 0, f"n={n}")
        _check("开关未被改动", cfg_after.enabled == prod_cfg.enabled,
               f"enabled={cfg_after.enabled}")

        ok = all(_RESULTS_PASS)
        print(f"\n== 演练结果: {'✅ ALL PASS' if ok else '❌ FAILED'} "
              f"({sum(_RESULTS_PASS)}/{len(_RESULTS_PASS)}) ==")
        return 0 if ok else 1
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
