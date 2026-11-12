# -*- coding: utf-8 -*-
"""全部合约 1 分钟行情采集器（2026-11-06 新增，同日做增量效率优化）。

与 ``MinuteCollector`` 的区别
-----------------------------
===========================  ==========================  ==========================
                             MinuteCollector             本模块
===========================  ==========================  ==========================
采集对象                     50 个 ``main_contracts`` 的  全市场全部上市合约
                             888 主连                     （~670 个）
新浪代码                     ``rb0``（品种+0 连续）       ``RB2701``（具体合约）
写入 layer                   continuous                   contract
src                          akshare_1min                 akshare_1min_contract
===========================  ==========================  ==========================

关键约束
--------
1. **L1 合成必须只看 continuous 层**。本模块会往 ``minute_bar`` 写入大量
   ``layer='contract'`` 行；若 L1 合成不加过滤，800 个合约会被合成进 bar_5m/15m/30m/60m。
   ``synthesizer._session_insert_sql`` 已同步加 ``AND layer='continuous'``。
2. **时间标签**：akshare ``futures_zh_minute_sina`` 的 datetime 是 end-label，
   与 CSV / 主连同口径，直接按上海本地解析，不做 +1min 平移。
3. **幂等**：主键 ``(symbol, ts)``，``ON CONFLICT DO NOTHING``，重复跑安全；
   历史 CSV 已写入的同 symbol+ts 行优先保留，不被 akshare 覆盖。
4. **失败隔离**：退市/无成交合约接口会抛 ``ValueError: Length mismatch``，
   单合约失败只记日志，不影响整批。

增量效率优化（2026-11-06 晚）
-----------------------------
akshare 1 分钟接口**固定返回最近约 1024 根**，无法只拉增量；但可以在写库前
按每合约已有 ``max(ts)`` 过滤，只提交真正新增的分钟行。实测收盘后每轮
原本提交 68 万行、实际新增 0 行，优化后提交 0 行。

* :func:`load_watermarks` 一条 SQL 批量取所有目标合约的水位线；
* :meth:`ContractMinuteCollector.collect` 用 ``watermarks`` 过滤 ``ts > 水位``；
* **无水位线的新合约保留全部返回行**（首次采集需要回填历史窗口）；
* ``--force`` 可绕过水位线过滤做全量重提（历史补采/口径修复用）；
* 水位线只回看 ``lookback_days`` 天，避免对 1.19 亿行 hypertable 全表扫描。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import logger
from app.ingest.l0_write_guard import filter_cross_src_occupied
from app.ingest.minute_collector import _parse_ak_minute_dt, _valid_minute_row

SRC = "akshare_1min_contract"

_INSERT = text(
    "INSERT INTO l0_raw.minute_bar "
    "(symbol, ts, open, high, low, close, volume, amount, open_interest, "
    " contract, src, layer, exchange, variety_code) "
    "VALUES (:symbol, :ts, :open, :high, :low, :close, :volume, :amount, "
    "        :open_interest, :contract, :src, 'contract', :exchange, :variety_code) "
    "ON CONFLICT (symbol, ts) DO NOTHING"
)

_WATERMARK_SQL = text(
    "SELECT symbol, max(ts) AS last_ts "
    "FROM l0_raw.minute_bar "
    "WHERE layer = 'contract' "
    "  AND symbol = ANY(:syms) "
    "  AND ts >= :floor "
    "GROUP BY symbol"
)


def load_watermarks(
    session: Session,
    symbols: Iterable[str],
    lookback_days: int = 8,
) -> dict[str, datetime]:
    """批量取每个合约在 contract 层的最新分钟时间戳。

    一次查询完成（``symbol = ANY(array)`` + 时间下界走 hypertable 索引），
    避免 668 次单合约查询。无历史数据的合约不出现在返回字典里。
    """
    syms = list(dict.fromkeys(symbols))
    if not syms:
        return {}
    floor = datetime.now(timezone.utc) - timedelta(days=max(1, int(lookback_days)))
    rows = session.execute(_WATERMARK_SQL, {"syms": syms, "floor": floor}).all()
    return {str(r[0]): r[1] for r in rows if r[1] is not None}


def _fetch_one(spec: dict[str, Any]) -> tuple[str, list[dict], str | None]:
    """单合约拉取（在线程内执行，不碰 DB session）。"""
    import akshare as ak  # type: ignore

    sym = spec["symbol"]
    try:
        df = ak.futures_zh_minute_sina(symbol=sym, period="1")
    except Exception as e:  # noqa: BLE001
        return sym, [], f"{type(e).__name__}: {str(e)[:80]}"
    if df is None or df.empty:
        return sym, [], "empty"
    rows: list[dict] = []
    for _, r in df.iterrows():
        dt = _parse_ak_minute_dt(r.get("datetime"))
        o, h, l, c = r.get("open"), r.get("high"), r.get("low"), r.get("close")
        if not _valid_minute_row(dt, o, h, l, c):
            continue
        vol = r.get("volume")
        oi = r.get("hold")
        try:
            volume = int(vol) if vol is not None else 0
        except (TypeError, ValueError):
            volume = 0
        rows.append({
            "symbol": sym,
            "ts": dt,
            "open": float(o), "high": float(h), "low": float(l), "close": float(c),
            "volume": volume,
            "amount": None,
            "open_interest": int(oi) if oi is not None else None,
            "contract": sym,
            "src": SRC,
            "exchange": spec.get("exchange"),
            "variety_code": spec.get("variety_code"),
        })
    return sym, rows, None


class ContractMinuteCollector:
    """并发拉取全部合约的 1 分钟线，主线程统一写库。"""

    def __init__(self, session: Session, workers: int = 6, pause_sec: float = 0.05):
        self.session = session
        self.workers = max(1, int(workers))
        self.pause_sec = max(0.0, float(pause_sec))

    def collect(
        self,
        contracts: Iterable[dict[str, Any]],
        dry_run: bool = False,
        batch_size: int = 2000,
        watermarks: dict[str, datetime] | None = None,
        incremental: bool = True,
    ) -> dict[str, Any]:
        """采集并写库。

        ``incremental=True`` 且给了 ``watermarks`` 时，只提交 ``ts > 水位线`` 的行；
        没有水位线的合约（新上市/首次采集）保留全部返回行。
        """
        specs = list(contracts)
        fetched: list[dict] = []
        errors: list[tuple[str, str]] = []
        rows_fetched = 0
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futs = {pool.submit(_fetch_one, s): s["symbol"] for s in specs}
            for fu in as_completed(futs):
                sym, rows, err = fu.result()
                if err:
                    errors.append((sym, err))
                else:
                    rows_fetched += len(rows)
                    if incremental and watermarks is not None:
                        wm = watermarks.get(sym)
                        if wm is not None:
                            rows = [r for r in rows if r["ts"] > wm]
                    fetched.extend(rows)
                if self.pause_sec:
                    time.sleep(self.pause_sec)

        # ★ CL-20261112-09：水位线只回看 8 天，查不到更早历史时会把老合约误判成
        # 「新合约」并保留全部 1024 根，例如 BB2705 撞上 local_csv_20261030 的
        # 10-08~10-29 历史键（实测 28 行冲突）。这里做一次精确键的跨源占位过滤，
        # 让 keep-first 显式生效；同源键仍由 ON CONFLICT DO NOTHING 兜底。
        n_cross_src = 0
        guard_failed = False
        if fetched:
            kept, n_cross_src, guard_failed = filter_cross_src_occupied(
                self.session, fetched, SRC
            )
            fetched = kept
        inserted = 0
        if not dry_run and fetched:
            for i in range(0, len(fetched), batch_size):
                self.session.execute(_INSERT, fetched[i:i + batch_size])
            self.session.commit()
            inserted = len(fetched)  # 实际写入 ≤ 该值（ON CONFLICT 去重）
        stats = {
            "contracts": len(specs),
            "ok": len(specs) - len(errors),
            "failed": len(errors),
            "rows": len(fetched),                       # 过滤后待写行数
            "submitted": inserted,
            "rows_fetched": rows_fetched,               # 接口原始返回行数
            "rows_skipped": rows_fetched - len(fetched),  # 被水位线/跨源占位滤掉
            "n_skipped_cross_src": n_cross_src,
            "guard_failed": bool(guard_failed),
            "incremental": bool(incremental and watermarks is not None),
            "watermarks": len(watermarks or {}),
            "seconds": round(time.time() - t0, 1),
            "errors": errors[:20],
        }
        logger.info(
            f"[contract_minute] done: {stats['ok']}/{stats['contracts']} 合约 "
            f"rows={stats['rows']} submitted={stats['submitted']} "
            f"skipped={stats['rows_skipped']} failed={stats['failed']} "
            f"{stats['seconds']}s"
        )
        return stats
