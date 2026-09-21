# -*- coding: utf-8 -*-
"""大商所 DCE 会员成交持仓排名采集器（Scrapling 版）—— qhyc 适配层

为什么需要它
------------
大商所官网 ``dcereport`` / ``publicweb`` 路径由 **瑞数动态防护**（RiverSecurity）保护：
先返回 412 + 一段混淆 JS 挑战页，必须由真浏览器执行 JS 换取会话 cookie 才能访问。

* ``requests`` / ``curl_cffi`` / ``urllib`` 直连 → 一律 412；
* akshare ``futures_dce_position_rank`` 由此抛 ``BadZipFile``；
* 新浪兜底 ``futures_hold_pos_sina`` 对 DCE 返回的是**非大商所品种**，若照单入库会污染
  ``member_position_rank``（历史上 393 行 ``src='sina_cot'`` 即此因）。

本模块用 **Scrapling** 的 ``StealthyFetcher``（真实 Chromium + 反检测指纹）先让瑞数挑战
跑完，再在**页面上下文内** ``fetch`` 官方批量下载接口取回 zip，从而绕开该防护。

数据源
------
``POST http://www.dce.com.cn/dcereport/publicweb/dailystat/memberDealPosi/batchDownload``

.. code-block:: json

    {"tradeDate": "20260918", "varietyId": "a", "contractId": "a2601",
     "tradeType": "1", "lang": "zh"}

批量返回当日**全部合约**的 zip，内部为每合约一个 txt（约 94~110 个）。

口径（务必理解后再改）
----------------------
三张表（成交量 / 持买单量 / 持卖单量）各自**按数值降序**排 1~20 名。本实现按名次对齐
合并成一行：``long_pos`` 是第 i 大多头持仓、``short_pos`` 是第 i 大空头持仓、
``vol_pos`` 是第 i 大成交量——**量级次序完整保留**，``member`` 只能记一个名字
（取多头表第 i 名，剥离 ``（代客）`` 后缀）。

这正是 qhyc 既有约定：``app/features/position_factors.py`` 用
``rows.nsmallest(5,'rank')['long_pos'].sum()`` 取前 5，依赖的正是「名次 = 降序位」，
所以多空两侧的 top5 求和都准确；``member`` 仅作展示/审计。**不要**为了让 member 与
short_pos 一一对应而改本映射，否则破坏与 CZCE/SHFE 行同构性。

依赖（可选）
------------
``pip install "scrapling[fetchers]"``，并确保有可用浏览器：
宿主机直接复用本机 Chrome/Edge；容器内需 ``scrapling install`` 装自带 Chromium。
未安装时 :func:`available` 返回 False，:func:`fetch_dce_rank` 抛 :class:`DceUnavailable`，
由调用方降级标注（不硬编码 try 豁免）。

外部采集器
----------
宿主机（环境已就绪）可直接跑工作区/技能的 ``DCE_scrapling_crawler.py``，与本模块口径一致、
``src`` 相同，双双幂等（冲突键 = trade_date+exchange+symbol+member+version）。
"""
from __future__ import annotations

import base64
import datetime as dt
import io
import os
import re
import time
import zipfile
from typing import Any, Callable

from app.core.logging import logger

SRC_TAG = "scrapling:dce_memberDealPosi"
VERSION = "v1.0"

PAGE_URL = "http://www.dce.com.cn/dalianshangpin/xqsj/tjsj26/rtj/rcjccpm/index.html"
HOME_URL = "http://www.dce.com.cn/"
API_URL = ("http://www.dce.com.cn/dcereport/publicweb/dailystat/"
           "memberDealPosi/batchDownload")

# 宿主机浏览器候选（容器内留空即可，交给 Scrapling 自带 Chromium）
BROWSER_CANDIDATES = [
    # Windows
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    # Linux（容器）
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/microsoft-edge",
]

RE_FILENAME = re.compile(r"^(\d{8})_([A-Za-z]+\d+)_")
RE_HEADER = re.compile(r"合约代码[:：]\s*([A-Za-z]+\d+)\s*.*?Date[:：]\s*(\d{4}-\d{2}-\d{2})")

METRIC_MAP = {"成交量": "vol", "持买单量": "long", "持卖单量": "short"}

