# -*- coding: utf-8 -*-
"""计算 V1（量价维度）与 V6（波动率分位）新因子 → factor_value。

来源：《架构设计_CB执行版.md》T14/T15 + 《PRD_借鉴落地验证.md》V1/V6。

新增因子（均已通过 migrations/002 预注册，enabled=false）
------------------------------------------------------
  f_vol_ratio          量比 VR  = 当日成交量 / 过去 20 日成交量中位数（>1.5 放量，<0.5 缩量）
  f_vol_z              量标准分 VZ = (当日量 − 40日均值) / 40日标准差（>2 极端放量）
  f_voldiv_divergence  量价背离 = +1 顶部背离（创新高但缩量）/ −1 底部背离（创新低但缩量）/ 0 无
  f_atr_pctile         ATR 分位 = ATR14 在过去 250 日中的分位数（>90% 降仓，<10% 警戒）

⚠️ 前视防护（与 factor_ic_scan.build_factors 同口径，红线）
--------------------------------------------------------
所有「当日统计量」只使用**决策时点（日盘最后一根 14:45）及以前**的 bar，
否则当日成交量会含 15:00 收盘那根，与未来收益产生机械相关（虚假 IC）。

用法
----
  python scripts/compute_factor_v1v6.py --since 2025 --limit 5 --dry-run
  python scripts/compute_factor_v1v6.py --since 2015
  python scripts/compute_factor_v1v6.py --port 15432      # 云端
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg2

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from factor_ic_scan import load_creds, load_symbol_bars, pick_symbols  # noqa: E402
from pgconn import add_conn_args, conn_from_args  # noqa: E402

VERSION = "1.0"
NEW_FACTORS = ("f_vol_ratio", "f_vol_z", "f_voldiv_divergence", "f_atr_pctile")

#: 决策时点：日盘最后一根（14:45）之后的数据不可用
DECISION_HM = "15:00"


def _cut_pre_decision(df: pd.DataFrame, hm: str = DECISION_HM) -> pd.DataFrame:
    """前视防护核心：丢弃决策时点（hm）及之后的 bar，只保留决策时点之前的数据。

    抽成独立函数以便单测 monkeypatch —— 验证「一旦防护被删，哨兵测试能报警」
    （2026-09-27 静默退化教训：不能依赖一个「压根没接线」也能通过的测试）。
    """
    return df[df["hm"] < hm].copy()


def _atr_wilder(h, l, c, n=14):
    m = len(c)
    if m == 0:
        return np.array([])
    tr = np.empty(m)
    tr[0] = h[0] - l[0]
    if m > 1:
        pc = c[:-1]
        tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - pc), np.abs(l[1:] - pc)])
    atr = np.full(m, np.nan)
    if m < n:
        return atr
    atr[n - 1] = np.nanmean(tr[:n])
    for i in range(n, m):
        atr[i] = (atr[i - 1] * (n - 1) + tr[i]) / n
    return atr


def has_lookahead_guard() -> bool:
    """T16 启动守卫：用合成数据验证「决策时点后 bar 不会泄漏进当日统计量」。

    构造某合约 30 个正常日 + 最后 1 日：前决策时点 bar 成交量都小（=10），
    唯独该日 15:00 那根塞入 1000 倍巨量。若防护有效，末日成交量之和应**不含**
    15:00 那根 → f_vol_ratio 接近且略大于 1（用自身历史中位数）；若防护失效，
    末日 sum 被巨量稀释，vr 会骤降到 ~0.04。返回 True 表示防护有效。
    """
    rows = []
    n_days = 30
    for d in range(n_days):
        day = f"2026-01-{1 + d:02d}"
        for hm in ("09:00", "10:00", "11:00", "13:30", "14:00", "14:30"):
            rows.append(dict(day=day, hm=hm, open=100, high=101, low=99,
                            close=100, volume=10 + d))  # 缓慢爬升，保证中位数有定义
        # 末日追加 15:00 巨量（决策时点之后，必须被滤掉）
        if d == n_days - 1:
            rows.append(dict(day=day, hm="15:00", open=100, high=101, low=99,
                             close=100, volume=1_000_000))
    df = pd.DataFrame(rows)
    df["bucket"] = df["day"] + " " + df["hm"] + ":00"
    out = build_v_factors(df)
    if out.empty:
        return False
    vr = float(out.iloc[-1]["f_vol_ratio"])
    # 防护有效：末日 sum≈6*(10+29)=234，历史中位数≈6*19=114，vr≈2（合理比值）。
    # 防护失效（15:00 巨量漏入）：sum≈1_000_234，vr≈8774（爆炸式偏离）。
    # 用区间 [0.1, 100] 区分：有效时 vr 落在正常比值带，失效时远超上界。
    return not np.isnan(vr) and 0.1 < vr < 100


def build_v_factors(df: pd.DataFrame) -> pd.DataFrame:
    """对单合约构造日级 V1/V6 因子行。

    入参 df 需含：day, hm, open, high, low, close, volume
    返回列：day, f_vol_ratio, f_vol_z, f_voldiv_divergence, f_atr_pctile
    """
    if df.empty or "day" not in df.columns:
        return pd.DataFrame()
    # 只保留决策时点前的 bar（防前视红线）
    ds = _cut_pre_decision(df)
    if len(ds) < 60:
        return pd.DataFrame()

    ds = ds.sort_values("bucket") if "bucket" in ds.columns else ds
    g = ds.groupby("day")
    vol_d = g["volume"].sum()
    close_d = g["close"].last()
    days = list(vol_d.index)

    # ATR（bar 级 Wilder，取当日最后一根）
    h = ds["high"].to_numpy(float); l = ds["low"].to_numpy(float)
    c = ds["close"].to_numpy(float)
    atr = _atr_wilder(h, l, c, 14)
    tmp = ds.copy()
    tmp["atr"] = atr
    atr_d = tmp.groupby("day")["atr"].last()

    rows = []
    for i, d in enumerate(days):
        v = float(vol_d.loc[d])
        # 量比 / 量标准分：只用**历史**窗口（不含当日）
        hist20 = vol_d.iloc[max(0, i - 20):i].to_numpy(float)
        hist40 = vol_d.iloc[max(0, i - 40):i].to_numpy(float)
        if len(hist20) >= 10:
            med = float(np.median(hist20))
            vr = v / med if med > 0 else np.nan
        else:
            vr = np.nan
        if len(hist40) >= 20:
            mu = float(np.mean(hist40)); sd = float(np.std(hist40))
            vz = (v - mu) / sd if sd > 0 else 0.0
        else:
            vz = np.nan

        # ATR 分位：过去 250 日
        a_now = float(atr_d.iloc[i]) if i < len(atr_d) else np.nan
        hist_atr = atr_d.iloc[max(0, i - 250):i + 1].to_numpy(float)
        hist_atr = hist_atr[np.isfinite(hist_atr)]
        if np.isfinite(a_now) and len(hist_atr) >= 60:
            ap = float((hist_atr <= a_now).mean())   # 0~1 分位
        else:
            ap = np.nan

        # 量价背离：价格创 20 日新高新低但成交未放量确认
        ch = close_d.iloc[max(0, i - 20):i].to_numpy(float)
        c_now = float(close_d.iloc[i])
        div = 0.0
        if len(ch) >= 10 and np.isfinite(vz):
            if c_now >= np.nanmax(ch) and vz < 0:
                div = 1.0     # 顶部背离：创新高但缩量 → 风险预警
            elif c_now <= np.nanmin(ch) and vz < 0:
                div = -1.0    # 底部背离：创新低但缩量
        rows.append(dict(day=d, f_vol_ratio=vr, f_vol_z=vz,
                         f_voldiv_divergence=div, f_atr_pctile=ap))
    return pd.DataFrame(rows)


def _zscore_cross_section(df: pd.DataFrame, factor: str) -> pd.DataFrame:
    """按 day 做横截面 z 标准化（winsorize 1%/99%）。"""
    out = []
    for d, g in df.groupby("day"):
        v = g[factor].astype(float)
        v = v.replace([np.inf, -np.inf], np.nan)
        if v.notna().sum() < 5:
            continue
        lo, hi = v.quantile(0.01), v.quantile(0.99)
        v = v.clip(lo, hi)
        sd = v.std()
        z = (v - v.mean()) / sd if sd and sd > 0 else v * 0
        gg = g.copy()
        gg["z_value"] = z
        out.append(gg)
    return pd.concat(out) if out else pd.DataFrame()


def compute(cur, since, until, limit=None):
    # pick_symbols 返回 (全部符号, by_base映射, 去重后主力列表)，取第三个
    _, _, syms = pick_symbols(cur, since)
    if limit:
        syms = syms[:limit]
    print(f"[v1v6] {len(syms)} 个品种 | {since}~{until}", flush=True)
    all_rows = []
    for i, sym in enumerate(syms):
        try:
            df = load_symbol_bars(cur, sym, since, until)
            if df is None or len(df) < 60:
                continue
            f = build_v_factors(df)
            if f.empty:
                continue
            f["symbol"] = sym
            all_rows.append(f)
        except Exception as e:  # noqa: BLE001
            print(f"  !! {sym} 失败：{e}", flush=True)
            continue
        if i % 20 == 0:
            print(f"  [{i}/{len(syms)}] {sym} rows={len(f) if not f.empty else 0}", flush=True)
    if not all_rows:
        print("[v1v6] 无数据")
        return []
    big = pd.concat(all_rows, ignore_index=True)
    out = []
    for fac in NEW_FACTORS:
        z = _zscore_cross_section(big, fac)
        if z.empty:
            continue
        for r in z.itertuples():
            out.append((fac, r.day, r.symbol,
                        None if not np.isfinite(getattr(r, fac)) else float(getattr(r, fac)),
                        None if not np.isfinite(r.z_value) else float(r.z_value)))
    print(f"[v1v6] 生成 {len(out)} 行（{len(NEW_FACTORS)} 因子）", flush=True)
    return out


def upsert(cur, rows):
    if not rows:
        return
    from psycopg2.extras import execute_values
    vals = [(f, d, s, raw, z, d, VERSION) for (f, d, s, raw, z) in rows]
    execute_values(cur, """
        INSERT INTO factor_value
          (factor_id, trade_date, symbol, raw_value, z_value, available_at, version)
        VALUES %s
        ON CONFLICT (factor_id, trade_date, symbol) DO UPDATE SET
          raw_value = EXCLUDED.raw_value,
          z_value   = EXCLUDED.z_value,
          available_at = EXCLUDED.available_at,
          version   = EXCLUDED.version
    """, vals, page_size=2000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2015")
    ap.add_argument("--until", default="2027")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    add_conn_args(ap)
    a = ap.parse_args()

    # 统一走 pgconn（环境变量优先），避免 load_creds() 硬编码 127.0.0.1 在容器内连不上
    conn = conn_from_args(a)
    c = psycopg2.connect(**conn)
    cur = c.cursor()
    rows = compute(cur, a.since, a.until, a.limit)
    if not a.dry_run and rows:
        upsert(cur, rows)
        c.commit()
        print(f"[v1v6] 已写入 {len(rows)} 行")
    else:
        print("[v1v6] dry-run，未写库")
    c.close()


if __name__ == "__main__":
    main()
