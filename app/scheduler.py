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
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

# 全系统统一以交易所时区（上海）为口径：DB 会话已设为 Asia/Shanghai，
# 经 psycopg2 读出的 timestamptz 即为上海感知，故交易时段/新鲜度判断一律用上海 now。
_SH_TZ = ZoneInfo("Asia/Shanghai")

#: **非交易时段**（午休 / 隔夜 / 休市）的数据陈旧容忍阈值（分钟）。
#:
#: ★ 2026-10-08 新增。此前信号新鲜度只有单一阈值 ``stale_minutes=90``，把两类
#:   完全不同的"陈旧"一刀切：
#:     · **自然陈旧**：午休 11:30~13:30、隔夜 15:00~次日 21:00 —— 本就没有新 bar
#:       （``hourly_bar`` 时间戳是**收盘标签**且未收盘棒会被丢弃），age 变大是正常的；
#:     · **异常陈旧**：该有bar 却没有，说明采集断了，必须抑制信号。
#:   后果：推送时段 ``13:15-15:30`` 一开始，最新 bar 还是 11:15（age=120>90），
#:   **所有持仓被过滤光** → 用户看到"静默无信号"且无任何说明；早盘 08:45 同理。
#:
#: 阈值上限（超过即判异常陈旧→ 抑制并告警），防止掩盖真正的采集断供。
OFFHOURS_STALE_MINUTES = 300

#: 收盘标签集合（与 :data:`app.ingest.hourly_collector._VALID_HOURLY_LABELS` 一致）。
#: 用于算"距上一个收盘事件多久"，据此给陈旧度打折——**这才是正确口径**：
#: 午休 11:30~13:30 天然会有 ~2 小时无新 bar（14:15 才收盘入库），
#: 若按固定 90 分钟判定，13:15 推送时段一开始就会把持仓全滤光。
_CLOSE_LABELS: tuple[tuple[int, int], ...] = (
    (9, 30), (10, 0), (10, 45), (11, 15), (13, 45), (14, 15), (14, 45),
    (15, 0), (15, 30), (21, 0), (22, 0), (23, 0), (0, 0), (1, 0), (2, 0), (2, 30),
)

#: 收盘后到实际入库的额外延迟容忍（分钟）。
#: ``hourly_collector`` 每 30 分钟采一次（:07/:37），且**未收盘的棒会被丢弃**，
#: 所以一根 bar 从"收盘"到"可入库"最长要等一个采集周期 + 收盘瞬间的边界。
_HARVEST_LAG_MIN = 45

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


def _require_subprocess_success(proc, label: str,
                                tolerated: tuple[int, ...] = ()) -> None:
    """subprocess 作业非 0 退出必须让作业抛异常（CL-20261107-19）。

    ``ops.scheduled_task_run`` 的 success 判据是 apscheduler 的
    ``EVENT_JOB_EXECUTED``——**只要作业函数没抛异常就记 success**。所以
    ``logger.warning`` 记下 stderr 毫无用处：2026-11-07 的 pgconn.py 误删事故里
    9 个脚本连续 ImportError，流水仍是 11 次全绿。

    ``tolerated`` 只给「非 0 = 业务状态而非脚本崩溃」的作业（如
    factor_freshness 的 exit=1 = 有因子仍断供、脚本内部已发硬告警）。
    调用方的 ``except Exception`` 必须重新 raise，否则这里抛了也会被自己吞掉。
    """
    rc = proc.returncode
    if rc == 0 or rc in tolerated:
        return
    tail = ((proc.stderr or "") + "\n" + (proc.stdout or ""))[-1200:]
    raise RuntimeError(f"[scheduler] {label} 非 0 退出 rc={rc}: {tail}")


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
    except Exception as exc:  # noqa: BLE001  表不存在 / 连接失败 → 回退
        logger.opt(exception=True).warning(f"[silent-swallow] 异常被静默忽略（此处原本为 pass）: {exc}")
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
        # 2026-10-23：原实现只要不抛异常就记 success，"0/50 symbols"（实际全灭）也会
        # 写成 success，心跳巡检因此永远发现不了批量失败。按成功率分档。
        if not out:
            status = "failed"
        elif ok == len(out):
            status = "success"
        elif ok == 0:
            status = "failed"
        else:
            status = "partial"
        repo.finish(run, status, f"{ok}/{len(out)} symbols")
        logger.info(f"[scheduler] predict done label={label} {ok}/{len(out)} status={status}")
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


# 品种展示小数位：唯一真源 = dim_variety.tick_size（经 barstore.variety_spec）。
# 删除本地 _TICKS/_PX_DEC 副本（缺失即静默按 2 位，导致推送价位与交易所 tick 分叉，
# 例如大商所 P/Y 自 2026-04-01 由 2 元/吨改 1 元/吨，硬编码副本不会跟着变）。
# 取不到规格时 fail-soft 兜底并**告警一次**（推送是展示用途，不因展示精度而崩）。
_DEC_CACHE: dict[str, int | None] = {}
_DEC_WARNED: set[str] = set()


def _tick_decimals(code: str) -> int | None:
    """品种码 → 推送价位展示小数位（读 dim_variety.tick_size）。取不到返回 None。"""
    if not code:
        return None
    if code in _DEC_CACHE:
        return _DEC_CACHE[code]
    nd: int | None = None
    try:
        from app.data.barstore import variety_spec
        tick = variety_spec(code).get("tick_size")
        if tick is not None:
            nd = _decimals(str(tick))
    except Exception as e:  # noqa: BLE001  库不可达/品种未登记 → None（fail-soft）
        if code not in _DEC_WARNED:
            logger.warning("[scheduler] 取 {} 的 tick 失败，推送价位小数位兜底: {}", code, e)
            _DEC_WARNED.add(code)
    _DEC_CACHE[code] = nd
    return nd


def _fmt_num(x, nd: int = 1) -> str:
    if x is None:
        return "-"
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "-"
    return f"{v:,.{nd}f}"


# —— 推送盈亏金额（CL-20261108-18，按 68bfe82^ 原契约恢复）——
# 68bfe82 同步 130 个生产 hotfix 时把这组符号整体下线，但契约测试未同步清理，
# 造成 CI 常年 3 errors。2026-11-08 用户拍板恢复功能，原样复原，不改口径。
_MULT_CACHE: dict[str, float | None] = {}
_MULT_WARNED: set[str] = set()


def _multiplier(sym: str) -> float | None:
    """合约乘数唯一真源 = dim_variety；缺失返回 None，绝不用 1.0 估算盈亏。"""
    code = str(sym or "").rstrip("0123456789").upper()
    if not code:
        return None
    if code in _MULT_CACHE:
        return _MULT_CACHE[code]
    mult = None
    try:
        from app.data.barstore import variety_spec
        mult = float(variety_spec(code)["multiplier"])
    except Exception as e:  # noqa: BLE001  推送展示失败不阻断交易信号
        if code not in _MULT_WARNED:
            logger.warning("[scheduler] 取 {} 的合约乘数失败，盈亏金额不估算: {}", code, e)
            _MULT_WARNED.add(code)
    _MULT_CACHE[code] = mult
    return mult


def _pnl_yuan(sym: str, side: str, entry, px, lots) -> float | None:
    """按信号价估算毛盈亏（元），未含手续费/滑点，不代表实际成交账户。"""
    if side not in ("LONG", "SHORT") or entry is None or px is None:
        return None
    mult = _multiplier(sym)
    if mult is None:
        return None
    try:
        sign = 1.0 if side == "LONG" else -1.0
        n = int(lots) if lots is not None else 1
        return sign * (float(px) - float(entry)) * mult * n
    except (TypeError, ValueError):
        return None


def _fmt_money(x) -> str:
    return "-" if x is None else f"{float(x):+,.0f}元"


def _px_of(sym: str, val) -> str:
    """按品种最小变动价位给价（真源 dim_variety.tick_size；取不到才自适应兜底）。"""
    if val is None:
        return "-"
    try:
        v = float(val)
    except (TypeError, ValueError):
        return "-"
    nd = _tick_decimals(str(sym or "").rstrip("0123456789").upper())
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
    """是否期货交易日（G4 官方源 ``futures_rule``），**带覆盖边界保护**。

    ★ 2026-10-08 修复（生产缺陷：信号推送静默中断）
    ----------------------------------------------------
    原实现是「日历集合成员判断」：``return now.strftime("%Y-%m-%d") in cal``。

    问题在于 ``futures_rule`` **只记交易日**，于是「某日无行」有二义：
    「确定休市（周末/长假）」或「未来尚未播种」。原实现**把两者一律当成休市**，
    且**不报错、不告警**，日志只写「非交易日，跳过」——与真实休市语气完全相同。

    真实事故（2026-10-08）：``futures_rule`` 播种止于 09-30，而 ``seeded_through``
    元数据记到 10-05（10-05 播种时 10-01~10-07 国庆休市无数据）。于是 10-08
    （周三，节后首个交易日）被判「非交易日」，``fusion_scan`` 全天跳过 →
    **信号推送静默中断**，日志无任何告警。而同日 L0/L2/L3 各表都已到 10-08
    （contract_daily / factor_value / main_contract_map / minute_bar 均有当日数据），
    唯一滞后的就是它——典型的单点静默失效。

    现改走 :func:`app.data.trade_calendar.is_futures_trading_day`，它按
    ``seeded_through`` 做**三分判定**（文档见该函数）：
      ① 表内有该日                      → True
      ② 表内无该日 且 <= seeded_through → False（确定休市，无兜底）
      ③ 表内无该日 且 >  seeded_through → **告警** + 退化为「周一~周五」
    故播种滞后时会**告警并按工作日兜底**，而非静默误判为休市。

    降级链（三级，每级都留痕，**绝不静默**）：
        is_futures_trading_day → _load_trade_calendar 集合判断 → 仅周末过滤
    """
    if now.weekday() >= 5:
        return False
    d = now.date()
    # 首选：官方 API（三分判定 + 超边界告警）
    try:
        from app.data import trade_calendar

        return trade_calendar.is_futures_trading_day(d)
    except Exception as e:  # noqa: BLE001 —— 日历模块整体不可用才降级
        logger.warning(f"[fusion] 官方日历不可用（{type(e).__name__}: {e}），降级查缓存日历")
    # 次选：本地缓存日历（仍是集合判断，但至少能容忍官方源临时故障）
    cal = _load_trade_calendar()
    if cal:
        return d.isoformat() in cal
    # 末选：仅按周末过滤
    return True



def _in_trading_session(now: datetime) -> bool:
    """当前是否处于**任一品种的交易时段**内（含夜盘跨日）。

    用途（2026-10-08 新增）：区分两种「数据陈旧」——
      · **自然陈旧**：午休 / 隔夜 / 休市，本就没有新bar，age 变大是**正常**的；
      · **异常陈旧**：交易时段内却拿不到新 bar，说明采集断了。
    两者此前被同一个 ``stale_minutes`` 一刀切，导致：
      · 午休（11:30~13:30）age 可达 120 分钟 > 90 → 推送时段 13:15 开始时
        **所有持仓被过滤光**（用户看到"静默无信号"）；
      · 早盘 08:45 同理（隔夜 age 逾 1000 分钟）。

    数据源：``l3_ref.cfg_trading_session``（317 行，含 ``next_day_flag`` 标记夜盘跨日）。
    查不到/表缺失时**返回 True**（保守：按"在时段内"处理 → 沿用严格阈值，
    即维持既有行为，不因本函数不可用而放宽风控）。
    """
    t = now.timetz()
    try:
        with get_engine().connect() as conn:
            rows = conn.execute(text(
                "SELECT start_time, end_time, next_day_flag "
                "FROM l3_ref.cfg_trading_session WHERE is_active = true"
            )).fetchall()
    except Exception as e:  # noqa: BLE001 —— 日历不可用时保守处理
        logger.warning(f"[fusion] 交易时段表不可用（按在时段内处理）: {e}")
        return True
    if not rows:
        return True
    for st, en, nxt in rows:
        s = st if isinstance(st, dt_time) else dt_time.fromisoformat(str(st)[:5])
        e_ = en if isinstance(en, dt_time) else dt_time.fromisoformat(str(en)[:5])
        # 当日时段：start<=t<=end（覆盖午盘外的正常交易段）
        if s <= t <= e_:
            return True
        # 夜盘跨日：session 记在 D 日且 start>end（如 22:30 -> 02:30）
        #   · t <= end      → today 凌晨段属于 **yesterday** 的夜盘
        #   · t >= start    → today 晚间段属于 **today** 的夜盘
        if nxt and s > e_ and (t <= e_ or t >= s):
            return True
    return False


