"""补充核验：缺席品种原因 + 单一整批 run 的独立显著性（避免跨 run 重复计数）"""
import sys
sys.path.insert(0, "/app")
from scipy import stats
from sqlalchemy import text
from app.core.db import session_scope
from app.core.config import get_settings
from app.features.pipeline import load_canonical_series

with session_scope() as s:
    settings = get_settings()
    all_syms = {x.symbol for x in settings.main_contracts}

    print("== 各 run 品种数 ==")
    for r in s.execute(text("SELECT run_id, count(DISTINCT symbol), count(*) FROM backtest_result GROUP BY run_id ORDER BY run_id")).all():
        print(f"  {r[0]}: {r[1]} 品种 / {r[2]} 行")

    r48 = {r[0] for r in s.execute(text(
        "SELECT DISTINCT symbol FROM backtest_result WHERE run_id='20260908_101301_bt'")).all()}
    missing = sorted(all_syms - r48)
    print(f"\n== 20260908_101301 缺席品种 {len(missing)} 个 ==")
    for sym in missing:
        try:
            df, src = load_canonical_series(s, sym)
            verdict = f"len={len(df)}" + ("（<310 数据不足→skipped）" if len(df) < 310 else "（数据足，缺席原因需查 errors）")
            print(f"  {sym}: {verdict} src={src}")
        except Exception as e:
            print(f"  {sym}: ERR {str(e)[:80]}")

    print("\n== 单一整批 run 独立显著性（detail 重算） ==")
    for rid in ("20260907_132747_bt", "20260908_101301_bt"):
        rows = s.execute(text(
            "SELECT model, "
            "sum(CASE WHEN pred_dir = CASE WHEN actual>0 THEN 'up' ELSE 'down' END THEN 1 ELSE 0 END), count(*) "
            "FROM backtest_detail WHERE run_id=:r GROUP BY model"), {"r": rid}).all()
        print(f"-- RUN {rid}")
        for m, k, n in sorted(rows):
            if not n:
                continue
            p = stats.binomtest(int(k), int(n), 0.5).pvalue
            flag = " << 显著>50%" if (p < 0.05 and k > n / 2) else (" << 显著<50%" if p < 0.05 else "")
            print(f"  {m:12s} {k:5d}/{n:5d} = {k/n:.4f}  p={p:.3g}{flag}")
