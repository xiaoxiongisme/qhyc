# -*- coding: utf-8 -*-
"""字典重建（akshare 权威口径）：品种规格/官方中文名/交易所/保证金/固定值手续费。

真源分工（2026-10-03 实测 87x81 品种交叉比对，用户已裁定）：
  akshare futures_fees_info（合约级）权威 —— 合约乘数、最小跳动、官方中文名、交易所、
    做多/做空保证金、固定值手续费(元/手)。理由：品种内乘数与跳动 100% 内部一致。
  交易所通知（db/seed/dim_trading_cost_seed.csv）权威 —— **百分比(‰)手续费**。
    理由：akshare 对 40 个百分比品种 开仓费用/手 恒为 0.01（≈免费），费率列
    0.000101/0.000051 与通知反推偏离 2~20 倍且无固定比例；误用会让 49% 品种
    手续费变近零 -> 回测成本严重低估。
  4 处固定值冲突（AP平今/FG开仓/EB开仓/PK开仓）以 akshare 为准。
  品种宇宙 = akshare 交集 87；LR(晚籼稻) 仅在通知中，按用户裁定不纳入。

用法：python scripts/rebuild_dict_from_akshare.py [--apply] [--no-refresh]
"""
from __future__ import annotations

import argparse
import ast
import csv
import os

import pandas as pd
from sqlalchemy import text

from app.core.db import session_scope

BROKER_MARKUP = 0.01
NOTICE_CSV = os.path.join("db", "seed", "dim_trading_cost_seed.csv")
AK_SNAPSHOT = os.path.join("db", "seed", "ak_futures_fees_variety.csv")
APPLY_DAY = "2026-10-03"


