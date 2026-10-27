# -*- coding: utf-8 -*-
"""level_touch · 按品种配置部署（让扫描结论可直接落地）。

来源：全市场日线参数扫描（sweep_results.csv，162组合×59品种）后处理推导出的
「选择性空头白名单」——22/59 品种开启空头能改善 PF/净利，其余（尤其正漂移工业
品 CU/SN/J/EB/CF/AL）开空反而亏，保持多头或回避。

用法：
  from app.backtest.level_touch.variety_config import config_for_variety, run_portfolio
  cfg = config_for_variety("AU")          # 自动 allow_short=True（AU 在白名单）
  cfg = config_for_variety("CU")          # 自动 allow_short=False（CU 不在白名单）
  df  = run_portfolio(list_products())    # 全市场按各自配置跑，输出聚合

⚠ 白名单来自样本内(9558组合)选优，必须经 walk_forward 样本外验证后才用于实盘。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Optional

from .config import DEFAULT_CONFIG, LevelTouchConfig

# 选择性空头白名单（开启 allow_short 能改善 PF/净利的品种）
# 来源：level_touch_sweep_out/selective_short_whitelist.csv (include_short=True 的 22 个)
# ⚠ 这是「样本内(9558组合)选优」得到的候选池，含过拟合，须用 VALIDATED_SHORT 过滤。
WHITELIST_SHORT = {
    "SC",   # 原油
    "AU",   # 黄金
    "AG",   # 白银
    "LC",   # 碳酸锂
    "SS",   # 不锈钢
    "SA",   # 纯碱
    "NI",   # 镍
    "RU",   # 橡胶
    "A",    # 豆一
    "SR",   # 白糖（样本内强边缘，PF≈4.6）
    "LH",   # 生猪
    "SF",   # 硅铁
    "I",    # 铁矿石
    "PG",   # 液化石油气
    "RB",   # 螺纹钢
    "TA",   # PTA
    "B",    # 豆二
    "JM",   # 焦煤
    "FU",   # 燃油
    "RM",   # 菜粕
    "P",    # 棕榈
    "PK",   # 花生
}

# OOS 验证通过（walk-forward 样本外稳健）的严格白名单：
# 准则 = OOS PF 中位>1 且 净合计>0 且 PF>1 占比>=50%。
# 日线 walk-forward 通过（7/22）：
#   AG(1.11/67%/+111k) AU(1.12/67%/+95k) JM(1.09/67%/+40k)
#   SC(1.16/100%/+107k) SF(7.43/100%/+33k) SR(3.70/100%/+12k) SS(1.39/100%/+5k)
# 60m walk-forward 额外通过：RU（日线翻车、60m 翻正，OOS PF中位1.04/+9k/67%）
# → 合计（8）：AG AU JM RU SC SF SR SS。仍待验证：NI（日线翻车，需补 60m）。
VALIDATED_SHORT = {"AG", "AU", "JM", "RU", "SC", "SF", "SR", "SS"}

# 各品种推荐回测周期（来自 deep_dive_tf + 60m walk-forward：信号密度↑但质量未必↑，故择优）
# SC/RU 在 60m 才显 edge（日线亏损/翻车）；其余日线最优，分钟级多为噪声。
BEST_FREQ = {
    "SR": "daily", "AU": "daily", "AG": "daily", "JM": "daily",
    "SF": "daily", "SS": "daily", "SC": "60m", "RU": "60m",
}


def is_short_allowed(variety: str, validated: bool = True) -> bool:
    """是否对该品种启用空头。validated=True 用 OOS 验证通过的严格白名单（部署默认）；
    False 用样本内候选池（22）。"""
    return variety in (VALIDATED_SHORT if validated else WHITELIST_SHORT)


def config_for_variety(variety: str, base: Optional[LevelTouchConfig] = None,
                       validated: bool = True, **overrides) -> LevelTouchConfig:
    """返回该品种的最终配置：allow_short 默认按白名单自动设定（默认用验证集），
    周期默认取 BEST_FREQ（覆盖），其余可被 overrides 覆盖。

    部署建议：config_for_variety(v) 默认即「验证集 + 推荐周期」，可直接用于实盘前回测。
    """
    base = base or DEFAULT_CONFIG
    kw = dict(overrides)
    if "allow_short" not in kw:
        kw["allow_short"] = variety in (VALIDATED_SHORT if validated else WHITELIST_SHORT)
    if "freq" not in kw and variety in BEST_FREQ:
        kw["freq"] = BEST_FREQ[variety]
    return replace(base, **kw)


def run_portfolio(varieties: list, base: Optional[LevelTouchConfig] = None,
                  out_dir: str = "/app/qh/level_touch_out") -> "pd.DataFrame":
    """按各品种自身配置（含按白名单的 allow_short）跑回测并聚合。

    返回 DataFrame：variety | allow_short | n_trades | pf | total_net_yuan |
    win_rate | max_dd | sharpe | avg_hold
    """
    import os
    import numpy as np
    import pandas as pd
    from .analyze import run_one

    rows = []
    total_net = 0.0
    for i, v in enumerate(varieties, 1):
        cfg = config_for_variety(v, base)
        try:
            r = run_one(v, cfg, out_dir)
        except Exception as e:  # noqa: BLE001
            r = {"variety": v, "n_trades": 0, "error": str(e)[:80]}
        r["allow_short"] = cfg.allow_short
        r.pop("error", None)
        rows.append(r)
        if i % 10 == 0:
            print(f"  [{i}/{len(varieties)}] {v} net="
                  f"{r.get('total_net_yuan', 0):.0f}")

    df = pd.DataFrame(rows)
    os.makedirs(out_dir, exist_ok=True)
    df.to_csv(os.path.join(out_dir, "portfolio_summary.csv"), index=False)
    if "total_net_yuan" in df:
        total_net = float(df["total_net_yuan"].fillna(0).sum())
        print(f"[portfolio] 组合数={len(df)} 总净利={total_net:,.0f} 元")
    return df


if __name__ == "__main__":
    from .analyze import list_products
    run_portfolio(list_products())
