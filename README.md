# 期货预测平台 qhyc（M1 数据层 + M2/M3 预测引擎 + M4 回测 + M5 看板 + M8 融合策略回测 + §16 传导）

> 工程根目录：`E:\Docker\qhyc`
> 数据模板源：`E:\Docker\qhyc\imports\<PRODUCT>\data\`
> 完整需求见 [`docs/期货预测平台需求文档_PRD.md`](docs/期货预测平台需求文档_PRD.md)

## M1 目标（§10）

1. PG + TimescaleDB 建库（含 6 张业务表 + 3 张超表 + 1 张连续聚合）
2. akshare 主源接入（日线）
3. tqsdk 校准（补缺 + 二次比对 + 异常工单）
4. 自动（每日 12:00 / 16:00 / 08:00）+ 手动更新
5. 按 §4.5 模板导入用户本地 FG/SA 历史数据

## 目录结构

```
qhyc/
├── app/                        # 应用代码
│   ├── api/                    # FastAPI 路由
│   ├── core/                   # 配置、日志、DB、异常
│   ├── ingest/                 # 采集、校准、导入
│   ├── models/                 # SQLAlchemy ORM
│   ├── repositories/           # 仓储层
│   ├── schemas/                # Pydantic 契约
│   ├── scheduler.py            # APScheduler 入口
│   └── main.py                 # FastAPI 入口
├── config/
│   ├── local.yaml              # 本地业务参数
│   └── cloud.yaml              # 云端配置（M7）
├── db/init/                    # 容器首次启动时执行的 SQL
│   ├── 01_schema.sql           # TimescaleDB 超表 + 压缩策略
│   └── 02_seed_main_contracts.sql
├── scripts/
│   ├── init_db.py              # DB 健康检查
│   ├── smoke_test.py           # M1 验收
│   ├── import_local.py         # CLI：本地历史导入
│   └── ingest_now.py           # CLI：手动触发 ingest
├── docs/期货预测平台需求文档_PRD.md
├── start.ps1 / start.bat       # 一键启动脚本（含 Docker 自动拉起）
├── docker-compose.yml          # 三容器编排
├── Dockerfile                  # 多阶段：node 构建看板 → python API 镜像
├── requirements.txt
├── .env.example                # 环境变量模板（提交）
└── .env                        # 真实密钥（不入库）
```

## 启动步骤（本地）

### 一键启动（推荐）

```powershell
cd E:\Docker\qhyc
.\start.ps1                # 启动全部服务（Docker 未运行会自动拉起 Docker Desktop）
.\start.ps1 -Build         # 代码变更后重建镜像并启动
.\start.ps1 -Action stop   # 停止（数据保留）
.\start.ps1 -Action status # 查看容器与健康状态
```

双击 `start.bat` 等效。脚本流程：拉起 Docker Desktop → `docker compose up -d`
→ 等待容器健康 → API/数据就绪自检 → 自动打开看板。

### 手动启动

```powershell
# 在工程根目录 E:\Docker\qhyc
copy .env.example .env
# 编辑 .env：填写 tqsdk 账号（已预填）/ DB 密码 / API Key 等
```

### 2. 一键起停

```powershell
# 启动（后台）
docker compose up -d --build

# 看日志
docker compose logs -f api
docker compose logs -f scheduler

# 停止
docker compose down
# 停止 + 删数据卷
docker compose down -v
```

### 3. M1 验收

```powershell
# 1. DB schema 校验
docker compose exec api python scripts/init_db.py

# 2. 本地历史导入（FG / SA）
docker compose exec api python scripts/import_local.py

# 3. 全量 smoke test
docker compose exec api python scripts/smoke_test.py
```

### 4. 触发一次手动采集

```powershell
# 全品种（首次会拉 2015 至今全量数据，耗时长）
docker compose exec api python scripts/ingest_now.py

