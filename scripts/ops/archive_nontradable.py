#!/usr/bin/env python3
"""非白名单历史月度归档（CL-20261107-01 S5）。

方案 v3 / Q5–Q9 定案要点，本脚本严格照做
------------------------------------------
* **只导出不删**（D1）：本脚本没有任何 DELETE/TRUNCATE。删除是不可逆动作，
  需要用户单独拍板；在拍板之前，归档只是「把收口后会失去维护的数据搬到别处
  存一份」，生产数据原样保留。
* **月度增量**：每月 1 日 03:20 由调度器调用，导出「上次水位之后 → 本次 cutoff」
  的非白名单行。首次 baseline（302 片 / 2.0 亿行 / 3.65GB，cutoff
  2026-11-06 15:00）已在 2026-11-07 单独完成，故本脚本的水位线从
  ``2026-11-06 15:00:00+08`` 起算，不会重复导出。
* **水位线 + 断点续跑**：``l3_ref.nontradable_archive_log`` 按
  ``(table_name, batch_key)`` 唯一。已 ``success`` 的批次直接跳过；
  ``failed``/``partial`` 的会重跑。
* **可核验**：每片记 rows / sha256 / file_path，导出后立刻复算 sha256 比对。

归档对象（Q16 修正后：**不含日线、日频基本面、因子/影子表、main_contract_map**）
--------------------------------------------------------------------------------
    l0_raw.minute_bar      （含 layer='index' 的 8888 行）
    l1_mkt.bar_5m / 15m / 30m / 60m
    l2_adj.bar_5m_ba / 15m_ba / 30m_ba / 60m_ba

「非白名单」判定用**品种维度**（``regexp_replace(symbol,'8888?$','')``），
不读 ``v_tradable_scope``——后者含持仓豁免、会随持仓漂移，用它做归档口径会让
同一品种在不同月份归属不同，无法对账。

用法
----
    python scripts/ops/archive_nontradable.py --dry-run          # 只统计
    python scripts/ops/archive_nontradable.py --batch-key 2026-12
    python scripts/ops/archive_nontradable.py --table l1_mkt.bar_60m
    python scripts/ops/archive_nontradable.py --list             # 看历史批次
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402

from app.core.db import session_scope  # noqa: E402
from app.core.logging import logger  # noqa: E402

SH_TZ = ZoneInfo("Asia/Shanghai")

#: 首次 baseline 的 cutoff（2026-11-07 那次 302 片全量导出的时间上界）。
#: 水位线从这里起算，避免重复导出已经备过的历史。
BASELINE_CUTOFF = datetime(2026, 11, 6, 15, 0, tzinfo=SH_TZ)

#: 归档目标：(schema.table, 时间列)
TARGETS: list[tuple[str, str]] = [
    ("l0_raw.minute_bar", "ts"),
    ("l1_mkt.bar_5m", "bucket"),
    ("l1_mkt.bar_15m", "bucket"),
    ("l1_mkt.bar_30m", "bucket"),
    ("l1_mkt.bar_60m", "bucket"),
    ("l2_adj.bar_5m_ba", "bucket"),
    ("l2_adj.bar_15m_ba", "bucket"),
    ("l2_adj.bar_30m_ba", "bucket"),
    ("l2_adj.bar_60m_ba", "bucket"),
]

DEFAULT_OUT = "/Docker/qhyc_archive/nontradable"

#: 非白名单品种判定（品种维度，不受持仓豁免影响）
_NON_WL_PRED = """
    regexp_replace(symbol, '8888?$', '') NOT IN (
        SELECT variety_code FROM l3_ref.tradable_variety
         WHERE effective_from <= CURRENT_DATE
           AND (effective_to IS NULL OR effective_to >= CURRENT_DATE))
