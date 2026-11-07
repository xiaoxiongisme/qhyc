# L0 分钟层「本地 CSV 取代云端 akshare」设计文档

> 状态：设计冻结（Q1–Q5 已确认，Q6 用户补数据中，Q7 已定方案）
> 日期：2026-11-04
> 范围：用 `D:\学习资料\期货分钟数据` **完全取代 qhyc L0 的 1 分钟原始子层 `minute_bar`**，并使其成为该层唯一来源。5/15/30/60m/daily/hourly/基本面不在本次范围。

---

## 0. 已确认决策（frontier 收敛）

| # | 决策点 | 结论 |
|---|---|---|
| Q1 | 取代范围 | **仅 1 分钟原始层 `minute_bar`**（其它频率/基本面维持现状） |
| Q2 | 8888 指数连 | **合成**：由 loader 以 **back-adjusted 连续法**合成 `XXXX8888`（剔除换月 carry 跳变，换月处平滑）；**L2 算法不变**（免 252/252 重验）。⚠️ 等权/VWAP 合成实测 0 段，已证伪 |
| Q3 | 825 合约是否入 L0 | **是**，全量写入 L0（新增能力，原 L0 只有 888 主连） |
| Q4 | 量/额归一 | DCE 量 ÷2 归单边（**主连与合约都要除**，两层同量纲；只除合约会造成 M888=2×M8888）；额**双列**（`amount` 真额 / `amount_raw` 源原生）；**不做 CZCE ×乘数**（已实证证伪），CZCE 标 `caliber_amt='no_multiplier'` |
| Q5 | 主连额语义 | 双列分离（由 Q4 落地）；akshare 增量 `amount`/`amount_raw` 均 NULL |
| Q6 | 覆盖缺口（9 品种） | 用户正在补数据；管道做成**品种无关**，补完自动纳入 |
| Q7 | 增量机制 | **一次性 bulk CSV 补到今日**；之后**全量+增量数据均由 akshare 采集**，且**须增强 akshare 采集全合约** |

---

## 1. 现状真值（已核实，非假设）

### 1.1 本地数据树（`D:\学习资料\期货分钟数据`）
```
<EXCH>_<中文名>/
  <VAR>_<中文名>/
    <VAR>主力连续.csv          # 主连层（12 列）
    <VAR><CONTRACT>.csv        # 合约层（8 列），如 IC2610.csv；DCE 小写前缀 m2611.csv
    退市/                      # 跳过
    更新/                      # 与根目录内容一致，取作权威源 + 按 (symbol,ts) 去重
```
- 6 交易所：`CFFEX_中国金融期货交易所`、`CZCE-郑州商品期货交易所`、`DCE-大连期货交易所`、`GFEX_广州期货交易所`、`INE_上海国际能源交易中心`、`SHFE_上海期货交易所`
- 88 品种 / 825 合约 + 88 主连；编码 UTF-8 BOM，合约层 CRLF、主连层 LF。

### 1.2 两层 CSV schema
| 层 | 列顺序 | 关键列说明 |
|---|---|---|
| 合约层（8 列） | `日期,开盘价,最高价,最低价,收盘价,成交额,成交量,持仓量` | `日期`=end-time 墙钟（如 `2025-11-14 21:02:00`）；`成交额`=**真额（已含乘数）**；`成交量`=DCE 双边、其余单边 |
| 主连层（12 列） | `exchange,symbol,open,close,high,low,amount,volume,position,bob,eob,type` | `eob` 带 `+08:00`；`amount/position`=**源原生值（定义与主连层专属，禁与合约层混算）**；`type` 恒=14（透传，不进逻辑） |

> **数据说明.txt 口径（权威）**：① DCE 量为双边（÷2 才单边）；② 主连层 `amount/position` 原始、与合约层定义不同；③ 夜盘 ≥20:00–02:30 计入下一交易日——但**经抽样证实分钟 ts 本身是墙钟时间**，该规则作用于日线聚合层，不影响分钟 `ts`。

