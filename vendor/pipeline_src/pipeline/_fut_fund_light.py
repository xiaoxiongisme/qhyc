# -*- coding: utf-8 -*-
"""
轻基本面数据层（M2 P2 / M4 数据供给）
====================================
数据源 = M1 已落 qhyc-timescaledb 的另类表（全部免费源、已回填）：
  · spot_basis            现货/基差           —— 主键含 report_date，symbol = **品种码**（FG / SI）
  · member_position_rank  前20会员持仓排名     —— symbol = **合约码**（UR2705），需去尾数字归到品种
  · inventory             仓单/库存           —— symbol = **品种码**（Y / FG）
  · macro_china           宏观指标            —— 7 指标（PMI/CPI/PPI/M1/M2/NEW_LOAN/RRR）

产出：每品种「边际三分项」——
  ① 基差（dom_basis_rate 变化）  ② 主力资金（主力合约前5会员净持仓变化）  ③ 库存边际（较上一期）
并给出 **基本面评分 score ∈ [-2, +2]** 与**缺口标注 gaps**（诚实口径）。

诚实边界（沿用 M0 覆盖矩阵）：不含社会库存/开工率/产量/进出口/上下游利润（付费墙）；
DCE 等无免费持仓排名的品种 → position 标 N/A；新闻/政策情绪非结构化，本层不接入。

用法：
  python _fut_fund_light.py                 # 计算全品种（读 _fut_universe_final.csv）→ 写 JSON
  python _fut_fund_light.py --roots FG,SA   # 只算指定品种
  python _fut_fund_light.py --date 20260921 # 指定落盘文件名日期（默认今天）
"""
import os
import sys
import json
import datetime
import argparse

WS = os.path.dirname(os.path.abspath(__file__))
if WS not in sys.path:
    sys.path.insert(0, WS)
try:
    from _fut_specs import FUT_SPECS
except Exception:
    FUT_SPECS = {}

import pandas as pd

# 输出目录与简报一致（E:\QH\期货简报\过程思考）
BASE_DIR = os.environ.get("QH_BRIEF_BASE", r"E:/QH/期货简报")
PROC_DIR = os.path.join(BASE_DIR, "过程思考")
os.makedirs(PROC_DIR, exist_ok=True)

PG_CONN = dict(host=os.environ.get("QH_PG_HOST", "localhost"),
               port=int(os.environ.get("QH_PG_PORT", 5432)),
               dbname=os.environ.get("QH_PG_DB", "futures"),
               user=os.environ.get("QH_PG_USER", "futures"),
               password=os.environ.get("QH_PG_PWD", "qhyc_dev_pwd_2026"),
               connect_timeout=10)

# 评分阈值
BASIS_THR = 0.005      # 基差率变化阈值（超过才计分）
POS_THR_RATIO = 0.02   # 前5净持仓变化 / 绝对值 的阈值
INV_THR_RATIO = 0.02   # 库存变化比例阈值
TOPN = 5               # 前 N 位会员净持仓


def norm_root(code):
    """合约码 → 品种根（UR2705 -> UR）"""
    return str(code or "").strip().upper().rstrip("0123456789")


def _conn():
    import psycopg2
    return psycopg2.connect(**PG_CONN)


def _q(sql, params=None):
    """执行查询返回 DataFrame；异常返回空 DataFrame"""
    try:
        conn = _conn()
        df = pd.read_sql_query(sql, conn, params=params)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()


