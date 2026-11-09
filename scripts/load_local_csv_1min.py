"""Load 国内期货 1 分钟 CSV（D:\\学习资料\\期货分钟数据）into qhyc L0 minute_bar。

设计依据：docs/L0_本地CSV取代设计.md
与旧 load_1min.py 的区别：旧脚本面向「期货主力连续1min最新 / *9999.* / *8888.*」旧树，
本脚本面向新树：
    <EXCH>_<中文名>/<VAR>_<中文名>/
        <VAR>主力连续.csv        # 主连层（12 列）
        <VAR><CONTRACT>.csv      # 合约层（8 列），DCE 小写前缀 m2611.csv
        更新/                    # 与根目录同内容（超集），同名文件优先取此处
        退市.zip                 # 历史已退市合约（zip 内 CSV），必须读，否则 8888 缺历史

关键归一化（入库即固化，杜绝下游误比）：
  * ts = **end-time 标记**，统一 UTC：
      - 主连 eob 本身即 end-time（带 +08:00）→ 直接转 UTC
      - 合约 日期 是 **起始时刻标签** → **+1 分钟** 后才与主连同口径
        （实证：RB2701 与主连同日按 +1min 对齐后 OHLC 四项 100.0% 全等，
          0min 仅 6.5%，-1min 4.2%。未归一会导致 L0 内两层不可 JOIN、
          8888 整体前移 1 分钟、L2 的 cc 与 idx_delta 比较错开窗口）
  * DCE 量 ÷2 归单边 —— **主连与合约都要除**：实证同日主连 Σvol 与当日主力合约
    Σvol 原值完全相同（M 853,445=853,445 / RB 954,895=954,895），两层同量纲；
    只除合约会造成 M888=2×M8888 的口径断裂（V3 实测比值 2.00）
  * 合约层 amount = 源原生额；CZCE 未含合约乘数 → caliber_amt='no_multiplier'，
    其余 'true'（真额）。下游跨品种比额须先按乘数折算。
  * 主连层 amount → amount_raw（源原生，语义不同，禁与合约层混算），amount=NULL，caliber_amt='raw'
  * 合成 XXXX8888 指数连（**back-adjusted 连续法**），供 L2 换月检测

写入策略（幂等，可反复重跑）：
  COPY → TEMP 暂存表 → INSERT ... ON CONFLICT (symbol, ts) DO UPDATE
  不再裸 COPY 主表：minute_bar 主键 (symbol, ts)，库内已有 akshare 的 888 行，
  裸 COPY 会撞主键整品种回滚。upsert 使重跑覆盖为新源、且不删区间外旧行。
  `--backup-old` 会先把将被覆盖的**非本脚本来源**旧行快照到
  <schema>.minute_bar_ab_baseline，供 scripts/ab_compare_l0.sql 做 A/B（否则旧数据被覆盖后无可比对象）。

schema 限定（必须）：
  db_layering 迁移后 minute_bar 实体表在 l0_raw，public 只是兼容视图；
  dim_symbol / dim_contract 实体表在 l3_ref，public 同为视图。
  对视图执行 COPY / INSERT..ON CONFLICT 会直接报错，故所有写入均经
  resolve_table() 动态定位**实体表**（relkind in r,p），不写死 schema。

运行（cwd 必须是 qhyc 项目根，库可达时；需先跑 migrations/034）：
    python scripts/load_local_csv_1min.py --root "D:/学习资料/期货分钟数据"
    python scripts/load_local_csv_1min.py --only IC,RB,CU --backup-old   # 冒烟 + 保留 A/B 基线
    python scripts/load_local_csv_1min.py --no-db                       # 只打印将处理的品种
加载后（库内）：
    ⚠ 2026-11-09 CL-20261109-14：sp_build_l1_from_minute 已弃用（migrations/067，
      调用即 RAISE），因为它用 time_bucket 自然整点切桶 = 旧网格，与回测口径冲突。
      L1 请改用会话网格重建（本脚本写入的是终点约定分钟，M19 已结案）：
        python scripts/ops/rebuild_l1_grid.py \
            --symbols ALL --start 2015-01-01 --end <今天> --freqs 60,30,15,5 --apply
    CALL sp_build_l2_roll_segment('min15');       -- 换月段（依赖 8888，已由本脚本写入）
  注意：sp_synth_8888_from_contracts 已废弃为告警空壳（等权/量加权会破坏 L2），不要调用。
"""
import os, re, io, gc, argparse, time, sys, zipfile
import numpy as np
import pandas as pd

try:
    import psycopg2
except Exception:            # 允许 --no-db 在无驱动环境（沙箱/CI）下做预演
    psycopg2 = None

# ---- 交易所目录前缀 → 代码 ----
EXCH_MAP = {
    "CFFEX_中国金融期货交易所": "CFFEX",
    "CZCE-郑州商品期货交易所": "CZCE",
    "DCE-大连期货交易所": "DCE",
    "GFEX_广州期货交易所": "GFEX",
    "INE_上海国际能源交易中心": "INE",
    "SHFE_上海期货交易所": "SHFE",
}
VAR_RE = re.compile(r"^[A-Za-z]{1,3}$")

# 主连层 12 列（注意 open,close,high,low 的特殊顺序）
MAIN_COLS = ["exchange", "symbol", "open", "close", "high", "low",
             "amount", "volume", "position", "bob", "eob", "type"]
# 合约层 8 列
CONT_COLS = ["日期", "开盘价", "最高价", "最低价", "收盘价", "成交额", "成交量", "持仓量"]