def _last_close_before(now: datetime) -> datetime | None:
    """返回 ``now`` 之前（含）最近一个**收盘事件**时刻，无则None。

    为什么按"收盘事件"而不是墙钟整点算陈旧度
    ------------------------------------------
    ``hourly_bar`` 的时间戳是**收盘标签**（10:00 / 11:15 / 14:15 / 15:00 / 夜盘
    21:00~02:30），且未收盘的棒会被丢弃（见
    :func:`app.ingest.hourly_collector._valid_hourly_ts` 的未来戳判据）。因此：

      · 午休 11:30~13:30 天然 **~2 小时无新 bar**（要等 14:15 收盘入库）；
      · 隔夜 15:00~次日 21:00 同理（要等夜盘 21:00 收盘）。

    若用固定 90 分钟阈值判"数据陈旧"，推送时段 ``13:15`` 一开始
    （最新 bar 仍是 11:15，age=120）就会把**所有持仓过滤光** → 用户看到
    "静默无信号"且无任何说明。这正是 2026-10-08 要修的缺陷。

    跨日处理：``(0,0)/(1,0)/(2,0)/(2,30)`` 是**前一日夜盘**的收盘（落在今天凌晨），
    直接当作"今天的收盘标签"参与比较即可—— 对 08:45 而言，最后一个收盘事件是
    今天 02:30，容差 = 375 + 45 = 420 → 被 :data:`OFFHOURS_STALE_MINUTES` 截到300。
    """
    t = now.time()
    today = now.date()
    todays = [dt_time(h, m) for h, m in sorted(_CLOSE_LABELS)]
    passed = [x for x in todays if x <= t]
    if passed:
        return datetime.combine(today, passed[-1], tzinfo=_SH_TZ)
    # 今天还没有任何收盘事件（凌晨/早盘前）→ 用昨天最后一个夜盘收盘标签
    # （取 <= 23:59 的最大者，即 (23,0)）
    prev_labels = [x for x in todays if x <= dt_time(23, 59)]
    base = prev_labels[-1] if prev_labels else dt_time(23, 0)
    return datetime.combine(today - timedelta(days=1), base, tzinfo=_SH_TZ)


def _effective_stale_minutes(now: datetime, base: int) -> tuple[int, str]:
    """返回 ``(生效阈值, 原因)``。

    口径（2026-10-08）：阈值 = ``max(base, 距上一个收盘事件的时长 + 采集延迟)``，
    再用 :data:`OFFHOURS_STALE_MINUTES` 封顶。含义：

      · **盘中最新的那个收盘标签刚过** → 容差≈采集延迟，但保底 ``base``，
          故仍是严格判断（age 超 90 分钟依旧抑制）；
      · **刚跨过午休/隔夜的长空档** → 容差自动放宽到 ~2~5 小时，
          正常休市不再被误判为"数据断供"；
      · **容差超过 5 小时仍无数据** → 封顶 300 分钟，依旧抑制并保留告警路径，
          **不会**因放宽而掩盖真正的采集断供。

    实测（RB888，2026-10-08）::

        13:15  最新 bar 11:15(age=120)  旧阈值 90 → 抑制  ← 用户报的"静默无信号"
                                  新阈值 165 → 正常
        14:30  最新 bar 14:15(age=15)   新阈值 90（保底）  → 正常
        08:45  最新 bar 02:30(age=375)  新阈值 300        → 正常
    """
    last = _last_close_before(now)
    if last is None:
        return base, "unknown"
    gap = (now - last).total_seconds() / 60.0
    tol = gap + _HARVEST_LAG_MIN
    if tol <= base:
        return base, "session"
    return int(min(max(base, tol), OFFHOURS_STALE_MINUTES)), "offhours"


