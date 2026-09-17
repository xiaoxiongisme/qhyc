"""
融合策略「完整回测」—— 在 akshare(=同花顺)口径下逐品种 walk-forward 复现引擎逻辑，
验证 Step 0（单源读取）修复后策略是否仍然成立。

逻辑与 fusion_signal.fusion_state_detail 逐位一致（同一套循环、同一组参数），
仅额外记录每笔成交（FLAT→多/空 为开仓，多/空→FLAT 为离场，close-only 按收盘价判定）。

用法（容器内）：
  docker exec qhyc-scheduler python /app/runtime/_bt_fusion.py            # akshare(默认)
  docker exec qhyc-scheduler python /app/runtime/_bt_fusion.py --src tqsdk # 口径对照
"""
from __future__ import annotations
import sys, os, json, argparse
sys.path.insert(0, "/app")
import numpy as np
import pandas as pd
from app.core.config import get_settings
from app.core.db import session_scope
from app.strategies.fusion_signal import read_hourly_bars, drop_forming_bars, ema, atr14


def bt_symbol(o, h, l, c, htf_dir, p):
    """逐位复刻 fusion_state_detail 的前向循环，额外记录成交。

    返回 trades: list[(entry_i, entry_px, entry_atr, entry_dir, exit_i, exit_px, R)]
    dir: 1=多 2=空；未平仓的成交 exit_i=-1。
    """
    n = len(c)
    if n < 40:
        return []
    ma20 = ema(c, p.ma_n)
    atr_arr = atr14(h, l, c, p.atr_n)
    ok_l = htf_dir >= 0
    ok_s = htf_dir <= 0

    ph, pl = [], []
    state = 0
    entry_px = 0.0
    entry_i = -1
    entry_dir = 0
    cooldown = 0
    e_atr = 0.0
    peak = 0.0
    trough = 0.0
    be_done = False

    trades = []

    for i in range(2, n):
        if h[i - 1] > h[i - 2] and h[i - 1] > h[i]:
            ph.append(i - 1)
        if l[i - 1] < l[i - 2] and l[i - 1] < l[i]:
            pl.append(i - 1)
        while ph and ph[0] < i - p.W:
            ph.pop(0)
        while pl and pl[0] < i - p.W:
            pl.pop(0)
        if cooldown > 0:
            cooldown -= 1

        # ---------------- 离场 ----------------
        if state == 1:
            if c[i] > peak:
                peak = c[i]
            st = entry_px - p.sl_atr * e_atr
            if p.trail_atr > 0:
                t = peak - p.trail_atr * e_atr
                if t > st:
                    st = t
            if p.be_r > 0 and (c[i] - entry_px) >= p.be_r * e_atr:
                be_done = True
            if be_done and entry_px > st:
                st = entry_px
            if c[i] <= st:
                R = (c[i] - entry_px) / e_atr if e_atr > 0 else 0.0
                trades.append((entry_i, entry_px, e_atr, entry_dir, i, float(c[i]), R))
                state = 0
                cooldown = p.cooldown_bars
                be_done = False
        elif state == 2:
            if c[i] < trough:
                trough = c[i]
            st = entry_px + p.sl_atr * e_atr
            if p.trail_atr > 0:
                t = trough + p.trail_atr * e_atr
                if t < st:
                    st = t
            if p.be_r > 0 and (entry_px - c[i]) >= p.be_r * e_atr:
                be_done = True
            if be_done and entry_px < st:
                st = entry_px
            if c[i] >= st:
                R = (entry_px - c[i]) / e_atr if e_atr > 0 else 0.0
                trades.append((entry_i, entry_px, e_atr, entry_dir, i, float(c[i]), R))
                state = 0
                cooldown = p.cooldown_bars
                be_done = False

        # ---------------- 进场 ----------------
        if state == 0 and cooldown == 0 and i >= 30:
            up = c[i] > ma20[i]
            dn = c[i] < ma20[i]
            cross_up = c[i] > ma20[i] and c[i - 1] < ma20[i - 1]
            cross_down = c[i] < ma20[i] and c[i - 1] > ma20[i - 1]
            tl = l[i] <= ma20[i]
            sbull = c[i] > o[i] and c[i] >= 0.5 * (h[i] + l[i])
            ts = h[i] >= ma20[i]
            sbear = c[i] < o[i] and c[i] <= 0.5 * (h[i] + l[i])
            use_pb = p.entry_mode in ("pullback", "both", "both_nm")
            use_bk = p.entry_mode in ("breakout", "breakout_nomacd", "both", "both_nm")
            no_macd = p.entry_mode in ("breakout_nomacd", "both_nm")
            s_tl = use_pb and ok_l[i] and up and tl and sbull and cross_up
            s_ts = use_pb and ok_s[i] and dn and ts and sbear and cross_down
            _hh10 = max(h[max(0, i - 10):i])
            _ll10 = min(l[max(0, i - 10):i])
            s_bkl = use_bk and ok_l[i] and c[i] > _hh10 and c[i] > o[i] and no_macd
            s_bks = use_bk and ok_s[i] and c[i] < _ll10 and c[i] < o[i] and no_macd
            if s_tl or s_bkl:
                state = 1; entry_px = c[i]; entry_i = i; e_atr = atr_arr[i]
                peak = c[i]; trough = c[i]; be_done = False; entry_dir = 1
            elif s_ts or s_bks:
                state = 2; entry_px = c[i]; entry_i = i; e_atr = atr_arr[i]
                peak = c[i]; trough = c[i]; be_done = False; entry_dir = 2

    # 期末仍持仓 → 标记未平仓（不计入 R 统计，单独列出）
    if state != 0:
        trades.append((entry_i, entry_px, e_atr, entry_dir, -1, float(c[-1]), None))
    return trades


