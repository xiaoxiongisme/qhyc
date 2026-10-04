-- =============================================================================
-- 011 · 回填 dim_variety 的 exchange / main_symbol（补采链路的硬依赖）
-- 日期：2026-10-01　发现源：scripts/backfill_bars_2026.py 实跑报 NotNullViolation
-- 适用：本地库与云端库同构执行（幂等，可重复执行）
--
-- ---------------------------------------------------------------------------
-- 【问题现象】（2026-10-01 实测）
--   补采 IF/BB/TS 时报：
--       (psycopg.errors.NotNullViolation) null value in column "symbol"
--       of relation "_hyper_68_5611_chunk" violates not-null constraint
--   根因：`_resolve()` 取 `dim_variety.main_symbol` 作为 upsert 的 symbol，而该列为 NULL。
--
-- 【字典现状】（1373 行，2026-10-01 实测）
--       main_symbol IS NULL : 1300 行
--       exchange    IS NULL : 1222 行
--   字典存在**两种命名形态**，且有效信息只在带 888 的那批上：
--       variety_code='RB'    → exchange=SHFE 但 main_symbol=NULL     ← 空壳（1300 行）
--       variety_code='RB888' → exchange=SHFE, main_symbol='RB888'    ← 有值（73 行）
--   84 个待补品种中：**main_symbol 缺失 84（全部）、exchange 缺失 10**。
--
-- 【修复】两段式回填（均为幂等 UPDATE，只填空不覆盖）
--   1) 同名回填：'RB' ← 'RB888' 的 exchange / main_symbol（命中 73 个）
--   2) 手工回填：'RB888' 条目也不存在的 11 个冷门品种，按 天勤 实测可达性补交易所。
--      其中 ME（甲醇旧码）、TC（动力煤旧码）经 天勤 探测确认**合约已废**
--      （graphql 报错 / K 线超时），仅补字典以便历史数据可追溯，采集时会 fail 并被跳过。
--      交易所归属经 2026-10-01 天勤 实拉验证（scripts/_probe_missing_varieties.py）：
--        DCE.bb / DCE.fb / CZCE.JR / CZCE.LR / CZCE.RI / CZCE.PM
--        CZCE.RS / CZCE.WH / CZCE.ZC  → 均可拉到 2026-09-30 数据
--        （CZCE.LR 仅到 2026-01-16）
--
-- 【回滚】
--   UPDATE dim_variety SET exchange=NULL, main_symbol=NULL, source=NULL
--    WHERE source='011_backfill';
-- =============================================================================

BEGIN;

-- 1) 同名回填：X ← X888（只填 NULL，不覆盖既有值）
UPDATE dim_variety d
SET exchange    = s.exchange,
    main_symbol = s.main_symbol,
    source      = '011_backfill',
    updated_at  = now()
FROM dim_variety s
WHERE s.variety_code = d.variety_code || '888'
  AND d.main_symbol IS NULL
  AND s.main_symbol IS NOT NULL;

-- 2) 手工回填：无 X888 条目的冷门品种（交易所经 天勤 实测）
UPDATE dim_variety SET exchange='DCE',  main_symbol='BB888', source='011_backfill', updated_at=now() WHERE variety_code='BB';
UPDATE dim_variety SET exchange='DCE',  main_symbol='FB888', source='011_backfill', updated_at=now() WHERE variety_code='FB';
UPDATE dim_variety SET exchange='CZCE', main_symbol='JR888', source='011_backfill', updated_at=now() WHERE variety_code='JR';
UPDATE dim_variety SET exchange='CZCE', main_symbol='LR888', source='011_backfill', updated_at=now() WHERE variety_code='LR';
UPDATE dim_variety SET exchange='CZCE', main_symbol='RI888', source='011_backfill', updated_at=now() WHERE variety_code='RI';
UPDATE dim_variety SET exchange='CZCE', main_symbol='PM888', source='011_backfill', updated_at=now() WHERE variety_code='PM';
UPDATE dim_variety SET exchange='CZCE', main_symbol='RS888', source='011_backfill', updated_at=now() WHERE variety_code='RS';
UPDATE dim_variety SET exchange='CZCE', main_symbol='WH888', source='011_backfill', updated_at=now() WHERE variety_code='WH';
UPDATE dim_variety SET exchange='CZCE', main_symbol='ZC888', source='011_backfill', updated_at=now() WHERE variety_code='ZC';
-- 以下两个合约经 天勤 探测已废（拉不到），仅补字典供历史数据追溯
UPDATE dim_variety SET exchange='CZCE', main_symbol='ME888', source='011_backfill_delisted', updated_at=now() WHERE variety_code='ME';
UPDATE dim_variety SET exchange='CZCE', main_symbol='TC888', source='011_backfill_delisted', updated_at=now() WHERE variety_code='TC';

COMMIT;
