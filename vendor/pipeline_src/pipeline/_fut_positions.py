# -*- coding: utf-8 -*-
r"""
持仓台账 + 手续费 / 账户参数管理
================================
设计原则（用户规则）：
  1. 持仓台账 _positions.csv 由用户直接编辑。脚本**只刷新行情派生列**，
     用户填写的列（买入点位 / 手数 / 止损位 / 止盈位 / 手续费 / 备注 …）原样保留。
     —— 用户改了就以用户的为准；用户没改就保持不变。
  2. 手续费与账户参数集中在 _fee_config.csv，用户可改，不改则用默认值。
  3. 所有点位以**当日收盘价**界定，次日开盘执行（收盘后确认、不盘中抢跑）。

文件：
  E:\QH\期货简报\持仓情况和手续费\_positions.csv   持仓台账（用户主编辑对象）
  E:\QH\期货简报\持仓情况和手续费\_fee_config.csv  手续费与账户参数（用户可编辑）
"""
import os
import csv
import datetime

import pandas as pd

WS = os.path.dirname(os.path.abspath(__file__))
# 输出根目录迁移至 E:\QH\期货简报\（2026-09-04 调整），持仓/手续费 csv 落到「持仓情况和手续费」
BASE_DIR = os.environ.get("QH_BRIEF_BASE", r"E:/QH/期货简报")
HOLD_DIR = os.path.join(BASE_DIR, "持仓情况和手续费")
os.makedirs(HOLD_DIR, exist_ok=True)

# 向后兼容：保留 OUT_DIR 名称（指向 HOLD_DIR，旧代码若引用会正确解析）
OUT_DIR = HOLD_DIR

POS_PATH = os.path.join(HOLD_DIR, "_positions.csv")
FEE_PATH = os.path.join(HOLD_DIR, "_fee_config.csv")

# ---- 台账列定义 ----
# 用户列：脚本只读不写（数值参与计算，文本原样保留）
# 按填写优先级与必要性分组（与生产配置说明 §4 持仓台账口径一致）：
#   ▣ 必填(开仓)  · 6 列  —— 缺一则该笔无法被引擎识别
#   ▣ 平仓          · 2 列  —— 平仓时填写，标记状态=已平
#   ▢ 选填(可默认) · 5 列  —— 留空则脚本按引擎/fee_config/品种规格自动取值
#   ▢ 备注          · 1 列  —— 自由文本，记录特殊情形
POS_USER_REQ_OPEN = ["品种", "代码", "方向", "买入日期", "买入点位", "手数"]
POS_USER_REQ_CLOSE = ["平仓点位", "状态"]
# 锁仓 3 列（v2.1，与回测 --lock-* 同语义）：
#   锁仓腿价 —— 用户锁仓执行后回填锁仓腿成交价；**非空 = 已锁仓**（与「平仓点位非空=已平」同哲学）
#   锁仓手数 —— 留空 = 与原仓等量（回测口径：反向等量锁仓）
#   锁仓ATR  —— 锁仓当日简报会提示当日 ATR，回填后用于解锁判定；留空 = 用当前 ATR 近似
POS_USER_OPT = ["止损位", "止盈位", "每手吨数", "手续费开", "手续费平",
                "锁仓腿价", "锁仓手数", "锁仓ATR", "备注"]
POS_USER_COLS = POS_USER_REQ_OPEN + POS_USER_REQ_CLOSE + POS_USER_OPT
# 自动列：每次运行由脚本按最新行情刷新（"参考止损/参考止盈"为引擎按 ATR 算出的默认值，
# 仅供你在"止损位/止盈位"留空时参考）
POS_AUTO_COLS = ["最新收盘日", "当前价", "毛盈亏", "开仓手续费", "平仓手续费",
                 "净浮盈", "保证金占用", "参考止损", "参考止盈", "建议动作", "动作说明", "更新时间",
                 "锁仓状态", "锁仓腿浮盈"]
POS_COLS = POS_USER_COLS + POS_AUTO_COLS

# ---- 手续费配置默认模板 ----
FEE_COLS = ["类型", "代码", "名称", "计费方式", "开仓费率", "平仓费率", "平今费率", "备注"]
DEFAULT_FEE_ROWS = [
    ["账户", "ACCOUNT", "账户参数", "-", "", "", "",
     "账户资金=150000;单笔风险=4%;月亏上限=8%;默认计费=per_lot"],   # M2 P1：本金 15w / 单笔 4% / 月亏 8%
    ["默认", "DEFAULT", "全品种默认", "per_lot", 3.0, 3.0, 3.0,
     "per_lot=元/手（开/平各计一次）;percent=按成交额比例(如0.0001=万分之一)"],
]


def _num(v):
    """宽松数值解析，空/非法值返回 None"""
    if v is None:
        return None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            if pd.isna(v):
                return None
        except Exception:
            pass
        return float(v)
    s = str(v).strip().replace(",", "").replace("，", "")
    if s in ("", "-", "nan", "None"):
        return None
    try:
        return float(s)
    except Exception:
        return None


def norm_root(code):
    """把合约代码归一化为品种字母根（FG2701 -> FG，RB2401 -> RB）；纯字母根原样返回。
    用于持仓台账「代码」列兼容用户填写的主力合约号（如 FG2701）而非品种根（FG）。"""
    s = str(code or "").strip().upper()
    return s.rstrip("0123456789") if s else s


# 已平仓状态标识：状态列命中以下任一即视为已了结
CLOSED_TOKENS = ("已平", "已平仓", "平仓", "平", "退出", "清仓",
                 "closed", "close", "exit", "done", "out")
# 备注/平仓点位里的已平关键词（用户常把"2026/9/3 平仓"写到备注或直接填「平仓点位」，
# 而不填「状态」列，故以下任一字段命中即视为已平）
CLOSED_KEYWORDS = ("平仓", "已平", "退出", "清仓", "离场")

