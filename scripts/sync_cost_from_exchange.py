# -*- coding: utf-8 -*-
"""交易成本按「交易所分源」同步（用户 2026-10-03 拍板）。

分源规则
--------
  CZCE  -> ak.futures_contract_info_czce()  郑交所官方接口：交易手续费 / 平今仓手续费 /
           最小变动价位 / 交易保证金率（26 品种全覆盖，含平今，权威度最高）
  其他所 -> 交易所手续费通知（D:\\学习资料 20260311 调整通知）db/seed/dim_trading_cost_seed.csv
           （DCE 官方接口 futures_contract_info_dce 实测 JSONDecodeError 不可用；
             SHFE 官方接口只有上市/到期日、无费率）
  akshare futures_fees_info -> **仅交叉参考，不写库**
           实测其 CZCE 固定值手续费与官方大面积不符（FG 2 vs 6、PK 2 vs 4、
           AP平今 10 vs 20、CJ 3 vs 10、SR 2 vs 3、SA 0.01 vs 2、MA 0.01 vs 1），
           且 40 个百分比品种费用恒为 0.01（≈免费）。

用法：python scripts/sync_cost_from_exchange.py [--apply] [--no-refresh]
"""
from __future__ import annotations

import argparse
import csv
import os
import re

from sqlalchemy import text

from app.core.db import session_scope

NOTICE_CSV = os.path.join("db", "seed", "dim_trading_cost_seed.csv")
CZ_SNAPSHOT = os.path.join("db", "seed", "czce_official_fees.csv")
BROKER_MARKUP = 0.01
APPLY_DAY = "2026-10-03"


def load_notice():
    if not os.path.exists(NOTICE_CSV):
        print("[WARN] no notice csv: %s" % NOTICE_CSV)
        return []
    with open(NOTICE_CSV, encoding="utf-8-sig") as f:
        return [r for r in csv.DictReader(f)
                if r.get("instrument_kind") == "FUTURE" and r.get("scope_kind") == "ALL"]


