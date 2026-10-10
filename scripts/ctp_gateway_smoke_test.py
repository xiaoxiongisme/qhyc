# -*- coding: utf-8 -*-
"""CTP 网关 HTTP 接口冒烟测试（**不需要真实 CTP 账号**）。

验证的是 contract 层而非 CTP 层：mock 掉 CtpSession 的下单/查单/撤单，用 FastAPI
TestClient 打真实路由，重点守住三条**会引发真实资金事故**的约定：

  T1. TIMEOUT_UNKNOWN 必须返回 **HTTP 200**。
      app/execution/broker_http.py 看到 status>=400 就当 REJECT 落 REJECTED 状态，
      若网关用 4xx 表达超时，超时单会被误判成拒单 → 上层重下 → **重复成交**。
  T2. REJECT 必须带 error 字段且 http>=400 或 state=REJECT（两者其一，不得同时缺失）。
  T3. 非法 lots / 缺失 price / 未知合约 → 4xx 明确拒绝，不静默通过。

运行：
    python scripts/ctp_gateway_smoke_test.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from fastapi.testclient import TestClient
except ImportError:
    sys.stderr.write("需要 httpx + pytest：pip install httpx pytest\n")
    raise SystemExit(2)

import scripts.ctp_gateway as gw  # noqa: E402

# ------------------------------------------------------------------ 用例
PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  [PASS] {name}")
    else:
        FAIL.append(f"{name}｜{detail}")
        print(f"  [FAIL] {name}｜{detail}")


class FakeSession:
    """替身：不连 CTP，按脚本设定的剧本返回结果。"""

    def __init__(self, mode: str = "ack") -> None:
        self.mode = mode
        self.ready = _FakeEvent(True)
        self.instrument_ready = _FakeEvent(True)
        self.login_error = None
        self.trading_day = "20261008"
        self.front_id = 1
        self.session_id = 1001
        self.instruments = {"FG701": {"instrument": "FG701", "exchange": "CZCE",
                                      "tick": 1.0, "multiplier": 20, "digits": "701",
                                      "product": "FG"}}
        self.orders: dict = {
            # 忠实模拟：下单后订单会进入订单簿，撤单/查单才能在里面找到
            "1": {"order_id": "1", "instrument": "FG701", "status": "SENT",
                  "filled_lots": 0, "avg_fill_price": 0.0, "lots": 2},
            "3": {"order_id": "3", "instrument": "FG701", "status": "ACCEPTED",
                  "filled_lots": 0, "avg_fill_price": 0.0, "lots": 2},
        }

    def start(self, timeout: float = 0.0) -> bool:
        return True

    def submit(self, symbol, direction, action, price, lots):
        # 忠实复刻真实会话：先解析合约（未知合约抛 ValueError → 上层转 4xx）
        self.resolve(symbol)
        if self.mode == "ack":
            return {"state": "ACK", "order_id": "1", "status": "SENT",
                    "filled_lots": 0, "avg_fill_price": 0.0}
        if self.mode == "timeout":
            return {"state": "TIMEOUT_UNKNOWN", "order_id": "2", "detail": "未收到回执"}
        if self.mode == "reject":
            return {"state": "REJECT", "order_id": "3", "error": "资金不足"}
        raise ValueError(f"合约 {symbol!r} 在合约表中不存在")

    def cancel(self, ref):
        # 忠实复刻真实会话：订单簿里没有就要 KeyError → 上层转 404
        if ref not in self.orders:
            raise KeyError(ref)
        if self.mode == "reject":
            return {"state": "REJECT", "order_id": ref, "error": "订单状态 FILLED 不可撤"}
        return {"state": "OK", "order_id": ref, "status": "CANCELED"}

    def get_order(self, ref):
        if ref == "missing":
            return None
        return {"order_id": ref, "status": "FILLED", "filled_lots": 2,
                "avg_fill_price": 1234.5}

    def resolve(self, symbol):
        if symbol.upper() == "FG2701":
            return {"instrument": "FG701", "exchange": "CZCE", "tick": 1.0, "multiplier": 20}
        raise ValueError(f"合约 {symbol!r} 在合约表中不存在")


class _FakeEvent:
    def __init__(self, val: bool) -> None:
        self._v = val

    def is_set(self) -> bool:
        return self._v

    def wait(self, timeout: float = 0.0) -> bool:
        return self._v


def run() -> int:
    print("=" * 70)
    print("CTP 网关 HTTP 契约冒烟测试（mock CTP，不联网）")
    print("=" * 70)

    # ---------- T1：超时必须 200
    gw.SESSION = FakeSession("timeout")
    client = TestClient(gw.app)
    r = client.post("/order", json={"real_symbol": "FG2701", "direction": "LONG",
                                    "action": "OPEN", "price": 1100.0, "lots": 2})
    body = r.json() if r.status_code != 500 else {}
    check("T1 超时返回 HTTP 200（不能 4xx，否则被上层误判 REJECT）",
          r.status_code == 200, f"实际 {r.status_code} body={body}")
    check("T1b 超时响应 state=TIMEOUT_UNKNOWN",
          body.get("state") == "TIMEOUT_UNKNOWN", f"实际 {body.get('state')}")
    check("T1c 超时响应带 order_id（供查单用）",
          bool(body.get("order_id")), f"实际 {body.get('order_id')}")

    # ---------- T2：拒单
    gw.SESSION = FakeSession("reject")
    r2 = client.post("/order", json={"real_symbol": "FG2701", "direction": "LONG",
                                     "action": "OPEN", "price": 1100.0, "lots": 2})
    b2 = r2.json()
    check("T2 拒单 state=REJECT", b2.get("state") == "REJECT", f"实际 {b2.get('state')}")
    check("T2b 拒单带 error 字段", bool(b2.get("error")), f"实际 {b2.get('error')}")

    # ---------- T3：参数校验
    gw.SESSION = FakeSession("ack")
    r3 = client.post("/order", json={"real_symbol": "FG2701", "direction": "LONG",
                                     "action": "OPEN", "lots": 2})
    check("T3 缺失 price → 4xx", r3.status_code >= 400, f"实际 {r3.status_code}")

    r3b = client.post("/order", json={"real_symbol": "FG2701", "direction": "LONG",
                                      "action": "OPEN", "price": 1100.0, "lots": 0})
    check("T3b lots=0 → 4xx", r3b.status_code >= 400, f"实际 {r3b.status_code}")

    r3c = client.post("/order", json={"real_symbol": "ZZ9999", "direction": "LONG",
                                      "action": "OPEN", "price": 1100.0, "lots": 1})
    check("T3c 未知合约 → 4xx（不静默通过）", r3c.status_code >= 400, f"实际 {r3c.status_code}")

    # ---------- T4：查单 / 撤单
    r4 = client.get("/order/1")
    check("T4 查单 200 + 返回订单", r4.status_code == 200 and r4.json().get("order_id") == "1",
          f"实际 {r4.status_code} {r4.text[:80]}")
    r4b = client.get("/order/missing")
    check("T4b 查不存在的单 → 404（上层映射为 NOT_FOUND）", r4b.status_code == 404,
          f"实际 {r4b.status_code}")

    r4c = client.post("/order/1/cancel")
    check("T4c 撤单成功 200", r4c.status_code == 200, f"实际 {r4c.status_code} {r4c.text[:80]}")
    r4d = client.post("/order/missing/cancel")
    check("T4d 撤不存在的单 → 404", r4d.status_code == 404, f"实际 {r4d.status_code}")

    gw.SESSION = FakeSession("reject")
    r4e = client.post("/order/3/cancel")
    check("T4e 不可撤的单 → 400（上层映射 REJECT）", r4e.status_code == 400,
          f"实际 {r4e.status_code} {r4e.text[:80]}")

    # ---------- T5：health / instruments
    r5 = client.get("/health")
    b5 = r5.json()
    check("T5 health healthy=true", b5.get("healthy") is True, f"实际 {b5}")
    check("T5b health 暴露 trading_day", bool(b5.get("trading_day")), f"实际 {b5.get('trading_day')}")

    r5b = client.get("/instruments/FG2701")
    check("T5c 合约解析 FG2701 → FG701（郑商所 3 位）",
          r5b.status_code == 200 and r5b.json().get("instrument") == "FG701",
          f"实际 {r5b.status_code} {r5b.text[:80]}")
    r5c = client.get("/instruments/XX9999")
    check("T5d 无法解析的合约 → 404", r5c.status_code == 404, f"实际 {r5c.status_code}")

    # ---------- T6：action 映射完整性
    expected = {("OPEN", "LONG"), ("OPEN", "SHORT"), ("CLOSE", "LONG"), ("CLOSE", "SHORT"),
                ("CLOSETODAY", "LONG"), ("CLOSETODAY", "SHORT"),
                ("CLOSEYESTERDAY", "LONG"), ("CLOSEYESTERDAY", "SHORT")}
    check("T6 action×direction 八组合齐全", set(gw.ACTION_MAP) == expected,
          f"缺失 {expected - set(gw.ACTION_MAP)}")

    # 开/平的买卖方向必须相反，写错就是反向加仓的灾难
    ok_pairs = all(
        gw.ACTION_MAP[(a, "LONG")][0] != gw.ACTION_MAP[(a, "SHORT")][0]
        for a in ("OPEN", "CLOSE", "CLOSETODAY", "CLOSEYESTERDAY")
    )
    check("T6b 同一 action 下 LONG/SHORT 的买卖方向必须相反", ok_pairs, "存在同向映射")

    # 开仓用 OF_Open，平仓用各自的 Close*
    check("T6c OPEN 映射到 Open 标志",
          gw.ACTION_MAP[("OPEN", "LONG")][1] == str(gw.tdapi.THOST_FTDC_OF_Open),
          f"实际 {gw.ACTION_MAP[('OPEN','LONG')][1]}")
    check("T6d CLOSETODAY 映射到 CloseToday 标志",
          gw.ACTION_MAP[("CLOSETODAY", "LONG")][1] == str(gw.tdapi.THOST_FTDC_OF_CloseToday),
          f"实际 {gw.ACTION_MAP[('CLOSETODAY','LONG')][1]}")

    # ---------- 汇总
    print("\n" + "=" * 70)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    if FAIL:
        for f in FAIL:
            print(f"  ✗ {f}")
        print("=" * 70)
        return 1
    print("全部通过：网关 HTTP 契约与 app/execution/broker_http.py 对齐")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
