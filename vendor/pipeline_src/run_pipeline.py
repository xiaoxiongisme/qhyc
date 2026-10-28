# -*- coding: utf-8 -*-
"""
每日期货简报流水线编排器（M2 P1-P4 + M3 + M4）
=============================================
存在的唯一理由：**顺序不能错**。研究层 JSON 必须在两次 render 之间落盘，
否则简报第六章「研究层：多空辩论 × 量化验证」会静默消失。手工跑极易漏步。

用法：
  python run_pipeline.py --check        # 只做环境/依赖/参数/章节静态检查（不联网抓数）
  python run_pipeline.py --all          # 跑完整一天（①~⑤），最后校验七章
  python run_pipeline.py --all --skip-quant   # 研究层跳过回测（快）
  python run_pipeline.py --daily        # 【17:30 定时】多空简报（= --all，落盘供媒体发布参考）
  python run_pipeline.py --intraday     # 只跑盘中监控（持仓异动）
  python run_pipeline.py --from 3       # 从第 N 步开始（失败续跑）

步骤：
  ① _fut_fund_light.py                  轻基本面（需 qhyc PG）
  ② _fut_daily_brief.py --render        抓 49 品种行情 + 出简报
  ③ _fut_research.py --auto             研究层（只算当日有动作 + 持仓）
  ④ _fut_daily_brief.py --render        重渲染 → 第六章才出现
  ⑤ _fut_fundamentals_agent.py --report M4 Agent（置信列 / 独立报告）

统一归属（2026-09-22）：
  本脚本是「skill 生成」的唯一入口。流水线脚本已从 E:/Docker/qhyc/imports/SX/
  迁入本技能 scripts/pipeline/，不再依赖 qhyc 源码目录。
  ⚠️ qhyc-scheduler 的 fusion_scan（每 15 分钟直推）按用户决定保留不动，与本脚本并行。
"""
import os
import re
import sys
import glob
import json
import argparse
import datetime
import subprocess

PY = sys.executable
# 流水线脚本已迁入本技能目录（skill 侧自足，不再依赖 qhyc 源码树）
SX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pipeline")
BASE = r"E:/QH/期货简报"
PROC = os.path.join(BASE, "过程思考")
HOLD = os.path.join(BASE, "持仓情况和手续费")
BRIEF = os.path.join(BASE, "简报内容")
LOG_DIR = os.path.join(BASE, "过程思考", "logs")


REQUIRED_SCRIPTS = ["_fut_active_contract.py", "_fut_fund_light.py", "_fut_daily_brief.py",
                    "_fut_positions.py", "_fut_intraday_monitor.py", "_fut_research.py",
                    "_fut_fundamentals_agent.py"]

STEPS = [
    ("轻基本面（PG → JSON）", ["_fut_fund_light.py"]),
    ("抓行情 + 渲染简报", ["_fut_daily_brief.py", "--render"]),
    ("研究层（辩论 × 量化）", ["_fut_research.py", "--auto"]),
    ("重渲染并入研究层", ["_fut_daily_brief.py", "--render"]),
    ("M4 基本面 Agent", ["_fut_fundamentals_agent.py", "--report"]),
]

H2_EXPECT = ["一、总览表", "二、可操作多头", "三、可操作空头", "四、持仓品种记录",
             "五、轻基本面边际", "七、纪律提醒"]
# 第六章/加仓观察为条件渲染：研究层 JSON 存在 / 台账有持仓才出现


def _log(msg, fh=None):
    print(msg, flush=True)
    if fh:
        fh.write(str(msg) + "\n")


def today():
    return datetime.date.today().strftime("%Y-%m-%d")


def ds(d=None):
    return (d or today()).replace("-", "")


# ------------------------------ 检查项 ------------------------------

def check_env(fh=None):
    ok = True
    for d in (SX, PROC, HOLD, BRIEF):
        e = os.path.isdir(d)
        ok &= e
        _log(("  OK   " if e else "  FAIL ") + "目录 " + d, fh)
    for s in REQUIRED_SCRIPTS:
        p = os.path.join(SX, s)
        e = os.path.exists(p)
        ok &= e
        _log(("  OK   " if e else "  FAIL ") + "脚本 " + s, fh)
    return ok


