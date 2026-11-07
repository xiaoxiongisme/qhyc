# -*- coding: utf-8 -*-
"""计算并落库因子时序到 factor_value（因子接入 PRD §T1~T7 的第一步）。

- A 组（日内价量）：复用 factor_ic_scan.build_factors，从 bar_15m 算 11 个因子，
  按交易日做横截面 z 标准化；覆盖 2015-2025 全品种（数据最完整）。
- B 组（横截面基本面）：从 spot_basis / member_position_rank_summary /
  inventory / warehouse_receipt / roll_yield 计算 11 个已注册因子，
  按交易日横截面 z 标准化。部分源数据区间短（spot_basis 仅 8 天等），
  会自动跳过无数据日期。

输出：factor_value(factor_id, trade_date, symbol, raw_value, z_value, available_at, version)
symbol 统一归一到 888 主力连续代码（与 factor_ic_scan._to_main 一致）。

用法：
  python scripts/compute_factor_value.py --since 2024        # 本地落库（默认 upsert）
  python scripts/compute_factor_value.py --since 2015 --dry-run   # 只统计不写库
  python scripts/compute_factor_value.py --groups B          # 仅 B 组
"""
from __future__ import annotations
import os, sys, argparse
import datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import numpy as np
import pandas as pd
import psycopg2
from psycopg2.extras import execute_values

from factor_ic_scan import pick_symbols, load_symbol_bars, build_factors, _to_main
# ★ 2026-11-06 接线前提修正：原先用 `load_creds()`（内部硬编码 127.0.0.1），
#   在**容器内**必然连不上库（scheduler 容器无本地 DB）→ 该脚本**根本无法在
#   生产调度环境运行**（这也是它从未被调度、22 个因子停在 09-30 的根因之一）。
#   改走 pgconn（环境变量优先，与 compute_factor_v1v6.py 一致）。
from pgconn import add_conn_args, conn_from_args

VERSION = "1.0"
A_FACTORS = ["f_vwap_dev", "f_close_loc", "f_close_loc14", "f_mom_1h", "f_mom_4h",
             "f_vol_surge", "f_oi_dir", "f_atr_pct", "f_range_comp", "f_gap", "f_amt_conc"]


# ---------------------------------------------------------------- A 组
def compute_a(cur, since, until):
    _, _, pick = pick_symbols(cur, since)
    frames = []
    for s in pick:
        df = load_symbol_bars(cur, s, since, until)
        if df is None or len(df) < 500:
            continue
        fr = build_factors(df)
        if fr.empty:
            continue
        fr["symbol"] = s
        frames.append(fr[["symbol", "day"] + A_FACTORS])
    if not frames:
        return []
    A = pd.concat(frames, ignore_index=True)
    A["trade_date"] = pd.to_datetime(A["day"]).dt.date
    rows = []
    for fac in A_FACTORS:
        sub = A[["symbol", "trade_date", fac]].dropna(subset=[fac]).copy()
        if sub.empty:
            continue
        z = sub.groupby("trade_date")[fac].transform(lambda x: (x - x.mean()) / x.std(ddof=0))
        for (sym, date), raw, zz in zip(zip(sub["symbol"], sub["trade_date"]), sub[fac], z):
            rows.append((fac, date, sym, float(raw), None if pd.isna(zz) else float(zz)))
    return rows


# ---------------------------------------------------------------- B 组
def _to_float_df(df):
    """psycopg2 返回的 numeric 是 Decimal，统一转 float（symbol/日期列除外）。"""
    for c in df.columns:
        if c in ("symbol", "report_date", "trade_date"):
            continue
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _zscore(frame, val_col):
    """frame: (symbol, trade_date, val_col) -> 追加 z 列（按交易日横截面）"""
    out = frame.dropna(subset=[val_col]).copy()
    if out.empty:
        out["z"] = np.nan
        return out

    def _z(x):
        s = x.std(ddof=0)
        if s is None or pd.isna(s) or s == 0:
            return pd.Series(np.nan, index=x.index)
        return (x - x.mean()) / s

    out["z"] = out.groupby("trade_date")[val_col].transform(_z)
    return out


