# -*- coding: utf-8 -*-
"""PRD §3 模块① 播种：cfg_symbol_margin（保证金）+ cfg_trading_session（交易时段）。

数据源
------
* 保证金：``D:\\学习资料\\20260324期货品种参数汇总表.xls``
  —— 含「交易所保证金 / 公司保证金」，且**同一品种按 D1/D2/D3 停板分段**多行。
* 交易时段：akshare ``futures_trading_time``（期货专属，非股票）。

用法：
    python scripts/seed_cfg_module1.py            # dry-run
    python scripts/seed_cfg_module1.py --apply
"""
from __future__ import annotations

import argparse
import os
import re
import sys

import pandas as pd
from sqlalchemy import text

from app.core.db import session_scope

sys.stdout.reconfigure(encoding="utf-8")

XLS = r"D:\学习资料\20260324期货品种参数汇总表.xls"
#: 容器内读不到宿主机的 D:\\学习资料，故由宿主机侧脚本
#: ``scripts/_gen_margin_seed.py`` 预先导出成仓库内 CSV，容器只读 CSV。
CSV = os.path.join("db", "seed", "cfg_symbol_margin_seed.csv")


def _num(x):
    m = re.sub(r"[^0-9.]", "", str(x))
    try:
        return float(m) if m else None
    except ValueError:
        return None


def read_margin_from_xls() -> dict[str, dict]:
    """读权威汇总表，返回 {品种码: {exch, broker, d2, d3}}（取该品种第一行，即 D1）。"""
    if not os.path.exists(XLS):
        print(f"[warn] 找不到 {XLS}，跳过保证金播种")
        return {}
    raw = pd.read_excel(XLS, dtype=str, header=None).fillna("")
    body = raw.iloc[2:].copy()
    body[0] = body[0].replace("", None).ffill()
    body[3] = body[3].replace("", None).ffill()

    # 交易所保证金列
    cols = [str(x).replace("\xa0", " ").strip() for x in raw.iloc[1].tolist()]
    try:
        mi = next(i for i, c in enumerate(cols) if "交易所保证金" in c)
    except StopIteration:
        print("[warn] 汇总表无「交易所保证金」列")
        return {}

    out: dict[str, dict] = {}
    for _, r in body.iterrows():
        code = str(r[3] or "").strip().upper()
        if not code or code == "代码":
            continue
        if code in out:
            continue                      # 只取该品种首行 = D1 段
        vals = [_num(r[mi + k]) if mi + k < len(cols) else None
                for k in range(MARGIN_COLS)]
        if vals[0] is None:
            continue
        # 汇总表是小数（0.13）；若 >=1 视为百分比误写，归一到小数
        def _norm(v):
            return v / 100.0 if (v is not None and v > 1) else v
        out[code] = {"exch": _norm(vals[0]), "broker": _norm(vals[1]),
                     "d2": _norm(vals[2]), "d3": None}
    return out


