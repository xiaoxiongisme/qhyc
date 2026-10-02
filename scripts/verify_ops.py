# -*- coding: utf-8 -*-
"""运维层 P1 验收脚本：P1-1 备份 + P1-2 告警巡检。

用法（本地容器内）：
    docker exec -w /app -e PYTHONPATH=/app qhyc-api python scripts/verify_ops.py

可选环境变量：
    BACKUP_TABLES=dim_variety,factor_registry   # 缩小备份范围做快速验证
    BACKUP_REMOTE_DIR=/app/runtime/backup_remote  # 验证异地副本
    ALERT_NOTIFY=0                               # 只巡检不推送（默认本机无 token 也不会真发）
"""
from __future__ import annotations

import json
import os

from app.core.db import session_scope
from app.ops.alerting import alert_config_from_env, check_all
from app.ops.backup import backup_config_from_env, run_backup, verify_backup

BAR = "=" * 72


def _fmt(title):
    print("")
    print(BAR)
    print(title)
    print(BAR)


def main():
    bcfg = backup_config_from_env()
    acfg = alert_config_from_env()

    _fmt("P1-1 备份配置")
    print("enabled      : {0}".format(bcfg.enabled))
    print("dir          : {0}".format(bcfg.dir))
    print("remote_dir   : {0}".format(bcfg.remote_dir or "(未配置，不异地)"))
    print("retain_days  : {0}".format(bcfg.retain_days))
    print("tables       : {0}".format(",".join(bcfg.tables)))

    with session_scope() as s:
        rep = run_backup(session=s, cfg=bcfg)
    _fmt("P1-1 备份执行结果")
    print("ok           : {0}".format(rep.get("ok")))
    print("目录         : {0}".format(rep.get("dir")))
    print("总行数       : {0}".format(rep.get("total_rows")))
    print("压缩后字节   : {0}".format(rep.get("total_bytes")))
    print("耗时(s)      : {0}".format(rep.get("seconds")))
    print("异地副本     : {0}".format(json.dumps(rep.get("remote"), ensure_ascii=False)))
    print("保留策略     : {0}".format(json.dumps(rep.get("retention"), ensure_ascii=False)))
    for t in rep.get("tables", []):
        print("  - {0:<24} {1:>8} 行  {2:>10} 字节  {3}s".format(
            t["table"], t["rows"], t["compressed_bytes"], t["seconds"]))
    for f in rep.get("failed", []):
        print("  ! {0} 失败: {1}".format(f["table"], f["error"]))

    v = verify_backup(rep.get("dir"))
    _fmt("P1-1 备份校验（文件非空 + manifest 一致）")
    print("ok           : {0}".format(v.get("ok")))
    if v.get("issues"):
        for i in v["issues"]:
            print("  ! {0}".format(i))
    print("校验表数     : {0}".format(len(v.get("tables", []))))

    _fmt("P1-2 告警配置")
    print("enabled      : {0}".format(acfg.enabled))
    print("interval_min : {0}".format(acfg.interval_min))
    print("阈值         : mem>={0}% disk>={1}% load>={2} lag>{3}d silence>{4}h".format(
        acfg.mem_pct, acfg.disk_pct, acfg.load_ratio, acfg.data_lag_days,
        acfg.push_silence_hours))
    print("推送         : {0}".format(acfg.notify))

    with session_scope() as s:
        arep = check_all(session=s, cfg=acfg)
    _fmt("P1-2 告警巡检结果")
    print("summary      : {0}".format(arep.get("summary")))
    for f in arep.get("findings", []):
        print("  [{0}] {1}: {2}".format(f["level"], f["key"], f["msg"]))
    if not arep.get("findings"):
        print("  无异常")

    out_path = os.getenv("OPS_VERIFY_JSON")
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fo:
            json.dump({"backup": rep, "verify": v, "alert": arep},
                      fo, ensure_ascii=False, indent=2, default=str)
        print("\n已落盘：{0}".format(out_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
