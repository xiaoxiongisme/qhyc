# -*- coding: utf-8 -*-
"""
研究层（M3）：LLM 多空辩论 × Quant 量化验证 交叉印证
====================================================
框架定位（见《国内期货多智能体融合框架设计》第六节 M3）：
  研究层双轨 = ① LLM 多空辩论（定性） ② Quant Validator 回测验证（客观），
  **任一方与另一方冲突时标红，不自动采信**。

本模块产出三件事：
  1. **多空论据结构化合成**：把技术面（方向层/ADX/阶段/位置/护栏）+ 基本面评分分项
     + 宏观状态，整理为 bull[] / bear[] 证据清单与各自强度分（供 LLM 做定性综述）。
     注：真正的"辩论综述"由 LLM（本智能体）在出简报时基于这些结构化证据完成，
     本模块负责把证据与强度客观化，避免纯主观。
  2. **Quant 验证**：用 `fut_kline` 日线主连（kind='continuous', freq='daily'）
     回放**融合策略 V2.0 简化规则**，产出每品种记分卡：
     交易次数 / 胜率 / 平均R / 期望R / 最大回撤 / 近1年 vs 近3年稳定性。
     ⚠️ 用主连回放（非合约级）原因：合约级序列短且分散，主连是策略回测的既有口径；
     结论用于"有效性背书"，不用于给点位。
  3. **冲突检测**：技术方向 vs 基本面方向 vs 量化有效性 → 共振 / 背离标红。

产出：`E:/QH/期货简报/过程思考/_brief_research_YYYYMMDD.json`

用法：
  python _fut_research.py                 # 计算全品种（读当日 _brief_tech_*.json + _brief_fund_*.json）
  python _fut_research.py --roots FG,SA   # 只算指定品种
  python _fut_research.py --no-quant      # 跳过回测（只做论据合成，快）
"""
import os
import sys
import json
import glob
import argparse
import datetime

WS = os.path.dirname(os.path.abspath(__file__))
if WS not in sys.path:
    sys.path.insert(0, WS)
import numpy as np
import pandas as pd
try:
    from _fut_specs import FUT_SPECS
except Exception:
    FUT_SPECS = {}

BASE_DIR = os.environ.get("QH_BRIEF_BASE", r"E:/QH/期货简报")
PROC_DIR = os.path.join(BASE_DIR, "过程思考")
os.makedirs(PROC_DIR, exist_ok=True)

PG_CONN = dict(host=os.environ.get("QH_PG_HOST", "localhost"),
               port=int(os.environ.get("QH_PG_PORT", 5432)),
               dbname=os.environ.get("QH_PG_DB", "futures"),
               user=os.environ.get("QH_PG_USER", "futures"),
               password=os.environ.get("QH_PG_PWD", "qhyc_dev_pwd_2026"),
               connect_timeout=10)

SLK, TRK, BER = 2.0, 2.0, 0.5     # 与 _fut_daily_brief 融合参数保持一致


# =====================================================================
# 数据入口
# =====================================================================
def _latest(pattern, default_dir=None):
    fs = sorted(glob.glob(os.path.join(PROC_DIR, pattern)))
    return fs[-1] if fs else None


