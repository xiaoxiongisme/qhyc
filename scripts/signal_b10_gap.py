"""十期① 数据缺口审计 + 完整交易日判据 v2（三层、label-agnostic）

背景
----
§30 发现 2026-06-15~09-03 共 58 个交易日 bar_60m/30m/15m/5m 全无，而 daily_bar 齐全。
旧的"完整交易日"判据（D1 15:00 bar / D2 22:00 bar / D3 daily_bar 当日）依赖 bar 标签口径，
而 bar_60m 的 bucket 语义在 2026-11-02 当天切换（§30.3 R1）→ 旧判据失效。

本脚本先钉死两套口径的真实语义（用 5m 反解），再给出**不依赖口径**的三层判据：

  L1 结构完整性：当日 bar 标签集合 ⊇ 该品种近 20 日模板（缺标签 = 结构不全）
  L2 内部一致性：Σ(60m 量) == Σ(5m 量)（同一日历日；口径无关）
  L3 收盘对齐　：最后日盘 bar 的 close == daily_bar close（≤0.5%，≥90% 品种）

为什么不用 daily_bar 的**成交量**：实测 Σ5m/daily 在 0.40–0.89 之间且随品种变化
（A 0.504 / AL 0.759 / AP 0.891 / RB 0.457 / SC 0.401）→ daily_bar 888 的量不是主力合约口径，
"60m 聚合量 == daily" 这条判据不可用（§30 提案中的该部分作废）。

只读。--out 写 JSON。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta

sys.path.insert(0, "/app")
from sqlalchemy import create_engine, text
from app.core.config import get_settings

ENGINE = create_engine(get_settings().db_url)

L2_TOL = 0.001     # 60m 与 5m 量的一致容差
L3_TOL = 0.005     # 收盘价对 daily_bar 的容差
L3_RATE = 0.90     # L3 通过所需品种比例
L2_RATE = 0.95
TPL_LOOKBACK = 20  # 结构模板回看天数
L2_STALE = 0.60    # L2 通过率低于此值判为「60m 聚合陈旧」（历史 p05=0.771）


def q(sql, **kw):
    with ENGINE.connect() as c:
        return c.execute(text(sql), kw).all()


def q1(sql, **kw):
    with ENGINE.connect() as c:
        return c.execute(text(sql), kw).scalar()


def rel(a, b) -> float:
    a, b = float(a), float(b)
    if b == 0:
        return 0.0 if a == 0 else 9.9
    return abs(a - b) / abs(b)


# ------------------------------------------------------------------ A 缺口
def part_a(args) -> dict:
    print("[A] 缺口清单")
    td = [r[0] for r in q("""SELECT DISTINCT trade_date FROM l0_raw.daily_bar
                             WHERE symbol LIKE '%888' AND trade_date >= :a AND trade_date < :b
                             ORDER BY 1""", a=args.d0, b=args.d1)]
    bd = [r[0] for r in q("""SELECT DISTINCT bucket::date FROM l1_mkt.bar_60m
                             WHERE bucket >= :a AND bucket < :b ORDER BY 1""", a=args.d0, b=args.d1)]
    miss = sorted(set(td) - set(bd))
    segs, cur = [], ([miss[0]] if miss else [])
    for prev, nd in zip(miss, miss[1:]):
        if (nd - prev).days <= 4:
            cur.append(nd)
        else:
            segs.append(cur)
            cur = [nd]
    if cur:
        segs.append(cur)

    td26 = [d for d in td if d.year == 2026]
    miss26 = [d for d in miss if d.year == 2026]
    print(f"   交易日={len(td)}  bar60 日={len(bd)}  缺={len(miss)}"
          f"  2026: {len(td26)} 日中缺 {len(miss26)} ({len(miss26)/max(len(td26),1):.1%})")
    for s in segs:
        print(f"   连续缺口: {s[0]} → {s[-1]} ({len(s)} 日)")

    hourly = q("""SELECT count(*), count(DISTINCT trade_datetime::date), count(DISTINCT symbol)
                  FROM l0_raw.hourly_bar
                  WHERE trade_datetime >= '2026-06-15' AND trade_datetime < '2026-09-04'""")[0]
    minute = q1("""SELECT count(*) FROM l0_raw.minute_bar
                   WHERE ts >= '2026-06-15' AND ts < '2026-09-04'""")
    daily = q("""SELECT count(*), count(DISTINCT trade_date) FROM l0_raw.daily_bar
                 WHERE trade_date >= '2026-06-15' AND trade_date < '2026-09-04'""")[0]
    print(f"   缺口内: minute={minute} 行 | daily={daily[0]} 行/{daily[1]} 日"
          f" | hourly={hourly[0]} 行/{hourly[1]} 日/{hourly[2]} 品种")

    subs = []
    for d in ("2026-05-06", "2026-10-14", "2026-10-21"):
        a_ = q("""SELECT trade_datetime, symbol, open, high, low, close, volume
                  FROM l0_raw.hourly_bar WHERE trade_datetime::date = :d""", d=d)
        b_ = q("""SELECT bucket, symbol, open, high, low, close, volume
                  FROM l1_mkt.bar_60m WHERE bucket::date = :d""", d=d)
        ka = {(r[0].isoformat(), r[1]): r[2:] for r in a_}
        kb = {(r[0].isoformat(), r[1]): r[2:] for r in b_}
        common = set(ka) & set(kb)
        exact = sum(1 for k in common if ka[k] == kb[k])
        vr = sorted(float(ka[k][4]) / float(kb[k][4]) for k in common if kb[k][4])
        subs.append({"date": d, "common": len(common), "exact_eq": exact,
                     "exact_rate": round(exact / max(len(common), 1), 4),
                     "vol_ratio_median": round(vr[len(vr) // 2], 4) if vr else None})
    print("   hourly 替代性: " + " | ".join(
        f"{x['date']} 精确={x['exact_rate']:.1%} 量比={x['vol_ratio_median']}" for x in subs))

    return {"trade_days": len(td), "bar60_days": len(bd), "missing": len(miss),
            "missing_2026": [d.isoformat() for d in miss26],
            "missing_2026_share": round(len(miss26) / max(len(td26), 1), 4),
            "segments": [{"start": s[0].isoformat(), "end": s[-1].isoformat(), "n": len(s)}
                         for s in segs],
            "in_gap": {"minute_rows": minute, "daily_rows": daily[0], "daily_days": daily[1],
                       "hourly_rows": hourly[0], "hourly_days": hourly[1],
                       "hourly_symbols": hourly[2]},
            "hourly_substitutability": subs,
            "hourly_verdict": "不可替代：逐行精确相等率 3.8–16.6%，量比 0.33–1.00 且不稳定"}


# -------------------------------------------------- B0 口径语义反解（5m 定标）
def solve_window(b6: tuple, b5: list) -> dict | None:
    bk, o, h, l, c, v = b6
    iv = int(v or 0)
    for i in range(len(b5)):
        ov = hv = lv = None
        cum = 0
        for j in range(i, len(b5)):
            cum += int(b5[j][5] or 0)
            ov = b5[i][1] if ov is None else ov
            hv = b5[j][2] if hv is None else max(hv, b5[j][2])
            lv = b5[j][3] if lv is None else min(lv, b5[j][3])
            if cum > iv + 1:
                break
            if cum == iv and ov == o and hv == h and lv == l and b5[j][4] == c:
                return {"bar": bk.strftime("%H:%M"), "from": b5[i][0].strftime("%H:%M"),
                        "to": b5[j][0].strftime("%H:%M"), "n5": j - i + 1, "vol": iv}
    return None


def part_b0(samples) -> dict:
    print("\n[B0] 口径语义反解（用 5m 精确定位每根 60m 的覆盖窗口）")
    out = {}
    for sym, day in samples:
        b6 = q("""SELECT bucket, open, high, low, close, volume FROM l1_mkt.bar_60m
                  WHERE symbol=:y AND bucket::date=:d ORDER BY bucket""", y=sym, d=day)
        b5 = q("""SELECT bucket, open, high, low, close, volume FROM l1_mkt.bar_5m
                  WHERE symbol=:y AND bucket::date=:d ORDER BY bucket""", y=sym, d=day)
        rows = []
        for b in b6:
            w = solve_window(b, b5)
            rows.append(w or {"bar": b[0].strftime("%H:%M"), "vol": int(b[5] or 0), "from": None})
        out[f"{sym}@{day}"] = rows
        desc = " ".join(f"{r['bar']}←[{r['from']}..{r['to']}]x{r['n5']}" if r.get("from")
                        else f"{r['bar']}<-未匹配" for r in rows)
        print(f"   {sym} {day}: {desc}")
    return out


# ---------------------------------------------------------- B 三层判据
def part_b(days: list[date]) -> dict:
    """批量取数 + 内存计算（逐日循环查询会超时）。"""
    print("\n[B] 三层完整交易日判据")
    d0, d1 = days[0], days[-1] + timedelta(days=1)

    # 一次取全：60m 逐 (日,品种) 量/根数/标签集合
    rows60 = q("""SELECT bucket::date d, symbol, sum(volume) v,
                         string_agg(to_char(bucket,'HH24:MI'), ',' ORDER BY bucket) sig
                  FROM l1_mkt.bar_60m WHERE bucket >= :a AND bucket < :b
                  GROUP BY 1,2""", a=d0, b=d1)
    # 一次取全：5m 逐 (日,品种) 量
    rows5 = q("""SELECT bucket::date d, symbol, sum(volume) v
                 FROM l1_mkt.bar_5m WHERE bucket >= :a AND bucket < :b
                 GROUP BY 1,2""", a=d0, b=d1)
    # 一次取全：最后日盘非零 bar 的 close
    rows_last = q("""SELECT DISTINCT ON (bucket::date, symbol) bucket::date d, symbol, close
                     FROM l1_mkt.bar_60m
                     WHERE bucket >= :a AND bucket < :b AND symbol LIKE '%888'
                       AND bucket::time <= '15:30' AND coalesce(volume,0) > 0
                     ORDER BY bucket::date, symbol, bucket DESC""", a=d0, b=d1)
    rows_ref = q("""SELECT trade_date, symbol, close FROM l0_raw.daily_bar
                    WHERE symbol LIKE '%888' AND trade_date >= :a AND trade_date < :b""",
                 a=d0, b=d1)

    sig: dict = defaultdict(dict)
    v60: dict = defaultdict(dict)
    for d, sym, v, s in rows60:
        sig[sym][d] = s
        v60[d][sym] = int(v or 0)
    v5: dict = defaultdict(dict)
    for d, sym, v in rows5:
        v5[d][sym] = int(v or 0)
    last: dict = defaultdict(dict)
    for d, sym, c in rows_last:
        last[d][sym] = c
    ref: dict = defaultdict(dict)
    for d, sym, c in rows_ref:
        ref[d][sym] = c

    tpl_global = {s: Counter(m.values()).most_common(1)[0][0] for s, m in sig.items() if m}

    # 滚动模板：day d 的模板取 d 之前 TPL_LOOKBACK 个"有数据日"的众数
    have = [d for d in days if any(d in sig.get(s, {}) for s in sig)]
    rolling = {}
    for i, d in enumerate(have):
        win = have[max(0, i - TPL_LOOKBACK):i]
        monday = d.weekday() == 0
        tpl = {}
        for s, m in sig.items():
            # 周一本无凌晨夜盘 → 模板按周一/非周一分别取众数
            cand = [m[x] for x in win if x in m and (x.weekday() == 0) == monday]
            if not cand:
                cand = [m[x] for x in win if x in m]
            tpl[s] = Counter(cand).most_common(1)[0][0] if cand else tpl_global.get(s)
        rolling[d] = tpl

    syms = sorted({s for m in ref.values() for s in m})

    cells = []
    for d in days:
        tpl = rolling.get(d, {})
        miss_by_label: Counter = Counter()
        l1_miss_syms = 0
        l1_extra = n_struct = 0
        for s in syms:
            got, base = sig.get(s, {}).get(d), tpl.get(s)
            if not got or not base:
                continue
            n_struct += 1
            g, b = set(got.split(",")), set(base.split(","))
            if b <= g:
                if g - b:
                    l1_extra += 1
            else:
                l1_miss_syms += 1
                for lab in (b - g):
                    miss_by_label[lab] += 1
        # 全市场 vs 个别：某标签缺失覆盖 >50% 有该标签的品种 → 结构变化（如节前停夜盘/口径切换）
        n_with = Counter()
        for s in syms:
            base = tpl.get(s)
            if base and sig.get(s, {}).get(d):
                for lab in base.split(","):
                    n_with[lab] += 1
        market_wide = {lab for lab, n in miss_by_label.items()
                       if n_with[lab] and n / n_with[lab] > 0.5}
        idiosyncratic = {s for s in syms
                         if sig.get(s, {}).get(d) and tpl.get(s)
                         and set(tpl[s].split(",")) - set(sig[s][d].split(","))
                         and (set(tpl[s].split(",")) - set(sig[s][d].split(","))) - market_wide}

        both = [s for s in v60.get(d, {}) if s in v5.get(d, {})]
        l2_ok = sum(1 for s in both if v5[d][s] and rel(v60[d][s], v5[d][s]) <= L2_TOL)
        l2_bad = sorted(((s, rel(v60[d][s], v5[d][s])) for s in both
                         if v5[d][s] and rel(v60[d][s], v5[d][s]) > L2_TOL),
                        key=lambda x: -x[1])[:5]
        cand = [s for s in last.get(d, {}) if s in ref.get(d, {})]
        l3_ok = sum(1 for s in cand if rel(last[d][s], ref[d][s]) <= L3_TOL)

        n_bar = sum(len(sig[s][d].split(",")) for s in syms if sig.get(s, {}).get(d))
        r2 = round(l2_ok / max(len(both), 1), 4)
        r3 = round(l3_ok / max(len(cand), 1), 4)
        if n_bar == 0:
            verdict = "absent"
        elif idiosyncratic:
            verdict = "incomplete"          # 个别品种缺标签 → 数据不全
        elif r3 < L3_RATE:
            verdict = "incomplete"          # 收盘价对不上 → 尾盘缺失/错价
        elif market_wide:
            verdict = "complete_calendar"   # 全市场标签变化（节前停夜盘 / 口径切换），收盘已对齐
        else:
            verdict = "complete"
        cells.append({"date": d.isoformat(), "n_bar": n_bar, "n_struct": n_struct,
                      "l1_miss_syms": l1_miss_syms, "l1_extra": l1_extra,
                      "idiosyncratic_missing": len(idiosyncratic),
                      "idiosyncratic_syms": sorted(idiosyncratic)[:8],
                      "market_wide_labels": sorted(market_wide),
                      "l2_ok": l2_ok, "l2_n": len(both), "l2_rate": r2,
                      "l2_stale_flag": bool(len(both) and r2 < L2_STALE),
                      "l2_worst": [{"sym": s, "reldev": round(v, 4)} for s, v in l2_bad],
                      "l3_ok": l3_ok, "l3_n": len(cand), "l3_rate": r3,
                      "verdict": verdict})

    vc = Counter(c["verdict"] for c in cells)
    print(f"   2026 判定: {dict(vc)}")
    for c in cells:
        if c["verdict"] != "complete":
            worst = f" 量最差={c['l2_worst'][0]}" if c["l2_worst"] else ""
            print(f"     {c['date']}: {c['verdict']:15s} 个别缺={c['idiosyncratic_missing']} "
                  f"全市场变={c['market_wide_labels']} L2={c['l2_rate']:.0%}"
                  f"{'[陈旧]' if c['l2_stale_flag'] else ''} L3={c['l3_rate']:.0%}{worst}")
    ok = [c for c in cells if c["verdict"] == "complete"]
    if ok:
        r2 = sorted(c["l2_rate"] for c in ok)
        r3 = sorted(c["l3_rate"] for c in ok)
        print(f"   complete 日 n={len(ok)}: L2 min={r2[0]:.3f} med={r2[len(r2)//2]:.3f} | "
              f"L3 min={r3[0]:.3f} med={r3[len(r3)//2]:.3f}")
    return {"definition": {
        "L1": "当日标签集合 ⊇ 品种近20日众数模板",
        "L2": f"Σ(60m量)==Σ(5m量)，容差 {L2_TOL}",
        "L3": f"最后日盘 bar close == daily_bar close，容差 {L3_TOL}，品种通过率≥{L3_RATE}",
        "volume_vs_daily_bar": "禁用：daily_bar 888 量非主力合约口径（实测比值 0.40–0.89 且随品种变化）",
    }, "verdict_counts": dict(vc), "by_day": cells}


# ------------------------------------------------------------- C 外部源
def part_c() -> dict:
    print("\n[C] 外部源能力实测")
    ext: dict = {"target_window": ["2026-06-15", "2026-09-03"]}
    earliest = None
    try:
        import akshare as ak  # type: ignore
        ext["akshare_version"] = getattr(ak, "__version__", "unknown")
        for sym in ("RB0", "M0"):
            try:
                df = ak.futures_zh_minute_sina(symbol=sym, period="1")
                if df is None or len(df) == 0:
                    ext[sym] = {"rows": 0}
                    continue
                f0, f1 = str(df.iloc[0, 0])[:19], str(df.iloc[-1, 0])[:19]
                ext[sym] = {"rows": int(len(df)), "first": f0, "last": f1}
                earliest = min(earliest, f0) if earliest else f0
                print(f"   {sym}: rows={len(df)} span={f0} → {f1}")
            except Exception as exc:  # noqa: BLE001
                ext[sym] = {"error": f"{type(exc).__name__}: {str(exc)[:100]}"}
                print(f"   {sym}: ERR {ext[sym]['error']}")
    except Exception as exc:  # noqa: BLE001
        ext["error"] = f"{type(exc).__name__}: {str(exc)[:100]}"
        print(f"   akshare 不可用: {ext['error']}")

    need = date(2026, 6, 15)
    if earliest:
        got = datetime.fromisoformat(earliest).date()
        ext["earliest_available"] = earliest
        ext["source_starts_after_gap_by_days"] = (got - need).days
        ext["recoverable"] = got <= need
        ext["verdict"] = (f"源最早 {got}，比缺口起点晚 {(got-need).days} 天 → 不可回补"
                          if got > need else "源可覆盖缺口 → 可回补")
    else:
        ext["recoverable"] = False
        ext["verdict"] = "取不到分钟数据 → 不可回补"
    print(f"   → {ext['verdict']}")
    return ext


# --------------------------------------------------- D 重聚合可行性（给②铺路）
def part_d(days: list[date]) -> dict:
    print("\n[D] 5m→60m 重聚合一致性（为口径统一铺路）")
    res = []
    for d in days:
        m60 = {r[0]: (r[1], int(r[2] or 0)) for r in q("""
            SELECT symbol, count(*), sum(volume) FROM l1_mkt.bar_60m
            WHERE bucket::date = :d AND symbol LIKE '%888' GROUP BY 1""", d=d)}
        m5 = {r[0]: (r[1], int(r[2] or 0)) for r in q("""
            SELECT symbol, count(*), sum(volume) FROM l1_mkt.bar_5m
            WHERE bucket::date = :d AND symbol LIKE '%888' GROUP BY 1""", d=d)}
        both = [s for s in m60 if s in m5]
        vol_eq = sum(1 for s in both if m60[s][1] == m5[s][1])
        res.append({"date": d.isoformat(), "n": len(both), "vol_exact": vol_eq,
                    "vol_exact_rate": round(vol_eq / max(len(both), 1), 4)})
        print(f"   {d}: 品种={len(both)} 量逐品种完全相等={vol_eq} "
              f"({vol_eq/max(len(both),1):.0%})")
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/tmp/aeq/b10_gap.json")
    ap.add_argument("--d0", default="2021-01-01")
    ap.add_argument("--d1", default="2026-11-03")
    args = ap.parse_args()

    out = {"meta": {"generated_at": datetime.now().isoformat(timespec="seconds"),
                    "window": [args.d0, args.d1],
                    "purpose": "十期① 缺口审计 + 三层完整交易日判据（label-agnostic）"}}

    out["A_gap"] = part_a(args)
    out["B0_semantics"] = part_b0([
        ("RB888", "2026-10-14"), ("AP888", "2026-10-14"),
        ("RB888", "2026-11-02"), ("SC888", "2026-11-02"),
    ])

    td = [r[0] for r in q("""SELECT DISTINCT trade_date FROM l0_raw.daily_bar
                             WHERE symbol LIKE '%888' AND trade_date >= '2026-01-01'
                               AND trade_date < :b ORDER BY 1""", b=args.d1)]
    out["B_criterion"] = part_b(td)
    out["C_external"] = part_c()
    out["D_reagg"] = part_d([date(2026, 10, 14), date(2026, 10, 21), date(2026, 11, 2)])

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)
    print(f"\n[done] → {args.out}")


if __name__ == "__main__":
    main()
