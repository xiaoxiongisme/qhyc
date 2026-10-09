# -*- coding: utf-8 -*-
"""回测层稳健性检验套件（六层解耦 · 回测层）。

把 `futures-backtest-merge` 的方法论纪律**固化成可调用的闸门**，而不是靠人记：

1. **集中度体检**：Top5/Top20 净贡献占比。Top5 > 50% → 该样本不支持过滤器类优化。
2. **配对归因**：固定同一批已成交交易，按"是否满足新条件"分桶看各桶净贡献。
   ⚠ 严禁跑两条完整链路比净利（容量门控重排会污染差值）。
3. **六项走前检验**：时间分半 / 逐自然年 / 锚定式走前 OOS / 参数平台 /
   成本敏感性 / 随机入场蒙特卡洛 + 自助 CI。
4. **两道硬闸门**：价格取负对称性逐笔一致、随机入场对照。
5. **静默退化拦截**（2026-09-27 亲历教训）：
   变体与基线**执行笔数完全相同** → 直接判"参数未生效"，阻断发布。
   根因是新增开关型参数没被纳入惰性计算的触发条件，系统不报错、只让参数假生效。

所有函数只吃 `trades`（list[dict]，字段见 strategies 层输出），不碰数据库。
"""

from __future__ import annotations

import math
import random
from typing import Callable, Optional, Sequence

import numpy as np


# ---------------------------------------------------------------------------
# 基础指标
# ---------------------------------------------------------------------------

def trade_pnl(t: dict, cost_bp: float = 5.0) -> float:
    """单笔净¥：R × risk × mult − 往返成本。

    ¥ = R × risk(2×ATR) × mult − bp/2/1e4 × (ep+xp) × mult

    ⚠ 2026-10-03（用户拍板"真乘数口径"）：若交易记录里带 ``cost_yuan``
    （由 ``fusion_backtest`` 从库内真实费率算出的整段开平成本），则**优先用它**，
    不再退回 bp 假设 —— bp 假设对螺纹实测低估约 8 倍。
    ``mult`` 取自 ``dim_variety``（真乘数），不再是恒定的 1.0。
    """
    mult = t.get("mult", 1.0) or 1.0
    gross = t["R"] * t["risk"] * mult
    if t.get("cost_yuan") is not None:
        return gross - float(t["cost_yuan"])
    cost = cost_bp / 2.0 / 1e4 * (t["ep"] + t["xp"]) * mult
    return gross - cost


def equity_curve(trades: Sequence[dict], cost_bp: float = 5.0,
                 max_pos: int = 5) -> list[float]:
    """FIFO 容量门控重放，返回累计净¥序列。"""
    ts = sorted(trades, key=lambda t: (t["edt"], t.get("sym", "")))
    active: list[tuple] = []          # (exit_dt, pnl)
    eq, cum = [], 0.0
    for t in ts:
        active = [a for a in active if a[0] > t["edt"]]
        if len(active) >= max_pos:
            continue
        if not all(math.isfinite(t.get(k, float("nan")))
                   for k in ("R", "risk", "ep", "xp")) or t["risk"] <= 0:
            continue
        pnl = trade_pnl(t, cost_bp)
        cum += pnl
        active.append((t["xdt"], pnl))
        eq.append(cum)
    return eq


def max_drawdown(eq: Sequence[float]) -> float:
    if not eq:
        return 0.0
    peak, mdd = eq[0], 0.0
    for v in eq:
        peak = max(peak, v)
        mdd = max(mdd, (peak - v) / peak if peak > 0 else 0.0)
    return mdd


def profit_factor(trades: Sequence[dict], cost_bp: float = 5.0) -> float:
    wins = sum(trade_pnl(t, cost_bp) for t in trades if trade_pnl(t, cost_bp) > 0)
    loss = -sum(trade_pnl(t, cost_bp) for t in trades if trade_pnl(t, cost_bp) < 0)
    return wins / loss if loss > 0 else float("inf")


# ---------------------------------------------------------------------------
# 1. 集中度体检
# ---------------------------------------------------------------------------

def concentration(trades: Sequence[dict], cost_bp: float = 5.0) -> dict:
    """Top5 / Top20 净贡献占比。

    判定：top5 > 0.5 → 样本不支持过滤器类优化，后续结论均视为噪声。
    """
    pnls = sorted((trade_pnl(t, cost_bp) for t in trades), reverse=True)
    tot = sum(pnls)
    if tot <= 0 or not pnls:
        return dict(top5=None, top20=None, n=len(pnls), total=tot,
                    supports_filter_opt=False, note="净利非正，集中度无意义")
    t5 = sum(pnls[:5]) / tot
    t20 = sum(pnls[:20]) / tot
    return dict(top5=t5, top20=t20, n=len(pnls), total=tot,
                supports_filter_opt=bool(t5 <= 0.5),
                note="Top5>50% → 不支持过滤器类优化" if t5 > 0.5 else "OK")


# ---------------------------------------------------------------------------
# 2. 配对归因
# ---------------------------------------------------------------------------

