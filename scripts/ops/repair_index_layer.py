#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""修复 8888 指数层：清掉 ffill 伪造行，并按真实合约分钟重建 L0 → L1。

背景（CL-20261107-13 / -14）
---------------------------
``scripts/ops/extend_index_8888.py`` 首版直接用了 ``synth_8888`` 的返回值，而它末尾
有一句 ``cont.reindex(anchor).ffill()``（anchor = 888 主连时间轴）。增量链路里 anchor
来自 ``layer='continuous'``、front 来自 ``layer='contract'``，两者可以不同步：

  * 2026-11-02：合约层整天没有 FU 的任何行 → front 空 → ffill 把 10-30 的收盘价铺满
    11-02 全天（344 行同一价位）；
  * 2026-10-31：周六，合约层无数据，被 anchor 拉出 865 行假 bar。

后果不是「精度差一点」，而是**把换月判据变成随机判据**：ratio 门是
``coalesce(idx_delta,1e9) < 0.40*|cc|``，常数平线的 ``idx_delta`` 恒为 0 ⇒ 门恒成立
⇒ 任何跳空都被判成换月。实测 FU 在 11-02 09:15 的**周末跳空** -146 被判成换月，
连带 8 个品种各写了一段假 offset 进 ``l2_adj.roll_segment``。

另有一个独立缺陷（CL-20261107-13）：品种口径 ``l3_ref.tradable_scope_array()`` 的
80 个符号**全是 ``*888``**，一个 ``*8888`` 都没有；而 ``_synth_one`` 把口径过滤加在
读取侧 ⇒ 指数层读不到任何行 ⇒ ``FU8888`` 的 ``bar_15m``/``bar_60m`` 停在
2026-11-02 23:00，而 L0 已到 11-06 23:00。指数层是换月 ratio 门的唯一输入，
停更 = 换月检测停更，且全程不报错。

本脚本做的三件事（顺序不可换）
------------------------------
1. 删掉 ``l0_raw.minute_bar`` 里 ``src='incr_synth_8888'`` 的全部行（都是本次伪造的）；
2. 调修好的 ``extend_index_8888.py`` 重建 L0（它会按 ``front.index`` 硬裁剪，不再 ffill）；
3. 删掉 L1 ``l1_mkt.bar_*`` 里窗口内的指数符号桶，再用 ``_synth_one(layer='index')``
   重建（该路径已不套品种口径）。

用法
----
    python scripts/ops/repair_index_layer.py                       # 干跑，只报告
    python scripts/ops/repair_index_layer.py --apply               # 实际修复
    python scripts/ops/repair_index_layer.py --apply --since 2026-10-29
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, "/app")
sys.path.insert(0, "./scripts")

SRC_BAD = "incr_synth_8888"
ROOT = Path(__file__).resolve().parents[2]


def _connect():
    """与 extend_index_8888.py 同一条连接路径（psycopg3 + .env 凭据）。"""
    from load_local_csv_1min import _load_db_creds  # noqa: E402
    import psycopg  # noqa: E402

    return psycopg.connect(**_load_db_creds())