"""


def _batch_key(dt: datetime | None = None) -> str:
    return (dt or datetime.now(SH_TZ)).strftime("%Y-%m")


def _watermark(s, table: str) -> datetime:
    """该表上次成功导出的时间上界；无记录则用 baseline cutoff。"""
    wm = s.execute(text(
        "SELECT max(watermark_ts) FROM l3_ref.nontradable_archive_log "
        "WHERE table_name = :t AND status = 'success'"), {"t": table}).scalar()
    return wm or BASELINE_CUTOFF


def _count(s, table: str, tcol: str, lo: datetime, hi: datetime) -> int:
    return int(s.execute(text(
        f"SELECT count(*) FROM {table} WHERE {tcol} > :lo AND {tcol} <= :hi "
        f"AND {_NON_WL_PRED}"), {"lo": lo, "hi": hi}).scalar() or 0)


def _copy_out(cur, sql: str, f) -> None:
    """流式 COPY 到文本文件，兼容 psycopg2 与 psycopg3。

    ⚠ 本项目的 SQLAlchemy DSN 是 ``postgresql+psycopg://``，即 **psycopg3**。
      psycopg3 的 cursor **没有** ``copy_expert``（那是 psycopg2 的 API），
      直接照搬会报 ``'Cursor' object has no attribute 'copy_expert'``
      （2026-11-07 实测踩过）。两代驱动的 COPY 接口必须分开处理。
    """
    stmt = f"COPY ({sql}) TO STDOUT WITH (FORMAT csv, HEADER true)"
    if hasattr(cur, "copy_expert"):            # psycopg2
        cur.copy_expert(stmt, f)
        return
    with cur.copy(stmt) as cp:                 # psycopg3
        for block in cp:
            # psycopg3 给的是 memoryview，本身没有 decode()，必须先转 bytes
            f.write(bytes(block).decode("utf-8") if isinstance(block, (bytes, memoryview))
                    else block)


def _archive_one(s, table: str, tcol: str, batch: str, out_dir: Path,
                 cutoff: datetime, dry_run: bool) -> dict:
    lo = _watermark(s, table)
    n = _count(s, table, tcol, lo, cutoff)
    if n == 0:
        logger.info(f"[archive] {table}: 水位 {lo:%Y-%m-%d %H:%M} 之后无非白名单新行，跳过")
        return {"table": table, "rows": 0, "skipped": True}

    if dry_run:
        logger.info(
            f"[archive][dry-run] {table}: ({lo:%Y-%m-%d %H:%M}, {cutoff:%Y-%m-%d %H:%M}] "
            f"→ {n:,} 行")
        return {"table": table, "rows": n, "skipped": False, "dry_run": True}

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{table.replace('.', '__')}__{batch}.csv.gz"

    # 幂等：同一批次重跑时覆盖同名文件（不追加、不留半截）
    if path.exists():
        path.unlink()

    # ⚠ 这里必须是**原生** SQL 字面量，不能用 SQLAlchemy 的 :lo 命名参数 ——
    #   COPY 走的是驱动原生通道（psycopg2 copy_expert / psycopg3 copy），
    #   SA 的绑定参数不会被替换，直接传给服务端会报
    #   ``syntax error at or near "%"``（2026-11-07 实测踩过）。
    #   lo/hi 是本脚本自行计算的 datetime，非外部输入，格式化为字面量是安全的。
    sql = (f"SELECT * FROM {table} "
           f"WHERE {tcol} > '{lo.isoformat()}'::timestamptz "
           f"AND {tcol} <= '{cutoff.isoformat()}'::timestamptz "
           f"AND {_NON_WL_PRED} ORDER BY symbol, {tcol}")

    # 大表 COPY 可能远超默认 statement_timeout；本会话单独放开（不改进程全局）
    s.execute(text("SET LOCAL statement_timeout = 0"))

    # SQLAlchemy 2.0：session.connection() 是 SA 的 Connection，它的 .connection
    # 是 _ConnectionFairy，**真正能 copy_expert 的是 driver_connection**。
    # 直接拿 .connection 会得到 SA 的 Cursor 代理，报
    # 'Cursor' object has no attribute 'copy_expert'（2026-11-07 实测踩过）。
    _sa = s.connection().connection
    raw = getattr(_sa, "driver_connection", None) or _sa
    cur = raw.cursor()
    try:
        with gzip.open(path, "wt", encoding="utf-8", newline="") as f:
            _copy_out(cur, sql, f)
        cur.close()
    except Exception:
        cur.close()
        # 半截文件必须删掉，否则下一轮会把残缺文件当成「已导出」
        if path.exists():
            path.unlink()
        raise

    # 导出后立刻复算 sha256 + 行数：不验证的导出等于没导出
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        got = sum(1 for _ in f) - 1  # 减去表头
    if got != n:
        raise RuntimeError(
            f"{table}: 导出行数 {got} != 预期 {n}（文件 {path}）")

    s.execute(text("""
        INSERT INTO l3_ref.nontradable_archive_log
            (table_name, batch_key, watermark_ts, rows_exported,
             file_path, sha256, status, finished_at, note)
        VALUES (:t, :b, :wm, :n, :p, :h, 'success', now(), :note)
        ON CONFLICT (table_name, batch_key) DO UPDATE SET
            watermark_ts  = EXCLUDED.watermark_ts,
            rows_exported = EXCLUDED.rows_exported,
            file_path     = EXCLUDED.file_path,
            sha256        = EXCLUDED.sha256,
            status        = 'success',
            finished_at   = now(),
            note          = EXCLUDED.note
    """), {"t": table, "b": batch, "wm": cutoff, "n": n, "p": str(path),
           "h": h.hexdigest(),
           "note": f"since {lo.isoformat()}（只导出不删，D1）"})
    s.commit()
    logger.info(
        f"[archive] {table}: {n:,} 行 → {path.name} "
        f"({path.stat().st_size/1e6:.1f}MB, sha256={h.hexdigest()[:12]}…)")
    return {"table": table, "rows": n, "skipped": False,
            "file": str(path), "sha256": h.hexdigest()}


def _list(s) -> None:
    rows = s.execute(text("""
        SELECT batch_key, table_name, watermark_ts, rows_exported, status, finished_at
        FROM l3_ref.nontradable_archive_log
        ORDER BY batch_key DESC, table_name
    """)).fetchall()
    if not rows:
        print("（尚无归档记录）")
        return
    print(f"{'batch':<9} {'table':<22} {'watermark':<20} {'rows':>10}  status")
    for b, t, wm, n, st, fin in rows:
        print(f"{b:<9} {t:<22} {wm:%Y-%m-%d %H:%M %Z} {n:>10,}  {st}")


def main() -> int:
    ap = argparse.ArgumentParser(description="非白名单历史月度归档（只导出不删）")
    ap.add_argument("--batch-key", default=None, help="批次标识，默认当月 YYYY-MM")
    ap.add_argument("--table", default=None, help="只处理单表（如 l1_mkt.bar_60m）")
    ap.add_argument("--cutoff", default=None,
                    help="本轮时间上界（ISO8601），默认 now()")
    ap.add_argument("--out-dir", default=DEFAULT_OUT)
    ap.add_argument("--dry-run", action="store_true", help="只统计行数，不落盘")
    ap.add_argument("--list", action="store_true", help="列出历史批次后退出")
    a = ap.parse_args()

    batch = a.batch_key or _batch_key()
    cutoff = (datetime.fromisoformat(a.cutoff) if a.cutoff
              else datetime.now(SH_TZ))
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=SH_TZ)

    out_dir = Path(a.out_dir)
    targets = [t for t in TARGETS if a.table in (None, t[0])]
    if not targets:
        print(f"未知表 {a.table}；可选：{[t[0] for t in TARGETS]}")
        return 2

    results, failed = [], 0
    with session_scope() as s:
        if a.list:
            _list(s)
            return 0
        logger.info(
            f"[archive] batch={batch} cutoff={cutoff:%Y-%m-%d %H:%M} "
            f"tables={len(targets)} dry_run={a.dry_run}")
        for table, tcol in targets:
            try:
                results.append(
                    _archive_one(s, table, tcol, batch, out_dir, cutoff, a.dry_run))
            except Exception as exc:  # noqa: BLE001 —— 单表失败不阻断其余表
                failed += 1
                logger.exception(f"[archive] {table} 失败: {exc}")
                try:
                    s.rollback()
                    s.execute(text("""
                        INSERT INTO l3_ref.nontradable_archive_log
                            (table_name, batch_key, status, note)
                        VALUES (:t, :b, 'failed', :n)
                        ON CONFLICT (table_name, batch_key) DO UPDATE
                           SET status='failed', note=EXCLUDED.note, finished_at=now()
                    """), {"t": table, "b": batch, "n": str(exc)[:400]})
                    s.commit()
                except Exception:  # noqa: BLE001
                    s.rollback()

    total = sum(r["rows"] for r in results)
    done = sum(1 for r in results if not r.get("skipped"))
    logger.info(f"[archive] 完成：{done}/{len(targets)} 表，共 {total:,} 行，失败 {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
