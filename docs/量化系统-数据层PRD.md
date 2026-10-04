# 量化系统 · 数据层 PRD（数据层模块化重构）

> 版本：v1.5（草稿，待 CB 改造）  
> 制定日期：2026-10-03（v1.0）；v1.1 补充：2026-10-03；v1.2 补充：2026-10-03；v1.3 补充：2026-10-03；v1.4 补充：2026-10-03；v1.5 补充：2026-10-03（专业评审补强） 
> 制定人：WorkBuddy（小齐）  
> 定位：本文件是 qhyc 系统**数据层**的权威需求文档，作为后续 CodeBuddy（CB）改造的单一真源。  
> 上层（因子层 / 回测层 / 策略层 / 应用层）**只消费本层接口，不直接触达底层表**——这是本 PRD 的硬约束。  
> **v1.1 变更**：① 新增 §6.4「数据源收敛：去除天勤(天勤)兜底，统一 akshare」（基于全库 天勤 引用排查）；② 扩充 §6.2「后复权存 offset 对回测的影响与双轨落地设计」；③ 全部需改造点以 `【CB 改造点 T#/B#】` 标注，并在 §14 汇总索引。  
> **v1.2 变更**：④ 新增 §6.5「交易日历与换月治理」（来自用户提供的外部参考文章），改造点以 `C1–C8` 标注并汇总于 §14.3；⑤ §3.1 校正：**不得新建 `cfg_symbol_fee`**（与既有 `dim_trading_cost` 重复造表，见 C8）、§3.4 说明与既有成本表的关系；⑥ §1.2 新增痛点 **P5**（交易日历推断模式 + 交割月护栏忽略节假日）。
> **v1.3 变更**：⑦ 三项用户拍板决策落地（原 §12 拍板项 ①②③，对应 R1/R2 与 C1 源选型）：**DEFAULT_CALIBER 改为后复权口径**（新增【CB 改造点 B9】）；**5 分钟复权序列扩展落地**（B9 含 5m 后复权序列生成与验收）；**交易日历官方源选型 = akshare 接口**（C1 落地数据源明确为 `tool_trade_date_hist_sina` / `option_trade_time_em`，§6.5.3）。同步修正 §9 / Phase A 中与 C8 矛盾的 `cfg_symbol_fee` 残留。  
> **v1.5 变更**：⑪ 专业评审补强（dev-expert 视角，多维度）：新增 §14.4「G 系列 · 评审补强改造点」（G1–G13）与 §15「评审结论与 CB 行动指引」；核心补强 = **G1 单一 akshare 源 SPOF 韧性层**（去天勤后摘掉 天勤 比对层，须配套重试/缓存/新鲜度 SLA/告警）、**G2 两套换月定义未对齐（main_contract_map.change_flag vs roll_segment 双门）须统一真源**、**G3 B9 默认 cont_adj 与 B2 缺日/小时段自相矛盾**、**G4 C1 日历源误用股票日历（tool_trade_date_hist_sina）须改期货专属 futures_rule**、**G5 B8 运行时 as-of join 不可规模化**、**G6 sp_build_l* 幂等 + 原子 swap**、**G7 采集源 Provider 抽象**、**G8 CI 门禁 + Golden Dataset 测试金字塔**。详细论证见配套文档 `数据层PRD_专业评审与疏漏补强.md`。⑧ 前复权链退役验证 + R3/R12 复验结论落位（R12 由 P0 降级为 P2 非缺陷，引擎正确）；⑨ 用户专项"时区问题"排查 → 新增 **§6.6 时区治理**：TZ-1(=R7/T4, sina +8h 标签偏移)、TZ-2(R13/T10, naive utcnow 写 tz 列偏 −8h)、TZ-3(R14/T11, 会话时区未钉死)、TZ-4(已排除, offset 关联时区安全)；⑩ **本轮回测校正 R12 两处误报**：负价品种实为 6 个（BC/CY/FB/RR/RS/WR 而非 CJ/CY/FB/JD/LH/RR/RU）、5m 复权无缺口（`bar_5m` 实测 2462 万行 raw + `roll_segment(min5)` 2911 段已存在，B9 由 `BarStore` 运行时套 offset 即可）。

---

## 0. 文档目的与范围

### 0.1 为什么要写这份 PRD

当前 qhyc 的数据处理不是分层架构，而是**"数据库为中心的放射状架构"**：采集、特征、预测、回测、策略、API 全部直接写 SQL 连同一批行情/基本面表，彼此通过列名约定（无外键）隐式耦合。这导致一个**天然的数据层瓶颈**，直接拖慢三条主线：

| 受影响主线 | 当前痛点（数据层导致） |
|---|---|
| **策略落地** | 信号计算与行情取数混在一起；换月、复权、命名空间问题藏在 `ingest` 下，策略层无法稳定拿到"口径明确"的序列 |
| **回测** | 同一品种存在 `continuous` / `cont_adj` / `contract` 三份、双命名空间（`FG888` vs `KQ.m@CZCE.FG`），回测口径与实盘口径对不上；换月跳空产生"假盈亏"（实测中位 4.6 个 ATR） |
| **实盘** | 研究用主连、执行用具体合约未系统分离；`underlying_symbol` 映射、移仓逻辑散落，回测与实盘持仓对不齐 |

### 0.2 范围

- **包含**：数据层自身的四大模块——① 配置管理、② 合约管理、③ 基础数据（L0）、④ 清洗数据（L1/L2/L3），以及它们的表结构、数据流向、对外接口、验收标准、迁移路线。
- **不包含**：因子计算逻辑、回测引擎实现、策略信号逻辑、前端展示。这些上层**只通过 `BarStore` / `FundamentalsStore` 接口消费数据**，其改造不在本文件内，但须遵守本文件第 8 节的接口契约。

### 0.3 与既有文档的关系

本文件**整合并提升**以下既有文档的结论，改造时以本文件为准，必要时回查：

| 既有文档 | 在本 PRD 中的落位 |
|---|---|
| `现状梳理_06_六层解耦迁移建议_20260928` | 第 2 节总分层、第 8 节接口契约的来源 |
| `现状梳理_01_数据库表清单与逻辑关系_20260928` | 第 1 节现状诊断的数据证据 |
| `数据库分层与字典/配置表设计_20260930` | 第 3/4/5/6/9 节表设计的直接基础（dim_* / cfg_* / L0-L2 存储过程） |
| `数据层复权架构裁定_20260930` | 第 6.2 节复权方案（存 offset 而非结果）的裁定依据 |
| `合约代码统一规范_20260920` | 第 4 节合约管理 / 代码归一的最终规范 |
| `前复权链退役执行报告_20261003`、`口径修复方案_20261003` | 第 6.2 / 12 节前复权退役与口径一致性的执行依据 |
| 全库 天勤 引用排查结论（2026-10-03） | 第 6.4 节数据源收敛（去天勤、统一 akshare）的依据 |
| 用户提供的《期货连续性回测：交易日历与主力切换》参考文章（截图，2026-10-03） | 第 6.5 节交易日历与换月治理（C 系列）、第 1.2 节 P5 的来源 |

---

## 1. 现状与问题诊断

### 1.1 当前数据架构的三个结构性缺陷

基于 `现状梳理_01` 的 40 张业务表清单盘点（2026-09-28）：

1. **零业务外键**：所有"关联"靠列名约定（`symbol` / `trade_date` / `run_id`），是 join 不上、对不齐的根因。
2. **同一逻辑对象多份物理存储**：同一段 FG 行情同时存在于 `fut_kline` / `bar_15m` / `daily_bar` / `hourly_bar` / `main_continuous`，各份口径与 symbol 命名空间不同。
3. **双 symbol 命名空间冲突**：`FG888`（主连）vs `KQ.m@CZCE.FG`（天勤原生），后者残留 18,866 行，与前者重复，跨周期 join 实测失败。

### 1.2 数据层四大痛点（影响策略/回测/实盘）

| # | 痛点 | 现状证据 | 下游影响 |
|---|---|---|---|
| P1 | **口径混乱** | `fut_kline` 三维 `freq×kind`（contract/continuous/cont_adj）；`daily/hourly` 又用 akshare 主源；PRD 规定回测用 `continuous`、统计用 `cont_adj`，代码混读 | 回测与实盘价格域不一致；"假盈亏"未被计量 |
| P2 | **换月跳空未系统处理** | 换月跳空中位 = 3.1~6.9 个 ATR14（中位约 4.6）；84 品种 × 年均 2 次 ≈ 每年 170 次换月事件，每次记一笔"不存在的盈亏" | 回测收益虚高/失真，策略过拟合风险 |
| P3 | **合约/代码不统一** | 郑商所 3 位码（`AP701`）、上期/大商小写（`cu2611`/`a2601`）、天勤 `CZCE.AP701`；跨源 join 全部对不上，因子序列被拆成稀疏两条 | 因子失真、跨源比对失效 |
| P4 | **配置/字典散落代码** | 手续费、保证金、交易时间、节假日靠 env / 代码常量 / 各采集器内 try-except；功能开关散落 scheduler | 成本模型不可复现；新数据源接入成本高；开关改动需改代码重启 |
| P5 | **交易日历不权威 + 换月治理缺失**（v1.2） | `trade_calendar` 多为 `exchange='INF'` **推断模式**（`calendar_infer.py` 按当日 ≥80% 主连品种有数据反推），`sessions` 写入为 `None`；`roll_policy._last_trading_day()` 推算交割月第 10 个交易日时**只跳周末、忽略节假日**；换月仅有 `change_flag` 布尔，无窗口归因、无切换日志 | 交割月护栏窗口在连续休假附近系统性偏移；回测看似顺滑，但换月窗口的收益/滑点不可解释，无法判定策略是否依赖换月噪声 |

### 1.3 云地数据差异（改造前必须先认清的约束）

`现状梳理_01 §5` 实测：本地无 1 分钟、5 分钟数据；`fut_kline` 的 min15/30/60 `continuous` 本地几乎为空（min15 continuous 仅 1 符号）；`member_position_rank` 云端回溯到 2015、本地仅 2023。

> **改造铁律（沿用）**：本地设计、云端执行采集与回灌；本地 → git → 云端 单向同步；防不可复现（脚本版本 / 数据快照 / 方法口径三问）。

---

## 2. 目标架构：数据层四大模块

```
┌──────────────────────────────────────────────────────────────────────┐
│                      上层消费方（因子/回测/策略/应用）                    │
│  只通过 BarStore.load(symbol, freq, caliber) / FundamentalsStore 取数    │
└───────────────────────────────────▲──────────────────────────────────┘
                                     │ 口径明确的 DataFrame
┌───────────────────────────────────┴──────────────────────────────────┐
│  模块④ 清洗数据（L1 / L2 / L3）   ★上层唯一直接出口★                      │
│  L1 归一化棒 bar_*（单命名空间，未复权）                                 │
│  L2 复权 offset roll_segment（后复权，只追加）                            │
│  L3 口径注册表 + 连续序列物化 + 主连映射 + 质量门控                        │
└───────────────────────────────────▲──────────────────────────────────┘
                                     │ 存储过程 sp_build_l*
┌───────────────────────────────────┴──────────────────────────────────┐
│  模块③ 基础数据 L0（原始采集，只入库备份不删）                            │
│  fut_kline(contract，akshare 派生) / daily_bar / hourly_bar / minute_bar(预留) │
│  基本面原始表：member_position_rank / spot_basis / warehouse_receipt /   │
│  inventory / roll_yield / contract_daily / macro_china                  │
└───────────────────────────────────▲──────────────────────────────────┘
                                     │ 采集器只写 raw（仅 akshare，见 §6.4）
┌───────────────────────────────────┴──────────────────────────────────┐
│  模块② 合约管理（dim / contract）                                       │
│  dim_exchange → dim_variety → dim_contract → main_contract_map          │
│  + 代码归一 symbol_code（屏蔽 888/8888/KQ.m@）                           │
└───────────────────────────────────▲──────────────────────────────────┘
                                     │ 被 L0/L1/L2 引用、被上层查询
┌───────────────────────────────────┴──────────────────────────────────┐
│  模块① 配置管理（cfg / 字典）  数据库控制，非代码                         │
│  交易时间/夜盘归属(cfg_trading_session) · 节假日(cfg_holiday)             │
│  主力切换规则版本(cfg_main_switch_rule) · 风控窗口(cfg_risk_window)       │
│  合约与代码对照(contract_code_map) · 手续费(dim_trading_cost)           │
│  保证金(cfg_symbol_margin) · 功能开关(cfg_feature_switch)               │
└──────────────────────────────────────────────────────────────────────┘
```