def max_drawdown(Rs):
    """按成交顺序的 R 序列，返回最大回撤（R，正数表示回撤幅度）。"""
    eq = 0.0; peak = 0.0; mdd = 0.0
    for r in Rs:
        eq += r
        if eq > peak:
            peak = eq
        dd = peak - eq
        if dd > mdd:
            mdd = dd
    return mdd


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default="akshare", choices=["akshare", "tqsdk"])
    ap.add_argument("--limit", type=int, default=2000,
                    help="读取小时线根数上限（默认2000=近似全历史；引擎实时上限为400）")
    args = ap.parse_args()

    st = get_settings()
    p = st.fusion
    specs = st.main_contracts
    src = args.src

    rows = []
    all_R = []
    tot_long = 0; tot_short = 0
    wins = 0; losses = 0; pf_win = 0.0; pf_loss = 0.0
    open_trades = 0
    skipped = 0

    for spec in specs:
        sym = spec.symbol
        with session_scope() as s:
            df = read_hourly_bars_src(s, sym, args.limit, src)
        if df is None:
            skipped += 1
            rows.append((sym, 0, 0, 0.0, 0.0, 0.0, "无数据"))
            continue
        if p.closed_bars_only:
            df = drop_forming_bars(df)
        if len(df) < p.min_bars:
            skipped += 1
            rows.append((sym, len(df), 0, 0.0, 0.0, 0.0, f"根数<{p.min_bars}"))
            continue
        o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
        l = df["low"].to_numpy(float); c = df["close"].to_numpy(float)
        ema140 = ema(c, p.ema_k)
        dir_raw = np.where(c > ema140, 1, -1)
        htf_dir = np.concatenate([[0], dir_raw[:-1]])
        tr = bt_symbol(o, h, l, c, htf_dir, p)
        closed = [t for t in tr if t[4] >= 0]
        opens = [t for t in tr if t[4] < 0]
        n = len(closed)
        if n == 0:
            rows.append((sym, len(df), 0, 0.0, 0.0, 0.0, "0笔成交"))
            continue
        Rs = [t[6] for t in closed]
        w = sum(1 for r in Rs if r > 0)
        meanR = float(np.mean(Rs))
        wr = w / n
        mdd = max_drawdown(Rs)
        tot_long += sum(1 for t in closed if t[3] == 1)
        tot_short += sum(1 for t in closed if t[3] == 2)
        for r in Rs:
            all_R.append(r)
            if r > 0:
                wins += 1; pf_win += r
            else:
                losses += 1; pf_loss += -r
        open_trades += len(opens)
        rows.append((sym, len(df), n, meanR, wr, mdd, "ok"))

    # 汇总
    N = len(all_R)
    meanR_all = float(np.mean(all_R)) if N else 0.0
    wr_all = wins / N if N else 0.0
    pf = (pf_win / pf_loss) if pf_loss > 0 else float("inf")
    # 等权每品种均R（含全部品种，不剔除负值）
    per_sym_R = [r[3] for r in rows if r[6] == "ok"]
    eq_meanR = float(np.mean(per_sym_R)) if per_sym_R else 0.0
    per_sym_mdd = [r[5] for r in rows if r[6] == "ok"]
    avg_mdd = float(np.mean(per_sym_mdd)) if per_sym_mdd else 0.0
    worst_mdd = float(max(per_sym_mdd)) if per_sym_mdd else 0.0

    print(f"# 融合策略完整回测  src={src}  limit={args.limit}  品种={len(specs)} 跳过={skipped}")
    print(f"# 参数: ema_k={p.ema_k} ma_n={p.ma_n} atr_n={p.atr_n} sl={p.sl_atr} "
          f"trail={p.trail_atr} be_r={p.be_r} W={p.W} entry={p.entry_mode} cd={p.cooldown_bars}")
    print("-" * 82)
    print(f"{'SYMBOL':9} {'BARS':>5} {'成交':>4} {'均R':>7} {'胜率':>6} {'最大回撤R':>9}  备注")
    for r in rows:
        if r[6] == "ok":
            sym, bars, n, meanR, wr, mdd, note = r
            print(f"{sym:9} {bars:>5} {n:>4} {meanR:>+7.3f} {wr*100:>5.1f}% {mdd:>9.2f}")
        else:
            sym, bars, n, meanR, wr, mdd, note = r
            print(f"{sym:9} {bars:>5} {n:>4} {'-':>7} {'-':>6} {'-':>9}  {note}")
    print("-" * 82)
    print(f"总成交(已平仓) = {N}   多={tot_long} 空={tot_short}   期末未平={open_trades}")
    print(f"总胜率 = {wr_all*100:.1f}%   总均R = {meanR_all:+.3f}")
    print(f"累计R = {sum(all_R):+.1f}   盈利因子(PF) = {pf:.2f}")
    print(f"等权每品种均R = {eq_meanR:+.3f}  (样本品种数={len(per_sym_R)})")
    print(f"每品种平均最大回撤 = {avg_mdd:.2f}R   单品种最差回撤 = {worst_mdd:.2f}R")

    # 落盘
    out = {
        "src": src,
        "params": {"ema_k": p.ema_k, "ma_n": p.ma_n, "atr_n": p.atr_n,
                    "sl_atr": p.sl_atr, "trail_atr": p.trail_atr,
                    "be_r": p.be_r, "W": p.W, "entry_mode": p.entry_mode,
                    "cooldown_bars": p.cooldown_bars, "min_bars": p.min_bars},
        "summary": {"symbols": len(specs), "skipped": skipped,
                     "trades": N, "long": tot_long, "short": tot_short,
                     "open": open_trades, "win_rate": wr_all, "mean_R": meanR_all,
                     "total_R": float(sum(all_R)), "profit_factor": (pf if pf != float("inf") else None),
                     "eq_mean_R": eq_meanR, "avg_max_dd_R": avg_mdd, "worst_max_dd_R": worst_mdd},
        "per_symbol": [
            {"symbol": sym, "bars": b, "trades": n, "mean_R": mr, "win_rate": wr, "max_dd_R": mdd, "note": nt}
            for (sym, b, n, mr, wr, mdd, nt) in rows
        ],
    }
    os.makedirs("/app/runtime/bt_out", exist_ok=True)
    fn = f"/app/runtime/bt_out/fusion_bt_{src}.json"
    with open(fn, "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\n已落盘: {fn}")


def read_hourly_bars_src(session, symbol, limit, src):
    """强制指定 src 读取（复刻 read_hourly_bars，但其 src 取自 settings）。"""
    from app.models import HourlyBar
    from sqlalchemy import select
    rows = (
        session.execute(
            select(HourlyBar.trade_datetime, HourlyBar.open, HourlyBar.high,
                   HourlyBar.low, HourlyBar.close)
            .where(HourlyBar.symbol == symbol)
            .where(HourlyBar.src == src)
            .order_by(HourlyBar.trade_datetime.desc())
            .limit(limit)
        ).all()
    )
    rows = list(reversed(rows))
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["dt", "open", "high", "low", "close"])
    for col in ("open", "high", "low", "close"):
        df[col] = df[col].astype(float)
    return df


if __name__ == "__main__":
    main()
