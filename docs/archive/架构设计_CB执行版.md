# 架构设计｜量化科普借鉴项融入 qhyc 预测系统（CB 执行版）

> 版本：v1.0　日期：2026-09-28　状态：待 CB 执行
> 上游：
> - 决策源 `PRD_借鉴落地验证.md`（v0.4，WB 评审，D1–D5 决策：全量融入 Docker、不拆分不独立）
> - 系统既有设计 `E:/Docker/qhyc/docs/详细设计.md`、`期货量化交易系统需求文档_PRD.md`
> - 因子层既有 PRD `E:/Docker/qhyc/docs/PRD/因子接入_PRD_20260926.md`（本设计的**接入基底**）
> - 现状核对 `E:/Docker/qhyc/docs/PRD与验收差距分析_20260927.md`、`系统验收测试报告_20260927.md`
> - 引擎真源 `E:/Docker/qhyc/app/strategies/fusion_signal.py`（`walk_fusion_states`）、`E:/Docker/qhyc/app/factor/asof.py`
> 目标读者：**CodeBuddy（开发执行方）**
> 铁律：① 不重写 V3.4 / `walk_fusion_states`；② 不破坏现有功能；③ 全部作为**分支补充**（默认关闭 = 旧行为）。

---

## 0. 一句话结论

借鉴项 V1–V6 **不是新架构**，而是挂载到 qhyc **已建成的因子层（factor layer）** 上的一组补充 + 1 个新增执行层风控标量（V5）。因子层（`app/factor/asof.py`）已实现 `available_at` 时间对齐、`FactorContext.lookup`、偏置乘子 `position_cap_scalar` / `entry_gate`、`max_weight` 启动校验——V1/V2/V3/V6 的"非冲突接入"机制**已存在**，CB 的工作是**注册新因子 + 补齐因子层闭环（T8–T10）+ 新增 V5 执行层标量 + V4 数据自检作业**，而非另写信号逻辑。

---

## 1. 系统上下文（现状，已实测）

### 1.1 运行时拓扑
`E:/Docker/qhyc` 多容器栈：`qhyc-api` / `qhyc-scheduler`（只读挂载 `./app:/app/app:ro`，不热重载）/ `qhyc-timescaledb`（PG `futures` `localhost:5432`）/ `qhyc-pipeline`（M8 简报）。行情真源 DolphinDB `localhost:8848`。

### 1.2 融合引擎（单一真源，禁止触碰逻辑）
- `app/strategies/fusion_signal.py`：`walk_fusion_states` 逐根生成持仓状态 0/1/2，与已验证回测口径逐位一致（V3.4：EMA140 方向 + ADX≥15 + 斐波汇流 + MA20 回踩 + 2×ATR 吊灯 + 阶梯加码 + 收盘判定）。
- 该文件已 `from app.factor import FactorContext, compute_bias_multipliers`，在仓位上限/入场门控处消费因子偏置乘子——**因子接入点已就位**。

### 1.3 因子层（接入基底，已部分建成）
| 资产 | 路径 | 状态（2026-09-27 实测） |
|---|---|---|
| 因子注册表 | `factor_registry` | 11 行（仅 B 组），**8 个在产因子未注册**（A 组 `f_close_loc`/`f_mom_1h`/`f_gap`/`f_atr_pct` 等未注册） |
| 因子值长表 | `factor_value` | 2,116,977 行（19 因子 × 91 品种 × 2015–2026），已入云 |
| 时间对齐 | `app/factor/asof.py:asof_join` | ✅ 已实现 |
| 取数唯一入口 | `app/factor/asof.py:FactorContext.lookup`（`available_at<=trade_date` 红线） | ✅ 已实现 |
| 偏置乘子 | `compute_bias_multipliers` → `position_cap_scalar` / `entry_gate` | ✅ 已实现 |
| `max_weight` 启动校验 | `validate_max_weight` + `config.py:FactorBiasConfig` | ✅ 已实现 |
| 横截面标准化 | `zscore`（winsorize + z） | ✅ 已实现 |
| 因子计算接入调度 | `app/scheduler.py` 因子作业 | ❌ 缺失（无因子 cron） |
| 断供降级验证 | T7 | 🟡 逻辑在，无验证 |
| 准入脚本 E1–E6 | T8 | ❌ 缺失 |
| `enabled=false` 回归 | T9 | ❌ 缺失 |
| 20 日滚动 IC 监控 | T10 | ❌ 缺失 |

