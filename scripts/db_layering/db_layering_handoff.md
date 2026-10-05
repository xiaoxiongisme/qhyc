# 数据层分层隔离 — CB 实施手册（交付包总索引）

> 日期：2026-10-05｜配套设计：`db_layering_design.md`｜脚本：`db_layering_migrate.sql` / `db_layering_verify.sql` / `db_layering_rollback.sql`
> 适用：库 `qhyc`（PostgreSQL 16.4 + TimescaleDB 2.17.1，容器 `timescaledb`，角色 `futures`）
> 一句话目标：**把平铺在 `public` 的 124 张表按 L0/L1/L2/L3 + 接入层拆成独立 schema + 独立角色 + 最小权限，从结构上杜绝"找错表/误改生产"（这正是此前回测误用 `hourly_bar` 的根因）。**

---

## 0. 交付物清单（交 CB 的就是这 4 份 + 本手册）

| 文件 | 用途 | CB 动作 |
|---|---|---|
| `db_layering_design.md` | 设计/可行性/架构/映射/风险（Why & What） | 阅读，确认方案 |
| `db_layering_migrate.sql` | 主迁移（建角色/建 schema/迁表/授权/建视图/兼容 shim），幂等 | **执行** |
| `db_layering_verify.sql` | 校验（`app_read` 只读 `access`、读不到 `l0_raw`、源/视图行数零丢失） | **执行** |
| `db_layering_rollback.sql` | 一键回退（把表迁回 `public`、删角色/schema） | 应急备用 |
| 本手册 | 执行顺序 + 命令 + R10 + 教训 | 照做 |

---

## 1. 前置条件（必做，否则会出今日那种锁死事故）

1. **维护窗口**：挑一个**所有读写进程都停掉**的时段——停掉回测脚本、采集作业（ETL）、API 写入。本迁移会对超表加 `AccessExclusiveLock`，窗口内有其他查询会被长时间阻塞。
2. **全库备份**：
   ```bash
   docker exec timescaledb pg_dump -U futures -d futures -Fc -f /tmp/qhyc_pre_layering.dump
   # 校验备份可读
   docker exec timescaledb pg_restore -l /tmp/qhyc_pre_layering.dump | head
   ```
3. **确认无孤儿后端**（今日教训）：执行前先查有没有残留的长事务/锁：
   ```bash
   docker exec timescaledb psql -U futures -d futures -c \
     "SELECT pid, state, now()-xact_start AS age, left(query,40) q FROM pg_stat_activity WHERE state<>'idle' AND pid<>pg_backend_pid() ORDER BY age DESC;"
   ```
   若有 `idle in transaction` 或长时间 `active` 的异常会话，先 `SELECT pg_terminate_backend(<pid>);` 清掉。
4. **不要**用"宿主 shell 启动、再杀宿主 shell"的方式跑迁移——容器内 python/psql 会变成孤儿继续持有锁。要么前台跑完，要么用 `pg_terminate_backend` 兜底清理。

---

## 2. 执行顺序

```
[1] 备份(pg_dump)            ← 第1步已含
[2] migrate.sql  (执行)      ← 建角色/建schema/迁表/授权/建视图/兼容shim
[3] verify.sql   (执行)      ← 断言权限与行数
[4] 人工核对 verify 输出     ← 全部 PASS 才继续
[5] 切应用连接串             ← 新增 app_read / app_write 两个串，详见 §3
[6] 观察 1~2 天无异常        ← 兼容 shim 保证 legacy 代码零改动
[7] （可选）删 public shim   ← 应用全部切到 access/app_state 后再做
```

> 若 [3] 不通过或中途失败：`db_layering_rollback.sql` 一键回退，再从 [2] 重来（migrate 幂等，已迁移的表会跳过）。

---

## 3. 具体命令（库在容器内，SQL 文件在宿主）

