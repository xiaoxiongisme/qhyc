-- 089_bar_synth_watermark.sql
-- CL-20261125-04：L1 合成真增量水位表，根治派生作业固定 3 天窗口重算的满负荷问题。
--
-- 背景（实证 2026-11-25）
-- ---------------------------------------------------------------------------
-- minute_and_bars 单次 ~1945s、30min 间隔（超 8%）；hourly_collect ~3650s、60min
-- 间隔。每次 purge 4 天 + synth 3 天重算约 57 万行分钟（近 4 天 minute_bar），
-- 而每天实际新增仅 10-24 万行。窗口固定、与新增量脱钩 → 长期满负荷追赶；
-- 线程池被长作业占满 → ④ 短作业（resync/v5x_scan）流水"耗时=调度间隔"的记账假象
-- （实测 resync DB 查询 0.045s，纯属排队等 worker）。
--
-- 为什么可以安全地只重算新增桶
-- ---------------------------------------------------------------------------
-- 会话式切分（synthesizer._session_insert_sql）的桶标签 = seg_date + 09:00/21:00
-- + 固定墙钟边界，**不依赖累计分钟计数**——某一交易日的桶完全由该日分钟决定，
-- 与"从哪天开始读"无关。唯一约束：段边界由 lag() 窗口判断，读取起点必须对齐到
-- 会话起点 09:00（夜盘 02:30 收、日盘 09:00 开，09:00 必在两段之间，从此处起算
-- 不会截断任何段）。现有 _session_floor 即此语义。
--
-- 水位语义（关键）
-- ---------------------------------------------------------------------------
-- covered_until = 上次合成"追平校验通过"时覆盖到的 L0 最新分钟 ts。
-- 下次合成：since = _session_floor(covered_until - overlap_days)。
--   · 正常每轮前进约 30min，窗口收缩到 overlap_days（默认 1 天，覆盖
--     前一交易日夜盘 21:00→次日02:30 跨日 + L0 回补），重算量从 ~62 万降到 ~39 万行
--     （2 个交易日 vs 旧的 3-4 天；实测每天 15-24 万行）。
--   · 作业挂机/漏跑多天 → covered_until 停滞，但 since 仍从该点起算，
--     窗口自动扩到"缺口 + overlap"，不会漏桶（这是相对固定 days_back 的核心优势）。
--   · 追平校验失败 → 水位不推进，下次仍从旧水位重算。
--   · 表内无行（冷启动/首次部署）→ 代码回退到原 days_back=3 保守窗口，
--     行为逐位不变，零风险灰度。
--
-- purge 与 synth 共用同一 since_session（resolve_synth_window 返回），保证
-- "删除范围 ⊆ 重建范围"——否则会出现删了不补的静默丢桶（§36 反复警告的坑）。

BEGIN;

CREATE TABLE IF NOT EXISTS ops.bar_synth_watermark (
    key             text PRIMARY KEY,
    covered_until   timestamptz,             -- 上次成功追平覆盖到的 L0 max(ts)；NULL=未初始化
    overlap_days    numeric(4,2) NOT NULL DEFAULT 1.0,
    reset_days      numeric(4,2) NOT NULL DEFAULT 3.0,  -- 冷启动/缺口回退的保守窗口
    runs            bigint NOT NULL DEFAULT 0,
    last_success_at timestamptz,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    note            text
);

COMMENT ON TABLE ops.bar_synth_watermark IS
    'CL-20261125-04 L1 合成增量水位：单键 l1_bar_synth；无行=冷启动回退3天窗口';
COMMENT ON COLUMN ops.bar_synth_watermark.covered_until IS
    '上次追平校验通过时 _max_minute_ts 的返回值；NULL 表示未初始化';
COMMENT ON COLUMN ops.bar_synth_watermark.overlap_days IS
    '水位回看重叠天数（默认1，覆盖前日夜盘跨日与L0回补）；since=floor(covered_until-overlap)';
COMMENT ON COLUMN ops.bar_synth_watermark.reset_days IS
    '无水位/异常时回退的固定窗口天数（3=改造前行为）';

-- 有意不 INSERT 初始行：冷启动必须由代码走原 days_back 路径，首个成功合成自行落水位。
-- 若手工插入 covered_until=NULL 的行也无妨（代码同样按无水处理）。

COMMIT;
