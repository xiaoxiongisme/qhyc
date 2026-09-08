"""
统一日志（loguru）
- 控制台 + 文件双输出
- 文件按 50 MB 滚动，保留 10 个
"""
from __future__ import annotations

import sys
from pathlib import Path

from loguru import logger

from app.core.config import get_settings

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOG_DIR = _PROJECT_ROOT / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return
    # Windows GBK 控制台兼容：统一重配置为 UTF-8，避免特殊字符导致输出失败
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    settings = get_settings()
    level = settings.log_level.upper()

    # 移除默认 handler
    logger.remove()

    # 控制台：彩色、紧凑
    logger.add(
        sys.stdout,
        level=level,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <7}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<level>{message}</level>"
        ),
        enqueue=False,
        backtrace=False,
        diagnose=False,
    )

    # 文件：滚动
    logger.add(
        _LOG_DIR / "qhyc.log",
        level=level,
        rotation="50 MB",
        retention=10,
        encoding="utf-8",
        enqueue=False,
        backtrace=False,
        diagnose=False,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | {name}:{function}:{line} | {message}",
    )

    # 错误单独文件
    logger.add(
        _LOG_DIR / "qhyc.err.log",
        level="ERROR",
        rotation="50 MB",
        retention=10,
        encoding="utf-8",
        enqueue=False,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | {name}:{function}:{line} | {message}",
    )

    _configured = True
    logger.info(f"[logging] initialized level={level} log_dir={_LOG_DIR}")


__all__ = ["logger", "setup_logging"]