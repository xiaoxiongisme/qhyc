"""
手动触发一次融合策略信号扫描（调试 / 验证用，不推送）
- 打印每个品种的：引擎当前应有持仓(state) / DB 持久化持仓 / 小时线根数 / 差异标记
- 用法（容器内）：docker compose exec api python scripts/fusion_scan_once.py

注意：本脚本只读取 hourly_bar 并评估，不会修改 FusionPosition 持仓状态。
"""
from __future__ import annotations

import os
import sys

# 允许以 `python scripts/fusion_scan_once.py` 方式直接运行（脚本目录不在 sys.path 时也能找到 app 包）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.core.config import get_settings
from app.core.db import get_engine, session_scope
from app.core.logging import logger, setup_logging
from app.strategies.fusion_signal import (
    POS_MAP,
    ensure_fusion_table,
    evaluate_all,
    get_position,
)


def main() -> None:
    setup_logging()
    settings = get_settings()
    if not settings.fusion.enabled:
        print("fusion.enabled = false，跳过")
        return
    with session_scope() as s:
        ensure_fusion_table(get_engine())
        results = evaluate_all(s, settings.main_contracts, settings.fusion)
        print(f"{'SYMBOL':10} {'ENGINE':6} {'DB':6} {'BARS':>5}  NOTE")
        print("-" * 50)
        for r in results:
            sym = r["symbol"]
            eng = POS_MAP.get(r["state"], "FLAT")
            db = get_position(s, sym)
            flag = "  <-- 状态变化" if db != eng else ""
            err = r.get("error") or ""
            print(f"{sym:10} {eng:6} {db:6} {str(r.get('bars', 0)):>5}  {err}{flag}")


if __name__ == "__main__":
    main()
