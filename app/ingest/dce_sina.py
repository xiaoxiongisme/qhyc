# -*- coding: utf-8 -*-
"""DCE 会员成交持仓排名：新浪「单合约」通道（CL-20261110-11）。

为什么存在
==========
大商所官网 ``www.dce.com.cn`` 全站瑞数动态防护，``GET/POST`` 一律 412 挑战页；
``ak.futures_dce_position_rank`` 打同一个 ``batchDownload`` 接口，实测 ``BadZipFile``。
官方全量 zip 只能靠真浏览器执行 JS（``app.ingest.dce_scrapling``），而该通道在
生产容器、生产宿主机、本机 Windows 三路实测均不可用（未装 Scrapling / Chrome，
或 Playwright ``ERR_BLOCKED_BY_CLIENT``）。2026-11-09 DCE 只采到 V/Y 共 12 合约
240 行，其余 14 个品种整块缺失。

2026-11-10 用户拍板 ①：允许 ``ak.futures_hold_pos_sina`` **按单个明确合约**查询
作为 DCE 通道（覆盖 2026-09-20「官方源唯一主源」拍板中 DCE 的部分）。
实测：官网 11-06 的 87 个 DCE 合约在 11-09 全部可取；11-06 同日
M2701/A2701/I2701/V2701 的多单、空单、成交量三表及其增减与官网库表逐行一致。

口径（必须与官网 batchDownload 行同构）
=======================================
官网每个合约 20 行，是**按名次横向拼接三张榜单**：

=====  ==========================================================
rank   i = 1..20
member 多单持仓榜第 i 名会员简称
long   ``多单持仓`` 榜第 i 名的数值 / 比上交易增减
short  ``空单持仓`` 榜第 i 名的数值 / 比上交易增减
vol    ``成交量`` 榜第 i 名的数值
=====  ==========================================================

⚠ **绝不能按会员名合并三表**：旧 ``rank_position._fetch_contract`` 那样会写出
31 行/合约，且把空单榜第 i 名的量挂到多单榜第 i 名会员上。
⚠ **合约清单绝不能用** ``ak.match_main_contract('dce')`` 批量拉：2026-09-20
退役新浪通道的根因正是它返回非 DCE 品种，污染 393 行 ``src='sina_cot'``。
本模块只接受来自 **库内 DCE 真实合约集合** 的明确合约码（``candidate_contracts``）。

设计原则：**官方主源优先，新浪只补库内缺失的合约**（幂等、不删官网行、
``ON CONFLICT DO NOTHING``）；补数前对当日已有合约做同日逐字段对拍硬门，
有任何不一致就整天拒写。src 用新标记 ``sina:futures_hold_pos_sina``，
不复用旧污染标记 ``sina_cot``。
"""
from __future__ import annotations

import datetime as _dt
from collections import defaultdict

from sqlalchemy import text

from app.core.db import get_engine
from app.ingest.blocked_varieties import is_blocked_symbol

SRC_TAG = "sina:futures_hold_pos_sina"
VERSION = "v1.0"
EXCHANGE = "DCE"
TABLE = "l0_raw.member_position_rank"  # 全限定：public 同名是纯投影视图

_CAT_LONG = "多单持仓"
_CAT_SHORT = "空单持仓"
_CAT_VOL = "成交量"
_CATS = (_CAT_LONG, _CAT_SHORT, _CAT_VOL)
FIELDS = ("rank", "member", "long_pos", "short_pos", "long_chg", "short_chg", "vol_pos")

_INSERT = text(
    """
    INSERT INTO l0_raw.member_position_rank
        (trade_date, exchange, symbol, member, rank,
         long_pos, short_pos, long_chg, short_chg, vol_pos, src, version)
    VALUES (:trade_date, :exchange, :symbol, :member, :rank,
            :long_pos, :short_pos, :long_chg, :short_chg, :vol_pos, :src, :version)
    ON CONFLICT (trade_date, exchange, symbol, member, version) DO NOTHING
    """
)


def _num(v):
    """akshare 数值列可能是 float / NaN / 带千分位字符串。"""
    if v is None:
        return None
    try:
        f = float(str(v).replace(",", "").strip())
        return None if f != f else int(f)  # NaN
    except (TypeError, ValueError):
        return None


def _fetch_cat(ak, cat: str, contract: str, date_str: str):
    """取一张榜单，硬校验 20 行且名次连续 1..20。"""
    err = "unknown"
    for _ in range(2):
        try:
            df = ak.futures_hold_pos_sina(symbol=cat, contract=contract, date=date_str)
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {str(e)[:90]}"
            continue
        if df is None or len(df) != 20:
            err = f"shape={None if df is None else df.shape}"
            continue
        ranks = [_num(v) for v in df.iloc[:, 0].tolist()]
        if ranks != list(range(1, 21)):
            err = f"ranks={ranks[:4]}"
            continue
        return df, None
    return None, err


