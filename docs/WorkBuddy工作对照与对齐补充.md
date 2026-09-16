# WorkBuddy 工作对照与对齐补充（qhyc × 桌面/融合 文件夹）

> 生成时间：2026-09-16 ｜ 生成方：WorkBuddy
> 配套阅读：CodeBuddy 已产出 `docs/状态总结_对齐用.md`（下称"CB总结"）。本文是其**补充**，重点补齐：
> 1. WorkBuddy 在 **`C:/Users/Seven/Desktop/融合/`** 文件夹的产出（CB总结完全未覆盖，因为不在 qhyc 内）；
> 2. WorkBuddy 在 qhyc 内 `runtime/`、`app/strategies/`、`app/notify/` 的产出；
> 3. 两边重叠点的对齐与冲突文件清单补全。
> 建议把两份文档合并为单一"项目状态书"，避免双份真相。

---

## 0. 总览：谁做了什么（一表对照）

| 工作域 | CodeBuddy（CB总结已覆盖） | WorkBuddy（本文补充） |
|---|---|---|
| 数据入库/抓取链路 | ✅ akshare+tqsdk 主链路、纠错文档 | —（仅修正文档口径，见 §3.2） |
| 复权（hourly 维度） | ✅ 缺口定位、待修 OI 门槛 | 复权（**daily 维度**）已完成（§1.1） |
| 复权（daily 维度） | 未提及 | ✅ `adjust_all --freq daily` 已产（§1.1） |
| 120万回测重跑+参数对比 | 未提及 | ✅ 已完成（§1.2） |
| 信号引擎 / 推送 | 未提及（仅提 fusion.py router） | ✅ `fusion_signal.py`+`pushplus.py`+定稿守卫（§1.3） |
| 特征/模型/前端/看板 | ✅ 大量新增 | — |
| **桌面/融合 文件夹产出** | ❌ 完全未覆盖 | ✅ 终版资料 01–04 + 策略研究报告（§2） |
| 数据标准文档 | ✅ `数据入库逻辑与纠错说明.md` | ✅ `01_数据储存获取更新标准.md`（另址，§3.2） |

---

## 1. WorkBuddy 已完成（qhyc 内）

### 1.1 日线复权（Task 1）— ✅ 已完成
- **链路**：`fut_kline` 的 hourly(continuous+contract) → 聚合出 daily(continuous/contract) → `adjust_all --freq daily` 生成 `daily/cont_adj`（加法平移前复权）。
- **核心脚本（在 `E:/Docker/qhyc/runtime/`，为临时研究脚本，非上线代码）**：
  - `_build_daily_continuous.py`（聚合 daily/continuous，197,661 行 / 73 品种）
  - `_build_daily_contract.py`（聚合 daily/contract，715,439 行 / 2,864 合约，已验证与天勤真日线 100% 对齐）
  - `_bt_multi.py`（回测脚本，含品种代码大小写归一修复：`FG888`↔`KQ.m@CZCE.FG`，使 50 品种全匹配）
- **结果（用户手动启动 Docker 后跑出）**：`daily/cont_adj` = **60,878 行 / 47 品种 / 2019-04 ~ 2026-09-15**。
- **已知缺口**：3 个低流动性品种因持仓量门槛 `RELIABLE_OI` 跳过 → 我这边缺 **`pb`(铅)/`sn`(锡)/`sc`(原油)** 的 daily cont_adj（与 CB总结 §2.1 的 hourly 缺口品类略有差异，见 §3.3）。

### 1.2 120万回测重跑 + 参数对比（Task 4）— ✅ 已完成
- 用 `_bt_multi.py` 直读 `fut_kline.hourly`（kind=continuous / cont_adj）重跑，复现"原 +1,209,197 元"结论的方法学。
- **结果**：fdf continuous 5bp 成本 **+17.8万**（PF1.05，不加权）/ +12.9万（加权）；fdf cont_adj **+12.5万**（PF1.04）；tqsdk 旧口径 **-20.2万**。
- **结论**：原 +120.9万 **无法 1:1 复现**（根因：原 continuous 小时线数据集已不在库 + 策略对成本极敏感，5bp盈/20bp+全亏）。
- **交付物**：`桌面/融合/终版资料/04_一百二十万回测方法维度步骤/` 下 `scripts/_bt_*.py` + `results/*.json` + `回测重跑与参数对比结果_20260916.md`。

