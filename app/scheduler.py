"""
APScheduler 调度（M1，§4.2）
- 日线每日三档（决策 7）：早盘前 08:00 / 午盘时 12:30 / 夜盘前 20:00
- 小时线每小时自动更新（决策 7）：整点触发一次全品种小时线增量采集
- 龙虎榜（新浪）每日 17:30 入库；库存（周频周五）与基差（日频）自动调度（决策 2）
- 触发后：
  1. 采集 + 入库（akshare 单一源；天勤清理后不再有双源校准/比对）
  2. 入库完成后 → M1 阶段占位（预测待 M2 接入）
- 任务状态写入 task_run 表
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# 全系统统一以交易所时区（上海）为口径：DB 会话已设为 Asia/Shanghai，
# 经 psycopg2 读出的 timestamptz 即为上海感知，故交易时段/新鲜度判断一律用上海 now。
_SH_TZ = ZoneInfo("Asia/Shanghai")

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import func, select, text

from app.core.config import get_settings
from app.core.db import session_scope
from app.core.logging import logger, setup_logging
from app.ingest.orchestrator import IngestOrchestrator
from app.repositories.task_repo import TaskRepository
from app.core.db import get_engine
from app.ops import (
    alert_config_from_env,
    backup_config_from_env,
    install_scheduler_listener,
)


def _switch_on(switch_key: str, env_name: str | None = None, default: bool = False) -> bool:
    """读功能开关：优先 cfg_feature_switch（迁移 008），表缺失/异常时回退 env。

    迁移 008 应用后由数据库控制；迁移前（表不存在）行为与现在完全一致（env 为准）。
    回退路径绝不抛错，避免熔断误杀。
    """
    try:
        with get_engine().connect() as conn:
            row = conn.execute(
                text("SELECT enabled FROM cfg_feature_switch WHERE switch_key = :k"),
                {"k": switch_key}).scalar()
            if row is not None:
                return bool(row)
    except Exception:  # noqa: BLE001  表不存在 / 连接失败 → 回退
        pass
    if env_name is not None:
        return os.getenv(env_name, "1" if default else "0") == "1"
    return default


from app.strategies.fusion_signal import (
    ensure_fusion_table,
    evaluate_all,
    get_position,
    set_push_state,
    upsert_position,
    persist_signals,
    POS_MAP,
    FusionPosition,
    FusionPushLog,
    FusionSignalLog,
)
from app.notify import send_notify


def _predict_job(session, symbols: list[str], label: str) -> int:
    """⑦ 数据更新入库后立即触发预测（轻模型在线更新）"""
    from app.engine.service import predict_symbols

    repo = TaskRepository(session)
    run = repo.start("predict", label=label, payload={"symbols": symbols})
    try:
        out = predict_symbols(session, symbols=symbols)
        ok = sum(1 for r in out if "error" not in r)
        repo.finish(run, "success", f"{ok}/{len(out)} symbols")
        logger.info(f"[scheduler] predict done label={label} {ok}/{len(out)}")
        return ok
    except Exception as e:
        repo.finish(run, "failed", str(e))
        logger.exception(f"[scheduler] predict failed label={label}: {e}")
        return 0


def _ingest_job(label: str) -> None:
    """单次完整 ingest 任务（akshare 单一源）→ 成功后联动预测（⑦）"""
    logger.info(f"[scheduler] ingest job start label={label}")
    try:
        with session_scope() as s:
            repo = TaskRepository(s)
            run = repo.start("ingest", label=label, payload={"trigger": "scheduler"})
            orch = IngestOrchestrator(s)
            reports = orch.ingest_all()
            failed = [r for r in reports if r.error]
            status = "success" if not failed else "partial"
            repo.finish(
                run,
                status,
                f"{len(reports)} symbols, failed={len(failed)}",
            )
            logger.info(
                f"[scheduler] ingest done label={label} status={status} "
                f"total={len(reports)} failed={len(failed)}"
            )

            # §16 大类指数更新（M2 排期）：分类同步 → 指数合成（增量重算）
            try:
                from app.sectors.builder import build_sector_index, sync_sector_map

                sync_sector_map(s)
                idx_stats = build_sector_index(s)
                logger.info(f"[scheduler] sector index done label={label}: {idx_stats}")
            except Exception as se:
                logger.warning(f"[scheduler] sector index failed label={label}: {se}")

            # M2.1 平滑主连自动延伸（R1）：先延伸再预测，保证预测用最新权威口径
            try:
                from app.ingest.smooth_extender import extend_all

                ext_results = extend_all(s)
                logger.info(f"[scheduler] smooth extend done label={label}: {ext_results}")
            except Exception as ee:
                logger.warning(f"[scheduler] smooth extend failed label={label}: {ee}")

            # ⑦ 预测联动：对本次成功入库的品种触发一次预测
            done_syms = [r.symbol for r in reports if not r.error]
            if done_syms:
                _predict_job(s, done_syms, label=label)
    except Exception as e:
        logger.exception(f"[scheduler] ingest job failed label={label}: {e}")


def _fusion_action(db: str, eng: str) -> str:
    if db == "FLAT" and eng == "LONG":
        return "买（开多）"
    if db == "FLAT" and eng == "SHORT":
        return "卖（开空）"
    if db == "LONG" and eng == "FLAT":
        return "平（平多）"
    if db == "SHORT" and eng == "FLAT":
        return "平（平空）"
    if db == "LONG" and eng == "SHORT":
        return "平多+反手开空"
    if db == "SHORT" and eng == "LONG":
        return "平空+反手开多"
    return "状态变化"


def _decimals(tick: float) -> int:
    s = repr(float(tick))
    return len(s.split(".")[1]) if "." in s else 0


# 品种最小变动价位（用于推送价位对齐交易所报价，避免 6420.75 这种不存在的价）
_TICKS = {
    "FG": 1, "SA": 1, "SR": 1, "CF": 5, "TA": 2, "MA": 1, "RM": 1, "OI": 1,
    "AP": 1, "UR": 1, "SH": 1, "PX": 2, "SF": 2, "SM": 2,
    "CU": 10, "AL": 5, "ZN": 5, "PB": 5, "NI": 10, "SN": 10, "AU": 0.02,
    "AG": 1, "RB": 1, "SS": 5, "FU": 1, "BU": 1, "RU": 5, "SP": 2, "AO": 1, "HC": 1,
    "SC": 0.1, "SI": 5, "LC": 20,
    "A": 1, "B": 1, "M": 1, "Y": 2, "P": 2, "C": 1, "CS": 1, "JD": 1,
    "L": 1, "V": 1, "PP": 1, "J": 0.5, "JM": 0.5, "I": 0.5, "EG": 1, "EB": 1, "LH": 5,
}
_PX_DEC = {k: _decimals(v) for k, v in _TICKS.items()}


def _fmt_num(x, nd: int = 1) -> str:
    if x is None:
        return "-"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "-"
    return f"{v:,.{nd}f}"


def _px_of(sym: str, val) -> str:
    """按品种最小变动价位给价（缺失则退化为 2 位/自适应）。"""
    if val is None:
        return "-"
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "-"
    nd = _PX_DEC.get(str(sym or "").rstrip("0123456789").upper())
    if nd is None:
        nd = 2 if abs(v) < 10000 else 1
    return f"{v:,.{nd}f}"


def _side_tag(side: str) -> str:
    # 国内习惯：多=红、空=绿
    return "🔴多" if side == "LONG" else "🟢空"


def _nm(sym: str, names: dict[str, str]) -> str:
    """品种显示名：品种码 + 中文名（如 “FG 玻璃”）。名称缺失时只显示品种码。

    sym 是完整合约码（FG888），展示用去掉尾部数字的品种码（FG）。
    """
    code = str(sym or "").rstrip("0123456789")
    n = (names or {}).get(sym)
    if n and str(n) != code:
        return f"{code} {n}"
    return code or str(sym)


def _norm_hm(s: str) -> str:
    """'9:5' -> '09:05'，统一成两位便于字符串比较。"""
    h, m = s.strip().split(":")
    return f"{int(h):02d}:{int(m):02d}"


def in_push_window(now: datetime, windows: list[str] | None) -> bool:
    """当前本地时间是否落在任一推送窗口内。

    windows 形如 ["08:45-12:00", "13:15-15:30", "20:45-23:30"]；
    空/None 表示不限制（任何时刻都可推）。
    窗口外整个扫描直接跳过——既不在用户睡觉时推送，也能让"隔夜/跨窗口"的状态变化
    在下一个窗口首次扫描时被合并成一条推出去，而不是被静默记录后又无声吞掉。
    """
    if not windows:
        return True
    hm = now.strftime("%H:%M")
    for w in windows:
        try:
            a, b = str(w).split("-")
            a, b = _norm_hm(a), _norm_hm(b)
        except (ValueError, AttributeError):
            continue
        if a <= hm <= b:
            return True
    return False


def _is_at(now: datetime, times: list[str] | None, tol_min: int = 7) -> bool:
    """当前时刻是否命中给定时刻表（用于盘前三档强制推送）。"""
    if not times:
        return False
    for t in times:
        try:
            h, m = _norm_hm(t).split(":")
            anchor = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        except (ValueError, AttributeError):
            continue
        if abs((now - anchor).total_seconds()) <= tol_min * 60:
            return True
    return False


# -----------------------------------------------------
# 交易日日历（节假日不推送；用 akshare 交易日历，缓存到 runtime/）
# -----------------------------------------------------
_CAL_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "runtime",
                         "trade_calendar.json")


def _load_trade_calendar(max_age_days: int = 7) -> set[str]:
    """读取交易日历（'YYYY-MM-DD' 集合）。优先本地缓存，超过 max_age_days 则刷新。

    任何网络/解析失败都退化为空集合 —— 此时只靠「周末判断」兜底，不影响主流程。
    """
    import json

    cached: set[str] = set()
    try:
        if os.path.exists(_CAL_PATH):
            with open(_CAL_PATH, "r", encoding="utf-8") as f:
                blob = json.load(f)
            cached = set(blob.get("dates") or [])
            ts = float(blob.get("ts") or 0)
            if cached and (time.time() - ts) < max_age_days * 86400:
                return cached
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[fusion] 交易日历缓存读取失败: {e}")
    try:
        from app.data import trade_calendar

        dates = {d.isoformat() for d in trade_calendar.futures_trading_days()}
        if dates:
            os.makedirs(os.path.dirname(_CAL_PATH), exist_ok=True)
            with open(_CAL_PATH, "w", encoding="utf-8") as f:
                json.dump({"ts": time.time(), "dates": sorted(dates)}, f)
            logger.info(
                f"[fusion] 期货官方交易日历已刷新（G4 futures_rule）：{len(dates)} 个交易日")
        return dates
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[fusion] 交易日历获取失败（退化为仅周末判断）: {e}")
        return cached


def is_trading_day(now: datetime) -> bool:
    """是否交易日。周末直接否；有日历则查日历，无日历则视为交易日（仅周末过滤）。"""
    if now.weekday() >= 5:
        return False
    cal = _load_trade_calendar()
    if cal:
        return now.strftime("%Y-%m-%d") in cal
    return True



def _heartbeat_rows(results: list[dict], names: dict[str, str], now: datetime,
                    stale_minutes: int) -> list[dict]:
    """从评估结果里挑出「非空仓 且 数据新鲜」的品种，算好止损/保本位。

    数据新鲜=最新小时K距今 <= stale_minutes；常规播报用 90 分钟把休市时段排除；
    盘前播报（08:45 等）用放宽阈值，否则夜盘 23:00 收盘到早盘 585 分钟的间隔
    会把所有持仓都过滤光。
    """
    rows = []
    for r in results:
        if r.get("error") or r.get("state") in (0, None):
            continue
        latest = r.get("latest_dt")
        if latest is None:
            continue
        if latest.tzinfo is None:
            latest = latest.replace(tzinfo=_SH_TZ)
        age_min = (now - latest).total_seconds() / 60.0
        if age_min < -10 or age_min > stale_minutes:   # 负 age=未来时间戳棒，同样视为不新鲜
            continue
        sym = r["symbol"]
        side = POS_MAP.get(r["state"], "FLAT")
        entry = r.get("entry_px")
        px = r.get("latest_close")
        pnl = None
        if entry and px:
            pnl = (px - entry) / entry if side == "LONG" else (entry - px) / entry
        rows.append({
            "symbol": sym,
            "name": names.get(sym, sym),
            "label": _nm(sym, names),          # “FG 玻璃”
            "icon": "🔴" if side == "LONG" else "🟢",
            "side": side,
            "entry": entry,
            "px": px,
            "pnl": pnl,
            "stop": r.get("cur_stop"),
            "be_trigger": r.get("be_trigger"),
            "be_done": bool(r.get("be_done")),
            "lots": int(r.get("lots") or 1),      # P1 加码后的手数（1 = 未加码）
            "atr": r.get("entry_atr"),
            "risk_px": (abs(float(entry) - float(r["init_stop"]))
                        if (entry is not None and r.get("init_stop") is not None)
                        else None),
            "risk_atr": 2.0,
            "age_min": age_min,
            "latest_dt": latest,
        })
    return rows


def _row_line(r: dict, show_levels: bool) -> str:
    """持仓一览的一行（紧凑）：方向 + 代码 名称 + 入场→现价 + 浮盈% [+ 止损/保本]。

    单根K超过 1 天（隔夜/周末/长假）时在行尾标注数据时间，避免误以为是最新价。
    """
    sym = r["symbol"]
    pnl = f"{r['pnl']:+.1%}" if r["pnl"] is not None else "-"
    line = (f"{r['icon']} {r['label']} "
            f"{_px_of(sym, r['entry'])}→{_px_of(sym, r['px'])} {pnl}")
    if int(r.get("lots") or 1) > 1:
        line += f" ×{int(r['lots'])}手"          # P1 加码：标明已加到手数
    if show_levels:
        if r["stop"] is not None:
            line += f" ｜⛔{_px_of(sym, r['stop'])}"
        if r["be_done"]:
            line += " 🎯✅"
        elif r["be_trigger"] is not None:
            line += f" 🎯{_px_of(sym, r['be_trigger'])}"
    if (r.get("age_min") or 0) > 1440:
        line += f" ⏱{r['latest_dt']:%m-%d %H:%M}"
    return line


def _format_push(rows: list[dict], signals: list[dict], changes: list[dict],
                 pending: list[dict], now: datetime, *, kind: str, max_rows: int,
                 show_levels: bool, data_asof: str | None = None) -> tuple[str, str]:
    """渲染推送正文（不发送）。返回 (title, content)。

    三段式，按"越靠上越需要动作"排序；无内容的段自动省略：
      ① 🚨 新信号   —— 开/平/反手，附 ⛔止损 / 🎯保本触发
      ② ⚠️ 止损变动 —— 吊灯止损上移、首次触发保本（可选推项）
      ③ 📋 持仓一览 —— 一行一个品种（代码 + 中文名）
    另加 ⏳ 待执行：近 signal_repeat_min 分钟内的开仓信号复提（防漏看）。
    kind: presession / signal / heartbeat
    """
    n_long = sum(1 for r in rows if r["side"] == "LONG")
    n_short = len(rows) - n_long
    if kind == "presession":
        head = "🌅 盘前持仓"
    elif signals:
        head = "🚨 融合策略信号"
    else:
        head = "📊 持仓简报"
    lines = [f"## {head} {now:%m-%d %H:%M}", ""]
    if rows:
        seg = f"持仓 **{len(rows)}** 个（多{n_long} 空{n_short}）"
        if data_asof:
            seg += f" ｜ 数据截至 {data_asof}"
        lines.append(seg)
        lines.append("")

    if signals:
        opens = [x for x in signals if x.get("to") and x["to"] != "FLAT"]
        closes = [x for x in signals if x.get("action", "").startswith("平")]
        others = [x for x in signals if x not in opens and x not in closes]
        for title, group in (("开仓", opens), ("平仓", closes), ("其他", others)):
            if not group:
                continue
            lines.append(f"### 🚨 {title} {len(group)} 个")
            for sig in group:
                sym = sig["symbol"]
                a = sig["action"]
                icon = "🔴" if "开多" in a else ("🟢" if "开空" in a else "🟡")
                entry_px = sig.get("new_entry") or sig.get("close")
                line = f"{icon} {sig['label']}　{a} @ {_px_of(sym, entry_px)}"
                lines.append(line)
                if sig.get("levels"):
                    lv = sig["levels"]
                    lines.append(
                        f"　　⛔ 止损 {_px_of(sym, lv['stop'])} ｜ 🎯 保本 {_px_of(sym, lv['be_trigger'])}"
                    )
            lines.append("")

    if changes:
        lines.append(f"### ⚠️ 止损/加码变动 {len(changes)} 个")
        for c in changes:
            sym = c["symbol"]
            if c["kind"] == "be":
                lines.append(f"✅ {c['label']}　已挂保本 · 止损提到 {_px_of(sym, c['new'])}")
            elif c["kind"] == "add":
                # P1 利弗莫尔加码：止损此刻已在加权均价之上 → 整仓最差=保本
                tail = (f" ｜ 均价 {_px_of(sym, c['avg'])} ｜ ⛔止损 {_px_of(sym, c['stop'])}"
                        if c.get("avg") is not None and c.get("stop") is not None else "")
                lines.append(f"➕ {c['label']}　加码 至 **{c['new']}手**（{c['old']}→{c['new']}）"
                             f"　整仓已锁保本{tail}")
            else:
                lines.append(
                    f"↑ {c['label']}　止损 {_px_of(sym, c['old'])} → **{_px_of(sym, c['new'])}**"
                )
        lines.append("")

    if pending:
        lines.append(f"### ⏳ 待执行（{len(pending)} 个）")
        for p in pending:
            icon = "🔴" if p["side"] == "LONG" else "🟢"
            verb = "开多" if p["side"] == "LONG" else "开空"
            lines.append(
                f"{icon} {p['label']}　{verb} @ {_px_of(p['symbol'], p['entry'])}"
                f"（{p['at']:%H:%M} 推送，如已执行请忽略）"
            )
        lines.append("")

    if rows:
        shown = sorted(rows, key=lambda x: (0 if x["side"] == "LONG" else 1, x["symbol"]))
        if len(shown) > max_rows:
            shown = shown[:max_rows]
        lines.append("### 📋 持仓一览")
        for r in shown:
            lines.append(_row_line(r, show_levels))
        if len(rows) > max_rows:
            lines.append(f"…另有 {len(rows) - max_rows} 个未列")
        lines.append("")
        lines.append("> ⛔止损=入场∓2×ATR 与吊灯止损取更紧者；🎯触发保本后止损提到成本价。")
    elif not signals and not changes:
        lines.append("当前无持仓、无信号。")

    content = "\n".join(lines).rstrip()
    parts = [now.strftime("%H:%M")]
    if signals:
        parts.append(f"信号{len(signals)}")
    if changes:
        parts.append(f"止损{len(changes)}")
    if rows:
        parts.append(f"持仓{len(rows)}")
    title = "融合策略 " + "·".join(parts)
    return title, content


def _deliver(s, title: str, content: str, now: datetime, *, kind: str,
             n_signals: int, n_rows: int, settings) -> bool:
    """发送（主通道→备用通道）+ 推送留痕。返回是否送达。"""
    ok, via = send_notify(title, content, settings.fusion.fallback_webhook)
    if not ok:
        logger.warning(f"[fusion] 推送未送达: {title}")
    if settings.fusion.push_log:
        try:
            s.add(FusionPushLog(
                pushed_at=datetime.now(timezone.utc),
                kind=kind, title=title, content=content,
                n_signals=n_signals, n_rows=n_rows,
                delivered=bool(ok), via=via,
            ))
            s.flush()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[fusion] 推送留痕失败: {e}")
    return ok


def _push_startup_notice(s, n_symbols: int, now: datetime, settings) -> None:
    """冷启动：只发一条极简上线通知（不列方向清单，避免与'新信号'混淆）

    两道闸门（2026-09-24 修复「每 15/30 分钟重复推送」）：
      ① 品种数为 0（无数据）时不推——此时"已记录 0 个品种基线"无意义；
      ② 同一自然日只推一次——数据表为空时 is_cold 会反复为真（无基线可建），
         否则用户每半小时收到一条"已启动"。
    """
    if n_symbols <= 0:
        logger.info("[fusion] 冷启动但品种数为 0（无数据），跳过上线通知")
        return
    day_start = datetime(now.year, now.month, now.day,
                         tzinfo=now.tzinfo).astimezone(timezone.utc)
    already = (s.query(FusionPushLog.id)
                 .filter(FusionPushLog.kind == "startup",
                         FusionPushLog.pushed_at >= day_start)
                 .first())
    if already:
        logger.info("[fusion] 今日已推送过上线通知，跳过")
        return
    f = settings.fusion
    content = "\n".join([
        "## 融合策略监控已启动",
        f"时间：{now:%Y-%m-%d %H:%M}",
        f"已记录 **{n_symbols}** 个品种的当前持仓基准。",
        "",
        "> 推送形态：**开/平/反手信号**即时推（附 ⛔止损 / 🎯保本位），"
        "**吊灯止损上移**≥0.5×ATR 补推，**盘前**（" + " / ".join(f.pre_session_times) + "）"
        "与盘中每 30 分钟各一份持仓清单。",
        "> 推送时段：" + " ｜ ".join(f.push_windows) + "；非交易日静默。",
        "> 同一笔开仓信号会在 2 小时内的每份简报里重复置顶，直到出现平仓/反手，避免你漏看。",
        "> 之前已持有的仓位属于历史信号，不会重复提醒。",
    ])
    _deliver(s, f"融合策略已启动 {now:%H:%M}", content, now, kind="startup",
             n_signals=0, n_rows=n_symbols, settings=settings)



def _collect_hourly_settled(s, f) -> None:
    """采集小时线，并对「刚收盘的那根K」做定稿复核（防用未定稿收盘价推信号）。

    起因（2026-09-15 JD 案例）：09:00-10:00 这根K在 10:00:00 收盘，而我们在 10:00:20
    就采集了 —— 数据源（新浪分钟线）此时还没定稿，返回 close=3814；1~2 分钟后被改写为 3810。
    后果：推送的入场价 3814 / 止损 3870 / 保本 3800 全套平移 4 点，用户在盘面上"找不到这个价"
    （他自己看的是定稿后的 3810），方向虽未变，但价位全部对不上。

    对策：若库中最新的小时K距当前时间不足 settle_delay_sec 秒，说明它可能尚未定稿
    —— 等够该秒数后再重采一次，用重采值覆盖。并在值确实被改写时留下告警日志，
    便于长期观测这个"数据源定稿漂移"的规模。
    """
    from app.ingest.hourly_collector import HourlyCollector

    hc = HourlyCollector(s)
    hc.collect_all(data_length=800)

    settle = int(getattr(f, "settle_delay_sec", 0) or 0)
    if settle <= 0:
        return
    mx = s.execute(text("select max(trade_datetime) from hourly_bar")).scalar()
    if mx is None:
        return
    if mx.tzinfo is None:
        mx = mx.replace(tzinfo=_SH_TZ)
    age = (datetime.now(_SH_TZ) - mx).total_seconds()
    if not (0 <= age < settle):
        return

    # 定稿指纹：同一时刻所有品种的 (根数, close 总和)
    fp = lambda: s.execute(  # noqa: E731
        text("select count(*), coalesce(sum(close),0) from hourly_bar where trade_datetime = :d"),
        {"d": mx},
    ).one()
    fp1 = fp()
    wait = settle - age + 3
    logger.info(f"[fusion] 最新K({mx:%Y-%m-%d %H:%M}) 刚收盘 {age:.0f}s，等 {wait:.0f}s 定稿后重采")
    time.sleep(wait)
    try:
        hc.collect_all(data_length=800)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[fusion] 定稿重采失败（沿用首采值）: {e}")
        return
    fp2 = fp()
    if fp1 != fp2:
        logger.warning(
            f"[fusion] ⚠ 定稿复核：{mx:%Y-%m-%d %H:%M} 这根K的值被数据源改写了 "
            f"(根数/收盘和 {fp1} → {fp2})，首采值已覆盖为定稿值"
        )
    else:
        logger.info(f"[fusion] 定稿复核通过：{mx:%Y-%m-%d %H:%M} 首采值即为定稿值")


def _fusion_scan_job() -> None:
    """融合策略扫描（每个整点后 :05/:35 触发，即小时线落盘后再生成信号/推送）。

    推送形态（2026-09-15 定稿）：
      - 🚨 新信号：开仓 / 平仓 / 反手即时推，附 ⛔止损位 / 🎯保本触发价
      - ⚠️ 止损变动：吊灯止损朝有利方向移动 ≥ trail_push_atr×ATR 时补推
      - 🌅 盘前预播报：08:45 / 13:15 / 20:45 强制推，带完整止损/保本位
      - 📊 定时简报：交易时段内每 heartbeat_interval_min 分钟一档（对齐 :00 / :30）
      - ⏳ 待执行复提：开仓信号在 signal_repeat_min 分钟内每轮重复置顶，防漏看
    非交易日 / 非推送窗口整轮冻结（不采集、不评估、不更新状态）。
    """
    logger.info("[scheduler] fusion scan start")
    try:
        settings = get_settings()
        f = settings.fusion
        if not f.enabled:
            return
        now = datetime.now(_SH_TZ)
        now_utc = datetime.now(timezone.utc)
        names = {x.symbol: x.name for x in settings.main_contracts}
        with session_scope() as s:
            ensure_fusion_table(get_engine())
            # 0) 闸门：非交易日 / 非推送窗口 → 整轮跳过。
            #    冷启动是例外——表为空时无论几点都要把基线建起来，否则永远建不了基线。
            is_cold_probe = s.query(FusionPosition).count() == 0
            if not is_cold_probe:
                if not is_trading_day(now):
                    logger.info(f"[scheduler] fusion scan {now:%m-%d %H:%M} 非交易日，跳过")
                    return
                if not in_push_window(now, f.push_windows):
                    logger.info(f"[scheduler] fusion scan {now:%H:%M} 非推送时段，跳过")
                    return
            # 1) 拉最新小时线（akshare 新浪60分钟线，单一源；失败沿用已有数据）
            #    只取尾部 ~800 根：足够覆盖 max_bars(400) + min_bars(160)，
            #    避免每 15 分钟都做一次 8000 根的全历史回填（那是分钟级耗时）
            try:
                _collect_hourly_settled(s, f)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[scheduler] hourly collect failed (沿用已有数据): {e}")
            # 2) 评估全部品种（含入场价/入场ATR/止损位等明细）
            results = evaluate_all(s, settings.main_contracts, f)

            # 2.5) V5 组合回撤熔断（Phase 5）：把「期望手数」按当前回撤缩放后再落库。
            #      enabled=False 时恒等变换（双向等价），无 portfolio_equity 时亦恒等 —— 不阻断决策链。
            results = _apply_portfolio_brake(s, results)

            is_cold = s.query(FusionPosition).count() == 0
            is_pre = _is_at(now, f.pre_session_times)

            signals: list[dict] = []
            changes: list[dict] = []
            for r in results:
                if r.get("error"):
                    continue
                sym = r["symbol"]
                label = _nm(sym, names)
                eng = POS_MAP.get(r["state"], "FLAT")
                # 冷启动：无条件以引擎当前状态建立基线（即便数据陈旧），否则基线永远建不起来
                if is_cold:
                    upsert_position(
                        s, sym, eng,
                        entry_price=r.get("entry_px") or r.get("latest_close"),
                        entry_at=now,
                        lots=r.get("lots"),
                    )
                    set_push_state(
                        s, sym,
                        last_stop=(r.get("cur_stop") if eng != "FLAT" else None),
                        last_be_done=(bool(r.get("be_done")) if eng != "FLAT" else False),
                        signal_at=None, pushed_at=None,
                    )
                    continue
                old = s.get(FusionPosition, sym)
                db = old.position if old is not None else "FLAT"
                db_entry = (float(old.entry_price)
                            if (old is not None and old.entry_price is not None) else None)
                # 数据新鲜度：陈旧K不产生任何信号/价位（防用过期数据触发）。
                # 2026-09-23 补丁：age 为负（未来时间戳棒）此前恒判"新鲜"，打穿本门控
                # → 一轮 18 信号误推送。未来棒与陈旧棒一律视为不新鲜。
                latest = r.get("latest_dt")
                age_min = None
                if latest is not None:
                    if latest.tzinfo is None:
                        latest = latest.replace(tzinfo=_SH_TZ)
                    age_min = (now - latest).total_seconds() / 60.0
                fresh = (age_min is not None) and (-10.0 <= age_min <= f.stale_minutes)

                if db != eng:
                    # ---------- 状态变化 → 信号 ----------
                    if not fresh:
                        logger.info(f"[fusion] {sym} 数据陈旧({age_min:.0f}分钟)，抑制状态变化")
                        continue
                    px = r.get("latest_close")
                    pnl = None
                    if db_entry and px:
                        if db == "LONG":
                            pnl = (px - db_entry) / db_entry
                        elif db == "SHORT":
                            pnl = (db_entry - px) / db_entry
                    new_entry = r.get("entry_px") if eng != "FLAT" else None
                    upsert_position(s, sym, eng, entry_price=new_entry or px, entry_at=now,
                                    lots=r.get("lots"))
                    sig = {
                        "symbol": sym,
                        "name": names.get(sym, sym),
                        "label": label,
                        "action": _fusion_action(db, eng),
                        "close": px,
                        "entry": db_entry,
                        "new_entry": new_entry,
                        "pnl": pnl,
                        "to": eng,
                    }
                    if (f.push_stop_levels and eng != "FLAT"
                            and r.get("init_stop") is not None and new_entry):
                        sig["levels"] = {
                            "stop": r.get("cur_stop"),
                            "be_trigger": r.get("be_trigger"),
                            "be_done": bool(r.get("be_done")),
                            "risk_px": abs(float(new_entry) - float(r["init_stop"])),
                            "risk_atr": f.sl_atr,
                            "atr": r.get("entry_atr"),
                        }
                    signals.append(sig)
                    set_push_state(
                        s, sym,
                        last_stop=(r.get("cur_stop") if eng != "FLAT" else None),
                        last_be_done=(bool(r.get("be_done")) if eng != "FLAT" else False),
                        signal_at=(now_utc if eng != "FLAT" else None),
                        pushed_at=now_utc,
                    )
                    continue

                # ---------- 状态未变：P1加码 / 吊灯止损上移 / 首次触发保本 ----------
                if eng == "FLAT" or old is None or not fresh:
                    continue
                cur = r.get("cur_stop")
                prev_stop = float(old.last_stop) if old.last_stop is not None else None
                atr = r.get("entry_atr")
                be_now = bool(r.get("be_done"))
                be_prev = bool(old.last_be_done)
                lots_now = int(r.get("lots") or 1)
                lots_prev = int(old.lots or 1)
                sign = 1.0 if eng == "LONG" else -1.0
                thr = (f.trail_push_atr * float(atr)) if (atr and f.trail_push_atr > 0) else None
                moved = None
                if (cur is not None and prev_stop is not None and thr is not None
                        and (float(cur) - prev_stop) * sign >= thr):
                    moved = (prev_stop, float(cur))
                be_flip = bool(be_now and not be_prev)
                # P1 利弗莫尔加码：手数增加即独立事件（加码后止损已锁在均价之上 → 整仓最差=保本）
                if lots_now > lots_prev:
                    changes.append({"symbol": sym, "label": label, "kind": "add",
                                    "old": lots_prev, "new": lots_now,
                                    "avg": (float(r["entry_px"]) if r.get("entry_px") else None),
                                    "stop": (float(cur) if cur is not None else None)})
                    set_push_state(s, sym,
                                   last_stop=(float(cur) if cur is not None else prev_stop),
                                   last_be_done=be_now, lots=lots_now, pushed_at=now_utc)
                    continue
                if prev_stop is None or (moved is None and not be_flip):
                    # 首次记录基准 / 变动未达阈值 → 只静默更新基准
                    set_push_state(s, sym,
                                   last_stop=(float(cur) if cur is not None else prev_stop),
                                   last_be_done=be_now, lots=lots_now)
                    continue
                if be_flip:
                    changes.append({"symbol": sym, "label": label, "kind": "be",
                                    "old": prev_stop,
                                    "new": (float(cur) if cur is not None else None)})
                else:
                    changes.append({"symbol": sym, "label": label, "kind": "trail",
                                    "old": moved[0], "new": moved[1]})
                set_push_state(s, sym,
                               last_stop=(float(cur) if cur is not None else prev_stop),
                               last_be_done=be_now, lots=lots_now, pushed_at=now_utc)

            # 2.5) 信号明细落库（供回测/复盘）：旁路，失败不阻塞推送
            #     冷启动轮也会产生基线信号，一并留存，保证"从哪天开始有记录"可追溯。
            try:
                n_logged = persist_signals(
                    s, signals, now, source="fusion_scan",
                    kind="startup" if is_cold else "signal",
                )
                if n_logged:
                    logger.info(f"[scheduler] 信号落库 {n_logged} 条")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[scheduler] 信号落库异常（已忽略）: {e}")

            if is_cold:
                n_ok = sum(1 for r in results if not r.get("error"))
                _push_startup_notice(s, n_ok, now, settings)
                logger.info(f"[scheduler] fusion scan 冷启动：已建 {n_ok} 品种基线，已推送上线通知")
                return

            # 3) 持仓一览（用「展示阈值」而非「信号阈值」：夜盘→早盘的 585 分钟空档、
            #    周末、长假都不该让持仓从清单里消失；信号仍受 stale_minutes 约束）
            rows = _heartbeat_rows(
                results, names, now, f.display_max_age_min,
            ) if (f.heartbeat or is_pre or signals or changes) else []

            # 4) 未确认开仓信号复提（防漏看：同一笔在 repeat 窗口内每轮置顶）
            pending: list[dict] = []
            if f.signal_repeat_min > 0 and rows:
                cutoff = now_utc - timedelta(minutes=f.signal_repeat_min)
                sig_syms = {x["symbol"] for x in signals}
                for r in results:
                    if r.get("error") or r.get("state") in (0, None):
                        continue
                    sym = r["symbol"]
                    if sym in sig_syms:
                        continue
                    o = s.get(FusionPosition, sym)
                    if o is None or o.signal_at is None:
                        continue
                    sa = o.signal_at
                    if sa.tzinfo is None:
                        sa = sa.replace(tzinfo=timezone.utc)
                    if sa >= cutoff:
                        stored_entry = (float(o.entry_price)
                                        if (o is not None and o.entry_price is not None)
                                        else r.get("entry_px"))
                        pending.append({
                            "symbol": sym,
                            "label": _nm(sym, names),
                            "side": POS_MAP.get(r["state"]),
                            "entry": stored_entry,
                            "at": sa.astimezone().replace(tzinfo=None),
                        })

            # 5) 推送决策：信号 > 盘前 > 止损变动 / 定时简报
            last_push = s.execute(select(func.max(FusionPushLog.pushed_at))).scalar()
            gap_ok = True
            if last_push is not None:
                lp = last_push if last_push.tzinfo else last_push.replace(tzinfo=timezone.utc)
                gap_ok = (now_utc - lp).total_seconds() >= f.heartbeat_interval_min * 60
            # 作业已固定在整点后 :05/:35 触发，心跳每次扫描均可触发，
            # 由 heartbeat_interval_min 控制最小间隔（gap_ok）。
            due_heartbeat = bool(f.heartbeat and rows and gap_ok)
            if signals:
                kind = "signal"
            elif is_pre:
                kind = "presession"
            elif changes or due_heartbeat:
                kind = "heartbeat"
            else:
                kind = None

            if kind is None:
                logger.info("[scheduler] fusion scan done, 本轮无需推送")
                return
            data_asof = None
            if is_pre and rows:
                data_asof = max(r["latest_dt"] for r in rows).strftime("%m-%d %H:%M")
            title, content = _format_push(
                rows, signals, changes, pending, now,
                kind=kind, max_rows=f.heartbeat_max_rows,
                show_levels=f.push_stop_levels, data_asof=data_asof,
            )
            ok = _deliver(s, title, content, now, kind=kind,
                          n_signals=len(signals), n_rows=len(rows), settings=settings)
            for r in rows:
                set_push_state(s, r["symbol"], pushed_at=now_utc)
            logger.info(
                f"[scheduler] fusion scan done kind={kind} 信号{len(signals)} "
                f"止损变动{len(changes)} 待执行{len(pending)} 持仓{len(rows)} 送达={ok}"
            )
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] fusion scan failed: {e}")

def _sync_cloud_local_job() -> None:
    """统一云地同步链路（PRD §14）：subprocess 隔离跑 scripts/sync_cloud_local.py。

    通过环境变量启用/配置（默认关闭，避免影响线上云调度）：
      CLOUD_SYNC_ENABLED=1          启用本作业
      CLOUD_SYNC_TABLES=...         逗号表名（默认见下）
      CLOUD_SYNC_DIRECTION=cloud2local
      CLOUD_SYNC_MODE=incremental   首次自动全量，之后增量
      CLOUD_SYNC_CRON_HOUR/MINUTE   默认 03:15
      CLOUD_SYNC_CHUNK_MONTHS=3
      CLOUD_SYNC_LOOKBACK_DAYS=30   增量时额外重放的回看窗口（修云端历史回补分歧）
    DSN 由脚本自身按 CLOUD_PG*/LOCAL_PG* 解析（CB 经隧道 15432 连云）。
    """
    import subprocess
    import sys as _sys
    from pathlib import Path

    script = (Path(__file__).resolve().parents[1] / "scripts" / "sync_cloud_local.py")
    if not script.exists():
        logger.error(f"[scheduler] 找不到同步脚本 {script}")
        return
    direction = os.getenv("CLOUD_SYNC_DIRECTION", "cloud2local")
    mode = os.getenv("CLOUD_SYNC_MODE", "incremental")
    tables = os.getenv(
        "CLOUD_SYNC_TABLES",
        "bar_15m,bar_30m,bar_60m,daily_bar,factor_value,"
        "warehouse_receipt,inventory,member_position_rank_summary",
    )
    cmd = [_sys.executable, str(script), "--tables", tables,
           "--direction", direction, "--mode", mode]
    chunk = os.getenv("CLOUD_SYNC_CHUNK_MONTHS")
    if chunk:
        cmd += ["--chunk-months", chunk]
    # 回看窗口：云端重建会往历史回补旧行，纯水位增量补不回来（见脚本头注释）
    lb = os.getenv("CLOUD_SYNC_LOOKBACK_DAYS")
    cmd += ["--lookback-days", lb or "30"]
    logger.info(f"[scheduler] cloud_local_sync start: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        logger.info(f"[scheduler] cloud_local_sync exit={proc.returncode} "
                    f"tail={(proc.stdout or '')[-600:]}")
        if proc.returncode != 0:
            logger.warning(f"[scheduler] cloud_local_sync stderr={(proc.stderr or '')[-800:]}")
    except subprocess.TimeoutExpired:
        logger.warning("[scheduler] cloud_local_sync timeout>7200s")
    except Exception as e:  # noqa: BLE001
        logger.exception("[scheduler] cloud_local_sync failed")


#: 调度以 subprocess 方式调用的生产脚本（相对仓库根的 scripts/）。
#: 2026-09-29：历史上因路径 parents[2] 拼错 + .dockerignore 排除 runtime/，
#: 这 5 个作业**每天静默失败**（日志仅一行 error，无人看）。此处集中登记，
#: 启动时 fail-fast 校验，缺一个就大声报错，杜绝再次静默。
SUBPROCESS_SCRIPTS = (
    "sync_cloud_local.py",
    "adjust_bars.py",
    "adjust_minute.py",
    # Phase 4（因子层）：缺任一都会在 16:20/16:45 静默失败，故一并纳入启动自检
    "compute_factor_v1v6.py",
    "factor_ic_monitor.py",
    # Phase 4/6（准入裁决 / 迁移执行）：人工与部署入口
    "admit_factors.py",
    "db_apply_migrations.py",
    # R3：仓单日报采集（scrapling 浏览器版，四所）；依赖镜像 WITH_SCRAPLING=1 装好 scrapling+chromium
    "collect_warehouse_receipt.py",
)


def _scripts_dir() -> Path:
    """容器内 /app/scripts（__file__=/app/app/scheduler.py → parents[1]=/app）。"""
    return Path(__file__).resolve().parents[1] / "scripts"


def _verify_script_assets() -> None:
    """启动自检：subprocess 脚本必须存在且可读，否则 ERROR 级告警（不阻断启动）。

    不阻断是因为：同步专用实例也可能不需要全部脚本；但必须**看得见**——
    旧行为是 `if not script.exists(): logger.error(...)` 埋在作业内部，
    只有到点触发时才报一行，等于静默。
    """
    d = _scripts_dir()
    missing = [n for n in SUBPROCESS_SCRIPTS if not (d / n).is_file()]
    if missing:
        logger.error(
            f"[scheduler][ASSET] scripts 目录={d} 缺失生产脚本 {missing} "
            f"—— 相关作业将全部失败（典型原因：.dockerignore 排除 / 镜像未 COPY）")
    else:
        logger.info(f"[scheduler][ASSET] 生产脚本自检通过 {len(SUBPROCESS_SCRIPTS)} 个 ({d})")


def _verify_lookahead_guard() -> None:
    """T16 启动守卫：因子计算的「前视防护」必须有效，否则 ERROR 级告警（fail-loud）。

    历史教训（2026-09-27）：新增开关型参数没被纳入惰性计算触发条件 → 系统不报错，
    只让参数「看起来生效实则无效」（静默退化）。前视防护 `hm < "15:00"` 一旦被
    误删，当日 15:00 巨量会泄漏进量比 → 与未来收益机械相关（虚假 IC）。因此启动
    时用合成数据验证防护仍生效；失效则**大声报错**，不让它带着 bug 静默跑。
    """
    try:
        import importlib.util
        import sys
        sd = _scripts_dir()
        mod_path = sd / "compute_factor_v1v6.py"
        if not mod_path.is_file():
            logger.warning("[scheduler][GUARD] compute_factor_v1v6.py 不在 scripts 目录，跳过前视守卫")
            return
        if str(sd) not in sys.path:
            sys.path.insert(0, str(sd))
        spec = importlib.util.spec_from_file_location("__la_guard__", mod_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        ok = bool(mod.has_lookahead_guard())
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[scheduler][GUARD] 前视守卫自检异常（未阻断启动）：{e}")
        return
    if ok:
        logger.info("[scheduler][GUARD] 前视防护自检通过（hm<15:00 仍有效）")
    else:
        # 红线失效但绝不静默：ERROR 级，运维必见
        logger.error(
            "[scheduler][GUARD] ⚠️ 前视防护疑似失效：15:00 之后 bar 已泄漏进当日量比！"
            " 请立即检查 compute_factor_v1v6.build_v_factors 的 `hm < DECISION_HM` 过滤。")


def _build_scheduler() -> BlockingScheduler:
    settings = get_settings()
    sched = BlockingScheduler(timezone=settings.env.TZ)
    _verify_script_assets()
    _verify_lookahead_guard()

    # CLOUD_SYNC_ENABLED=1 → 本调度实例为「云地同步专用」：
    # 仅注册 cloud_local_sync 作业并立即返回，其余采集/训练/回测任务全部由云端
    # 调度器负责。目的有二：
    #   1) 本实例跑在 CB 裸机（经隧道 15432 连云、127.0.0.1:5432 连本地），
    #      其余任务用 get_engine()→POSTGRES_HOST=timescaledb（docker 服务名）在裸机不可解析；
    #   2) 避免与云端调度器重复执行、重复写库。
    # 云容器内不设置此变量（无隧道），照常注册全部任务。
    if os.getenv("CLOUD_SYNC_ENABLED") == "1":
        _sh = int(os.getenv("CLOUD_SYNC_CRON_HOUR", "3"))
        _sm = int(os.getenv("CLOUD_SYNC_CRON_MINUTE", "15"))
        sched.add_job(
            _sync_cloud_local_job,
            trigger=CronTrigger(hour=_sh, minute=_sm, timezone=settings.env.TZ),
            id="cloud_local_sync",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=1800,
        )
        logger.info(f"[scheduler] SYNC-ONLY mode: registered cloud_local_sync at {_sh:02d}:{_sm:02d} (CLOUD_SYNC_ENABLED)")
        return sched

    for spec in settings.cron_specs:
        trigger = CronTrigger(
            hour=spec.hour, minute=spec.minute, timezone=settings.env.TZ
        )
        sched.add_job(
            _ingest_job,
            trigger=trigger,
            args=[spec.label],
            id=f"ingest_{spec.label}",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(
            f"[scheduler] registered cron {spec.hour:02d}:{spec.minute:02d} ({spec.label})"
        )

    # ⑱ LSTM 每周重训一次（周六 06:00，避开交易时段）
    sched.add_job(
        _lstm_weekly_job,
        trigger=CronTrigger(day_of_week="sat", hour=6, minute=0, timezone=settings.env.TZ),
        id="lstm_weekly_retrain",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("[scheduler] registered cron sat 06:00 (lstm_weekly_retrain)")

    # §16.7 ⑤ 传导权重每周重算（与 LSTM 周训对齐，周六 06:30 在 LSTM 重训后）
    sched.add_job(
        _transmission_weekly_job,
        trigger=CronTrigger(day_of_week="sat", hour=6, minute=30, timezone=settings.env.TZ),
        id="transmission_weekly_recalc",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("[scheduler] registered cron sat 06:30 (transmission_weekly_recalc)")

    # M4 回测每周一次（周六 07:00，动态权重/传导更新后）
    sched.add_job(
        _backtest_weekly_job,
        trigger=CronTrigger(day_of_week="sat", hour=7, minute=0, timezone=settings.env.TZ),
        id="backtest_weekly",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("[scheduler] registered cron sat 07:00 (backtest_weekly)")

    # ⑳ 权重月更（每月 1 日 06:30，读最近回测的近 60 日准确率）
    sched.add_job(
        _weights_monthly_job,
        trigger=CronTrigger(day=1, hour=6, minute=30, timezone=settings.env.TZ),
        id="weights_monthly_update",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info("[scheduler] registered cron day1 06:30 (weights_monthly_update)")

    # 融合策略实时信号扫描：每个整点后 :05/:35 触发（小时线落盘后再生成信号/推送，
    # 与每小时 :00 的 hourly_collect 错开，避免重叠；与每日三次 ingest 并行，互不影响）
    if settings.fusion.enabled:
        sched.add_job(
            _fusion_scan_job,
            trigger=CronTrigger(minute="5,35", timezone=settings.env.TZ),
            id="fusion_scan",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info("[scheduler] registered fusion_scan at :05/:35 (post hourly collect)")

    # 复权主连每日重算（adjust_cont_adj）已于 2026-10-04 随 fdf 模块退役停用：
    # cont_adj 不再由定时作业生成（fdf 为唯一生产者，已删）。保留 minute_bar（L0 原始）。
    logger.info("[scheduler] adjust_cont_adj 停用（fdf 退役，无生产者）")

    # 1 分钟复权作业（adjust_minute_bar_adj）已于 2026-10-03 退役：minute_bar_adj 为
    # 加法前复权，与项目"后复权唯一口径"冲突且云端实测已崩坏；保留 minute_bar（未复权
    # L0 原始数据）作为唯一可再生基础。本作业不再注册。
    logger.info("[scheduler] adjust_minute_bar_adj 已退役（2026-10-03 停采集，不再注册）")

    # 会员持仓排名（龙虎榜）每日收盘后入库（交易所官方 CSV）
    rp = settings.yaml.rank_position
    sched.add_job(
        _rank_job,
        trigger=CronTrigger(hour=rp.run_hour, minute=rp.run_minute, timezone=settings.env.TZ),
        id="rank_position",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    logger.info(f"[scheduler] registered cron {rp.run_hour:02d}:{rp.run_minute:02d} (rank_position)")

    # R3：仓单日报（四所，scrapling 浏览器版）每日收盘后采集。
    # 开关走 cloud.yaml 的 warehouse_receipt.enabled（与采集类开关约定一致，可见且显式）。
    wrc = settings.yaml.warehouse_receipt
    if wrc.enabled:
        sched.add_job(
            _warehouse_receipt_job,
            trigger=CronTrigger(hour=wrc.run_hour, minute=wrc.run_minute, timezone=settings.env.TZ),
            id="warehouse_receipt",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(f"[scheduler] registered cron {wrc.run_hour:02d}:{wrc.run_minute:02d} (warehouse_receipt)")
    else:
        logger.info("[scheduler] warehouse_receipt 按配置关闭（warehouse_receipt.enabled=false）")

    # 小时线每小时自动更新（决策 7）：整点触发一次全品种增量采集
    hcfg = settings.hourly_config
    if hcfg.enabled:
        sched.add_job(
            _hourly_job,
            trigger=CronTrigger(minute=hcfg.minute, hour="*", timezone=settings.env.TZ),
            id="hourly_collect",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(f"[scheduler] registered hourly collect every :{hcfg.minute:02d}")
    else:
        logger.info("[scheduler] hourly collect disabled (config)")

    # #5 分钟优先管线：每 30 分钟把实时 1 分钟并入 minute_bar 并增量合成 bar_5/15/30/60m
    # （bar_60m 即"小时数据"）。与 hourly_collect 解耦，各自服务不同表，互不冲突。
    # ★错峰（2026-10-06）：由 minute="*/30"（:00/:30）改为 "7,37" —— 避开整点，
    #   使本作业不与 hourly_collect（:00）及任何整点拉取峰值重叠；配合作业内
    #   advisory 锁 + 合成后追平校验，彻底消除 bar_* 最新日静默漏桶（见 job 注释）。
    sched.add_job(
        _minute_and_bars_job,
        trigger=CronTrigger(minute="7,37", timezone=settings.env.TZ),
        id="minute_and_bars",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=900,
    )
    logger.info("[scheduler] registered minute_and_bars at :07/:37 (staggered, minute-first pipeline)")

    # fut_kline 增量/派生重建作业已于 2026-10-04 随 fdf 模块退役一并停用（天勤清理专项）。
    # fut_kline 表保留为孤儿表待观测，不再有写入通路（_fut_kline_job/_rebuild_fut_kline_job 已删除）。
    logger.info("[scheduler] fut_kline 增量/重建作业已停用（fdf 退役，天勤清理）")

    # 统一云地同步链路（PRD §14）：在 CLOUD_SYNC_ENABLED=1 时，本函数已在顶部
    # 以「同步专用」模式提前返回（仅注册 cloud_local_sync）。此处不再重复注册。

    # 库存 / 仓单（决策 2，周频周五）
    inv = settings.inventory_config
    if inv.enabled:
        sched.add_job(
            _inventory_job,
            trigger=CronTrigger(day_of_week=inv.run_day, hour=inv.run_hour,
                               minute=inv.run_minute, timezone=settings.env.TZ),
            id="inventory_weekly",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(f"[scheduler] registered inventory weekly dow={inv.run_day} "
                    f"{inv.run_hour:02d}:{inv.run_minute:02d}")

    # 基差 / 现货（决策 2，日频）
    sbc = settings.spot_basis_config
    if sbc.enabled:
        sched.add_job(
            _spot_basis_job,
            trigger=CronTrigger(hour=sbc.run_hour, minute=sbc.run_minute,
                               timezone=settings.env.TZ),
            id="spot_basis_daily",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger.info(f"[scheduler] registered spot_basis daily "
                    f"{sbc.run_hour:02d}:{sbc.run_minute:02d}")

    # ---- 每日期货数据刷新（C6：消除 C1/C2/C5 陈旧的唯一种子）----------------
    # 收盘后 + spot_basis 入库后运行；惰性导入脚本模块，避免其异常拖垮整个调度器
    # （F7/F12：顶层 `from scripts import ...` 曾导致 21 个作业全挂）。
    # 注：本作业属「采集类」；若本实例 CLOUD_SYNC_ENABLED=1（本地同步专用）会在上方
    #     早退分支直接返回、不注册；由云端（非同步）调度器注册运行。
    def _daily_futures_refresh_job():
        from scripts.daily_futures_refresh import main as _refresh
        _refresh()
    sched.add_job(
        _daily_futures_refresh_job,
        trigger=CronTrigger(hour=18, minute=10, timezone=settings.env.TZ),
        id="daily_futures_refresh", replace_existing=True,
        max_instances=1, coalesce=True, misfire_grace_time=1800,
    )
    logger.info("[scheduler] registered cron 18:10 daily_futures_refresh "
                "(①contract_daily ②main_contract_map；本地同步实例不注册)")

    # ---- 因子层（Phase 4 / 新 PRD T22）-------------------------------------
    # 顺序：收盘(15:00) → 16:20 因子计算 → 16:45 IC 监控 → 17:35 数据自检。
    # 三者都用 subprocess 隔离（耗时长 / 需独立 DB 连接），且脚本自带幂等。
    if _switch_on("factor_v1v6_enabled", "FACTOR_COMPUTE_ENABLED", default=True):
        sched.add_job(
            _factor_v1v6_job,
            trigger=CronTrigger(hour=16, minute=20, timezone=settings.env.TZ),
            id="factor_v1v6_daily",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        logger.info("[scheduler] registered cron 16:20 (factor_v1v6_daily)")

    if _switch_on("factor_ic_monitor_enabled", "FACTOR_IC_MONITOR_ENABLED", default=True):
        sched.add_job(
            _factor_ic_monitor_job,
            trigger=CronTrigger(hour=16, minute=45, timezone=settings.env.TZ),
            id="factor_ic_monitor_daily",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        logger.info("[scheduler] registered cron 16:45 (factor_ic_monitor_daily)")

    # ---- 数据自检（Phase 5 / 新 PRD T19，V4）--------------------------------
    # 默认关闭（SelfCheckConfig.enabled=False），由 DATA_SELFCHECK_ENABLED=1 开启；
    # 只写 anomaly_ticket 工单，**不阻塞**决策链。
    if _switch_on("data_selfcheck_enabled", "DATA_SELFCHECK_ENABLED", default=False):
        sched.add_job(
            _data_selfcheck_job,
            trigger=CronTrigger(hour=17, minute=35, timezone=settings.env.TZ),
            id="data_selfcheck_daily",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        logger.info("[scheduler] registered cron 17:35 (data_selfcheck_daily)")
    else:
        logger.info("[scheduler] data_selfcheck 停用（DATA_SELFCHECK_ENABLED!=1）")

    # ---- 运维层（整改 P1-1 备份 / P1-2 告警）--------------------------------
    # 备份：rebuild 大事务前必须有一次可回滚的备份；容器内无 pg_dump/dockr，
    #       故走 app.ops.backup 的 COPY 流式逻辑备份（小表优先）。
    # 告警：资源/数据新鲜度/DB uptime 定期巡检 + 作业失败事件监听。
    try:
        _bcfg = backup_config_from_env()
        if _bcfg.enabled:
            sched.add_job(
                _backup_daily_job,
                trigger=CronTrigger(hour=_bcfg.run_hour, minute=_bcfg.run_minute,
                                    timezone=settings.env.TZ),
                id="ops_backup_daily",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=3600,
            )
            logger.info(f"[scheduler] registered backup daily "
                        f"{_bcfg.run_hour:02d}:{_bcfg.run_minute:02d} → {_bcfg.dir}")
        else:
            logger.info("[scheduler] ops backup 停用（BACKUP_ENABLED!=1）")
    except Exception as e:
        logger.exception(f"[scheduler] backup 作业注册失败: {e}")

    try:
        _acfg = alert_config_from_env()
        if _acfg.enabled:
            sched.add_job(
                _alert_check_job,
                trigger=IntervalTrigger(minutes=max(1, int(_acfg.interval_min))),
                id="ops_alert_check",
                replace_existing=True,
                max_instances=1,
                coalesce=True,
                misfire_grace_time=600,
            )
            install_scheduler_listener(sched, _acfg)
            logger.info(f"[scheduler] registered alert check every "
                        f"{_acfg.interval_min}min")
        else:
            logger.info("[scheduler] alerting 停用（ALERT_ENABLED!=1）")
    except Exception as e:
        logger.exception(f"[scheduler] 告警作业注册失败: {e}")

    return sched


# ---------------------------------------------------------------------------
# Phase 4/5 新增作业
# ---------------------------------------------------------------------------
def _backup_daily_job() -> None:
    """运维 P1-1：每日定时备份（逻辑备份 + 保留策略 + 可选异地）。

    容器内没有 pg_dump / docker CLI（实测 2026-10-02），因此由 app.ops.backup
    用 psycopg COPY 流式导出关键小表（字典/配置/登记/权重/工单）。
    备份失败会主动告警——因为 rebuild 前的可回滚点是最后一道防线。
    """
    from app.ops.backup import backup_config_from_env, run_backup

    cfg = backup_config_from_env()
    try:
        with session_scope() as s:
            rep = run_backup(session=s, cfg=cfg)
        if not rep.get("ok"):
            send_notify("[qhyc] CRIT 备份异常",
                        "失败表：{0}\n目录：{1}".format(rep.get("failed"), rep.get("dir")))
        else:
            logger.info(f"[scheduler] backup ok rows={rep.get('total_rows')} "
                        f"dir={rep.get('dir')}")
    except Exception as e:
        logger.exception(f"[scheduler] backup failed: {e}")
        send_notify("[qhyc] CRIT 备份失败", str(e)[:500])


def _alert_check_job() -> None:
    """运维 P1-2：定期巡检（资源/数据新鲜度/DB uptime/静默），异常即告警。"""
    from app.ops.alerting import alert_config_from_env, check_all

    cfg = alert_config_from_env()
    try:
        with session_scope() as s:
            rep = check_all(session=s, cfg=cfg)
        n = len(rep.get("findings", []))
        if n:
            logger.warning(f"[scheduler] alert findings={n} :: {rep.get('summary')}")
        else:
            logger.info("[scheduler] alert check ok")
    except Exception as e:
        logger.exception(f"[scheduler] alert check failed: {e}")


def _factor_v1v6_job() -> None:
    """V1/V6 因子每日增量计算（T22）。

    增量口径：默认回溯 60 天（FACTOR_COMPUTE_LOOKBACK_DAYS），脚本 UPSERT 幂等；
    首次部署或修复历史时手工跑一次全量（--since 2015）。
    """
    import subprocess
    import sys as _sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "compute_factor_v1v6.py"
    if not script.exists():
        logger.error(f"[scheduler] 找不到脚本 {script}")
        return
    lookback = os.getenv("FACTOR_COMPUTE_LOOKBACK_DAYS", "60")
    since = os.getenv("FACTOR_COMPUTE_SINCE") or (
        datetime.now() - timedelta(days=int(lookback))).date().isoformat()
    cmd = [_sys.executable, str(script), "--since", since]
    logger.info(f"[scheduler] factor_v1v6 start since={since}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        logger.info(f"[scheduler] factor_v1v6 exit={proc.returncode} "
                    f"tail={(proc.stdout or '')[-400:]}")
        if proc.returncode != 0:
            logger.warning(f"[scheduler] factor_v1v6 stderr={(proc.stderr or '')[-600:]}")
    except subprocess.TimeoutExpired:
        logger.warning("[scheduler] factor_v1v6 timeout>3600s")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] factor_v1v6 failed: {e}")


def _factor_ic_monitor_job() -> None:
    """因子滚动 IC 监控（T21）：写入 factor_ic_roll，异常因子进告警清单。"""
    import subprocess
    import sys as _sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "factor_ic_monitor.py"
    if not script.exists():
        logger.error(f"[scheduler] 找不到脚本 {script}")
        return
    cmd = [_sys.executable, str(script), "--apply",
           "--window", os.getenv("FACTOR_IC_WINDOW", "20")]
    if os.getenv("FACTOR_IC_ONLY_ENABLED", "1") == "1":
        cmd.append("--enabled-only")
    logger.info("[scheduler] factor_ic_monitor start")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        logger.info(f"[scheduler] factor_ic_monitor exit={proc.returncode} "
                    f"tail={(proc.stdout or '')[-800:]}")
        if proc.returncode != 0:
            logger.warning(f"[scheduler] factor_ic_monitor stderr={(proc.stderr or '')[-600:]}")
    except subprocess.TimeoutExpired:
        logger.warning("[scheduler] factor_ic_monitor timeout>3600s")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] factor_ic_monitor failed: {e}")


def _data_selfcheck_job() -> None:
    """数据自检（T19/V4）：缺失 / 零成交 / 尾部回退 → anomaly_ticket 工单。"""
    try:
        from app.ingest.data_selfcheck import SelfCheckConfig, run as _run
        from app.core.db import session_scope
    except Exception as e:  # noqa: BLE001
        logger.error(f"[scheduler] data_selfcheck 导入失败：{e}")
        return
    cfg = SelfCheckConfig(
        enabled=True,
        missing_pct_alarm=float(os.getenv("DATA_SELFCHECK_MISSING_PCT", "0.05")),
        zero_volume_days=int(os.getenv("DATA_SELFCHECK_ZERO_VOL_DAYS", "3")),
    )
    try:
        with session_scope() as s:
            res = _run(session=s, cfg=cfg)
        logger.info(f"[scheduler] data_selfcheck done: {res}")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] data_selfcheck failed: {e}")


def _apply_portfolio_brake(session, results: list[dict]) -> list[dict]:
    """V5 组合回撤熔断：按当前回撤缩放「期望手数」（Phase 5）。

    设计要点
    * 默认**关闭**（PORTFOLIO_BRAKE_ENABLED），打开前必须先在 portfolio_equity 里
      有真实权益曲线（migrations/004 建表），否则恒等返回；
    * 手数取整用 ceil：1 手 × 0.5 = 0.5 → 1 手（不把 1 手直接砍成 0，
      "清仓"只由 dd_stop 的 scalar=0.0 触发，语义必须唯一）；
    * 任何异常都只记录并恒等返回 —— 熔断失效可以，误杀不行。
    """
    if not _switch_on("portfolio_brake_enabled", "PORTFOLIO_BRAKE_ENABLED", default=False) or not results:
        return results
    try:
        import math

        from app.risk.portfolio_brake import (
            BrakeConfig, apply as _brake_apply, brake_scalar, load_equity_curve,
        )
        cfg = BrakeConfig(
            enabled=True,
            dd_warn=float(os.getenv("PORTFOLIO_BRAKE_DD_WARN", "0.15")),
            dd_stop=float(os.getenv("PORTFOLIO_BRAKE_DD_STOP", "0.25")),
            lookback=int(os.getenv("PORTFOLIO_BRAKE_LOOKBACK", "250")),
        )
        equity = load_equity_curve(session=session, lookback=cfg.lookback)
        if not equity:
            # ⚠ fail-loud：熔断已开启但**无权益曲线** → 恒等放行等于熔断失效。
            #   这不是"正常降级"，是"风控空转"——必须告警而非 info 静默。
            #   权益曲线来自 portfolio_equity（当前 0 行，无已实现盈亏可派生）。
            logger.warning(
                "[portfolio_brake] 熔断已开启但 portfolio_equity 无数据 → 恒等放行，"
                "**风控实际未生效**（非降级，是空转）。请先补权益曲线或关闭开关。"
            )
            return results
        sc = brake_scalar(equity, cfg)
        if sc >= 1.0:
            return results
        n_cut = 0
        for r in results:
            lots = r.get("lots") or 0
            if not lots:
                continue
            if sc <= 0.0:
                r["lots"] = 0
            else:
                r["lots"] = max(1, int(math.ceil(float(lots) * sc - 1e-9)))
            n_cut += 1
        logger.warning(f"[portfolio_brake] 回撤熔断生效 scalar={sc} → 缩放 {n_cut} 个品种手数")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[portfolio_brake] 熔断异常，恒等放行：{e}")
    return results


def _backtest_weekly_job() -> None:
    """M4 周度回测：全品种逐评估点预测 vs 真实"""
    logger.info("[scheduler] weekly backtest start")
    try:
        with session_scope() as s:
            repo = TaskRepository(s)
            run = repo.start("backtest", label="weekly")
            try:
                from app.backtest.engine import BacktestParams, backtest_symbols

                out = backtest_symbols(s, params=BacktestParams())
                ok = sum(1 for r in out if "error" not in r and "skipped" not in r)
                repo.finish(run, "success", f"{ok}/{len(out)} symbols")
                logger.info(f"[scheduler] weekly backtest done {ok}/{len(out)}")
            except Exception as e:
                repo.finish(run, "failed", str(e))
                logger.exception(f"[scheduler] weekly backtest failed: {e}")
    except Exception as e:
        logger.exception(f"[scheduler] backtest weekly job error: {e}")


def _weights_monthly_job() -> None:
    """⑳ 权重月更：按最近回测的近 60 日准确率更新 model_weights"""
    logger.info("[scheduler] monthly weight update start")
    try:
        with session_scope() as s:
            repo = TaskRepository(s)
            run = repo.start("weights_update", label="monthly")
            try:
                from app.backtest.weights import update_model_weights

                res = update_model_weights(s)
                repo.finish(run, "success", str(res))
                logger.info(f"[scheduler] weight update done: {res}")
            except Exception as e:
                repo.finish(run, "failed", str(e))
                logger.exception(f"[scheduler] weight update failed: {e}")
    except Exception as e:
        logger.exception(f"[scheduler] weights monthly job error: {e}")


def _transmission_weekly_job() -> None:
    """§16.2 第 2 层：数据驱动动态权重每周重算（corr/granger/te/var/blend）"""
    logger.info("[scheduler] transmission weekly recalc start")
    try:
        with session_scope() as s:
            repo = TaskRepository(s)
            run = repo.start("transmission_calc", label="weekly")
            try:
                from app.sectors.dynamic_weights import calc_dynamic_weights

                stats = calc_dynamic_weights(s)
                repo.finish(run, "success", str(stats)[:400])
                logger.info(f"[scheduler] transmission recalc done: {stats}")
            except Exception as e:
                repo.finish(run, "failed", str(e))
                logger.exception(f"[scheduler] transmission recalc failed: {e}")
    except Exception as e:
        logger.exception(f"[scheduler] transmission weekly job error: {e}")


def _lstm_weekly_job() -> None:
    """⑱ LSTM 周度重训：全部主连品种，权重落 /app/runtime/lstm"""
    logger.info("[scheduler] lstm weekly retrain start")
    try:
        with session_scope() as s:
            repo = TaskRepository(s)
            run = repo.start("lstm_train", label="weekly")
            try:
                from app.features.pipeline import build_features
                from app.predictors.lstm_train import _ensure_torch, train_symbol

                torch = _ensure_torch()
                settings = get_settings()
                seq_len = settings.yaml.predict.lstm.seq_len
                epochs = settings.yaml.predict.lstm.epochs
                os.environ.setdefault("LSTM_WEIGHT_DIR", settings.yaml.predict.lstm.weight_dir)

                ok, total = 0, 0
                for spec in settings.main_contracts:
                    total += 1
                    try:
                        snap = build_features(s, spec.symbol)
                        # §16.4 多变量：附加传导特征 v1
                        from app.features.transmission import build_transmission_frame

                        tframe = build_transmission_frame(s, spec.symbol, end=snap.last_date)
                        r = train_symbol(
                            torch, spec.symbol, snap.rets,
                            dates=snap.dates, extra=tframe,
                            seq_len=seq_len, epochs=epochs,
                        )
                        ok += 1 if "error" not in r and "skipped" not in r else 0
                    except Exception as e:
                        logger.warning(f"[lstm] {spec.symbol} 训练失败: {e}")
                repo.finish(run, "success", f"{ok}/{total} symbols")
                logger.info(f"[scheduler] lstm weekly retrain done {ok}/{total}")
            except Exception as e:
                repo.finish(run, "failed", str(e))
                logger.exception(f"[scheduler] lstm weekly retrain failed: {e}")
    except Exception as e:
        logger.exception(f"[scheduler] lstm weekly job error: {e}")


def _rank_job() -> None:
    """每日收盘后入库会员持仓排名（龙虎榜）。交易所官方 CSV，单所失败不影响其他所。"""
    logger.info("[scheduler] rank_position start")
    try:
        settings = get_settings()
        cfg = settings.yaml.rank_position
        if not cfg.enabled:
            # ★ 不得静默 skip：采集类作业被关闭会导致数据断供，而下游
            # pipeline readiness 依赖 member_position_rank 新鲜度（曾因此恒 False）。
            logger.warning(
                "[scheduler] rank_position 已被配置关闭（rank_position.enabled=false），"
                "本次跳过采集 —— member_position_rank 将逐渐滞后并拖垮 pipeline readiness；"
                "若非有意停采请检查 config/cloud.yaml 与 app/core/config.py 的默认值")
            return
        from app.ingest import rank_position as RP
        summary = RP.run(exchanges=cfg.exchanges)
        logger.info(f"[scheduler] rank_position done: {summary}")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] rank_position job error: {e}")


def _warehouse_receipt_job() -> None:
    """仓单日报采集（scrapling 浏览器版，四所全量）。

    依赖容器内已装 scrapling + 无头 chromium（镜像须以 WITH_SCRAPLING=1 构建）。
    未装时脚本/导入会直接抛错（fail-loud），便于发现镜像缺漏，而非静默缺数据。
    默认关闭：镜像重建并置 WAREHOUSE_RECEIPT_ENABLED=1 后启用。
    """
    import subprocess
    import sys as _sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "collect_warehouse_receipt.py"
    if not script.exists():
        logger.error(f"[scheduler] 找不到脚本 {script}")
        return
    # 抓最近 2 个交易日（含周末/节假日补采），脚本内部按交易所工作日过滤
    end = datetime.now().date()
    start = end - timedelta(days=2)
    cmd = [_sys.executable, str(script), "--start", start.isoformat(),
           "--end", end.isoformat(), "--exchange", "all"]
    logger.info(f"[scheduler] warehouse_receipt start {cmd}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5400)
        logger.info(f"[scheduler] warehouse_receipt exit={proc.returncode} "
                    f"tail={(proc.stdout or '')[-600:]}")
        if proc.returncode != 0:
            logger.warning(f"[scheduler] warehouse_receipt stderr={(proc.stderr or '')[-800:]}")
    except subprocess.TimeoutExpired:
        logger.warning("[scheduler] warehouse_receipt timeout>5400s")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] warehouse_receipt failed: {e}")


def _hourly_job() -> None:
    """小时线每小时自动更新（决策 7）：整点触发一次全品种小时线增量采集。

    与主连日线三档调度解耦；融合信号扫描在交易时段内另有 15 分钟尾部刷新兜底。
    """
    logger.info("[scheduler] hourly collect start")
    try:
        with session_scope() as s:
            from app.ingest.hourly_collector import HourlyCollector

            hc = HourlyCollector(s)
            h_stats = hc.collect_all()
            h_ok = sum(1 for r in h_stats if "error" not in r)
            logger.info(f"[scheduler] hourly collect done: {h_ok}/{len(h_stats)} symbols")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] hourly collect failed: {e}")


def _minute_and_bars_job() -> None:
    """#5 分钟优先管线：实时 1 分钟入库 → 增量合成 5/15/30/60 分钟。

    使 minute_bar 成为唯一事实来源（历史 CSV + 实时 akshare 1 分钟同口径），
    bar_*（含 bar_60m = 小时数据）随调度低成本刷新。hourly_bar（akshare 直拉）的去留
    取决于 #5 分叉决策——本作业只负责 bar_* 一侧，互不冲突。

    ★错峰（2026-10-06）：整个「采集 + 合成」持会话级 advisory 锁
    ``LOCK_MINUTE_PIPELINE``，与并发实例（本作业另一副本 / 手动脚本 /
    其它进程写 minute_bar）互斥。并发是 bar_* 最新交易日**静默整段漏桶**的根因：
    DELETE 已删最近桶、INSERT 却读到 minute_bar 未提交快照，脚本 rowcount 仍为正
    不报错（实测 09-30 一度回退到 09-29）。拿不到锁则等待，超时抛错而非带病运行。
    合成函数内部另有「落库后追平校验 + 重试」作为第二道防线。
    """
    logger.info("[scheduler] minute_and_bars start")
    try:
        with session_scope() as s:
            from app.ingest.minute_collector import MinuteCollector
            from app.ingest.synthesizer import minute_pipeline_lock, synthesize_bars_incremental

            with minute_pipeline_lock(s):
                mc = MinuteCollector(s)
                m_stats = mc.collect_all()
                m_ok = sum(1 for r in m_stats if "error" not in r)
                synth_stats = synthesize_bars_incremental(s)
            logger.info(
                f"[scheduler] minute_and_bars done: minute_ok={m_ok}/{len(m_stats)} "
                f"synth={synth_stats}"
            )
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] minute_and_bars failed: {e}")


def _inventory_job() -> None:
    """库存 / 仓单自动采集（决策 2，周频周五，akshare futures_inventory_em）。

    覆盖全部主连品种；单品种失败隔离，不阻塞其余。
    """
    logger.info("[scheduler] inventory collect start")
    try:
        from app.ingest import inventory as INV

        settings = get_settings()
        if not settings.inventory_config.enabled:
            # 采集类作业关闭必须告警：静默 skip 会让 inventory 数据断供（曾发生同类事故）
            logger.warning(
                "[scheduler] inventory 已被配置关闭（inventory.enabled=false），本次跳过采集；"
                "若非有意停采请检查 config/cloud.yaml 与 app/core/config.py 的默认值")
            return
        products = [spec.product for spec in settings.main_contracts]
        with session_scope() as s:
            repo = TaskRepository(s)
            run = repo.start("inventory", label="weekly")
            try:
                stats = INV.collect_inventory_em(s, products)
                repo.finish(run, "success", str(stats))
                logger.info(f"[scheduler] inventory done: {stats}")
            except Exception as e:  # noqa: BLE001
                repo.finish(run, "failed", str(e))
                logger.exception("[scheduler] inventory failed")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] inventory job error: {e}")


def _spot_basis_job() -> None:
    """基差 / 现货自动采集（决策 2，日频，akshare futures_spot_price_daily）。

    每轮回填空品种最近 window_days 天增量窗口；products=None 拉全品种。
    """
    logger.info("[scheduler] spot_basis collect start")
    try:
        import datetime as _dt

        from app.ingest import spot_basis as SB

        settings = get_settings()
        if not settings.spot_basis_config.enabled:
            # 同 inventory：采集类作业关闭必须告警，不得静默 skip
            logger.warning(
                "[scheduler] spot_basis 已被配置关闭（spot_basis.enabled=false），"
                "本次跳过采集；pipeline readiness 依赖 spot_basis 新鲜度，"
                "若非有意停采请检查 config/cloud.yaml 与 app/core/config.py 的默认值")
            return
        end = _dt.date.today()
        start = end - _dt.timedelta(days=settings.spot_basis_config.window_days)
        with session_scope() as s:
            repo = TaskRepository(s)
            run = repo.start("spot_basis", label="daily")
            try:
                stats = SB.collect_spot_basis(s, start, end, products=None)
                repo.finish(run, "success", str(stats))
                logger.info(f"[scheduler] spot_basis done: {stats}")
            except Exception as e:  # noqa: BLE001
                repo.finish(run, "failed", str(e))
                logger.exception("[scheduler] spot_basis failed")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] spot_basis job error: {e}")


def _adjust_job() -> None:
    """每日（含夜盘）收盘后重算所有主品种复权主连（hourly + daily），幂等 upsert。

    单品种异常隔离：任一品种失败仅记日志，不影响其余品种与外層调度。
    耗时较长（50+ 品种 × 2 周期），故排在 02:30 空闲时段。
    """
    logger.info("[scheduler] adjust cont_adj start")
    try:
        settings = get_settings()
        with session_scope() as s:
            repo = TaskRepository(s)
            run = repo.start("adjust", label="nightly")
            try:
                from app.ingest.fdf import adjust_fdf
            except Exception as imp_e:  # noqa: BLE001
                repo.finish(run, "failed", f"import failed: {imp_e}")
                logger.exception("[scheduler] adjust import failed")
                return
            ok = fail = 0
            for spec in settings.main_contracts:
                key = f"{spec.exchange}.{spec.product.lower()}"
                for freq in ("hourly", "daily"):
                    try:
                        adjust_fdf.run(key, freq)
                        ok += 1
                    except Exception as e:  # noqa: BLE001
                        fail += 1
                        logger.warning(f"[scheduler] adjust {key} {freq} failed: {e}")

            # —— 扩展：15/30/60 分钟未复权主连(bar_*) → fut_kline cont_adj ——
            # 加法平移前复权，仅平移换月断层(对比8888指数连)，不改任意两点价差/盈亏点数。
            # subprocess 隔离（耗时较长，避免占满 APScheduler 线程池；同 _fut_kline_job 之因）。
            try:
                import subprocess as _sp
                import sys as _sys
                from pathlib import Path as _P
                adj_script = (_P(__file__).resolve().parents[1]
                              / "scripts" / "adjust_bars.py")
                if adj_script.exists():
                    for mf in ("min15", "min30", "min60"):
                        mcmd = [_sys.executable, str(adj_script), "--freq", mf]
                        logger.info(f"[scheduler] adjust bars {mf} start")
                        try:
                            mproc = _sp.run(mcmd, capture_output=True, text=True, timeout=3600)
                            logger.info(f"[scheduler] adjust bars {mf} exit={mproc.returncode} "
                                        f"tail={(mproc.stdout or '')[-400:]}")
                            if mproc.returncode != 0:
                                logger.warning(f"[scheduler] adjust bars {mf} stderr="
                                               f"{(mproc.stderr or '')[-600:]}")
                                fail += 1
                        except _sp.TimeoutExpired:
                            logger.warning(f"[scheduler] adjust bars {mf} timeout>3600s")
                            fail += 1
                        except Exception as me:  # noqa: BLE001
                            logger.exception(f"[scheduler] adjust bars {mf} failed: {me}")
                            fail += 1
                else:
                    logger.warning(f"[scheduler] 找不到复权脚本 {adj_script}")
            except Exception as e:  # noqa: BLE001
                logger.exception(f"[scheduler] adjust bars job error: {e}")

            status = "success" if fail == 0 else "partial"
            repo.finish(run, status, f"ok={ok} fail={fail}")
            logger.info(f"[scheduler] adjust done ok={ok} fail={fail}")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] adjust job error: {e}")


def _adjust_minute_job() -> None:
    """1 分钟原始数据复权 → minute_bar_adj（夜间增量，云端 15432）。

    since=72h 前：期间 15m 检测出换月的品种须全量重算（前复权锚定最新段，
    新换月会整体平移历史）；否则只追加新 bar（cum=0，复权价=原价）。
    subprocess 隔离；脚本自带幂等（先 DELETE 后 COPY）。
    """
    import subprocess
    import sys as _sys
    from datetime import datetime, timedelta
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "adjust_minute.py"
    if not script.exists():
        logger.error(f"[scheduler] 找不到 1 分钟复权脚本 {script}")
        return
    since = (datetime.now() - timedelta(hours=72)).isoformat(timespec="seconds")
    cmd = [_sys.executable, str(script), "--since", since]
    logger.info(f"[scheduler] adjust minute_bar_adj start since={since}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        logger.info(f"[scheduler] adjust minute exit={proc.returncode} "
                    f"tail={(proc.stdout or '')[-400:]}")
        if proc.returncode != 0:
            logger.warning(f"[scheduler] adjust minute stderr={(proc.stderr or '')[-600:]}")
    except subprocess.TimeoutExpired:
        logger.warning("[scheduler] adjust minute timeout>7200s")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] adjust minute failed: {e}")


def _ensure_hourly_uniq_index() -> None:
    """G5：为 hourly_bar 补 (symbol, trade_datetime, src) 唯一约束（幂等、失败不阻断启动）。

    单表混存 csv/akshare 多 src，靠 src 过滤隔离；若历史已存在同主键重复行，
    建索引会失败——此时跳过并告警（重复行需先清洗），不阻断调度启动。
    """
    from sqlalchemy import text

    from app.core.db import get_engine

    ddl = (
        "CREATE UNIQUE INDEX IF NOT EXISTS hourly_bar_uniq "
        "ON hourly_bar (symbol, trade_datetime, src)"
    )
    try:
        with get_engine().begin() as conn:
            conn.execute(text(ddl))
        logger.info("[scheduler] hourly_bar unique index ensured")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[scheduler] hourly_bar unique index skipped (可能已存在重复行，需清洗): {e}")


def main() -> None:
    setup_logging()
    settings = get_settings()
    logger.info(f"[scheduler] starting role={settings.env.ROLE}")

    sched = _build_scheduler()

    def _shutdown(*_):
        logger.info("[scheduler] shutdown signal received")
        try:
            sched.shutdown(wait=False)
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # 启动时立刻执行一次（方便调试），由 RUN_ON_BOOT=0 关闭。
    # 同步专用模式（CLOUD_SYNC_ENABLED=1，跑在 CB 裸机、不经 docker 服务名）不连本地
    # timescaledb，跳过 boot 作业与 hourly 索引检查，避免无害报错噪声。
    if os.getenv("CLOUD_SYNC_ENABLED") != "1" and os.getenv("RUN_ON_BOOT", "1") == "1":
        time.sleep(3)  # 等 DB ready
        _ensure_hourly_uniq_index()
        _ingest_job("boot")

    try:
        sched.start()
    except (KeyboardInterrupt, SystemExit):
        _shutdown()


if __name__ == "__main__":
    main()