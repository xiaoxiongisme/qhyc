# 期货量化交易系统 — 需求文档 (PRD) · 统一版

> 版本：v2.0.0（统一版）｜ 生成：2026-09-16
> 本文件**合并并取代**《期货预测平台需求文档_PRD.md v1.3.5》与 WorkBuddy 的融合/信号/回测补充文档，是 codebuddy（CB）编码、workbuddy（WB）复审的**唯一权威需求来源**。
> 工程根目录 `E:\Docker\qhyc`；数据模板源 `E:\QH\{PRODUCT}\data`；桌面权威参考 `C:\Users\Seven\Desktop\融合\终版资料\01~04`。

---

## 1. 项目概述与目标

构建一个**本地运行的期货量化交易系统**，在统一的数据底座上，同时具备：

1. **预测系统**：基于数学/统计/机器学习模型，预测下一交易日涨跌方向与幅度（CB 主导）。
2. **交易策略判定**：基于融合策略 V2.0（额外执行口径），在小时线级别判定每个品种的**持仓状态**（空仓/持多/持空）与**开/平/反手**信号（WB 主导）。
3. **信号推送**：将交易信号实时推送至微信（pushplus）/ 企业微信 / 钉钉 / 飞书，含止损位、保本位、持仓简报（WB 主导）。
4. **回测与研究**：可手动调整参数进行回测，支持**鲁棒性测试**（多源/多容量/多成本/多 seed）、**walk-forward 测试**，并纳入系统 UI（WB 主导，方法学源自 04 资料）。

**非目标（本期）**：自动下单执行、多用户账号体系、云端多租户（架构预留云端演进）。

> 一句话定位：**预测系统给"方向概率"，交易策略给"当下该不该动"，信号推送把结论送到手里，回测研究验证策略 robustness**——四者共用同一数据底座、同一套口径铁律。

---

## 2. 总体架构（分层）

```
[数据源] akshare(主,含新浪60分) ──┐
                                  ├─→ [采集/校准层] 入库 + tqsdk 补缺/比对 + 异常工单 + 小时线逐根增量
[校准源] tqsdk(天勤) ──────────────┘
                                       ↓
[数据层] PostgreSQL + TimescaleDB
   · 运行时表：daily_bar(日线) / hourly_bar(小时线,逐根增量) / main_continuous(主连)
   · 回测底座：fut_kline 超表(freq×kind×symbol×trade_datetime，含 cont_adj 复权)
                                       ↓
[计算层] ┌─ 预测引擎（方向/幅度，模型库集成）        ← CB
         ├─ 交易策略引擎 fusion_signal（开/平/反手状态机） ← WB
         └─ 回测引擎（信号层 walk-forward + 组合层 FIFO 重放） ← WB/CB
                                       ↓
[服务层] FastAPI：行情 / 预测 / 信号 / 回测 / 数据更新 / 任务 / 异常 / 对接
                                       ↓
[展示层] React 看板（预测 / 信号 / 回测调参）+ 微信推送（pushplus/webhook）
```

---

## 3. 技术栈（已确认）

| 层 | 选型 |
|---|---|
| 语言 | Python 3.11+ |
| 后端 | FastAPI（异步，自动文档） |
| 数据库 | PostgreSQL 16 + TimescaleDB 2.17.1（超表） |
| 前端 | React 18 + Vite + TS + AntD + ECharts |
| 计算 | scipy / statsmodels / scikit-learn / PyTorch / prophet |
| 调度 | APScheduler（本地常驻） |
| 行情源 | akshare（主，含新浪 60 分钟）+ tqsdk（校准/兜底/复权） |
| 推送 | pushplus（微信主通道）+ 通用 webhook（企微/钉钉/飞书 降级） |
| 部署 | Docker Compose（本地起步，云端演进预留） |

---

## 4. 数据层（统一标准 + 日线/小时线获取与更新机制）★ 重点

### 4.1 落盘位置（物理与逻辑）
**所有行情最终落盘 Docker 容器内 TimescaleDB，不是本地 CSV/文件。**
- 容器 `qhyc-timescaledb`；库/用户 `futures`；端口 5432 映射宿主机。
- 物理卷：`E:/Docker/qhyc/pgdata/`（容器 `/var/lib/postgresql/data` 挂载）。
- 启动前提：**必须 Docker Desktop 运行**（沙箱内 `wsl.exe` 被安全策略拦截，需手动开 Docker）。
- 本地 `E:/QH/...` 与容器导入源仅作**离线备份/导入**，非落盘点；上线不依赖。

