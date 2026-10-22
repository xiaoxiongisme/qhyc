# -*- coding: utf-8 -*-
"""v5x_regime.py —— V5X 自进化「在线波动体制层」

设计目标（见 v5x_self_evolution_design.html）：
  波动体制（高/中/低波）是本系统已验证真实有效的维度（HMM K=3 三态在 holdout
  的 log2/bar 1.6–1.8，远优于六位 K 线形态链 2.2）。本模块把"固定门控"升级为
  "体制感知"——但不碰方向 alpha，只做两件事：
    ① 高波态自动触发 P2 禁开仓（扩展静态 hvol 80 分位规则）；
    ② 体制驱动仓位缩放（高波降仓、低波加仓），不预测方向。

关键纪律：本模块默认只"估计体制"，绝不自行改变 strat 参数。是否把体制注入引擎
由 fusion_v5x_engine.V5XConfig.REGIME_P2_AUTO / REGIME_POS_SCALE 控制（默认全关，
保证与已验证基线逐笔对拍完全一致）。

接口：
  estimate_regimes(ba)            -> DataFrame[sym, td, vol, regime, vol_z]
  calibrate_thresholds(ba)        -> {sym: (q33, q66)}  用于在线分类
  OnlineRegime                    -> 给定近期日收益/区间，输出 regime（生产盘前调用）
  build_regime_ctx(base_ctx, regime_df, sym, td, thr, pos_map)
                                  -> 返回带 regime/pos_scale 的 DailyCtx（生产接线用）
  calibrate_to_csv(ba_path, out)  -> 落盘 regime_table.csv（离线标定，可上线）
"""
from __future__ import annotations
import os, sys, json
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

N_STATES = 3
# 体制 -> 仓位缩放（默认映射；高波降仓、低波加仓，幅度保守）。仅在 REGIME_POS_SCALE=True 时生效。
DEFAULT_POS_MAP = {0: 1.2, 1: 1.0, 2: 0.6}


def _daily_vol(ba: pd.DataFrame) -> pd.DataFrame:
    """每品种每日已实现波动率（60m 对数收益 std，按交易日聚合）。"""
    ba = ba.sort_values(['symbol', 'ts'])
    ba = ba.copy()
    ba['lr'] = np.log(ba.close / ba.close.shift(1))
    g = ba.groupby(['symbol', 'td'], sort=True)
    out = g['lr'].std().reset_index()
    out.columns = ['symbol', 'td', 'vol']
    return out


def calibrate_thresholds(ba: pd.DataFrame) -> dict:
    """每品种日波动率分布的分位阈值（33/66），用于在线二/三分位判定。"""
    dv = _daily_vol(ba)
    thr = {}
    for sym, sub in dv.groupby('symbol'):
        v = sub.vol.dropna().values
        if len(v) < 30:
            thr[sym] = (np.nan, np.nan)
        else:
            thr[sym] = (float(np.nanpercentile(v, 33)),
                        float(np.nanpercentile(v, 66)))
    return thr


def estimate_regimes(ba: pd.DataFrame, thr: dict | None = None) -> pd.DataFrame:
    """估计每品种每日波动体制：0=低波 1=中波 2=高波。
    regime 由日波动率相对该品种历史分位（33/66）判定。"""
    dv = _daily_vol(ba)
    if thr is None:
        thr = calibrate_thresholds(ba)
    rows = []
    for sym, sub in dv.groupby('symbol'):
        q33, q66 = thr.get(sym, (np.nan, np.nan))
        for _, r in sub.iterrows():
            v = r['vol']
            if not np.isfinite(v):
                regime = 1
            elif not np.isfinite(q33):
                regime = 1
            elif v <= q33:
                regime = 0
            elif v >= q66:
                regime = 2
            else:
                regime = 1
            rows.append((sym, r['td'], v, regime))
    df = pd.DataFrame(rows, columns=['sym', 'td', 'vol', 'regime'])
    # vol_z：相对该品种历史的 z 分数（便于人工查看）
    mu = df.groupby('sym').vol.transform('mean')
    sd = df.groupby('sym').vol.transform('std')
    df['vol_z'] = ((df.vol - mu) / sd.replace(0, np.nan)).fillna(0.0)
    return df