# =====================================================================
# 1. 基差（spot_basis，symbol = 品种码）
# =====================================================================
def fetch_basis(roots):
    """返回 {root: {report_date, spot_price, dominant_contract, dom_basis, dom_basis_rate,
                     prev_rate, chg}}"""
    out = {}
    df = _q("""
        SELECT symbol, report_date, spot_price, dominant_contract,
               dominant_contract_price, dom_basis, dom_basis_rate
        FROM spot_basis
        WHERE report_date >= (SELECT max(report_date) - 20 FROM spot_basis)
        ORDER BY symbol, report_date DESC""")
    if df.empty:
        return out
    for sym, g in df.groupby("symbol"):
        g = g.sort_values("report_date", ascending=False).reset_index(drop=True)
        root = str(sym).strip().upper()
        if roots and root not in roots:
            continue
        latest = g.iloc[0]
        prev = g.iloc[min(TOPN, len(g) - 1)] if len(g) > 1 else None
        cur_rate = float(latest["dom_basis_rate"]) if latest["dom_basis_rate"] is not None else None
        prev_rate = float(prev["dom_basis_rate"]) if (prev is not None and prev["dom_basis_rate"] is not None) else None
        out[root] = dict(
            report_date=str(latest["report_date"]),
            spot_price=float(latest["spot_price"]) if latest["spot_price"] is not None else None,
            dominant_contract=str(latest["dominant_contract"] or ""),
            dominant_price=float(latest["dominant_contract_price"]) if latest["dominant_contract_price"] is not None else None,
            dom_basis=float(latest["dom_basis"]) if latest["dom_basis"] is not None else None,
            dom_basis_rate=cur_rate,
            prev_rate=prev_rate,
            chg=(cur_rate - prev_rate) if (cur_rate is not None and prev_rate is not None) else None,
        )
    return out


# =====================================================================
# 2. 主力资金（member_position_rank，symbol = 合约码）
#    语义：一行 = 一个 rank 位；取该品种「主力合约」的 rank<=TOPN 净持仓
# =====================================================================
def fetch_position(roots):
    """返回 {root: {trade_date, contract, net_topn, prev_net_topn, chg}}"""
    out = {}
    # 最近 8 个交易日
    dates_df = _q("""SELECT DISTINCT trade_date FROM member_position_rank
                     ORDER BY trade_date DESC LIMIT 8""")
    if dates_df.empty:
        return out
    dates = [d for (d,) in dates_df.values]
    latest_d, prev_d = dates[0], (dates[TOPN] if len(dates) > TOPN else dates[-1])
    rows = _q("""
        SELECT trade_date, symbol, rank, long_pos, short_pos, vol_pos
        FROM member_position_rank
        WHERE trade_date IN (%s, %s)""", (latest_d, prev_d))
    if rows.empty:
        return out

    def agg_on(day):
        """该交易日：每品种 → 主力合约 + 前N净持仓"""
        sub = rows[rows["trade_date"] == day]
        res = {}
        by_root = {}
        for sym, g in sub.groupby("symbol"):
            by_root.setdefault(norm_root(sym), []).append((sym, g))
        for root, lst in by_root.items():
            if roots and root not in roots:
                continue
            # 主力合约 = 该品种当日 (long+short+vol) 合计最大的合约
            best_sym, best_tot = None, -1
            for sym, g in lst:
                tot = float(g["long_pos"].fillna(0).sum() + g["short_pos"].fillna(0).sum()
                            + g["vol_pos"].fillna(0).sum())
                if tot > best_tot:
                    best_tot, best_sym = tot, sym
            g = next(gg for s, gg in lst if s == best_sym)
            topn = g[g["rank"] <= TOPN]
            net = float((topn["long_pos"].fillna(0) - topn["short_pos"].fillna(0)).sum())
            res[root] = dict(contract=best_sym, net_topn=net,
                             long_topn=float(topn["long_pos"].fillna(0).sum()),
                             short_topn=float(topn["short_pos"].fillna(0).sum()))
        return res

    cur, prev = agg_on(latest_d), agg_on(prev_d)
    for root, c in cur.items():
        p = prev.get(root)
        out[root] = dict(
            trade_date=str(latest_d),
            contract=c["contract"],
            net_topn=c["net_topn"], long_topn=c["long_topn"], short_topn=c["short_topn"],
            prev_net_topn=(p["net_topn"] if p else None),
            prev_date=str(prev_d) if p else None,
            chg=(c["net_topn"] - p["net_topn"]) if p else None,
        )
    return out