### 4.2 双表架构（两套表，用途不同，禁止混用）
| 表 | 周期 | 主键 | 写入方 | 用途 |
|---|---|---|---|---|
| `daily_bar` | 日线 | `(symbol, trade_date)` | `orchestrator.upsert_daily_bars` | akshare 主源+校准，日级特征/展示 |
| `hourly_bar` | 小时线 | `(symbol, trade_datetime)` | `HourlyCollector`（逐根增量 + 定时全量兜底） | **信号引擎实时读取**（按 `fusion.hourly_src` 单源） |
| `main_continuous` | 主连 | `(product, trade_date)` | `upsert_main_continuous` | 同时存裸价与复权价 |
| `fut_kline`（超表） | 全频率/全口径 | `(freq, kind, symbol, trade_datetime)` | `fdf/*` + `runtime/_build_daily_*` | **回测/研究底座**，含 `cont_adj` 复权权威 |

- `fut_kline.kind ∈ {continuous, contract, cont_adj}`；`freq ∈ {daily, hourly, min15, ...}`；`adj` 列标记已/未复权段（回测只取 `adj=True`）。

### 4.3 获取与更新机制

#### 4.3.1 日线更新（路径 A，已接调度）
- 主源 akshare `futures_main_sina` → `daily_bar`（`src=akshare`，未复权拼接价）。
- 校准：tqsdk 补缺 + 二次比对，异常开 `anomaly_ticket`（不阻塞主源）。
- **频率**：每日三次 cron —— `12:00`(noon) / `16:00`(close) / 次日 `08:00`(night)。
- 增量幂等：`(symbol, trade_date)` upsert，断点可续；范围 `2015-01-01` 起。

#### 4.3.2 小时线更新（★逐根增量更新 —— 用户硬性要求）
> **需求：小时线更新必须"每完成一根小时线，更新一根"**，而非定时批量重抓整段。

**新增机制 `HourlyBarIncrementalUpdater`（建议扩展 `app/ingest/hourly_collector.py`）：**
- **触发**：在每个交易时段内的小时 K 线**收盘时刻之后**增量采集。实现二选一（推荐组合）：
  - (a) **分钟级 cron**：在交易所小时线收盘时点后 1 分钟触发（如 `:01`/`:31`/`:46`，覆盖白盘 10:15/11:30/13:30/14:15/15:00、夜盘 21:00/22:00/23:00/23:30/01:00/02:00/02:30 等，由配置化的收盘时刻表驱动）；
  - (b) **轮询兜底**：每 5 分钟检测"比本品种上次成功更新更晚收盘的 bar"，补采遗漏。
- **行为**：只抓取「自上次成功更新以来**新收盘**的小时 K」，**逐根 upsert** 到 `hourly_bar`（主键 `(symbol, trade_datetime)` 覆盖）—— 即"完成一根、更新一根"，满足实时性。
- **定稿守卫**：对最新一根（距现在 < `settle_delay_sec=150s`）等待定稿后重采一次覆盖（复用 `_collect_hourly_settled` 逻辑，防新浪收盘瞬间过渡值致 JD 案例式反手）。
- **主源/兜底**：akshare 新浪 60 分钟主源；失败/限流 → tqsdk `get_kline_serial` 兜底（全品种**复用单 TqApi 连接**，避免 50× 登录超时）。
- **断点续传**：记录每个品种最后更新时间；akshare 仅近期窗口、历史回填用 `--prefer tqsdk` 并调大 `data_length`。
- **与既有机制互补（不冲突）**：保留 ① `16:00` close 档联动的**全量快照** `collect_all()`（对齐/校准用）、② 融合扫描每 15 分钟尾部 800 根**刷新**（容错/补齐）。逐根增量是"准实时主通道"，批量是"兜底校准通道"。

