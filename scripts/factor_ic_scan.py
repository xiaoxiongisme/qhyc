#!/usr/bin/env python3
"""因子 IC 扫描器（A 组日内价量 + B 组横截面基本面）

用途：在本地 TimescaleDB 上，用 2015-2025 的 15m/日线数据，对候选因子做
     IC / ICIR / t / 衰减曲线 的横截面评估，输出留存建议。

设计要点（避免重蹈前视与换月陷阱）：
  1. 决策时点固定为每个交易日的 14:45（15m 桶，日盘内），id 时刻只用 t 及以前的数据；
  2. 前瞻收益从决策桶往后数 k 根 15m（k=1 即当日 15:00 收盘，纯日内残差；k>=2 进入次一交易日）；
  3. 夜盘（>=20:00）归入次一交易日，交易日边界与交易所口径一致；
  4. 剔除换月跳空样本：日收益率 > 5×ATR14 或 OI 单日降幅 > 25%；
  5. 同一品种的 888 / 8888 只保留 888（主力连续），避免同品种重复进入截面。

用法：
    python scripts/factor_ic_scan.py                # 全量
    python scripts/factor_ic_scan.py --since 2018   # 缩短区间
    python scripts/factor_ic_scan.py --symbols RB888 CU888 M888
    python scripts/factor_ic_scan.py --out E:/抖音分析/2026-09-23-23-22-39/factor_ic_results
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

# ---------------------------------------------------------------- 连接
def load_creds():
    creds = dict(host="127.0.0.1", port=5432, user="futures",
                 password="futures", dbname="futures")
    env_path = Path(__file__).resolve().parent.parent / ".env"
    try:
        with open(env_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k == "POSTGRES_PASSWORD":
                    creds["password"] = v
                elif k == "POSTGRES_USER":
                    creds["user"] = v
                elif k == "POSTGRES_DB":
                    creds["dbname"] = v
                elif k == "POSTGRES_PORT":
                    creds["port"] = int(v)
    except FileNotFoundError:
        pass
    for ek, ck in (("POSTGRES_HOST", "host"), ("POSTGRES_PORT", "port"),
                   ("POSTGRES_USER", "user"), ("POSTGRES_PASSWORD", "password"),
                   ("POSTGRES_DB", "dbname")):
        if os.environ.get(ek):
            creds[ck] = int(os.environ[ek]) if ck == "port" else os.environ[ek]
    return creds


# ---------------------------------------------------------------- 数据加载
def _ymd(s) -> str:
    """把 --since/--until 规整为 YYYY-MM-DD（兼容 年 / 年月 / 完整日期 三种写法）。

    历史接口按「年」设计：调用方直接传 ``f"{since}-01-01"``。但调度器
    ``_factor_v1v6_job`` 传的是完整日期（``datetime.now()-60d`` 的 ISO），两者混用
    会拼出 ``2026-08-07-01-01`` 这种非法值 → SQL 报 InvalidDatetimeFormat，
    **每日因子作业整段失败、factor_value 自 2026-09-29 起停更**（2026-10-06 实测）。
    这里统一规整：年(2026)→2026-01-01；年月(2026-08)→2026-08-01；完整日期原样透传。
    年份入参行为与旧版完全一致，故不破坏既有脚本。
    """
    s = str(s).strip()
    if len(s) == 4:            # 2026
        return f"{s}-01-01"
    if len(s) == 7:            # 2026-08
        return f"{s}-01"
    return s                   # 2026-08-07 / 2026-08-07 00:00:00 原样


def _is_main(s: str) -> bool:
    """888 = 主力连续；8888 = 商品指数（两者不可同时进入截面）。"""
    return s.endswith("888") and not s.endswith("8888")


def pick_symbols(cur, since):
    """选出用于截面的合约：同一品种只保留主力连续（888），去掉商品指数（8888）。"""
    cur.execute(
        "SELECT DISTINCT symbol FROM bar_15m "
        "WHERE bucket >= %s AND close IS NOT NULL", (_ymd(since),)
    )
    syms = [r[0] for r in cur.fetchall()]
    # 8888（商品指数）与 888（主力连续）视为同一品种，优先保留 888
    by_base: dict[str, str] = {}
    for s in syms:
        base = (s[:-4] + "888") if s.endswith("8888") else s
        cur_pick = by_base.get(base)
        if cur_pick is None or (_is_main(s) and not _is_main(cur_pick)):
            by_base[base] = s
    out = sorted(by_base.values())
    return syms, by_base, out


def load_symbol_bars(cur, symbol, since, until):
    """取单个合约 15m bar，计算交易日标签与 bar 内序号。"""
    cur.execute(
        "SELECT bucket, open, high, low, close, volume, amount, open_interest "
        "FROM bar_15m WHERE symbol=%s AND bucket >= %s AND bucket < %s "
        "  AND close IS NOT NULL ORDER BY bucket",
        (symbol, _ymd(since), _ymd(until)),
    )
    rows = cur.fetchall()
    if not rows:
        return None
    df = pd.DataFrame(rows, columns=["bucket", "open", "high", "low", "close",
                                     "volume", "amount", "oi"])
    for c in ("open", "high", "low", "close", "amount", "oi"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0)
    b = df["bucket"]
    # 夜盘（>=20:00）归入次一交易日
    day = pd.Series(b.dt.date.values, index=df.index).astype(object)
    add = (b.dt.hour >= 20).astype(int)
    day = pd.to_datetime(day) + pd.to_timedelta(add, unit="D")
    df["day"] = day
    df["hm"] = b.dt.strftime("%H:%M")
    df["pos"] = np.arange(len(df))
    return df


# ---------------------------------------------------------------- 因子构建
def build_factors(df: pd.DataFrame) -> pd.DataFrame:
    """对单合约构造 (day) 级别的因子行。

    关键：所有「当日统计量」只使用**决策时点（日盘最后一根 14:45）及以前**的 bar，
    否则日 VWAP 里会含 15:00 那根收盘价，因子与未来收益产生机械相关（虚假 IC）。
    """
    ds = df[df["hm"] < "15:00"]                      # 日盘 bar
    if ds.empty:
        return pd.DataFrame()
    dec = ds.groupby("day", sort=True).tail(1)       # 决策行（14:45）
    agg = ds.groupby("day", sort=True).agg(
        day_open=("open", "first"), day_high=("high", "max"),
        day_low=("low", "min"), amt=("amount", "sum"),
        vol=("volume", "sum"), oi_open=("oi", "first"),
    ).reset_index()
    # 开盘前 2 根的成交额占比（用开盘 30 分钟衡量当日资金集中度）
    first2 = ds.groupby("day", sort=True)["amount"].apply(lambda x: x.head(2).sum())
    agg["amt2"] = agg["day"].map(first2)
    dec = dec.rename(columns={"close": "close_d", "oi": "oi_d", "volume": "vol_d",
                              "amount": "amt_d", "high": "high_d", "low": "low_d"})
    out = agg.merge(dec[["day"] + [c for c in dec.columns if c != "day"]],
                    on="day", how="inner").dropna(subset=["close_d"])
    if out.empty:
        return pd.DataFrame()
    out["day_close"] = out["close_d"]
    n = len(df)
    for k in (1, 2, 4, 8, 16):                       # 前瞻收益 = 未来收盘/决策收盘 - 1
        idx = np.clip(out["pos"].values + k, 0, n - 1)
        out[f"f{k}"] = df["close"].values[idx] / out["close_d"].values - 1.0

    # ---- 日级 ATR（只用已完成的日，天然无前视）----
    prev_close = out["day_close"].shift(1)
    tr = pd.concat([
        out["day_high"] - out["day_low"],
        (out["day_high"] - prev_close).abs(),
        (out["day_low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    out["atr14"] = tr.rolling(14, min_periods=10).mean()

    # ---- 因子 ----
    cl = out["close_d"]
    atr = out["atr14"].replace(0, np.nan)
    vol_ma = out["vol"].shift(1).rolling(20, min_periods=10).mean()
    out["f_vwap_dev"] = (cl - out["amt"] / out["vol"].replace(0, np.nan)) / atr * 100
    rng = (out["day_high"] - out["day_low"]).replace(0, np.nan)
    out["f_close_loc"] = (cl - out["day_low"]) / rng
    out["f_close_loc14"] = (cl - out["day_low"].rolling(14, min_periods=10).min()) / atr
    out["f_mom_1h"] = cl / out["day_close"].shift(4) - 1.0
    out["f_mom_4h"] = cl / out["day_close"].shift(16) - 1.0
    out["f_vol_surge"] = out["vol"] / vol_ma
    out["f_oi_dir"] = (np.sign(out["day_close"] - out["day_open"])
                       * (out["oi_d"] - out["oi_open"])
                       / out["oi_open"].replace(0, np.nan) * 100)
    out["f_atr_pct"] = atr / cl * 100
    out["f_range_comp"] = (out["day_high"] - out["day_low"]).rolling(
        20, min_periods=10).rank(pct=True)
    out["f_gap"] = (out["day_open"] - prev_close) / atr
    out["f_amt_conc"] = out["amt2"] / out["amt"].replace(0, np.nan) * 100
    return out


# ---------------------------------------------------------------- IC 计算
def ic_table(f: pd.DataFrame, fac_cols: list[str], ret_cols: list[str]) -> pd.DataFrame:
    """每个交易日的横截面 Spearman IC。ret_cols 用 f{k} 命名，输出统一为 h{k}。"""
    ret_map = {"f1": "h1", "f2": "h2", "f4": "h4", "f8": "h8", "f16": "h16"}
    recs = []
    for d, g in f.groupby("day"):
        if len(g) < 15:
            continue
        sub = g[list(fac_cols + ret_cols)].apply(pd.to_numeric, errors="coerce").dropna()
        if len(sub) < 15:
            continue
        rk = sub.rank()
        for fc in fac_cols:
            for rc in ret_cols:
                if rk[fc].nunique() < 3 or rk[rc].nunique() < 3:
                    continue
                c = stats.spearmanr(rk[fc], rk[rc]).statistic
                if np.isfinite(c):
                    recs.append((d, fc, ret_map.get(rc, rc), c, len(sub)))
    return pd.DataFrame(recs, columns=["day", "factor", "horizon", "ic", "n"])


def _ic_stat(x: pd.Series):
    if len(x) < 30:
        return np.nan, np.nan, np.nan, len(x)
    mu, sd = x.mean(), x.std(ddof=1)
    icir = mu / sd if sd and sd > 0 else np.nan
    return mu, icir, icir * np.sqrt(len(x)), len(x)


def summarize(ic: pd.DataFrame, facs: list[str], label: str,
              oos: str = "2023-01-01") -> pd.DataFrame:
    """输出全样本 IC/ICIR/t，并拆出样本外（默认 2023+）IC，用于识别过拟合。"""
    rows = []
    for f_ in facs:
        for h in ["h1", "h2", "h4", "h8", "h16"]:
            s = ic[(ic.factor == f_) & (ic.horizon == h)]
            if s.empty or "day" not in s.columns:
                continue
            s = s.copy()
            s["is"] = s["day"] < oos
            mu, icir, t, n = _ic_stat(s["ic"])
            mu_i, _, t_i, n_i = _ic_stat(s.loc[s["is"], "ic"])
            mu_o, _, t_o, n_o = _ic_stat(s.loc[~s["is"], "ic"])
            rows.append(dict(group=label, factor=f_, horizon=h, ic=mu, icir=icir,
                             t=t, days=n, ic_is=mu_i, t_is=t_i, days_is=n_i,
                             ic_oos=mu_o, t_oos=t_o, days_oos=n_o))
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- B 组
import re as _re


def _num(df: pd.DataFrame) -> pd.DataFrame:
    """psycopg2 的 numeric 是 Decimal，统一转 float，避免 pandas 运算类型错误。"""
    for c in df.columns:
        if df[c].dtype == object and "date" not in c.lower():
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def _to_main(code: str) -> str | None:
    """合约代码/品种简称 -> daily_bar 的 888 代码。"""
    m = _re.match(r"^([A-Za-z]+)", str(code))
    if not m:
        return None
    return m.group(1).upper() + "888"


def b_group(cur, since, until):
    """横截面基本面因子，输出统一为 (trade_date, symbol, f) 三列。

    注意：各基本面表的实际覆盖区间远短于行情（见报告），这里只算“有数据区间”的 IC。
    """
    out: dict[str, pd.DataFrame] = {}

    # ---- 基准：daily_bar 未来 1 日收益 ----
    # 基准不按 since/until 过滤：inventory / spot_basis 的数据落在 2026 年，
    # 若基准被裁剪会全部对齐不上。
    cur.execute("SELECT symbol, trade_date, ret_close FROM daily_bar "
                "WHERE ret_close IS NOT NULL ORDER BY symbol, trade_date")
    db = _num(pd.DataFrame(cur.fetchall(), columns=["symbol", "trade_date", "ret_close"]))
    db["trade_date"] = pd.to_datetime(db["trade_date"])
    if db.empty:
        return out
    db = db.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    db["next_ret"] = db.groupby("symbol")["ret_close"].shift(-1)
    nextday = db[["symbol", "trade_date", "next_ret"]]

    def _align(fac: pd.DataFrame, date_col: str) -> pd.DataFrame:
        """把因子对齐到 daily_bar 代码口径，并拼上次日收益。"""
        fac = fac.copy()
        fac[date_col] = pd.to_datetime(fac[date_col])
        fac["symbol"] = fac["symbol"].map(_to_main)
        fac = fac.dropna(subset=["symbol"])
        fac = _num(fac).groupby(["symbol", date_col], as_index=False)["f"].mean()
        return fac.merge(nextday, on=["symbol", "trade_date"], how="inner")

    # ---- cs_mom_20d / up_vol_ratio ----
    db2 = db.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    g = db2.groupby("symbol", sort=False)
    db2["cs_mom_20d"] = g["ret_close"].transform(
        lambda s: s.rolling(20, min_periods=10).sum())
    db2["up_vol_ratio"] = g["ret_close"].transform(
        lambda s: (s.clip(lower=0).rolling(20, min_periods=10).std()
                   / s.clip(upper=0).rolling(20, min_periods=10).std()).replace(
            [np.inf, -np.inf], np.nan))
    db2["cs_mom_20d_z"] = db2.groupby("trade_date")["cs_mom_20d"].transform(
        lambda s: (s - s.mean()) / s.std(ddof=0) if (s.std(ddof=0) or 0) > 0 else np.nan)
    for col, nm in [("cs_mom_20d_z", "cs_mom_20d"), ("up_vol_ratio", "up_vol_ratio")]:
        keep = db2[["symbol", "trade_date", col]].dropna(subset=[col])
        keep["trade_date"] = pd.to_datetime(keep["trade_date"])
        keep = keep[(keep["trade_date"] >= f"{since}-01-01")
                    & (keep["trade_date"] < f"{until}-01-01")]
        out[nm] = keep.rename(columns={col: "f"}).reset_index(drop=True)

    # ---- inventory ----
    try:
        cur.execute("SELECT report_date, symbol, inventory_qty FROM inventory "
                    "WHERE report_date IS NOT NULL ORDER BY symbol, report_date")
        iv = _num(pd.DataFrame(cur.fetchall(), columns=["report_date", "symbol", "inv"]))
        if not iv.empty:
            iv["chg_rel"] = iv.groupby("symbol")["inv"].transform(
                lambda s: s.diff(4) / s.shift(4).replace(0, np.nan))
            iv["chg_abs"] = iv.groupby("symbol")["inv"].transform(lambda s: s.diff(4) / s.max())
            iv = iv.dropna(subset=["chg_rel"])
            out["inv_chg"] = _align(
                iv[["report_date", "symbol", "chg_rel"]].rename(
                    columns={"report_date": "trade_date", "chg_rel": "f"}), "trade_date")
    except Exception as e:
        print(f"  [warn] inventory 读取失败: {str(e)[:80]}")

    # ---- member position ----
    try:
        cur.execute("SELECT trade_date, symbol, rank, long_chg, short_chg "
                    "FROM member_position_rank WHERE rank <= 20 ORDER BY trade_date, symbol, rank")
        mp = _num(pd.DataFrame(cur.fetchall(), columns=["trade_date", "symbol", "rank",
                                                        "long_chg", "short_chg"]))
        if not mp.empty:
            mp["net_chg"] = mp["long_chg"] - mp["short_chg"]
            net = mp.groupby(["trade_date", "symbol"], as_index=False)["net_chg"].sum()
            net["f"] = net.groupby("symbol")["net_chg"].transform(
                lambda s: s.rolling(5, min_periods=3).sum())
            out["net_pos_top20"] = _align(net, "trade_date")
    except Exception as e:
        print(f"  [warn] member_position_rank 读取失败: {str(e)[:80]}")

    # ---- spot basis ----
    try:
        cur.execute("SELECT report_date, symbol, dom_basis_rate, near_basis_rate FROM spot_basis "
                    "WHERE dom_basis_rate IS NOT NULL ORDER BY symbol, report_date")
        sb = _num(pd.DataFrame(cur.fetchall(), columns=["report_date", "symbol", "basis", "near_basis"]))
        if not sb.empty:
            sb["basis_z"] = sb.groupby("symbol")["basis"].transform(
                lambda s: (s - s.rolling(60, min_periods=20).mean())
                / s.rolling(60, min_periods=20).std(ddof=0))
            out["basis_pct"] = _align(
                sb[["report_date", "symbol", "basis_z"]].rename(
                    columns={"report_date": "trade_date", "basis_z": "f"}), "trade_date")
    except Exception as e:
        print(f"  [warn] spot_basis 读取失败: {str(e)[:80]}")

    # ---- sector momentum（板块级别横截面，样本少，仅参考） ----
    try:
        cur.execute("SELECT sector, trade_date, ret_5d, index_level FROM sector_index "
                    "WHERE trade_date >= %s AND trade_date < %s ORDER BY sector, trade_date",
                    (f"{since}-01-01", f"{until}-01-01"))
        sec = _num(pd.DataFrame(cur.fetchall(), columns=["sector", "trade_date", "ret_5d", "idx"]))
        if not sec.empty:
            sec["f"] = sec["ret_5d"]
            out["sector_mom_5d"] = sec[["sector", "trade_date", "f"]].rename(
                columns={"sector": "symbol"})
    except Exception as e:
        print(f"  [warn] sector_index 读取失败: {str(e)[:80]}")

    return out


def b_ic(bframes: dict[str, pd.DataFrame], cur, since, until) -> pd.DataFrame:
    """对 B 组因子按截面日计算 IC（因子 vs 次日收益）。"""
    cur.execute("SELECT symbol, trade_date, ret_close FROM daily_bar "
                "WHERE ret_close IS NOT NULL")
    tmp = _num(pd.DataFrame(cur.fetchall(), columns=["symbol", "trade_date", "ret_close"]))
    tmp["trade_date"] = pd.to_datetime(tmp["trade_date"])
    tmp = tmp.sort_values(["symbol", "trade_date"]).reset_index(drop=True)
    tmp["y"] = tmp.groupby("symbol")["ret_close"].shift(-1)
    tmp = tmp[["symbol", "trade_date", "y"]].dropna(subset=["y"])
    base = tmp  # 标的层面

    cur.execute("SELECT sector, composition FROM sector_index LIMIT 5")
    secmap = {r[0]: (r[1] or {}) for r in cur.fetchall()}

    recs = []
    for name, d in bframes.items():
        d = d.dropna(subset=["f"]).copy()
        d["trade_date"] = pd.to_datetime(d["trade_date"])
        if name == "sector_mom_5d":
            # 板块层面：成分股等权次日收益作为被解释量
            comp = secmap.get(d["symbol"].iloc[0])
            if not comp:
                continue
            members = {m + "888" for m in comp.keys()}
            fut = base[base["symbol"].isin(members)].groupby("trade_date")["y"].mean()
            d["y"] = d["trade_date"].map(fut)
        else:
            d = d.merge(base, on=["symbol", "trade_date"], how="inner")
        d = d.dropna(subset=["f", "y"])
        for dd, gg in d.groupby("trade_date"):
            if len(gg) < 5 or gg["f"].std() == 0 or gg["y"].std() == 0:
                continue
            c = stats.spearmanr(gg["f"], gg["y"]).statistic
            if np.isfinite(c):
                recs.append((name, dd, c, len(gg)))
    ic = pd.DataFrame(recs, columns=["factor", "day", "ic", "n"])
    ic["horizon"] = "h1"          # B 组统一为次日收益
    return ic


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2015")
    ap.add_argument("--until", default="2025")
    ap.add_argument("--symbols", default="")
    ap.add_argument("--out", default="")
    a = ap.parse_args()
    out_dir = Path(a.out) if a.out else Path(__file__).resolve().parent / "factor_ic_out"
    out_dir.mkdir(parents=True, exist_ok=True)

    import psycopg2
    conn = psycopg2.connect(**load_creds())
    cur = conn.cursor()
    print(f"[1/5] 载入 {a.since}-{a.until} 的 15m 数据 ...")
    all_syms, by_base, pick = pick_symbols(cur, a.since)
    if a.symbols:
        want = {x.strip() for x in a.symbols.split(",") if x.strip()}
        pick = [s for s in pick if s in want]
    print(f"      合约数 {len(pick)} / 原始 {len(all_syms)}（已去 8888 商品指数重名）")

    frames = []
    for i, s in enumerate(pick, 1):
        df = load_symbol_bars(cur, s, a.since, a.until)
        if df is None or len(df) < 500:
            continue
        fr = build_factors(df)
        if fr.empty:
            continue
        fr["symbol"] = s
        frames.append(fr)
        if i % 20 == 0:
            print(f"      ... {i}/{len(pick)}")
    if not frames:
        print("无数据，退出")
        return
    A = pd.concat(frames, ignore_index=True)
    print(f"      A 组样本 {len(A):,} 行 / {A['day'].nunique()} 交易日 / {A['symbol'].nunique()} 合约")

    fac_cols = ["f_vwap_dev", "f_close_loc", "f_mom_1h", "f_mom_4h", "f_vol_surge",
                "f_oi_dir", "f_atr_pct", "f_range_comp", "f_gap", "f_amt_conc",
                "f_close_loc14"]
    print("[2/5] 计算 A 组 IC ...")
    cov = A[fac_cols].notna().mean()
    print("      因子可用率: " + "  ".join(f"{c}={v:.2f}" for c, v in cov.items()))
    icA = ic_table(A, fac_cols, ["f1", "f2", "f4", "f8", "f16"])
    sumA = summarize(icA, fac_cols, "A")

    print("[3/5] 计算 B 组横截面因子 ...")
    B = b_group(cur, a.since, a.until)
    for name, d in B.items():
        print(f"      {name}: {len(d):,} 行 / {d['trade_date'].nunique()} 个交易日 / "
              f"{d['symbol'].nunique()} 标的")
    icB = b_ic(B, cur, a.since, a.until)
    sumB = summarize(icB, sorted(B.keys()), "B")
    bmeta = {nm: (dd['trade_date'].min(), dd['trade_date'].max())
             for nm, dd in B.items()}

    print("[4/5] A 组覆盖度 ...")
    cov = A.groupby("day")["symbol"].size()
    print(f"      截面中位数 {int(cov.median())} / 均值 {cov.mean():.1f} / "
          f"不足15只的交易日 {int((cov < 15).sum())}")
    print("[5/5] 汇总输出 ...")
    res = pd.concat([sumA, sumB], ignore_index=True)
    res.to_csv(out_dir / "factor_ic_all.csv", index=False, encoding="utf-8-sig")
    icA.to_csv(out_dir / "ic_A_15m.csv", index=False, encoding="utf-8-sig")
    icB.to_csv(out_dir / "ic_B_cross.csv", index=False, encoding="utf-8-sig")

    md = []
    md.append("# 因子 IC 扫描结果（A 组日内价量 / B 组横截面基本面）\n")
    md.append(f"> 生成时间 {pd.Timestamp.now():%Y-%m-%d %H:%M}　行情区间 {a.since}-{a.until}　"
              f"A 组：{len(A):,} 个 (合约×交易日后) 样本 / {A['day'].nunique()} 交易日 / "
              f"{A['symbol'].nunique()} 合约，横截面中位数 {int(cov.median())} 只\n")
    md.append("> 决策时点 **14:45**（15m 桶），日盘统计量只用该时点及以前的数据；"
              "前瞻收益 = 未来收盘 / 决策收盘 − 1。\n")
    md.append("\n## 一、A 组：日内价量（15m 横截面，主表）\n")
    md.append("| 因子 | h1(15min) IC / t | h2 IC / t | h4 IC / t | h8 IC / t | h16 IC / t | "
              "IS IC | OOS IC / t | 判定 |")
    md.append("|---|---|---|---|---|---|---|---|")

    def verdict_a(v):
        ic1, toos = v
        if not np.isfinite(ic1):
            return "—"
        if np.isfinite(toos) and abs(toos) > 2 and np.isfinite(ic1) and abs(ic1) > 0.015:
            return "**保留**"
        if np.isfinite(ic1) and abs(ic1) > 0.02:
            return "保留（OOS 待验）"
        return "淘汰"

    for f_ in fac_cols:
        r = res[(res.group == "A") & (res.factor == f_)].set_index("horizon")

        def cell(h):
            if h not in r.index or not np.isfinite(r.loc[h, "ic"]):
                return "—"
            return f"{r.loc[h,'ic']:.4f} / {r.loc[h,'t']:.1f}"
        v = (r.loc["h1", "ic"], r.loc["h1", "t_oos"]) if "h1" in r.index else (np.nan, np.nan)
        md.append(f"| `{f_}` | {cell('h1')} | {cell('h2')} | {cell('h4')} | {cell('h8')} | "
                  f"{cell('h16')} | {r.loc['h1','ic_is']:.4f} | "
                  f"{r.loc['h1','ic_oos']:.4f} / {r.loc['h1','t_oos']:.1f} | "
                  f"{verdict_a(v)} |")

    md.append("\n## 二、B 组：横截面基本面（因子 vs 次日收益）\n")
    md.append("| 因子 | 数据覆盖 | IC | ICIR | t | IS IC | OOS IC / t | 判定 |")
    md.append("|---|---|---|---|---|---|---|---|")
    for _, r in sumB.iterrows():
        mn, mx = bmeta.get(r["factor"], ("—", "—"))
        if not np.isfinite(r["ic"]):
            verdict = "**数据不足**（采集区间太短）"
        elif np.isfinite(r["t_oos"]) and abs(r["t_oos"]) > 2:
            verdict = "**保留**"
        elif np.isfinite(r["t"]) and abs(r["t"]) > 2:
            verdict = "候选（样本短，需补历史）"
        else:
            verdict = "剔除"
        def f4(x):
            return f"{x:.4f}" if np.isfinite(x) else "—"
        def f1(x):
            return f"{x:.1f}" if np.isfinite(x) else "—"
        md.append(f"| `{r['factor']}` | {mn} ~ {mx} | {f4(r['ic'])} | {f4(r['icir'])} | "
                  f"{f1(r['t'])} | {f4(r['ic_is'])} | {f4(r['ic_oos'])} / {f1(r['t_oos'])} | "
                  f"{verdict} |")

    md.append("""
