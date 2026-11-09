#!/usr/bin/env python3
"""L1 会话网格重建驱动（CL-20261109-14）。

为什么需要它
------------
`rebuild_bars_session.py` 一次只能重建**一个频率**、且 `run()` 是「整段聚合 →
fetchall 到内存 → 删 → 插」，把一个品种 11 年 × 4 频塞进一次调用会 OOM
（本机 4GB 内存，实测 26M 行全表聚合即被 cgroup kill）。所以必须
「逐品种 × 逐频率」推进。

但直接在外层写 bash 循环会有两个问题：
  1. 每次 `docker exec python ...` 都要重新 import app.* 与建连接池（~1.5s），
     304 次就是 8 分钟纯开销；
  2. 中断后没有进度记录，只能从头再来。

本驱动在**同一进程内**逐个调用 `rebuild_bars_session.run()`，并用 JSON 记录
已完成的 (symbol, step)，重跑时自动跳过 —— 中断可续。

用法
----
    python scripts/ops/rebuild_l1_grid.py \
        --symbols A888,RB888 --start 2015-01-01 --end 2026-10-28 \
        --freqs 60,30,15,5 --apply

    # 断点续跑：同样的参数再加 --resume
    python scripts/ops/rebuild_l1_grid.py ... --resume

进度文件默认 `/app/logs/rebuild_l1_grid_progress.json`。

⚠ 频率顺序默认 60,30,15,5：先写小的（60m 只有 4+夜盘 根/日），
任何中断都先保证高周期一致，避免「5m 写了一半、60m 还是旧网格」的中间态。
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path


def _load_rebuild_module():
    """按文件路径加载 rebuild_bars_session，绕开 scripts/ 不是包的问题。"""
    here = Path(__file__).resolve()
    root = next(p for p in here.parents if (p / "app").is_dir())
    target = root / "scripts" / "validate" / "rebuild_bars_session.py"
    if not target.is_file():
        raise SystemExit(f"未找到 {target}")
    spec = importlib.util.spec_from_file_location("_rbs", target)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # type: ignore[union-attr]
    return mod


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", required=True,
                    help="逗号分隔的 symbol 列表（只接受 *888 / *8888）")
    ap.add_argument("--start", required=True,
                    help="YYYY-MM-DD，按 **UTC** 解析（= 上海当日 08:00），区间左闭")
    ap.add_argument("--end", required=True,
                    help="YYYY-MM-DD，按 **UTC** 解析（= 上海当日 08:00），区间右开 —— "
                         "要覆盖到 D 日就必须传 D+1。起始日建议对齐周六，"
                         "避免删除窗口落在还有夜盘交易的交易日上")
    ap.add_argument("--freqs", default="60,30,15,5", help="逗号分隔，默认 60,30,15,5")
    ap.add_argument("--max-gap", type=int, default=150)
    ap.add_argument("--apply", action="store_true", help="不带则全部 dry-run")
    ap.add_argument("--resume", action="store_true", help="跳过进度文件里已完成的组合")
    ap.add_argument("--progress", default="/app/logs/rebuild_l1_grid_progress.json")
    ap.add_argument("--limit-days", type=int, default=0)
    args = ap.parse_args()

    rbs = _load_rebuild_module()
    rbs._patch_engine_guc()               # 解除压缩超表整块解压上限

    syms = [s.strip() for s in args.symbols.split(",") if s.strip()]
    bad = [s for s in syms if not s[-3:] in ("888",) and not s.endswith("8888")]
    if bad:
        raise SystemExit(f"symbols 只接受 *888 / *8888，非法：{bad}")
    steps = [int(x) for x in args.freqs.split(",")]

    prog_path = Path(args.progress)
    prog_path.parent.mkdir(parents=True, exist_ok=True)
    done: set[str] = set()
    if args.resume and prog_path.is_file():
        try:
            done = set(json.loads(prog_path.read_text(encoding="utf-8")).get("done", []))
            print(f"[resume] 已有完成记录 {len(done)} 条，将跳过", flush=True)
        except Exception:                                      # noqa: BLE001
            print("[resume] 进度文件无法解析，从头开始", flush=True)

    total = len(syms) * len(steps)
    i = 0
    t_all = time.time()
    failed: list[str] = []
    for sym in syms:
        for step in steps:
            i += 1
            key = f"{sym}|{step}"
            if key in done:
                continue
            t0 = time.time()
            print(f"\n[{i}/{total}] {sym} {step}m  "
                  f"({datetime.now():%H:%M:%S})", flush=True)
            try:
                rc = rbs.run([sym], args.start, args.end, step, args.max_gap,
                             rbs.DEFAULT_PRIORITY, args.apply,
                             f"l1_mkt.bar_{step}m", args.limit_days)
            except Exception:                                  # noqa: BLE001
                rc = 1
                print(f"  ❌ 异常：\n{traceback.format_exc()[-800:]}", flush=True)
            dur = time.time() - t0
            if rc == 0:
                done.add(key)
                prog_path.write_text(json.dumps(
                    {"done": sorted(done), "updated_at": datetime.now().isoformat()},
                    ensure_ascii=False), encoding="utf-8")
                print(f"  ✅ {dur:.1f}s（累计 {len(done)}/{total}）", flush=True)
            else:
                failed.append(key)
                print(f"  ❌ 退出码 {rc}，{dur:.1f}s", flush=True)

    el = time.time() - t_all
    print(f"\n{'='*70}\n完成 {len(done)}/{total} 组合，用时 {el/60:.1f} 分钟", flush=True)
    if failed:
        print(f"失败：{failed}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
