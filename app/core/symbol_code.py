# -*- coding: utf-8 -*-
"""合约代码统一口径：**标准码 = 品种码(大写) + YYMM(四位)**，如 ``AP2701``。

为什么需要这个模块
------------------
五所官方合约代码的「月份位数 + 大小写」各不相同（都是交易所原生写法）：

| 交易所 | 原生写法 | 月份位数 | 大小写 | 例 |
|---|---|---|---|---|
| CZCE  | 品种码 + **个位年** + 月 | **3 位** | 大写 | ``AP701`` |
| SHFE  | 品种码 + 两位年 + 月     | 4 位 | **小写** | ``cu2611`` |
| DCE   | 同上                     | 4 位 | **小写** | ``a2601`` |
| GFEX  | 同上                     | 4 位 | **小写** | ``si2611`` |
| CFFEX | 同上                     | 4 位 | 大写 | ``IF2609`` |
| 新浪  | 全部两位年 + 月          | **4 位** | 大写 | ``AP2701`` |

已实际发生的后果（2026-09-20 审计）：
- ``member_position_rank`` 里同一合约曾出现两套 symbol（官方 ``AP701`` / 新浪 ``AP2701``），
  跨所聚合、跨源 join 全部对不上，下游按 symbol 聚合的持仓因子被拆成两条稀疏序列；
- ``contract_daily`` / ``main_contract_map`` / ``spot_basis`` 各自沿用不同写法，
  库内四套口径并存。

本模块把上述写法统一成**标准码**，并把各源原生写法登记进 ``contract_code_map``
代码表（DDL 见 ``db/init/14_contract_code.sql``）。**采集器入库前一律 ``to_std()``**；
需要回写交易所原生码时用 ``to_native()``。

命名空间说明（不参与转换）
--------------------------
- ``<品种>888``：本项目**主力连续（合成）码**，不是真实合约。``futures_symbol`` /
  ``daily_bar`` / ``hourly_bar`` / ``prediction_result`` / ``fusion_position`` 等
  继续使用，``to_std`` 原样返回。
- ``KQ.m@EXCHANGE.PRODUCT``：天勤主连码。
- ``CZCE.AP701``：天勤具体合约码（**原生 3 位**，天勤按此订阅）。见
  ``contract_code_map.tqsdk_symbol``，**物理值不改**（改了天勤就取不到数）。
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional, Tuple

__all__ = [
    "CONTINUOUS_SUFFIX",
    "NATIVE_MONTH_DIGITS",
    "NATIVE_CASE",
    "is_continuous",
    "is_std",
    "split",
    "to_std",
    "to_native",
    "to_sina",
    "to_tqsdk",
    "product_of",
    "delivery_ym",
]

#: 主力连续（合成）码后缀，如 ``FG888``
CONTINUOUS_SUFFIX = "888"

#: 各交易所**原生**月份位数（3 = 郑商所个位年写法）
NATIVE_MONTH_DIGITS: dict[str, int] = {
    "CZCE": 3, "SHFE": 4, "DCE": 4, "GFEX": 4, "CFFEX": 4, "INE": 4,
}

#: 各交易所**原生**字母大小写（上期/大商/广期/上期能源小写，郑商/中金大写）
NATIVE_CASE: dict[str, str] = {
    "CZCE": "upper", "SHFE": "lower", "DCE": "lower",
    "GFEX": "lower", "CFFEX": "upper", "INE": "lower",
}

# 原生/标准码：字母（品种）+ 数字（月份）
_CODE_RE = re.compile(r"^([A-Za-z]+)(\d{2,6})$")


def is_continuous(symbol: object) -> bool:
    """是否主力连续（合成）码，形如 ``FG888``。"""
    s = str(symbol or "").strip()
    return s.endswith(CONTINUOUS_SUFFIX) and s[: -len(CONTINUOUS_SUFFIX)].isalpha()


def is_std(symbol: object) -> bool:
    """是否已是标准码（品种大写 + YYMM 四位）。"""
    prod, num = split(symbol)
    return prod is not None and len(num) == 4 and not is_continuous(symbol)


def split(symbol: object) -> Tuple[Optional[str], Optional[str]]:
    """拆成 ``(品种码大写, 月份串)``；不是「字母+数字」形态时返回 ``(None, None)``。"""
    m = _CODE_RE.match(str(symbol or "").strip().upper())
    if not m:
        return None, None
    return m.group(1), m.group(2)


def _today() -> date:
    return date.today()


def _expand_ymm(year_digit: str, mm: str, ref: date) -> str:
    """郑商所 3 位月份 → ``YYMM``。

    3 位码的「年」只有**个位**（十年一循环）。取「不早于参考月的最小年份」：

    ==========  ==========  =========================================
    3 位码      参考月      结果
    ==========  ==========  =========================================
    ``AP701``   2026-09     ``AP2701``（2027-01）
    ``CF609``   2026-09     ``CF2609``（2026-09，当月合约）
    ``SR105``   2026-09     ``SR3105``（2031-05；2021-05 已过）
    ==========  ==========  =========================================
    """
    d = int(year_digit)
    ref_ym = ref.year * 100 + ref.month
    target_m = int(mm)
    for y in range(ref.year - 1, ref.year + 13):
        if y % 10 == d and y * 100 + target_m >= ref_ym:
            return f"{y % 100:02d}{mm}"
    # 不可达（13 年足以覆盖个位循环 × 月份偏移）
    raise ValueError(f"无法为 3 位月份 {year_digit}{mm} 补全年份（参考 {ref}）")


def to_std(
    symbol: object,
    exchange: Optional[str] = None,
    ref_date: Optional[date] = None,
) -> str:
    """任意写法 → **标准码**（品种大写 + YYMM 四位）。

    - 已是标准码 / 连续码（``888``）/ 天勤码（含 ``.`` 或 ``@``）→ 原样返回（仅去空白、转大写）
    - **3 位月份**（仅郑商所原生形态）→ 按 ``ref_date`` 补全年份
    - 其他（非「字母+数字」）→ 原样返回

    ``exchange`` 只用于断言/日志（3 位码在郑商所之外不存在，故判定不依赖它）。
    ``ref_date`` 默认今天；回溯历史数据时**必须显式传该行的交易/报告日**，
    否则跨年会补错年份。
    """
    raw = str(symbol or "").strip()
    # 主连码 / 天勤码 / 指数码 —— 非「合约码」命名空间，原样返回
    # （``KQ.m@`` 的 ``m`` 大小写敏感，绝不能整体 upper）
    if not raw or "@" in raw or "." in raw or ":" in raw:
        return raw
    if is_continuous(raw):
        return raw
    s = raw.upper()
    prod, num = split(s)
    if prod is None:
        return s
    if len(num) == 4:
        return s
    if len(num) == 3:
        # 3 位 = 郑商所原生（个位年 + 月）
        return prod + _expand_ymm(num[0], num[1:], ref_date or _today())
    return s


def to_native(std_symbol: object, exchange: object) -> str:
    """标准码 → 指定交易所的**原生写法**。

    - CZCE：``AP2701`` → ``AP701``（3 位）
    - SHFE / DCE / GFEX / INE：``RB2610`` → ``rb2610``（小写）
    - CFFEX：``IF2609`` → ``IF2609``（不变）
    - 连续码（``FG888``）/ 无法识别 → 原样返回
    """
    s = str(std_symbol or "").strip().upper()
    if is_continuous(s):
        return s
    prod, num = split(s)
    if prod is None:
        return s
    ex = str(exchange or "").strip().upper()

    if NATIVE_MONTH_DIGITS.get(ex, 4) == 3:
        # 郑商所：YYYY 月份去掉首位年（2701 -> 701）
        return f"{prod}{num[1:]}" if len(num) == 4 else s
    if NATIVE_CASE.get(ex, "upper") == "lower":
        return f"{prod.lower()}{num}"
    return s


def to_sina(std_symbol: object, exchange: Optional[str] = None) -> str:
    """标准码 → **新浪源写法**。

    实测（2026-09-20，``futures_hold_pos_sina`` + ``match_main_contract``）：
    新浪对**所有**交易所都用「两位年 + 月、全大写」，即与标准码同构
    （郑商所 ``AP2701``、上期 ``RB2610``、大商 ``A2611``、广期 ``SI2611``）。
    —— 这正说明新浪本身不产生 symbol 冲突，冲突来自它与**官方原生 3 位码**并存。

    保留为独立函数而非直接用 ``to_std``：将来若新浪换写法，只需改这里。
    """
    return to_std(std_symbol, exchange=exchange)


def to_tqsdk(std_symbol: object, exchange: object) -> str:
    """标准码 → 天勤具体合约码（``CZCE.AP701`` 形式，郑商所仍是原生 3 位）。

    ⚠️ 仅用于**登记到代码表**（``contract_code_map.tqsdk_symbol``）；
    ``fut_kline.symbol`` 存的已是天勤原生码，**不做物理改写**（改了天勤取数就对不上）。
    """
    ex = str(exchange or "").strip().upper()
    return f"{ex}.{to_native(std_symbol, ex)}" if ex else str(std_symbol or "")


def product_of(symbol: object) -> str:
    """取品种码（大写）。``AP2701`` → ``AP``；``FG888`` → ``FG``；
    ``CZCE.AP610`` → ``AP``；识别失败返回 ``""``。
    """
    raw = str(symbol or "").strip()
    if not raw:
        return ""
    if "@" in raw or "." in raw:
        raw = raw.split(".")[-1]      # 天勤码取 ``EX.`` 之后那段
    if ":" in raw:
        return ""
    raw = raw.upper()
    prod, num = split(raw)
    if prod is not None:
        return prod
    return raw if raw.isalpha() else ""


def delivery_ym(std_symbol: object, ref_date: Optional[date] = None) -> Tuple[Optional[int], Optional[int]]:
    """标准码 → ``(交割年, 交割月)``。3 位原生码会先用 ``ref_date`` 补全。"""
    std = to_std(std_symbol, ref_date=ref_date)
    if is_continuous(std):
        return None, None
    _, num = split(std)
    if num is None or len(num) != 4:
        return None, None
    return 2000 + int(num[:2]), int(num[2:])