#### 4.3.3 回测底座 `fut_kline` 更新（路径 B/C + 聚合，非 scheduler 自动）
- `continuous`/`contract`：tqsdk `fetch_fdf.py` → `db_pg.save_bars`。
- `cont_adj`：`build_continuous.py` + `adjust_fdf.py` 重建（**先 `delete_bars` 清旧再插**，防脏累积）。
- `daily`（回测用）：由 hourly 聚合（`_build_daily_*.py`，含夜盘归属修正 ≥20:00 归下一交易日）。
- **触发**：手动 / CI（容器 `RUN_ON_BOOT=0`，避免重启重复全量回补）。数据更新后应重跑路径 B/C + 日线聚合，再跑回测。

#### 4.3.4 周/月维护（scheduler）
| 时间 | 任务 |
|---|---|
| 周六 06:00 | LSTM 周重训 |
| 周六 06:30 | 传导权重周重算（corr/granger/te/var） |
| 周六 07:00 | M4 周度回测 |
| 每月 1 日 06:30 | 模型权重月更（近 60 日准确率） |

#### 4.3.5 一致性红线（口径铁律，两边都必须遵守）
1. **单源消费**：信号引擎按 `fusion.hourly_src`（默认 `akshare`）读 `hourly_bar` 唯一口径；回测按 `(freq,kind,symbol,src)` 单源读 `fut_kline`。**杜绝双源混读**（akshare 结束时刻标 vs tqsdk 起点标会被当成两根喂指标，砍半周期）。
2. **剔除未收盘 K**：`drop_forming_bars` 丢弃 `now < dt` 的正在形成 K。
3. **定稿守卫**：`drop_unsettled` 剔除收盘距现在不足 `settle_delay_sec` 秒的最新一根。
4. **复权唯一权威**：回测复权价只取 `fut_kline.cont_adj`；`daily_bar`/`hourly_bar` 的 akshare 主连**未复权**。
5. **涨跌口径统一 `close`**（非 settle），见 §5.4 / §17。
6. **标签完整**：任何数据源入库带 `src`；回测/信号指定 `src` 读取。

### 4.4 复权标准（加法平移前复权）
- 主连换月仅一处「新旧合约价差」断层，正确修复是「把断层之前全部历史整体平移 delta」。
- 算法：`pick_dominant`（日持仓量最大为主力，需 `ROLL_CONFIRM=3` 日领先确认、只向前换）→ `delta=新(t-1)收−旧(t-1)收` → 前复权 `t` 前全部 `+=delta`；剔除未走完 K 与主力持仓不达标（`RELIABLE_OI=100000`、`RELIABLE_HOLD=20`）早期区间；`adj` 列标记。
- 口径归口：`fut_kline.cont_adj` 唯一权威；`main_continuous.adj_*` 另存主连复权裸价供线上特征。

### 4.5 字段统一约定速查
| 项 | 约定 |
|---|---|
| 品种规格 | `fdf/symbols.json`（`code_digits`/`multiplier`/`tick`/`months`） |
| 交易所代码 | CZCE(大写)/SHFE/DCE/INE/CFFEX/GFEX(小写品种码) |
| 合约码位数 | 郑商所 3 位（FG609），其余 4 位（rb2601） |
| 时间 | `trade_datetime` = 北京时间墙钟的 K 线**收盘时刻** |
| 价格精度 | `NUMERIC(20,4)`；展示按 `_TICKS` 最小变动价位对齐 |

---

## 5. 预测引擎（CB 预测系统 · 摘要 + 锁定结论）

> 详细模型实现见旧 PRD《期货预测平台需求文档_PRD.md》§5/§16/§17/§18。本节保留**对编码有约束力的锁定裁决**，CB 照此实现，WB 照此复审。

### 5.1 特征工程
输入窗口：当日涨跌幅 + 前 5/20 根 K 涨跌幅（以"根数"表达，见 §4.3 频率无关）；波动率、动量、Hurst、样本熵、ATR；Zigzag 拐点、Qi(ATR+MACD) 辅助。

### 5.2 模型库（按族）
变换类（傅里叶/小波）、统计时序（ARIMA/SARIMA/GARCH/卡尔曼）、状态概率（马尔可夫/HMM/贝叶斯/蒙特卡洛）、ML/DL（LSTM/GRU/Transformer/XGBoost/LightGBM/RF/GPR）、复杂系统（Hurst/样本熵/相空间重构）。

