# -*- coding: utf-8 -*-
"""仓单日报采集（Scrapling 版，本地/服务器通用）。

为什么不用 akshare：akshare 的 warehouse_receipt 系列依赖浏览器执行 JS / 直接下载
xlsx，在 headless 容器里拿不到（PRD §7 已知限制）。本脚本用 Scrapling 在**有浏览器
的本机**抓取各交易所仓单数据，解析后幂等 upsert 到 warehouse_receipt 表。

数据源（来自 akshare 现有实现的反向推导）：
- SHFE : https://www.shfe.com.cn/data/tradedata/future/dailydata/{date}dailystock.dat  (JSON, Fetcher 直连)
- GFEX : http://www.gfex.com.cn/u/interfacesWebTdWbillWeeklyQuotes/loadList           (表单 POST, 浏览器会话)
- CZCE : http://www.czce.com.cn/cn/DFSStaticFiles/Future/{yyyy}/{yyyymmdd}/FutureDataWhsheet.xls (xls, Fetcher 下载+xlrd 解析)
- DCE  : http://www.dce.com.cn/dcereport/publicweb/dailystat/wbillWeeklyQuotes        (JSON, 瑞数防护→浏览器会话)

落库口径（warehouse_receipt 表，PK: report_date+exchange+symbol+warehouse）：
- symbol = 品种代码 + '888'（主力连续口径，与既有 akshare 解析一致）
- warehouse = 仓库名（去 $$英文后缀 / 取简称）
- receipt_qty = 当日仓单量；change_qty = 当日增减；unit = 单位（吨/张/千克）
- 各所含「小计/总计」汇总行均已剔除

DB 连接：优先读环境变量 PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD，
默认 localhost:5432（本地）；服务器端设 PGHOST=timescaledb 即可复用同一脚本。
"""
from __future__ import annotations

import argparse
import base64
import datetime as _dt
import json
import os
import re
import sys
from datetime import date as _date

sys.path.insert(0, r"C:/Users/Seven/.codebuddy/skills/scrapling-qi/scripts")

import psycopg2  # noqa: E402
from psycopg2.extras import execute_batch  # noqa: E402

SRC = "scrapling:warehouse_receipt"
VERSION = "v1.0"

PG = dict(
    host=os.environ.get("PGHOST", "localhost"),
    port=int(os.environ.get("PGPORT", "5432")),
    dbname=os.environ.get("PGDATABASE", "futures"),
    user=os.environ.get("PGUSER", "futures"),
    password=os.environ.get("PGPASSWORD", "qhyc_dev_pwd_2026"),
)

# --------------------------------------------------------------- 浏览器
BROWSER_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
]

