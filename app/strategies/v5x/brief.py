# -*- coding: utf-8 -*-
"""V5X 收盘状态简报（CL-20261110-21）。

为什么需要它
------------
V5X 是**无状态回放**扫描器，只在「当根 bar 产生了开/加/平事件」时推一条信号。
于是无信号的小时里微信完全静默：用户无法区分「策略正常运行、本根无事」
与「扫描挂了 / 推送通道挂了」。2026-11-10 实测 ``v5x_close_scan`` 22:00:10
success 但 ``n_unsent=0``，整晚一条推送都没有；同期 20 条重复告警又把当天
仅有的 3 条交易信号埋掉（见 CL-20261110-20）。

V3.4 时代有「每 30 分钟持仓简报」，但 V3.4 自 2026-11-06 停用后该产物消失，
且它与 V5X 的持仓假设互不相容，不能直接复活。

设计约束
--------
1. **不重复**：本根有待推信号时信号消息即通知，简报不推；
2. **去重持久化**：``ops.notify_dedup(kind='v5x_brief')`` 按收盘标签去重，
   跨调度器重启有效（原 ``_PUSH_GATE`` 是进程内字典，重启即失效）；
3. **只在送达成功后占位**：绝不「先写去重行再发送」把失败固化成已送达；
4. **非交易时段静默**：复用 ``is_hourly_collection_allowed``（已处理夜盘
   跨零点与节假日），不另立一套日历判据；
5. **持仓摘要复用唯一重放路径** ``snapshot.theoretical_positions``，
   不另写循环（两套重放必然分叉）；持仓算不出来时降级照推，不拖垮收盘链；
6. **推送失败外抛**：与 ``run_v5x_scan`` / ``push_snapshot`` 同一条纪律，
   否则 ``ops.scheduled_task_run`` 会把「简报没送达」记成 success。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

from app.core.logging import logger

#: 去重键与告警 digest 共用一张表，靠 kind 分流
_KIND = "v5x_brief"


def _key(label: datetime) -> str:
    return label.strftime("%Y-%m-%d %H:%M")


def _engine():
    from app.core.db import get_engine
    return get_engine()


def _brief_seen(label: datetime) -> bool:
    """该收盘标签是否已推过。**表不可用时返回 True（不推）**：宁可少一条简报，
    也不能在去重失效时每根 K 线重复刷屏。"""
    try:
        with _engine().connect() as conn:
            return conn.execute(text(
                "SELECT 1 FROM ops.notify_dedup "
                "WHERE kind = :kind AND dedup_key = :k"
            ), {"kind": _KIND, "k": _key(label)}).first() is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[v5x.brief] 去重表不可读（本轮不推简报）: {0}: {1}".format(
            type(exc).__name__, str(exc)[:120]))
        return True


def _claim_brief(label: datetime) -> bool:
    """送达成功后才写留痕。"""
    try:
        with _engine().begin() as conn:
            conn.execute(text(
                "INSERT INTO ops.notify_dedup(kind, dedup_key, fingerprint, sent_at) "
                "VALUES (:kind, :k, 'close', now()) "
                "ON CONFLICT (kind, dedup_key) DO NOTHING"
            ), {"kind": _KIND, "k": _key(label)})
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[v5x.brief] 去重留痕写入失败（不影响本条已送达）: {0}".format(
            str(exc)[:120]))
        return False


def _next_close_label(label: datetime) -> datetime:
    """下一根 60m 收盘标签（与 live.CLOSE_HM 同源，不另立时间表）。"""
    from app.strategies.v5x.live import CLOSE_HM

    for off in (0, 1):
        for hh, mm in sorted(CLOSE_HM):
            cand = (label + timedelta(days=off)).replace(
                hour=hh, minute=mm, second=0, microsecond=0)
            if cand > label:
                return cand
    return label + timedelta(hours=1)


def build_brief(session, label: datetime, summary: dict) -> tuple[str, str]:
    """渲染**系统状态**消息（只读，不推送）。

    CL-20261128-03：持仓与交易计划已移出本函数，改由 :func:`build_trade_brief`
    单独发一条交易消息。本函数只保留扫描/覆盖/分批/下一根收盘等运行状态，
    两者职责严格分离。
    """
    scan = summary.get("scan") or {}
    cov = summary.get("coverage") or (0, 0)
    n_sym = summary.get("symbols", 0)

    lines = [
        f"## 系统状态 {label:%m-%d %H:%M}",
        "",
        f"- 本根收盘链：扫描 **{n_sym}** 个品种，分钟覆盖 {cov[0]}/{cov[1]}，"
        f"采集 {summary.get('attempts', '?')} 轮",
        f"- 新信号：**{scan.get('n_new', 0)}**（无开/加/平仓事件）",
    ]

    # ★ 两批次口径（CL-20261111-10）：让用户看得见「这根收盘分了几批发」，
    #   以及「哪些品种还在等数据」。否则无信号时用户无法区分
    #   「策略本来就没信号」与「信号被压在次批还没发」。
    ready_n = summary.get("ready_n")
    if ready_n is not None:
        pending_n = summary.get("pending_n", 0)
        idle_n = summary.get("idle_n", 0)
        lines.append(
            f"- 分批：首批（已就绪）**{ready_n}** 个已出信号"
            + (f"；次批（等数据）**{pending_n}** 个" if pending_n else "；次批 无")
            + (f"；本时段不交易 {idle_n} 个" if idle_n else "")
        )
        det = summary.get("pending_detail") or {}
        if det:
            items = "、".join(f"{k}({v})" for k, v in list(det.items())[:6])
            more = f"…另 {len(det) - 6} 个" if len(det) > 6 else ""
            lines.append(f"  · 待补发：{items}{more}")
        late = summary.get("late_batches") or []
        if late:
            lines.append(
                f"  · 次批已补发 {len(late)} 批："
                + "、".join("+".join(b.get("symbols", [])) for b in late[:4])
            )
    lines += [
        "",
        f"> 下一根收盘：{_next_close_label(label):%m-%d %H:%M}",
        "> 本条仅为系统运行状态；持仓、交易计划与累计盈亏见单独的「交易信息」消息。",
    ]
    title = f"[系统状态] {label:%m-%d %H:%M} · 扫描 {n_sym} 品种"
    return title, "\n".join(lines)


def build_trade_brief(session, label: datetime) -> tuple[str, str]:
    """渲染**交易专用**消息（当前持仓 + 交易计划 + 累计信号盈亏）。

    CL-20261128-03：无信号的收盘/整点本来只推状态简报，用户看不到持仓与累计盈亏。
    这里复用唯一重放路径 :func:`snapshot.theoretical_positions`，与信号消息同一口径；
    取数失败时照推（持仓标空仓、累计行省略），不拖垮推送链。
    """
    from app.strategies.v5x.snapshot import (
        cumulative_signal_pnl, format_positions, theoretical_positions,
    )

    rows: list[dict] = []
    cumulative: float | None = None
    try:
        rows = theoretical_positions(session).get("rows") or []
        cumulative = cumulative_signal_pnl(session)
    except Exception as exc:  # noqa: BLE001 —— 展示降级，不阻断状态确认
        logger.warning("[v5x.brief] 交易信息取数不可用（降级照推）: {0}: {1}".format(
            type(exc).__name__, str(exc)[:120]))

    title = f"[交易信息] {label:%m-%d %H:%M}"
    lines: list[str] = []
    lines += format_positions(rows)
    lines += ["", "交易计划：", "  本时段无新增交易计划"]
    if cumulative is not None:
        lines += ["", f"自2026年11月2日以来，累计交易信号盈亏：{cumulative:+,.0f}元。"]
    return title, "\n".join(lines)


#: 交易信息独立去重键（CL-20261128-03）：与状态简报分开，各自送达各自留痕。
_KIND_TRADE = "v5x_trade"


def _trade_seen(key: str) -> bool:
    """该小时交易信息是否已推过。**表不可用时返回 True（不推）**。"""
    try:
        with _engine().connect() as conn:
            return conn.execute(text(
                "SELECT 1 FROM ops.notify_dedup "
                "WHERE kind = :kind AND dedup_key = :k"
            ), {"kind": _KIND_TRADE, "k": key}).first() is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[v5x.brief] 交易去重表不可读（本轮不推）: {0}: {1}".format(
            type(exc).__name__, str(exc)[:120]))
        return True


def _claim_trade(key: str) -> bool:
    """送达成功后才写留痕。"""
    try:
        with _engine().begin() as conn:
            conn.execute(text(
                "INSERT INTO ops.notify_dedup(kind, dedup_key, fingerprint, sent_at) "
                "VALUES (:kind, :k, 'trade', now()) "
                "ON CONFLICT (kind, dedup_key) DO NOTHING"
            ), {"kind": _KIND_TRADE, "k": key})
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[v5x.brief] 交易去重留痕写入失败（不影响本条已送达）: {0}".format(
            str(exc)[:120]))
        return False


def _signal_pushed_for_hour(session, label: datetime) -> bool:
    """本小时是否已有信号消息送达——它已带持仓/累计盈亏，避免交易信息重复。"""
    start = label.replace(minute=0, second=0, microsecond=0)
    end = start + timedelta(hours=1)
    try:
        return session.execute(text(
            "SELECT 1 FROM public.v5x_signal_log "
            "WHERE source = 'live' AND notify_ok IS TRUE "
            "  AND bar_ts >= :a AND bar_ts < :b LIMIT 1"
        ), {"a": start, "b": end}).first() is not None
    except Exception:  # noqa: BLE001
        return False


def _push_pair(session, label: datetime, summary: dict, *, kind: str,
               do_push: bool) -> dict[str, Any]:
    """同一轮发两条：**交易信息**与**系统状态**；各自去重、各自留痕。"""
    from app.core.config import get_settings
    from app.notify import send_notify

    hour_key = label.strftime("%Y-%m-%d %H:00")
    trade_title, trade_content = build_trade_brief(session, label)
    sys_title, sys_content = build_brief(session, label, summary)
    if not do_push:
        return {"sent": False, "skip": "dry-run", "title": sys_title,
                "trade_title": trade_title,
                "content_preview": sys_content[:200]}

    # 备用通道与交易信号共用同一配置项（CL-20261109-06 的教训：漏传 = 单点）。
    # CL-20261110-06 仍 deferred：用户暂不配 webhook，此处如实传空串。
    fallback = getattr(get_settings().fusion, "fallback_webhook", "") or ""
    errors: list[str] = []
    sent_trade = sent_sys = False
    via: str = ""

    if _signal_pushed_for_hour(session, label):
        pass                       # 信号消息已带交易信息
    elif _trade_seen(hour_key):
        pass                       # 本小时交易信息已送达
    else:
        ok, via_t = send_notify(trade_title, trade_content, fallback)
        if ok:
            _claim_trade(hour_key)
            sent_trade, via = True, via_t
        else:
            errors.append(f"交易信息推送失败 via={via_t}")

    if _brief_seen_kind(kind, hour_key):
        pass
    else:
        ok, via_s = send_notify(sys_title, sys_content, fallback)
        if ok:
            _claim_brief_kind(kind, hour_key)
            sent_sys, via = True, via_s
        else:
            errors.append(f"系统状态推送失败 via={via_s}")

    if errors:
        # 不写去重行 → 下一轮可重试；外抛让流水记 failed。
        raise RuntimeError("；".join(errors) + "（未留痕，下一轮可重试）")
    if not sent_trade and not sent_sys:
        return {"sent": False, "skip": "两类消息本小时均已推送", "key": hour_key}
    logger.info(f"[v5x.brief] {hour_key} 已推送 trade={sent_trade} system={sent_sys} via={via}")
    return {"sent": True, "via": via, "key": hour_key, "title": sys_title,
            "trade_sent": sent_trade, "system_sent": sent_sys}


def push_close_brief(session, label: datetime, summary: dict,
                     *, do_push: bool = True) -> dict[str, Any]:
    """收盘链后推两条（交易信息 + 系统状态）；无信号的收盘根才推，失败外抛。"""
    from app.ingest.trading_window import is_hourly_collection_allowed

    if not is_hourly_collection_allowed(label):
        return {"sent": False, "skip": "非交易时段静默"}
    if (summary.get("scan") or {}).get("n_unsent", 0):
        return {"sent": False, "skip": "本根有待推信号，信号消息即交易信息"}
    return _push_pair(session, label, summary, kind=_KIND, do_push=do_push)


def _brief_seen_kind(kind: str, key: str) -> bool:
    """指定 kind 的该去重键是否已推过。**表不可用时返回 True（不推）**。"""
    try:
        with _engine().connect() as conn:
            return conn.execute(text(
                "SELECT 1 FROM ops.notify_dedup "
                "WHERE kind = :kind AND dedup_key = :k"
            ), {"kind": kind, "k": key}).first() is not None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[v5x.brief] 去重表不可读（本轮不推简报）: {0}: {1}".format(
            type(exc).__name__, str(exc)[:120]))
        return True


def _claim_brief_kind(kind: str, key: str) -> bool:
    """送达成功后才写留痕。"""
    try:
        with _engine().begin() as conn:
            conn.execute(text(
                "INSERT INTO ops.notify_dedup(kind, dedup_key, fingerprint, sent_at) "
                "VALUES (:kind, :k, 'hourly', now()) "
                "ON CONFLICT (kind, dedup_key) DO NOTHING"
            ), {"kind": kind, "k": key})
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[v5x.brief] 去重留痕写入失败（不影响本条已送达）: {0}".format(
            str(exc)[:120]))
        return False


def _close_brief_present_for_hour(session, label: datetime) -> bool:
    """同一小时是否已有收盘简报（v5x_brief）去重行，避免与收盘简报双推。"""
    prefix = label.strftime("%Y-%m-%d %H:")
    try:
        return session.execute(text(
            "SELECT 1 FROM ops.notify_dedup WHERE kind = 'v5x_brief' "
            "AND dedup_key LIKE :p LIMIT 1"
        ), {"p": prefix + "%"}).first() is not None
    except Exception:  # noqa: BLE001
        return False


#: 每小时状态简报去重键（与收盘简报/交易信息区分）
_KIND_HOURLY = "v5x_hourly_brief"


def push_periodic_brief(session, label: datetime, summary: dict,
                        *, do_push: bool = True) -> dict[str, Any]:
    """每交易小时推两条：交易信息（持仓/计划/累计盈亏）+ 系统状态。

    CL-20261128-03：原来是「一条状态简报塞满采集细节」，现在按用户要求分流。
    与收盘简报、信号消息的去重仍按小时，避免同小时双推。
    """
    from app.ingest.trading_window import is_hourly_collection_allowed

    if not is_hourly_collection_allowed(label):
        return {"sent": False, "skip": "非交易时段静默"}
    if _close_brief_present_for_hour(session, label):
        return {"sent": False, "skip": "同小时收盘简报已覆盖"}
    return _push_pair(session, label, summary, kind=_KIND_HOURLY, do_push=do_push)
