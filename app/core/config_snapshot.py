# -*- coding: utf-8 -*-
"""启动参数快照 + 当前生效 params_hash（P2-2 / CL-20261108-10）。

单一真源
--------
参数指纹的定义与覆盖链解析**全部复用** ``scripts/ops/dump_fusion_profile.py``
（P2-1 / CL-20261108-07 已验收）。这里刻意不再实现第二份 hash 逻辑：
CLI（人读/CI）、守卫断言、app 运行时三处消费者共用一份实现，才不会出现
「回测报告的参数」与「生产在跑的参数」各算各的。

⚠ 依赖方向说明：app 运行时通过文件路径加载 scripts 下的 helper，属于有意的
   反向引用。理由是 P2-1 的 CLI 与守卫已经以该文件为真源，把它搬进 app/ 需要
   同时改三处并重新做双环境 hash 验收；当前先用只读加载保持单一真源，代价是
   app 依赖仓库里存在 scripts/ops/dump_fusion_profile.py（发布通道保证同步）。

语义
----
* ``current_params_hash()`` 返回**本进程当前生效**的 signal_params_hash。
  ``get_settings()`` 本身是 ``lru_cache``，所以这正好反映「这个进程加载的那套
  参数」；配置文件被改而进程未重载时，快照里的 ``config_sha1`` 会是磁盘新值，
  而 hash 仍是进程旧值——这个差异本身就是有价值的运行态诊断信号。
* ``record_config_snapshot()`` 每次进程启动写一行，失败只告警不阻断启动；
  「快照没写」由守卫 ``OPS-CONFIG-SNAPSHOT-FRESH`` 兜底可见，不是静默失效。
"""
from __future__ import annotations

import importlib.util
import json
import os
import platform
import socket
from functools import lru_cache
from pathlib import Path

from sqlalchemy import text

from app.core.logging import logger

_PROFILE_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "ops" / "dump_fusion_profile.py"
)


@lru_cache(maxsize=1)
def _profile_module():
    spec = importlib.util.spec_from_file_location("_qhyc_fusion_profile", _PROFILE_SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"参数 profile 模块不可用：{_PROFILE_SCRIPT}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build_profile() -> dict:
    """返回与 dump_fusion_profile.py --json 相同结构的 profile。"""
    return _profile_module().build_profile()


def current_params_hash() -> str | None:
    """本进程生效的 signal_params_hash；解析失败返回 None（不编造、不静默）。"""
    try:
        return build_profile()["hashes"]["signal_params_hash"]
    except Exception as exc:  # noqa: BLE001 —— hash 不可用不能拖垮信号落库
        logger.warning(f"[config_snapshot] 生效参数 hash 解析失败：{exc}")
        return None


def record_config_snapshot(session, role: str | None = None) -> bool:
    """把当前进程的生效参数快照写入 ``ops.config_snapshot``。

    调用方须持有可提交的 session（``session_scope()`` 会在退出时 commit）。
    返回是否写入成功；任何异常只告警，不阻断进程启动。
    """
    try:
        p = build_profile()
        meta, h, layers = p["meta"], p["hashes"], p["layers"]
        params = {
            "role": role or os.getenv("ROLE") or "unknown",
            "app_env": meta["app_env"],
            "git_head": meta["repo_head"],
            "config_sha1": meta["config_sha1"],
            "signal_params_hash": h["signal_params_hash"],
            "full_params_hash": h["full_params_hash"],
            "profile_label": p["profile_label"],
            "effective": json.dumps(p["effective"], ensure_ascii=False, default=str),
            "layers": json.dumps(layers, ensure_ascii=False, default=str),
            "warnings": json.dumps(p["warnings"], ensure_ascii=False, default=str),
            "python": meta["python"],
            "pid": meta["pid"],
            "host": socket.gethostname(),
        }
        session.execute(text("""
            INSERT INTO ops.config_snapshot (
                role, app_env, git_head, config_sha1,
                signal_params_hash, full_params_hash, profile_label,
                effective, layers, warnings, python, pid, host)
            VALUES (
                :role, :app_env, :git_head, :config_sha1,
                :signal_params_hash, :full_params_hash, :profile_label,
                CAST(:effective AS jsonb), CAST(:layers AS jsonb),
                CAST(:warnings AS jsonb), :python, :pid, :host)
        """), params)
        logger.info(
            f"[config_snapshot] 启动快照已写 role={params['role']} "
            f"hash={h['signal_params_hash'][:12]}… label={p['profile_label']}")
        return True
    except Exception as exc:  # noqa: BLE001 —— 启动链路不能被旁路账本拖垮
        logger.warning(f"[config_snapshot] 启动快照写入失败（守卫会报 FAIL）：{exc}")
        return False


__all__ = ["build_profile", "current_params_hash", "record_config_snapshot"]
