-- =============================================================================
-- 002 · 因子注册补全 + 幽灵注册治理 + 首批准入外键
-- 日期：2026-09-29　Phase 1（数据层）/ Phase 4（因子层 T13）前置
-- 适用：本地库与云端库同构执行（幂等）
--
-- 实测问题（2026-09-29）
--   1. factor_value 共 19 个因子、211 万行，其中 **185.6 万行（88%）是孤儿**
--      —— 对应 11 个 A 组因子（f_close_loc / f_mom_1h / f_gap / f_atr_pct …）
--      在 factor_registry 里**从未注册**；
--   2. 反向问题：registry 有 3 个「幽灵注册」——roll_yield_z / spot_mom_z /
--      structure_slope_z 已注册但 factor_value 里**零数据**（源表缺失）；
--   3. max_weight 额度已用 0.88/1.0（11 项 × 0.08），而 validate_max_weight
--      **不区分 enabled**，直接注册 15 个新因子会突破 1.0 → 服务启动失败。
--
-- 处置
--   * 补齐 11 个 A 组因子注册（enabled=false，PRD 铁律：注册即中性）
--   * 预注册 V1/V6 的 4 个新因子（f_vol_ratio / f_vol_z / f_voldiv_divergence /
--     f_atr_pctile），enabled=false，待 T14 计算 + E1–E6 准入后才可开启
--   * 3 个幽灵注册 max_weight 归零并 enabled=false，释放 0.24 额度
--     最终：0.88 − 0.24 + 15×0.02 = 0.94 ≤ 1.0 ✅ 启动校验可过
--   * 注册补全后建立 factor_value → factor_registry 外键（父表仅 26 行，可常驻内存）
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. 幽灵注册治理：无数据源的注册项权重归零并关闭（释放 max_weight 额度）
-- ---------------------------------------------------------------------------
UPDATE factor_registry
   SET enabled = false,
       max_weight = 0,
       default_weight = 0,
       description = coalesce(description, '') || ' ｜[2026-09-29] 源表无数据，权重归零并停用，待补数后重估'
 WHERE factor_id IN ('roll_yield_z', 'spot_mom_z', 'structure_slope_z');

-- ---------------------------------------------------------------------------
-- 2. 补全 11 个 A 组因子注册（已有 factor_value 数据，enabled=false 初始）
--    category=A（价格/量能派生），data_sources={bar_15m}，lag_days=0（盘内即知）
-- ---------------------------------------------------------------------------
INSERT INTO factor_registry
  (factor_id, name, category, description, data_sources,
   min_history_days, horizon, lag_days, enabled, default_weight, max_weight, version)
VALUES
  ('f_close_loc',    '收盘位置 A',   'A', '收盘价在当日高低区间中的相对位置（bar_15m 派生）',            '{bar_15m}', 60, 'A', 0, false, 0, 0.02, '1.0'),
  ('f_close_loc14',  '收盘位置14',   'A', '14 日窗口收盘位置（bar_15m 派生）',                            '{bar_15m}', 60, 'A', 0, false, 0, 0.02, '1.0'),
  ('f_mom_1h',       '1小时动量',    'A', '近 1 小时收益率动量（bar_15m 派生）',                          '{bar_15m}', 60, 'A', 0, false, 0, 0.02, '1.0'),
  ('f_mom_4h',       '4小时动量',    'A', '近 4 小时收益率动量（bar_15m 派生）',                          '{bar_15m}', 60, 'A', 0, false, 0, 0.02, '1.0'),
  ('f_gap',          '跳空幅度',     'A', '开盘相对前收的跳空幅度（bar_15m 派生）',                        '{bar_15m}', 60, 'A', 0, false, 0, 0.02, '1.0'),
  ('f_atr_pct',      'ATR占比',      'A', 'ATR(14)/close 波动率占比（bar_15m 派生，IC 0.0157）',           '{bar_15m}', 60, 'A', 0, false, 0, 0.02, '1.0'),
  ('f_vol_surge',    '量能突进',     'A', '成交量相对基准的突进倍数（bar_15m 派生）',                      '{bar_15m}', 60, 'A', 0, false, 0, 0.02, '1.0'),
  ('f_amt_conc',     '成交额集中度', 'A', '成交额在时间上的集中度（bar_15m 派生）',                        '{bar_15m}', 60, 'A', 0, false, 0, 0.02, '1.0'),
  ('f_oi_dir',       '持仓方向',     'A', '持仓量变化的方向性（bar_15m 派生）',                            '{bar_15m}', 60, 'A', 0, false, 0, 0.02, '1.0'),
  ('f_range_comp',   '区间压缩',     'A', '价格区间相对历史的压缩程度（bar_15m 派生）',                    '{bar_15m}', 60, 'A', 0, false, 0, 0.02, '1.0'),
  ('f_vwap_dev',     'VWAP偏离',     'A', '收盘价相对成交均价 VWAP 的偏离（bar_15m 派生）',                '{bar_15m}', 60, 'A', 0, false, 0, 0.02, '1.0')
