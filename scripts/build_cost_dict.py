# -*- coding: utf-8 -*-
"""从 D:\\学习资料 解析真实交易成本，产出 dim_trading_cost 种子 CSV（可审计、可重复执行）。

来源
----
1) ``20260311-手续费标准调整-fu&lu&sc.xlsx`` —— 交易所标准（费率与平今说明）
2) ``20260324期货品种参数汇总表.xls``          —— 品种中文名 → 代码 映射（并交叉校验乘数）

口径（用户 2026-10-02 拍板）
--------------------------
* 实际成本 = **交易所标准 + 券商加收 1 分/手**（0.01 元/手，固定值）
* 区分 **开仓 / 平昨 / 平今** 三动作
* 费率两类：**固定值**(元/手) 与 **百分比**(‰，按成交额)
* 交易所对**特定合约**给不同费率 → scope 限定（交割月 / 指定合约）
* 滚动：交易所调费靠新增行 + 闭合旧行；本脚本只产出"当期"费率并带 effective_from

输出
----
``db/seed/dim_trading_cost_seed.csv``；未匹配品种与无法解析的费率单独报告（不编造）。
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")

SRC_DIR = Path(r"D:\学习资料")
FEE_XLSX = SRC_DIR / "20260311-手续费标准调整-fu&lu&sc.xlsx"
PARAM_XLS = SRC_DIR / "20260324期货品种参数汇总表.xls"
OUT_CSV = Path(__file__).resolve().parents[1] / "db" / "seed" / "dim_trading_cost_seed.csv"
#: 库内字典导出的「品种中文名 → 品种码」（由 scripts/export_variety_names.py 从
#: dim_variety 导出）。作用是让**别名映射住在数据库里**而不是写死在脚本中 ——
#: 新增品种只需入库，映射自动生效，符合「字典表为唯一真源、代码不存配置」。
NAMES_CSV = Path(__file__).resolve().parents[1] / "db" / "seed" / "dim_variety_names.csv"
#: 「交易所官方品种名 → 品种码」别名种子（数据文件，非代码）。
#: 存在的理由：交易所官方名（如"天胶""胶板印刷纸""大豆1号"）与库内简称
#: （"天然橡胶""漂针浆""豆一"）不一致，而费率表用官方名、字典表用简称。
#: 长期应升级为库内 ``dim_variety_alias`` 表；当前先以种子文件承载，可审计。
ALIAS_CSV = Path(__file__).resolve().parents[1] / "db" / "seed" / "dim_variety_alias_seed.csv"

#: 券商加收（用户当前约定：+1 分/手）
BROKER_MARKUP = 0.01
#: 来源费率文件的生效日（调整通知日期）
EFFECTIVE_FROM = "2026-03-11"

#: 交易所名（费率表内中文）→ dim_exchange.exchange_code
EX_MAP = {
    "郑商所": "CZCE", "大商所": "DCE", "上期所": "SHFE",
    "广期所": "GFEX", "中金所": "CFFEX", "能源交易所": "INE",
}

COLS = ["exchange", "variety_code", "instrument_kind", "action", "scope_kind",
        "scope_months", "scope_contracts", "fee_type", "fee_value",
        "broker_markup_type", "broker_markup_value", "effective_from", "source", "note"]


# ---------------------------------------------------------------------------
# 1. 品种中文名 → 代码（来自参数汇总表；乘数用于交叉校验）
# ---------------------------------------------------------------------------
def load_name_to_code() -> dict[str, tuple[str, float | None]]:
    df = pd.read_excel(PARAM_XLS, sheet_name="期货品种", header=None)
    df[0] = df[0].ffill()
    out: dict[str, tuple[str, float | None]] = {}
    for _, r in df.iterrows():
        name, code, mult = r[1], r[3], r[4]
        if pd.isna(name) or pd.isna(code):
            continue
        name = str(name).strip()
        code = str(code).strip().upper()
        if not re.fullmatch(r"[A-Z]{1,3}", code):
            continue
        m: float | None = None
        if not pd.isna(mult):
            m2 = re.search(r"[\d.]+", str(mult))
            if m2:
                try:
                    m = float(m2.group())
                except ValueError:
                    m = None
        out.setdefault(name, (code, m))
        # 参数汇总表里同一品种常按合约区间拆多行，名字带括号：
        #   "白银（2603-2703合约）" / "白银（其他合约）" / "铝（2603-2702合约）"
        # 费率表用的是裸名，故额外登记「去括号」后的名字。
        bare = re.sub(r"[（(][^）)]*[）)]", "", name).strip()
        if bare and bare != name:
            out.setdefault(bare, (code, m))
        # 去掉末尾的合约后缀，如 "焦煤2601" → "焦煤"
        b2 = re.sub(r"[\d、,，\s合约&]+$", "", bare).strip()
        if b2 and b2 != bare:
            out.setdefault(b2, (code, m))
    return out


def load_symbols_json_names() -> dict[str, tuple[str, float | None]]:
    """仓内权威品种表 ``app/ingest/fdf/symbols.json`` 的中文名 → (品种码, 乘数)。

    补充参数汇总表未覆盖的品种（螺纹钢/天然橡胶/漂针浆等）。该文件是仓内
    multiplier/tick 的权威源（见 scripts/seed_variety_specs.py），故优先采信其乘数。
    """
    p = Path(__file__).resolve().parents[1] / "app" / "ingest" / "fdf" / "symbols.json"
    if not p.exists():
        return {}
    import json
    with p.open(encoding="utf-8-sig") as f:
        raw = json.load(f)
    out: dict[str, tuple[str, float | None]] = {}
    for k, v in raw.items():
        if k.startswith("_") or not isinstance(v, dict):
            continue
        code = k.split(".", 1)[1].upper()
        nm = (v.get("name") or "").strip()
        if not nm:
            continue
        mult = v.get("multiplier")
        out.setdefault(nm, (code, float(mult) if isinstance(mult, (int, float)) else None))
        # "CZCE.FG" 形态同时登记裸品种名，便于按品种码反查
        out.setdefault(code, (code, float(mult) if isinstance(mult, (int, float)) else None))
    return out


def load_db_names() -> dict[str, tuple[str, float | None]]:
    """读库内导出的 品种中文名 → (品种码, 乘数)，作为参数汇总表的补充。

    别名映射的**真源是 dim_variety**，此处只消费导出物，避免把映射写死在代码里。
    乘数一并带出用于交叉校验（库内值应与参数汇总表一致）。
    """
    if not NAMES_CSV.exists():
        return {}
    out: dict[str, tuple[str, float | None]] = {}
    with NAMES_CSV.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            nm = (row.get("variety_name") or "").strip()
            code = (row.get("variety_code") or "").strip().upper()
            if not nm or not code:
                continue
            mult = row.get("multiplier") or ""
            try:
                m = float(mult) if mult else None
            except ValueError:
                m = None
            out.setdefault(nm, (code, m))
    return out


def load_alias_seed() -> dict[str, tuple[str, float | None]]:
    """读「交易所官方名 → 品种码」别名种子（数据文件）。"""
    if not ALIAS_CSV.exists():
        return {}
    out: dict[str, tuple[str, float | None]] = {}
    with ALIAS_CSV.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            al = (row.get("alias") or "").strip()
            code = (row.get("variety_code") or "").strip().upper()
            if al and code:
                out.setdefault(al, (code, None))
    return out


# ---------------------------------------------------------------------------
# 2. 合约范围解析
# ----------------------------------------------------------------------------
# 样例：
#   "白银6、12合约&2601、2603、2605合约"      → months=[6,12] contracts=[2601,2603,2605]
#   "燃料油1、5、9合约"                      → months=[1,5,9]
#   "碳酸锂2601、2602、…、2702"               → contracts=[2601..2702]
#   "铂2606"                                → contracts=[2606]
#   "螺纹钢1、5、10合约&2602、2603、2604合约"   → months=[1,5,10] + contracts
# ---------------------------------------------------------------------------
def parse_scope(text: str) -> tuple[str, list[int], list[str]]:
    t = str(text or "")
    months: list[int] = []
    for grp in re.findall(r"([\d、,，\s]+?)\s*(?:合约|月)", t):
        for n in re.findall(r"\d{1,2}", grp):
            v = int(n)
            if 1 <= v <= 12 and v not in months:
                months.append(v)
    contracts: list[str] = []
    for n in re.findall(r"(?<!\d)(\d{4})(?!\d)", t):
        if n not in contracts:
            contracts.append(n)
    if contracts:
        return "CONTRACTS", sorted(months), contracts
    if months:
        return "MONTHS", sorted(months), []
    return "ALL", [], []


# ---------------------------------------------------------------------------
# 3. 费率值解析
# ----------------------------------------------------------------------------
def parse_fee(raw: str) -> tuple[str, float, str]:
    """→ (fee_type, value, note)；无法识别返回 ('FREE', 0, 原因) 以免编造。"""
    s = str(raw or "").strip()
    if not s:
        return "FREE", 0.0, "来源未给费率"
    if "免" in s:
        return "FREE", 0.0, s
    is_pct = ("%" in s) or ("‰" in s)
    s2 = s.replace("%%", "").replace("‰", "").replace("交易", "").strip()
    if "隔日开平" in s2:
        m = re.search(r"([\d.]+)", s2)
        if m:
            return ("PCT" if is_pct else "FIXED"), float(m.group(1)), "隔日开平=开仓+平昨"
    if "当日开平" in s2:
        m = re.search(r"([\d.]+)", s2)
        if m:
            return (("PCT" if is_pct else "FIXED"), float(m.group(1)),
                    "当日开平（当日开仓+平今）；是否也适用于当日开仓需核对来源")
    m = re.fullmatch(r"([\d.]+)\s*%?", s2)
    if m:
        return ("PCT" if is_pct else "FIXED"), float(m.group(1)), ""
    return "FREE", 0.0, f"未识别费率原文：{s}"


#: 费率表里「平今」说明的前缀（"平今仓20" / "平今20" / "平今免"）
_CLOSE_TODAY_RE = re.compile(r"^平今(?:仓)?\s*(.+)$")


def parse_close_today(raw: str) -> tuple[str, float, str] | None:
    """解析「平今」续行说明；非平今说明返回 None。"""
    s = str(raw or "").strip()
    m = _CLOSE_TODAY_RE.match(s)
    if not m:
        return None
    return parse_fee(m.group(1))


# ---------------------------------------------------------------------------
# 4. 主解析
# ---------------------------------------------------------------------------
def build_rows() -> tuple[list[dict], list[str], list[str]]:
    name2code = load_name_to_code()
    db_names = load_db_names()
    sj_names = load_symbols_json_names()
    alias_seed = load_alias_seed()
    # 库内字典 + 仓内 symbols.json + 别名种子 补充（别名的真源是字典表，不是本脚本）
    conflicts: list[str] = []
    for src_name, src in (("dim_variety", db_names), ("symbols.json", sj_names),
                          ("alias_seed", alias_seed)):
        for nm, (code, m) in src.items():
            if nm in name2code and name2code[nm][0] != code:
                conflicts.append(
                    f"名称冲突 {nm!r}：参数表={name2code[nm][0]} vs {src_name}={code}（跳过该别名）")
                continue
            name2code.setdefault(nm, (code, m))
    # 库内乘数与参数表乘数交叉校验（不一致 → 报告，不静默）
    mult_conflict: list[str] = []
    for nm, (code, m_param) in list(name2code.items()):
        if nm in db_names and m_param is not None and db_names[nm][1] is not None:
            if abs(m_param - db_names[nm][1]) > 1e-9:
                s = f"乘数不一致 {code}（{nm}）：参数表={m_param} vs 库内dim_variety={db_names[nm][1]}"
                if s not in mult_conflict:
                    mult_conflict.append(s)
    problems_all = conflicts + mult_conflict

    df = pd.read_excel(FEE_XLSX, sheet_name="Sheet1", header=None)

    rows: list[dict] = []
    problems: list[str] = []
    unmapped: set[str] = set()
    ex = ""
    cur: dict | None = None

    def flush(cont_text: str | None = None) -> None:
        if cur is None:
            return
        # 续行文本只有「平今…」才是平今费率；其余（如「隔日开平…」）仅作备注
        parsed_ct = parse_close_today(cont_text) if cont_text else None
        close_today = cont_text if parsed_ct else None
        ftype, fval, fnote = parse_fee(cur["std"])
        ex_c = EX_MAP.get(cur["ex"], "")
        if not ex_c:
            unmapped.add(f"交易所未映射:{cur['ex']}")
            return
        kind = "OPTION" if "期权" in cur["name"] else "FUTURE"
        base = re.sub(r"期权$", "", cur["name"]).strip()
        # 迭代剥离末尾合约后缀："白银2602、白银2604" → "白银2602" → "白银"
        # （单次 re.sub 只锚定末尾，"白银2602、白银" 仍带数字，故需循环）
        for _ in range(4):
            b2 = re.sub(r"[\d、,，\s合约&]+$", "", base).strip()
            if b2 == base:
                break
            base = b2
        mapped = name2code.get(cur["name"]) or name2code.get(base)
        if mapped is None:
            unmapped.add(f"{cur['ex']}/{cur['name']}")
            return
        code = mapped[0]
        scope_kind, months, contracts = parse_scope(cur["name"])
        c4 = [f"{code}{n}" for n in contracts]
        has_fee = ftype != "FREE"
        common = dict(
            exchange=ex_c, variety_code=code, instrument_kind=kind,
            scope_kind=scope_kind,
            scope_months=";".join(str(m) for m in months),
            scope_contracts=";".join(c4),
            broker_markup_type="FIXED" if (kind == "FUTURE" and has_fee) else "NONE",
            broker_markup_value=BROKER_MARKUP if (kind == "FUTURE" and has_fee) else 0.0,
            effective_from=EFFECTIVE_FROM, source="exchange_notice_20260311",
        )
        for act in ("OPEN", "CLOSE_YEST"):
            rows.append({**common, "action": act, "fee_type": ftype,
                         "fee_value": fval, "note": fnote or cur["std"]})
        if close_today:
            ct, cv, cn = parsed_ct
            if "未识别" in cn:
                problems.append(f"{code} 平今无法识别：{close_today}")
            rows.append({**common, "action": "CLOSE_TODAY", "fee_type": ct,
                         "fee_value": cv, "note": cn or close_today})
        elif "隔日开平" in str(cur["std"]):
            problems.append(
                f"{code}（{cur['name']}）来源仅给「隔日开平」，**平今费率未知** → 未落库（不编造）")
        elif cont_text:
            fnote = f"{fnote}；附注：{cont_text}" if fnote else f"附注：{cont_text}"

    for _, r in df.iterrows():
        if not pd.isna(r[0]):
            ex = str(r[0]).strip()
        std = "" if pd.isna(r[3]) else str(r[3]).strip()
        if pd.isna(r[1]):
            if cur is not None and std:
                flush(cont_text=std)
                cur = None
            continue
        name = str(r[1]).strip()
        if name in ("交易所标准", "品种"):
            continue
        flush()
        cur = {"ex": ex, "name": name, "std": std}
    flush()

    return rows, sorted(unmapped), problems_all + problems


def main() -> int:
    rows, unmapped, problems = build_rows()
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in COLS})

    from collections import Counter
    print(f"已写出 {OUT_CSV}（{len(rows)} 行）")
    print("\n动作分布:", dict(Counter(r["action"] for r in rows)))
    print("费率类型:", dict(Counter(r["fee_type"] for r in rows)))
    print("范围限定:", dict(Counter(r["scope_kind"] for r in rows)))
    print("品种数:", len({r["variety_code"] for r in rows}),
          "  含期权:", len({r["variety_code"] for r in rows if r["instrument_kind"] == "OPTION"}))
    print("交易所:", dict(Counter(r["exchange"] for r in rows)))
    if unmapped:
        print(f"\n⚠ 未能映射到品种码 {len(unmapped)} 个（已跳过，未编造）：")
        for u in unmapped:
            print("   ", u)
    if problems:
        print(f"\n⚠ 口径缺口 {len(problems)} 处：")
        for p in problems:
            print("   ", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
