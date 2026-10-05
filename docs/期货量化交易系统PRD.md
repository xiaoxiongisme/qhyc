# 期货量化交易系统 PRD（整合定稿版）

> **版本**：v1.0（整合定稿，2026-10-05）
> **整合人**：WorkBuddy（小齐）
> **定位**：本文件是 qhyc 期货量化交易系统的**唯一权威需求文档**，整合了原《PRD 最终版》《数据层 PRD（v1.5）》《P0-1 执行反解层》《P0-2 实盘通道》《PRD 改造落地执行方案》《上线准入评估报告》及 D:/过程 全过程台账的**结论性内容**。
> **使用约定**：
> - 本文只保留**结论性、已裁定**的内容；分析过程、待裁决项、过程性记录一律不载入。
> - **待裁决项与过程文档**已统一归档至 `docs/archive/`（本地保留、不入 git、云端已删除），详见《测试报告》。
> - CB（CodeBuddy）改造以本文件第 13 节验收标准与既有专项 PRD（数据层 / P0-1 / P0-2）逐条勾销为准。

---

## 0. 文档治理规则（已拍板）

1. `docs/` 目录只保留本文件为唯一需求文档；其余过程/验证/分析文档归入 `docs/archive/`，不进 git、不驻云端。
2. 任何改造点的「结论」必须回到本文件对应章节；「分析/过程」不得逆向污染本文件。
3. 防不可复现三问（脚本版本 / 数据快照 / 方法口径）由 D:/过程 tasklog 承载，不在本文件展开。

---

## 1. 系统概述与目标

qhyc 是一套**商品期货量化交易系统**：从多源行情与基本面数据采集出发，经数据层清洗与复权，驱动预测引擎与融合策略产出信号，经执行反解层把连续主力信号反解为真实合约可执行订单，最终经实盘通道（CTP/QMT）落地。

**核心目标**：
- 数据层单一真源、口径明确、可复现；
- 回测与实盘信号口径一致（杜绝换月假盈亏）；
- 信号→真实合约订单反解自洽、可人工对照；
- 实盘下单有完整状态机、幂等、断电恢复与风控联动。

**当前进度（截至 2026-10-05）**：数据采集/数据层主体完成；P0-1 代码完成且现网可用条件已具备；P0-2 实盘通道 Sprint1 框架已建（DDL 已应用、四核心模块已写，未部署）；运维准入 SOP 已升级并每日巡检。

---

## 2. 总体架构（分层）

```
采集器(app/ingest，仅 akshare)
   │ write_once
   ▼
L0 基础数据(fut_kline / daily_bar / hourly_bar / minute_bar / 基本面原始表×7)
   │ sp_build_l1_*
   ▼
L1 归一化棒(bar_5/15/30/60m，单命名空间，未复权)
   │ sp_build_l2_roll_segment(双门检测换月→cum_offset)
   ▼
L2 换月偏移(roll_segment，后复权只追加)
   │ caliber_registry + 运行时套 offset + 质量门控
   ▼
L3 口径合成与服务层(连续序列物化 / main_contract_map / anomaly_ticket)
   │ BarStore.load(symbol, freq, caliber) / FundamentalsStore.load
   ▼
上层：预测引擎 → 融合策略 → 信号推送 → 执行反解层(P0-1) → 实盘通道(P0-2)
```

**接口硬约束**：上层（预测/回测/策略/API）只通过 `BarStore`/`FundamentalsStore` 取数；业务代码库禁止裸 `SELECT FROM bar_*/fut_kline/daily_bar/hourly_bar`（唯一合法裸写方：采集器、存储过程、ORM、migrations）。

---

## 3. 技术栈（已确认）

- 语言：Python 3.13（managed）；Web：FastAPI + 前端（简报/看板）。
- 数据库：PostgreSQL + TimescaleDB（超表 `segmentby=symbol` + 压缩）。
- 数据获取：**akshare 单一源**（已去天勤）；部署形态 = 固定版本 akshare 融入现有 `qhyc:latest` 镜像（不新增 AKTools/AKDocker 容器）。
- 调度：APScheduler（`qhyc-scheduler` 容器）。
- 部署：Docker Compose（云端 `docker-compose.cloud.yml`），4 容器：api / scheduler / timescaledb / pipeline。
- 实盘通道（P0-2）：CTP 网关进程（`vnpy_ctp`，Linux 容器）+ QMT 桥接进程（`xtquant`，Windows 本机），qhyc 侧均为 HTTP 客户端。

