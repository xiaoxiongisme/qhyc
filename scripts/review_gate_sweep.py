"""只读：反转 gate 阈值 sweep v2（离线扫描，等价于多次重跑回测）。

v1 失败原因：生产只在"未发信号"行写 gate_reason（形如 "|ret_t|=1.44%<=2.0%"），
            signaled=True 行的 gate_reason 为 NULL —— 故无法仅凭 gate_reason 得到 >2% 的 |ret_t|。

v2 方案：用生产自有函数 `load_canonical_series` 取每品种 canonical 收盘价，
        逐日算 ret_t = close.pct_change()*100（与 technical.ret_series 同式），
        在 backtest_detail 已存的 (symbol, eval_date) 锚点上重算 |ret_t|：
          - 用"未发信号行"的 gate_reason 解析值**反向校验**计算精度（应近 100% 一致）；
          - 用"发信号行"验证 |ret_t| > 2.0%。
        然后扫任意 gate，输出覆盖率 / 池化 dir_acc / Wilson CI / 二项 p / 毛&净 P&L。
"""
from __future__ import annotations

import re
from math import erfc, sqrt

import numpy as np
import pandas as pd
from sqlalchemy import text

from app.core.db import session_scope
from app.features.pipeline import load_canonical_series

RUN = "20260910_143938_bt"
GATES = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0]
COST = 2 * 0.065  # %，§18.3：双向开平，每笔扣 2×cost_pct（=0.13，与生产 engine.py 一致）
COVER_MIN, ACC_MIN = 15.0, 0.53


def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def p_two_sided(k, n, p0=0.5):
    if n == 0:
        return float("nan")
    z = (k / n - p0) / sqrt(p0 * (1 - p0) / n)
    return erfc(abs(z) / sqrt(2))


def parse_absret(gr):
    m = re.search(r"\|ret_t\|=([\d.]+)%", gr or "")
    return float(m.group(1)) if m else float("nan")