```bash
# 把脚本拷进容器
docker cp db_layering_migrate.sql timescaledb:/tmp/migrate.sql
docker cp db_layering_verify.sql  timescaledb:/tmp/verify.sql
docker cp db_layering_rollback.sql timescaledb:/tmp/rollback.sql

# [2] 执行迁移（前台跑，别杀宿主 shell；留意输出末尾的 migrate_log 汇总）
docker exec timescaledb psql -U futures -d futures -v ON_ERROR_STOP=1 -f /tmp/migrate.sql

# [3] 校验
docker exec timescaledb psql -U futures -d futures -f /tmp/verify.sql

# 应急回退
docker exec timescaledb psql -U futures -d futures -f /tmp/rollback.sql
```

**迁移完成后必查（防止孤儿锁，今日教训）：**
```bash
docker exec timescaledb psql -U futures -d futures -c \
  "SELECT count(*) FROM pg_stat_activity WHERE state<>'idle' AND pid<>pg_backend_pid();"
# 应接近 0（只剩 API 连接池）。若仍有长时间 active，pg_terminate_backend 清掉。
```

---

## 4. 应用侧连接串切换（migrate 后由开发做）

migrate 后表物理迁移，但 `public` 下有**兼容 shim 视图**，`schema=None` 的 legacy 模型仍可跑。逐步切换：
- 新增两个连接串：`app_read`（连 `app_read` 角色，只读 `access`）、`app_write`（连 `app_write` 角色，写 `app_state`）。
- **Python 日常分析/回测**：统一连 `app_read`，只 `SELECT access.*`：
  - 小时线主连 = `access.v_bar_60m_main`
  - 小时线后复权主连 = `access.v_fut_kline_cont_hourly`
  - 真实合约1分钟 = `access.v_contract_minute`（R10 建好后）
- 采集作业：连 `etl_ingest`，只写 `l0_raw`。
- **绝不**让日常 Python 再拿 `futures` 超级权限。

---

## 5. R10 前置：真实合约分钟线采集（新建，不在 migrate 内）

**数据现状（2026-10-05 实测）——直接回答"真实合约分钟线没有么"：**

| 数据 | 频率 | 真实合约? | 范围 | 规模 |
|---|---|---|---|---|
| `contract_daily` | 日 | ✅ | 2016-01-04→2026-09-30 | 3935 合约/81.9万行 |
| `fut_kline`(contract) | 小时 | ✅ | 2016-01-04→2026-09-02 | 3087 合约/527万行 |
| `fut_kline`(contract) | 15分 | ✅ | 2023-01-17→2026-09-02 | 仅36品种/19.5万行 |
| `fut_kline`(contract) | 1分/30分/60分 | ❌ **0 行** | — | — |
| `minute_bar` | 1分 | ❌ 100% 是 8888主连/9999指数/GFEX连续，且只2026 | 2026-01-05→2026-09-30 | 164符号/180万行 |

**结论：真实合约 1 分钟线（如 RB2410@1min）确实不存在，R10 必须新建采集。**

**落库设计（纳入本分层）：**
- 原始采集表 `l0_raw.contract_minute_raw`（由 `etl_ingest` 写）
- 派生 `l1_mkt.contract_minute`（由 `l1_builder` 增改）
- 接入层 `access.v_contract_minute`（`app_read` 唯一出口）