---

## 4. 数据层（结论性要求与验收口径）

> 完整改造点索引见原《数据层 PRD（v1.5）》§14（T/B/C/G 四系列，共 40 项）。本文件只列**已裁定结论**与**验收口径**。

### 4.1 四大模块
| 模块 | 职责 | 结论 |
|---|---|---|
| ① 配置管理 | 功能开关/交易时间/节假日/代码对照/成本/保证金收口到库 | `cfg_feature_switch` 热更；成本唯一真源 = 既有 `dim_trading_cost`（**禁止另建 `cfg_symbol_fee`**）；新增 `cfg_symbol_margin`/`cfg_trading_session`/`cfg_holiday`/`cfg_risk_window` |
| ② 合约管理 | 单命名空间、合约生命周期、主力映射 | `symbol_code` 唯一换算入口；主力判定 = 持仓量单向不可逆 + T+1；`main_contract_map` 为信号→执行映射真源 |
| ③ 基础数据 L0 | 原始采集只写 raw、只备份不删 | 数据源仅 akshare；`minute_bar` 预留 |
| ④ 清洗 L1/L2/L3 | 归一化棒 / 换月偏移 / 口径合成 | 复权**只存 offset（后复权）**，前复权链退役 |

### 4.2 已裁定关键决策
| 决策 | 结论 |
|---|---|
| 复权方案 | 存换月事件 offset（后复权），不存复权结果；前复权链退役（NI 累计贴水会推成负价） |
| 默认口径（v1.3 用户拍板） | `DEFAULT_CALIBER` 由 `continuous` 改为 **`back_adj`（等差后复权）**；长周期收益类指标须回真实合约空间，不沿用默认后复权长跨度差值（`cont_adj` 加法前复权已废弃：会产生负价，C7 移除） |
| 5 分钟复权（v1.3 用户拍板） | 扩展后复权序列至 5m：`bar_5m` 套 roll_segment offset |
| 换月检测 | 双门（振幅门 + 指数连比率门 0.40），主力判定持仓量单向不可逆 + T+1 |
| 夜盘归属 | `hour >= 夜盘开始` → 归属下一交易日 |
| 主连/具体合约 | 信号用主连、执行用具体合约，经 `main_contract_map` 映射；换月显式移仓 |
| 数据源收敛 | **只保留 akshare**，移除天勤兜底；1 分钟/小时线修 sina +8h 标签、CZCE 换东财源 |
| 回测双轨 | 信号后复权 / 执行真实合约 + 显式展期成本；长周期收益回真实合约空间 |
| 交易日历（v1.2） | `trade_calendar` 必须官方化（**期货专属 `futures_rule` 源，非股票日历 `tool_trade_date_hist_sina`**），`sessions` 落日盘/夜盘时段 |
| 交割月护栏 | `roll_policy._last_trading_day()` 改用真实交易日历推算（修复忽略节假日缺陷） |
| 主力切换口径 | 参数化 + 版本化（`cfg_main_switch_rule`，`rule_version` 样本期内唯一） |
| 成本字典 | 唯一真源 = 既有 `dim_trading_cost`（含生效期）；禁止另建第二份费率表 |
| 时区治理 | 全库 `datetime.utcnow()` → `datetime.now(timezone.utc)`；`db.py` 显式 `SET TIME ZONE 'Asia/Shanghai'` |

### 4.3 验收口径（结论性指标）
- ① 全部功能开关/手续费/保证金/交易时间/节假日/代码对照可在库内 UPDATE 生效，无需改代码；成本唯一真源覆盖在产品种、含平今加倍。
- ② 全库 symbol 经 `symbol_code` 归一 0 冲突；`dim_contract` 覆盖全部合约上市/交割窗口。
- ③ L0 只采集写入无计算；`minute_bar` 结构就位。
- ④ L1 上层裸表引用 = 0；`bar_*` 单命名空间无 KQ.m@ 残留。
- L2 `roll_segment` 双门 A/B 对拍通过、历史段零改写、后复权物化与 legacy 逐 bar 差分 = 0。
- L3 `caliber_registry` 默认项 = `back_adj`；`BarStore.load` 缺省返回后复权；`anomaly_ticket` 拦截脏数据。
- 去天勤：全库 `import tqsdk` 残留（除只读历史列）= 0；三周期 akshare 取得。
- 日历：`calendar_mode()=official`，`sessions` 日/夜盘非空且逐品种核对通过。
- 换月治理：护栏对账一致；`rule_version` 样本期唯一；`roll_switch_log` 可复算。