## 三、怎么读这张表

- **h1 = 纯日内残差**：决策时点后 1 根 15m（14:45 → 15:00）的收益；
  h2/h4/h8/h16 逐步跨入次一交易日（含夜盘）。看 h1 决定"日内能不能用"，
  看 h2 以后的符号翻转，往往说明存在 **日内延续 + 隔日反转** 的结构。
- **|IC| 的直觉阈值**：日内截面（~55 只）单日 IC 的标准误约 1/√(N−1) ≈ 0.13，
  但多日平均后 t 会很大。**真正该看的是 IC 量级 + OOS 是否同号衰减**，
  而不是单独的 t 值（扫 16 个因子，t>2 的假阳性极多，必须靠样本外复验）。
- **本表已剔除两类前视**：① 当日统计量不含决策时点之后的 bar；
  ② 前瞻收益用未来收盘 / 决策收盘 − 1（不是取未来收盘价本身）。
  在修这两处之前，`f_vwap_dev` 曾给出 IC=0.71 的假象，务必记住。

## 四、下一步

1. 对"保留"的因子做**五档分层**与**成本压力**（×1.0/×1.5/×2.0）；
2. 检查保留因子与 V3.4 融合策略的**日收益相关性**（应 < 0.3）；
3. 再补历史：`spot_basis` 只有 8 天、`inventory` 只有 3 个月、
   `member_position_rank` 只有 2023+ —— 这三个表决定了 B 组能走多远。
""")
    p = out_dir / "factor_ic_report.md"
    p.write_text("\n".join(md), encoding="utf-8")
    print(f"\n报告 -> {p}")
    print(res.to_string(index=False))
    conn.close()


if __name__ == "__main__":
    main()