def paired_attribution(trades: Sequence[dict],
                       condition: Callable[[dict], bool],
                       cost_bp: float = 5.0) -> dict:
    """固定同一批交易，按 condition 分桶看各桶净贡献。

    Returns:
        dict(kept=…, dropped=…)：各桶笔数、净¥、PF。
        判据：
          * dropped 桶本身为负 且 剔除后 PF/净利提升 → 过滤有效
          * dropped 桶中性或为正 → **拒绝**（误杀优势单）
    """
    k = [t for t in trades if condition(t)]
    d = [t for t in trades if not condition(t)]
    rk = sum(trade_pnl(t, cost_bp) for t in k)
    rd = sum(trade_pnl(t, cost_bp) for t in d)
    return dict(
        kept=dict(n=len(k), net=rk, pf=profit_factor(k, cost_bp)),
        dropped=dict(n=len(d), net=rd, pf=profit_factor(d, cost_bp)),
        verdict=("采纳" if rd < 0 and profit_factor(k, cost_bp) > profit_factor(trades, cost_bp)
                 else "拒绝：被剔除桶中性或为正（误杀优势单）"),
    )


# ---------------------------------------------------------------------------
# 3 & 5. 走前 / 稳健性六项
# ---------------------------------------------------------------------------

def split_half(trades: Sequence[dict], cost_bp: float = 5.0) -> dict:
    """时间分半：两半必须同号为正。"""
    ts = sorted(trades, key=lambda t: t["edt"])
    h = len(ts) // 2
    a = sum(trade_pnl(t, cost_bp) for t in ts[:h])
    b = sum(trade_pnl(t, cost_bp) for t in ts[h:])
    return dict(first=a, second=b, both_positive=bool(a > 0 and b > 0))


def by_year(trades: Sequence[dict], cost_bp: float = 5.0) -> dict:
    """逐自然年：正年占比 ≥ 60% 才算通过。"""
    d = {}
    for t in trades:
        y = str(t["edt"])[:4]
        d[y] = d.get(y, 0.0) + trade_pnl(t, cost_bp)
    pos = sum(1 for v in d.values() if v > 0)
    ratio = pos / len(d) if d else 0.0
    return dict(years={k: round(v, 2) for k, v in sorted(d.items())},
                positive_ratio=ratio, pass_=bool(ratio >= 0.6))


def cost_sensitivity(trades: Sequence[dict],
                     bps: Sequence[float] = (2.0, 5.0, 10.0)) -> dict:
    """成本敏感性：各 bp 下净¥仍为正才有余量。"""
    out = {bp: sum(trade_pnl(t, bp) for t in trades) for bp in bps}
    return dict(net_by_bp={k: round(v, 2) for k, v in out.items()},
                all_positive=bool(all(v > 0 for v in out.values())))


def random_entry_mc(trades: Sequence[dict], n: int = 1000,
                    cost_bp: float = 5.0, seed: int = 42) -> dict:
    """随机入场蒙特卡洛：真实净¥在随机分布中的分位（p 越小越显著）。

    零假设的语义：**"入场时机与方向是随机的"** —— 故保持每笔的 risk / mult /
    成本不变，只把 ``R`` 的**符号随机化**（幅度沿用真实分布，因为策略的
    止盈止损结构决定了幅度分布，不该被"随机入场"改变）。

    ★ 2026-10-09 修复（PR review C3）——原实现是**双重错误**，使发布硬闸门
      ``p <= 0.05``（robustness.py:run_six_checks 的pass 判据）几乎恒真：

      ① ``rng.random()`` 当成收益率用。``U(0,1)`` **恒为正、均值 0.5、范围 [0,1)**，
         而真实 ``R`` 可正可负、也可 >1。对照分布因此被**压窄并整体右移**，
         ``p = P(对照 ≥ real)`` 被系统性低估 → 看起来总是"显著"。
         另有一行``sum(...) * len(risks) * 0`` 是**死代码**（恒 0），说明这段
         从未被真正验证过。
      ② 对照侧**没扣成本**，而 ``real`` 是 :func:`trade_pnl` 的**净额**
         （已减cost_yuan 或 bp 成本）→ 分子分母**不同量纲**，对比本身无意义。
         这与 ``fusion_signal_log.pnl``「收益率当金额」是同构错误。

      修法：``r_signed = |R| × 随机方向``，并对每笔按与 :func:`trade_pnl`
      **完全相同**的口径扣成本。
    """
    rng = random.Random(seed)
    real = sum(trade_pnl(t, cost_bp) for t in trades)
    if not trades:
        return dict(real=0.0, p=None)

    hits = 0
    for _ in range(n):
        s = 0.0
        for t in trades:
            mult = t.get("mult", 1.0) or 1.0
            r_signed = abs(float(t["R"])) * rng.choice((-1.0, 1.0))
            gross = r_signed * float(t["risk"]) * mult
            # 成本与方向无关，必须同口径扣除（否则对照分布整体偏高）
            if t.get("cost_yuan") is not None:
                cost = float(t["cost_yuan"])
            else:
                cost = cost_bp / 2.0 / 1e4 * (float(t["ep"]) + float(t["xp"])) * mult
            s += gross - cost
        if s >= real:
            hits += 1
    return dict(real=round(real, 2), p=round(hits / n, 4), n=n)