with session_scope() as s:
    det = pd.read_sql(
        text(
            "SELECT symbol, eval_date, state, pred_dir, actual, signaled, gate_reason, caliber "
            "FROM backtest_detail WHERE run_id=:r AND model='reversal' "
            "ORDER BY symbol, eval_date"
        ),
        s.get_bind(),
        params={"r": RUN},
    )
    det["eval_date"] = pd.to_datetime(det["eval_date"])
    det["actual"] = pd.to_numeric(det["actual"], errors="coerce")

    # ---- 逐品种取 canonical 日收益 ----
    retmap = {}
    srcmap = {}
    for sy in sorted(det["symbol"].unique()):
        try:
            df, source = load_canonical_series(s, sy)
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] {sy} load_canonical_series 失败: {str(e)[:60]}")
            continue
        if df is None or "close" not in df.columns or len(df) < 2:
            continue
        retmap[sy] = (df["close"].pct_change() * 100)
        srcmap[sy] = source

    def get_absret(r):
        ser = retmap.get(r.symbol)
        if ser is None:
            return float("nan")
        v = ser.get(r.eval_date.date(), np.nan)
        try:
            v = float(v)
        except (TypeError, ValueError):
            return float("nan")
        return abs(v) if np.isfinite(v) else float("nan")

    det["absret"] = det.apply(get_absret, axis=1)
    total_pts = len(det)
    n_sym = det["symbol"].nunique()
    src_dist = pd.Series(list(srcmap.values())).value_counts().to_dict()

    print("=" * 112)
    print(f"反转 gate 阈值 sweep v2   run={RUN}")
    print(f"评估点={total_pts}  品种={n_sym}  |ret_t| 重算成功={det.absret.notna().sum()}  "
          f"失败={det.absret.isna().sum()}  source分布={src_dist}")
    print(f"caliber 取值={det['caliber'].dropna().unique().tolist()}")
    print("=" * 112)

    # ---- 自洽校验 A：未发信号行，我方 |ret| vs 生产 gate_reason ----
    ns = det[~det["signaled"].fillna(False)].copy()
    ns["gr_abs"] = ns["gate_reason"].map(parse_absret)
    ok = ns["gr_abs"].notna() & ns["absret"].notna()
    diff = (ns.loc[ok, "absret"] - ns.loc[ok, "gr_abs"]).abs()
    print(f"[校验A] 未发信号行 {len(ns)}，其中可比 {int(ok.sum())}；"
          f"|我方|ret|-生产|ret_t||: max={diff.max():.4f}  "
          f"P99={diff.quantile(0.99):.4f}  一致(≤0.01)={int((diff <= 0.01).sum())}/{int(ok.sum())}")

    # ---- 自洽校验 B：发信号行，|ret| 应 > 2.0% ----
    yg = det[det["signaled"].fillna(False)]["absret"].dropna()
    print(f"[校验B] 发信号行 {int(det['signaled'].fillna(False).sum())}，"
          f"|ret| 最小值={yg.min():.4f}%  （应 >2.0%）  违约(|ret|≤2.0)={int((yg <= 2.0).sum())}")

    # ---- 库内真值（gate=2.0%）----
    sig_db = det[det["signaled"].fillna(False)]
    k_db = int(((sig_db["pred_dir"] == "up") == (sig_db["actual"] > 0)).sum())
    print(f"[库内] signaled={len(sig_db)}  池化 acc={k_db/len(sig_db):.4f}  (应≈0.5492)")

    # ---- 命中 / P&L ----
    det["psign"] = np.where(det["pred_dir"] == "up", 1.0, -1.0)
    det["hit"] = ((det["pred_dir"] == "up") == (det["actual"] > 0)).astype(int)
    det["pnl"] = det["psign"] * det["actual"]

    hdr = (f"{'gate%':>6} {'signaled':>9} {'覆盖%':>8} {'池化acc':>9} "
           f"{'Wilson95':>19} {'p(双尾)':>10} {'显著':>5} {'毛PnL%':>8} {'净PnL%':>8} {'达标':>5}")
    print("\n=== 阈值 sweep（池化口径，分母=总评估点 %d）===" % total_pts)
    print(hdr)
    print("-" * len(hdr))

    rows = []
    for g in GATES:
        sub = det[det.absret > g]
        n = len(sub)
        if n == 0:
            print(f"{g:>6.1f} {0:>9} {'-':>8} {'-':>9} {'-':>19} {'-':>10} {'-':>5} "
                  f"{'-':>8} {'-':>8} {'-':>5}")
            continue
        k = int(sub["hit"].sum())
        acc = k / n
        lo, hi = wilson(k, n)
        p = p_two_sided(k, n)
        gross = float(sub["pnl"].mean())
        net = gross - COST
        cover = n / total_pts * 100
        star = "★" if (p < 0.05 and acc > 0.5) else ""
        okf = "✅" if (cover >= COVER_MIN and acc >= ACC_MIN and net > 0) else ""
        rows.append(dict(gate=g, n=n, cover=cover, acc=acc, lo=lo, hi=hi, p=p,
                         gross=gross, net=net))
        print(f"{g:>6.1f} {n:>9} {cover:>7.2f}% {acc:>9.4f} "
              f"[{lo:>7.4f},{hi:>7.4f}] {p:>10.3e} {star:>5} "
              f"{gross:>+8.4f} {net:>+8.4f} {okf:>5}")

    # ---- 按 state 分层 ----
    print("\n=== 按 state × gate 分层（池化 acc / n）===")
    states = [x for x in sorted(det["state"].dropna().unique())]
    h2 = f"{'gate%':>6} " + " ".join(f"{st:>17}" for st in states)
    print(h2)
    print("-" * len(h2))
    for g in GATES:
        sub = det[det.absret > g]
        cells = []
        for st in states:
            ss = sub[sub["state"] == st]
            cells.append(f"{'-':>17}" if len(ss) == 0
                         else f"{ss['hit'].mean():.4f}(n={len(ss):>4})".rjust(17))
        print(f"{g:>6.1f} " + " ".join(cells))

    # ---- 逐品种覆盖率 ----
    print("\n=== 逐品种覆盖率分布 ===")
    per = det.groupby("symbol")["absret"].apply(lambda x: x.notna().sum())
    for g in (1.0, 1.5, 2.0):
        cnt = det[det.absret > g].groupby("symbol").size().reindex(per.index, fill_value=0)
        cov = (cnt / per.replace(0, np.nan)) * 100
        print(f"  gate={g:.1f}%: 中位={cov.median():.1f}%  均值={cov.mean():.1f}%  "
              f"min={cov.min():.1f}%  max={cov.max():.1f}%  零信号品种={int((cnt == 0).sum())}")

    # ---- 结论 ----
    print("\n=== 结论 ===")
    best = [r for r in rows if r["cover"] >= COVER_MIN and r["acc"] >= ACC_MIN and r["net"] > 0]
    if best:
        b = max(best, key=lambda r: r["acc"])
        print("满足「覆盖率≥%.0f%% & acc≥%.2f & 净PnL>0」的阈值: %s"
              % (COVER_MIN, ACC_MIN, ", ".join(f"{r['gate']}%" for r in best)))
        print(f"其中 acc 最高: gate={b['gate']}%  cover={b['cover']:.2f}%  acc={b['acc']:.4f}  "
              f"p={b['p']:.2e}  净PnL={b['net']:+.4f}%")
    else:
        print(f"无阈值同时满足「覆盖率≥{COVER_MIN}% & acc≥{ACC_MIN} & 净PnL>0」。")
    sig_rows = [r for r in rows if r["p"] < 0.05 and r["acc"] > 0.5]
    print("显著(p<0.05 且 acc>0.5)的阈值: "
          + (", ".join(f"{r['gate']}%(acc={r['acc']:.4f},cover={r['cover']:.2f}%)" for r in sig_rows)
             or "无"))
    pos_rows = [r for r in rows if r["net"] > 0]
    print("净PnL>0 的阈值: "
          + (", ".join(f"{r['gate']}%(net={r['net']:+.4f}%)" for r in pos_rows) or "无"))
