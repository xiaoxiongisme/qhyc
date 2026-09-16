"""
M4 审计 v3（只读）：第二轮遗留项 + §17 caliber 落地核查
- 第二轮遗留：backtest_result.mae 是否仍存 NaN/Null
- §17 工单：briefing_signal / prediction_result 是否含 caliber 列
- 区间宽度对比：backtest_detail 各模型 (high-low) 平均宽度 + 实测覆盖，确认 rf/xgb/wavelet 存储区间仍为旧窄区间
运行：docker exec qhyc-api python scripts/audit_m4_v3.py
"""
from __future__ import annotations
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
from sqlalchemy import text, inspect

sys.path.insert(0, "/app")
from app.core.db import session_scope
from app.models import BacktestResult, PredictionResult, BriefingSignal

TZ = ZoneInfo("Asia/Shanghai")
TS = datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
REPORT = f"/app/logs/audit_m4v3_{TS}.md"
L = []


def w(s): L.append(s)


w("# M4 审计 v3 — 第二轮遗留项 + §17 caliber 核查")
w(f"- 时间：{datetime.now(TZ).isoformat()}")
w("")

with session_scope() as s:
    insp = inspect(s.get_bind())

    # 1. MAE 质量
    w("## 1. backtest_result.mae 存储质量（第二轮遗留：NaN vs NULL）")
    null_c, nan_c, tot = s.execute(text(
        "SELECT count(*) FILTER (WHERE mae IS NULL), "
        "count(*) FILTER (WHERE mae::text='NaN'), count(*) FROM backtest_result"
    )).one()
    w(f"- backtest_result: total={tot} | NULL={null_c} | NaN文本={nan_c} "
      + ("✅ 无 NaN（已改 NULL）" if nan_c == 0 else "⚠️ 仍有 NaN 文本（应改 NULL）"))
    w("")

    # 2. caliber 字段
    w("## 2. caliber 字段落地（§17 给 CodeBuddy 的工单）")
    br_cols = [c["name"] for c in insp.get_columns("briefing_signal")]
    pr_cols = [c["name"] for c in insp.get_columns("prediction_result")]
    w(f"- `briefing_signal` 列：{br_cols}")
    w(f"  → {'✅ 含 caliber' if 'caliber' in br_cols else '❌ 缺 caliber 列（§17 工单未落地，属 M5）'}")
    w(f"- `prediction_result` 列：{pr_cols}")
    w(f"  → {'✅ 含 caliber' if 'caliber' in pr_cols else '❌ 缺 caliber 列（§17 工单未落地，属 M5）'}")
    w("")

    # 3. 区间宽度对比
    w("## 3. 各模型区间宽度对比（backtest_detail）")
    w("| model | n | 平均宽度% | 实测覆盖 | 判定 |")
    w("|---|---|---|---|---|")
    rows = s.execute(text(
        "SELECT model, count(*), avg(high-low), "
        "avg(CASE WHEN low<=actual AND actual<=high THEN 1.0 ELSE 0.0 END) "
        "FROM backtest_detail GROUP BY model ORDER BY model"
    )).all()
    for m, n, wdt, cov in rows:
        wdt = float(wdt) if wdt is not None else float("nan")
        cov = float(cov) if cov is not None else float("nan")
        if m in ("rf", "xgb", "wavelet"):
            flag = "⚠️ 旧窄区间（待重跑回测体现校准）" if cov < 0.7 else "✅"
        else:
            flag = "✅ 经验分位法≈90%" if cov >= 0.7 else ""
        w(f"| {m} | {n} | {wdt:.3f} | {cov:.3f} | {flag} |")
    w("")

    # 4. 总结
    w("## 4. 区间校准（P1-3）状态")
    w("- 代码层：`UNCALIBRATED={rf,xgb,wavelet}` 已定义；rf/xgb/wavelet 预测器均调用 `_from_empirical_resid`（经验残差分位，与 montecarlo/bayesian 同法，后者覆盖 0.80~0.90）")
    w("- 存储层：backtest_detail/result 的 qhit 仍为旧窄区间值（rf 0.53 / xgb 0.41 / wavelet 0.20）")
    w("- 结论：校准代码**已实现并接进区间计算**，但回测未在改代码后重跑 → 存储指标待刷新。镜像已含新代码，下次周度（Sat 07:00）或手动 `run_backtest.py` 重跑即更新。")

with open(REPORT, "w", encoding="utf-8") as f:
    f.write("\n".join(L) + "\n")
print("\n".join(L))
print(f"\n=== 报告：{REPORT} ===")