# minute_bar 全列顺序（原 15 + 新增 8）
OUT_COLS = ["symbol", "ts", "open", "high", "low", "close", "volume", "amount",
            "open_interest", "high_limit", "low_limit", "pre_close", "settle_price",
            "contract", "src", "ts_end", "layer", "exchange", "variety_code",
            "caliber_vol", "caliber_amt", "amount_raw", "raw_type"]

SH_FMT = "%Y-%m-%d %H:%M:%S%z"  # 2025-01-02 09:01:00+0000（UTC）

SRC = "local_csv_20261030"
COPY_CHUNK = 500_000          # 每批 COPY 行数（控内存峰值，全库外推约 5 亿行）

# 合约层「日期」为起始时刻标签 → +1min 归一为 end-time（与主连 eob 同口径）
CONT_TS_SHIFT = pd.Timedelta(minutes=1)

# 量纲一致性门：主连 Σvol / 主力合约 Σvol 的容忍区间（按年）
VOL_RATIO_LO, VOL_RATIO_HI = 0.4, 2.5


def _load_db_creds():
    creds = dict(host="127.0.0.1", port=5432, user="futures",
                 password="futures", dbname="futures")
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".env")
    try:
        with open(env_path, encoding="utf-8-sig") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k == "POSTGRES_PASSWORD": creds["password"] = v
                elif k == "POSTGRES_USER": creds["user"] = v
                elif k == "POSTGRES_DB": creds["dbname"] = v
                elif k == "POSTGRES_PORT": creds["port"] = int(v)
    except FileNotFoundError:
        pass
    for ek, ck in {"POSTGRES_HOST": "host", "POSTGRES_PORT": "port",
                  "POSTGRES_USER": "user", "POSTGRES_PASSWORD": "password",
                  "POSTGRES_DB": "dbname"}.items():
        if os.environ.get(ek):
            creds[ck] = int(os.environ[ek]) if ck == "port" else os.environ[ek]
    return creds


DB = _load_db_creds()


# ---------------------------------------------------------------------------
# 时间戳归一
# ---------------------------------------------------------------------------
def _norm_ts_main(eob):
    """主连 eob（end-time，带 +08:00）→ UTC tz-aware。"""
    return pd.to_datetime(eob, utc=True)


def _norm_ts_cont(日期):
    """合约 日期（naive SH 墙钟，**起始时刻标签**）→ +1min 归 end-time → UTC。

    实证依据见模块 docstring：RB2701 按 +1min 对齐后与主连 OHLC 100% 全等。
    """
    return ((pd.to_datetime(日期) + CONT_TS_SHIFT)
            .dt.tz_localize("Asia/Shanghai").dt.tz_convert("UTC"))


# ---------------------------------------------------------------------------
# 目录解析 / 文件发现
# ---------------------------------------------------------------------------
def parse_variety_dir(vdir):
    """从品种目录的**完整路径**解析 (exchange, variety_code)。

    交易所前缀写在父目录（SHFE_上海期货交易所/RB_螺纹钢），不在品种目录名上；
    早期版本对 basename 匹配 EXCH_RE，恒返回 (None, None) → --only 过滤后 0 个品种、全库空转。
    这里改为逐段回扫路径，命中 EXCH_MAP 的段即交易所，末段才是品种码。
    """
    p = os.path.abspath(vdir.rstrip("/\\"))
    parts = [x for x in re.split(r"[\\/]+", p) if x]
    if not parts:
        return None, None
    vname = parts[-1]
    variety = vname.split("_", 1)[0].upper()
    if not VAR_RE.match(variety):
        return None, None
    for seg in reversed(parts[:-1]):
        if seg in EXCH_MAP:
            return EXCH_MAP[seg], variety
    return None, None


def _is_retired_path(path):
    parts = [x for x in re.split(r"[\\/]+", path) if x]
    return any("退市" in x for x in parts)


def discover_files(vdir):
    """返回 (main_file, contract_sources[])；contract_sources 元素为文件路径(str)
    或 (zip_path, entry) 元组（来自 退市.zip 的历史合约链）。同名文件优先 更新/。"""
    def _walk(sub):
        files = {}
        for root, _, fs in os.walk(sub):
            if _is_retired_path(root):
                continue
            for f in fs:
                if f.endswith(".csv"):
                    files[f] = os.path.join(root, f)
        return files

    root_files = _walk(vdir)
    upd_dir = os.path.join(vdir, "更新")
    upd_files = _walk(upd_dir) if os.path.isdir(upd_dir) else {}
    merged = dict(root_files)          # 更新/ 为超集，同名覆盖
    merged.update(upd_files)

    main_file = None
    contracts = []
    for fn, path in merged.items():
        if fn[:-4].endswith("主力连续"):
            main_file = path
        else:
            contracts.append(path)

    # 退市.zip：历史合约链（每品种一个），必须读，否则 8888 缺全历史
    try:
        entries = sorted(os.listdir(vdir))
    except OSError:
        entries = []
    for f in entries:
        if f.endswith("退市.zip"):
            zp = os.path.join(vdir, f)
            try:
                with zipfile.ZipFile(zp) as z:
                    for entry in z.namelist():
                        if entry.endswith(".csv") and "主力连续" not in entry:
                            contracts.append((zp, entry))
            except Exception as e:
                print(f"  [warn] 无法打开 {f}: {e}")
    return main_file, contracts


