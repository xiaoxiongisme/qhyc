# -*- coding: utf-8 -*-
"""附录 C · V3「过拟合五关」发布闸门（不可妥协，决策 D3）。

背景（PRD 附录 C §0.3 / §4 / D3）
------------------------------
任何"加过滤器/改规则"类改动发布前必须走完整流程，缺一项不得定级。此前这些纪律
分散在 `robustness.py` 的各函数里，但**没有一道统一的门禁**，也没人能证明
"门禁真的会响"（2026-09-27 `keyline_excl` 教训：参数改了、笔数相同却不报错）。

本脚本把闸门固化成可 CI 调用的命令，任一不过 → 非零退出码，阻断发布。

闸门顺序（严格按附录 C §4）
-------------------------
1. 净值集中度体检（Top5>50% → 样本不支持过滤器类优化，后续结论降级为噪声）
2. 配对归因三重判据：参数平台 / 跨体系同向 / 横截面分半同向
3. 六项走前检验：`robustness.run_six_checks`
4. 两道硬闸门：价格取负对称性、随机入场对照（已含在 run_six_checks 内，此处显式复核）
5. 静默退化拦截：变体 vs 基线 **笔数完全相同且参数不同** → 判「参数未生效」

用法
----
  # ① 单批：只跑集中度 + 六项 + 硬闸门
  python scripts/release_gate.py --trades trades.json

  # ② 完整发布闸门（变体 vs 基线，含静默退化拦截）
  python scripts/release_gate.py --trades variant.json --base base.json \
      --base-params '{}' --variant-params '{"keyline_excl":1}'

  # ③ 配对归因三重判据
  python scripts/release_gate.py --trades trades.json \
      --condition "voka:ge:1.5" --plateau plateau.json --cross-system alt.json

  # ④ 已知退化案例回填：证明闸门真会响（必须 FAIL）
  python scripts/release_gate.py --case silent-degradation
"""
from __future__ import annotations

import argparse
import json
import sys

from app.backtest.robustness import (
    bootstrap_ci,
    concentration,
    paired_attribution,
    profit_factor,
    random_entry_mc,
    run_six_checks,
    silent_degradation,
    trade_pnl,
)

BAR = "=" * 72