**建表草案（CB 在 L0 阶段执行，注意：迁表脚本不改这张新表）：**
```sql
-- 在 l0_raw 下建原始真实合约分钟表（迁表脚本运行前或运行后均可，它不在映射表里）
CREATE SCHEMA IF NOT EXISTS l0_raw;
CREATE TABLE l0_raw.contract_minute_raw (
    symbol      text      NOT NULL,   -- 如 RB2410
    contract    text      NOT NULL,   -- 真实合约代码
    exchange    text,
    ts          timestamptz NOT NULL, -- 分钟时间戳(带时区)
    open        numeric, high numeric, low numeric, close numeric,
    volume      bigint,  open_interest numeric,
    pre_close   numeric, high_limit numeric, low_limit numeric,
    src         text,                 -- 数据源(akshare/tqsdk/ctp...)
    created_at  timestamptz DEFAULT now()
);
SELECT create_hypertable('l0_raw.contract_minute_raw','ts');
CREATE INDEX ix_cmr_sym_ts ON l0_raw.contract_minute_raw(symbol, ts DESC);
GRANT SELECT, INSERT, UPDATE, DELETE ON l0_raw.contract_minute_raw TO etl_ingest;
GRANT SELECT ON l0_raw.contract_minute_raw TO app_read;  -- 经 access 视图再授，见下
-- access 视图（app_read 实际只走视图）
CREATE VIEW access.v_contract_minute AS
  SELECT symbol, contract, exchange, ts, open, high, low, close, volume, open_interest, pre_close
  FROM l0_raw.contract_minute_raw;
GRANT SELECT ON access.v_contract_minute TO app_read;
```

**采集窗口建议（回答"采集多久到多久"）：**
- **实时采集（硬性必需）**：从现在起持续采集**在交易的真实合约** 1 分钟（实盘要把连续主连信号映射到真实合约下单）。
- **历史回填（仅当 R10 含分钟级回测才需要）**：
  - 品种：**活跃主力对应的真实合约**（≈84 主连符号的历年合约，见 `dim_contract` 共 3691 个合约），不要全量 3000+ expired 合约（1 分钟数据量不可行：真实合约小时线已 527 万行，1 分钟约 ×240）。
  - 时间：**2020-01-01 → 2026-09-30**（与回测主周期对齐；2015–2019 用已有真实合约小时线近似即可）。存储受限可降到 2022 起或降级 5 分钟。
- 若 R10 只做"信号→真实合约下单映射 + roll_segment 复权"，**无需历史 1 分钟**（真实合约日线 2016 起、小时 2016 起已够），只需实时 1 分钟。

---

## 6. 验收标准（verify.sql 断言 + 人工）

- [ ] `app_read` 能 `SELECT access.*` 全部视图，**不能** `SELECT l0_raw.*`（应报 permission denied）。
- [ ] `app_read` 能查 `access.v_bar_60m_main` 且行数 ≈ `l1_mkt.bar_60m` 全量（源/视图零丢失）。
- [x] `public` 下原表名消失；`hourly_bar` 仅指向 `l0_raw.hourly_bar`（兼容 shim），新代码强制用 `access.v_bar_60m_main`。
      （2026-10-06 R1/031：该表已由 `hourly_bar_akshare_deprecated` 改回 `hourly_bar`——
      它是 G9 退役 continuous 后**小时线主连的唯一存储**，原名中的 `akshare_deprecated`
      后缀属误导性命名，死资产审计中差点据此误删。改名后与其他分层表完全同构，
      Phase 4a 的"隔离改名"特殊分支已删除。）
- [ ] `etl_ingest`/`l1_builder`/`l2_builder`/`ref_maintainer`/`app_write` 各自只能改本层，互不越权。
- [ ] 迁移后无孤儿后端（`pg_stat_activity` 干净）。
- [ ] `pg_dump` 备份文件存在且可列举。

---

## 7. 今日实测教训（CB 执行时务必规避）

1. **孤儿进程持锁**：校验/迁移若被宿主 shell 中断，容器内后端仍存活并持有 `AccessExclusiveLock`，会让全库相关查询"假死"。**任何时候中断都要 `pg_terminate_backend` 兜底**。
2. **大超表迁移慢**：`fut_kline`（1608 万行，压缩）单表迁 schema 需数十秒～分钟；整库 124 表累计更长，留足窗口，脚本已逐表 `try/except` 隔离。
3. **校验不落盘**：任何"干跑"必须 `BEGIN ... ROLLBACK`，且跑完确认无残留后端。