# 单品种快速验证
docker compose exec api python scripts/ingest_now.py --symbol FG888
```

## 关键接口（M1，§7）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 健康检查 |
| GET  | `/symbols` | 品种列表 |
| GET  | `/symbols/{symbol}` | 单品种详情 |
| GET  | `/bars/{symbol}` | 日线查询 |
| GET  | `/bars/main/{product}` | 主连平滑/原始价 |
| POST | `/ingest` | 手动触发采集（async_run=true 后台） |
| POST | `/ingest/symbol/{symbol}` | 单品种采集 |
| GET  | `/anomalies` | 异常工单列表（status=pending/all） |
| POST | `/anomalies/{id}/resolve` | 裁决：`accept_tqsdk` 或 `false_positive` |
| GET  | `/calendar/open` | 交易日历查询 |
| GET  | `/calendar/missing` | 缺失检测 |
| POST | `/imports/local` | 本地历史数据导入 |
| GET  | `/imports/discover` | 扫描可导入品种 |
| GET  | `/tasks` / `/tasks/{id}` | 任务流水 |

完整 Swagger 文档：<http://localhost:8000/docs>

## 数据模型（§4.3，TimescaleDB 超表）

- `daily_bar`（超表）—— 主源/校准日线，按 `(symbol, trade_date)`
- `main_continuous`（超表）—— 主连原始/平滑两套价（⑪），按 `(product, trade_date)`
- `hourly_bar`（超表，M6）—— 小时线，按 `(symbol, trade_datetime)`
- `main_contract_map` —— 换月 + delta（⑪）
- `trade_calendar` —— 交易日历（含日/夜盘 sessions）
- `futures_symbol` —— 品种/合约元数据
- `anomaly_ticket` —— 校准异常工单
- `prediction_result` —— 预测结果（M2+）
- `backtest_result` —— 回测（M4+）
- `briefing_signal` —— 简报引擎信号入库（§13）
- `task_run` —— 任务流水

## 本地数据挂载（§4.5）

`docker-compose.yml` 把 `${LOCAL_QH_PATH}`（默认 `E:/Docker/qhyc/imports`）挂到容器内 `/app/imports`。
导入器按**文件名前缀**扫描（`**/data/{P}_*.{json,csv}`），兼容两种布局：

```
E:\Docker\qhyc\imports\
├── FG\data\            # 实际布局：FG 与 SA 文件混放于此
│   ├── FG_daily.json
│   ├── ...
│   ├── SA_daily.json
│   └── SA_rolls.csv
└── SA\data\            # PRD 假设布局（若后续整理成此结构亦可）
```

实测数据（dry-run 已验证）：

| 品种 | daily.json | cont_adj.json | rolls | contracts.json | hourly.csv |
|---|---|---|---|---|---|
| FG | 1605 行（2020-01-02~2026-08-17） | 1602 行 | 20 次换月 | 21 合约 / 5311 行 | 9076 行 |
| SA | 1623 行（2019-12-06~2026-08-17） | 1622 行 | 17 次换月 | 18 合约 / 4336 行 | 8260 行 |

注意：`E:\Docker\qhyc\imports` 下还有 `SX`、`TS`、`tqsdk` 等无关目录，导入器只认
`data/` 目录内 `{PROD}_daily.json` / `{PROD}_rolls.csv` 前缀文件，不会误导入。

## 调度（§4.2）

- `12:00`：盘中快照
- `16:00`：日盘收盘后完整入库
- 次日 `08:00`：含夜盘修正版

每次触发：akshare 拉取 → upsert daily_bar → tqsdk 补缺 → tqsdk 二次比对 → 异常工单。
任务状态可在 `/tasks` 查看。

## 资源占用（§9.1）

- 推荐：4 核 / 8 GB / 50 GB SSD
- 本仓库默认 docker-compose 配置：timescaledb 2 核 / 2 GB；api+scheduler 共享资源

## 安全注意

- `.env` 不入库（已加入 .gitignore）
- tqsdk 账号仅在容器内使用，不写日志、不打印
- 异常工单裁决接口只允许 `accept_tqsdk` 或 `false_positive`，禁止手改数值（⑰）

## M2 预测链路（已上线）

```powershell
# 单品种预测（§5.3 契约：方向+概率+幅度区间+置信度+状态门控）
Invoke-RestMethod -Method Post http://127.0.0.1:8000/predict -ContentType 'application/json' -Body '{"symbol":"FG888"}'

# 全市场批量预测（后台，供热力图）
Invoke-RestMethod -Method Post http://127.0.0.1:8000/predict/batch -ContentType 'application/json' -Body '{"async_run":true}'

