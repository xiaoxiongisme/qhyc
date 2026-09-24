# -*- coding: utf-8 -*-
"""
基本面 / 宏观 / 情绪 Agent（M4）
================================
框架定位（《国内期货多智能体融合框架设计》第二节 角色 3/4/5 + 第六节 M4）：
  Fundamentals Analyst（现货/基差/仓单/持仓）+ Macro Analyst（宏观状态/政策）
  + Sentiment/Positioning（主力资金方向、情绪评分）。

在 M2 的「轻基本面数据层」(`_fut_fund_light.py`) 之上做 **agent 化**，补三件事：
  1. **板块归类与宏观传导**：按板块（黑色/有色/能化/农产品/软商品）给宏观指标的差异化权重，
     产出宏观调节项 macro_adj（PMI/信贷偏工业品，CPI 偏农产品，PPI 偏工业品）。
  2. **资金/情绪代理**：用「主力合约前N会员净持仓变化」+「基差方向」合成情绪分；
     **新闻/政策情绪诚实标注为 N/A**（非结构化、无稳定免费 API，见 M0 覆盖矩阵）。
  3. **完备度与置信度**：统计三分项可得数（0~3）→ 置信度 高/中/低；
     综合评分 = 三分项评分 + 宏观调节（clip 到 [-2, +2]）。

诚实边界（必须随产物一起声明）：
  不含社会库存 / 开工率 / 产量 / 进出口 / 上下游利润（付费墙）；
  DCE 等无免费会员持仓排名源 → 资金项 N/A；新闻/政策情绪未接入。

产出：
  `E:/QH/期货简报/过程思考/_fundamentals_agent_YYYYMMDD.json`
  `E:/QH/期货简报/简报内容/基本面Agent报告_YYYYMMDD.md`（--report）

用法：
  python _fut_fundamentals_agent.py                 # 计算全品种并落 JSON
  python _fut_fundamentals_agent.py --report        # 额外生成独立 Markdown 报告
  python _fut_fundamentals_agent.py --roots FG,SA
"""
import os
import sys
import json
import argparse
import datetime

WS = os.path.dirname(os.path.abspath(__file__))
if WS not in sys.path:
    sys.path.insert(0, WS)
import pandas as pd
try:
    from _fut_specs import FUT_SPECS
except Exception:
    FUT_SPECS = {}
import _fut_fund_light as FL

BASE_DIR = os.environ.get("QH_BRIEF_BASE", r"E:/QH/期货简报")
PROC_DIR = os.path.join(BASE_DIR, "过程思考")
BRIEF_DIR = os.path.join(BASE_DIR, "简报内容")
os.makedirs(PROC_DIR, exist_ok=True)
os.makedirs(BRIEF_DIR, exist_ok=True)

# ---------------------------------------------------------------- 板块归类
SECTOR_MAP = {
    # 黑色
    "RB": "黑色", "HC": "黑色", "I": "黑色", "J": "黑色", "JM": "黑色",
    "SF": "黑色", "SM": "黑色", "SS": "黑色",
    # 有色
    "CU": "有色", "AL": "有色", "ZN": "有色", "NI": "有色", "SN": "有色",
    "PB": "有色", "AO": "有色", "SI": "有色", "LC": "有色", "PS": "有色",
    # 贵金属
    "AU": "贵金属", "AG": "贵金属",
    # 能化
    "RU": "能化", "NR": "能化", "BR": "能化", "FU": "能化", "LU": "能化",
    "BU": "能化", "SC": "能化", "TA": "能化", "EG": "能化", "EB": "能化",
    "MA": "能化", "PP": "能化", "L": "能化", "V": "能化", "PG": "能化",
    "PF": "能化", "PX": "能化", "SH": "能化", "UR": "能化", "SA": "能化",
    "FG": "能化",
    # 农产品
    "A": "农产品", "B": "农产品", "M": "农产品", "Y": "农产品", "P": "农产品",
    "C": "农产品", "CS": "农产品", "RM": "农产品", "OI": "农产品",
    "JD": "农产品", "LH": "农产品", "AP": "农产品", "CJ": "农产品",
    "PK": "农产品", "CF": "农产品", "SR": "农产品",
}
INDUSTRIAL_SECTORS = ("黑色", "有色", "能化", "贵金属")


def sector_of(root):
    return SECTOR_MAP.get(str(root).upper(), "其他")