### 1.3 现有 `minute_bar` DDL（`scripts/load_1min.py`，**普通表，非超表**）
```sql
CREATE TABLE minute_bar (
  symbol text NOT NULL, ts timestamptz NOT NULL,
  open numeric, high numeric, low numeric, close numeric,
  volume bigint, amount numeric, open_interest numeric,
  high_limit numeric, low_limit numeric, pre_close numeric, settle_price numeric,
  contract text, src text,
  PRIMARY KEY (symbol, ts)
);
```
- `ts` = **end-time 标签**（日盘首根 09:01、末根 15:00）；合成用 `time_bucket(区间, ts - INTERVAL '1 minute', 'Asia/Shanghai')`。
- `symbol` 命名空间：`XXX888`=主连、`XXX8888`=指数连、纯合约码如 `IC2610`。
- **现状**：生产库 `minute_bar` 仅有 akshare 主连(888) 行，**无真实合约行、无 8888**（`execution/validator.py`、`data_selfcheck.py` 已核实）。

### 1.4 下游过程关键约束（源码核实）
| 过程 | 行 | 约束 | 对取代的影响 |
|---|---|---|---|
| `sp_build_l1_from_minute` | 108, 137 | `WHERE symbol ~ '^[A-Za-z]+8888?$'` | 纯合约码（如 `IC2610`）**被排除出 L1**；要进 L1 须放宽正则 |
| `sp_build_l2_roll_segment` | 88 | 遍历 `symbol ~ '^[A-Za-z]+888$'` | 只给主连建换月段 |
| 同上 | 96–99 | `replace(888→8888)` 必须存在，否则 `RAISE NOTICE … CONTINUE` | **无 8888 → 该品种 roll_segment 整段跳过** → `back_adj`（默认口径）在 5/15/30/60m 全部回落 `continuous` 并告警 |
| `caliber.py` | 34/44 | `CALIBERS=("continuous","back_adj","contract")`；`DEFAULT_CALIBER="back_adj"` | 后复权是回测基准，依赖 roll_segment 有段 |

### 1.5 既有加载器 `scripts/load_1min.py`
- 针对**旧数据树**（`期货主力连续1min最新`/`*9999.*`→`888`、`期货商品指数1min最新`/`*8888.*`→`8888`），与本次 `期货分钟数据` 结构**不兼容**。
- **可复用**：DB 写入路径（COPY）、`ts` end-time 处理、`time_bucket(ts-1min)` 分桶逻辑、`9999→888` 映射思路。
- **须重写**：文件发现（按交易所/品种子目录）、两层 schema 解析、8888 合成。

### 1.6 维度表
- `dim_symbol(symbol PK, product, namespace[main|index|contract|kq|other], exchange, is_preferred, …)`
- `dim_contract(contract_code PK, variety_code, exchange, … main_symbol, is_main, is_active, …)`

---

## 2. 目标表结构（扩展 `minute_bar`，向后兼容）

保留原 15 列不动（让 `barstore`、`sp_build_l1/l2`、`validator` 零改动），**新增**元数据列吸收两层与口径差异：

```sql
-- l0_raw.minute_bar（扩展后；普通表，不建超表：20y×~170 symbol 下 7 天分块会炸，PK 索引足够）
CREATE TABLE IF NOT EXISTS l0_raw.minute_bar (
    -- ===== 原 15 列（不变）=====
    symbol          text        NOT NULL,
    ts              timestamptz NOT NULL,        -- end-time 标记 = 本地 日期/eob
    open            numeric, high numeric, low numeric, close numeric,
    volume          bigint,                      -- 已归一为单边量
    amount          numeric,                     -- 真额(合约行)；主连行 NULL；akshare 增量 NULL
    open_interest   numeric,
    high_limit      numeric, low_limit numeric,
    pre_close       numeric, settle_price numeric,
    contract        text,                       -- 合约行=同 symbol；主连/指数行=NULL
    src             text        NOT NULL DEFAULT 'local_csv_20261030',

    -- ===== 新增：分层 / 口径 / 来源 =====
    ts_end          timestamptz,                 -- 主连 eob（合约行可推 ts+1min，可选）
    layer           text NOT NULL DEFAULT 'continuous',  -- 'continuous' | 'contract' | 'index'
    exchange        text,                        -- 由目录推导
    variety_code    text,                        -- 大写品种码：IC/RB/M…
    caliber_vol     text NOT NULL DEFAULT 'single_side', -- 'single_side'(归一) | 'double_side_raw'
    caliber_amt     text NOT NULL DEFAULT 'true',        -- 'true'(含乘数真额) | 'raw'(主连原生)
    amount_raw      numeric,                     -- 源原生额(主连层填；合约/akshare 为 NULL)
    raw_type        int,                         -- 本地主连 type(=14) 透传
    PRIMARY KEY (symbol, ts)
);
CREATE INDEX IF NOT EXISTS ix_mm_sym_layer ON l0_raw.minute_bar(symbol, layer);
```