def compute_b(cur):
    rows = []

    def add(fid, raw_frame, val_col):
        rf = raw_frame.rename(columns={val_col: "raw"}).copy()
        rf["raw"] = pd.to_numeric(rf["raw"], errors="coerce")
        rf["symbol"] = rf["symbol"].map(_to_main)
        rf = rf.dropna(subset=["symbol", "raw"])
        rf["trade_date"] = pd.to_datetime(rf["trade_date"]).dt.date
        zz = _zscore(rf, "raw")
        for (sym, date), raw, z in zip(zip(zz["symbol"], zz["trade_date"]), zz["raw"], zz["z"]):
            if pd.isna(z):
                continue
            rows.append((fid, date, sym, float(raw), float(z)))

    # 1) basis_rate_z：基差率（dom 优先，缺则用 near）
    cur.execute("SELECT report_date, symbol, dom_basis_rate, near_basis_rate FROM spot_basis")
    sb = _to_float_df(pd.DataFrame(cur.fetchall(),
                      columns=["report_date", "symbol", "dom", "near"]))
    sb["raw"] = sb["dom"].fillna(sb["near"])
    add("basis_rate_z", sb.rename(columns={"report_date": "trade_date"})[["trade_date", "symbol", "raw"]],
        "raw")

    # 8) spot_mom_z：现货价格 20 日动量
    cur.execute("SELECT report_date, symbol, spot_price FROM spot_basis WHERE spot_price IS NOT NULL")
    sp = _to_float_df(pd.DataFrame(cur.fetchall(),
                      columns=["report_date", "symbol", "spot"]))
    sp["trade_date"] = pd.to_datetime(sp["report_date"])
    sp = sp.sort_values(["symbol", "trade_date"])
    sp["mom"] = sp.groupby("symbol")["spot"].transform(
        lambda x: x / x.shift(20).replace(0, np.nan) - 1.0)
    add("spot_mom_z", sp[["report_date", "symbol", "mom"]].rename(columns={"report_date": "trade_date"}),
        "mom")

    # 2/3/4/10) 会员相关
    cur.execute("""SELECT report_date, symbol, vol_top5, vol_top10, vol_top15, vol_top20,
                          long_open_interest_top5, long_open_interest_top10,
                          short_open_interest_top5, short_open_interest_top10
                   FROM member_position_rank_summary""")
    mp = _to_float_df(pd.DataFrame(cur.fetchall(), columns=[
        "report_date", "symbol", "v5", "v10", "v15", "v20", "lo5", "lo10", "so5", "so10"]))
    mp["net"] = mp["lo5"] - mp["so5"]
    mp["ls"] = mp["lo5"] / mp["so5"].replace(0, np.nan)
    mp["volc"] = mp["v5"] / (mp["v5"] + mp["v10"] + mp["v15"] + mp["v20"]).replace(0, np.nan)
    mp["net_prev"] = mp.sort_values("report_date").groupby("symbol")["net"].shift(1)
    mp["trend"] = mp["net"] - mp["net_prev"]
    add("member_net_z", mp[["report_date", "symbol", "net"]].rename(columns={"report_date": "trade_date"}), "net")
    add("member_ls_z", mp[["report_date", "symbol", "ls"]].rename(columns={"report_date": "trade_date"}), "ls")
    add("vol_concentration_z", mp[["report_date", "symbol", "volc"]].rename(columns={"report_date": "trade_date"}), "volc")
    add("member_trend_z", mp[["report_date", "symbol", "trend"]].rename(columns={"report_date": "trade_date"}), "trend")

    # 6) inventory_z
    cur.execute("SELECT report_date, symbol, inventory_qty FROM inventory WHERE inventory_qty IS NOT NULL")
    iv = _to_float_df(pd.DataFrame(cur.fetchall(), columns=["report_date", "symbol", "qty"]))
    ivg = iv.groupby(["report_date", "symbol"], as_index=False)["qty"].sum()
    add("inventory_z", ivg.rename(columns={"report_date": "trade_date"}), "qty")

    # 7) warehouse_receipt_z
    cur.execute("SELECT report_date, symbol, receipt_qty FROM warehouse_receipt WHERE receipt_qty IS NOT NULL")
    wr = _to_float_df(pd.DataFrame(cur.fetchall(), columns=["report_date", "symbol", "qty"]))
    wrg = wr.groupby(["report_date", "symbol"], as_index=False)["qty"].sum()
    add("warehouse_receipt_z", wrg.rename(columns={"report_date": "trade_date"}), "qty")

    # 5/9) roll_yield（可能为空）
    cur.execute("SELECT report_date, symbol, roll_yield, near_price, far_price FROM roll_yield")
    ry = _to_float_df(pd.DataFrame(cur.fetchall(), columns=["report_date", "symbol", "ry", "np", "fp"]))
    if not ry.empty:
        add("roll_yield_z", ry.rename(columns={"report_date": "trade_date"}), "ry")
        ry["slope"] = (ry["fp"] - ry["np"]) / ry["np"].replace(0, np.nan)
        add("structure_slope_z", ry.rename(columns={"report_date": "trade_date"}), "slope")

    # 11) cross_rank_z：其余 B 组 z 的等权均值
    tmp = pd.DataFrame(rows, columns=["factor_id", "trade_date", "symbol", "raw_value", "z_value"])
    b_z = tmp[tmp["factor_id"] != "cross_rank_z"]
    if not b_z.empty:
        pivot = b_z.pivot_table(index=["symbol", "trade_date"], columns="factor_id", values="z_value")
        comp = pivot.mean(axis=1, skipna=True).reset_index(name="z")
        for (sym, date), z in zip(zip(comp["symbol"], comp["trade_date"]), comp["z"]):
            if pd.isna(z):
                continue
            rows.append(("cross_rank_z", date, sym, None, float(z)))
    return rows