> **结论**：V1/V2/V3/V6 落地的 80% 工作量 = 把因子层"闭环补全"（T8–T10 + 注册完整性 + 调度接入），这正是 `PRD与验收差距分析` 已点名的"因子工程未闭环"整改项。本设计**复用**该工作，不另起炉灶。

### 1.4 现有因子已含量能雏形
`factor_value` 中已存在 `f_vol_surge`（量能突进，仅零波动日 null），`f_atr_pct`（A 组波动率，IC 0.0157）。故 V1 量比/量标准分、V6 波动率分位数**部分特征已可派生**，新因子以补充为主。

---

## 2. 非冲突原则（CB 必须逐条遵守）

1. **不重写 V3.4**：`walk_fusion_states` 的状态机逻辑、参数、出场规则一个字不改。
2. **因子只调暴露，不触发交易**：仅通过 `position_cap_scalar`（压仓位上限）/ `entry_gate`（逆势侧禁开）两个既有插入点生效（因子接入 PRD §6.3 否决项）。
3. **默认关闭 = 旧行为**：新因子 `factor_registry.enabled` 默认 `false`（注册即中性）；V5 `portfolio_brake.enabled` 默认 `false`（标量=1.0）；V4 作业默认 `enabled=false`。上线须过 §7 门槛。
4. **`available_at` 唯一入口**：因子取数一律经 `FactorContext.lookup`，禁止在因子/接入代码里直连源表或手写时间过滤（因子接入 PRD §5.1 红线）。
5. **`max_weight` 硬上限 0.3**：任一因子 `default_weight > 0.3` 或全因子 `max_weight` 之和 > 1.0 → 服务**启动失败**（fail-fast，非运行期告警）。
6. **V5 不动状态机**：仅在"执行/仓位落地层"叠加全局标量，不改 `walk_fusion_states` 输出，故**不触发 Qi Analisy skill 双源同步**（skill 是状态机真源，非执行层风控）。
7. **双向等价性**：`enabled=false` 时系统行为须与"该因子/模块从未上线"逐位一致（T9 回归测试拦截）。

---

## 3. 模块分解：V1–V6 → 现有/新增

| 借鉴项 | 落地形态（CB 执行） | 触及文件 | 是否改引擎状态机 |
|---|---|---|---|
| **V1 量价维度** | 注册 3 个 price_volume 因子（`f_vol_ratio` 量比 VR、`f_vol_z` 量标准分 VZ、`f_voldiv_divergence` 量价背离），经既存 `compute_bias_multipliers` 接入 `position_cap_scalar`/`entry_gate`。路径 A（放量确认）= 入场门控偏置；路径 B（背离预警）= 压仓偏置 | `factor_registry`（INSERT）、`scripts/compute_factor_value.py`（A 组 bar_15m 派生，复用 `factor_ic_scan.build_factors`）、`config/local.yaml` 的 `factor_bias.weights` | 否（仅注册+计算） |
| **V2 前视加固** | 新增单测：断言 `lag_days=1` 因子在 T 日取不到 `available_at==T` 行（因子接入 PRD T2 断言点）；新增启动守卫：扫描 `factor_registry.data_sources` 均有对应 `_lag` 视图 / intraday 因子 `available_at=0`。期货盘后数据（持仓排名/仓单/基差）复用既有 `v_*_lagged` 视图 | `app/factor/asof.py`（补单测）、`app/core/bootstrap.py` 或 `fusion_scan` 启动处（加守卫） | 否 |
| **V3 过拟合五关** | = 执行因子接入 PRD **T8（E1–E6 准入脚本）+ T9（enabled=false 回归）+ T10（IC 监控）**，直接闭环"因子工程未闭环" | `scripts/`（新增 `admit_factors.py` / `factor_ic_monitor.py`）、`tests/` | 否 |
| **V4 数据质量自检** | 新增 `app/ingest/data_selfcheck.py`：缺失>5% 报警 + 切备用源、换月断层检测（复用 `build_continuous.py` 的 oi 主导逻辑）、`anomaly_ticket` 写入；`app/scheduler.py` 加 `data_selfcheck` cron（17:35，在 `rank_position` 17:30 之后） | `app/ingest/data_selfcheck.py`（新）、`app/scheduler.py`、`anomaly_ticket` 表复用 | 否 |
| **V5 组合回撤熔断** | 新增执行层风控标量模块 `app/risk/portfolio_brake.py`：读组合权益曲线（由 `fusion_position` + 已平仓盈亏派生，或新增 `portfolio_equity` 轻表），按回撤分位算 `portfolio_brake_scalar` ∈ {1.0, 0.5, 0.0}（阈值 **15%/25%**，D2），在 scheduler 提交 `FusionPosition` 时乘以期望手数。**默认 1.0（关闭）** | `app/risk/portfolio_brake.py`（新）、`app/scheduler.py` 仓位落地处、`config/local.yaml` 的 `portfolio_brake` | 否（仅执行层，不动状态机） |
| **V6 波动率分位数** | 注册 `f_atr_pctile`（ATR 250 日分位），接入 `position_cap_scalar`（高位降仓/观望、低位警戒），与 V1 同机制 | `factor_registry`（INSERT）、`scripts/compute_factor_value.py`、`config/local.yaml` | 否 |