**双列方案解决「主连额 ≠ 合约层额」硬冲突**：`amount` 永远是可跨层比较的真额（主连行留 NULL，因主连原生额不可比）；主连原生额存 `amount_raw` 并由 `caliber_amt='raw'` 标记。下游比较 amount 前**必须先读 `caliber_amt`**。

---

## 3. 归一化规则（入库即固化，杜绝下游误比）

| 维度 | 合约行 (`layer='contract'`) | 主连行 (`layer='continuous'`) | akshare 增量 |
|---|---|---|---|
| `symbol` | `<VAR><CONTRACT>`（大写，DCE `m2611`→`M2611`） | `<VAR>888` | 同左两规则 |
| `ts` | `日期` 列，墙钟 SH，存 UTC（end-time） | `eob`（去 `+08:00`→UTC） | akshare 返回 datetime→UTC |
| `volume` | DCE ÷2 单边；其余直接；`caliber_vol='single_side'` | 源值（主连原生量）；`caliber_vol` 同 | akshare 原值（见风险 R2） |
| `amount` | 真额（已含乘数），`caliber_amt='true'` | **NULL**（真额不可得），`caliber_amt='raw'` | **NULL** |
| `amount_raw` | NULL | 源原生额 | NULL |
| `open_interest` | `持仓量` | `position` | akshare `open_interest` |
| `raw_type` | NULL | 主连 `type`(=14) 透传 | NULL |
| `exchange/variety_code` | 由目录推导 | 同 | 由 dim 推导 |

**不做 CZCE ×乘数**（实证：CZCE `CF` `成交额/(收盘×量)=5.000`、SHFE `RB` `≈10`，合约层额本就含乘数，与 `数据说明`第 5 条自相矛盾——**以数据为准**）。

---

## 4. 8888 指数连合成规格（Q2，阻断项）

**为什么必须**：`sp_build_l2_roll_segment` 第 96–99 行——无 8888 则整个品种换月段被 `CONTINUE` 跳过；`back_adj`（默认口径）在 5/15/30/60m 全部失效。→ **8888 缺失 = 后复权崩坏**，故为阻断项。

**算法（bulk 与增量通用）—— back-adjusted 连续合成（2026-11 修正）**：

> ⚠️ **重要修正**：早期设计为「量加权(VWAP) / 等权均值」合成，实测**均失败**——
> 换月瞬间 8888 与主力(888)同步跳空，使 `sp_build_l2_roll_segment` 的 ratio 门
> `abs(idx_delta) < 0.40·abs(cc)` 恒不成立 → `roll_segment` 整品种为空 → `back_adj`
> （默认口径）在 5/15/30/60m 全部失效。故必须做 **back-adjustment（剔除换月 carry 跳变）**。

对每个品种（实现见 `scripts/load_local_csv_1min.py` 的 `synth_8888()`）：
1. 取该品种全部 `layer='contract'` 分钟行，按 `ts` 取「front 合约」= 该分钟持仓量(oi)最大者
   （平手取合约月份更大者）。
2. front 价格序列 `fc` 前向填充补齐缺口；front 切换(symbol 变化)处记 `carry = 切换前后 front 价差`。
3. 累计偏移 `cum = cumsum(carry)`；`8888.O/H/L/C(ts) = front.O/H/L/C(ts) − cum`
   （剔除换月跳变，得到换月处平滑、真实涨跌处仍如实跳动的连续序列）。
4. 量/额/持仓取全合约按 `ts` 求和（市场真值合计）。
5. 按主连时间轴对齐并前向填充，保证 8888 覆盖主连全部分钟
   （避免 L2 `LEFT JOIN` 时 `idx_delta` 为 NULL 而漏检换月）。
6. `symbol='<VAR>8888', layer='index', caliber_amt='true', caliber_vol='single_side'`。

