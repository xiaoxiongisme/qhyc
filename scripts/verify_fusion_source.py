#!/usr/bin/env python
"""融合信号「数据源切换」对账工具（只读，不写库）。

用途
----
把同一批品种用**不同的小时线来源**各跑一次生产 ``evaluate_all``，逐项比较
state / 根数 / 最新K / 入场价 / ATR / 止损位，回答两个问题：

  1. 如果现在把 ``fusion.hourly_src`` 从 akshare 切到 bar_60m，**哪些品种的仓位会变**？
  2. 差异有多大（ATR、止损位、入场时刻偏移）？

背景（docs/V2样本外崩塌排查.md §35）
----------------------------------
回测自 2026-10-31 起读 ``l1_mkt.bar_60m``（``fusion_params_from_config`` 硬编码），
实盘仍读 ``l0_raw.hourly_bar(src=akshare)``。实测同时间戳、同 800 根下，
50/50 品种的 state 序列存在差异（分散 5%~31%），当前时点约 3/50 品种仓位不同。
故切换前必须先跑本工具对账。

用法
----
    python scripts/verify_fusion_source.py                     # 默认比 akshare vs bar_60m
    python scripts/verify_fusion_source.py --sources akshare,bar_60m --out /tmp/v.json
    python scripts/verify_fusion_source.py --symbols RB888,AG888

退出码：0 = 两源 state 全一致；1 = 存在差异（切换需人工确认）；2 = 执行错误。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.strategies.fusion_signal import _POS, evaluate_all


def _fmt(v, nd=4):
    if v is None:
        return None
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return str(v)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", default="akshare,bar_60m",
                    help="逗号分隔，第一个作基准")
    ap.add_argument("--symbols", default="", help="逗号分隔；空=全部主力")
    ap.add_argument("--out", default="", help="证据 JSON 输出路径")
    args = ap.parse_args()

    cfg = get_settings()
    srcs = [x.strip() for x in args.sources.split(",") if x.strip()]
    if len(srcs) < 2:
        print("[verify] 至少需要两个源", file=sys.stderr)
        return 2
    base_src = srcs[0]

    eng = create_engine(cfg.db_url, pool_pre_ping=True)
    Sess = sessionmaker(bind=eng)

    if args.symbols:
        wanted = {x.strip() for x in args.symbols.split(",") if x.strip()}
        specs = [s for s in cfg.main_contracts if s.symbol in wanted]
    else:
        specs = list(cfg.main_contracts)

    per_src: dict[str, dict[str, dict]] = {}
    with Sess() as s:
        for src in srcs:
            # get_settings() 有缓存 → 改的是同一单例，read_hourly_bars 内部读到新值
            cfg.fusion.hourly_src = src
            res = evaluate_all(s, specs, cfg.fusion)
            per_src[src] = {r["symbol"]: r for r in res}
            print(f"[verify] src={src:<10} 评估 {len(res)} 品种，"
                  f"bars<min 的 {sum(1 for r in res if r.get('error'))} 个")

    base = per_src[base_src]
    rows, diffs = [], []
    for sym in sorted(base):
        b = base[sym]
        row = {"symbol": sym, "base_state": b.get("state"),
               "base_pos": _POS.get(b.get("state"), "?"),
               "base_bars": b.get("bars"), "base_dt": str(b.get("latest_dt")),
               "base_close": _fmt(b.get("latest_close")), "sources": {}}
        for src in srcs[1:]:
            o = per_src[src].get(sym) or {}
            row["sources"][src] = {
                "state": o.get("state"), "pos": _POS.get(o.get("state"), "?"),
                "bars": o.get("bars"), "dt": str(o.get("latest_dt")),
                "close": _fmt(o.get("latest_close")),
                "entry_px": _fmt(o.get("entry_px")), "entry_atr": _fmt(o.get("entry_atr")),
                "cur_stop": _fmt(o.get("cur_stop")), "lots": o.get("lots"),
                "state_diff": o.get("state") != b.get("state"),
                "bars_diff": o.get("bars") != b.get("bars"),
            }
        rows.append(row)
        if any(v["state_diff"] for v in row["sources"].values()):
            diffs.append(row)

    print("\n== 汇总 ==")
    print(f"  品种 {len(rows)} 个；state 有差异 {len(diffs)} 个")
    if diffs:
        print(f"  {'品种':<8}{'基准':<8}", end="")
        for src in srcs[1:]:
            print(f"{src:<14}", end="")
        print()
        for r in diffs:
            print(f"  {r['symbol']:<8}{r['base_pos']:<8}", end="")
            for src in srcs[1:]:
                v = r["sources"][src]
                print(f"{v['pos'] + '(' + str(v['bars']) + ')':<14}", end="")
            print()

    if args.out:
        payload = {"meta": {"sources": srcs, "n_symbols": len(rows),
                            "max_bars": cfg.fusion.max_bars,
                            "min_bars": cfg.fusion.min_bars,
                            "settle_delay_sec": cfg.fusion.settle_delay_sec},
                   "n_state_diff": len(diffs), "rows": rows}
        Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
        print(f"\n[verify] 证据已写入 {args.out}")

    return 0 if not diffs else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"[verify] 执行失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(2)