### 4.4 当前数据层状态（2026-10-05 实测，云端权威库）
- ✅ 去天勤 `import tqsdk` = 0；`caliber` 默认 `cont_adj`；前复权作业已停。
- ✅ `main_contract_map` 刷新至 2026-09-30、55 品种、近 7 天 54（调度 `daily_futures_refresh 18:10` 已注册）。
- ✅ `price_tick` 覆盖 2329/2612（gate 口径 73 全满）；`contract_daily` 3646 合约 / 76.6 万行。
- ✅ 迁移台账 `schema_migrations` 登记至 **030**；`futures_rule` 表已建、`continuous_follow_rule` 已播种 63 行。
- ⚠️ `roll_segment` 仅 min5/15/30/60（**无 daily/hourly**，按 D1 方案 A 为明确决策，G3 显式回退告警不静默）。
- ⚠️ `dim_main_contract_inferred` 仅 63 品种（活跃 91），其余走 `underlying` 回退。
- ⚠️ 前复权残留 `fut_kline.cont_adj`(1012 万) + `minute_bar_adj`(5862 万) 待与 G9 合并后删（D3 挂起）。

---

## 5. 预测引擎（摘要 + 锁定裁决）

- **特征工程**：多周期（1m/5m/15m/30m/60m/日）技术指标 + 基本面（持仓排名/基差/库存/仓单/展期收益）。
- **模型库**：按族（趋势/均值回归/突破/波动率）组织，集成机制固定。
- **锁定裁决（编码必须遵守）**：
  1. 特征计算必须使用 `BarStore` 口径明确的序列，禁止裸表；
  2. 跨品种/大类传导须经显式相关性与时滞校准，禁止朴素平移；
  3. 任何新条款上线前须过四维鲁棒性测试（见 §9）。

---

## 6. 交易策略层（融合策略 V3.4 定稿口径）

五层结构：**方向层 → 入场层 → 离场层 → 执行层 → 风控层**。

- **方向层**：多周期均线 + ADX 门控 + 斐波那契·汇流 + 利弗莫尔·阶梯加码（V3.4 融合口径）。
- **入场层**：信号经回踩确认；仓位 ≤ 2%（单品种），截盈放亏。
- **离场层**：跟踪止损 + 形态止盈；亏损单不加码。
- **执行层**：对接 P0-1 反解（连续价→真实合约价）。
- **风控层**：`portfolio_brake` 联动（上线置 on）。
- **策略红线（评审必须守住）**：不顺加亏损仓；不裸裸跨周期偷价；不把换月跳空当收益；不静态假设成本。
- **方法学铁律**：任何新条款评估前必读 §6.9；回测须显式区分「信号空间/执行空间」。

---

## 7. 信号推送层

- **扫描调度**：定时扫描融合信号状态机，去重（同品种同方向 N 分钟去抖）。
- **推送形态**：三段式（结论 + 依据 + 风险）。
- **通道**：旁路（飞书/微信 webhook），失败不阻塞主流程。
- **推送窗口/日历**：跳过非交易日（依赖 §4 官方日历）。

---

## 8. 回测与研究模块

- **定位**：两层回测架构，`fusion_state_detail` 为单一真源。
- **四维测试矩阵**：不同成本/容量梯度/参数扰动/样本外，结论须过样本外。
- **可调参数面板**：`config/local.yaml` `fusion.*` 均可经回测面板调参。
- **walk-forward**：滚动窗口，禁止单窗口过拟合。
- **鲁棒性测试**：容量梯度（3/5/7/10 仓）为要指标；收益线性增而 MDD 恒定 ⇒ edge 独立非拥挤。
- **回测前置门禁（C7）**：日历完整性 / 切换一致性 / 跳变处理 / 成本评估 任一项不过 → 拒绝出结论。

---

## 9. 执行反解层 P0-1（结论：代码完成，现网可用）

> 完整规格见原《PRD P0-1 执行反解层》。

- **范围**：把融合信号（连续 888 + 价）反解为可执行订单建议（真实合约 + 原始价 + 整数手 + 校验结论）；REST `/execution/reverse`。
- **四大组件**：① 价格反解 ② 换月规程 ③ 手数计算 ④ 点位校验。
- **实测勘误（CB 落地已修正，5 处）**：
  1. 真实合约取 `main_contract_map.underlying`（非 `main_symbol`）；
  2. 真实合约元数据回退同品种 888 连续码（`multiplier`/`exchange`/`price_tick` 品种级不变）；
  3. 盘口一致性闸源 = `contract_daily`（日频，两级阈值 2%/5%）；
  4. 价空间默认 `raw`（偏移置 0），仅显式传 `adj` 才减偏移；
  5. `futures_symbol.price_tick` 列已增补（迁移 012），缺失时校验仅 WARN 跳过。
