# -*- coding: utf-8 -*-
"""品种规格表：把 config.json 里的一个 symbol 展开成回测需要的全部参数。

原先合约乘数、最小变动价位是写在 config.json 里的两个裸数字，只对玻璃成立。
换品种时这两个数必须跟着改，改错了不会报错，只会算出一个看起来正常的错数
（乘数差 10 倍，盈亏就差 10 倍）。所以把它们收进 symbols.json 按品种查，
config.json 里只填 symbol，避免人工填错。

symbols.json 里的规格按交易所公开的合约细则录入，若交易所调整了乘数，
改那个文件即可，代码不用动。
"""
import json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(HERE, "symbols.json")


class SymbolError(Exception):
    """品种代码有问题。提示信息直接给使用者看。"""


def _load():
    if not os.path.exists(_PATH):
        raise SymbolError(f"找不到品种规格表：{_PATH}\n"
                          f"  它应该和 config.json 放在同一个文件夹里，"
                          f"从交付包里重新解压一份即可。")
    try:
        with open(_PATH, encoding="utf-8-sig") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise SymbolError(f"symbols.json 格式有误（第 {e.lineno} 行）：{e.msg}\n"
                          f"  从交付包里重新解压一份即可。")
    return {k: v for k, v in raw.items() if not k.startswith("_")}


TABLE = _load()

# 代码(小写) -> 规范 key 反查表，供「裸合约代码」(如 FG701) 反推交易所前缀。
# 郑商所代码大写(FG)、上期所小写(rb)，反查时统一小写比对，命中后回带规范 key。
_CODE_TO_KEY = {}
for _k in TABLE:
    _CODE_TO_KEY[_k.split(".", 1)[1].lower()] = _k

# 交易所中文名，只用于报告里显示
EXCHANGES = {
    "SHFE": "上期所", "INE": "上期能源", "DCE": "大商所",
    "CZCE": "郑商所", "GFEX": "广期所", "CFFEX": "中金所",
}

# 每个品种必须齐备的字段，缺一个就说清楚缺哪个，别等到回测算到一半才崩
_REQUIRED = ("name", "exchange", "multiplier", "tick", "months", "code_digits")


def normalize(sym):
    """把用户可能的各种写法收敛成表里的键。

    允许：CZCE.FG / czce.fg / FG / fg / KQ.m@CZCE.FG
    交易所前缀大小写不敏感；品种代码大小写在国内是有区分的
    （郑商所全大写 FG，上期所全小写 rb），所以只在匹配阶段忽略大小写，
    返回的仍然是表里的规范写法。
    """
    s = str(sym).strip()
    if not s:
        raise SymbolError("config.json 里的 symbol 是空的，请填一个品种代码，例如 CZCE.FG。")
    # 天勤主连写法 KQ.m@CZCE.FG，取 @ 后面那段
    if "@" in s:
        s = s.split("@", 1)[1]

    if s in TABLE:
        return s

    low = s.lower()
    # 带交易所前缀：整体比对
    for k in TABLE:
        if k.lower() == low:
            return k
    # 不带前缀：只比对品种部分，唯一匹配才接受
    hits = [k for k in TABLE if k.split(".", 1)[1].lower() == low]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        raise SymbolError(
            f"品种代码「{sym}」在多个交易所都有：{'、'.join(hits)}\n"
            f"  请写全，例如 {hits[0]}。")
    raise SymbolError(
        f"不认识的品种代码「{sym}」。\n"
        f"  支持的品种共 {len(TABLE)} 个，写法是「交易所.代码」，例如 CZCE.FG（玻璃）、"
        f"SHFE.rb（螺纹钢）、DCE.i（铁矿石）。\n"
        f"  完整清单见 symbols.json，或运行 python symbols.py 列出全部。")


# 具体合约：交易所.品种代码+合约年月（3~4 位数字）。例如 CZCE.FG2701（玻璃 2027-01 合约）、
# SHFE.rb2601（螺纹钢 2026-01 合约）。郑商所代码大写(FG)、上期所小写(rb)，数字 3 或 4 位。
_CONTRACT_RE = re.compile(r"^([A-Za-z]+)\.([A-Za-z]+)(\d{3,4})$")
# 裸写法：品种代码+数字，不带交易所前缀。例如 FG701 / fg701 / rb2601。
# 反查交易所前缀靠 _CODE_TO_KEY（郑商所大写 FG、上期所小写 rb，统一小写比对）。
_BARE_RE = re.compile(r"^([A-Za-z]+)(\d{3,4})$")


