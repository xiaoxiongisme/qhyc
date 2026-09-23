"""M8 pipeline 运行期配置：`config/local.yaml` 的 `pipeline:` 段 + 环境变量覆盖。

优先级：**环境变量 > local.yaml > 内置默认**（容器内由 compose 注入 env）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_SIGNAL_SLOTS = ["09:00", "10:00", "11:00", "13:30", "14:00", "15:00",
                         "21:00", "22:00", "23:00"]


@dataclass
class PipelineSettings:
    enabled: bool = True
    # 日链（17:40：给 rank_position 17:30 留 10 分钟，PRD §6.2）
    daily_hour: int = 17
    daily_minute: int = 40
    signal_slots: list[str] = field(default_factory=lambda: list(DEFAULT_SIGNAL_SLOTS))
    intraday_enabled: bool = True
    # 周自检（周一 08:05）
    check_day: str = "mon"
    check_hour: int = 8
    check_minute: int = 5
    # 数据就绪闸门
    readiness_enabled: bool = True
    readiness_timeout_min: int = 10
    readiness_poll_sec: int = 60
    # 启动快照
    src: str = "/app/pipeline_src"
    dest: str = "/app/runtime/pipeline_snapshot"
    # 执行
    step_timeout_sec: int = 1800
    max_instances: int = 1
    notify_on_failure: bool = True
    # 产出根目录（容器内）
    brief_base: str = "/app/qh"


def _env_str(key: str) -> str:
    return (os.environ.get(key) or "").strip()


def load() -> PipelineSettings:
    """读取配置；local.yaml 缺 `pipeline:` 段时静默用默认值（向后兼容）。"""
    s = PipelineSettings()
    try:
        from app.core.config import get_settings

        cfg = getattr(get_settings().yaml, "pipeline", None)
    except Exception:  # noqa: BLE001 - 配置不可用时仍应能用默认值启动
        cfg = None

    if cfg is not None:
        s.enabled = bool(getattr(cfg, "enabled", s.enabled))
        d = getattr(cfg, "daily_cron", None)
        if d is not None:
            s.daily_hour = int(getattr(d, "hour", s.daily_hour))
            s.daily_minute = int(getattr(d, "minute", s.daily_minute))
        slots = getattr(cfg, "signal_slots", None)
        if slots:
            s.signal_slots = [str(x) for x in slots]
        c = getattr(cfg, "check_cron", None)
        if c is not None:
            s.check_day = str(getattr(c, "day_of_week", s.check_day))
            s.check_hour = int(getattr(c, "hour", s.check_hour))
            s.check_minute = int(getattr(c, "minute", s.check_minute))
        r = getattr(cfg, "readiness", None)
        if r is not None:
            s.readiness_enabled = bool(getattr(r, "enabled", s.readiness_enabled))
            s.readiness_timeout_min = int(getattr(r, "timeout_min", s.readiness_timeout_min))
            s.readiness_poll_sec = int(getattr(r, "poll_sec", s.readiness_poll_sec))
        sn = getattr(cfg, "snapshot", None)
        if sn is not None:
            s.src = (getattr(sn, "src", "") or s.src)
            s.dest = (getattr(sn, "dest", "") or s.dest)
        s.step_timeout_sec = int(getattr(cfg, "step_timeout_sec", s.step_timeout_sec))
        s.max_instances = int(getattr(cfg, "max_instances", s.max_instances))
        s.notify_on_failure = bool(getattr(cfg, "notify_on_failure", s.notify_on_failure))
        s.intraday_enabled = bool(getattr(cfg, "intraday_enabled", s.intraday_enabled))

    # 环境变量覆盖（compose 注入）
    if _env_str("PIPELINE_SRC"):
        s.src = _env_str("PIPELINE_SRC")
    if _env_str("PIPELINE_SNAPSHOT"):
        s.dest = _env_str("PIPELINE_SNAPSHOT")
    if _env_str("QH_BRIEF_BASE"):
        s.brief_base = _env_str("QH_BRIEF_BASE")
    if _env_str("PIPELINE_ENABLED"):
        s.enabled = _env_str("PIPELINE_ENABLED").lower() in ("1", "true", "yes")
    return s
