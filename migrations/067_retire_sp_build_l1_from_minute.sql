-- 067_retire_sp_build_l1_from_minute.sql
-- CL-20261109-14：弃用 L1 旧网格派生存储过程，收敛为单一生产者
--
-- 为什么是「弃用」而不是「改造」
-- ------------------------------
-- sp_build_l1_from_minute 的核心是 `time_bucket($1, ts) GROUP BY symbol, time_bucket(...)`，
-- time_bucket 给的是**桶左沿** → 天生产出旧网格（09:00/10:00/11:00/13:00…），
-- 与回测/盘面使用的会话网格（10:00/11:15/14:15/15:00）不是一套口径。
--
-- 想把它「改成会话网格」需要同时移植三样东西：
--   ① 会话边界 CASE（_SESSION_DAY_BOUNDS，商品日盘 09:00 起、跨两次休市）
--   ② 多源去重（DISTINCT ON (symbol, ts_n) + src 优先级）—— 不做的话重叠期
--      volume/amount 直接翻倍，且 min(low) 会吃到另一源的脏值（2026-10-31 实测
--      该根多出 856、low 2940 vs 真实 3545、呈 -17% 假暴跌）
--   ③ 末根时间戳归一（终点约定源 -1min）
-- 也就是把 app/ingest/synthesizer.py 里约 100 行逻辑在 plpgsql 里重写一遍，
-- 连 src 清单都要再抄一份 —— 而这正是今天连续踩两次的坑（「同一份真源抄多份」）。
--
-- 因此选择：**弃用**。库层不再保留第二个 L1 生产者，唯一生产者是
-- app/ingest/synthesizer.py::synthesize_bars_incremental（日增量）
-- 与 scripts/validate/rebuild_bars_session.py（历史重建）。
--
-- 保留过程名并改为「响亮失败」，目的有二：
--   1. 若有人把 config.ingest.bar_synth_mode 切回 "sp" 做「回滚」，
--      当晚就会拿到明确异常，而不是**静默写入旧网格**（那才是真事故）；
--   2. 调用点（scripts/load_local_csv_1min.py 注释、scripts/cloud_preflight/
--      03_sp_l1_fixture.sql）在跑起来时会立刻看到指向新入口的提示。
--
-- 回滚：本迁移可用 migrations/053 的该过程体原样恢复，但恢复即恢复旧网格，
--       除排障对照外不应恢复。

-- 单参数旧重载（009 曾 DROP 过，防御性再删一次）
DROP PROCEDURE IF EXISTS public.sp_build_l1_from_minute(text);

CREATE OR REPLACE PROCEDURE public.sp_build_l1_from_minute(
    p_freq text DEFAULT 'min15',
    p_from timestamptz DEFAULT NULL,
    p_to   timestamptz DEFAULT NULL)
LANGUAGE plpgsql AS $$
BEGIN
    -- SESSION_GRID_RETIRED  ← 守卫 MIG-PROC-BASELINE 以此 token 判「本过程仍是弃用态」，
    --                          若有人用旧迁移覆盖回 time_bucket 版本，token 消失即判红。
    RAISE EXCEPTION
        'sp_build_l1_from_minute 已弃用（migrations/067, CL-20261109-14）：'
        '它用 time_bucket 自然整点切桶，产出旧网格（09:00/10:00/11:00/13:00），'
        '与回测/盘面使用的会话网格（10:00/11:15/14:15/15:00）不是一套口径。'
        'L1 的唯一生产者是 app/ingest/synthesizer.py（日增量，'
        'config.ingest.bar_synth_mode="python"）与 '
        'scripts/validate/rebuild_bars_session.py（历史重建）。'
        '历史重建请用：python scripts/ops/rebuild_l1_grid.py --symbols ... --start ... --end ... --apply'
        '（调用参数 p_freq=%，p_from=%，p_to=%）',
        p_freq, p_from, p_to;
END;
$$;

COMMENT ON PROCEDURE public.sp_build_l1_from_minute(text, timestamptz, timestamptz) IS
    '【已弃用 · 调用即报错】CL-20261109-14：旧 time_bucket 网格会与回测口径冲突。'
    'L1 唯一生产者 = app/ingest/synthesizer.py（日增量）+ '
    'scripts/validate/rebuild_bars_session.py（历史重建，驱动 scripts/ops/rebuild_l1_grid.py）。'
    '保留空壳是为了让误用「响亮失败」而不是静默写错网格。';