def fetch_contract_rows(contract: str, trade_date: _dt.date):
    """按名次横向拼三表 → 20 行 dict，与官网行逐字段同构。

    返回 ``(rows, None)`` 或 ``(None, 原因)``。``contract`` 必须是库内
    DCE 真实合约码（如 ``M2701``），不接受批量清单里的陌生品种。
    """
    import akshare as ak  # type: ignore

    date_str = trade_date.strftime("%Y%m%d")
    dfs = {}
    for cat in _CATS:
        df, err = _fetch_cat(ak, cat, contract, date_str)
        if df is None:
            return None, f"{cat}: {err}"
        dfs[cat] = df

    lo, sh, vo = dfs[_CAT_LONG], dfs[_CAT_SHORT], dfs[_CAT_VOL]
    rows = []
    for i in range(20):
        member = str(lo.iloc[i, 1]).strip()
        if not member:
            return None, f"第 {i + 1} 名会员简称为空"
        rec = {
            "trade_date": trade_date,
            "exchange": EXCHANGE,
            "symbol": contract,
            "member": member,
            "rank": i + 1,
            "long_pos": _num(lo.iloc[i, 2]),
            "long_chg": _num(lo.iloc[i, 3]),
            "short_pos": _num(sh.iloc[i, 2]),
            "short_chg": _num(sh.iloc[i, 3]),
            "vol_pos": _num(vo.iloc[i, 2]),
            "src": SRC_TAG,
            "version": VERSION,
        }
        # 榜单数值必须是非负整数；NaN/负数说明拿到错日或错合约的数据
        for k in ("long_pos", "short_pos", "vol_pos"):
            if rec[k] is None or rec[k] < 0:
                return None, f"第 {i + 1} 名 {k}={rec[k]} 非法"
        rows.append(rec)
    # 榜单按数值降序，三条都应是单调不增（允许并列）
    for k in ("long_pos", "short_pos", "vol_pos"):
        vals = [r[k] for r in rows]
        if any(vals[j] < vals[j + 1] for j in range(19)):
            return None, f"{k} 非降序（疑似错日/错合约数据）"
    return rows, None


def candidate_contracts(trade_date: _dt.date, window_days: int = 7) -> list[str]:
    """当日应有合约候选 = 前后 window_days 自然日内**库里真实出现过**的 DCE 合约。

    这是本模块最关键的安全边界：只从库内取，不向 akshare 要批量合约清单。
    窗口内多出的合约（当日尚未上市 / 已退市）在 fetch 时会被新浪拒绝并跳过。
    """
    lo = trade_date - _dt.timedelta(days=window_days)
    hi = trade_date + _dt.timedelta(days=window_days)
    with get_engine().connect() as conn:
        return [r[0] for r in conn.execute(text(
            f"SELECT DISTINCT symbol FROM {TABLE} "
            "WHERE exchange = :e AND trade_date BETWEEN :lo AND :hi ORDER BY 1"
        ), {"e": EXCHANGE, "lo": lo, "hi": hi}).all()]


def existing_rows(trade_date: _dt.date) -> dict[str, list[dict]]:
    """当日已有 DCE 行（按 symbol 分组，供同日对拍用）。"""
    with get_engine().connect() as conn:
        rs = conn.execute(text(
            "SELECT symbol, rank, member, long_pos, short_pos, long_chg, short_chg, vol_pos "
            f"FROM {TABLE} WHERE trade_date = :d AND exchange = :e "
            "ORDER BY symbol, rank"
        ), {"d": trade_date, "e": EXCHANGE}).all()
    out: dict[str, list[dict]] = defaultdict(list)
    for r in rs:
        out[r[0]].append({
            "rank": r[1], "member": r[2], "long_pos": r[3], "short_pos": r[4],
            "long_chg": r[5], "short_chg": r[6], "vol_pos": r[7],
        })
    return dict(out)


def compare_rows(off: list[dict], sina: list[dict]) -> list[tuple]:
    """逐行逐字段比对，返回差异 ``(rank, field, 官网值, 新浪值)``。"""
    if len(off) != 20 or len(sina) != 20:
        return [(0, "行数", len(off), len(sina))]
    diffs = []
    for a, b in zip(off, sina):
        for f in FIELDS:
            if a[f] != b[f]:
                diffs.append((a["rank"], f, a[f], b[f]))
    return diffs