### 5.3 集成机制
- **状态门控**：Hurst 判定 trend/mean_revert 选择启用子集。
- **方向投票**：加权投票（权重每月按近 60 日回测准确率更新，⑳）；起步涨/跌二分类。
- **幅度融合**：中位数点估计 + [P5,P95]。
- **输出契约**（v1 JSON，对接简报复用）：`symbol/target_date/as_of/run_id/direction/direction_prob/ret_point/ret_low/ret_high/confidence/participated_models/state`。

### 5.4 锁定裁决（⚠️ 编码必须遵守）
| 项 | 结论 |
|---|---|
| 涨跌口径 | **`close` 收盘价**（§17 已裁决，禁止回到 settle） |
| 反转门控 `gate_pct` | **2.0%**（§18.11 阈值对齐结案；验收只用池化口径，禁止调阈值迎合覆盖率） |
| 覆盖率门槛 | ≥15% 为**结构性未达**已知偏差（K6），维持硬门槛、登记已知偏差 |
| 预测标的 | 仅主力连续合约（FG888 等） |
| 预测频率 | 每次数据更新后预测（一天三次：12:00/16:00/08:00） |
| 简报集成 | `/integrations/*` 与 `briefing_signal` 须显式声明 `caliber="close"` |

### 5.5 跨品种/大类传导（§16，已确认需求）
`sector_map` / `sector_index` / `transmission_weights`；先验+数据驱动（Granger/传递熵/VAR）融合；特征一律滞后防前视；权重每周重算。

---

## 6. 交易策略层（融合策略 V2.0 · 额外执行口径）★ 新增

> 源：`桌面/融合/终版资料/02_融合引擎与额外策略最终版` + `app/strategies/fusion_signal.py`。本层与回测引擎**逐位一致（单一真源）**。

### 6.1 五层结构
```
① 方向层（日线/小时 EMA140）  只做顺势一侧（多或空）
② 入场层（小时线）           A 回踩重启(优先) ∨ B 10根突破(补位)
③ 离场层（小时线·额外口径）   2×ATR 初始硬止损 + 2×ATR 吊灯 + 0.5R 保本；仅小时线收盘击穿才平仓
④ 执行层                    当根收盘成交；离场后冷却 3 根；单品种至多 1 仓
⑤ 风控层                    单笔风险封顶；保证金上限；月度熔断
```
**三条不可移除硬约束**：① 不设固定止盈（靠吊灯让利润奔跑）；② 不设结构止损；③ 移动止损只向有利方向移动，永不回撤。

### 6.2 方向层
- 小时 EMA140 方向（等价日线 EMA20）：`REF(C,1) > REF(EMA(C,140),1)` → 只做多；反之只做空；临界两侧都不做。
- 用**已收盘**小时线确认；指标取 `DIRPREV` = 上一根方向，避免未收盘值（无前视）。

### 6.3 入场层
- **A 回踩重启（优先）**：方向允许多 + `C>EMA20(H1)` + `L≤EMA20` + `C>O 且 C≥(H+L)/2` + 上一根 `C<EMA20`（刚穿越回）。
- **B 10 根突破（补位）**：方向允许多 + `C>前10根最高价(不含当根)` + `C>O`。
- 入场模式参数 `entry_mode ∈ {pullback, breakout, both, both_nm}`（默认 `both_nm`，去 MACD）。

### 6.4 离场层（额外执行口径）
- 初始止损 `init_stop = entry ∓ sl_atr×ATR`（默认 `sl_atr=2.0`）。
- 吊灯止损 `trail_stop = peak/trough ∓ trail_atr×ATR`（默认 `trail_atr=2.0`）。
- 保本 `be_trigger = entry ± be_r×ATR`（默认 `be_r=0.5`）；触发后止损上移至成本价。
- 当前生效止损 `cur_stop = 三者中最紧者`（多取 max / 空取 min）。
- **仅小时线收盘击穿 `cur_stop` 才平仓**（盘中插针无视）。

### 6.5 执行层
- **close-only**：开仓价=开仓根收盘价，与回测"只看收盘价"完全一致；离场后冷却 `cooldown_bars=3`；单品种至多 1 仓。

### 6.6 风控层
- 单笔风险封顶（1R=单笔愿亏）；保证金上限；月度熔断（亏损达阈值停新开）。
- 预热：加载后前 `min_bars=160` 根信号不可用（ATR/EMA 未收敛）。