**分层契约**：

- **模块①/② 是元数据/字典层**：被采集、清洗、上层共同引用，自身不持有时序行情。
- **模块③ L0 = 输入**：只入库保存、定期备份、不删除（`write_once_backup`）。
- **模块④ L1/L2/L3 由存储过程产生**：L1 只增改、L2 只追加、L3 为服务/治理层。
- **上层只能碰模块④的对外接口**，模块①②③的表除"写入方"（采集器、存储过程、ORM、migrations）外，业务读方一律经接口。
- **数据源收敛（v1.1 新增决策）**：L0 采集**只保留 akshare**，天勤 天勤 兜底移除（详见 §6.4）；`fut_kline` 由 akshare 权威分钟源派生，不再 天勤 直采。

---

## 3. 模块①：配置管理（cfg / 字典）

> 职责：把"写死在代码/env 里的配置"收口到数据库，使**功能开关、交易时间、节假日、手续费、保证金、合约-代码对照**成为可审计、可热更的字典数据。上层与成本模型、K 线对齐、夜盘归属全部从这里读。

### 3.1 表清单

| 表 | 作用 | 关键列 | 备注 |
|---|---|---|---|
| `cfg_feature_switch` | **功能开关**（替代 env/代码常量） | switch_key(PK) / enabled / value / switch_group | 已有（008）。启停功能只 UPDATE，不改代码/重启冷路径 |
| `cfg_trading_session` | 交易时间段（日盘/夜盘起止） | exchange / variety_or_all / session_type(day/night) / start_time / end_time / next_day_flag | **新增**。夜盘归属与 K 线对齐的唯一权威 |
| `cfg_holiday` | 休市/节假日日历 | trade_date(PK) / is_trading_day / note | 扩展现有 `trade_calendar` 或独立；用于跳过非交易日、判定"下一交易日" |
| `contract_code_map` | **合约与代码对照表**（各源原生 ↔ 标准码） | (exchange, std_symbol, version) PK / official_symbol / sina_symbol / 天勤_symbol / cfg_native_symbol / product / deliv_year / deliv_month | 已有（14_contract_code.sql）。跨源 join 桥梁；去天勤后 `天勤_symbol` 迁为只读/`cfg_native_symbol`（见 §6.4 T8） |
| `cfg_symbol_fee` | **手续费记录 —— ⚠️ v1.2 校正：不得另建，改为封装既有 `dim_trading_cost`（C8）** | symbol_or_variety(PK) / fee_mode(by_volume\|by_amount) / fee_value / today_multiplier(平今加倍) / min_fee / src | **【CB 改造点 C8】禁止新建第二份成本真源**；既有 `dim_trading_cost`（迁移 013/014：三动作 OPEN/CLOSE_YEST/CLOSE_TODAY × 费率 FIXED/PCT × 范围 CONTRACTS/MONTHS/ALL × 生效期 `effective_from/effective_to` × `slip_ticks`，`app/data/cost.py` 未知即抛 `CostNotFoundError`）已是唯一权威，本行应改为其**接口封装/命名统一**，不新建表 |
| `cfg_symbol_margin` | **保证金记录** | symbol_or_variety(PK) / exchange_margin_ratio / broker_margin_ratio / multiplier(合约乘数) / tick_value / src | **新增**（仅保证金部分；合约乘数 / tick 价值已在 `dim_variety` / `futures_symbol`，勿重复建列） |
| `cfg_basic_indicator` | 基本面/仓单指标字典 | indicator_code(PK) / src_table / frequency / unit / layer | 已有（008） |

### 3.2 设计要点

- **手续费分品种、平今加倍**：国内商品期货手续费分"按手数"与"按成交额万分比"两类，且存在"平今加倍"。既有 `dim_trading_cost` 的 **三动作（OPEN / CLOSE_YEST / CLOSE_TODAY）× 费率（FIXED/PCT）× 生效期** 已完整建模平今加倍（平今=独立动作行不同费率，如苹果开 5 元 / 平今 20 元）；回测须显式传 `on_date` 取历史费率，否则日内策略回测成本严重失真（参考回测精度实测：统一费率下日内策略回测偏差极大）。
- **滑点安全垫**：成本模型以 `dim_trading_cost` 为**唯一真源**，回测滑点设置应**至少比实盘均值高 30%~50%**，给执行偏差留缓冲（同上参考）。
- **交易时间/夜盘归属**：`cfg_trading_session` 解决"夜盘 K 线归属"。规则：`hour >= 夜盘开始` 的 K 线归属**下一交易日**（如 21:00–23:00 归次日）。这是 K 线开盘/收盘/高低点正确的前提，任何品种都要逐时间戳核对（参考：铜、镍等夜盘活跃品种归属错则走势差之千里）。
- **合约乘数 / tick 价值**：`cfg_symbol_margin.multiplier` / `tick_value` 是"点数 → 金额（¥ = R × risk × mult − 往返成本）"换算的唯一来源，回测层统一从此读取。
- **功能开关热更**：沿用 `app/core/feature_switch.py`，带 60s TTL 缓存与"表缺失回退 default"容错；代码侧读取，运维只改库。

### 3.3 维护与数据源

- `cfg_feature_switch` 种子与现状 env 实际生效值对齐（迁移即行为不变）。
- `dim_trading_cost`（成本，既有，C8 禁止另建 `cfg_symbol_fee`）/ `cfg_symbol_margin`（保证金，新增）初始来自交易所官网 + 期货公司标准，人工 seed 后由运维按调整周期 UPDATE。
- `contract_code_map` 由 `app.ingest.contract_code build/normalize` 从库内实际 symbol 反推，新增品种收尾 `--rebuild-map`。

### 3.4 与既有 `dim_trading_cost` 的关系（v1.2 重要校正）

- **成本字典已经存在，且比本 PRD v1.0 的设想更完善**：`dim_trading_cost`（迁移 013/014）= 三动作（开 / 平昨 / 平今，例：苹果开仓 5 元 vs 平今 20 元）× 两类费率（FIXED 元/手、PCT ‰）× 三层范围（CONTRACTS > MONTHS > ALL，取最具体一条）× **生效期**（`effective_from/effective_to`，支持历史费率回算，回测必须显式传 `on_date`）× `slip_ticks`；`app/data/cost.py` 是唯一读取入口，且**未知即抛 `CostNotFoundError`，绝不当 0 成本静默计算**。
- 因此 **§3.1 的 `cfg_symbol_fee` 应取消"新建"**，改为对 `dim_trading_cost` 的**命名统一 / 接口封装**；模块①真正需要新建的只有**保证金**（`cfg_symbol_margin`）。
- 分工：`dim_trading_cost` 保留在**成本域**（含滑点、生效期、范围），模块①只负责"谁是权威、谁可 UPDATE"的注册与只读暴露（登记进 `data_layer_catalog`，或由 `cfg_feature_switch` 管辖开关）。

> **【CB 改造点 C8】** 禁止新建 `cfg_symbol_fee` 造成**双份成本真源**；成本统一到既有 `dim_trading_cost`（含生效日），模块①仅新增 `cfg_symbol_margin` 保证金字典与 `cfg_risk_window`（见 §6.5）。

---

## 4. 模块②：合约管理（dim / contract）

> 职责：建立**唯一权威**的合约/品种命名空间，屏蔽 `888`/`8888`/`KQ.m@` 等多形态，承载合约生命周期（上市/交割/是否主力/所属主连）与"研究主连 → 执行具体合约"的映射。

### 4.1 表清单

| 表 | 主键 | 作用 | 备注 |
|---|---|---|---|
| `dim_exchange` | exchange_code | 交易所字典（6 所静态种子） | 静态 |
| `dim_variety` | variety_code | 品种字典（tick / multiplier / main_symbol / trade_unit / name） | 由 `futures_symbol` + `dim_symbol` 回填 |
| `dim_contract` | contract_code | **合约配置表**：上市/交割起止、是否主力、所属主连 | **核心新增**。`list_date/first_trade_date/last_trade_date`、`delivery_year/month`、`main_symbol`(如 CU888)、`is_main`、`is_active` |
| `main_contract_map` | (variety, trade_date) | **主连 → 具体合约每日映射**（信号序列 → 执行序列） | 已有，承载换月与执行映射 |
| `contract_code_map` | — | 见模块①（代码对照，桥接跨源） | 跨源原生码 ↔ 标准码 |
| `v_symbol_canonical` / `v_variety_contracts` | 视图 | 单命名空间出口、品种→合约族概览 | 已有/新增视图 |

### 4.2 合约生命周期与主力判定

- **上市/交割窗口**：`dim_contract` 固化每个合约的 `list_date` ~ `last_trade_date`，是"该合约在库数据日期范围"与"是否可交易"的权威判断。
- **主力判定规则**（参考连续合约拼接实战）：以**持仓量**为信号（比成交量稳定），**单向不可逆 + T+1 生效**——t1 天新合约持仓量超越旧合约触发换月信号，t1+1 天正式切换（t1 当天交易已完成，不可立即切）。结果写入 `main_contract_map`。
- **主连（研究）vs 具体合约（执行）分离**（参考主连/指数/具体合约分工）：
  - **信号/研究序列**用主连（连续）算均线、突破等；
  - **执行序列**必须落到有流动性的具体合约，通过 `main_contract_map.main_symbol` 或 quote 的 `underlying_symbol` 映射；
  - 换月时策略必须显式移仓（平旧月、开新月），不能假设主连 symbol 不变。
  - 此分离直接消解"回测订主连、实盘对不上"的根因。

### 4.3 代码归一（最终规范）

- 标准码 = 品种码（大写）+ 交割年月（`YYMM`，两位年 + 两位月），如 `AP2701` / `CU2611`。
- 唯一换算入口 `app.core.symbol_code`：`to_std / to_native / to_sina / to_天勤(废弃) / delivery_ym / product_of / is_continuous`。
- 命名空间（`FG888` / `KQ.m@CZCE.FG` / `IDX:黑色` / `CZCE.AP701`）**不参与合约码归一**，由字典屏蔽，对外只暴露标准码/品种码。
- **铁律**：送天勤前必须转回原生码；郑商所 3 位码补年必须带逐行交易日（十年循环）；改采集器后必须重启容器；新数据入库后必须 `--rebuild-map`。
- **去天勤后**：`to_天勤()` 标记废弃（§6.4 T9），代码归一不再依赖天勤原生码生成。

---

## 5. 模块③：基础数据 L0（原始采集）

> 职责：存放**直接采集**的原始数据，是 L1/L2/L3 的唯一输入。策略：**只入库保存、定期备份、不删除**（`write_once_backup`）。**数据源仅 akshare（见 §6.4）**。

### 5.1 表清单

| 表 | 作用 | 策略 | 备注 |
|---|---|---|---|
| `fut_kline` | **行情超表**（**原天勤 天勤 直采；改造后改为 akshare 权威分钟源派生**，见 §6.4 / 【CB 改造点 T1/T2】），`freq×kind`，symbol=标准码 | write_once_backup | 回测/对账价格基准（contract / continuous / cont_adj 三维）；`adj` 列现状为假值 `0.0000`（见【CB 改造点 B3】） |
| `daily_bar` | 日线主连（akshare 主源，同花顺口径） | write_once_backup | 预测/回测默认日线源 |
| `hourly_bar` | 小时线主连（akshare，启用前须修 sina +8h 标签，见 T4） | write_once_backup | 融合策略实时信号唯一输入源 |
| `minute_bar` | 1 分钟原始合约 K 线（须新增 akshare 采集，见 T3） | write_once_backup | **本地当前为空，预留**；未来补回可重建各周期 L1 |
| 基本面原始表（7 张） | `member_position_rank` / `spot_basis` / `warehouse_receipt` / `inventory` / `roll_yield` / `contract_daily` / `macro_china` | write_once_backup | 原始即落库，清洗在 L1 汇总层做 |