def load_json(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def load_tech():
    return load_json(_latest("_brief_tech_*.json"))


def load_fund():
    return load_json(_latest("_brief_fund_*.json"))


def daily_continuous(roots=None, min_bars=250):
    """从 fut_kline 取日线主连，返回 {root: DataFrame}（列 open/high/low/close）"""
    try:
        import psycopg2
    except Exception:
        return {}
    try:
        conn = psycopg2.connect(**PG_CONN)
        sym_sql = "SELECT DISTINCT symbol FROM fut_kline WHERE freq='daily' AND kind='continuous'"
        syms = pd.read_sql_query(sym_sql, conn)["symbol"].tolist()
        # KQ.m@CZCE.FG -> FG
        root2sym = {}
        for s in syms:
            r = str(s).split(".")[-1].upper()
            if roots and r not in roots:
                continue
            root2sym[r] = s
        if not root2sym:
            conn.close()
            return {}
        q = ("SELECT symbol, trade_datetime, open, high, low, close FROM fut_kline "
             "WHERE freq='daily' AND kind='continuous' AND symbol IN %s "
             "ORDER BY symbol, trade_datetime")
        df = pd.read_sql_query(q, conn, params=(tuple(root2sym.values()),))
        conn.close()
    except Exception:
        return {}
    out = {}
    for sym, g in df.groupby("symbol"):
        root = str(sym).split(".")[-1].upper()
        g = g.sort_values("trade_datetime").reset_index(drop=True)
        if len(g) < min_bars:
            continue
        out[root] = pd.DataFrame({
            "date": pd.to_datetime(g["trade_datetime"]).dt.strftime("%Y-%m-%d"),
            "open": g["open"].astype(float), "high": g["high"].astype(float),
            "low": g["low"].astype(float), "close": g["close"].astype(float),
        })
    return out


# =====================================================================
# Quant 验证：融合规则简化回放
# =====================================================================
def _atr(df, n=14):
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - l, np.maximum(np.abs(h - pc), np.abs(l - pc)))
    a = pd.Series(tr).rolling(n).mean().bfill().values
    return a


def fusion_backtest(df, years_split=1.0):
    """回放融合策略简化规则，返回记分卡 dict。

    规则（与日线融合 V2.0 对齐的简化版）：
      方向层：close_prev > EMA20_prev 偏多；< 偏空
      入场 A：回踩 EMA10 后收回（close>EMA10 且 close_prev<EMA10_prev 且 low<=EMA10 且收强）
      入场 B：突破 10 日高点/低点且收强/收弱
      离场：初始 2×ATR 止损 + 2×ATR 吊灯移动止损（保本线 0.5×ATR）；
            方向层反转/转弱（close_prev 反向穿越 EMA20）也离场
    """
    n = len(df)
    if n < 60:
        return None
    c = df["close"].values; o = df["open"].values
    h = df["high"].values; l = df["low"].values
    dates = df["date"].values
    ema20 = pd.Series(c).ewm(span=20, adjust=False).mean().values
    ema10 = pd.Series(c).ewm(span=10, adjust=False).mean().values
    atr = _atr(df)
    hh10 = pd.Series(h).rolling(10).max().shift(1).bfill().values
    ll10 = pd.Series(l).rolling(10).min().shift(1).bfill().values

    trades = []
    pos = 0; entry = stop = init_stop = 0.0; pk = tr = 0.0; ei = 0
    for i in range(30, n):
        cp = c[i - 1]
        dir_long = cp > ema20[i - 1]
        dir_short = cp < ema20[i - 1]
        if pos != 0:
            # 更新极值 + 吊灯
            if pos == 1:
                pk = max(pk, h[i])
                cand = pk - TRK * atr[i]                      # 吊灯：自持仓期最高点回撤 TRK×ATR
                if (pk - entry) >= BER * atr[i]:              # 保本线：浮盈达 0.5×ATR 后上移
                    cand = max(cand, entry + BER * atr[i])
                stop = max(stop, cand)
            else:
                tr = min(tr, l[i])
                cand = tr + TRK * atr[i]
                if (entry - tr) >= BER * atr[i]:
                    cand = min(cand, entry - BER * atr[i])
                stop = min(stop, cand)
            # 止损触发（按 bar 内触及近似）
            hit = (l[i] <= stop) if pos == 1 else (h[i] >= stop)
            flip = (pos == 1 and dir_short) or (pos == -1 and dir_long)
            if hit or flip:
                xp = stop if hit else c[i]
                r = ((xp - entry) / abs(entry - init_stop)) if pos == 1 else ((entry - xp) / abs(entry - init_stop))
                if abs(entry - init_stop) > 0:
                    trades.append(dict(entry_i=ei, exit_i=i, entry_date=str(dates[ei]),
                                       exit_date=str(dates[i]), side=pos,
                                       entry=float(entry), exit=float(xp), R=float(r),
                                       reason=("止损/吊灯" if hit else "方向层反转")))
                pos = 0; pk = 0.0; tr = 0.0
                continue
        if pos == 0:
            mid = (h[i] + l[i]) / 2
            strb = (c[i] > o[i]) and (c[i] >= mid)
            strr = (c[i] < o[i]) and (c[i] <= mid)
            up = (c[i] > ema10[i]) and (cp < ema10[i - 1])
            dn = (c[i] < ema10[i]) and (cp > ema10[i - 1])
            a_long = dir_long and up and (l[i] <= ema10[i]) and strb
            a_short = dir_short and dn and (h[i] >= ema10[i]) and strr
            b_long = dir_long and (c[i] > hh10[i]) and (c[i] > o[i])
            b_short = dir_short and (c[i] < ll10[i]) and (c[i] < o[i])
            if a_long or b_long:
                pos = 1; entry = c[i]; init_stop = entry - SLK * atr[i]
                stop = init_stop; pk = h[i]; ei = i
            elif a_short or b_short:
                pos = -1; entry = c[i]; init_stop = entry + SLK * atr[i]
                stop = init_stop; tr = l[i]; ei = i

    if not trades:
        return dict(n=0)

    def stats(ts):
        """回撤用 **R 单位**（peak-to-trough），不用百分比——
        基于 R 的累计权益可能为负，(peak-eq)/peak 会算出 >100% 的无意义百分比。"""
        if not ts:
            return dict(n=0, win=0.0, avgR=0.0, expR=0.0, mdd_r=0.0)
        rs = [t["R"] for t in ts]
        wins = sum(1 for r in rs if r > 0)
        eq, peak, mdd_r = 0.0, 0.0, 0.0
        for r in rs:
            eq += r
            peak = max(peak, eq)
            mdd_r = max(mdd_r, peak - eq)
        return dict(n=len(ts), win=wins / len(ts), avgR=float(np.mean(rs)),
                    expR=float(np.mean(rs)), mdd_r=float(mdd_r))

    all_s = stats(trades)
    # 近 N 年子集
    last_date = pd.Timestamp(dates[-1])
    cut = last_date - pd.Timedelta(days=int(365 * years_split))
    recent = [t for t in trades if pd.Timestamp(t["exit_date"]) >= cut]
    recent_s = stats(recent)
    all_s["recent_n"] = recent_s["n"]
    all_s["recent_expR"] = recent_s["expR"]
    all_s["recent_win"] = recent_s["win"]
    all_s["start"] = str(dates[0]); all_s["end"] = str(dates[-1])
    # 有效性判定：样本足够 且 期望为正 且 近1年不显著劣化
    all_s["valid"] = bool(all_s["n"] >= 20 and all_s["expR"] > 0
                          and (recent_s["n"] < 5 or recent_s["expR"] > -0.05))
    return all_s


