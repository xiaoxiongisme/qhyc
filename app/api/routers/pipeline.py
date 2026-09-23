"""M8 · 决策链路 API（docs/M8_决策链路容器化_PRD_20260922.md §8）。

并入现有 `qhyc-api`（不新开端口、不新建容器）；只做观测与手动触发，
**策略口径零改动**——本模块不参与任何策略计算。
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel

from app.core.logging import logger
from app.pipeline import config, recorder, snapshots

router = APIRouter()

_KINDS = ("daily", "signal", "intraday", "check")
_JSON_KEYS = ("tech", "fund", "research", "agent")


class RunRequest(BaseModel):
    kind: str = "daily"
    force: bool = False


def _manifest_state() -> dict:
    """快照 vs 源码 的 md5 清单校验（单一真源审计）。

    注意：run_pipeline.py 的 BASE 在**快照副本**上被打过 env 补丁（WB 的 P0 项
    尚未在源文件落地），比对时排除该文件，避免误报「落后」。
    """
    cfg = config.load()
    try:
        src_m = [m for m in snapshots.manifest_of(cfg.src) if m["name"] != "run_pipeline.py"]
        dst_m = [m for m in snapshots.manifest_of(cfg.dest) if m["name"] != "run_pipeline.py"]
        match = bool(src_m) and bool(dst_m) and (
            snapshots.manifest_digest(src_m) == snapshots.manifest_digest(dst_m)
        )
        patched = False
        rp = Path(cfg.dest) / "run_pipeline.py"
        if rp.exists():
            patched = 'os.environ.get("QH_BRIEF_BASE"' in rp.read_text(
                encoding="utf-8", errors="replace")
        return {"manifest_match": match, "base_patch_applied": patched,
                "src_files": len(src_m), "snap_files": len(dst_m)}
    except Exception as e:  # noqa: BLE001
        return {"manifest_match": None, "error": str(e)[:200]}


@router.get("/status")
def get_status() -> dict[str, Any]:
    cfg = config.load()
    out: dict[str, Any] = {
        "enabled": cfg.enabled,
        "snapshot_dir": cfg.dest,
        "brief_base": cfg.brief_base,
        "daily_cron": f"{cfg.daily_hour:02d}:{cfg.daily_minute:02d}",
        "signal_slots": cfg.signal_slots,
    }
    for k in _KINDS:
        out[k] = recorder.latest(k)
    out.update(_manifest_state())
    return out


@router.get("/runs")
def list_runs(kind: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    recorder.ensure_tables()
    return recorder.list_runs(kind=kind, limit=limit)


@router.get("/run/{run_id}")
def get_run(run_id: int) -> dict[str, Any]:
    recorder.ensure_tables()
    row = recorder.get_run(run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    return row


@router.get("/brief/{yyyymmdd}")
def get_brief(yyyymmdd: str) -> dict[str, Any]:
    """读某日简报 md + tech/fund/research/agent 四份 JSON 摘要。"""
    cfg = config.load()
    base = Path(cfg.brief_base)
    if not base.exists():
        raise HTTPException(status_code=503, detail=f"brief base 不可见: {base}")
    md_text, md_path = "", ""
    for p in sorted(base.rglob(f"*{yyyymmdd}*.md")):
        try:
            md_text = p.read_text(encoding="utf-8", errors="replace")
            md_path = str(p)
            break
        except Exception:  # noqa: BLE001
            continue
    jsons: dict[str, Any] = {}
    paths: dict[str, str] = {"md": md_path}
    for key in _JSON_KEYS:
        hit = None
        for p in sorted(base.rglob(f"*{yyyymmdd}*.json")):
            if key in p.name.lower():
                hit = p
                break
        if hit is None:
            jsons[key] = None
            continue
        paths[key] = str(hit)
        try:
            jsons[key] = json.loads(hit.read_text(encoding="utf-8", errors="replace"))
        except Exception as e:  # noqa: BLE001
            jsons[key] = {"_error": str(e)[:200]}
    if not md_path and not any(jsons.values()):
        raise HTTPException(status_code=404, detail=f"{yyyymmdd} 无简报产物")
    return {"date": yyyymmdd, "md": md_text, "json": jsons, "paths": paths}


def _bg_run(kind: str, force: bool) -> None:
    try:
        from app.pipeline.worker import run_once

        res = run_once(kind, wait_readiness=(kind == "daily"),
                       extra_args=(["--skip-quant"] if force else None))
        logger.info(f"[api] pipeline run {kind} -> {res.get('status')}")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[api] pipeline run {kind} failed: {e}")


@router.post("/run")
def trigger_run(req: RunRequest, bg: BackgroundTasks) -> dict[str, Any]:
    """手动触发（异步，不阻塞 worker）。daily 同日已有 ok/run 记录时需 force。"""
    if req.kind not in _KINDS:
        raise HTTPException(status_code=400, detail=f"kind 必须是 {_KINDS}")
    recorder.ensure_tables()
    if not req.force and req.kind == "daily":
        last = recorder.latest("daily")
        if last and last.get("run_date") == date.today().isoformat() \
                and last.get("status") in ("ok", "running"):
            return {"accepted": False, "reason": "今日 daily 已有记录，需 force=true",
                    "last": last}
    bg.add_task(_bg_run, req.kind, req.force)
    return {"accepted": True, "kind": req.kind, "force": req.force}


@router.get("/health")
def health() -> dict[str, Any]:
    from app.pipeline.healthcheck import _checks

    results = [{"name": n, "ok": ok, "note": note} for n, ok, note in _checks()]
    bad = [r["name"] for r in results if not r["ok"]]
    if bad:
        raise HTTPException(status_code=503, detail={"unhealthy": bad, "checks": results})
    return {"status": "ok", "checks": results}


@router.get("/manifest")
def manifest() -> dict[str, Any]:
    cfg = config.load()
    return {
        "generated_at": None,
        "src": cfg.src,
        "dest": cfg.dest,
        "files": snapshots.manifest_of(cfg.dest),
    }
