"""
V3.4 验收脚本（等价性验证）
==========================
在【生产真数据源 fut_kline 连续主连】上跑 CB 引擎 walk_fusion_states，
再用研究侧容量5 FIFO + real_cost(1.0) 回放，核对研发锚点：
  events=16400 / executed=3226 / win_rate=23.5% / net=+69.6万 / mdd=16.8万 / 加码1583

用法：docker exec qhyc-api python /app/scripts/accept_v34.py
"""
from __future__ import annotations
import json, sys, os
sys.path.insert(0, "/app")
import numpy as np
import pandas as pd
from sqlalchemy import text

from app.core.db import session_scope
from app.core.config import get_settings
from app.core.logging import logger
from app.backtest.fusion_backtest import FusionBacktestParams, _htf_direction
from app.strategies.fusion_signal import walk_fusion_states

# ---- 真实成本口径（抄自 _fusion_realcost_governance.py，研究侧 real_cost(1.0)）----
SPEC = {
 "FG":(1,6),"SA":(1,3.5),"SR":(1,3),"CF":(5,4.3),"TA":(2,3),"MA":(1,2),"RM":(1,1.5),
 "OI":(1,2),"AP":(1,5),"UR":(1,5),"SH":(1,4),"PX":(2,3),"SF":(2,3),"SM":(2,3),
 "RB":(1,4),"HC":(1,4),"SS":(5,4),"BU":(1,4),"RU":(5,5),"SP":(2,5),"AO":(1,6),
 "CU":(10,17),"AL":(5,3),"ZN":(5,3),"PB":(5,3),"NI":(10,3),"SN":(10,3),"AU":(0.02,10),
 "AG":(1,5),"FU":(1,2),"SC":(0.1,20),"SI":(5,6),"LC":(20,8),
 "A":(1,2),"B":(1,1),"M":(1,1.5),"Y":(2,2.5),"P":(2,3),"C":(1,1.2),"CS":(1,1.5),
 "JD":(1,8),"L":(1,1),"V":(1,1),"PP":(1,3),"J":(0.5,15),"JM":(0.5,15),"I":(0.5,10),
 "EG":(1,4),"EB":(1,3),"LH":(5,20),
}
def prod_of(sym):  # "FG888" -> "FG"
    return sym[:-3].upper()


def real_cost_per_lot(sym, mult, price=None, cost_mode="dict", on_date=None):
    """一次开平的**每手**成本（元）。

    ⚠ 2026-10-03（用户拍板"真乘数口径"）：``SPEC`` 这份硬编码表把**百分比费率当成
    固定元/手**，导致严重低估 —— CU 写 17 元/手，而交易所实际为 **0.5‰**，
    按 78000 计 = 195 元/手（差 11 倍）。故默认改用库内 ``dim_trading_cost``
    （交易所标准 + 券商 1 分 + 滑点 1 跳/边）。

    :param price: 成交价。PCT 费率按成交额计，缺价时退回 legacy 口径。
    :param cost_mode: ``dict``（默认，真实费率）/ ``legacy``（旧 SPEC，仅作对照）
    :param on_date: **建仓日**（2026-10-07 新增）。不传则用「今天」的费率算历史 ——
        滚动费率表的意义正是在此，历史基准必须按建仓日取费率。
        另按建仓日反查当时主力合约，使「特定合约」档位（如螺纹 1/5/10 月 1‱
        而其余 0.2‱）能够命中，避免主力合约成本低估 5 倍。
    """
    if cost_mode == "legacy" or price is None:
        tick, fee = SPEC[prod_of(sym)]
        return 2 * fee + 2 * 1.0 * (tick * mult)   # 旧：往返 1 跳滑点 + 双边手续费
    from app.data.cost import (cost_coefficients, cost_yuan_at,
                               main_contract_at, _variety as _vc)
    d0 = None
    ctr = None
    if on_date is not None:
        d0 = on_date.date() if hasattr(on_date, "date") else on_date
        ctr = main_contract_at(_vc(sym), d0)
    return cost_yuan_at(
        cost_coefficients(sym, contract=ctr, close_action="CLOSE_YEST", on_date=d0),
        price)