def is_closed(row):
    """台账该持仓是否已平仓/了结——四选一命中即视为终态：
       ① 状态列命中 CLOSED_TOKENS（最显式）
       ② 平仓点位非空（用户填了 = 已平）
       ③ 备注含已平关键词（"2026/9/3 平仓" 等）
       ④ 平仓手续费非空（用户填了开/平手续费 = 已实现盈亏有记录）
    终态下系统不再推送止损/止盈/反手提醒，仅保留已实现盈亏快照供复盘。"""
    # ① 状态列
    s = str(row.get("状态", "")).strip()
    if s and (s in CLOSED_TOKENS or s.lower() in CLOSED_TOKENS):
        return True
    # ② 平仓点位非空
    if _num(row.get("平仓点位")) is not None:
        return True
    # ③ 备注含已平关键词
    note = str(row.get("备注", "")).strip()
    if note and any(kw in note for kw in CLOSED_KEYWORDS):
        return True
    return False


def _name_to_root(specs):
    """品种中文名/字母根 -> 字母根 的反查表（用于从台账「品种」列重建合约代码）。
    同时支持「品种」列已填字母根（MA/CF/...）的情形——直接自映射，避免漏匹配。"""
    out = {}
    for k, v in (specs or {}).items():
        if v.get("name"):
            out[str(v["name"]).strip()] = k
        out[str(k).strip()] = k  # 自映射：MA→MA，便于「品种」列已写字母根时直接命中
    return out


def _row_key(row):
    """台账行唯一键（品种|代码），用于具体合约行情映射，避免不同品种同月份代码冲突。"""
    return f"{str(row.get('品种', '')).strip()}|{str(row.get('代码', '')).strip()}"