### 5.2 设计要点

- L0 界定（拍板）：因 1 分钟已不可得（2026-09-30 明确），L0 = **直接采集的各周期原始棒 + 基本面原始表**，而非"L0=minute_bar"。
- L0 不参与任何计算/合成/复权（那是 L1/L2 的事）。采集器（`app/ingest/*`）只抓、只写 raw，数据源选择、限流、重试、断点统一框架化。
- `minute_bar` 预留：一旦补回，通过 `sp_build_l1_from_minute` 重建各周期 L1 棒，无需改上层。

---

## 6. 模块④：清洗数据 L1 / L2 / L3

> 职责：把 L0 原始数据加工成**口径明确、可直接消费**的序列。由存储过程产生，L1 只增改、L2 只追加、L3 为服务/治理层。

### 6.1 L1 · 归一化棒（单命名空间，未复权）

| 表 | 作用 | 策略 |
|---|---|---|
| `bar_5m` / `bar_15m` / `bar_30m` / `bar_60m` | 未复权归一化棒（单一命名空间 888/8888，屏蔽 KQ.m@） | add_modify_only（UPSERT，由 `sp_build_l1_*` 经 `dim_symbol` 归一写入） |
| `member_position_rank_summary` | 会员持仓品种级汇总 | add_modify_only |

- L1 完成三件事：**命名空间归一**（888/8888 统一，KQ.m@ 屏蔽）、**周期聚合**（1m→Xm，未来）、**基础逻辑校验**（高≥max(o,c)、低≤min(o,c)、量>0）。
- 数据质量校验脚本（参考数据清洗规范）：随机抽 10 合约核对 OHLC 逻辑、量正、主连无跳空缺失。

### 6.2 L2 · 复权层（存 offset，后复权只追加）

| 表 | 作用 | 策略 |
|---|---|---|
| `roll_segment` | **换月事件 offset**（每合约段一个常数 `cum_offset`，锚定最早段=0） | append_only（`sp_build_l2_roll_segment`，fail-loud 守卫，历史段永不改） |

**复权裁定（核心，来自《数据层复权架构裁定_20260930》实测）**：

- **前复权 vs 后复权数学等价**（同 10 品种逐 bar 差分差异 = 0），但**前复权锚定最新段、每天在变，删历史底表即废**；**后复权只追加、历史永不重算**。
- 因此**改存"换月事件 offset"而非"复权结果"**：回测取数 `adj = raw + offset(ts)`（as-of join），或预生成后复权物化表。
- **前复权链退役**：商品期货前复权存在历史价被推成负值风险（NI 累计 −14880，若价格量级小于累计贴水，历史价转负 → 任何百分比计算爆炸）；offset 方案天然规避。不再生成 `minute_bar_adj`。
- 换月检测用**双门**（振幅门 + 指数连比率门 0.40），漏检会低估影响，须 A/B 对拍通过后再退役 Python 脚本。

#### 6.2.1 机制：为什么"存 offset、不存复权结果"（落地现状）

- `cont_adj`（存结果的复权序列）**已删除 994 万行**；`continuous` 存**原始拼接**（换月处有真实跳空）；`roll_segment` 存每次换月的**偏移量 + 锚**；`back_adjust.py` 在 `BarStore` 取数时**运行时套用 offset** → 这就是"存 offset、不存结果"。
- **后复权 = 锚定最新段**：最新段是真实价，历史段整体平移（加"后续所有换月差之和"）使各段首尾 level 连续。

#### 6.2.2 对回测的三类影响（要害）

1. **级别类指标（MA / BOLL / 枢轴 / 支撑压力 / 形态）**：必须用后复权序列，否则换月处 MA 出现假跳变、信号失真。→ 后复权解决 ✅。
2. **换月那一根的收益（最关键，也是选"后复权"而非"前复权"的根本原因）**：后复权把换月价差吸收进"历史段整体平移"，换月边界的**调整后收益 = 0**，不会凭空产生假盈亏。前复权把平移加在最新段，会在最近段制造假收益——`cont_adj`/`minute_bar_adj` 前复权已崩坏成负价（3.4% 负价、最低 −2571.6）就是教训。**短窗口收益类指标（RSI / 随机 / 短 vol）因此干净** ✅。
3. **但长周期收益会被"累计平移"污染（必须警惕）**：250 日动量、跨多年年化收益如果直接在后复权序列上算，会把"历史段累计平移量"当成真实涨跌 → 数值偏高或偏低。**这类指标必须回到真实合约收益空间**：每段内算真实收益，换月处当作"非收益事件"桥接（或计入真实展期成本）后链式相乘，不能用后复权序列的长跨度差值。

#### 6.2.3 回测落地设计：双轨 + 显式展期成本（避免跳空）

- **价格双轨**：`signal_series` = 后复权连续（roll_segment 运行时套用），用于一切指标；`execution_series` = 逐合约真实价 + 显式换月事件。
- **换月事件驱动**：回测循环里当 `active_contract` 在 t−1→t 改变时：①按旧合约真实收盘平旧仓；②按新合约真实开盘/收盘开新仓；③这步的（新开−旧平）×手数 = **真实展期成本**，单独建模（价差 + 滑点）。**绝不让 offset 跳变进入 P&L**——offset 只是信号/可视化的记账装置，不是可交易价格。
- **出场价换算**：信号在后复权空间决策，下单时把出场价位用「该段累计 offset 逆运算（adjusted − 该段 cum_offset）」换算回活跃合约真实价。
- **长周期收益**：动量/年化在真实合约空间算，换月桥接不计入收益（或计入展期成本），不在后复权序列上做长跨度差值。
- **质量闸门**：用 **8888 指数连**作对照，区分"真实市场跳空"（888 与 8888 同步跳）vs"拼接假跳"（仅 888 跳）——避免把源数据真缺口误当换月缝平掉，也避免漏检换月缝。

#### 6.2.4 前置硬条件（不解决则"存 offset"形同虚设 — 全是 CB 改造点）

> **【CB 改造点 B1】** `roll_segment` 必须真正构建并调度：`build_roll_segments` **现状从未注册 cron**（等于空转），实际跑的是 raw `continuous`；须注册定时任务并 fail-loud 守卫。
> **【CB 改造点 B2】** `roll_segment` 必须补 **daily / hourly** 覆盖：现状只覆盖 min5/15/30/60，缺日线/小时线 → 日线/小时线回测无 offset 可用。
> **【CB 改造点 B3】** `fut_kline.adj` 列**全是假值 `0.0000`**（994 万行 cont_adj 已删，adj 未重算）：任何 `WHERE adj=True` 的回测会**静默取 0 行不报错**——必须作废该列语义或修正为真实 offset 标记。
> **【CB 改造点 B4】** 回测引擎实现**双轨**（signal_series 后复权 / execution_series 真实合约 + 显式展期成本），见 6.2.3。
> **【CB 改造点 B5】** 出场价/下单价换算：后复权决策价 → 真实合约价的逆 offset 换算（adjusted − 该段 cum_offset），见 6.2.3。
> **【CB 改造点 B6】** 长周期收益（动量/年化）迁移到真实合约空间计算，换月桥接不计收益，见 6.2.2 第 3 点。
> **【CB 改造点 B7】** 质量闸门接入 8888 指数连对照，区分真实跳空 vs 拼接假跳，见 6.2.3。
> **【CB 改造点 B8】** `BarStore.load` 返回后复权序列时**运行时套用 roll_segment offset**（as-of join），而非返回 raw；缺口时 fail-loud。
> **【CB 改造点 B9】** **默认口径改为后复权 + 5m 复权序列落地（v1.3，来自用户拍板 R1/R2）**：① `caliber_registry` 默认项与 `BarStore.load` 缺省 `caliber` 由 `continuous` 改为 `cont_adj`（运行时套 roll_segment offset），**消除现有回测隐含的换月假盈亏**；② 在 `bar_5m` 上套用 roll_segment offset 生成后复权 5m 序列（`roll_segment` 已含 min5 段，依赖 B2），`BarStore.load(symbol,'5m',cont_adj)` 须返回该序列，回测/信号若用 5m 必须取后复权而非 raw；③ 长周期收益类指标（动量/年化，B6）即便默认后复权，仍须显式回真实合约空间，不沿用后复权长跨度差值。依赖 B1/B2/B8 先就绪；**改默认口径后，历史回测结论（基于 continuous 含换月假盈亏）须全部重跑复核**。

> ⚠️ 若 B1–B3 不做，"存 offset"就没有 offset 可套，回测会**退化为 raw continuous（带跳空）**，跳空问题原样回归。

### 6.3 L3 · 口径合成与服务层（上层唯一直接出口的承载）

| 对象 | 作用 |
|---|---|
| `caliber_registry`（口径注册表） | (表 + caliber + 用途) 映射：`continuous` / `cont_adj` / `contract` 各对应什么、供什么用途；含**默认口径项 = `cont_adj`（后复权，v1.3 决策）**。上层**只按用途名取，不按表名取**；`BarStore.load()` 口径由注册表收口，缺省取默认项（v1.3 前为缺参报错，现已改为默认后复权，仍鼓励显式声明） |
| 连续序列物化（continuous_main 等） | 预生成或视图化"研究用主连"序列，承载后复权 offset 应用，供信号/回测消费 |
| `main_contract_map` | 信号序列 → 执行序列映射（见 §4.2），实盘下单落到具体合约 |
| `anomaly_ticket` | 质量门控工单（异常 K 线、跳空超阈值、缺失），拦截脏数据进入上层 |
| `hourly_1d_rollup`（CAgg） | 跨周期 rollup（小时→日），连续聚合 |

- L3 解决现状矛盾 #1（同一对象多份存储）→ 口径注册表；#2（双命名空间）→ 单命名空间出口；#3（口径混用）→ 参数化 caliber。
- **上层消费契约**：`BarStore.load(symbol, freq, caliber, start, end)` 返回口径明确的 DataFrame；`FundamentalsStore` 同理供基本面。上层代码库 `FROM bar_*|fut_kline|daily_bar|hourly_bar` 出现次数目标 = **0**。

---

### 6.4 数据源收敛：去除天勤（天勤）兜底，统一 akshare（CB 改造项）

> 决策：数据源**只保留 akshare**，移除天勤 天勤 兜底，以便整合代码。本节基于全库 天勤 引用排查结论，给出"哪些是绝对依赖、akshare 能否替代、必须改造的点"。

**6.4.1 排查结论：天勤 不是启动硬依赖，但有功能级绝对依赖**

- **加载期风险仅 1 处且不在启动路径**：全库唯一顶层 `from 天勤 import 天勤` 在 `app/ingest/fdf/fetch_fdf.py:21`；但 `app/ingest/__init__.py` 不导入 fdf，`app/scheduler.py` 对 fdf 的 import 在函数内且包 `try/except`；其余 天勤 引用均为**函数内惰性 import**，调用处均有 `try/except` 降级。→ 删 天勤 后应用照常起，不会崩。
- **功能级绝对依赖共 3 类**（去掉即断供/断链），集中在增量采集与 fut_kline 旧链路。

**6.4.2 三项"必须补 akshare 实现才能去天勤"——akshare 均可实现（无真正阻塞）**

| # | 项 | 现状缺口 | akshare 可用接口（已确认） | 改造 | 可行性 |
|---|---|---|---|---|---|
| ① | **1 分钟采集** | `MinuteCollector` 只有 `_fetch_天勤`，无 akshare 分支；`akshare_source.py` 无 1 分钟实现 | `futures_zh_minute_sina(symbol, period="1")`，本项目 `_fut_intraday_monitor.py:117` **已在用** sina 分时 | 新增 `MinuteCollector._fetch_akshare` | ✅ |
| ② | **小时线** | akshare 分支 `_fetch_akshare` 已写但被 2026-09-23 sina **+8h 标签事故**禁用（`return []`），默认走 天勤 | `futures_zh_minute_sina(symbol, period="60")`，已用 | 修 sina +8h 标签口径并启用分支 | ✅ |
| ③ | **CZCE 合约级日线** | sina `futures_zh_daily_sina` 对 CZCE 报 Length mismatch → 天勤 兜底 | 官方 `get_czce_daily`（郑商所日线）、东财 `futures_zh_daily`/`futures_hist_em`（按合约跨交易所） | 新增东财源函数 + CZCE 符号格式联调（3 位 vs 4 位） | ✅ |

