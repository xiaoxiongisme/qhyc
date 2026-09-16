# -*- coding: utf-8 -*-
"""天勤取数的公共部分：读账号、读当前品种、等数据稳定。

两个抓取脚本原先各自复制了一份一模一样的 get_auth，改一处就得记着改另一处。
这里合并成一份，顺便把「当前要取哪个品种」也统一从 config.json 读，
保证抓取的品种和回测的品种永远是同一个，不会出现取了 A 却回测 B 的情况。
"""
import sys, os, json, time
HERE = os.path.dirname(os.path.abspath(__file__))


def read_config():
    try:
        return json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8-sig"))
    except FileNotFoundError:
        print("\n找不到 config.json，它应该和本脚本在同一个文件夹里。\n")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"\nconfig.json 格式有误（第 {e.lineno} 行）：{e.msg}\n"
              f"  常见原因：多了或少了逗号、用了中文引号。\n")
        sys.exit(1)


def current_symbol():
    """取 config.json 里的 symbol，解析成规格 dict。"""
    import symbols as SYM
    cfg = read_config()
    sym = cfg.get("symbol")
    if not sym:
        print("\nconfig.json 里没有 symbol（回测品种）这一项。\n"
              '  在文件开头加一行，例如： "symbol": "CZCE.FG",\n'
              "  支持的品种清单：python symbols.py\n")
        sys.exit(1)
    try:
        return SYM.get(sym)
    except SYM.SymbolError as e:
        print(f"\n{e}\n")
        sys.exit(1)


def get_auth():
    """天勤账号从本项目 .env 的 TQSDK_PHONE / TQSDK_PASSWORD 读。"""
    try:
        from tqsdk import TqAuth
    except ImportError:
        raise RuntimeError("未安装 tqsdk，请先 pip install tqsdk")
    from app.core.config import get_settings
    st = get_settings()
    phone = str(getattr(st.env, "TQSDK_PHONE", "") or "").strip()
    pwd = str(getattr(st.env, "TQSDK_PASSWORD", "") or "").strip()
    if not phone or not pwd:
        raise RuntimeError("未配置 TQSDK_PHONE / TQSDK_PASSWORD（请写入 .env）")
    return TqAuth(phone, pwd)


def out_path(rel):
    """data/xxx.json -> 绝对路径，并保证 data 目录存在。"""
    p = os.path.join(HERE, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def wait_stable(api, serials, timeout=180, need=5, on_timeout=None):
    """等到所有订阅的 K 线根数不再变化。

    天勤是流式推送，刚订阅时根数会持续增长。这里等到连续 need 次
    wait_update 根数都没变才算取完，避免拿到半截数据就写文件。
    serials 传 dict 或单个序列都行。

    on_timeout：可选回调，签名 f(symbol)。某个合约在超时内仍不稳定（通常是
    天勤没保留该历史合约、wait_update 抛 TqTimeoutError）时调用它，并把该合约
    从待稳定集合里移除，其余合约继续等。不传则保持旧行为：超时直接整体返回。
    返回：超时仍未稳定的合约列表（为空表示全部稳定）。
    """
    if not isinstance(serials, dict):
        serials = {"_": serials}
    pending = dict(serials)
    dl = time.time() + timeout
    last = {s: -1 for s in serials}
    stab = {s: 0 for s in serials}
    while pending and time.time() < dl:
        try:
            got = api.wait_update(deadline=time.time() + 5)
        except Exception as e:
            # 单次 wait_update 抛异常（典型 TqTimeoutError）：把正在等的合约
            # 交给 on_timeout，不要因为一根弦断了全场陪葬。
            if on_timeout is not None:
                for s in list(pending):
                    on_timeout(s, type(e).__name__)
                    pending.pop(s, None)
            break
        if not got:
            # wait_update 返回 False = 当前没有新数据推送（数据已全部到达
            # 或暂时无更新）。这等价于「根数不变」，应当推进稳定计数，而
            # 不是 break——否则数据量少的具体合约在几秒内推完后立刻 stall，
            # 会被误判为超时。主连脚本数据量大不易触发此问题，具体合约会。
            pass

        done = True
        for s, k in list(pending.items()):
            try:
                n = int(k["close"].notna().sum())
            except Exception:
                n = k.shape[0]
            if n == last[s]:
                stab[s] += 1
            else:
                stab[s], last[s] = 0, n
            if stab[s] < need:
                done = False
            else:
                pending.pop(s, None)
        if done and not pending:
            break
    timed_out = [s for s in serials if s in pending]
    return timed_out


def to_rows(kl, require_oi=False):
    """天勤 K 线序列 -> 回测用的 [{date,open,high,low,close,volume,oi}]。"""
    import datetime as dt
    rows = []
    for r in kl.to_dict("records"):
        c = r.get("close")
        if not r.get("datetime") or c is None or c != c:
            continue
        ts = dt.datetime.fromtimestamp(r["datetime"] / 1e9)
        # 保留完整时分秒（小时/分钟线需要精确时间；日线时间恒为 00:00:00，不受影响）
        rows.append({"date": ts.strftime("%Y-%m-%d %H:%M:%S"),
                     "open": float(r["open"]), "high": float(r["high"]),
                     "low": float(r["low"]), "close": float(c),
                     "volume": int(r.get("volume") or 0),
                     "oi": int(r.get("close_oi") or 0)})
    rows = [r for r in rows if r["close"] > 0 and (not require_oi or r["oi"] > 0)]
    rows.sort(key=lambda x: x["date"])
    return rows
