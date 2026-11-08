#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""组合权益每日录入（portfolio_equity UPSERT，熔断权益流真源）。

背景（2026-11-07 审计 P0-1）
----------------
`portfolio_equity` 自 migrations/004 建表以来 **0 行**——全代码库只有读取方
（`app/risk/portfolio_brake.load_equity_curve`），**写入方从未实现**，导致组合
回撤熔断（dd>15% 半仓 / dd>25% 清仓）没有任何输入，组合层风控形同虚设。

本脚本 = migrations/004 注释里设想的「每日结算后写一行」的最小实现：
把**真实账户权益**（含初始资金的净值口径）UPSERT 进表。幂等，可反复重跑、
可回填历史。

⚠ 口径纪律
  * ``--equity`` 必须是**账户净值（含初始资金）**，如 40,000 元本金 → 39,120 元。
    不是"当日盈亏"、不是"从 0 起的累计盈亏"——后者会导致全程亏损时峰值 ≤ 0，
    回撤数学上无定义（portfolio_brake.current_drawdown 返回 inf → 熔断不动作）。
  * ``--realized-pnl`` 可选：当日已实现盈亏（元）。equity 缺失的行才会走
    realized_pnl 逐日累加的回退口径，一般不用填。

用法（生产容器内）
----------------
    docker exec -w /app -e PYTHONPATH=/app qhyc-api \
        python scripts/ops/upsert_equity.py --equity 39120.50

    # 回填历史某日
    docker exec -w /app -e PYTHONPATH=/app qhyc-api \
        python scripts/ops/upsert_equity.py --date 2026-11-06 --equity 40350.00

    # 只看状态不写库
    docker exec -w /app -e PYTHONPATH=/app qhyc-api \
        python scripts/ops/upsert_equity.py --show

行为
----
1. UPSERT 一行（trade_date 主键，migrations/004 设计的幂等重跑不会重复放大）；
2. 写入后立即回读权益曲线，打印：样本天数 / 距 20 天门槛进度 / 当前回撤 /
   当前熔断 scalar（1.0 / 0.5 / 0.0）/ 当前开关与阈值；
3. 若最新一行距今超过 --stale-days（默认 3 个自然日），打印新鲜度警告
   （权益流断供会让熔断基于过期回撤动作）。