> **与 PRD v0.4 的修正说明**：v0.4 写"V1 移植过滤器框架进 `fusion_signal.py`"。实测引擎已通过 `compute_bias_multipliers` 消费因子偏置——故 V1/V6 的"融入"= **注册为因子并被既有乘子消费**，无需在 `fusion_signal.py` 内写任何新过滤代码。这比 v0.4 字面表述**更非冲突**，是推荐实现。

---

## 4. 数据流（端到端，不破坏现有链路）

```
[行情/基本面源] → compute_factor_value.py（A组 bar_15m / B组基本面，复用既有）
      ↓ upsert factor_value(z_value, available_at)   ← 禁止直连，走 ctx.lookup
[factor_registry]（enabled / default_weight / max_weight / lag_days / data_sources）
      ↓ FactorContext.load() 缓存
[fusion_scan 每 :05/:35] → walk_fusion_states(...) → state 0/1/2   ← 单一真源，不改
      ↓ compute_bias_multipliers(z_by_factor, registry)            ← 既有插入点
   position_cap_scalar / entry_gate  → 期望手数
      ↓ app.risk.portfolio_brake.apply(期望手数, equity_curve)     ← V5 新增，默认×1.0
   调整后手数 → 提交 FusionPosition（状态变化才推送）
```
- 因子层对 V3.4 的影响**仅**是缩放"期望手数上限"与"逆势侧是否开仓"，不动 state 本身。
- V5 在最末端做组合级缩放，与因子乘子**乘法合成**，互不干扰；关闭时 = 恒等变换。

---

## 5. 配置开关（均在 `config/local.yaml` / `cloud.yaml`）

```yaml
# 既存（因子接入 PRD §5），本设计扩展 weights
factor_bias:
  enabled: true
  gate_threshold: 0.0      # entry_gate 低于此值禁逆势开仓
  cap_floor: 0.5
  gate_floor: -1.0
  degrade_missing: floor   # 因子缺失→最保守地板（T7 降级）
  max_weight_registry_sum: 1.0
  weights:
    f_vol_ratio:     0.10   # V1 量比（初设，须过 E1–E6 才生效）
    f_vol_z:         0.10   # V1 量标准分
    f_voldiv_divergence: 0.08  # V1 背离预警（路径B，压仓）
    f_atr_pctile:    0.10   # V6 波动率分位
    # （既有 B 组因子保持不变）

# 新增（V5）
portfolio_brake:
  enabled: false           # 默认关闭 = 旧行为
  dd_warn: 0.15            # 回撤>15% → scalar=0.5（降半仓）
  dd_stop: 0.25            # 回撤>25% → scalar=0.0（清仓观望）
  lookback: 250            # 权益峰值回看窗口（交易日）

# 新增（V4）
data_selfcheck:
  enabled: false           # 默认关闭
  missing_pct_alarm: 0.05  # 单合约单日缺失>5% 报警
  failover_source: 天勤   # 主 akshare 失败时切换
```

