# 云端预检 harness（007 / 008 / 009 + L2 A/B 对拍）

> 本地无 DB，本目录所有脚本**只在云端 STAGING 克隆库上执行**，绝不连生产。
> 目的：在动 2026 补采、跑存储过程写库之前，把风险挡在前面。

## 重要发现（写本 harness 时确认）

### 发现 1（已修复）· `sp_build_l2_roll_segment` 原先是简化版，现已忠实移植
初版 009 写 sp 时把双门检测"凭印象重写"而非逐行移植，漏掉了你踩坑固化的三道硬约束：
15m 锚定、`gap=open[i]-close[i-1]`、price_shift 的 ceil 公式。2026-09-30 你拍板「以 15m 锚定
原版为准，不得简化/改变/添加」后，009 的 sp 已改为**忠实移植** `build_roll_segments.py`（252/252 验收）：

| 维度 | Python 基准（验收版） | sp 版（009，修复后） | 状态 |
|---|---|---|---|
| 检测锚点 | 统一 15m 锚定再映射 | 固定 `bar_15m` 锚点 + 包含关系映射到目标 freq | ✅ 一致 |
| roll_delta 基准 | 锚点 gap = `open[i]-close[i-1]` | 同左（映射落点用锚点 gap） | ✅ 一致 |
| 双门 | `\|cc\|>(amp+1e-6)` & `\|Δ8888\|<0.40\|cc\|` | 同左 | ✅ 一致 |
| price_shift | ceil 公式，仅当 min≤0，默认不调 | `p_positivity` 默认 false → 0；true 时取 ceil 公式 | ✅ 一致（默认） |
| src_freq | 恒 `min15` | 恒 `'min15'` | ✅ 一致 |
| cum_offset | `-Σdelta`，段 0=0 | 同左；append-only 增量接龙 | ✅ 一致 |

**结论**：两侧为同一算法，A/B 应**逐行相等**。本 harness（`02`）现在做严格逐行比对，
任何不一致即退出码 2（视为回归，必须先修 sp，不得退役 Python）。

### 发现 2 · `sp_build_l1_from_minute` 列名 bug（`oi` → `open_interest`）
`009` 原 INSERT 列名写成 `oi`，但 `bar_5m` 真实列是 `open_interest`（`scripts/load_1min.py:125`）。
当前因 1m 缺失是 no-op 未暴露；2026 补采把 1m 补回后该 sp 会直接报错。
**已修复 009**，并由 `03_sp_l1_fixture.sql` 在预检中显式验证 INSERT 路径。

## 文件清单
- `00_apply_migrations.sql` — 在 staging 上应用 007/008/009（`\i` 聚合）
- `01_validate_dict_cfg.sql` — 字典/配置表断言（计数 / 开关对齐现状 env / FK / 视图）
- `02_fixture_and_roll_ab.py` — **L2 A/B 对拍**（sp vs Python），synthetic 模式全程清理（源文件已失，只剩 pyc）
- `02b_gap_aware_ab.py` — **CL-15 跨休市专项 A/B**（自包含、不依赖 pyc，默认拒绝连生产库）
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
- 退出码 0 + `逐行相等=true` → 忠实移植校验通过，可退役 Python（保留为基准）。
- `❌ seg_no=K: ... delta/cum/shift/start 不一致` → 回归，退出码 2，**必须先修 sp**，切勿退役 Python。
- 注意：默认 sp 不带 positivity → price_shift 两侧均=0；若要验证 ceil 抬升分支，
  让 sp 传 `p_positivity=true`、Python 传 `--positivity` 后单独再比一次。
- 增量模式（`--mode real`）仅比对「新增段」（`seg_no > 跑前最大值`），历史段不在 sp 增量范围。

## 决策状态
1. ✅ **已拍板（2026-09-30）**：sp 以 15m 锚定原版为准做忠实移植，不得简化/改变/添加。
   009 已改完，A/B 现应逐行相等；退役 Python 的前提是 `02` 在 staging 跑出退出码 0。
2. ⏳ 2026 补采前仍须先在 staging 跑通本 harness（尤其 `03` 验证 `oi` 已修、`02` 验证 A/B 相等）。
3. 🔄 **重新拍板（2026-11-08 12:05，CL-20261107-15）**：用户选择改算法，上条「不得改变」
   的约束**仅在本项上解除**。Python `map_roll_to_freq` 与 `migrations/055` 同步改为
   「上界 clamp 到 min(下一根, bucket+名义周期) + 跨休市空隙归下一根」，两版仍须
   `02` 逐行相等。其余忠实移植约束（15m 锚定 / gap 取锚点 / 双门 / cum_offset /
   price_shift / src_freq）**全部保持不变**。
   ⚠ 055 同时加 `p_symbol`（CL-20261107-16 的 C1）；`low_off` 改 JOIN 属 C2，
   后置到 056，不得与映射改动一起进对拍（失败时无法归因）。
