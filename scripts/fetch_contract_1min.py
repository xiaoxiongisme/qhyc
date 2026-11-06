# -*- coding: utf-8 -*-
"""全品种全部合约 1 分钟行情补采（cron_fetch_1min 入口）。

背景
----
``cron_fetch_1min`` 原引用的 ``scripts/fetch_contract_1min.py`` 已于
63163ce (2026-10-09) 删除，任务每 15 分钟空转报 No such file，2026-10-31 被软禁用。
2026-11-06 按用户要求重建：改为「全市场活跃品种 → 全部上市合约」采集，
并落地 ``l3_ref.contract_universe`` 合约清单。

调用（crontab，外层已有 flock -n /tmp/fetch_1min.lock 防重入）
--------------------------------------------------------------
    docker exec qhyc-api python scripts/fetch_contract_1min.py --all --data-length 8000

    # 手动
    docker exec qhyc-api python scripts/fetch_contract_1min.py --refresh-universe --dry-run
    docker exec qhyc-api python scripts/fetch_contract_1min.py --varieties RB,CU --max-contracts 20

说明
----
* ``--data-length`` 保留仅为兼容旧 crontab 命令；akshare 1 分钟接口固定返回
  最近约 1024 根（跨约 6 个交易日），参数不生效。
* 每次运行会按 ``--universe-ttl-hours``（默认 12h）判断是否重刷合约清单；
  清单刷新需 86 次接口调用，故不每次都刷。
* 写入 ``l0_raw.minute_bar`` 的 ``layer='contract'``，``ON CONFLICT DO NOTHING`` 幂等。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.db import session_scope  # noqa: E402
from app.core.logging import logger  # noqa: E402
from app.ingest import contract_universe as uni  # noqa: E402
from app.ingest.contract_minute_collector import ContractMinuteCollector  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="全品种全部合约 1 分钟行情补采")
    ap.add_argument("--all", action="store_true", help="全品种（默认行为，兼容旧 crontab）")
    ap.add_argument("--data-length", type=int, default=8000,
                    help="兼容旧参数：akshare 固定 1024 根，不生效")
    ap.add_argument("--refresh-universe", action="store_true", help="强制重刷合约清单")
    ap.add_argument("--universe-ttl-hours", type=float, default=12.0)
    ap.add_argument("--workers", type=int, default=6, help="并发拉取线程数")
    ap.add_argument("--pause-sec", type=float, default=0.05, help="每完成一合约后的节流")
    ap.add_argument("--max-contracts", type=int, default=0, help="调试/灰度：只采前 N 个合约")
    ap.add_argument("--varieties", default="", help="逗号分隔品种码，如 RB,CU")
    ap.add_argument("--dry-run", action="store_true", help="只拉取不写库")
    args = ap.parse_args()

    with session_scope() as s:
        age = uni.universe_age_hours(s)
        if args.refresh_universe or age is None or age > args.universe_ttl_hours:
            logger.info(f"[fetch_1min] 刷新合约清单 (age={age})")
            n = uni.refresh(s)
            print(f"[universe] active_contracts={n}", flush=True)
        else:
            print(f"[universe] 复用清单 age={age:.1f}h", flush=True)

        contracts = uni.load_active(s)
        if args.varieties:
            want = {v.strip().upper() for v in args.varieties.split(",") if v.strip()}
            contracts = [c for c in contracts if c["variety_code"].upper() in want]
        if args.max_contracts:
            contracts = contracts[: args.max_contracts]
        if not contracts:
            print("[fetch_1min] 合约清单为空，退出", file=sys.stderr)
            return 2

        stats = ContractMinuteCollector(
            s, workers=args.workers, pause_sec=args.pause_sec
        ).collect(contracts, dry_run=args.dry_run)
        stats["dry_run"] = args.dry_run
        print("[fetch_1min] " + json.dumps(stats, ensure_ascii=False, default=str), flush=True)
        # 单合约失败属常态（退市/无成交/接口空），只有「全部失败」才判非零
        return 0 if stats["ok"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
