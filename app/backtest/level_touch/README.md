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

## 4. 三类假设（信号，含空头对称）

- **S1 支撑反弹 / S1 阻力回落**：价格跌入强支撑带并收回 → 多；反之涨入强阻力带
  并收回 → 空（均值回归，对称）。
- **S2 ATH 突破/回踩 / S2 ATL 突破/回踩**：近期创 ATH（或回踩至 ATH 带内收回）
  → 多；近期创 ATL（或回踩至 ATL 带内收回）→ 空（动量延续，对称）。
- **S3 难回买价 / S3 难回卖价**：出现 ≥`drawdown_thr` 的「坑」，自坑底回升
  ≥`recover_gain`，且拟合漂移下「不回坑底(卖价)」概率 ≥ 阈值 → 多（正向漂移）/
  空（负向漂移），直接检验 Q3。

空头由 `allow_short` 开关（默认 **False**，见 §10 实证结论）。

## 5. 回测口径（铁律）

1. 未复权主连（`continuous`）；2. 决策用 $t$ 收盘、成交用 $t+1$ 开盘（次根），
   bar 内止损/止盈（保守、无前视）；3. 成本走 `app.data.cost`（历史费率/乘数/
   tick/滑点，按(品种,日期)缓存）；4. $|bar收益率|>$`limit_excl` 的换月/涨跌停
   bar 剔除（分钟级按比例缩放）。

## 6. 多周期（freq）与参数缩放

`config.freq ∈ {daily, 60m, 15m}`。日级参数经 `resolve_freq()` 按 `bars_per_day`
（daily=1 / 60m=4 / 15m=16，近似值、忽略夜盘）缩放到 bar 级：
`warmup / max_hold / atr_window / s3_window` 按倍数放大，`limit_excl` 按
$\sqrt{bars\_per\_day}$ 缩小（捕捉同量级换月缺口）。分钟级回测性能关键优化：
ATR 与滚动漂移/波动用 cumsum 向量化；z 与强价位列表按 `z_every`（日线每 bar、
分钟级每日）重算；ATH/ATL 用 O(1) 增量维护；S3 坑结构用双单调队列 O(n) 预计算。
故 15m 全历史（~88 万 bar）单品种可在 ~10s 跑完。

## 7. 运行

```bash
# 容器内（有 DB + app 包 + 依赖）
docker exec qhyc-api PYTHONPATH=/app python -m app.backtest.level_touch.analyze
# 单品种（可指定周期 / 空头）
docker exec qhyc-api PYTHONPATH=/app python -c "
from app.backtest.level_touch.analyze import run_one
from app.backtest.level_touch.config import LevelTouchConfig as C
print(run_one('RB', C(freq='60m', allow_short=True), '/app/qh/level_touch_out'))
"
# 参数扫描
docker exec qhyc-api PYTHONPATH=/app python -m app.backtest.level_touch.sweep
```

产物：`level_stats_<PROD>.csv`（逐价位触达图）、`backtest_summary.csv`、
`backtest_report.md`；扫描另产 `sweep_results.csv` / `sweep_summary.md`。

## 8. 参数扫描（sweep）

`sweep.py` 对每个品种只加载一次 bars，遍历参数网格（`freq / allow_short /
support_z_thr / stop_atr_mult / target_atr_mult / s3_no_return_p_thr`，键即
`LevelTouchConfig` 字段名）跑 `run_backtest`，输出每行(品种×组合)指标与每品种
最优组合 + 各维度盈利稳健性。分钟级网格较慢，建议先用小网格 + 少量品种探索。

## 9. 单测

`pytest tests/test_level_touch.py`（纯数学引擎，无 DB 依赖，12 项）。

## 10. 诚实结论（实证发现，勿虚高）

- **日线多头基线（S1+S2+S3 全开）全市场净值约 −6 万**（59 品种、标准化¥100万账户、
  单边1手）。注意：早期版本报出的 +44 万 实因 **S3 漂移切片 bug（`lookback_days=None`
  时 `logret` 切片为空 → `mu=0` → S3 从未触发，等价于仅 S2）**，并非真实 Edge。
  本次重构让 S3 真正生效，而它在商品上 **净亏损**（如 RB 24 笔 PF 0.63）。
- **空头默认关闭**：全市场开启空头后净值由 +44 万(仅多头 S2) 转 **−48 万**——
  多数商品具正漂移，趋势上行市做空天然吃亏。故 `allow_short` 默认 False，需逐品种
  调参后选择性启用。
- **S2 是最稳健的正贡献族**；S3（抄底）在成本后整体无正期望，属"逻辑成立但被
  摩擦吃掉"的典型。模块如实呈现，配合 §8 扫描寻找子集正 Edge。