"""
from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(next(p for p in Path(__file__).resolve().parents if (p / "app").is_dir())))

from sqlalchemy import text  # noqa: E402


def _session():
    from app.core.db import get_session_factory

    return get_session_factory()()


def _upsert(s, d: date, equity: float | None, realized_pnl: float | None,
            note: str | None = None) -> None:
    s.execute(text("""
        INSERT INTO portfolio_equity (trade_date, equity, realized_pnl, note, updated_at)
        VALUES (:d, :eq, :pnl, :note, now())
        ON CONFLICT (trade_date) DO UPDATE
        SET equity = EXCLUDED.equity,
            realized_pnl = EXCLUDED.realized_pnl,
            note = EXCLUDED.note,
            updated_at = now()
    """), {"d": d, "eq": equity, "pnl": realized_pnl, "note": note})
    s.commit()


def _sync_from_account_equity(s, dry_run: bool = False) -> int:
    """把 ``public.account_equity`` 里**用户提供的真实净值**同步进熔断曲线。

    CL-20261107-20：``portfolio_equity`` 长期 0 行，而账户动态权益其实已经
    以 MANUAL 方式录在 ``account_equity``（实测 2026-10-27 创元5 = 30000.00）。
    两张表各有一半链路，中间没人接。这里只做搬运，**绝不推算、绝不填充**：
    - 只取 ``equity IS NOT NULL AND equity > 0`` 的真实行；
    - 同日多账户按 SUM 聚合（组合口径），note 记清账户与来源便于审计；
    - 没有新行就是 0 行，不会为了让曲线好看而造数。

    ⚠ 同步 1 行不等于熔断生效：``portfolio_brake`` 要求 ≥ 20 个交易日样本。
    剩余天数必须由用户逐日提供真实净值（或提供历史净值序列）才能补齐。
    """
    from sqlalchemy import text

    rows = s.execute(text("""
        SELECT trade_date, sum(equity) AS eq,
               string_agg(DISTINCT account, ',') AS accounts,
               string_agg(DISTINCT source, ',') AS sources
        FROM public.account_equity
        WHERE equity IS NOT NULL AND equity > 0
        GROUP BY trade_date ORDER BY trade_date
    """)).all()
    if dry_run:
        for d, eq, accts, srcs in rows:
            print(f"[dry-run] {d} equity={float(eq):,.2f} "
                  f"accounts={accts} sources={srcs}")
        return 0
    for d, eq, accts, srcs in rows:
        _upsert(s, d, round(float(eq), 2), None,
                note=f"自动同步自 account_equity（账户 {accts}，来源 {srcs}）；"
                     f"用户提供的真实净值，非估算")
    return len(rows)


def _show_status(s) -> None:
    from app.risk.portfolio_brake import (
        EQUITY_MIN_DAYS, brake_scalar, equity_curve_status,
        load_brake_config, load_equity_curve,
    )

    n = s.execute(text("SELECT count(*) FROM portfolio_equity")).scalar()
    last = s.execute(text(
        "SELECT trade_date, equity FROM portfolio_equity ORDER BY trade_date DESC LIMIT 1"
    )).first()
    cfg = load_brake_config()
    print(f"[开关] portfolio_brake_enabled={cfg.enabled} "
          f"dd_warn={cfg.dd_warn} dd_stop={cfg.dd_stop} lookback={cfg.lookback}")
    print(f"[表] portfolio_equity {n} 行；最新: "
          f"{f'{last[0]} equity={last[1]}' if last else '（空）'}")

    if not last:
        print(f"[状态] ❌ 表为空 → 熔断无输入（{equity_curve_status()['reason']}）。"
              f"每天录一行，满 {EQUITY_MIN_DAYS} 个交易日后熔断开始生效。")
        return

    stale = (date.today() - last[0]).days
    if stale > 3:
        print(f"[新鲜度] ⚠ 最新一行距今 {stale} 个自然日 > 3，"
              f"熔断正基于过期回撤动作，请尽快补录。")

    eq = load_equity_curve(session=s, lookback=cfg.lookback)
    if not eq:
        st = equity_curve_status()
        print(f"[状态] ❌ 权益曲线不可用：reason={st['reason']} "
              f"样本={st['n_days']}/{st['need_days']} → 熔断不动作（恒等放行）。")
        return
    peak = max(eq[-cfg.lookback:])
    dd = (peak - eq[-1]) / peak if peak > 0 else float("inf")
    sc = brake_scalar(eq, cfg)
    tag = {1.0: "放行（回撤未过线）", 0.5: "⚠ 半仓", 0.0: "🛑 清仓观望"}[sc]
    print(f"[曲线] {len(eq)} 天；当前回撤 {dd:.2%}（峰值 {peak:,.0f}）→ scalar={sc} {tag}")


def main() -> int:
    ap = argparse.ArgumentParser(description="portfolio_equity 每日权益录入（UPSERT）")
    ap.add_argument("--date", type=str, default=None,
                    help="交易日 YYYY-MM-DD，默认今天")
    ap.add_argument("--equity", type=float, default=None,
                    help="账户净值（元，含初始资金口径，必填除非 --show）")
    ap.add_argument("--realized-pnl", type=float, default=None,
                    help="当日已实现盈亏（元，可选）")
    ap.add_argument("--note", type=str, default=None,
                    help="备注（如：手工对账/影子盘），可选")
    ap.add_argument("--show", action="store_true", help="只看状态，不写库")
    ap.add_argument("--from-account-equity", action="store_true",
                    help="从 public.account_equity 同步用户已录入的真实净值（幂等）")
    ap.add_argument("--dry-run", action="store_true",
                    help="配合 --from-account-equity：只打印将同步的行，不写库")
    args = ap.parse_args()

    s = _session()
    try:
        if args.show:
            _show_status(s)
            return 0

        if args.from_account_equity:
            n = _sync_from_account_equity(s, dry_run=args.dry_run)
            print(f"[同步] account_equity → portfolio_equity：{n} 行"
                  f"{'（dry-run，未写库）' if args.dry_run else '（UPSERT 幂等）'}")
            _show_status(s)
            return 0

        if args.equity is None:
            ap.error("--equity 必填（账户净值，元）；只看状态用 --show")
        if args.equity <= 0:
            ap.error(f"--equity 必须为正（净值口径含初始资金），收到 {args.equity}")

        d = date.fromisoformat(args.date) if args.date else date.today()
        if d > date.today():
            ap.error(f"不允许录入未来日期 {d}")
        _upsert(s, d, round(args.equity, 2),
                round(args.realized_pnl, 2) if args.realized_pnl is not None else None,
                note=args.note)
        print(f"[写入] {d} equity={args.equity:,.2f} "
              f"realized_pnl={args.realized_pnl} ✔（UPSERT 幂等）")
        _show_status(s)
        return 0
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