def parse_contract(sym):
    """把具体合约写法解析成 (基础品种key, 合约代码)。

    支持两种写法：
      · 带前缀：CZCE.FG2701 -> ('CZCE.FG', 'FG2701')
      · 裸代码：FG701 / fg701 -> ('CZCE.FG', 'FG701')  （自动反查交易所前缀）
    不是具体合约写法时返回 None。用途：具体合约的乘数/跳动与主连相同，
    可继承基础品种规格，仅数据源换成该合约本身。
    """
    s = str(sym).strip()
    if "@" in s:           # KQ.m@ 主连不算具体合约
        return None
    m = _CONTRACT_RE.match(s)
    if m:
        ex, code, num = m.group(1), m.group(2), m.group(3)
        target = f"{ex}.{code}".lower()
        for k in TABLE:
            if k.lower() == target:
                return k, f"{code}{num}"
        return None
    # 裸写法：品种代码 + 3~4 位数字，用反向表找交易所前缀
    m = _BARE_RE.match(s)
    if m:
        code, num = m.group(1), m.group(2)
        key = _CODE_TO_KEY.get(code.lower())
        if key:
            return key, f"{code.upper()}{num}"
    return None


def get(sym):
    """取某个品种或具体合约的规格。返回 dict 含 key / 中文全名 / 数据源代码 tq_cont。

    传入具体合约（如 CZCE.FG2701）时，继承该品种主连的乘数/跳动等规格，
    但 code/tq_cont 改为合约代码本身（数据源即该合约，而非 KQ.m@ 主连）。
    """
    pc = parse_contract(sym)
    if pc:
        base_key, contract_code = pc
        contract_code = contract_code.upper()   # 天勤合约代码大写（CZCE.FG2701）
        spec = dict(TABLE[base_key])
        miss = [f for f in _REQUIRED if f not in spec]
        if miss:
            raise SymbolError(f"symbols.json 里 {base_key} 缺少字段：{'、'.join(miss)}")
        for f in ("multiplier", "tick"):
            v = spec[f]
            if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
                raise SymbolError(f"symbols.json 里 {base_key} 的 {f} 必须是正数，现在是「{v}」。")
        # key 用完整合约代码（CZCE.FG701），而非主连 base_key，
        # 否则回测 load() 用 cfg['symbol'] 反查时会当成主连、读错表。
        spec["key"] = f"{base_key.split('.', 1)[0]}.{contract_code}"
        spec["code"] = contract_code
        spec["base_key"] = base_key
        spec["is_contract"] = True
        spec["multiplier"] = float(spec["multiplier"])
        spec["tick"] = float(spec["tick"])
        spec["exchange_cn"] = EXCHANGES.get(spec["exchange"], spec["exchange"])
        spec["label"] = f"{spec['name']}{contract_code}"
        # 数据源 = 该具体合约本身（非 KQ.m@ 主连）；大写对齐天勤，始终带交易所前缀
        # base_key 形如 CZCE.FG，交易所前缀取第一段，避免拼成 CZCE.FG.FG701
        spec["tq_cont"] = f"{base_key.split('.', 1)[0]}.{contract_code}"
        return spec

    key = normalize(sym)
    spec = dict(TABLE[key])

    miss = [f for f in _REQUIRED if f not in spec]
    if miss:
        raise SymbolError(f"symbols.json 里 {key} 缺少字段：{'、'.join(miss)}")
    for f in ("multiplier", "tick"):
        v = spec[f]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise SymbolError(f"symbols.json 里 {key} 的 {f} 必须是正数，现在是「{v}」。")

    spec["key"] = key
    spec["code"] = key.split(".", 1)[1]
    spec["is_contract"] = False
    spec["multiplier"] = float(spec["multiplier"])
    spec["tick"] = float(spec["tick"])
    spec["exchange_cn"] = EXCHANGES.get(spec["exchange"], spec["exchange"])
    spec["label"] = f"{spec['name']}{spec['code']}"
    # 天勤主力连续代码
    spec["tq_cont"] = f"KQ.m@{key}"
    return spec


