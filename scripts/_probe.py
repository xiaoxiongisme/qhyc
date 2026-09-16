import sys, traceback
try:
    import akshare as ak
    fns = [f for f in dir(ak) if "minute" in f.lower()]
    print("AK_minute_fns=", fns)
    # 试具体合约分钟线（非主连）
    for fn in ("futures_zh_minute", "futures_zh_minute_sina"):
        if not hasattr(ak, fn):
            continue
        for sym in ("FG2609", "FG2610"):
            try:
                df = getattr(ak, fn)(symbol=sym, period="60")
                print(f"{fn}({sym},60) -> n={len(df)} cols={list(df.columns)[:6]}")
                if len(df):
                    print("   head=", df.head(1).to_dict("records"))
            except Exception as e:
                print(f"{fn}({sym},60) ERR=", repr(e)[:160])
except Exception as e:
    print("ERR=", repr(e)[:300])
    traceback.print_exc()
