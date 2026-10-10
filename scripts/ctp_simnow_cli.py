# -*- coding: utf-8 -*-
"""qhyc · SimNow CTP 连通性探测器（P0-2 Sprint 2 前置，唯一第三方依赖 openctp_ctp）

做什么
------
在动用任何真实资金之前，先用**只读方式**确认六件事，把"这一步哪里不对"说清楚：

  1. 交易前置是否**网络可达**（TCP 层）
  2. ReqAuthenticate（穿透式认证，AppID/AuthCode 是否正确）
  3. ReqUserLogin（InvestorID / Password 是否正确）
  4. SettlementInfoConfirm（**结算单确认——跳过它下单会全部失败，这是 CTP 头号坑**）
  5. 只读查询：资金 / 持仓 / 合约总数（顺便抽样验证 qhyc ``to_native()`` 的代码格式假设）
  6. 行情前置订阅 Tick（可选，确认 md 通道与合约代码写法）

默认**不下单**。下单/撤单冒烟须显式加 ``--place-test``，且发的是**深度虚值限价单**
后立即撤单（不会成交，不产生真实损益）。

环境（数据来源：SimNow 官网通知公告 2025/07/15 + openctp 官方 CTPAPI 页面）
--------------------------------------------------------------------------
  * **第一套（标准仿真，跟随实盘交易时段）**——新注册用户立即可用
      td = tcp://182.254.243.31:30001
      md = tcp://182.254.243.31:30011
  * **第二套（7x24 测试环境）**——⚠ 官网明载：**新注册用户需等到第三个交易日才能使用**
      td = tcp://182.254.243.31:40001
      md = tcp://182.254.243.31:40011
    该环境仅服务 CTP API 调试，**不提供结算**；钱/仓跟第一套上一交易日一致。
    服务时间：交易日 16:00~次日 09:00，非交易日 16:00~次日 12:00。

  BrokerID  = 9999
  AppID     = simnow_client_test
  AuthCode  = 0000000000000000（16 个零）

用法
----
    # 1) 只读连通性探测（第一套环境，交易时段内才有交易前置回应）
    set CTP_USER_ID=123456
    set CTP_PASSWORD=你的密码
    python scripts/ctp_simnow_cli.py probe

    # 2) 指定 7x24 环境（新号第三天之后）
    python scripts/ctp_simnow_cli.py probe --env=724

    # 3) 下单 + 撤单冒烟（默认关闭）
    python scripts/ctp_simnow_cli.py probe --place-test --symbol=rb2610

    # 4) 输出 JSON 便于归档
    python scripts/ctp_simnow_cli.py probe --json

环境变量
--------
    CTP_USER_ID   必填。SimNow 短信通知的 InvestorID（6 位）
    CTP_PASSWORD  必填。SimNow 密码
    CTP_BROKER_ID 默认 9999
    CTP_APP_ID    默认 simnow_client_test
    CTP_AUTH_CODE 默认 0000000000000000

退出码
------
    0 = 六项全通过；1 = 中途失败（日志会指出失败在第几步）；2 = 缺少必填环境变量
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from typing import Any

try:
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
except Exception:
    pass

try:
    from openctp_ctp import mdapi, tdapi
except ImportError:
    sys.stderr.write(
        "缺少依赖 openctp_ctp。"
        "请先执行：pip install openctp-ctp\n"
        "（选择与 Python 版本匹配的 wheel，本机实测 cp313-win_amd64 可直接安装无需编译）\n"
    )
    raise SystemExit(2)

API_VERSION = tdapi.CThostFtdcTraderApi.GetApiVersion()

# ---------------------------------------------------------------- 环境表
ENVIRONMENTS: dict[str, dict[str, str]] = {
    "sim": {"td": "tcp://182.254.243.31:30001", "md": "tcp://182.254.243.31:30011",
            "desc": "第一套 · 标准仿真（跟随实盘交易时段，新号立即可用）"},
    "724": {"td": "tcp://182.254.243.31:40001", "md": "tcp://182.254.243.31:40011",
            "desc": "第二套 · 7x24 测试（新注册用户需等第三个交易日）"},
}

DEFAULT_BROKER_ID = "9999"
DEFAULT_APP_ID = "simnow_client_test"
DEFAULT_AUTH_CODE = "0000000000000000"


# ---------------------------------------------------------------- 凭据
class Credentials:
    """CTP 接入凭据。缺 user_id/password 时 fail-loud，不用占位符蒙混过关。"""

    def __init__(self) -> None:
        self.user_id = (os.getenv("CTP_USER_ID") or "").strip()
        self.password = (os.getenv("CTP_PASSWORD") or "").strip()
        self.broker_id = (os.getenv("CTP_BROKER_ID") or DEFAULT_BROKER_ID).strip()
        self.app_id = (os.getenv("CTP_APP_ID") or DEFAULT_APP_ID).strip()
        self.auth_code = (os.getenv("CTP_AUTH_CODE") or DEFAULT_AUTH_CODE).strip()
        #: 穿透式报备的客户端标识。SimNow 阶段用 "qhyc" 占位；
        #: 广发申请授权码后会分配正式 AppID，届时同步替换本项与 CTP_APP_ID。
        self.user_product_info = (os.getenv("CTP_USER_PRODUCT_INFO") or "qhyc").strip()

    def validate(self) -> list[str]:
        missing: list[str] = []
        if not self.user_id:
            missing.append("CTP_USER_ID")
        if not self.password:
            missing.append("CTP_PASSWORD")
        return missing


# ---------------------------------------------------------------- 诊断
KNOWN_ERRORS: dict[int, str] = {
    1: "签退失败/密码错误：InvestorID 或 Password 不对。注意 SimNow 密码区分大小写，"
       "且新注册后须等短信下发 InvestorID（不是注册时的邮箱/手机号）",
    2: "用户不存在（{missing_id} 未在柜台开户）",
    3: "会话未同步：同一客户重复登录，或上次连接未断干净被踢。等待 1-3 分钟后重试",
    4: "交互事务未初始化",
    5: "不合法的登录：未先调用 ReqAuthenticate（穿透式认证）",
    6: "忙/队列满：稍后重试",
    7: "未确认结算单：必须调用 ReqSettlementInfoConfirm 后才能下单（本脚本已处理）",
    8: "连接超出 broker 限制：可能有多个进程在用同一账号登录。关闭其它 CTP 程序后重试",
    9: "业务未就绪：柜台尚未完成初始化。刚开盘/刚收盘容易遇到",
    -1: "网络层错误：前置不可达或被拒。检查 IP/端口是否写错、本机防火墙、"
        "以及是否处在 SimNow 的服务时间窗内",
    -2: "未处理请求超过许可数：慢下来，降低请求频率",
    -3: "每秒发送请求数超过许可数：降频",
    -4: "已被其它连接强行终止（同账号重复登录）",
    -5: "网络连接失败",
    -6: "网络读/写失败",
    -7: "未收到心跳超时（长时间无 Session）",
    26: "资金不足（SimNow 初始资金 2000 万，不该出现；若出现说明账号未同步）",
    27: "平仓量超过持仓量：部位不足",
    31: "价格超出涨跌停板范围：报价不在允许区间",
    47: "找不到此合约：InstrumentID 写法不对（注意大小写与郑商所 3 位月份）",
    48: "合约已到期/不在交易状态",
    63: "没有报单权限：程序化交易权限未开通，或未做xx报备",
    64: "非交易时间：当前不在该前置的服务时间窗内",
}


def explain(error_id: int, default: str = "") -> str:
    return KNOWN_ERRORS.get(error_id, default or f"未收录的错误码 {error_id}")


def _err(p_rsp_info: Any) -> str:
    """把 RspInfo 归一成 'ErrorID=xx_msg' 形式。pRspInfo 可能为 None。"""
    if p_rsp_info is None:
        return "ErrorID=0"
    return f"ErrorID={p_rsp_info.ErrorID}_{p_rsp_info.ErrorMsg or ''}"


def _ok(p_rsp_info: Any) -> bool:
    return p_rsp_info is None or p_rsp_info.ErrorID == 0


# ---------------------------------------------------------------- 交易 Spi
class ProbeSpi(tdapi.CThostFtdcTraderSpi):
    """交易 SPI：按 CTP 登录三段式 authenticate → login → 结算单确认 推进。"""

    def __init__(self, ctx: "ProbeCtx") -> None:
        super().__init__()
        self.ctx = ctx

    # ---------------- 连接层
    def OnFrontConnected(self) -> None:
        self.ctx.log("STEP1", "交易前置已连接（TCP OK）")
        self.ctx.req_authenticate()

    def OnFrontDisconnected(self, nReason: int) -> None:
        self.ctx.log("WARN", f"交易前置断开，reason={nReason}")
        self.ctx.front_up.clear()

    def OnHeartBeatWarning(self, nTimeLapse: int) -> None:
        self.ctx.log("WARN", f"心跳超时预警 {nTimeLapse}s")

    # ---------------- 认证层
    def OnRspAuthenticate(self, *args: Any) -> None:
        p_rsp_field, p_rsp_info, n_request_id, b_is_last = args[:4]
        if _ok(p_rsp_info):
            self.ctx.log("STEP2", "穿透式认证通过（AppID/AuthCode 正确）")
            self.ctx.req_login()
        else:
            eid = p_rsp_info.ErrorID
            self.ctx.fail("STEP2", f"穿透式认证失败 {_err(p_rsp_info)}｜{explain(eid)}")

    # ---------------- 登录层
    def OnRspUserLogin(self, *args: Any) -> None:
        p_login, p_rsp_info, n_request_id, b_is_last = args[:4]
        if not _ok(p_rsp_info):
            eid = p_rsp_info.ErrorID
            self.ctx.fail("STEP3", f"登录失败 {_err(p_rsp_info)}｜{explain(eid)}")
            return
        self.ctx.login_result = {
            "trading_day": p_login.TradingDay,
            "login_time": p_login.LoginTime,
            "front_id": p_login.FrontID,
            "session_id": p_login.SessionID,
        }
        self.ctx.log(
            "STEP3",
            f"登录成功 交易日={p_login.TradingDay} 时间={p_login.LoginTime} "
            f"FrontID={p_login.FrontID} SessionID={p_login.SessionID}",
        )
        self.ctx.req_settlement_confirm()

    def OnRspSettlementInfoConfirm(self, *args: Any) -> None:
        p_confirm, p_rsp_info, n_request_id, b_is_last = args[:4]
        if not _ok(p_rsp_info):
            eid = p_rsp_info.ErrorID
            self.ctx.fail("STEP4", f"结算单确认失败 {_err(p_rsp_info)}｜{explain(eid)}")
            return
        self.ctx.log("STEP4", "结算单已确认（此后才具备下单资格）")
        self.ctx.ready.set()

    # ---------------- 只读查询
    def OnRspQryTradingAccount(self, *args: Any) -> None:
        p_account, p_rsp_info, n_request_id, b_is_last = args[:4]
        if not _ok(p_rsp_info):
            self.ctx.fail("STEP5a", f"查资金失败 {_err(p_rsp_info)}｜{explain(p_rsp_info.ErrorID)}")
            return
        if p_account:
            self.ctx.account = {
                "pre_balance": p_account.PreBalance,
                "balance": p_account.Balance,
                "available": p_account.Available,
                "curr_margin": p_account.CurrMargin,
                "close_profit": p_account.CloseProfit,
                "position_profit": p_account.PositionProfit,
                "withdraw_quota": p_account.WithdrawQuota,
            }
        if b_is_last:
            if self.ctx.account:
                a = self.ctx.account
                self.ctx.log(
                    "STEP5a",
                    f"资金查询成功 上日结存=¥{a['pre_balance']:,.2f} 当前权益=¥{a['balance']:,.2f} "
                    f"可用=¥{a['available']:,.2f} 占用保证金=¥{a['curr_margin']:,.2f}",
                )
            self.ctx.qry_account_done.set()

    def OnRspQryInvestorPosition(self, *args: Any) -> None:
        p_pos, p_rsp_info, n_request_id, b_is_last = args[:4]
        if not _ok(p_rsp_info):
            self.ctx.fail("STEP5b", f"查持仓失败 {_err(p_rsp_info)}｜{explain(p_rsp_info.ErrorID)}")
            return
        if p_pos and getattr(p_pos, "Position", 0) > 0:
            self.ctx.positions.append({
                "instrument": p_pos.InstrumentID,
                "direction": "多" if p_pos.PosiDirection == mdapi.THOST_FTDC_PD_Long
                             else ("空" if p_pos.PosiDirection == mdapi.THOST_FTDC_PD_Short else "净"),
                "position": p_pos.Position,
                "today_position": p_pos.TodayPosition,
                "yd_strike_frozen": p_pos.YdPosition,
            })
        if b_is_last:
            if self.ctx.positions:
                for p in self.ctx.positions:
                    self.ctx.log(
                        "STEP5b",
                        f"持仓 {p['instrument']} {p['direction']} {p['position']} 手"
                        f"（今仓={p['today_position']} 昨仓={p['yd_strike_frozen']}）",
                    )
            else:
                self.ctx.log("STEP5b", "查持仓成功（当前无持仓，符合新号预期）")
            self.ctx.qry_position_done.set()

    def OnRspQryInstrument(self, *args: Any) -> None:
        p_inst, p_rsp_info, n_request_id, b_is_last = args[:4]
        if not _ok(p_rsp_info):
            self.ctx.fail("STEP5c", f"查合约失败 {_err(p_rsp_info)}｜{explain(p_rsp_info.ErrorID)}")
            return
        if p_inst and len(self.ctx.instrument_samples) < 40:
            self.ctx.instrument_samples.append({
                "instrument": p_inst.InstrumentID,
                "exchange": p_inst.ExchangeID,
                "name": getattr(p_inst, "InstrumentName", ""),
                "multiplier": p_inst.VolumeMultiple,
                "tick": p_inst.PriceTick,
            })
        if b_is_last:
            self.ctx.log(
                "STEP5c",
                f"合约表查询成功 共 {len(self.ctx.instrument_samples)} 条样本"
                f"（到达 bIsLast，说明合约表接收完整）",
            )
            self.ctx.qry_instrument_done.set()

    # ---------------- 下单/撤单（仅 --place-test）
    def OnRspOrderInsert(self, *args: Any) -> None:
        p_order, p_rsp_info, n_request_id, b_is_last = args[:4]
        if not _ok(p_rsp_info):
            eid = p_rsp_info.ErrorID
            self.ctx.fail("STEP6a", f"报单被拒 {_err(p_rsp_info)}｜{explain(eid)}")
            return
        if p_order:
            self.ctx.order_ref = p_order.OrderRef
            self.ctx.log("STEP6a", f"报单已受理 OrderRef={p_order.OrderRef} 合约={p_order.InstrumentID}")

    def OnErrRtnOrderInsert(self, *args: Any) -> None:
        p_order, p_rsp_info = args[:2]
        eid = p_rsp_info.ErrorID if p_rsp_info else -999
        self.ctx.fail("STEP6a", f"报单错误回报 {_err(p_rsp_info)}｜{explain(eid)}")

    def OnRspOrderAction(self, *args: Any) -> None:
        p_action, p_rsp_info, n_request_id, b_is_last = args[:4]
        if not _ok(p_rsp_info):
            eid = p_rsp_info.ErrorID
            self.ctx.fail("STEP6b", f"撤单被拒 {_err(p_rsp_info)}｜{explain(eid)}")
            return
        self.ctx.log("STEP6b", "撤单请求已受理")

    def OnErrRtnOrderAction(self, *args: Any) -> None:
        p_action, p_rsp_info = args[:2]
        eid = p_rsp_info.ErrorID if p_rsp_info else -999
        self.ctx.fail("STEP6b", f"撤单错误回报 {_err(p_rsp_info)}｜{explain(eid)}")

    def OnRtnOrder(self, *args: Any) -> None:
        p_order = args[0]
        if not p_order or self.ctx.order_ref is None:
            return
        if p_order.OrderRef != self.ctx.order_ref:
            return
        status = {
            mdapi.THOST_FTDC_OST_AllTraded: "全部成交",
            mdapi.THOST_FTDC_OST_PartTradedQueueing: "部分成交",
            mdapi.THOST_FTDC_OST_NoTradeQueueing: "未成交在队列中",
            mdapi.THOST_FTDC_OST_Canceled: "已撤单",
            mdapi.THOST_FTDC_OST_Unknown: "未知",
            mdapi.THOST_FTDC_OST_NoTradeNotQueueing: "未成交不在队列",
        }.get(p_order.OrderStatus, str(p_order.OrderStatus))
        self.ctx.log("RTN", f"委托状态={status} 已成交={p_order.VolumeTraded}手 "
                            f"剩余={p_order.VolumeTotal}手")
        if p_order.OrderStatus == mdapi.THOST_FTDC_OST_AllTraded:
            self.ctx.traded.set()
        if p_order.OrderStatus == mdapi.THOST_FTDC_OST_Canceled:
            self.ctx.canceled.set()

    def OnRtnTrade(self, *args: Any) -> None:
        p_trade = args[0]
        if p_trade and p_trade.OrderRef == self.ctx.order_ref:
            self.ctx.log("RTN", f"成交回报 {p_trade.InstrumentID} 价={p_trade.Price} "
                                f"量={p_trade.Volume}")


# ---------------------------------------------------------------- 行情 Spi
class ProbeMdSpi(mdapi.CThostFtdcMdSpi):
    """行情 SPI：登录 → 订阅 → 收 Tick。"""

    def __init__(self, ctx: "ProbeCtx") -> None:
        super().__init__()
        self.ctx = ctx

    def OnFrontConnected(self) -> None:
        self.ctx.log("STEP7a", "行情前置已连接（TCP OK）")
        self.ctx.req_md_login()

    def OnFrontDisconnected(self, nReason: int) -> None:
        self.ctx.log("WARN", f"行情前置断开 reason={nReason}")

    def OnRspUserLogin(self, *args: Any) -> None:
        p_login, p_rsp_info, n_request_id, b_is_last = args[:4]
        if not _ok(p_rsp_info):
            eid = p_rsp_info.ErrorID
            self.ctx.fail("STEP7b", f"行情登录失败 {_err(p_rsp_info)}｜{explain(eid)}")
            return
        self.ctx.log("STEP7b", "行情登录成功")
        self.ctx.subscribe()

    def OnRspSubMarketData(self, *args: Any) -> None:
        p_inst, p_rsp_info, n_request_id, b_is_last = args[:4]
        if not _ok(p_rsp_info):
            eid = p_rsp_info.ErrorID
            self.ctx.fail("STEP7c", f"订阅失败 {_err(p_rsp_info)}｜{explain(eid)}")
            return
        instrument = p_inst.InstrumentID if p_inst else "?"
        if instrument not in (self.ctx.subscribed_ok or []):
            (self.ctx.subscribed_ok or []).append(instrument)
        self.ctx.log("STEP7c", f"订阅确认 {instrument}")

    def OnRtnDepthMarketData(self, *args: Any) -> None:
        p_tick = args[0]
        if p_tick:
            self.ctx.md_tick = {
                "instrument": p_tick.InstrumentID,
                "update_time": p_tick.UpdateTime,
                "last_price": p_tick.LastPrice,
                "volume": p_tick.Volume,
            }
            self.ctx.md_got.set()


# ---------------------------------------------------------------- 上下文
class ProbeCtx:
    """探测过程的共享状态与流程控制。"""

    def __init__(self, cred: Credentials, env: dict[str, str], *, verbose: bool = True) -> None:
        self.cred = cred
        self.env = env
        self.verbose = verbose

        self.front_up = threading.Event()
        self.ready = threading.Event()
        self.qry_account_done = threading.Event()
        self.qry_position_done = threading.Event()
        self.qry_instrument_done = threading.Event()
        self.traded = threading.Event()
        self.canceled = threading.Event()
        self.md_got = threading.Event()

        self.steps: list[dict[str, str]] = []
        self.failure_step: str | None = None
        self.login_result: dict[str, Any] = {}
        self.account: dict[str, float] = {}
        self.positions: list[dict[str, Any]] = []
        self.instrument_samples: list[dict[str, Any]] = []
        self.subscribed_ok: list[str] = []
        self.md_tick: dict[str, Any] = {}

        self.order_ref: str | None = None

        self._req_id = 0
        self._id_lock = threading.Lock()

        self.td_api: Any = None
        self.md_api: Any = None

    # ---------------- 日志
    def log(self, step: str, msg: str) -> None:
        if self.verbose:
            print(f"[{step:<7}] {msg}", flush=True)
        self.steps.append({"step": step, "msg": msg, "at": time.strftime("%H:%M:%S")})

    def fail(self, step: str, msg: str) -> None:
        self.failure_step = step
        self.log(f"{step}!!", msg)
        self.ready.set()
        for ev in (self.qry_account_done, self.qry_position_done, self.qry_instrument_done):
            ev.set()

    # ---------------- 请求构造
    def next_id(self) -> int:
        with self._id_lock:
            self._req_id += 1
            return self._req_id

    def req_authenticate(self) -> None:
        req = tdapi.CThostFtdcReqAuthenticateField()
        req.BrokerID = self.cred.broker_id
        req.UserID = self.cred.user_id
        req.AppID = self.cred.app_id
        req.AuthCode = self.cred.auth_code
        req.UserProductInfo = self.cred.user_product_info
        self.td_api.ReqAuthenticate(req, self.next_id())

    def req_login(self) -> None:
        req = tdapi.CThostFtdcReqUserLoginField()
        req.BrokerID = self.cred.broker_id
        req.UserID = self.cred.user_id
        req.Password = self.cred.password
        req.UserProductInfo = self.cred.user_product_info
        self.td_api.ReqUserLogin(req, self.next_id())

    def req_settlement_confirm(self) -> None:
        req = tdapi.CThostFtdcSettlementInfoConfirmField()
        req.BrokerID = self.cred.broker_id
        req.InvestorID = self.cred.user_id
        self.td_api.ReqSettlementInfoConfirm(req, self.next_id())

    def req_qry_account(self) -> None:
        req = tdapi.CThostFtdcQryTradingAccountField()
        req.BrokerID = self.cred.broker_id
        req.InvestorID = self.cred.user_id
        req.CurrencyID = "CNY"
        self.td_api.ReqQryTradingAccount(req, self.next_id())

    def req_qry_position(self) -> None:
        req = tdapi.CThostFtdcQryInvestorPositionField()
        req.BrokerID = self.cred.broker_id
        req.InvestorID = self.cred.user_id
        self.td_api.ReqQryInvestorPosition(req, self.next_id())

    def req_qry_instrument(self) -> None:
        req = tdapi.CThostFtdcQryInstrumentField()
        self.td_api.ReqQryInstrument(req, self.next_id())

    def req_md_login(self) -> None:
        req = mdapi.CThostFtdcReqUserLoginField()
        req.BrokerID = self.cred.broker_id
        req.UserID = self.cred.user_id
        req.Password = self.cred.password
        self.md_api.ReqUserLogin(req, self.next_id())

    def subscribe(self) -> None:
        if not self.md_symbol:
            return
        self.md_api.SubscribeMarketData([self.md_symbol])

    def place_and_cancel(self, symbol: str, price: float) -> None:
        """挂一笔深度虚值限价单（不会成交），然后撤单。"""
        req = tdapi.CThostFtdcInputOrderField()
        req.BrokerID = self.cred.broker_id
        req.InvestorID = self.cred.user_id
        req.UserID = self.cred.user_id
        req.InstrumentID = symbol
        req.OrderRef = str(self.next_id() % 1000000)
        req.Direction = mdapi.THOST_FTDC_D_Buy
        req.CombOffsetFlag = tdapi.THOST_FTDC_OF_Open
        req.CombHedgeFlag = tdapi.THOST_FTDC_HF_Speculation
        req.LimitPrice = price
        req.VolumeTotalOriginal = 1
        req.OrderPriceType = mdapi.THOST_FTDC_OPT_LimitPrice
        req.TimeCondition = mdapi.THOST_FTDC_TC_GFD
        req.VolumeCondition = mdapi.THOST_FTDC_VC_AV
        req.MinVolume = 1
        req.ContingentCondition = mdapi.THOST_FTDC_CC_Immediately
        req.ForceCloseReason = mdapi.THOST_FTDC_FCC_NotForceClose
        req.IsAutoSuspend = 0
        req.UserForceClose = 0
        self.td_api.ReqOrderInsert(req, self.next_id())

    def cancel_order(self, symbol: str) -> None:
        act = tdapi.CThostFtdcInputOrderActionField()
        act.BrokerID = self.cred.broker_id
        act.InvestorID = self.cred.user_id
        act.InstrumentID = symbol
        act.OrderRef = self.order_ref or ""
        act.ActionFlag = mdapi.THOST_FTDC_AF_Delete
        self.td_api.ReqOrderAction(act, self.next_id())

    md_symbol: str = ""


# ---------------------------------------------------------------- 主流程
def run_probe(args: argparse.Namespace) -> int:
    cred = Credentials()
    missing = cred.validate()
    if missing:
        print(f"[FATAL] 缺少必填环境变量：{', '.join(missing)}")
        print("        set CTP_USER_ID=<SimNow 短信下发的 InvestorID>")
        print("        set CTP_PASSWORD=<密码>")
        return 2

    env = ENVIRONMENTS[args.env]
    ctx = ProbeCtx(cred, env, verbose=not args.json)

    print("=" * 72)
    print(f"qhyc · SimNow CTP 连通性探测    API 版本={API_VERSION}")
    print(f"环境：{env['desc']}")
    print(f"  交易前置 td = {env['td']}")
    print(f"  行情前置 md = {env['md']}")
    print(f"  BrokerID={cred.broker_id}  UserID={cred.user_id}  AppID={cred.app_id}")
    print("=" * 72, flush=True)

    # ---------- 交易通道
    td_spi = ProbeSpi(ctx)
    td_api = tdapi.CThostFtdcTraderApi.CreateFtdcTraderApi(f"qhyc_probe_{os.getpid()}")
    td_api.RegisterFront(env["td"])
    td_api.RegisterSpi(td_spi)
    td_api.SubscribePrivateTopic(tdapi.THOST_TERT_QUICK)
    td_api.SubscribePublicTopic(tdapi.THOST_TERT_QUICK)
    td_api.Init()
    ctx.td_api = td_api

    if not ctx.ready.wait(timeout=args.timeout):
        print(f"\n[FAIL] 登录流程超时 {args.timeout}s。")
        print("       最常见原因：①非交易时段前置不回应；②IP/端口被防火墙拦；")
        print(f"       ③'{args.env}' 环境尚未对你开放（官网：新注册用户第二套环境需等第三个交易日）")
        if args.json:
            print(json.dumps({"ok": False, "steps": ctx.steps,
                              "failure_step": ctx.failure_step or "LOGIN_TIMEOUT"},
                             ensure_ascii=False, indent=2))
        return 1

    if ctx.failure_step:
        print(f"\n[FAIL] 卡在 {ctx.failure_step}。按上面的错误码说明处理。")
        if args.json:
            print(json.dumps({"ok": False, "steps": ctx.steps,
                              "failure_step": ctx.failure_step},
                             ensure_ascii=False, indent=2))
        return 1

    # ---------- 只读查询
    ctx.req_qry_account()
    ctx.req_qry_position()
    ctx.req_qry_instrument()
    time.sleep(1.5)
    if not ctx.qry_account_done.wait(timeout=args.timeout):
        print("\n[WARN] 查资金超时（不影响主链路判定）")
    if not ctx.qry_position_done.wait(timeout=args.timeout):
        print("\n[WARN] 查持仓超时（不影响主链路判定）")
    if not ctx.qry_instrument_done.wait(timeout=max(args.timeout, 20)):
        print("\n[WARN] 查合约超时（不影响主链路判定）")

    # ---------- 行情通道
    if not args.skip_md:
        md_spi = ProbeMdSpi(ctx)
        md_api = mdapi.CThostFtdcMdApi.CreateFtdcMdApi(f"qhyc_md_{os.getpid()}")
        md_api.RegisterFront(env["md"])
        md_api.RegisterSpi(md_spi)
        md_api.Init()
        ctx.md_api = md_api
        ctx.md_symbol = (args.md_symbol or args.symbol or "rb2610").encode("gbk")
        if ctx.md_got.wait(timeout=args.timeout_md):
            t = ctx.md_tick
            print(f"[STEP7d ] 收到 Tick {t['instrument']} {t['update_time']} "
                  f"最新价={t['last_price']} 成交量={t['volume']}")
        else:
            print(f"\n[WARN] {args.timeout_md}s 内未收到 Tick。")
            print("       若当前非交易时段属正常现象；若在交易时段仍无 Tick，")
            print("       多半是合约代码写法不对（注意大小写/郑商所 3 位月份），")
            print("       换一个活跃合约重试：--md-symbol=rb2610")

    # ---------- 下单撤单冒烟（默认关闭）
    place_ok: str | None = None
    if args.place_test:
        sym = args.symbol or "rb2610"
        ctx.log("STEP6", f"下单冒烟：对 {sym} 挂深度虚值限价买单 1 手（价=1，不可能成交）")
        ctx.place_and_cancel(sym, args.test_price)
        if ctx.canceled.wait(timeout=args.timeout) or ctx.traded.wait(timeout=3):
            ctx.log("STEP6c", "报单 → 撤单闭环成功")
            place_ok = "CANCELED"
        else:
            ctx.log("STEP6c", "撤单未在超时时间内确认（可能是非交易时段前置不处理）")
            if ctx.order_ref:
                ctx.cancel_order(sym)
                place_ok = "SUBMITTED_UNCONFIRMED"

    # ---------- 结论
    ok = ctx.failure_step is None
    print("\n" + "=" * 72)
    if ok:
        print("[RESULT] 通过：认证 / 登录 / 结算单确认 / 资金 / 持仓 / 合约 六项全部 OK")
        print(f"         交易日={ctx.login_result.get('trading_day')} "
              f"SessionID={ctx.login_result.get('session_id')}")
        if ctx.account:
            print(f"         可用资金=¥{ctx.account['available']:,.2f} "
                  f"（初始预算由此可见）")
        print("         下一步：启动 CTP 网关，把 qhyc 的 EXECUTION_BROKER 切到 ctp")
    else:
        print(f"[RESULT] 失败：{ctx.failure_step}")
    print("=" * 72)

    if args.json:
        print(json.dumps({
            "ok": ok,
            "api_version": API_VERSION,
            "env": args.env,
            "front": env,
            "login": ctx.login_result,
            "account": ctx.account,
            "positions": ctx.positions,
            "instrument_sample_count": len(ctx.instrument_samples),
            "instrument_samples": ctx.instrument_samples[:10],
            "md_symbol": args.md_symbol or args.symbol,
            "md_tick": ctx.md_tick,
            "place_test": place_ok,
            "failure_step": ctx.failure_step,
            "steps": ctx.steps,
        }, ensure_ascii=False, indent=2, default=str))

    try:
        td_api.Release()
    except Exception:
        pass
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description="qhyc · SimNow CTP 连通性探测器")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("probe", help="只读连通性探测（默认不下单）")

    pr.add_argument("--env", choices=list(ENVIRONMENTS), default="sim",
                    help="sim=第一套标准仿真（默认）；724=第二套 7x24")
    pr.add_argument("--timeout", type=float, default=15.0,
                    help="登录/查询超时秒数（默认 15）")
    pr.add_argument("--timeout-md", type=float, default=10.0,
                    help="等待首个 Tick 的秒数（默认 10）")
    pr.add_argument("--symbol", default="rb2610",
                    help="下单冒烟使用的合约（默认 rb2610）")
    pr.add_argument("--md-symbol", default=None,
                    help="行情订阅合约；缺省与 --symbol 相同")
    pr.add_argument("--skip-md", action="store_true", help="跳过行情通道")
    pr.add_argument("--place-test", action="store_true",
                    help="额外做一次下单+撤单冒烟（默认关闭）")
    pr.add_argument("--test-price", type=float, default=1.0,
                    help="冒烟单限价，深度虚值不会成交（默认 1.0）")
    pr.add_argument("--json", action="store_true", help="额外输出结构化 JSON")
    pr.set_defaults(func=run_probe)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