# ---------------------------------------------------------------- C 组
def compute_c(cur, since):
    """基差时序因子（C 组）：由 ``app.features.position_factors.basis_factors`` 派生。

    ★ 2026-11-06 接线：``basis_factors`` 此前**无任何调用方**（死代码）。本函数
      用它把 ``spot_basis`` 的基差率时序转成两个**逐日逐品种**的因子：

        · ``f_basis_mean60_z`` —— ``dom_basis_rate`` 的 60 日均值（横截面 z）
        · ``f_basis_chg5_z``   —— ``dom_basis_rate`` 的 5 日变化（横截面 z）

    防前视（§18.7① 红线）：每个交易日 d 只传入 **report_date <= d** 的窗口
      （``basis_factors`` 内部对传入时序取尾部聚合 ⇒ 天然无前视）。

    ⚠ 因子身份已由迁移 049 注册（``factor_registry``），且 enabled=false
      （注册即中性）—— 真正启用须过回测闸门 + 准入流程。
    """
    from app.features.position_factors import basis_factors

    cur.execute(
        "SELECT report_date, symbol, dom_basis_rate, dom_basis, near_basis "
        "FROM spot_basis WHERE report_date >= %s AND dom_basis_rate IS NOT NULL",
        (since,))
    df = pd.DataFrame(cur.fetchall(), columns=[
        "report_date", "symbol", "dom_basis_rate", "dom_basis", "near_basis"])
    if df.empty:
        return []
    df["symbol"] = df["symbol"].map(_to_main)
    df["dom_basis_rate"] = pd.to_numeric(df["dom_basis_rate"], errors="coerce")
    df = df.dropna(subset=["symbol", "dom_basis_rate"])
    if df.empty:
        return []

    raw_rows = []
    for sym, g in df.groupby("symbol"):
        g = g.sort_values("report_date").reset_index(drop=True)
        # i>=5：窗口至少 6 行，basis_factors 才有 basis_change_5d
        for i in range(5, len(g)):
            win = g.iloc[max(0, i - 59):i + 1]
            out = basis_factors(win)
            if not out:
                continue
            d = g["report_date"].iloc[i]
            m60 = out.get("dom_basis_rate_mean_60")
            c5 = out.get("basis_change_5d")
            if m60 is not None:
                raw_rows.append(("f_basis_mean60_z", d, sym, float(m60)))
            if c5 is not None:
                raw_rows.append(("f_basis_chg5_z", d, sym, float(c5)))
    if not raw_rows:
        return []

    tmp = pd.DataFrame(raw_rows, columns=["factor_id", "trade_date", "symbol", "raw"])
    rows: list = []
    for fid, sub in tmp.groupby("factor_id"):
        zz = _zscore(sub, "raw")
        for (sym, date), raw, z in zip(zip(zz["symbol"], zz["trade_date"]),
                                      zz["raw"], zz["z"]):
            if pd.isna(z):
                continue
            rows.append((fid, date, sym, float(raw), float(z)))
    return rows


