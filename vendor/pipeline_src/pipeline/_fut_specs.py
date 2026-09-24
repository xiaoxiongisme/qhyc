# 国内商品期货规格表（用于保证金过滤）
# multiplier: 每手交易单位（吨 / 克 / 千克 / 桶）
# margin: 交易所保证金比例（近似常态，实盘以期货公司通知为准）
# exch: SHFE 上期所 / DCE 大商所 / CZCE 郑商所 / INE 能源中心 / GFEX 广期所
# 中金所 CFFEX（股指期货 IF/IC/IH/IM/TF/TS/T）按要求剔除，不列入

FUT_SPECS = {
    # ===== 上期所 SHFE =====
    "RB": dict(name="螺纹钢", multiplier=10,   margin=0.09, exch="SHFE"),
    "HC": dict(name="热卷",   multiplier=10,   margin=0.09, exch="SHFE"),
    "CU": dict(name="沪铜",   multiplier=5,    margin=0.09, exch="SHFE"),
    "AL": dict(name="沪铝",   multiplier=5,    margin=0.09, exch="SHFE"),
    "ZN": dict(name="沪锌",   multiplier=5,    margin=0.09, exch="SHFE"),
    "NI": dict(name="沪镍",   multiplier=1,    margin=0.12, exch="SHFE"),
    "SN": dict(name="沪锡",   multiplier=1,    margin=0.12, exch="SHFE"),
    "PB": dict(name="沪铅",   multiplier=5,    margin=0.09, exch="SHFE"),
    "AU": dict(name="沪金",   multiplier=1000, margin=0.08, exch="SHFE"),  # 克
    "AG": dict(name="沪银",   multiplier=15,   margin=0.11, exch="SHFE"),  # 千克
    "RU": dict(name="橡胶",   multiplier=10,   margin=0.09, exch="SHFE"),
    "FU": dict(name="燃油",   multiplier=10,   margin=0.10, exch="SHFE"),
    "BU": dict(name="沥青",   multiplier=10,   margin=0.10, exch="SHFE"),
    "SP": dict(name="纸浆",   multiplier=10,   margin=0.09, exch="SHFE"),
    "SS": dict(name="不锈钢", multiplier=5,    margin=0.09, exch="SHFE"),
    # ===== 大商所 DCE =====
    "I":  dict(name="铁矿石", multiplier=100,  margin=0.11, exch="DCE"),
    "JM": dict(name="焦煤",   multiplier=60,   margin=0.12, exch="DCE"),
    "J":  dict(name="焦炭",   multiplier=100,  margin=0.12, exch="DCE"),
    "M":  dict(name="豆粕",   multiplier=10,   margin=0.08, exch="DCE"),
    "Y":  dict(name="豆油",   multiplier=10,   margin=0.08, exch="DCE"),
    "P":  dict(name="棕榈油", multiplier=10,   margin=0.08, exch="DCE"),
    "C":  dict(name="玉米",   multiplier=10,   margin=0.08, exch="DCE"),
    "JD": dict(name="鸡蛋",   multiplier=10,   margin=0.08, exch="DCE"),
    "LH": dict(name="生猪",   multiplier=16,   margin=0.08, exch="DCE"),
    "V":  dict(name="PVC",    multiplier=5,    margin=0.08, exch="DCE"),
    "PP": dict(name="聚丙烯", multiplier=5,    margin=0.08, exch="DCE"),
    "L":  dict(name="聚乙烯", multiplier=5,    margin=0.08, exch="DCE"),
    "EG": dict(name="乙二醇", multiplier=10,   margin=0.09, exch="DCE"),
    "EB": dict(name="苯乙烯", multiplier=5,    margin=0.10, exch="DCE"),
    "PG": dict(name="液化石油气", multiplier=20, margin=0.10, exch="DCE"),
    # ===== 郑商所 CZCE =====
    "CF": dict(name="棉花",   multiplier=5,    margin=0.08, exch="CZCE"),
    "SR": dict(name="白糖",   multiplier=10,   margin=0.08, exch="CZCE"),
    "TA": dict(name="PTA",    multiplier=5,    margin=0.08, exch="CZCE"),
    "MA": dict(name="甲醇",   multiplier=10,   margin=0.09, exch="CZCE"),
    "FG": dict(name="玻璃",   multiplier=20,   margin=0.10, exch="CZCE"),
    "SA": dict(name="纯碱",   multiplier=20,   margin=0.10, exch="CZCE"),
    "RM": dict(name="菜粕",   multiplier=10,   margin=0.08, exch="CZCE"),
    "OI": dict(name="菜油",   multiplier=10,   margin=0.08, exch="CZCE"),
    "UR": dict(name="尿素",   multiplier=20,   margin=0.08, exch="CZCE"),
    "PF": dict(name="短纤",   multiplier=5,    margin=0.08, exch="CZCE"),
    "SM": dict(name="锰硅",   multiplier=5,    margin=0.10, exch="CZCE"),
    "SF": dict(name="硅铁",   multiplier=5,    margin=0.10, exch="CZCE"),
    "AP": dict(name="苹果",   multiplier=10,   margin=0.10, exch="CZCE"),
    "CJ": dict(name="红枣",   multiplier=5,    margin=0.10, exch="CZCE"),
    # ===== 能源中心 INE =====
    "SC": dict(name="原油",   multiplier=1000, margin=0.09, exch="INE"),   # 桶
    "NR": dict(name="20号胶", multiplier=10,   margin=0.08, exch="INE"),
    "LU": dict(name="低硫燃油", multiplier=10, margin=0.10, exch="INE"),
    # ===== 郑商所 CZCE (补充) =====
    "PK": dict(name="花生",   multiplier=5,    margin=0.08, exch="CZCE"),
    "SH": dict(name="烧碱",   multiplier=30,   margin=0.08, exch="CZCE"),
    "PX": dict(name="对二甲苯", multiplier=5,  margin=0.08, exch="CZCE"),
    # ===== 大商所 DCE (补充) =====
    "A":  dict(name="豆一",   multiplier=10,   margin=0.08, exch="DCE"),
    "B":  dict(name="豆二",   multiplier=10,   margin=0.08, exch="DCE"),
    "CS": dict(name="淀粉",   multiplier=10,   margin=0.08, exch="DCE"),
    # ===== 上期所 SHFE (补充) =====
    "AO": dict(name="氧化铝", multiplier=20,   margin=0.09, exch="SHFE"),
    "BR": dict(name="丁二烯橡胶", multiplier=5, margin=0.09, exch="SHFE"),
    # ===== 广期所 GFEX (补充) =====
    "PS": dict(name="多晶硅", multiplier=3,    margin=0.09, exch="GFEX"),
    # ===== 广期所 GFEX =====
    "SI": dict(name="工业硅", multiplier=5,    margin=0.09, exch="GFEX"),
    "LC": dict(name="碳酸锂", multiplier=1,    margin=0.10, exch="GFEX"),
}

# 中金所股指期货（剔除，不匹配规格）
INDEX_FUTURES = {"IF", "IC", "IH", "IM", "TF", "TS", "T"}