4. ✅ **已验收（2026-11-08，CL-20261108-04）**：staging 上 054 把 21:15 的换月
   归到 min60 的 15:00 桶，055 改归 22:00；min15(21:15)/min30(21:00) 不变，
   三周期 A/B 逐行相等、退出码 0；日盘连续网格同样逐行相等（无空隙映射无回归）。
   生产事务内试算 → `ROLLBACK` → 正式应用 055 → 只重跑 PF888 min60/min30：
   `min60 seg19=2026-11-04 22:00+08`、`min30 seg19=21:30+08`，
   `l2_adj.bar_60m_ba@22:00` 的 `adj_offset` 80→320、close 8066→8306，
   −240 断层抹平，且 15:00 那根仍为 80（无 +240 假跳空）。
5. ✅ **C2 已验收（2026-11-08，CL-20261107-16）**：`migrations/056` 让
   `p_positivity=false`（生产默认）的 `low_off` / `shift_calc` 走
   `AND 'f'::boolean` 常数折叠直接短路，true 分支改 `LEFT JOIN full_segs` 区间连接
   并取消第二遍全表聚合。staging 用负价格 fixture 强制算出 `price_shift=100`，
   055 与 056 逐行完全一致。生产事务内试算 min5 单品种 **70 秒无 OOM**
   （历史两次 42/71 分钟、3 GiB 容器被打爆）；PF888 min5 与 106,453 行
   `bar_5m_ba` 已重建。全量 82 品种 append-only 重跑经量化判定当下零收益
   （11-04 后新锚点事件=0、10 个落后品种在口径外、5 个口径内最新段已对齐），
   经用户拍板不执行；两项遗留另立 `CL-20261108-05` / `CL-20261108-06` 挂账。

## 工具现状与已知坑（2026-11-08 实测，CL-20261108-04）
- `02_fixture_and_roll_ab.py` 与 `run_preflight.sh` 的**源文件已不在仓库**，只剩
  `__pycache__/02_fixture_and_roll_ab.cpython-312.pyc`。宿主 venv 为 Python 3.12.3，
  版本匹配，补 `PYTHONPATH=/Docker/qhyc/scripts:/Docker/qhyc` 后 `--help` 正常，但有三个坑：
  1. ⚠ **`load_brs()` 路径缺陷**：它用 `dirname(abspath(__file__))/../build_roll_segments.py`
     定位基准文件；pyc 在 `__pycache__/` 下，该路径指向
     `cloud_preflight/build_roll_segments.py`（不存在）→ `main()` 一跑就崩，`--help`
     能出只是因为 `parse_args` 提前退出。绕法：把 pyc 放到 `<harness>/__pycache__/`、
     基准文件放 `<harness>/build_roll_segments.py`，相对路径正好命中，还可切换 old/new 两版。
  2. ⚠ **`gen_synthetic()` 只造日盘**：buckets 过滤条件为 `9 <= hour < 15`，
     所以 `--roll-at 21:15` 造不出夜盘换月事件，**不能用它验收 CL-15 的跨休市场景**
     （12:05 曾误判「可用 --roll-at 做专项 fixture」，此处更正）。
  3. ⚠ **`n_bars` 误判**：`compare()` 把「存在 `n_bars IS NULL` 段」直接判
     `rows_eq=False`。054 SP 的 INSERT 漏写该列，生产 16,972 段全部为 NULL，
     252/252 验收链在当前代码上永远跑不出 PASS。055 已补 `fs.n_bars` / `ns.n_bars`。
- ✅ 已建 `qhyc_staging`（同一 PG 实例上的**独立库、最小结构**，不含任何生产数据）：
  `public.bar_5m/15m/30m/60m` 与 `public.roll_segment` 都是**无过滤普通表**——
  生产同名对象是带 `l3_ref` 白名单过滤的视图，会挡住测试品种 `ZZ888`；
  另建 `public.l1_table_of(text)`。
- ✅ `02b_gap_aware_ab.py` 自带跨休市 fixture（60m 日盘末根 15:00、夜盘首根 22:00，
  事件 21:15），直接加载仓库 `scripts/build_roll_segments.py` 算期望值，不依赖 pyc：
  ```bash
  set -a; . /home/ubuntu/qhyc_work/qhyc.env; set +a
  PYTHONPATH=/Docker/qhyc /home/ubuntu/qhyc_work/venv/bin/python \
    /Docker/qhyc/scripts/cloud_preflight/02b_gap_aware_ab.py --db qhyc_staging
  ```
  默认 `dbname=futures` 直接拒绝。可用 `--brs-path` 指向 `git show HEAD^:...` 导出的旧版
  做 old/new 对照，用 `--call-symbol` 验证 055 的 `p_symbol` 收窄。

## ⚠ 安全
- `synthetic` 模式用测试品种 `ZZ888/ZZ8888`，插入后全程 `DELETE` 清理，不污染真实数据。
- `real` 模式对真实品种会**追加** roll_segment 段，对比后删除新增段（`seg_no > 追加前最大值`）还原。
- 任何一步失败（`ON_ERROR_STOP`）立即停止，绝不继续写库。
