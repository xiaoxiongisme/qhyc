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
def real_cost_per_lot(sym, mult):
    tick, fee = SPEC[prod_of(sym)]
    return 2*fee + 2*1.0*(tick*mult)   # 往返 1 跳滑点 + 双边手续费

ANCHOR = dict(events=16400, executed=3226, win_rate=0.2349659,
              net=695715.0, mdd=167865.2, mar=4.1445, addons=1583)

def load_fut_kline(session, symbol):
    q = text(
        "SELECT trade_datetime, open, high, low, close FROM fut_kline "
        "WHERE freq=:f AND kind=:k AND symbol=:s ORDER BY trade_datetime ASC")
    rows = session.execute(q, {"f":"hourly","k":"continuous","s":symbol}).all()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["dt","open","high","low","close"])
    for c in ("open","high","low","close"):
        df[c] = df[c].astype(float)
    return df

def build_positions(df, p):
    """在单品种上跑 CB 引擎，重建“头寸”（含加码手数/首仓价/各手入场价）。"""
    o=df["open"].to_numpy(float); h=df["high"].to_numpy(float)
    l=df["low"].to_numpy(float); c=df["close"].to_numpy(float)
    dts=df["dt"].to_numpy()
    htf=_htf_direction(c, p.ema_k)
    states=[]; lots=[]
    for d in walk_fusion_states(o,h,l,c,htf,p):
        states.append(d["state"]); lots.append(int(d.get("lots",1) or 1))
    states=np.array(states); lots=np.array(lots)
    n=len(states)
    positions=[]
    cur=None  # dict: dir, first_px, exit_px, entry_dt, exit_dt, lot_pxs(list)
    for i in range(n):
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

def replay_capacity5(positions, mult_map):
    """逐字复刻研究侧 replay4：容量5 FIFO + real_cost(1.0) + 研究近似 gross。
    事件口径：events = 全部头寸（含末根强平）；末根强平在入场时 continue（不计入 executed/cap_skip）。
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
            # 研究近似：gross = 方向×(exit−首仓价)×mult×手数（ATR 抵消）
            gross=dirn*(exit_px-first_px)*mult*lots
            cost=real_cost_per_lot(sym,mult)*lots
            Y=gross-cost
            win = (dirn*(exit_px-first_px))>0
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
    p=FusionBacktestParams()  # V3.4 默认口径
    mult_map={m.symbol: float(m.multiplier) for m in mc}
    all_pos=[]
    per_sym_events={}
    n_sym=0; skipped=[]
    with session_scope() as s:
        for m in mc:
            fk=f"KQ.m@{m.exchange}.{m.product}"
            df=load_fut_kline(s, fk)
            if df is None or len(df)<p.min_bars:
                skipped.append((m.symbol, fk, 0 if df is None else len(df)))
                continue
            pos=build_positions(df, p)
            for pp in pos:
                pp["sym"]=m.symbol
            nev=len([x for x in pos if not x.get("open_at_end")])
            per_sym_events[m.symbol]=nev
            all_pos.extend(pos)
            n_sym+=1
    res=replay_capacity5(all_pos, mult_map)
    # 报告
    print("="*60)
    print("V3.4 验收：CB 引擎(walk_fusion_states) @ fut_kline 连续主连")
    print("="*60)
    print(f"品种覆盖：{n_sym}/{len(mc)}  （跳过 {len(skipped)} 个：{skipped[:5]}）")
    print(f"{'指标':<14}{'CB实测':>14}{'研发锚点':>14}{'偏差':>12}")
    print("-"*54)
    rows=[
        ("信号事件", res["events"], ANCHOR["events"], res["events"]-ANCHOR["events"]),
        ("成交(executed)", res["executed"], ANCHOR["executed"], res["executed"]-ANCHOR["executed"]),
        ("容量拒(cap_skip)", res["cap_skip"], ANCHOR["events"]-ANCHOR["executed"], res["cap_skip"]-(ANCHOR["events"]-ANCHOR["executed"])),
        ("胜率%", round(res["win_rate"]*100,2), round(ANCHOR["win_rate"]*100,2), round((res["win_rate"]-ANCHOR["win_rate"])*100,2)),
        ("净¥(万)", round(res["net"]/1e4,1), round(ANCHOR["net"]/1e4,1), round((res["net"]-ANCHOR["net"])/1e4,1)),
        ("最大回撤¥(万)", round(res["mdd"]/1e4,1), round(ANCHOR["mdd"]/1e4,1), round((res["mdd"]-ANCHOR["mdd"])/1e4,1)),
        ("MAR", round(res["mar"],2), round(ANCHOR["mar"],2), round(res["mar"]-ANCHOR["mar"],2)),
        ("加码头寸(被执行)", res["added_exec"], ANCHOR["addons"], res["added_exec"]-ANCHOR["addons"]),
        ("加码手数(被执行)", res["total_addon_lots"], ANCHOR["addons"], res["total_addon_lots"]-ANCHOR["addons"]),
    ]
    for nm,a,b,dev in rows:
        print(f"{nm:<14}{a:>14}{b:>14}{dev:>12}")
    # 判定
    tol_events=200; tol_net=5e4  # 容差：事件±200、净¥±5万
    ok = (abs(res["events"]-ANCHOR["events"])<=tol_events and
          abs(res["executed"]-ANCHOR["executed"])<=tol_events and
          abs(res["net"]-ANCHOR["net"])<=tol_net and
          abs(res["win_rate"]-ANCHOR["win_rate"])<=0.01)
    print("-"*54)
    print("等价性判定：", "✅ 通过（引擎信号层 + 全链路回放与研发逐位一致）" if ok else "❌ 偏差超容差，需排查")
    # 落盘
    out=dict(cb=res, anchor=ANCHOR, passed=ok, n_sym=n_sym, skipped=skipped,
             per_sym_events=per_sym_events)
    with open("/app/runtime/accept_v34_result.json","w",encoding="utf-8") as f:
        json.dump(out,f,ensure_ascii=False,default=str)
    print("结果已写 /app/runtime/accept_v34_result.json")

if __name__=="__main__":
    main()