def load_trades(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        obj = json.load(f)
    if isinstance(obj, dict):
        obj = obj.get("trades") or obj.get("oos_trades") or []
    return obj


def parse_condition(spec: str):
    """安全的条件解析器：`字段:运算符:阈值`，禁止 eval。

    例：`voka:ge:1.5` → lambda t: float(t.get("voka", 0)) >= 1.5
    """
    ops = {"ge": ">=", "gt": ">", "le": "<=", "lt": "<", "eq": "==", "ne": "!="}
    key, op, raw = spec.split(":", 2)
    if op not in ops:
        raise SystemExit("未知运算符 {0}，可选：{1}".format(op, ",".join(ops)))
    try:
        thr = float(raw)
    except ValueError:
        raise SystemExit("阈值必须是数字：{0}".format(raw))

    def _fn(t: dict) -> bool:
        try:
            v = float(t.get(key, 0) or 0)
        except (TypeError, ValueError):
            return False
        o = ops[op]
        if o == ">=":
            return v >= thr
        if o == ">":
            return v > thr
        if o == "<=":
            return v <= thr
        if o == "<":
            return v < thr
        if o == "==":
            return v == thr
        return v != thr
    return _fn


def check_concentration(trades, cost_bp):
    c = concentration(trades, cost_bp)
    ok = bool(c.get("supports_filter_opt"))
    print("  集中度 Top5={0} Top20={1} → {2}".format(
        c.get("top5"), c.get("top20"), c.get("note")))
    return ok, c


def check_cross_section(trades, cost_bp):
    """横截面分半：按 symbol 哈希奇偶两半必须同号。"""
    buckets: dict[int, float] = {0: 0.0, 1: 0.0}
    for t in trades:
        sym = str(t.get("sym") or t.get("symbol") or "")
        k = sum(ord(ch) for ch in sym) % 2
        buckets[k] += trade_pnl(t, cost_bp)
    same = (buckets[0] > 0 and buckets[1] > 0) or (buckets[0] < 0 and buckets[1] < 0)
    print("  横截面分半：奇={0:.2f} 偶={1:.2f} → {2}".format(
        buckets[0], buckets[1], "同向 OK" if same else "不同向 FAIL"))
    return same, buckets


def check_plateau(path):
    """参数平台：最优不应是孤立尖峰，邻域净利须同号为正。

    输入 JSON：[[{...params}, net], ...]，按输入顺序视为参数轴。
    """
    rows = json.load(open(path, encoding="utf-8"))
    nets = [float(r[1]) for r in rows]
    best = max(range(len(nets)), key=lambda i: nets[i])
    neighbours = [nets[i] for i in (best - 1, best + 1) if 0 <= i < len(nets)]
    ok = all(n > 0 for n in neighbours) if neighbours else False
    print("  参数平台：最优 idx={0} net={1:.2f}，邻域={2} → {3}".format(
        best, nets[best], ["%.2f" % n for n in neighbours],
        "平台 OK" if ok else "孤立尖峰/邻域非正 FAIL"))
    return ok, {"nets": nets, "best": best}


def check_cross_system(trades_alt, cost_bp):
    alt = sum(trade_pnl(t, cost_bp) for t in trades_alt)
    print("  跨体系对照净利={0:.2f} → {1}".format(alt, "OK" if alt > 0 else "FAIL"))
    return alt > 0, alt


def run_gate(args) -> int:
    cost = args.cost_bp
    trades = load_trades(args.trades)
    print(BAR)
    print("附录 C · V3 发布闸门")
    print(BAR)
    print("成交样本：{0} 笔，cost_bp={1}".format(len(trades), cost))

    gates: list[tuple[str, bool]] = []

    # ---- 第 1 关：集中度体检 ----
    print("\n[第 1 关] 净值集中度体检")
    ok1, _ = check_concentration(trades, cost)
    gates.append(("集中度体检", ok1))
    limitation = None
    if not ok1:
        limitation = ("样本不支持过滤器类优化（Top5>50%）：后续所有过滤器类结论 "
                      "降级为噪声，只能验证正交类（离场/风控）")

    # ---- 第 2 关：配对归因三重判据 ----
    if args.condition:
        print("\n[第 2 关] 配对归因三重判据")
        cond = parse_condition(args.condition)
        pa = paired_attribution(trades, cond, cost)
        print("  kept  : n={0} net={1:.2f} pf={2}".format(
            pa["kept"]["n"], pa["kept"]["net"], pa["kept"]["pf"]))
        print("  dropped: n={0} net={1:.2f} pf={2}".format(
            pa["dropped"]["n"], pa["dropped"]["net"], pa["dropped"]["pf"]))
        print("  判定  : {0}".format(pa["verdict"]))
        gates.append(("配对归因接受/明示拒绝", True))  # 负结果本身有效（记录拒绝理由）

        ok2a, _ = check_cross_section(trades, cost)
        gates.append(("配对归因·横截面分半同向", ok2a))
        if args.plateau:
            ok2b, _ = check_plateau(args.plateau)
            gates.append(("配对归因·参数平台", ok2b))
        if args.cross_system:
            alt = load_trades(args.cross_system)
            ok2c, _ = check_cross_system(alt, cost)
            gates.append(("配对归因·跨体系同向", ok2c))
    else:
        print("\n[第 2 关] 跳过（未给 --condition，本改动非过滤器类）")

    # ---- 第 3 / 4 关：六项走前检验 + 两道硬闸门 ----
    print("\n[第 3 关] 六项走前检验")
    six = run_six_checks(trades, cost_bp=cost)
    print("  net={0} pf={1} mdd={2}".format(six["net"], six["pf"], six["mdd"]))
    for k, v in six["gates"].items():
        print("   {0} {1}".format("PASS" if v else "FAIL", k))
    gates.append(("六项走前检验 pass_all", bool(six["pass_all"])))

    print("\n[第 4 关] 两道硬闸门（显式复核）")
    sym_ok = six["symmetry"]["pass_"]
    mc_p = six["mc"]["p"]
    mc_ok = mc_p is not None and mc_p <= 0.05
    print("  价格取负对称一致 : {0} (不一致 {1} 笔)".format(sym_ok, six["symmetry"]["mismatched"]))
    print("  随机入场 MC p   : {0} → {1}".format(mc_p, "PASS" if mc_ok else "FAIL"))
    gates.append(("硬闸门·价格对称", bool(sym_ok)))
    gates.append(("硬闸门·随机入场", bool(mc_ok)))

    # ---- 第 5 关：静默退化拦截 ----
    if args.base:
        print("\n[第 5 关] 静默退化拦截（变体 vs 基线）")
        base = load_trades(args.base)
        bp = json.loads(args.base_params or "{}")
        vp = json.loads(args.variant_params or "{}")
        sd = silent_degradation(base, trades, bp, vp)
        print("  基线笔数={0} 变体笔数={1} 参数不同={2}".format(
            sd["n_base"], sd["n_variant"], sd["params_differ"]))
        print("  结论  : {0}".format(sd["verdict"]))
        gates.append(("非静默退化", not sd["silent"]))
    else:
        print("\n[第 5 关] 跳过（未给 --base，无变体对照）")

    print("\n" + BAR)
    print("闸门汇总")
    print(BAR)
    failed = []
    for name, ok in gates:
        print("  {0} {1}".format("PASS" if ok else "FAIL", name))
        if not ok:
            failed.append(name)
    if limitation:
        print("\n⚠ 结论上限声明：{0}".format(limitation))
    ok_all = not failed
    print("\n发布裁定：{0}".format("✅ 通过（可发布）" if ok_all else
                             "❌ 阻断（failed: {0}）".format(",".join(failed))))
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            json.dump({"gates": gates, "failed": failed, "pass": ok_all,
                       "six": six, "limitation": limitation},
                      f, ensure_ascii=False, indent=2, default=str)
        print("已落盘：{0}".format(args.json_out))
    return 0 if (ok_all or args.allow_fail) else 1


def _case_silent_degradation(args) -> int:
    """已知退化案例回填（2026-09-27 keyline_excl 事件）：证明闸门会真的阻断。

    构造：基线 30 笔成交；变体**逐位等于基线**（笔数相同）、但参数字典不同 →
    正确闸门必须判 silent=True 并阻断；若它放行，说明门禁没接线（或其测试是摆设）。
    """
    trades = []
    for i in range(30):
        pnl_dir = 1 if i % 2 else -1
        trades.append({
            "sym": "FG888", "dir": pnl_dir, "risk": 20.0, "mult": 1.0,
            "ep": 3000.0, "xp": 3000.0 + pnl_dir * 20.0 * (1.0 if i % 2 else -0.8),
            "edt": "2026-0{0}-{1:02d}".format(1 + (i % 9), 1 + (i % 27)) if i < 27 else "2026-09-30",
            "xdt": "2026-09-30", "R": (1.0 if i % 2 else -0.8),
        })
    sd = silent_degradation(trades, list(trades), {}, {"keyline_excl": 1})
    print(BAR)
    print("案例回填：keyline_excl 静默退化")
    print(BAR)
    print("基线笔数={0} 变体笔数={1} 相同={2} 参数不同={3}".format(
        sd["n_base"], sd["n_variant"], sd["identical_count"], sd["params_differ"]))
    print("silent={0}\n结论：{1}".format(sd["silent"], sd["verdict"]))
    if not sd["silent"]:
        print("\n❌ 门禁失效：执行笔数完全相同却被放行，说明闸门未接线！")
        return 1
    print("\n✅ 闸门按预期阻断（此处故意返回 FAIL=1，表示「已拦截」）")
    return 0 if args.allow_fail else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="附录 C V3 发布闸门")
    ap.add_argument("--trades", help="成交 JSON（list 或 {trades:[...]}）")
    ap.add_argument("--base", help="基线成交 JSON（启用静默退化拦截）")
    ap.add_argument("--base-params", default="{}")
    ap.add_argument("--variant-params", default="{}")
    ap.add_argument("--condition", help="配对归因条件，形如 字段:运算符:阈值")
    ap.add_argument("--plateau", help="参数平台 JSON：[[{params}, net], ...]")
    ap.add_argument("--cross-system", help="跨体系对照成交 JSON")
    ap.add_argument("--cost-bp", type=float, default=5.0)
    ap.add_argument("--json", dest="json_out")
    ap.add_argument("--case", choices=["silent-degradation"])
    ap.add_argument("--allow-fail", action="store_true",
                    help="即使闸门 FAIL 也返回 0（用于演示/留档）")
    a = ap.parse_args()

    if a.case == "silent-degradation":
        return _case_silent_degradation(a)
    if not a.trades:
        ap.error("需 --trades，或用 --case silent-degradation")
    return run_gate(a)


if __name__ == "__main__":
    sys.exit(main())