def fetch_czce(refresh=True):
    """郑交所官方：每个品种取其首个合约的 手续费/平今/最小变动价位/保证金。"""
    if not refresh and os.path.exists(CZ_SNAPSHOT):
        with open(CZ_SNAPSHOT, encoding="utf-8-sig") as f:
            return list(csv.DictReader(f))
    import akshare as ak
    df = ak.futures_contract_info_czce()
    out = {}
    for _, r in df.iterrows():
        code = str(r["产品代码"]).strip().upper()
        if code in out:
            continue

        def num(x):
            m = re.sub(r"[^0-9.]", "", str(x))
            try:
                return float(m) if m else None
            except ValueError:
                return None
        out[code] = {
            "variety_code": code,
            "official_name": str(r["产品名称"]).strip(),
            "tick": num(r["最小变动价位"]),
            "unit": str(r["交易单位"]),
            "open_fee": num(r["交易手续费"]),
            "ct_fee": num(r["平今仓手续费"]),
            "margin_rate": num(r["交易保证金率"]),
            # ★手续费收取方式：绝对值=固定元/手；比例值=按成交额‰。
            #   2026-10-03 实测：MA/PX/SA/SH/UR 为「比例值」，此前被一律当固定值写入是错的
            #   （把 1‰ 当成 1 元/手）。
            "basis": str(r["手续费收取方式"]).strip(),
        }
    rows = list(out.values())
    os.makedirs(os.path.dirname(CZ_SNAPSHOT) or ".", exist_ok=True)
    with open(CZ_SNAPSHOT, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print("[CZCE] official varieties: %d -> %s" % (len(rows), CZ_SNAPSHOT))
    return rows


def upsert(s, vc, ex, act, ft, fv, note, src):
    """按**滚动语义**写入：先闭合该键的旧「当前有效」行，再插入新行。

    ⚠ 2026-10-03 修正：原先只按 (vc,kind,action,scope,effective_from) 做 ON CONFLICT，
      而通知行(2026-03-11)与官方行(2026-10-03)生效日不同 → 两者都成为「有效」，
      造成同一品种动作出现两条口径（如 SA 同时有 PCT 2‰ 与 FIXED 4.0）。
      现改为：闭合旧行(effective_to=APPLY_DAY) + 插入新行，保证任一时刻每个键只有一条有效。
    """
    # ── 修复（2026-10-03）：同日重跑必须能「复活」被闭合的行 ──────────────
    # 场景：首轮写入 doc 行(effective_from=APPLY_DAY) → 018 去重把它闭合(同日起) →
    #       重跑时 INSERT 命中 uq_dim_trading_cost(键含 effective_from) 的
    #       ON CONFLICT DO NOTHING → 静默跳过 → 该键**永远没有有效行**
    #       （实测 BZ/EB/PL 的 CLOSE_TODAY 因此丢失，日内成本被按隔夜计）
    s.execute(text(
        "UPDATE dim_trading_cost SET effective_to = NULL, updated_at = now() "
        "WHERE variety_code = :v AND instrument_kind = 'FUTURE' AND action = :a "
        "  AND scope_kind = 'ALL' AND effective_from = CAST(:ed AS DATE) "
        "  AND effective_to IS NOT NULL"),
        {"v": vc, "a": act, "ed": APPLY_DAY})
    prev = s.execute(text(
        "SELECT fee_value, source, effective_from FROM dim_trading_cost "
        "WHERE variety_code=:v AND instrument_kind='FUTURE' AND action=:a "
        "AND scope_kind='ALL' AND effective_to IS NULL "
        "  AND effective_from < CAST(:ed AS DATE)"),
        {"v": vc, "a": act, "ed": APPLY_DAY}).fetchall()
    s.execute(text(
        "UPDATE dim_trading_cost SET effective_to = CAST(:ed AS DATE), updated_at=now() "
        "WHERE variety_code=:v AND instrument_kind='FUTURE' AND action=:a "
        "AND scope_kind='ALL' AND effective_to IS NULL "
        "  AND effective_from < CAST(:ed AS DATE)"),
        {"v": vc, "a": act, "ed": APPLY_DAY})
    s.execute(text(
        "INSERT INTO dim_trading_cost (exchange, variety_code, instrument_kind, action, "
        " scope_kind, fee_type, fee_value, exchange_fee_value, broker_markup_type, "
        " broker_markup_value, slip_ticks, effective_from, source, note) VALUES "
        " (:ex,:v,'FUTURE',:a,'ALL',:ft,:fv,:fv,'FIXED',:bm,1,CAST(:ed AS DATE),:src,:nt) "
        "ON CONFLICT ON CONSTRAINT uq_dim_trading_cost DO UPDATE SET "
        " fee_type=EXCLUDED.fee_type, fee_value=EXCLUDED.fee_value, "
        " exchange_fee_value=EXCLUDED.exchange_fee_value, "
        " broker_markup_type=EXCLUDED.broker_markup_type, "
        " broker_markup_value=EXCLUDED.broker_markup_value, slip_ticks=1, "
        " source=EXCLUDED.source, note=EXCLUDED.note, updated_at=now()"),
        {"ex": ex, "v": vc, "a": act, "ft": ft, "fv": fv, "bm": BROKER_MARKUP,
         "ed": APPLY_DAY, "src": src, "nt": note})
    return prev


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--no-refresh", action="store_true")
    args = ap.parse_args()

    notice = load_notice()
    czce = fetch_czce(not args.no_refresh)
    print("[input] notice rows(scope=ALL,FUTURE)=%d  czce official=%d"
          % (len(notice), len(czce)))

    n_upd = 0
    with session_scope() as s:
        for r in notice:
            vc = r["variety_code"].upper()
            ex = r["exchange"]
            for act in ("OPEN", "CLOSE_YEST", "CLOSE_TODAY"):
                if act == "CLOSE_YEST":
                    ftype, fval = r["fee_type"], r["fee_value"]   # 平昨与开仓同口径
                elif act == "OPEN":
                    ftype, fval = r["fee_type"], r["fee_value"]
                else:
                    continue                                    # 平今只由官方/通知续行提供
                if ftype == "PCT":
                    continue                                     # 百分比不在此覆盖
                fv = float(fval or 0)
                prev = upsert(s, vc, ex, act, ftype, fv,
                              "exchange_notice 20260311", "exchange_notice_20260311")
                if prev and prev[0] and prev[0][1] and "akshare" in str(prev[0][1]):
                    n_upd += 1
    print("[done] notice-sourced rows restored: %d" % n_upd)

    n_cz = 0
    with session_scope() as s:
        # ---- BZ/EB/PL：用户指定第三方文档基准（2026-06-27/28），逐品种核实后写入 ----
        #   BZ 纯苯  万分之1(=0.1permille) 比例值，开/平昨/**平今均万分之1**（平今不翻倍也不免）
        #           ⚠ 单位教训：文档原文是「万分之1」，其算式 186930×0.0001=18.69 元可反证。
        #             此前误按「1permille」入库使成本虚高 10 倍（实测 433.88 vs 应为 97.38）。
        #           乘数经汇总表核对为 30 吨/手（文档正文误写 10，其自身算式用的 30 才对）
        #   EB 苯乙烯 3 元/手 固定值，平今同 3（不免不翻倍）  ← 与交易所通知一致
        #   PL 丙烯  3 元/手 固定值，平昨 3、**平今 0 免收**；交易所 = **郑商所**（汇总表 ffill 核实，
        #             此前误写 DCE）。⚠ PL 与交易所通知的 1permille 冲突，按用户裁定采用文档值。
        doc_rows = [
            ("BZ", "DCE", "OPEN", "PCT", 0.1),
            ("BZ", "DCE", "CLOSE_YEST", "PCT", 0.1),
            ("BZ", "DCE", "CLOSE_TODAY", "PCT", 0.1),
            ("EB", "DCE", "OPEN", "FIXED", 3.0),
            ("EB", "DCE", "CLOSE_YEST", "FIXED", 3.0),
            ("EB", "DCE", "CLOSE_TODAY", "FIXED", 3.0),
            ("PL", "CZCE", "OPEN", "FIXED", 3.0),
            ("PL", "CZCE", "CLOSE_YEST", "FIXED", 3.0),
            ("PL", "CZCE", "CLOSE_TODAY", "FREE", 0.0),
        ]
        for vc, ex, act, ft, fv in doc_rows:
            note = ("BZ/EB/PL 第三方文档 2026-06-27/28（用户裁定采用）"
                    + ("; BZ=万分之1(0.1permille)非1permille" if vc == "BZ" else "")
                    + ("; 注意PL与交易所通知1permille冲突，待交易所公告复核"
                       if vc == "PL" else ""))
            upsert(s, vc, ex, act, ft, fv, note, "doc_20260627")
        print("[done] BZ/EB/PL rows from user docs: BZ 1permille(3 actions) / "
              "EB 3(3 actions) / PL 3+3+close-today-free")
        # ---- 沪金 AU：用户指定第三方文档基准（2026-06-12，主力 au2608）----
        #   开仓 20 / 平昨 20 / 平今 0（免收）；文档另注「主力 20、其他 10」
        #   （属合约范围限定，当前按主力写 ALL，差异记入 note）
        for act, fv in (("OPEN", 20.0), ("CLOSE_YEST", 20.0), ("CLOSE_TODAY", 0.0)):
            upsert(s, "AU", "SHFE", act, "FREE" if fv <= 1e-9 else "FIXED", fv,
                   "沪金AU 第三方文档 2026-06-12 (au2608): open20/close_yest20/ct-free; "
                   "doc notes main=20 others=10", "au_doc_20260612")
        print("[done] AU rows from user doc: 20 / 20 / close-today-free")
        for r in czce:
            vc = r["variety_code"]
            if r["open_fee"] is None:
                continue
            # ★按「手续费收取方式」分流：比例值 -> PCT(‰)，绝对值 -> FIXED(元/手)
            proportional = "比例" in str(r.get("basis", ""))
            for act, key in (("OPEN", "open_fee"), ("CLOSE_YEST", "open_fee"),
                             ("CLOSE_TODAY", "ct_fee")):
                v = r.get(key)
                if v is None:
                    continue
                fv = float(v)
                if proportional:
                    ft = "FREE" if fv <= 1e-9 else "PCT"
                else:
                    ft = "FREE" if fv <= 1e-9 else "FIXED"
                note = "CZCE official futures_contract_info_czce basis=%s" % r.get("basis")
                prev = upsert(s, vc, "CZCE", act, ft, fv, note, "czce_official")
                if prev and prev[0] and prev[0][1] and "akshare" in str(prev[0][1]):
                    n_cz += 1
    print("[done] CZCE official rows written: %d" % n_cz)

    with session_scope() as s:
        print("\n[verify] source distribution (active):")
        for a, b in s.execute(text("SELECT source, count(*) FROM dim_trading_cost "
                                   "WHERE effective_to IS NULL "
                                   "GROUP BY source ORDER BY 2 DESC")).fetchall():
            print("   %-26s %s" % (a, b))
        print("\n[verify] CZCE key values:")
        for vc, act in (("FG", "OPEN"), ("PK", "OPEN"), ("AP", "CLOSE_TODAY"),
                        ("SA", "OPEN"), ("MA", "OPEN"), ("SR", "OPEN")):
            r = s.execute(text(
                "SELECT fee_type, fee_value, source FROM dim_trading_cost "
                "WHERE variety_code=:v AND instrument_kind='FUTURE' AND action=:a "
                "AND scope_kind='ALL' AND effective_to IS NULL"),
                {"v": vc, "a": act}).fetchone()
            print("   %-4s %-12s %-6s %-8s %s" % (vc, act, r[0], r[1], r[2]) if r
                  else "   %-4s %-12s MISSING" % (vc, act))
    if not args.apply:
        print("\n[dry-run] --apply to write")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
