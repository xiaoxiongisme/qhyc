# -*- coding: utf-8 -*-
"""数据库定时备份（整改优先级 P1-1）。

形态：psycopg ``COPY (...) TO STDOUT`` 流式导出 → gzip → 本地目录 → 保留策略 → 异地副本。

为什么不是 pg_dump
------------------
容器内无 pg_dump / docker CLI（见 `app/ops/__init__.py` 说明）。逻辑备份覆盖的是
**不可重建的小表**（字典、配置、登记、权重、工单）——这些一旦丢失无法从行情源重算；
大体量行情表（fut_kline / minute_bar 等）由 05:00 派生作业可重建，默认不进每日备份
（可用 `--full-data` / env 显式加入，代价是时间与磁盘）。
"""
from __future__ import annotations

import gzip
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from app.core.logging import logger

# 默认备份表：小而不可重建优先（大体量行情表默认不进，可显式加入）
DEFAULT_BACKUP_TABLES: list[str] = [
    "schema_migrations",
    "cfg_feature_switch",
    "factor_registry",
    "dim_exchange",
    "dim_variety",
    "dim_contract",
    "model_weights",
    "portfolio_equity",
    "anomaly_ticket",
    "contract_code_map",
]


@dataclass
class BackupConfig:
    enabled: bool = True
    run_hour: int = 2
    run_minute: int = 0
    dir: str = "/app/runtime/backup"
    remote_dir: str = ""          # 异地目录（另一挂载点/NAS）；空字符串=不异地
    retain_days: int = 7
    tables: list[str] = field(default_factory=lambda: list(DEFAULT_BACKUP_TABLES))
    timeout_sec: int = 3600
    gzip_level: int = 6


def backup_config_from_env(env=None) -> BackupConfig:
    """从 EnvSettings/env 读取备份配置（未在 yaml 落地，避免误配混淆 miaoshu）。"""
    e = env
    if e is None:
        from app.core.config import get_settings
        e = get_settings().env

    def _i(name, default):
        v = os.getenv(name)
        try:
            return int(v) if v not in (None, "") else default
        except ValueError:
            return default

    def _b(name, default):
        v = os.getenv(name)
        if v in (None, ""):
            return default
        return str(v).strip().lower() in ("1", "true", "yes", "on")

    tables = os.getenv("BACKUP_TABLES", "").strip()
    return BackupConfig(
        enabled=_b("BACKUP_ENABLED", True),
        run_hour=_i("BACKUP_HOUR", 2),
        run_minute=_i("BACKUP_MINUTE", 0),
        dir=os.getenv("BACKUP_DIR", "/app/runtime/backup") or "/app/runtime/backup",
        remote_dir=os.getenv("BACKUP_REMOTE_DIR", "") or "",
        retain_days=_i("BACKUP_RETAIN_DAYS", 7),
        tables=[t.strip() for t in tables.split(",") if t.strip()]
        if tables else list(DEFAULT_BACKUP_TABLES),
        timeout_sec=_i("BACKUP_TIMEOUT_SEC", 3600),
    )


def _count_rows(session, table: str) -> int:
    return int(session.execute(text('SELECT count(*) FROM "{0}"'.format(table))).scalar() or 0)


def _dump_table(session, table: str, dest_dir: str, cfg: BackupConfig) -> dict:
    """单表流式导出为 gzip 文件，返回 {table, file, rows, bytes}。"""
    path = os.path.join(dest_dir, table + ".sql.gz")
    rows = _count_rows(session, table)
    conn = session.connection().connection          # DBAPI 连接（psycopg3）
    started = time.time()
    written = 0
    with conn.cursor() as cur, gzip.open(
        path, "wb", compresslevel=cfg.gzip_level
    ) as gz:
        with cur.copy('COPY (SELECT * FROM "{0}") TO STDOUT'.format(table)) as cp:
            while True:
                chunk = cp.read()
                if not chunk:
                    break
                if isinstance(chunk, memoryview):
                    chunk = chunk.tobytes()
                gz.write(chunk)
                written += len(chunk)
    return {
        "table": table, "file": os.path.basename(path), "rows": rows,
        "bytes": written, "compressed_bytes": os.path.getsize(path),
        "seconds": round(time.time() - started, 2),
    }


