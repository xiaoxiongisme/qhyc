# -*- coding: utf-8 -*-
"""全部合约 1 分钟行情采集器（2026-11-06 新增）。

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
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import logger
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
    ) -> dict[str, Any]:
        specs = list(contracts)
        fetched: list[dict] = []
        errors: list[tuple[str, str]] = []
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futs = {pool.submit(_fetch_one, s): s["symbol"] for s in specs}
            for fu in as_completed(futs):
                sym, rows, err = fu.result()
                if err:
                    errors.append((sym, err))
                else:
                    fetched.extend(rows)
                if self.pause_sec:
                    time.sleep(self.pause_sec)

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
            "rows": len(fetched),
            "submitted": inserted,
            "seconds": round(time.time() - t0, 1),
            "errors": errors[:20],
        }
        logger.info(
            f"[contract_minute] done: {stats['ok']}/{stats['contracts']} 合约 "
            f"rows={stats['rows']} submitted={stats['submitted']} "
            f"failed={stats['failed']} {stats['seconds']}s"
        )
        return stats
