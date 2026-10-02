# -*- coding: utf-8 -*-
"""上线准入 SOP 校验脚本（deploy_gate）。

部署/发布前一键跑，把"模块存在、路由注册、关键表/列、数据覆盖度、运维开关、
前端产物、鉴权状态"固化成一道准入闸。任意 BLOCK 项 → 退出码 1（CI 拦截）；
仅 WARN → 退出码 0 但打印告警；全 PASS → 0。

用法（容器内）：
    docker exec -w /app -e PYTHONPATH=/app qhyc-api python scripts/deploy_gate.py

可选环境变量：
    DEPLOY_GATE_JSON=/path/to/out.json   # 落盘结构化报告
    REQUIRE_AUTH=1                        # 鉴权关闭视为 BLOCK（默认仅 WARN）
    GATE_HTTP=1                           # 额外做 HTTP 探测（/assets、/symbols）；默认开
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

from sqlalchemy import text

from app.core.db import session_scope

BAR = "=" * 72
REQUIRE_AUTH = os.getenv("REQUIRE_AUTH") == "1"
DO_HTTP = os.getenv("GATE_HTTP", "1") == "1"
API_BASE = "http://127.0.0.1:8000"

# 连续主力品种（用于数据覆盖度抽检；与 futures_symbol.is_main 对齐更稳，见下）
CONTINUOUS_SAMPLE = ["RB888", "FU888", "I888", "TA888", "MA888"]


def _http(path: str, timeout: int = 5):
    try:
        req = urllib.request.Request(API_BASE + path)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, len(r.read())
    except urllib.error.HTTPError as e:
        return e.code, 0
    except Exception as e:  # noqa: BLE001
        return -1, str(e)[:60]


def main() -> int:
    findings: list[dict] = []

    def add(level, key, msg):
        findings.append({"level": level, "key": key, "msg": msg})
        tag = {"PASS": "✅", "WARN": "🟡", "BLOCK": "❌"}.get(level, level)
        print(f"  [{tag}] {key}: {msg}")

    print(BAR)
    print("上线准入 SOP 校验（deploy_gate）")
    print(BAR)

    # ---------- 1. 执行反解层模块 ----------
    print("\n[1] P0-1 执行反解层模块")
    try:
        import app.execution as ex  # noqa: F401
        assert hasattr(ex, "resolve_signal_to_order")
        add("PASS", "execution.module", "app/execution 可导入且含 resolve_signal_to_order")
    except Exception as e:  # noqa: BLE001
        add("BLOCK", "execution.module", f"无法导入 app.execution: {e}")

    # 路由注册
    try:
        from app.api import api_router
        paths = [getattr(r, "path", "") for r in api_router.routes]
        hit = any(p.startswith("/execution") for p in paths)
        add("PASS" if hit else "BLOCK", "execution.route",
            "/execution 已在 api_router 注册" if hit else "/execution 未注册到 api_router")
    except Exception as e:  # noqa: BLE001
        add("WARN", "execution.route", f"无法校验路由注册: {e}")

    # 口径校准（WARN，非阻断）：确认 888 信号价空间假设（raw/adj；见 PRD §3.1/§9）
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "calibrate_price_space",
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibrate_price_space.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        from datetime import date as _d, timedelta as _td
        with session_scope() as s2:
            res = mod.calibrate(s2, ["RB888", "AG888", "AU888"], _d.today() - _td(days=1))
        verdicts = [r[5] for r in res if r[5] in ("raw", "adj")]
        rec = "raw" if verdicts.count("raw") >= len(verdicts) / 2 else "adj"
        add("PASS" if verdicts else "WARN", "execution.calibration",
            f"价空间裁决={rec}（样本 {verdicts}）；EXECUTION_PRICE_SPACE 默认 raw")
    except Exception as e:  # noqa: BLE001
        add("WARN", "execution.calibration", f"口径校准未运行: {e}")

    # ---------- 2. 关键表/列 ----------
    print("\n[2] 关键表与列（PRD §6 / P0-1 数据依赖）")
    with session_scope() as s:
        def tbl_exists(t):
            return s.execute(text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name=:t LIMIT 1"), {"t": t}).first() is not None

        def col_exists(t, c):
            return s.execute(text(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_name=:t AND column_name=:c LIMIT 1"),
                {"t": t, "c": c}).first() is not None

        for t in ("futures_symbol", "main_contract_map", "roll_segment"):
            add("PASS" if tbl_exists(t) else "BLOCK", f"table.{t}",
                f"{t} 存在" if tbl_exists(t) else f"{t} 缺失")

        add("PASS" if col_exists("futures_symbol", "multiplier") else "BLOCK",
            "col.futures_symbol.multiplier", "")
        # price_tick 为 P0-1 新增列：缺失=WARN（待 DDL 增补），非阻断
        if col_exists("futures_symbol", "price_tick"):
            add("PASS", "col.futures_symbol.price_tick", "已存在")
        else:
            add("WARN", "col.futures_symbol.price_tick",
                "缺失（P0-1 DDL 待增补；tick 对齐校验将跳过）")

    # ---------- 3. 数据覆盖度 ----------
    print("\n[3] 数据覆盖度（反解前置条件）")
    with session_scope() as s:
        from datetime import date, timedelta
        today = date.today()
        cutoff = today - timedelta(days=7)
        # main_contract_map 最近覆盖
        n_map = s.execute(text(
            "SELECT count(DISTINCT product) FROM main_contract_map "
            "WHERE trade_date >= :c"), {"c": cutoff}).scalar() or 0
        add("PASS" if n_map >= 30 else "WARN", "coverage.main_contract_map",
            f"近 7 天有主力映射的品种数 = {n_map}")
        # roll_segment 行数
        n_seg = s.execute(text("SELECT count(*) FROM roll_segment")).scalar() or 0
        add("PASS" if n_seg > 0 else "BLOCK", "coverage.roll_segment",
            f"roll_segment 行数 = {n_seg}")
        # multiplier 非空覆盖度
        tot = s.execute(text("SELECT count(*) FROM futures_symbol "
                              "WHERE is_main IS TRUE OR symbol LIKE '%888'")).scalar() or 0
        ok_m = s.execute(text("SELECT count(*) FROM futures_symbol "
                              "WHERE (is_main IS TRUE OR symbol LIKE '%888') "
                              "AND multiplier IS NOT NULL")).scalar() or 0
        cov = (ok_m / tot) if tot else 0.0
        add("PASS" if cov >= 0.95 else "WARN", "coverage.multiplier",
            f"主力/连续品种 multiplier 非空覆盖 = {ok_m}/{tot} ({cov:.0%})")
        # price_tick 覆盖（P0-1 新增，缺失仅 WARN；列不存在时降级不阻断）
        if col_exists("futures_symbol", "price_tick"):
            ok_t = s.execute(text("SELECT count(*) FROM futures_symbol "
                                  "WHERE (is_main IS TRUE OR symbol LIKE '%888') "
                                  "AND price_tick IS NOT NULL")).scalar() or 0
            add("PASS" if ok_t == tot else "WARN", "coverage.price_tick",
                f"price_tick 非空 = {ok_t}/{tot}（缺失不影响导入，仅 tick 校验跳过）")
        else:
            add("WARN", "coverage.price_tick",
                f"price_tick 列缺失（共 {tot} 个主力/连续品种）；先应用迁移 012")

    # ---------- 4. 运维开关（反映文档刷新结论） ----------
    print("\n[4] 运维开关状态")
    backup_on = os.getenv("BACKUP_ENABLED") in ("1", "true", "True")
    add("PASS" if backup_on else "WARN", "switch.backup",
        f"BACKUP_ENABLED={'on' if backup_on else 'off（建议置 1）'}")
    brake_on = os.getenv("PORTFOLIO_BRAKE_ENABLED") in ("1", "true", "True")
    add("PASS" if brake_on else "WARN", "switch.portfolio_brake",
        f"PORTFOLIO_BRAKE_ENABLED={'on' if brake_on else 'off（P0-2 已实现，按需启用）'}")

    # ---------- 5. 前端产物（§10） ----------
    print("\n[5] 看板前端产物（§10）")
    web_dist = "/app/web/dist"
    if os.path.isdir(web_dist):
        add("PASS", "frontend.web_dist", f"{web_dist} 存在")
    else:
        add("WARN", "frontend.web_dist",
            f"{web_dist} 不存在（§10 看板前端未构建部署；/assets 将 404）")

    # ---------- 6. HTTP 探测（鉴权 / 前端） ----------
    if DO_HTTP:
        print("\n[6] HTTP 探测（localhost:8000）")
        code, _ = _http("/assets/")
        add("PASS" if code == 200 else "WARN", "http.assets",
            f"GET /assets/ -> {code}（200=前端已挂载，404=未构建）")
        sc, _ = _http("/symbols?limit=1")
        if sc in (401, 403):
            add("PASS", "http.auth", f"GET /symbols -> {sc}（鉴权已启用）")
        elif sc == 200:
            lv = "BLOCK" if REQUIRE_AUTH else "WARN"
            add(lv, "http.auth",
                "GET /symbols -> 200（鉴权未启用）" +
                ("【REQUIRE_AUTH=1 视为阻断】" if REQUIRE_AUTH else "（上云前须启用）"))
        else:
            add("WARN", "http.auth", f"GET /symbols -> {sc}（无法判定鉴权态）")

    # ---------- 汇总 ----------
    print("\n" + BAR)
    n_block = sum(1 for f in findings if f["level"] == "BLOCK")
    n_warn = sum(1 for f in findings if f["level"] == "WARN")
    n_pass = sum(1 for f in findings if f["level"] == "PASS")
    print(f"汇总：PASS={n_pass}  WARN={n_warn}  BLOCK={n_block}")
    print(BAR)

    out_path = os.getenv("DEPLOY_GATE_JSON")
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fo:
            json.dump({"pass": n_pass, "warn": n_warn, "block": n_block,
                       "findings": findings}, fo, ensure_ascii=False, indent=2)
        print(f"已落盘：{out_path}")

    return 1 if n_block > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
