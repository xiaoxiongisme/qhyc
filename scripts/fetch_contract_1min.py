# -*- coding: utf-8 -*-
"""全品种全部合约 1 分钟行情补采（cron_fetch_1min 入口）。

背景
----
``cron_fetch_1min`` 原引用的 ``scripts/fetch_contract_1min.py`` 已于
63163ce (2026-10-09) 删除，任务每 15 分钟空转报 No such file，2026-10-31 被软禁用。
2026-11-06 按用户要求重建：改为「全市场活跃品种 → 全部上市合约」采集，
并落地 ``l3_ref.contract_universe`` 合约清单。

效率优化（2026-11-06 晚）
-------------------------
1. **交易时段门控**：非交易时段直接返回，省掉整轮 668 次 HTTP。
   见 :mod:`app.ingest.trading_window`；``--force`` / ``--no-session-gate`` 可绕过。
2. **增量提交**：写库前按每合约 ``max(ts)`` 水位线过滤，收盘后每轮从
   「提交 68 万行」降到「提交 0 行」；``--force`` 恢复全量重提。
3. 统计新增 ``rows_fetched`` / ``rows_skipped`` / ``gated``，便于验收与看板。

调用（crontab，外层已有 flock -n /tmp/fetch_1min.lock 防重入）
--------------------------------------------------------------
    docker exec qhyc-api python scripts/fetch_contract_1min.py --all --data-length 8000

    # 手动：强制全量补采（忽略时段门控与水位线）
    docker exec qhyc-api python scripts/fetch_contract_1min.py --force
    # 手动：忽略门控但保留增量（休市期验证增量逻辑）
    docker exec qhyc-api python scripts/fetch_contract_1min.py --no-session-gate
    # 灰度：只采 RB 的 3 个合约
    docker exec qhyc-api python scripts/fetch_contract_1min.py --varieties RB --max-contracts 3

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
from app.ingest.contract_minute_collector import (  # noqa: E402
    ContractMinuteCollector,
    load_watermarks,
)
from app.ingest.trading_window import describe, is_collection_window  # noqa: E402


def _gate_enabled(args: argparse.Namespace) -> bool:
    """配置里的门控开关（可被 CLI 覆盖）。"""
    if args.no_session_gate or args.force:
        return False
    try:
        from app.core.config import get_settings
        return bool(get_settings().yaml.ingest.contract_session_gate)
    except Exception:  # noqa: BLE001  配置缺失时默认开启，保守跳过无效轮次
        return True


def _lookback_days(args: argparse.Namespace) -> int:
    if args.watermark_lookback_days:
        return args.watermark_lookback_days
    try:
        from app.core.config import get_settings
        return int(get_settings().yaml.ingest.contract_watermark_lookback_days)
    except Exception:  # noqa: BLE001
        return 8


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
    # ---- 效率优化参数 ----
    ap.add_argument("--force", action="store_true",
                    help="强制全量：跳过交易时段门控 + 跳过水位线过滤（历史补采用）")
    ap.add_argument("--no-session-gate", action="store_true",
                    help="只跳过交易时段门控，保留增量提交")
    ap.add_argument("--no-incremental", action="store_true",
                    help="只跳过水位线过滤，保留门控（等价于全量提交）")
    ap.add_argument("--watermark-lookback-days", type=int, default=0,
                    help="水位线查询回看天数（0=读配置，默认 8）")
    args = ap.parse_args()

    # 1) 交易时段门控：必须放在任何网络/DB 操作之前
    if _gate_enabled(args) and not is_collection_window():
        msg = {
            "gated": True,
            "reason": "非交易时段（见 app/ingest/trading_window.py）",
            "now": describe(),
            "hint": "--force 或 --no-session-gate 可绕过",
        }
        print("[fetch_1min] " + json.dumps(msg, ensure_ascii=False), flush=True)
        return 0

    incremental = not (args.force or args.no_incremental)

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

        # 2) 批量水位线（一条 SQL，避免 668 次查询）
        wm = None
        if incremental:
            wm = load_watermarks(
                s, (c["symbol"] for c in contracts),
                lookback_days=_lookback_days(args),
            )
            print(f"[watermark] contracts={len(contracts)} watermarked={len(wm)} "
                  f"new_or_cold={len(contracts) - len(wm)}", flush=True)

        stats = ContractMinuteCollector(
            s, workers=args.workers, pause_sec=args.pause_sec
        ).collect(contracts, dry_run=args.dry_run,
                  watermarks=wm, incremental=incremental)
        stats["dry_run"] = args.dry_run
        stats["gated"] = False
        stats["force"] = bool(args.force)
        print("[fetch_1min] " + json.dumps(stats, ensure_ascii=False, default=str), flush=True)
        # 单合约失败属常态（退市/无成交/接口空），只有「全部失败」才判非零
        return 0 if stats["ok"] > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