def check_params(fh=None):
    """账户口径必须 15w / 4% / 8 仓；LH 必须解析为奇数月合约。"""
    ok = True
    try:
        sys.path.insert(0, SX)
        import _fut_daily_brief as B
        exp = {"ACCOUNT": 150000.0, "RISK": 0.04, "MAX_POS": 8}
        for k, v in exp.items():
            got = getattr(B, k, None)
            good = (got == v)
            ok &= good
            _log(("  OK   " if good else "  FAIL ") + f"{k} = {got}（期望 {v}）", fh)
        ok &= (os.path.realpath(B.BRIEF_DIR) == os.path.realpath(BRIEF))
        _log("  OK   BRIEF_DIR = " + B.BRIEF_DIR, fh)
    except Exception as e:
        ok = False
        _log("  FAIL 读取 _fut_daily_brief 参数: " + str(e), fh)
    try:
        import _fut_active_contract as A
        sym = A.active_contract("LH", datetime.date.today(), "heuristic")
        m = int(sym[-2:])
        good = (sym[:2] == "LH" and m % 2 == 1)
        ok &= good
        _log(("  OK   " if good else "  FAIL ") + f"LH 活跃合约 = {sym}（须奇数月）", fh)
    except Exception as e:
        ok = False
        _log("  FAIL 活跃合约解析: " + str(e), fh)
    check_recon_engine(fh)
    return ok


# ---------------------------------------------------------------
# PRD §16.5 对账回归断言（静态、不联网）
# 背景：日线实现 _fut_daily_brief 与线上引擎 app/strategies/fusion_signal.py 是两套独立手写实现，
#       2026-09-22 对账发现 2 处真实缺陷（详见 过程思考/PRD16_对账_日线实现_vs_线上引擎_20260922.md）：
#         D1  ADX 的 -DM 用 l.diff()（符号反）→ 门控通过率 53%→92%、方向翻转 41%
#         D2  ATR 用 ewm(span=14)（α=2/15）而非 Wilder α=1/14
#       本断言通过**源码文本特征**检测二者是否已被修正：未修 → WARN（不阻断）；
#       修正后若又退化 → 同样 WARN 提示，防止外部同步回退。
# ---------------------------------------------------------------
def check_recon_engine(fh=None):
    p = os.path.join(SX, "_fut_daily_brief.py")
    try:
        src = open(p, encoding="utf-8", errors="replace").read()
    except Exception as e:
        _log(f"  --  对账断言跳过（读不到源码）: {e}", fh)
        return
    # D1：缺陷特征是 adx() 内 `dn = l.diff()`；修正后应为 `shift(1)` 或 `lprev` 形式
    d1_bad = bool(re.search(r"up\s*=\s*h\.diff\(\)\s*;\s*dn\s*=\s*l\.diff\(\)", src))
    # D2：缺陷特征是 atr() 内 `ewm(span=n, adjust=False)`；修正后应为 alpha=1/n 或 Wilder 式
    m_atr = re.search(r"def atr\(df[^)]*\):(.{0,400}?)def ", src, re.S)
    seg = m_atr.group(1) if m_atr else ""
    d2_bad = ("ewm(span=n, adjust=False)" in seg) and ("1.0 / n" not in seg) and ("1.0/n" not in seg)
    _log(f"  {'WARN' if d1_bad else 'OK  '} D1 ADX -DM 符号"
         + ("（仍为 l.diff()，缺陷未修，见 PRD §16 对账报告）" if d1_bad else "（已修正）"), fh)
    _log(f"  {'WARN' if d2_bad else 'OK  '} D2 ATR 平滑系数"
         + ("（仍为 ewm(span=n)，缺陷未修）" if d2_bad else "（已修正）"), fh)


def check_json(fh=None):
    """当日过程数据是否齐备（决定简报会不会缺章）。"""
    d = ds()
    need = {"技术快照": f"_brief_tech_{d}.json", "轻基本面": f"_brief_fund_{d}.json"}
    opt = {"研究层": f"_brief_research_{d}.json", "M4 Agent": f"_fundamentals_agent_{d}.json"}
    for k, f in need.items():
        p = os.path.join(PROC, f)
        _log(("  OK   " if os.path.exists(p) else "  MISS ") + f"{k} {f}", fh)
    for k, f in opt.items():
        p = os.path.join(PROC, f)
        _log(("  OK   " if os.path.exists(p) else "  --   ") + f"{k} {f}（缺则不渲染对应章/列）", fh)


def verify_brief(fh=None):
    d = ds()
    p = os.path.join(BRIEF, f"国内期货多空策略简报_{d}.md")
    used_today = os.path.exists(p)
    if not used_today:
        # 当日未跑（如清晨跑 --check）→ 回退校验最近一份，避免误报 FAIL
        fs = sorted(glob.glob(os.path.join(BRIEF, "国内期货多空策略简报_*.md")))
        if not fs:
            _log("  MISS 当日简报不存在，且无历史简报可校验: " + p, fh)
            return False
        p = fs[-1]
        _log("  --   当日简报尚未生成，回退校验最近一份: " + os.path.basename(p), fh)
    s = open(p, encoding="utf-8", errors="replace").read()
    ok = True
    for h in H2_EXPECT:
        good = ("## " + h) in s
        ok &= good
        _log(("  OK   " if good else "  MISS ") + "章节 " + h, fh)
    has_res = os.path.exists(os.path.join(PROC, f"_brief_research_{d}.json"))
    if used_today and has_res:
        good = "研究层：多空辩论" in s
        ok &= good
        _log(("  OK   " if good else "  FAIL ") + "章节 六、研究层（JSON 已落盘，必须出现）", fh)
    elif used_today:
        _log("  --   研究层 JSON 未落盘 → 第六章不渲染（先跑 ③再跑 ④）", fh)
    else:
        _log("  --   非当日简报，跳过研究层落盘判据", fh)
    has_conf = "置信" in s
    _log(("  OK   " if has_conf else "  --   ") + "M4 置信列" + ("" if has_conf else "（Agent JSON 未落盘时不出现）"), fh)
    _log(f"  简报长度 {len(s)} 字符｜路径 {p}", fh)
    return ok