**关键澄清（改变旧认知）**：
- 小时线/1 分钟的数据**本就在 sina**，不是"缺数据"，而是确定性的 **+8 小时标签偏移**（真实 09:00–10:00 收盘棒被标成 18:00、夜盘 22:00–01:00 被标成 06:00–09:00，与昼夜边界自洽）。修正 = **减去 8 小时再重新校验交易时段**，**不是换数据源**。
- 天勤 路径因标签是"起点"（09:00 棒）额外 +1h 转收盘口径；**sina 已是收盘口径（+8h 偏移），启用 akshare 分支时只做"−8h"，不要再加 +1h**，否则双重修正。
- 主连日线（`daily_bar`）早已全 akshare（`futures_main_sina`，CZCE 主连也通）；CZCE 缺口**仅限合约级日线**（near/dominant 具体合约），影响 carry/期限结构因子，不影响主连行情。

**6.4.3 非绝对依赖（摘除只丢增强功能，主流程不受影响）**

- `天勤_calibrator`（orchestrator 校准/补缺/异常工单）：akshare 是主源，天勤 仅补缺+比对，包在 `try/except` 内；去 天勤 主流程照常，仅丢"双源比对异常工单"。
- `smooth_extender`（主力映射维护）：用 天勤 合约日线做 dominant 判定；但 `main_contract_map` 每日常规维护走 `daily_futures_refresh → refresh_main_contract_map`（`scripts/refresh_main_contract_map.py` 已确认零 天勤 依赖），故主力映射**不绝对依赖天勤**。

**6.4.4 纯元数据/配置残留（去 天勤 无运行时影响，仅命名/清理）**

- `app/core/symbol_code.py:to_天勤()`——仅**生成**天勤订阅码字符串存入 `contract_code_map.天勤_symbol` 列，函数本身不 import 天勤；去掉后该列沦为闲置元数据（建议迁入 PRD 通用 `cfg_native_symbol` 列，见 §3.1 / T8）。
- `app/core/config.py` 的 `天勤_symbol_template`、口径注释 `"天勤=起点口径"`；`.env`/`config/*.yaml` 的 `天勤_PHONE/PASSWORD`；`requirements.txt` 的 `天勤==3.7.5`。
- `barstore.py` 读 `天勤_symbol` 列、`api/symbols.py` 返回该字段、`models/domain.py`、`schemas/anomaly.py` 的 `天勤_val`、`api/routers/anomalies.py` 的 `accept_天勤` 处置动作——均绑定库内天勤值/工单，去天勤后保留为只读历史或随 PRD 配置模块迁移。

**6.4.5 CB 改造点清单（按风险排序，T 系列）**

> **【CB 改造点 T1】** 零风险可立即摘除：fdf 整模块（旧 天勤 直采：`fetch_fdf`/`adjust_fdf`/`build_continuous`/`run_fdf`）+ `scripts/ingest_fut_kline_incremental.py` + `adjust_all_main.py`；届时 `_fut_kline_job` / `_adjust_job` 内对 fdf 的惰性 import 会失败被 try 捕获 → 这两个 job 变 no-op，**须配合 T2 改为调用 `_rebuild_fut_kline_job` 派生路径或直接删除**。
> **【CB 改造点 T2】** `app/scheduler.py` 的 `_fut_kline_job` / `_adjust_job`：移除 fdf 引用，改为 akshare 派生路径（`_rebuild_fut_kline_job`）或删除；避免 no-op 静默。
> **【CB 改造点 T3】** `app/ingest/minute_collector.py` `MinuteCollector` 新增 `_fetch_akshare`（sina `futures_zh_minute_sina` period=1），并验证潜在的 sina +8h 标签（先验证是否同小时线事故），否则 1 分钟采集断供。
> **【CB 改造点 T4】** `app/ingest/hourly_collector.py` `HourlyCollector`：修复 sina +8h 标签口径（真实时刻 −8h，勿再加 +1h）后启用 `_fetch_akshare`（period=60）；否则小时线采集死亡或数据错位。
> **【CB 改造点 T5】** `app/ingest/contract_bars.py` `CARRY_SOURCES`：CZCE `sina=False,天勤=True` → 新增 akshare/东财 CZCE 合约日线源替代 天勤 兜底（符号 3/4 位联调）。
> **【CB 改造点 T6】** `scripts/accept_v34.py`（CB 引擎验收）：从读 `fut_kline` 连续主连切到 akshare 派生后的序列（或 `daily_bar` 主连喂 CB 引擎），否则策略验收断供。
> **【CB 改造点 T7】** 质量门控：删 `天勤_calibrator` 双源比对 + `anomalies.accept_天勤` 分支（或换第二源东财/交易所）；保留门控精神。
> **【CB 改造点 T8】** 元数据列 `天勤_symbol` / `天勤_val`：保留只读或迁入 PRD 通用对照列 `cfg_native_symbol`（模块①），勿硬删以免破坏既有数据。
> **【CB 改造点 T9】** 依赖与配置清理：`requirements.txt` 删 `天勤==3.7.5`；`.env`/`config/*.yaml` 删 `天勤_PHONE/PASSWORD`；`config.py` 删 `天勤_symbol_template` 与 `"天勤=起点口径"` 注释；`symbol_code.to_天勤()` 标记废弃或改生成通用 native 码。

**6.4.6 部署形态决策：融入现有镜像，不新增容器**

> 结论：**将 akshare 作为固定版本依赖集成进现有 `qhyc:latest` 镜像（scheduler / api 共用），不新增 AKDocker / AKTools 容器。**

- 官方两种形态：
  - **AKDocker**（`registry.cn-shanghai.aliyuncs.com/akfamily/aktools:jupyter`）：JupyterLab + akshare，**无 HTTP API、面向人工写 notebook**，对自动化定时采集零价值。
  - **AKTools**（`registry.cn-shanghai.aliyuncs.com/akfamily/aktools:[版本]`，FastAPI/Gunicorn，端口 8080）：把 akshare 包成 HTTP 服务，面向非 Python 消费方或集中化取数。
- 选"融入"而非"加容器"的理由：
  1. qhyc 采集是**批处理 + 定时任务**，已在 `scheduler` 容器内以 Python 进程运行；akshare 是纯 Python 库，进程内调用零网络开销、最快。
  2. AKTools 的 HTTP 微服务形态会带来**额外网络延迟、单点故障、跨调用方的限流/超时协调成本**，对全 Python 的批采集无收益。
  3. **可复现铁律**：AKDocker 镜像随 akshare 发布自动更新 → 版本漂移 → 违反"防不可复现三问"；在自有镜像里 `pip install akshare==<固定版本>` 锁版本，严格控制。
- 若需组织隔离：从同一 `qhyc:latest` 起一个**专用 ingest 服务容器**（现有 `scheduler` 已是此角色），仍进程内 import akshare，**不要**直接用上游 AKDocker 镜像。
- 落实（CB）：在 `requirements.txt` / `Dockerfile` 固定 `akshare==<版本>`（当前主线约 1.19.x）；删除 天勤 依赖（见 T9）。

---

### 6.5 交易日历与换月治理（v1.2 新增，来自外部参考文章）

> 参考：用户提供的截图文章《期货量化连续性回测：交易日历与主力切换》（2026-10-03，标题与链接未提供）。其核心主张与本 PRD 同向：**回测里顺滑、实盘却在换月附近异常增多，根因是两个——交易日历处理不完整、主力合约切换规则不统一**。本节把该文的五项抓手转成数据层可验收的改造点（**C 系列**）。

**6.5.1 可借鉴点与不可照抄点**

可借鉴（方法层，与数据源无关）：

1. 交易日历必须先保证"**可交易时间**"准确：① 法定节假日与调休 ② 夜盘与日盘时间段映射 ③ 特殊时段的风控限制窗口。时间边界不准，信号触发与成交评估都会被污染。
2. **主力切换先定规则再跑回测**：常见规则为成交量优先 / 持仓量优先 / 固定提前 N 日切换；**无论采用哪一种，都要在整个样本期保持一致**，避免结果被切换口径变化影响。
3. 连续性处理要盯两类问题：**价格跳变（指标突变）+ 持仓迁移（执行成本上升）**；建议在回测中**单独标记换月窗口，统计该窗口的收益贡献、滑点和回撤变化**，确认策略是否依赖换月噪声。
4. 结构化**换月切换日志**（复盘用）：切换日期 / 原主力 / 新主力 / 切换后首日开盘价相对前主力收盘价偏移 % / 同窗口滑点均值（较非换月日上升比例）→ 收益异常时能快速区分"策略逻辑变了"还是"主力切换带来的价格跳变与执行成本变化"。
5. 回测前四项检查清单：日历完整性 / 切换一致性 / 跳变处理 / 成本评估。

**不可照抄**：该文示例使用 `天勤 / TqAuth / TqBacktest` 与 `KQ.m@SHFE.rb2405`。本项目正在**去天勤**（§6.4），且 `main_contract_map.change_flag` 已提供比 天勤 内存库更可控、可审计的换月事件来源——**借方法、不借实现**。

**6.5.2 现状核对（qhyc 已有 vs 缺口，2026-10-03 代码核对）**

| 该文要求 | qhyc 现状 | 判定 |
|---|---|---|
| 交易日历（含节假日 / 调休） | `trade_calendar(exchange, trade_date, is_open, sessions, note)` 已存在；但实测多为 **`exchange='INF'` 推断模式**（`calendar_infer.py`：当日 ≥80% 主连品种有数据即视为交易日），`calendar_mode()` 可返回 official / inferred / empty | **半具备**：无官方日历；推断模式下"节假日"由数据反推，非权威 |
| 夜盘 / 日盘时间段映射 | `trade_calendar.sessions` 列存在，但推断写入时为 `None`；`cfg_trading_session`（§3.1）尚未建 | **缺口** |
| 特殊时段风控限制窗口 | 仅有 `app/execution/roll_policy.delivery_guard`（距最后交易日 ≤ `ROLL_BUFFER_DAYS`，默认 5 日，禁止新开） | **缺口**（无涨跌停扩板 / 保证金调整 / 限仓窗口字典） |
| 用交易日历推算"最后交易日" | ⚠️ **实测缺陷**：`roll_policy._last_trading_day()` 以"交割月第 10 个**工作日**（**仅跳周末、忽略节假日**）"简化推算 | **缺陷（P1）**：连休越长偏差越大，注释自称"偏保守"但窗口偏移是**系统性**的，不可解释 |
| 主力切换规则 | `scripts/refresh_main_contract_map.py` 每日维护 `main_contract_map`，`change_flag = 与同品种上一交易日 underlying 不同`；全链路零 天勤 依赖 | **具备事件标记**，但**口径未参数化、未版本化** |
| 换月窗口标记 | `change_flag` 已给出换月事件点（`execution/service.py` 已据此触发移仓） | **具备** |
| 换月窗口收益 / 滑点 / 回撤归因 | 无 | **缺口** |
| 换月切换日志（切换日 / 原新主力 / 首日偏移 % / 窗口滑点均值） | 无（`change_flag` 只有布尔） | **缺口** |
| 回测前检查清单门禁 | 无（要求散见 §10 验收，未做成回测前置门禁） | **缺口** |

**6.5.3 CB 改造点（C 系列）**