ANCHOR = dict(events=16400, executed=3226, win_rate=0.2349659,
              net=695715.0, mdd=167865.2, mar=4.1445, addons=1583)

#: ★ 锚点的可复现性元数据（2026-10-07 补记）
#:
#: 锚点来自**已退役的数据源** ``fut_kline(freq='hourly', kind='continuous')``，
#: 且**没有记录品种列表与时间窗口**。G9 退役 continuous 后该口径已无法复现，
#: 本脚本改用 ``hourly_bar`` 的 888 主力连续（2020-02 起，多数品种 2021+ 才
#: 有数据）。因此：
#:
#:   **锚点与当前实测不在同一数据集上，`ok` 判定不成立、不可作为验收依据。**
#:
#: 2026-10-07 实测（云端权威库，50 品种）：信号事件 13432 vs 锚点 16400（-2968）；
#: 且实测**加仓口径无关**——`add_max_lots=2`/`add_on_times=2/3` 三种口径下
#: 信号事件恒为 13432（仅加仓手数 6487/10413/13046 变化），故该偏差**不是**
#: 「总手数上限 vs 加仓次数」语义映射导致（已排除工单假设 1）。
#:
#: 结论：偏差来自**数据窗口/品种集合差异**。后续验收应以「同一脚本、同一数据源
#: 的自洽重跑」为准（`--save-baseline` 落盘后作新锚点），而非继续比对legacy 数字。
ANCHOR_META = dict(
    source="fut_kline(freq='hourly', kind='continuous') [已随 G9 退役]",
    reproducible=False,
    note="锚点未记录品种列表/时间窗口，与当前 hourly_bar 口径不同数据集；"
         "2026-10-07 实测偏差 -2968 已排除加仓语义映射，主因为数据窗口差异。",
)

#: 是否把当前实测落盘为新基线（`--save-baseline`），用作后续自洽回归的锚点。
SAVE_BASELINE = False

def _resolve_fut_symbol(session, product, exchange):
    """解析 hourly_bar 里的 888 主力连续码。

    ★ 2026-10-07 数据源切换（G9 退役 continuous）：
      原实现返回天勤码 ``KQ.m@{exch}.{PROD}`` 并读 ``fut_kline(freq='hourly',
      kind='continuous')``。但 **continuous 已随 G9 退役、该口径 0 行**（实测
      ``kind='continuous'`` 无任何数据；fut_kline 现只有 ``kind='contract'``），
      导致本脚本「品种覆盖 0/0」、全部指标为 0 —— 是静默失效而非报错。
      现改用 ``hourly_bar`` 的 888 主力连续（50 品种 / 2020-02~2026-09 / 38.5 万行），
      与 ``app/backtest/fusion_backtest.py:_read_hourly`` **同源同口径**，
      保证本验收脚本与融合回测的数字可比。
    """
    for cand in (f"{product.upper()}888", f"{product.lower()}888"):
        n = session.execute(text(
            "SELECT count(*) FROM hourly_bar WHERE symbol=:s"), {"s": cand}).scalar()
        if n:
            return cand
    return None


def load_fut_kline(session, symbol):
    """读 888 主力连续小时线（``hourly_bar``），返回 [dt,open,high,low,close]。"""
    q = text(
        "SELECT trade_datetime, open, high, low, close FROM hourly_bar "
        "WHERE symbol=:s ORDER BY trade_datetime ASC")
    rows = session.execute(q, {"s": symbol}).all()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["dt","open","high","low","close"])
    for c in ("open","high","low","close"):
        df[c] = df[c].astype(float)
    return df