# ---------------------------------------------------------------- 落库
def upsert(cur, rows):
    # 实际表列：factor_id, trade_date, symbol, raw_value, z_value, available_at,
    # version, created_at(NOT NULL DEFAULT now())。INSERT 仅显式给前 7 列，
    # created_at 走 DEFAULT；available_at 取 trade_date（因子于该交易日即可用）。
    sql = """
    INSERT INTO factor_value
        (factor_id, trade_date, symbol, raw_value, z_value, available_at, version)
    VALUES %s
    ON CONFLICT (factor_id, trade_date, symbol) DO UPDATE SET
        raw_value = EXCLUDED.raw_value, z_value = EXCLUDED.z_value,
        available_at = EXCLUDED.available_at, version = EXCLUDED.version
    """
    data = [(r[0], r[1], r[2], r[3], r[4], r[1], VERSION) for r in rows]
    execute_values(cur, sql, data, page_size=5000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2015")
    ap.add_argument("--until", default="2026")
    ap.add_argument("--groups", default="A,B", help="逗号分隔，默认 A,B（A=日内价量 B=基本面 C=基差时序）")
    ap.add_argument("--dry-run", action="store_true")
    add_conn_args(ap)
    a = ap.parse_args()
    # ★ 2026-11-06 部署兜底：--until 默认值 "2026" 会被 factor_ic_scan._ymd 展开成
    #   2026-01-01（"年"语义）→ A 组 SQL 变成 bucket >= <since> AND bucket < 2026-01-01
    #   → **恒空**（A 组 11 个因子自 2026 年起从未产出过）。
    #   调度器已显式传 --until；但**进程未重启时旧调度器不传**，此处再兜一层：
    #   仅当「since 是完整日期（增量模式）」且 until 仍是默认的 "2026" 时，
    #   把 until 兜底为「今天+1」。手工 `--since 2024`（4 位年）**不触发**，
    #   保持原有全历史语义（避免 A 组一次拉 2 年数据打爆小实例）。
    if a.until == "2026" and a.since and len(a.since) == 10:
        a.until = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
        print(f"[until] 未显式传入且为增量模式 → 兜底 until={a.until}")
    groups = {g.strip() for g in a.groups.split(",") if g.strip()}

    conn = psycopg2.connect(**conn_from_args(a))
    try:
        cur = conn.cursor()
        all_rows = []
        if "A" in groups:
            print("[A 组] 从 bar_15m 计算日内价量因子 ...")
            ar = compute_a(cur, a.since, a.until)
            print(f"      A 组 {len(ar):,} 行")
            all_rows += ar
        if "B" in groups:
            print("[B 组] 从基本面表计算横截面因子 ...")
            br = compute_b(cur)
            # ★ 2026-11-06 部署修正：B 组各 SQL 是**全历史**扫描（compute_b 不接
            #   since），实测 --since 2026-10-20 仍产出 751,361 行（≈全历史 5.5 年）
            #   → 每日作业会把 75 万行 UPSERT 打进生产 factor_value（现 261 万行）。
            #   z 值按「当日横截面」算，与历史窗口无关 ⇒ **只写增量窗口是等价且幂等的**。
            #   这里做最小改动（不在 compute_b 的 6 处 SQL 里逐个加 WHERE，避免口径风险）。
            _sd = datetime.date.fromisoformat(a.since) if a.since else None
            if _sd is not None:
                _n0 = len(br)
                br = [r for r in br if r[1] >= _sd]
                print(f"      [增量过滤] {_n0:,} -> {len(br):,} 行（trade_date >= {_sd}）")
            print(f"      B 组 {len(br):,} 行")
            all_rows += br
        if "C" in groups:
            print("[C 组] 从 spot_basis 计算基差时序因子（basis_factors）...")
            cr = compute_c(cur, a.since)
            print(f"      C 组 {len(cr):,} 行")
            all_rows += cr
        conn.commit()

        # 统计
        df = pd.DataFrame(all_rows, columns=["factor_id", "trade_date", "symbol", "raw", "z"])
        print(f"\n合计 {len(df):,} 行；各因子行数：")
        for fid, n in df.groupby("factor_id").size().sort_index().items():
            print(f"  {fid:22s} {n:>10,}")

        if a.dry_run:
            print("\n[dry-run] 未写入 factor_value")
            return
        cur = conn.cursor()
        upsert(cur, all_rows)
        conn.commit()
        print(f"\n已 upsert {len(all_rows):,} 行到 factor_value (version={VERSION})")
    finally:
        conn.close()


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
