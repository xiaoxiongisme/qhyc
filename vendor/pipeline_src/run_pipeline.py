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
  python run_pipeline.py --signal       # 【交易日整点】期货简报信号 → pushplus 简短推送
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

# 期货简报信号推送（交易日整点）：9/10/11、13:30/14:00/15:00、21:00/22:00/23:00
SIGNAL_HOURS_AM = (9, 10, 11)
SIGNAL_HOURS_PM = (14, 15)          # 下午：14:00 / 15:00（13:30 单列）
SIGNAL_HOURS_NIGHT = (21, 22, 23)
SIGNAL_MINUTE = 0
# 13:30 单独处理（下午开盘首推）
SIGNAL_PM_OPEN = (13, 30)

# pushplus 推送（token 归属：环境变量 > 本流程数据配置 > qhyc .env 只读兜底）
PUSHPLUS_ENDPOINT = "https://www.pushplus.plus/send"
PUSHPLUS_TOKEN_FILES = [os.path.join(BASE, "配置", "pushplus_token.txt")]
QHYC_ENV = r"E:/Docker/qhyc/.env"


def _load_pushplus_token():
    """返回 (token, 来源标签)。三级解析，任一命中即用。"""
    t = (os.environ.get("PUSHPLUS_TOKEN") or "").strip()
    if t:
        return t, "env:PUSHPLUS_TOKEN"
    for p in PUSHPLUS_TOKEN_FILES:
        try:
            if os.path.exists(p):
                t = open(p, encoding="utf-8-sig").read().strip()
                if t:
                    return t, os.path.basename(p)
        except Exception:
            pass
    try:
        if os.path.exists(QHYC_ENV):
            for line in open(QHYC_ENV, encoding="utf-8-sig", errors="replace"):
                if line.strip().upper().startswith("PUSHPLUS_TOKEN"):
                    t = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if t:
                        return t, "qhyc/.env"
    except Exception:
        pass
    return "", "none"

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


# ------------------------------ 整点信号推送 ------------------------------

def in_signal_slot(now=None):
    """当前时刻是否属于「期货简报信号」应推送的整点（交易日由外部保证）。"""
    now = now or datetime.datetime.now()
    h, m = now.hour, now.minute
    if h in SIGNAL_HOURS_AM and m == SIGNAL_MINUTE:
        return True
    if h in SIGNAL_HOURS_NIGHT and m == SIGNAL_MINUTE:
        return True
    if (h, m) == SIGNAL_PM_OPEN:          # 13:30
        return True
    if h in SIGNAL_HOURS_PM and m == SIGNAL_MINUTE:
        return True
    return False


def is_trading_day(d=None):
    """交易日判定（周一~周五；法定节假日由 akshare 交易日历兜底，失败则退化为工作日）。"""
    d = d or datetime.date.today()
    if d.weekday() >= 5:
        return False
    try:
        sys.path.insert(0, SX)
        import akshare as ak
        cal = ak.tool_trade_date_hist_sina()
        days = set(str(x) for x in cal["trade_date"])
        return d.strftime("%Y-%m-%d") in days
    except Exception:
        return True