def contract_codes(sym, years=range(10), from_year=None):
    """枚举该品种可能的具体合约代码，供抓取历史合约用。

    郑商所是 3 位码（FG601，年份只有个位数、十年一循环），其余是 4 位（rb2601）。
    这里把可能的年份全列出来，不存在或已下市的合约在抓取时会被跳过，
    所以宁可多列，不要漏。

    ⚠️ 本函数产出的是**天勤原生码**（``CZCE.FG601``），不是全库统一的 4 位标准码
    —— 天勤按原生码订阅，故这里**必须保持 3 位**。
    需要与业务表（standard 4 位）对齐时用 ``app.core.symbol_code.to_std``；
    反向（标准码 → 天勤码）用 ``symbol_code.to_tqsdk``。

    from_year：可选，只枚举该年份（含）之后的合约。取数脚本会据此尊重
    config.json 的 start —— 从 start 的前一年起取（多取一年是为了让 start
    那年年初的换月也能对上复权锚点），避免无谓地去请求天勤早已下架的远古合约
    （如纯碱 SA005），既快又不会撞 TqTimeoutError。不传时仍用 years，保持旧行为。
    """
    spec = get(sym)
    ex, code, nd = spec["exchange"], spec["code"], spec["code_digits"]
    # 品种上市年份：早于上市年的合约在天勤根本不存在（如纯碱 SA 2021 才上市，
    # 用 from_year=2020 会枚举出 SA001/SA005 这些噪音合约，订阅必报「未上市」）。
    # 枚举起点取 max(config 的 start 年, 实际上市年)，从源头消除不存在的合约码。
    listed = int(spec.get("listed_year", 2006))
    if from_year is not None:
        from_year = max(int(from_year), listed)
        cy = _this_year()
        if nd == 3:
            # 郑商所 3 位码年份只有个位（十年一循环），完整年份取个位。
            # 注意 range 在个位上跨十年会回绕（如 2021->1, 2026->6），
            # 生成的 SA1xx..SA6xx 同时覆盖 201x 与 202x，天勤按代码取最近，
            # 这对「取 start 前后几年」足够。默认 range(10) 仍是老行为。
            fy = max(0, int(from_year) % 10)
            years = range(fy, (cy % 10) + 1)
        else:
            years = range(max(0, int(from_year)), cy + 1)
    out = []
    if nd == 3:
        for y in years:
            for m in spec["months"]:
                out.append(f"{ex}.{code}{y}{m:02d}")
    else:
        for y in years:
            for m in spec["months"]:
                # 4 位码=2位年份后两位+2位月份，如 eg2001。y 是完整年份(2020)，
                # 必须取 %100(20)，否则会拼成 eg202001(6位)导致合约代码错误。
                out.append(f"{ex}.{code}{y % 100:02d}{m:02d}")
    return out


def _this_year():
    import datetime as _dt
    return _dt.datetime.now().year


def data_files(sym):
    """该品种的数据文件名。按品种分开存，换品种不会互相覆盖。"""
    code = get(sym)["code"]
    return {
        "daily":  f"data/{code}_daily.json",       # 天勤主连（未复权）
        "contracts": f"data/{code}_contracts.json",  # 各具体合约
        "cont_adj":  f"data/{code}_cont_adj.json",   # 自建复权主连
        "rolls":  f"data/{code}_rolls.csv",         # 换月表
    }


def describe(sym):
    """一行话说明该品种的规格，报告和信号脚本都用它，保证口径一致。"""
    s = get(sym)
    return (f"{s['name']}（{s['key']}，{s['exchange_cn']}）  "
            f"合约乘数 {s['multiplier']:g} / 手，"
            f"最小变动价位 {s['tick']:g} {s.get('unit', '')}")


def tick_value(sym):
    """一跳等于多少钱（每手）。滑点换算成金额时用。"""
    s = get(sym)
    return s["tick"] * s["multiplier"]


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"共支持 {len(TABLE)} 个品种：\n")
    by_ex = {}
    for k in TABLE:
        by_ex.setdefault(TABLE[k]["exchange"], []).append(k)
    for ex in ("SHFE", "INE", "DCE", "CZCE", "GFEX", "CFFEX"):
        if ex not in by_ex:
            continue
        print(f"—— {EXCHANGES.get(ex, ex)}（{ex}）——")
        for k in by_ex[ex]:
            s = TABLE[k]
            print(f"  {k:<12} {s['name']:<8} 乘数 {s['multiplier']:>7g}  "
                  f"最小变动 {s['tick']:<7g} {s.get('unit', '')}")
        print()
    print("在 config.json 的 symbol 填上面任意一个代码即可，")
    print("合约乘数和最小变动价位会自动带出来，不用手填。")