def build_positions(df, p, legacy_align=False):
    """在单品种上跑 CB 引擎，重建“头寸”（含加码手数/首仓价/各手入场价）。

    legacy_align=True 复刻历史错位写法：walk_fusion_states 产出的第 k 个状态实际描述的是
    第 k 根 K 线**之后**的仓位意图，却直接配 c[k]，等效于提前 2 根成交 → 前视偏差
    （表现为虚假的 89% 胜率、852 万净利）。
    legacy_align=False（默认，正确）：状态右移 2 根对齐，与 fusion_backtest._generate_states
    口径一致。
    """
    o=df["open"].to_numpy(float); h=df["high"].to_numpy(float)
    l=df["low"].to_numpy(float); c=df["close"].to_numpy(float)
    dts=df["dt"].to_numpy()
    htf=_htf_direction(c, p.ema_k)
    n=len(c)
    if legacy_align:
        states=[]; lots=[]
        for d in walk_fusion_states(o,h,l,c,htf,p):
            states.append(d["state"]); lots.append(int(d.get("lots",1) or 1))
        states=np.array(states); lots=np.array(lots)
        dts=dts[:len(states)]
        o,h,l,c = o[:len(states)],h[:len(states)],l[:len(states)],c[:len(states)]
    else:
        states=np.zeros(n,dtype=int); lots=np.zeros(n,dtype=int)
        for k,d in enumerate(walk_fusion_states(o,h,l,c,htf,p)):
            if k+2 < n:
                states[k+2]=d["state"]
                lots[k+2]=int(d.get("lots",1) or 1)
    positions=[]
    cur=None  # dict: dir, first_px, exit_px, entry_dt, exit_dt, lot_pxs(list)
    for i in range(len(states)):
        st=int(states[i]); lt=int(lots[i])
        if cur is None:
            if st!=0:
                cur=dict(dir=st, first_px=float(c[i]), entry_dt=dts[i],
                         exit_dt=None, exit_px=None, lot_pxs=[float(c[i])])
        else:
            if st==cur["dir"] and lt>len(cur["lot_pxs"]):
                for _ in range(lt-len(cur["lot_pxs"])):
                    cur["lot_pxs"].append(float(c[i]))
            elif st!=cur["dir"]:
                # 平仓或反手：先平旧仓
                cur["exit_px"]=float(c[i]); cur["exit_dt"]=dts[i]
                positions.append(cur); cur=None
                if st!=0:
                    cur=dict(dir=st, first_px=float(c[i]), entry_dt=dts[i],
                             exit_dt=None, exit_px=None, lot_pxs=[float(c[i])])
    if cur is not None:
        # 末根仍持仓：以最后收盘价强平（不计入已平仓）
        cur["exit_px"]=float(c[-1]); cur["exit_dt"]=dts[-1]
        cur["open_at_end"]=True
        positions.append(cur)
    return positions

def replay_capacity5(positions, mult_map, pnl_mode="perlot", cost_mode="dict"):
    """逐字复刻研究侧 replay4：容量5 FIFO + real_cost(1.0)。
    事件口径：events = 全部头寸（含末根强平）；末根强平在入场时 continue（不计入 executed/cap_skip）。

    pnl_mode:
      "perlot"（默认，正确）—— 各手按各自实际入场价独立计价。加码手的入场价通常劣于首仓价，
        按首仓价统一计价会系统性高估利润（实测高估 209 万）。
      "first" —— 研究侧历史近似：全部手数按首仓价计价。仅用于复现旧数字。

    历史 bug：默认口径曾为 "first"，导致净利虚高。
    """
    tl=[]
    for pos in positions:
        tl.append((pos["entry_dt"],0,pos))          # 每个头寸都进时间线（含 open_at_end）
        if not pos.get("open_at_end"):
            tl.append((pos["exit_dt"],1,pos))
    tl.sort(key=lambda x:(x[0],x[1]))
    open_pos={}
    eq=peak=mdd=0.0
    wins=losses=0
    executed=cap_skip=0
    added_exec=0  # 被执行且带加码的头寸数
    total_addon_lots=0
    worstY=0.0
    for dt,typ,pos in tl:
        sym=pos["sym"]
        if typ==1:
            if sym in open_pos:
                pp=open_pos.pop(sym)
                eq+=pp["Y"]
                if pp["win"]: wins+=1
                else: losses+=1
                worstY=min(worstY,pp["Y"])
                peak=max(peak,eq); mdd=max(mdd,peak-eq)
        else:
            if pos.get("open_at_end"):
                continue  # 末根强平：仅占位，不执行不计数
            lots=len(pos["lot_pxs"])
            if len(open_pos)>=5 or sym in open_pos:
                cap_skip+=1; continue
            mult=mult_map[sym]
            dirn=1.0 if pos["dir"]==1 else -1.0
            first_px=pos["first_px"]; exit_px=pos["exit_px"]
            if pnl_mode=="perlot":
                gross=sum(dirn*(exit_px-px)*mult for px in pos["lot_pxs"])
                # 成本按**各自入场价**计（PCT 费率随成交额变动），与 gross 口径对齐；
                # 费率按**建仓日**取（2026-10-07：历史基准必须用当时费率，见 real_cost_per_lot）
                cost=sum(real_cost_per_lot(sym,mult,px,cost_mode,on_date=pos["entry_dt"])
                         for px in pos["lot_pxs"])
            else:
                # 研究近似：gross = 方向×(exit−首仓价)×mult×手数（ATR 抵消）
                gross=dirn*(exit_px-first_px)*mult*lots
                cost=real_cost_per_lot(sym,mult,first_px,cost_mode,
                                       on_date=pos["entry_dt"])*lots
            Y=gross-cost
            win = Y>0 if pnl_mode=="perlot" else (dirn*(exit_px-first_px))>0
            open_pos[sym]=dict(Y=Y, win=win)
            executed+=1
            if lots>1:
                added_exec+=1
                total_addon_lots+= (lots-1)
    n=wins+losses
    wr=wins/n if n else 0.0
    mar=eq/mdd if mdd>0 else float("inf")
    return dict(events=len(positions),
                executed=executed, cap_skip=cap_skip, win_rate=wr,
                net=eq, mdd=mdd, mar=mar, added_exec=added_exec,
                total_addon_lots=total_addon_lots, worstY=worstY)