# ---------------------------------------------------------------- 宏观传导
def macro_adjustment(root, macro):
    """按板块给宏观指标的差异化传导。返回 (adj, [notes])"""
    sec = sector_of(root)
    indus = sec in INDUSTRIAL_SECTORS
    agri = sec in ("农产品",)
    adj, notes = 0.0, []
    pmi = macro.get("PMI") or {}
    cpi = macro.get("CPI") or {}
    ppi = macro.get("PPI") or {}
    loan = macro.get("NEW_LOAN") or {}

    if pmi.get("value") is not None:
        v = pmi["value"]
        if indus:
            if v >= 50:
                adj += 0.25; notes.append(f"PMI {v:.1f} 扩张 → 工业品需求端偏多")
            else:
                adj -= 0.25; notes.append(f"PMI {v:.1f} 收缩 → 工业品需求端偏空")
        if pmi.get("mom") is not None:
            m = pmi["mom"]
            if abs(m) >= 0.2:
                d = 0.1 if m > 0 else -0.1
                adj += d; notes.append(f"PMI 环比 {m:+.1f} → {'需求边际改善' if m > 0 else '需求边际走弱'}")
    if loan.get("mom") is not None and indus:
        m = loan["mom"]
        if m != 0:
            d = 0.15 if m > 0 else -0.15
            adj += d; notes.append(f"新增信贷环比{'增' if m > 0 else '减'} → 工业品资金/需求{'偏多' if m > 0 else '偏空'}")
    if ppi.get("mom") is not None and indus:
        m = ppi["mom"]
        if m != 0:
            d = 0.1 if m > 0 else -0.1
            adj += d; notes.append(f"PPI 环比 {m:+.2f} → 工业品价格链{'偏多' if m > 0 else '偏空'}")
    if cpi.get("mom") is not None and agri:
        m = cpi["mom"]
        if m != 0:
            d = 0.15 if m > 0 else -0.15
            adj += d; notes.append(f"CPI 环比 {m:+.2f} → 农产品{'通胀偏多' if m > 0 else '通缩偏空'}")
    adj = max(-0.5, min(0.5, adj))
    return round(adj, 2), notes


# ---------------------------------------------------------------- 情绪代理
def sentiment_proxy(basis, pos):
    """用主力资金 + 基差方向合成资金/情绪分（-1~+1）；新闻情绪诚实标 N/A。"""
    s, notes = 0.0, []
    if pos and pos.get("chg") is not None:
        d = 1.0 if pos["chg"] > 0 else (-1.0 if pos["chg"] < 0 else 0.0)
        s += 0.5 * d
        notes.append(f"主力资金{'净多增仓' if d > 0 else ('净空增仓' if d < 0 else '无变化')}")
    if basis and basis.get("chg") is not None:
        # 基差率下行=贴水加深=现货紧 → 偏多
        d = 1.0 if basis["chg"] < -FL.BASIS_THR else (-1.0 if basis["chg"] > FL.BASIS_THR else 0.0)
        s += 0.5 * d
        notes.append(f"基差{'贴水加深(现货紧)' if d > 0 else ('升水扩大(现货松)' if d < 0 else '平稳')}")
    s = max(-1.0, min(1.0, s))
    return round(s, 2), notes


# ---------------------------------------------------------------- 主流程
def build_agent(roots=None, date=None):
    date = date or datetime.date.today().strftime("%Y-%m-%d")
    fund = FL.build_fund(roots, date)
    fund_map = fund.get("by_root", {})
    macro = fund.get("macro", {}) or {}

    by_root = {}
    for root, fr in fund_map.items():
        basis, pos, inv = fr.get("basis"), fr.get("position"), fr.get("inventory")
        coverage = sum(1 for x in (basis and basis.get("chg") is not None,
                                   pos and pos.get("chg") is not None,
                                   inv and inv.get("chg_ratio") is not None) if x)
        conf = "高" if coverage == 3 else ("中" if coverage == 2 else "低")
        adj, mnotes = macro_adjustment(root, macro)
        sent, snotes = sentiment_proxy(basis, pos)
        base = fr.get("score") or 0.0
        total = max(-2.0, min(2.0, base + adj))
        gaps = list(fr.get("gaps") or [])
        gaps.append("新闻/政策情绪 N/A（非结构化，无稳定免费 API）")
        by_root[root] = dict(
            name=fr.get("name") or root,
            sector=sector_of(root),
            base_score=round(base, 2), macro_adj=adj,
            score=round(total, 2),
            sentiment=sent, coverage=coverage, confidence=conf,
            basis=basis, position=pos, inventory=inv,
            parts=fr.get("parts") or [],
            macro_notes=mnotes, sentiment_notes=snotes,
            gaps=gaps,
        )
    return dict(date=date,
                generated_at=datetime.datetime.now().isoformat(timespec="seconds"),
                macro=macro, macro_regime=fund.get("macro_regime", ""),
                honest_limits=[
                    "不含社会库存 / 开工率 / 产量 / 进出口 / 上下游利润（付费墙）",
                    "DCE 等无免费会员持仓排名源 → 资金项 N/A",
                    "新闻/政策情绪未接入（非结构化）",
                ],
                by_root=by_root)