def read_sessions_from_akshare() -> list[tuple]:
    """akshare 期货交易时间表 → (exchange, variety, type, start, end)。"""
    rows: list[tuple] = []
    try:
        import akshare as ak
        df = ak.futures_trading_time()
    except Exception as e:  # noqa: BLE001
        print(f"[warn] futures_trading_time 不可用：{e}")
        return rows
    # 列：品种/交易所/交易时间(夜盘)/交易时间(日盘)...
    for _, r in df.iterrows():
        try:
            name = str(r.iloc[0])
            night = str(r.get("夜盘交易时间", "") or "")
            day = str(r.get("日盘交易时间", "") or "")
            for kind, seg in (("night", night), ("day", day)):
                if not seg or seg.lower() in ("nan", "none", "-"):
                    continue
                m = re.findall(r"(\d{1,2}:\d{2})\s*[-~至]\s*(\d{1,2}:\d{2})", seg)
                for s, e in m:
                    nxt = False
                    if kind == "night" and s >= "20:00":
                        nxt = True        # 夜盘 21:00 起 → 归属次日
                    rows.append(("", name, kind, s, e, nxt))
        except Exception:  # noqa: BLE001
            continue
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    margins = read_margin_from_xls()
    if not margins and os.path.exists(CSV):
        import csv as _csv
        with open(CSV, encoding="utf-8-sig") as f:
            for r in _csv.DictReader(f):
                def _f(k):
                    v = (r.get(k) or "").strip()
                    try:
                        return float(v) if v else None
                    except ValueError:
                        return None
                if r.get("variety_code"):
                    margins[r["variety_code"].upper()] = {
                        "exch": _f("exchange_margin_ratio"),
                        "broker": _f("broker_margin_ratio"),
                        "d2": _f("margin_d2"), "d3": None}
        print(f"[input] 由种子 CSV 读入保证金 {len(margins)} 个品种（{CSV}）")
    else:
        print(f"[input] 汇总表保证金品种 {len(margins)} 个")

    if not a.apply:
        print("[dry-run] --apply 才落库")
        for c in list(sorted(margins))[:8]:
            print("   %-5s exch=%-8s broker=%-8s d2=%s"
                  % (c, margins[c]["exch"], margins[c]["broker"], margins[c]["d2"]))
        return 0

    n = 0
    with session_scope() as s:
        for code, m in margins.items():
            s.execute(text(
                "INSERT INTO cfg_symbol_margin "
                " (variety_code, exchange_margin_ratio, broker_margin_ratio, "
                "  margin_d2, margin_d3, source, note) "
                "VALUES (:c, :e, :b, :d2, :d3, :src, :note) "
                "ON CONFLICT (variety_code) DO UPDATE SET "
                " exchange_margin_ratio = EXCLUDED.exchange_margin_ratio, "
                " broker_margin_ratio = EXCLUDED.broker_margin_ratio, "
                " margin_d2 = EXCLUDED.margin_d2, margin_d3 = EXCLUDED.margin_d3, "
                " source = EXCLUDED.source, note = EXCLUDED.note, updated_at = now()"
            ), {"c": code, "e": m["exch"], "b": m["broker"], "d2": m["d2"],
                "d3": m["d3"], "src": "汇总表20260324",
                "note": "D1 段；D2/D3 为连续同方向停板次日分段"})
            n += 1
        print(f"[done] cfg_symbol_margin 写入 {n} 行")

        # 顺带把 dim_variety.margin_rate_long 对齐（若该列存在）
        try:
            r = s.execute(text(
                "UPDATE dim_variety dv SET margin_rate_long = m.exchange_margin_ratio, "
                "updated_at = now() FROM cfg_symbol_margin m "
                "WHERE dv.variety_code = m.variety_code "
                "  AND m.exchange_margin_ratio IS NOT NULL "
                "  AND dv.margin_rate_long IS DISTINCT FROM m.exchange_margin_ratio"
            ))
            print(f"[done] dim_variety.margin_rate_long 对齐 {r.rowcount} 行")
        except Exception as e:  # noqa: BLE001
            print(f"[info] dim_variety 保证金列对齐跳过：{str(e)[:80]}")

    # ---- cfg_trading_session：优先用宿主机导出的种子 CSV ----
    sess_csv = os.path.join("db", "seed", "cfg_trading_session_seed.csv")
    n_sess = 0
    if os.path.exists(sess_csv):
        import csv as _csv
        with session_scope() as s, open(sess_csv, encoding="utf-8-sig") as f:
            for r in _csv.DictReader(f):
                s.execute(text(
                    "INSERT INTO cfg_trading_session "
                    " (exchange, variety_code, session_type, start_time, end_time, "
                    "  next_day_flag, source) VALUES (:e,:v,:t,:s,:en,:n,:src) "
                    "ON CONFLICT (exchange, COALESCE(variety_code,'*'), "
                    "            session_type, start_time) DO NOTHING"),
                    {"e": r["exchange"], "v": r["variety_code"] or None,
                     "t": r["session_type"], "s": r["start_time"], "en": r["end_time"],
                     "n": (r["next_day_flag"] or "").lower() == "true",
                     "src": "汇总表20260324"})
                n_sess += 1
        print(f"[done] cfg_trading_session 载入 {n_sess} 条（日盘+夜盘，来源 汇总表）")
    else:
        sess = read_sessions_from_akshare()
        print(f"[info] 无种子 CSV，akshare 交易时段条目 {len(sess)}；"
              f"本版本无 futures_trading_time 接口，cfg_trading_session 待补")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