### 6.7 策略参数表（来自 `config/local.yaml` `fusion.*`，均可经回测面板调参）
| 参数 | 默认 | 含义 |
|---|---|---|
| `ema_k` | 140 | 方向层 EMA 周期 |
| `atr_n` | 14 | ATR 周期 |
| `ma_n` | 20 | 入场均线周期 |
| `sl_atr` | 2.0 | 初始止损倍数 |
| `trail_atr` | 2.0 | 吊灯止损倍数 |
| `be_r` | 0.5 | 保本触发倍数 |
| `W` | 60 | 分型窗口 |
| `entry_mode` | both_nm | 入场模式 |
| `cooldown_bars` | 3 | 离场后冷却 |
| `min_bars` | 160 | 预热根数 |
| `max_bars` | 400 | 实时读取上限 |

---

## 7. 信号推送层 ★ 新增

> 源：`app/scheduler.py`（`_fusion_scan_job` / `_collect_hourly_settled`）+ `app/notify/pushplus.py` + `app/api/routers/fusion.py`。

### 7.1 扫描调度
- 融合策略扫描**每 15 分钟**一次（与每日三次 ingest 并行），由 APScheduler `minute="*/15"` 触发。
- 每轮：① 拉最新小时线（逐根增量 / 尾部 800 根兜底 + 定稿守卫）；② `evaluate_all` 评估全部品种；③ 状态机去重（仅状态变化时推送）；④ 渲染并发送。

### 7.2 状态机去重
- 持久化 `FusionPosition`（symbol, position, entry_price, entry_at, last_stop, last_be_done, signal_at, pushed_at）。
- 冷启动（表空）：以引擎当前状态无条件建基线，再推送上线通知。
- 数据新鲜度：最新 K 距现在 > `stale_minutes=90` 分钟 → 抑制状态变化信号（防过期数据触发）。

### 7.3 推送形态（三段式）
1. 🚨 **新信号**：开/平/反手即时推，附 ⛔止损位 / 🎯保本位（价位按 `_TICKS` 最小变动对齐，如 FG=1、AU=0.02、SC=0.1）。
2. ⚠️ **止损变动**：吊灯止损朝有利方向移动 ≥ `trail_push_atr=0.5×ATR` 补推；首次触发保本补推。
3. 📊 **持仓简报**：盘前（`pre_session_times` 08:45/13:15/20:45）强制推完整清单；交易时段内每 `heartbeat_interval_min=30` 一档（对齐 :00/:30）。
4. ⏳ **待执行复提**：开仓信号在 `signal_repeat_min=120` 分钟内每轮置顶，防漏看。

### 7.4 通道（旁路，失败不阻塞主流程）
- 主通道 **pushplus**（微信）：`POST https://www.pushplus.plus/send`，`template=markdown`，失败自动重试 1 次。
- 备用通道：通用 webhook（企微/钉钉/飞书），按 URL 域名自动选载荷格式；主通道失败降级。
- 留痕：`FusionPushLog`（pushed_at/kind/title/content/n_signals/n_rows/delivered/via），可回查历史信号。

### 7.5 推送窗口 / 日历
- `push_windows`：08:45-12:00 / 13:15-15:30 / 20:45-23:30；窗口外整轮冻结。
- 非交易日（周末 + akshare 交易日历缓存，超 7 天刷新）静默。

---

## 8. 回测与研究模块 ★ 新增（纳入系统）

> 源：`桌面/融合/终版资料/04_一百二十万回测方法维度步骤` + `runtime/_bt_fusion.py` / `_bt_multi.py` / `_bt_fusion_timeweight.py`。本模块把"手动调参回测 + 鲁棒性测试 + walk-forward"做成系统内功能（CB 编码、WB 复审）。

### 8.1 定位
- 用户可在 UI/API **手动调整策略与组合参数**，即时跑回测，对比不同参数下的净¥/PF/回撤/胜率。
- 内置 **鲁棒性测试**（四维矩阵 × 多 seed）与 **walk-forward 测试**（滚动窗口重估），验证策略稳健性。

