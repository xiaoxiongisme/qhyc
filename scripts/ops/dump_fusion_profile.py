#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""逐项打印「实际生效」的 fusion 参数 profile（P2-1 / CL-20261108-07）。

为什么需要
----------
审计报告 §4④ 指出「三套参数口径并存」，并担心「回测报告的参数」和「生产在跑的
参数」不是同一套。此前没有机器可验证的答案，只能靠人翻代码。本脚本把覆盖链
**逐层展开**，给出每个字段的最终值与其来源，任何一层被改动都能立刻看出来。

覆盖链（按优先级从低到高，实证于 2026-11-08 生产）
----------------------------------------------------
  L1 代码默认   ``FusionConfig()``                       ← 生产 48/48 字段落在这里
  L2 YAML       ``config/cloud.yaml`` 的 ``fusion:`` 段  ← 生产该文件**没有** fusion 段
  L3 env        ``QH_FUSION_PROFILE`` / ``QH_FUSION_*``  ← 生产零覆盖
  L4 注册表     ``strategies.<key>.params``             ← 只作用于 scheduler 的扫描 cfg
  L5 DB         ``cfg_feature_switch``                  ← 全是功能开关，**不覆盖数值参数**

⚠ 「看起来能改、其实不生效」的层（2026-11-09 更新）
  · L4 已接线（CL-20261108-09）：`YamlConfig` 现在有 `strategies` 字段，
    `Settings.strategies` 把它 dump 成纯 dict 供 `registry.params_for()` 读。
    生产 `cloud.yaml` **仍无** `strategies` 段 → 覆盖为空 → 生效参数 = 代码默认。
    将来真写进去，`OPS-FUSION-PROFILE-PINNED` 会因 hash 偏离基线而 FAIL，
    必须登记台账并重钉 `PINNED_FUSION_SIGNAL_HASH`。
  · DB ``cfg_feature_switch`` 只控制作业/开关（含 portfolio_brake 阈值），
    不参与 fusion 参数合并。审计报告的「DB 叠加参数」这一层经实证不存在。

两个消费者
----------
  · ``get_settings().fusion``：回测 ``fusion_params_from_config``、取数
    ``fusion_signal.read_fusion_bars``（kline_src / min_bars）都用它；
  · ``app.scheduler._fusion_params(settings.fusion, "fusion_v34")``：主信号扫描
    ``evaluate_all`` 用它。默认不导入 scheduler（顶层会拉起 APScheduler/SQLAlchemy），
    需要实测两个消费者是否一致时加 ``--with-scheduler``。
  只要 L4 覆盖为空，两者必然一致；一旦有人在 L4 注入 ``kline_src`` / ``min_bars``
  之类字段，取数侧不会同步 → 本脚本会显式告警。

权限与边界
----------
  · 只读：不写库、不改配置、不发推送。
  · 脱敏：``pushplus_token`` / ``fallback_webhook`` 只显示 ``<set>`` / ``<empty>``，
    且不进入任何 hash 的原始值。
  · 运行态：本脚本按「当前磁盘 + 当前进程 env」解析。常驻服务用
    ``@lru_cache`` 缓存配置，进程早于配置文件修改时会持有旧值——这由
    P0-1 的 C4「运行态==磁盘」断言负责，不在本脚本范围内。

用法
----
  python scripts/ops/dump_fusion_profile.py                  # 人读表格
  python scripts/ops/dump_fusion_profile.py --json           # 机器读
  python scripts/ops/dump_fusion_profile.py --expect-signal-hash <sha256>
  python scripts/ops/dump_fusion_profile.py --with-scheduler # 实测两个消费者一致
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

def _find_repo_root() -> Path:
    """定位仓库根：正式路径是 scripts/ops/ 下两级；临时放到 /tmp 时回退到
    cwd / PYTHONPATH（生产宿主用 ``cd /Docker/qhyc`` 启动即可）。"""
    here = Path(__file__).resolve()
    candidates = [*here.parents, Path.cwd(),
                  *(Path(x) for x in sys.path if x)]
    for p in candidates:
        try:
            if (p / "app" / "core" / "config.py").is_file():
                return p
        except OSError:
            continue
    return Path.cwd()


REPO_ROOT = _find_repo_root()
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# ⚠ app 的导入**故意延迟到 build_profile() 内部**：app.core.logging 在 import
#   阶段会把 loguru 控制台 sink 挂到 sys.stdout 并打一行 INFO。若放在模块顶层，
#   守卫 import 本模块时会被污染输出；延迟后 CLI 可用 --json 把这段 stdout 捕获掉，
#   而守卫自己调用 build_profile() 时行为完全不变。

