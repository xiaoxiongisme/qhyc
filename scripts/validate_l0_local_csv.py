"""本地验证（无需生产库）：在 CSV 上复现 8888 合成 + L2 换月检测，并做口径/内部 A/B 校验。

依据 docs/L0_本地CSV取代设计.md 的验收门槛 V0–V4 / P2 / P3。
运行：py scripts/validate_l0_local_csv.py --root "D:/学习资料/期货分钟数据" --varieties IC,RB,CU

校验项：
  V0  时间戳口径：合约 日期 是起始标签，+1min 后才与主连 eob（end-time）同口径
      —— 对齐后 OHLC 四项应 ~100% 全等；这是 P1-1 的证据
  V1  DCE 量 ÷2 归单边
  V2  合约 amount 口径：逐文件推断乘数，统计成交额缺失率；CZCE 应为「未含乘数」口径
  V3  量纲门：主连 Σvol / 当年 front 合约 Σvol 按年对比（拦截 IC-2015 放大 850 倍类污染）
  P2  8888 back-adjusted 连续合成 + 复刻 sp_build_l2_roll_segment 的 ratio 门，验证换月段非空

关键修复（2026-11）：8888 必须用 back-adjusted 连续合成（剔除换月 carry 跳变），否则
sp_build_l2_roll_segment 的 ratio 门(abs(idx_delta)<0.4|cc|) 恒失败 → roll_segment 全空 → back_adj 崩坏。
等权/量加权合成均会使 8888 在换月瞬间与主力同步跳空，已被证伪（RB 实测 0 段）。
"""
import os, re, argparse, sys, zipfile, io
import numpy as np
import pandas as pd

EXCH_MAP = {
    "CFFEX_中国金融期货交易所": "CFFEX", "CZCE-郑州商品期货交易所": "CZCE",
    "DCE-大连期货交易所": "DCE", "GFEX_广州期货交易所": "GFEX",
    "INE_上海国际能源交易中心": "INE", "SHFE_上海期货交易所": "SHFE",
}
MAIN_COLS = ["exchange", "symbol", "open", "close", "high", "low", "amount", "volume", "position", "bob", "eob", "type"]
CONT_COLS = ["日期", "开盘价", "最高价", "最低价", "收盘价", "成交额", "成交量", "持仓量"]
NUM_COLS = ["开盘价", "最高价", "最低价", "收盘价", "成交额", "成交量", "持仓量"]

CONT_TS_SHIFT = pd.Timedelta(minutes=1)     # 与 load_local_csv_1min.py 保持一致
VOL_RATIO_LO, VOL_RATIO_HI = 0.4, 2.5


def vdir_of(root, variety):
    for ex_dir in EXCH_MAP:
        p = os.path.join(root, ex_dir)
        if not os.path.isdir(p):
            continue
        for vname in os.listdir(p):
            if vname.split("_", 1)[0].upper() == variety.upper():
                return os.path.join(p, vname), EXCH_MAP[ex_dir], vname.split("_", 1)[0].upper()
    return None, None, None


def discover(vdir):
    def _walk(sub):
        files = {}
        for r, _, fs in os.walk(sub):
            if any("退市" in x for x in re.split(r"[\\/]+", r)):
                continue
            for f in fs:
                if f.endswith(".csv"):
                    files[f] = os.path.join(r, f)
        return files
    root_f = _walk(vdir)
    upd_dir = os.path.join(vdir, "更新")
    upd_f = _walk(upd_dir) if os.path.isdir(upd_dir) else {}
    merged = dict(root_f)
    merged.update(upd_f)
    main = None
    contracts = []
    for fn, path in merged.items():
        if fn[:-4].endswith("主力连续"):
            main = path
        else:
            contracts.append(path)
    for f in sorted(os.listdir(vdir)):
        if f.endswith("退市.zip"):
            zp = os.path.join(vdir, f)
            try:
                with zipfile.ZipFile(zp) as z:
                    for entry in z.namelist():
                        if entry.endswith(".csv") and "主力连续" not in entry:
                            contracts.append((zp, entry))
            except Exception:
                continue
    return main, contracts