- **验收（现网）**：模块可导入、`/execution` 路由已注册、10 品种反解 9 OK（MA888 曾 BLOCK 12.10%，D4 已随合约切换自行闭环 → 10/10 OK）；集成测试 `scripts/test_execution_integration.py` 通过。
- **当前状态**：✅ 代码完成、数据新鲜度与 tick 覆盖达标，现网可产出可执行订单建议。

---

## 10. 实盘通道 P0-2（结论：Sprint1 立项，框架已建未部署）

> 完整规格见原《PRD P0-2 实盘通道 CTP+QMT》。

- **范围**：CTP（商品期货）+ QMT（商品期权/未来股票）双通道并行；路由/持仓管理/订单状态机/持久化/异步回执/撤单/软件止损/断电恢复。
- **架构**：两通道均为「网关进程 + HTTP」，qhyc 侧 `broker_http` 共用基类。
- **D1–D4 关联裁决（2026-10-05）**：D2 立项 Sprint1（SimNow，不接真实资金）。
- **当前状态（2026-10-05）**：
  - ✅ DDL `migrations/030_execution_channels.sql` 已应用云端并登记（4 表：execution_order / fill / position / channel_health；含 `idempotency_key` UNIQUE 防重复下单、`status` CHECK 状态机、net+long+short+today 持仓）。
  - ✅ 四核心模块已写（`persistence` / `position_manager` / `broker_http` / `execution_runtime`），AST + lint 0 错，含幂等防重复、开平推导（CZCE 平今收敛）、超时转查单不重试、断电恢复 `recover_on_boot`。
  - ⚠️ **未部署**：需 commit + push + 云端 pull + 重建容器；SimNow 仿真（B0–B9）/ 模拟账户 / 软件止损 / 风控联动均未验证。
- **验收路线**：Sprint1 架构骨架（A1–A8）→ Sprint2 CTP 仿真（B0–B9，含合规 10 日 20 笔）→ Sprint3 QMT → Sprint4 模拟账户打磨。**真实资金前须 `EXECUTION_ENABLED=1` 且 deploy_gate 实盘校验全 PASS**。

---

## 11. 服务层 / 展示层 / 部署运维

- **服务层 API**：FastAPI；`/health`、`/symbols`（403 鉴权启用）、`/execution/reverse`、`/execution/callback`（回执）。
- **展示层**：前端简报/看板（`web/dist`），`/` 与 `/assets` 正常。
- **部署运维**：
  - 云端 4 容器；`BACKUP_ENABLED=1`（每日 02:00 逻辑备份 `/app/runtime/backup` + 30min 告警巡检）。
  - `deploy_gate` 上线准入闸已挂 **cron 每日 06:30**（flock + 日志）；升级项：`main_contract_map` 近 7 天=0 → BLOCK、`price_tick<95%` → BLOCK、`execution.enabled=true` 时强阻断、纳入 P0-2 校验。
  - `portfolio_brake` 上线前确认独立可用后置 on。

---

## 12. 上线准入 SOP（结论）

`scripts/deploy_gate.py` 为权威总闸，覆盖：P0-1 模块/路由、表与列、覆盖度、开关、前端、HTTP。
**已升级**：`main_contract_map` 陈旧与 `price_tick` 不足升为 BLOCK；P0-2 通道/网关/DDL 入闸；镜像构建阶段 `pytest` 非零即失败（G8 方向）。
**绿灯 ≠ 可上线**：P0-2 未部署前不能通过「实盘就绪」判定。

---

## 13. 验收标准总表（按模块，结论性）