def _report_l0(cur) -> list[tuple]:
    cur.execute("""
        SELECT ts::date AS d, count(*) AS n, count(DISTINCT symbol) AS nsym,
               count(DISTINCT close) AS nclose
        FROM l0_raw.minute_bar
        WHERE layer='index' AND src=%s
        GROUP BY 1 ORDER BY 1
    """, (SRC_BAD,))
    return cur.fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-10-29",
                    help="L1 重建起点（含），默认 2026-10-29")
    ap.add_argument("--apply", action="store_true", help="实际写库（默认干跑）")
    ap.add_argument("--skip-l0", action="store_true")
    ap.add_argument("--skip-l1", action="store_true")
    a = ap.parse_args()

    conn = _connect()
    cur = conn.cursor()

    # ---------- 步骤 1：报告并删除伪造的 L0 指数行 ----------
    rows = _report_l0(cur)
    total = sum(r[1] for r in rows)
    print(f"[1] src={SRC_BAD} 共 {total} 行 / {len(rows)} 天")
    for d, n, nsym, nclose in rows:
        flag = "  ← 整天恒值(伪造平线)" if nclose == 1 and n > 20 else ""
        print(f"    {d}  rows={n:5d}  syms={nsym:3d}  distinct_close={nclose:4d}{flag}")

    if not a.apply:
        print("\n干跑结束（加 --apply 实际修复）")
        conn.close()
        return 0

    if not a.skip_l0:
        cur.execute("DELETE FROM l0_raw.minute_bar WHERE layer='index' AND src=%s",
                    (SRC_BAD,))
        print(f"[1] 已删除 {cur.rowcount} 行")
        conn.commit()

        # ---------- 步骤 2：用修好的脚本重建 L0 ----------
        print("[2] 重建 L0 指数层（extend_index_8888.py）…")
        rc = subprocess.run(
            [sys.executable, "scripts/ops/extend_index_8888.py"],
            cwd=str(ROOT), timeout=7200).returncode
        print(f"[2] 退出码 {rc}（2 = 有品种接缝校验失败，需人工看）")

    # ---------- 步骤 3：L1 定点重建（指数符号，绕过口径）----------
    if a.skip_l1:
        conn.close()
        return 0

    cur.execute("""
        SELECT ts::date AS d, count(*) AS n, count(DISTINCT symbol) AS nsym,
               count(DISTINCT close) AS nclose
        FROM l0_raw.minute_bar
        WHERE layer='index' AND ts >= %s
        GROUP BY 1 ORDER BY 1
    """, (a.since,))
    print("[3] 重建前 L0 指数层：")
    for d, n, nsym, nclose in cur.fetchall():
        print(f"    {d}  rows={n:5d}  syms={nsym:3d}  distinct_close={nclose:4d}")
    conn.close()

    from sqlalchemy import text as sqltext  # noqa: E402

    from app.core.db import session_scope  # noqa: E402
    from app.ingest.synthesizer import (  # noqa: E402
        _BARS, _INDEX_SYM_PRED, _SESSION_BARS, _UPSERT_TAIL, _SH_TZ, _session_floor,
        _synth_one,
    )

    since_dt = datetime.fromisoformat(a.since).replace(tzinfo=_SH_TZ)
    # 会话表必须回退到会话起点并多留一天，否则窗口首根桶会被写成半截
    since_session = _session_floor(since_dt - timedelta(days=1))

    with session_scope() as s:
        for t, _ in _BARS:
            r = s.execute(sqltext(
                f"DELETE FROM l1_mkt.{t} WHERE bucket >= :c AND {_INDEX_SYM_PRED}"),
                {"c": since_session})
            print(f"[3] DELETE l1_mkt.{t} 指数桶 {r.rowcount} 行")
        for t, iv in _BARS:
            since_t = since_session if t in _SESSION_BARS else since_dt
            n = _synth_one(s, t, iv, since_t, _UPSERT_TAIL, None, layer="index")
            print(f"[3] {t} 重建 {n} 行（since={since_t:%Y-%m-%d %H:%M}）")
        s.commit()

    # ---------- 验收：不得再有「整天恒值」的指数日 ----------
    c2 = _connect()
    k = c2.cursor()
    k.execute("""
        SELECT d, count(*) AS n_flat FROM (
            SELECT symbol, ts::date AS d, count(DISTINCT close) AS nc, count(*) AS n
            FROM l0_raw.minute_bar WHERE layer='index' AND ts >= %s
            GROUP BY 1,2
        ) x WHERE nc = 1 AND n > 20 GROUP BY d ORDER BY d
    """, (a.since,))
    left = k.fetchall()
    print(f"[验收] 仍存在的「整天恒值」指数日：{left if left else '无'}")
    c2.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