### 1.3 信号引擎 / 推送代码（新增 + 修正）— ✅ 已落码
- 新增（未在 CB总结冲突清单内，见 §3.4）：
  - `app/strategies/fusion_signal.py` — 融合状态机 `fusion_state_detail`、单源读取 `read_hourly_bars`、定稿守卫 `drop_forming_bars`/`drop_unsettled`。
  - `app/notify/pushplus.py` — pushplus + 通用 webhook 降级 + `FusionPushLog` 留痕。
  - `app/ingest/hourly_collector.py` — 小时线在线采集（akshare 新浪主源 + tqsdk 兜底，全品种复用单连接）。
  - `app/api/routers/fusion.py` — 融合信号 API（若由 CB 新增，请核对分工）。
  - `app/scheduler.py` — 融合扫描每 15 分钟 + 定稿守卫采集。
- **今日修正**：`fusion_signal.py:336` 注释"同花顺图上看到的 K" → "akshare 新浪主连分时"（**实际主源是新浪 sina，非同花顺**；该误注是 03 文档旧误写的源头，已一并纠正）。

---

## 2. WorkBuddy 已完成（桌面/融合 文件夹 — CB总结未覆盖，因不在 qhyc）

### 2.1 终版资料 01–04（本会话核心交付，用户指定路径）
路径：`C:/Users/Seven/Desktop/融合/终版资料/`
- **01 数据储存获取更新标准**：今日已按代码实况重写 → ①落盘位置=Docker 内 TimescaleDB 库 `futures`（物理卷 `E:/Docker/qhyc/pgdata`）；②日线/小时线**双表架构**（运行时 `daily_bar`/`hourly_bar` + 回测底座 `fut_kline`）；③写明数据更新规则（日线每日三次 cron、小时线 16:00 联动 + 每15分钟定稿守卫、回测底座手动重建、周/月维护）；④纠"DolphinDB"误记为 TimescaleDB、纠"同花顺"误记为新浪。
- **02 融合引擎与额外策略最终版**：原样复制 `融合策略V2.0_额外执行口径_完整版.md` + 4 个同花顺/文华公式 txt（不改）。
- **03 信号获取推送机制频率代码设计**：今日已同步 01 标准（§2.4 重写：落盘/双表/更新规则/新浪主源纠错）。
- **04 一百二十万回测方法维度步骤**：方法学 + Task4 重跑结果 + 参数对比 + scripts + results。

### 2.2 策略研究 / 回测报告（WorkBuddy 历史产出，项目资料）
`C:/Users/Seven/Desktop/融合/` 下含大量研究报告（CB总结未列，因在 qhyc 外）：
- 融合策略 48 品种实盘回测、甲乙额外执行口径对比、盘中触碰实盘模拟、主连基准对账、回测与现实偏差防护 SOP、逐笔对账说明、近期成交样本 50 笔等（`.md`/`.html`/`.csv`/`.json`）。
- 融合策略 V2.0 实盘操作手册、指标使用说明、同花顺/文华指标代码、方向层 EMA 周期对比、容量5门控鲁棒性等。
- **用途**：这些是研究/对照资料，**非上线代码**；其中的"融合策略 V2.0_额外执行口径"即为 02 文档的源。

---

## 3. 两边重叠 / 需对齐点

### 3.1 复权维度：daily（WorkBuddy）vs hourly（CodeBuddy）— 互补不冲突
- 机制共用 `app/ingest/fdf/{build_continuous,adjust_fdf}.py`。
- WorkBuddy 跑的是 **`--freq daily`**（已产 47 品种）；CodeBuddy 关注 **hourly** 维度的缺口（§2.1/§2.5）。两者输出不同 freq，不互相覆盖。
- **建议**：复权缺口裁定（§4.1）后，hourly 由 CodeBuddy 补齐、daily 已由 WorkBuddy 完成；如需统一重跑，两边共用 `adjust_all` 即可。

### 3.2 数据标准文档：两份并存，需合并为单一权威
- WorkBuddy：`桌面/融合/终版资料/01_数据储存获取更新标准.md`（"标准"口径，含双表/落盘/更新规则）。
- CodeBuddy：`docs/数据入库逻辑与纠错说明.md`（"纠错"口径，侧重 akshare/tqsdk 差异）。
- **风险**：两份都在描述同一套数据架构，后续开发若各信各的会分叉。**建议合并为 qhyc 内单一权威文档**（可保留 01 为标准主文，把纠错说明作为附录/引用）。

### 3.3 三品种缺口差异：需核实
- WorkBuddy（daily cont_adj）缺：**pb(铅)/sn(锡)/sc(原油)**。
- CodeBuddy（hourly cont_adj）缺：**sc(原油)/ru(橡胶)/sn(锡)**。
- 差异在 **铅(pb) vs 橡胶(ru)**。待裁定时一并确认是"日/小时维度差异"还是"数据本身差异"，避免修补漏掉其一。