# ---------------------------------------------------------------------------
# 构帧
# ---------------------------------------------------------------------------
def build_main_df(path, exchange, variety):
    df = pd.read_csv(path, header=0, names=MAIN_COLS, encoding="utf-8-sig")
    for c in ["open", "close", "high", "low", "amount", "volume", "position"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["close", "eob"]).copy()
    df["symbol"] = variety + "888"
    df["ts"] = _norm_ts_main(df["eob"])
    vol = df["volume"].fillna(0).astype("float64")
    if exchange == "DCE":
        # DCE 为双边计数。实证：同日主连 Σvol 与当日主力合约 Σvol **原值完全相同**
        # （M 853,445 = 853,445；RB 954,895 = 954,895）→ 两层同量纲。
        # 若只对合约 ÷2 而不对主连 ÷2，M888 的量会是 M8888/RB888 的 2 倍（V3 实测比值 2.00）。
        vol = vol / 2.0
    out = pd.DataFrame({
        "symbol": df["symbol"],
        # ts/ts_end 保持 datetime64（8B/行）；字符串化推迟到 COPY 分块内完成，
        # 否则 26 字符 Python 串 × 千万行 = 数 GB 常驻，7GB 生产机会直接被 OOM killer 干掉。
        "ts": df["ts"],
        "open": df["open"], "high": df["high"], "low": df["low"], "close": df["close"],
        "volume": _safe_vol(vol),
        "amount": np.nan,                         # 主连真额不可得
        "open_interest": df["position"],
        "high_limit": np.nan, "low_limit": np.nan,
        "pre_close": np.nan, "settle_price": np.nan,
        "contract": df["symbol"],                 # 主连行 contract=自身 symbol
        "src": SRC,
        "ts_end": df["ts"],
        "layer": "continuous",
        "exchange": exchange,
        "variety_code": variety,
        "caliber_vol": "single_side",             # 全库统一单边（DCE 已÷2）
        "caliber_amt": "raw",                     # 主连原生额不可比
        "amount_raw": df["amount"],               # 源原生额
        "raw_type": pd.to_numeric(df["type"], errors="coerce").astype("Int64"),
    })
    return out[OUT_COLS]