JS_FETCH = """
async (args) => {
  const [url, payload, isForm] = args;
  const init = {
    method: 'POST',
    headers: {'Content-Type': 'application/json;charset=UTF-8',
              'Accept': 'application/json, text/plain, */*'},
    body: JSON.stringify(payload),
    credentials: 'include'
  };
  if (isForm) {
    init.headers['Content-Type'] = 'application/x-www-form-urlencoded';
    init.body = new URLSearchParams(payload).toString();
  }
  const r = await fetch(url, init);
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

# --------------------------------------------------------------- 常量
SHFE_URL = "https://www.shfe.com.cn/data/tradedata/future/dailydata/{d}dailystock.dat"
# 2025-11-18 起 SHFE 停更 dailystock.dat，仓单改由该 HTML 报表提供
SHFE_HTML_URL = "https://www.shfe.com.cn/data/tradedata/future/stockdata/dailystock_{d}/ZH/all.html"
# CZCE 下载的临时文件目录（运行时自动创建，避免依赖 PoC 目录）
TMP_DIR = "e:/Docker/qhyc/_tmp"
GFEX_API = "http://www.gfex.com.cn/u/interfacesWebTdWbillWeeklyQuotes/loadList"
GFEX_HOME = "http://www.gfex.com.cn/"
DCE_API = "http://www.dce.com.cn/dcereport/publicweb/dailystat/wbillWeeklyQuotes"
DCE_HOME = "http://www.dce.com.cn/"
DCE_PAGE = "http://www.dce.com.cn/dalianshangpin/xqsj/tjsj26/rtj/rcjccpm/index.html"

SHFE_VARIETY = {
    "铜": "CU", "铝": "AL", "锌": "ZN", "铅": "PB", "镍": "NI", "锡": "SN",
    "黄金": "AU", "白银": "AG", "螺纹钢": "RB", "线材": "WR", "热轧卷板": "HC",
    "燃料油": "FU", "石油沥青": "BU", "沥青": "BU", "天然橡胶": "RU",
    "20号胶": "NR", "纸浆": "SP", "不锈钢": "SS", "中质含硫原油": "SC",
    "原油": "SC", "低硫燃料油": "LU", "国际铜": "BC", "氧化铝": "AO",
    "丁二烯橡胶": "BR", "集运指数": "EC",
}
WGHTUNIT_MAP = {"1": "千克", "2": "吨", "3": "克"}


# --------------------------------------------------------------- 工具
def log(msg: str) -> None:
    print(f"[{_dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def pick_browser() -> str | None:
    for p in BROWSER_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _safe_goto(page, url: str, wait_ms: int = 3500, tries: int = 3):
    for _ in range(tries):
        try:
            r = page.goto(url, timeout=90000, wait_until="domcontentloaded")
            page.wait_for_timeout(wait_ms)
            return r.status if r else None
        except Exception:
            page.wait_for_timeout(1500)
    return None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def upsert(rows: list[dict]) -> int:
    if not rows:
        return 0
    conn = psycopg2.connect(connect_timeout=10, **PG)
    try:
        with conn, conn.cursor() as cur:
            execute_batch(cur, """
                INSERT INTO warehouse_receipt
                    (report_date, exchange, symbol, warehouse, receipt_qty, change_qty, unit, src, version)
                VALUES (%(report_date)s, %(exchange)s, %(symbol)s, %(warehouse)s,
                        %(receipt_qty)s, %(change_qty)s, %(unit)s, %(src)s, %(version)s)
                ON CONFLICT (report_date, exchange, symbol, warehouse) DO UPDATE SET
                    receipt_qty = EXCLUDED.receipt_qty,
                    change_qty  = EXCLUDED.change_qty,
                    unit        = EXCLUDED.unit,
                    src         = EXCLUDED.src
            """, rows, page_size=500)
        return len(rows)
    finally:
        conn.close()


def existing_dates(exchange: str, start: _date, end: _date) -> set:
    """查询某交易所已存在于 DB 的 report_date 集合，用于断点续跑。"""
    conn = psycopg2.connect(connect_timeout=10, **PG)
    try:
        with conn, conn.cursor() as cur:
            cur.execute(
                "SELECT report_date FROM warehouse_receipt "
                "WHERE exchange=%s AND report_date BETWEEN %s AND %s",
                (exchange, start, end))
            return {r[0] for r in cur.fetchall()}
    finally:
        conn.close()


# --------------------------------------------------------------- SHFE (Fetcher: JSON .dat + HTML 回退)
def _shfe_code(name: str):
    n = (name or "").strip()
    if "BC" in n or "国际铜" in n:
        return "BC"
    n2 = re.sub(r"[\(（][^\)）]*[\)）]", "", n).strip()
    n2 = n2.replace("仓库", "").replace("厂库", "").strip()
    return SHFE_VARIETY.get(n2)


def _parse_shfe_dat(cur: list, d: _date) -> list[dict]:
    out, skipped = [], 0
    for row in cur:
        vname = str(row.get("VARNAME", "")).split("$")[0].strip().replace("仓库", "").replace("厂库", "").strip()
        code = SHFE_VARIETY.get(vname)
        if not code:
            skipped += 1
            continue
        wh = str(row.get("WHABBRNAME", "")).split("$")[0].strip()
        if not wh:
            continue
        out.append({
            "report_date": d, "exchange": "SHFE", "symbol": code + "888",
            "warehouse": wh, "receipt_qty": _int(row.get("WRTWGHTS")),
            "change_qty": _int(row.get("WRTCHANGE")),
            "unit": WGHTUNIT_MAP.get(str(row.get("WGHTUNIT", "")), str(row.get("WGHTUNIT", ""))),
            "src": SRC, "version": VERSION,
        })
    if skipped:
        log(f"[shfe] {d:%Y%m%d} 跳过未映射品种 {skipped} 行")
    return out


def _parse_shfe_html(html: bytes, d: _date) -> list[dict]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    out, skipped = [], 0
    for tbl in soup.find_all("table"):
        sp = tbl.find("td", colspan="3")
        if not sp:
            continue
        name = (sp.get_text() or "").strip()
        if not name or name == "地区":
            continue
        code = _shfe_code(name)
        if not code:
            skipped += 1
            continue
        unit = "吨"
        sib = sp.find_next_sibling("td")
        if sib:
            um = re.search(r"单位[:：]\s*([\u4e00-\u9fa5]+)", sib.get_text() or "")
            if um:
                unit = um.group(1)
        region = ""
        for tr in tbl.find_all("tr"):
            cls = tr.get("class", []) or []
            if not any("el-table__row" in c for c in cls):
                continue
            if "special_row_type" in cls or tr.find("td", colspan=True):
                continue
            tds = tr.find_all("td")
            vals = [(td.get_text() or "").strip() for td in tds]
            if not vals:
                continue
            if any(k in "".join(vals) for k in ("小计", "总计")):
                continue
            if len(vals) == 4 and not re.fullmatch(r"-?\d+", vals[0]):
                region, wh, qty, chg = vals[0], vals[1], _int(vals[2]), _int(vals[3])
            elif len(vals) == 3:
                wh, qty, chg = vals[0], _int(vals[1]), _int(vals[2])
            else:
                continue
            if not wh or wh == region:
                continue
            out.append({
                "report_date": d, "exchange": "SHFE", "symbol": code + "888",
                "warehouse": wh, "receipt_qty": qty, "change_qty": chg,
                "unit": unit, "src": SRC, "version": VERSION,
            })
    if skipped:
        log(f"[shfe] {d:%Y%m%d} HTML 跳过未映射品种 {skipped} 个")
    return out


def fetch_shfe(d: _date) -> list[dict]:
    from scrapling.fetchers import Fetcher
    ds = d.strftime("%Y%m%d")
    try:
        r = Fetcher.get(SHFE_URL.format(d=ds), stealthy_headers=True, timeout=30000)
        if r.status == 200:
            cur = (r.json() or {}).get("o_cursor") or []
            if cur:
                return _parse_shfe_dat(cur, d)
    except Exception:
        pass
    try:
        r = Fetcher.get(SHFE_HTML_URL.format(d=ds), stealthy_headers=True, timeout=30000)
        if r.status == 200 and len(r.body) > 2000:
            return _parse_shfe_html(r.body, d)
    except Exception as e:
        log(f"[shfe] {ds} HTML 请求失败: {e}")
    log(f"[shfe] {ds} 无数据，跳过")
    return []


# --------------------------------------------------------------- CZCE (xls<xlsx + xlrd/openpyxl)
def _open_czce_rows(fn: str, ext: str):
    if ext == "xls":
        import xlrd
        wb = xlrd.open_workbook(fn)
        sh = wb.sheet_by_index(0)
        for ri in range(sh.nrows):
            yield [str(sh.cell_value(ri, ci)) for ci in range(min(9, sh.ncols))]
    else:
        from openpyxl import load_workbook
        wb = load_workbook(fn, read_only=True, data_only=True)
        sh = wb.active
        for row in sh.iter_rows(values_only=True):
            yield [("" if v is None else str(v)) for v in list(row)[:9]]


def fetch_czce(d: _date) -> list[dict]:
    from scrapling.fetchers import Fetcher
    ds = d.strftime("%Y%m%d")
    ext = "xlsx" if int(ds) > 20251101 else "xls"
    url = f"http://www.czce.com.cn/cn/DFSStaticFiles/Future/{d:%Y}/{ds}/FutureDataWhsheet.{ext}"
    r = Fetcher.get(url, stealthy_headers=True, timeout=30000)
    if r.status != 200 or len(r.body) < 1000:
        log(f"[czce] {ds} HTTP {r.status}/len {len(r.body)}, 跳过")
        return []
    os.makedirs(TMP_DIR, exist_ok=True)
    fn = os.path.join(TMP_DIR, f"_czce_{ds}.{ext}")
    open(fn, "wb").write(r.body)
    out = []
    cur_symbol = None
    cur_unit = "张"
    re_var = re.compile(r"品种[:：]\s*(?:[\u4e00-\u9fa5]+\s*)?([A-Za-z]{1,3})")
    re_unit = re.compile(r"单位[:：]\s*([\u4e00-\u9fa5]+)")
    for vals in _open_czce_rows(fn, ext):
        c0 = str(vals[0]).strip() if vals else ""
        if not c0:
            continue
        if c0.startswith("品种"):
            m = re_var.search(c0)
            if m:
                cur_symbol = m.group(1).upper() + "888"
            continue
        if "单位" in c0:
            m = re_unit.search(c0)
            if m:
                cur_unit = m.group(1)
            continue
        if not cur_symbol:
            continue
        if c0 in ("小计", "总计"):
            continue
        wh = str(vals[1]).strip() if len(vals) > 1 else ""
        if not wh or wh == "仓库简称":
            continue
        qty = _int(vals[5]) if len(vals) > 5 else None
        chg = _int(vals[6]) if len(vals) > 6 else None
        if qty is None and chg is None:
            continue
        out.append({
            "report_date": d, "exchange": "CZCE", "symbol": cur_symbol,
            "warehouse": wh, "receipt_qty": qty, "change_qty": chg,
            "unit": cur_unit, "src": SRC, "version": VERSION,
        })
    return out


# --------------------------------------------------------------- DCE / GFEX (浏览器会话)
def _parse_dce_ents(ents: list, d: _date) -> list[dict]:
    out = []
    for e in ents:
        var = str(e.get("variety", "") or "").strip()
        if var in ("总计",) or var.endswith("小计"):
            continue
        code = str(e.get("varietyOrder", "") or "").strip().upper()
        if not code:
            continue
        wh = str(e.get("whAbbr", "") or "").strip()
        if not wh:
            continue
        out.append({
            "report_date": d, "exchange": "DCE", "symbol": code + "888",
            "warehouse": wh, "receipt_qty": _int(e.get("wbillQty")),
            "change_qty": _int(e.get("diff") if e.get("diff") is not None else e.get("regWbillQty")),
            "unit": "吨", "src": SRC, "version": VERSION,
        })
    return out


def _parse_gfex_rows(rows: list, d: _date) -> list[dict]:
    out = []
    for e in rows:
        var = str(e.get("variety", "") or "").strip()
        if var in ("总计",) or not var:
            continue
        code = str(e.get("varietyOrder", "") or "").strip().upper()
        if not code:
            continue
        wh = str(e.get("whAbbr", "") or "").strip()
        if not wh:
            continue
        out.append({
            "report_date": d, "exchange": "GFEX", "symbol": code + "888",
            "warehouse": wh, "receipt_qty": _int(e.get("wbillQty")),
            "change_qty": _int(e.get("diff") if e.get("diff") is not None else e.get("regWbillQty")),
            "unit": "吨", "src": SRC, "version": VERSION,
        })
    return out


def fetch_browser(exchange: str, dates: list[_date]) -> dict[str, list[dict]]:
    """用单个 StealthyFetcher 会话抓 DCE/GFEX 整段日期（瑞数每次取数前需重过挑战页）。"""
    from scrapling.fetchers import StealthyFetcher
    api = DCE_API if exchange == "DCE" else GFEX_API
    home = DCE_HOME if exchange == "DCE" else GFEX_HOME
    page_url = DCE_PAGE if exchange == "DCE" else GFEX_HOME
    is_form = (exchange == "GFEX")
    results: dict[str, list[dict]] = {}

    def page_action(page):
        _safe_goto(page, home, 2500)
        for d in dates:
            ds = d.strftime("%Y%m%d")
            try:
                if exchange == "DCE":
                    _safe_goto(page, page_url, 3500)
                    payload = {"tradeDate": ds, "varietyId": "all"}
                    res = page.evaluate(JS_FETCH, [api, payload, False])
                    raw = base64.b64decode(res["b64"]) if res.get("b64") else b""
                    rows = []
                    if raw[:1] in (b"{", b"["):
                        data = json.loads(raw)
                        ents = (data.get("data") or {}).get("entityList") or []
                        rows = _parse_dce_ents(ents, d)
                else:
                    body = ("gen_date=" + ds).encode()
                    resp = page.request.post(api, data=body,
                                             headers={"Content-Type": "application/x-www-form-urlencoded",
                                                      "Referer": GFEX_HOME,
                                                      "Accept": "application/json, text/plain, */*"})
                    raw = resp.body()
                    rows = []
                    if raw[:1] in (b"{", b"["):
                        data = json.loads(raw)
                        rows = _parse_gfex_rows(data.get("data") or [], d)
                # 按日即时落库：即使后续日期崩溃也不丢已抓数据
                n = upsert(rows)
                results[ds] = n
                log(f"  [{exchange}] {ds} 入库 {n} 行")
            except Exception as ex:
                log(f"  [{exchange}] {ds} 失败: {type(ex).__name__}: {ex}")
                results[ds] = 0
        return page

    exe = pick_browser()
    kwargs = dict(headless=True, network_idle=False, timeout=180000, wait=2000,
                  locale="zh-CN", google_search=False, retries=1, page_action=page_action)
    if exe:
        kwargs["executable_path"] = exe
    StealthyFetcher.fetch(home, **kwargs)
    return sum(results.values())


# --------------------------------------------------------------- 驱动
def collect_one(d: _date, exchange: str) -> int:
    if exchange == "SHFE":
        return upsert(fetch_shfe(d))
    if exchange == "CZCE":
        return upsert(fetch_czce(d))
    if exchange in ("DCE", "GFEX"):
        return fetch_browser(exchange, [d])
    log(f"[warehouse_receipt] {exchange} 未知交易所，跳过")
    return 0


def collect_range(start: _date, end: _date, exchanges: list[str]) -> int:
    days = [d for d in (start + _dt.timedelta(days=i) for i in range((end - start).days + 1))
            if d.isoweekday() <= 5]
    total = 0
    # Fetcher 类（SHFE/CZCE）逐日；浏览器类（DCE/GFEX）整段一次会话
    for ex in exchanges:
        if ex in ("SHFE", "CZCE"):
            for d in days:
                total += collect_one(d, ex)
        else:
            done = existing_dates(ex, start, end)
            todo = [d for d in days if d not in done]
            log(f"[{ex}] 已存在 {len(done)} 天，待抓取 {len(todo)} 天")
            if todo:
                total += fetch_browser(ex, todo)
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description="仓单日报采集（Scrapling 版，四所）")
    ap.add_argument("--date", help="单日 YYYY-MM-DD")
    ap.add_argument("--start", help="开始 YYYY-MM-DD")
    ap.add_argument("--end", help="结束 YYYY-MM-DD（含）")
    ap.add_argument("--exchange", default="all",
                    help="SHFE/CZCE/DCE/GFEX/all（默认 all=四所依次）")
    args = ap.parse_args()
    exs = ["SHFE", "CZCE", "DCE", "GFEX"] if args.exchange == "all" else [args.exchange.upper()]

    if args.date:
        d = _date.fromisoformat(args.date)
        tot = sum(collect_one(d, ex) for ex in exs)
        log(f"[warehouse_receipt] {d} 入库 {tot} 行")
    else:
        s = _date.fromisoformat(args.start) if args.start else _date.today()
        e = _date.fromisoformat(args.end) if args.end else s
        tot = collect_range(s, e, exs)
        log(f"[warehouse_receipt] 区间合计入库 {tot} 行")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
