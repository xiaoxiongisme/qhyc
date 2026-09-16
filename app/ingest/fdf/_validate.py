import pandas as pd
from app.ingest.fdf import db_pg as D, symbols as SYM

spec = SYM.get("CZCE.FG")
freq = "hourly"
kq = spec["tq_cont"]

adj = D.load_bars(freq, "cont_adj", kq).sort_values("date")
cont = D.load_bars(freq, "continuous", kq).sort_values("date")
adj["date"] = pd.to_datetime(adj["date"])
cont["date"] = pd.to_datetime(cont["date"])

print("cont_adj:", len(adj), adj["date"].min(), "~", adj["date"].max())
gaps = adj["date"].diff().dropna().dt.total_seconds() / 3600.0
print("最大间隔(小时):", gaps.max(), " 中位数(小时):", gaps.median())

# 尾端对齐：forward 复权锚定当前合约，末根应与未复权主连末根收盘相等
print("末根 adj.close =", adj["close"].iloc[-1], " cont.close =", cont["close"].iloc[-1],
      " 差 =", round(adj["close"].iloc[-1] - cont["close"].iloc[-1], 4))

# 换月平滑性：取 2025-01-01 前后各 5 根，adjusted 应基本连续（无跳变），raw 主连可能有跳
w = (adj["date"] >= "2024-12-20") & (adj["date"] <= "2025-01-10")
seg = adj[w][["date", "close"]]
print("\n2025 元旦前后 adjusted close（应连续）:")
print(seg.to_string(index=False))

# 统计单根最大涨跌（复权后不应出现换月断层 > 5%）
chg = adj["close"].pct_change().abs()
print("\n复权后单根最大涨跌幅:", round(chg.max() * 100, 2), "%  (换月断层应已消除)")
