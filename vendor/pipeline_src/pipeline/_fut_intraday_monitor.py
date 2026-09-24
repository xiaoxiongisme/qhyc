# -*- coding: utf-8 -*-
"""
盘中监控（M2 P4 · 7×24 无人值守的监控端）
========================================
职责（每小时整点由调度触发一次）：
  1. 载入当日简报技术快照 `_brief_tech_YYYYMMDD.json` → 各品种基准
     （direction / ema10 / ema20 / atr / entry / stop / target / breakeven / 活跃合约号）
  2. 载入持仓台账 `_positions.csv` → 实际持仓的方向/手数/止损/止盈
  3. 对「**持仓品种 + 今日有信号品种**」集合，**live 抓取 60 分钟线最新价**
     （akshare `futures_zh_minute_sina`；⚠️ 不读 fut_kline——它滞后约 4 个交易日）
  4. 判定事件：触及止损 / 触及止盈 / 触及保本 / 加仓触发 / 方向层疑似反转 / 活跃合约换月 / 行情缺失
  5. 与**上一整点状态快照** diff → 只输出**新增事件**（避免重复轰炸）
  6. 写 `_intraday_state.json`（本次状态）+ `_intraday_alerts_YYYYMMDD.json`（本次新增告警），
     并打印可直接推送的文案

用法：
  python _fut_intraday_monitor.py              # 跑一次；有告警打印推送文案，无告警打印「无新事件」
  python _fut_intraday_monitor.py --json       # 输出 JSON（供推送通道消费）
  python _fut_intraday_monitor.py --quiet      # 无告警时静默

诚实口径：盘中不重算完整日线融合信号（需完整日线 EMA 上下文，代价过高）。
方向层沿用当日简报的日线结论，盘中只做**价格层面的触发检查**与「价格穿越 EMA20」的软提示。
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
import pandas as pd
try:
    import akshare as ak
except Exception:
    ak = None
import _fut_positions as POS
from _fut_active_contract import active_contract

BASE_DIR = os.environ.get("QH_BRIEF_BASE", r"E:/QH/期货简报")
HOLD_DIR = os.path.join(BASE_DIR, "持仓情况和手续费")
PROC_DIR = os.path.join(BASE_DIR, "过程思考")
os.makedirs(PROC_DIR, exist_ok=True)

STATE_PATH = os.path.join(PROC_DIR, "_intraday_state.json")


# ---------------------------------------------------------------- 工具
def _num(v):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        return float(v)
    except Exception:
        return None


def latest_tech_json():
    """找最新的 _brief_tech_*.json"""
    fs = sorted(glob.glob(os.path.join(PROC_DIR, "_brief_tech_*.json")))
    if not fs:
        fs = sorted(glob.glob(os.path.join(HOLD_DIR, "_brief_tech_*.json")))
    return fs[-1] if fs else None


def load_tech():
    p = latest_tech_json()
    if not p:
        return None, None
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d, p
    except Exception:
        return None, p


def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("by_root", {}) or {}
    except Exception:
        return {}


def save_state(by_root):
    payload = dict(ts=datetime.datetime.now().isoformat(timespec="seconds"), by_root=by_root)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)


def _sym_candidates(sym):
    """新浪分钟线代码候选：标准码 + 郑商所 3 位写法（FG2610 → FG610）"""
    out = []
    s = str(sym or "").strip()
    if not s:
        return out
    out.append(s)
    root = POS.norm_root(s)
    digits = "".join(ch for ch in s if ch.isdigit())
    if root and len(digits) == 4:
        out.append(root + digits[1:])      # 3 位写法
    return out


def live_price(sym):
    """live 抓取最新价（60 分钟线优先，回退 15/5/1 分钟）。返回 (price, ts) 或 (None, None)"""
    if ak is None:
        return None, None
    for cand in _sym_candidates(sym):
        for period in ("60", "15", "5", "1"):
            try:
                df = ak.futures_zh_minute_sina(symbol=cand, period=period)
                if df is not None and len(df):
                    last = df.iloc[-1]
                    col = "close" if "close" in df.columns else df.columns[-1]
                    px = _num(last.get(col))
                    ts = str(last.get("datetime") or last.get("date") or "")
                    if px and px > 0:
                        return px, ts
            except Exception:
                continue
    return None, None


# ---------------------------------------------------------------- 事件判定
def evaluate_root(root, tech, pos_rows, specs):
    """返回 (events: list[str], price, sym)"""
    events = []
    sym = tech.get("symbol") or (root + "0")
    price, ts = live_price(sym)
    if price is None:
        events.append(f"⚠️盘中行情未取到（{sym}），跳过本次判定")
        return events, None, sym

    atr = _num(tech.get("atr")) or 0.0
    direction = tech.get("direction") or ""
    ema10 = _num(tech.get("ema10"))
    ema20 = _num(tech.get("ema20"))
    close_prev = _num(tech.get("close_prev"))
    ema10_prev = _num(tech.get("ema10_prev"))

    # 1) 持仓层面：止损 / 止盈 / 保本
    for _, r in pos_rows.iterrows():
        if POS.is_closed(r):
            continue
        dirc = str(r.get("方向", "")).strip()
        is_long = ("空" not in dirc) and ("S" not in dirc.upper() or "L" in dirc.upper())
        lots = _num(r.get("手数")) or 0.0
        if lots <= 0:
            continue
        stop = _num(r.get("止损位")) or _num(r.get("参考止损"))
        targ = _num(r.get("止盈位")) or _num(r.get("参考止盈"))
        be = _num(tech.get("breakeven"))
        if stop is not None:
            if is_long and price <= stop:
                events.append(f"⛔触及止损：现价 {price:,.0f} ≤ 止损 {stop:,.0f}（{lots:.0f} 手，次日/即时按纪律离场）")
            elif (not is_long) and price >= stop:
                events.append(f"⛔触及止损：现价 {price:,.0f} ≥ 止损 {stop:,.0f}（{lots:.0f} 手，次日/即时按纪律离场）")
        if targ is not None:
            if is_long and price >= targ:
                events.append(f"✅触及止盈：现价 {price:,.0f} ≥ 止盈 {targ:,.0f}（{lots:.0f} 手）")
            elif (not is_long) and price <= targ:
                events.append(f"✅触及止盈：现价 {price:,.0f} ≤ 止盈 {targ:,.0f}（{lots:.0f} 手）")
        if be is not None and atr > 0:
            if is_long and price >= be:
                events.append(f"🟡触及保本线：现价 {price:,.0f} ≥ 保本 {be:,.0f}（可考虑止损上移保本）")
            elif (not is_long) and price <= be:
                events.append(f"🟡触及保本线：现价 {price:,.0f} ≤ 保本 {be:,.0f}")

    # 2) 加仓触发（沿用 P3 口径：盈利顺势 + 回踩快线收回）
    for _, r in pos_rows.iterrows():
        if POS.is_closed(r):
            continue
        dirc = str(r.get("方向", "")).strip()
        is_long = ("空" not in dirc) and ("S" not in dirc.upper() or "L" in dirc.upper())
        entry = _num(r.get("买入点位"))
        lots = _num(r.get("手数")) or 0.0
        if entry is None or lots <= 0 or atr <= 0 or ema10 is None:
            continue
        profit_pts = (price - entry) if is_long else (entry - price)
        aligned = (direction == "多头" and is_long) or (direction == "空头" and (not is_long))
        reclaim = ((price > ema10 and (close_prev or 0) < (ema10_prev or 0)) if is_long
                   else (price < ema10 and (close_prev or 0) > (ema10_prev or 0)))
        if profit_pts >= POS.ADDON_MIN_PROFIT_ATR * atr and aligned and reclaim:
            events.append(f"🔺加仓触发：浮盈 {profit_pts/atr:.2f}×ATR、方向层{direction}顺势、"
                          f"回踩 EMA10({ema10:,.0f}) 后收回 → 可 +1 手（独立 2×ATR 止损）")

    # 3) 方向层软提示：价格穿越 EMA20（日线方向层的代理）
    if ema20 and direction in ("多头", "空头"):
        if direction == "多头" and price < ema20:
            events.append(f"🟠方向层预警：现价 {price:,.0f} 跌破 EMA20 {ema20:,.0f}"
                          f"（日线方向层仍记『多头』，收盘确认前不视为反转）")
        elif direction == "空头" and price > ema20:
            events.append(f"🟠方向层预警：现价 {price:,.0f} 站上 EMA20 {ema20:,.0f}"
                          f"（日线方向层仍记『空头』，收盘确认前不视为反转）")

    # 4) 换月检测（2026-09-23 起 live_heuristic：真实持仓量优先，防 RU2610 类错配）
    try:
        cur = active_contract(root, datetime.date.today(), strategy="live_heuristic")
        if cur and sym and cur != sym and not str(sym).endswith("0"):
            events.append(f"🔄活跃合约换月：{sym} → {cur}（旧仓注意移仓/平旧开新）")
    except Exception:
        pass

    return events, price, sym


# ---------------------------------------------------------------- 主流程
def run(json_out=False, quiet=False):
    tech_payload, tech_path = load_tech()
    if not tech_payload:
        msg = "盘中监控跳过：未找到当日简报技术快照（请先跑 `python _fut_daily_brief.py`）"
        if not quiet:
            print(msg)
        return {"ok": False, "reason": msg, "alerts": []}

    results = tech_payload.get("results") or []
    today = tech_payload.get("today") or datetime.date.today().strftime("%Y-%m-%d")
    tech_map = {x.get("root"): x for x in results if x.get("root")}

    df = POS.load_positions()
    pos_by_root = {}
    if len(df):
        for _, r in df.iterrows():
            root = POS.norm_root(str(r.get("代码", "")).strip())
            pos_by_root.setdefault(root, []).append(r)

    tracked = set(pos_by_root.keys())
    for x in results:
        if x.get("action") in ("做多", "做空"):
            tracked.add(x.get("root"))
    tracked = sorted(t for t in tracked if t in tech_map)

    prev_state = load_state()
    new_state, alerts = {}, []
    for root in tracked:
        tech = tech_map[root]
        rows = pd.DataFrame(pos_by_root.get(root, []))
        spec = None
        try:
            from _fut_specs import FUT_SPECS
            spec = FUT_SPECS.get(root, {})
        except Exception:
            spec = {}
        events, price, sym = evaluate_root(root, tech, rows, spec)
        new_state[root] = events
        before = set(prev_state.get(root, []) or [])
        for e in events:
            if e not in before:
                alerts.append(dict(root=root,
                                   name=tech.get("name") or root,
                                   symbol=sym, price=price, event=e))
    save_state(new_state)

    # 落盘本次告警
    # 口径修正（2026-09-22）：
    #   ① 目录 → HOLD_DIR（持仓情况和手续费），与调度任务约定的产出路径一致；
    #      旧版写在 PROC_DIR（过程思考），导致告警文件与持仓台账分离、下游找不到。
    #   ② 文件名日期 → 用**运行日**（datetime.date.today()），不用 tech 快照的 today；
    #      旧版沿用快照日期，简报隔日未重跑时会产生「昨天的文件名装今天的内容」。
    os.makedirs(HOLD_DIR, exist_ok=True)
    run_date = datetime.date.today().strftime("%Y-%m-%d")
    apath = os.path.join(HOLD_DIR, f"_intraday_alerts_{run_date.replace('-','')}.json")
    payload_out = dict(ts=datetime.datetime.now().isoformat(timespec="seconds"),
                       today=today, run_date=run_date, tech_snapshot=(tech_path or ""),
                       tracked=tracked, count=len(alerts), alerts=alerts)
    with open(apath, "w", encoding="utf-8") as f:
        json.dump(payload_out, f, ensure_ascii=False, indent=1)

    if json_out:
        print(json.dumps(payload_out, ensure_ascii=False, indent=1))
        return payload_out

    now = datetime.datetime.now().strftime("%m-%d %H:%M")
    if alerts:
        print(f"【盘中监控 {now}】新增事件 {len(alerts)} 条（跟踪 {len(tracked)} 品种）")
        for a in alerts:
            px = f"{a['price']:,.0f}" if a.get("price") else "—"
            print(f"· {a['name']} {a['root']} {a['symbol'] or ''}（现价 {px}）：{a['event']}")
        print(f"\n> 告警已落盘：{apath}")
    elif not quiet:
        print(f"【盘中监控 {now}】无新事件（跟踪 {len(tracked)} 品种，状态未变化）")
    return payload_out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="输出 JSON（供推送通道消费）")
    ap.add_argument("--quiet", action="store_true", help="无告警时静默")
    a = ap.parse_args()
    run(json_out=a.json, quiet=a.quiet)