def build_contract_df(source, exchange, variety, amt_caliber="true"):
    if isinstance(source, tuple):  # (zip_path, entry)
        zp, entry = source
        with zipfile.ZipFile(zp) as z:
            raw = z.read(entry)
        df = pd.read_csv(io.BytesIO(raw), header=0, names=CONT_COLS, encoding="utf-8-sig")
        base = os.path.basename(entry)[:-4]       # 去 .csv
    else:
        df = pd.read_csv(source, header=0, names=CONT_COLS, encoding="utf-8-sig")
        base = os.path.basename(source)[:-4]
    for c in ["开盘价", "最高价", "最低价", "收盘价", "成交额", "成交量", "持仓量"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.dropna(subset=["收盘价", "日期"]).copy()
    symbol = base.upper()                         # m2611 → M2611
    df["symbol"] = symbol
    df["ts"] = _norm_ts_cont(df["日期"])          # +1min → end-time
    vol = df["成交量"].fillna(0).astype("float64")
    if exchange == "DCE":
        vol = vol / 2.0                           # DCE 双边 → 单边
    out = pd.DataFrame({
        "symbol": df["symbol"],
        "ts": df["ts"],                           # datetime64，同 build_main_df（见其注释）
        "open": df["开盘价"], "high": df["最高价"], "low": df["最低价"], "close": df["收盘价"],
        "volume": _safe_vol(vol),
        "amount": df["成交额"],                   # 源原生额（CZCE 未含乘数）
        "open_interest": df["持仓量"],
        "high_limit": np.nan, "low_limit": np.nan,
        "pre_close": np.nan, "settle_price": np.nan,
        "contract": df["symbol"],
        "src": SRC,
        "ts_end": df["ts"],
        "layer": "contract",
        "exchange": exchange,
        "variety_code": variety,
        "caliber_vol": "single_side",
        "caliber_amt": amt_caliber,
        "amount_raw": np.nan,
        "raw_type": np.nan,
    })
    return out[OUT_COLS]


def synth_8888(cur, exchange, variety, amt_caliber="true", front=None, carry=None):
    """由暂存表中的合约链合成 XXXX8888 指数连（平滑连续，供 L2 换月检测 ratio 门使用）。

    方法：按**交易日**取唯一 front 合约（日末持仓量最大者）的分钟价格，对每次换月
    （front 切换）用新旧合约同一分钟的价差做累计剔除（back-adjustment），得到一条
    在换月处平滑、在真实涨跌处仍如实跳动的连续序列。这样 sp_build_l2_roll_segment 的
    ratio 门(abs(idx_delta) < 0.4·|cc|) 才能在换月时正确触发（main 跳、8888 不跳）。

    关键修复（2026-11）：早期『等权/量加权』合成法在换月瞬间与主力同步跳空 → idx_delta
    始终≈cc → ratio 门恒失败 → L2 roll_segment 全空 → back_adj 崩坏。必须做 back-adjustment。

    【2026-11-04 流式改造】front 选择下推到 SQL（DISTINCT ON + 持仓量排序），合约原始行
    不再整体驻留 pandas：CU 单品种实测 ~1300 万行，'全合约 concat + 合成' 峰值 >7GB 触发
    OOM killer。改造后内存只与「front 序列(≈主连分钟数) + 单个合约文件」同阶。

    量/额/持仓取全合约之和（市场真值合计）。价格按 anchor（主连时间轴）对齐并前向填充，
    使 8888 在每个主连分钟都有价（避免 L2 join 时 idx_delta 为 NULL 而漏检换月）；
    **量/额/持仓不 ffill**——缺失分钟按「该分钟无成交」处置，置 0，绝不复制上一分钟的量
    （否则 L1 聚合量被系统性放大，违背源数据「稀疏库、不补零」口径）。
    """
    if front is None:
        front, carry = fetch_front(cur)
    if front is None or len(front) == 0:
        return None
    sums = fetch_contract_sums(cur)      # index=ts，列 v/amt/oi（全合约合计）
    anchor = fetch_anchor_ts(cur)        # 主连时间轴；无主连文件时为 None

    # 按日累计换月偏移：某交易日 bar 的 cum = 截至该日(含)所有 carry 之和。
    # carry 为「新旧 front 同一分钟价差」，不同于旧实现用「相邻 front bar 收盘差」
    # （后者在按日口径下等价于跨夜差值，会把隔夜真实行情一并抹掉）。
    if carry is not None and len(carry):
        all_days = pd.Index(
            sorted(set(front["dd"].dropna().unique()) | set(carry.index)), name="dd")
        daily_cum = carry["carry"].reindex(all_days).fillna(0.0).cumsum()
    else:
        daily_cum = None
    if daily_cum is None:
        cum_s = pd.Series(0.0, index=front.index)
    else:
        cum_s = front["dd"].map(daily_cum).fillna(0.0).astype("float64")
    cont = pd.DataFrame({
        "open": front["open"] - cum_s, "high": front["high"] - cum_s,
        "low": front["low"] - cum_s, "close": front["close"] - cum_s,
    }, index=front.index)
    if anchor is not None and len(anchor):
        cont = cont.reindex(anchor).ffill()
        sums = sums.reindex(anchor)
    cont = cont.dropna(how="all")
    if len(cont) == 0:
        return None
    symbol = variety + "8888"
    vol = _safe_vol(sums["v"].reindex(cont.index).fillna(0))
    out = pd.DataFrame({
        "symbol": symbol,
        "ts": cont.index,
        "open": cont["open"], "high": cont["high"], "low": cont["low"], "close": cont["close"],
        "volume": vol,
        "amount": sums["amt"].reindex(cont.index).fillna(0),
        "open_interest": sums["oi"].reindex(cont.index).fillna(0),
        "high_limit": np.nan, "low_limit": np.nan,
        "pre_close": np.nan, "settle_price": np.nan,
        "contract": symbol,
        "src": "local_csv_synth_8888",
        "ts_end": cont.index,
        "layer": "index",
        "exchange": exchange,
        "variety_code": variety,
        "caliber_vol": "single_side",
        "caliber_amt": amt_caliber,
        "amount_raw": np.nan,
        "raw_type": np.nan,
    })
    return out[OUT_COLS]


# ---------------------------------------------------------------------------
# 量纲一致性门（拦截源数据量纲污染，如 IC 2015 Σvol 放大 ~850 倍）
# ---------------------------------------------------------------------------
def check_volume_scale(cur, front):
    """按年对比『主连 Σvol』与『当年 front 合约 Σvol』，返回异常年份列表。

    预期比值≈1。实证：两层同量纲（同日主连 Σvol 与当日主力合约 Σvol 原值完全相同），
    故 loader 对 DCE 的**主连与合约同时 ÷2**，归一后所有交易所比值都应≈1。
    越界即判定该年源数据量纲异常（如 IC 2015 Σvol 放大 850 倍），交由调用方决定告警或拒绝。

    【流式改造】主连 Σvol 由 SQL 按年汇总；front Σvol 由已取回的 front 序列按年汇总，
    不再需要把合约束帧载入内存。
    """
    if front is None or len(front) == 0:
        return [], {}
    cur.execute(
        """SELECT extract(year from ts)::int AS yr, sum(volume)
           FROM stage_dedup WHERE layer='continuous' GROUP BY 1""")
    mv = {int(y): float(v) for y, v in cur.fetchall() if v is not None}
    if not mv:
        return [], {}
    fv = front.groupby(front.index.year)["volume"].sum()
    bad, detail = [], {}
    for y in sorted(set(mv) & set(fv.index)):
        if float(fv[y]) <= 0:
            continue
        r = float(mv[y]) / float(fv[y])
        detail[int(y)] = round(r, 3)
        if not (VOL_RATIO_LO <= r <= VOL_RATIO_HI):
            bad.append(int(y))
    return bad, detail


# ---------------------------------------------------------------------------
# 库写入
# ---------------------------------------------------------------------------
_TABLE_CACHE = {}


def resolve_table(cur, name, prefer=("l0_raw", "l3_ref", "public")):
    """定位 name 的**实体表**（relkind r/p），返回 'schema.name'；视图(shim)不算。

    重要：db_layering 迁移后 public.<t> 是兼容视图，对视图 COPY/ON CONFLICT 必失败。
    """
    key = (name, prefer)
    if key in _TABLE_CACHE:
        return _TABLE_CACHE[key]
    cur.execute(
        """SELECT n.nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE c.relname = %s AND c.relkind IN ('r','p')
             AND n.nspname = ANY(%s)""", (name, list(prefer)))
    rows = {r[0] for r in cur.fetchall()}
    if not rows:
        raise RuntimeError(f"找不到实体表 {name}（views 不算），已查 schema={list(prefer)}")
    for s in prefer:
        if s in rows:
            _TABLE_CACHE[key] = f"{s}.{name}"
            return _TABLE_CACHE[key]
    got = sorted(rows)[0]
    _TABLE_CACHE[key] = f"{got}.{name}"
    return _TABLE_CACHE[key]


def _stage_name():
    return "stage_mm_localcsv"


def create_stage(cur):
    """建本品种用的 TEMP 暂存表（追加写，一个品种用完即 DROP）。

    额外 `_seq` 自增序号用于去重时保留「后写入者」——与旧实现 drop_duplicates(keep='last')
    同义：更新/ 目录与根目录的同名合约、以及散装与退市包可能重复覆盖同一 (symbol, ts)。
    """
    l0 = resolve_table(cur, "minute_bar")
    cur.execute(f"DROP TABLE IF EXISTS pg_temp.{_stage_name()}")
    cur.execute(f"CREATE TEMP TABLE {_stage_name()} (LIKE {l0} INCLUDING DEFAULTS)")
    cur.execute(f"ALTER TABLE {_stage_name()} ADD COLUMN _seq bigint")
    return l0


def copy_append(cur, df, seq_base, table=None):
    """把单帧追加进暂存表。ts 在此处（分块内）才字符串化，避免全量字符串驻留内存。

    table 缺省为 stage_mm_localcsv；合成 8888 时直接追加进 stage_dedup（其 symbol 全新，
    不与既有行冲突，故无需再走一次去重物化）。
    """
    if df is None or len(df) == 0:
        return 0
    stage = table or _stage_name()
    cols = ",".join(OUT_COLS)
    n = len(df)
    for i in range(0, n, COPY_CHUNK):
        part = df.iloc[i:i + COPY_CHUNK].copy()
        for c in ("ts", "ts_end"):
            part[c] = pd.to_datetime(part[c], utc=True).dt.strftime(SH_FMT)
        part["_seq"] = np.arange(seq_base + i, seq_base + i + len(part), dtype="int64")
        buf = io.StringIO()
        part.to_csv(buf, index=False, header=False, na_rep="")
        buf.seek(0)
        cur.copy_expert(
            f"COPY {stage} ({cols},_seq) FROM STDIN WITH (FORMAT CSV, NULL '')", buf)
    return n


def materialize_dedup(cur):
    """stage → stage_dedup（同 (symbol,ts) 保留 _seq 最大者），并建索引。

    一次性排序物化，避免后续 front/sums/gate/upsert 各自重排一遍；同时把重复行挡在
    ON CONFLICT 之外（否则同一语句内两次命中同一主键会直接报错）。
    """
    cur.execute("DROP TABLE IF EXISTS pg_temp.stage_dedup")
    cur.execute(
        f"""CREATE TEMP TABLE stage_dedup AS
            SELECT DISTINCT ON (symbol, ts) *
            FROM {_stage_name()} ORDER BY symbol, ts, _seq DESC""")
    cur.execute("CREATE UNIQUE INDEX ON stage_dedup (symbol, ts)")
    cur.execute("ANALYZE stage_dedup")
    cur.execute("SELECT count(*) FROM stage_dedup")
    n_dedup = cur.fetchone()[0]
    cur.execute(f"DROP TABLE IF EXISTS pg_temp.{_stage_name()}")   # 及时释放磁盘
    return n_dedup


# front = 每 ts 持仓量最大的合约（平手取合约月份更大）。substring 取 4 位，
# 与旧实现 str.extract(r"(\d{4})$") 一致（CZCE 3 位码取不到 → 退化为 NULL，不参与平手裁决）。
_YM_SQL = "substring(symbol from '[0-9]{4}$')::int"


def fetch_front(cur):
    """按日口径返回 (front_df, carry_df)。

    【2026-11-04 按日口径改造】此前逐分钟取 open_interest 首位：CU 实测 11,135 次
    front 翻转（真实换月约 279 次 / 22.4 年 ≈ 12.5 次/年），给 8888 注入约 1,290 次
    伪跳空，且使 8888 与 888 在换月处同步跳动 → L2 ratio 门失效。改为：

      * 每个交易日按「日末持仓量最大」确定**唯一** front 合约，当日全部分钟均取该合约价；
      * front 切换即换月。carry 不能用「昨收 vs 今开」（含隔夜真实行情，会被误剔除），
        而取新旧合约在换月日 ±5 天窗口内**最后一个共同分钟**的价差 = B.close − A.close；
      * 累计 carry 做 back-adjustment，使 8888 在换月处平滑、日内真实涨跌如实保留。

    front_df: index=ts，列 symbol/open/high/low/close/volume/dd（dd=上海交易日）。
    carry_df: index=dd，列 carry（换月价差；无共同分钟的换月点记 0）。
    """
    # 1) 每日 front 合约（日末 OI 最大，平手取合约月份更大者）
    cur.execute("DROP TABLE IF EXISTS pg_temp.stage_daily_front")
    cur.execute(
        f"""CREATE TEMP TABLE stage_daily_front AS
            WITH c AS (
              SELECT ts, symbol, open_interest,
                     (ts AT TIME ZONE 'Asia/Shanghai')::date AS dd
              FROM stage_dedup WHERE layer='contract' AND close IS NOT NULL
            ), doi AS (
              SELECT DISTINCT ON (dd, symbol) dd, symbol, open_interest
              FROM c ORDER BY dd, symbol, ts DESC
            )
            SELECT DISTINCT ON (dd) dd, symbol AS front_symbol
            FROM doi
            ORDER BY dd, open_interest DESC NULLS LAST,
                     {_YM_SQL} DESC NULLS LAST""")
    cur.execute("ANALYZE stage_daily_front")

    # 2) front 分钟序列（按 (symbol, ts) 索引做范围扫描，避免全表表达式 join）
    cur.execute(
        """SELECT c.ts, c.symbol, c.open, c.high, c.low, c.close, c.volume, d.dd
           FROM stage_daily_front d
           JOIN stage_dedup c
             ON c.symbol = d.front_symbol
            AND c.ts >= (d.dd::timestamp AT TIME ZONE 'Asia/Shanghai')
            AND c.ts <  ((d.dd + 1)::timestamp AT TIME ZONE 'Asia/Shanghai')
           WHERE c.layer='contract' AND c.close IS NOT NULL
           ORDER BY c.ts""")
    rows = cur.fetchall()
    if not rows:
        return None, None
    df = pd.DataFrame(rows, columns=["ts", "symbol", "open", "high", "low",
                                     "close", "volume", "dd"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    front = df.set_index("ts").sort_index()

    # 3) 换月 carry：新旧 front 在换月日 ±5 天内最后一个共同分钟的价差
    cur.execute(
        """WITH sw AS (
             SELECT dd, front_symbol AS new_sym,
                    lag(front_symbol) OVER (ORDER BY dd) AS old_sym
             FROM stage_daily_front
           ), s AS (
             SELECT * FROM sw WHERE old_sym IS NOT NULL AND new_sym <> old_sym
           )
           SELECT s.dd, s.old_sym, s.new_sym,
             (SELECT b.close - a.close
                FROM stage_dedup a JOIN stage_dedup b ON a.ts = b.ts
               WHERE a.symbol = s.old_sym AND b.symbol = s.new_sym
                 AND a.ts >= (s.dd::timestamp AT TIME ZONE 'Asia/Shanghai') - interval '5 days'
                 AND a.ts <  (s.dd::timestamp AT TIME ZONE 'Asia/Shanghai') + interval '5 days'
                 AND a.layer='contract' AND b.layer='contract'
                 AND a.close IS NOT NULL AND b.close IS NOT NULL
               ORDER BY a.ts DESC LIMIT 1) AS carry
           FROM s ORDER BY s.dd""")
    crows = cur.fetchall()
    if crows:
        carry = pd.DataFrame(crows, columns=["dd", "old_sym", "new_sym", "carry"])
        carry["carry"] = pd.to_numeric(carry["carry"], errors="coerce").fillna(0.0)
        carry = carry.set_index("dd")[["carry"]].sort_index()
        n_roll = len(carry)
        n_zero = int((carry["carry"] == 0).sum())
    else:
        carry = pd.DataFrame(columns=["carry"])
        n_roll, n_zero = 0, 0
    print(f"  [front/daily] {len(front):,} 根 front bar / {front['dd'].nunique():,} 个交易日 / "
          f"换月 {n_roll} 次（carry 缺失 {n_zero}）")
    return front, carry


def fetch_contract_sums(cur):
    """返回全合约逐分钟合计（index=ts，列 v/amt/oi）。口径同旧实现：剔除 close 为空的行。"""
    cur.execute(
        """SELECT ts, sum(volume), sum(amount), sum(open_interest)
           FROM stage_dedup WHERE layer='contract' AND close IS NOT NULL GROUP BY ts""")
    rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["ts", "v", "amt", "oi"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    for c in ("v", "amt", "oi"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return df.set_index("ts").sort_index()


def fetch_anchor_ts(cur):
    """主连时间轴（8888 的对齐基准）。用 epoch 秒回传，避免千万级 datetime 对象驻留。"""
    cur.execute("SELECT round(extract(epoch from ts))::bigint FROM stage_dedup "
                "WHERE layer='continuous' ORDER BY ts")
    arr = np.fromiter((r[0] for r in cur.fetchall()), dtype="int64", count=-1)
    if arr.size == 0:
        return None
    return pd.DatetimeIndex(pd.to_datetime(arr, unit="s", utc=True))


def assert_pk(cur, table, cols=("symbol", "ts")):
    """确认 (symbol, ts) 上有主键/唯一约束——ON CONFLICT 的前提，缺了必须早失败。"""
    key = ("pk", table)
    if key in _TABLE_CACHE:
        return
    cur.execute(
        """SELECT array_agg(a.attname ORDER BY k.ord)
           FROM pg_constraint c
           JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
           JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
           WHERE c.conrelid = %s::regclass AND c.contype IN ('p', 'u')
           GROUP BY c.oid""", (table,))
    found = [tuple(r[0]) for r in cur.fetchall()]
    if tuple(cols) not in found:
        raise RuntimeError(
            f"{table} 上缺 {'+'.join(cols)} 主键/唯一约束（现有={found}），"
            f"无法 upsert；请先补 ALTER TABLE {table} ADD PRIMARY KEY (symbol, ts)")
    _TABLE_CACHE[key] = True


# 纯插入模式（由 l0_batch_run 流水线在「主表已清空、逐品种不重叠」时开启）。
# 压缩 chunk 上的 INSERT..ON CONFLICT 需做冲突探测/唯一性校验，实测比纯 INSERT 慢一倍以上；
# 切换期每品种 symbol 集合互不重叠，无需 upsert。默认 False，保持独立运行时的幂等性。
PURE_INSERT = False

# 单分钟成交量合理上限。源 CSV 偶发出现超出 int64 的值（实测 AG888 2013-07-09 的
# volume = 9223372036854775808 = 2^63，强转 int64 后变 INT64_MIN，污染日量汇总与 L1 量）。
VOL_CAP = 1_000_000_000_000


def _safe_vol(v):
    """成交量归一为 int64，并钳制溢出/负值/NaN（源数据脏值防御）。"""
    x = pd.to_numeric(v, errors="coerce")
    x = x.replace([np.inf, -np.inf], np.nan).fillna(0)
    return x.clip(lower=0, upper=VOL_CAP).round().astype("int64")


def upsert_from_stage(cur, symbols, ts_min, ts_max, backup_old=False):
    """暂存表 → 主表。默认 upsert（幂等）；PURE_INSERT=True 时为纯插入（切换期提速）。"""
    l0 = resolve_table(cur, "minute_bar")
    if not PURE_INSERT:
        assert_pk(cur, l0)
    cols = ",".join(OUT_COLS)
    upd = ",".join(f"{c}=EXCLUDED.{c}" for c in OUT_COLS
                   if c not in ("symbol", "ts"))
    if backup_old:
        schema = l0.split(".")[0]
        bl = f"{schema}.minute_bar_ab_baseline"
        cur.execute(f"CREATE TABLE IF NOT EXISTS {bl} (LIKE {l0})")
        cur.execute(f"CREATE UNIQUE INDEX IF NOT EXISTS ux_mm_ab_baseline ON {bl} (symbol, ts)")
        cur.execute(
            f"""INSERT INTO {bl}
                SELECT t.* FROM {l0} t
                WHERE t.symbol = ANY(%s) AND t.ts BETWEEN %s AND %s
                  AND coalesce(t.src, '') NOT LIKE 'local_csv%%'
                ON CONFLICT DO NOTHING""",
            (list(symbols), ts_min, ts_max))
        cur.execute(f"SELECT count(*) FROM {bl} WHERE symbol = ANY(%s)", (list(symbols),))
        print(f"  [ab] 旧源基线快照（{bl}）现有 {cur.fetchone()[0]} 行")
    if PURE_INSERT:
        cur.execute(f"INSERT INTO {l0} ({cols}) SELECT {cols} FROM stage_dedup")
    else:
        cur.execute(
            f"""INSERT INTO {l0} ({cols}) SELECT {cols} FROM stage_dedup
                ON CONFLICT (symbol, ts) DO UPDATE SET {upd}""")
    n = cur.rowcount
    cur.execute("DROP TABLE IF EXISTS pg_temp.stage_dedup")
    return n


def register_dim(cur, symbols, exchange, variety):
    """登记 dim_symbol / dim_contract（实体表在 l3_ref；public 是视图，不可写）。

    【流式改造】symbols 由暂存表 DISTINCT 查出即可；同一品种内 exchange/variety 恒定，
    无需再从整帧 groupby。
    """
    ds = resolve_table(cur, "dim_symbol", ("l3_ref", "public"))
    dc = resolve_table(cur, "dim_contract", ("l3_ref", "public"))

    rows = []
    for sym in symbols:
        if sym.endswith("8888"):
            ns, prod = "index", variety
        elif sym.endswith("888"):
            ns, prod = "main", variety
        else:
            ns, prod = "contract", (variety or sym[:-4])
        rows.append((sym, prod, ns, exchange))
    if rows:
        cur.executemany(
            f"""INSERT INTO {ds} (symbol, product, namespace, exchange, source_tables, updated_at)
                VALUES (%s, %s, %s, %s, ARRAY['local_csv'], now())
                ON CONFLICT (symbol) DO UPDATE
                  SET updated_at = now(), namespace = EXCLUDED.namespace,
                      product = EXCLUDED.product, exchange = EXCLUDED.exchange,
                      source_tables = EXCLUDED.source_tables""", rows)

    crows = [(s, variety or s[:-4], exchange)
             for s in symbols
             if not s.endswith("888") and not s.endswith("8888")]
    if crows:
        # dim_contract 有 FK 到 dim_variety / dim_exchange；用 EXISTS 前置过滤，
        # 避免约束违例把整个事务打成 aborted（那样后续 commit 必失败、整品种回滚）。
        dv = resolve_table(cur, "dim_variety", ("l3_ref", "public"))
        de = resolve_table(cur, "dim_exchange", ("l3_ref", "public"))
        codes, vars_, exs = zip(*crows)
        cur.execute(
            f"""INSERT INTO {dc} (contract_code, variety_code, exchange, source)
                SELECT s.c, s.v, s.e, 'local_csv'
                FROM unnest(%s::text[], %s::text[], %s::text[]) AS s(c, v, e)
                WHERE EXISTS (SELECT 1 FROM {dv} WHERE variety_code = s.v)
                  AND EXISTS (SELECT 1 FROM {de} WHERE exchange_code = s.e)
                ON CONFLICT (contract_code) DO NOTHING""",
            (list(codes), list(vars_), list(exs)))
        if cur.rowcount < len(codes):
            print(f"  [dim] dim_contract 新增 {cur.rowcount}/{len(codes)}"
                  f"（其余为已存在或品种/交易所未登记，已跳过）")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def load_variety(conn, vdir, strict_volume=False, backup_old=False):
    exchange, variety = parse_variety_dir(vdir)
    if not exchange:
        print(f"  [skip] 无法解析交易所/品种：{vdir}")
        return None
    main_file, contracts = discover_files(vdir)
    if not main_file and not contracts:
        print(f"  [skip] {variety} 无 CSV（品种目录为空，属待补数据）")
        return None

    amt_caliber = "no_multiplier" if exchange == "CZCE" else "true"
    cur = conn.cursor()
    try:
        # ---- 1) 逐文件流式入暂存表（任一刻只驻留「一个文件」的行）----
        create_stage(cur)
        seq = 0
        if main_file:
            df = build_main_df(main_file, exchange, variety)
            seq += copy_append(cur, df, seq)
            del df
            gc.collect()
        n_file = 0
        for cf in contracts:
            df = build_contract_df(cf, exchange, variety, amt_caliber)
            if df is not None and len(df) > 0:
                seq += copy_append(cur, df, seq)
                n_file += 1
            del df
            gc.collect()          # 退市包逐合约读取，必须及时归还，否则 numpy 缓冲累积
        if seq == 0:
            print(f"  [skip] {variety} 文件无有效行")
            return None

        # ---- 2) 去重物化（同 (symbol,ts) 保留后写入者），stage 随即释放 ----
        n_dedup = materialize_dedup(cur)
        if seq != n_dedup:
            print(f"  [dedup] {variety} 去除重复 (symbol, ts) {seq - n_dedup} 行")

        # ---- 3) 量纲门：主连 vs front 合约，按年比（front 为按日口径）----
        front, carry = fetch_front(cur)
        bad_years, ratios = check_volume_scale(cur, front)
        if bad_years:
            top = ", ".join(f"{y}:{ratios[y]}" for y in bad_years[:6])
            msg = f"  [vol-gate] {variety} 主连/合约量比越界年份 {len(bad_years)} 个（{top}）"
            if strict_volume:
                print(msg + " → --strict-volume 拒绝入库")
                conn.rollback()
                return None
            print(msg + "（仅告警，建议核查源数据量纲）")

        # ---- 4) 合成 8888 并追加进暂存 ----
        n_src = seq                       # 源文件读入行数（不含合成行）
        s8888 = synth_8888(cur, exchange, variety, amt_caliber, front=front, carry=carry)
        n_idx = 0
        if s8888 is not None:
            n_idx = copy_append(cur, s8888, seq, table="stage_dedup")
            seq += n_idx
            del s8888
            gc.collect()

        # ---- 5) upsert 主表 + 登记维度 ----
        cur.execute("SELECT array_agg(DISTINCT symbol), min(ts), max(ts) FROM stage_dedup")
        symbols, ts_min, ts_max = cur.fetchone()
        n = upsert_from_stage(cur, sorted(symbols), ts_min, ts_max, backup_old=backup_old)
        register_dim(cur, sorted(symbols), exchange, variety)
        conn.commit()
        print(f"  [ok] {variety}: 文件 {1 if main_file else 0}+{n_file} 个 / "
              f"源 {n_src} 行 / 去重 {n_dedup} 行 / 合成8888 {n_idx} 行 / "
              f"入库 {seq} 行（upsert 影响 {n}，{exchange}, amt={amt_caliber}, "
              f"symbols={len(symbols)}）")
        return n
    except Exception as e:
        conn.rollback()
        print(f"  [FAIL] {variety}: {e}")
        return 0
    finally:
        cur.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=r"D:/学习资料/期货分钟数据")
    ap.add_argument("--only", nargs="+", default=None,
                    help="只处理指定品种，支持 'IC,RB,CU' 或 'IC RB CU'")
    ap.add_argument("--limit-varieties", type=int, default=None, help="只处理前 N 个品种（冒烟）")
    ap.add_argument("--no-db", action="store_true", help="不写库，仅打印将处理的品种（dry-run）")
    ap.add_argument("--strict-volume", action="store_true",
                    help="量纲门越界时拒绝该品种入库（默认仅告警）")
    ap.add_argument("--backup-old", action="store_true",
                    help="覆盖前把旧源(非 local_csv)行快照到 <schema>.minute_bar_ab_baseline，供 A/B")
    args = ap.parse_args()

    only = None
    if args.only:
        only = {w.strip().upper() for s in args.only for w in s.split(",") if w.strip()}

    # 收集品种目录（交易所目录下的子目录）
    vdirs = []
    for ex_dir in EXCH_MAP:
        ex_path = os.path.join(args.root, ex_dir)
        if not os.path.isdir(ex_path):
            continue
        for vname in sorted(os.listdir(ex_path)):
            vdirs.append(os.path.join(ex_path, vname))
    parsed = [(v,) + parse_variety_dir(v) for v in vdirs]
    parsed = [p for p in parsed if p[2]]           # 只保留可解析的品种目录
    if only:
        parsed = [p for p in parsed if p[2] in only]
    if args.limit_varieties:
        parsed = parsed[:args.limit_varieties]

    print(f"待处理品种目录：{len(parsed)}")
    for v, ex, va in parsed:
        print(f"  {va} ({ex})  {os.path.basename(v)}")
    if args.no_db or not parsed:
        return
    if psycopg2 is None:
        raise SystemExit("缺少 psycopg2：请在 qhyc 宿主 venv 下运行（--no-db 可免库预演）")

    conn = psycopg2.connect(**DB)
    # TimescaleDB 默认限制「单事务解压 10 万行」。覆盖旧源时 INSERT..ON CONFLICT 必须先
    # 解压被命中的旧 segment，而旧 888 序列单品种就有 ~100 万行 → 必然越界
    # （CU 实测：current limit 100000, tuples decompressed 100320，整品种回滚）。
    # 置 0 = 不限制。解压以 chunk 为单位（7 天/块，~31 万行/块），内存可控。
    try:
        with conn.cursor() as _c:
            _c.execute("SET timescaledb.max_tuples_decompressed_per_dml_transaction = 0")
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"  [warn] 未能放宽解压上限（非 TimescaleDB？）：{e}")
    total = 0
    t0 = time.time()
    failed, skipped = [], []
    for i, (v, ex, va) in enumerate(parsed, 1):
        n = load_variety(conn, v, strict_volume=args.strict_volume,
                         backup_old=args.backup_old)
        if n is None:                      # 无数据 / 主动跳过（非故障）
            skipped.append(va)
        else:
            total += n
            if n == 0:
                failed.append(va)
        if i % 5 == 0:
            print(f"[{i}/{len(parsed)}] 累计 {total} 行，{time.time()-t0:.0f}s")
    print(f"DONE 总计 {total} 行，用时 {time.time()-t0:.0f}s"
          f"（跳过 {len(skipped)} 个，失败 {len(failed)} 个）")
    if skipped:
        print(f"SKIP 无数据/主动跳过：{','.join(skipped[:40])}")
    if failed:
        print(f"WARN 零产出品种 {len(failed)} 个（需排查）：{','.join(failed[:30])}")
    conn.close()


if __name__ == "__main__":
    main()