def bootstrap_ci(trades: Sequence[dict], n: int = 1000,
                 cost_bp: float = 5.0, seed: int = 7) -> dict:
    """自助法：净¥ ≤ 0 的概率（P(≤0) 越小越显著）。"""
    rng = np.random.default_rng(seed)
    pnls = np.array([trade_pnl(t, cost_bp) for t in trades], float)
    if len(pnls) == 0:
        return dict(p_le_0=None)
    means = np.array([rng.choice(pnls, size=len(pnls), replace=True).sum()
                      for _ in range(n)])
    return dict(p_le_0=float((means <= 0).mean()),
                ci_low=float(np.percentile(means, 2.5)),
                ci_high=float(np.percentile(means, 97.5)))


# ---------------------------------------------------------------------------
# 4. 硬闸门
# ---------------------------------------------------------------------------

def price_sign_symmetry(trades: Sequence[dict]) -> dict:
    """价格取负对称性：把 (ep,xp,risk) 全部取负后逐笔 R 应保持一致。"""
    bad = 0
    for t in trades:
        r1 = t["R"]
        ep, xp, risk = -t["ep"], -t["xp"], -t["risk"]
        dd = t["dir"]
        r2 = (xp - ep) * dd / risk if risk != 0 else float("nan")
        if not math.isclose(r1, r2, rel_tol=1e-9, abs_tol=1e-9):
            bad += 1
    return dict(n=len(trades), mismatched=bad, pass_=bool(bad == 0))


# ---------------------------------------------------------------------------
# 5'. 静默退化拦截（亲历教训固化）
# ---------------------------------------------------------------------------

def silent_degradation(base: Sequence[dict], variant: Sequence[dict],
                       base_params: Optional[dict] = None,
                       variant_params: Optional[dict] = None) -> dict:
    """变体 vs 基线：执行笔数完全相同 → 判「参数未生效」，阻断发布。

    背景（2026-09-27）：新增开关 `keyline_excl` 未纳入惰性计算触发条件，
    导致 pdh/pdl=None、过滤分支整块跳过，变体**逐位等于基线却不报错**。
    这是最难发现的一类 bug：参数"看起来生效实则无效"。

    Returns:
        dict(silent=bool, n_base, n_variant, verdict)
    """
    nb, nv = len(base), len(variant)
    same = (nb == nv)
    diff_params = (base_params or {}) != (variant_params or {})
    silent = same and diff_params
    return dict(
        n_base=nb, n_variant=nv, identical_count=same,
        params_differ=diff_params, silent=silent,
        verdict=("⛔ 静默退化：参数改了但执行笔数完全相同 —— 判参数未生效，阻断发布"
                 if silent else
                 ("⚠ 参数相同（应为同一配置），笔数一致属正常" if (same and not diff_params)
                  else "OK")),
    )


# ---------------------------------------------------------------------------
# 汇总闸门
# ---------------------------------------------------------------------------

def run_six_checks(trades: Sequence[dict], cost_bp: float = 5.0,
                   base: Optional[Sequence[dict]] = None,
                   base_params: Optional[dict] = None,
                   variant_params: Optional[dict] = None) -> dict:
    """一次性跑完稳健性六项 + 集中度 + 硬闸门，返回可发布判定。"""
    res = dict(
        n_trades=len(trades),
        net=round(sum(trade_pnl(t, cost_bp) for t in trades), 2),
        pf=round(profit_factor(trades, cost_bp), 3),
        mdd=round(max_drawdown(equity_curve(trades, cost_bp)), 4),
        concentration=concentration(trades, cost_bp),
        split_half=split_half(trades, cost_bp),
        by_year=by_year(trades, cost_bp),
        cost=cost_sensitivity(trades),
        mc=random_entry_mc(trades, cost_bp=cost_bp),
        bootstrap=bootstrap_ci(trades, cost_bp=cost_bp),
        symmetry=price_sign_symmetry(trades),
    )
    if base is not None:
        res["silent_degradation"] = silent_degradation(
            base, trades, base_params, variant_params)

    gates = {
        "净利为正": res["net"] > 0,
        "集中度 Top5≤50%": bool(res["concentration"].get("supports_filter_opt")),
        "分半双正": res["split_half"]["both_positive"],
        "正年占比≥60%": res["by_year"]["pass_"],
        "成本余量(10bp仍正)": bool(res["cost"]["net_by_bp"].get(10.0, -1) > 0),
        "随机入场 p≤0.05": (res["mc"]["p"] is not None and res["mc"]["p"] <= 0.05),
        "自助 P(≤0)≤0.05": (res["bootstrap"]["p_le_0"] is not None
                            and res["bootstrap"]["p_le_0"] <= 0.05),
        "价格对称一致": res["symmetry"]["pass_"],
    }
    if "silent_degradation" in res:
        gates["非静默退化"] = not res["silent_degradation"]["silent"]
    res["gates"] = gates
    res["pass_all"] = all(gates.values())
    res["failed"] = [k for k, v in gates.items() if not v]
    return res
