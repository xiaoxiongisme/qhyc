# -*- coding: utf-8 -*-
"""QhycBridge 离线自测（**不需要安装无限易**）。

mock 掉 ctaTemplate/ctaBase，专注验证**翻译正确性**与**安全保护**——这两类 bug
在真金白银环境里的后果最严重：

  * 翻译错：开仓写成平仓、平多发成买平 → 反向加仓，可能爆仓
  * 保护失效：重复下单（幂等失效）、手数失控（maxLots 失效）

运行：
    python scripts/infinitrader/test_qhycbridge_offline.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
for p in (REPO, HERE):
    if p not in sys.path:
        sys.path.insert(0, p)

# ------------------------------------------------------------------ 注入假无限易环境
class FakeCtaTemplate(object):
    def __init__(self, ctaEngine=None, setting: dict = {}) -> None:  # noqa: B006
        self.ctaEngine = ctaEngine
        self.calls: List[Dict[str, Any]] = []

    def output(self, *a, **k) -> None:
        pass

    def onInit(self) -> None: pass
    def onStart(self) -> None: pass
    def onStop(self) -> None: pass
    def onTick(self, t) -> None: pass
    def onOrder(self, o) -> None: pass
    def onTrade(self, t, log=False) -> None: pass  # noqa: FBT002
    def onErr(self, e) -> None: pass
    def regTimer(self, tid: int, mSecs: int) -> None: pass
    def subSymbol(self) -> None: pass          # 真实签名无参数（实测 ctaTemplate.py）
    def sendOrder(self, orderType, price, volume, symbol, exchange,
                  investor="", memo=None):    # 底层下单接口（实测签名）
        self.calls.append({"orderType": orderType, "price": price,
                           "volume": volume, "symbol": symbol,
                           "exchange": exchange, "investor": investor})
        return 90


mod = types.ModuleType("ctaTemplate")
mod.CtaTemplate = FakeCtaTemplate  # type: ignore[attr-defined]
sys.modules["ctaTemplate"] = mod
base = types.ModuleType("ctaBase")
sys.modules["ctaBase"] = base

import QhycBridge as qb  # noqa: E402

PASS: List[str] = []
FAIL: List[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print("  [PASS] {0}".format(name))
    else:
        FAIL.append("{0} | {1}".format(name, detail))
        print("  [FAIL] {0} | {1}".format(name, detail))


def make_bridge(**kw) -> Any:
    setting = {"signalUrl": "http://127.0.0.1:8000/execution/bridge",
               "dryRun": "0", "maxLots": 5, "pollSeconds": 3}
    setting.update(kw)
    b = qb.QhycBridge(None, setting)
    b.trading = True
    b._statePath = os.path.join(tempfile.gettempdir(), "qhyc_bridge_test_state.json")
    b._seen = set()
    b._orderMap = {}
    # 拦住真实 HTTP
    b._http = lambda method, path, body=None: {"claimed": True}
    b.reports: List[dict] = []
    b._report = lambda qid, status, broker_order_id=None, filled_lots=0, \
        avg_price=0.0, error=None: b.reports.append(
            {"qid": qid, "status": status, "broker_order_id": broker_order_id,
             "filled_lots": filled_lots, "avg_price": avg_price, "error": error})
    return b


# ============================================================ 1. 合约代码解析
print("=" * 72)
print("1. 合约代码解析（qhyc 写法 → CTP 原生码）")
print("=" * 72)
CASES = [
    ("FG2701", "CZCE", "FG701", "郑商所 4 位→3 位"),
    ("RB2610", "SHFE", "rb2610", "上期所转小写"),
    ("m2601", "DCE", "m2601", "大商所小写"),
    ("IF2609", "CFFEX", "IF2609", "中金所大写"),
    ("sc2610", "INE", "sc2610", "能源中心小写"),
    ("si2611", "GFEX", "si2611", "广期所小写"),
]
b0 = make_bridge()
for src, ex, expect, desc in CASES:
    got = b0._resolve_symbol(src, ex)
    check("{0} {1}+{2} -> {3}".format(desc, src, ex, expect), got == expect,
          "实际 {0!r}".format(got))
# 已是原生码的输入不应被二次截位（幂等）
check("已是原生码 FG701 不重复截位", b0._resolve_symbol("FG701", "CZCE") == "FG701")
check("缺交易所：返回空 → 拒绝下单（不猜）", b0._resolve_symbol("FG2701", "") == "")
check("未知交易所：返回空 → 拒绝下单（不猜）",
      b0._resolve_symbol("RB2610", "UNKNOWN_EXCH") == "")
check("品种/数字缺失：返回空", b0._resolve_symbol("RB", "SHFE") == "")


# ============================================================ 2. 开仓方向
print()
print("=" * 72)
print("2. 开仓：LONG=buy / SHORT=short（写反就是反向交易）")
print("=" * 72)


def spy_open(b: Any) -> None:
    b.buy_calls: List[tuple] = []
    b.short_calls: List[tuple] = []
    b.buy = lambda price, volume, symbol=None, exchange=None, memo=None, \
        investor=None: (b.buy_calls.append((price, volume, symbol, exchange, investor)), 1)[1]
    b.short = lambda price, volume, symbol=None, exchange=None, memo=None, \
        investor=None: (b.short_calls.append((price, volume, symbol, exchange, investor)), 2)[1]


b1 = make_bridge()
spy_open(b1)
b1._execute({"id": 1, "real_symbol": "RB2610", "exchange": "SHFE", "lots": 2,
             "price": 3200.0, "action": "OPEN", "direction": "BUY"})
check("OPEN+BUY 调用 buy", len(b1.buy_calls) == 1 and len(b1.short_calls) == 0,
      "buy={0} short={1}".format(len(b1.buy_calls), len(b1.short_calls)))
check("buy 参数(价/手/合约/交易所)正确",
      b1.buy_calls and b1.buy_calls[0][:4] == (3200.0, 2, "rb2610", "SHFE"),
      "{0}".format(b1.buy_calls))

b2 = make_bridge()
spy_open(b2)
b2._execute({"id": 2, "real_symbol": "RB2610", "exchange": "SHFE", "lots": 2,
             "price": 3200.0, "action": "OPEN", "direction": "SELL"})
check("OPEN+SELL 调用 short", len(b2.short_calls) == 1 and len(b2.buy_calls) == 0,
      "buy={0} short={1}".format(len(b2.buy_calls), len(b2.short_calls)))


# ============================================================ 3. 平仓方向与今昨
print()
print("=" * 72)
print("3. 平仓：平多=sell / 平空=buy，SHFE/INE 才拆今昨")
print("=" * 72)


def spy_close(b: Any) -> None:
    b.close_calls: List[dict] = []
    b.cover_calls: List[tuple] = []
    b.sell_calls: List[tuple] = []
    b.auto_close_position = lambda **kw: (b.close_calls.append(kw), 77)[1]
    b.cover = lambda price, volume, symbol=None, exchange=None, stop=False, \
        investor=None: (b.cover_calls.append((price, volume)), 78)[1]
    b.sell = lambda price, volume, symbol=None, exchange=None, stop=False, \
        investor=None: (b.sell_calls.append((price, volume)), 79)[1]


def do_close(action: str, direction: str, exchange: str, symbol: str) -> dict:
    bb = make_bridge()
    spy_close(bb)
    bb._execute({"id": 3, "real_symbol": symbol, "exchange": exchange, "lots": 1,
                 "price": 100.0, "action": action, "direction": direction})
    return bb.close_calls[0] if bb.close_calls else {}


kw = do_close("CLOSE", "SELL", "CZCE", "FG2701")
check("平多(SELL)用 sell", kw.get("order_direction") == "sell", str(kw))
check("CZCE 不传 shfe_close_first（该所指今昨由成交决定）",
      "shfe_close_first" not in kw, str(kw))

kw2 = do_close("CLOSE", "BUY", "CZCE", "FG2701")
check("平空(BUY)用 buy", kw2.get("order_direction") == "buy", str(kw2))

kw3 = do_close("CLOSE_TODAY", "SELL", "SHFE", "RB2610")
check("SHFE 平今：shfe_close_first=False（今仓优先）",
      kw3.get("shfe_close_first") is False, str(kw3))

kw4 = do_close("CLOSE_YEST", "SELL", "SHFE", "RB2610")
check("SHFE 平昨：shfe_close_first=True（昨仓优先）",
      kw4.get("shfe_close_first") is True, str(kw4))

kw5 = do_close("CLOSE_TODAY", "SELL", "INE", "sc2610")
check("INE 同样区分今昨（与 SHFE 一致）",
      kw5.get("shfe_close_first") is False, str(kw5))

# ﻿开平混淆专项：CLOSE+LONG 绝不能走 buy（那是平空的语法）
check("★ CLOSE+LONG 未误用 buy（平多发成买平=反向加仓）",
      kw.get("order_direction") == "sell" and kw2.get("order_direction") == "buy",
      "close_long={0} close_short={1}".format(kw.get("order_direction"),
                                              kw2.get("order_direction")))


# ============================================================ 4. 安全保护
print()
print("=" * 72)
print("4. 安全保护：幂等 / 手数上限 / 非法参数")
print("=" * 72)

b4 = make_bridge()
spy_open(b4)
order = {"id": 99, "real_symbol": "RB2610", "exchange": "SHFE", "lots": 1,
         "price": 3200.0, "action": "OPEN", "direction": "BUY"}
b4._handle(order)
n_after1 = len(b4.buy_calls)
b4._handle(order)   # 同一单再来一次
n_after2 = len(b4.buy_calls)
check("幂等：同一 order_id 只下单一次", n_after1 == 1 and n_after2 == 1,
      "第一次 {0} 次，第二次后 {1} 次".format(n_after1, n_after2))

b5 = make_bridge(maxLots=2)
spy_open(b5)
b5._execute({"id": 5, "real_symbol": "RB2610", "exchange": "SHFE", "lots": 500,
             "price": 3200.0, "action": "OPEN", "direction": "BUY"})
check("maxLots 截断：500 手被限到 2 手",
      b5.buy_calls and b5.buy_calls[0][1] == 2, "{0}".format(b5.buy_calls))

b6 = make_bridge()
try:
    b6._execute({"id": 6, "real_symbol": "RB2610", "exchange": "SHFE", "lots": 0,
                 "price": 3200.0, "action": "OPEN", "direction": "BUY"})
    check("lots=0 应抛异常", False, "未抛异常（会下废单）")
except ValueError:
    check("lots=0 被拦截（fail-loud）", True)

b7 = make_bridge()
try:
    b7._execute({"id": 7, "real_symbol": "RB2610", "exchange": "SHFE", "lots": 1,
                 "price": 0, "action": "OPEN", "direction": "BUY"})
    check("price=0 应抛异常", False, "未抛异常")
except ValueError:
    check("price=0 被拦截（避免 0 价单）", True)

b8 = make_bridge()
try:
    b8._execute({"id": 8, "real_symbol": "RB2610", "exchange": "SHFE", "lots": 1,
                 "price": 3200.0, "action": "UNKNOWN", "direction": "LONG"})
    check("未知 action 应抛异常", False, "未抛异常")
except ValueError:
    check("未知 action 被拦截（不静默通过）", True)

b9 = make_bridge()
try:
    b9._execute({"id": 9, "real_symbol": "RB2610", "exchange": "UNKNOWN_EXCH",
                 "lots": 1, "price": 3200.0, "action": "OPEN", "direction": "BUY"})
    check("未知交易所应抛异常", False, "未抛异常")
except ValueError:
    check("未知交易所被拦截", True)


# ============================================================ 5. dryRun
print()
print("=" * 72)
print("5. dryRun：只打日志，绝不发单")
print("=" * 72)
b10 = make_bridge(dryRun="1")
spy_open(b10)
spy_close(b10)
b10._execute({"id": 10, "real_symbol": "RB2610", "exchange": "SHFE", "lots": 1,
              "price": 3200.0, "action": "OPEN", "direction": "BUY"})
b10._execute({"id": 11, "real_symbol": "RB2610", "exchange": "SHFE", "lots": 1,
              "price": 3200.0, "action": "CLOSE", "direction": "SELL"})
check("dryRun 下未调用任何下单 API",
      not b10.buy_calls and not b10.short_calls and not b10.close_calls,
      "buy={0} short={1} close={2}".format(len(b10.buy_calls), len(b10.short_calls),
                                           len(b10.close_calls)))
check("dryRun 仍回报（便于观察链路）",
      any(r["status"] == "DRYRUN" for r in b10.reports), str(b10.reports))


# ============================================================ 6. 老版回退
print()
print("=" * 72)
print("6. 老版本无限易回退（无 auto_close_position 时）")
print("=" * 72)
LOT = qb.QhycBridge._legacy_order_type
check("回退 平多(SELL)+CLOSE_TODAY+SHFE → 卖平今",
      LOT("sell", "CLOSE_TODAY", True) == "卖平今", str(LOT("sell", "CLOSE_TODAY", True)))
check("回退 平多(SELL)+CLOSE_YEST+SHFE → 卖平(即 CTP 平昨)",
      LOT("sell", "CLOSE_YEST", True) == "卖平", str(LOT("sell", "CLOSE_YEST", True)))
check("回退 平空(BUY)+CLOSE_TODAY+SHFE → 买平今",
      LOT("buy", "CLOSE_TODAY", True) == "买平今", str(LOT("buy", "CLOSE_TODAY", True)))
check("回退 平空(BUY)+CLOSE_YEST → 买平",
      LOT("buy", "CLOSE_YEST", True) == "买平", str(LOT("buy", "CLOSE_YEST", True)))
check("回退 不区分今昨的交易所(DCE/CZCE) → 卖平（通用平仓）",
      LOT("sell", "CLOSE", False) == "卖平", str(LOT("sell", "CLOSE", False)))
check("回退 不区分今昨的交易所(DCE/CZCE) 平空 → 买平",
      LOT("buy", "CLOSE", False) == "买平", str(LOT("buy", "CLOSE", False)))

# 老版实例真的会走 sendOrder（而**不是**已被废弃的空实现 cover/sell）
# 实测本机 ctaTemplate.py：`cover`/`sell` 带 @deprecated 且函数体是 `...`（返回 None）
def spy_stub(b, name):
    def _boom(*a, **k):
        b._stub_hit.append(name)
        return None        # 真实废弃桩就是返回 None
    b.__dict__[name] = _boom

b12 = make_bridge()
b12._stub_hit = []
for _n in ("cover", "sell", "cover_t", "cover_y", "sell_t", "sell_y"):
    spy_stub(b12, _n)
b12.__dict__.pop("auto_close_position", None)  # 模拟老版本：没有 auto_close_position
b12.calls = []
b12._execute({"id": 12, "real_symbol": "RB2610", "exchange": "SHFE", "lots": 1,
              "price": 100.0, "action": "CLOSE_TODAY", "direction": "SELL"})
check("老版回退走 sendOrder 且下单类型=卖平今",
      b12.calls and b12.calls[0]["orderType"] == "卖平今", str(b12.calls))
check("老版回退未误调已废弃的 sell/cover 空实现",
      not b12._stub_hit, "命中 {0}".format(b12._stub_hit))

b13 = make_bridge()
b13._stub_hit = []
for _n in ("cover", "sell", "cover_t", "cover_y", "sell_t", "sell_y"):
    spy_stub(b13, _n)
b13.__dict__.pop("auto_close_position", None)
b13.calls = []
b13._execute({"id": 13, "real_symbol": "RB2610", "exchange": "SHFE", "lots": 1,
              "price": 100.0, "action": "CLOSE_TODAY", "direction": "BUY"})
check("老版回退 平空(BUY)+平今 → 买平今（不是卖平今）",
      b13.calls and b13.calls[0]["orderType"] == "买平今", str(b13.calls))

print()
print("=" * 72)
print("通过 {0} 项，失败 {1} 项".format(len(PASS), len(FAIL)))
if FAIL:
    for f in FAIL:
        print("  x {0}".format(f))
    print("=" * 72)
    raise SystemExit(1)
print("全部通过：QhycBridge 翻译逻辑与安全保护正确")
print("=" * 72)
raise SystemExit(0)