# 预测留痕（同 as_of 多 run 可追溯）
Invoke-RestMethod 'http://127.0.0.1:8000/predict?symbol=FG888&limit=10'
```

- 模型（M3 全族 13 个）：傅里叶 / 马尔可夫 / ARIMA / **小波** / **GARCH** / **卡尔曼** / **HMM** / **贝叶斯** / **蒙特卡洛** / **随机森林** / **高斯过程** / **XGBoost** / **LSTM（torch-cpu）**
- Hurst 状态门控选择启用子集（trend→7 模型偏时序/ML；mean_revert→6 模型偏均值回复；neutral→8 模型）
- 口径：csv_smooth 优先（R1）；**M2.1 平滑主连自动延伸**——CSV 滞后段用换月检测 + 比例平滑自动延伸，锚定 CSV 末日水平（`scripts/extend_smooth.py`，每日 ingest 后自动执行）
- 联动 ⑦：每日三次 ingest 成功后自动预测；手动 `/ingest` 同样联动
- ⑱ LSTM 每周六 06:00 自动重训（权重 `/app/runtime/lstm/`，api/scheduler 共享卷）
- 门控 R3①：数据未就绪时 `/predict` 返回 503，不静默计算
- 参数（窗口/阈值/权重/门控子集）集中在 `config/local.yaml:predict`（容器只读挂载，改 yaml 免重建）

## §16 跨品种传导（M2 排期已上线）

- `config/transmission_config.yaml`：6 大类分类（黑/能化/农/有色新能源/贵金属/金融）+ 31 条产业链先验（原油系、煤焦钢、纯碱→玻璃、大豆压榨、替代对、需求反馈）
- 大类指数：成交量加权（成交额缺失自动回退），成分每日留痕，历史回补 13,692 行
- 传导特征 v1（9 个，全部滞后防前视）：sector 动量、sector_excess、上游 lag1/lag5、cost_gap、corr_regime、te_topk——rf/xgb/gpr/lstm 特征集已统一接入
- 金融期货（IF/IH/IC/IM/国债）默认**不纳入预测**（单品种请求返回 400）
- 预测响应新增 `drivers` 字段（v1 契约不变，v2 预留）
- 品种扩至 73（新增 HC/SF/SM + GFEX 的 SI/LC/PS，走 tqsdk 兜底采集）

## §16 跨品种传导（M3 动态层已上线）

- 动态权重（每周六 06:30 自动重算，与 LSTM 周训对齐）：corr / granger / 传递熵 / VAR / **blend=0.5×先验+0.5×数据**，样本不足回落纯先验
- 层级预测（§16.2 第 3 层）：大类指数（IDX:black 等 5 板块）先用同一模型库预测，落库后作为品种级特征 `sector_pred_prob`
- 特征 v2 共 10 个：`te_topk` 已切真实传递熵；CLI `scripts/calc_transmission.py`

## M4 回测引擎（已上线）

- 逐评估点滚动回测（⑲250 日窗口、每 3 根一评估点、防前视切片），per-model 方向准确率 / MAE / RMSE / 分位命中 / **Hurst 分层**
- ⑳ 权重月更闭环：`w = clip((acc60−0.5)×4, 0.1, 2.0)` 写入 `model_weights` 表，**预测自动读取 DB 权重**（回落 config）
- API：`POST /backtest`（async）、`GET /backtest`（最新 run 结果排名）、`POST /backtest/update-weights`
- 调度：周六 07:00 周度回测；每月 1 日 06:30 权重月更；CLI `scripts/run_backtest.py`

## M5 React 看板（已上线：http://localhost:8000/）

- 技术栈：Vite + React 18 多阶段构建（node 构建 → FastAPI 静态托管，无新增容器）
- 5 页签：品种总览（板块分组 + 最新预测）/ 预测详情（§16.6 传导卡片）/ 大类热力图
  （板块强弱排名 + 成员热力）/ 回测面板（跨品种聚合 + Wilson 95% CI + 未校准标注）/
  数据质量（就绪门控 + 日历模式 + 回补进度 + 异常工单）
- 展示规范（复验报告）：全部涨跌标收盘价口径徽标；dir_acc 带 Wilson 区间与样本量；
  ensemble 措辞"未显著优于随机"；rf/xgb/wavelet 区间标"未校准"
- 聚合 API：`/dashboard/overview|sectors|backtest-summary|quality` +
  `/dashboard/backtest/{run_id}/detail`（逐点下钻）

## 下一步

- §13 简报引擎对接（/integrations/*，caliber 契约已就绪）
- M6 小时级特征（hourly_bar 与连续聚合已就绪）
- M7 云端部署（cloud.yaml 就绪，Dockerfile 多阶段构建云端可用）