# =====================================================================
# 多空论据合成
# =====================================================================
def bull_bear(tech, fund_row, quant, macro_regime=""):
    """返回 (bull_list, bear_list, bull_score, bear_score)"""
    bull, bear = [], []
    d = tech.get("direction") or ""
    act = tech.get("action") or "观望"
    adx = tech.get("adx") or 0
    stage = tech.get("stage") or ""
    # —— 技术面 ——
    if d == "多头":
        bull.append(f"方向层 EMA20 偏多（收盘站上慢线），阶段「{stage}」")
    elif d == "空头":
        bear.append(f"方向层 EMA20 偏空（收盘跌破慢线），阶段「{stage}」")
    if act == "做多":
        bull.append(f"融合引擎今日给出**做多**信号（A 回踩重启 / B 10根突破）")
    elif act == "做空":
        bear.append(f"融合引擎今日给出**做空**信号（A 回踩重启 / B 10根突破）")
    elif act == "观望":
        bull.append("引擎未触发（趋势转震荡），多空双方均缺乏入场依据")
        bear.append("引擎未触发（趋势转震荡），逆势抢跑风险高")
    if adx >= 25:
        bull.append(f"ADX {adx:.0f}（趋势性强，利于趋势跟随策略）")
    elif adx < 20:
        bear.append(f"ADX {adx:.0f} 偏弱（震荡市，趋势策略易被洗）")
    rsn = tech.get("reason") or ""
    if "警示" in rsn:
        bear.append("🛡 护栏提示：价格已远离突破参考点，追高风险上升")
    if "背离" in rsn:
        bear.append("⚠️ 中期 MA20/60 与方向层背离（长趋势中的回调）")

    # —— 基本面 ——
    if fund_row:
        sc = fund_row.get("score")
        if sc:
            if sc > 0:
                bull.append(f"基本面评分 {sc:+.1f}（偏多）")
            elif sc < 0:
                bear.append(f"基本面评分 {sc:+.1f}（偏空）")
        for ptxt in (fund_row.get("parts") or []):
            if "偏多" in ptxt:
                bull.append("基本面·" + ptxt)
            elif "偏空" in ptxt:
                bear.append("基本面·" + ptxt)
        for g in (fund_row.get("gaps") or []):
            bull.append("（数据缺口，不计分）" + g) if False else None

    # —— 量化验证 ——
    if quant and quant.get("n", 0) > 0:
        if quant.get("valid"):
            bull.append(f"量化背书：近 {quant['n']} 笔、胜率 {quant['win']*100:.0f}%、"
                        f"期望 {quant['expR']:+.2f}R（规则在该品种历史有效）")
        else:
            bear.append(f"量化未背书：近 {quant.get('n',0)} 笔、期望 {quant.get('expR',0):+.2f}R"
                        f"（样本不足或历史无效，谨慎）")
    if macro_regime:
        bull.append("宏观：" + macro_regime) if False else None

    def score(lst):
        s = 0.0
        for x in lst:
            if "引擎今日给出" in x:
                s += 2
            elif x.startswith("基本面评分"):
                s += 1.5
            elif x.startswith("方向层"):
                s += 1
            elif x.startswith("量化"):
                s += 1
            elif x.startswith("ADX"):
                s += 0.5
            elif x.startswith("基本面·"):
                s += 0.5
            else:
                s += 0.3
        return round(s, 1)

    return bull, bear, score(bull), score(bear)