# 页面上下文内下载 zip（带 credentials，复用浏览器解出的瑞数会话 cookie）
JS_FETCH = """
async (args) => {
  const [url, payload] = args;
  const r = await fetch(url, {
    method: 'POST',
    headers: {'Content-Type': 'application/json;charset=UTF-8',
              'Accept': 'application/json, text/plain, */*'},
    body: JSON.stringify(payload),
    credentials: 'include'
  });
  const buf = await r.arrayBuffer();
  const bytes = new Uint8Array(buf);
  let bin = '';
  const CH = 0x8000;
  for (let i = 0; i < bytes.length; i += CH) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
  }
  return {status: r.status, size: bytes.length, b64: btoa(bin)};
}
"""

CSV_COLS = ["trade_date", "exchange", "symbol", "member", "rank", "long_pos",
            "short_pos", "long_chg", "short_chg", "vol_pos", "src", "version"]


class DceUnavailable(RuntimeError):
    """Scrapling 或浏览器不可用（例如容器内未安装依赖）"""


# ---------------------------------------------------------------- 环境探测

def _log(msg: str) -> None:
    logger.info(f"[dce_scrapling] {msg}")


def pick_browser() -> str | None:
    """返回可复用的本机浏览器路径；无则 None（交给 Scrapling 自带 Chromium）"""
    for p in BROWSER_CANDIDATES:
        try:
            if os.path.exists(p):
                return p
        except OSError:
            continue
    return None


def available() -> bool:
    """Scrapling 是否可用（只探测 import，不启动浏览器）"""
    try:
        import scrapling  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


def browser_hint() -> str:
    exe = pick_browser()
    if exe:
        return exe
    return "Scrapling 自带 Chromium（需 scrapling install）"


# ---------------------------------------------------------------- 工具