def main():
    settings=get_settings()
    mc=settings.main_contracts
    # 2026-10-07（工单 add_max_lots 语义裁定）：加仓口径**显式可传**。
    # 默认 add_on_times=2 —— 与 WB `run_param_scan_v3` 的 `add_max_lots=2`
    # 同义（那边语义 = ADD 事件数上限，总手 = 1 + N）。用 add_on_times 而非
    # add_max_lots，避免「总手数上限」与「加仓次数」两种语义再次混用。
    p = FusionBacktestParams(
        add_max_lots=1,
        add_on_times=(ARGS.add_on_times if ARGS.add_on_times is not None else 2),
    )
    logger.info(
        f"[accept_v34] 加仓口径: add_on_times={p.add_on_times} "
        f"(= 加仓次数；总手数上限 = 1 + {p.add_on_times})")
    # 乘数真源 = dim_variety（库内字典）；config 仅作回退。
    # 2026-10-03：旧代码只读 config.main_contracts（YAML 硬编码），是代码内字典。
    from app.data.barstore import variety_spec, VarietySpecNotFoundError
    mult_map={}
    for m in mc:
        try:
            mult_map[m.symbol]=float(variety_spec(m.symbol)["multiplier"])
        except (VarietySpecNotFoundError, Exception):
            mult_map[m.symbol]=float(m.multiplier)
    # 费率覆盖前置检查（2026-10-03）：缺费率品种既不能按 0 成本、也不该用 legacy
    # 混入（会污染口径），故**整品种剔除**并显式报告，保证净额口径边界清晰。
    from app.data.cost import cost_coefficients, CostNotFoundError
    if COST_MODE=="dict":
        no_fee=[]
        for m in mc:
            try:
                cost_coefficients(m.symbol, close_action="CLOSE_YEST")
            except (CostNotFoundError, Exception):
                no_fee.append(m.symbol)
        if no_fee:
            print(f"⚠ 费率缺失（将整品种剔除，不参与净额统计）：{no_fee}")
            mc=[m for m in mc if m.symbol not in no_fee]
            mult_map={k:v for k,v in mult_map.items() if k not in no_fee}
            print(f"  有效品种：{len(mc)}（原 {len(settings.main_contracts)}）")
    all_pos=[]
    per_sym_events={}
    n_sym=0; skipped=[]
    with session_scope() as s:
        for m in mc:
            fk=_resolve_fut_symbol(s, m.product, m.exchange)
            df=load_fut_kline(s, fk) if fk else None
            if df is None or len(df)<p.min_bars:
                skipped.append((m.symbol, fk or f"KQ.m@{m.exchange}.{m.product}",
                                0 if df is None else len(df)))
                continue
            pos=build_positions(df, p, legacy_align=LEGACY_ALIGN)
            for pp in pos:
                pp["sym"]=m.symbol
            nev=len([x for x in pos if not x.get("open_at_end")])
            per_sym_events[m.symbol]=nev
            all_pos.extend(pos)
            n_sym+=1
    res=replay_capacity5(all_pos, mult_map, pnl_mode=PNL_MODE, cost_mode=COST_MODE)
    res_legacy=None
    if COST_MODE=="dict":
        # 同时跑一遍旧口径，仅作对照（不作为结论）
        res_legacy=replay_capacity5(all_pos, mult_map, pnl_mode=PNL_MODE, cost_mode="legacy")
    # 报告
    print("="*60)
    print("V3.4 验收：CB 引擎(walk_fusion_states) @ fut_kline 连续主连")
    print(f"成本口径：{COST_MODE}  （dict=库内真实费率；legacy=旧 SPEC 硬编码，仅对照）")
    print("="*60)
    print(f"品种覆盖：{n_sym}/{len(mc)}  （跳过 {len(skipped)} 个：{skipped[:5]}）")
    print()
    print("【A. 信号层等价性】与成本无关，仍对研发锚点逐位核对")
    print(f"{'指标':<16}{'CB实测':>14}{'研发锚点':>14}{'偏差':>12}")
    print("-"*56)
    sig_rows=[
    ("信号事件", res["events"], ANCHOR["events"], res["events"]-ANCHOR["events"]),
    ("成交(executed)", res["executed"], ANCHOR["executed"], res["executed"]-ANCHOR["executed"]),
    ("容量拒(cap_skip)", res["cap_skip"], ANCHOR["events"]-ANCHOR["executed"], res["cap_skip"]-(ANCHOR["events"]-ANCHOR["executed"])),
    ("胜率%", round(res["win_rate"]*100,2), round(ANCHOR["win_rate"]*100,2), round((res["win_rate"]-ANCHOR["win_rate"])*100,2)),
    ("加码头寸(被执行)", res["added_exec"], ANCHOR["addons"], res["added_exec"]-ANCHOR["addons"]),
    ("加码手数(被执行)", res["total_addon_lots"], ANCHOR["addons"], res["total_addon_lots"]-ANCHOR["addons"]),
    ]
    for nm,a,b,dev in sig_rows:
        print(f"{nm:<16}{a:>14}{b:>14}{dev:>12}")
    print()
    print("【B. 金额指标】随成本口径变化，旧锚点不可直接比对")
    print(f"{'指标':<16}{'真实费率':>14}{'旧口径':>14}{'旧锚点':>14}")
    print("-"*56)
    lg = res_legacy if res_legacy else res
    for nm, key in (("净¥(万)","net"), ("最大回撤¥(万)","mdd"), ("MAR","mar")):
        print(f"{nm:<16}{round(res[key]/1e4,1) if key!='mar' else round(res[key],2):>14}"
              f"{round(lg[key]/1e4,1) if key!='mar' else round(lg[key],2):>14}"
              f"{round(ANCHOR[key]/1e4,1) if key!='mar' else round(ANCHOR[key],2):>14}")
    if res_legacy:
        d=(res["net"]-res_legacy["net"])/1e4
        print(f"\n  真实费率 vs 旧口径：净利 {d:+.1f} 万"
              f"（旧口径把百分比费率当固定元/手，如 CU 写 17 而实际 0.5‰≈195）")
    # 判定：仅信号层（成本无关）参与等价性判定；金额指标随口径变化，不做等价判定
    tol_events=200
    ok = (abs(res["events"]-ANCHOR["events"])<=tol_events and
          abs(res["executed"]-ANCHOR["executed"])<=tol_events and
          abs(res["win_rate"]-ANCHOR["win_rate"])<=0.01)
    print("-"*56)
    # ★ 2026-10-07：锚点来自已退役数据源、且未记录品种/窗口 → 判定不成立。
    #   继续输出「❌ 需排查」会把「数据集不同」误导成「引擎回归」，必须明示。
    if not ANCHOR_META["reproducible"]:
        print("等价性判定：**不适用**（锚点不可复现，非引擎回归）")
        print(f"  原因：{ANCHOR_META['note']}")
        print(f"  锚点源：{ANCHOR_META['source']}")
        print("  处置：以「同一脚本 + 同一数据源」自洽重跑为准 —— "
              "加 --save-baseline 落盘为新基线，后续回归比对新基线。")
        ok = None      # tri-state：None = 不适用，避免下游把 None 当 False
    else:
        print("等价性判定（信号层）：", "✅ 通过（引擎信号层与研发逐位一致）" if ok else "❌ 偏差超容差，需排查")
    print(f"金额基准（{COST_MODE} 口径）：净 {res['net']/1e4:.1f} 万 / 回撤 {res['mdd']/1e4:.1f} 万 / MAR {res['mar']:.2f}")
    print("  ⚠ 旧锚点 +69.6 万系 legacy 成本口径，已作废，不得再作为验收基准。")
    print(f"  （加仓口径：add_on_times={p.add_on_times} → 总手数上限 "
          f"{1 + (p.add_on_times or 0)} 手）")
    # 落盘
    out=dict(cb=res, anchor=ANCHOR, anchor_meta=ANCHOR_META, passed=ok,
             add_on_times=p.add_on_times, n_sym=n_sym, skipped=skipped,
             per_sym_events=per_sym_events)
    with open("/app/runtime/accept_v34_result.json","w",encoding="utf-8") as f:
        json.dump(out,f,ensure_ascii=False,default=str)
    print("结果已写 /app/runtime/accept_v34_result.json")
    if SAVE_BASELINE:
        # 自洽基线：同一脚本 + 同一数据源 + 同一加仓口径，可复现 → 可作回归锚点
        base=dict(events=res["events"], executed=res["executed"],
                  win_rate=res["win_rate"], addons=res["total_addon_lots"],
                  net=res["net"], mdd=res["mdd"], mar=res["mar"])
        with open("/app/runtime/accept_v34_baseline.json","w",encoding="utf-8") as f:
            json.dump(dict(source="hourly_bar 888 主力连续",
                           add_on_times=p.add_on_times, n_sym=n_sym,
                           reproducible=True, baseline=base),
                      f, ensure_ascii=False, default=str)
        print(f"新基线已写 /app/runtime/accept_v34_baseline.json"
              f"（add_on_times={p.add_on_times}，n_sym={n_sym}）——后续回归比对此基线")

