#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qhyc 数据层 L0-L3 云端↔本地 全层比对（流式打印，单查询超时兜底）。只读。"""
import psycopg2, sys

PW = "qhyc_dev_pwd_2026"; DB = "futures"; USER = "futures"
LAYERS = [("L0", "l0_raw", "原始层"), ("L1", "l1_mkt", "行情聚合层"),
          ("L2", "l2_adj", "调整/衍生层"), ("L3", "l3_ref", "参考/维度层")]
ENT_CAND = ["symbol", "variety", "product", "contract_code", "exchange", "main_symbol"]
T_CAND   = ["ts", "bucket", "trade_datetime", "trade_date", "seg_start"]

def conn(port):
    c = psycopg2.connect(host="127.0.0.1", port=port, dbname=DB, user=USER,
                         password=PW, connect_timeout=8)
    c.set_session(autocommit=True)
    c.cursor().execute("SET statement_timeout = 120000")
    return c

def get_tables(cur, schema):
    cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema=%s AND table_type='BASE TABLE' ORDER BY table_name", (schema,))
    return [r[0] for r in cur.fetchall()]

def get_cols(cur, schema, table):
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s", (schema, table))
    return [r[0] for r in cur.fetchall()]

def fmt_ts(v):
    if v is None: return "-"
    if hasattr(v, "strftime"): return v.strftime("%Y-%m-%d")
    return str(v)[:10]

def metrics(cur, schema, table, cols):
    ent = next((c for c in ENT_CAND if c in cols), None)
    tcol = next((c for c in T_CAND if c in cols), None)
    try:
        cur.execute(f"SELECT count(*) FROM {schema}.{table}"); rows = cur.fetchone()[0]
    except Exception as e:
        return f"ERR:{type(e).__name__}", None, "-", "-"
    sym = None
    if ent:
        try:
            cur.execute(f"SELECT count(DISTINCT {ent}) FROM {schema}.{table}"); sym = cur.fetchone()[0]
        except Exception: sym = None
    tmin = tmax = None
    if tcol:
        try:
            if tcol == "seg_start" and "seg_end" in cols:
                cur.execute(f"SELECT min(seg_start), max(seg_end) FROM {schema}.{table}")
            else:
                cur.execute(f"SELECT min({tcol}), max({tcol}) FROM {schema}.{table}")
            tmin, tmax = cur.fetchone()
        except Exception: tmin = tmax = "ERR"
    return rows, sym, fmt_ts(tmin), fmt_ts(tmax)

def year_dist(cur, schema, t, tcol):
    expr = "EXTRACT(YEAR FROM seg_start)" if (t == "roll_segment") else f"EXTRACT(YEAR FROM {tcol})"
    try:
        cur.execute(f"SELECT {expr}::int y, count(*) FROM {schema}.{t} GROUP BY y ORDER BY y")
        return "  ".join(f"{y}:{n:,}" for y, n in cur.fetchall())
    except Exception as e:
        return "ERR " + type(e).__name__

def main():
    local = conn(5432); lcur = local.cursor()
    cloud = conn(15432); ccur = cloud.cursor()
    print(f"{'表':<28}{'本地行':>14}{'云端行':>15}{'本地品':>8}{'云端品':>8}  {'本地覆盖':>13}{'云端覆盖':>14}", flush=True)
    for lid, schema, desc in LAYERS:
        tabs = get_tables(lcur, schema)
        print(f"\n===== {lid}  {schema}  ({desc})  本地表 {len(tabs)} 张 =====", flush=True)
        for t in tabs:
            lcols = get_cols(lcur, schema, t); ccols = get_cols(ccur, schema, t)
            lr, ls, lmn, lmx = metrics(lcur, schema, t, lcols)
            cr, cs, cmn, cmx = metrics(ccur, schema, t, ccols)
            print(f"{t:<28}{str(lr):>14}{str(cr):>15}{str(ls):>8}{str(cs):>8}  {f'{lmn}~{lmx}':>13}{f'{cmn}~{cmx}':>14}", flush=True)

    core = [("l0_raw","minute_bar","ts"), ("l1_mkt","bar_15m","bucket"),
            ("l0_raw","daily_bar","trade_date"), ("l0_raw","fut_kline","trade_datetime"),
            ("l2_adj","roll_segment","seg_start")]
    print("\n##### 云端按年行数（核心表，检缺年） #####", flush=True)
    for s,t,c in core: print(f"[{s}.{t}] " + year_dist(ccur, s, t, c), flush=True)
    print("\n##### 本地按年行数（核心表，看是否仅 2026+） #####", flush=True)
    for s,t,c in core: print(f"[{s}.{t}] " + year_dist(lcur, s, t, c), flush=True)
    local.close(); cloud.close()

if __name__ == "__main__":
    main()