def conflict_flags(tech, fund_row, quant, bull_s, bear_s):
    """技术 / 基本面 / 量化 三方冲突检测 → 标记列表"""
    flags = []
    d = tech.get("direction") or ""
    fs = (fund_row or {}).get("score") or 0
    qv = (quant or {}).get("valid")
    qe = (quant or {}).get("expR")
    tech_dir = 1 if d == "多头" else (-1 if d == "空头" else 0)
    fund_dir = 1 if fs > 0 else (-1 if fs < 0 else 0)
    if tech_dir and fund_dir and tech_dir * fund_dir < 0:
        flags.append(f"🔴 **技术 vs 基本面背离**：技术{d}，基本面{fs:+.1f}——两者相反，"
                     "按框架要求不自动采信，需人工/LLM 定夺（倾向减仓或等共振）")
    if qv is False and tech.get("action") in ("做多", "做空"):
        flags.append(f"🟠 **量化未背书**：今日有{tech['action']}信号，但该品种历史期望 "
                     f"{qe if qe is not None else 0:+.2f}R 未通过有效性门槛——可降级为观察")
    if tech_dir and fund_dir and tech_dir * fund_dir > 0:
        flags.append(f"🟢 **技术+基本面共振**（同向），置信度提升")
    if abs(bull_s - bear_s) < 0.6 and (bull_s + bear_s) > 0:
        flags.append("🟡 多空论据强度接近（分歧大），建议等方向确认或减半仓")
    return flags


# =====================================================================
# 主流程
# =====================================================================
def _auto_roots():
    """每日流水线用：只取『今日有做多/做空动作』+『当前持仓』的品种，避免 49 品种全量回测。"""
    sel = []
    try:
        tech = load_tech() or {}
        import _fut_daily_brief as _B
        for x in (tech.get("results") or []):
            act = None
            try:
                act = (_B.fusion_signal(dict(x)) or {}).get("action")
            except Exception:
                act = x.get("action")
            if act in ("做多", "做空"):
                r = (x.get("root") or "").strip().upper()
                if r:
                    sel.append(r)
    except Exception as e:
        print(f"[auto] 读取技术快照失败: {e}")
    try:
        import _fut_positions as _P
        df = _P.load_positions()
        if df is not None and len(df) and "代码" in df.columns:
            for c in df["代码"].astype(str):
                r = (_P.norm_root(c.strip()) or "").strip().upper()
                if r:
                    sel.append(r)
    except Exception as e:
        print(f"[auto] 读取持仓台账失败: {e}")
    seen, out = set(), []
    for r in sel:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