def agent_path(date=None):
    d = (date.strftime("%Y%m%d") if hasattr(date, "strftime")
         else str(date or datetime.date.today()).replace("-", ""))
    return os.path.join(PROC_DIR, f"_fundamentals_agent_{d}.json")


def save_agent(agent, date=None):
    p = agent_path(date or agent.get("date"))
    with open(p, "w", encoding="utf-8") as f:
        json.dump(agent, f, ensure_ascii=False, indent=1, default=str)
    return p


def load_agent(date=None):
    p = agent_path(date)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def report_md(agent):
    """独立 Markdown 报告"""
    a = agent or {}
    L = [f"# 基本面 / 宏观 / 情绪 Agent 报告（{a.get('date','')}）\n",
         f"> 宏观：{a.get('macro_regime','—')}\n",
         "## 诚实边界（必读）\n"]
    for x in (a.get("honest_limits") or []):
        L.append(f"- {x}")
    L.append("\n## 品种明细\n")
    L.append("| 品种 | 板块 | 综合评分 | 三分项基分 | 宏观调节 | 资金情绪 | 完备度 | 置信 |")
    L.append("|------|------|----------|------------|----------|----------|--------|------|")
    rows = sorted((a.get("by_root") or {}).items(), key=lambda kv: -abs(kv[1].get("score") or 0))
    for r, v in rows:
        L.append(f"| {v.get('name')} {r} | {v.get('sector')} | **{v.get('score'):+.2f}** | "
                 f"{v.get('base_score'):+.2f} | {v.get('macro_adj'):+.2f} | "
                 f"{v.get('sentiment'):+.2f} | {v.get('coverage')}/3 | {v.get('confidence')} |")
    L.append("\n## 评分口径\n")
    L.append("- 三分项基分 = 基差率变化 ±0.5 + 前5会员净持仓变化 ±0.5 + 库存变化 ±0.5（clip ±1.5）")
    L.append("- 宏观调节 = 按板块差异化传导（PMI/信贷/PPI→工业品，CPI→农产品），clip ±0.5")
    L.append("- 资金情绪 = 主力资金 ±0.5 + 基差方向 ±0.5，clip ±1.0；新闻情绪 N/A")
    L.append("- 综合评分 = 基分 + 宏观调节，clip ±2.0；完备度 = 三分项可得数（0~3）")
    L.append("\n**免责声明：仅供教学与决策参考，不构成投资建议；期货有杠杆，风险自负。**")
    return "\n".join(L)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", default="")
    ap.add_argument("--date", default=None)
    ap.add_argument("--report", action="store_true", help="额外生成独立 Markdown 报告")
    a = ap.parse_args()
    roots = [x.strip().upper() for x in a.roots.split(",") if x.strip()] or None
    if not roots:
        try:
            univ = pd.read_csv(os.path.join(WS, "_fut_universe_final.csv"), encoding="utf-8-sig")
            roots = [str(x).strip().upper() for x in univ["root"].tolist()]
        except Exception:
            roots = None
    agent = build_agent(roots, a.date)
    p = save_agent(agent, a.date)
    print(f"基本面Agent已落盘: {p}")
    print(f"品种 {len(agent['by_root'])}｜宏观 {agent['macro_regime']}")
    conf = {}
    for v in agent["by_root"].values():
        conf[v.get("confidence")] = conf.get(v.get("confidence"), 0) + 1
    print("置信分布:", conf)
    rows = sorted(agent["by_root"].items(), key=lambda kv: -abs(kv[1]["score"]))
    print("\n综合评分（按绝对值，前 10）：")
    for r, v in rows[:10]:
        print(f"  {r:<4} {v['name']:<6} {v['sector']:<4} {v['score']:+.2f} "
              f"(基分{v['base_score']:+.2f} 宏观{v['macro_adj']:+.2f} 情绪{v['sentiment']:+.2f}) "
              f"置信{v['confidence']}")
    if a.report:
        out = os.path.join(BRIEF_DIR, f"基本面Agent报告_{str(agent['date']).replace('-','')}.md")
        with open(out, "w", encoding="utf-8") as f:
            f.write(report_md(agent))
        print(f"\n报告已生成: {out}")