- 合成在 **loader（Python）内完成**；DB 端 `sp_synth_8888_from_contracts` 已废弃为「仅告警」占位，
  防止误跑把正确 8888 覆盖成错误的等权数据。
- 合成后 `8888` 行进 `minute_bar`(layer='index') → 经 `time_bucket` 自动生成 `bar_15m` →
  L2 过程 `LEFT JOIN … replace('888','8888')` **零改动**命中。

**验收门槛 V3（实测，2026-11，本地 `validate_l0_local_csv.py`）**：
  RB≈43 段(2.4/年)、CU≈20 段(1.1/年)、IC≈56 段(4.9/年)；换月处 `|idx_delta|` 仅占 `|cc|` 的
  9%~20%（即 8888 平滑），ratio 门正确区分真实跳空与换月。
  段数低于「理想换月日历」是因为 ratio 门(0.4) + is_large 过滤只捕捉 carry 跳变足够大的换月，
  该阈值由固定门决定，属预期；关键是**非零且平滑**（旧等权/VWAP 实测为 0 段）。

---

## 5. 加载器设计（新脚本 `scripts/load_local_csv_1min.py`）

复用 `load_1min.py` 的 COPY 写入与 `ts` end-time 处理；重写发现/解析层：

1. 遍历根 `D:\学习资料\期货分钟数据`，解析交易所前缀 → `exchange` 码，品种码 `VAR`（去 `_中文`）。
2. 优先读 `更新/` 下文件；根目录同文件作去重兜底（PK 冲突 `ON CONFLICT DO NOTHING`）。
3. `退市/` 子目录整体跳过。
4. 文件名匹配：
   - `<VAR>主力连续.csv` → `symbol=VAR888`，按 12 列解析，`eob`→`ts`，`amount`→`amount_raw`+`caliber_amt='raw'`，`amount=NULL`，`position`→`open_interest`，`raw_type=type`，`layer='continuous'`。
   - `<VAR><CONTRACT>.csv`（前缀大小写不敏感，`m2611`→`M2611`）→ `symbol=VAR+CONTRACT` 大写，`layer='contract'`，8 列解析，`日期`→`ts`，`成交额`→`amount`(`caliber_amt='true'`)，`成交量` DCE÷2，`持仓量`→`open_interest`。
5. 批量 `COPY` 入 `minute_bar` 扩展表（品种无关，补数据自动纳入）。
6. 全量加载后调用 §4 合成 8888。
7. 登记 `dim_symbol`/`dim_contract`（namespace: main/index/contract）避免 `sp_normalize_l1_symbols` 告警。

---

## 6. akshare 增量改造规格（Q7）

改写 `app/ingest/minute_collector.py`：

- **遍历全合约宇宙**（取自 `dim_contract WHERE is_active`），逐合约 `futures_zh_minute_sina(symbol=lower(contract), period='1')` → 写 `minute_bar`（`symbol=contract_code, layer='contract'`，量口径同 §3）。
- **主连 888**：`futures_zh_minute_sina(symbol=lower(var)+'888')` → `symbol=VAR888, layer='continuous'`。
- **8888**：不直取，由 loader 按 §4 的 back-adjusted 连续法在加载时直接合成写入（DB 端 `sp_synth_8888_from_contracts` 已废弃为仅告警占位，勿再 CALL）。
- `ON CONFLICT(symbol,ts) DO NOTHING`：bulk CSV 历史 与 akshare 增量尾部**互不覆盖**（CSV=历史主干，akshare=近期尾部+新合约）。
- akshare 单次上限 ~1024 行（≈6 交易日）→ 增量只刷近期；启动时可补抓缺失近期日。
- 旧「仅 888 主连」采集路径退役，注册「全合约 + 重合成 8888」为新常态。

---

## 7. 下游联动改造清单