def to_int(v) -> int | None:
    """'1,234' / '-1,234' → int；空 → None"""
    if v is None:
        return None
    s = str(v).strip().replace(",", "")
    if not s or s in ("-", "--"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def clean_member(name: str) -> str:
    """剥离 DCE 的（代客）后缀，与其他交易所会员名对齐；保留（自营）等特殊标识"""
    if not name:
        return ""
    return re.sub(r"[（(]代客[)）]$", "", name.strip()).strip()


def trading_days(start: dt.date, end: dt.date) -> list[dt.date]:
    """粗筛交易日（去周末）；调休/节假日由接口空返回自然过滤"""
    out, d = [], start
    while d <= end:
        if d.isoweekday() <= 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


# ---------------------------------------------------------------- 解析

def parse_rank_txt(text: str) -> dict:
    """单个合约 txt → {contract, date, tables:{vol/long/short: {rank: (member,val,chg)}}}"""
    contract = date_str = None
    tables: dict[str, dict[int, tuple]] = {"vol": {}, "long": {}, "short": {}}
    cur = None

    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        if contract is None:
            m = RE_HEADER.search(line)
            if m:
                contract, date_str = m.group(1), m.group(2)
                continue
        toks = [t for t in re.split(r"\t+", line.strip()) if t != ""]
        if not toks:
            continue
        head = toks[0].replace(" ", "")

        if head.startswith("名次"):                      # 表头行
            cur = next((METRIC_MAP[t] for t in toks if t in METRIC_MAP), None)
            continue
        if head.startswith(("合计", "总计", "小计")):      # 汇总行
            cur = None
            continue
        if cur and head.isdigit() and len(toks) >= 3:     # 数据行
            tables[cur][int(head)] = (
                clean_member(toks[1]),
                to_int(toks[2]),
                to_int(toks[3]) if len(toks) > 3 else None,
            )

    return {"contract": (contract or "").upper(), "date": date_str, "tables": tables}


def parse_rank_zip(zip_bytes: bytes, want_date: dt.date | None = None) -> list[dict]:
    """zip → 标准化行（名次对齐，三表合并；同会员去重合并）"""
    rows: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        for name in z.namelist():
            m = RE_FILENAME.match(os.path.basename(name))
            if not m:
                continue
            fdate, fcontract = m.group(1), m.group(2).upper()
            if want_date and fdate != want_date.strftime("%Y%m%d"):
                continue
            try:
                data = z.read(name).decode("utf-8", errors="ignore")
            except Exception:  # noqa: BLE001
                data = z.read(name).decode("gbk", errors="ignore")
            parsed = parse_rank_txt(data)
            contract = parsed["contract"] or fcontract
            d = parsed["date"] or f"{fdate[:4]}-{fdate[4:6]}-{fdate[6:]}"
            t = parsed["tables"]

            by_member: dict[str, dict] = {}
            for r in sorted(set(t["vol"]) | set(t["long"]) | set(t["short"])):
                l, s, v = t["long"].get(r), t["short"].get(r), t["vol"].get(r)
                member = (l or s or v or ("", None, None))[0]
                if not member:
                    continue
                row = {
                    "trade_date": d,
                    "exchange": "DCE",
                    "symbol": contract,
                    "member": member,
                    "rank": r,
                    "long_pos": l[1] if l else None,
                    "long_chg": l[2] if l else None,
                    "short_pos": s[1] if s else None,
                    "short_chg": s[2] if s else None,
                    "vol_pos": v[1] if v else None,
                    "src": SRC_TAG,
                    "version": VERSION,
                }
                if member in by_member:          # 同名会员出现在不同名次 → 补齐合并
                    old = by_member[member]
                    for k in ("long_pos", "long_chg", "short_pos", "short_chg", "vol_pos"):
                        if old.get(k) is None:
                            old[k] = row[k]
                    continue
                by_member[member] = row
            rows.extend(by_member.values())
    return rows


def rows_to_frames(rows: list[dict]) -> dict[str, "Any"]:
    """标准化行 → {合约: DataFrame}（供 member_position 矩阵消费）"""
    import pandas as pd

    if not rows:
        return {}
    df = pd.DataFrame(rows)
    out: dict[str, Any] = {}
    for contract, part in df.groupby("symbol"):
        out[str(contract)] = part[[
            "symbol", "rank", "member", "long_pos", "long_chg",
            "short_pos", "short_chg", "vol_pos",
        ]].reset_index(drop=True)
    return out


# ---------------------------------------------------------------- 抓取

def _safe_goto(page, url: str, wait_ms: int = 3500, tries: int = 3):
    """导航并等待；瑞数挑战页会自行刷新，必须等 JS 跑完再操作"""
    for _ in range(tries):
        try:
            r = page.goto(url, timeout=90000, wait_until="domcontentloaded")
            page.wait_for_timeout(wait_ms)
            return r.status if r else None
        except Exception:  # noqa: BLE001
            try:
                page.wait_for_timeout(1500)
            except Exception:  # noqa: BLE001
                pass
    return None


def _safe_fetch(page, trade_date: str, tries: int = 3):
    """页面内 fetch 批量下载接口 → (status, bytes)"""
    payload = {"tradeDate": trade_date, "varietyId": "a", "contractId": "a2601",
               "tradeType": "1", "lang": "zh"}
    for _ in range(tries):
        try:
            res = page.evaluate(JS_FETCH, [API_URL, payload])
            raw = base64.b64decode(res["b64"]) if res.get("b64") else b""
            return res.get("status"), raw
        except Exception:  # noqa: BLE001
            # 典型：Execution context was destroyed（挑战页在自刷新）
            try:
                page.wait_for_timeout(2000)
            except Exception:  # noqa: BLE001
                pass
    return None, b""


def fetch_zips(dates: list[dt.date], *, headless: bool = True,
               on_result: Callable[[str, bytes], None] | None = None) -> dict[str, bytes]:
    """浏览器会话内批量取回 zip → {YYYYMMDD: bytes}

    关键点（实测结论，勿删）：
    瑞数对受保护路径 **每次页面加载只放行一个请求**——首次 fetch 成功，
    紧接着的第二次必得 412。因此**每个交易日取数前都必须重新导航到受保护页面**，
    让挑战 JS 重跑、换发会话凭证。
    """
    if not available():
        raise DceUnavailable("scrapling 未安装（pip install \"scrapling[fetchers]\"）")

    from scrapling.fetchers import StealthyFetcher

    got: dict[str, bytes] = {}
    failures: list[str] = []

    def page_action(page):
        try:
            _safe_goto(page, HOME_URL, wait_ms=2000)
            _log(f"首页已打开：{page.title()[:30]}")
        except Exception as e:  # noqa: BLE001
            _log(f"首页打开异常（忽略）：{type(e).__name__}")

        for d in dates:
            ds = d.strftime("%Y%m%d")
            raw, status = b"", None
            for attempt in range(1, 4):
                pst = _safe_goto(page, PAGE_URL, wait_ms=3500)   # 重过挑战页
                status, raw = _safe_fetch(page, ds)
                if raw[:2] == b"PK":
                    break
                _log(f"  {ds} 第{attempt}次未通过（page={pst} api={status}），重试…")
                try:
                    page.wait_for_timeout(1500)
                except Exception:  # noqa: BLE001
                    pass

            if raw[:2] == b"PK":
                got[ds] = raw
                _log(f"  {ds} ✔ {len(raw):>8,} bytes")
                if on_result:
                    on_result(ds, raw)
            else:
                failures.append(ds)
                snippet = raw[:120].decode("utf-8", errors="ignore").replace("\n", " ")
                _log(f"  {ds} ✘ status={status} size={len(raw)} :: {snippet[:70]}")
            time.sleep(0.6)
        return page

    kwargs: dict[str, Any] = dict(
        headless=headless, network_idle=False, timeout=180000, wait=2000,
        locale="zh-CN", google_search=False, retries=1, page_action=page_action,
    )
    exe = pick_browser()
    if exe:
        kwargs["executable_path"] = exe
    _log(f"浏览器：{exe or browser_hint()}")

    StealthyFetcher.fetch(HOME_URL, **kwargs)
    if failures:
        _log(f"未取到数据的日期（非交易日或被拦）：{', '.join(failures)}")
    return got


# ---------------------------------------------------------------- 对外接口

def fetch_dce_rows(dates: list[dt.date], *, headless: bool = True) -> list[dict]:
    """批量抓取 → 标准化行 list[dict]（含 src/version）"""
    zips = fetch_zips(dates, headless=headless)
    out: list[dict] = []
    for ds in sorted(zips):
        got = parse_rank_zip(zips[ds])
        _log(f"  {ds} 解析 {len(got)} 行 / {len({r['symbol'] for r in got})} 合约")
        out.extend(got)
    return out


def fetch_dce_rank(trade_date: dt.date) -> dict[str, Any]:
    """矩阵接口：单日 → {合约代码: DataFrame}

    与 ``member_position.EXCHANGE_COVERAGE['DCE']['func']`` 对接。
    无数据（非交易日/被拦）时返回 ``{}``；依赖缺失抛 :class:`DceUnavailable`。
    """
    rows = fetch_dce_rows([trade_date])
    return rows_to_frames(rows)


def save_rows(rows: list[dict]) -> int:
    """幂等入库（PK: trade_date+exchange+symbol+member+version）"""
    if not rows:
        return 0
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.core.db import get_engine
    from app.models import MemberPositionRank

    seen: dict[tuple, dict] = {}
    for r in rows:
        seen[(r["trade_date"], r["exchange"], r["symbol"], r["member"], r["version"])] = r
    data = list(seen.values())

    eng = get_engine()
    with eng.begin() as conn:
        stmt = pg_insert(MemberPositionRank).values(data)
        conn.execute(stmt.on_conflict_do_update(
            index_elements=["trade_date", "exchange", "symbol", "member", "version"],
            set_={
                "rank": stmt.excluded.rank,
                "long_pos": stmt.excluded.long_pos,
                "short_pos": stmt.excluded.short_pos,
                "long_chg": stmt.excluded.long_chg,
                "short_chg": stmt.excluded.short_chg,
                "vol_pos": stmt.excluded.vol_pos,
                "src": stmt.excluded.src,
            },
        ))
    return len(data)


def run(date: dt.date | None = None, days: int = 1) -> dict:
    """抓取并入库：单日（默认今天）或最近 N 个交易日。返回统计。

    供 scheduler / CLI 调用；依赖缺失时抛出 :class:`DceUnavailable`，
    调用方应降级标注而非静默吞掉。
    """
    today = dt.date.today()
    if date is not None:
        dates = [date]
    else:
        start = today - dt.timedelta(days=int(days * 1.6) + 3)
        dates = trading_days(start, today)[-days:]

    _log(f"目标交易日：{', '.join(d.isoformat() for d in dates)}（浏览器 {browser_hint()}）")
    rows = fetch_dce_rows(dates)
    if not rows:
        return {"dates": [d.isoformat() for d in dates], "rows": 0,
                "symbols": 0, "src": SRC_TAG, "note": "无数据（非交易日或仍被拦）"}
    n = save_rows(rows)
    stats = {
        "dates": sorted({r["trade_date"] for r in rows}),
        "rows": n,
        "symbols": len({r["symbol"] for r in rows}),
        "src": SRC_TAG,
    }
    _log(f"入库 {n} 行 / {stats['symbols']} 合约")
    return stats


if __name__ == "__main__":  # 便于容器内 nohup/exec 直接调试
    import sys

    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    d = None
    if len(sys.argv) > 1:
        d = dt.datetime.strptime(sys.argv[1], "%Y-%m-%d").date()
    print("scrapling available:", available(), "| browser:", browser_hint())
    print(run(d))