### 8.2 两层回测架构（与 `fusion_state_detail` 单一真源）
- **信号层（逐品种前向循环）**：逻辑与 `fusion_signal.fusion_state_detail` **逐位一致**（EMA140 方向 → MA20 回踩/突破 → 2×ATR 止损/吊灯/0.5R 保本 → close-only）。输出每笔 `(entry_i, entry_px, entry_atr, dir, exit_i, exit_px, R)`。
- **组合层（时间轴 FIFO 重放）**：把所有品种成交事件按 `entry_dt/exit_dt` 排序重放；闸门 `max_positions`（默认 5）、各品种 `lots=1`；净金额 `Y = R×entry_atr×multiplier − 往返成本(bp)`。
- **铁律**：引擎 `fusion_signal` 与回测 `bt_symbol` 必须同步改，改策略后两处都改并跑回测确认口径未分叉。

### 8.3 四维测试矩阵（鲁棒性）
| 维度 | 取值 |
|---|---|
| ① 数据源 `src` | tqsdk / akshare / csv |
| ② 容量 `max_pos` | 1 / 2 / 3 / 5 仓 |
| ③ 成本 `cost_bp` | 5 / 10 / 20 / 30 / 50 bp |
| ④ 时段权重 | 不加权(p=1) / 加权(tqsdk 分时段期望) |
交叉组合最多 120 组，UI 可勾选批量跑。

### 8.4 可调参数面板（手动调参）
| 类别 | 参数 |
|---|---|
| 策略 | `sl_atr` / `trail_atr` / `be_r` / `entry_mode` / `W` / `ema_k` / `atr_n` / `ma_n` / `cooldown_bars` |
| 组合 | `max_positions` / `lots` / `cost_bp` / `seeds` |
| 数据 | `src` / `freq` / `kind` / `symbols` / `min_year` / `limit` |
| 加权 | `N_MIN` / 概率映射 |

### 8.5 walk-forward 设计
- **信号层**：按时间切滚动窗口（训练/评估不泄漏），定期用滚动窗口重估参数（与 §4.3.4 LSTM 周训/权重月更对齐）。
- **组合层**：滚动 FIFO 重放，输出分年度/分时段净值曲线。
- 机制性消除前视：评估点 `step` 抽样、参数估计只用 `as_of` 之前数据。

### 8.6 鲁棒性测试
- 多 `seed`（`seeds=30~60`）取均值±标准差，检验随机性敏感。
- 分年度 / 分品种拆解；成本盈亏平衡点扫描（已知 ~20–30bp）。
- 头部品种（j/au/ag/lh/jm）专项（原 +120 万结论中贡献毛利 44.5%）。

### 8.7 指标与输出
- 均 R、净 R、净 ¥、盈利因子 PF、胜率、最大回撤（R 与 ¥ 双口径）、覆盖率-准确率曲线。
- 每次回测 JSON 落 `bt_out/`，命名带 `tag`（日期/数据版本），横向可对比。
- **必须标注口径**：所用 `(freq,kind,src)`、品种窗口、成本 —— 不同口径结论可差 5~30 倍。

### 8.8 API / 前端（纳入系统）
- `POST /backtest/run`：提交参数（8.4 面板），返回 `task_id`。
- `GET /backtest/result/{id}`：结果（汇总 + 分品种 + 分年度 + 曲线）。
- `GET /backtest/matrix`：四维矩阵对比表。
- 前端「回测调参」页：参数表单 + 运行 + 结果对比表/图（PF/净¥/回撤随参数热力图）。

---

## 9. 服务层 API（合并）