| 项 | 动作 | 必要性 |
|---|---|---|
| `minute_bar` 列扩展 | 见 §2 DDL，旧 15 列不变 | 必须 |
| `sp_build_l2_roll_segment` | **不改**（依赖合成 8888） | 必须（免重验） |
| `sp_build_l1_from_minute` | 暂**不改**（合约留 L0 即可） | 可选：若策略要合约级 L1 棒再放宽正则 `^[A-Za-z]+\d{3,4}$`∪`8888?$` |
| `caliber.py` | 加 `("1m","contract")` 路由 → `minute_bar WHERE symbol=合约码`（不污染 L1，供研究取数） | 推荐 |
| `dim_symbol`/`dim_contract` | 注册所有新 symbol（main/index/contract） | 必须（否则 `sp_normalize_l1_symbols` 告警） |
| `barstore`/`validator` | 无需改（列兼容）；`validator` 现假设「只有 888」需放宽 | 需复核 |

---

## 8. 分阶段切换路线图（Q8：完全取代的落地）

- **P0 设计冻结**（本文档）。
- **P1 DDL 扩展 + 加载器**：新建/扩展 `minute_bar`；跑 `load_local_csv_1min.py` 入**暂存**（同表，先 `pg_dump` 生产旧数据作回滚锚）。
- **P2 8888 合成 + L2 验收**：loader 已合成 8888（back-adjusted 连续）→ 跑 `sp_build_l1_from_minute('min15')` + `sp_build_l2_roll_segment('min15')` → 本地先经 `validate_l0_local_csv.py` 验收（RB/CU/IC 段数非零且 8888 平滑）→ 校验 `roll_segment` 非空、`back_adj` 在 5/15/30/60m 生效（V3/V4）。
- **P3 A/B 比对**：对关键品种（IC/RB/CU/AG…）取旧 akshare-888 序列与 CSV 新序列最近 N 日，逐行比对行数/OHLC/量纲/caliber；差异 < ε 方可（V5）。
- **P4 影子并行**：保留旧 akshare-888 采集、新 CSV 作新源，并行数日观测。
- **P5 切换**：停旧「仅 888」采集，改为「全合约 + 8888 重合成」为唯一写者；旧历史仍在同表（CSV 已覆盖），无需 TRUNCATE。
- **P6 退役**：旧采集路径下线；`dim` 完整性校验通过（V6）。
- **P7 长期保鲜**：历史靠 CSV 更新包（数据说明「独立维护/单品种更新」）；每日增量 + 新合约靠 akshare。

---

## 9. 验收门槛（每条须先过再进下一阶段）

| ID | 门槛 | 方法 |
|---|---|---|
| V1 | DCE 合约量 ÷2 == 交易所单边公布值 | 抽 1 个 DCE 合约与 akshare 单边量比对 |
| V2 | CZCE/SHFE 合约 `amount == 收盘×量×乘数` | 已实测 CF=5、RB≈10；bulk 后复跑 |
| V3 | 8888 合成 → L2 段数 ≈ 换月日历 | RB/IC/CU 跑 L2 比对 |
| V4 | `back_adj` 在 5/15/30/60m 返回非 continuous | `roll_segment` 非空 + 路由生效 |
| V5 | A/B 关键品种 OHLC 差 < ε | 最近 N 日逐行比对 |
| V6 | `sp_normalize_l1_symbols` 无告警 | dim 注册完整 |

---

## 10. 风险登记

| # | 风险 | 处置 / 门槛 |
|---|---|---|
| R1 | 8888 合成法（等权/VWAP 已于 2026-11 证伪为 0 段） | 改用 back-adjusted 连续法；V3 实测 RB=43/CU=20/IC=56 段且 8888 平滑 |
| R2 | akshare DCE 量是否双边存疑 | 与 CSV(÷2) 比对，必要时 akshare 也 ÷2 |
| R3 | akshare CZCE 额口径（数据说明警告） | 实证实证优先；不一致则打 `caliber_amt` 标记 |
| R4 | akshare 与 CSV 的 ts end-time 对齐 | 抽样比对同品种同分钟 ts |
| R5 | 生产 ~1.19 亿行加载性能 | COPY 分批 + 普通表 PK 索引；先 `ts` 排序 |
| R6 | 放宽 L1 正则会误把合约棒带进策略 | 默认不改 L1，仅经 caliber 1m/contract 路由取数 |
| R7 | 新合约 symbol 未入 dim → 告警 | 加载器末步登记 dim |
| R8 | 9 品种缺口（AD/AL/ME/NI/OP/PD/PT/TC/WR） | 用户补数据自动纳入；补前对应 symbol 缺位（可接受） |

---

## 11. 代码审查修正（2026-11-04，dev-expert 复审后落地）