def _remote_copy(dest_dir: str, remote_dir: str, files: list[str]) -> dict:
    if not remote_dir:
        return {"enabled": False, "copied": 0, "note": "未配置 BACKUP_REMOTE_DIR"}
    try:
        os.makedirs(remote_dir, exist_ok=True)
        n = 0
        for f in files:
            shutil.copy2(os.path.join(dest_dir, f), os.path.join(remote_dir, f))
            n += 1
        return {"enabled": True, "copied": n, "dir": remote_dir}
    except Exception as e:      # 异地不可用不应导致备份整体失败
        return {"enabled": True, "copied": 0, "error": str(e)[:200]}


def _retention(dest_dir: str, retain_days: int) -> dict:
    """按保留天数清理历史备份目录 dt-snapshot 只清理“本次未用到”的旧目录。"""
    removed, kept = 0, 0
    now = time.time()
    try:
        for name in os.listdir(dest_dir):
            p = os.path.join(dest_dir, name)
            if not os.path.isdir(p):
                continue
            age_days = (now - os.path.getmtime(p)) / 86400.0
            if age_days > retain_days:
                shutil.rmtree(p, ignore_errors=True)
                removed += 1
            else:
                kept += 1
    except Exception as e:
        return {"removed": 0, "kept": 0, "error": str(e)[:120]}
    return {"removed": removed, "kept": kept, "retain_days": retain_days}


def run_backup(session=None, cfg: BackupConfig | None = None,
               dest_dir: str | None = None, tables: list[str] | None = None,
               remote_dir: str | None = None) -> dict:
    """执行一次备份。返回结构化报告（出错不抛，写入 report.error）。"""
    cfg = cfg or backup_config_from_env()
    started = time.time()
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d_%H%M%S")
    dest_dir = dest_dir or os.path.join(cfg.dir, stamp)
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except Exception as e:
        return {"ok": False, "error": "备份目录不可写: {0}".format(e)}

    tables = tables or cfg.tables
    remote_dir = remote_dir if remote_dir is not None else cfg.remote_dir

    report: dict = {
        "ok": True, "started_at": stamp, "dir": dest_dir,
        "tables": [], "failed": [], "total_rows": 0, "total_bytes": 0,
    }

    owns_session = session is None
    if owns_session:
        from app.core.db import session_scope
        ctx = session_scope()
        session = ctx.__enter__()
    try:
        for t in tables:
            try:
                info = _dump_table(session, t, dest_dir, cfg)
                report["tables"].append(info)
                report["total_rows"] += info["rows"]
                report["total_bytes"] += info["compressed_bytes"]
                logger.info("[backup] {0} {1} 行 → {2} ({3} 字节压缩后)".format(
                    t, info["rows"], info["file"], info["compressed_bytes"]))
            except Exception as e:
                report["failed"].append({"table": t, "error": str(e)[:200]})
                logger.warning("[backup] 表 {0} 备份失败: {1}".format(t, str(e)[:160]))
        manifest = {
            "stamp": stamp, "tables": report["tables"], "failed": report["failed"],
            "total_rows": report["total_rows"], "created_at": stamp,
        }
        with open(os.path.join(dest_dir, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
    finally:
        if owns_session:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass

    report["remote"] = _remote_copy(
        dest_dir, remote_dir,
        [os.path.basename(os.path.join(dest_dir, x["file"]))
         for x in report["tables"]] + ["manifest.json"])
    report["retention"] = _retention(cfg.dir, cfg.retain_days)
    report["seconds"] = round(time.time() - started, 2)
    report["ok"] = not report["failed"]
    logger.info("[backup] 完成 {0} 表 / {1} 行 / {2}s / ok={3}".format(
        len(report["tables"]), report["total_rows"], report["seconds"], report["ok"]))
    return report


def verify_backup(backup_dir: str) -> dict:
    """校验一个备份目录：manifest 存在、每个文件非空、行数与 manifest 一致。"""
    mpath = os.path.join(backup_dir, "manifest.json")
    res: dict = {"dir": backup_dir, "ok": False, "tables": [], "issues": []}
    if not os.path.exists(mpath):
        res["issues"].append("manifest.json 缺失")
        return res
    try:
        with open(mpath, encoding="utf-8") as f:
            man = json.load(f)
    except Exception as e:
        res["issues"].append("manifest 解析失败: {0}".format(str(e)[:120]))
        return res
    for t in man.get("tables", []):
        p = os.path.join(backup_dir, t["file"])
        ok_filesize = os.path.exists(p) and os.path.getsize(p) > 0
        ent = {"table": t["table"], "file": t["file"], "rows": t["rows"],
               "exists": ok_filesize}
        if not ok_filesize:
            res["issues"].append("{0} 文件缺失或为空".format(t["file"]))
        res["tables"].append(ent)
    res["ok"] = not res["issues"]
    return res
