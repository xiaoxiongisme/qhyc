# -*- coding: utf-8 -*-
"""全量重算复权主连（hourly + daily），对齐主连。单品种异常隔离，不中断整体。

用法（容器内）：
    python /app/runtime/adjust_all_main.py
日志会逐行打印每个品种结果，结尾给出 ok/fail 汇总。
"""
import sys
import traceback

sys.stdout.reconfigure(encoding="utf-8")

from app.core.config import get_settings
from app.ingest.fdf import adjust_fdf


def main() -> None:
    s = get_settings()
    ok = fail = 0
    total = len(s.main_contracts) * 2
    print(f"=== 全量复权重算开始，共 {len(s.main_contracts)} 品种 × 2 周期 ===", flush=True)
    for i, spec in enumerate(s.main_contracts, 1):
        key = f"{spec.exchange}.{spec.product.lower()}"
        for freq in ("hourly", "daily"):
            try:
                adjust_fdf.run(key, freq)
                ok += 1
                print(f"[{ok + fail}/{total}] OK   {key} {freq}", flush=True)
            except Exception as e:  # noqa: BLE001
                fail += 1
                print(f"[{ok + fail}/{total}] FAIL {key} {freq}: {e}", flush=True)
    print(f"=== 全量复权重算结束 ok={ok} fail={fail} ===", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
