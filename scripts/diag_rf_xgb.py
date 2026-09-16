"""§18.2 诊断任务：rf(0.4924)/xgb(0.4919) 为何 <0.5（方向学反？）

检查项：
1. 标签/特征滞后符号约定：X[t]=[rets[t-lag]...] → y[t]=rets[t]，预测 X_last=[rets[-1],...]
   → 数学上预测 rets[t+1]，自回归方向正确（纸面检查）
2. 实证：训练后预测值的符号 vs 最近收益 ret_t 的符号（学的是动量还是反转？）
3. 预测幅度分布：|point| 是否趋近 0（方向由噪声决定 → acc≈0.5）
4. 方向翻转实验：若把 pred 取反，acc 变为 1-acc ≈ 0.51 → 说明信息存在但符号约定反

只读诊断，不写库。容器内执行：python scripts/diag_rf_xgb.py [SYMBOL]
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/app")

import numpy as np

from app.core.db import session_scope
from app.core.logging import logger


def main(symbol: str = "FG888"):
    import numpy as np

    from app.features.pipeline import features_from_series, load_canonical_series, ret_series
    from app.predictors import MODEL_REGISTRY

    with session_scope() as s:
        df, source = load_canonical_series(s, symbol)
    print(f"== 诊断 {symbol}（source={source}, {len(df)} 根）==\n")

    for name in ("rf", "xgb"):
        cls = MODEL_REGISTRY[name]
        m = cls()
        # 用截至最后一天的全部数据
        sub = df
        snap = features_from_series(symbol, sub, source)
        rets = np.asarray(snap.rets, dtype=float)
        m._dates = snap.dates

        out = m.predict(rets)
        point = out.ret_point

        # 最近一日收益（模型看到的"昨天"）
        ret_t = float(rets[-1])

        # 扫描多个历史评估点：预测符号 vs 最近收益符号（动量/反转倾向）
        n_scan = 0
        agree = 0
        rev_hit = 0
        abs_points = []
        for back in range(60, 10, -5):
            sub2 = df.iloc[: len(df) - back]
            if len(sub2) < 300:
                continue
            snap2 = features_from_series(symbol, sub2, source)
            r2 = np.asarray(snap2.rets, dtype=float)
            m2 = cls()
            m2._dates = snap2.dates
            try:
                o2 = m2.predict(r2)
            except Exception as e:
                print(f"  [{name}] back={back} predict 失败: {e}")
                continue
            rt2 = float(r2[-1])
            n_scan += 1
            if np.sign(o2.ret_point) == np.sign(rt2):
                agree += 1
            else:
                rev_hit += 1
            abs_points.append(abs(o2.ret_point))

        print(f"[{name}] 最后时点预测: point={point:+.5f}%  (ret_t={ret_t:+.3f}%)")
        print(
            f"[{name}] 60 根内扫描 {n_scan} 点: 预测符号与 ret_t 同向(动量) {agree} / "
            f"反向(反转) {rev_hit}"
        )
        if abs_points:
            print(
                f"[{name}] 预测幅度 |point|: mean={np.mean(abs_points):.5f}% "
                f"max={np.max(abs_points):.5f}%（同期真实日波动约 1%）"
            )
        print()


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "FG888"
    main(sym)