def day_coverage(trade_date: _dt.date) -> dict:
    """主表聚合：行数 / 合约数 / 品种数（状态结论只认主表，不认事件表）。"""
    with get_engine().connect() as conn:
        r = conn.execute(text(
            "SELECT count(*), count(DISTINCT symbol), "
            "count(DISTINCT upper(regexp_replace(symbol, '[0-9]+$', ''))) "
            f"FROM {TABLE} WHERE trade_date = :d AND exchange = :e"
        ), {"d": trade_date, "e": EXCHANGE}).one()
    return {"rows": int(r[0]), "contracts": int(r[1]), "varieties": int(r[2])}


def expected_varieties() -> int:
    """近 30 天 DCE 每日品种数的**中位数**，作为「应有覆盖」基准。

    用中位数而非最大值：某天偶然多出一个品种（新品种上市）会把最大值抬到
    17，导致此后每天都误触发补齐。
    """
    with get_engine().connect() as conn:
        v = conn.execute(text(
            "SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY v) FROM ("
            "  SELECT trade_date, "
            "         count(DISTINCT upper(regexp_replace(symbol,'[0-9]+$','')))::float8 v "
            f" FROM {TABLE} WHERE exchange = :e AND trade_date >= now() - interval '30 days'"
            "  GROUP BY 1) t"
        ), {"e": EXCHANGE}).scalar()
    return int(round(float(v or 0)))


def insert_rows(rows: list[dict]) -> int:
    """幂等批量写入；过滤屏蔽品种；返回实际提交行数（DO NOTHING 的不计）。"""
    rows = [r for r in rows if not is_blocked_symbol(r["symbol"])]
    if not rows:
        return 0
    with get_engine().begin() as conn:
        conn.execute(_INSERT, rows)
    return len(rows)


def prev_trade_date(trade_date: _dt.date):
    """库内 DCE 该日之前最近的交易日（用于跨日增减自洽）。"""
    with get_engine().connect() as conn:
        return conn.execute(text(
            f"SELECT max(trade_date) FROM {TABLE} WHERE exchange = :e AND trade_date < :d"
        ), {"e": EXCHANGE, "d": trade_date}).scalar()


def verify_date_sensitive(contract: str, d1: _dt.date, d2: _dt.date):
    """门 0：同一合约两个不同交易日必须返回**不同**数据。

    防的是「新浪接口改为忽略 date、恒返回最新一天」——那会把 11-10 的
    持仓当成 11-09 写进历史（未来数据污染）。2026-11-10 实测 7 个合约
    × 4 个日期结果全部互异，且新浪 11-10 与官网 11-10 逐值吻合。
    """
    r1, e1 = fetch_contract_rows(contract, d1)
    r2, e2 = fetch_contract_rows(contract, d2)
    if r1 is None or r2 is None:
        return False, f"取数失败 d1={e1} d2={e2}"

    def sig(rs):
        return tuple((r["member"], r["long_pos"], r["short_pos"], r["vol_pos"])
                     for r in rs)

    if sig(r1) == sig(r2):
        return False, f"{contract} 在 {d1}/{d2} 返回完全相同 → date 可能已失效"
    return True, f"{contract} {d1}≠{d2}"


def cross_day_selfcheck(rows: list[dict], contract: str, prev_date) -> tuple[int, int]:
    """门 2：用**新浪自身**上一交易日数据，按会员名校验增减自洽。

    ``今日持仓 - 比上交易增减 == 上一交易日持仓``。不按名次校验（榜单名次
    每天会变，第 i 名未必是同一会员）。实测正常数据命中率 100%
    （M2701 19/19、V2701 20/20、PP2701 18/18、PG2701 17/17），低命中率可
    强力识别「错日 / 错合约 / 回落主力」。

    ⚠ 基准**不能取官网库表**：2026-11-10 首轮预演拿官网 11-06 作 prev，
    而官网 11-06 的 P2705/PP2701/PG2701 等 7 个远月合约本身就是残缺值
    （20 名合计仅为前一日 5%），把新浪 11-09 的正确数据全判成 0/9~0/13
    而拒写（见 CL-20261110-12）。改用同源 prev 日后完全自证。
    """
    if prev_date is None:
        return 0, 0
    prev_rows, _err = fetch_contract_rows(contract, prev_date)
    if prev_rows is None:
        return 0, 0  # prev 日不可取 → 样本不足，跳过该门（不拒写）
    prev = {r["member"]: r["long_pos"] for r in prev_rows}
    hit = tot = 0
    for r in rows:
        p = prev.get(r["member"])
        if p is None or r["long_chg"] is None:
            continue
        tot += 1
        if r["long_pos"] - r["long_chg"] == p:
            hit += 1
    return hit, tot