# =====================================================================
# 手续费与账户参数
# =====================================================================
def init_fee_config(force=False):
    """生成手续费配置模板（已存在则不覆盖，除非 force）"""
    if os.path.exists(FEE_PATH) and not force:
        return FEE_PATH
    with open(FEE_PATH, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(FEE_COLS)
        for r in DEFAULT_FEE_ROWS:
            w.writerow(r)
        # 预置 49 个品种的空白覆盖行，方便用户逐个填真实手续费
        try:
            univ = pd.read_csv(os.path.join(WS, "_fut_universe_final.csv"), encoding="utf-8-sig")
            for _, rr in univ.iterrows():
                w.writerow(["品种", rr["root"], rr["name"], "", "", "", "", "留空则用 DEFAULT"])
        except Exception:
            pass
    return FEE_PATH


def load_fee_config():
    """返回 dict：{'account': {...}, 'default': {...}, 'by_root': {root: {...}}}"""
    init_fee_config()
    cfg = {"account": {"资金": 150000.0, "单笔风险": 0.04, "月亏上限": 0.08, "默认计费": "per_lot"},   # M2 P1：硬默认同步 150k/4%/8%
           "default": {"mode": "per_lot", "open": 3.0, "close": 3.0, "closetoday": 3.0},
           "by_root": {}}
    try:
        df = pd.read_csv(FEE_PATH, encoding="utf-8-sig", dtype=str)
    except Exception:
        return cfg
    df = df.fillna("")
    for _, r in df.iterrows():
        typ = str(r.get("类型", "")).strip()
        code = str(r.get("代码", "")).strip()
        mode = str(r.get("计费方式", "")).strip().lower() or None
        op = _num(r.get("开仓费率"))
        cl = _num(r.get("平仓费率"))
        ct = _num(r.get("平今费率"))
        if typ == "账户" or code == "ACCOUNT":
            note = str(r.get("备注", ""))
            for kv in note.replace("；", ";").split(";"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    k, v = k.strip(), v.strip()
                    try:
                        if k in ("账户资金", "资金"):
                            cfg["account"]["资金"] = float(v)
                        elif k in ("单笔风险", "风险比例"):
                            cfg["account"]["单笔风险"] = float(v.replace("%", "")) / (
                                100 if "%" in v else 1)
                        elif k in ("月亏上限",):
                            cfg["account"]["月亏上限"] = float(v.replace("%", "")) / (
                                100 if "%" in v else 1)
                        elif k in ("默认计费",):
                            cfg["account"]["默认计费"] = v
                    except Exception:
                        pass
            continue
        rec = {}
        if mode:
            rec["mode"] = mode
        if op is not None:
            rec["open"] = op
        if cl is not None:
            rec["close"] = cl
        if ct is not None:
            rec["closetoday"] = ct
        if not rec:
            continue  # 空行视为未覆盖
        if code in ("DEFAULT", "默认", ""):
            cfg["default"].update(rec)
        else:
            cfg["by_root"][code] = rec
    return cfg


def fee_of(root, cfg, price=None, mult=1.0, lots=1.0):
    """返回 (开仓手续费, 平仓手续费)（总金额，已乘手数）"""
    d = cfg["default"]
    r = cfg["by_root"].get(root, {})
    mode = r.get("mode", d.get("mode", "per_lot"))
    fop = r.get("open", d.get("open", 0.0)) or 0.0
    fcl = r.get("close", d.get("close", fop)) or 0.0
    if fcl is None:
        fcl = fop
    if mode == "percent":
        base = (price or 0.0) * (mult or 1.0) * lots
        return base * fop, base * fcl
    # per_lot：元/手
    return fop * lots, fcl * lots


# =====================================================================
# 持仓台账
# =====================================================================
def init_positions(force=False):
    if os.path.exists(POS_PATH) and not force:
        return POS_PATH
    with open(POS_PATH, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerow(POS_COLS)
    return POS_PATH


def load_positions():
    """读取台账；保证列齐全；过滤掉空行与代码为空的行"""
    init_positions()
    try:
        df = pd.read_csv(POS_PATH, encoding="utf-8-sig", dtype=str)
    except Exception:
        df = pd.DataFrame(columns=POS_COLS)
    df = df.fillna("")
    for c in POS_COLS:
        if c not in df.columns:
            df[c] = ""
    df = df[POS_COLS]
    # 过滤：代码为空 / 代码为 DEMO 示例行
    def keep(r):
        code = str(r.get("代码", "")).strip()
        return code != "" and code.upper() != "DEMO"
    df = df[df.apply(keep, axis=1)].reset_index(drop=True)
    return df


def save_positions(df):
    df = df.copy()
    for c in POS_COLS:
        if c not in df.columns:
            df[c] = ""
    df[POS_COLS].to_csv(POS_PATH, index=False, encoding="utf-8-sig")


def _fmt(v, nd=1):
    if v is None:
        return "-"
    try:
        return f"{float(v):,.{nd}f}"
    except Exception:
        return str(v)


# =====================================================================
# M2 P3 金字塔加仓常量（与纪律「盈利仓最多加码一次/亏损不加仓」一致）
# =====================================================================
ADDON_MAX_LEGS = 2          # 同一品种最多腿数（含首仓）；V3.4 由 3 → 2（= fusion.add_max_lots）
ADDON_MIN_PROFIT_ATR = 1.0  # 浮盈 ≥ 1.0×ATR 才允许加仓（= fusion.add_thr_atr；V3.4 由 0.5 → 1.0）
ADDON_MARGIN_CAP = 0.40     # 加仓后总保证金 ≤ 权益 40%
ADDON_LOTS = 1              # 每次建议加仓手数（金字塔，固定 1 手）

# =====================================================================
# 锁仓机制常量（v2.1 生产默认，与 _fut_backtest.py --lock-* / 简报层保持同步）
# =====================================================================
LOCK_MAX = 1          # 最大并发锁仓品种数（0=不启用锁仓）
LOCK_TRIG = 0.8       # 锁仓触发：原仓浮亏 ≥ 0.8×ATR 才锁（锁早冻结本会自愈的浮亏=净伤害）
LOCK_UNLOCK = 0.0     # 解锁：组合浮亏回补至 ≥0 元（保本）即平锁仓腿，原仓恢复原止损/止盈
LOCK_MAX_TRIES = 2    # 同一持仓最多锁仓尝试次数（简报层不做精确计数，靠用户纪律，回测 74 周期 0 次耗尽）


def evaluate_position(row, tech, cfg, specs, today, specific=None, lock_quota_left=LOCK_MAX):
    """
    对单条持仓按最新行情刷新派生列并给出建议动作。
    tech: 该品种最新技术指标 dict（含 close/atr/ma20/ma60/ma250/adx/trend/action/entry/stop/target）
    specific: 该持仓实际合约（如 MA2701）行情 dict(ok, sym, close, atr, date)，可选；
              用于「具体合约月份」持仓按真实收盘价更新盈亏（见 Bug3 修复）。
    返回 dict（自动列的新值）
    """
    root = str(row.get("代码", "")).strip()
    name = str(row.get("品种", "")).strip() or root
    dirc = str(row.get("方向", "")).strip()
    # 兼容"多 / 做多 / 多头 / L / long"与"空 / 做空 / 空头 / S / short"；
    # 只要不含"空"字即按多头处理（"做多".startswith("多") 为假，不能只判前缀）
    is_long = ("空" not in dirc) and ("S" not in dirc.upper() or "L" in dirc.upper())
    lots = _num(row.get("手数")) or 0.0
    entry = _num(row.get("买入点位"))
    sp = specs.get(root, {})
    mult = _num(row.get("每手吨数"))
    if mult is None:
        mult = sp.get("multiplier", 1.0)
    mrate = sp.get("margin", 0.1)

    out = {c: "" for c in POS_AUTO_COLS}
    out["更新时间"] = today

    # ---- 已平仓（状态=已平/已平仓/平仓…）：停止一切提醒，仅保留已实现盈亏快照 ----
    # （Bug1 修复：用户止损/止盈后，只要标记「状态=已平」，系统不再天天弹止损提醒；
    #  若引擎日后再次给出同品种信号，简报会以「新信号」提示并给点位，实现再入场。）
    if is_closed(row):
        px = _num(row.get("平仓点位"))
        if px is None:
            px = float(tech["close"]) if (tech is not None and tech.get("close") is not None) else (entry or 0.0)
        if entry is not None and lots > 0:
            gross = (px - entry) * mult * lots if is_long else (entry - px) * mult * lots
            fo = _num(row.get("手续费开")); fc = _num(row.get("手续费平"))
            if fo is None or fc is None:
                _o, _c = fee_of(root, cfg, price=entry or px, mult=mult, lots=lots)
                fo = fo if fo is not None else _o
                fc = fc if fc is not None else _c
            out["毛盈亏"] = round(gross, 2)
            out["净浮盈"] = round(gross - fo - fc, 2)
            out["开仓手续费"] = round(fo, 2); out["平仓手续费"] = round(fc, 2)
            out["保证金占用"] = ""   # 已平仓，无保证金占用
        out["最新收盘日"] = str(tech.get("date", today)) if tech else today
        out["当前价"] = round(px, 2) if px is not None else ""
        out["参考止损"] = ""; out["参考止盈"] = ""
        out["建议动作"] = "✔已平仓（停止提醒）"
        out["动作说明"] = ("该持仓已标记「状态=已平」，系统不再推送止损/止盈/反手提醒；"
                           "如需重新交易，待引擎再次给出信号时，简报将以「新信号」提示并给点位。")
        return out

    if tech is None or tech.get("close") is None:
        out["建议动作"] = "⏳数据待补算"
        out["动作说明"] = "行情接口未取到该品种，持仓状态无法刷新，请次日复核"
        if entry is not None:
            # 行情缺失时无法按 ATR 推算参考止损，留空（不编造）
            out["参考止损"] = ""
        return out

    # 具体合约（非主力连续）行情处理（Bug3 修复）：
    #   优先用实际合约收盘价更新当前价/P&L；
    #   若与主力连续价基本一致（同月/近月，偏差≤1.5%）→ 视为等效主力，照常给建议；
    #   若明显偏离（跨月合约·额外交易）→ 抑制买卖建议与 ATR 点位，仅跟踪盈亏。
    use_sp = False; suppress = False; src_note = ""
    if specific is not None and specific.get("ok"):
        sp_c = float(specific["close"])
        use_sp = True
        main_c = float(tech["close"]) if (tech is not None and tech.get("close") is not None) else None
        if main_c and main_c > 0 and abs(sp_c - main_c) / main_c > 0.015:
            suppress = True
        atr = float(specific.get("atr") or (tech.get("atr") if tech else 0.0) or 0.0)
        src_note = f"｜数据来源：实际合约 {specific.get('sym','')} 收盘 {sp_c:,.0f}"
    elif specific is not None and not specific.get("ok"):
        src_note = f"｜⚠️实际合约 {specific.get('sym','')} 行情未取到，暂用主力连续价，P&L 可能偏差"
        atr = float(tech.get("atr") or 0.0)
    else:
        atr = float(tech.get("atr") or 0.0)
    price = float(specific["close"]) if use_sp else float(tech["close"])
    out["最新收盘日"] = (str(specific.get("date")) if (specific and specific.get("ok")) else str(tech.get("date", today)))
    out["当前价"] = round(price, 2)

    # 手续费：用户若在台账里单独填了则优先，否则取配置表
    fee_o = _num(row.get("手续费开"))
    fee_c = _num(row.get("手续费平"))
    if fee_o is None or fee_c is None:
        _o, _c = fee_of(root, cfg, price=entry or price, mult=mult, lots=lots)
        if fee_o is None:
            fee_o = _o
        if fee_c is None:
            fee_c = _c
    out["开仓手续费"] = round(fee_o, 2)
    out["平仓手续费"] = round(fee_c, 2)

    if entry is None or lots <= 0:
        out["建议动作"] = "⚠️记录不完整"
        out["动作说明"] = "买入点位或手数缺失，请在 _positions.csv 补全后重跑"
        return out

    # 毛盈亏 / 净浮盈
    if is_long:
        gross = (price - entry) * mult * lots
    else:
        gross = (entry - price) * mult * lots
    out["毛盈亏"] = round(gross, 2)
    out["净浮盈"] = round(gross - fee_o - fee_c, 2)

    # ---- v2.1 锁仓状态计算（锁仓腿 = 原仓反向、手数默认等量）----
    # 「锁仓腿价」非空 = 已锁仓（与「平仓点位非空 = 已平」同哲学）。
    hentry = _num(row.get("锁仓腿价"))
    hlots = _num(row.get("锁仓手数"))
    if hlots is None or hlots <= 0:
        hlots = lots
    hatr = _num(row.get("锁仓ATR"))
    if hatr is None or hatr <= 0:
        hatr = atr          # 留空用当前 ATR 近似（回测口径为锁仓时点 ATR）
    locked = hentry is not None
    hedge_pnl = 0.0
    if locked:
        # 锁仓腿浮盈：原仓多→锁仓腿空（(hentry-price)）；原仓空→锁仓腿多（(price-hentry)）
        hedge_pnl = (hentry - price) * mult * hlots if is_long else (price - hentry) * mult * hlots
        out["锁仓腿浮盈"] = round(hedge_pnl, 2)
        # 锁仓腿保证金并入占用（回测口径：锁仓后双倍保证金）
        out["保证金占用"] = round((entry or price) * mult * lots * mrate
                                  + hentry * mult * hlots * mrate, 2)
    comb_pnl = gross + hedge_pnl     # 组合浮盈亏（毛口径，不含手续费）
    out["锁仓状态"] = "已锁仓" if locked else "未锁仓"

    # 跨月额外合约：引擎信号按主力连续计算、不直接适用 → 抑制建议与 ATR 点位，仅跟踪盈亏
    if suppress:
        out["参考止损"] = ""
        out["参考止盈"] = ""
        if locked:
            # v2.1 盲区修复：已锁仓的跨月合约行不能静默——锁仓腿浮盈已算出，但解锁/双平
            # 判定依赖主力连续引擎信号（suppress 语义下不自动执行），提示人工按主力信号判定。
            out["建议动作"] = "ⓘ锁仓持有中·跨月合约仅跟踪"
            out["动作说明"] = (f"已锁仓（锁{'空' if is_long else '多'} {hlots:.0f}手 @{hentry:,.0f}，"
                              f"组合浮盈 {comb_pnl:,.0f} 元）。该持仓为跨月实际合约，引擎信号不直接适用，"
                              f"解锁/双平请对照**主力连续信号**人工判定：组合回补保本→平锁仓腿；"
                              f"主力信号反转/转弱→双平。" + src_note)
        else:
            out["建议动作"] = "ⓘ实际合约·仅跟踪盈亏"
            out["动作说明"] = ("该持仓为具体合约（与主力连续明显偏离），引擎信号按主力连续计算、不直接适用，"
                               "故不给出买卖建议与 ATR 止损止盈点位；仅按实际合约收盘价更新盈亏。" + src_note)
        return out

    # 用户自定义止损/止盈优先；留空则**以买入价为基准**按融合 ATR 口径推算
    # （不能用今日 target——那是"假设今天开仓"的点位，与既有持仓无关）
    # 融合策略：初始止损 2×ATR；吊灯移动止损（自持仓期极值收盘回撤 2×ATR）；**无固止盈**。
    try:
        from _fut_daily_brief import SLK as _SLK, TRK as _TRK
    except Exception:
        _SLK, _TRK = 2.0, 2.0
    pk_win = tech.get("pk_win"); tr_win = tech.get("tr_win")
    stop = _num(row.get("止损位"))
    targ = _num(row.get("止盈位"))   # 融合无固止盈：留空即无（由吊灯移动止损替代）
    if stop is None:
        if is_long:
            stop = max(entry - _SLK * atr, (pk_win - _TRK * atr)) if pk_win is not None else (entry - _SLK * atr)
        else:
            stop = min(entry + _SLK * atr, (tr_win + _TRK * atr)) if tr_win is not None else (entry + _SLK * atr)
    out["参考止损"] = round(stop, 2)
    out["参考止盈"] = (round(targ, 2) if targ is not None else "")

    # ---- 动作判定（优先级：止盈 > 止损 > 反手 > 转震荡 > 持有）----
    # v2.1：已锁仓 → 原仓冻结（止损/止盈/反手/转弱/时间止损全部跳过，与回测 Pass A 一致），
    #       仅判定锁仓三分支：信号反转→双平 / 趋势转弱→双平 / 组合回补保本→解锁。
    act = tech.get("action", "观望")          # 引擎今日给出的方向：做多 / 做空 / 观望
    engine = tech.get("engine", "融合")
    want_long = act == "做多"
    want_short = act == "做空"

    hit_tp = ((price >= targ) if is_long else (price <= targ)) if targ is not None else False
    hit_sl = (price <= stop) if is_long else (price >= stop)

    if locked:
        _dir_cn = "多" if is_long else "空"
        _hdir_cn = "空" if is_long else "多"
        if (is_long and want_short) or ((not is_long) and want_long):
            out["建议动作"] = "🔒🔄锁仓·信号反转·次日双平"
            out["动作说明"] = (f"已锁仓（原{_dir_cn} {lots:.0f}手 + 锁{_hdir_cn} {hlots:.0f}手 @{hentry:,.0f}），"
                              f"引擎{engine}今日转向{'做空' if is_long else '做多'}——信号反转，"
                              f"锁仓腿与原仓**同时平仓**，亏损定格（组合浮盈 {comb_pnl:,.0f} 元），不再等待")
        elif act == "观望":
            out["建议动作"] = "🔒⚠️锁仓·趋势转弱·次日双平"
            out["动作说明"] = (f"已锁仓，引擎今日转观望（ADX={tech.get('adx', 0):.0f}）——趋势转弱，"
                              f"锁仓腿与原仓同时平仓离场（组合浮盈 {comb_pnl:,.0f} 元）")
        elif comb_pnl >= -LOCK_UNLOCK * hatr * mult * lots:
            _unl_cn = "保本" if LOCK_UNLOCK <= 0 else f"-{LOCK_UNLOCK:g}×ATR"
            out["建议动作"] = "🔒✅锁仓·回补解锁·次日平锁仓腿"
            out["动作说明"] = (f"组合浮亏已回补至{_unl_cn}（组合浮盈 {comb_pnl:,.0f} 元）——"
                              f"平锁仓腿（{_hdir_cn} {hlots:.0f}手 @{hentry:,.0f}），"
                              f"原仓恢复原止损 {stop:,.0f} / 止盈 {targ:,.0f} 继续持有；平后请清空台账「锁仓腿价」")
        else:
            out["建议动作"] = "🔒🔒锁仓持有中"
            out["动作说明"] = (f"已锁仓（原{_dir_cn} {lots:.0f}手 + 锁{_hdir_cn} {hlots:.0f}手 @{hentry:,.0f}），"
                              f"净浮亏冻结（组合浮盈 {comb_pnl:,.0f} 元）；原仓止损/止盈冻结，"
                              f"等待①组合回补保本→解锁 或 ②信号反转/转弱→双平；勿手动平单边")
        return out

    if hit_tp:
        out["建议动作"] = "✅触及止盈·次日开盘平仓"
        out["动作说明"] = f"收盘 {price:,.0f} 已达止盈 {targ:,.0f}，按收盘价平仓了结（净浮盈约 {out['净浮盈']:,.0f} 元）"
    elif hit_sl:
        out["建议动作"] = "⛔触及止损·次日开盘平仓"
        out["动作说明"] = f"收盘 {price:,.0f} 已破止损 {stop:,.0f}，按规则离场，不摊平不扛单"
    elif (is_long and want_short) or ((not is_long) and want_long):
        out["建议动作"] = "🔄方向反转·平仓并反手"
        out["动作说明"] = (f"引擎{engine}今日转向{'做空' if is_long else '做多'}；"
                              f"先平{'多' if is_long else '空'}仓，再以收盘价 {price:,.0f} 反手"
                              f"（参考止损 {_fmt(tech.get('stop'))} / 止盈 {_fmt(tech.get('target'))}）")
    elif act == "观望":
        out["建议动作"] = "⚠️趋势转弱·减仓/离场"
        out["动作说明"] = f"该品种今日不满足{engine}入场条件（ADX={tech.get('adx', 0):.0f}），趋势转震荡，建议减仓或离场观望"
    else:
        trail = (price - _TRK * atr) if is_long else (price + _TRK * atr)
        out["建议动作"] = "🔒持有"
        _targ_disp = f"{targ:,.0f}" if targ is not None else "无固止盈（吊灯移动）"
        _be = (entry + 0.5 * atr) if is_long else (entry - 0.5 * atr)
        _be_hit = ((pk_win is not None and pk_win >= _be) if is_long
                   else (tr_win is not None and tr_win <= _be))
        _be_note = "；保本线已触发" if _be_hit else ""
        out["动作说明"] = (f"方向未变，继续持有；跟踪止损{'上' if is_long else '下'}移至 {trail:,.0f}"
                          f"（自{'最高' if is_long else '最低'}点回撤 {_TRK:g}×ATR 离场），"
                          f"止损 {stop:,.0f} / 止盈 {_targ_disp}{_be_note}")
        # v2.1 锁仓触发检测：原仓浮亏 ≥ 0.8×ATR 且信号仍同向 → 明日开盘反向等量锁仓。
        # 优先级低于止损/止盈/反手/转弱（当日触发前三者则不锁，与回测一致）；
        # 并发满（lock_quota_left=0）→ 放弃锁仓、原仓维持常规止损纪律。
        if LOCK_MAX > 0 and mult * lots > 0 and gross < 0:
            _loss_pts = (-gross) / (mult * lots)
            if _loss_pts >= LOCK_TRIG * atr:
                _hdir_cn = "空" if is_long else "多"
                if lock_quota_left > 0:
                    _hfee_o, _ = fee_of(root, cfg, price=price, mult=mult, lots=hlots)
                    out["建议动作"] = "🔐建议锁仓·次日开盘反向等量开仓"
                    out["动作说明"] = (f"浮亏 {_loss_pts:.1f}×ATR ≥ 触发阈值 {LOCK_TRIG:g}×ATR 且信号未反转——"
                                      f"按 v2.1 锁仓规则，明日开盘反向开{_hdir_cn} {hlots:.0f}手"
                                      f"（参考价 {price:,.0f}，开仓费约 {_hfee_o:,.0f} 元）；"
                                      f"锁仓后原仓止损/止盈**冻结**，等组合回补保本解锁或信号反转双平；"
                                      f"执行后请在台账回填「锁仓腿价」")
                else:
                    out["动作说明"] += (f"｜浮亏 {_loss_pts:.1f}×ATR 已达锁仓阈值，但并发锁仓已满 {LOCK_MAX} 个——"
                                       f"维持原止损纪律，不锁仓")

    return out


def refresh_positions(results, cfg=None, specs=None, today=None):
    """
    用最新行情刷新台账并回写 CSV。
    results: 技术指标 list（含 root/close/.../engine/action/stop/target）
    返回 (df, summary)
    """
    cfg = cfg or load_fee_config()
    if specs is None:
        try:
            from _fut_specs import FUT_SPECS
            specs = FUT_SPECS
        except Exception:
            specs = {}
    today = today or datetime.date.today().strftime("%Y-%m-%d")

    tech_map = {}
    for x in results:
        root = x.get("root")
        if root:
            tech_map[root] = x

    # 具体合约行情抓取（Bug3）：对「代码」含月份数字（非主力连续 0）的持仓，
    # 重建实际合约代码（如 MA2701）并尽量抓取真实收盘价；失败则标记 ok=False 由 evaluate 降级处理。
    specific_map = {}
    try:
        from _fut_daily_brief import fetch_series as _fetch
    except Exception:
        _fetch = None
    if _fetch is not None:
        n2r = _name_to_root(specs)
        _tmp = load_positions()
        for i in range(len(_tmp)):
            r0 = _tmp.iloc[i]
            code = str(r0.get("代码", "")).strip()
            digits = "".join(ch for ch in code if ch.isdigit())
            if not digits or digits == "0":
                continue
            root = norm_root(code)
            if not root:
                root = n2r.get(str(r0.get("品种", "")).strip(), "")
            if not root:
                continue
            sym = f"{root}{digits}"
            try:
                s = _fetch(sym)
                if s is not None and len(s) > 0:
                    last = s.iloc[-1]
                    specific_map[_row_key(r0)] = dict(
                        ok=True, sym=sym,
                        close=float(last["close"]),
                        atr=(float(last["atr"]) if "atr" in s.columns and pd.notna(last.get("atr")) else None),
                        date=str(last["date"])[:10])
                else:
                    specific_map[_row_key(r0)] = dict(ok=False, sym=sym, close=None, date=None)
            except Exception:
                specific_map[_row_key(r0)] = dict(ok=False, sym=sym, close=None, date=None)

    df = load_positions()
    if len(df) == 0:
        return df, {"count": 0, "net": 0.0, "gross": 0.0, "fee": 0.0,
                    "margin": 0.0, "alerts": [], "closed": 0}

    alerts = []
    # v2.1 锁仓并发额度：LOCK_MAX − 活跃已锁仓行数；建议锁仓后按行递减（当日建议数也不超额度）
    _quota = max(0, LOCK_MAX - int(sum(
        1 for _, rr in df.iterrows()
        if _num(rr.get("锁仓腿价")) is not None and not is_closed(rr)))) if len(df) else 0
    for i in range(len(df)):
        row = df.iloc[i]
        key = _row_key(row)
        if is_closed(row):
            # 已平仓：跳过提醒推送（建议动作已由 evaluate_position 写为 ✔已平仓）；仅刷新派生列
            tech = (tech_map.get(str(row.get("代码", "")).strip())
                    or tech_map.get(norm_root(str(row.get("代码", "")).strip()))
                    or tech_map.get(str(row.get("品种", "")).strip()))
            new = evaluate_position(row, tech, cfg, specs, today)
            for c in POS_AUTO_COLS:
                v = new[c]
                df.at[df.index[i], c] = "" if v == "" or v is None else str(v)
            continue
        root = str(row.get("代码", "")).strip()
        tech = tech_map.get(root) or tech_map.get(norm_root(root)) or tech_map.get(str(row.get("品种", "")).strip())
        spec_row = specific_map.get(key)
        new = evaluate_position(row, tech, cfg, specs, today, specific=spec_row,
                                lock_quota_left=_quota)
        for c in POS_AUTO_COLS:
            v = new[c]
            df.at[df.index[i], c] = "" if v == "" or v is None else str(v)
        if not (_num(row.get("锁仓腿价")) is not None) and str(new.get("建议动作", "")).startswith("🔐"):
            _quota = max(0, _quota - 1)   # 当日已建议一笔锁仓，额度递减（与回测并发上限一致）
        if str(new["建议动作"]).startswith(("⛔", "✅", "🔄", "⚠️", "🔐",
                                            "🔒🔄", "🔒⚠️", "🔒✅")):
            alerts.append(f"{root} {row.get('品种', '')}：{new['建议动作']}")

    # ---- M2 P3：加仓建议（把提示写回台账「动作说明」，并随 summary 暴露给简报第四章）----
    _addons = []
    try:
        _addons = addon_suggestions(df, tech_map, cfg, specs, today)
        for _ad in _addons:
            _m = (df["代码"].astype(str).str.strip() == _ad["code"]) & (~df.apply(is_closed, axis=1))
            for i in df.index[_m]:
                _cur = str(df.at[i, "动作说明"] or "")
                if "加仓观察" not in _cur:
                    df.at[i, "动作说明"] = _cur + "｜🔺符合加仓条件（详见第四章「加仓观察」）"
    except Exception:
        _addons = []

    save_positions(df)

    # 汇总仅统计「活跃（未平仓）」持仓；已平仓行仅留作历史记录在第四章展示
    active = df[~df.apply(is_closed, axis=1)] if len(df) else df
    gross = sum(_num(v) or 0 for v in active["毛盈亏"])
    fee_o = sum(_num(v) or 0 for v in active["开仓手续费"])
    fee_c = sum(_num(v) or 0 for v in active["平仓手续费"])
    margin = sum(_num(v) or 0 for v in active["保证金占用"])
    net = gross - fee_o - fee_c
    locks = int(sum(1 for _, rr in active.iterrows() if _num(rr.get("锁仓腿价")) is not None))
    summary = {"count": len(active), "gross": gross, "fee": fee_o + fee_c,
               "net": net, "margin": margin, "alerts": alerts,
               "closed": int(len(df) - len(active)), "locks": locks,
               "addons": _addons}      # M2 P3：加仓建议（供简报第四章渲染）
    return df, summary


def positions_md_table(df):
    """把台账渲染成 Markdown 表格（用于简报第四章）"""
    if df is None or len(df) == 0:
        return ["（当前无持仓记录。请在 `E:\\QH\\期货简报\\持仓情况和手续费\\_positions.csv` 中登记：品种 / 代码 / 方向 / 买入日期 / 买入点位 / 手数；"
                "止损位与止盈位可留空，留空则按引擎默认 ATR 点位计算。）"]
    head = ["品种", "代码", "方向", "买入日期", "买入点位", "手数", "当前价",
            "止损位", "止盈位", "毛盈亏", "手续费(开+平)", "净浮盈", "锁仓状态", "锁仓腿浮盈", "建议动作"]
    L = ["| " + " | ".join(head) + " |",
         "|" + "|".join(["---"] * len(head)) + "|"]
    for _, r in df.iterrows():
        gross = _num(r.get("毛盈亏")) or 0.0
        fee = (_num(r.get("开仓手续费")) or 0.0) + (_num(r.get("平仓手续费")) or 0.0)
        net = _num(r.get("净浮盈")) or 0.0
        color = "🔴" if net > 0 else ("🟢" if net < 0 else "⚪")
        # 止损/止盈：用户填了显示用户值，没填显示引擎参考值并加 * 标记
        s = _num(r.get("止损位")); s_r = _num(r.get("参考止损"))
        t = _num(r.get("止盈位")); t_r = _num(r.get("参考止盈"))
        s_disp = _fmt(s, 0) if s is not None else (f"{_fmt(s_r, 0)}*" if s_r is not None else "-")
        t_disp = _fmt(t, 0) if t is not None else (f"{_fmt(t_r, 0)}*" if t_r is not None else "-")
        # 锁仓：已锁行显示锁仓状态+锁仓腿浮盈；未锁显示 -
        _lk = _num(r.get("锁仓腿价")) is not None and not is_closed(r)
        _lk_disp = ("🔒已锁@" + _fmt(_num(r.get("锁仓腿价")), 0)) if _lk else "-"
        _lk_pnl = f"{_num(r.get('锁仓腿浮盈')):,.0f}" if (_lk and _num(r.get("锁仓腿浮盈")) is not None) else "-"
        L.append("| {v} {c} | {c} | {d} | {bd} | {e} | {l} | {p} | {s} | {t} | {g} | {f} | {n} | {lk} | {lkp} | {a} |".format(
            v=str(r.get("品种", "")).strip(), c=str(r.get("代码", "")).strip(),
            d=str(r.get("方向", "")).strip(), bd=str(r.get("买入日期", "")).strip(),
            e=_fmt(_num(r.get("买入点位")), 0), l=_fmt(_num(r.get("手数")), 0),
            p=_fmt(_num(r.get("当前价")), 0), s=s_disp, t=t_disp,
            g=f"{gross:,.0f}", f=f"{fee:,.0f}", n=f"{color} {net:,.0f}",
            lk=_lk_disp, lkp=_lk_pnl,
            a=str(r.get("建议动作", "")).strip()))
    L.append("")
    L.append("> `*` = 你未填写，显示的是**融合引擎**参考值（2×ATR 吊灯止损，以**买入价**为基准推算）；"
             "融合策略**无固止盈**——止盈位留空即靠 2×ATR 吊灯移动止损让利润奔跑，保本线 = 买入价 ± 0.5×ATR。")
    return L


def _addon_new_extreme(row, tech, is_long, price):
    """V3.4 阶梯加码 条件④：本根收盘是否创「入场以来」新高（多）/新低（空）。

    口径严格对齐 app/strategies/fusion_signal.py（= _fusion_v33.bt33）：
        多头  c[i] > max(h[entry_i : i])     空头  c[i] < min(l[entry_i : i])
    即与「自**买入日期**起、**排除本根**」的最高价/最低价比较（不是最高收盘）。
    数据源：tech 内嵌的调整后日线序列（seq_dates / seq_high / seq_low，与信号同源）。
    序列缺失、无买入日期、或内嵌窗口未覆盖买入日期 → 返回 (False, None, 说明)
    —— **保守不触发，不猜测放行**（PRD §16.4 第 2 条）。
    返回 (是否创新极值, 参考价, 说明)
    """
    dts = tech.get("seq_dates"); hs = tech.get("seq_high"); ls = tech.get("seq_low")
    if not dts or not hs or not ls or not (len(dts) == len(hs) == len(ls)):
        return False, None, "缺日线序列（未随 tech 传入），保守不触发"
    open_date = str(row.get("买入日期", "")).strip()[:10]
    if not open_date:
        return False, None, "台账缺「买入日期」，保守不触发"
    today = str(tech.get("date", "")).strip()[:10]
    if open_date < dts[0]:
        return False, None, f"内嵌序列({len(dts)}根)未覆盖买入日期 {open_date}，保守不触发"
    idx = [k for k in range(len(dts)) if dts[k] >= open_date and (not today or dts[k] < today)]
    if not idx:                      # 买入日 == 本根：线上口径退化为与本根极值比较 → 恒不触发
        return False, None, None
    ref = max(hs[k] for k in idx) if is_long else min(ls[k] for k in idx)
    return (price > ref) if is_long else (price < ref), ref, None


def addon_suggestions(df, tech_map, cfg=None, specs=None, today=None):
    """M2 P3：金字塔加仓建议（盈利顺势才加，亏损腿不加仓）。

    全部满足才建议：
      ① 未平仓、未锁仓；
      ② 浮盈 ≥ ADDON_MIN_PROFIT_ATR × ATR（以「每单位价格点数」衡量，亏损腿自动排除）；
      ③ 融合方向层仍顺势（tech['direction'] 与持仓方向一致，EMA20 未走平/未反转）；
      ④ **本根收盘创入场以来新高/新低**（V3.4 阶梯加码；原「回踩 EMA10 后收回」已删除）；
      ⑤ 同品种腿数 < ADDON_MAX_LEGS（V3.4：2）；
      ⑥ 加仓后总保证金 ≤ 权益 × ADDON_MARGIN_CAP。
    不引入「吊灯已推进到加码后均价之上」（结构性保本自 V3.4 停用）。
    动作：+ADDON_LOTS 手，加仓腿**独立 2×ATR 止损**，不与首仓摊平。
    """
    if df is None or len(df) == 0:
        return []
    cfg = cfg or load_fee_config()
    if specs is None:
        try:
            from _fut_specs import FUT_SPECS
            specs = FUT_SPECS
        except Exception:
            specs = {}
    try:
        from _fut_daily_brief import SLK as _SLK
    except Exception:
        _SLK = 2.0
    equity = float((cfg.get("account") or {}).get("资金") or 0.0)
    tech_map = tech_map or {}

    active = df[~df.apply(is_closed, axis=1)] if len(df) else df
    if len(active) == 0:
        return []
    margin_now = sum(_num(v) or 0 for v in active["保证金占用"])
    out = []
    for _, r in active.iterrows():
        code = str(r.get("代码", "")).strip()
        root = norm_root(code)
        tech = (tech_map.get(root) or tech_map.get(code)
                or tech_map.get(str(r.get("品种", "")).strip()))
        if not tech or tech.get("close") is None:
            continue
        if _num(r.get("锁仓腿价")) is not None:      # ① 已锁仓不加仓
            continue
        dirc = str(r.get("方向", "")).strip()
        is_long = ("空" not in dirc) and ("S" not in dirc.upper() or "L" in dirc.upper())
        sp = specs.get(root, {}) or {}
        mult = _num(r.get("每手吨数")) or sp.get("multiplier", 1.0)
        mrate = sp.get("margin", 0.1)
        lots = _num(r.get("手数")) or 0.0
        gross = _num(r.get("毛盈亏")) or 0.0
        atr = float(tech.get("atr") or 0.0)
        price = float(tech["close"])
        if lots <= 0 or not mult or mult <= 0 or atr <= 0:
            continue
        profit_pts = gross / (mult * lots)             # 每单位价格点数浮盈
        # ⑾（PRD16 对账 2026-09-22）：⚠ 本门槛**不含 lots**；线上引擎
        #    fusion_signal.walk_fusion_states 的门槛是 `lots × add_thr_atr × ATR`（含 lots）。
        #    当前 add_max_lots=2（只加一次、加码时 lots 恒为 1）二者等价；
        #    一旦放开 ADDON_MAX_LEGS ≥ 3，此处必须先改成 `lots * ADDON_MIN_PROFIT_ATR * atr` 对齐，
        #    否则日线侧门槛不会随手数抬升 → 与线上分叉。
        if profit_pts < ADDON_MIN_PROFIT_ATR * atr:    # ② 亏损/微利不加仓
            continue
        want = "多头" if is_long else "空头"
        if tech.get("direction") != want:              # ③ 方向层必须顺势
            continue
        # ④ V3.4 阶梯加码：本根收盘须创「入场以来」新高/新低
        #    （原「价格回踩快线 EMA10 后收回」已按 PRD §16.4 删除）
        new_ext, ext_ref, _seq_note = _addon_new_extreme(r, tech, is_long, price)
        if not new_ext:
            continue
        legs = int(sum(1 for _, rr in active.iterrows()
                       if norm_root(str(rr.get("代码", "")).strip()) == root))
        if legs >= ADDON_MAX_LEGS:                     # ⑤ 腿数封顶
            continue
        add_margin = price * mult * mrate * ADDON_LOTS
        if equity > 0 and (margin_now + add_margin) > ADDON_MARGIN_CAP * equity:   # ⑥ 保证金护栏
            continue
        stop = price - _SLK * atr if is_long else price + _SLK * atr
        _ext_cn = "新高" if is_long else "新低"
        _ref_txt = f"（突破 {ext_ref:,.0f}）" if ext_ref is not None else ""
        cond = (f"浮盈 {profit_pts/atr:.2f}×ATR ≥ {ADDON_MIN_PROFIT_ATR:g}×ATR；"
                f"方向层{tech.get('direction')}顺势；收盘 {price:,.0f} 创入场以来{_ext_cn}{_ref_txt}；"
                f"腿数 {legs}/{ADDON_MAX_LEGS}") + (
               f"；加仓后保证金 {(margin_now+add_margin)/equity*100:.0f}% ≤ {ADDON_MARGIN_CAP*100:.0f}%"
               if equity else "")
        out.append(dict(root=root, code=code,
                        name=str(r.get("品种", "")).strip() or root,
                        direction=("多" if is_long else "空"), lots=lots, legs=legs,
                        price=price, add_lots=ADDON_LOTS, stop=stop, atr=atr,
                        profit_pts=profit_pts, profit_atr=profit_pts / atr,
                        add_margin=add_margin, cond=cond))
    return out


def addon_md_table(suggestions):
    """M2 P3：加仓观察子表（简报第四章）"""
    if not suggestions:
        return []
    L = ["### 🔺 加仓观察（金字塔 · 仅盈利顺势仓，亏损腿不加仓）\n",
         "| 品种 | 合约 | 方向 | 已持手数 | 腿数 | 浮盈 | 建议加仓价 | 加仓手数 | 加仓腿止损 | 触发条件 |",
         "|------|------|------|----------|------|------|------------|----------|------------|----------|"]
    for s in suggestions:
        L.append(f"| {s['name']} {s['root']} | {s['code']} | {s['direction']} | {s['lots']:.0f} | "
                 f"{s['legs']}/{ADDON_MAX_LEGS} | {s['profit_atr']:.2f}×ATR | {s['price']:,.0f} | "
                 f"+{s['add_lots']} | {s['stop']:,.0f} | {s['cond']} |")
    L.append("")
    L.append(f"> 口径（V3.4 阶梯加码）：浮盈 ≥ **{ADDON_MIN_PROFIT_ATR:g}×ATR** + 方向层顺势 + **本根收盘创入场以来新高/新低** → 建议 **+{ADDON_LOTS} 手**；"
             f"加仓腿**独立 2×ATR 止损**，不与首仓摊平；同品种腿数封顶 **{ADDON_MAX_LEGS}**；"
             f"加仓后总保证金仍须 ≤ 权益 **{ADDON_MARGIN_CAP*100:.0f}%**；"
             "点位以当日收盘价界定、**次日开盘执行**。")
    return L


if __name__ == "__main__":
    init_fee_config()
    init_positions()
    print("持仓台账:", POS_PATH)
    print("手续费配置:", FEE_PATH)
    d = load_positions()
    print("当前持仓条数:", len(d))