> 硬约束：任一 `weights` 项 `> max_weight(0.3)` 或全因子 `max_weight` 之和 > `max_weight_registry_sum(1.0)` → `validate_max_weight` 启动即失败。

---

## 6. 实施任务（CB 可执行，T 编号接续因子接入 PRD 的 T12）

### 阶段一 · P0（必须，对应 PRD M1/M2）
- [x] **T13** `factor_registry` 补全注册：把 A 组 4 + C 组 2 等未注册因子补齐（关闭"因子工程未闭环"P0 项），含 `horizon`/`lag_days`/`max_weight`。（复用 `scripts/compute_factor_value.py` 已算值，仅补元数据）
- [x] **T14** `scripts/compute_factor_value.py` 新增 V1 三因子（VR/VZ/背离）与 V6（`f_atr_pctile`）的 A 组派生逻辑，复用 `factor_ic_scan.build_factors` 与 `bar_15m`；落 `factor_value` 幂等 upsert；`available_at=0`（intraday）。
- [x] **T15** `factor_registry` 注册 V1/V6 新因子（`enabled=false` 初始），写入 §5 的 `weights` 初值。
- [ ] **T16（V2）** `app/factor/asof.py` 单测：构造 `lag_days=1` 假因子，断言 T 日取不到 `available_at==T` 行；新增启动守卫扫描 `data_sources` 均有 `_lag` 视图或 intraday 标记。
- [x] **T17（V3=T8）** 新增 `scripts/admit_factors.py`：自动跑「有因子 / 无因子」两组回放，输出 E1（相关性<0.3）/ E2（ablation MAR 净升、回撤不恶化）/ E3（IS/OOS 同号）/ E4（扣 2bp/5bp 仍正）/ E5（可关闭）/ E6（断供降级）六项判定。
- [ ] **T18（V3=T9）** `enabled=false` 回归测试：断言关闭全部新增因子后，引擎输出与"因子从未上线"逐位一致（diff 基线）。

### 阶段二 · P1
- [x] **T19（V4）** 新增 `app/ingest/data_selfcheck.py` + `app/scheduler.py` 的 `data_selfcheck` cron（17:35）；缺失>5% 报警切源、`build_continuous` oi 主导逻辑做换月断层检测、`anomaly_ticket` 写入。
- [x] **T20（V5）** 新增 `app/risk/portfolio_brake.py` + `config.portfolio_brake`；在 scheduler 提交 `FusionPosition` 处乘 `portfolio_brake_scalar`（默认 1.0）；不触及 `walk_fusion_states`。
- [x] **T21（V3=T10）** 新增 `scripts/factor_ic_monitor.py`：20 日滚动 IC 监控，连续 10 日转负告警 + 建议 `enabled=false`。

### 阶段三 · P2（储备）
- [x] **T22** 因子计算接入 `app/scheduler.py` 持续增量（关闭"因子工程未闭环"P1）；与 V4 自检共用日终流程。
- [ ] **T23** `factor_value` 按季度分区（数据量大后）。

---

## 7. 验证门槛（不可妥协，对应 PRD D3）

一项固化入引擎，须同时过：

| 门槛 | 内容 | 来源 |
|---|---|---|
| **因子准入 E1–E6** | 相关性<0.3 / ablation MAR 净升 / IS·OOS 同号 / 成本压力 / 可关闭 / 断供降级 | 因子接入 PRD §8（= PRD V3 五关） |
| **配对归因三重判据** | 参数平台 / 跨体系同向 / 横截面分半（V1 量价须过，否则降级为仅压仓偏置） | PRD §0.3 / backtest-merge |
| **两道硬闸门** | 价格取负对称、随机入场对照 | PRD §0.3 |
| **`available_at` 红墙** | 任一因子无法绕过 `FactorContext.lookup` | 因子接入 PRD §3/§7 + V2 单测 |
| **`max_weight` 启动失败** | 超限即 fail-fast | 因子接入 PRD §6.2/§11 V3 |
| **双向等价性** | `enabled=false` == 从未上线 | T9 回归 |