def signal_brief(fh=None, push=True):
    """算当日信号并（可选）推送简短文本。

    直接在本进程内 import 简报引擎复用 analyze()/fusion_signal()，**不落完整简报**，
    只产出精简信号摘要（供 pushplus 手机推送）。
    """
    _log("\n[信号快报] 抓行情 + 融合信号（in-process）", fh)
    if SX not in sys.path:
        sys.path.insert(0, SX)
    try:
        import pandas as pd
        import _fut_daily_brief as B
        from _fut_active_contract import active_contract
    except Exception as e:
        _log("  FAIL 加载简报引擎: " + str(e), fh)
        return False

    univ = pd.read_csv(B.UNIV, encoding="utf-8-sig")
    asof = datetime.date.today()
    sigs, pend = [], []
    n = len(univ)
    for i, (_, r) in enumerate(univ.iterrows(), 1):
        root0 = r["root"]
        C = active_contract(root0, asof, strategy="live_heuristic")
        m = B.fetch_series_contract(C, root0)
        if m is None:
            m = B.fetch_series(f"{root0}0")
        if m is None:
            pend.append(root0); continue
        try:
            met = B.analyze(m, root0)
            met["root"] = root0; met["name"] = r["name"]
            met["symbol"] = C
            if met.get("action") in ("做多", "做空"):
                sigs.append(met)
        except Exception as e:
            pend.append(f"{root0}({e})")

    sigs.sort(key=lambda x: -abs(x.get("conviction") or 0))
    longs = [s for s in sigs if s["action"] == "做多"]
    shorts = [s for s in sigs if s["action"] == "做空"]
    now = datetime.datetime.now()
    lines = [f"【期货信号快报】{now:%m-%d %H:%M}",
             f"多 {len(longs)}｜空 {len(shorts)}｜(49品种)"]
    top = sigs[:10]
    if not top:
        lines.append("今日无新开仓信号（全体观望）")
    for s in top:
        tag = "多" if s["action"] == "做多" else "空"
        px = s.get("entry") or s.get("close")
        st = s.get("stop"); be = s.get("breakeven")
        seg = f"{tag} {s.get('symbol')} {s.get('name','')} 入{px:,.0f} 损{st:,.0f}"
        if be:
            seg += f" 保{be:,.0f}"
        lines.append(seg)
    if pend:
        lines.append(f"(待恢复 {len(pend)})")
    text = "\n".join(lines)
    _log(text, fh)

    out = os.path.join(PROC, f"_brief_signal_{ds()}.json")
    try:
        json.dump({"time": now.strftime("%Y-%m-%d %H:%M"), "text": text,
                   "longs": len(longs), "shorts": len(shorts),
                   "signals": [{"symbol": s.get("symbol"), "name": s.get("name"),
                                "action": s.get("action"), "entry": s.get("entry"),
                                "stop": s.get("stop"), "breakeven": s.get("breakeven"),
                                "atr": s.get("atr"), "score": s.get("conviction"),
                                "reason": s.get("reason")} for s in sigs]},
                  open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        _log("  信号 JSON -> " + out, fh)
    except Exception as e:
        _log("  写 JSON 失败: " + str(e), fh)

    if push:
        _push_signal("期货信号快报", text, fh)
    return True


def _push_signal(title, text, fh=None):
    """推送 pushplus。token 解析顺序：环境变量 → 本流程数据配置 → qhyc .env（只读兜底）。

    失败一律只记日志、不抛异常（推送是旁路，不能影响主流程）。
    """
    import urllib.request
    import time
    token, src = _load_pushplus_token()
    if not token:
        _log("  ⚠ 未找到 pushplus token（env / 配置 / qhyc .env 均无）→ 仅落盘不推送", fh)
        return False
    payload = {"token": token, "title": title, "content": text, "template": "markdown"}
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                PUSHPLUS_ENDPOINT,
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = resp.read().decode("utf-8", "replace")
            try:
                code = json.loads(body).get("code")
            except Exception:
                code = None
            if code in (200, 0):
                _log("  已推送 pushplus（token 来源：" + src + "）", fh)
                return True
            _log(f"  pushplus 返回 code={code}｜{body[:160]}（第 {attempt + 1} 次）", fh)
        except Exception as e:
            _log(f"  pushplus 请求异常 {type(e).__name__}: {e}（第 {attempt + 1} 次）", fh)
        if attempt == 0:
            time.sleep(1.5)
    return False


# ------------------------------ 主流程 ------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="静态检查（不抓数）")
    ap.add_argument("--all", action="store_true", help="跑完整一天 ①~⑤")
    ap.add_argument("--daily", action="store_true", help="【17:30】多空简报（= --all）")
    ap.add_argument("--signal", action="store_true", help="【交易日整点】信号快报 → pushplus")
    ap.add_argument("--no-push", action="store_true", help="信号模式只算不推")
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

    if a.check or not (a.all or a.intraday or a.signal):
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

    if a.signal:
        if not in_signal_slot():
            _log(f"  非信号整点（{datetime.datetime.now():%H:%M}），跳过。"
                 f"允许: 9/10/11、13:30、14/15、21/22/23", fh)
            fh.close()
            sys.exit(0)
        if not is_trading_day():
            _log("  非交易日，跳过。", fh)
            fh.close()
            sys.exit(0)
        ok = signal_brief(fh, push=(not a.no_push))
        fh.close()
        sys.exit(0 if ok else 1)

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