def _run(args, fh=None, timeout=1800):
    cmd = [PY] + args
    _log("$ " + " ".join(cmd), fh)
    try:
        r = subprocess.run(cmd, cwd=SX, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        _log("  TIMEOUT " + " ".join(args), fh)
        return False
    tail = (r.stdout or "").strip().splitlines()
    for line in tail[-8:]:
        _log("    " + line, fh)
    if r.returncode != 0:
        _log("  FAIL rc=%s" % r.returncode, fh)
        for line in (r.stderr or "").strip().splitlines()[-10:]:
            _log("    ! " + line, fh)
        return False
    return True




def is_trading_day(d=None):
    """交易日判定（周一~周五；法定节假日由 akshare 交易日历兜底，失败则退化为工作日）。"""
    d = d or datetime.date.today()
    if d.weekday() >= 5:
        return False
    try:
        # G4：期货官方日历 futures_rule（弃 A 股 tool_trade_date_hist_sina——期货作息
        # 与 A 股不同：中金所 09:15 开盘、股指/国债无夜盘、节前各所提前收盘）。
        from app.data import trade_calendar

        return trade_calendar.is_futures_trading_day(d)
    except Exception:
        # app 包不可用（如独立运行）时退化为工作日，不静默套用 A 股日历
        return d.weekday() < 5




# ------------------------------ 主流程 ------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="静态检查（不抓数）")
    ap.add_argument("--all", action="store_true", help="跑完整一天 ①~⑤")
    ap.add_argument("--daily", action="store_true", help="【17:30】多空简报（= --all）")
    ap.add_argument("--intraday", action="store_true", help="只跑盘中监控")
    ap.add_argument("--from", dest="start", type=int, default=1, help="从第 N 步开始（续跑）")
    ap.add_argument("--skip-quant", action="store_true", help="研究层跳过回测")
    a = ap.parse_args()

    if a.daily:
        a.all = True

    os.makedirs(LOG_DIR, exist_ok=True)
    logp = os.path.join(LOG_DIR, f"_pipeline_{ds()}.log")
    fh = open(logp, "a", encoding="utf-8")
    _log("=" * 70, fh)
    _log(f"[pipeline] {datetime.datetime.now():%Y-%m-%d %H:%M:%S}  args={sys.argv[1:]}", fh)

    if a.check or not (a.all or a.intraday):
        _log("\n[1] 环境与脚本", fh)
        ok1 = check_env(fh)
        _log("\n[2] 参数口径", fh)
        ok2 = check_params(fh)
        _log("\n[3] 当日过程数据", fh)
        check_json(fh)
        _log("\n[4] 简报章节", fh)
        ok4 = verify_brief(fh)
        _log("\n检查结论: " + ("PASS" if (ok1 and ok2 and ok4) else "有问题，见上"), fh)
        _log("日志: " + logp, fh)
        fh.close()
        sys.exit(0 if (ok1 and ok2 and ok4) else 1)


    if a.intraday:
        _log("\n[盘中监控]", fh)
        ok = _run(["_fut_intraday_monitor.py"], fh)
        fh.close()
        sys.exit(0 if ok else 1)

    if a.all:
        results = []
        for i, (name, args) in enumerate(STEPS, 1):
            if i < a.start:
                _log(f"\n[{i}] {name} —— 跳过（--from {a.start}）", fh)
                continue
            if i == 3 and a.skip_quant:
                args = args + ["--no-quant"]
            _log(f"\n[{i}] {name}", fh)
            ok = _run(args, fh)
            results.append((i, name, ok))
            if not ok and i in (1, 2):
                _log("  ⚠ 前置步骤失败，后续渲染可能缺章；可 --from %d 续跑" % i, fh)
        _log("\n[验收] 简报章节", fh)
        verify_brief(fh)
        _log("\n步骤汇总:", fh)
        for i, name, ok in results:
            _log(f"  {'OK  ' if ok else 'FAIL'} [{i}] {name}", fh)
        bad = [i for i, _, ok in results if not ok]
        _log("日志: " + logp, fh)
        fh.close()
        sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