| 方法 | 路径 | 说明 | 归属 |
|---|---|---|---|
| GET | /symbols | 品种列表 | 共用 |
| GET | /bars/{symbol}?freq= | 行情查询（daily/hourly） | 数据 |
| POST | /ingest | 手动更新（指定品种/全量） | 数据 |
| GET/POST | /integrations/* | 简报引擎对接 | 预测 |
| POST | /predict | /predict/batch | 预测 | 预测 |
| POST | /backtest/run | GET /backtest/result/{id} | /backtest/matrix | 回测 | 回测 |
| GET | /signals/latest | /signals/history | 当前/历史融合信号 | 信号 |
| GET | /anomalies | /anomalies/{id}/resolve | 异常工单 | 数据 |
| GET | /tasks/{id} | 任务状态 | 共用 |

---

## 10. 展示层（看板）

React 18 + Vite + TS + AntD + ECharts，涨跌配色**涨红跌绿**。
- **品种总览**：最新价、N 日涨跌、次日预测方向。
- **单品种预测详情**：K 线 + 方向概率/幅度区间/参与模型/特征/三次预测对比。
- **全市场热力图**：涨跌概率分布 + 大类强弱排名 + 传导来源卡片。
- **信号面板**：实时持仓（多红/空绿）、开/平/反手记录、止损/保本位、推送日志。
- **回测调参面板**：参数表单 + 运行 + 结果对比表/曲线（PF/净¥/回撤热力图）。
- **数据质量面板**：异常工单 + 裁决 + 校准日志 + 数据完整性自检。

---

## 11. 部署与运维（本地 Docker）

- 三容器：`timescaledb` + `python-api(含 scheduler)` + `web(nginx)`。
- 配置：token/更新时刻/校准阈值/模型权重 → `config/{local,cloud}.yaml` + `.env`（密钥不进 git）。
- 资源：日常 4 核/8GB/50GB SSD；深度重训 8 核/16GB。
- 启动前提：Docker Desktop 运行（手动开）。

---

## 12. 里程碑（合并编排）

| 阶段 | 内容 | 归属 |
|---|---|---|
| M1 | 数据层 + 日线获取与更新（每日三次） | CB |
| M2 | 特征 + 单模型预测 | CB |
| M3 | 全模型库 + 集成 + 传导 | CB |
| M4 | 回测引擎（walk-forward 机制） | CB |
| M5 | React 看板 + 简报对接 | CB |
| M6 | 小时线扩展 + 因子（库存/龙虎榜/carry） | CB |
| **M6x** | **交易策略融合引擎 + 信号推送 + 小时线逐根增量更新（§4.3.2/§6/§7）** | **WB** |
| **M8** | **回测与研究模块 UI/API（手动调参/鲁棒性/walk-forward，§8）** | **WB+CB** |
| M7 | 云端部署演进 | 共用 |

> M6x 与 M8 为本次统一 PRD 新增阶段，优先级高（用户当前重点）。

---

## 13. 待裁定 / 已知问题

| # | 事项 | 优先级 | 归属 |
|---|---|---|---|
| 4.1 | 复权 OI 门槛：低流动性品种（sc/ru/sn/pb）如何复权 | 高 | CB 主+WB |
| 4.2 | 复权是否接入自动调度 | 高 | 共担 |
| 4.3 | 权威数据口径（akshare 未复权 vs 天勤复权） | 高 | 共担 |
| 4.5 | 陈旧 cont_adj 全量重跑（57 品种） | 中 | CB |
| K1 | 龙虎榜 SHFE/DCE 接口坏（覆盖 25/73） | Open | CB |
| K4 | INE 无持仓排名/库存接口（硬上限 ~68/73） | Open | CB |
| K6 | 反转覆盖率 ≥15% 结构性未达（已知偏差） | Accepted | CB |

---

## 14. WorkBuddy 复审要点（供 CB 交付后复审）

WB 在 CB 完成编码后将按以下清单复审，**任一不符即退回**：

1. **单一真源**：`fusion_signal.fusion_state_detail` 与回测 `bt_symbol` 逻辑逐位一致；改其一必须同步另一并跑回测。
2. **口径铁律**：信号只读 `hourly_bar` 单一 `src`；回测只读 `fut_kline.cont_adj`；无双源混读；涨跌 `close` 口径。
3. **小时线逐根增量**：§4.3.2 的"完成一根更新一根"已实现且带定稿守卫；既有批量兜底不冲突。
4. **定稿守卫**：最新 K < `settle_delay_sec=150s` 时等待重采覆盖（JD 案例防反手）。
5. **推送窗口/日历**：非交易日/非窗口整轮冻结；价位按 `_TICKS` 对齐。
6. **回测可复现**：参数面板改动可一键复跑；输出带 `(freq,kind,src)` 口径标注；四维矩阵与多 seed 可用。
7. **不入库**：`.env`/`config/local.yaml`/`pgdata/`/`runtime/`/根目录临时文件均被 `.gitignore` 排除。
8. **git 基线**：双方改动已提交 baseline，分支开发，无互相覆盖。

---

*本 PRD v2.0.0（2026-09-16 统一版）合并自 CB 预测系统 PRD v1.3.5 与 WB 融合/信号/回测补充；预测引擎深度模型细节沿用旧 PRD §5/§16/§17/§18，本版补齐交易策略判定、信号推送、统一数据标准（含小时线逐根增量更新）、回测与研究模块。*