| 模块 | 关键验收指标 | 当前状态 |
|---|---|---|
| 数据层·去天勤 | `import tqsdk` 残留 = 0；三周期 akshare 取得 | ✅ 0 |
| 数据层·复权 | 默认 `cont_adj`；`roll_segment` 双门对拍；历史段零改写 | ✅ 默认；⚠ 无 daily/hourly（D1 方案 A） |
| 数据层·主力映射 | `main_contract_map` 追平 T-1、近 7 天 ≥ 30 品种 | ✅ 55 品种/T-1 |
| 数据层·tick | `price_tick` 覆盖 ≥ 95% | ✅ 2329/2612 |
| 数据层·日历 | `futures_rule` 已建、`calendar_mode=official` | ✅ 表已建；⚠ 官方源切换(G4)待复核 |
| 数据层·迁移台账 | `schema_migrations` 登记至 030 | ✅ 030 |
| P0-1 反解 | 模块导入/路由/10 品种反解/集成测试 | ✅ 10/10 OK |
| P0-2 通道 | DDL 030 应用 + 四模块 + SimNow 验证 | 🟡 DDL+框架已建，未部署/未仿真 |
| 运维 SOP | `deploy_gate` cron + BACKUP + 告警 | ✅ 已落地 |
| 前端/API | `/`=200、`/health`=200、`/symbols`=403 | ✅ |

---

## 14. 里程碑与当前进度

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 数据采集 + 数据层主体 | akshare 单一源、复权 offset、主力映射 | ✅ 完成 |
| M2 P0-1 执行反解层 | 代码 + 现网可用 | ✅ 完成 |
| M3 运维准入 SOP | deploy_gate + 备份 + 告警 + cron | ✅ 完成 |
| M4 P0-2 实盘通道 Sprint1 | 框架 + DDL | 🟡 框架已建未部署 |
| M5 P0-2 SimNow/模拟账户 | 仿真验证 | ⬜ 未启动 |
| M6 回测/策略打磨 | 容量梯度实盘偏差 | ⬜ 用户主导 |

---

## 15. 待裁定 / 已知问题（仅列结论，详情见《测试报告》）

1. **前复权删数据（D3）**：挂起，等 G9（`fut_kline.continuous` 退役/重定位）合并处置，避免顺序错不可逆。
2. **roll_segment daily/hourly（D1）**：方案 A 明确决策（维持 continuous + 显式标注），非数据缺口。
3. **MA888 口径（D4）**：随合约切换自行闭环，其余 888 与主力表不一致仅 JD/LH/SM 三个，纳入日常监控。
4. **G4 期货日历源**：✅ 已完成（2026-10-05）。新增唯一权威入口 `app/data/trade_calendar.py`（读 `futures_rule` 的 distinct `trade_date`），并收口 4 处原 `tool_trade_date_hist_sina` 调用（`app/scheduler.py` 融合推送日历、`app/ingest/backfill_rank.py` 预筛、`app/ingest/akshare_source.py` 回退源、`vendor/pipeline_src/run_pipeline.py`）；代码内已 0 处 active 引用股票日历。覆盖边界 fail-loud：超出已播种区间时告警/抛错，不静默套用 A 股日历。
5. **明文口令**：`qhyc_dev_pwd_2026` 硬编码 59 处 → 收敛为 env fail-fast（P2）。
6. **G7 与物理删 tqsdk 方向**：接受物理删，可复活性由 git 历史 + provider 抽象保证。

---

## 16. CB 交付清单（剩余项，按优先级）

### P0（上线前必须清零）
1. **P0-2 Sprint1 部署**：commit + push + 云端 pull + 重建容器，使四核心模块与 DDL 030 生效。
2. **P0-2 SimNow 仿真**：B0–B9 用例验证 + 合规 10 日 20 笔归档。

### P1（上线前应尽量完成）
3. **G4 期货日历源复核**：确认 `futures_rule` 官方源切换，弃股票日历。
4. **CI 打包测试**：镜像构建 `pytest` 非零即失败；补 `test_execution_broker.py`。
5. **风控联动确认**：`portfolio_brake` 独立可用，上线置 on。
6. **inferred 覆盖**：`dim_main_contract_inferred` 扩至全活跃品种（当前 63/91）。

### P2（健壮性增强）
7. **前复权删数据**（D3，与 G9 合并）。
8. **G5–G13 评审补强**逐条复核（尤其 G1 韧性层、G8 CI 门禁）。
9. **明文口令收敛**（59 处 → env）。
10. **一致性闸精度**：接入真实合约分钟线/实时 tick（盘中防错）。

---

> **结语**：本系统已具备「数据采集 → 数据层 → 策略信号 → 执行反解」完整链路并现网可用；实盘通道 P0-2 处于 Sprint1 框架落地阶段，是通往生产的总前提。后续改造严格对照本文件第 13 节验收总表与原专项 PRD 改造点索引（T/B/C/G 四系列 + P0-1/P0-2），未达标不视为完成。
