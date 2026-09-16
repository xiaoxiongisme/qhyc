# -*- coding: utf-8 -*-
"""期货全品种 1h/15min 行情批量入库（天勤 tqsdk → PostgreSQL）。

- 免费账号：get_kline_serial 尾部回退（连续主连 + 分合约都取），1h 主连约到 2020-10、
  15min 主连约到 2024-11（均受 10000 根上限约束）；分合约用正确代码（郑商所 3 位）可取到，
  串起来覆盖主连窗口。
- 专业版：自动探测到 get_kline_data_series 时间窗特性后，按年/季切窗拉全 2020 起历史。

落库：app.ingest.fdf.db_pg（fut_kline 超表，kind∈{continuous,contract,cont_adj}）。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import sys
import time

from app.ingest.fdf import symbols as SYM
from app.ingest.fdf import tqhelper as H
from app.ingest.fdf import db_pg as D
from tqsdk import TqApi

DURATION = D.DURATION

# 免费尾部模式单次上限（天勤硬上限 10000）
TAIL_CAP = 10000
TAIL_LEN = {"hourly": 10000, "min15": 10000}
TAIL_LEN_CONTRACT = {"hourly": 9000, "min15": 10000}

# 各频率的起始年（15min 受 10000 根限制主连只能到 ~2024，故从 2024 起取分合约即可）
FREQ_START_YEAR = {"hourly": 2020, "min15": 2024}

# 用户优先品种（排到最前，中途断了先覆盖重要品种）
PRIORITY = ["CZCE.FG", "CZCE.SA", "INE.BR", "CZCE.MA", "DCE.EG", "DCE.JD"]

TIMEOUT_PER_BATCH = 60
MAX_RETRY = 2
RETRY_WAIT = 6
PROD_COOLDOWN = 5  # 品种间冷却，降低免费服务被限流的概率
MAX_CONSEC_FAIL = 5  # 连续这么多合约取不到即判定数据缺失/会话中止，提前结束该品种
BLACKLIST = set()


def all_symbols():
    keys = list(SYM.TABLE.keys())
    head = [k for k in PRIORITY if k in keys]
    rest = [k for k in keys if k not in head]
    return head + rest


def supports_windowed(api):
    """探测是否支持 get_kline_data_series（专业版 tq_dl）。"""
    try:
        api.get_kline_data_series("SHFE.rb2601", 86400,
                                  _dt.date(2024, 1, 2), _dt.date(2024, 1, 3))
        return True
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        if "专业版" in msg or "tq_dl" in msg:
            return False
        return True


def fetch_windowed(api, code, dur, freq, start_dt, end_dt):
    rows = []
    cur = start_dt
    wd = {"hourly": 450, "min15": 120}.get(freq, 200)
    while cur < end_dt:
        nxt = min(cur + _dt.timedelta(days=wd), end_dt)
        try:
            kl = api.get_kline_data_series(code, dur, cur, nxt)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "专业版" in msg or "tq_dl" in msg:
                raise PermissionError("WINDOWED_NEEDS_PRO")
            time.sleep(RETRY_WAIT)
            try:
                kl = api.get_kline_data_series(code, dur, cur, nxt)
            except Exception:  # noqa: BLE001
                print(f"    窗口 {cur.date()}~{nxt.date()} 取数失败，跳过")
                cur = nxt
                continue
        if kl is not None and len(kl):
            rows.extend(H.to_rows(kl))
        cur = nxt
    return rows or None


def fetch_tail(api, code, dur, data_len):
    dl = min(int(data_len), TAIL_CAP)
    for attempt in range(MAX_RETRY + 1):
        try:
            kl = api.get_kline_serial(code, dur, data_length=dl)
        except Exception as e:  # noqa: BLE001
            if attempt < MAX_RETRY:
                print(f"    订阅失败 {code}: {type(e).__name__}（重试）")
                time.sleep(RETRY_WAIT)
                continue
            return None
        left = H.wait_stable(api, {code: kl}, timeout=TIMEOUT_PER_BATCH, need=5)
        if left and attempt < MAX_RETRY:
            time.sleep(RETRY_WAIT)
            continue
        if left:
            return None
        return H.to_rows(kl) or None
    return None


def fetch_continuous(api, spec, freq, start_dt, end_dt, windowed, full):
    """抓主连。成功返回行列表（供上层取起始时间），失败返回 None。"""
    code = spec["tq_cont"]
    dur = DURATION[freq]
    if windowed:
        rows = fetch_windowed(api, code, dur, freq, start_dt, end_dt)
    else:
        rows = fetch_tail(api, code, dur, TAIL_LEN[freq])
    if not rows:
        print(f"  [主连] {code} 取数失败")
        return None
    n = D.save_bars(freq, "continuous", code, rows)
    print(f"  [主连] {code}: {n} 根  {rows[0]['date']} ~ {rows[-1]['date']}")
    return rows


def _contract_ym(code, spec):
    """由合约代码解析 (年, 月)。4 位码：yy+mm；3 位码（郑商所）：个位年+mm。"""
    suffix = code.split(".", 1)[1][len(spec["code"]):]
    if spec["code_digits"] == 3:
        y = 2020 + int(suffix[:-2])
    else:
        y = 2000 + int(suffix[:-2])
    return (y, int(suffix[-2:]))


def fetch_contracts(api, spec, freq, start_year, windowed, start_dt, end_dt, min_ym=None):
    """抓该品种全部具体合约。

    关键：天勤免费会话一旦「首个订阅」打到不存在的合约（已下市），会把整个会话打坏，
    后续有效合约也全部 TqTimeoutError。因此这里：
      1) 合约按「新→旧」顺序遍历，保证首个永远是有效合约；
      2) 4 位码品种直接剔除早于主连数据起始（min_ym）的已下市合约。
    """
    contracts = SYM.contract_codes(spec["key"], from_year=start_year)
    if spec["code_digits"] == 4 and min_ym:
        contracts = [c for c in contracts if _contract_ym(c, spec) >= min_ym]
    contracts = list(reversed(contracts))  # 新→旧
    dur = DURATION[freq]
    done = 0
    failed = []
    consec = 0
    for c in contracts:
        if c in BLACKLIST:
            continue
        rows = None
        try:
            if windowed:
                rows = fetch_windowed(api, c, dur, freq, start_dt, end_dt)
            else:
                rows = fetch_tail(api, c, dur, TAIL_LEN_CONTRACT[freq])
        except PermissionError:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"    订阅失败 {c}: {type(e).__name__}（跳过）")
        if rows:
            rows = D.filter_contract_rows(rows, c, spec["code"], spec["code_digits"])
        if not rows:
            failed.append(c)
            consec += 1
            if consec >= MAX_CONSEC_FAIL:
                print(f"  [合约] 连续 {consec} 个取不到，疑似数据缺失/会话中止，提前结束该品种")
                break
            continue
        D.save_bars(freq, "contract", c, rows)
        done += 1
        consec = 0
        print(f"    {c}: {len(rows):>4} 根  {rows[0]['date']} ~ {rows[-1]['date']}", flush=True)
    if failed:
        BLACKLIST.update(failed)
        print(f"  [合约] {len(failed)} 个取不到（已下市/未保留/限流），已进黑名单")
    return done


def run(freqs, symbols=None, start=None, full=False, tail=False):
    freqs = freqs or ["hourly", "min15"]
    for f in freqs:
        if f not in D.FREQS:
            print(f"未知频率 {f}，可选：{list(D.FREQS)}")
            sys.exit(1)
    syms = symbols or all_symbols()
    start_year = {f: FREQ_START_YEAR.get(f, int(str(start)[:4])) for f in freqs}
    start_dt = {f: _dt.datetime(int(start_year[f]), 1, 1) for f in freqs}
    end_dt = _dt.datetime.combine(_dt.date.today(), _dt.time(23, 59, 59))

    D.ensure_schema(freqs)
    auth = H.get_auth()
    state = {"api": None}

    def reconnect():
        """关旧会话、开新会话。长会话跑久了会退化（整批订阅 TqTimeoutError），
        每个品种起一个干净会话即可规避。"""
        try:
            if state["api"] is not None:
                state["api"].close()
        except Exception:  # noqa: BLE001
            pass
        state["api"] = TqApi(auth=auth)
        return state["api"]

    print("正在连接天勤行情…")
    api = reconnect()
    print("  天勤连接成功。\n")

    windowed = False
    if not tail:
        try:
            windowed = supports_windowed(api)
        except Exception:  # noqa: BLE001
            windowed = False
    if windowed:
        print("[模式] 专业版：get_kline_data_series 时间窗分批，可拉全 2020 起")
    else:
        print("[模式] 免费尾部：get_kline_serial（1h 主连约到 2020-10，15min 约到 2024-11）")

    try:
        for freq in freqs:
            print(f"\n========== 频率：{D.FREQS[freq][1]} ({freq}) ==========")
            for key in syms:
                spec = SYM.get(key)
                print(f"\n— {spec['name']}（{key}）—")
                time.sleep(PROD_COOLDOWN)
                api = reconnect()  # 每个品种起全新会话
                min_ym = None
                try:
                    crows = fetch_continuous(api, spec, freq, start_dt[freq], end_dt,
                                             windowed, full)
                    if crows:
                        d0 = str(crows[0]["date"])
                        min_ym = (int(d0[:4]), int(d0[5:7]))
                except PermissionError:
                    print("  [回退] 专业版时间窗不可用，本频率改用免费尾部重取…")
                    windowed = False
                    api = reconnect()
                    crows = fetch_continuous(api, spec, freq, start_dt[freq], end_dt,
                                             windowed, full)
                    if crows:
                        d0 = str(crows[0]["date"])
                        min_ym = (int(d0[:4]), int(d0[5:7]))
                n = fetch_contracts(api, spec, freq, start_year[freq], windowed,
                                    start_dt[freq], end_dt, min_ym)
                print(f"  具体合约入库：{n} 个")
    finally:
        try:
            state["api"].close()
        except Exception:  # noqa: BLE001
            pass
    print("\n取数完成。再跑 adjust_fdf 生成复权主连（forward，加法平移前复权）。")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="期货 1h/15min 行情入库（PostgreSQL）")
    ap.add_argument("--freq", default=None, help="只跑某频率，如 hourly/min15")
    ap.add_argument("--symbol", default=None, help="只跑某品种，如 CZCE.FG")
    ap.add_argument("--start", default="2020-01-01", help="起始日期，默认 2020-01-01")
    ap.add_argument("--full", action="store_true", help="强制重取整段")
    ap.add_argument("--tail", action="store_true", help="强制免费尾部模式")
    args = ap.parse_args()
    fs = [args.freq] if args.freq else ["hourly", "min15"]
    syms = [args.symbol] if args.symbol else None
    run(fs, symbols=syms, start=args.start, full=args.full, tail=args.tail)