# =====================================================================
# 3. 库存/仓单（inventory，symbol = 品种码）
# =====================================================================
def fetch_inventory(roots):
    out = {}
    df = _q("""
        SELECT symbol, report_date, SUM(COALESCE(inventory_qty,0)) AS qty,
               MAX(unit) AS unit
        FROM inventory
        WHERE report_date >= (SELECT max(report_date) - 30 FROM inventory)
        GROUP BY symbol, report_date
        ORDER BY symbol, report_date DESC""")
    if df.empty:
        return out
    for sym, g in df.groupby("symbol"):
        g = g.sort_values("report_date", ascending=False).reset_index(drop=True)
        root = str(sym).strip().upper()
        if roots and root not in roots:
            continue
        latest = g.iloc[0]
        prev = g.iloc[min(4, len(g) - 1)] if len(g) > 1 else None
        cur_q = float(latest["qty"]) if latest["qty"] is not None else None
        prev_q = float(prev["qty"]) if (prev is not None and prev["qty"] is not None) else None
        out[root] = dict(
            report_date=str(latest["report_date"]),
            qty=cur_q, prev_qty=prev_q, unit=str(latest["unit"] or ""),
            chg=(cur_q - prev_q) if (cur_q is not None and prev_q is not None) else None,
            chg_ratio=((cur_q - prev_q) / prev_q) if (cur_q is not None and prev_q not in (None, 0)) else None,
        )
    return out


# =====================================================================
# 4. 宏观（macro_china）
# =====================================================================
def fetch_macro():
    out = {}
    df = _q("""SELECT indicator, period_date, value, unit FROM macro_china
               ORDER BY indicator, period_date DESC""")
    if df.empty:
        return out
    for ind, g in df.groupby("indicator"):
        g = g.sort_values("period_date", ascending=False).reset_index(drop=True)
        latest = g.iloc[0]
        prev = g.iloc[1] if len(g) > 1 else None
        cur_v = float(latest["value"]) if latest["value"] is not None else None
        prev_v = float(prev["value"]) if (prev is not None and prev["value"] is not None) else None
        out[str(ind)] = dict(
            period_date=str(latest["period_date"]), value=cur_v, prev_value=prev_v,
            mom=(cur_v - prev_v) if (cur_v is not None and prev_v is not None) else None,
            unit=str(latest["unit"] or ""),
        )
    return out


def macro_regime(macro):
    """宏观状态一句话（工业品语境）：PMI 与信贷方向"""
    pmi = macro.get("PMI") or {}
    loan = macro.get("NEW_LOAN") or {}
    pmi_v = pmi.get("value")
    if pmi_v is None:
        return "宏观数据缺失"
    if pmi_v >= 50:
        base = f"PMI {pmi_v:.1f} 位于荣枯线上（扩张）"
    else:
        base = f"PMI {pmi_v:.1f} 位于荣枯线下（收缩）"
    if pmi.get("mom") is not None:
        base += f"，环比 {'+%.1f' % pmi['mom'] if pmi['mom'] >= 0 else '%.1f' % pmi['mom']}"
    if loan.get("mom") is not None:
        base += f"；新增信贷环比 {'增' if loan['mom'] > 0 else '减'}"
    return base


# =====================================================================
# 5. 汇总评分
# =====================================================================
def score_root(basis, pos, inv):
    """三分项边际 → 基本面评分 [-2, +2] + 分项贡献 + 缺口标注"""
    s, parts, gaps = 0.0, [], []

    # ① 基差：dom_basis = 期货 - 现货；基差率下行（贴水加深）= 现货紧 = 偏多
    if basis and basis.get("chg") is not None:
        chg = basis["chg"]
        if chg < -BASIS_THR:
            s += 0.5; parts.append(f"基差率 {chg*100:+.2f}pct（贴水加深·现货偏紧）→ 偏多")
        elif chg > BASIS_THR:
            s -= 0.5; parts.append(f"基差率 {chg*100:+.2f}pct（升水扩大·现货宽松）→ 偏空")
        else:
            parts.append(f"基差率 {chg*100:+.2f}pct（变化不大）→ 中性")
    else:
        gaps.append("基差 N/A（该品种无免费现货源）")

    # ② 主力资金：前N会员净持仓增加 → 偏多
    if pos and pos.get("chg") is not None:
        chg, base = pos["chg"], abs(pos.get("prev_net_topn") or 0)
        denom = max(base, 1.0)
        if chg > POS_THR_RATIO * denom:
            s += 0.5; parts.append(f"前{TOPN}会员净持仓 {chg:+,.0f} 手（主力增多）→ 偏多")
        elif chg < -POS_THR_RATIO * denom:
            s -= 0.5; parts.append(f"前{TOPN}会员净持仓 {chg:+,.0f} 手（主力增空）→ 偏空")
        else:
            parts.append(f"前{TOPN}会员净持仓 {chg:+,.0f} 手（变化有限）→ 中性")
    else:
        gaps.append("会员持仓 N/A（DCE 等无免费排名源，见 M0 覆盖矩阵）")

    # ③ 库存：去库 → 偏多；累库 → 偏空
    if inv and inv.get("chg_ratio") is not None:
        r = inv["chg_ratio"]
        if r < -INV_THR_RATIO:
            s += 0.5; parts.append(f"库存 {r*100:+.1f}%（去库）→ 偏多")
        elif r > INV_THR_RATIO:
            s -= 0.5; parts.append(f"库存 {r*100:+.1f}%（累库）→ 偏空")
        else:
            parts.append(f"库存 {r*100:+.1f}%（变化有限）→ 中性")
    else:
        gaps.append("库存 N/A（该品种无免费仓单源）")

    s = max(-2.0, min(2.0, s))
    return round(s, 2), parts, gaps


