# -*- coding: utf-8 -*-
"""qhyc 执行代理（运行在**无限易 PythonGO 内部**）——零策略逻辑版。

定位
----
本文件**不包含任何交易策略**。它只是一个"搬运工 + 翻译器"：

    qhyc 融合信号（全部策略资产，仍在 qhyc 侧）
        ↓  /execution/bridge/pending
    本代理轮询拉取订单建议
        ↓  翻译成无限易下单调用
    无限易客户端（已登录 CTP 柜台）
        ↓
    期货公司前置 → 交易所

这样，既满足了期货公司"必须走合规桌面交易软件"的要求，又**保留 qhyc 全部策略资产**
（融合信号 + 反解层 + 持仓管理 + 止损 + 风控都不用动），改造只在最末 1 米的下单通道上。

如何放置与使用
--------------
1. 无限易客户端 → 策略 → PythonGO → 新建策略，**文件名必须与类名一致：QhycBridge.py**
2. 把本文件复制到无限易的策略目录（具体路径见客户端 PythonGO 面板提示）
3. 在面板参数里填：signalUrl / apiKey / investor / maxLots / dryRun
4. **务必先用 dryRun=1 跑一整天**（只打日志不下单），确认拉单/翻译/回写都正常
5. 再关掉 dryRun，从 maxLots=1 开始实盘

为什么用轮询而不是回调
----------------------
无限易策略跑在客户端进程内，不适合起 HTTP 服务；反过来由它主动轮询 qhyc REST 最稳。
qhyc 策略是日线/小时线级别，**3 秒轮询延迟完全可以接受**。

⚠ 关键实现纪律（都是会真金白银出事的坑）
-----------------------------------------
* **原子认领**：拉单必须走 /claim 而不是只读列表，否则两次轮询会拿到同一单→重复下单
* **幂等**：本地记已处理的 order_id 并落盘，重启/重连不会重复下单
* **超时≠失败**：网络失败只记日志，**绝不自动重发**，等下一轮 qhyc 侧状态澄清
* **手数上限**：maxLots 硬顶，防止上游异常数据一次下 1000 手
* PythonGO 多为 Python 3.6~3.8：本文件刻意避开 3.9+ 语法（不用 dict[str]、X | None）
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from typing import Any, Dict, List, Optional, Set  # noqa: F401  (3.6 兼容写法)

try:
    from ctaBase import *  # noqa: F401,F403  无限易提供的数据结构与常量
except ImportError:  # 便于在本机做语法/逻辑自测
    pass
from ctaTemplate import CtaTemplate  # noqa: E402

#: ⚠ 编码口径来自 migrations/030_execution_channels.sql 的 CHECK 约束（**以 DB 为准**）：
#:     direction ∈ ('BUY', 'SELL')            —— 是**买卖方向**，不是 LONG/SHORT
#:     action    ∈ ('OPEN','CLOSE','CLOSE_TODAY','CLOSE_YEST')
#: 故 OPEN+BUY=买开(多头)，OPEN+SELL=卖开(空头)，
#:    CLOSE*/SELL=平多，CLOSE*/BUY=平空。
OPEN_ACTION = "OPEN"
CLOSE_ACTIONS = ("CLOSE", "CLOSE_TODAY", "CLOSE_YEST")

#: 兼容别名：若上游仍用 LONG/SHORT 或长写法，先归一化（防御多余的口径，不猜）
_DIRECTION_ALIAS = {"LONG": "BUY", "SHORT": "SELL", "BUY": "BUY", "SELL": "SELL"}
_ACTION_ALIAS = {
    "OPEN": "OPEN", "CLOSE": "CLOSE",
    "CLOSE_TODAY": "CLOSE_TODAY", "CLOSETODAY": "CLOSE_TODAY",
    "CLOSE_YEST": "CLOSE_YEST", "CLOSEYESTERDAY": "CLOSE_YEST",
}

#: 区分平今/平昨的交易所（上期所/能源中心规则，非软件规则）
SPLIT_EXCHANGES = ("SHFE", "INE")

#: 下单类型常量 —— 取值抄自本机 pyStrategy/ctaBase.py（**实测**，勿凭印象改）：
#:   CTAORDER_BUY='买开'  CTAORDER_SHORT='卖开'
#:   CTAORDER_SELL='卖平'(=CTP 平昨)  CTAORDER_SELL_TODAY='卖平今'
#:   CTAORDER_COVER='买平'(=CTP 平昨) CTAORDER_COVER_TODAY='买平今'
#: 之所以写字面量而不 import：ctaBase 在离线自测环境下不可导入，
#: 且这些是**协议级字符串**，不随 Python 版本变化。
OT_BUY = "买开"
OT_SHORT = "卖开"
OT_SELL = "卖平"
OT_SELL_TODAY = "卖平今"
OT_COVER = "买平"
OT_COVER_TODAY = "买平今"

#: 已知交易所白名单。不在其中的一律拒单，绝不猜代码
KNOWN_EXCHANGES = ("SHFE", "DCE", "CZCE", "CFFEX", "INE", "GFEX")


class QhycBridge(CtaTemplate):
    """qhyc → 无限易 执行代理（无策略逻辑）。"""

    className = "QhycBridge"

    # 可视化参数映射表（无限易面板会展示）
    paramMap = {
        "signalUrl": "qhyc 桥接地址",
        "apiKey": "API Key",
        "investor": "资金账号",
        "pollSeconds": "轮询间隔(秒)",
        "maxLots": "单笔手数上限",
        "dryRun": "只日志不下单(1/0)",
        "monitorSymbol": "保持行情订阅的合约",
        "monitorExchange": "订阅合约的交易所",
    }
    varMap = {
        "trading": "运行中",
        "lastPoll": "上次轮询时间",
        "submitted": "已下单数",
        "rejected": "拒单数",
        "lastError": "最近错误",
    }
    paramList = list(paramMap.keys())
    varList = list(varMap.keys())

    # ------------------------------------------------------------ 初始化
    def __init__(self, ctaEngine=None, setting: dict = {}) -> None:  # noqa: B006
        super(QhycBridge, self).__init__(ctaEngine, setting)

        self.signalUrl = str(setting.get("signalUrl", "http://127.0.0.1:8000/execution/bridge"))
        self.apiKey = str(setting.get("apiKey", "") or "")
        self.investor = str(setting.get("investor", "") or "")
        self.pollSeconds = int(setting.get("pollSeconds", 3))
        self.maxLots = int(setting.get("maxLots", 1))
        self.dryRun = str(setting.get("dryRun", "1")) in ("1", "true", "True")
        self.monitorSymbol = str(setting.get("monitorSymbol", "rb2610") or "")
        self.monitorExchange = str(setting.get("monitorExchange", "SHFE") or "")
        self._scheduler = None

        # 运行态变量（无限易面板可见）
        self.trading = False
        self.lastPoll = ""
        self.submitted = 0
        self.rejected = 0
        self.lastError = ""

        self._lock = threading.Lock()
        self._seen: Set[str] = set()          # 已处理过的 qhyc order_id
        self._orderMap: Dict[str, str] = {}    # qhyc order_id -> 无限易 order_id
        self._instCache: Dict[str, str] = {}   # "EXCH|FG2701" -> 原生码

        self._statePath = os.path.join(
            os.path.expanduser("~"), ".qhyc", "infinitrader_bridge_state.json"
        )
        self._load_state()

        self.output(
            "[QhycBridge] 初始化 url={0} dryRun={1} maxLots={2} investor={3}".format(
                self.signalUrl, self.dryRun, self.maxLots, self.investor or "(默认账号)"
            )
        )

    # ------------------------------------------------------------ 生命周期
    def onInit(self) -> None:
        super(QhycBridge, self).onInit()

    def onStart(self) -> None:
        # ★ 必须先设好 vtSymbol/exchange 再调 super().onStart()：
        #   实测 ctaTemplate.onStart() 内部会
        #       symbolList = self.vtSymbol.split(';'); subSymbol()
        #   也就是说**基类会自己订阅**，我不需要也不该自己调 subSymbol()
        #   （早期版本写成 subSymbol(self.monitorSymbol) → TypeError，
        #    因为真实签名是 subSymbol(self)，无参数）。
        if self.monitorSymbol:
            self.vtSymbol = self.monitorSymbol
        if self.monitorExchange:
            self.exchange = self.monitorExchange

        super(QhycBridge, self).onStart()
        self.trading = True
        self.output("[QhycBridge] 启动轮询，间隔 {0}s".format(self.pollSeconds))
        self._start_timer()

    def _start_timer(self) -> None:
        """注册轮询定时器。

        首选 regTimer（引擎原生定时器，不依赖第三方库）；
        失败才回退 utils.Scheduler —— 实测 `Scheduler` 是**类**不是实例，
        必须先实例化再 add_job/start（直接 Scheduler.add_job 会报未绑定方法）。
        """
        try:
            self.regTimer(tid=1, mSecs=self.pollSeconds * 1000)
            self.output("[QhycBridge] 已注册引擎定时器 regTimer(1, {0}ms)".format(
                self.pollSeconds * 1000))
            return
        except Exception as e:
            self.output("[QhycBridge][warn] regTimer 失败，回退 APScheduler：{0!r}".format(e))
        try:
            import utils

            self._scheduler = utils.Scheduler()
            self._scheduler.add_job(self._poll_once, "interval", seconds=self.pollSeconds)
            self._scheduler.start()
            self.output("[QhycBridge] 已注册 APScheduler 定时任务")
        except Exception as e:
            self.lastError = "timer: {0!r}".format(e)
            self.output("[QhycBridge][error] 定时器注册彻底失败：{0!r}".format(e))

    def onStop(self) -> None:
        super(QhycBridge, self).onStop()
        self.trading = False
        self._save_state()
        self.output("[QhycBridge] 已停止")

    def onTick(self, tick) -> None:
        """行情回调。本代理不依赖行情做决策，仅维持连接心跳。"""
        super(QhycBridge, self).onTick(tick)

    def onTimer(self, tid: int) -> None:
        if tid == 1:
            self._poll_once()

    # ------------------------------------------------------------ 主循环
    def _poll_once(self) -> None:
        if not self.trading:
            return
        try:
            orders = self._fetch_pending()
            for od in orders:
                self._handle(od)
            self.lastPoll = datetime.now().strftime("%H:%M:%S")
        except Exception as e:
            # 网络/解析异常只记录，绝不重试下单（超时≠失败）
            self.lastError = "poll: {0!r}".format(e)
            self.output("[QhycBridge][error] 轮询失败：{0!r}".format(e))

    def _handle(self, od: dict) -> None:
        qid = str(od.get("id"))
        # 幂等：已处理过直接跳过（重连/重启的核心保护）
        with self._lock:
            if qid in self._seen:
                return

        # ★ 原子认领：只有认领成功的一方才下单，防重复成交
        claimed = self._claim(qid)
        if not claimed:
            with self._lock:
                self._seen.add(qid)
            return

        try:
            self._execute(od)
        except Exception as e:
            self.lastError = "exec {0}: {1!r}".format(qid, e)
            self.output("[QhycBridge][error] 执行订单 {0} 失败：{1!r}".format(qid, e))
            self._report(qid, "REJECTED", error=str(e))
        finally:
            with self._lock:
                self._seen.add(qid)
            self._save_state()

    def _execute(self, od: dict) -> None:
        qid = str(od.get("id"))
        real_symbol = str(od.get("real_symbol") or "")
        exchange = str(od.get("exchange") or "")
        lots = int(od.get("lots") or 0)
        price = float(od.get("price") or 0)
        action = str(od.get("action") or "").upper()
        direction = str(od.get("direction") or "").upper()

        # ---------- 前置校验（fail-loud，不放过任何异常数据）
        if lots <= 0:
            raise ValueError("手数非法 lots={0}".format(lots))
        if price <= 0:
            raise ValueError("价格非法 price={0}".format(price))
        if lots > self.maxLots:
            self.output(
                "[QhycBridge][risk] 订单 {0} 手数 {1} 超过上限 {2}，已截断".format(
                    qid, lots, self.maxLots
                )
            )
            lots = self.maxLots
        symbol = self._resolve_symbol(real_symbol, exchange)
        if not symbol:
            raise ValueError("合约无法解析 real_symbol={0} exchange={1}".format(
                real_symbol, exchange))

        if self.dryRun:
            self.output(
                "[QhycBridge][dry] {0} {1}({2}) {3} {4}手 @{5}".format(
                    qid, real_symbol, symbol, "{0}/{1}".format(action, direction), lots, price
                )
            )
            self._report(qid, "DRYRUN", broker_order_id="dry-" + qid)
            return

        # ---------- 下单（direction 即买卖方向，见文件头口径说明）
        action_n = _ACTION_ALIAS.get(action)
        direction_n = _DIRECTION_ALIAS.get(direction)
        if not action_n or not direction_n:
            raise ValueError(
                "无法识别的 direction×action：{0}×{1}（期望 direction∈BUY/SELL，"
                "action∈OPEN/CLOSE/CLOSE_TODAY/CLOSE_YEST）".format(direction, action)
            )

        oid = None
        if action_n == OPEN_ACTION:
            oid = self._api_open(direction_n == "BUY", symbol, exchange, price, lots)
        elif action_n in CLOSE_ACTIONS:
            oid = self._api_close(action_n, direction_n, symbol, exchange, price, lots)
        else:
            raise ValueError("不支持的 action：{0}".format(action))

        if oid is None or int(oid) < 0:
            self.rejected += 1
            raise RuntimeError("无限易返回下单失败 order_id={0}".format(oid))

        with self._lock:
            self._orderMap[qid] = str(oid)
        self.submitted += 1
        self.output("[QhycBridge] 已下单 qhyc#{0} → 无限易#{1} {2} {3}手 @{4}".format(
            qid, oid, symbol, lots, price))
        self._report(qid, "SENT", broker_order_id=str(oid))

    # ------------------------------------------------------------ 下单适配
    def _api_open(self, is_long: bool, symbol: str, exchange: str,
                  price: float, lots: int) -> Optional[int]:
        """开仓。

        真实签名（实测 ctaTemplate.py）：
            buy(price, volume, symbol='', exchange='', memo=None, investor='')
            short(...)  同上
        ⚠ 早期版本误把第 5 个位置参数当 investor，实际是 **memo**；
          这里改用关键字传 investor，避免把账号写进备注字段。
        """
        fn = getattr(self, "buy" if is_long else "short", None)
        if fn is not None:
            try:
                return fn(price, lots, symbol, exchange,
                          investor=self.investor or "")
            except TypeError:
                # 极老版本可能不支持 investor 关键字 → 退回位置参数（memo 传 None）
                for args in (
                    (price, lots, symbol, exchange, None),
                    (price, lots, symbol, exchange),
                    (price, lots, symbol),
                ):
                    try:
                        return fn(*args)
                    except TypeError:
                        continue
        return self.sendOrder(OT_BUY if is_long else OT_SHORT,
                              price, lots, symbol, exchange,
                              self.investor or "")

    def _api_close(self, action: str, direction: str, symbol: str, exchange: str,
                   price: float, lots: int) -> Optional[int]:
        """平仓。新版推荐 auto_close_position；老版回退 cover/sell (+_t/_y)。

        ``direction`` 是**买卖方向**：BUY=买平(平空)，SELL=卖平(平多)。
        """
        close_dir = "buy" if direction == "BUY" else "sell"
        ex_flag = exchange.upper()
        split = ex_flag in SPLIT_EXCHANGES

        # 新版：auto_close_position（自动处理今昨，官方推荐）
        fn = getattr(self, "auto_close_position", None)
        if fn is not None:
            kwargs = {"price": price, "volume": lots, "symbol": symbol,
                      "exchange": exchange, "order_direction": close_dir}
            if self.investor:
                kwargs["investor"] = self.investor
            if split:
                # 上期所/能源中心区分；True=昨仓优先，False=今仓优先（默认）
                kwargs["shfe_close_first"] = (action == "CLOSE_YEST")
            try:
                return fn(**kwargs)
            except TypeError:
                pass

        # 老版回退：直接走底层 sendOrder（sendOrder 是 ctaTemplate 的原始接口，全版本都有）
        return self.sendOrder(
            self._legacy_order_type(close_dir, action, split),
            price, lots, symbol, exchange, self.investor or "")

    @staticmethod
    def _legacy_order_type(close_dir: str, action: str, split: bool) -> str:
        """回退路径的下单类型（ctaBase 常量字面量）。

        ⚠ 为什么不用 cover()/sell()：
            实测本机 ctaTemplate.py 里 `cover`/`sell` 已被标记 @deprecated，
            函数体是 `...`（**空实现，返回 None**）——
            早期版本把它们当回退会静默不下单。
            只有带后缀的 sell_t/sell_y/cover_t/cover_y 才是真实现，
            但为统一口径，这里一律走 sendOrder(下单类型)。

        语义（CTP 口径）：'卖平'/'买平' = 平昨；'卖平今'/'买平今' = 平今。
        """
        if close_dir == "buy":          # 买平 = 平空
            return OT_COVER_TODAY if (split and action == "CLOSE_TODAY") else OT_COVER
        return OT_SELL_TODAY if (split and action == "CLOSE_TODAY") else OT_SELL

    # ------------------------------------------------------------ 合约解析
    def _resolve_symbol(self, real_symbol: str, exchange: str) -> str:
        """qhyc 的品种+年份月写法 → CTP 原生合约码。

        实测对应规则（与 qhyc symbol_code.to_native 一致，可交叉验证）：
            FG2701+CZCE  -> FG701    郑商所 3 位
            RB2610+SHFE  -> rb2610   上期所小写
            m2601+DCE    -> m2601    大商所小写
            IF2609+CFFEX -> IF2609   中金所大写
            sc2610+INE   -> sc2610   能源小写
            si2611+GFEX  -> si2611   广期小写
        """
        key = "{0}|{1}".format(exchange, real_symbol)
        if key in self._instCache:
            return self._instCache[key]
        s = (real_symbol or "").strip()
        ex = (exchange or "").upper()
        # ★ fail-loud：交易所缺失或不认识时**拒绝下单**，绝不拿原符号去猜。
        #   合约代码写错（如郑商所少截一位）会以错误合约成交，属不可接受的实盘风险。
        if not s or ex not in KNOWN_EXCHANGES:
            return ""
        digits = "".join(c for c in s if c.isdigit())
        alpha = "".join(c for c in s if c.isalpha())
        if not digits or not alpha:
            return ""
        if ex == "CZCE":
            native = alpha.upper() + (digits[1:] if len(digits) == 4 else digits)
        elif ex in ("SHFE", "DCE", "INE", "GFEX"):
            native = alpha.lower() + digits
        elif ex == "CFFEX":
            native = alpha.upper() + digits
        else:
            native = s
        self._instCache[key] = native
        return native

    # ------------------------------------------------------------ HTTP
    def _http(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        url = self.signalUrl.rstrip("/") + path
        data = None
        headers = {"Content-Type": "application/json"}
        if self.apiKey:
            headers["X-API-Key"] = self.apiKey
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise RuntimeError("HTTP {0} {1}".format(e.code, e.read()[:200]))
        except urllib.error.URLError as e:
            raise RuntimeError("网络不可达 {0!r}".format(e))

    def _fetch_pending(self) -> List[dict]:
        res = self._http("GET", "/pending")
        if isinstance(res, dict):
            return res.get("orders") or []
        return []

    def _claim(self, qid: str) -> bool:
        """原子认领。claim 由后端用 UPDATE ... WHERE status='NEW' 保证只有一个认领者。"""
        try:
            res = self._http("POST", "/claim", {"order_id": int(qid)})
        except Exception as e:
            self.lastError = "claim {0}: {1!r}".format(qid, e)
            return False
        return bool(isinstance(res, dict) and res.get("claimed"))

    def _report(self, qid: str, status: str, broker_order_id: Optional[str] = None,
                filled_lots: int = 0, avg_price: float = 0.0,
                error: Optional[str] = None) -> None:
        body = {"order_id": int(qid), "status": status,
                "filled_lots": filled_lots, "avg_price": avg_price}
        if broker_order_id:
            body["broker_order_id"] = broker_order_id
        if error:
            body["error"] = error
        try:
            self._http("POST", "/report", body)
        except Exception as e:
            self.lastError = "report {0}: {1!r}".format(qid, e)
            self.output("[QhycBridge][error] 回报回写失败（不重试）：{0!r}".format(e))

    # ------------------------------------------------------------ 回报回调
    def onOrder(self, order) -> None:
        super(QhycBridge, self).onOrder(order)
        qid = self._qid_by_broker(getattr(order, "orderID", "") or getattr(order, "vtOrderID", ""))
        if qid:
            self._report(qid, "SENT", filled_lots=int(getattr(order, "tradedVolume", 0) or 0))

    def onTrade(self, trade) -> None:
        super(QhycBridge, self).onTrade(trade, log=True)
        qid = self._qid_by_broker(getattr(trade, "orderID", "") or getattr(trade, "vtOrderID", ""))
        if qid:
            self._report(qid, "FILLED",
                         filled_lots=int(getattr(trade, "volume", 0) or 0),
                         avg_price=float(getattr(trade, "price", 0) or 0))

    def onErr(self, err) -> None:
        super(QhycBridge, self).onErr(err)
        self.lastError = str(err)
        self.output("[QhycBridge][onErr] {0}".format(err))

    def _qid_by_broker(self, broker_id: str) -> Optional[str]:
        if not broker_id:
            return None
        with self._lock:
            for k, v in self._orderMap.items():
                if v == str(broker_id):
                    return k
        return None

    # ------------------------------------------------------------ 幂等状态落盘
    def _save_state(self) -> None:
        try:
            d = os.path.dirname(self._statePath)
            if d and not os.path.isdir(d):
                os.makedirs(d)
            with self._lock:
                payload = {"seen": sorted(self._seen), "order_map": dict(self._orderMap)}
            with open(self._statePath, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        except Exception as e:
            self.output("[QhycBridge][warn] 状态落盘失败：{0!r}".format(e))

    def _load_state(self) -> None:
        try:
            if not os.path.exists(self._statePath):
                return
            with open(self._statePath, "r", encoding="utf-8") as f:
                payload = json.load(f)
            with self._lock:
                self._seen = set(payload.get("seen") or [])
                self._orderMap = dict(payload.get("order_map") or {})
            self.output("[QhycBridge] 已加载幂等状态：{0} 笔已处理".format(len(self._seen)))
        except Exception as e:
            self.output("[QhycBridge][warn] 状态加载失败（从空状态开始）：{0!r}".format(e))