if __name__=="__main__":
    import argparse
    _ap=argparse.ArgumentParser(description="V3.4 验收（等价性验证）")
    _ap.add_argument("--legacy-align", action="store_true",
                     help="复刻历史错位对齐（前视），仅用于回归对比，勿作为结论")
    _ap.add_argument("--pnl-mode", default="perlot", choices=["perlot","first"],
                     help="perlot=逐手独立计价(正确,默认) / first=研究侧旧近似(高估)")
    _ap.add_argument("--cost-mode", default="dict", choices=["dict","legacy"],
                     help="dict=库内真实费率 dim_trading_cost(默认,用户2026-10-03拍板) / "
                          "legacy=旧 SPEC 硬编码(把百分比费率当固定元/手,仅作对照)")
    _ap.add_argument("--add-on-times", type=int, default=None,
                     help="加仓次数(默认 2)。与 WB run_param_scan_v3 的 "
                          "add_max_lots 同义(那边=ADD 事件数上限，总手=1+N)。"
                          "注意 CB 的 add_max_lots 是「总手数上限」，语义不同，勿混用。")
    _ap.add_argument("--save-baseline", action="store_true",
                     help="把本次实测落盘为新基线(accept_v34_baseline.json)。"
                          "旧锚点来自已退役的 fut_kline continuous、不可复现，"
                          "故自洽基线才是后续回归的正确锚点。")
    _a=_ap.parse_args()
    LEGACY_ALIGN=_a.legacy_align
    PNL_MODE=_a.pnl_mode
    COST_MODE=_a.cost_mode
    ARGS=_a
    SAVE_BASELINE=_a.save_baseline
    main()
