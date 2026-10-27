# level_touch · 价格点位触达频率回测模块

## 1. 研究目标

对期货历史数据（2015 至今）按**真实绝对价位**打标签、自我分析并演进：

- 标出每日高低点、计算代表价（典型价）；
- 建立**价格点位触达图**：价位 $p$ 被某日日区间 $[low,high]$ 覆盖（含）即记一次触及；
- 派生：触达频次、停留密度、局部时间（Tanaka）、支撑/阻力强度 $z$ 分数、
  历史最高/最低（ATH/ATL）、稀有层级（仅触及 1/2 次）、漂移 $\mu$/波动 $\sigma$、
  「涨 $G$% 后难回买价」递归概率；
- 据上述派生量生成三类假设并做**成本感知回测**。

## 2. 数学基础

- **局部时间 / 触达图**：连续情形为 Itô 过程的局部时间
  $\ell_T(p)=\lim_{\varepsilon\to0}\frac1{2\varepsilon}\int_0^T\mathbf 1_{|S_t-p|<\varepsilon}dt$，
  Tanaka 公式 $|S_t-p|=|S_0-p|+\int\operatorname{sgn}(S_u-p)dS_u+\ell_t(p)$。
- **常返 vs 瞬态**：一维无漂移布朗（公平随机游走）是**常返**的，每个价位必被
  无限次访问 ⇒ 无漂移时「难回某价」不可能；唯有**正漂移**使下方价位瞬态，
  越过后回归概率 $<1$。这是 Q3「难回买价」的唯一数学前提。
- **首达时 / 反射原理**：带漂移首达时服从逆高斯分布；
  $P(M_T\ge a)=2\Phi(a/\sqrt T)$（漂移 0）给出 ATH 的分布。
- **递归概率闭式解**（直接回答 Q3）：
  $$P(\text{涨 }G\%\text{ 后永不回买价})=1-\exp\!\Big(-\frac{2\mu\ln(1+G)}{\sigma^2}\Big),\quad\mu>0$$
- **代数几何 / 拓扑（诚实边界）**：严格代数几何（复簇）对一维时间序列无直接应用；
  真正成立的是 **Morse/Reeb 拓扑**（触达次数 = 跨该价位的单调段数）、
  **奇点/灾变论**（支撑阻力击穿 = Whitney 折/尖点）、**分形/Hausdorff 维数**与
  **Takens 延迟嵌入**的吸引子几何。详见对话记录，勿过度引申。

## 3. 后复权可信度（结论）

| 研究类型 | 是否用后复权 | 原因 |
|---|---|---|
| 动量/波动/跨期收益 | **必须** | 需收益率连续，剔除换月缺口 |
| **本模块（绝对点位触达）** | **不可用** | 后复权平移历史绝对价位，使「该点位是否被触及」失真 |

⇒ 本模块一律使用 `barstore.load(variety,'daily','continuous')` 的**未复权**主连
（`main_continuous.raw_*` 真实价位）。历史绝对高位/当前价历史到达次数/难回买价
只有在真实点位上才有意义。

## 4. 三类假设（信号）

- **S1 支撑反弹**：价格跌入历史上被高频触及（高 $z$）的支撑带并当日收回 → 多。
- **S2 ATH 突破/回踩**：近期创 ATH（或回踩至 ATH 带内收回）→ 多（动量延续）。
- **S3 难回买价**：出现 ≥`drawdown_thr` 的「坑」，自坑底回升 ≥`recover_gain`，
  且拟合漂移下「不回坑底」概率 ≥ 阈值 → 多（直接检验 Q3）。

## 5. 回测口径（铁律）

1. 未复权日线主连；2. 决策用 $t$ 收盘、成交用 $t+1$ 开盘（次根），
   bar 内止损/止盈（保守、无前视）；3. 成本走 `app.data.cost`（历史费率/乘数/
   tick/滑点）；4. $|日收益|>10\%$ 的换月/涨跌停 bar 剔除。

## 6. 运行

```bash
# 容器内（有 DB + app 包 + 依赖）
docker exec qhyc-api PYTHONPATH=/app python -m app.backtest.level_touch.analyze
# 单品种快速验证
docker exec qhyc-api PYTHONPATH=/app python -c "
from app.backtest.level_touch.analyze import run_one
from app.backtest.level_touch.config import DEFAULT_CONFIG as C
print(run_one('RB', C, '/app/qh/level_touch_out'))
"
```

产物：`level_stats_<PROD>.csv`（逐价位触达图）、`backtest_summary.csv`、
`backtest_report.md`。

## 7. 单测

`pytest tests/test_level_touch.py`（纯数学引擎，无 DB 依赖）。