> **【CB 改造点 C1】** **交易日历官方化 + `sessions` 落地**（v1.3 数据源选型 = **akshare 接口**）：`trade_calendar` 从 `INF` 推断模式升为**权威日历**（交易日 + 节假日 + 调休），**官方源 = akshare 交易日历接口 `tool_trade_date_hist_sina()` / `option_trade_time_em()`**，由 `calendar_infer` 改造为 authoritative loader 落地（替代"当日 ≥80% 主连品种有数据即视为交易日"的推断）；并把**日盘/夜盘时间段写入 `sessions`**（时段规则由 akshare `futures_trading_time` 或静态配置补充，或建 `cfg_trading_session` 并由 `sessions` 同步）；`calendar_mode()` 须能由 `inferred` 升级为 `official`，看板显示当前模式。
> **【CB 改造点 C2】** **修正 `app/execution/roll_policy._last_trading_day()` 忽略节假日的缺陷**：改为查 `trade_calendar` 真实交易日历推算"交割月第 N 个交易日"（支持各交易所不同规则），消除连休附近交割月护栏窗口的系统性偏移——这是该文"日历不完整污染实盘"在 qhyc 的具体落点。
> **【CB 改造点 C3】** **主力切换规则参数化 + 版本化**：新增 `cfg_main_switch_rule`（`rule_kind ∈ {OI 持仓量优先, VOL 成交量优先, DAYS_BEFORE 固定提前 N 日}`、`n_days`、`effective_from/effective_to`、`rule_version`），并在 `main_contract_map` 写入时记录 `rule_version`；**同一回测样本期内 `rule_version` 必须唯一**（禁止中途改口径）。
> **【CB 改造点 C4】** **换月窗口标记与归因统计**：以 `change_flag` 定义换月窗口 `[t−1, t+1]`（可配），在回测结果中**单列该窗口的收益贡献、滑点、回撤变化**，输出"策略收益是否依赖换月噪声"的结论。
> **【CB 改造点 C5】** **换月切换日志（复盘用，落库）**：新建 `roll_switch_log`：`(variety, switch_date, old_underlying, new_underlying, old_close, new_open, open_offset_pct, window_slip_mean, nonroll_slip_mean, slip_ratio)`；其中**首日开盘偏移 %** 与**同窗口滑点均值较非换月日的上升比例**须可由库内数据独立复算——直接服务"防不可复现三问"。
> **【CB 改造点 C6】** **特殊时段风控窗口字典 `cfg_risk_window`**：涨跌停扩板 / 保证金临时上调 / 交割月限仓 / 交易所临时风控；供回测"可交易性"判定与执行校验（`app/execution/validator.py`）消费。
> **【CB 改造点 C7】** **回测前置检查清单门禁**：把该文四项（日历完整性 / 切换一致性（C3 版本唯一）/ 跳变处理（B4–B8）/ 成本评估（换月窗口成本单列，C5））做成**回测运行前的硬门禁**，任一不过则**拒绝产出回测结论**，防"顺滑回测 → 实盘失真"。
> **【CB 改造点 C8】** **禁止成本字典重复造表（校正项）**：既有 `dim_trading_cost` 已是唯一权威，§3.1 的 `cfg_symbol_fee` **不得另建**；详见 §3.4。

**6.5.4 与该文"回测前检查清单"的映射**

| 检查项 | 该文核心问题 | 合格标准 | 本 PRD 落点 |
|---|---|---|---|
| 日历完整性 | 是否覆盖全部交易时段 | 无缺失交易日 + `sessions` 非空 | C1 / C2 |
| 切换一致性 | 样本期规则是否统一 | 无临时改口径（`rule_version` 唯一） | C3 |
| 跳变处理 | 指标是否受换月冲击 | 影响可解释、可控 | B4–B8 / C4 |
| 成本评估 | 换月窗口成本是否单列 | 有独立统计 | C5 / C6 |

---

### 6.6 时区治理（v1.3 新增，来自用户专项排查）

数据层所有行情时间列均为 Postgres `timestamptz`（`DateTime(timezone=True)`），底层统一存 UTC、按会话时区解释。本轮回测实测结论：

- **行情层时区一致、offset 关联安全**：`bar_5m` / `bar_15m.bucket` 与 `roll_segment.seg_start` 读出均为 **Asia/Shanghai 感知（UTC+8）**，后复权 as-of join 不存在时区错位（坐实 R12 假阳性、offset 引擎时区无虞）。
- **DB 会话时区 = `Asia/Shanghai` 真实成立**（`SHOW TIME ZONE` 实测），但靠**服务器默认**满足，`app/core/db.py` 与迁移**未显式钉死** → 换环境（UTC 默认）会静默偏 8h。

四个要点（TZ-1 即既有 R7/T4；TZ-2、TZ-3 为新增改造点）：

| 编号 | 问题 | 现状 | 严重度 |
|---|---|---|---|
| **TZ-1（=R7/T4）** | akshare/sina 分钟&小时线 datetime **标签偏移 +8h 量级**（日盘/夜盘偏移量不一致，须精确测量，不能假设统一 −8h）；`_parse_ak_dt` 误把偏移标签当真实上海时间转 UTC 存储 | `HourlyCollector._fetch_akshare` 因此禁用（返回 `[]`）；`MinuteCollector` 未验证 | P0（行情数据正确性，去天勤前置） |
| **TZ-2** | naive `datetime.utcnow()` 写入 `timestamptz` 列：`task_repo.py:30` / `anomalies.py:66,70` / `api/routers/ingest.py:82` / `health.py:75` | 在会话 TZ=Asia/Shanghai 下，naive 值被当上海时间再存 UTC → 比意图偏 −8h（仅影响 audit/health/ingest 回执等运营字段，非行情） | P2（口径不一致；且 `utcnow()` 在 Py3.12+ 已弃用） |
| **TZ-3** | 会话时区未在应用/迁移钉死，依赖服务器默认 | 备份恢复到 UTC 默认环境 → 所有读数静默偏 8h | P2（配置性隐患） |
| **TZ-4（已排除）** | `roll_segment.seg_start` vs `bar_*.bucket` 时区错位 | 实测两者皆上海时区感知，**不存在** | — |

> **【CB 改造点 T10】** 全库 `datetime.utcnow()` → `datetime.now(timezone.utc)`（并确认写入 `timestamptz` 时不丢时区），消除 TZ-2 的 −8h 偏差与弃用告警（R13）。
> **【CB 改造点 T11】** 在 `app/core/db.py` 引擎初始化或迁移里显式 `SET TIME ZONE 'Asia/Shanghai'`（或 SQLAlchemy `connect_args`/engine event），把 TZ-3 的服务器默认改为代码强约束，杜绝换环境静默失效（R14）。
> **【CB 改造点 T4（既有）】** 启用 sina 小时线前**先精确测量**标签偏移（不能假设统一 −8h：夜盘 22:00→06:00 确为 +8h，日盘 09:00→18:00 描述存 +9h 疑点），对 `dt` 做口径矫正 + 未来棒剔除后再入库，且**勿叠加 +1h**（天勤 路径因起点口径另有 +1h，sina 已是收盘口径只减 8h）。`MinuteCollector` 同法验证（T3）。

---

## 7. 关键算法与设计决策

| 决策点 | 结论 | 依据 |
|---|---|---|
| 复权存什么 | 存**换月事件 offset**（后复权），不存复权结果；前复权链退役 | 《数据层复权架构裁定》实测：前/后复权等价，后复权只追加 |
| 默认口径（DEFAULT_CALIBER，v1.3） | 由 `continuous` 改为**后复权（`cont_adj`，运行时套 roll_segment offset）**；消除现有回测隐含的换月假盈亏；长周期收益类指标（B6）须显式回真实合约空间，不沿用默认后复权长跨度差值 | 用户拍板（v1.3，原 R1）；`caliber_registry` 默认项改 `cont_adj`（B9） |
| 5 分钟复权（v1.3） | 扩展后复权序列至 **5m**：`bar_5m` 套 roll_segment offset（`roll_segment` 已含 min5 段，B2）生成后复权 5m 序列；回测/信号若用 5m 必须取后复权而非 raw | 用户拍板（v1.3，原 R2）；【CB 改造点 B9】 |
| 换月检测 | **双门**（振幅门 + 比率门 0.40），主力判定**持仓量单向不可逆 + T+1** | 连续合约拼接实战；减少假盈亏 |
| 夜盘归属 | `hour >= 夜盘开始` → 归属**下一交易日** | 数据清洗规范；铜镍等活跃品种错位致走势失真 |
| 主连/具体合约 | **信号用主连、执行用具体合约**，经 `main_contract_map` 映射；换月显式移仓 | 主连/指数/具体合约分工（两篇参考） |
| 命名空间 | 对外只暴露标准码/品种码，屏蔽 888/8888/KQ.m@ | 《合约代码统一规范》；双命名空间冲突 P0 |
| 成本模型 | 手续费分品种 + 平今加倍；回测滑点 +30~50% 安全垫 | 回测精度权衡实测 |
| 质量门控 | OHLC 逻辑校验 + 换月跳空 <1% + 缺失告警 | 数据清洗规范 |
| 功能开关 | 数据库控制（`cfg_feature_switch`），热更不改代码 | 配置管理需求；已有 feature_switch |
| 数据源收敛 | **只保留 akshare**，移除 天勤 兜底；1 分钟/小时线修 sina +8h 标签、CZCE 换东财源 | 全库 天勤 排查（§6.4）；无真正阻塞 |
| 回测双轨 | 信号后复权 / 执行真实合约 + 显式展期成本；长周期收益回真实合约空间 | §6.2.2 / §6.2.3（避免跳空假盈亏） |
| 交易日历权威性（v1.2） | `trade_calendar` 必须官方化（含节假日 / 调休，**数据源 = akshare 交易日历接口 `tool_trade_date_hist_sina` / `option_trade_time_em`**），`sessions` 落日盘 / 夜盘时段；不得长期依赖 `INF` 推断模式 | §6.5.2 / §6.5.3（C1；用户拍板源选型 v1.3，原拍板项③） |
| 交割月护栏基准（v1.2） | 最后交易日改用**真实交易日历**推算，不得只跳周末忽略节假日 | §6.5.2 实测缺陷（C2） |
| 主力切换口径（v1.2） | 参数化 + 版本化（OI / VOL / 固定提前 N 日 + `rule_version`），**样本期内唯一** | §6.5（C3；文章第二点：先定规则再跑回测） |
| 换月窗口治理（v1.2） | 窗口 `[t−1, t+1]` 单列归因（收益贡献 / 滑点 / 回撤）+ `roll_switch_log` 复盘日志 | §6.5（C4 / C5；文章第三、四点） |
| 风控限制窗口（v1.2） | `cfg_risk_window`（涨跌停扩板 / 保证金调整 / 交割月限仓）纳入可交易性判定 | §6.5.3（C6） |
| 回测前置门禁（v1.2） | 日历完整性 / 切换一致性 / 跳变处理 / 成本评估 四项任一不过 → 拒绝出结论 | §6.5.4（C7） |
| 成本字典真源（v1.2） | **唯一权威 = 既有 `dim_trading_cost`**（含生效期）；禁止另建 `cfg_symbol_fee` | §3.4（C8） |

---

## 8. 数据接口与流向

### 8.1 数据流

```
采集器(app/ingest，仅 akshare) → L0(fut_kline[akshare派生]/bar原始/基本面原始)   [只写 raw]
        │  sp_build_l1_*（命名空间归一+周期聚合+逻辑校验）
        ▼
   L1(bar_5/15/30/60m, summary)                              [只增改]
        │  sp_build_l2_roll_segment（双门检测换月→cum_offset，含 daily/hourly，B2）
        ▼
   L2(roll_segment)                                          [只追加，须真调度 B1]
        │  caliber_registry + 后复权运行时套 offset(B8) + 质量门控(B7)
        ▼
   L3(连续序列物化 + main_contract_map + anomaly_ticket + roll_switch_log[C5])   [服务/治理]
        │  BarStore.load(symbol, freq, caliber)
        ▼
   上层（因子/回测/策略/应用）                                [只读接口；回测双轨 B4/B5/B6]
```

### 8.2 对外接口契约（CB 改造必须实现）