def load_ba(path: str | None = None) -> pd.DataFrame:
    """从 prod50 装载后复权 60m（与 step46 同口径）。"""
    import step46_prod_bt as s46
    ba, _, _, _, _, _ = s46.load_all()
    return ba


def calibrate_to_csv(ba_path: str | None = None, out: str = 'regime_table.csv') -> str:
    ba = load_ba(ba_path) if ba_path is None else pd.read_csv(ba_path, parse_dates=['ts'])
    thr = calibrate_thresholds(ba)
    df = estimate_regimes(ba, thr)
    df.to_csv(out, index=False, encoding='utf-8-sig')
    # 同时落盘阈值，供在线分类使用
    thr_out = os.path.join(os.path.dirname(out), 'regime_thresholds.json')
    json.dump({k: list(v) for k, v in thr.items()}, open(thr_out, 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print(f'regime table -> {out} ({len(df)} rows); thresholds -> {thr_out}')
    return out


class OnlineRegime:
    """生产盘前用的轻量在线体制估计。
    用法：每天盘前把当日之前 N 日日波动率喂入 push()，取 .regime。
    用离线标定的 (q33,q66) 作阈值，不在线重新拟合（避免前视/过拟合）。"""

    def __init__(self, sym_thr: dict, window: int = 20, pos_map: dict | None = None):
        self.thr = sym_thr
        self.window = window
        self.pos_map = pos_map or DEFAULT_POS_MAP
        self.buf: dict[str, list] = {}

    def push(self, sym: str, daily_vol: float) -> int:
        """返回该品种当前体制（0/1/2）。daily_vol 应为当日已实现波动率。"""
        b = self.buf.setdefault(sym, [])
        b.append(daily_vol)
        if len(b) > self.window:
            b.pop(0)
        q33, q66 = self.thr.get(sym, (np.nan, np.nan))
        if not (np.isfinite(q33) and np.isfinite(q66)):
            return 1
        v = daily_vol
        if v <= q33:
            return 0
        if v >= q66:
            return 2
        return 1

    def pos_scale(self, regime: int) -> float:
        return float(self.pos_map.get(int(regime), 1.0))


def build_regime_ctx(base_ctx, sym: str, td, regime_df: pd.DataFrame | None,
                     onr: 'OnlineRegime | None', daily_vol: float | None,
                     pos_scale_override: float | None = None) -> object:
    """把体制信息注入 DailyCtx。
    - 优先用离线 regime_df（已标定）查 sym/td；
    - 否则用 OnlineRegime 在线分类（需 daily_vol）；
    - pos_scale 取 pos_map 映射（REGIME_POS_SCALE 开启时引擎才使用）。
    返回的 DailyCtx 与原 base_ctx 共享 hvol/member_z/blocked/sess_last_hours。"""
    from fusion_v5x_engine import DailyCtx
    regime = None
    if regime_df is not None:
        hit = regime_df[(regime_df.sym == sym) & (regime_df.td == td)]
        if len(hit):
            regime = int(hit.regime.iloc[0])
    if regime is None and onr is not None and daily_vol is not None:
        regime = onr.push(sym, daily_vol)
    pos_scale = pos_scale_override if pos_scale_override is not None else (
        onr.pos_scale(regime) if (onr is not None and regime is not None) else 1.0)
    return DailyCtx(
        td=base_ctx.td, hvol=base_ctx.hvol, member_z=base_ctx.member_z,
        blocked=base_ctx.blocked, sess_last_hours=base_ctx.sess_last_hours,
        regime=regime, pos_scale=pos_scale)


if __name__ == '__main__':
    out = calibrate_to_csv()
    df = pd.read_csv(out)
    print(df.groupby('regime').size())
    print(df.head())
