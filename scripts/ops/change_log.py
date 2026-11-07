#!/usr/bin/env python
"""改造台账 CLI（ops.change_log 的唯一操作入口）。

设计意图
========
「改造」不是一次 commit，而是一个有生命周期的对象：
    proposed → approved → in_progress → implemented（或 deferred / rejected / rolled_back）

只有把生命周期**显式记录**，才不会再出现「方案讨论过、表落了库、下游没做、
半年后没人记得」这种状态。本 CLI 就是让它有成本足够低的操作路径。

与守卫的联动
============
`invariant_keys` 声明该改造影响哪些断言。状态处于未完成集合时，
guard_invariants.py 把这些断言的失败降级为 WAIVED（已知缺口）；
改成 implemented 后豁免自动消失——**这就是"不许假装还清"的机制**。

常用
====
    python scripts/ops/change_log.py list                 # 未完成/暂缓清单
    python scripts/ops/change_log.py list --all
    python scripts/ops/change_log.py show CL-20261107-01
    python scripts/ops/change_log.py close CL-20261107-01 \
        --commit 5f83b9e --verified --note "演练 11/11 通过"
    python scripts/ops/change_log.py seed                 # 写入历史台账（幂等）
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqlalchemy import text  # noqa: E402

from app.core.db import session_scope  # noqa: E402

OPEN = ("proposed", "approved", "in_progress", "deferred")


def _next_id(s, day: str | None = None) -> str:
    day = day or f"{date.today():%Y%m%d}"
    n = s.execute(text(
        "SELECT count(*) FROM ops.change_log WHERE change_id LIKE :p"
    ), {"p": f"CL-{day}-%"}).scalar()
    return f"CL-{day}-{n + 1:02d}"


def cmd_list(args) -> int:
    sql = """
        SELECT change_id, status, risk, category, title,
               coalesce(invariant_keys, ''), coalesce(decision_note, ''),
               (CURRENT_DATE - created_at::date) AS age
        FROM ops.change_log
        {where}
        ORDER BY CASE risk WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
                 created_at DESC
    """
    where = "" if args.all else f"WHERE status IN ({','.join(chr(39) + s + chr(39) for s in OPEN)})"
    with session_scope() as s:
        rows = s.execute(text(sql.format(where=where))).fetchall()
    if not rows:
        print("（无记录）")
        return 0
    print(f"{'ID':<16} {'状态':<12} {'风险':<7} {'类别':<8} {'龄':>4}  标题")
    print("-" * 110)
    for cid, st, risk, cat, title, keys, note, age in rows:
        print(f"{cid:<16} {st:<12} {risk:<7} {cat:<8} {age:>3}d  {title[:56]}")
        if keys:
            print(f"{'':<16} └ 不变量：{keys}")
        if note and st in ("deferred", "rejected"):
            print(f"{'':<16} └ 原因：{note[:70]}")
    print(f"\n共 {len(rows)} 条")
    return 0


def cmd_show(args) -> int:
    with session_scope() as s:
        r = s.execute(text("SELECT * FROM ops.change_log WHERE change_id = :i"),
                      {"i": args.change_id}).mappings().first()
    if not r:
        print(f"未找到 {args.change_id}")
        return 1
    for k, v in r.items():
        if k == "items" and v:
            v = json.dumps(v, ensure_ascii=False)
        print(f"{k:<16}: {v}")
    return 0


def cmd_add(args) -> int:
    with session_scope() as s:
        cid = args.change_id or _next_id(s)
        # verified_at 必须一起写：ck_change_implemented_evidence 要求
        # implemented ⇒ (commit_sha 或 migration_file) AND verified_at 非空。
        # 原来的 INSERT 漏了这列，于是「登记一条已完成的改造」直接撞 CHECK 失败。
        s.execute(text("""
            INSERT INTO ops.change_log
                (change_id, title, category, status, risk, rationale, change_summary,
                 impact_scope, invariant_keys, decision_note, commit_sha, migration_file,
                 rollback, archive_ref, verified_at)
            VALUES (:cid, :title, :cat, :st, :risk, :why, :sum, :scope, :keys, :note,
                    :commit, :mig, :rb, :ref,
                    CASE WHEN :st = 'implemented' THEN now() ELSE NULL END)
        """), {"cid": cid, "title": args.title, "cat": args.category, "st": args.status,
               "risk": args.risk, "why": args.rationale, "sum": args.summary,
               "scope": args.scope, "keys": args.invariant_keys, "note": args.note,
               "commit": args.commit, "mig": args.migration, "rb": args.rollback,
               "ref": args.archive_ref})
        s.commit()
    print(f"已登记 {cid}")
    return 0


def cmd_update(args) -> int:
    sets, params = [], {"cid": args.change_id}
    for f in ("title", "status", "risk", "category", "rationale", "change_summary",
              "impact_scope", "invariant_keys", "decision_note", "commit_sha",
              "migration_file", "rollback", "archive_ref"):
        v = getattr(args, f, None)
        if v is not None:
            sets.append(f"{f} = :{f}")
            params[f] = v
    if args.verified:
        sets.append("verified_at = now()")
    if args.clear_verified:
        sets.append("verified_at = NULL")
    if not sets and not args.items_json:
        print("没有要更新的字段")
        return 1
    sets.append("updated_at = now()")

    with session_scope() as s:
        if args.items_json:
            # items 是分阶段进度（S1/S2/...），按 key **合并**而非整体覆盖：
            # 收口过程中会逐个完成子项，整体覆盖容易把前面已记的进度冲掉。
            cur = s.execute(text(
                "SELECT items FROM ops.change_log WHERE change_id = :cid"),
                {"cid": args.change_id}).scalar()
            merged = dict(cur or {})
            try:
                merged.update(json.loads(args.items_json))
            except json.JSONDecodeError as exc:
                print(f"--items-json 不是合法 JSON：{exc}")
                return 1
            sets.append("items = :items")
            params["items"] = json.dumps(merged, ensure_ascii=False)
        n = s.execute(text(
            f"UPDATE ops.change_log SET {', '.join(sets)} WHERE change_id = :cid"
        ), params).rowcount
        s.commit()
    print(f"{'已更新' if n else '未找到'} {args.change_id}")
    return 0 if n else 1


def cmd_close(args) -> int:
    with session_scope() as s:
        n = s.execute(text("""
            UPDATE ops.change_log
               SET status = 'implemented',
                   commit_sha = coalesce(:commit, commit_sha),
                   migration_file = coalesce(:mig, migration_file),
                   change_summary = coalesce(:note, change_summary),
                   verified_at = CASE WHEN :verified THEN now() ELSE verified_at END,
                   updated_at = now()
             WHERE change_id = :cid
        """), {"cid": args.change_id, "commit": args.commit,
               "mig": args.migration, "note": args.note,
               "verified": args.verified}).rowcount
        s.commit()
    if not n:
        print(f"未找到 {args.change_id}")
        return 1
    print(f"已收口 {args.change_id}（相关不变量豁免已失效，须真通过）")
    return 0


# ------------------------------------------------------------------ 历史台账
# 2026-11-07 审计 + 当日修复过程中确认的改造事实。幂等：已存在则跳过。
SEED = [
    dict(
        change_id="CL-20261107-01",
        title="数据层交易白名单下游过滤（方案 v3 / D2）",
        category="data", status="deferred", risk="high",
        rationale="归档 2026-11-07 数据层收官：l3_ref.tradable_variety 已落库 38 品种，"
                  "但 L1/L2/信号池/兼容视图的下游过滤从未实施（归档明文写 D2 属"
                  "『待拍板』，且方案 v3 二审发现 7 个承重问题未解）。实测四套口径并存："
                  "白名单 38 / cloud.yaml 50 / L1 主连 81 / 融合信号 41（其中 12 个非白名单）。",
        change_summary="仅完成：白名单表建表 + 38 行落库。未完成：L1/L2 过滤、"
                       "配置对齐、信号池收口、7 个 public 兼容视图过滤、L2 存储过程侧 fail-open。",
        impact_scope="l3_ref.tradable_variety / l1_mkt / l2_adj / config/cloud.yaml / "
                     "fusion_signal 扫描池 / public 兼容视图",
        invariant_keys="VAR-SIGNAL-IN-WHITELIST,VAR-CONFIG-IN-WHITELIST,"
                       "VAR-WHITELIST-IN-CONFIG,VAR-L1-IN-WHITELIST",
        decision_note="D2 待拍板未落地；方案 v3 二审未通过（兼容视图无过滤、"
                      "总开关管不到 L2 SP、单 bool 撑不起灰度）。不得基于当前状态做"
                      "『白名单已生效』的判断。",
        archive_ref=r"D:\WorkBuddy对话归档\2026\11\07\193903-qhyc数据层改造与清理收官.md",
        rollback="表删除即回退；无代码引用，无下游副作用。",
        items={"D1": "A 只导出不删", "D2": "待落地", "D7": "待定"},
    ),
    dict(
        change_id="CL-20261107-02",
        title="组合熔断权益流写入方（P0-1）",
        category="code", status="implemented", risk="high",
        rationale="portfolio_equity 自 migrations/004 建表以来 0 行——全库只有读取方 "
                  "load_equity_curve，写入方从未实现，组合回撤熔断（>15% 半仓 / >25% 清仓）"
                  "恒放行。",
        change_summary="新增 scripts/ops/upsert_equity.py（每日权益 UPSERT + 即时回显）与 "
                       "scripts/ops/drill_portfolio_brake.py（事务内演练，零残留零推送）；"
                       "cfg_feature_switch.portfolio_brake_enabled 置 t + 显式阈值 15/25/250。",
        impact_scope="ops/portfolio_equity / cfg_feature_switch / portfolio_brake / 执行层手数缩放",
        invariant_keys="DATA-EQUITY-FRESH",
        commit_sha="5f83b9e", verified_at_note="演练 11/11 PASS（16%→0.5、26%→0、"
                                              "样本不足放行、口径异常不误杀）",
        rollback="把 portfolio_brake_enabled 置 f 即回到旧行为；脚本为新增，不删不影响链路。",
        archive_ref="E:\\抖音分析\\2026-11-07-19-50-36\\.workbuddy\\memory\\2026-11-07.md",
    ),
    dict(
        change_id="CL-20261107-03",
        title="周度作业 cron 星期语义错位（周六被解析成周日）",
        category="ops", status="implemented", risk="high",
        rationale="ops.scheduled_task 三作业 schedule 为 `* * 6`，apscheduler "
                  "CronTrigger.from_crontab 里 6=周日（0=周一），与代码 day_of_week='sat' "
                  "相反。实测 10-18/10-25/11-01 三条成功记录全是周日，11-07 周六零记录。",
        change_summary="DB 三条 schedule 改 `* * 5`（=周六）；代码注册处补语义陷阱注释；"
                       "新增守卫 OPS-WEEKLY-DOW 防复发。",
        impact_scope="ops.scheduled_task / lstm_weekly_retrain / transmission_weekly_recalc / "
                     "backtest_weekly",
        invariant_keys="OPS-WEEKLY-DOW",
        commit_sha="8510e85",
        rollback="改回 `* * 6` 即恢复旧（错误）行为。",
    ),
    dict(
        change_id="CL-20261107-04",
        title="日检重开 + 双开关打架修正 + 补 layer 新鲜度盲区",
        category="code", status="implemented", risk="high",
        rationale="data_selfcheck_enabled=t 但 ops.scheduled_task.data_selfcheck_daily.enabled=f，"
                  "后者把作业移除 → 日检从未执行。且 check_missing 只遍历当日有数据的 symbol，"
                  "某层彻底断更时不在结果集里，永远查不到。",
        change_summary="字典置 t；check_missing 期望值改按品种近 20 日中位数、非交易日跳过；"
                       "新增 check_layer_freshness（窗口化查询，避免全表聚合 OOM）。",
        impact_scope="app/ingest/data_selfcheck.py / app/scheduler.py / ops.scheduled_task / "
                     "cfg_feature_switch",
        invariant_keys="OPS-SWITCH-JOB-CONSIST,DATA-L0-LAYERS-FRESH",
        commit_sha="448fa6b",
        rollback="字典 enabled 置 f 即恢复停用；新增检查项可单独注释。",
    ),
    dict(
        change_id="CL-20261107-05",
        title="L0 layer=index（XXXX8888）断更 6 个交易日",
        category="data", status="deferred", risk="medium",
        rationale="8888 指数连供 L2 换月检测 ratio 门使用，自 2026-10-30 15:00 停更；"
                  "源 local_csv_synth_8888 止于 10-30，增量 akshare 只采 *888，合成步骤"
                  "从未接入增量链路。日检新增 layer 新鲜度后已能被自动发现。",
        change_summary="未实施。影响面已量化：不进融合信号（BA 只处理 ^[A-Za-z]+888$，"
                       "fusion_kline.py:17），但会让换月检测与换月断层判据缺参照。",
        impact_scope="l0_raw.minute_bar(layer=index) / l1_mkt.bar_15m / build_roll_segments / "
                     "l2_adj.roll_segment",
        invariant_keys="DATA-L0-LAYERS-FRESH",
        decision_note="补 8888 需 back-adjust 状态续算（front/carry），不是简单聚合；"
                      "归档明令不得为已知缺口匆忙造数据。待评估续算方案后实施。",
        rollback="不适用（未改动）。",
    ),
    dict(
        change_id="CL-20261107-06",
        title="11-06 采集中断 5 小时（cron_fetch_1min 曾被注释停用）",
        category="ops", status="in_progress", risk="medium",
        rationale="11-04 起 cron_fetch_1min 被注释停用（『L0 切换停用』），11-06 16:37 由 "
                  "restore_cron_1min.py 恢复，中间约 5 小时无采集 → 11-06 15:00 桶仅 50/80 "
                  "品种、夜盘 42/55，日检首跑报 215 张缺失工单（真实缺口，非误报）。",
        change_summary="cron 已恢复（每 15 分钟，session gate 正常）。缺口数据本身尚未回补。",
        impact_scope="l0_raw.minute_bar / l1_mkt.bar_60m（2026-11-06）",
        invariant_keys="",
        decision_note="待定：回补 11-06 下午+夜盘，或在新数据上重跑回测时接受该日为不完整日。",
        rollback="不适用。",
    ),
]


def cmd_seed(args) -> int:
    added = skipped = 0
    with session_scope() as s:
        for row in SEED:
            exists = s.execute(text(
                "SELECT 1 FROM ops.change_log WHERE change_id = :i"
            ), {"i": row["change_id"]}).first()
            if exists:
                skipped += 1
                continue
            d = dict(row)
            # implemented 条目需要 verified_at 满足 CHECK 约束
            s.execute(text("""
                INSERT INTO ops.change_log
                    (change_id, title, category, status, risk, rationale, change_summary,
                     impact_scope, invariant_keys, decision_note, commit_sha, verified_at,
                     rollback, archive_ref, items)
                VALUES (:change_id, :title, :category, :status, :risk, :rationale,
                        :change_summary, :impact_scope, :invariant_keys, :decision_note,
                        :commit_sha,
                        CASE WHEN :status = 'implemented' THEN now() ELSE NULL END,
                        :rollback, :archive_ref, :items)
            """), {
                "change_id": d["change_id"], "title": d["title"], "category": d["category"],
                "status": d["status"], "risk": d["risk"],
                "rationale": d.get("rationale"), "change_summary": d.get("change_summary"),
                "impact_scope": d.get("impact_scope"),
                "invariant_keys": d.get("invariant_keys"),
                "decision_note": d.get("decision_note"),
                "commit_sha": d.get("commit_sha"), "rollback": d.get("rollback"),
                "archive_ref": d.get("archive_ref"),
                "items": json.dumps(d["items"], ensure_ascii=False) if d.get("items") else None,
            })
            added += 1
        s.commit()
    print(f"台账 seed：新增 {added}，跳过已存在 {skipped}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="qhyc 改造台账")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="列出改造")
    p.add_argument("--all", action="store_true", help="含已完成/已否决")
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", help="查看单条")
    p.add_argument("change_id")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("add", help="登记新改造")
    p.add_argument("--change-id")
    p.add_argument("--title", required=True)
    p.add_argument("--category", required=True,
                   choices=["code", "data", "config", "param", "schema", "ops", "research"])
    p.add_argument("--status", default="proposed",
                   choices=["proposed", "approved", "in_progress", "deferred",
                            "implemented", "rolled_back", "rejected"])
    p.add_argument("--risk", default="medium", choices=["low", "medium", "high"])
    p.add_argument("--rationale", help="为什么要做（须有实证或决策依据）")
    p.add_argument("--summary", help="改了什么")
    p.add_argument("--scope", help="影响面")
    p.add_argument("--invariant-keys", help="逗号分隔的断言 key")
    p.add_argument("--note", help="未完成原因/拍板说明")
    p.add_argument("--commit")
    p.add_argument("--migration")
    p.add_argument("--rollback")
    p.add_argument("--archive-ref")
    p.set_defaults(fn=cmd_add)

    p = sub.add_parser("update", help="更新字段")
    p.add_argument("change_id")
    for f in ("title", "status", "risk", "category", "rationale", "change_summary",
              "impact_scope", "invariant_keys", "decision_note", "commit_sha",
              "migration_file", "rollback", "archive_ref"):
        p.add_argument("--" + f.replace("_", "-"), dest=f)
    p.add_argument("--verified", action="store_true")
    p.add_argument("--clear-verified", action="store_true")
    p.add_argument("--items-json", dest="items_json",
                   help='按 key 合并 items，如 \'{"S2":"done ..."}\'')
    p.set_defaults(fn=cmd_update)

    p = sub.add_parser("close", help="收口（置 implemented + 验收时间）")
    p.add_argument("change_id")
    p.add_argument("--commit")
    p.add_argument("--migration")
    p.add_argument("--note")
    p.add_argument("--verified", action="store_true")
    p.set_defaults(fn=cmd_close)

    p = sub.add_parser("seed", help="写入 2026-11-07 历史台账（幂等）")
    p.set_defaults(fn=cmd_seed)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