def _nums(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return []
    s = str(v).strip()
    if s.startswith("["):
        try:
            return [float(x) for x in ast.literal_eval(s)]
        except Exception:
            return []
    try:
        return [float(s)]
    except Exception:
        return []


def fetch_akshare(refresh=True):
    """取 akshare **合约级**原始数据。

    快照存的是原始合约级（不是品种级聚合），因为 build_varieties 依赖
    交易所/品种代码/品种名称/合约乘数 等原始列做 groupby。
    """
    if not refresh and os.path.exists(AK_SNAPSHOT):
        return pd.read_csv(AK_SNAPSHOT, encoding="utf-8-sig")
    import akshare as ak
    df = ak.futures_fees_info()
    df["更新时间"] = df["更新时间"].astype(str)
    n0 = len(df)
    df = df[~df["品种代码"].astype(str).str.lower().str.endswith("_f")].copy()
    print(f"[akshare] 原始 {n0} 行 -> 过滤伪码后 {len(df)} 行")
    os.makedirs(os.path.dirname(AK_SNAPSHOT) or ".", exist_ok=True)
    df.to_csv(AK_SNAPSHOT, index=False, encoding="utf-8-sig")
    print(f"[akshare] 合约级快照已存 {AK_SNAPSHOT}")
    return df


def build_varieties(raw):
    out = {}
    for (ex, code, name), g in raw.groupby(["交易所", "品种代码", "品种名称"]):
        key = str(code).strip().upper()
        u = lambda c: sorted(set(g[c].dropna().round(6)))
        mult, tick = u("合约乘数"), u("最小跳动")
        mrl, mrs = u("做多保证金率"), u("做空保证金率")
        mpl = sorted(set(g["做多保证金/手"].dropna().round(4)))
        of, cf, tf = u("开仓费用/手"), u("平仓费用/手"), u("平今费用/手")
        rec = {"variety_code": key, "exchange": str(ex).strip().upper(),
               "official_name": str(name).strip(), "n_contracts": len(g),
               "asof": g["更新时间"].max(),
               "multiplier": mult[0] if mult else None,
               "tick_size": tick[0] if tick else None,
               "mult_conflict": mult if len(mult) > 1 else None,
               "tick_conflict": tick if len(tick) > 1 else None,
               "margin_base": mrl[0] if mrl else None,
               "margin_min": mrl[0] if mrl else None,
               "margin_max": mrl[-1] if mrl else None,
               "margin_rate_short": mrs[0] if mrs else None,
               "margin_per_lot_long": mpl[0] if mpl else None,
               "ak_open_fee": of, "ak_close_fee": cf, "ak_ct_fee": tf,
               "ak_is_pct": bool(of and of[0] <= BROKER_MARKUP + 1e-9)}
        if key in out:
            out[key]["n_contracts"] += rec["n_contracts"]
        else:
            out[key] = rec
    return out


def load_notice_pct():
    if not os.path.exists(NOTICE_CSV):
        print(f"[通知] WARN 缺 {NOTICE_CSV}")
        return {}
    out = {}
    with open(NOTICE_CSV, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if r.get("instrument_kind") != "FUTURE" or r.get("scope_kind") != "ALL":
                continue
            if r.get("fee_type") != "PCT":
                continue
            d = out.setdefault(r["variety_code"].upper(), {})
            if r["action"] == "OPEN":
                d["open_pct"] = float(r["fee_value"])
            elif r["action"] == "CLOSE_TODAY":
                d["ct_pct"] = float(r["fee_value"])
    return out


def report(varieties, notice):
    with session_scope() as s:
        cur = {r[0]: r for r in s.execute(text(
            "SELECT variety_code, multiplier, tick_size, variety_name "
            "FROM dim_variety WHERE is_active")).fetchall()}
    dm, dt, nv, nf, pn = [], [], [], [], []
    for vc, r in sorted(varieties.items()):
        if r.get("mult_conflict"):
            print(f"  [{vc}] 品种内乘数不一致: {r['mult_conflict']}")
        if r.get("tick_conflict"):
            print(f"  [{vc}] 品种内跳动不一致: {r['tick_conflict']}")
        old = cur.get(vc)
        if not old:
            nv.append(vc)
        else:
            if old[1] is not None and r.get("multiplier") is not None and abs(float(old[1]) - r["multiplier"]) > 1e-9:
                dm.append((vc, old[1], r["multiplier"], r["official_name"]))
            if old[2] is not None and r.get("tick_size") is not None and abs(float(old[2]) - r["tick_size"]) > 1e-9:
                dt.append((vc, old[2], r["tick_size"], r["official_name"]))
            if not old[3]:
                nf.append((vc, r["official_name"]))
        if r.get("ak_is_pct") and vc not in notice:
            pn.append(vc)
    print(f"\n新增品种 {len(nv)}: {nv}")
    print(f"\n[*] 乘数变更 {len(dm)}:")
    for x in dm:
        print(f"    {x[0]:<5}{x[3]:<14}{x[1]} -> {x[2]}")
    print(f"\n[*] 跳动变更 {len(dt)}:")
    for x in dt:
        print(f"    {x[0]:<5}{x[3]:<14}{x[1]} -> {x[2]}")
    print(f"\n[*] 补简称(variety_name) {len(nf)}:")
    for x in nf:
        print(f"    {x[0]:<5}-> {x[1]}")
    print(f"\n[WARN] akshare 判为 permille 但通知无费率 {len(pn)}: {pn}")


def apply_db(varieties):
    """只写**规格类**字段（乘数/跳动/中文名/交易所/保证金）。

    ⚠ 2026-10-03 起**不再写手续费** —— 实测 akshare `futures_fees_info` 的费率不可作权威：
      · CZCE 26 品种固定值与郑交所官方接口大面积不符（FG 2 vs 6、PK 2 vs 4、
        AP平今 10 vs 20、CJ 3 vs 10、SR 2 vs 3、SA/MA/PX 0.01 vs 官方值）
      · 40 个百分比品种 `开仓费用/手` 恒为 0.01（≈免费）
    手续费改由 ``scripts/sync_cost_from_exchange.py`` 按交易所分源写入。
    保留 akshare 快照仅作交叉参考。
    """
    up = 0
    with session_scope() as s:
        for vc, r in varieties.items():
            p = {"v": vc, "nm": r["official_name"], "ex": r["exchange"],
                 "mul": r.get("multiplier"), "tick": r.get("tick_size"),
                 "mrl": r.get("margin_base"), "mrs": r.get("margin_rate_short"),
                 "mpl": r.get("margin_per_lot_long"),
                 "mmin": r.get("margin_min"), "mmax": r.get("margin_max")}
            if s.execute(text("SELECT 1 FROM dim_variety WHERE variety_code=:v"),
                         {"v": vc}).first():
                s.execute(text(
                    "UPDATE dim_variety SET official_name=:nm, "
                    " variety_name=COALESCE(variety_name,:nm), "
                    " exchange_name_src=CASE WHEN variety_name IS NULL THEN 'akshare' "
                    " ELSE COALESCE(exchange_name_src,'akshare') END, "
                    " exchange=COALESCE(exchange,:ex), multiplier=:mul, tick_size=:tick, "
                    " margin_rate_long=:mrl, margin_rate_short=:mrs, "
                    " margin_per_lot_long=:mpl, margin_min_rate=:mmin, "
                    " margin_max_rate=:mmax, updated_at=now() WHERE variety_code=:v"), p)
            else:
                s.execute(text(
                    "INSERT INTO dim_variety (variety_code, variety_name, official_name, "
                    " exchange, "
                    " multiplier, tick_size, margin_rate_long, margin_rate_short, "
                    " margin_per_lot_long, margin_min_rate, margin_max_rate, "
                    " is_active, source, exchange_name_src) VALUES "
                    " (:v,:nm,:nm,:ex,:mul,:tick,:mrl,:mrs,:mpl,:mmin,:mmax,true,"
                    " 'akshare_fees_info','akshare')"), p)
            up += 1
    print(f"\n[done] dim_variety 更新/新增 {up} 行")

    ins = skip = 0
    with session_scope() as s:
        for vc, r in varieties.items():
            if r.get("ak_is_pct"):
                continue
            for act, key in (("OPEN", "ak_open_fee"), ("CLOSE_YEST", "ak_close_fee"),
                             ("CLOSE_TODAY", "ak_ct_fee")):
                vals = r.get(key) or []
                if not vals:
                    continue
                row = s.execute(text(
                        "SELECT fee_value, source FROM dim_trading_cost "
                        "WHERE variety_code=:v "
                        "AND instrument_kind='FUTURE' AND action=:a AND scope_kind='ALL' "
                        "AND effective_to IS NULL"), {"v": vc, "a": act}).first()
                if row is not None:
                    # 固定值手续费以 akshare 为权威（用户 2026-10-03 裁定）：
                    # 已有 scope=ALL 行时覆盖其值，否则交易所通知的值会残留
                    # （含已裁定按 akshare 的 4 处冲突）。CONTRACTS/MONTHS 行不动。
                    _exv = round(max(vals) - BROKER_MARKUP, 6)
                    _nt = f"akshare {r['asof']} auth (net of 0.01 broker)"
                    if row[0] is not None and abs(float(row[0]) - _exv) > 1e-9:
                        _nt += f"; overwrote {row[0]} from {row[1]}"
                    s.execute(text(
                        "UPDATE dim_trading_cost SET fee_type=:ft, fee_value=:fv, "
                        " exchange_fee_value=:fv, broker_markup_type='FIXED', "
                        " broker_markup_value=:bm, slip_ticks=1, "
                        " source='akshare_fees_info', note=:nt, updated_at=now() "
                        "WHERE variety_code=:v AND instrument_kind='FUTURE' AND action=:a "
                        "AND scope_kind='ALL' AND effective_to IS NULL"),
                        {"ft": "FREE" if _exv <= 1e-9 else "FIXED", "fv": _exv,
                         "bm": BROKER_MARKUP, "nt": _nt, "v": vc, "a": act})
                    skip += 1
                    continue
                exv = round(max(vals) - BROKER_MARKUP, 6)
                s.execute(text(
                    "INSERT INTO dim_trading_cost (exchange, variety_code, "
                    " instrument_kind, action, scope_kind, fee_type, fee_value, "
                    " exchange_fee_value, broker_markup_type, broker_markup_value, "
                    " slip_ticks, effective_from, source, note) VALUES "
                    " (:ex,:v,'FUTURE',:a,'ALL',:ft,:fv,:fv,'FIXED',:bm,1,"
                    " CAST(:ed AS DATE),'akshare_fees_info',:note) "
                    "ON CONFLICT ON CONSTRAINT uq_dim_trading_cost DO NOTHING"),
                    {"ex": r["exchange"], "v": vc, "a": act,
                     "ft": "FREE" if exv <= 1e-9 else "FIXED", "fv": exv,
                     "bm": BROKER_MARKUP, "ed": APPLY_DAY,
                     "note": f"akshare {r['asof']} 权威口径（已扣券商 1 分）"})
                ins += 1
    print(f"[done] dim_trading_cost 固定值新增 {ins} 行；已有有效行跳过 {skip} 行")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-refresh", action="store_true")
    args = ap.parse_args()
    varieties = build_varieties(fetch_akshare(not args.no_refresh))
    notice = load_notice_pct()
    print(f"[汇总] akshare 品种 {len(varieties)}；通知含 permille 费率 {len(notice)}")
    report(varieties, notice)
    if not args.apply:
        print("\n[dry-run] --apply 才落库")
        return 0
    apply_db(varieties)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
