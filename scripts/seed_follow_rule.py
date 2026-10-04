# -*- coding: utf-8 -*-
"""丙/丁方案：物化 dim_main_contract_inferred + 播种 dim_variety.continuous_follow_rule。

流程
----
1) 对每个活跃且非存档品种，取最近窗口的 888 参考价（hourly_bar/daily_bar），
   与 contract_daily 同日该品种全部合约收盘价做价格匹配，反推 888 当日实际所跟合约，
   写入 dim_main_contract_inferred（复用 infer_main_contract 的匹配逻辑）。
2) 按「窗口内最长连续跟踪 + 近月排名 + 与 main_contract_map 一致性」判定 follow_rule：
     - 888 在窗口内稳定跟踪【最近月】合约 → NEAR_MONTH
     - 跟踪【次近月】 → NEXT_MONTH
     - 与 main_contract_map.underlying 一致 → TRUE_MAIN
     - 固定某一月 → FIXED_MONTH
     - 无法判定（单合约无换月 / 不匹配上述任一）→ CUSTOM（交人工，绝不猜 NEAR/NEXT）
   约定（丙/丁 R6）：**绝不静默默认**；判定不确定者写 CUSTOM，未取到数据者留 NULL
   （执行层遇 NULL 必须 ERROR，G1 fail-loud）。

依赖：contract_daily 对品种的全历史完整度。本地库仅含部分品种近 3 月数据，
故本脚本在本地只能标注其中可判定的子集；完整标注须在有全量 contract_daily 的
云端库运行。

用法
----
  python scripts/seed_follow_rule.py                 # 写库 + 打印摘要
  python scripts/seed_follow_rule.py --dry-run       # 只打印，不写
  python scripts/seed_follow_rule.py --days 90       # 匹配窗口天数（默认 60）
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import text

from app.core.db import session_scope
from infer_main_contract import MATCH_TOL, ensure_table, latest_dates, ref_888  # noqa: E402


def _parse_yymm(symbol: str) -> int | None:
    """从合约码提取 YYMM（末尾 3~4 位数字），如 rb2611→2611, i2609→2609。"""
    digits = [c for c in symbol if c.isdigit()]
    if not digits:
        return None
    s = "".join(digits)
    # 取末尾 4 位；若不足 4 位取全部
    return int(s[-4:]) if len(s) >= 4 else int(s)


def infer_and_collect(s, vc: str, days: int, tol: float):
    """返回该品种的 (inferred_rows, agree_rate, all_yymm_set)。"""
    rows = []
    agrees = 0
    total = 0
    yymm_set = set()
    dates = latest_dates(s, [vc], days).get(vc) or []
    for d in dates:
        ref, method = ref_888(s, vc, d)
        if not ref:
            continue
        cands = s.execute(text(
            "SELECT symbol, close FROM contract_daily "
            "WHERE upper(left(symbol, :n)) = :v AND trade_date = :d "
            "AND close IS NOT NULL AND close > 0"),
            {"n": len(vc), "v": vc, "d": d}).fetchall()
        if not cands:
            continue
        best, bestd = None, None
        for sym, close in cands:
            ym = _parse_yymm(sym)
            if ym is not None:
                yymm_set.add(ym)
            diff = abs(ref - float(close)) / float(close)
            if bestd is None or diff < bestd:
                best, bestd = sym, diff
        mapped = s.execute(text(
            "SELECT underlying FROM main_contract_map "
            "WHERE upper(product)=:v AND trade_date=:d"),
            {"v": vc, "d": d}).scalar()
        if bestd is not None and bestd <= tol:
            agree = (mapped is not None and str(mapped).upper() == str(best).upper())
            if agree:
                agrees += 1
            total += 1
            rows.append((vc, d, best, ref, bestd, len(cands), method, mapped, agree))
    agree_rate = (agrees / total) if total else None
    return rows, agree_rate, yymm_set


def classify(vc: str, rows, agree_rate, yymm_set, main_underlying, latest_yymm=None):
    """返回 (rule, note)。无法判定→CUSTOM；无数据→(None, ...)。

    latest_yymm：窗口内最晚交易日所在年月；用于剔除已进入/过了交割月的合约，
    使「近月」= 当前仍可交易的近月（修复 EG/RB 因计入退市合约而误判的 bug）。
    """
    if not rows:
        return None, "无匹配数据（contract_daily 缺失或 888 无价）"
    inferred = [r[2] for r in rows]
    mode_sym = Counter(inferred).most_common(1)[0][0]
    distinct = set(inferred)
    mode_yymm = _parse_yymm(mode_sym)

    # 一致性高 → 直接 TRUE_MAIN
    if agree_rate is not None and agree_rate >= 0.8:
        return "TRUE_MAIN", f"inferred 与 underlying 一致率 {agree_rate:.0%}（{mode_sym}）"

    if len(distinct) == 1:
        # 窗口内未观测到换月，单合约无法区分近月/主力 → 交人工
        if agree_rate is not None and agree_rate >= 0.5:
            return "TRUE_MAIN", f"单合约无换月且一致率 {agree_rate:.0%}（{mode_sym}）"
        return "CUSTOM", f"单合约无换月且一致率 {agree_rate if agree_rate is not None else 'NA'}（{mode_sym}），无法区分近月/主力"

    if mode_yymm is None:
        return "CUSTOM", f"无法解析月份排名（mode={mode_sym}）"
    # 仍可交易的合约（交割月 > 窗口最晚月）用于近月排名
    tradable = sorted(y for y in yymm_set
                     if y is not None and (latest_yymm is None or y > latest_yymm))
    if not tradable:
        return "CUSTOM", f"无可交易合约用于排名（mode={mode_sym}）"
    # 众数合约若已退市（如国庆休市 + 2609 刚交割，2610 尚未在数据中确立主导），
    # 回退到「最近一个仍可交易的推断合约」作为有效主力（通常即当前主力 2701 或次月）。
    eff_sym, eff_yymm = mode_sym, mode_yymm
    if mode_yymm not in set(tradable):
        recent = None
        for sym in reversed(inferred):
            ym = _parse_yymm(sym)
            if ym is not None and ym in set(tradable):
                recent = (sym, ym)
                break
        if recent is None:
            return "CUSTOM", f"最长跟踪 {mode_sym} 已退市（窗口最晚月 {latest_yymm}）且无近期可交易合约，需人工"
        eff_sym, eff_yymm = recent
    rank = tradable.index(eff_yymm)
    if rank == 0:
        return "NEAR_MONTH", f"最长跟踪=最近月（{eff_sym}，排名{rank}）"
    if rank == 1:
        return "NEXT_MONTH", f"最长跟踪=次近月（{eff_sym}，排名{rank}）"
    if main_underlying and str(main_underlying).upper() == str(eff_sym).upper():
        return "TRUE_MAIN", f"与 underlying({main_underlying}) 一致（{eff_sym}）"
    # 特定固定月
    if len(tradable) == 1:
        return "FIXED_MONTH", f"固定月（{eff_sym}）"
    return "CUSTOM", f"最长跟踪 {eff_sym}（排名{rank}）不匹配近/次/主力，需人工"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--tol", type=float, default=MATCH_TOL)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true",
                   help="覆盖已有人工标注的 continuous_follow_rule（默认保留人工标注）")
    ap.add_argument("--only", default=None, help="逗号分隔仅处理这些品种（调试用）")
    a = ap.parse_args()

    with session_scope() as s:
        if not a.dry_run:
            ensure_table(s)
        varieties = [r[0] for r in s.execute(text(
            "SELECT variety_code FROM dim_variety "
            "WHERE is_active AND NOT is_archive_only "
            "ORDER BY variety_code")).fetchall()]
        if a.only:
            varieties = [v.strip().upper() for v in a.only.split(",") if v.strip()]

        summary = []
        for vc in varieties:
            main_und = s.execute(text(
                "SELECT underlying FROM main_contract_map "
                "WHERE upper(product)=:v ORDER BY trade_date DESC LIMIT 1"),
                {"v": vc}).scalar()
            rows, agree_rate, yymm_set = infer_and_collect(s, vc, a.days, a.tol)
            if not a.dry_run and rows:
                for r in rows:
                    s.execute(text(
                        "INSERT INTO dim_main_contract_inferred "
                        "(variety_code, trade_date, inferred_symbol, ref_close, "
                        "match_diff, n_candidates, method, mapped_symbol, agrees_with_map) "
                        "VALUES (:v,:d,:i,:r,:df,:n,:m,:mp,:ag) "
                        "ON CONFLICT (variety_code, trade_date) DO UPDATE SET "
                        "inferred_symbol=EXCLUDED.inferred_symbol, "
                        "ref_close=EXCLUDED.ref_close, match_diff=EXCLUDED.match_diff, "
                        "n_candidates=EXCLUDED.n_candidates, method=EXCLUDED.method, "
                        "mapped_symbol=EXCLUDED.mapped_symbol, "
                        "agrees_with_map=EXCLUDED.agrees_with_map"),
                        {"v": r[0], "d": r[1], "i": r[2], "r": r[3], "df": r[4],
                         "n": r[5], "m": r[6], "mp": r[7], "ag": r[8]})
            latest_d = max((r[1] for r in rows), default=None)
            latest_yymm = (latest_d.year % 100 * 100 + latest_d.month) if latest_d else None
            rule, note = classify(vc, rows, agree_rate, yymm_set, main_und, latest_yymm)
            cur_rule = s.execute(text(
                "SELECT continuous_follow_rule FROM dim_variety WHERE variety_code=:v"),
                {"v": vc}).scalar()
            if rule:
                if cur_rule is not None and not a.force:
                    # 人工已标注优先：自动推断仅为初筛（R6），绝不覆盖人工结论
                    summary.append((vc, len(rows), f"保留({cur_rule})",
                                    "人工已标注，未覆盖（--force 可覆盖）"))
                elif not a.dry_run:
                    s.execute(text(
                        "UPDATE dim_variety SET continuous_follow_rule=:r, updated_at=now() "
                        "WHERE variety_code=:v"), {"r": rule, "v": vc})
                    summary.append((vc, len(rows), rule, note))
                else:
                    summary.append((vc, len(rows), rule, note))
            else:
                summary.append((vc, len(rows), "NULL", note))

        print(f"\n[summary] 处理 {len(summary)} 个品种（days={a.days}, "
              f"{'DRY-RUN' if a.dry_run else '已写库'}）")
        from collections import Counter as _C
        rc = _C(x[2] for x in summary)
        print("  follow_rule 分布:", dict(rc))
        for vc, n, rule, note in summary:
            if rule != "NULL":
                print(f"  {vc:5} n={n:3} -> {rule:12} {note}")


if __name__ == "__main__":
    raise SystemExit(main())
