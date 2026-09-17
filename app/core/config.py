"""
配置加载
- .env 由 pydantic-settings 解析
- YAML（config/local.yaml | cloud.yaml）由 ruamel / pyyaml 解析
- 运行时根据 APP_ENV 选择（默认 local）
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"
ENV_FILE = PROJECT_ROOT / ".env"


# ---------- .env 强类型 ----------
class EnvSettings(BaseSettings):
    """从 .env 读取的密钥与基础环境变量（不进 git）"""

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # tqsdk
    TQSDK_PHONE: str = ""
    TQSDK_PASSWORD: str = ""

    # 数据库
    POSTGRES_USER: str = "futures"
    POSTGRES_PASSWORD: str = ""
    POSTGRES_DB: str = "futures"
    POSTGRES_PORT: int = 5432
    POSTGRES_HOST: str = "timescaledb"

    # 集成
    INTEGRATION_API_KEY: str = ""

    # 微信推送（pushplus / 推送加）——融合策略信号每15分钟推送
    PUSHPLUS_TOKEN: str = ""

    # 采集/校准
    INGEST_CRON_1: str = "08:00"     # 早盘前（决策 7）
    INGEST_CRON_2: str = "12:30"     # 午盘时
    INGEST_CRON_3: str = "20:00"     # 夜盘前
    TQSDK_PRICE_DIFF_THRESHOLD_PCT: float = 0.5
    TQSDK_TIMEOUT_SEC: int = 15
    TQSDK_MAX_RETRIES: int = 2
    HISTORY_START_DATE: str = "2015-01-01"

    # Web
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    LOG_LEVEL: str = "INFO"
    TZ: str = "Asia/Shanghai"

    # 运行时角色：api / scheduler / all（容器内区分）
    ROLE: str = "api"

    # 整体覆盖
    DATABASE_URL: str | None = None

    def database_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return (
            f"postgresql+psycopg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )


# ---------- YAML 业务参数 ----------
class MainContractSpec(BaseModel):
    symbol: str
    exchange: str
    product: str
    name: str
    unit: str
    multiplier: float


class IngestConfig(BaseModel):
    history_start: str = "2015-01-01"
    main_contracts: list[MainContractSpec] = Field(default_factory=list)
    tqsdk_symbol_template: str = "KQ.m@{exchange}.{product}888"


class CalibrationConfig(BaseModel):
    # R1 口径版本（§15.R1）：模型口径 = csv 平滑主连
    canonical_main_source: str = "csv_smooth"
    canonical_main_version: str = "v1_csv_smooth"
    price_diff_threshold_pct: float = 0.5
    volume_anomaly: bool = True
    timeout_sec: int = 15
    max_retries: int = 2


class ReadinessConfig(BaseModel):
    """R3① 数据就绪门控参数"""
    ready_ratio: float = 0.9
    max_lag_days: int = 5


class BacktestConfig(BaseModel):
    """M4 审计修复配置（P1/P2）+ M4.1 walk-forward + M5.5 成本模型"""
    label_metric: str = "close"        # P2-1 已裁决 close（PRD §15/§17，2026-09-08）；同时作为契约 caliber 声明
    min_sample_n: int = 60             # P1-2 权重月更最小样本
    min_symbols_in_run: int = 10       # P1-2 仅整批 run 参与权重月更
    lstm_weight_freeze: float | None = None  # P1-1：null=不冻结（M4.1 泄漏已根治）
    lstm_retrain_every: int = 60       # M4.1 walk-forward 重训节奏（根）
    cost_pct: float = 0.065            # §18.3（v1.3）：双边成本 手续费+滑点 %（可配置，18.11③）
    coverage_grid: list[float] = Field(  # §18.3 覆盖率-准确率曲线网格（|point| 分位阈值）
        default_factory=lambda: [0.1, 0.2, 0.3, 0.5, 0.7, 1.0]
    )


class PredictWindowsConfig(BaseModel):
    ret: list[int] = Field(default_factory=lambda: [5, 20])
    vol: list[int] = Field(default_factory=lambda: [5, 20])


class PredictHurstConfig(BaseModel):
    window: int = 250
    trend_threshold: float = 0.55
    mean_revert_threshold: float = 0.45


class PredictLstmConfig(BaseModel):
    seq_len: int = 20
    epochs: int = 30
    weight_dir: str = "/app/runtime/lstm"


class ReversalConfig(BaseModel):
    """§18.2（v1.3）P1 条件反转 + 信号门控"""
    gate_pct: float = 2.0        # |ret_t| <= gate_pct% → 无观点；v1.3.3 对齐 research τ*=2.0%（验收=上线）
    strength: float = 0.3        # 反转幅度点估计 = -ret_t × strength
    oi_boost_prob: float = 0.05  # Δoi>0 软上调（非硬门控）；oi 缺失时跳过
    max_prob: float = 0.85


class PooledConfig(BaseModel):
    """M6c.1（P6）池化面板分类模型：rf/xgb 改分类目标 + 全品种池化"""
    window: int = 250            # 单品种推断窗口（根）
    n_estimators: int = 200
    max_depth: int = 6
    model_dir: str = "/app/runtime/pooled"   # 训练落盘目录（与 LSTM 同机制）
    train_end: str | None = None  # 防泄漏快照截止日（None=全历史）；回测建议按 eval_date 设置


class PredictConfig(BaseModel):
    """M2/M3 预测引擎参数（PRD §5.1–5.4）"""
    model_set: str = "m3"
    history_bars: int = 250
    min_bars: int = 60
    windows: PredictWindowsConfig = Field(default_factory=PredictWindowsConfig)
    hurst: PredictHurstConfig = Field(default_factory=PredictHurstConfig)
    weights: dict[str, float] = Field(
        default_factory=lambda: {"fourier": 1.0, "markov": 1.0, "arima": 1.0}
    )
    gate_models: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "trend": ["arima", "fourier"],
            "mean_revert": ["markov", "fourier"],
            "neutral": ["fourier", "markov", "arima"],
        }
    )
    lstm: PredictLstmConfig = Field(default_factory=PredictLstmConfig)
    reversal: ReversalConfig = Field(default_factory=ReversalConfig)
    pooled: PooledConfig = Field(default_factory=PooledConfig)


class CronSpec(BaseModel):
    hour: int
    minute: int
    label: str


class HourlyConfig(BaseModel):
    """小时线每小时自动更新（决策 7）。"""
    enabled: bool = True
    minute: int = 0                # 每小时该分钟触发（配合 hour='*'）
    run_hour: str = "all"          # 'all' = 全天 24 次；或指定小时列表


class SchedulerConfig(BaseModel):
    cron: list[CronSpec] = Field(default_factory=list)
    hourly: HourlyConfig = Field(default_factory=HourlyConfig)


class InventoryConfig(BaseModel):
    """库存 / 仓单自动采集（决策 2）。数据源 akshare futures_inventory_em。"""
    enabled: bool = False
    freq: str = "weekly"           # weekly / daily
    run_day: int = 5               # 周频：周几（0=周一 … 6=周日）
    run_hour: int = 17
    run_minute: int = 0


class SpotBasisConfig(BaseModel):
    """基差 / 现货自动采集（决策 2）。数据源 akshare futures_spot_price_daily。"""
    enabled: bool = False
    freq: str = "daily"
    run_hour: int = 17
    run_minute: int = 10
    window_days: int = 5           # 每次增量回填最近 N 天


class ImportsConfig(BaseModel):
    source_root: str = ""
    container_root: str = "/app/imports"
    format_version: int = 1


class AppYAML(BaseModel):
    env: str = "local"
    debug: bool = False
    log_level: str = "INFO"
    timezone: str = "Asia/Shanghai"


class DbYAML(BaseModel):
    url_env: str = "DATABASE_URL"
    pool_size: int = 5
    max_overflow: int = 10
    echo: bool = False


class FusionConfig(BaseModel):
    """融合策略实时信号扫描（每15分钟）参数——与回测验证口径一致（额外执行口径）"""
    enabled: bool = True
    ema_k: int = 140                 # 方向层 EMA 周期（小时线）
    atr_n: int = 14                 # ATR 周期
    ma_n: int = 20                  # 入场 MA20
    sl_atr: float = 2.0             # 初始止损 = sl_atr × ATR（额外口径=2.0）
    trail_atr: float = 2.0          # 吊灯移动止损 = trail_atr × ATR
    be_r: float = 0.5               # 保本触发 = 0.5 × 初始风险
    W: int = 60                     # 波峰/波谷回溯窗口
    thr: float = 0.03               # 行情分级斜率阈值（实际未用于门控）
    entry_mode: str = "both_nm"     # 回踩 + 突破（去 MACD）
    cooldown_bars: int = 3          # 离场后冷却根数
    min_bars: int = 160             # 最少历史根数（需 >= ema_k + 缓冲）
    stale_minutes: int = 90         # 「信号新鲜度」上限（分钟）：最新K超过此值则抑制状态变化信号
    display_max_age_min: int = 14400  # 「播报展示」上限（分钟，默认10天）
    #   为什么两个阈值：夜盘收盘到次日早盘天然有 585 分钟空档，周末/长假更是几十小时。
    #   用同一个 90 分钟会把所有持仓从播报里过滤光（早盘 09:00-10:00 的简报变成空的）。
    #   因此：信号要「新」（防用过期K触发），展示可「旧」（持仓本身还在，只是价格旧）。
    #   单根K超过 1 天时会在行尾标注数据时间。
    max_bars: int = 400             # 单次读取小时线根数上限
    pushplus_token: str = ""        # 微信推送 TOKEN（也可放 .env 的 PUSHPLUS_TOKEN）
    # ---- 推送形态（2026-09-14 新增，2026-09-15 扩展节奏）----
    push_stop_levels: bool = True   # 推送中给出止损位 / 保本触发价
    heartbeat: bool = True          # 定时播报当前持仓（默认开；false=只在有信号时推）
    heartbeat_interval_min: int = 30  # 定时播报的最小间隔（分钟）；实际只在 :00/:30 触发
    heartbeat_max_rows: int = 60    # 播报行数上限（超出按浮动盈亏绝对值排序取前 N）
    # 开市前预播报（本地时间，强制推送且带完整止损/保本位）：开盘前给你一次完整清单
    pre_session_times: list[str] = Field(
        default_factory=lambda: ["08:45", "13:15", "20:45"]
    )
    # 吊灯止损「朝有利方向」移动 ≥ trail_push_atr×ATR 时推送一条（0=关闭）
    trail_push_atr: float = 0.5
    # 开仓信号在此分钟数内每轮复提（防漏看）；0=不复提
    signal_repeat_min: int = 120
    push_log: bool = True           # 推送留痕写 fusion_push_log 表（可随时回查历史信号）
    # 备用通道：pushplus 失败时降级发送（企业微信/钉钉/飞书机器人 webhook，留空=不启用）
    fallback_webhook: str = ""
    # 允许推送的时间窗（本地时间，"HH:MM-HH:MM"，可多个）。窗口需覆盖"小时K收盘+采集延迟"，
    # 例如 11:30 收盘的K要到 11:31~11:35 才入库，所以上午窗口给到 12:00。空列表 = 不限制。
    push_windows: list[str] = Field(
        default_factory=lambda: ["08:45-12:00", "13:15-15:30", "20:45-23:30"]
    )
    closed_bars_only: bool = True   # 只用「已收盘」小时K评估（防信号重绘，与回测口径一致）
    # 收盘「定稿」缓冲（秒）。K 刚收盘的瞬间，数据源（新浪分钟线）尚未定稿：实测
    # 09:00-10:00 这根在 10:00:00 收盘，10:00:20 取到 close=3814，几分钟后被改写为 3810。
    # 若不等待，推送给用户的入场价/止损/保本会整体偏移（JD 案例差 4 点），
    # 用户在盘面上「找不到这个价」。故：最新K距now不足该秒数时，等够再重采一次。
    settle_delay_sec: int = 150
    # 引擎消费哪一套小时线口径（必须与回测/看盘一致，否则周期参数被砍半、信号与图表对不上）。
    # - "akshare" = 同花顺口径（K线按收盘时刻标：日盘 10:00/11:15/14:15/15:00），与用户盘面一致（推荐）
    # - "tqsdk"   = 起点口径（K线按整点起点标：日盘 09:00/10:00/11:00/13:00/14:00），与历史 CSV 回测一致
    # 两套时间戳不同，不可混用；本字段强制引擎只读取单一 src，杜绝"同一段行情数两遍"。
    hourly_src: str = "akshare"


class RankPositionConfig(BaseModel):
    """会员持仓排名（龙虎榜）每日入库。

    数据源：新浪财经期货成交持仓（akshare futures_hold_pos_sina + match_main_contract，
    见 app/ingest/rank_position.py）。仅商品期货；金融期货（CFFEX）不纳入、不补（决策 3）。
    exchanges：走新浪 match_main_contract 的商品期货交易所列表。
    """
    enabled: bool = False
    exchanges: list[str] = Field(default_factory=lambda: ["CZCE", "SHFE", "DCE", "GFEX", "INE"])
    run_hour: int = 17
    run_minute: int = 30


class YamlConfig(BaseModel):
    app: AppYAML = Field(default_factory=AppYAML)
    database: DbYAML = Field(default_factory=DbYAML)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    calibration: CalibrationConfig = Field(default_factory=CalibrationConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    inventory: InventoryConfig = Field(default_factory=InventoryConfig)
    spot_basis: SpotBasisConfig = Field(default_factory=SpotBasisConfig)
    imports: ImportsConfig = Field(default_factory=ImportsConfig)
    readiness: ReadinessConfig = Field(default_factory=ReadinessConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    predict: PredictConfig = Field(default_factory=PredictConfig)
    fusion: FusionConfig = Field(default_factory=FusionConfig)
    rank_position: RankPositionConfig = Field(default_factory=RankPositionConfig)


@lru_cache(maxsize=1)
def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache(maxsize=1)
def get_yaml_config() -> YamlConfig:
    """根据 APP_ENV 选择 local.yaml / cloud.yaml"""
    env = os.getenv("APP_ENV", "local").lower()
    name = "cloud.yaml" if env == "cloud" else "local.yaml"
    cfg_path = CONFIG_DIR / name
    raw = _load_yaml(cfg_path)
    return YamlConfig.model_validate(raw)


@lru_cache(maxsize=1)
def get_env() -> EnvSettings:
    return EnvSettings()


class Settings(BaseModel):
    """运行期统一配置（env + yaml 合并）"""

    env: EnvSettings
    yaml: YamlConfig

    @property
    def db_url(self) -> str:
        return self.env.database_url()

    @property
    def log_level(self) -> str:
        return self.env.LOG_LEVEL or self.yaml.app.log_level

    @property
    def main_contracts(self) -> list[MainContractSpec]:
        return self.yaml.ingest.main_contracts

    @property
    def history_start(self) -> str:
        return self.yaml.ingest.history_start or self.env.HISTORY_START_DATE

    @property
    def calibration_threshold_pct(self) -> float:
        return float(
            self.yaml.calibration.price_diff_threshold_pct
            or self.env.TQSDK_PRICE_DIFF_THRESHOLD_PCT
        )

    @property
    def cron_specs(self) -> list[CronSpec]:
        return self.yaml.scheduler.cron

    @property
    def hourly_config(self) -> HourlyConfig:
        return self.yaml.scheduler.hourly

    @property
    def inventory_config(self) -> InventoryConfig:
        return self.yaml.inventory

    @property
    def spot_basis_config(self) -> SpotBasisConfig:
        return self.yaml.spot_basis

    @property
    def fusion(self) -> FusionConfig:
        return self.yaml.fusion

    @property
    def pushplus_token(self) -> str:
        return self.env.PUSHPLUS_TOKEN or self.yaml.fusion.pushplus_token


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(env=get_env(), yaml=get_yaml_config())


# 调试用入口
if __name__ == "__main__":
    s = get_settings()
    print("[config] env=", s.env.POSTGRES_HOST)
    print("[config] main_contracts=", len(s.main_contracts))
    print("[config] cron=", [(c.hour, c.minute, c.label) for c in s.cron_specs])