def backfill_missing(trade_date: _dt.date, *, window_days: int = 7,
                     verify_existing: bool = True, max_verify: int = 16,
                     apply: bool = True, min_selfcheck: float = 0.70) -> dict:
    """补当日 DCE **缺失合约**；返回结构化摘要（dry-run 时 ``apply=False``）。

    三道硬门（任何一道不过就拒写，宁可漏补不可错补）：

    门 0 ``verify_date_sensitive``
        同一合约两个交易日返回必须不同，防「date 失效 → 写入未来数据」。
    门 1 已有合约同日逐字段对拍（抽样，每品种 1 个）
        证明新浪该日数据日期正确。**容忍官网局部坏行**：官网 11-06
        实测有 11 个合约残缺/数值异常（P2705 合计 220849→11955 等），
        故只有 mismatch 率 > 50% 才拒写；行数不足 20 的官网行不参与判定。
    门 2 待补合约跨日增减自洽
        样本 ≥ 8 且命中率 < ``min_selfcheck`` 的合约单独拒写。

    只 INSERT 库内不存在的合约（``ON CONFLICT DO NOTHING``），**不删不改官网行**，
    因此官网残缺行不会被本次补数放大（另挂台账处理）。
    """
    have = existing_rows(trade_date)
    cands = candidate_contracts(trade_date, window_days)
    missing = [c for c in cands if c not in have]
    prev_d = prev_trade_date(trade_date)

    result = {
        "date": str(trade_date), "before": day_coverage(trade_date),
        "candidates": len(cands), "missing": len(missing), "prev_date": str(prev_d),
        "gates": {}, "verified": 0, "mismatch": [], "fetch_failed": [],
        "selfcheck_rejected": [], "rows_to_write": 0, "written": 0,
        "net_new": 0, "ok": True, "reason": "",
    }

    # --- 门 0：date 敏感性 ---
    probe = next((c for c in sorted(have) if len(have[c]) == 20), None) or (
        cands[0] if cands else None)
    if probe and prev_d:
        good, msg = verify_date_sensitive(probe, prev_d, trade_date)
        result["gates"]["date_sensitive"] = msg
        if not good:
            result["ok"] = False
            result["reason"] = f"门0 未过：{msg}"
            return result
    else:
        result["gates"]["date_sensitive"] = "skipped(无探针合约/无前交易日)"

    # --- 门 1：已有合约同日对拍（抽样）---
    if verify_existing:
        seen_variety, picks = set(), []
        for c in sorted(have):
            if len(have[c]) != 20:       # 官网残缺行不参与判定
                continue
            v = c.rstrip("0123456789")
            if v in seen_variety:
                continue
            seen_variety.add(v)
            picks.append(c)
            if len(picks) >= max_verify:
                break
        checked = 0
        for c in picks:
            sina, err = fetch_contract_rows(c, trade_date)
            if sina is None:
                result["fetch_failed"].append((c, err))
                continue
            checked += 1
            diffs = compare_rows(have[c], sina)
            if diffs:
                result["mismatch"].append((c, diffs[:3]))
            else:
                result["verified"] += 1
        result["gates"]["same_day"] = (
            f"PASS {result['verified']}/{checked}，mismatch {len(result['mismatch'])}")
        if checked and result["verified"] / checked < 0.50:
            result["ok"] = False
            result["reason"] = (f"门1 未过：同日对拍仅 {result['verified']}/{checked} 一致，"
                                f"疑似新浪日期错或口径漂移 {result['mismatch'][:2]}")
            return result

    # --- 门 2 + 只补缺失合约 ---
    to_write: list[dict] = []
    for c in missing:
        rows, err = fetch_contract_rows(c, trade_date)
        if rows is None:
            result["fetch_failed"].append((c, err))
            continue
        hit, tot = cross_day_selfcheck(rows, c, prev_d)
        if tot >= 8 and hit / tot < min_selfcheck:
            result["selfcheck_rejected"].append((c, f"{hit}/{tot}"))
            continue
        to_write.extend(rows)
    result["gates"]["selfcheck"] = (
        f"拒写 {len(result['selfcheck_rejected'])} 合约；"
        f"不可取 {len(result['fetch_failed'])} 合约")
    result["rows_to_write"] = len(to_write)

    if apply and to_write:
        result["written"] = insert_rows(to_write)
    result["after"] = day_coverage(trade_date) if apply else result["before"]
    result["net_new"] = result["after"]["rows"] - result["before"]["rows"]
    return result