- `BarStore.load(symbol, freq, caliber=None, start=None, end=None) -> DataFrame`：`symbol` 只接受品种码/合约码（如 `FG` / `AP2701`）；`caliber ∈ {continuous, cont_adj, contract}`；**缺省 `caliber=None` 时默认取 `cont_adj`（后复权，v1.3 决策，消除现有回测隐含的换月假盈亏，B9）**，仍鼓励显式声明以防口径漂移（仍由 `caliber_registry` 收口，未注册口径报错）。
- `BarStore.load` 返回后复权序列时**运行时套用 roll_segment offset**（as-of join，【CB 改造点 B8】）；缺口 fail-loud，绝不静默返回 raw。
- `FundamentalsStore.load(indicator, symbol, start, end) -> DataFrame`：经 `cfg_basic_indicator` 路由到源表，经 `_lagged` 视图做前视隔离（T 日数据 T+1 才可读）。
- 上层模块（策略 / 回测 / 因子 / API）**禁止**出现 `SELECT ... FROM bar_* / fut_kline / daily_bar / hourly_bar` 裸引用；唯一合法裸写方：采集器、存储过程、ORM、migrations。
- `roll_events(product, start, end) -> DataFrame`（v1.2 新增，【CB 改造点 C4/C5】）：只读接口，返回换月事件与窗口上下文（`change_flag`、原 / 新主力、首日开盘偏移 %、窗口滑点均值 / 非换月日滑点均值），供回测窗口归因与复盘消费；**回测与复盘不得各自重算换月点**。
- `barstore` 侧的交易日历入口（v1.2，【CB 改造点 C1/C2】）：`is_trading_day(exchange, date)` / `nth_trading_day(exchange, year, month, n)` 只读接口，供执行护栏（`roll_policy`）与回测对齐使用，**禁止各处自行实现"跳周末"式近似**。

---

## 9. 数据模型 DDL 要点（改造落地清单）

| 表 | 层 | 主键 | 唯一约束建议 | mutable_policy |
|---|---|---|---|---|
| `cfg_feature_switch` | ① | switch_key | — | 行级 UPDATE |
| `cfg_trading_session` | ① | (exchange, variety_or_all, session_type) | — | 行级 UPDATE |
| `cfg_holiday` | ① | trade_date | — | 行级 UPDATE |
| `contract_code_map` | ① | (exchange, std_symbol, version) | — | rebuild/UPSERT（含 `cfg_native_symbol`，T8） |
| `cfg_symbol_fee` | ① | — | — | **不新建（C8）：成本唯一真源为既有 `dim_trading_cost`；本行仅标注历史冲突，禁止建表** |
| `cfg_symbol_margin` | ① | symbol_or_variety | — | 行级 UPDATE |
| `dim_exchange` | ② | exchange_code | — | 静态 |
| `dim_variety` | ② | variety_code | — | add_modify |
| `dim_contract` | ② | contract_code | — | add_modify |
| `main_contract_map` | ②/③ | (variety, trade_date) | — | add_modify |
| `fut_kline` | ③ | (symbol, freq, kind, ts) | 联合唯一 | write_once_backup（`adj` 列须按 B3 作废/修正） |
| `daily_bar` / `hourly_bar` | ③ | (symbol, trade_date) | 联合唯一 | write_once_backup（hourly 启用前修 +8h，T4） |
| `minute_bar` | ③ | (symbol, ts) | 联合唯一 | write_once_backup（预留，须补 akshare 采集 T3） |
| 基本面原始表 ×7 | ③ | 各自业务键 | — | write_once_backup |
| `bar_5/15/30/60m` | ④L1 | (symbol, ts) | 联合唯一 | add_modify_only |
| `member_position_rank_summary` | ④L1 | (variety, trade_date) | — | add_modify_only |
| `roll_segment` | ④L2 | (symbol, seg_id) | — | append_only（须含 daily/hourly，B1/B2 调度） |
| `caliber_registry` | ④L3 | (table, caliber, usage) | — | 行级 UPDATE |
| `anomaly_ticket` | ④L3 | ticket_id | — | append_only |
| `trade_calendar` | ①（既有） | (exchange, trade_date) | — | 行级 UPDATE（须由 `INF` 推断升为 official，`sessions` 非空，C1） |
| `cfg_trading_session` | ① | (exchange, variety_or_all, session_type) | — | 行级 UPDATE（可与 `trade_calendar.sessions` 互同步，C1） |
| `cfg_main_switch_rule` | ①/② | (variety_or_all, rule_version, effective_from) | — | 行级 UPDATE（切换口径版本化，回测样本期内唯一，C3） |
| `dim_trading_cost` | ①（**既有，成本唯一真源**） | (variety_code, action, scope_kind, effective_from) | — | 行级 UPDATE（`cfg_symbol_fee` 不得另建，C8） |
| `cfg_risk_window` | ① | (exchange, variety_or_all, window_type, start_date, end_date) | — | 行级 UPDATE（涨跌停扩板 / 保证金调整 / 限仓，C6） |
| `roll_switch_log` | ④L3 | (variety, switch_date) | — | append_only（切换日 / 原新主力 / 首日偏移 % / 窗口滑点比，C5） |

> 外键补强（解决 D1）：至少给 `backtest_detail→backtest_result`、`factor_value→factor_registry` 加 FK；行情表加联合唯一约束（防重复写入）。
> **数据源变更（v1.1）**：`fut_kline` 由 akshare 权威分钟源派生（T1/T2），不再 天勤 直采；移除 天勤 相关列/字段的强制依赖（T8/T9）。

---

## 10. 验收标准（CB 改造对照）

| 模块/层 | 硬指标（验收口径） |
|---|---|
| ① 配置管理 | 全部功能开关、手续费、保证金、交易时间、节假日、代码对照均可在库内 UPDATE 生效，无需改代码/重启冷路径；成本唯一真源 = 既有 `dim_trading_cost`（覆盖在产 84 品种、含平今加倍标记，C8 禁止另建 `cfg_symbol_fee`）；保证金由 `cfg_symbol_margin` 承载 |
| ② 合约管理 | 全库 symbol 经 `symbol_code` 归一后 0 冲突；`dim_contract` 覆盖全部合约的上市/交割窗口；`main_contract_map` 与回测换月逐位一致 |
| ③ L0 | L0 表只读采集写入、无计算逻辑；`minute_bar` 预留结构就位；基本面原始表与采集链路对账行数一致 |
| ④ L1 | 上层代码库 `FROM bar_*\|fut_kline\|daily_bar\|hourly_bar` 裸引用 = 0；`bar_*` 单命名空间，无 KQ.m@ 残留 |
| ④ L2 | `roll_segment` 经 `sp_build_l2` 双门结果 A/B 对拍通过；历史段零改写；后复权物化与 legacy 复权逐 bar 差分差异 = 0 |
| ④ L2 回测落地 | `roll_segment` 覆盖 daily/hourly + 已注册 cron 调度（B1/B2）；`fut_kline.adj` 假列作废或修正（B3）；回测引擎实现双轨 + 出场价逆 offset 换算（B4/B5/B8）；长周期收益回真实合约空间（B6）；8888 指数连闸门（B7） |
| ④ L3 | `caliber_registry` 覆盖所有消费口径且默认项 = `cont_adj`（后复权，v1.3）；`BarStore.load` 缺省返回后复权（非 raw），口径经注册表收口；`anomaly_ticket` 拦截已知脏数据 |
| 默认口径 / 5m 复权（v1.3） | `caliber_registry` 默认项 = `cont_adj`；`BarStore.load` 缺省返回后复权；`BarStore.load(symbol,'5m',cont_adj)` 返回后复权 5m 序列（依赖 B2 的 min5 段）；长周期收益指标不沿用默认后复权长跨度差值（B9） |
| 数据源收敛（去 天勤） | 全库 `import 天勤` 残留（除只读历史列）= 0；1 分钟/小时线/CZCE 合约日线均经 akshare 取得；`fut_kline` 由 akshare 派生路径重建；CB 验收（`accept_v34`）改走派生序列 |
| 跨层 | 实时与回测信号逐笔一致（锚点常绿）；任一策略改参数 30 分钟内跑完稳健性套件 |
| ① 交易日历（v1.2） | `calendar_mode()` = `official`（非 inferred）；`trade_calendar.sessions` 日盘 / 夜盘时段非空，且与真实交易时段**逐品种**核对通过（C1） |
| ①/② 换月治理（v1.2） | `roll_policy._last_trading_day` 改用真实交易日历，**连休前后护栏日与真实最后交易日对账一致**（C2）；`cfg_main_switch_rule` 版本化且同一回测样本期 `rule_version` 唯一（C3） |
| ④L3 换月归因（v1.2） | 换月窗口 `[t−1, t+1]` 的收益贡献 / 滑点 / 回撤可单列输出（C4）；`roll_switch_log` 可由库内数据独立复算出首日偏移 % 与窗口滑点比（C5） |
| 回测前置门禁（v1.2） | 日历完整性 / 切换一致性 / 跳变处理 / 成本评估 四项未过 → 回测**拒绝出结论**（C7） |
| 成本真源（v1.2） | `dim_trading_cost` 为唯一成本表；全库不存在第二份费率表（`cfg_symbol_fee` 未建，C8） |

---

## 11. 迁移路线（阶段，沿用本地→git→云端）

> 原则：本批均为**加法**（建表/建过程/插种子/加视图），无 DROP；开关类一行 UPDATE 还原；`roll_segment` 因 append_only 可安全裁剪回退。

- **Phase A · 配置管理落地（①）**：建 `cfg_trading_session` / `cfg_holiday` / `cfg_symbol_margin`；**成本不另建表，统一用既有 `dim_trading_cost`（C8 校正：禁止新建 `cfg_symbol_fee`），仅补齐生效期与平今加倍标记**；种子与现状 env 对齐；`feature_switch` 已有。验收：成本模型从 `dim_trading_cost` 读取。
- **Phase B · 合约管理补全（②）**：建 `dim_exchange/variety/contract` + `v_variety_contracts`；`sp_refresh_dim_contract_dates` 回填；`symbol_code` 收口（含 `to_天勤` 废弃标记）。验收：上层不再拼 symbol。
- **Phase C · L0 界定固化（③）**：`data_layer_catalog` 登记 L0 血缘与 `write_once_backup` 策略；`minute_bar` 预留结构确认。
- **Phase D · L1 归一（④L1）**：`sp_build_l1_*` / `sp_normalize_l1_symbols` 落地；`lint_direct_table_refs` 整改业务读方（逐文件改、跑 pytest + 单品种对拍）。
- **Phase E · L2 复权（④L2）**：`sp_build_l2_roll_segment` 落地 + A/B 对拍（含 **daily/hourly 覆盖，B2**）；对拍通过后退役 `minute_bar_adj` 与 legacy 复权脚本；注册调度（B1）。
- **Phase F · L3 服务层（④L3）**：`caliber_registry` + `BarStore`/`FundamentalsStore` 接口（`BarStore` 运行时套 offset，B8）；`anomaly_ticket` 门控（B7）；上层接入接口。
- **Phase G · 云地补齐**：本地缺失的 1m/5m、`fut_kline` min15/30/60 continuous 走云端采集 + 回灌（白名单待定），验收行数 + 水位两端逐位核对。
- **Phase H · 数据源收敛（去 天勤，T 系列）**：T1 摘除 fdf 旧链路 → T2 `_fut_kline_job`/`_adjust_job` 改派生路径 → T3 补 1 分钟 akshare → T4 修 sina +8h 并启用小时线 → T5 CZCE 换东财源 → T6 `accept_v34` 切派生序列 → T7 删 天勤 比对门控 → T8 元数据列迁移 → T9 依赖/配置清理。验收：全库 `import 天勤` 残留（除只读历史）= 0，三周期 akshare 取得。
- **Phase I · 后复权回测落地（B 系列）**：B3 修 `adj` 假列 → B1/B2 `roll_segment` 真构建+调度+补 daily/hourly → B8 `BarStore` 运行时套 offset → B4/B5 回测引擎双轨 + 逆 offset 出场价 → B6 长周期收益空间 → B7 8888 闸门。验收：回测换月处无假盈亏，跳空被显式计为展期成本。
- **Phase J · 交易日历与换月治理（C 系列，v1.2）**：C8 先做（**减项**，确认成本真源唯一、不新建 `cfg_symbol_fee`）→ C1 日历官方化 + `sessions` 落地 → C2 修 `roll_policy._last_trading_day` 忽略节假日 → C3 切换口径参数化 + 版本化 → C5 `roll_switch_log` → C4 换月窗口归因 → C6 `cfg_risk_window` → C7 回测前置门禁。验收：日历 official、护栏对账一致、样本期口径唯一、换月窗口归因可出、门禁生效（见 §10 v1.2 各行）。