def build_fund(roots=None, date=None):
    """主入口：返回 fund dict（含 meta / macro / by_root）"""
    date = date or datetime.date.today().strftime("%Y-%m-%d")
    roots = set(r.strip().upper() for r in roots) if roots else None
    basis = fetch_basis(roots)
    pos = fetch_position(roots)
    inv = fetch_inventory(roots)
    macro = fetch_macro()

    all_roots = sorted(set(basis) | set(pos) | set(inv) | (roots or set()))
    by_root = {}
    for r in all_roots:
        b, p, i = basis.get(r), pos.get(r), inv.get(r)
        sc, parts, gaps = score_root(b, p, i)
        by_root[r] = dict(basis=b, position=p, inventory=i,
                          score=sc, parts=parts, gaps=gaps,
                          name=(FUT_SPECS.get(r, {}) or {}).get("name", r))
    return dict(date=date,
                generated_at=datetime.datetime.now().isoformat(timespec="seconds"),
                meta=dict(topn=TOPN, basis_thr=BASIS_THR,
                          sources=["spot_basis", "member_position_rank",
                                   "inventory", "macro_china"]),
                macro=macro,
                macro_regime=macro_regime(macro),
                by_root=by_root)


def fund_path(date=None):
    date = date or datetime.date.today()
    d = (date.strftime("%Y%m%d") if hasattr(date, "strftime") else str(date).replace("-", ""))
    return os.path.join(PROC_DIR, f"_brief_fund_{d}.json")


def save_fund(fund, date=None):
    p = fund_path(date or fund.get("date"))
    with open(p, "w", encoding="utf-8") as f:
        json.dump(fund, f, ensure_ascii=False, indent=1, default=str)
    return p


def load_fund(date=None):
    """供简报 --render 读取；不存在返回 None"""
    p = fund_path(date)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _fmt_num(v, nd=2):
    return "-" if v is None else f"{v:,.{nd}f}"


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", default="", help="品种根，逗号分隔；默认取品种全集")
    ap.add_argument("--date", default=None, help="文件名日期 YYYY-MM-DD")
    a = ap.parse_args()

    if a.roots:
        roots = [x.strip().upper() for x in a.roots.split(",") if x.strip()]
    else:
        try:
            univ = pd.read_csv(os.path.join(WS, "_fut_universe_final.csv"), encoding="utf-8-sig")
            roots = [str(x).strip().upper() for x in univ["root"].tolist()]
        except Exception:
            roots = sorted(FUT_SPECS.keys())

    fund = build_fund(roots, a.date)
    p = save_fund(fund, a.date)
    print(f"轻基本面已落盘: {p}")
    print(f"宏观: {fund['macro_regime']}")
    print(f"品种数: {len(fund['by_root'])}")
    # 控制台摘要（前 12 个有分的）
    rows = [(r, v) for r, v in fund["by_root"].items() if v["parts"]]
    rows.sort(key=lambda kv: -abs(kv[1]["score"]))
    print("\n基本面评分（按绝对值排序，前 12）：")
    for r, v in rows[:12]:
        print(f"  {r:<4} {v['name']:<6} score={v['score']:+.1f}  " +
              " | ".join(v["parts"][:2]))
    miss = [r for r, v in fund["by_root"].items() if len(v["gaps"]) >= 2]
    if miss:
        print(f"\n⚠ 数据缺口≥2 项（诚实标注）: {', '.join(sorted(miss))}")
