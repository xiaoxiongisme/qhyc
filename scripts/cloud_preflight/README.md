# 云端预检 harness（007 / 008 / 009 + L2 A/B 对拍）

> 本地无 DB，本目录所有脚本**只在云端 STAGING 克隆库上执行**，绝不连生产。
> 目的：在动 2026 补采、跑存储过程写库之前，把风险挡在前面。

## 重要发现（写本 harness 时确认）

### 发现 1 · `sp_build_l2_roll_segment` 不是 Python 版（`build_roll_segments.py` 已 252/252 验收）的忠实移植
逐条比对，有 4 处实质差异：

| 维度 | Python 基准（验收版） | sp 版（009） | 后果 |
|---|---|---|---|
| 检测锚点 | **统一 15m 锚定**再映射到目标 freq | 直接在目标 freq 跑双门（无固定锚） | 真实数据上 30m/60m 换月位置不一致（Python 注释实测 30/60m 段数少 17%） |
| roll_delta 基准 | **锚点 gap = `open[i]-close[i-1]`** | `cc = close[i]-close[i-1]` | 各周期累积偏移量级发散 |
| price_shift | `ceil((-min+0.01·range)/100)·100`，仅当 min≤0 | `abs(min(cum_offset))` 对所有行 | 数值与触发条件都不同 |
| src_freq | 恒 `min15` | 等于 `p_freq` | 治理口径不一致 |

**结论**：二者不是「同一算法的两个实现」，不能直接行级相等 A/B。
本 harness 改为比对 **换月位置（seg_start）一致性 + cum_offset 相对形状**，并把上述差异显式列为「已知分歧、需产品决策」。

### 发现 2 · `sp_build_l1_from_minute` 列名 bug（`oi` → `open_interest`）
`009` 原 INSERT 列名写成 `oi`，但 `bar_5m` 真实列是 `open_interest`（`scripts/load_1min.py:125`）。
当前因 1m 缺失是 no-op 未暴露；2026 补采把 1m 补回后该 sp 会直接报错。
**已修复 009**，并由 `03_sp_l1_fixture.sql` 在预检中显式验证 INSERT 路径。

## 文件清单
- `00_apply_migrations.sql` — 在 staging 上应用 007/008/009（`\i` 聚合）
- `01_validate_dict_cfg.sql` — 字典/配置表断言（计数 / 开关对齐现状 env / FK / 视图）
- `02_fixture_and_roll_ab.py` — **L2 A/B 对拍**（sp vs Python），synthetic 模式全程清理
- `03_sp_l1_fixture.sql` — L1 聚合夹具，专门捕获 `oi` 列名 bug
- `run_preflight.sh` — 编排以上四步

## 运行方式
```bash
# 1) 准备一个生产 schema 的克隆库（如 qhyc_staging），配置 PG 环境变量
export PGHOST=... PGPORT=5432 PGUSER=... PGPASSWORD=... PGDATABASE=qhyc_staging

# 2) 跑全套预检（默认 synthetic，安全无污染）
bash scripts/cloud_preflight/run_preflight.sh synthetic

# 3) 想看真实数据上的锚点分歧（30m/60m 位置差异），用 real 模式 + 真实品种
bash scripts/cloud_preflight/run_preflight.sh real --symbol RB888 --freqs min15,min30,min60
```

## 如何解读 `02` 的输出
- `[min15] 位置一致=true` + 退出码 0 → 双门数学本身自洽（A/B 在 min15 上同源）
- `delta 基准一致=false` → 已知分歧#2（A 用 cc / B 用 gap），属预期
- `price_shift` A≠B → 已知分歧#3，属预期
- `[min30]/[min60] 位置一致=false` → 已知分歧#1（锚点），**非 bug**，正是需要产品拍板的项
- 若 `[min15] 位置一致=false` → 双门移植有真 bug，退出码 2，必须先修

## 需要你拍板的决策
1. **sp 是否要忠实移植 Python**（加 15m 锚定 + gap delta + ceil 正数抬升）？
   - 选「是」→ 改 009，使 A/B 可退化为相等校验，之后才能退役 Python（保留为基准）。
   - 选「否」→ 接受 sp 为「简化独立实现」，则「A/B 对拍通过」的说法不成立，需重写为「独立再验证」，并明确以哪一侧为权威。
2. 无论选哪个，**2026 补采前必须先在 staging 跑通本 harness**（尤其 `03` 验证 `oi` 已修）。

## ⚠ 安全
- `synthetic` 模式用测试品种 `ZZ888/ZZ8888`，插入后全程 `DELETE` 清理，不污染真实数据。
- `real` 模式对真实品种会**追加** roll_segment 段，对比后删除新增段（`seg_no > 追加前最大值`）还原。
- 任何一步失败（`ON_ERROR_STOP`）立即停止，绝不继续写库。
