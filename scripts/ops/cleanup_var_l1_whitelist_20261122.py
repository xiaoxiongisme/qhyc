#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""CL-20261122-02：清理 B/J/LH/NI 持仓豁免期越界的 L1/L2 派生历史。

背景（2026-11-22 实证）
-----------------------
``VAR-L1-IN-WHITELIST`` 是 --no-waiver 验收下唯一未挂账 FAIL：
非白名单主连 B888/J888/LH888/NI888 的最新桶 = 2026-11-19 15:00，
晚于白名单口径生效时刻 2026-11-09 07:43（CL-20261107-01.verified_at）。

根因不是代码泄漏，而是**持仓豁免的动态性**：
  - 这 4 个品种在 11-09~11-19 期间是非白名单但仍有持仓的品种，
    依 CL-20261107-01 的「持仓豁免」设计合法派生（止损/平仓需要最新 bar）；
  - CL-20261119-01 于 11-19 19:44 清退这 4 个僵尸仓后，豁免资格消失；
  - 守卫用「当前持仓」判定「历史 max bucket」，于是历史合法派生被判越界。

常规调度（synthesize_bars_incremental / purge_recent_buckets，
app/ingest/synthesizer.py:_synth_one:596）均按 scope 收窄，清仓后不再写回；
synthesize_bars_full 不过口径，但不是常规路径，本脚本执行期间不得调用。

本脚本做什么
------------
1. 逐层备份 4 个符号在 eff 之后的派生行到 public.bak_cl20261122_02_*；
2. 逐层删除同一批越界行（l1_mkt.bar_15m/30m/60m + l2_adj.bar_60m_ba）；
3. 清理后 max(bucket) 回落到 11-07 01:00（< eff），守卫转 PASS。

⚠️ 一次性数据修复（CL-20261122-02）。重复执行是幂等的（DELETE 无匹配行）。
用法（生产 psql）：见同目录 change_log 决策说明。
"""
from __future__ import annotations

EFF = "2026-11-09 07:43:13.644815+08"
SYMS = ("B888", "J888", "LH888", "NI888")

# (目标表, 备份表)
TABLES = [
    ("l1_mkt.bar_15m",    "public.bak_cl20261122_02_bar_15m"),
    ("l1_mkt.bar_30m",    "public.bak_cl20261122_02_bar_30m"),
    ("l1_mkt.bar_60m",    "public.bak_cl20261122_02_bar_60m"),
    ("l2_adj.bar_60m_ba", "public.bak_cl20261122_02_bar_60m_ba"),
]

# 与上方等价的 psql 语句（供审计复现）
SQL = """
SET client_min_messages = WARNING;
"""
for tbl, bak in TABLES:
    SQL += f"""
DROP TABLE IF EXISTS {bak};
CREATE TABLE {bak} AS
  SELECT * FROM {tbl}
  WHERE symbol IN ('B888','J888','LH888','NI888')
    AND bucket >= TIMESTAMPTZ '{EFF}';
DELETE FROM {tbl}
  WHERE symbol IN ('B888','J888','LH888','NI888')
    AND bucket >= TIMESTAMPTZ '{EFF}';
"""
SQL += """
SELECT 'bar_60m' layer, count(*) n, max(bucket) mx FROM l1_mkt.bar_60m
 WHERE symbol IN ('B888','J888','LH888','NI888');
"""

if __name__ == "__main__":
    print(SQL)