def build_research(roots=None, with_quant=True, date=None):
    date = date or datetime.date.today().strftime("%Y-%m-%d")
    tech_payload = load_tech() or {}
    fund = load_fund() or {}
    results = tech_payload.get("results") or []
    if roots:
        roots = set(r.strip().upper() for r in roots)
        results = [x for x in results if x.get("root") in roots]
    fund_map = fund.get("by_root", fund) if isinstance(fund, dict) else {}
    macro_regime = fund.get("macro_regime", "") if isinstance(fund, dict) else ""

    quant_map = {}
    if with_quant:
        rlist = [x["root"] for x in results if x.get("root")]
        data = daily_continuous(rlist)
        for root, df in data.items():
            try:
                st = fusion_backtest(df)
                if st and st.get("n", 0) > 0:
                    quant_map[root] = st
            except Exception:
                continue

    by_root = {}
    for x in results:
        root = x.get("root")
        if not root:
            continue
        fr = fund_map.get(root)
        q = quant_map.get(root)
        bull, bear, bs, bes = bull_bear(x, fr, q, macro_regime)

        flags = conflict_flags(x, fr, q, bs, bes)
        by_root[root] = dict(
            name=x.get("name") or root, symbol=x.get("symbol"),
            action=x.get("action"), direction=x.get("direction"),
            bull=bull, bear=bear, bull_score=bs, bear_score=bes,
            quant=q, flags=flags,
            verdict=("偏多" if bs > bes else ("偏空" if bes > bs else "分歧")),
        )
    return dict(date=date,
                generated_at=datetime.datetime.now().isoformat(timespec="seconds"),
                macro_regime=macro_regime,
                quant_enabled=bool(with_quant),
                quant_cover=len(quant_map),
                by_root=by_root)


def research_path(date=None):
    d = (date.strftime("%Y%m%d") if hasattr(date, "strftime") else str(date or "").replace("-", ""))
    if not d:
        d = datetime.date.today().strftime("%Y%m%d")
    return os.path.join(PROC_DIR, f"_brief_research_{d}.json")


def save_research(res, date=None):
    p = research_path(date or res.get("date"))
    with open(p, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=1, default=str)
    return p


def load_research(date=None):
    p = research_path(date)
    return load_json(p)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--roots", default="")
    ap.add_argument("--date", default=None)
    ap.add_argument("--auto", action="store_true",
                    help="只算『今日有做多/做空动作 + 当前持仓』品种（快；供每日流水线用）")
    ap.add_argument("--no-quant", action="store_true", help="跳过回测（只做论据合成）")
    a = ap.parse_args()
    roots = [x.strip().upper() for x in a.roots.split(",") if x.strip()] or None

    if a.auto:
        roots = _auto_roots()
        print(f"[auto] 聚焦品种（今日有动作 ∩ 持仓）: {roots or '无'}")
    res = build_research(roots, with_quant=not a.no_quant, date=a.date)
    p = save_research(res, a.date)
    print(f"研究层已落盘: {p}")
    print(f"品种数 {len(res['by_root'])}｜量化覆盖 {res['quant_cover']}｜宏观 {res['macro_regime']}")
    rows = sorted(res["by_root"].items(), key=lambda kv: -abs(kv[1]["bull_score"] - kv[1]["bear_score"]))
    print("\n研究结论（按多空强度差排序，前 10）：")
    for r, v in rows[:10]:
        q = v.get("quant") or {}
        qs = f"量化 n={q.get('n',0)} expR={q.get('expR',0):+.2f} {'✔' if q.get('valid') else '✘'}" if q else "量化 N/A"
        print(f"  {r:<4} {v['name']:<6} 结论={v['verdict']:<3} 多{v['bull_score']}/空{v['bear_score']} ｜ {qs}")
        for f_ in v["flags"][:2]:
            print(f"        {f_}")
    nc = sum(1 for v in res["by_root"].values() if v["flags"])
    print(f"\n带冲突/共振标记的品种: {nc}")