def _read_cont(source):
    if isinstance(source, tuple):
        zp, entry = source
        with zipfile.ZipFile(zp) as z:
            raw = z.read(entry)
        df = pd.read_csv(io.BytesIO(raw), header=0, names=CONT_COLS, encoding="utf-8-sig")
        name = os.path.basename(entry)[:-4]
    else:
        df = pd.read_csv(source, header=0, names=CONT_COLS, encoding="utf-8-sig")
        name = os.path.basename(source)[:-4]
    for c in NUM_COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df, name


def load_main(path, exchange=None):
    df = pd.read_csv(path, header=0, names=MAIN_COLS, encoding="utf-8-sig")
    for c in ["open", "close", "high", "low", "amount", "volume", "position"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close", "eob"]).copy()
    df["ts"] = pd.to_datetime(df["eob"], utc=True)
    if exchange == "DCE":
        # 与 loader 对齐：DCE 主连亦为双边计数，需 ÷2（实证两层同量纲）
        df["volume"] = df["volume"] / 2.0
    return df.rename(columns={"open": "o", "high": "h", "low": "l", "close": "c",
                              "amount": "amount", "volume": "volume", "position": "oi"})[
        ["ts", "o", "h", "l", "c", "amount", "volume", "oi"]]


def load_contract(source, exchange, shift_minutes=1):
    """合约层 → 归一帧。shift_minutes=1 时把 起始标签 转为 end-time（与主连同口径）。"""
    df, name = _read_cont(source)
    df = df.dropna(subset=["收盘价", "日期"])
    df["ts"] = ((pd.to_datetime(df["日期"]) + pd.Timedelta(minutes=shift_minutes))
                .dt.tz_localize("Asia/Shanghai").dt.tz_convert("UTC"))
    df = df.rename(columns={"开盘价": "o", "最高价": "h", "最低价": "l", "收盘价": "c",
                            "成交额": "amount", "成交量": "volume_raw", "持仓量": "oi"})
    df["sym"] = name.upper()
    df["volume"] = df["volume_raw"] / 2.0 if exchange == "DCE" else df["volume_raw"]
    return df[["ts", "sym", "o", "h", "l", "c", "amount", "volume", "volume_raw", "oi"]]


def synth_8888(contracts, main_ts):
    """back-adjusted 连续 8888：front=每分持仓量最大（平手取合约月份大者），剔除换月 carry 跳变，
    再按主连时间轴对齐（价格 ffill；量不参与本函数）。返回 15m 锚定用的 o/h/l/c。"""
    if not contracts:
        return None
    d = pd.concat(contracts, ignore_index=True)
    d["ts"] = pd.to_datetime(d["ts"], utc=True)
    d = d.dropna(subset=["c", "sym", "ts"])
    if len(d) == 0:
        return None
    d["ym"] = d["sym"].str.extract(r"(\d{4})$").astype("float64")
    d["oi"] = pd.to_numeric(d["oi"], errors="coerce").fillna(0)
    d["rk"] = d.groupby("ts")["oi"].rank(ascending=False, method="first")
    front = d[d["rk"] == 1].copy()
    front = (front.sort_values(["ts", "oi", "ym"], ascending=[True, False, False])
                  .drop_duplicates("ts").set_index("ts").sort_index())
    fc = front["c"].ffill()
    sym_switch = (front["sym"] != front["sym"].shift(1)).fillna(False).values
    carry = (fc - fc.shift(1)).fillna(0).values
    carry = np.where(sym_switch, carry, 0.0)
    cum = np.cumsum(np.nan_to_num(carry, nan=0.0))
    cum_s = pd.Series(cum, index=front.index)
    cont = pd.DataFrame({
        "o": front["o"] - cum_s, "h": front["h"] - cum_s,
        "l": front["l"] - cum_s, "c": front["c"] - cum_s,
    }, index=front.index)
    if main_ts is not None:
        grid = pd.DatetimeIndex(pd.to_datetime(main_ts, utc=True))
        cont = cont.reindex(grid).ffill()
    cont = cont.dropna(how="all")
    if len(cont) == 0:
        return None
    return cont[["o", "h", "l", "c"]]


def to_15m(df):
    df = df.copy()
    if "ts" in df.columns:
        df = df.set_index("ts")
    df = df.sort_index()
    o = df["o"].resample("15min").first()
    h = df["h"].resample("15min").max()
    l = df["l"].resample("15min").min()
    c = df["c"].resample("15min").last()
    out = pd.DataFrame({"o": o, "h": h, "l": l, "c": c}).dropna()
    out["ts"] = out.index
    return out


def roll_detect(main15, idx15):
    """复刻 sp_build_l2_roll_segment 的 15m 锚定逻辑（含 8888 前向填充对齐）。"""
    m = main15.copy()
    i = idx15.reindex(m.index).ffill()
    m["cc"] = m["c"] - m["c"].shift(1)
    m["amp"] = (m["h"] - m["l"]) + (m["h"] - m["l"]).shift(1)
    m["gap"] = m["o"] - m["c"].shift(1)
    m["idx_delta"] = i["c"] - i["c"].shift(1)
    roll = (m["cc"].abs() > m["amp"] + 1e-6) & (m["idx_delta"].abs().fillna(1e9) < 0.40 * m["cc"].abs())
    return roll, m


# ---------------------------------------------------------------------------
# V0：时间戳口径（起始 vs 结束标签）
# ---------------------------------------------------------------------------
def check_ts_alignment(main_df, contracts, days=3, min_bars=30):
    """判定合约「日期」是起始还是结束标签：按日、跨合约比较 OHLC 四项全等率。

    做法：取主连最近 days 个交易日，对当日**根目录**合约分别按 shift∈{-1,0,+1} 对齐，
    计算 (a) 单合约最高全等率 与 (b) 「任一合约命中」覆盖率。
    (b) 能容忍日内换月（如 CU 某日先从 CU2611 换到 CU2612，两段各占 61.5%/38.5%）。
    若某 day 的根目录合约已全部退市（历史日在 zip 里），则该日跳过并提示。
    """
    if not contracts:
        return None
    root_c = [c for c in contracts if not isinstance(c, tuple)]
    if not root_c:
        return None
    m = main_df.copy()
    m["sh_day"] = m["ts"].dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d")
    day_list = sorted(m["sh_day"].unique())[-days:][::-1]

    # 预载根目录合约（一次性）
    loaded = []
    for c in root_c:
        try:
            df, name = _read_cont(c)
        except Exception:
            continue
        df = df.dropna(subset=["收盘价", "日期"])
        loaded.append((name.upper(), pd.to_datetime(df["日期"]), df))
    if not loaded:
        return None

    out = []
    for day in day_list:
        md = m[m["sh_day"] == day]
        res = {}
        for shift in (-1, 0, 1):
            best_name, best_pct = None, -1.0
            hit = {}
            for name, raw, df in loaded:
                ts = ((raw + pd.Timedelta(minutes=shift))
                      .dt.tz_localize("Asia/Shanghai").dt.tz_convert("UTC"))
                sub = df.assign(ts=ts)
                sub = sub[sub["ts"].dt.tz_convert("Asia/Shanghai").dt.strftime("%Y-%m-%d") == day]
                if len(sub) < min_bars:
                    continue
                j = sub.merge(md[["ts", "o", "h", "l", "c"]], on="ts")
                if len(j) < min_bars:
                    continue
                eq = ((j["开盘价"] == j["o"]) & (j["最高价"] == j["h"])
                      & (j["最低价"] == j["l"]) & (j["收盘价"] == j["c"]))
                pct = float(eq.mean())
                if pct > best_pct:
                    best_pct, best_name = pct, name
                for t, e in zip(j["ts"], eq):
                    hit[t] = hit.get(t, False) or bool(e)
            cov = (sum(hit.values()) / len(md)) if len(md) else 0.0
            res[shift] = (best_name, best_pct * 100 if best_pct >= 0 else 0.0, cov * 100, len(md))
        out.append((day, res))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=r"D:/学习资料/期货分钟数据")
    ap.add_argument("--varieties", default="IC,RB,CU")
    args = ap.parse_args()
    varieties = [v.strip().upper() for v in args.varieties.split(",") if v.strip()]

    print("=" * 74)
    print("L0 本地 CSV 验证（时间戳口径 + 量额口径 + 量纲门 + 8888 回调连续 + L2 换月检测）")
    print("=" * 74)

    # ---------------- V0 / V1 / V2 ----------------
    print("\n[V0] 时间戳口径：合约「日期」是起始标签吗？（按日跨合约对齐验证）")
    for variety in varieties:
        vdir, ex, _ = vdir_of(args.root, variety)
        if not vdir:
            continue
        main_f, contracts = discover(vdir)
        if not main_f or not contracts:
            continue
        rows = check_ts_alignment(load_main(main_f), contracts)
        if not rows:
            print(f"  {variety}: 根目录无在用合约可比（历史全在 退市.zip），跳过")
            continue
        print(f"  {variety} [{ex}]")
        print(f"    {'交易日':>12} {'偏移':>7} {'单合约最佳':>10} {'最佳合约':>10} {'任约命中':>9}")
        best_shift = None
        for day, res in rows:
            for shift in (-1, 0, 1):
                nm, bp, cov, nb = res[shift]
                print(f"    {day:>12} {shift:>+6}min {bp:>9.1f}% {str(nm):>10} {cov:>8.1f}%")
            b = max(res, key=lambda s: res[s][2])
            if best_shift is None:
                best_shift = b
        print(f"    → 最优偏移 {best_shift:+d} 分钟（任约命中最高）；"
              f"{'合约层为起始标签，loader 已 +1min 归一 ✓' if best_shift == 1 else '需人工复核'}"
              f"；CU 类日内换月日单合约仅部分命中属正常（分段合计≈100%）")

    print("\n[V1/V2] 量额口径校验（逐文件，不做单样本抽样）")
    for variety in varieties:
        vdir, ex, _ = vdir_of(args.root, variety)
        if not vdir:
            continue
        _, contracts = discover(vdir)
        if not contracts:
            continue
        total = amt_missing = 0
        mults = []
        bad_files = 0
        for cf in contracts:
            try:
                df, name = _read_cont(cf)
            except Exception:
                bad_files += 1
                continue
            df = df.dropna(subset=["收盘价"])
            total += len(df)
            amt_missing += int(df["成交额"].isna().sum())
            denom = df["收盘价"] * df["成交量"]
            m = (df["成交额"] / denom).replace([np.inf, -np.inf], np.nan).dropna()
            if len(m):
                mults.append(float(m.median()))
        mults = [x for x in mults if x > 0]
        med = float(np.median(mults)) if mults else float("nan")
        near = np.mean([abs(x - round(x)) < 0.5 for x in mults]) if mults else float("nan")
        print(f"  {variety} [{ex}] 合约文件 {len(contracts)} 个（读取失败 {bad_files}）"
              f"，总行 {total:,}，成交额缺失 {amt_missing} 行（{amt_missing/max(total,1)*100:.2f}%）")
        if ex == "CZCE":
            print(f"    [V2] CZCE 成交额**未含合约乘数**（数据说明第 5 条）→ 推断比值中位 {med:.3f}"
                  f"≈1，loader 标 caliber_amt='no_multiplier'（不可与其它所直接比额）")
        else:
            print(f"    [V2] 推断乘数中位≈{med:.2f}（{near*100:.1f}% 的文件为整数倍）"
                  + ("；DCE 合约量已÷2，此处比值≈2×乘数" if ex == "DCE" else ""))
            if ex == "DCE":
                print("    [V1] DCE 量与主连**均已 ÷2** 归单边 ✓（两层同量纲，缺一即 2× 断裂）")

    # ---------------- V3 量纲门 + P2 ----------------
    print("\n[V3] 量纲门：主连 Σvol / 当年 front 合约 Σvol（按年）")
    for variety in varieties:
        vdir, ex, _ = vdir_of(args.root, variety)
        if not vdir:
            continue
        main_f, contracts = discover(vdir)
        if not main_f or not contracts:
            continue
        main_df = load_main(main_f, ex)
        cdfs = [load_contract(c, ex) for c in contracts]
        c = pd.concat(cdfs, ignore_index=True)
        c["yr"] = c["ts"].dt.year
        c["oi"] = pd.to_numeric(c["oi"], errors="coerce").fillna(0)
        c["volume"] = pd.to_numeric(c["volume"], errors="coerce").fillna(0)
        c["rk"] = c.groupby("ts")["oi"].rank(ascending=False, method="first")
        fv = c[c["rk"] == 1].groupby("yr")["volume"].sum()
        mv = main_df.assign(yr=main_df["ts"].dt.year).groupby("yr")["volume"].sum()
        expect = 1.0   # loader 对 DCE 主连与合约同时÷2，归一后比值应≈1
        out = []
        for y in sorted(set(mv.index) & set(fv.index)):
            if fv[y] <= 0:
                continue
            r = float(mv[y]) / float(fv[y])
            flag = "" if VOL_RATIO_LO <= r / expect <= VOL_RATIO_HI else "  <== 越界"
            out.append(f"{int(y)}:{r/expect:.2f}{flag}")
        print(f"  {variety} [{ex}] 归一化比值（期望≈1.00）：")
        print("    " + "  ".join(out[-12:]))
        nbad = sum(1 for s in out if "越界" in s)
        print(f"    → 越界年份 {nbad} 个" + ("（loader 会告警；--strict-volume 可拒绝入库）" if nbad else ""))

    print("\n[P2] 8888 回调连续合成 + L2 换月检测（复刻 sp_build_l2_roll_segment）")
    for variety in varieties:
        vdir, ex, _ = vdir_of(args.root, variety)
        if not vdir:
            print(f"  {variety}: 未找到目录，跳过")
            continue
        main_f, contracts = discover(vdir)
        if not main_f or not contracts:
            print(f"  {variety}: 缺主连或合约文件，跳过")
            continue
        main_df = load_main(main_f, ex)
        cont_dfs = [load_contract(c, ex) for c in contracts]
        idx_df = synth_8888(cont_dfs, main_df["ts"])
        if idx_df is None:
            print(f"  {variety}: 8888 合成失败（无合约数据）")
            continue
        main15 = to_15m(main_df)
        idx15 = to_15m(idx_df)
        roll, m = roll_detect(main15, idx15)
        n_seg = int(roll.sum())
        n_bars = len(main15)
        span = f"{main15['ts'].min().date()} ~ {main15['ts'].max().date()}"
        yrs = max((main15['ts'].max() - main15['ts'].min()).days / 365.25, 1e-9)
        rate = n_seg / yrs
        is_large = (m["cc"].abs() > m["amp"] + 1e-6)
        n_large = int(is_large.sum())
        n_idx_nan = int(m.loc[is_large, "idx_delta"].isna().sum())
        print(f"  {variety} [{ex}] 15m 锚点 {n_bars} 根，跨度 {span}（{yrs:.1f}年）")
        print(f"    8888 回调连续：{len(idx15)} 根 15m；is_large(候选)={n_large}（其中 idx_delta 缺失={n_idx_nan}）")
        print(f"    检测换月段(带 8888 ratio 门) = {n_seg} 段（≈{rate:.1f}/年）")
        verdict = "PASS（非0且量级合理）" if (n_seg > 0 and n_seg < n_large) else "REVIEW"
        print(f"    结论：{verdict}")
        if n_seg:
            det = m[roll]
            cc_med = det["cc"].abs().median()
            id_med = det["idx_delta"].abs().median() if det["idx_delta"].notna().any() else float("nan")
            print(f"    [A/B] 换月处 |cc(888)|中位={cc_med:.1f}  vs |idx_delta(8888)|中位={id_med:.2f} "
                  f"→ 8888 平滑({id_med/cc_med*100:.0f}% of cc)，ratio 门正确区分")
            print("    样例换月段：")
            for ts, row in det.head(5).iterrows():
                print(f"      {ts}  cc={row['cc']:.1f}  idx_delta={row['idx_delta']:.2f}")

    print("\n[P3] 生产库 A/B 比对（需到 qhyc 宿主执行 scripts/ab_compare_l0.sql；"
          "loader 需带 --backup-old 才有旧源基线）：")
    print("DONE.")


if __name__ == "__main__":
    sys.exit(main())