### 3.4 git 冲突文件清单补全（CB总结 §4 漏列 WorkBuddy 新增）
CB总结 §4 列了 `config/domain/engine/backtest/predictors/api` 等；**未列 WorkBuddy 新增/修改的**：
- `app/strategies/fusion_signal.py`（新增）
- `app/notify/pushplus.py`（新增）
- `app/ingest/hourly_collector.py`（新增，untracked）
- `app/api/routers/fusion.py`（新增，untracked）
- `app/scheduler.py`（修改，CB总结未列）
- **以上文件合并时同样易冲突，请纳入分工共识。**

### 3.5 权威数据口径裁定（CB总结 §2.3）影响两边
- 该裁定（线上喂模型/看板以哪套表为准）直接决定：WorkBuddy 的 01 标准"双表架构"如何落地、CodeBuddy 的看板/API 读哪张表。**需先于上层联调裁定。**

---

## 4. 待裁定 / 需用户拍板（整合 CB总结 + 本文）

| # | 事项 | 优先级 | 归属 | 说明 |
|---|---|---|---|---|
| 4.1 | 复权 OI 门槛：低流动性品种（sc/ru/sn/pb）如何复权 | 高 | CodeBuddy 主 + WorkBuddy 配合 | 选项：A 按品种设可调阈值 / B 改用成交量选主力 / C 接受不进复权 |
| 4.2 | 复权是否接入自动调度 | 高 | 共担 | `adjust_fdf` 当前仅手动；建议接在每日收盘采集后 |
| 4.3 | 权威数据口径（akshare 未复权表 vs 天勤复权表） | 高 | 共担 | 决定喂模型/看板读哪套；影响 01 标准落地 |
| 4.4 | hourly_bar 覆盖偏稀（~2400/品种）是否补采 | 中 | 视 4.3 | 先定口径再决定 |
| 4.5 | 陈旧 cont_adj 全量重跑 | 中 | CodeBuddy 主 | 57 品种统一重跑 |
| 4.6 | **git 基线 / 分工冲突** | **紧急** | 用户+两边 | 仅1提交全未提交；MM/AM 互相覆盖风险；先 commit baseline + 各自分支 |
| 4.7 | 两份数据标准文档合并为单一权威 | 中 | 共担 | 见 §3.2 |
| 4.8 | 桌面终版资料 01–04 是否作为开发唯一参考 | 建议 | 用户确认 | WorkBuddy 主张：是 |

---

## 5. 后续计划

**立即（今天，与 CB总结一致）**
- 5.1 建立 git 基线（commit baseline），WorkBuddy / CodeBuddy 各开分支，杜绝互相覆盖（对应 4.6）。
- 5.2 用户裁定 4.1 / 4.2 / 4.3 三个高优项。

**WorkBuddy 计划**
- 5.3 **信号层参数扫描**：对 `sl_atr / trail_atr / be_r / entry_mode / W` 做 `--param-sweep`，进一步逼近原 120 万量级（Task 4 的自然延伸，待用户确认执行）。
- 5.4 **文档统一**：推动 01 标准 + CodeBuddy 纠错说明合并为单一权威；确保 02/03/04 终版资料被后续开发引用。
- 5.5 **上层/特征线**（按 CB总结 §3 建议归属 WorkBuddy）：`position_factors.py`、`reversal_model.py` 接入与单测；`dashboard.py`/`position.py` API 与 `web/*` 前端联调；`engine.py`/`weights.py` 与 `research_m5/m6a` 结论收敛。
- 5.6 复权缺口裁定后，协助对 daily+hourly 统一重跑验证。

**清理（防误入库）**
- 工作区残留临时产物：`qhyc/_build6.flag`、`_compile6.txt`、`_mem*.txt`、`_docschk.txt`（CB总结已提）；
- WorkBuddy 的 `qhyc/runtime/_*.py` / `*.log` / `*.csv` / `backup_*.json` 均为临时研究脚本与日志，**应加入 `.gitignore` 或清理，勿入库**。

---

## 6. 给 CodeBuddy 的对齐要点
- **桌面 `融合/终版资料/01–04` 是 WorkBuddy 的权威交付**，开发请以 01（数据标准）、03（信号/推送）为准；不要另起一套数据架构描述。
- **复权**：daily 维度 WorkBuddy 已完成；hourly 维度归你；机制共用 `app/ingest/fdf/*`，勿重复实现 `adjust_fdf`/`fetch_fdf`。
- **信号/推送**：`app/strategies/fusion_signal.py`、`app/notify/pushplus.py`、`app/ingest/hourly_collector.py`、`app/scheduler.py` 已是 WorkBuddy 产物；如需改信号逻辑，须同步回测 `_bt_*.bt_symbol`（单一真源铁律）。
- **未复权 vs 复权**：`hourly_bar`/`daily_bar` 是 akshare 未复权；`fut_kline.cont_adj` 是天勤复权权威；混读会砍半周期（已在 01 §5.6 写明）。
- 冲突文件清单请以本文 §3.4 补全后的版本为准。
