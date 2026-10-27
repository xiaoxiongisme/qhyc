# -*- coding: utf-8 -*-
"""把券商/期货公司的「成交明细」CSV 导入 ``trade_journal``（往返级）。

支持格式（三家实测）
--------------------
```text
序号,成交时间,合约代码,方向,开/平,手数,成交均价,平仓盈亏,手续费
1,26-10-27 14:36:21,MA701,买,平,5,3468,-15550,17.4
```
* 编码：``utf-8-sig`` / ``gb18030`` 自动探测（实测两家是 GB18030、一家 UTF-8-BOM）
* 时间格式：``%y-%m-%d %H:%M:%S``（**两字年**，必须显式指定格式，
  否则 pandas 的 mixed 推断会把 ``26-04-10`` 解析成 2001 年——本项目真实踩过）

口径
----
* ``gross`` 直接取账单「平仓盈亏」，``fee`` 取「手续费」，**不做二次估算**。
* ``open_at`` / ``open_px`` 由 FIFO 重建（近似）。境内外期交所的「平今/平昨」
  优先级不同（郑商所平今优先），故开仓价**仅供分析参考**，金额一律以账单为准。
* ``planned_stop`` 留空：历史账单里没有"当时计划的止损"，**绝不事后倒推**
  （否则 R 倍数会被人为美化，行为违规的取证就失效了）。

用法
----
    python -m scripts.import_broker_bills --dir "E:/知识沉淀/交易数据复盘"
    python -m scripts.import_broker_bills --dir ... --dry-run   # 只统计不写库
"""
from __future__ import annotations

import argparse
import glob
import os
import re
from collections import defaultdict, deque
from datetime import datetime
from typing import Any

import pandas as pd

from app.core.db import session_scope
from app.core.logging import logger
from app.risk.trade_journal import TradeRow, upsert

_ENCODINGS = ("utf-8-sig", "gb18030", "utf-16")
_TS_FMT = "%y-%m-%d %H:%M:%S"


def _read_any(path: str) -> pd.DataFrame:
    last: Exception | None = None
    for enc in _ENCODINGS:
        try:
            df = pd.read_csv(path, encoding=enc)
            df.columns = [c.strip() for c in df.columns]
            return df
        except (UnicodeDecodeError, UnicodeError) as e:
            last = e
            continue
    raise RuntimeError(f"无法解码 {path}: {last}")


def _parse_ts(s: pd.Series) -> pd.Series:
    """两字年时间列 → datetime。**必须显式 format**，见模块 docstring。"""
    cleaned = s.astype(str).str.replace(r"\s+", " ", regex=True).str.strip()
    ts = pd.to_datetime(cleaned, format=_TS_FMT, errors="coerce")
    bad = int(ts.isna().sum())
    if bad:
        logger.warning(f"[import] {bad} 行时间解析失败，已丢弃（请核对时间格式）")
    return ts


def load_bills(directory: str, account: str | None = None) -> pd.DataFrame:
    # Windows 文件系统大小写不敏感：``*.CSV`` 与 ``*.csv`` 会匹配同一批文件，
    # 直接 glob 两遍会把每行读两次（实测行数翻倍、净利翻倍）。故按 realpath 去重。
    paths: dict[str, str] = {}
    for pat in ("*.CSV", "*.csv"):
        for p in glob.glob(os.path.join(directory, pat)):
            paths.setdefault(os.path.realpath(p).lower(), p)
    if not paths:
        logger.warning(f"[import] {directory} 下未找到 CSV")
        return pd.DataFrame()

    frames = []
    for p in sorted(paths.values()):
        acct = re.sub(r"^成交明细-?", "", os.path.splitext(os.path.basename(p))[0])
        if account and acct != account:
            continue
        df = _read_any(p)
        df["acct"] = acct
        df["_src"] = os.path.basename(p)
        frames.append(df)
        logger.info(f"[import] {acct}: {len(df)} 行 ← {os.path.basename(p)}")
    if not frames:
        return pd.DataFrame()
    d = pd.concat(frames, ignore_index=True)
    d["dt"] = _parse_ts(d["成交时间"])
    d = d.dropna(subset=["dt"]).copy()
    d["contract"] = d["合约代码"].astype(str).str.strip().str.upper()
    d["side"] = d["方向"].astype(str).str.strip()
    d["oc"] = d["开/平"].astype(str).str.strip()
    d["lots"] = pd.to_numeric(d["手数"], errors="coerce").fillna(0).astype(int)
    d["px"] = pd.to_numeric(d["成交均价"], errors="coerce")
    d["pnl"] = pd.to_numeric(d["平仓盈亏"], errors="coerce").fillna(0.0)
    d["fee"] = pd.to_numeric(d["手续费"], errors="coerce").fillna(0.0)
    sort_key = "序号" if "序号" in d.columns else None
    d = d.sort_values(["acct", "dt"] + ([sort_key] if sort_key else [])).reset_index(drop=True)
    return d


