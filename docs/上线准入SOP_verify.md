# 上线准入 SOP —— deploy_gate 校验脚本

> **性质**：部署/发布前的强制准入闸（对应 `PRD缺口审视_20261001.md` §2 P1-4「策略/因子上线准入 SOP」的**系统层**落地版）
> **脚本**：`scripts/deploy_gate.py`
> **制定**：2026-10-02｜**配套**：`qhyc_模块缺口核查_20261002.md`、本仓库 `docs/PRD_P0-1_执行反解层_20261002.md`

---

## 0. 目的

把"能不能上线"从**主观判断**变成**一道可机检的闸**：任何一次发布/镜像更新前跑一遍 `deploy_gate.py`，任意 `BLOCK` 项未清零 → 退出码 1 → CI/人工拦截，禁止发布。

本 SOP 同时固化了 10-02 实测对 10-01 文档的**勘误结论**（walk-forward/监控/备份/鉴权/数据均已实现或恢复，仅默认关闭），并纳入新的真缺口（P0-1 执行反解层、§10 前端）。

---

## 1. 怎么跑

### 1.1 容器内（推荐，环境最完整）
```bash
docker exec -w /app -e PYTHONPATH=/app qhyc-api \
  python scripts/deploy_gate.py
# 落地 JSON 报告：
docker exec -w /app -e PYTHONPATH=/app -e DEPLOY_GATE_JSON=/app/runtime/deploy_gate.json \
  qhyc-api python scripts/deploy_gate.py
```

### 1.2 本地仓库（需装依赖）
```bash
cd E:/Docker/qhyc
PYTHONPATH=. python scripts/deploy_gate.py
```

### 1.3 环境变量
| 变量 | 默认 | 作用 |
|---|---|---|
| `DEPLOY_GATE_JSON` | 无 | 结构化报告落盘路径 |
| `REQUIRE_AUTH` | 0 | =1 时"鉴权未启用"视为 BLOCK（上云前应置 1） |
| `GATE_HTTP` | 1 | 是否做 `/assets`、`/symbols` HTTP 探测 |

---

## 2. 校验项清单（6 大类）

| # | 类别 | 校验项 | 失败级别 |
|---|---|---|---|
| 1 | **P0-1 执行反解层** | `app/execution` 可导入且含 `resolve_signal_to_order` | BLOCK |
| 1 | 同上 | `/execution` 已注册到 `api_router` | BLOCK |
| 2 | **关键表/列** | `futures_symbol` / `main_contract_map` / `roll_segment` 存在 | BLOCK |
| 2 | 同上 | `futures_symbol.multiplier` 存在 | BLOCK |
| 2 | 同上 | `futures_symbol.price_tick` 存在（P0-1 新增列） | **WARN**（待 DDL 增补） |
| 3 | **数据覆盖度** | `roll_segment` 有行 | BLOCK |
| 3 | 同上 | 近 7 天 `main_contract_map` 主力品种数 ≥ 30 | WARN |
| 3 | 同上 | 主力/连续品种 `multiplier` 非空覆盖 ≥ 95% | WARN |
| 3 | 同上 | `price_tick` 非空覆盖（缺失仅 WARN） | WARN |
| 4 | **运维开关** | `BACKUP_ENABLED` 状态（默认 off→WARN） | WARN |
| 4 | 同上 | `PORTFOLIO_BRAKE_ENABLED` 状态（默认 off→WARN） | WARN |
| 5 | **前端产物** | `/app/web/dist` 存在（§10 看板） | **WARN**（真缺口，待构建） |
| 6 | **HTTP 探测** | `GET /assets/` → 200 | WARN |
| 6 | 同上 | `GET /symbols` → 401/403（鉴权启用） | WARN（REQUIRE_AUTH=1 时为 BLOCK） |

---

## 3. 退出码与发布闸门
- **退出码 0**：无 BLOCK（WARN 允许发布，但须人工确认 WARN 清单）。
- **退出码 1**：存在 BLOCK → **禁止发布**，先清零 BLOCK 项。
- CI 接入示例（伪代码）：
  ```yaml
  deploy_gate:
    script: docker exec ... python scripts/deploy_gate.py
    # 非零退出即失败，阻断后续发布步骤
  ```

---

## 4. 与既有 verify 脚本的关系
- `scripts/verify_ops.py`：聚焦 P1-1 备份 + P1-2 告警巡检（运维层）。
- `scripts/verify_factor_jobs.py` / `verify_appendix_c.py`：聚焦因子/附录 C 准入（研究层）。
- **`scripts/deploy_gate.py`（本 SOP）**：聚焦**系统层整体准入**（模块/表/数据/开关/前端/鉴权），是发布前的总闸，覆盖前两者未触及的执行反解层与前端。

---

## 5. 维护约定
- 新增系统级模块（如自动下单通道）时，**同步在 `deploy_gate.py` 增一项 BLOCK 校验**。
- 每次修订 `PRD缺口审视` 的缺口状态后，若涉及"是否已实现"判定，回来更新本脚本的级别（BLOCK/WARN）以保持与文档一致。

## 6. P0-1 落地补遗（CB 2026-10-02，对应 PRD §9）

执行反解层已落地，`deploy_gate.py` 的 P0-1 校验项同步增强（模块导入 / `/execution` 路由 / 表列 / **价空间口径校准**）。

- **`price_tick` 列**：PRD §6 规划的列由迁移 `012_execution_price_tick.sql` 新增；数值由 `scripts/seed_price_tick.py` 从行情数据**派生**（非编造），缺失时 tick 校验仅 WARN 跳过。部署时需先应用该迁移（云端库当前尚无此列）。
- **口径校准**：`scripts/calibrate_price_space.py` 以 `contract_daily` 真实合约日频价为真值，裁决 888 信号价空间（实测为 **raw 原始连续价**），`reverse_price` 默认 `EXECUTION_PRICE_SPACE=raw`（偏移置 0）。该脚本作为 deploy_gate 的 `execution.calibration` 子项（WARN 级）。
- **一致性闸源**：实测 `hourly_bar`/`bar_*` 仅存 888 连续序列、无真实合约分钟线，故盘口一致性闸改用 `contract_daily`（日频）。日频参照引入 `DAILY_TOL`(2%)/`BLOCK_TOL`(5%) 两级阈值（见 `app/execution/validator.py`）；`daily_bar` 无涨跌停列，该闸暂静默跳过。
- **真实合约来源**：取 `main_contract_map.underlying`（非 `main_symbol`）；真实合约元数据经 `real_contract_metadata` 回退同品种 888 连续码（multiplier 等品种级不变）。
- **联调前置**：完整库内校验需 `git push → 云端重建` 并应用迁移 `012` 后，再跑 `deploy_gate.py`。