审查结论：**原实现的 8888 合成算法方向正确，但有 4 项 P0 阻断 + 4 项 P1 口径错误，直接上生产会零产出或写入错数据**。以下已全部修复，并附实测证据。

### 11.1 P0 阻断项（不修则零产出）

| # | 缺陷 | 后果 | 修复 |
|---|---|---|---|
| P0-1 | `parse_variety_dir` 对 `basename(vdir)` 匹配交易所正则，而交易所前缀在**父目录** | 恒返回 `(None,None)` → `--only` 过滤后 0 个品种 → 全库空转打印 "DONE 总计 0 行" | 改为回扫完整路径，命中 `EXCH_MAP` 的段即交易所；实测 88 个条目解析出 85 个品种（另 3 个为 `数据说明.txt`） |
| P0-2 | `COPY minute_bar` 未限定 schema | 命中 public 的**兼容视图** → `cannot copy to/from view` | 新增 `resolve_table()` 动态定位 `relkind in ('r','p')` 实体表；实测解析为 `l0_raw.minute_bar` |
| P0-3 | `dim_symbol`/`dim_contract` 同样写 public 视图 | `no unique or exclusion constraint matching` | 同上，解析为 `l3_ref.dim_*`；`dim_contract` 另用 `EXISTS` 前置过滤 FK，避免违例把事务打成 aborted |
| P0-4 | 裸 `COPY` 纯 append，撞主键 `(symbol,ts)` | 库内已有 1.19 亿行 akshare `*888`，重叠即 COPY 失败 → 整品种回滚 → 0 行 | 改为 `COPY → TEMP 暂存表 → INSERT ... ON CONFLICT (symbol,ts) DO UPDATE`，幂等可重跑；加载前 `assert_pk()` 早失败 |

### 11.2 P1 口径错误

| # | 缺陷 | 证据 | 修复 |
|---|---|---|---|
| P1-1 | 合约层 `日期` 与主连 `eob` **相差 1 分钟** | 按日跨合约对齐实测（`validate` V0）：RB/IC/M/SR 在 **+1min 时 OHLC 四项 100.0% 全等**，0min 仅 3.8–6.8%、-1min 1.2–4.2%；CU 单合约 61.5% + 另一合约 38.5% = 100%（**该日日内换月**）。合约层是**起始时刻标签**，主连是**结束时刻标签** | 合约 `ts = 日期 + 1min` 后再转 UTC（`CONT_TS_SHIFT`）。修复后主连与合约 ts 交集从 **0** 变为 **75,554/75,554**（RB 单合约全命中） |
| P1-2 | 源数据量纲污染未被拦截：IC 2015 主连 Σvol 放大 **850 倍** | 新增 V3 量纲门：按年对比「主连 Σvol / 当年 front 合约 Σvol」。RB 0.99–1.01 ✓、CU 0.97–1.02 ✓、IC 2015=**850.82** ← 越界 | `check_volume_scale()`，默认告警，`--strict-volume` 拒绝入库；DCE 预期比值取 2.0（主连双边/合约已÷2） |
| P1-3 | CZCE 成交额**未含合约乘数**（数据说明第 5 条），原实现统一标 `caliber_amt='true'` | CZCE 推断比值中位≈1.0（SR 实证）；RB=10、CU=5、IC=200 均为整数倍 | CZCE 合约与 8888 均标 `caliber_amt='no_multiplier'`；下游跨所比额须先按乘数折算 |
| P1-4 | `validate` V2 只抽 1 个品种 + 1 个合约（最新合约成交额齐全）→ 系统性漏检历史合约缺失 | 改为逐文件统计：RB 218 文件 665 万行，成交额缺失 **139,320 行（2.09%）**；CU 10.72M 行缺失 2.98%；IC 缺失 0.00% | V1/V2 遍历全部合约文件，输出缺失行数与占比 |
| P1-5 | **DCE 量 ÷2 只施加在合约层，主连层未除** → M888 的量是 M8888 / RB888 的 **2 倍**，跨品种比量断裂 | 同日实测：主连 Σvol 与当日主力合约 Σvol **原值完全相同**（M 853,445 = 853,445；RB 954,895 = 954,895）→ 两层同量纲，DCE 双边计数对两层同时成立。V3 量纲门在 DCE 上读出 **2.00**（预期 1.00）即此缺陷的指纹 | `build_main_df` 对 DCE 主连也 ÷2；`caliber_vol` 统一为 `single_side`；量纲门预期比值统一取 1.0（去掉 DCE 特例）。复验 M：1.00–1.03 ✓ |

