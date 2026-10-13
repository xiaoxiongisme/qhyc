"""ops_monitor 遗留代码 characterization 测试（Feathers《与遗留代码有效工作》法）。

目的：在给 collect() 引入 seam（sh_fn/psql_fn/system_fn 可注入）后，纯函数式
刻画并锁定 2026-10-08 P3 迁云修复后的**正确**行为——

  scheduler / pipeline 已改为 systemd 单元、不再是 Docker 容器；旧版 collect()
  把它们当容器 `docker inspect`，会持续误报「容器未运行」。

这些测试不连 docker / systemctl / psql，只验证 collect() 的判定逻辑，因而可在
任意环境下运行，作为该修复的回归护栏：若有人把 scheduler/pipeline 重新塞回
CONTAINERS（坏版本），test_scheduler_pipeline_systemd_no_false_container_alarm
会立刻变红。
"""
from __future__ import annotations

import importlib.util
import os

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "ops_monitor_under_test",
    os.path.join(_REPO, "scripts", "ops", "ops_monitor.py"),
)
ops_monitor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ops_monitor)


def _make_sh(table):
    """按命令特征分发伪造的子进程返回值（docker inspect / systemctl / curl）。"""
    def sh_fn(cmd, timeout=20):
        s = " ".join(cmd)
        if s.startswith("docker inspect"):
            for c in ("qhyc-timescaledb", "qhyc-api", "qhyc-scheduler", "qhyc-pipeline"):
                if c in s:
                    return table["docker:" + c]
        if s.startswith("systemctl is-active"):
            for u in ("qhyc-scheduler.service", "qhyc-pipeline.service"):
                if u in s:
                    return table["systemd:" + u]
        if "/health" in s:
            return table["curl"]
        return (0, "")
    return sh_fn


def _fake_psql(table):
    def psql_fn(sql, timeout=30):
        return table.get(sql, (False, ""))
    return psql_fn


def _healthy_system():
    return {"load5": 0.5, "cores": 4, "disk_pct": 50.0, "disk_free_gb": 100.0,
            "mem_pct": 40.0, "mem_total_gb": 8.0, "swap_pct": 0.0}


def _base_tables():
    sh_table = {
        "docker:qhyc-timescaledb": (0, "true"),
        "docker:qhyc-api": (0, "true"),
        "docker:qhyc-scheduler": (1, ""),       # 非容器：docker inspect 失败
        "docker:qhyc-pipeline": (1, ""),
        "systemd:qhyc-scheduler.service": (0, "active"),
        "systemd:qhyc-pipeline.service": (0, "active"),
        "curl": (0, "200"),
    }
    psql_table = {
        "SELECT 1": (True, ""),
        "SELECT max(trade_date)::text FROM daily_bar": (True, "2026-10-12"),
        "SELECT max(pushed_at)::text FROM fusion_push_log WHERE delivered": (True, "2026-10-13T01:00:00+00:00"),
    }
    return sh_table, psql_table


def test_scheduler_pipeline_systemd_no_false_container_alarm():
    """迁云后 scheduler/pipeline 不是容器：绝不触发「容器未运行」误报。"""
    sh_table, psql_table = _base_tables()
    data = ops_monitor.collect(
        sh_fn=_make_sh(sh_table),
        psql_fn=_fake_psql(psql_table),
        system_fn=_healthy_system,
    )
    crit_keys = [k for lvl, k, _ in data["issues"] if lvl == "crit"]
    assert "container" not in crit_keys, f"误报容器未运行: {data['issues']}"
    assert data["metrics"]["containers_down"] == []
    assert data["metrics"]["units_down"] == []          # systemd 单元均 active
    assert data["metrics"]["db_ok"] is True
    assert data["metrics"]["api_code"] == "200"
    # 数据/推送新鲜度正常，不应有 lag / push 静默告警
    assert "data_lag" not in [k for _, k, _ in data["issues"]]
    assert "push_silence" not in [k for _, k, _ in data["issues"]]


def test_real_container_down_raises_container_alarm():
    """真实容器宕机仍必须触发「容器未运行」（修复不能丢失真实告警）。"""
    sh_table, psql_table = _base_tables()
    sh_table["docker:qhyc-timescaledb"] = (1, "")      # 真·容器宕机
    data = ops_monitor.collect(
        sh_fn=_make_sh(sh_table),
        psql_fn=_fake_psql(psql_table),
        system_fn=_healthy_system,
    )
    container_issues = [d for lvl, k, d in data["issues"] if k == "container"]
    assert container_issues, "真实容器宕机必须触发「容器未运行」"
    assert "qhyc-timescaledb" in container_issues[0]


def test_systemd_unit_down_raises_service_alarm():
    """systemd 单元宕机必须触发「systemd 服务未运行」（迁云后的关键覆盖）。"""
    sh_table, psql_table = _base_tables()
    sh_table["systemd:qhyc-scheduler.service"] = (1, "inactive")  # 真·systemd 宕机
    data = ops_monitor.collect(
        sh_fn=_make_sh(sh_table),
        psql_fn=_fake_psql(psql_table),
        system_fn=_healthy_system,
    )
    service_issues = [d for lvl, k, d in data["issues"] if k == "service"]
    assert service_issues, "systemd 单元宕机必须触发「systemd 服务未运行」"
    assert "qhyc-scheduler.service" in service_issues[0]


def test_db_unreachable_raises_db_alarm():
    """DB 不可查询必须触发 db 告警。"""
    sh_table, psql_table = _base_tables()
    psql_table["SELECT 1"] = (False, "connection refused")
    psql_table["SELECT max(trade_date)::text FROM daily_bar"] = (False, "")
    psql_table["SELECT max(pushed_at)::text FROM fusion_push_log WHERE delivered"] = (False, "")
    data = ops_monitor.collect(
        sh_fn=_make_sh(sh_table),
        psql_fn=_fake_psql(psql_table),
        system_fn=_healthy_system,
    )
    db_issues = [d for lvl, k, d in data["issues"] if k == "db"]
    assert db_issues, "DB 不可查询必须触发 db 告警"
