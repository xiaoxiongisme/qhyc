-- 025_dim_variety_archive_only.sql
-- 任务②：股指标记「仅存档」（PRD §4.2 + 用户指令 2026-10-04）。
--
-- 中金所金融期货（股指期货 IF/IC/IH/IM、国债期货 T/TF/TS/TL）作息与商品期货迥异
-- （09:15 开盘、无夜盘、节假日提前收盘、各所夜盘时段不一），且 G4 已决定日历改期货
-- 专属源。这些品种保留历史数据，但不再参与活跃采集 / 复权 / 信号，故标记为「仅存档」。
--
-- 与 is_active 区分：is_active=false 指退市；is_archive_only=true 指"数据保留但
-- 不主动处理"（如 roll_segment 重建、连续序列物化应跳过）。
ALTER TABLE dim_variety
    ADD COLUMN IF NOT EXISTS is_archive_only boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN dim_variety.is_archive_only IS
    '仅存档：保留历史数据但不再参与活跃采集/复权/信号（如中金所金融期货 IF/IC/IH/IM/T/TF/TS/TL）。'
    'build_roll_segments 等重建作业应跳过 is_archive_only=true 的品种。';

-- 标记 CFFEX 股指期货 / 国债期货为仅存档（dim_variety 基础品种码）
UPDATE dim_variety
   SET is_archive_only = true, updated_at = now()
 WHERE variety_code IN ('IF', 'IC', 'IH', 'IM', 'T', 'TF', 'TS', 'TL')
   AND exchange = 'CFFEX';

-- 幂等校验：确认恰好 8 个被标记（若数量不对则告警，绝不静默）
DO $$
DECLARE n int;
BEGIN
    SELECT count(*) INTO n FROM dim_variety WHERE is_archive_only;
    IF n <> 8 THEN
        RAISE WARNING '025: is_archive_only 标记数=%（预期 8：IF/IC/IH/IM/T/TF/TS/TL），请核查', n;
    ELSE
        RAISE NOTICE '025: 已标记 % 个 CFFEX 金融期货为仅存档', n;
    END IF;
END $$;