#: 影响信号 / 离场 / 仓位 / 取数的字段。只对这些求 signal_params_hash——
#: 改心跳间隔或推送窗口不该触发「策略参数漂移」告警。
SIGNAL_KEYS: tuple[str, ...] = (
    "enabled", "ema_k", "atr_n", "ma_n", "sl_atr", "trail_atr", "be_r",
    "W", "thr", "entry_mode", "cooldown_bars", "adx_n", "adx_min",
    "use_sbull", "fib_confl", "fib_ratios", "fib_tol_atr", "add_max_lots",
    "add_on_times", "add_thr_atr", "add_guard_atr", "min_bars",
    "entry_gate_profile", "stale_minutes", "max_bars", "closed_bars_only",
    "settle_delay_sec", "kline_src", "max_hold_days",
)

#: 运维 / 展示 / 推送字段（含已废弃但保留兼容的 hourly_src）。
OPS_KEYS: tuple[str, ...] = (
    "display_max_age_min", "hourly_src", "pushplus_token", "push_stop_levels",
    "heartbeat", "heartbeat_interval_min", "heartbeat_max_rows",
    "pre_session_times", "trail_push_atr", "signal_repeat_min", "push_log",
    "fallback_webhook", "push_windows",
)

SECRET_KEYS: frozenset[str] = frozenset({"pushplus_token", "fallback_webhook"})

ENV_GRANULAR: dict[str, str] = {
    "sl_atr": "QH_FUSION_SL_ATR",
    "trail_atr": "QH_FUSION_TRAIL_ATR",
    "be_r": "QH_FUSION_BE_R",
}
#: QH_FUSION_PROFILE=candidate 会覆盖的三个字段（与 app.core.config.FUSION_EXIT_CANDIDATE
#: 的键一致；写在此处是为了让模块顶层不依赖 app 的导入）。
ENV_PROFILE_FIELDS: frozenset[str] = frozenset({"sl_atr", "trail_atr", "be_r"})


def _redact(key: str, value) -> str:
    if key in SECRET_KEYS:
        return "<set>" if value else "<empty>"
    return value


