# -*- coding: utf-8 -*-
"""E1 执行契约：分叉声明表 + 成交价纯函数（CL-20261108-14）。

背景（2026-11-08 E1 只读调研）：
    审计报告 §3.6 / 路线图 E1 记录「回测与实盘处理链 6 处分叉」，字面目标是
    「抽 ExecutionModel 让两端共用，把 6 条降到 1 条」。逐条复核后发现多数分叉
    属于**不同层次或零影响**，为大抽象去改实盘下单链路得不偿失：
      · 止损：策略信号层（close-only）vs 账户风控层（exit_guard/stop_loss），
        本就不该合并；盘中击穿应交给 P2-5 B1 执行惩罚版回测建模。
      · 持仓上限：回测 max_hold_bars=0 vs 实盘 max_hold_days=365。实测
        10037 笔中持仓 >365 天 0 笔、最长 47.46 天、中位 1.58 天 → 对回测零影响；
        实盘这条是「800 根滚动窗导致状态冻结」的护栏，必须保留。
      · 计算窗口：回测 lookback_bars=0（全窗）vs 实盘 max_bars=800。
        5 品种逐位对拍：前 200 根 warmup 差 19~75 根，200 根后 0 差异 → 仅 warmup 边界。
      · entry_gates：默认 entry_gate_profile='off'，两端 gates 均为 None → 当前无分叉。
      · 风控层：回测本就不该有运维风控（新鲜度 / 白名单 / 熔断），允许分叉。
      · 成交价：唯一真实影响结论的分叉，已由 CL-20261108-11 引入
        fill_mode=close|next_open 量化；本模块把计算抽成纯函数供两端共用。

因此采用「窄接口 + 声明式白名单」：6 条逐条标注状态、代码定位（文件:符号，
不用行号以抗漂移）、实测证据与处置理由，由守卫 BT-EXEC-FORK-DECLARED
机器核验「声明是否仍与当前行为一致」，防止本表沦为过期文档。

⚠ 本模块是纯数据 + 纯函数，**禁止 import app.* / DB / 配置**，避免回测与守卫
被拖入执行层依赖。E1-C（CL-20261108-15）已把回测默认口径切到 next_open 并加
0.5 ATR 跳空过滤；实盘下单链路仍零改动（影子对账归 E1-D → P2-9）。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Sequence

__all__ = [
    "ForkStatus",
    "ForkDecision",
    "EXECUTION_FORKS",
    "ExecutionContract",
    "validate_fork_declaration",
    "VALID_FILL_MODES",
    "fill_price",
]


class ForkStatus(str, Enum):
    """分叉处置状态。"""

    UNIFIED = "unified"                            # 已由同一实现统一
    ALLOWED_WITH_REASON = "allowed_with_reason"   # 允许保留，附理由与依据
    DEFERRED_TO_B1 = "deferred_to_b1"             # 转 P2-5 B1 执行惩罚版回测建模


@dataclass(frozen=True)
class ForkDecision:
    key: str
    title: str
    status: ForkStatus
    backtest_ref: str     # 「文件:符号」定位，不用行号（抗漂移）
    live_ref: str
    evidence: str         # 实测证据（真实数字 / 真实对拍结论）
    decision: str         # 为什么这样处置
    guard_key: str = ""   # 关联断言键 / 台账编号


EXECUTION_FORKS: tuple[ForkDecision, ...] = (
    ForkDecision(
        key="fill_price",
        title="成交价：信号根收盘 vs 下一根开盘 +1tick",
        status=ForkStatus.UNIFIED,
        backtest_ref="app/backtest/fusion_backtest.py:_simulate_trades -> app.contracts.execution.fill_price",
        live_ref="app/scheduler.py:_fusion_scan_job latest_close",
        evidence=(
            "CL-20261108-11 全量 51 品种三轮 A/B：原版 close 与新 close 逐笔 "
            "CSV 10038 行完全一致；next_open 9984 笔，胜率 0.3025→0.3103、"
            "总收益 -99.9901%→-99.9381%"
        ),
        decision=(
            "回测已支持 fill_mode=close|next_open；E1-C / CL-20261108-15 起默认改为 "
            "next_open（下一根开盘 ±1tick 不利滑点）并启用 0.5 ATR 不利跳空过滤，"
            "旧 B0 基线标 pre-E1C、显式传 fill_mode=close 仍逐位可复现。"
            "成交价计算抽为纯函数 fill_price() 置于本契约层，供未来实盘影子对账复用；"
            "实盘下单链路不在本次范围（E1-D → P2-9）"
        ),
        guard_key="BT-FILL-MODE-WIRED",
    ),
    ForkDecision(
        key="stop_loss",
        title="止损：策略 close-only 信号 vs 账户级 exit_guard/stop_loss",
        status=ForkStatus.DEFERRED_TO_B1,
        backtest_ref="app/backtest/fusion_backtest.py:_simulate_trades（close-only）",
        live_ref="app/execution/exit_guard.py / stop_loss.py（盘中击穿）",
        evidence="层次不同：策略信号离场 vs 账户风控强平，非同一语义",
        decision=(
            "不合并。盘中击穿 / 执行惩罚建模转 P2-5 B1，避免把账户风控塞进信号层"
            "造成回测语义污染"
        ),
        guard_key="CL-20261108-08",
    ),
    ForkDecision(
        key="max_hold",
        title="持仓上限：回测 max_hold_bars=0 vs 实盘 max_hold_days=365",
        status=ForkStatus.ALLOWED_WITH_REASON,
        backtest_ref="app/backtest/fusion_backtest.py:FusionBacktestParams.max_hold_bars",
        live_ref="app/core/config.py:FusionConfig.max_hold_days / app/scheduler.py:_fusion_scan_job",
        evidence=(
            "E1 只读量化：10037 笔中持仓 >365 天 0 笔，最长 47.46 天、"
            "中位 1.58 天 → 对回测结论零影响"
        ),
        decision=(
            "实盘 365 天护栏是「在线引擎 800 根滚动窗导致状态冻结」的必要兜底，"
            "必须保留；回测信号层自带吊灯 / 止损会离场，关闭上限不改变研究基线"
        ),
        guard_key="BT-EXEC-FORK-DECLARED",
    ),
    ForkDecision(
        key="window",
        title="计算窗口：回测 lookback_bars=0 全窗 vs 实盘 max_bars=800",
        status=ForkStatus.ALLOWED_WITH_REASON,
        backtest_ref="app/backtest/fusion_backtest.py:FusionBacktestParams.lookback_bars",
        live_ref="app/strategies/fusion_signal.py:evaluate_symbol max_bars",
        evidence=(
            "E1 只读对拍 5 品种：前 200 根 warmup 差 19~75 根，"
            "200 根后逐位 0 差异 → 仅 warmup 边界"
        ),
        decision=(
            "实盘滚动窗是内存 / 延迟约束，回测全窗是 walk-forward 天然成立；"
            "差异仅在 warmup，不影响稳态信号，保留但由断言钉住默认值"
        ),
        guard_key="BT-EXEC-FORK-DECLARED",
    ),
    ForkDecision(
        key="entry_gates",
        title="入场门控：entry_gate_profile 默认 off，两端 gates 均 None",
        status=ForkStatus.ALLOWED_WITH_REASON,
        backtest_ref="app/strategies/fusion_signal.py:walk_fusion_states entry_gates",
        live_ref="app/strategies/fusion_signal.py:evaluate_symbol gates",
        evidence=(
            "E1 只读核实：默认 entry_gate_profile='off' → 两端均传 None，当前无分叉"
        ),
        decision=(
            "当前无分叉，但一旦启用 final_v2 门控，回测 _generate_states 必须同步传 "
            "gates，否则会静默分叉；由断言钉住默认值 off，启用时须另立台账"
        ),
        guard_key="BT-EXEC-FORK-DECLARED",
    ),
    ForkDecision(
        key="ops_risk",
        title="运维风控层：回测无 / 实盘新鲜度·白名单·熔断",
        status=ForkStatus.ALLOWED_WITH_REASON,
        backtest_ref="（无，回测不含运维风控）",
        live_ref="app/scheduler.py:_fusion_scan_job fresh / VAR-SCOPE-WIRED",
        evidence="回测研究口径本就不该包含运维风控；实盘风控由 VAR-* 断言独立覆盖",
        decision=(
            "允许分叉且不列入「需统一」清单；E4 白名单从 1 条扩为 2 条"
            "（剔除未收盘 K + 运维风控），两条都有明确理由与断言"
        ),
        guard_key="VAR-SCOPE-WIRED",
    ),
)


def validate_fork_declaration() -> tuple[bool, list[str]]:
    """自检声明表完整性：6 条齐全、key 唯一、状态合法、每条有证据与处置。"""
    problems: list[str] = []
    keys = [f.key for f in EXECUTION_FORKS]
    expected = {"fill_price", "stop_loss", "max_hold", "window",
                "entry_gates", "ops_risk"}
    if set(keys) != expected:
        problems.append(f"分叉集合不符：{sorted(keys)} != {sorted(expected)}")
    if len(keys) != len(set(keys)):
        problems.append("存在重复 key")
    for f in EXECUTION_FORKS:
        if not isinstance(f.status, ForkStatus):
            problems.append(f"{f.key}: status 非 ForkStatus")
        for name in ("title", "backtest_ref", "live_ref", "evidence",
                     "decision", "guard_key"):
            if not str(getattr(f, name, "")).strip():
                problems.append(f"{f.key}: {name} 为空")
    unified = [f.key for f in EXECUTION_FORKS
               if f.status is ForkStatus.UNIFIED]
    if unified != ["fill_price"]:
        problems.append(
            f"UNIFIED 条目必须且只能是 fill_price，实得 {unified}"
            "（其余应为白名单或延后）")
    return (not problems), problems


class ExecutionContract:
    """窄接口：查询分叉是否允许保留，以及仍需后续工程处理的条目。"""

    FORKS: tuple[ForkDecision, ...] = EXECUTION_FORKS

    @classmethod
    def get(cls, key: str) -> ForkDecision | None:
        return next((f for f in cls.FORKS if f.key == key), None)

    @classmethod
    def is_allowed(cls, key: str) -> bool:
        """允许保留的分叉必须有状态 + 非空理由，否则视为未声明。"""
        f = cls.get(key)
        return bool(f and f.status is ForkStatus.ALLOWED_WITH_REASON
                    and f.decision.strip())

    @classmethod
    def unresolved(cls) -> tuple[ForkDecision, ...]:
        """尚未统一、也不在白名单内（转 B1 的条目）。"""
        return tuple(f for f in cls.FORKS
                     if f.status is ForkStatus.DEFERRED_TO_B1)


VALID_FILL_MODES = ("close", "next_open")


def fill_price(
    i: int,
    dirn: float,
    is_open: bool,
    closes: Sequence[float],
    opens: Sequence[float],
    n_bars: int,
    fill_mode: str,
    tick: float,
) -> tuple[float, int]:
    """成交价纯函数：给定信号 bar 索引 i，返回 (成交价, 成交 bar 索引)。

    这是 E1-B 抽出的**唯一共用实现**（回测已接线；实盘暂不接，见 E1-D）。
    不放进 ``app/execution/`` 是为了避开该包 ``__init__`` 的五个 service 子模块。

    close 模式：恒返回 ``(closes[i], i)``，与 CL-20261108-11 之前的历史基线逐位一致。
    next_open 模式：bar i 收盘出信号，bar i+1 开盘成交；开仓按 ``+dirn×tick``、
    平仓按 ``-dirn×tick`` 加不利滑点。末尾无 i+1 时退回 ``closes[i]`` 仍加滑点，
    避免凭空多一根 K。

    ⚠ 纯函数不做 fill_mode 校验（调用方 ``_simulate_trades`` 已校验），
    也不读任何全局状态 —— 守卫可直接合成数组做逐位对拍。
    """
    if fill_mode == "close":
        return float(closes[i]), i
    j = i + 1
    base = float(opens[j]) if j < n_bars else float(closes[i])
    sign = dirn if is_open else -dirn
    return base + sign * tick, min(j, n_bars - 1)