> V1 特别：路径 A（放量确认作入场门控）经配对归因若被判定"与入场不正交、误杀优势单" → **不启用 `f_vol_ratio` 的 `entry_gate` 偏置**，仅保留 `f_voldiv_divergence` 的压仓偏置（路径 B，正交可移植）——即 PRD D1 降级在因子层内的实现，仍属引擎内、非外部简报。

---

## 8. 分支工作流与 Docker 运维

```
本地 /e/Docker/qhyc 开发  →  git push  →  服务器 git pull
   →  docker compose -f docker-compose.cloud.yml build && up -d
```
- 改 `app/`（引擎/采集器/因子/风控）后须 **`docker restart qhyc-api qhyc-scheduler`**（只读挂载 `./app:/app/app:ro` 不热重载）。
- V5 新增 `app/risk/` → 同理 restart；因不动 `walk_fusion_states`，**不需**同步 Qi Analisy skill（双源同步铁律仅约束状态机真源）。
- V1/V6 仅改 `factor_registry` 数据 + `compute_factor_value.py` + `config.yaml` → 前者为库数据（restart scheduler 后 `FactorContext.load` 即生效），后者为配置（restart 生效）。
- 容器内查进程用 `/proc`（无 `ps`）；长任务 `docker exec -d`，勿宿主机 `nohup &`。
- PG 长回填防 WAL EINTR（Windows Docker）：V4 注入测试多所串行。

---

## 9. 风险与回滚

| 风险 | 应对（回滚即默认） |
|---|---|
| 新因子污染 V3.4 优势 | 全部 `enabled=false` → 引擎输出 = 旧行为（T9 保证）；`max_weight` 越界启动即失败 |
| V5 误刹 | `portfolio_brake.enabled=false` → scalar 恒 1.0，等价无刹车 |
| V4 自检误报/阻塞 | `data_selfcheck.enabled=false`；写入 `anomaly_ticket` 不阻塞决策链 |
| 因子绕过 `available_at` | V2 单测 + 启动守卫拦截；代码走查双重保证 |
| 调度冲突 | V4 cron 排在 `rank_position`(17:30) 之后 17:35，避开复权/合成竞态（见验收报告 §5.2.1 竞态整改） |

**回滚总原则**：任何新增项均带 `enabled` 开关，默认 `false`；出问题只需改配置 `false` + `docker restart`，无需回退代码。

---

## 10. 验收标准（CB 勾选）

- [x] V1 三因子 + V6 已在 `factor_registry` 注册（`enabled=false` 初始），`factor_value` 有值。
- [ ] V2 单测覆盖 `lag_days=1` 取不到断言；启动守卫扫描 `data_sources` 完整性。
- [ ] V3 T17 产出 E1–E6 可复跑报告；T18 `enabled=false` 回归 diff 全 0。
- [ ] V4 `data_selfcheck` 作业存在且注入"3 日缺失/成交量连续 0"样本能报警 + 切源。
- [ ] V5 `portfolio_brake` 历史净值回放：回撤>15%→scalar 0.5、>25%→0.0；`enabled=false` 时逐位等于无刹车。
- [ ] 全因子 `max_weight` 之和 ≤ 1.0，越界启动失败被验证。
- [ ] 本地 `git push` → 云端 `pull` → `docker compose build && up -d` → `docker restart qhyc-api qhyc-scheduler` 后，既有 `fusion_scan`、简报、回测基线（9/9 年全正、MAR 4.14）**无变化**（对照验收报告 §2 基线）。

---

*文档结束。与 `因子接入_PRD_20260926.md` 冲突处以该 PRD + 本设计 §2 非冲突原则为准；与 `PRD_借鉴落地验证.md` 冲突处以 PRD 决策（D1–D5）为准。*
