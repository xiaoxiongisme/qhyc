"""四频换月段表统一重算 + 后复权级联重建（CL-20261108-05 / -06）。

背景
----
``l2_adj.roll_segment`` 的换月事件在 **15m 锚点** 上检测一次，再映射到各目标
频率（min5/15/30/60）。历史上不同频率是在不同版本的检测代码下建的，于是
「min15 有 144 段、min5 只有 138 段」这种差异里混着两种原因：
  1. 分辨率导致的多对一合并（合法，5m 桶内的两笔换月落到同一根）；
  2. 旧口径残留（同一事件在新口径下根本不算换月）。
本脚本把四个频率一次性按**当前**口径重算，消除第 2 类差异，使
``ROLL-SEG-COUNT-PARITY`` 之类的跨频断言只剩第 1 类（可解释的）差异。

为什么必须带 ``jit = off``
-------------------------
实测（2026-11-09）：该 CTE 在 A888 上生成 5713 个函数的计划，PG 的 JIT 编译
本身要 32s Optimization + 25s Emission，占总耗时 58/60 秒。关掉后单品种
min5 从 75s 降到 8.2s，结果逐位不变。这是历史上「重建慢到要 OOM」的真凶之一，
另一个放大器是容器 /dev/shm 只有 64MB（已改 1g，见 docker-compose.cloud.yml）。

用法
----
    python scripts/ops/rebuild_roll_all_freqs.py                     # 38 白名单 × 4 频
    python scripts/ops/rebuild_roll_all_freqs.py --freqs min60       # 只跑 60m（V5X 实盘口径）
    python scripts/ops/rebuild_roll_all_freqs.py --symbols PF888,SM888 --skip-ba
    python scripts/ops/rebuild_roll_all_freqs.py --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from sqlalchemy import text

from app.core.db import get_engine

TBL = {"min5": "bar_5m", "min15": "bar_15m", "min30": "bar_30m", "min60": "bar_60m"}
BA = {"min5": "bar_5m_ba", "min15": "bar_15m_ba", "min30": "bar_30m_ba", "min60": "bar_60m_ba"}


def whitelist_symbols(eng) -> list[str]:
    """38 个可交易品种主连（l3_ref.tradable_variety），不是 config 里的 53 个。"""
    with eng.connect() as c:
        rows = c.execute(text("SELECT variety FROM l3_ref.tradable_variety ORDER BY variety")).fetchall()
    return [f"{r[0]}888" for r in rows]


def seg_sig(conn, sym: str, freq: str) -> tuple[int, str]:
    """段表指纹：段数 + 边界时刻的 md5（改前改后可直接比）。"""
    row = conn.execute(text("""
        SELECT count(*), coalesce(md5(string_agg(seg_start::text, ',' ORDER BY seg_no)), '')
          FROM l2_adj.roll_segment WHERE symbol = :s AND freq = :f"""),
        {"s": sym, "f": freq}).fetchone()
    return int(row[0]), row[1]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--freqs", default="min5,min15,min30,min60")
    ap.add_argument("--symbols", default="", help="逗号分隔；留空 = 38 白名单")
    ap.add_argument("--skip-ba", action="store_true", help="只重算段表，不重建后复权")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log", default="/tmp/rebuild_roll.jsonl")
    args = ap.parse_args()

    freqs = [f.strip() for f in args.freqs.split(",") if f.strip()]
    for f in freqs:
        if f not in TBL:
            print(f"不支持的 freq={f}", file=sys.stderr)
            return 2

    eng = get_engine()
    syms = ([s.strip() for s in args.symbols.split(",") if s.strip()]
            if args.symbols else whitelist_symbols(eng))
    print(f"频率 {freqs} / 品种 {len(syms)} 个 / skip_ba={args.skip_ba} / dry_run={args.dry_run}")

    log = open(args.log, "a", encoding="utf-8")
    total = len(freqs) * len(syms)
    done = 0
    t_all = time.time()
    fail = []

    for freq in freqs:
        for sym in syms:
            done += 1
            t0 = time.time()
            rec = {"freq": freq, "sym": sym, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}
            try:
                with eng.connect() as c:
                    rec["seg_before"], rec["sig_before"] = seg_sig(c, sym, freq)
                    rec["ba_before"] = int(c.execute(
                        text(f"SELECT count(*) FROM l2_adj.{BA[freq]} WHERE symbol = :s"),
                        {"s": sym}).scalar() or 0)

                if args.dry_run:
                    rec["status"] = "dry"
                else:
                    with eng.begin() as c:
                        # JIT 关掉是性能关键：见模块 docstring 的实测
                        c.execute(text("SET LOCAL jit = off"))
                        c.execute(text("SET LOCAL max_parallel_workers_per_gather = 0"))
                        c.execute(text("SET LOCAL work_mem = '64MB'"))
                        c.execute(text("SET LOCAL statement_timeout = 0"))
                        c.execute(text("DELETE FROM l2_adj.roll_segment WHERE symbol = :s AND freq = :f"),
                                  {"s": sym, "f": freq})
                        c.execute(text("CALL sp_build_l2_roll_segment(:f, false, :s)"),
                                  {"f": freq, "s": sym})
                        rec["t_seg"] = round(time.time() - t0, 1)
                        if not args.skip_ba:
                            c.execute(text("CALL l2_adj.sp_build_l2_back_adjusted(:f, :s, true)"),
                                      {"f": freq, "s": sym})
                            rec["t_ba"] = round(time.time() - t0 - rec["t_seg"], 1)
                    with eng.connect() as c:
                        rec["seg_after"], rec["sig_after"] = seg_sig(c, sym, freq)
                        rec["ba_after"] = int(c.execute(
                            text(f"SELECT count(*) FROM l2_adj.{BA[freq]} WHERE symbol = :s"),
                            {"s": sym}).scalar() or 0)
                    rec["status"] = "ok"
                    rec["changed"] = rec["sig_before"] != rec["sig_after"]
            except Exception as e:  # noqa: BLE001 —— 逐条记账，不中断整批
                rec["status"] = "fail"
                rec["err"] = f"{type(e).__name__}: {str(e).splitlines()[0][:160]}"
                fail.append((freq, sym, rec["err"]))
            rec["sec"] = round(time.time() - t0, 1)
            log.write(json.dumps(rec, ensure_ascii=False) + "\n"); log.flush()
            flag = {"ok": "✔" if not rec.get("changed") else "★改", "dry": "·", "fail": "✘"}[rec["status"]]
            print(f"[{done:3d}/{total}] {freq:5s} {sym:7s} {flag} "
                  f"seg {rec.get('seg_before')}→{rec.get('seg_after')} "
                  f"ba {rec.get('ba_before')}→{rec.get('ba_after')} {rec['sec']}s "
                  f"{rec.get('err', '')}", flush=True)

    print(f"\n完成 {done} 项，用时 {(time.time() - t_all) / 60:.1f} 分钟，失败 {len(fail)} 项")
    for f in fail:
        print("  FAIL", f)
    return 0 if not fail else 1


if __name__ == "__main__":
    raise SystemExit(main())