def to_trades(d: pd.DataFrame) -> list[TradeRow]:
    """按「平仓事件」聚合：每个平仓行 → 一条往返。

    为什么不用逐笔 FIFO 配成多条：账单的 ``平仓盈亏`` 是**该行**的权威金额，
    拆成多笔会与账单对不上；以平仓行为单位则总额精确可核。
    """
    out: list[TradeRow] = []
    for (acct, contract), g in d.groupby(["acct", "contract"], sort=False):
        long_q: deque[list[Any]] = deque()   # [px, lots, dt, fee_per_lot]
        short_q: deque[list[Any]] = deque()
        for _, r in g.iterrows():
            n = int(r["lots"])
            if n <= 0:
                continue
            if r["oc"].startswith("开"):
                (long_q if r["side"] == "买" else short_q).append(
                    [float(r["px"]), n, r["dt"], float(r["fee"]) / n])
                continue
            # 平：卖平→减少多头；买平→减少空头
            is_long = r["side"] == "卖"
            q = long_q if is_long else short_q
            need, cost, lots_done, first_dt, open_fee = n, 0.0, 0, None, 0.0
            while need > 0 and q:
                opx, olots, odt, ofee = q[0]
                take = min(need, olots)
                cost += opx * take
                open_fee += ofee * take          # 开仓侧手续费也计入本笔成本
                lots_done += take
                first_dt = first_dt or odt
                need -= take
                if take == olots:
                    q.popleft()
                else:
                    q[0][1] = olots - take
            open_px = (cost / lots_done) if lots_done else float(r["px"])
            out.append(TradeRow(
                account=acct, real_symbol=contract,
                direction="LONG" if is_long else "SHORT",
                open_at=first_dt or r["dt"], close_at=r["dt"], lots=n,
                open_px=round(open_px, 4), close_px=float(r["px"]),
                gross=float(r["pnl"]), fee=round(float(r["fee"]) + open_fee, 4),
                planned_stop=None, close_reason="IMPORTED", source="IMPORT"))
    return out


def summarize(trades: list[TradeRow]) -> dict[str, Any]:
    if not trades:
        return {"n": 0}
    wins = [t for t in trades if t.net > 0]
    loss = [t for t in trades if t.net <= 0]
    aw = sum(t.net for t in wins) / len(wins) if wins else 0.0
    al = abs(sum(t.net for t in loss) / len(loss)) if loss else 0.0
    by_acct: dict[str, list[float]] = defaultdict(list)
    for t in trades:
        by_acct[t.account].append(t.net)
    return {
        "n": len(trades),
        "net": round(sum(t.net for t in trades), 2),
        "win_rate": round(len(wins) / len(trades), 4),
        "avg_win": round(aw, 2), "avg_loss": round(al, 2),
        "rr": round(aw / al, 3) if al else None,
        "by_account": {k: round(sum(v), 2) for k, v in by_acct.items()},
        "fee_total": round(sum(t.fee for t in trades), 2),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="导入成交明细到 trade_journal")
    ap.add_argument("--dir", required=True, help="账单目录")
    ap.add_argument("--account", default=None, help="只导入某个账户（默认全部）")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写库")
    args = ap.parse_args()

    d = load_bills(args.dir, args.account)
    if d.empty:
        logger.warning("[import] 没有读到任何账单")
        return
    trades = to_trades(d)
    logger.info(f"[import] 往返 {len(trades)} 笔：{summarize(trades)}")
    print(summarize(trades))

    if args.dry_run:
        print("\n[dry-run] 未写库")
        return
    with session_scope() as s:
        tried, added = upsert(s, trades)
    print(f"写入：尝试 {tried} 笔，新增 {added} 笔（冲突跳过 {tried - added}）")


if __name__ == "__main__":
    main()