def _norm(value):
    """稳定序列化：list/tuple→list，dict→按键排序，其它→原值或字符串。"""
    if isinstance(value, (list, tuple)):
        return [_norm(x) for x in value]
    if isinstance(value, dict):
        return {k: _norm(value[k]) for k in sorted(value)}
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def params_hash(cfg, keys: tuple[str, ...]) -> str:
    """对指定字段求 sha256。字段顺序、列表顺序、类型归一化均固定，跨进程可复现。"""
    payload = {k: _norm(_redact(k, getattr(cfg, k))) for k in keys}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _file_sha1(path: Path) -> str | None:
    try:
        return hashlib.sha1(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _git_head() -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _yaml_overrides(yaml_cfg, defaults) -> dict:
    return {k: getattr(yaml_cfg, k)
            for k in defaults.model_fields
            if getattr(yaml_cfg, k) != getattr(defaults, k)}


def build_profile(with_scheduler: bool = False) -> dict:
    """按当前磁盘 + 当前进程 env 解析覆盖链，返回结构化 profile。"""
    from app.core.config import (  # 延迟导入，见文件头说明
        CONFIG_DIR,
        FUSION_EXIT_CANDIDATE,
        FusionConfig,
        YamlConfig,
        get_settings,
        get_yaml_config,
    )
    from app.strategies.registry import params_for

    app_env = os.getenv("APP_ENV", "local").lower()
    cfg_name = "cloud.yaml" if app_env == "cloud" else "local.yaml"
    cfg_path = CONFIG_DIR / cfg_name

    settings = get_settings()
    effective = settings.fusion
    defaults = FusionConfig()
    yaml_cfg = get_yaml_config().fusion

    try:
        raw_yaml = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 —— 配置文件缺失/损坏也要能出报告
        raw_yaml = {}
    fusion_section_present = isinstance(raw_yaml, dict) and "fusion" in raw_yaml

    env_profile = (os.getenv("QH_FUSION_PROFILE") or "").strip()
    env_granular = {k: os.environ.get(v) for k, v in ENV_GRANULAR.items()}
    env_granular_active = {k: v for k, v in env_granular.items() if v not in (None, "")}
    profile_candidate = env_profile.lower() == "candidate"

    reg_overrides = dict(params_for("fusion_v34") or {})
    strategies_attr = hasattr(settings, "strategies")
    strategies_field = "strategies" in YamlConfig.model_fields

    warnings: list[str] = []
    if not fusion_section_present:
        warnings.append(
            f"{cfg_name} 无 fusion: 段 → L2 整层为空，参数全部落回代码默认（L1）")
    if not strategies_field or not strategies_attr:
        warnings.append(
            "Settings/YamlConfig 无 strategies 字段 → L4 config 覆盖层为死代码，"
            "写 strategies.<key>.params 不会生效（CL-20261108-09）")
    if reg_overrides:
        warnings.append(
            "L4 注册表覆盖非空 → 主信号扫描 cfg 与取数/回测 cfg 可能分叉，"
            "须用 --with-scheduler 实测")
    if profile_candidate:
        warnings.append("QH_FUSION_PROFILE=candidate 已启用，偏离回测定稿口径")
    if env_granular_active:
        warnings.append("存在 granular env 覆盖，偏离回测定稿口径")

    def layer_of(key: str) -> str:
        # 从最高优先级往回判；注意 L4 只作用于 scheduler，不体现在 effective 里。
        if key in reg_overrides:
            return "L4:registry(scheduler only)"
        if key in env_granular_active:
            return "L3:env_granular"
        if profile_candidate and key in ENV_PROFILE_FIELDS:
            return "L3:env_profile"
        if getattr(yaml_cfg, key) != getattr(defaults, key):
            return "L2:yaml"
        return "L1:code_default"

    fields = {}
    for key in effective.model_fields:
        value = getattr(effective, key)
        dfl = getattr(defaults, key)
        fields[key] = {
            "value": _redact(key, _norm(value)),
            "default": _redact(key, _norm(dfl)),
            "source": layer_of(key),
            "differs_from_default": value != dfl,
            "group": "signal" if key in SIGNAL_KEYS else "ops",
        }

    sig_hash = params_hash(effective, SIGNAL_KEYS)
    full_hash = params_hash(effective, SIGNAL_KEYS + OPS_KEYS)
    default_sig_hash = params_hash(defaults, SIGNAL_KEYS)

    sl, tr, be = effective.sl_atr, effective.trail_atr, effective.be_r
    if (sl, tr, be) == tuple(FUSION_EXIT_CANDIDATE[k] for k in ("sl_atr", "trail_atr", "be_r")):
        label = "candidate (2026-10-29 SL1.0/TR6.0/BE0.5, 未过回测闸门)"
    elif (sl, tr, be) == (2.0, 2.0, 0.5) and sig_hash == default_sig_hash:
        label = "V3.4 (2026-09-21 定稿 / 代码默认)"
    else:
        label = "CUSTOM / 未登记口径"

    consumers: dict = {
        "settings.fusion": "主回测参数 + 取数(kline_src/min_bars)",
        "scheduler._fusion_params": "未加载（加 --with-scheduler 实测）",
        "consistent": None,
    }
    if with_scheduler:
        try:
            from app.scheduler import _fusion_params
            sched_cfg = _fusion_params(effective, "fusion_v34")
            same = sched_cfg.model_dump() == effective.model_dump()
            consumers["scheduler._fusion_params"] = (
                "与 settings.fusion 一致" if same else "分叉：覆盖后字段不同")
            consumers["consistent"] = same
            if not same:
                warnings.append("两个消费者 cfg 不一致 → 回测/取数与主信号扫描口径分叉")
        except Exception as exc:  # noqa: BLE001
            consumers["scheduler._fusion_params"] = f"加载失败：{exc}"
            consumers["consistent"] = None
            warnings.append(f"scheduler 消费者加载失败：{exc}")

    return {
        "meta": {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "app_env": app_env,
            "config_file": str(cfg_path),
            "config_sha1": _file_sha1(cfg_path),
            "config_dir": str(CONFIG_DIR),
            "repo_head": _git_head(),
            "python": platform.python_version(),
            "pid": os.getpid(),
        },
        "layers": {
            "L1_code_default": {"active": True, "fields": len(defaults.model_fields)},
            "L2_yaml": {
                "file": cfg_name,
                "fusion_section_present": fusion_section_present,
                "overrides": _yaml_overrides(yaml_cfg, defaults),
            },
            "L3_env": {
                "QH_FUSION_PROFILE": env_profile or None,
                "granular": env_granular_active,
                "profile_candidate_active": profile_candidate,
            },
            "L4_registry": {
                "overrides": reg_overrides,
                "settings_has_strategies_attr": strategies_attr,
                "yaml_has_strategies_field": strategies_field,
                "config_layer_wired": strategies_attr and strategies_field,
            },
            "L5_db_switch": {
                "covers_fusion_numeric_params": False,
                "note": "cfg_feature_switch 只控制作业/功能开关，不参与参数合并",
            },
        },
        "profile_label": label,
        "equals_code_default": sig_hash == default_sig_hash,
        "hashes": {
            "signal_params_hash": sig_hash,
            "full_params_hash": full_hash,
            "code_default_signal_hash": default_sig_hash,
            "signal_key_count": len(SIGNAL_KEYS),
        },
        "consumers": consumers,
        "effective": fields,
        "warnings": warnings,
    }


def _print_human(p: dict) -> None:
    m, h = p["meta"], p["hashes"]
    print("=" * 100)
    print(f"fusion 实际生效 profile  |  {p['profile_label']}")
    print("=" * 100)
    print(f"APP_ENV={m['app_env']}  config={m['config_file']}")
    print(f"config_sha1={m['config_sha1']}  repo_head={m['repo_head']}  "
          f"python={m['python']}  pid={m['pid']}")
    print(f"generated_at={m['generated_at']}")
    print()
    L = p["layers"]
    print("[覆盖链]")
    print(f"  L1 代码默认      : {L['L1_code_default']['fields']} 字段")
    print(f"  L2 YAML fusion 段: {'存在' if L['L2_yaml']['fusion_section_present'] else '不存在'}  "
          f"覆盖项={L['L2_yaml']['overrides'] or '{}'}")
    print(f"  L3 env           : profile={L['L3_env']['QH_FUSION_PROFILE']!r}  "
          f"granular={L['L3_env']['granular'] or '{}'}")
    print(f"  L4 注册表        : 覆盖={L['L4_registry']['overrides'] or '{}'}  "
          f"config层已接线={L['L4_registry']['config_layer_wired']}")
    print(f"  L5 DB 开关       : 覆盖 fusion 数值参数={L['L5_db_switch']['covers_fusion_numeric_params']}")
    print()
    print(f"[hash]  signal_params_hash = {h['signal_params_hash']}  ({h['signal_key_count']} 字段)")
    print(f"        full_params_hash   = {h['full_params_hash']}")
    print(f"        equals_code_default= {p['equals_code_default']}")
    print()
    print(f"[消费者]  settings.fusion : {p['consumers']['settings.fusion']}")
    print(f"          scheduler     : {p['consumers']['scheduler._fusion_params']}")
    print()
    print("[逐项] 字段                     生效值                          来源                       组")
    print("-" * 100)
    for k, v in p["effective"].items():
        val = str(v["value"])
        if len(val) > 32:
            val = val[:29] + "..."
        mark = " *" if v["differs_from_default"] else "  "
        print(f" {k:26s} {val:33s} {v['source']:27s} {v['group']}{mark}")
    print("-" * 100)
    print(" * = 与代码默认值不同")
    if p["warnings"]:
        print()
        print("[告警]")
        for w in p["warnings"]:
            print(f"  - {w}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true", help="输出结构化 JSON")
    ap.add_argument("--with-scheduler", action="store_true",
                    help="导入 app.scheduler 实测两个消费者的 cfg 是否一致")
    ap.add_argument("--expect-signal-hash", default=None,
                    help="断言 signal_params_hash 等于该值，不等则退出码 1")
    a = ap.parse_args(argv)

    # --json 模式下把 build_profile() 期间（含 app 首次 import 打出的 INFO）的
    # stdout 全部捕获丢弃，保证 stdout 只有一段合法 JSON；loguru 的 console sink
    # 会绑定到捕获器，restore 后也不会再污染 stdout。
    if a.json:
        with contextlib.redirect_stdout(io.StringIO()):
            p = build_profile(with_scheduler=a.with_scheduler)
        print(json.dumps(p, ensure_ascii=False, indent=2, default=str))
    else:
        p = build_profile(with_scheduler=a.with_scheduler)
        _print_human(p)

    rc = 0
    if a.expect_signal_hash and p["hashes"]["signal_params_hash"] != a.expect_signal_hash:
        print(f"[FAIL] signal_params_hash 不匹配：期望 {a.expect_signal_hash}，"
              f"实测 {p['hashes']['signal_params_hash']}", file=sys.stderr)
        rc = 1
    if p["consumers"].get("consistent") is False:
        rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
