# -*- coding: utf-8 -*-
"""Walk-forward 走前回测 + robustness 六项检查（整改优先级 P1 / PRD §8.5-§8.7）。

为什么合到一个脚本
------------------
PRD §8.5 要求「OOS 判据：报告 OOS 衰减比」；§8.7 要求「统计加固包默认输出」。
整改 P1 把两件事合并验收：先用 walk-forward 产出**真·样本外成交**，再把这些成交
喂给 `app.backtest.robustness.run_six_checks`（此前融合成交缺 R 口径，跑不了）。

用法（在本地容器内；注意必须带 PYTHONPATH=/app）：
    docker exec -w /app -e PYTHONPATH=/app qhyc-api python scripts/run_walkforward.py \
        --symbols FG888,RB888,CU888 --start 2022-01-01 --end 2026-09-01 \
        --train-days 720 --test-days 60 --step-days 60 --json runtime/wf_result.json

    # 开启滚动窗内参数重估（成本 ×网格数）
    ... --reestimate
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date

from app.backtest.fusion_backtest import to_robustness_trades
from app.backtest.robustness import run_six_checks
from app.backtest.walkforward import WalkForwardParams, run_walk_forward
from app.core.db import session_scope

BAR = "=" * 72


def _d(s):
    return date.fromisoformat(s) if s else None


def _print_walkforward(res):
    print("")
    print(BAR)
    print("Walk-forward 结果")
    print(BAR)
    print("区间            : {0}".format(res.get("range")))
    print("品种数          : {0}".format(len(res.get("symbols") or [])))
    print("有效窗数        : {0}".format(res.get("n_windows")))
    print("IS  净利(合计)  : {0}".format(res.get("is_net")))
    print("OOS 净利(合计)  : {0}".format(res.get("oos_net")))
    print("OOS 成交笔数    : {0}".format(res.get("n_oos_trades")))
    print("衰减比 OOS/IS   : {0}".format(res.get("decay")))
    print("判定            : {0}".format(res.get("verdict")))

    wins = res.get("windows") or []
    if wins:
        print("\n逐窗口：")
        head = "{0:<24}{1:>6}{2:>14}{3:>14}{4:>9}  选参".format(
            "测试窗", "n", "IS净利", "OOS净利", "衰减")
        print(head)
        for w in wins:
            left = "{0}~{1}".format(str(w["test"][0])[:10], str(w["test"][1])[:10])
            dec_v = w.get("decay", float("nan"))
            print("{0:<24}{1:>6}{2:>14.4f}{3:>14.4f}{4:>9.2f}  {5}".format(
                left, w["n_oos"], w["is_net"], w["oos_net"], dec_v, w["chosen"]))


def _print_six(res, cost_bp):
    robust = to_robustness_trades(res.get("oos_trades") or [])
    print("")
    print(BAR)
    print("robustness 六项检查（对象：walk-forward OOS 样本外成交）")
    print(BAR)
    n_total = res.get("n_oos_trades") or 0
    print("可检验成交数：{0} / OOS 总数 {1}".format(len(robust), n_total))
    if len(robust) < 10:
        print("[warn] 样本不足（<10 笔），六项检查结论不可用于发布裁定")

    six = run_six_checks(robust, cost_bp=cost_bp)
    conc = six["concentration"]
    sh = six["split_half"]
    by = six["by_year"]
    cs = six["cost"]
    bs = six["bootstrap"]
    sy = six["symmetry"]

    print("成交笔数 n     : {0}".format(six["n_trades"]))
    print("净¥ / PF / MDD : {0} / {1} / {2}".format(six["net"], six["pf"], six["mdd"]))
    print("集中度 Top5    : {0}  ({1})".format(conc.get("top5"), conc.get("note")))
    print("分半           : 前半 {0:.2f} / 后半 {1:.2f}  双正={2}".format(
        sh["first"], sh["second"], sh["both_positive"]))
    print("逐年正年占比   : {0:.2%}  {1}".format(by["positive_ratio"], by["years"]))
    print("成本敏感性     : {0}  全正={1}".format(cs["net_by_bp"], cs["all_positive"]))
    print("随机入场 MC p  : {0}".format(six["mc"]["p"]))
    print("自助 CI        : P(<=0)={0}  [{1}, {2}]".format(
        bs["p_le_0"], bs.get("ci_low"), bs.get("ci_high")))
    print("价格对称一致   : {0} (不一致 {1} 笔)".format(sy["pass_"], sy["mismatched"]))

    print("\n发布闸门：")
    for k, v in six["gates"].items():
        print("  {0} {1}".format("PASS" if v else "FAIL", k))
    print("\npass_all = {0}   failed = {1}".format(six["pass_all"], six["failed"]))
    return six


def main():
    ap = argparse.ArgumentParser(description="walk-forward 回测 + 六项稳健性检查")
    ap.add_argument("--symbols", help="逗号分隔；缺省=main_contracts 全部")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--train-days", type=int, default=720)
    ap.add_argument("--test-days", type=int, default=60)
    ap.add_argument("--step-days", type=int, default=60)
    ap.add_argument("--anchored", action="store_true", help="锚定式走前（训练窗起点固定）")
    ap.add_argument("--reestimate", action="store_true", help="训练窗内重估参数")
    ap.add_argument("--min-oos", type=int, default=5, dest="min_oos")
    ap.add_argument("--warmup-days", type=int, default=150)
    ap.add_argument("--cost-bp", type=float, default=1.3)
    ap.add_argument("--json", dest="json_path", default=None)
    ap.add_argument("--no-checks", action="store_true", help="只跑 walk-forward，跳过六项")
    a = ap.parse_args()

    symbols = None
    if a.symbols:
        symbols = [s.strip() for s in a.symbols.split(",") if s.strip()]

    p = WalkForwardParams(
        train_days=a.train_days, test_days=a.test_days, step_days=a.step_days,
        anchored=a.anchored, reestimate=a.reestimate,
        min_oos_trades=a.min_oos, warmup_days=a.warmup_days,
    )

    with session_scope() as s:
        res = run_walk_forward(s, symbols=symbols, params=p,
                               start=_d(a.start), end=_d(a.end), verbose=True)

    _print_walkforward(res)

    payload = dict((k, v) for k, v in res.items() if k != "oos_trades")
    if not a.no_checks:
        six = _print_six(res, a.cost_bp)
        payload["robustness"] = six

    if a.json_path:
        d = os.path.dirname(a.json_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.json_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        print("\n已落盘：{0}".format(a.json_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
