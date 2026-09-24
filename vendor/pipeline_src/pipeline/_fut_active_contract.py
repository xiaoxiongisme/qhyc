# -*- coding: utf-8 -*-
"""
活跃合约解析器（active-contract resolver）· M2 P1 新增
===================================================
把「品种根（FG / RB / M …）」解析为「当前应跟踪的具体活跃合约（FG2601 / RB2610 …）」，
用于把日线简报的数据层从主连（FG0）切到合约级：
  · 买卖点是真实可挂单价（非主连合成价）；
  · 换月无合成跳空（主连拼接会在换月处产生假缺口）；
  · 保证金 / 手数按真实合约。

解析策略（active_contract 的 strategy 参数）：
  - "live_heuristic"（**默认**，2026-09-23 起）：实时 OI 择优优先 → 日期启发式兜底，
    分歧时打印审计行。起因 = RU2610 事故（heuristic 近月规则把主力 1/5/9 的橡胶
    选到清淡的 2610，价差 4.4%）；持仓量源 = 新浪日线接口（原 akshare 路径无持仓列）。
  - "heuristic"：纯日期法，不依赖网络/数据库，离线可用、可测试（自检保留）。
  - "live"：仅实时 OI 择优，失败返回 None，调用方自行兜底。

注：akshare 无「主连→真实主力合约号」直接映射函数（futures_display_main_sina 仅列主连清单），
故 live 路径用 OI 择优实现，而非查表。

换月判定（rollover_note）：对比上一运行记录的活跃合约，若不同则给出换月提示文案。

已知 v1 局限（2026-09-23 更新）：
  heuristic 用「近月 + 回避交割月」规则，农产品/软商品走 1/5/9（AP 苹果含 1/5/10/11/12），
  其余走近月。RU2610 事故暴露近月规则对主力固定季月的品种会选错合约 → 修复双管齐下：
  ① ONE_FIVE_NINE 补 RU/SP/NR（持仓量实证）；② live_heuristic 设为默认（真实持仓量优先，
  名单仅作离线兜底）。live 路径每次请求节流 0.15s，49 品种全批 ≈ 多耗时 1–2 分钟。
"""
import os
import sys
import datetime

WS = os.path.dirname(os.path.abspath(__file__))
if WS not in sys.path:
    sys.path.insert(0, WS)
try:
    from _fut_specs import FUT_SPECS
except Exception:
    FUT_SPECS = {}

# ---- 活跃月份约定 ----
# 1/5/9（奇数季月）活跃品种：经典农产品/软商品 + 橡胶系；其余品种走近月（front-month）。
# ⚠️ 2026-09-23 RU2610 事故：RU（沪胶）主力固定 1/5/9，但未在名单 → 近月规则选中 RU2610
#    （当时真实主力 RU2701，两合约价差 845 点 / 4.4%），推送给到清淡合约的可挂单价。
# 持仓量实证（新浪日线 2026-09-22 收盘）：
#    RU2610 持仓 455 vs RU2701 143,364（315×）→ 加 RU
#    SP2610 持仓 802 vs SP2701 132,975（166×）→ 加 SP（同病实锤）
#    NR2610 持仓 7,427 vs NR2701 39,224（5.3×）→ 加 NR（主力也是 01）
# 注：live OI 择优（live_heuristic 策略）已设为默认，名单仅作离线兜底与自检参照。
ONE_FIVE_NINE = {"A", "C", "CS", "M", "Y", "P", "RM", "OI", "CF", "SR", "JD", "B",
                 "RU", "SP", "NR"}
# 苹果 AP 额外月份（1/5/10/11/12）；生猪 LH 为奇数月（1/3/5/7/9/11）
EXTRA_MONTHS = {"AP": [1, 5, 10, 11, 12],
                "LH": [1, 3, 5, 7, 9, 11]}

# 交割月回避缓冲：合约近似到期日（当月 15 日）距今日不足该天数 → 视为进入交割月，跳过
DELIVERY_BUFFER_DAYS = 12

# 解析缓存：(root, datestr) -> symbol
_CACHE = {}


def _expiry(y, m):
    """近似到期日：多数商品期货在合约月第 15 个交易日前后进入交割/最后交易。"""
    try:
        return datetime.date(y, m, 15)
    except Exception:
        return datetime.date(y, m, 28)


def _candidate_months(root, as_of):
    """生成候选合约月份（不含已过期、不含已进入交割月的），按到期日升序。"""
    months = EXTRA_MONTHS.get(root)
    if months is None:
        months = [1, 5, 9] if root in ONE_FIVE_NINE else list(range(1, 13))
    cands = []
    for y in (as_of.year, as_of.year + 1, as_of.year + 2):
        for m in months:
            exp = _expiry(y, m)
            if exp > as_of:                       # 仍在交易（未过期）
                cands.append((exp, y % 100, m))
    buf = as_of + datetime.timedelta(days=DELIVERY_BUFFER_DAYS)
    cands = [c for c in cands if c[0] > buf]      # 剔除进入交割月的合约
    cands.sort()
    return cands


