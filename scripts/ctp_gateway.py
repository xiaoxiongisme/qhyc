# -*- coding: utf-8 -*-
"""qhyc · CTP 网关进程（Sprint 2）—— 把 CTP 柜台包装成 HTTP 三态接口。

为什么是独立进程，而不是 qhyc 库内直连
---------------------------------------
CTP 的 Python 绑定是**带状态的 C 扩展 + 回调线程**，与 qhyc 容器内的其它任务共用
同一个解释器极易出问题（尤其行情回调洪峰会拖垮定时作业）。独立成进程后：
  * qhyc 侧保持纯 HTTP 客户端（`app/execution/broker_http.py` 一套代码通吃 CTP/QMT）；
  * 网关崩了不影响策略决策，重启网关即可；
  * Windows 本机就能跑，不必在 Linux 容器里折腾 CTP 动态库。

对外接口（严格对齐 app/execution/broker_http.py，勿自行改动）
-------------------------------------------------------------
  POST   /order                 下单 → {"order_id", "state", "status", "filled_lots", ...}
  GET    /order/{order_id}      查单 → {"order_id", "status", ...} / 404
  POST   /order/{order_id}/cancel  撤单 → {"order_id", "status"} / 400 + {"state":"REJECT"}
  GET    /health                健康探测
  GET    /instruments/{symbol}  合约查询（带解析：FG2701 → FG701）

**超时 ≠ 失败**（真实资金事故高发点）：本网关按 CTP 语义，下单 8s 内未收到**Qt报错/受理**
回执即返回 state=TIMEOUT_UNKNOWN，调用方**必须先 GET 查单再决定**，禁止直接重发——
`app/execution/execution_runtime.py` 已用 retry_count 做了第三道幂等闸。

启动
----
    set CTP_USER_ID=123456
    set CTP_PASSWORD=你的密码
    set CTP_TD_FRONT=tcp://182.254.243.31:30001
    set CTP_MD_FRONT=tcp://182.254.243.31:30011
    python scripts/ctp_gateway.py --port=8770

    # qhyc 侧
    set EXECUTION_BROKER=ctp
    set EXECUTION_BROKER_BASE_URL=http://host.docker.internal:8770
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

try:
    from openctp_ctp import mdapi, tdapi
except ImportError:
    sys.stderr.write("缺少 openctp_ctp：pip install openctp-ctp\n")
    raise SystemExit(2)

# ================================================================ 配置
BROKER_ID = os.getenv("CTP_BROKER_ID", "9999")
APP_ID = os.getenv("CTP_APP_ID", "simnow_client_test")
AUTH_CODE = os.getenv("CTP_AUTH_CODE", "0000000000000000")
#: 穿透式报备的客户端标识。SimNow 阶段用 "qhyc" 占位；
#: 期货公司申请授权码后会分配正式 AppID，届时同步替换 CTP_APP_ID 与本项。
USER_PRODUCT_INFO = (os.getenv("CTP_USER_PRODUCT_INFO") or "qhyc").strip()
USER_ID = (os.getenv("CTP_USER_ID") or "").strip()
PASSWORD = (os.getenv("CTP_PASSWORD") or "").strip()
TD_FRONT = os.getenv("CTP_TD_FRONT", "tcp://182.254.243.31:30001")
MD_FRONT = os.getenv("CTP_MD_FRONT", "tcp://182.254.243.31:30011")

#: 下单等待回执的秒数。超过即返回 TIMEOUT_UNKNOWN（不重试，交给查单）
ACK_WAIT = float(os.getenv("CTP_ACK_WAIT", "8.0"))

#: 订单流水日志（append-only JSONL）。**网关重启不丢订单**的关键：
#: 若只存内存，重启后在途订单全部消失，上层 reconcile 查单得到 NOT_FOUND，
#: 会出现「柜台实际已成交、系统以为没下过」的最坏情况。置空字符串可关闭。
ORDER_LOG = os.getenv("CTP_ORDER_LOG", "~/.qhyc/ctp_orders.jsonl")

# ================================================================ 状态映射
OST_NEW = mdapi.THOST_FTDC_OST_NoTradeQueueing
OST_PARTIAL = mdapi.THOST_FTDC_OST_PartTradedQueueing
OST_ALL = mdapi.THOST_FTDC_OST_AllTraded
OST_CANCELED = mdapi.THOST_FTDC_OST_Canceled
OST_UNKNOWN = mdapi.THOST_FTDC_OST_Unknown
OST_NOT_QUEUE = mdapi.THOST_FTDC_OST_NoTradeNotQueueing

#: CTP OrderStatus → qhyc 统一状态名（须与 persistence 的枚举一致）
STATUS_MAP: dict[Any, str] = {
    OST_NEW: "SENT",
    OST_PARTIAL: "PARTIAL",
    OST_ALL: "FILLED",
    OST_CANCELED: "CANCELED",
    OST_UNKNOWN: "UNSUBMITTED",
    OST_NOT_QUEUE: "ACCEPTED",
}

#: qhyc action × direction → (CTP Direction, CombOffsetFlag)
#: CombOffsetFlag 是**单字符字符串**字段，必须 str()，实测 openctp 常量是 int
ACTION_MAP: dict[tuple[str, str], tuple[Any, str]] = {
    ("OPEN", "LONG"):            (mdapi.THOST_FTDC_D_Buy,  str(tdapi.THOST_FTDC_OF_Open)),
    ("OPEN", "SHORT"):           (mdapi.THOST_FTDC_D_Sell, str(tdapi.THOST_FTDC_OF_Open)),
    ("CLOSE", "LONG"):           (mdapi.THOST_FTDC_D_Sell, str(tdapi.THOST_FTDC_OF_Close)),
    ("CLOSE", "SHORT"):          (mdapi.THOST_FTDC_D_Buy,  str(tdapi.THOST_FTDC_OF_Close)),
    ("CLOSETODAY", "LONG"):      (mdapi.THOST_FTDC_D_Sell, str(tdapi.THOST_FTDC_OF_CloseToday)),
    ("CLOSETODAY", "SHORT"):     (mdapi.THOST_FTDC_D_Buy,  str(tdapi.THOST_FTDC_OF_CloseToday)),
    ("CLOSEYESTERDAY", "LONG"):  (mdapi.THOST_FTDC_D_Sell, str(tdapi.THOST_FTDC_OF_CloseYesterday)),
    ("CLOSEYESTERDAY", "SHORT"): (mdapi.THOST_FTDC_D_Buy,  str(tdapi.THOST_FTDC_OF_CloseYesterday)),
}


# ================================================================ 会话
class CtpSession:
    """CTP 交易会话：登录三段式 + 合约表缓存 + 订单/成交订阅。"""

    def __init__(self) -> None:
        self.ready = threading.Event()
        self.login_error: str | None = None
        self.trading_day: str = ""
        self.session_id: int = 0
        self.front_id: int = 0

        # 合约表：InstrumentID → {instrument, exchange, tick, multiplier, name}
        self.instruments: dict[str, dict[str, Any]] = {}
        self._product_index: dict[str, list[dict[str, Any]]] = {}
        self.instrument_ready = threading.Event()

        # 订单簿：order_ref → order dict
        self.orders: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()
        self._req_id = 0
        self._id_lock = threading.Lock()

        self.api: Any = None
        self.spi: Any = None
        self._order_seq = 0

        self.log_path: Path | None = Path(ORDER_LOG).expanduser() if ORDER_LOG else None
        self._persist_lock = threading.Lock()

    # ---------------------------------------------------- 订单持久化
    def _persist(self, rec: dict[str, Any]) -> None:
        """append-only 落盘一条订单快照（须在 self._lock 持有时调用）。

        用 append-only + 后写覆盖语义，天然实现「状态机终态保护」：
        同一 order_id 后写的行代表更新的状态，回放时取最后一行。
        """
        if self.log_path is None:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            dump = {k: (sorted(v) if isinstance(v, set) else v)
                    for k, v in rec.items() if not k.startswith("_")}
            dump["_trades"] = sorted(rec.get("_trades") or [])
            with self._persist_lock, self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(dump, ensure_ascii=False, default=str) + "\n")
        except Exception as e:  # 落盘失败不能拖垮交易，但要打出来让人看见
            print(f"[CTP][WARN] 订单落盘失败 {rec.get('order_id')}: {e!r}", flush=True)

    def _replay(self) -> int:
        """启动时回放订单日志，重建内存订单簿（按 order_id 取最后一条）。"""
        if self.log_path is None or not self.log_path.exists():
            return 0
        latest: dict[str, dict[str, Any]] = {}
        try:
            for line in self.log_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(d, dict) and d.get("order_id") is not None:
                    latest[str(d["order_id"])] = d
        except Exception as e:
            print(f"[CTP][WARN] 订单日志回放失败：{e!r}", flush=True)
            return 0
        with self._lock:
            for key, d in latest.items():
                rec = dict(d)
                rec["_updated"] = threading.Event()
                rec["_updated"].set()
                rec["_trades"] = set(d.get("_trades") or [])
                self.orders[key] = rec
            digits = (int(k) for k in latest if k.isdigit())
            self._order_seq = max(digits, default=0)
        return len(latest)

    # ---------------------------------------------------- 生命周期
    def start(self, timeout: float = 20.0) -> bool:
        spi = _TraderSpi(self)
        api = tdapi.CThostFtdcTraderApi.CreateFtdcTraderApi(f"qhyc_gw_{os.getpid()}")
        api.RegisterFront(TD_FRONT)
        api.RegisterSpi(spi)
        api.SubscribePrivateTopic(tdapi.THOST_TERT_QUICK)
        api.SubscribePublicTopic(tdapi.THOST_TERT_QUICK)
        api.Init()
        self.api = api
        self.spi = spi
        if not self.ready.wait(timeout=timeout):
            self.login_error = self.login_error or f"登录超时（{timeout}s）"
            return False
        # 先回放历史订单（重启后让在途订单"复活"），再拉合约表
        if (n := self._replay()) > 0:
            print(f"[CTP] 回放历史订单 {n} 笔（来自 {self.log_path}）", flush=True)
        # 全量拉合约表：既支撑合约解析，也在 /instruments 提供 tick/multiplier
        self._qry_instruments()
        return self.instrument_ready.wait(timeout=30.0)

    def stop(self) -> None:
        try:
            self.api.Release()
        except Exception:
            pass

    def next_id(self) -> int:
        with self._id_lock:
            self._req_id += 1
            return self._req_id

    def next_order_ref(self) -> str:
        with self._lock:
            self._order_seq += 1
            return str(self._order_seq)

    # ---------------------------------------------------- 登录三段式
    def req_authenticate(self) -> None:
        req = tdapi.CThostFtdcReqAuthenticateField()
        req.BrokerID = BROKER_ID
        req.UserID = USER_ID
        req.AppID = APP_ID
        req.AuthCode = AUTH_CODE
        req.UserProductInfo = USER_PRODUCT_INFO
        self.api.ReqAuthenticate(req, self.next_id())

    def req_login(self) -> None:
        req = tdapi.CThostFtdcReqUserLoginField()
        req.BrokerID = BROKER_ID
        req.UserID = USER_ID
        req.Password = PASSWORD
        req.UserProductInfo = USER_PRODUCT_INFO
        self.api.ReqUserLogin(req, self.next_id())

    def req_settlement_confirm(self) -> None:
        req = tdapi.CThostFtdcSettlementInfoConfirmField()
        req.BrokerID = BROKER_ID
        req.InvestorID = USER_ID
        self.api.ReqSettlementInfoConfirm(req, self.next_id())

    def _qry_instruments(self) -> None:
        req = tdapi.CThostFtdcQryInstrumentField()
        self.api.ReqQryInstrument(req, self.next_id())

    # ---------------------------------------------------- 合约解析
    def resolve(self, symbol: str) -> dict[str, Any]:
        """把 qhyc 的品种+年月写法解析成 CTP 真实 InstrumentID。

        上层只给 `real_symbol`（如 FG2701），**不带交易所**，而郑商所 CTP 侧是 3 位月份
        （FG701）、上期/大商是小写（rb2610）。这里用合约表做差分解，避免上层改契约。
        """
        s = (symbol or "").strip()
        if not s:
            raise ValueError("real_symbol 为空")
        # 1) 精确
        for key in (s, s.upper(), s.lower()):
            if key in self.instruments:
                return self.instruments[key]
        # 2) 拆出字母品种 + 数字，按同品种做尾号匹配（处理郑商所 4 位→3 位）
        digits = "".join(ch for ch in s if ch.isdigit())
        product = "".join(ch for ch in s if ch.isalpha()).upper()
        if not product or not digits:
            raise ValueError(f"无法解析合约名 {symbol!r}（字母/数字部分缺失）")
        cands = self._product_index.get(product, [])
        short = digits[1:] if len(digits) == 4 else digits
        for c in cands:
            cd = c["digits"]
            if cd == digits or cd == short:
                return c
        raise ValueError(
            f"合约 {symbol!r} 在合约表中不存在"
            f"（品种={product}，已加载 {len(cands)} 个该品种合约；"
            f"注意已到期合约会从合约表消失）"
        )

    # ---------------------------------------------------- 下单
    def submit(self, symbol: str, direction: str, action: str,
               price: float, lots: int) -> dict[str, Any]:
        inst = self.resolve(symbol)
        combination = ACTION_MAP.get((action.upper(), direction.upper()))
        if combination is None:
            raise ValueError(
                f"不支持的 direction×action 组合：{direction}×{action}；"
                f"合法 action={sorted(a for a, _ in ACTION_MAP)}"
            )
        ctp_direction, offset = combination

        ref = self.next_order_ref()
        req = tdapi.CThostFtdcInputOrderField()
        req.BrokerID = BROKER_ID
        req.InvestorID = USER_ID
        req.UserID = USER_ID
        req.InstrumentID = inst["instrument"]
        req.OrderRef = ref
        req.Direction = ctp_direction
        req.CombOffsetFlag = offset
        req.CombHedgeFlag = str(tdapi.THOST_FTDC_HF_Speculation)
        req.LimitPrice = price
        req.VolumeTotalOriginal = lots
        req.OrderPriceType = mdapi.THOST_FTDC_OPT_LimitPrice
        req.TimeCondition = mdapi.THOST_FTDC_TC_GFD
        req.VolumeCondition = mdapi.THOST_FTDC_VC_AV
        req.MinVolume = 1
        req.ContingentCondition = mdapi.THOST_FTDC_CC_Immediately
        req.ForceCloseReason = mdapi.THOST_FTDC_FCC_NotForceClose
        req.IsAutoSuspend = 0
        req.UserForceClose = 0

        with self._lock:
            self.orders[ref] = {
                "order_id": ref,
                "real_symbol": symbol,
                "instrument": inst["instrument"],
                "exchange": inst["exchange"],
                "direction": direction.upper(),
                "action": action.upper(),
                "price": price,
                "lots": lots,
                "status": "SUBMITTING",
                "filled_lots": 0,
                "avg_fill_price": 0.0,
                "error": None,
                "created_at": time.time(),
                "_updated": threading.Event(),
            }
            self._persist(self.orders[ref])

        ret = self.api.ReqOrderInsert(req, self.next_id())
        if ret != 0:
            # ReqOrderInsert 返回值：0=已发送；-1=网络连接失败；-2=未处理请求超过许可数；-3=每秒请求数超限
            reason = {
                -1: "网络连接失败",
                -2: "未处理请求数超过许可数（降频）",
                -3: "每秒发送请求数超过许可数（降频）",
            }.get(ret, f"ReqOrderInsert 返回 {ret}")
            with self._lock:
                self.orders[ref]["status"] = "REJECTED"
                self.orders[ref]["error"] = reason
                self.orders[ref]["_updated"].set()
                self._persist(self.orders[ref])
            return {"state": "REJECT", "error": reason, "order_id": ref}

        # 等待 CTP 回执（受理/撤拒），超时返回 TIMEOUT_UNKNOWN——**不重试**
        rec = self.orders[ref]
        if rec["_updated"].wait(timeout=ACK_WAIT):
            with self._lock:
                snap = dict(rec)
            if snap["error"]:
                return {"state": "REJECT", "error": snap["error"], "order_id": ref}
            if snap["status"] == "REJECTED":
                return {"state": "REJECT", "error": snap["error"] or "柜台拒单", "order_id": ref}
            return {"state": "ACK", "order_id": ref, "status": snap["status"],
                    "filled_lots": snap["filled_lots"], "avg_fill_price": snap["avg_fill_price"]}
        return {"state": "TIMEOUT_UNKNOWN", "order_id": ref,
                "detail": f"提交后 {ACK_WAIT}s 未收到回执；**禁止重发**，请 GET /order/{ref} 查单判定"}

    # ---------------------------------------------------- 撤单
    def cancel(self, order_ref: str) -> dict[str, Any]:
        with self._lock:
            rec = self.orders.get(order_ref)
            if rec is None:
                raise KeyError(order_ref)
            instrument = rec["instrument"]
            # 已成交/已撤：不可再撤（与柜台语义一致）
            if rec["status"] in ("FILLED", "CANCELED", "REJECTED"):
                return {"state": "REJECT", "order_id": order_ref,
                        "error": f"订单状态 {rec['status']} 不可撤"}
            rec["_updated"].clear()

        act = tdapi.CThostFtdcInputOrderActionField()
        act.BrokerID = BROKER_ID
        act.InvestorID = USER_ID
        act.InstrumentID = instrument
        act.OrderRef = order_ref
        act.ActionFlag = mdapi.THOST_FTDC_AF_Delete
        ret = self.api.ReqOrderAction(act, self.next_id())
        if ret != 0:
            return {"state": "REJECT", "order_id": order_ref,
                    "error": f"ReqOrderAction 返回 {ret}"}

        rec = self.orders[order_ref]
        if rec["_updated"].wait(timeout=ACK_WAIT):
            with self._lock:
                snap = dict(rec)
            if snap["error"]:
                return {"state": "REJECT", "order_id": order_ref, "error": snap["error"]}
            return {"state": "OK", "order_id": order_ref, "status": snap["status"]}
        return {"state": "TIMEOUT_UNKNOWN", "order_id": order_ref,
                "detail": f"撤单 {ACK_WAIT}s 未确认，请 GET /order/{order_ref} 查单确认是否已撤"}

    def get_order(self, order_ref: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self.orders.get(order_ref)
            return {k: v for k, v in rec.items() if not k.startswith("_")} if rec else None


class _TraderSpi(tdapi.CThostFtdcTraderSpi):
    """CTP 交易回调实现。所有回调只更新 Sesssion 状态，不做重活（避免堵住回调线程）。"""

    def __init__(self, session: CtpSession) -> None:
        super().__init__()
        self.s = session

    def OnFrontConnected(self) -> None:
        self.s.req_authenticate()

    def OnFrontDisconnected(self, nReason: int) -> None:
        print(f"[CTP] 前置断开 reason={nReason}", flush=True)

    def OnRspAuthenticate(self, *a: Any) -> None:
        _f, info, _rid, _last = a[:4]
        if info is not None and info.ErrorID != 0:
            self.s.login_error = f"穿透式认证失败 ErrorID={info.ErrorID} {info.ErrorMsg}"
            self.s.ready.set()
            return
        self.s.req_login()

    def OnRspUserLogin(self, *a: Any) -> None:
        login, info, _rid, _last = a[:4]
        if info is not None and info.ErrorID != 0:
            self.s.login_error = f"登录失败 ErrorID={info.ErrorID} {info.ErrorMsg}"
            self.s.ready.set()
            return
        self.s.trading_day = login.TradingDay
        self.s.session_id = login.SessionID
        self.s.front_id = login.FrontID
        # ★ 结算单确认：漏了它所有下单都会失败，这是 CTP 头号坑
        self.s.req_settlement_confirm()

    def OnRspSettlementInfoConfirm(self, *a: Any) -> None:
        _f, info, _rid, _last = a[:4]
        if info is not None and info.ErrorID != 0:
            self.s.login_error = f"结算单确认失败 ErrorID={info.ErrorID} {info.ErrorMsg}"
            self.s.ready.set()
            return
        print(f"[CTP] 登录完成 交易日={self.s.trading_day} "
              f"FrontID={self.s.front_id} SessionID={self.s.session_id}", flush=True)
        self.s.ready.set()

    def OnRspQryInstrument(self, *a: Any) -> None:
        inst, info, _rid, last = a[:4]
        if info is not None and info.ErrorID != 0:
            print(f"[CTP] 查合约失败 ErrorID={info.ErrorID} {info.ErrorMsg}", flush=True)
            self.s.instrument_ready.set()
            return
        if inst is None:
            if last:
                self.s.instrument_ready.set()
            return
        inj = inst.InstrumentID
        digits = "".join(ch for ch in inj if ch.isdigit())
        product = "".join(ch for ch in inj if ch.isalpha()).upper()
        rec = {
            "instrument": inj,
            "exchange": inst.ExchangeID,
            "name": getattr(inst, "InstrumentName", ""),
            "tick": inst.PriceTick,
            "multiplier": inst.VolumeMultiple,
            "digits": digits,
            "product": product,
        }
        self.s.instruments[inj] = rec
        self.s._product_index.setdefault(product, []).append(rec)
        if last:
            print(f"[CTP] 合约表加载完成：{len(self.s.instruments)} 个合约，"
                  f"{len(self.s._product_index)} 个品种", flush=True)
            self.s.instrument_ready.set()

    def OnRspOrderInsert(self, *a: Any) -> None:
        order, info, _rid, _last = a[:4]
        if info is not None and info.ErrorID != 0:
            ref = order.OrderRef if order else None
            if ref:
                _mark_error(self.s, ref, f"报单被拒 ErrorID={info.ErrorID} {info.ErrorMsg}")

    def OnErrRtnOrderInsert(self, *a: Any) -> None:
        order, info = a[:2]
        ref = order.OrderRef if order else None
        if ref:
            _mark_error(self.s, ref,
                        f"报单错误回报 ErrorID={info.ErrorID if info else '?'} "
                        f"{info.ErrorMsg if info else ''}")

    def OnRtnOrder(self, *a: Any) -> None:
        order = a[0]
        if order is None:
            return
        ref = order.OrderRef
        with self.s._lock:
            rec = self.s.orders.get(ref)
            if rec is None:
                return
            rec["status"] = STATUS_MAP.get(order.OrderStatus, str(order.OrderStatus))
            rec["filled_lots"] = order.VolumeTraded
            # ★ 绝不能在这里动 avg_fill_price：CTP 部分成交时会反复推 OnRtnOrder，
            #   覆盖会把 OnRtnTrade 加权累计出来的均价清零。均价只由 OnRtnTrade 维护。
            rec["_updated"].set()
            self.s._persist(rec)

    def OnRtnTrade(self, *a: Any) -> None:
        trade = a[0]
        if trade is None:
            return
        ref = trade.OrderRef
        with self.s._lock:
            rec = self.s.orders.get(ref)
            if rec is None:
                return
            # 去重：CTP 会重复推送同一笔成交（跨交易日/重连时尤甚），重复累加会虚增持仓
            seen = rec.setdefault("_trades", set())
            tkey = f"{trade.TradeID}|{trade.Volume}|{trade.Price}"
            if tkey in seen:
                return
            seen.add(tkey)
            # 加权平均成交价：CTP 逐笔推送，不能拿单笔价格直接覆盖
            prev = rec.get("filled_lots") or 0
            prev_px = rec.get("avg_fill_price") or 0.0
            vol, px = trade.Volume, trade.Price
            total = prev + vol
            rec["avg_fill_price"] = round((prev_px * prev + px * vol) / total, 6) if total else 0.0
            rec["filled_lots"] = total
            rec["status"] = "FILLED" if total >= rec["lots"] else "PARTIAL"
            rec["_updated"].set()
            self.s._persist(rec)

    def OnRspOrderAction(self, *a: Any) -> None:
        action, info, _rid, _last = a[:4]
        if info is not None and info.ErrorID != 0:
            ref = action.OrderRef if action else None
            if ref:
                _mark_error(self.s, ref,
                            f"撤单被拒 ErrorID={info.ErrorID} {info.ErrorMsg}")

    def OnErrRtnOrderAction(self, *a: Any) -> None:
        action, info = a[:2]
        ref = action.OrderRef if action else None
        if ref:
            _mark_error(self.s, ref,
                        f"撤单错误回报 ErrorID={info.ErrorID if info else '?'} "
                        f"{info.ErrorMsg if info else ''}")


def _mark_error(session: CtpSession, ref: str, msg: str) -> None:
    with session._lock:
        rec = session.orders.get(ref)
        if rec:
            rec["error"] = msg
            rec["status"] = "REJECTED"
            rec["_updated"].set()
            session._persist(rec)


# ================================================================ HTTP 层
app = FastAPI(title="qhyc CTP Gateway", version="1.0")
SESSION = CtpSession()


class OrderIn(BaseModel):
    real_symbol: str
    direction: str
    action: str = "OPEN"
    price: float | None = None
    lots: int
    channel: str | None = None
    account: str | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    return {
        "healthy": SESSION.ready.is_set() and not SESSION.login_error,
        "login_error": SESSION.login_error,
        "trading_day": SESSION.trading_day,
        "front_id": SESSION.front_id,
        "session_id": SESSION.session_id,
        "instruments_loaded": len(SESSION.instruments),
        "orders_total": len(SESSION.orders),
        "api_version": tdapi.CThostFtdcTraderApi.GetApiVersion(),
        "td_front": TD_FRONT,
        "user_id": USER_ID,
    }


@app.get("/instruments/{symbol}")
def get_instrument(symbol: str) -> dict[str, Any]:
    try:
        return SESSION.resolve(symbol)
    except ValueError as e:
        raise HTTPException(404, str(e))


@app.post("/order")
def place(body: OrderIn) -> dict[str, Any]:
    if not SESSION.ready.is_set():
        raise HTTPException(503, f"CTP 未就绪：{SESSION.login_error or '尚未登录'}")
    if body.price is None:
        raise HTTPException(400, "price 必填：本网关首版只发限价单（市价单各家期货公司风控不同）")
    if body.lots <= 0:
        raise HTTPException(400, f"lots 必须为正整数，收到 {body.lots}")
    try:
        res = SESSION.submit(body.real_symbol, body.direction,
                             body.action, body.price, body.lots)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if res["state"] == "TIMEOUT_UNKNOWN":
        # 不能以 4xx 返回：那样 broker_http 会当 REJECT 处理。必须让上层走查单流程。
        return {**res, "state": "TIMEOUT_UNKNOWN"}
    if res["state"] == "REJECT":
        return {**res, "error": res.get("error", "柜台拒单")}
    return res


@app.get("/order/{order_id}")
def query(order_id: str) -> dict[str, Any]:
    rec = SESSION.get_order(order_id)
    if rec is None:
        raise HTTPException(404, f"订单不存在：{order_id}")
    return rec


@app.post("/order/{order_id}/cancel")
def cancel(order_id: str) -> dict[str, Any]:
    try:
        res = SESSION.cancel(order_id)
    except KeyError:
        raise HTTPException(404, f"订单不存在：{order_id}")
    if res["state"] == "REJECT":
        # broker_http 约定：>=400 视为 REJECT
        raise HTTPException(400, res.get("error", "不可撤"))
    return res


def main() -> int:
    p = argparse.ArgumentParser(description="qhyc CTP 网关进程")
    p.add_argument("--port", type=int, default=8770)
    p.add_argument("--host", default="127.0.0.1")
    a = p.parse_args()

    missing = [n for n, v in (("CTP_USER_ID", USER_ID), ("CTP_PASSWORD", PASSWORD)) if not v]
    if missing:
        sys.stderr.write(f"[FATAL] 缺少环境变量：{', '.join(missing)}\n")
        return 2

    print("=" * 68)
    print(f"qhyc CTP Gateway  td={TD_FRONT}  md={MD_FRONT}")
    print(f"  BrokerID={BROKER_ID} UserID={USER_ID} AppID={APP_ID}")
    print("=" * 68, flush=True)

    if not SESSION.start():
        print(f"[FATAL] CTP 登录失败：{SESSION.login_error}")
        return 1

    import uvicorn
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