### 11.3 工程质量（P2）

- `register_dim` 原按 symbol 逐个过滤 797 万行 DataFrame（O(sym×rows)）→ 改 `groupby` + `executemany`；`dim_contract` 改单条集合式 `unnest` 插入。
- `copy_df` 单品种整块转 `StringIO`（内存 GB 级）→ 按 `COPY_CHUNK=500,000` 行分批 COPY。
- 8888 的 `agg.reindex(grid).ffill()` 会把「无成交分钟」变成「复读上一分钟的量」→ **价格 ffill、量/额/持仓不 ffill（置 0）**，符合源数据「稀疏库、不补零」口径。实测 8888 `volume==0` 行占比：SR 0.13%、IC 0.00%。
- 034 只加列不改存量：旧行 `variety_code`/`exchange` 为 NULL → 新增**可选** `scripts/backfill_l0_meta.sql`（一次性全表扫描，运维显式执行）。
- loader docstring 里指向已废弃的 `CALL sp_synth_8888_from_contracts()` → 改为明确「已废弃为空壳，勿调用」。

### 11.4 修复后本地验收（沙箱 pandas 复刻，无 DB）

| 品种 | 8888 行 | 与主连 ts 同轴 | L2 换月段 | `|idx_delta|/|cc|`（换月处） | V3 量纲门 |
|---|---|---|---|---|---|
| RB | 1,322,391 | ✓ 全命中 | **43**（2.4/年） | 9% | 0 越界 |
| CU | 1,708,857 | ✓ 全命中 | **22**（1.2/年） | 18% | 0 越界 |
| IC | 677,767 | ✓ 全命中（起点晚于主连属正常） | **55**（4.8/年） | 17% | 2015 越界（正确拦截） |
| M (DCE) | 1,373,008 | ✓ 全命中 | **51**（2.8/年） | 9% | 0 越界（修正后 1.01–1.02） |
| SR (CZCE) | 1,364,522 | ✓ 全命中 | — | — | 0 越界 |

> 注：IC 8888 起点晚于主连（主连 2015-04-15 起，合约链 2015-05 起），8888 未覆盖的主连早期分钟在 L2 `LEFT JOIN` 后 `idx_delta` 为 NULL，被 `coalesce(…,1e9)` 正确排除，不会误判换月。

### 11.5 宿主执行顺序（修订）

```bash
cd /Docker/qhyc
# 0) 先确认实体表位置与主键（P0-2/P0-3 的判据）
psql "$PGURI" -c "SELECT n.nspname, c.relname, c.relkind FROM pg_class c
  JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE c.relname IN ('minute_bar','dim_symbol','dim_contract') AND n.nspname IN ('public','l0_raw','l3_ref');"

# 1) DDL（仅扩展列 + 废弃 proc 占位，幂等）
psql "$PGURI" -v ON_ERROR_STOP=1 -f migrations/034_l0_minute_bar_extend.sql

# 2) 可选：回填旧行元数据（一次性全表扫描，119M 行，数分钟）
psql "$PGURI" -v ON_ERROR_STOP=1 -f scripts/backfill_l0_meta.sql

# 3) 冒烟：3 个品种 + 保留旧源 A/B 基线（关键，否则旧数据被覆盖后无可比对象）
python scripts/load_local_csv_1min.py --only IC,RB,CU --backup-old

# 4) L1 + L2（sp_synth_8888_from_contracts 已废弃，不要调）
psql "$PGURI" -c "CALL sp_build_l1_from_minute('min15');"
psql "$PGURI" -c "CALL sp_build_l2_roll_segment('min15');"

# 5) A/B 验收（基线存在时走逐行比对）
psql "$PGURI" -v ON_ERROR_STOP=1 -f scripts/ab_compare_l0.sql

# 6) 全量（确认 ⑤ 段数非 0 后）
python scripts/load_local_csv_1min.py --root "D:/学习资料/期货分钟数据"
```