ON CONFLICT (factor_id) DO UPDATE
   SET data_sources = EXCLUDED.data_sources,
       category     = EXCLUDED.category,
       horizon      = EXCLUDED.horizon;

-- ---------------------------------------------------------------------------
-- 3. 预注册 V1/V6 新因子（架构设计_CB执行版 T14/T15）
--    当前无 factor_value 数据，须先跑 compute_factor_value.py 再评估
-- ---------------------------------------------------------------------------
INSERT INTO factor_registry
  (factor_id, name, category, description, data_sources,
   min_history_days, horizon, lag_days, enabled, default_weight, max_weight, version)
VALUES
  ('f_vol_ratio',          '量比 VR',       'A', 'V1：vol_t / median(vol,20)，>1.5 放量 <0.5 缩量',              '{bar_15m}', 60, 'A', 0, false, 0, 0.02, '1.0'),
  ('f_vol_z',              '量标准分 VZ',   'A', 'V1：成交量滚动40日 z-score，>2 极端放量',                       '{bar_15m}', 60, 'A', 0, false, 0, 0.02, '1.0'),
  ('f_voldiv_divergence',  '量价背离',      'A', 'V1 路径B：价格创新高/新低而 VZ 未同步，连续≥2 根（离场预警）',   '{bar_15m}', 60, 'A', 0, false, 0, 0.02, '1.0'),
  ('f_atr_pctile',         'ATR分位',       'A', 'V6：ATR 在 250 日中的分位数，>90% 降仓 <10% 警戒',              '{bar_15m}', 60, 'A', 0, false, 0, 0.02, '1.0')
ON CONFLICT (factor_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 4. 额度自检：全因子 max_weight 之和必须 ≤ 1.0，否则回滚并报错
--    （对应 app/factor/asof.py:validate_max_weight，cap=1.0）
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    s numeric;
BEGIN
    SELECT sum(max_weight) INTO s FROM factor_registry;
    IF s > 1.0 THEN
        RAISE EXCEPTION 'max_weight 之和 % 超过上限 1.0，已回滚（请先降低既有因子权重）', round(s, 4);
    END IF;
    -- 注意：PL/pgSQL 的 RAISE 只支持 % 占位符，不支持 %.4f 这类格式说明
    RAISE NOTICE 'max_weight 之和 = % / 1.0 OK 启动校验可过', round(s, 4);
END $$;

COMMIT;

-- ---------------------------------------------------------------------------
-- 5. 外键（父表极小，可常驻内存；写入放大可忽略）
--    注意：亿级行情表（minute_bar / bar_* / fut_kline）**不加外键**，
--          原因见 001 迁移头部说明。此处只加中低频表。
-- ---------------------------------------------------------------------------
BEGIN;
-- 幂等：约束已存在则跳过（本迁移可能被重复应用，见 db_apply_migrations.py）
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_factor_value_registry') THEN
        ALTER TABLE factor_value
            ADD CONSTRAINT fk_factor_value_registry
            FOREIGN KEY (factor_id) REFERENCES factor_registry(factor_id)
            ON UPDATE CASCADE ON DELETE RESTRICT;
        RAISE NOTICE 'factor_value → factor_registry 外键已建立';
    ELSE
        RAISE NOTICE 'factor_value → factor_registry 外键已存在，跳过';
    END IF;
END $$;
COMMIT;

BEGIN;
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='backtest_detail')
       AND EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='backtest_result') THEN
        IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='fk_bt_detail_result') THEN
            ALTER TABLE backtest_detail
                ADD CONSTRAINT fk_bt_detail_result
                FOREIGN KEY (run_id, symbol, model)
                REFERENCES backtest_result(run_id, symbol, model)
                ON DELETE CASCADE;
            RAISE NOTICE 'backtest_detail → backtest_result 外键已建立';
        END IF;
    END IF;
EXCEPTION WHEN OTHERS THEN
    -- 存在历史孤儿明细行时建不上：只告警，不阻断（清理后重跑本迁移即可）
    RAISE WARNING 'backtest_detail 外键跳过：%', SQLERRM;
END $$;
COMMIT;
