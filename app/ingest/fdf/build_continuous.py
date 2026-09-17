# -*- coding: utf-8 -*-
"""把未复权的「天勤主连 / 具体合约」重建为复权主连（加法平移前复权）。

算法逐字节对齐原始交付版 E:\\QH\\回测\\build_continuous.py（已验证回撤/笔数与
用户原始口径一致），只是把输入从「单文件 FG_contracts.json」改成「宽表接口」，
以便复用给 DDB 路径（adj_to_ddb.py）和本地 CSV 路径（_main_cli）。

为什么用加法平移前复权、不用乘法连乘：
  期货主连换月只存在「新旧合约价差」这一处断层，正确的修复是「把断层之前的
  历史整体平移 delta，使序列连续」。加法平移不改变任何两点之间的价差，因此
  不影响盈亏点数，只修复 ATR/MACD/分形等指标。乘法连乘会系统性抬高/压低全部
  历史价格，导致 ATR/止损止盈价位偏移、交易笔数与回撤金额全变——这正是之前
  FG 版回撤虚高、笔数对不上的根因。

做法（期货连续合约标准做法，与原始版一致）：
  1. 每日取持仓量最大的合约为主力；需连续 ROLL_CONFIRM 日领先才确认换月，避免抖动
  2. 主力只向前换（只接受到期更晚的合约），不回退
  3. 换月日 t：delta = 新合约(t-1)收盘 − 旧合约(t-1)收盘（两个合约在同一天的价差）
  4. 前复权：把 t 之前的所有历史价格整体加上 delta，使序列连续
  5. 剔除未走完的当日 K 线；截掉主力持仓量不达标的早期不可信区间
  6. 前段（下架合约区）用天勤主连平移接上
"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
from app.ingest.fdf.tqhelper import read_config, out_path, current_symbol  # noqa

ROLL_CONFIRM = 3          # 新合约持仓量需连续领先的天数
RELIABLE_OI = 100_000     # 主力合约持仓量门槛（手，默认；高流动性品种用此值）
RELIABLE_HOLD = 20        # 需连续达标的交易日数

# 低持仓量品种：单合约持仓量长期低于 100_000 手，用统一门槛会"识别不出可靠主力区间"
# 而永远无法复权。按品种给更低门槛（依据库内实测主力持仓峰值）：
#   SC 原油 峰值 ~6.1万 / SN 锡 ~7.7万 / PB 铅 ~13.7万 / RU 橡胶 ~19万（但 RU 合约符号异常，见下）
# 注意：RU 的 hourly contract 表只有一个被合并的符号 SHFE.ru2001，单降门槛会产出错误序列，
# 需先修数据再复权，故此处不下调 RU 门槛。
RELIABLE_OI_MAP = {
    "SC": 20_000,
    "SN": 25_000,
    "PB": 40_000,
}


def reliable_oi(product: str) -> int:
    """按品种返回主力持仓量门槛；未知/空品种回落默认 100_000。

    保证：对已经能正常复权的 47 个高流动性品种（不在 MAP 中）完全不改行为。
    """
    if not product:
        return RELIABLE_OI
    return RELIABLE_OI_MAP.get(str(product).upper(), RELIABLE_OI)


def _load_contracts_wide(contract_df):
    """contract_df 形式：列 [date, <合约1>, <合约2>, ...]，值为 close。
    为避免 open/high/low 失真，调用方应优先用 build_adjusted 的 frames 入口；
    此函数仅用于「只有 close+oi 宽表」的退化场景（DDB 路径未存 OHLC 时）。
    返回 (frames, close_wide, oi_wide)：
        frames     : {合约: DataFrame[date,open,high,low,close,volume,oi]}，open/high/low 退化为 close
        close_wide : 行=日期、列=合约、值=close
        oi_wide    : 行=日期、列=合约、值=oi（若 contract_df 不含 oi 列则退化为 close_wide）
    """
    df = contract_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").set_index("date")
    cols = [c for c in df.columns
            if c.lower() not in ("date", "open", "high", "low", "close", "volume", "oi", "datetime")]
    oi_cols = [c for c in df.columns if c.lower() == "oi"]

    frames = {}
    for c in cols:
        sub = pd.DataFrame({
            "open": df[c], "high": df[c], "low": df[c], "close": df[c],
            "volume": np.nan, "oi": (df[c + "_oi"] if f"{c}_oi" in df else df[c]),
        })
        sub.index = df.index
        frames[c] = sub

    close_wide = df[cols]
    oi_wide = df[oi_cols] if oi_cols else close_wide
    return frames, close_wide, oi_wide


def build_adjusted_from_frames(frames, raw_main_df=None, product: str = ""):
    """核心复权（与原始版 main() 逐字节对齐）。

    frames       : {合约: DataFrame[date,open,high,low,close,volume,oi]}，已切好早期残段
    raw_main_df  : 可选，天勤原始主连（前段下架合约区平移接上用）
    product      : 品种代码（如 "SC"），用于按品种取 OI 门槛；空则回落默认 100_000
    """
    if not frames:
        raise ValueError("具体合约数据为空，无法推断主力（复权锚点）")

    oi = pd.DataFrame({s: f["oi"] for s, f in frames.items()}).sort_index()
    cl = pd.DataFrame({s: f["close"] for s, f in frames.items()}).sort_index()

    dom = pick_dominant(oi).dropna()
    print(f"  主力序列覆盖 {dom.index[0].date()} ~ {dom.index[-1].date()}，共 {len(dom)} 个交易日")

    recs = []
    for d, s in dom.items():
        f = frames.get(s)
        if f is None or d not in f.index:
            continue
        r = f.loc[d]
        recs.append({"date": d, "symbol": s,
                     "open": r["open"], "high": r["high"],
                     "low": r["low"], "close": r["close"],
                     "volume": (r["volume"] if not pd.isna(r["volume"]) else 0),
                     "oi": (r["oi"] if not pd.isna(r["oi"]) else 0)})
    C = pd.DataFrame(recs).set_index("date").sort_index()

    rolls = []
    prev = None
    for d, s in C["symbol"].items():
        if prev is not None and s != prev:
            i = C.index.get_loc(d)
            pday = C.index[i - 1]
            a = cl.loc[pday, prev] if prev in cl.columns else np.nan
            b = cl.loc[pday, s] if s in cl.columns else np.nan
            if pd.notna(a) and pd.notna(b):
                rolls.append({"date": d, "old": prev, "new": s, "delta": float(b - a)})
            else:
                print(f"  警告：{d.date()} 换月（{prev}→{s}）缺少对照收盘价，该点未做复权")
                rolls.append({"date": d, "old": prev, "new": s, "delta": 0.0})
        prev = s
    print(f"  检出 {len(rolls)} 次换月；最早一日累计调整 {sum(r['delta'] for r in rolls):+.0f} 元/吨")

    adj = C.copy()
    for r in reversed(rolls):
        m = adj.index < r["date"]
        for c in ("open", "high", "low", "close"):
            adj.loc[m, c] = adj.loc[m, c] + r["delta"]

    if len(adj) > 21:
        ref = adj["volume"].iloc[-21:-1].median()
        while len(adj) and adj["volume"].iloc[-1] < ref * 0.5:
            print(f"  剔除未走完的 K 线 {adj.index[-1].date()}"
                  f"（量 {int(adj['volume'].iloc[-1]):,} vs 近20日中位 {int(ref):,}）")
            adj = adj.iloc[:-1]
            C = C.iloc[:-1]

    thr = reliable_oi(product)
    ok = adj["oi"] >= thr
    roll_ok = ok.rolling(RELIABLE_HOLD).sum()
    good = roll_ok[roll_ok >= RELIABLE_HOLD]
    if len(good) == 0:
        # 用 RuntimeError 而非 SystemExit：见 adjust_fdf，避免弄死编排器进程。
        raise RuntimeError("没有任何区间的主力持仓量达标，无法复权")
    start = adj.index[adj.index.get_loc(good.index[0]) - RELIABLE_HOLD + 1]
    dropped = int((adj.index < start).sum())
    print(f"  主力持仓量达标（≥{thr:,}手 连续{RELIABLE_HOLD}日）起点：{start.date()}"
          f"，丢弃此前 {dropped} 根（真主力已下架，识别不可信）")
    adj = adj.loc[start:]

    if raw_main_df is not None and len(raw_main_df) > 0:
        raw = raw_main_df.copy()
        raw["date"] = pd.to_datetime(raw["date"])
        raw = raw.set_index("date").sort_index()
        if start in raw.index:
            head = raw.loc[:start].iloc[:-1]
            if len(head):
                shift = float(adj.loc[start, "close"] - raw.loc[start, "close"])
                head = head.copy()
                for c in ("open", "high", "low", "close"):
                    head[c] = head[c] + shift
                head["symbol"] = "KQ.m@CZCE.FG(未复权)"
                # 天勤主连原始表不含 oi 列，前段未复权区 oi 置 0（与 cont_adj 写库约定一致）
                head["oi"] = 0
                adj["symbol"] = adj["symbol"].astype(str)
                adj = pd.concat([head[["open", "high", "low", "close", "volume", "oi", "symbol"]], adj])
                print(f"  前段接入天勤主连 {len(head)} 根（{head.index[0].date()} ~ "
                      f"{head.index[-1].date()}），整体平移 {shift:+.0f} 元/吨对齐")

    out_rows = []
    for d, r in adj.iterrows():
        is_adj = not str(r["symbol"]).startswith("KQ.m@")
        out_rows.append({
            "date": d.strftime("%Y-%m-%d"),
            "open": round(float(r["open"]), 2), "high": round(float(r["high"]), 2),
            "low": round(float(r["low"]), 2), "close": round(float(r["close"]), 2),
            "volume": int(r["volume"]), "oi": int(r["oi"]),
            "adj": bool(is_adj),
        })
    out = pd.DataFrame(out_rows)
    n_adj = int(out["adj"].sum())
    print(f"  写入复权主连 {len(out)} 根，区间 {out['date'].iloc[0]} ~ {out['date'].iloc[-1]}")
    print(f"    其中已复权 {n_adj} 根（{out[out['adj']]['date'].iloc[0]} 起），"
          f"未复权 {len(out)-n_adj} 根")
    return out


def pick_dominant(oi):
    """每日主力合约：持仓量最大且连续领先 ROLL_CONFIRM 日；只向前换。

    与原始版完全一致：用 oi.idxmax(axis=1) 取每日 oi 最大合约，仅当新合约比
    当前主力「到期更晚」（字符串比较，覆盖 FG010~FG609 区间）且连续领先 3 日才换。
    """
    top = oi.idxmax(axis=1)
    dom = []
    cur = None
    streak = 0
    cand = None
    for d in oi.index:
        t = top.loc[d]
        if pd.isna(t):
            dom.append(cur)
            continue
        if cur is None:
            cur = t
        elif t != cur:
            if t > cur:                 # 只接受到期更晚的合约
                if t == cand:
                    streak += 1
                else:
                    cand, streak = t, 1
                if streak >= ROLL_CONFIRM:
                    cur, cand, streak = t, None, 0
            # t < cur 说明近月持仓回升，不换
        else:
            cand, streak = None, 0
        dom.append(cur)
    return pd.Series(dom, index=oi.index)


def build_adjusted(cont_df=None, contract_df=None, oi_df=None, raw_main_df=None,
                   sym_col_map=None, frames=None, product: str = ""):
    """用未复权主连 + 具体合约两张表，算出复权主连 DataFrame（加法平移前复权）。

    两种调用方式：
      A. 真实 OHLC（推荐，对齐原始版）：
         build_adjusted(frames={合约: DataFrame[open,high,low,close,volume,oi]},
                        raw_main_df=天勤主连DataFrame)
      B. 退化宽表（仅 close+oi，DDB 未存 OHLC 时）：
         build_adjusted(cont_df, contract_df=close宽表, oi_df=oi宽表, raw_main_df)

    返回：复权后的 DataFrame，列 [date, open, high, low, close, volume, adj]，按日期升序。
          adj 为布尔：前段未复权区 False，已复权区 True。
    """
    if frames is not None and frames:
        return build_adjusted_from_frames(frames, raw_main_df=raw_main_df, product=product)

    # 退化路径：只有 close/oi 宽表
    if contract_df is None or len(contract_df) == 0:
        raise ValueError("具体合约数据为空，无法推断主力（复权锚点）")
    frames, _, _ = _load_contracts_wide(contract_df)
    if oi_df is not None and len(oi_df) > 0:
        _oi = oi_df.copy()
        _oi["date"] = pd.to_datetime(_oi["date"])
        _oi = _oi.set_index("date").sort_index()
        oi_cols = [c for c in _oi.columns
                   if c.lower() not in ("date", "open", "high", "low", "close", "volume", "oi", "datetime")]
        if oi_cols:
            for c in oi_cols:
                if c in frames:
                    frames[c]["oi"] = _oi[c].reindex(frames[c].index)
    return build_adjusted_from_frames(frames, raw_main_df=raw_main_df)


def _main_cli():
    """本地 CSV 路径：读 data/FG_daily.json（天勤主连）与 data/FG_contracts.json
    （具体合约），生成 data/FG_cont_adj.json。对齐原始版 main()。"""
    HERE = os.path.dirname(os.path.abspath(__file__))
    code = "FG"
    daily = json.load(open(os.path.join(HERE, "data", f"{code}_daily.json"), encoding="utf-8"))
    contracts = json.load(open(os.path.join(HERE, "data", f"{code}_contracts.json"), encoding="utf-8"))

    # 构造 frames / wide 形式（与原始版 load_contracts 对齐：切掉早期残段）
    frames = {}
    for sym, rows in contracts.items():
        d = pd.DataFrame(rows)
        d["date"] = pd.to_datetime(d["date"])
        d = d.sort_values("date").reset_index(drop=True)
        gap = d["date"].diff().dt.days
        cut = gap[gap > 180]
        if len(cut):
            d = d.iloc[cut.index[-1]:].reset_index(drop=True)
        frames[sym] = d.set_index("date")

    # close_wide 与 oi_wide
    close_wide = pd.DataFrame({s: f["close"] for s, f in frames.items()}).sort_index()
    oi_wide = pd.DataFrame({s: f["oi"] for s, f in frames.items()}).sort_index()

    out = build_adjusted(frames=frames, raw_main_df=pd.DataFrame(daily))

    path = out_path(f"data/{code}_cont_adj.json")
    json.dump(out.to_dict("records"), open(path, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"\n  写入 {path}：{len(out)} 根，{out['date'].iloc[0]} ~ {out['date'].iloc[-1]}")
    print(f"  复权方式：forward（加法平移前复权，对齐原始版）")


if __name__ == "__main__":
    _main_cli()
