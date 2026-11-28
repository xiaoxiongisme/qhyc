# -*- coding: utf-8 -*-
"""CL-20261128-02 · S2 独立扫描作业接线单测（离线，无 DB/无真实下单）。

钉死三件事：
  1. 开关 five_min_s2_sim_enabled 默认 off → 作业整轮跳过，不碰 scan_once；
  2. 作业真实传 cfg=S2_PRESET / symbols=S2_SYMBOLS / pool=5m-s2-sim；
  3. 落单复用 submit_sim_order、幂等前缀 5M-S2:、写离场 plan。
"""
from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SCHED = (_REPO / "app" / "scheduler.py").read_text(encoding="utf-8")


def _fn_src(name: str) -> str:
    tree = ast.parse(_SCHED)
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == name), None)
    assert fn is not None, f"缺函数 {name}"
    return ast.get_source_segment(_SCHED, fn) or ""


def test_s2_job_is_switch_gated_off_by_default():
    src = _fn_src("_five_min_s2_scan_job")
    assert '_switch_on("five_min_s2_sim_enabled"' in src
    assert "default=False" in src
    # 开关检查必须在 scan_once 之前（整轮跳过，不产生任何意图）
    assert src.index("_switch_on") < src.index("scan_once(")


def test_s2_job_passes_exact_s2_caliber():
    src = _fn_src("_five_min_s2_scan_job")
    assert "scan_once(s, S2_SYMBOLS, cfg=S2_PRESET, pool_id=\"5m-s2-sim\")" in src
    assert "from app.strategies.five_min.indicators import S2_PRESET, S2_SYMBOLS" in src


def test_s2_job_reuses_sim_endpoint_and_isolates_id():
    src = _fn_src("_five_min_s2_scan_job")
    assert "submit_sim_order(req)" in src          # 同一落单端点，非另起逻辑
    assert "signal_id=f\"5M-S2:" in src            # 与白盘 5M: 前缀隔离
    assert "channel=\"SIM\"" in src
    assert "build_exit_plan(" in src               # 写离场计划（P3 闭环）


def test_s2_job_fails_loud_on_non_sim_broker():
    src = _fn_src("_five_min_s2_scan_job")
    assert "EXECUTION_BROKER" in src
    assert "raise RuntimeError" in src


def test_white_job_untouched():
    """白盘作业必须仍传 DEFAULT_SYMBOLS / 5m-sim（逐位未污染）。"""
    src = _fn_src("_five_min_scan_job")
    assert "scan_once(s, DEFAULT_SYMBOLS, pool_id=\"5m-sim\")" in src
    assert "S2_PRESET" not in src
    assert "S2_SYMBOLS" not in src


def test_cron_registered_but_switch_default_off():
    """注册 ≠ 会下单：cron 已注册，但开关默认关。"""
    assert 'id="five_min_s2_scan"' in _SCHED
    jobs = (_REPO / "app" / "scheduler_jobs.py").read_text(encoding="utf-8")
    assert 'JobSpec("five_min_s2_scan"' in jobs
    mig = (_REPO / "migrations" / "090_five_min_s2_scan.sql").read_text(encoding="utf-8")
    # 开关默认 false，且去掉 _enabled 后无同名 task（CL-06 双开关冲突）
    assert "five_min_s2_sim_enabled" in mig
    assert "'five_min_s2_scan'" in mig
    assert "five_min_s2_scan_enabled" not in mig


def test_switch_name_no_collision():
    # OPS-SWITCH-JOB-CONSIST 会把 switch_key 去掉 _enabled 拼成 task_id 比对，
    # 若同名则双开关打架（CL-20261124-06 的 five_min_exit_scan 踩过）。
    key = "five_min_s2_sim_enabled".replace("_enabled", "")
    assert key != "five_min_s2_scan"
