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
    """渲染简报（只读，不推送）。返回 (title, content)。"""
    from app.strategies.v5x.snapshot import theoretical_positions

    scan = summary.get("scan") or {}
    cov = summary.get("coverage") or (0, 0)
    n_sym = summary.get("symbols", 0)

    lines = [
        f"## 📊 V5X 收盘简报 {label:%m-%d %H:%M}",
        "",
        f"- 本根收盘链：扫描 **{n_sym}** 个品种，分钟覆盖 {cov[0]}/{cov[1]}，"
        f"采集 {summary.get('attempts', '?')} 轮",
        f"- 新信号：**{scan.get('n_new', 0)}**（无开/加/平仓事件）",
    ]
    try:
        snap = theoretical_positions(session)
        rows = snap.get("rows") or []
        lines.append(
            f"- 理论持仓：{len(rows)} 个（多 {snap.get('n_long', 0)} / "
            f"空 {snap.get('n_short', 0)}），截至 {snap.get('last_bar') or '—'}")
        if rows:
            lines.append("")
            lines.append("### 持仓一览")
            for r in rows[:8]:
                tag = "保本" if r.get("be_done") else "止损"
                lines.append(
                    f"  · {r['symbol']} {'多' if r['dir'] == 'LONG' else '空'} "
                    f"{r['lots']:g}手 现{r['last_price']:.1f} "
                    f"({r['unrealized_yuan']:+,.0f}) {tag}{r['stop_real']:.1f}")
            if len(rows) > 8:
                lines.append(f"  · …另 {len(rows) - 8} 个略")
        else:
            lines.append("  · 当前空仓（按规则无需持有任何品种）")
        stale = snap.get("stale_days")
        if stale is not None and stale >= 2:
            lines.append(f"  · ⚠ 最后一根 bar 已是 {stale} 天前，清单可能过期")
    except Exception as exc:  # noqa: BLE001 —— 持仓算不出也要让用户知道链路活着
        logger.warning("[v5x.brief] 理论持仓摘要不可用（降级照推）: {0}: {1}".format(
            type(exc).__name__, str(exc)[:120]))
        lines.append("- 理论持仓：暂不可用（重放异常，不影响信号链路）")

    lines += [
        "",
        f"> 下一根收盘：{_next_close_label(label):%m-%d %H:%M}",
        "> 这份是**状态确认**，不是交易信号；V5X 只出信号、不下单、不维护持仓。",
        "> 口径：V5X.3 · BA 后复权信号 + 真实价反解，t-1 门控。",
    ]
    title = f"[V5X 简报] {label:%m-%d %H:%M} · 无新信号"
    return title, "\n".join(lines)


def push_close_brief(session, label: datetime, summary: dict,
                     *, do_push: bool = True) -> dict[str, Any]:
    """收盘链后补一条状态确认。无信号才推；失败外抛。"""
    from app.core.config import get_settings
    from app.ingest.trading_window import is_hourly_collection_allowed
    from app.notify import send_notify

    if not is_hourly_collection_allowed(label):
        return {"sent": False, "skip": "非交易时段静默"}
    if (summary.get("scan") or {}).get("n_unsent", 0):
        return {"sent": False, "skip": "本根有待推信号，信号消息即通知"}
    if _brief_seen(label):
        return {"sent": False, "skip": "该收盘标签已推过（或去重表不可用）"}

    title, content = build_brief(session, label, summary)
    if not do_push:
        return {"sent": False, "skip": "dry-run", "title": title,
                "content_preview": content[:200]}

    # 备用通道与交易信号共用同一配置项（CL-20261109-06 的教训：漏传 = 单点）。
    # CL-20261110-06 仍 deferred：用户 22:00 明确暂不配 webhook，此处如实传空串。
    fallback = getattr(get_settings().fusion, "fallback_webhook", "") or ""
    ok, via = send_notify(title, content, fallback)
    if not ok:
        # 不写去重行 → 下一根收盘链/兜底扫描可重试；外抛让流水记 failed。
        raise RuntimeError(
            f"V5X 收盘简报 {label:%m-%d %H:%M} 推送失败（pushplus + fallback "
            f"均未送达，via={via}）—— CL-20261110-06 单通道问题，未配 webhook")
    _claim_brief(label)
    logger.info(f"[v5x.brief] {label:%m-%d %H:%M} 已推送 via={via}")
    return {"sent": True, "via": via, "key": _key(label), "title": title}


#: 每小时简报去重键（与收盘简报 v5x_brief 区分，避免同小时双推）
_KIND_HOURLY = "v5x_hourly_brief"


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


def push_periodic_brief(session, label: datetime, summary: dict,
                        *, do_push: bool = True) -> dict[str, Any]:
    """每交易小时一条状态确认（CL-20261111-01）。

    与 ``push_close_brief`` 的区别：不因为「本根有待推信号」而静默——它是独立的
    心跳类状态确认，保证用户**每个交易小时**都能收到一条「系统活着」的回执，
    不论该小时有没有信号。收盘简报（无信号收盘根）仍按原设计保留。

    与收盘简报协作去重：若同一小时已经有收盘简报（说明该小时已由更精确的收盘
    标签覆盖），则不再重复推，避免同小时双推。
    """
    from app.core.config import get_settings
    from app.ingest.trading_window import is_hourly_collection_allowed
    from app.notify import send_notify

    if not is_hourly_collection_allowed(label):
        return {"sent": False, "skip": "非交易时段静默"}
    if _close_brief_present_for_hour(session, label):
        return {"sent": False, "skip": "同小时收盘简报已覆盖"}

    hour_key = label.strftime("%Y-%m-%d %H:00")
    if _brief_seen_kind(_KIND_HOURLY, hour_key):
        return {"sent": False, "skip": "该小时简报已推过（或去重表不可用）"}

    title, content = build_brief(session, label, summary)
    if not do_push:
        return {"sent": False, "skip": "dry-run", "title": title,
                "content_preview": content[:200]}

    # 备用通道与交易信号共用同一配置项（CL-20261109-06 的教训：漏传 = 单点）。
    # CL-20261110-06 仍 deferred：用户明确暂不配 webhook，此处如实传空串。
    fallback = getattr(get_settings().fusion, "fallback_webhook", "") or ""
    ok, via = send_notify(title, content, fallback)
    if not ok:
        # 不写去重行 → 下一轮可重试；外抛让流水记 failed。
        raise RuntimeError(
            f"V5X 每小时简报 {hour_key} 推送失败（pushplus + fallback 均未送达，"
            f"via={via}）—— CL-20261110-06 单通道问题，未配 webhook")
    _claim_brief_kind(_KIND_HOURLY, hour_key)
    logger.info(f"[v5x.brief] {hour_key} 已推送 via={via}")
    return {"sent": True, "via": via, "key": hour_key, "title": title}
