import pandas as pd
from app.ingest.fdf import db_pg as D, symbols as SYM, build_continuous as BC

spec = SYM.get("CZCE.FG")
freq = "hourly"
cont = D.load_bars(freq, "continuous", spec["tq_cont"])
print("cont", len(cont), cont["date"].min(), cont["date"].max())
frames = D.load_contract_frames(freq, spec["exchange"], spec["code"])
print("frames", len(frames))
oi = pd.DataFrame({s: f["oi"] for s, f in frames.items()}).sort_index()
print("oi rows", len(oi), "days", oi.index.floor("D").nunique())
oi_daily = oi.groupby(oi.index.floor("D")).last()
print("oi_daily shape", oi_daily.shape)
dom = BC.pick_dominant(oi_daily).dropna()
print("dom", len(dom), dom.index.min(), dom.index.max())
print("dom head", dom.head(2).to_dict())
print("dom tail", dom.tail(2).to_dict())
# 每年 dominant 数量
print("dom per year:", dom.index.year.value_counts().sort_index().to_dict())
