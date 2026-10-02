# -*- coding: utf-8 -*-
"""prediction_result 精度验收（整改优先级 P2 · PRD §5）。

测试报告记载 prediction_result 仅 2,989 行、"精度未做验收评估"。本脚本做两件事：

1. **精度评估**（--evaluate-only，便宜）：把已有预测与**实际次日收益**对齐，
   输出方向准确率 / 逐模型准确率 / MAE / 区间命中率 / 分年表现。
   ⚠ 口径纪律：只统计"已实现"的样本（target_date 之后确实有行情数据），
   否则等于用未实现的未来做评估。

2. **扩样本**（--expand-dates N，慢）：对最近 N 个交易日回放 `predict_symbols`，
   把样本量做大后再评估（模型需重训，耗时较长，默认关闭）。

用法：
    docker exec -w /app -e PYTHONPATH=/app qhyc-api \
        python scripts/evaluate_prediction.py --evaluate-only --json runtime/pred_eval.json
    ... --expand-dates 20      # 追加扩样本
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import timedelta

from sqlalchemy import text

from app.core.db import session_scope

BAR = "=" * 72


def _fetch_eval_rows(s):
    """把预测与次日/目标日实际收益对齐（close 口径）。"""
    sql = text("""
        WITH pd AS (
            SELECT run_id, symbol, target_date, model_set, direction,
                   direction_prob, ret_point, ret_low, ret_high, confidence
            FROM prediction_result
        ),
        base AS (
            SELECT symbol, trade_date,
                   close,
                   lead(close) OVER (PARTITION BY symbol ORDER BY trade_date) AS next_close
            FROM daily_bar
        )
        SELECT pd.symbol,
               pd.target_date,
               pd.model_set,
               pd.direction,
               pd.direction_prob,
               pd.ret_point,
               pd.ret_low,
               pd.ret_high,
               b.close           AS base_close,
               b.next_close      AS next_close,
               CASE WHEN b.close > 0
                    THEN (b.next_close - b.close) / b.close END AS realized_ret
        FROM pd
        JOIN base b
          ON b.symbol = pd.symbol AND b.trade_date = pd.target_date
        WHERE b.next_close IS NOT NULL
    """)
    return [dict(r) for r in s.execute(sql).mappings().all()]


def _metrics(rows):
    if not rows:
        return {"n": 0}
    n = len(rows)
    hit = sum(1 for r in rows
              if r["direction"] and r["realized_ret"] is not None
              and ((r["direction"] == "up" and r["realized_ret"] > 0)
                   or (r["direction"] == "down" and r["realized_ret"] < 0)))
    dir_rows = [r for r in rows if r["direction"] in ("up", "down")]
    n_dir = len(dir_rows)
    mae = None
    vals = [abs((r["ret_point"] or 0) - (r["realized_ret"] or 0))
            for r in rows if r["ret_point"] is not None and r["realized_ret"] is not None]
    if vals:
        mae = sum(vals) / len(vals)
    in_range = sum(1 for r in rows
                   if r["ret_low"] is not None and r["ret_high"] is not None
                   and r["realized_ret"] is not None
                   and r["ret_low"] <= r["realized_ret"] <= r["ret_high"])
    n_range = sum(1 for r in rows
                  if r["ret_low"] is not None and r["ret_high"] is not None
                  and r["realized_ret"] is not None)

    by_model: dict[str, list[int]] = {}
    for r in rows:
        if r["direction"] not in ("up", "down") or r["realized_ret"] is None:
            continue
        m = r["model_set"] or "unknown"
        ok = ((r["direction"] == "up" and r["realized_ret"] > 0)
              or (r["direction"] == "down" and r["realized_ret"] < 0))
        by_model.setdefault(m, [0, 0])
        by_model[m][0] += 1
        by_model[m][1] += 1 if ok else 0

    by_year: dict[str, list[int]] = {}
    for r in rows:
        if r["direction"] not in ("up", "down") or r["realized_ret"] is None:
            continue
        y = str(r["target_date"])[:4]
        ok = ((r["direction"] == "up" and r["realized_ret"] > 0)
              or (r["direction"] == "down" and r["realized_ret"] < 0))
        by_year.setdefault(y, [0, 0])
        by_year[y][0] += 1
        by_year[y][1] += 1 if ok else 0

    return {
        "n": n, "n_direction": n_dir, "direction_hit": hit,
        "direction_acc": round(hit / n_dir, 4) if n_dir else None,
        "mae_ret": round(mae, 6) if mae is not None else None,
        "interval_hit": in_range, "n_interval": n_range,
        "interval_hit_rate": round(in_range / n_range, 4) if n_range else None,
        "by_model": {k: {"n": v[0], "acc": round(v[1] / v[0], 4)}
                     for k, v in by_model.items()},
        "by_year": {k: {"n": v[0], "acc": round(v[1] / v[0], 4)}
                    for k, v in sorted(by_year.items())},
    }


def _expand(s, dates: int) -> int:
    """对最近 N 个交易日回放预测，扩充样本（慢）。"""
    from app.engine.service import predict_symbols

    ds = [d[0] for d in s.execute(text(
        "SELECT DISTINCT trade_date FROM daily_bar "
        "ORDER BY trade_date DESC LIMIT :n"
    ), {"n": dates}).all()]
    total = 0
    for d in sorted(ds):
        try:
            out = predict_symbols(s, as_of_date=d)
            ok = sum(1 for r in out if "error" not in r)
            total += ok
            print("  {0}: +{1}".format(d, ok), flush=True)
        except Exception as e:
            print("  {0}: 失败 {1}".format(d, str(e)[:120]), flush=True)
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evaluate-only", action="store_true")
    ap.add_argument("--expand-dates", type=int, default=0)
    ap.add_argument("--json", dest="json_path")
    a = ap.parse_args()

    with session_scope() as s:
        total = int(s.execute(text("SELECT count(*) FROM prediction_result")).scalar() or 0)
        if a.expand_dates > 0:
            print("扩样本：回放最近 {0} 个交易日…".format(a.expand_dates))
            added = _expand(s, a.expand_dates)
            print("新增预测行：{0}".format(added))
        rows = _fetch_eval_rows(s)

    print(BAR)
    print("prediction_result 精度验收")
    print(BAR)
    print("库内预测总行数：{0}".format(total))
    print("可对齐已实现收益的样本：{0}".format(len(rows)))
    m = _metrics(rows)
    if not m.get("n"):
        print("\n无可评估样本（预测目标日之后尚无行情数据）。")
        return 0
    print("方向准确率     : {0}  ({1}/{2})".format(
        m["direction_acc"], m["direction_hit"], m["n_direction"]))
    print("ret_point MAE  : {0}".format(m["mae_ret"]))
    print("区间命中率     : {0}  ({1}/{2})".format(
        m["interval_hit_rate"], m["interval_hit"], m["n_interval"]))
    print("\n逐模型：")
    for k, v in m["by_model"].items():
        print("  {0:<28} n={1:<6} acc={2}".format(k, v["n"], v["acc"]))
    print("\n分年：")
    for k, v in m["by_year"].items():
        print("  {0}  n={1:<6} acc={2}".format(k, v["n"], v["acc"]))

    verdict = "样本量 {0} 仍偏小（PRD 未给验收阈值，建议 ≥5000 后再做采纳裁定）".format(
        m["n"]) if m["n"] < 5000 else "样本量足够，可进入采纳裁定流程"
    print("\n判定：{0}".format(verdict))

    if a.json_path:
        d = os.path.dirname(a.json_path)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(a.json_path, "w", encoding="utf-8") as f:
            json.dump({"metrics": m, "verdict": verdict, "total": total},
                      f, ensure_ascii=False, indent=2, default=str)
        print("已落盘：{0}".format(a.json_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