def heuristic_active(root, as_of=None):
    """离线日期启发式：取最近的非交割月合约作为活跃合约。"""
    as_of = as_of or datetime.date.today()
    cands = _candidate_months(root, as_of)
    if not cands:
        # 极端情形（全部进入交割）：放宽缓冲取最近候选
        cands = [c for c in _candidate_months(root, as_of - datetime.timedelta(days=DELIVERY_BUFFER_DAYS + 30))
                 if c[0] > as_of] or _candidate_months(root, as_of)
        if not cands:
            return None
    exp, yy, mm = cands[0]
    return f"{root}{yy:02d}{mm:02d}"


def _oi_of(sym):
    """取某具体合约最新持仓量（open interest）；失败返回 (None, None)。

    2026-09-23 重写：原实现用 akshare `futures_hist_em`，但其返回列**不含持仓量**
    （`last.get("持仓量")` 永远 None → live 路径静默失效）。改用新浪期货日线接口
    `InnerFuturesNewService.getDailyKLine`（实测末根含持仓字段 `p`，连发不限流；
    2026-09-22 实证 RU2701 持仓 143,364 vs RU2610 455，可正确区分主力）。
    不依赖 akshare，纯 urllib；每次请求后节流 0.15s 防批量限流。"""
    try:
        import time
        import urllib.request
        import json as _json
        import re as _re
        url = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/"
               "var%20t=/InnerFuturesNewService.getDailyKLine?symbol=" + str(sym))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        txt = urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "ignore")
        m = _re.search(r"\((\[.*\])\)", txt, _re.S)
        rows = _json.loads(m.group(1)) if m else []
        time.sleep(0.1)                      # 批量场景（49品种×5候选）节流
        if rows:
            last = rows[-1]
            try:
                oi = float(last.get("p") or 0)
            except Exception:
                oi = 0.0
            if oi > 0:
                return oi, str(last.get("d", ""))[:10]
    except Exception:
        pass
    return None, None


def live_active(root, as_of=None):
    """实时 OI 择优：对最近几个候选合约取实时持仓量最大者作为活跃合约。

    2026-09-23 起持仓量源 = 新浪日线接口（纯 urllib，见 _oi_of），
    **不再依赖 akshare**——原实现在此 import akshare，无 akshare 环境直接
    返回 None（live 路径静默失效的第二个原因）。任何失败返回 None（兜底 heuristic）。"""
    as_of = as_of or datetime.date.today()
    # 活跃合约几乎总在最近 1~3 个候选月内；但主力固定季月的品种（RU/SP 等 1/5/9），
    # 9 月下旬真实主力（次年 01）排在候选第 4 位（2610/2611/2612/2701）——
    # 2026-09-23 修复：窗口 3 → 5，确保季月主力进得了扫描范围。
    cands = _candidate_months(root, as_of)[:5]
    if not cands:
        return None
    best, best_oi = None, -1.0
    for exp, yy, mm in cands:
        sym = f"{root}{yy:02d}{mm:02d}"
        oi, _ = _oi_of(sym)
        if oi is None:
            continue
        if oi > best_oi:
            best_oi, best = oi, sym
    return best


def active_contract(root, as_of=None, strategy="live_heuristic"):
    """返回该品种当前活跃合约号（如 RU2701）。root 为品种字母根。

    strategy:
      "live_heuristic" 实时 OI 择优优先（新浪持仓量，不依赖 akshare），失败/无数据
                       → 日期启发式兜底；live 与 heuristic 分歧时打印审计行（**默认**）
      "auto"           同 live_heuristic（旧名保留兼容）
      "live"           仅实时 OI 择优（失败返回 None，调用方自行兜底）
      "heuristic"      仅日期启发式（离线、确定性；自检用）
    """
    as_of = as_of or datetime.date.today()
    key = (root, as_of.isoformat())
    if key in _CACHE:
        return _CACHE[key]
    sym = None
    live_tried = False
    if strategy in ("auto", "live", "live_heuristic"):
        live_tried = True
        sym = live_active(root, as_of)
    if sym is None and strategy in ("auto", "heuristic", "live_heuristic"):
        h = heuristic_active(root, as_of)
        if live_tried and h:
            print(f"[active_contract] {root}: live OI 择优失败 → heuristic 兜底 {h}")
        sym = h
    if sym is None:
        sym = f"{root}0"          # 终极兜底：主连
    # 审计：live（真实持仓）与 heuristic（日期规则）分歧时提示——正是 2026-09-23
    # RU2610 事故的检测信号（heuristic 选 2610，live 持仓择优会选 2701）
    if strategy in ("auto", "live_heuristic"):
        try:
            h = heuristic_active(root, as_of)
            if sym and h and sym != h:
                print(f"[active_contract] {root}: live={sym} vs heuristic={h}"
                      f"（以实时持仓量为准）")
        except Exception:
            pass
    _CACHE[key] = sym
    return sym


def rollover_note(root, prev_symbol, cur_symbol, name=""):
    """若活跃合约发生换月，返回提示文案；否则返回空串。"""
    if not prev_symbol or not cur_symbol or prev_symbol == cur_symbol:
        return ""
    nm = (name or root).replace("连续", "")
    return (f"🔄换月：{nm}{prev_symbol} → {nm}{cur_symbol}，"
            f"旧仓按次日开盘平旧开新（或移仓至新活跃合约）")