---

## 12. 风险与待拍板

| # | 事项 | 选项 / 建议 | 严重度 |
|---|---|---|---|
| R1 | `DEFAULT_CALIBER` 由 `continuous` 改为复权口径 | **已决策（v1.3）：改为后复权 `cont_adj`**，消除现有回测隐含的换月假盈亏；见【CB 改造点 B9】 | 已拍板 |
| R2 | 5 分钟周期复权 | **已决策（v1.3）：扩展 5m 后复权序列**（`bar_5m` 套 roll_segment offset，B2 已含 min5 段）；见【CB 改造点 B9】 | 已拍板 |
| R3 | 前复权链退役时间线 | **本地链已于 2026-10-03 验证退役**（表不存在 + `cont_adj` kind=0）；R3 安全门对拍（backadj_feasibility）初跑四项全 FAIL，但**经严格复验为验证脚本假阳性**（脚本段映射闭区间 + 末段 `seg_end=None` 恒 False + 阈值误判高价品种日常波动）——真实 `back_adjust` 对 84 品种 2830 个换月点抵消率 **中位 0.00%、零品种 >1% 残留**（✅ 引擎正确）；云端 `minute_bar_adj`(58.6M)/`cont_adj`(1012万) 待隧道恢复后删 | P0（守卫，已复核通过） |
| R4 | 云地同步白名单 | 本地无 1m/5m、continuous 缺失，需定白名单或本地重采 | P1 |
| R5 | 1 分钟本地缺失 | 按用户指令跳过；`minute_bar` 仅预留，L1 各周期由云端 `minute_bar` 聚合后回灌 | P2 |
| R6 | 去 天勤 后的 1 分钟/CZCE 采集缺口 | 必须先有 akshare 等价实现（T3/T5），否则断供 | P0（改造前置） |
| R7 | sina +8h 标签修正 | 启用 akshare 小时线（T4）前必须先修，且**勿叠加 +1h**，否则双重修正 | P0（数据正确性） |
| R8 | `fut_kline.adj` 假列 | 不修（B3）则回测 `WHERE adj` 静默取 0 行，属静默失败 | P0（静默失败风险） |
| R9 | 交易日历仍处推断模式（v1.2） | 不修（C1/C2）则交割月护栏在连休附近系统性偏移、回测时间边界不可信 | P1（数据正确性） |
| R10 | 双份成本真源（v1.2） | 若照 v1.0 新建 `cfg_symbol_fee` 与既有 `dim_trading_cost` 并存 → 成本口径再次分叉 | P1（单一真源，C8 校正） |
| R11 | 主力切换口径漂移（v1.2） | 若切换规则不版本化（C3），跨样本期回测结果不可比、不可复现 | P1（可复现性） |
| R12 | **后复权 offset 对齐（2026-10-03 复验结论：非缺陷，本轮回测校正）** | 初判"复权后跳空 > 原始、抹平率 34%"为 `backadj_feasibility.py` **验证脚本假阳性**（段映射闭区间 + 末段 `seg_end=None` 恒 False 不落库 + 阈值误判 CU/TA/MA 高价品种日常波动）。**真实 `back_adjust` 经 `roll_ts` 精确定位复验：84 品种 2830 换月点抵消率中位 0.00%、零品种 >1% 残留 → 引擎正确，不阻塞 B9**。真实遗留（本轮回测校正）：① **6 个 backwardation 品种负价**（实测 `BC888/CY888/FB888/RR888/RS888/WR888`，因 `build_roll_segments` 从未跑 `--positivity`，本地 0/84 启用）→ 重建加 `--positivity` 即消除，点数/MA/ATR 信号不受影响；② **5m 复权无缺口**（此前误报"bar_5m 为空"已校正）：`bar_5m` 实测 2462 万行（raw 连续、零负价、无 adj 列），`roll_segment(min5)` 2911 段已存在 → B9 的 5m 后复权由 `BarStore` 运行时套 offset（B8）即可，无需新建物化表 | P2（非引擎缺陷；负价为已知约束） |

| R13 | 时区治理 · naive UTC 写入 tz 列（TZ-2，§6.6） | `datetime.utcnow()` 写入 `timestamptz` 在会话 TZ=Asia/Shanghai 下偏 −8h，且 Py3.12+ 已弃用 | P2（运营字段口径不一致） |
| R14 | 时区治理 · 会话时区未钉死（TZ-3，§6.6） | 依赖服务器默认 `Asia/Shanghai`，备份恢复到 UTC 默认环境 → 所有读数静默偏 8h | P2（配置性隐患） |

**用户拍板项（已全部决策，v1.3 落地）**：① `DEFAULT_CALIBER` → **改为后复权口径**（消除现有回测隐含的换月假盈亏，见 R1 / B9）；② 5 分钟复权 → **扩展 5m 后复权序列**（见 R2 / B9）；③ 交易日历官方源选型 → **akshare 交易日历接口**（`tool_trade_date_hist_sina` / `option_trade_time_em`，见拍板项③ / C1）。以上三项已写入对应改造点与 §10 验收，CB 按 §14（B9 / C1）执行即可。

> 注：R3（前复权链退役时间线）本地已于 2026-10-03 验证退役（表不存在 + `cont_adj` kind=0）；R3 安全门对拍（backadj_feasibility）初跑四项全 FAIL，但**经严格复验为验证脚本假阳性**——真实 `back_adjust` 引擎对 84 品种 2830 换月点抵消率 0%（R12 已更正，非阻塞）。云端 `minute_bar_adj`(58.6M)/`cont_adj`(1012万) 仍待 SSH 隧道恢复后删除（见《前复权链退役执行报告》§八）。日历源选型（原拍板项③）现已定为 akshare，C1 落地数据源已明确。

---

## 13. 参考资料与既有文档索引

### 13.1 本次调研的外部资料（CSDN）

1. 《Python 如何处理期货主力合约数据？连续合约回测时需要注意什么》（qq_33617536）— 主力/连续/合约区分、独立合约管理模块、研究价 vs 成交价。
2. 《期货量化交易系统构建（一）：数据获取与主力合约拼接实战》（2301_81559356）— 数据获取→主力识别→复权拼接→清洗；后复权；夜盘归属；持仓量 T+1 换月；OHLC 逻辑校验。
3. 《期货量化主连和具体合约怎么切：天勤 KQ.m 与 KQ.i 用法》（qq524425141）— 研究用主连、执行用具体月；`underlying_symbol` 映射；换月检测。
4. 《期货主连、指数与具体月份怎么选：回测研究与实盘下单分工》（weixin_49134609）— 主连/指数/具体合约分工；信号序列 vs 执行序列；换月显式处理。
5. 《2026 期货回测工具排名：精度与易用性的真实权衡》（weixin_29053383）— 回测精度三层面（撮合/成本/数据口径）；手续费分品种、平今加倍；滑点 +30~50%；夜盘时间处理；数据层质量校验。
6. 《行情数据库 / 数据层架构》（shinnyringo, 160828153）— 访问时被安全验证拦截，未直接引用；其主题（数据层设计）已被上述 1–5 与既有文档覆盖。
7. 《期货量化连续性回测：交易日历与主力切换》（用户提供的**截图**，2026-10-03，标题与链接未提供）— 主张：回测顺滑而实盘换月附近异常增多，根因是**交易日历处理不完整 + 主力切换规则不统一**。五部分：① 可交易时间（节假日调休 / 夜盘日盘映射 / 风控限制窗口）② 主力切换规则先定后用且样本期一致 ③ 换月窗口的"价格跳变 + 持仓迁移"两类问题、须单独标记窗口统计收益 / 滑点 / 回撤 ④ 结构化换月切换日志（首日偏移 % / 同窗口滑点均值较非换月日）⑤ 回测前四项检查清单。示例用 天勤（`天勤` / `KQ.m@`），**方法可借鉴、实现不照抄**（本项目去天勤，§6.4）。落位 §6.5（C 系列）。

### 13.2 本仓库既有文档（改造交叉引用）

- `现状梳理_01_数据库表清单与逻辑关系_20260928`（数据证据）
- `现状梳理_06_六层解耦迁移建议_20260928`（总分层与接口契约）
- `数据库分层与字典/配置表设计_20260930`（dim_*/cfg_*/L0-L2 表与存储过程）
- `数据层复权架构裁定_20260930`（复权存 offset 裁定）
- `合约代码统一规范_20260920`（symbol_code 最终规范）
- `前复权链退役执行报告_20261003`、`口径修复方案_20261003`、`口径一致性全面排查_20261003`（执行依据）
- 全库 天勤 引用排查结论（2026-10-03，数据源收敛依据，落位 §6.4）

---

## 14. CB 改造点索引（去天勤 + 后复权回测 + 日历换月治理 + 时区治理 + 评审补强，全量汇总）

> 本节汇总本 PRD 全部【CB 改造点】，便于 CB 改造时逐项勾销。T 系列 = 去天勤数据源收敛（§6.4.5）；B 系列 = 后复权回测落地（§6.2.4，含 v1.3 默认口径/5m 扩展 B9）；C 系列 = 交易日历与换月治理（§6.5.3，v1.2）；**G 系列 = 专业评审补强（§14.4，v1.5）**。逐项详情见对应小节。

### 14.1 T 系列 · 去天勤（天勤）数据源收敛 + 时区治理（§6.4.5 / §6.6）

| 编号 | 改造点 | 位置 | 风险 |
|---|---|---|---|
| T1 | 摘除 fdf 整模块 + `ingest_fut_kline_incremental.py` + `adjust_all_main.py`（旧 天勤 直采） | `app/ingest/fdf/*`、`scripts/*` | 零风险（须配 T2） |
| T2 | `_fut_kline_job` / `_adjust_job` 改调 `_rebuild_fut_kline_job` 派生路径或删除 | `app/scheduler.py` | 否则 no-op 静默 |
| T3 | `MinuteCollector` 新增 `_fetch_akshare`（sina period=1）+ 验证 +8h 标签 | `app/ingest/minute_collector.py` | P0（断供） |
| T4 | `HourlyCollector` 修 sina +8h 标签（−8h 勿 +1h）并启用 `_fetch_akshare`（period=60） | `app/ingest/hourly_collector.py` | P0（断供/数据错） |
| T5 | `CARRY_SOURCES` CZCE 新增 akshare/东财合约日线源替代 天勤 兜底 | `app/ingest/contract_bars.py` | P0（CZCE carry 断供） |
| T6 | `accept_v34.py` 验收从 `fut_kline` 连续主连切到 akshare 派生序列 | `scripts/accept_v34.py` | P0（策略验收断供） |
| T7 | 删 `天勤_calibrator` 双源比对 + `anomalies.accept_天勤` 分支（或换第二源） | `app/ingest/天勤_calibrator`、`api/routers/anomalies.py` | 仅丢增强 |
| T8 | `天勤_symbol` / `天勤_val` 保留只读或迁入 `cfg_native_symbol` | `contract_code_map` 等 | 勿硬删 |
| T9 | 依赖/配置清理：requirements 删 天勤、env 删 天勤_*、config 删 天勤 模板、`to_天勤()` 废弃 | `requirements.txt`/`.env`/`config.py`/`symbol_code.py` | 收尾 |
| T10 | 时区治理：全库 `datetime.utcnow()` → `datetime.now(timezone.utc)`（确认写 `timestamptz` 不丢时区） | `task_repo.py:30` / `anomalies.py:66,70` / `api/routers/ingest.py:82` / `health.py:75` | P2（TZ-2/R13） |
| T11 | 时区治理：在 `db.py` 引擎初始化或迁移显式 `SET TIME ZONE 'Asia/Shanghai'`，钉死会话时区 | `app/core/db.py` / 迁移 | P2（TZ-3/R14，配置性隐患） |

### 14.2 B 系列 · 后复权回测落地（避免跳空）

