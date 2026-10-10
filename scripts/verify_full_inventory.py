#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""全量盘点：枚举云端/本地全部物理表，逐表对账行数与时间覆盖。
重点回答：
  Q1 云端行情/因子/字典/配置各层是否齐全？
  Q2 本地是否包含云端所有数据（本地 >= 云端）？
输出：文本报告 + 写入 markdown 文件。
"""
import psycopg2, io, sys, os, datetime as _dt

PW = "qhyc_dev_pwd_2026"; DB = "futures"; USER = "futures"
CLOUD = dict(host="127.0.0.1", port=15432, user=USER, password=PW, dbname=DB)
LOCAL = dict(host="127.0.0.1", port=5432, user=USER, password=PW, dbname=DB)
SCHEMAS = ["l0_raw", "l1_mkt", "l2_adj", "l3_ref"]
T_CAND = ["ts", "bucket", "trade_datetime", "trade_date", "seg_start",
          "seg_end", "report_date", "datetime", "date", "time", "day", "ts_start"]

LAYER_OF = {
    "l0_raw": "L0原始层", "l1_mkt": "L1行情聚合层",
    "l2_adj": "L2调整/衍生层", "l3_ref": "L3参考/维度层",
}
# 表 -> 分类（用于 Q1 报告）
CATEGORY = {}
def cat(schema, tbl):
    if schema == "l0_raw":
        return "行情/原始"
    if schema == "l1_mkt":
        return "行情聚合"
    if schema == "l2_adj":
        if tbl.startswith("factor") or tbl in ("model_weights","prediction_result","transmission_weights","sector_index"):
            return "因子"
        return "调整/衍生"
    if schema == "l3_ref":
        if tbl.startswith("dim") or "map" in tbl or tbl.startswith("main_contract"):
            return "字典/维度"
        if tbl.startswith("cfg") or tbl in ("data_layer_catalog","trade_calendar"):
            return "配置"
        return "参考"
    return "?"


def conn(cfg):
    c = psycopg2.connect(**cfg)
    c.set_session(autocommit=True)
    c.cursor().execute("SET statement_timeout=180000")
    return c


def list_tables(cur, schema):
    cur.execute(
        "SELECT schemaname, tablename FROM pg_tables "
        "WHERE schemaname=%s ORDER BY tablename", (schema,))
    return [(s, t) for s, t in cur.fetchall()]


def detect_tcol(cur, schema, table):
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name=%s", (schema, table))
    cols = [r[0] for r in cur.fetchall()]
    for cand in T_CAND:
        if cand in cols:
            return cand, cols
    return None, cols


def get_count(cur, schema, table):
    cur.execute(f'SELECT count(*) FROM {schema}."{table}"')
    return cur.fetchone()[0]


def get_trange(cur, schema, table, tcol):
    cur.execute(f'SELECT min({tcol}), max({tcol}) FROM {schema}."{table}"')
    return cur.fetchone()


def year_dist(cur, schema, table, tcol):
    cur.execute(
        f"SELECT EXTRACT(YEAR FROM ({tcol})::timestamp)::int yr, count(*) "
        f"FROM {schema}.\"{table}\" GROUP BY yr ORDER BY yr")
    return {int(y): c for y, c in cur.fetchall()}


def main():
    ccl = conn(CLOUD); clc = conn(LOCAL)
    ccur = ccl.cursor(); lcur = clc.cursor()

    # 枚举两端表
    cloud_tabs = {}   # (schema,name) -> True
    local_tabs = {}
    for sch in SCHEMAS:
        for s, t in list_tables(ccur, sch):
            cloud_tabs[(s, t)] = True
        for s, t in list_tables(lcur, sch):
            local_tabs[(s, t)] = True

    all_tabs = sorted(set(cloud_tabs) | set(local_tabs))
    out = []
    out.append("# 全量盘点：云端 vs 本地 逐表对账")
    out.append(f"生成时间：{_dt.datetime.now():%Y-%m-%d %H:%M:%S}")
    out.append("")

    # 分类统计（Q1 云端齐全性）
    cat_cloud_missing = {}   # category -> list of (schema,table) missing in cloud (should be none)
    cat_cloud_present = {}
    rows = []
    for (s, t) in all_tabs:
        in_cloud = (s, t) in cloud_tabs
        in_local = (s, t) in local_tabs
        c_cnt = l_cnt = None
        c_range = l_range = None
        c_yd = l_yd = None
        tcol = None
        if in_cloud:
            try:
                c_cnt = get_count(ccur, s, t)
                tcol, _ = detect_tcol(ccur, s, t)
                if tcol:
                    c_range = get_trange(ccur, s, t, tcol)
            except Exception as e:
                c_cnt = f"ERR:{e}"[:60]
        if in_local:
            try:
                l_cnt = get_count(lcur, s, t)
                if tcol is None:
                    tcol, _ = detect_tcol(lcur, s, t)
                if tcol:
                    l_range = get_trange(lcur, s, t, tcol)
            except Exception as e:
                l_cnt = f"ERR:{e}"[:60]
        catn = cat(s, t)
        rows.append((s, t, catn, in_cloud, in_local, c_cnt, l_cnt, tcol, c_range, l_range))

    # ---- Q1：云端齐全性（按分类）----
    out.append("## Q1 云端各层齐全性（按分类）")
    out.append("")
    by_cat = {}
    for (s, t, catn, ic, il, cc, lc, tc, cr, lr) in rows:
        if ic:
            by_cat.setdefault(catn, [0, 0])  # [present, total_in_cloud]
            by_cat[catn][0] += 1
            by_cat[catn][1] += 1
    for catn in sorted(by_cat):
        p, tot = by_cat[catn]
        out.append(f"- **{catn}**：云端 {p} 张表")
    out.append("")

    # ---- Q2：本地 >= 云端？----
    out.append("## Q2 本地是否包含云端所有数据")
    out.append("")
    out.append("| schema | 表 | 分类 | 云端 | 本地 | 时间列 | 云端范围 | 本地范围 | 判定 |")
    out.append("|---|---|---|---:|---:|---|---|---|---|")
    gap_local_missing = []      # 云端有本地无
    gap_local_shorter = []      # 本地有但行数 < 云端
    only_local = []             # 本地有云端无
    for (s, t, catn, ic, il, cc, lc, tc, cr, lr) in rows:
        verdict = ""
        if ic and not il:
            gap_local_missing.append((s, t, catn, cc))
            verdict = "❌云端有本地无"
        elif il and not ic:
            only_local.append((s, t, catn, lc))
            verdict = "⚠️本地独有"
        elif ic and il:
            # 比较行数
            try:
                cc_n = int(cc) if isinstance(cc, int) else None
                lc_n = int(lc) if isinstance(lc, int) else None
            except Exception:
                cc_n = lc_n = None
            if cc_n is not None and lc_n is not None:
                if lc_n < cc_n:
                    gap_local_shorter.append((s, t, catn, cc_n, lc_n, cc_n - lc_n))
                    verdict = f"❌本地少 {cc_n-lc_n:,}"
                elif lc_n == cc_n:
                    verdict = "✅一致"
                else:
                    verdict = f"✅本地多 {lc_n-cc_n:,}"
            else:
                verdict = "⚠️计数异常"
        c_rng = f"{cr[0]}~{cr[1]}" if cr and cr[0] else "-"
        l_rng = f"{lr[0]}~{lr[1]}" if lr and lr[0] else "-"
        out.append(f"| {s} | {t} | {catn} | {cc} | {lc} | {tc or '-'} | {c_rng} | {l_rng} | {verdict} |")

    out.append("")
    out.append("## 缺口汇总")
    out.append("")
    out.append(f"### A. 云端有而本地无（{len(gap_local_missing)} 张）")
    for s, t, catn, cc in gap_local_missing:
        out.append(f"- {s}.{t} [{catn}] 云端 {cc:,} 行")
    out.append("")
    out.append(f"### B. 本地行数 < 云端（{len(gap_local_shorter)} 张）")
    for s, t, catn, cc, lc, d in gap_local_shorter:
        out.append(f"- {s}.{t} [{catn}] 云端 {cc:,} / 本地 {lc:,} / 差 {d:,}")
    out.append("")
    out.append(f"### C. 本地独有（云端无，{len(only_local)} 张）")
    for s, t, catn, lc in only_local:
        out.append(f"- {s}.{t} [{catn}] 本地 {lc:,} 行")
    out.append("")

    txt = "\n".join(out)
    print(txt)
    # 同时写文件
    report = r"E:\抖音分析\2026-09-11-10-49-23\qhyc_全量盘点_20261008.md"
    with open(report, "w", encoding="utf-8") as f:
        f.write(txt)
    print(f"\n[written] {report}")
    ccl.close(); clc.close()


if __name__ == "__main__":
    main()
