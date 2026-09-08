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

    # 采集/校准
    INGEST_CRON_1: str = "12:00"
    INGEST_CRON_2: str = "16:00"
    INGEST_CRON_3: str = "08:00"
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
    """M4 审计修复配置（P1/P2）+ M4.1 walk-forward"""
    label_metric: str = "close"        # P2-1：close（现状）| settle（待用户裁决切换）
    min_sample_n: int = 60             # P1-2 权重月更最小样本
    min_symbols_in_run: int = 10       # P1-2 仅整批 run 参与权重月更
    lstm_weight_freeze: float | None = None  # P1-1：null=不冻结（M4.1 泄漏已根治）
    lstm_retrain_every: int = 60       # M4.1 walk-forward 重训节奏（根）


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


class CronSpec(BaseModel):
    hour: int
    minute: int
    label: str


class SchedulerConfig(BaseModel):
    cron: list[CronSpec] = Field(default_factory=list)


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


class YamlConfig(BaseModel):
    app: AppYAML = Field(default_factory=AppYAML)
    database: DbYAML = Field(default_factory=DbYAML)
    ingest: IngestConfig = Field(default_factory=IngestConfig)
    calibration: CalibrationConfig = Field(default_factory=CalibrationConfig)
    scheduler: SchedulerConfig = Field(default_factory=SchedulerConfig)
    imports: ImportsConfig = Field(default_factory=ImportsConfig)
    readiness: ReadinessConfig = Field(default_factory=ReadinessConfig)
    backtest: BacktestConfig = Field(default_factory=BacktestConfig)
    predict: PredictConfig = Field(default_factory=PredictConfig)


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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings(env=get_env(), yaml=get_yaml_config())


# 调试用入口
if __name__ == "__main__":
    s = get_settings()
    print("[config] env=", s.env.POSTGRES_HOST)
    print("[config] main_contracts=", len(s.main_contracts))
    print("[config] cron=", [(c.hour, c.minute, c.label) for c in s.cron_specs])