| 编号 | 改造点 | 位置 | 风险 |
|---|---|---|---|
| B1 | `roll_segment` 真正构建并注册 cron 调度（现状空转） | `build_roll_segments` | P0（offset 不可用） |
| B2 | `roll_segment` 补 daily / hourly 覆盖（现状仅 min5/15/30/60） | `sp_build_l2_roll_segment` | P0（日/小时线无 offset） |
| B3 | `fut_kline.adj` 假列（全 0.0000）作废或修正为真实 offset 标记 | `fut_kline.adj` | P0（回测静默取 0 行） |
| B4 | 回测引擎实现双轨（signal 后复权 / execution 真实合约 + 显式展期成本） | 回测引擎 | P0（跳空假盈亏） |
| B5 | 出场价/下单价逆 offset 换算（adjusted − 该段 cum_offset → 真实合约价） | 回测引擎 / `BarStore` | P1 |
| B6 | 长周期收益（动量/年化）回真实合约空间算，换月桥接不计收益 | 回测引擎 | P1（污染） |
| B7 | 质量闸门接入 8888 指数连对照，区分真实跳空 vs 拼接假跳 | 质量门控 | P1 |
| B8 | `BarStore.load` 返回后复权序列时运行时套用 roll_segment offset（as-of join） | `BarStore` / `back_adjust.py` | P0（核心机制） |
| B9 | **默认口径改为后复权 + 5m 复权序列落地（v1.3，用户拍板 R1/R2）**：`caliber_registry`/`BarStore.load` 缺省 `caliber` 由 `continuous` 改 `cont_adj`；`bar_5m` 套 roll_segment offset 生成后复权 5m 序列；长周期收益指标显式回真实合约空间 | `caliber_registry` / `BarStore` / `bar_5m` | P0（默认口径，依赖 B1/B2/B8；**R12 已复验为引擎正确，不再阻塞**） |

> ⚠️ **联动风险**：B1–B3 任一未做，"存 offset"即无 offset 可套，回测退化为 raw `continuous`（带跳空），跳空问题原样回归；B9 依赖 B1/B2/B8 先就绪（无 offset 则默认后复权无从套用）；**改默认口径为 `cont_adj` 后，历史回测结论（基于 `continuous` 含换月假盈亏）须全部重跑复核**；T3/T4/T5 任一未做，去天勤后对应周期/品种断供。CB 改造须按 §10 验收逐条勾销。

### 14.3 C 系列 · 交易日历与换月治理（v1.2，来自外部参考文章）

| 编号 | 改造点 | 位置 | 风险 |
|---|---|---|---|
| C1 | 交易日历官方化（节假日 / 调休，**数据源 = akshare `tool_trade_date_hist_sina` / `option_trade_time_em`**）+ `sessions` 落日盘 / 夜盘时段，`calendar_mode()` 升为 `official` | `trade_calendar` / `calendar_infer.py` / `cfg_trading_session` | P1（时间边界，用户拍板源选型 v1.3） |
| C2 | 修 `_last_trading_day` 忽略节假日缺陷 → 用真实交易日历推算交割月第 N 个交易日 | `app/execution/roll_policy.py` | P1（护栏偏移） |
| C3 | 主力切换口径参数化 + 版本化（OI / VOL / 固定提前 N 日 + `rule_version`），样本期内唯一 | `cfg_main_switch_rule` / `scripts/refresh_main_contract_map.py` | P1（口径漂移） |
| C4 | 换月窗口 `[t−1, t+1]` 单列归因（收益贡献 / 滑点 / 回撤变化） | 回测结果层 + `roll_events` 只读接口 | P1（换月噪声不可解释） |
| C5 | 新建 `roll_switch_log`（切换日 / 原新主力 / 首日开盘偏移 % / 窗口滑点均值 vs 非换月日） | L3 表 + 调度 | P1（复盘不可复现） |
| C6 | 新建 `cfg_risk_window`（涨跌停扩板 / 保证金调整 / 交割月限仓）供可交易性判定 | 模块① / `app/execution/validator.py` | P2 |
| C7 | 回测前置门禁：日历完整性 / 切换一致性 / 跳变处理 / 成本评估 四项 | 回测入口 | P0（防"顺滑回测 → 实盘失真"） |
| C8 | **禁止新建 `cfg_symbol_fee`**：统一到既有 `dim_trading_cost`（含生效日），仅新增保证金字典 | 模块① / `app/data/cost.py` | P1（双份真源，**减项**） |

> ⚠️ **C 系列联动**：C7 门禁依赖 C1 / C2（日历）、B4–B8（跳变）与 C5（成本单列）——**门禁不是新造轮子，而是把已存在的要件串成"不过不许出结论"的闸门**。C8 是**减项**（防重复造表），执行只需一次确认，无代码风险。

### 14.4 G 系列 · 评审补强改造点（v1.5，来自专业评审，dev-expert 视角）

> 本系列为对前序 T/B/C 系列的**补强**，非重复。详细论证、每条的"发现/为何是疏漏/建议/严重度"见配套文档 `数据层PRD_专业评审与疏漏补强.md`。优先级：**P0 三件套（G1/G2/G3）应先于 B/T/C 主线落地**。

| 编号 | 维度 | 改造点 | 位置 | 严重度 |
|---|---|---|---|---|
| G1 | 健壮性 | **akshare 单一源韧性层**：重试/指数退避/断路器/本地缓存/限流 + 每品种每周期新鲜度 SLA + 告警；以"与上一成功批次一致性"自动校验替代 天勤 双源比对；部分失败记 `anomaly_ticket` 不静默 | 采集框架 + 监控 | **P0** |
| G2 | 逻辑 | **统一换月真源**：`roll_segment` 改由 `main_contract_map.change_flag` 派生，双门/8888 比率门降级为校验告警，消除信号(后复权)-执行(真实合约)在换月边界的错位 | `build_roll_segments` / `refresh_main_contract_map` | **P0** |
| G3 | 逻辑 | **B9 与 B2 顺序约束**：日/小时线 offset 段先于默认口径切换生效；或默认 `cont_adj` 按 freq 渐进、未覆盖 freq 显式 fallback 告警（禁静默返回 raw） | `caliber_registry` / `BarStore` | **P0** |
| G4 | 数据正确性 | **C1 改期货专属日历源**：弃 `tool_trade_date_hist_sina`（股票日历），改 `futures_rule`（期货按日日历，含保证金列可喂 C6）/ 交易所官方日历；夜盘时段用 `futures_trading_time` | `calendar_infer` / C1 | **P1** |
| G5 | 性能 | **B8 规模化决策**：选"重建即物化 `bar_*_adj`"或"运行时套用 + 缓存"，定性能目标（如单品种 10 年 15m 加载 < 200ms） | `BarStore` / `sp_build_l2` | **P1** |
| G6 | 健壮性/性能 | **`sp_build_l*` 幂等 + 临时表原子 swap**；`roll_segment` 重跑跳过已建段；`bar_*` 为 TimescaleDB 超表 `segmentby=symbol` + 压缩 | 存储过程 / schema | **P1** |
| G7 | 可拓展 | **`SourceProvider` 抽象**：akshare 为唯一 enabled provider，天勤 标 disabled 而非物理删，第二源可低成本复活 | 采集框架 | **P2** |
| G8 | 测试 | **CI 门禁 + 测试金字塔**：裸表引用 lint 入 CI/pre-commit；单测（symbol_code/cost on_date/夜盘归属/back_adjust 抵消）；Golden Dataset 回归；fail-loud 异常测试 | CI / `tests/` | **P1** |
| G9 | 架构 | **退役/重定位 `fut_kline.continuous`**（派生品不应在 L0），修正 L0 定义不含派生；明确 1m 暂缓下 L1 构建源 | `fut_kline` / §2·§5 | **P1** |
| G10 | 逻辑 | **重审 `--positivity`**：默认不强制平移（负价对收益类指标无害，仅比值类需非负保护）；B6 长周期收益数据源明确定义为 `load(caliber='contract')` 经 `main_contract_map` | `build_roll_segments` / §8.2 | **P2** |
| G11 | 可观测 | **新增可观测性章节**：新鲜度/成功率/延迟 SLA、重建时长、offset 覆盖率看板 + 告警 | 新增 § / 监控 | **P2** |
| G12 | 架构 | **`FundamentalsStore.load(..., as_of)`** 区分回测(锁 T+1) / 实盘(直读) | `FundamentalsStore` | **P2** |
| G13 | 范围校正 | **C5 `roll_switch_log` 滑点字段由回测/执行层回填**，数据层仅算 `open_offset_pct`（来自价格），修正"库内独立复算"表述 | `roll_switch_log` / §6.5.3 | **P2** |

---

## 15. 评审结论与 CB 行动指引（v1.5 专业评审）

> 本 PRD 经专业评审（dev-expert，多维度），结论：**顶层设计正确、可改造，但在"工程落地安全网"上有系统性疏漏**。完整论证见 `数据层PRD_专业评审与疏漏补强.md`，改造点以 §14.4 的 **G 系列（G1–G13）** 索引。

**总体结论**：四模块分层、接口硬约束、单一 akshare 源、复权只存 offset、双轨回测、成本单一真源（C8 校正）均为成熟自洽的顶层设计；offset 引擎已用真实管线实证正确、时区一致无虞。待补强的五类核心风险：

1. **健壮性最大风险（G1, P0）**：去天勤后只留 akshare 单一免费爬虫源且摘掉 天勤 比对层 = 单点依赖却无韧性层（限流/脏数据/失败无保险）。
2. **逻辑最大风险（G2, P0）**：两套换月定义（`change_flag` 按符号 / `roll_segment` 按价格）各自为政，换月边界信号-执行可能错位。
3. **自相矛盾（G3, P0）**：B9 默认 `cont_adj` 但 B2 缺日/小时线 offset 段 → 默认口径对最重要日线源先坏后修。
4. **数据正确性（G4, P1）**：C1 误用股票日历 `tool_trade_date_hist_sina` 套期货（中金所 09:15 开盘/无夜盘/国债差异/节假日提前收盘），须改期货专属 `futures_rule`。
5. **性能/保险缺失（G5/G6/G8）**：B8 运行时 as-of join 不可规模化；存储过程重建缺幂等与原子 swap；"上层禁裸 SQL"等硬约束无 CI 门禁会随时间腐烂。

**CB 执行优先级**：① 先落 **P0 三件套 G1/G2/G3** 再动 B/T/C 主线；② Phase J 第一步即改 **G4（C1 期货日历源）**，否则 C2/C7 建立在错误日历上；③ G5/G6 与 B8/B9 同步设计；④ 尽早建 **G8（CI 门禁 + Golden Dataset）** 作为所有硬约束不腐烂的保险。

> ⚠️ 本评审**不要求 WB 立即实现** G1–G13 任何一项；全部交由 CB 在改造中落地。PRD 以 §14.4 + 本节索引承接，未达标不视为完成。

---

> **结语**：本 PRD 把"数据层"从放射状耦合中剥离为四个可独立验收的模块，并明确数据源统一为 akshare（§6.4）、复权只存 offset（§6.2）。改造的成败不取决于写了多少表，而取决于**上层是否真的只走 `BarStore`/`FundamentalsStore` 接口、口径是否真的被注册表收口、复权是否真的只追加、offset 是否真的被构建并调度、去天勤后采集是否真的无断供**，以及（v1.2）**交易日历是否真的权威（期货专属源，C1/G4）、主力切换口径是否真的版本唯一、换月窗口的盈亏是否真的被单列解释**（§6.5）；并（v1.3）**默认口径是否真的改为后复权（`cont_adj`）、5m 后复权序列是否真的可用、历史回测结论是否真的因换月假盈亏而重跑复核**（B9）。**v1.5 评审进一步要求**：去天勤后单一 akshare 源须配套**韧性层（G1）**、换月须**统一真源（G2）**、默认口径切换须与 offset 覆盖**顺序对齐（G3）**、硬约束须有**CI 门禁与回归（G8）**。CB 改造时请严格对照第 10 节验收标准与第 14 节改造点索引（**T / B / C / G 四系列**），未达标不视为完成。