def _heartbeat_rows(results: list[dict], names: dict[str, str], now: datetime,
                    stale_minutes: int) -> list[dict]:
    """从评估结果里挑出「非空仓 且 数据新鲜」的品种，算好止损/保本位。

    数据新鲜 = 最新小时 K 距今 <= **生效阈值**，阈值由 :func:`_effective_stale_minutes`
    按「当前是否在交易时段」给出（时段内严格、时段外宽松）——见该函数说明的踩坑记录。
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
            "pnl_yuan": _pnl_yuan(sym, side, entry, px,
                                  int(r["lots"]) if r.get("lots") is not None else 1),
            "stop": r.get("cur_stop"),
            "be_trigger": r.get("be_trigger"),
            "be_done": bool(r.get("be_done")),
            # ★ 2026-10-08（PR review C1）：原为 ``int(r.get("lots") or 1)``，
            #   而 ``0 or 1 == 1`` —— 熔断清仓（lots=0）会被这里**复活成 1**。
            #   显式区分「None（无值，取默认 1）」与「0（目标清仓）」。
            "lots": int(r["lots"]) if r.get("lots") is not None else 1,
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
            f"{_px_of(sym, r['entry'])}→{_px_of(sym, r['px'])} "
            f"{pnl} / {_fmt_money(r.get('pnl_yuan'))}")
    # 同 C1：0 是有意义的「清仓」，不能被 `or 1` 吞掉
    if (int(r["lots"]) if r.get("lots") is not None else 1) > 1:
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
        pnl_values = [r.get("pnl_yuan") for r in rows if r.get("pnl_yuan") is not None]
        if pnl_values:
            seg += f" ｜ 估算浮盈合计 **{_fmt_money(sum(pnl_values))}**"
            if len(pnl_values) < len(rows):
                seg += f"（{len(pnl_values)}/{len(rows)}个可估算）"
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
                if sig.get("pnl_yuan") is not None:
                    pnl_pct = f"{sig['pnl']:+.1%}" if sig.get("pnl") is not None else "-"
                    lines.append(
                        f"　　平仓估算盈亏 **{_fmt_money(sig['pnl_yuan'])}**（{pnl_pct}，未含费用）"
                    )
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
        lines.append("> 金额按信号价×合约乘数×手数估算，未含手续费/滑点，非实际成交账户盈亏。")
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
    from app.ingest.trading_window import is_hourly_collection_allowed
    if not is_hourly_collection_allowed():
        logger.info("[scheduler] B5 gate: 非交易日/非交易时段，跳过 hourly 定稿复核采集")
        return

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


def _fusion_params(base, key: str = "fusion_v34"):
    """把**策略注册表/config** 的 params 覆盖落到 FusionConfig 上。

    ★ 2026-11-06 接线（此前 `params_for` 生产零调用方，注册表参数无处可施）。

    口径（保证**默认逐位不变**）：
      · `params_for(key)` 默认返回 ``{}``（注册表 default_params 为空 + config 无
        `strategies` 段）→ 原样返回 `base`，生产行为与改动前**完全一致**；
      · 只有当 config 显式写了 ``strategies.<key>.params`` 才会覆盖；
      · 覆盖项必须是 FusionConfig **已有字段**（未知字段丢弃并记 error，
        避免 pydantic 静默接受拼错的参数名）；
      · 任何**非默认值**的覆盖都记 warning：融合参数是回测定稿口径，
        线上偏离必须由人确认已过回测闸门（"改配置即调参"≠"可以随便改"）。
    """
    from app.strategies.registry import params_for

    overrides = params_for(key)
    if not overrides:
        return base
    known = set()
    try:
        known = set(type(base).model_fields.keys())  # pydantic v2
    except Exception:  # noqa: BLE001 —— 非 pydantic 对象则退回 dir()
        known = {a for a in dir(base) if not a.startswith("_")}
    unknown = sorted(k for k in overrides if k not in known)
    if unknown:
        logger.error(
            f"[scheduler] strategies.{key}.params 含 FusionConfig **不存在**的字段 {unknown}"
            " → 已丢弃（防拼错参数名被静默忽略）")
        overrides = {k: v for k, v in overrides.items() if k in known}
    if not overrides:
        return base
    changed = {k: {"now": getattr(base, k, None), "new": v}
               for k, v in overrides.items() if getattr(base, k, None) != v}
    if changed:
        logger.warning(
            f"[scheduler] 融合参数被 config 覆盖：{changed} —— **已偏离回测定稿口径**，"
            "须自行确认已过回测闸门")
    try:
        return base.model_copy(update=overrides)
    except Exception:  # noqa: BLE001 —— 非 pydantic 对象则逐属性兜底
        for k, v in overrides.items():
            try:
                setattr(base, k, v)
            except Exception:  # noqa: BLE001
                logger.warning(f"[scheduler] 参数 {k} 覆盖失败（不可写）")
        return base


def _tradable_scan_pool(specs):
    """按交易白名单收口融合扫描池（CL-20261107-01）。

    背景（2026-11-07 实测）：白名单 38 / cloud.yaml 50 / L1 主连 82 / 融合信号 41
    四套口径并存，其中 12 个非白名单品种仍在产出信号、3 个白名单品种（BR/PF/PK）
    因不在配置里永远扫不到。

    三条安全设计
    ------------
    1. **fail-open**：开关关闭、表缺失、读取异常、白名单为空 → 一律返回原池并告警。
       宁可多扫，也不能因为一张参考表读不到就零信号。
    2. **已持仓豁免**：非白名单品种若当前有持仓（position <> 'FLAT'），继续纳入扫描，
       否则它既不会产出平仓/止损信号，持仓会永久冻结。语义是「不再开新仓，
       但存量持仓管理到平仓为止」。
    3. **可秒回退**：`cfg_feature_switch.tradable_filter_enabled=f` 即恢复全池扫描。

    Returns:
        过滤后的 MainContractSpec 列表（顺序保持不变）。
    """
    if not _switch_on("tradable_filter_enabled", "QH_TRADABLE_FILTER", default=True):
        logger.info("[scheduler] tradable_filter_enabled=off → 扫描池不做白名单收口")
        return specs
    try:
        with get_engine().connect() as conn:
            codes = {r[0] for r in conn.execute(text(
                "SELECT variety_code FROM l3_ref.tradable_variety "
                "WHERE effective_from <= CURRENT_DATE "
                "AND (effective_to IS NULL OR effective_to >= CURRENT_DATE)"))}
            held = {r[0] for r in conn.execute(text(
                "SELECT symbol FROM fusion_position "
                "WHERE coalesce(position, 'FLAT') <> 'FLAT'"))}
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[scheduler] 白名单/持仓读取失败 → 本轮不收口（fail-open）: {e}")
        return specs
    if not codes:
        logger.warning("[scheduler] 白名单为空 → 本轮不收口（fail-open，避免零信号）")
        return specs

    def _v(sym: str) -> str:
        return sym[:-3] if sym.endswith("888") else sym

    kept = [x for x in specs if _v(x.symbol) in codes or x.symbol in held]
    dropped = sorted({_v(x.symbol) for x in specs
                      if _v(x.symbol) not in codes and x.symbol not in held})
    if dropped:
        logger.info(
            f"[scheduler] 白名单收口：{len(specs)} → {len(kept)}，"
            f"跳过非白名单且无持仓的 {len(dropped)} 个：{dropped}")
    orphans = sorted({_v(x.symbol) for x in specs
                      if _v(x.symbol) not in codes and x.symbol in held})
    if orphans:
        logger.warning(
            f"[scheduler] 非白名单但仍有持仓，继续管理至平仓（不再开新仓）：{orphans}")
    missing = sorted(codes - {_v(x.symbol) for x in specs})
    if missing:
        logger.warning(f"[scheduler] 白名单品种不在主连配置（永远不产信号）：{missing}")
    return kept


def _v5x_scan_job() -> dict | None:
    """V5X.3 小时级信号扫描（C3-min：**只推送，不建单、不回写持仓**）。

    与 :func:`_fusion_scan_job` 的三点区别（2026-11-08，CL-20261108-08）：
      1. 信号跑在 **BA 后复权空间**，出口处反解回真实可成交价（``price_real``）；
         BA 价可为负（JD888 实测 -462），下游不得直接引用 BA 价下单。
      2. 日频门控用 **t-1 口径**（``gate_lag=1`` + ``regime_mode=strict``）：
         hvol / 簇冲击 / 体制三道门只吃 ≤ T-1 的日线聚合，全天恒定 ——
         这是小时级扫描唯一可复现的口径（当日口径要等收盘才算得出来）。
      3. 只写 ``public.v5x_signal_log``（去重 + 留痕）并推送；**不**碰
         ``execution_order`` 与 ``fusion_position``。持仓回写统一由
         :mod:`app.execution.position_sync` 收口并默认关闭（CL-20261108-26）。

    触发节奏：:data:`app.strategies.v5x.live.SCAN_MINUTES` = 每小时 :05 与 :35。
    **必须是两个时刻，不能只跑 :05**（2026-11-08 实测修正，CL-20261108-28）：
    真实 60m bar 的收盘标签不是整点（日盘 10:00/11:15/14:15/15:00，
    夜盘 22:00/23:00/00:00/01:00/02:00/02:30），只跑 :05 会让 14:15 那根
    在 15:05 已被 15:00 顶掉、在 14:05 又还没收盘 —— 永远不被评估。
    实证：2026-11-06 14:15 的 FU888 平仓信号在 :05-only 下从未产出。

    时段门用 :func:`app.ingest.trading_window.is_hourly_collection_allowed`
    而不是 :func:`is_trading_day`：夜盘跨午夜（周五 21:00→周六 02:30）时，
    按日历日判断会把 00:00-02:30 的 bar 全部误杀。该函数已专门处理这个边界。

    开关 ``cfg_feature_switch.v5x_live_enabled``（migrations/059，2026-11-08 开启），
    关闭时整轮不跑（不采集、不评估、不推送）。
    """
    if not _switch_on("v5x_live_enabled", "QH_V5X_LIVE", default=False):
        logger.debug("[scheduler] v5x_live_enabled=off → 跳过 V5X 扫描")
        return
    now = datetime.now(_SH_TZ)

    from app.ingest.trading_window import is_hourly_collection_allowed
    if not is_hourly_collection_allowed(now):
        logger.info(f"[scheduler] v5x scan {now:%m-%d %H:%M} 非交易时段，跳过")
        return

    # 扫描节奏常量与扫描器同源（不在这里另抄一份，否则两处会漂）
    from app.strategies.v5x.live import RETRY_WINDOW_HOURS, SCAN_MINUTES, run_v5x_scan

    if now.minute not in SCAN_MINUTES:
        logger.debug(f"[scheduler] v5x scan {now:%H:%M} 非扫描时刻，跳过")
        return

    try:
        with session_scope() as s:
            res = run_v5x_scan(s, now=now)
    except Exception as e:  # noqa: BLE001
        # ★ 必须**外抛**，不能吞（CL-20261109-06）。
        #   ops.scheduled_task_run 的 success 判据是 apscheduler 有没有收到异常
        #   （EVENT_JOB_EXECUTED = 没抛 = success）。原实现只 logger.exception
        #   然后返回，等于「失败静默」—— 2026-11-07 的 pgconn.py 误删事故就是
        #   这么让 9 个脚本连续 ImportError 而流水全绿的。
        #   至于注释里原先担心的「影响同一分钟触发的 fusion_scan」：apscheduler
        #   的作业互相隔离，一个 job 抛异常只改它自己的 run 记录；且 V3.4 已停用
        #   （fusion_v34_live_enabled=f），不存在互相拖累。
        logger.exception(f"[scheduler] v5x scan 失败：{e}")
        raise

    logger.info(f"[scheduler] v5x scan 完成：{res}")

    # 有信号但没送达 → 升级为任务失败。否则「推送挂了」和「本轮没信号」
    # 在监控和用户侧完全不可区分（CL-20261109-06）。
    if res.get("n_unsent") and not res.get("pushed"):
        raise RuntimeError(
            f"V5X {res['n_unsent']} 条信号已落库但推送失败（via={res.get('via')}）——"
            f"数据在 v5x_signal_log（notify_ok=false），{RETRY_WINDOW_HOURS:g}h 内下一轮"
            f"会自动补发；持续失败请查 PUSHPLUS_TOKEN / fusion.fallback_webhook / 网络"
        )
    return res


def _v5x_snapshot_job() -> dict | None:
    """V5X 理论持仓快照（只读 + 推送）— CL-20261109-08。

    为什么需要它：V5X 是**无状态回放**扫描器，只推「当根 bar 发生了什么事件」，
    从不回答「按规则现在应该持有什么」。2026-11-09 首次实盘前实测：
    按 11-07 末根重放，理论上持有 5 个多头（A888/P888/SF888/SS888/Y888），
    而用户手上一个都没有 —— 于是之后推这 5 个的 ``CLOSE`` 时用户无仓可平，
    统计与实盘从此永久错位，且没有任何地方会提醒。

    三条约束：
      1. **只读**：不写 ``execution_order`` / ``execution_position`` /
         ``fusion_position`` / ``v5x_signal_log``；
      2. 复用 :func:`app.strategies.v5x.live._replay_orders` 的唯一重放路径，
         不另写循环（两套重放必然分叉）；
      3. 推送失败**外抛** —— 与 ``_v5x_scan_job`` 同一条纪律（CL-20261109-06）：
         ``ops.scheduled_task_run`` 的 success 判据是「作业有没有抛异常」。
    """
    if not _switch_on("v5x_live_enabled", "QH_V5X_LIVE", default=False):
        logger.debug("[scheduler] v5x_live_enabled=off → 跳过持仓快照")
        return
    from app.strategies.v5x.snapshot import push_snapshot

    try:
        res = push_snapshot()
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] v5x 持仓快照失败：{e}")
        raise
    logger.info(f"[scheduler] v5x 持仓快照完成：{res}")
    return res


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
    try:
        settings = get_settings()
        f = settings.fusion
        if not f.enabled:
            return
        # ---- 生产推送源单源开关（CL-20261108-27，2026-11-08 用户拍板）----
        # V3.4 与 V5X.3 的持仓假设互不相容（V3.4 维护 fusion_position 意图台账，
        # V5X 无状态回放、不认台账），同时推送必然出现「一边开多、一边平空」。
        # 故默认关闭 V3.4，生产推送源唯一化为 V5X.3。
        #
        # 为什么在函数开头就 return，而不是让作业不注册：
        #   · 保留回切能力（改一行开关即恢复，不必回滚代码/重启注册表）；
        #   · 作业仍被调度 = 每分钟仍有一条可观测的「它在但没跑」的痕迹；
        #   · DB 字典（ops.scheduled_task）里的启用位另有一套语义（移除作业），
        #     两者叠加时以字典为准——此处开关只表达「策略是否在生产推送」。
        #
        # 一并停掉的还有本作业内的 `_collect_hourly_settled`（akshare 小时线定稿复核）。
        # 这条采集服务的是 V3.4 自己读的 `hourly_bar`；V5X 读 l2_adj.bar_60m_ba，
        # 由 minute_and_bars(:07/:37) 合成 + back_adjusted_rebuild 校准，
        # 且 akshare 原始小时线另有独立作业 hourly_collect(`0 * * * *`) 在采。
        # 所以停 V3.4 不会让任何一条供数链断掉（三链互相独立）。
        if not _switch_on("fusion_v34_live_enabled", "QH_FUSION_V34_LIVE", default=False):
            logger.debug("[scheduler] fusion_v34_live_enabled=off → V3.4 不评估、不推送")
            return
        # 「start」日志放在开关之后：否则每 30 分钟都打一条 start 再静默 return，
        # 日志看起来像在跑，实际一行没评估 —— 正是最费时间的误判来源。
        logger.info("[scheduler] fusion scan start")
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
            #    ★ 2026-11-06：改为**策略注册表驱动**。此前这里硬编码调 evaluate_all，
            #      注册表里的策略与"实际跑的东西"毫无关联 —— all_specs/params_for/
            #      enabled_keys 三个函数**生产零调用方**，注册表形同虚设。
            #      现由注册表决定"本轮跑哪些策略 + 用什么参数"：
            #        · fusion_v34 不在候选 → 本轮**不产出融合信号**（明写日志，不静默）；
            #        · 有"已启用但未接入信号链"的策略 → 记 warning 点名（不静默空转）；
            #        · 参数默认逐位不变（params_for 默认返回空 dict），仅当 config 显式
            #          写了 strategies.<key>.params 才会覆盖，并**必须**自行确认已过回测闸门。
            from app.strategies import registry as _registry

            _scan_specs = _registry.scan_candidates()
            _scan_keys = [s.key for s in _scan_specs]
            if "fusion_v34" not in _scan_keys:
                logger.warning(
                    "[scheduler] fusion scan 跳过：注册表未启用/未匹配主周期的 fusion_v34"
                    f"（候选={_scan_keys}）→ 本轮不产出融合信号")
                return
            _unwired = _registry.unwired_keys()
            if _unwired:
                logger.warning(
                    f"[scheduler] 注册表存在已启用但**未接入信号链**的策略：{_unwired}"
                    "（仅登记，不产生任何信号——需显式接线）")
            results = evaluate_all(s, _tradable_scan_pool(settings.main_contracts),
                                   _fusion_params(settings.fusion, "fusion_v34"))

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
                # ★ 2026-10-08：阈值按「当前是否在交易时段」切换（见 OFFHOURS_STALE_MINUTES）。
                #   修的是每天 11:30~12:00 / 14:30~15:30 静默无信号：那些时段本就无新
                #   bar（收盘标签 + 未收盘棒丢弃），age 自然 > stale_minutes=90，
                #   于是状态变化被误判为"数据陈旧"而抑制。
                _eff_stale, _why = _effective_stale_minutes(now, f.stale_minutes)
                fresh = (age_min is not None) and (-10.0 <= age_min <= _eff_stale)

                # ---------- 部署侧隐患修复（2026-11-01）：持仓超 max_hold_days 强制平仓+再入场 ----------
                # 在线引擎每轮只重算最近 max_bars(800) 根；若行情持续同向，eng 在滚动窗口内恒为同向、
                # db 与 eng 长期一致 → entry_at 冻结、永不重估 → 强趋势下"持11年不平仓"。超时则强制
                # eng=FLAT，复用下方 db!=eng 分支生成平仓信号；下一轮若趋势仍在则正常再入场。
                # 仅在数据新鲜时执行，避免用过期/异常 K 误平。
                if (f.max_hold_days > 0 and db != "FLAT" and old is not None
                        and old.entry_at is not None and fresh):
                    _held_days = (now_utc - old.entry_at).total_seconds() / 86400.0
                    if _held_days > f.max_hold_days:
                        logger.info(
                            f"[fusion] {sym} 持仓 {_held_days:.0f}天 > {f.max_hold_days}天上限，"
                            f"强制平仓+再入场（防持11年不平仓）")
                        eng = "FLAT"

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
                    old_lots = int(old.lots) if (old is not None and old.lots is not None) else 1
                    pnl_yuan = (_pnl_yuan(sym, db, db_entry, px, old_lots)
                                if db != "FLAT" else None)
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
                        "pnl_yuan": pnl_yuan,
                        "lots": old_lots,
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
                lots_now = int(r["lots"]) if r.get("lots") is not None else 1
                lots_prev = int(old.lots) if old.lots is not None else 1
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
            #    周末、长假都不该让持仓从清单里消失；信号仍受 _eff_stale 约束）
            #    ★ 2026-10-08：展示阈值同样按时段兜底 —— 午休/隔夜 age 可达 120~1000
            #    分钟，若沿用 display_max_age_min 之外的固定值会把持仓清空。
            _disp_stale = max(f.display_max_age_min,
                              _effective_stale_minutes(now, f.stale_minutes)[0])
            rows = _heartbeat_rows(
                results, names, now, _disp_stale,
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
        _require_subprocess_success(proc, "cloud_local_sync")
    except subprocess.TimeoutExpired:
        logger.warning("[scheduler] cloud_local_sync timeout>7200s")
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("[scheduler] cloud_local_sync failed")
        raise


#: 调度以 subprocess 方式调用的生产脚本（相对仓库根的 scripts/）。
#: 2026-09-29：历史上因路径 parents[2] 拼错 + .dockerignore 排除 runtime/，
#: 这 5 个作业**每天静默失败**（日志仅一行 error，无人看）。此处集中登记，
#: 启动时 fail-fast 校验，缺一个就大声报错，杜绝再次静默。
SUBPROCESS_SCRIPTS = (
    "sync_cloud_local.py",
    # Phase 4（因子层）：缺任一都会在 16:20/16:45 静默失败，故一并纳入启动自检
    "compute_factor_v1v6.py",
    "factor_ic_monitor.py",
    # Phase 4/6（准入裁决 / 迁移执行）：人工与部署入口
    "admit_factors.py",
    "db_apply_migrations.py",
    # R3：仓单日报采集（scrapling 浏览器版，四所）；依赖镜像 WITH_SCRAPLING=1 装好 scrapling+chromium
    "collect_warehouse_receipt.py",
    # 2026-10-27：futures_rule（保证金率/涨跌停/合约乘数）此前**没有任何调度**，纯靠人工播种；
    # 最后一次 2026-10-08 人工执行后停更 19 天，导致盘前门"规则过期"判 UNKNOWN 阻塞、
    # G4 交易日历退化为二义判定。脚本自带 max(trade_date)+1 续跑且按日幂等，故纳入每日调度
    # + 启动自检，杜绝再次静默停滞。
    "seed_futures_rule.py",
    # 2026-10-28 同类排查（"还有没有第二个 futures_rule"）：按数据新鲜度扫全库，
    # daily_bar/contract_daily/hourly_bar/spot_basis/warehouse_receipt/inventory 全部新鲜
    # （10-27/10-28），唯独字典与采集类中 3 张滞后，且均无调度、与 futures_rule 同属
    # "纯手工家族"：
    #   roll_yield     最新 2026-09-30，停更 27 天  -> collect_roll_yield.py（每日补采）
    #   dim_trading_cost 最新 updated_at 2026-10-08，停更 19 天（与 futures_rule 同一时刻停摆）
    #                                                -> sync_cost_from_exchange.py（每周 --apply）
    #   dim_variety_tick_history 由 rebuild_dict_from_akshare.py 写入（每周 dry-run 漂移检测，
    #                                                刻意不 --apply，理由见作业 docstring）
    "collect_roll_yield.py",
    "sync_cost_from_exchange.py",
    "rebuild_dict_from_akshare.py",
    # CL-20261107-01 S5：月度归档（子目录，路径相对 scripts/）。
    # 缺了不会报错、只会每月静默少一份归档，所以同样纳入启动自检。
    "ops/archive_nontradable.py",
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


def _seed_futures_rule_job() -> None:
    """每日补齐 futures_rule（保证金率 / 涨跌停板 / 合约乘数）。

    事故背景（2026-10-27）：本作业此前**不存在**，播种全靠人工；最后一次人工执行
    2026-10-08 后停更 19 天，盘前门因此把"规则过期"判为 UNKNOWN 而阻塞，G4 交易日历
    也退化为二义判定。故补每日调度。

    幂等性：脚本内部先取 `max(trade_date)`，从其后一天续跑到今天；已播种的日期
    `day_already_seeded()` 直接跳过。因此每日重跑安全、可重复。

    回看窗口：默认 14 天（SEED_RULE_LOOKBACK_DAYS），避免库被清空时从 2015 年全量重扫。
    """
    import sys as _sys
    from datetime import date as _date, timedelta as _td

    script = _scripts_dir() / "seed_futures_rule.py"
    if not script.is_file():
        logger.error(f"[scheduler][ASSET] 找不到播种脚本 {script}")
        return
    lb = int(os.getenv("SEED_RULE_LOOKBACK_DAYS", "14"))
    since = (_date.today() - _td(days=lb)).isoformat()
    cmd = [_sys.executable, str(script), "--since", since]
    logger.info(f"[scheduler] seed_futures_rule start: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        logger.info(f"[scheduler] seed_futures_rule exit={proc.returncode} "
                    f"tail={(proc.stdout or '')[-600:]}")
        _require_subprocess_success(proc, "seed_futures_rule")
    except subprocess.TimeoutExpired:
        logger.warning("[scheduler] seed_futures_rule timeout>3600s")
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("[scheduler] seed_futures_rule failed")
        raise


def _collect_roll_yield_job() -> None:
    """每日补采 roll_yield（展期收益）。

    事故背景（2026-10-28 全库新鲜度排查）：roll_yield 最新只到 **2026-09-30**（停更 27 天），
    而 daily_bar / contract_daily / hourly_bar / spot_basis / warehouse_receipt / inventory
    全部停在 2026-10-27 —— 即主链路采集正常，**唯独 roll_yield 这一路从来没有调度**，
    与 futures_rule 同属"纯手工、无调度"家族。

    幂等性：app.ingest.roll_yield 内部 ``ON CONFLICT (report_date, symbol, version)
    DO UPDATE``，重复跑同一天只会覆盖为同值，安全。

    回看窗口：默认从 ``max(report_date) + 1`` 续跑；查库失败时回退
    ROLL_YIELD_LOOKBACK_DAYS（默认 30 天）。**刻意不使用脚本默认的 2018-01-01**——
    那会按交易日×品种全量重扫（约 2000 天 × 40 品种 × sleep，单次即数十分钟），
    放进每日调度窗口会挤占正常采集。
    """
    import sys as _sys
    from datetime import date as _date, timedelta as _td

    script = _scripts_dir() / "collect_roll_yield.py"
    if not script.is_file():
        logger.error(f"[scheduler][ASSET] 找不到脚本 {script}")
        return
    lb = int(os.getenv("ROLL_YIELD_LOOKBACK_DAYS", "30"))
    start = (_date.today() - _td(days=lb)).isoformat()
    try:
        from sqlalchemy import text as _text

        from app.core.db import session_scope

        with session_scope() as s:
            mx = s.execute(_text("SELECT max(report_date) FROM roll_yield")).scalar()
        if mx is not None:
            start = (mx + _td(days=1)).isoformat()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[scheduler] roll_yield 读 max(report_date) 失败，回退 {lb} 天窗口: {e}")
    today = _date.today().isoformat()
    if start > today:
        logger.info(f"[scheduler] roll_yield 已最新（start={start} > today），跳过")
        return
    cmd = [_sys.executable, str(script), "--start", start, "--end", today,
           "--sleep", os.getenv("ROLL_YIELD_SLEEP", "0.5")]
    logger.info(f"[scheduler] collect_roll_yield start: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        logger.info(f"[scheduler] collect_roll_yield exit={proc.returncode} "
                    f"tail={(proc.stdout or '')[-600:]}")
        _require_subprocess_success(proc, "collect_roll_yield")
    except subprocess.TimeoutExpired:
        logger.warning("[scheduler] collect_roll_yield timeout>3600s")
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("[scheduler] collect_roll_yield failed")
        raise


def _sync_trading_cost_job() -> None:
    """每周同步 dim_trading_cost（手续费字典）。

    事故背景（2026-10-28 排查）：dim_trading_cost 的 ``max(updated_at)`` 停在
    **2026-10-08 15:05** —— 与 futures_rule 停更**同一天、几乎同一时刻**，可判定为
    同一次人工维护后再无人接手。该表是回测成本与盘前门保证金的口径来源，手续费变更
    （交易所通知 / 官网调费）漏采会让成本被系统性低估。

    执行方式：``sync_cost_from_exchange.py --apply``。不带 --apply 时为 dry-run，
    不会落库。脚本内部按 (品种, 动作, scope) UPSERT 并带 source 标记，可重复执行。

    频率取"每周"而非"每日"：手续费由交易所通知驱动，变更低频；且该脚本要抓郑商所官网，
    每日跑属无谓的外部请求压力。可用 SYNC_COST_CRON_* 覆盖。
    """
    import sys as _sys

    script = _scripts_dir() / "sync_cost_from_exchange.py"
    if not script.is_file():
        logger.error(f"[scheduler][ASSET] 找不到脚本 {script}")
        return
    cmd = [_sys.executable, str(script), "--apply"]
    logger.info(f"[scheduler] sync_cost_from_exchange start: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        logger.info(f"[scheduler] sync_cost_from_exchange exit={proc.returncode} "
                    f"tail={(proc.stdout or '')[-800:]}")
        _require_subprocess_success(proc, "sync_cost_from_exchange")
    except subprocess.TimeoutExpired:
        logger.warning("[scheduler] sync_cost_from_exchange timeout>1800s")
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("[scheduler] sync_cost_from_exchange failed")
        raise


def _rebuild_dict_dryrun_job() -> None:
    """每周 dry-run 比对 akshare 权威口径与库内字典（**只报告，不落库**）。

    覆盖对象含 dim_variety_tick_history（最小变动价位）与 dim_trading_cost 固定值手续费。
    这两张表 2026-10-28 排查时分别停在 updated_at 2026-10-03 与 2026-10-08。

    ⚠ **刻意不加 --apply**：rebuild_dict_from_akshare 会用 akshare 口径**覆盖**已有
    有效行，而库内存在用户已裁定的值——
      · BZ/EB/PL 采用第三方文档基准（sync_cost_from_exchange.py 内 doc_rows）
      · 百分比(‰)手续费以交易所通知为权威（akshare 对 40 个百分比品种开仓费用恒为
        0.01，误用会让 49% 品种手续费近零）
    自动覆盖会静默抹掉这些裁定，风险远大于收益。故本作业只做**漂移检测**，
    有差异时输出 WARNING，由人工判断后再手动执行 --apply。
    """
    import sys as _sys

    script = _scripts_dir() / "rebuild_dict_from_akshare.py"
    if not script.is_file():
        logger.error(f"[scheduler][ASSET] 找不到脚本 {script}")
        return
    cmd = [_sys.executable, str(script)]  # 无 --apply：仅 dry-run
    logger.info(f"[scheduler] rebuild_dict(dry-run) start: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        out = (proc.stdout or "")
        logger.info(f"[scheduler] rebuild_dict(dry-run) exit={proc.returncode} tail={out[-800:]}")
        _require_subprocess_success(proc, "rebuild_dict(dry-run)")
    except subprocess.TimeoutExpired:
        logger.warning("[scheduler] rebuild_dict(dry-run) timeout>1800s")
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception("[scheduler] rebuild_dict(dry-run) failed")
        raise


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
        _apply_task_registry(sched)
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
    #
    # ⚠ **DB 覆盖语义陷阱（2026-11-07 实证，必读）**：本处 ``day_of_week="sat"`` 会被
    #   ``ops.scheduled_task.schedule`` 的 5 段 cron 覆盖（见 _apply_task_registry /
    #   resync_jobs_from_db），而 ``CronTrigger.from_crontab`` 的星期语义是
    #   **0=周一 … 6=周日**（与标准 crontab 的 0=周日/6=周六**错位一天**）。
    #   故「周六」在 DB 里必须写 **5**，写 6 会静默变成周日跑：
    #   实测 2026-11-07 前该字典值为 ``0 6 * * 6`` → 实际在**周日** 06:00 触发
    #   （10-18/10-25/11-01 三条成功记录全是周日），与代码注释/日志 "sat" 相反。
    #   已于 2026-11-07 修正为 ``0 6 * * 5``。改这类字典值后务必用
    #   ``CronTrigger.from_crontab(expr).get_next_fire_time()`` 打印真实星期验收，
    #   **不要**看 ``registered cron sat ...`` 日志（那只是硬编码字符串）。
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

    # S5 非白名单历史月度归档（CL-20261107-01）：每月 1 日 03:20，只导出不删。
    # 早于 06:30 的月更，磁盘最空；纯读操作，与分钟管线 :07/:37 不冲突。
    sched.add_job(
        _archive_nontradable_job,
        trigger=CronTrigger(day=1, hour=3, minute=20, timezone=settings.env.TZ),
        id="archive_nontradable",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=7200,
    )
    logger.info("[scheduler] registered cron day1 03:20 (archive_nontradable)")

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
        # 注册 ≠ 会跑。真正的实盘开关在 job 内部（CL-20261108-27）：
        # 关掉时整轮直接 return，日志只有 debug，所以启动时**必须**把当前推送源说清楚，
        # 否则「作业在跑但永远不出信号」是最难排查的一类静默故障。
        if _switch_on("fusion_v34_live_enabled", "QH_FUSION_V34_LIVE", default=False):
            logger.info("[scheduler] V3.4 实盘推送 = 开启（fusion_v34_live_enabled=true）")
        else:
            logger.warning(
                "[scheduler] V3.4 实盘推送 = 关闭（fusion_v34_live_enabled=false）→ "
                "fusion_scan 每轮空跑，生产推送源为 V5X.3（v5x_live_enabled）")

    # V5X.3 小时级信号扫描（C3-min，CL-20261108-08）：每小时 :05 与 :35。
    # **无条件注册**，由 job 内部读 cfg_feature_switch.v5x_live_enabled 决定跑不跑 ——
    # 这样打开/关闭开关**不需要重启调度器**（该开关见 migrations/059）。
    # 2026-11-08 起它是**唯一**生产推送源（V3.4 已由 CL-20261108-27 停用），
    # 但两个作业仍物理并存：V3.4 保留代码与开关，随时可回切。
    # ★ 节奏必须与 SCAN_MINUTES 同源（:05/:35），只跑 :05 会漏掉 11:15 / 14:15
    #   这两根非整点收盘的日盘 bar（CL-20261108-28 实证）。
    from app.strategies.v5x.live import SCAN_MINUTES as _V5X_MINUTES

    sched.add_job(
        _v5x_scan_job,
        trigger=CronTrigger(minute=",".join(str(m) for m in _V5X_MINUTES),
                            timezone=settings.env.TZ),
        id="v5x_scan",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=1800,
    )
    logger.info("[scheduler] registered v5x_scan at "
                + "/".join(f":{m:02d}" for m in _V5X_MINUTES)
                + " hourly (switch v5x_live_enabled)")

    # V5X 理论持仓快照（CL-20261109-08）：日盘收盘后 15:05 推一份「按规则现在
    # 应该持有什么」。V5X 是无状态回放，只推事件不推持仓 —— V3.4 停用后
    # 「盘前预播报」承担的持仓告知整体消失，用户漏看一条 CLOSE 就与统计永久错位。
    # 只读引擎内部状态，不写任何表（与已关闭的持仓同步 CL-20261108-26 无关）。
    sched.add_job(
        _v5x_snapshot_job,
        trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=5,
                            timezone=settings.env.TZ),
        id="v5x_snapshot",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=1800,
    )
    logger.info("[scheduler] registered v5x_snapshot at 15:05 mon-fri")

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

    # P1-1：后复权物化 bar_*_ba 定期重建（工单 #8，依赖 l2_adj.roll_segment + l1_mkt.bar_*）。
    # 错峰在分钟管线 :07/:37 之后 18 分钟，确保 bar_* 已落盘再 as-of join，避免读未提交快照。
    # sp_build_l2_back_adjusted 自带触发签名（price_shift / Σcum_offset / Σepoch(seg_start) / 段数），
    # roll_segment 任一项变化自动全量重建，否则仅增量补最新桶（廉价）。本实例为同步专用
    # （CLOUD_SYNC_ENABLED=1）时已在上方早退分支，不会注册——由云端调度器运行。
    sched.add_job(
        _back_adjusted_job,
        trigger=CronTrigger(hour="2,11,15", minute="55", timezone=settings.env.TZ),
        id="back_adjusted_rebuild",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=900,
    )
    logger.info("[scheduler] registered back_adjusted_rebuild at 02:55/11:55/15:55 "
                "(post bars; depends roll_segment + bar_*)")

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

    # ★ 2026-11-06 新增（接线批次）：A/B/C 组因子日计算 + 会员持仓汇总。
    #   此前 scripts/compute_factor_value.py 与 scripts/collect_member_rank_summary.py
    #   **从未被调度** → 22 个已注册因子实测全部停在 2026-09-30。
    #   顺序（依赖链，硬约束）：
    #     16:20 factor_v1v6_daily（既有）
    #     16:35 factor_value_daily   （A 组 11 因子；依赖 bar_15m，16:20 已就绪）
    #     16:45 factor_ic_monitor_daily（既有；能看到 16:35 的 A 组产出）
    #     17:30 rank_position（既有）
    #     17:45 collect_member_rank_summary_daily（依赖 rank_position）
    #     18:00 factor_value_b_daily （B 组 11 + C 组 2；依赖 17:45 的 summary）
    if _switch_on("factor_value_enabled", "FACTOR_VALUE_ENABLED", default=True):
        sched.add_job(
            _factor_value_job,
            trigger=CronTrigger(hour=16, minute=35, timezone=settings.env.TZ),
            id="factor_value_daily",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        logger.info("[scheduler] registered cron 16:35 (factor_value_daily / A 组)")

    if _switch_on("member_rank_summary_enabled", "MEMBER_RANK_SUMMARY_ENABLED",
                  default=True):
        sched.add_job(
            _collect_member_rank_summary_job,
            trigger=CronTrigger(hour=17, minute=45, timezone=settings.env.TZ),
            id="collect_member_rank_summary_daily",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=1800,
        )
        logger.info("[scheduler] registered cron 17:45 "
                    "(collect_member_rank_summary_daily)")

    if _switch_on("factor_value_enabled", "FACTOR_VALUE_ENABLED", default=True):
        sched.add_job(
            _factor_value_b_job,
            trigger=CronTrigger(hour=18, minute=0, timezone=settings.env.TZ),
            id="factor_value_b_daily",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        logger.info("[scheduler] registered cron 18:00 (factor_value_b_daily / B+C 组)")

    # ★ 2026-11-07 新增：因子级新鲜度巡检 + 自动补算（依赖链末端）。
    #   排在 19:10 collect_roll_yield 之后、20:00 pre_night 之前。
    if _switch_on("factor_freshness_enabled", "FACTOR_FRESHNESS_ENABLED", default=True):
        _ffh = int(os.getenv("FACTOR_FRESHNESS_CRON_HOUR", "19"))
        _ffm = int(os.getenv("FACTOR_FRESHNESS_CRON_MINUTE", "30"))
        sched.add_job(
            _factor_freshness_job,
            trigger=CronTrigger(hour=_ffh, minute=_ffm, timezone=settings.env.TZ),
            id="factor_freshness_daily",
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=3600,
        )
        logger.info(f"[scheduler] registered cron {_ffh:02d}:{_ffm:02d} "
                    f"(factor_freshness_daily / 因子级巡检+自动补算)")

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

    # 字典周期再同步作业：定期把 ops.scheduled_task.schedule 变更同步到内存作业，
    # 免重启即时生效（详见 app.ops.task_registry.resync_jobs_from_db）。
    # 默认每 5 分钟一次；若 DB 已登记 scheduler_resync 行，下方的 apply_registry_to_scheduler
    # 会在启动时按 DB schedule 覆盖此默认值（同样无需改代码）。
    try:
        from apscheduler.triggers.cron import CronTrigger as _Cron
        from app.ops.task_registry import resync_jobs_from_db

        sched.add_job(
            resync_jobs_from_db,
            trigger=_Cron.from_crontab("*/5 * * * *", timezone=settings.env.TZ),
            id="scheduler_resync",
            args=[sched],
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            misfire_grace_time=300,
        )
        logger.info(
            "[scheduler] registered scheduler_resync (every 5min, DB-driven)"
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[scheduler] scheduler_resync 注册失败: {e}")

    # ---- P0 盘前作战门（2026-10-27，migrations/044）------------------------
    # 三个时点对应三段交易：早盘 08:30 / 午盘 12:55 / 夜盘 20:30。
    # 比既有盘前播报（08:45/13:15/20:45）早 15 分钟，留出"看到红灯还能改单"的时间窗。
    # 默认关闭（PREOPEN_GATE_ENABLED=1 开启）：未配置 account_equity 时只会判 UNKNOWN，
    # 开了也刷屏，故必须显式启用。
    try:
        from app.risk.preopen_gate import GateConfig as _GateCfg
        if _GateCfg().enabled:
            accts = [x.strip() for x in
                     (os.getenv("PREOPEN_ACCOUNTS", "") or "").split(",") if x.strip()]
            if not accts:
                logger.warning("[scheduler] PREOPEN_GATE_ENABLED=1 但 PREOPEN_ACCOUNTS 为空，"
                               "跳过盘前门注册")
            else:
                for _hm, _kind in (("08:30", "day"), ("12:55", "day"), ("20:30", "night")):
                    _h, _mi = (int(x) for x in _hm.split(":"))
                    sched.add_job(
                        _preopen_gate_job,
                        trigger=CronTrigger(hour=_h, minute=_mi, timezone=settings.env.TZ),
                        id=f"preopen_gate_{_kind}_{_hm.replace(':', '')}",
                        args=[accts, _kind],
                        replace_existing=True,
                        max_instances=1,
                        coalesce=True,
                        misfire_grace_time=600,
                    )
                logger.info(f"[scheduler] registered preopen_gate 08:30/12:55/20:30 "
                            f"for {len(accts)} account(s)")
        else:
            logger.info("[scheduler] preopen_gate 停用（PREOPEN_GATE_ENABLED!=1）")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] preopen_gate 注册失败: {e}")

    # futures_rule 每日播种（保证金率/涨跌停/合约乘数）。
    # 时刻必须**早于 08:30 盘前门**：门会校验"规则是否过期"，若本作业晚于门运行，
    # 门读到的是昨日规则 → 判 UNKNOWN 阻塞。2026-10-27 前此作业根本不存在，
    # 播种纯靠人工，最后一次 2026-10-08 后停更 19 天。
    _srh = int(os.getenv("SEED_RULE_CRON_HOUR", "8"))
    _srm = int(os.getenv("SEED_RULE_CRON_MINUTE", "0"))
    sched.add_job(
        _seed_futures_rule_job,
        trigger=CronTrigger(hour=_srh, minute=_srm, timezone=settings.env.TZ),
        id="seed_futures_rule",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=1800,
    )
    logger.info(f"[scheduler] registered seed_futures_rule at {_srh:02d}:{_srm:02d} "
                f"(须早于盘前门 08:30)")

    # ---- 2026-10-28 同类治理：把另外 3 个"纯手工、无调度"的字典/采集脚本纳入调度 ----
    # 排查方法：按各表业务日期/updated_at 做全库新鲜度扫描。主数据链路（daily_bar /
    # contract_daily / hourly_bar / spot_basis / warehouse_receipt / inventory）全部新鲜，
    # 滞后的只有字典类——它们共同点是"只在人工想起时才跑一次"。
    # 19:10 而非 18:40：18:40 已有 warehouse_receipt（scrapling 浏览器抓取，最重），
    # 且 18:10 的 daily_futures_refresh 刚刷新完 daily_bar（本作业的交易日列表取自它）。
    _ryh = int(os.getenv("ROLL_YIELD_CRON_HOUR", "19"))
    _rym = int(os.getenv("ROLL_YIELD_CRON_MINUTE", "10"))
    sched.add_job(
        _collect_roll_yield_job,
        trigger=CronTrigger(hour=_ryh, minute=_rym, timezone=settings.env.TZ),
        id="collect_roll_yield",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    logger.info(f"[scheduler] registered collect_roll_yield at {_ryh:02d}:{_rym:02d} "
                f"(停更 27 天治理；从 max(report_date)+1 续跑)")

    _sch = int(os.getenv("SYNC_COST_CRON_HOUR", "3"))
    _scm = int(os.getenv("SYNC_COST_CRON_MINUTE", "20"))
    _scd = os.getenv("SYNC_COST_CRON_DOW", "sun")
    sched.add_job(
        _sync_trading_cost_job,
        trigger=CronTrigger(day_of_week=_scd, hour=_sch, minute=_scm, timezone=settings.env.TZ),
        id="sync_trading_cost",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    logger.info(f"[scheduler] registered sync_trading_cost at {_scd} {_sch:02d}:{_scm:02d} "
                f"(dim_trading_cost 停更 19 天治理)")

    _rdh = int(os.getenv("REBUILD_DICT_CRON_HOUR", "3"))
    _rdm = int(os.getenv("REBUILD_DICT_CRON_MINUTE", "40"))
    _rdd = os.getenv("REBUILD_DICT_CRON_DOW", "sun")
    sched.add_job(
        _rebuild_dict_dryrun_job,
        trigger=CronTrigger(day_of_week=_rdd, hour=_rdh, minute=_rdm, timezone=settings.env.TZ),
        id="rebuild_dict_dryrun",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,
    )
    logger.info(f"[scheduler] registered rebuild_dict_dryrun at {_rdd} {_rdh:02d}:{_rdm:02d} "
                f"(只做漂移检测，不 --apply)")

    _apply_task_registry(sched)
    return sched


def _preopen_gate_job(accounts: list[str], session_kind: str) -> None:
    """盘前门作业：逐账户跑一次并推送。任何异常只记日志（旁路，不拖垮调度）。"""
    from datetime import date as _date

    from app.core.db import session_scope
    from app.risk import preopen_gate as _pg

    with session_scope() as s:
        for acct in accounts:
            try:
                res = _pg.run_gate(s, account=acct, trade_date=_date.today(),
                                   session_kind=session_kind)
                _pg.persist(s, res)
                _pg.push(res)
                _u = f"{res['now'].usage:.1%}" if res["now"] else "n/a"
                logger.info(f"[preopen] {acct} {session_kind} → {res['verdict']} 占用 {_u}")
            except Exception as e:  # noqa: BLE001
                logger.exception(f"[preopen] {acct} 执行失败: {e}")


def _apply_task_registry(sched: BlockingScheduler) -> None:
    """把 ops.scheduled_task 字典控制应用到调度器（2026-10-14，迁移 043）。

    ① 字典 enabled=false 的作业移除（不注册，日志显式说明——绝不静默）；
    ② 挂运行流水监听：全部作业的 SUBMITTED/EXECUTED/ERROR/MISSED 落
       ops.scheduled_task_run（不依赖各作业自己写 task_run）。

    字典表不可用（迁移未应用/库不通）时按原样注册并 WARNING，不阻断启动。
    """
    try:
        from app.ops.task_registry import apply_registry_to_scheduler

        apply_registry_to_scheduler(sched)
    except Exception as e:  # noqa: BLE001 —— 记账/字典基础设施故障不得拖垮调度器
        logger.warning(f"[scheduler] task_registry 应用失败（按原样注册）: {e}")


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
        _require_subprocess_success(proc, "factor_v1v6")
    except subprocess.TimeoutExpired:
        logger.warning("[scheduler] factor_v1v6 timeout>3600s")
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] factor_v1v6 failed: {e}")
        raise


def _factor_ic_monitor_job() -> None:
    """因子滚动 IC 监控（T21）：写入 factor_ic_roll，异常因子进告警清单。"""
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
        _require_subprocess_success(proc, "factor_ic_monitor")
    except subprocess.TimeoutExpired:
        logger.warning("[scheduler] factor_ic_monitor timeout>3600s")
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] factor_ic_monitor failed: {e}")
        raise


def _factor_value_job() -> None:
    """A 组因子日计算（scripts/compute_factor_value.py --groups A）。

    ★ 2026-11-06 接线（此前该脚本**从未被调度**）
      ------------------------------------------------
      实测（2026-11-06 云端库）：factor_value 里 26 个因子中，**11 个 A 组**
      （f_vwap_dev / f_close_loc / f_close_loc14 / f_mom_1h / f_mom_4h /
      f_gap / f_atr_pct / f_vol_surge / f_oi_dir / f_range_comp / f_amt_conc）
      全部停在 **2026-09-30** —— 「注册了、作业报 success、但没人算」，
      属本项目重点缺陷类型（开关看似生效、实则静默空转）。

      另一处根因：该脚本原先用 ``load_creds()``（内部硬编码 127.0.0.1），
      在**调度容器内必然连不上库** —— 本批已改走 scripts/pgconn.py。

    增量口径：默认回溯 ``FACTOR_VALUE_LOOKBACK_DAYS``（默认 30）天，UPSERT 幂等。
    """
    import sys as _sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "compute_factor_value.py"
    if not script.exists():
        logger.error(f"[scheduler] 找不到脚本 {script}")
        return
    lookback = os.getenv("FACTOR_VALUE_LOOKBACK_DAYS", "45")
    since = os.getenv("FACTOR_VALUE_SINCE") or (
        datetime.now() - timedelta(days=int(lookback))).date().isoformat()
    # ★ 2026-11-06 部署修正：脚本的 --until 默认值是 "2026"，而 factor_ic_scan._ymd
    #   会把「年」展开成 2026-01-01 → SQL 变成 bucket >= <since> AND bucket < 2026-01-01
    #   → **恒空**（A 组自 2026 年起永远 0 行，这就是它从没被调度过的第二重根因）。
    #   显式传「明天」（含当日），不改脚本默认值（避免影响其他按「年」语义调用它的脚本）。
    until = os.getenv("FACTOR_VALUE_UNTIL") or (
        datetime.now() + timedelta(days=1)).date().isoformat()
    cmd = [_sys.executable, str(script), "--groups", "A", "--since", since,
           "--until", until]
    logger.info(f"[scheduler] factor_value(A) start since={since}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        logger.info(f"[scheduler] factor_value(A) exit={proc.returncode} "
                    f"tail={(proc.stdout or '')[-400:]}")
        _require_subprocess_success(proc, "factor_value(A)")
    except subprocess.TimeoutExpired:
        logger.warning("[scheduler] factor_value(A) timeout>3600s")
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] factor_value(A) failed: {e}")
        raise


def _factor_value_b_job() -> None:
    """B 组（基本面）+ C 组（基差时序）因子日计算。

    ★ 2026-11-06 接线：B 组 11 个因子（basis_rate_z / member_net_z /
      member_ls_z / member_trend_z / vol_concentration_z / inventory_z /
      warehouse_receipt_z / roll_yield_z / spot_mom_z / structure_slope_z /
      cross_rank_z）实测同样停在 2026-09-30 —— 与 A 组同因（脚本未被调度）。
      其中 member_* / vol_concentration_* 的**唯一上游** member_position_rank_summary
      也停在 09-30（其生产者 collect_member_rank_summary.py 从未被调度），
      故本作业排在 17:45 的汇总作业**之后**（18:00）。

    C 组为 §18.5 M6a ``basis_factors`` 派生的两个新因子（迁移 049 注册）。
    """
    import sys as _sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "compute_factor_value.py"
    if not script.exists():
        logger.error(f"[scheduler] 找不到脚本 {script}")
        return
    lookback = os.getenv("FACTOR_VALUE_LOOKBACK_DAYS", "45")
    since = os.getenv("FACTOR_VALUE_SINCE") or (
        datetime.now() - timedelta(days=int(lookback))).date().isoformat()
    until = os.getenv("FACTOR_VALUE_UNTIL") or (
        datetime.now() + timedelta(days=1)).date().isoformat()
    cmd = [_sys.executable, str(script), "--groups", "B,C", "--since", since,
           "--until", until]
    logger.info(f"[scheduler] factor_value(B,C) start since={since}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        logger.info(f"[scheduler] factor_value(B,C) exit={proc.returncode} "
                    f"tail={(proc.stdout or '')[-400:]}")
        _require_subprocess_success(proc, "factor_value(B,C)")
    except subprocess.TimeoutExpired:
        logger.warning("[scheduler] factor_value(B,C) timeout>3600s")
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] factor_value(B,C) failed: {e}")
        raise


def _collect_member_rank_summary_job() -> None:
    """会员持仓排名汇总（member_position_rank_summary）。

    ★ 2026-11-06 接线：此前**从未被调度** → 上游 summary 停更到 2026-09-30
      （82245 行），连带 B 组 member_net_z / member_ls_z / member_trend_z /
      vol_concentration_z 四个因子全部停更。

    依赖链：rank_position（17:30，产出 member_position_rank）→ 本作业（17:45）
      → factor_value(B,C)（18:00）。**排在上游之后**是硬约束：提前跑只会
      拿旧数据重算一遍（幂等但无意义）。

    另一处根因：该脚本原先 ``os.environ["PGPASSWORD"]``（下标取值）在容器内
    **必然 KeyError** → 本批已改走 scripts/pgconn.py。
    """
    import sys as _sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "collect_member_rank_summary.py"
    if not script.exists():
        logger.error(f"[scheduler] 找不到脚本 {script}")
        return
    cmd = [_sys.executable, str(script)]
    logger.info("[scheduler] collect_member_rank_summary start")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        logger.info(f"[scheduler] collect_member_rank_summary exit={proc.returncode} "
                    f"tail={(proc.stdout or '')[-400:]}")
        _require_subprocess_success(proc, "collect_member_rank_summary")
    except subprocess.TimeoutExpired:
        logger.warning("[scheduler] collect_member_rank_summary timeout>1800s")
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] collect_member_rank_summary failed: {e}")
        raise


def _factor_freshness_job() -> None:
    """因子级新鲜度巡检 + **自动补算**（每日 19:30）。

    ★ 2026-11-07 新增（用户诉求：因子数据每天采集/调用）
      ----------------------------------------------------
      事故背景：`app/ops/alerting.py::_check_freshness` 是**表级**检查（只看
      ``factor_value`` 整表的 max(trade_date)）⇒ **发现不了「某个因子停更」**。
      实测：22 个已注册因子里 16 个停在 **2026-09-30**，而整表 max 是 11-06
      （v1v6 的 4 个新因子在更新）⇒ **表级巡检一直显示「正常」，断更 40 天无人发现**。
      这正是本项目「开关看似生效、实则静默空转」的又一例。

    本作业做三件事：①**因子级**新鲜度（逐个 factor_registry.factor_id 查 max）
      ②断供则**按组自动补算**（幂等 UPSERT；`f_atr_pctile` 需要 250 交易日历史，
      补算时自动放宽到 420 自然日）③补后仍断供 → **硬告警**。
      排在 19:10 collect_roll_yield 之后、20:00 pre_night 之前（依赖链末端）。
    """
    import sys as _sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[1] / "scripts" / "factor_freshness_check.py"
    if not script.exists():
        logger.error(f"[scheduler] 找不到脚本 {script}")
        return
    cmd = [_sys.executable, str(script)]
    logger.info("[scheduler] factor_freshness start")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5400)
        out = (proc.stdout or "")[-2500:]
        logger.info(f"[scheduler] factor_freshness exit={proc.returncode}\n{out}")
        # exit=1 = 有因子仍断供（脚本内部已发硬告警），是业务状态不是脚本崩溃；
        # 其余非 0 一律 raise，否则 ops.scheduled_task_run 会记成 success。
        _require_subprocess_success(proc, "factor_freshness", tolerated=(1,))
        if proc.returncode == 1:
            logger.warning(f"[scheduler] factor_freshness 有因子仍断供（已告警）: "
                           f"{(proc.stderr or '')[-400:]}")
    except subprocess.TimeoutExpired:
        logger.warning("[scheduler] factor_freshness timeout>5400s")
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] factor_freshness failed: {e}")
        raise


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
        # ★ 2026-11-07：层间断层必须推送。这类故障的特征是「缺席者不在结果集里」——
        #   日志淹在每 30 分钟一轮的调度日志中，此前 index 层停更 6 天全程无人知。
        #   res 为 -1 表示检查本身未执行（同样需要人看见，不能当"通过"）。
        n_layer = res.get("layer_freshness", 0)
        if n_layer != 0:
            try:
                from app.ops.alerting import alert_config_from_env, send_alert

                ok, note = send_alert(
                    title="数据自检：层间断层",
                    content=(
                        f"data_selfcheck 检出 {n_layer} 处层/表断层"
                        f"（-1 表示检查未执行）。明细见 qhyc.err.log 中 "
                        f"[data_selfcheck] 的 error 行：\n"
                        f"· L0 各 layer 落后全库最新 >36h ⇒ 该层停止产出"
                        f"（如 index 层 8888 未接入增量合成）\n"
                        f"· l1_mkt/l2_adj 落后 L0 continuous >36h ⇒ 聚合/物化链断\n"
                        f"处置：先查该层数据源是否还在采集，再查合成/聚合作业是否注册。"),
                    cfg=alert_config_from_env(),
                    key="data_selfcheck_layer_gap",
                )
                if not ok:
                    logger.warning(f"[data_selfcheck] 层断层告警未发出：{note}")
            except Exception as e:  # noqa: BLE001 —— 告警失败不阻断自检结果记录
                logger.error(f"[data_selfcheck] 层断层告警异常（已忽略）：{e}")
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] data_selfcheck failed: {e}")


#: ``_alert_portfolio_brake_idle`` 的节流状态：同一 **(自然日, reason)** 只告警一次。
#: 理由：``_apply_portfolio_brake`` 每轮fusion_scan 都会调用（约每 30 分钟一次），
#: 不节流会把用户微信刷爆，反而导致真告警被无视。
#: ⚠ 两点注意（2026-10-08 修正）：
#:   1. 键是 **tuple**（日 + reason）而非"日"——reason 变化（表为空 → DB 故障）
#:      需重新提示，否则运维会照旧方向排查错。故变量名不再叫 ``..._DAY``。
#:   2. 节流是**按自然日**，跨零点即重试，**不是滚动 24 小时**。
#:   3. 通道关闭（ALERT_ENABLED/ALERT_NOTIFY 为假）时**不写**此标记 → 通道关闭期间
#:      会每轮打一条 logger.error（刷日志而非刷微信），这是有意的。
_BRAKE_IDLE_ALERT_KEY: tuple[str, str] | None = None


def _alert_portfolio_brake_idle(status: dict) -> None:
    """风控空转告警：**按自然日 + 按原因**节流，走 pushplus 推给用户。

    ★ 2026-10-08 新增。此前该问题只 ``logger.warning``，而 warning 淹没在
    每 30 分钟一次的调度日志里，实际从未被人看见 —— 于是「开关显示开启、
    风控实际空转」持续运行而无人知晓。现在升级为真告警。

    刻意**不**阻断决策链：回测/研究需要能跑通，熔断缺失以「告警 + 状态可查」
    暴露，而不是让整条信号链路挂掉。

    ★★ 2026-10-08 二修（代码审查发现）：``send_alert`` 的契约是**永不抛异常**，
    失败一律以返回值 ``(ok, note)`` 表达（内部 try/except 兜住 pushplus）。
    若忽略返回值就写节流标记，则「推送 token 过期 / 网络中断 / 通道未启用」
    这些最需要告警的场景会**被记成「今天已告警」**，当天剩余时间不再重试
    —— 恰好退化成修复前「只有日志、无推送」的状态。故：
      · 只有 ``ok=True`` 才写节流标记；失败 → 记 error 且**下轮继续重试**。
      · 通道本身未启用（``enabled``/``notify`` 为假）也**不**写标记，
        否则通道修好后当天仍不会补发。
      · 节流键含 ``reason``：故障性质变了（表为空 → query_error）应重新提示，
        否则运维会照旧方向排查错。
    """
    global _BRAKE_IDLE_ALERT_KEY
    day = datetime.now(_SH_TZ).strftime("%Y-%m-%d")
    reason = str(status.get("reason"))
    key = (day, reason)
    if key == _BRAKE_IDLE_ALERT_KEY:
        return
    try:
        from app.ops.alerting import alert_config_from_env, send_alert

        cfg_alert = alert_config_from_env()
        if not (cfg_alert.enabled and cfg_alert.notify):
            # 通道关着 → 记 error 但**不写标记**（通道修好后立刻能补发）
            logger.error(
                f"[portfolio_brake] 风控空转告警**无法送达**：ALERT_ENABLED={cfg_alert.enabled} "
                f"ALERT_NOTIFY={cfg_alert.notify}。熔断开关开着但告警通道关闭，此风险目前只有日志可查。"
                "请修好告警通道，或关闭 cfg_feature_switch.portfolio_brake_enabled "
                "让状态与实际一致。")
            return
        ok, note = send_alert(
            "⚠️ 风控空转：组合回撤熔断实际未生效",
            "**熔断开关是开的，但没有任何仓位级风控在起作用。**\n\n"
            "- `cfg_feature_switch.portfolio_brake_enabled = true`\n"
            "- 但权益曲线不可用 → 恒等放行（scalar ≡ 1.0）\n"
            "- 后果：回撤超 15%/25% 时**不会**降仓/清仓，等于没有熔断保护\n\n"
            f"诊断：reason=`{reason}` "
            f"样本={status.get('n_days')}/{status.get('need_days')} 交易日\n\n"
            "处置二选一：\n"
            "1. 补 `portfolio_equity` 权益曲线（需 ≥{need} 个交易日；"
            "注意 `fusion_signal_log.pnl` 是**收益率**不是金额，且样本极少，"
            "不能直接累加）\n"
            "2. 把 `portfolio_brake_enabled` 置 false，让状态与实际一致".format(
                need=status.get("need_days")),
            cfg_alert,
        )
        if not ok:
            # ★ 不写节流标记 → 下轮 fusion_scan 会重试（这是本函数存在的意义）
            logger.error(
                f"[portfolio_brake] 风控空转告警**未送达**（note={note}）；未写入节流标记，"
                "下轮 fusion_scan 将重试。请检查 PUSHPLUS_TOKEN / 网络 / "
                "ALERT_ENABLED / ALERT_NOTIFY。")
            return
        _BRAKE_IDLE_ALERT_KEY = key
    except Exception as e:  # noqa: BLE001 —— 告警失败绝不阻断信号链路
        logger.error(f"[portfolio_brake] 空转告警发送异常（已忽略，不阻断信号链）: {e}")


def _apply_portfolio_brake(session, results: list[dict]) -> list[dict]:
    """V5 组合回撤熔断：按当前回撤缩放「期望手数」（Phase 5）。

    设计要点
    * 默认**关闭**（PORTFOLIO_BRAKE_ENABLED），打开前必须先在 portfolio_equity 里
      有真实权益曲线（migrations/004 建表），否则恒等返回；
    * 手数取整用 ceil：1 手 × 0.5 = 0.5 → 1 手（不把 1 手直接砍成 0，
      "清仓"只由 dd_stop 的 scalar=0.0 触发，语义必须唯一）；
    * 任何异常都只记录并恒等返回 —— 熔断失效可以，误杀不行。
    """
    if not results:
        return results
    try:
        import math

        from app.risk.portfolio_brake import (
            brake_scalar, load_brake_config, load_equity_curve,
        )
        # ★ 2026-11-06 接线：改用 load_brake_config()（库内 cfg_feature_switch 优先，
        #   env 兜底保留在函数内部），**不再**在此处自行拼 BrakeConfig。
        #   旧写法让库内 `portfolio_brake_enabled` 的 JSON（dd_warn/dd_stop/lookback）
        #   永远不生效 ——「配置显示可调、实际不生效」，属静默失效。
        #   口径等价性：库内无该行 → 走 env（与旧 _switch_on 的回退分支一致）；
        #   库内有该行 → 以库内 enabled 为准，阈值库内未给则回退 env。
        cfg = load_brake_config(env_fallback=True)
        if not cfg.enabled:
            return results
        equity = load_equity_curve(session=session, lookback=cfg.lookback)
        if not equity:
            # ⚠ fail-loud：熔断已开启但**无权益曲线** → 恒等放行等于熔断失效。
            #   这不是"正常降级"，是"风控空转"——必须告警而非 info 静默。
            #
            # ★ 2026-10-08：原来只 ``logger.warning``，结果运行期一直打 warning
            #   而**无人察觉**（开关显示开启、风控实际空转）。现升级为
            #   **真告警（推微信）+ 按 (自然日, reason) 节流**。
            #   注意**不**改为阻断决策链：回测/研究场景仍需能跑通，
            #   熔断失效以"告警 + 状态可查"的方式暴露，而非让整条链路挂掉。
            #
            # ⚠ **已知未覆盖边界**（勿以为已全覆盖）：本告警只覆盖两种情形 ——
            #   ① 权益曲线为空；② 调用抛异常（见下方兜底 except）。
            #   「口径异常」这一条走的是 :func:`brake_scalar` 内部的
            #   ``peak <= 0 → dd=inf`` 分支：**只记 logger.error，不推送、不置节流
            #   标记、也不改 ``LAST_EQUITY_STATUS``**，故读数层面仍显示 ok=True/"正常"。
            #   即"开着但在空转"仍有第二条通道，待补（需把该分支也接入本告警）。
            from app.risk.portfolio_brake import equity_curve_status

            st = equity_curve_status()
            logger.error(
                "[portfolio_brake] 熔断已开启但无可用权益曲线 → 恒等放行，"
                "**风控实际未生效**（非降级，是空转）。原因={} 样本={}/{}(交易日)。"
                "请补 portfolio_equity 或关闭 cfg_feature_switch.portfolio_brake_enabled。".format(
                    st.get("reason"), st.get("n_days"), st.get("need_days"))
            )
            _alert_portfolio_brake_idle(st)
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
                # ★ 2026-10-08 修（PR review C1，致命）：原先只置``lots=0``，
                #   但下游三处都是 ``int(r.get("lots") or 1)`` —— **`0 or 1` → 1**，
                #   于是 dd_stop 的"清仓观望"被完全吞掉：
                #     · state 仍是 LONG/SHORT → 引擎不产生平仓信号 → 用户收不到清仓通知；
                #     · 心跳仍显示"×1手"，推送文案与真实风控状态相反（正在亏 25%
                #       却告诉用户"持多 1 手"）；
                #     · 若曾有 sc=0.5 把 lots 写成 1，熔断解除后引擎 lots=2 vs
                #       DB lots=1 → 判定"加码 1→2" → **推送一个不存在的加仓**。
                #   修法：清仓时同时把 state 置 0（让引擎产生真实平仓），
                #   并让下游能区分「0 = 目标清仓」与「None = 无值」。
                r["lots"] = 0
                r["state"] = 0
                r["brake_liquidated"] = True
            else:
                r["lots"] = max(1, int(math.ceil(float(lots) * sc - 1e-9)))
            n_cut += 1
        logger.warning(f"[portfolio_brake] 回撤熔断生效 scalar={sc} → 缩放 {n_cut} 个品种手数")
    except Exception as e:  # noqa: BLE001
        # ★ 2026-10-08 二修（代码审查 C4）：原实现只 logger.exception，**完全不告警**。
        #   但这个 try 覆盖了 load_equity_curve / equity_curve_status /
        #   _alert_portfolio_brake_idle —— 也就是说 **DB 层异常时（最需要告警的
        #   场景）告警代码根本不会被执行**，熔断空转 + 零推送。与被修的缺陷 A 同构。
        #   现在兜底路径同样告警，并标明是「异常」而非「无数据」。
        logger.exception(f"[portfolio_brake] 熔断异常，恒等放行：{e}")
        try:
            _alert_portfolio_brake_idle({
                "reason": f"brake_exception:{type(e).__name__}",
                "n_days": 0,
                "need_days": 0,
            })
        except Exception:  # noqa: BLE001 —— 告警再失败也不能掩盖原始异常
            logger.exception("[portfolio_brake] 熔断异常的告警亦失败")
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
        _require_subprocess_success(proc, "warehouse_receipt")
    except subprocess.TimeoutExpired:
        logger.warning("[scheduler] warehouse_receipt timeout>5400s")
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] warehouse_receipt failed: {e}")
        raise


def _hourly_job() -> None:
    """小时线每小时自动更新（决策 7）：整点触发一次全品种小时线增量采集。

    与主连日线三档调度解耦；融合信号扫描在交易时段内另有 15 分钟尾部刷新兜底。
    """
    # B5 (2026-11-07): 非交易日日盘 akshare 仍会返回 09:30 幽灵 K，
    # 此处在调度入口拦截；手动补采脚本不经过本函数，不受影响。
    from app.ingest.trading_window import is_hourly_collection_allowed
    if not is_hourly_collection_allowed():
        logger.info("[scheduler] B5 gate: 非交易日/非交易时段，跳过 hourly_bar 直拉采集")
        return
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


def _extend_index_8888() -> None:
    """补 XXXX8888 指数层分钟（CL-20261107-05）。

    指数层是**唯一不在采集链路里**的数据：akshare 只给 ``*888`` 主连，8888 由
    「合约分钟 → 按日末持仓量选主力 → 换月 carry 累计 → back-adjust」合成。
    这条合成原来只存在于一次性 CSV 导入脚本里，导入结束后就没有任何环节再产出，
    于是 L1 的 8888 停在导入那天，``sp_build_l2_roll_segment`` 的 ratio 门
    （``coalesce(idx_delta,1e9) < 0.40*|cc|``）因为 idx_delta 恒为 NULL 而**永不成立**
    —— 换月段静默不再追加，后复权价在换月处保留原始跳空。**没有报错、没有告警。**

    失败只 warning 不抛：指数层坏掉不该连带 bar 合成停摆，但必须每次可见。
    """
    import sys as _sys

    try:
        p = subprocess.run(
            [_sys.executable, "scripts/ops/extend_index_8888.py"],
            cwd=str(Path(__file__).resolve().parents[1]),
            capture_output=True, text=True, timeout=1800,
        )
        tail = (p.stdout or "").strip().splitlines()[-3:]
        if p.returncode == 0:
            logger.info(f"[scheduler] index_8888 扩展完成: {' | '.join(tail)}")
        else:
            # CL-20261107-19 的有意例外：本函数是 _minute_and_bars_job 内部的一个
            # 步骤，**不是独立作业**，抛异常会连带中断整条 bar 合成链路（更糟）。
            # 它有独立的机器兜底：扩展失败 ⇒ L0 index 层停更 ⇒
            # DATA-L0-LAYERS-FRESH（block）次日 09:10 必报 FAIL，不是静默绿。
            # exit 2 = 有品种接缝校验没过（合约层缺主力），数据未写入，需人工看。
            logger.error(f"[scheduler] index_8888 扩展返回 {p.returncode}: "
                         f"{' | '.join(tail)}")
            if p.returncode == 2:
                logger.error("[scheduler] 8888 接缝校验失败 → 该品种换月检测本轮无参照，"
                             "检查合约层（contract）当日是否含主力合约")
    except Exception as e:  # noqa: BLE001
        logger.error(f"[scheduler] index_8888 扩展异常（不影响 bar 合成）：{e}")


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
        from app.core.config import get_settings
        # ★ CL-20261109-04（2026-11-09）：本作业此前**无任何时段门**，每小时 :07/:37
        # 无条件拉 akshare。而新浪在休市期会返回时间戳落在非交易时段的合成 bar
        # （实测 ag0 在 11-07 02:30 收盘后紧跟一根 2026-11-09 00:00、volume=121），
        # 于是周一凌晨 00:07 这一轮就把 11 根幽灵 bar 写进 l0 → 60m → bar_60m_ba，
        # 占住 V5X 状态机上一根的位置，全程不报错。
        # 与 cron_fetch_1min 用同一个 is_collection_window（它早已正确排除周一凌晨）。
        # 采集器内部另有逐根判定做第二道防线，防其他调用方绕过本门。
        from app.ingest.trading_window import is_collection_window

        if not is_collection_window():
            logger.info("[scheduler] minute_and_bars 跳过：非采集窗口（休市期无新 bar）")
            return
        # 注意：IngestConfig 挂在 YamlConfig 下（Settings 只有 env/yaml 两个顶层字段），
        # 必须走 .yaml.ingest；写成 .ingest 会在本作业首次触发时抛 AttributeError，
        # 被外层 except 吞掉后表现为「作业静默不干活」，正是本次要根治的那类故障。
        mode = get_settings().yaml.ingest.bar_synth_mode
        with session_scope() as s:
            from app.ingest.minute_collector import MinuteCollector
            from app.ingest.synthesizer import minute_pipeline_lock

            with minute_pipeline_lock():   # 2026-10-08 起锁走独立连接（PR review I1）
                mc = MinuteCollector(s)
                m_stats = mc.collect_all()
                m_ok = sum(1 for r in m_stats if "error" not in r)
                # 2026-10-09 起改用健壮的块化 procedure 兜底 L1 聚合：
                # sp_build_l1_from_minute 增量起点 = target.max(bucket)+interval，
                # 故任何缺口（含>3天）下次运行都会自动补齐；纯 upsert、1天/块，
                # 无 synthesize_bars_incremental 的 DELETE 静默漏桶问题。
                synth_stats = {}
                # 2026-10-14：存储过程也纳入 ops.scheduled_task 字典控制与流水记录
                # （用户要求"存储过程也需要"）。字典禁用则跳过 CALL 并显式记日志。
                from app.ops.task_registry import record_execution, sp_enabled
                from app.ingest.synthesizer import (
                    purge_recent_buckets, synthesize_bars_incremental)

                # ★ 2026-11-03 口径回归修复（docs/V2样本外崩塌排查.md §36）：
                #   10-09 起本作业改成 CALL sp_build_l1_from_minute，而该 SP 用
                #   time_bucket 以**自然整点**切桶（09:00/10:00/11:00/13:00…），
                #   与回测/盘面使用的会话网格（10:00/11:15/14:15/15:00）不一致；
                #   synthesizer 里已修好的 dedup 与网格定义（4bce7fd，11-01）
                #   因此从未被调用 —— 修在了死代码上。
                #   现改回 Python 增量合成：先 purge 最近 N 天旧桶（纯 upsert 不会
                #   删掉另一套网格的旧桶，否则两套并存、一天 15 根），再重算。
                #   回滚开关：config 里 ingest.bar_synth_mode="sp"。
                if mode == "python":
                    # CL-20261107-01 S2：口径只读一次，purge 与 synth 共用。
                    # 两者范围必须严格一致（purge ⊇ synth 的删除边界），否则会出现
                    # 「删了不补」的静默丢数据；此处用同一个 Scope 对象保证这一点。
                    from app.data.tradable_scope import load_scope
                    scope = load_scope(s)
                    # ★ CL-20261107-05：先补 XXXX8888 指数层的分钟。
                    #   指数层不在任何增量采集里（akshare 只给 *888 主连），过去完全
                    #   依赖一次性 CSV 导入；源一停，L1 的 8888 就停，换月检测的
                    #   ratio 门因为 idx_delta 为 NULL 而**恒不成立**，换月段从此
                    #   静默不再追加（实测卡了 8 天没人发现）。
                    #   失败只告警不抛：它坏了不等于 bar 合成要停摆，但必须看得见。
                    _extend_index_8888()
                    purge_recent_buckets(s, days_back=4, scope=scope)
                    synth_stats = synthesize_bars_incremental(
                        s, days_back=3, scope=scope)
                elif not sp_enabled("sp_build_l1_from_minute"):
                    logger.warning(
                        "[scheduler] sp_build_l1_from_minute 被字典禁用"
                        "（ops.scheduled_task.enabled=false），跳过 L1 聚合"
                    )
                else:
                    with record_execution(
                        "sp_build_l1_from_minute",
                        payload={"freqs": ["min5", "min15", "min30", "min60"]},
                    ):
                        for _f in ("min5", "min15", "min30", "min60"):
                            s.execute(text("CALL public.sp_build_l1_from_minute(:f)"), {"f": _f})
                            synth_stats[_f] = "ok"
                        s.flush()
            logger.info(
                f"[scheduler] minute_and_bars done: minute_ok={m_ok}/{len(m_stats)} "
                f"synth={synth_stats}"
            )
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[scheduler] minute_and_bars failed: {e}")


def _back_adjusted_job() -> None:
    """P1-1：后复权物化 l2_adj.bar_*_ba 定期重建（工单 #8）。

    对 min5/min15/min30/min60 四个频率各调一次
    ``CALL l2_adj.sp_build_l2_back_adjusted(:f)``，由存储过程内部依据
    roll_segment 触发签名决定 full / incremental：

    * roll_segment 任一维度（price_shift / Σcum_offset / Σepoch(seg_start) / 段数）
      变化 ⇒ 全量重建该 symbol（覆盖 P0-5 等差改写、roll_segment 修正等所有口径变更）；
    * 否则仅增量补最新桶（廉价，一天三次无压力）。

    写入落 l2_adj.bar_*m_ba 并刷新 back_adjusted_meta.updated_at，供
    ``_check_freshness`` 的新鲜度告警监控。本作业依赖 ``_minute_and_bars_job``
    已把 l1_mkt.bar_* 落盘（错峰各整点 :55：02:55/11:55/15:55），且依赖 roll_segment 已是最新。
    若 roll_segment 不存在，存储过程会抛异常并由下方 except 记日志（不静默）。
    """
    logger.info("[scheduler] back_adjusted start")
    from app.ops.task_registry import record_start, record_finish, sp_enabled
    if not sp_enabled("sp_build_l2_back_adjusted"):
        logger.warning(
            "[scheduler] sp_build_l2_back_adjusted 被字典禁用"
            "（ops.scheduled_task.enabled=false），跳过后复权重建"
        )
        return
    run_id = record_start(
        "sp_build_l2_back_adjusted",
        trigger="auto",
        payload={"freqs": ["min5", "min15", "min30", "min60"]},
    )
    try:
        with session_scope() as s:
            for _f in ("min5", "min15", "min30", "min60"):
                s.execute(
                    text("CALL l2_adj.sp_build_l2_back_adjusted(:f)"), {"f": _f}
                )
            s.flush()
        # 溯源补强：本次运行刷新的符号数（区分"增量补桶"与"全量级联重锚"，
        # 回应 2026-10-15 溯源诉求——全量重建会刷新全部 228 个符号）。
        with session_scope() as s2:
            touched = s2.execute(
                text(
                    "SELECT count(*) FROM l2_adj.back_adjusted_meta "
                    "WHERE updated_at >= now() - interval '15 minutes'"
                )
            ).scalar() or 0
        record_finish(
            run_id,
            "success",
            payload={"freqs": ["min5", "min15", "min30", "min60"],
                      "touched_symbols": int(touched)},
        )
        logger.info(f"[scheduler] back_adjusted done; touched={touched} symbols")
    except Exception as e:  # noqa: BLE016
        record_finish(run_id, "failed", message=str(e)[:500])
        logger.exception(f"[scheduler] back_adjusted failed: {e}")


def _archive_nontradable_job() -> None:
    """S5：非白名单历史月度归档（CL-20261107-01）。

    每月 1 日 03:20 以 subprocess 调 ``scripts/ops/archive_nontradable.py``，
    把「上次水位 → 现在」的非白名单行增量导出到 ``/Docker/qhyc_archive/nontradable``。
    水位线落 ``l3_ref.nontradable_archive_log``，``(table_name, batch_key)`` 唯一，
    失败重跑不会重复导出。

    ⚠ **本作业不删任何数据**。方案 v3 / D1 定案是「只导出不删，观察 1 个月」——
    删除不可逆，且 L0 分钟是唯一可再生源，删了就只能靠归档还原。
    删除必须由用户单独拍板，不在本作业职责内。

    选 03:20 的理由：早于 06:30 的权重月更与日间采集，磁盘 IO 最空；
    且月度归档是纯读操作，与分钟管线（:07/:37）不冲突。
    """
    import sys as _sys

    script = _scripts_dir() / "ops" / "archive_nontradable.py"
    if not script.is_file():
        logger.error(
            f"[scheduler] 找不到归档脚本 {script} —— 本月归档未执行"
            "（典型原因：scripts/ops 未随镜像/仓库同步）")
        return
    batch = datetime.now(_SH_TZ).strftime("%Y-%m")
    cmd = [_sys.executable, str(script), "--batch-key", batch]
    logger.info(f"[scheduler] archive_nontradable start: {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        logger.info(
            f"[scheduler] archive_nontradable exit={proc.returncode} "
            f"batch={batch} tail={(proc.stdout or '')[-600:]}")
        _require_subprocess_success(proc, "archive_nontradable")
    except subprocess.TimeoutExpired:
        logger.warning("[scheduler] archive_nontradable timeout>7200s")
        raise
    except Exception:  # noqa: BLE001
        logger.exception("[scheduler] archive_nontradable failed")
        raise


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


def _reap_stale_running() -> None:
    """启动自愈：把上一进程遗留的 ``status='running'`` 记录收尾为 ``aborted``。

    ★ 2026-11-07 新增（实测缺陷）。进程被 restart 打断时，正在执行的作业来不及
      走 ``TaskRepository.finish()`` / 监听器的 EXECUTED 事件，``ops.scheduled_task_run``
      会**永远**留下 ``status='running' / finished_at=NULL`` 的僵尸行。
      实测（2026-11-07 21:50）：33/9065 条，最早 11-05，分布
      scheduler_resync 28 / fusion_scan 3 / hourly_collect 2，且每次重启恰好新增一条
      （11-06 22:30 resync ↔ 22:31 重启；11-07 14:00 resync ↔ 14:04 重启）。

    危害是**监控数据污染**：任何「查 running 任务判断是否卡住」的诊断都会被误导
    （2026-11-07 审计时曾据此误判"有任务 hang 住"）。

    为何语义铁定成立：本函数在 ``sched.start()`` **之前**执行，此刻进程内还没有
    任何作业实例在跑 ⇒ 库里所有 running 必然是上一进程（已被 kill）的遗物。

    只改记录不动业务；任何异常都不阻断启动。
    """
    from sqlalchemy import text as _text

    try:
        from app.core.db import get_session_factory

        with get_session_factory()() as s:
            n = s.execute(_text(
                "UPDATE ops.scheduled_task_run SET "
                "  status = 'aborted', "
                "  finished_at = COALESCE(finished_at, now()), "
                "  message = COALESCE(message, '') || ' [reaped: 上一进程重启遗留]' "
                "WHERE status = 'running'"
            )).rowcount
            s.commit()
        if n:
            logger.warning(
                f"[scheduler] 启动自愈：{n} 条上一进程遗留的 running 记录已置 aborted"
                f"（进程重启打断正在执行的作业所致，非任务故障）")
    except Exception as e:  # noqa: BLE001 —— 记账清理失败绝不阻断启动
        logger.warning(f"[scheduler] 启动自愈清理 running 记录失败（忽略，不阻断启动）: {e}")


def main() -> None:
    setup_logging()
    settings = get_settings()
    logger.info(f"[scheduler] starting role={settings.env.ROLE}")

    sched = _build_scheduler()

    # ★ 2026-11-07：启动自愈——收尾上一进程遗留的 running 僵尸记录（见函数 docstring）。
    #   必须在本进程任何作业开跑之前执行，故置于 sched.start() 之前。
    if settings.env.ROLE != "api":  # 仅调度器进程收尾，API 容器不动记账
        _reap_stale_running()

    # ★ P2-2 / CL-20261108-10：启动参数快照（在本进程任何作业开跑之前）。
    #   放在 _reap_stale_running 之后、sched.start() 之前，保证快照对应的是
    #   「这一轮进程真正生效的参数」。失败只告警，由守卫兜底可见。
    try:
        from app.core.config_snapshot import record_config_snapshot
        with session_scope() as s:
            record_config_snapshot(s, role=settings.env.ROLE or "scheduler")
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[scheduler] 启动参数快照写入失败（守卫会报 FAIL）：{exc}")

    def _shutdown(*_):
        logger.info("[scheduler] shutdown signal received")
        try:
            sched.shutdown(wait=False)
        except Exception as exc:
            logger.opt(exception=True).warning(f"[silent-swallow] 异常被静默忽略（此处原本为 pass）: {exc}")
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