-- =============================================================================
-- 034_l0_minute_bar_extend.sql
-- L0 分钟层「本地 CSV 取代」：扩展 minute_bar + 提供 8888 指数连合成过程
--
-- 设计依据：docs/L0_本地CSV取代设计.md
-- 兼容策略：原 15 列不动（barstore / sp_build_l1/l2 / validator 零改动），
--           仅追加分层/口径/来源元数据列。
--
-- 部署：在 qhyc 宿主 venv 环境、对 timescaledb 执行：
--   psql "postgres://futures:${POSTGRES_PASSWORD}@timescaledb:5432/futures" \
--        -v ON_ERROR_STOP=1 -f migrations/034_l0_minute_bar_extend.sql
-- 回滚：DROP PROCEDURE IF EXISTS sp_synth_8888_from_contracts;
--       ALTER TABLE <base>.minute_bar DROP COLUMN IF EXISTS ts_end, layer, ...;
--
-- 注意：本迁移只做 DDL，**不改存量数据**。加列后旧行（akshare 等）的
--       variety_code / exchange 为 NULL，按这两列过滤会静默漏掉旧数据。
--       如需回填，另行执行（一次性全表扫描，约 119M 行，数分钟）：
--           psql "$PGURI" -v ON_ERROR_STOP=1 -f scripts/backfill_l0_meta.sql
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1) 扩展 minute_bar（自动定位 base 表：l0_raw 已分层则用 l0_raw，否则 public）
--    普通表（非超表）：20y×~170 symbol 下 7 天分块会炸，PK 索引足够，不建超表。
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    v_base text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='l0_raw' AND tablename='minute_bar') THEN
        v_base := 'l0_raw.minute_bar';
    ELSIF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='minute_bar') THEN
        v_base := 'public.minute_bar';
    ELSE
        RAISE EXCEPTION '034: 找不到 minute_bar（l0_raw / public 均无），请先跑 009 基础层';
    END IF;

    EXECUTE format('ALTER TABLE %s ADD COLUMN IF NOT EXISTS ts_end        timestamptz', v_base);
    EXECUTE format('ALTER TABLE %s ADD COLUMN IF NOT EXISTS layer         text NOT NULL DEFAULT ''continuous''', v_base);
    EXECUTE format('ALTER TABLE %s ADD COLUMN IF NOT EXISTS exchange      text', v_base);
    EXECUTE format('ALTER TABLE %s ADD COLUMN IF NOT EXISTS variety_code  text', v_base);
    EXECUTE format('ALTER TABLE %s ADD COLUMN IF NOT EXISTS caliber_vol   text NOT NULL DEFAULT ''single_side''', v_base);
    EXECUTE format('ALTER TABLE %s ADD COLUMN IF NOT EXISTS caliber_amt   text NOT NULL DEFAULT ''true''', v_base);
    EXECUTE format('ALTER TABLE %s ADD COLUMN IF NOT EXISTS amount_raw    numeric', v_base);
    EXECUTE format('ALTER TABLE %s ADD COLUMN IF NOT EXISTS raw_type      int', v_base);

    -- 索引：分层查询加速（按 symbol+layer 定位主连/合约/指数）
    EXECUTE format('CREATE INDEX IF NOT EXISTS ix_mm_sym_layer ON %s (symbol, layer)', v_base);

    RAISE NOTICE '[034] minute_bar 已扩展（base=%）', v_base;
END $$;

-- ---------------------------------------------------------------------------
-- 2) sp_synth_8888_from_contracts ——【已废弃 / 安全占位】
--    2026-11 修正：8888 指数连改为「back-adjusted 连续合成」（剔除换月 carry 跳变），
--    由 load_local_csv_1min.py 在加载时直接写入 minute_bar（layer='index'）。
--    原因：等权/量加权合成会使 8888 在换月瞬间与主力同步跳空，导致
--    sp_build_l2_roll_segment 的 ratio 门(abs(idx_delta)<0.4|cc|) 恒失败、
--    roll_segment 整品种空、back_adj 崩坏（RB/CU/IC 实测 0 段）。back-adjusted 连续
--    合成使 8888 在换月处平滑、真实涨跌处仍如实跳动，实测 RB=43 / CU=20 / IC=56 段。
--    因此本过程改为【只告警、不动数据】，防止误跑把正确 8888 覆盖成错误等权数据。
--    正确 8888 由 loader 生成；若需重建请重跑 loader，而非本过程。
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE sp_synth_8888_from_contracts(p_variety text DEFAULT NULL)
LANGUAGE plpgsql AS $$
BEGIN
    RAISE NOTICE '[034] sp_synth_8888_from_contracts 已废弃：8888 由 load_local_csv_1min.py 以 '
                  'back-adjusted 连续法写入（layer=''index''）。本过程不再合成，避免覆盖正确数据。';
END $$;

COMMENT ON PROCEDURE sp_synth_8888_from_contracts(text) IS
'【废弃】8888 指数连改由 load_local_csv_1min.py 以 back-adjusted 连续法合成并写入；本过程仅告警，不再生成数据。';
