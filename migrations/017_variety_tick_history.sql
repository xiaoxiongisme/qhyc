-- =============================================================================
-- 017 · tick 时序化：品种最小变动价位会随交易所调整而变，按生效日版本化
-- 日期：2026-10-03　用户裁定：tick 自 2026-04 起变化，回测用旧值(2)，当期用新值(1)
-- 依据：新浪财经 2026-04-01 报道（用户提供）
-- 适用：本地库与云端库同构执行（幂等，可重复运行）
--
-- 背景（为何必须时序化）
--   大商所 棕榈油(P) / 豆油(Y) 最小变动价位自 2026-04-01 由 2 元/吨 改为 1 元/吨。
--   两个数据源因此「各对一半」：
--     akshare futures_fees_info (asof 2026-09-30) = 1.0  -> 对应当期
--     symbols.json（早前按旧规则录入）        = 2.0  -> 对应变更前
--   若 tick 只存单值：历史回测用 1.0 会低估滑点/成本；当期交易用 2.0 会价位对齐错误。
--   故 tick 必须像交易成本一样按 effective_from/to 版本化。
--
-- 读取约定：app/data/barstore.py variety_spec(symbol, as_of=...)
--   as_of 缺省 = 今天 -> 取当前有效行（实盘/当期回测）
--   as_of=历史日期   -> 取该日有效行（历史回测**必须显式传**，否则会用当期 tick）
-- =============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS dim_variety_tick_history (
    variety_code   TEXT        NOT NULL,
    effective_from DATE        NOT NULL,
    effective_to   DATE,
    tick_size      NUMERIC(20,10) NOT NULL,
    source         TEXT        NOT NULL,
    note           TEXT,
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_dim_variety_tick PRIMARY KEY (variety_code, effective_from),
    CONSTRAINT ck_tick_range CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE INDEX IF NOT EXISTS ix_dim_variety_tick_lookup
    ON dim_variety_tick_history (variety_code, effective_from DESC);

COMMENT ON TABLE dim_variety_tick_history IS
    '品种最小变动价位(tick)的时效历史。交易所会调整 tick（大商所 P/Y 自 2026-04-01 由 2 改为 1），'
    '故 tick 必须版本化：历史回测取 as_of 当日有效值，当期交易取当前有效行。'
    '单值并存于 dim_variety.tick_size（=当前值冗余，便于既有查询）。';

COMMENT ON COLUMN dim_variety_tick_history.source IS
    '来源：akshare_fees_info=akshare 当期值；user_adjudicated=用户拍板；exchange_notice=交易所公告。';

-- 1) 回填现有 dim_variety 的 tick 为「当前有效」（akshare asof 晚于 2026-04，与当期一致）
INSERT INTO dim_variety_tick_history
    (variety_code, effective_from, effective_to, tick_size, source, note)
SELECT v.variety_code, DATE '2026-04-01', NULL, v.tick_size,
       'akshare_fees_info', '当期值（生效日按 2026-04 变更批处理）'
  FROM dim_variety v
 WHERE v.is_active AND v.variety_code !~ '888$' AND v.tick_size IS NOT NULL
ON CONFLICT (variety_code, effective_from) DO NOTHING;

-- 2) 回填 P/Y 变更前的历史段（2026-04-01 之前 tick = 2.0）
INSERT INTO dim_variety_tick_history
    (variety_code, effective_from, effective_to, tick_size, source, note)
SELECT v.variety_code, DATE '1900-01-01', DATE '2026-04-01', 2.0,
       'user_adjudicated',
       '变更前历史值：大商所 P/Y 最小变动价位 2026-04-01 起由 2 元/吨改为 1 元/吨'
  FROM dim_variety v
 WHERE v.variety_code IN ('P', 'Y') AND v.is_active
ON CONFLICT (variety_code, effective_from) DO UPDATE
    SET tick_size = EXCLUDED.tick_size, effective_to = EXCLUDED.effective_to,
        source = EXCLUDED.source, note = EXCLUDED.note, updated_at = now();

-- 3) 自检：P/Y 应各有两段，且历史段在 2026-04-01 闭合
DO $$
DECLARE
    v_bad TEXT;
BEGIN
    SELECT string_agg(variety_code || '(' || cnt || ')', ',') INTO v_bad
      FROM (SELECT variety_code, count(*) cnt
              FROM dim_variety_tick_history
             WHERE variety_code IN ('P','Y') GROUP BY variety_code) t
     WHERE cnt <> 2;
    IF v_bad IS NULL THEN
        RAISE NOTICE 'tick 时序表自检通过：P/Y 各 2 段';
    ELSE
        RAISE EXCEPTION 'tick 时序表自检失败：% 段数不为 2', v_bad;
    END IF;
END $$;

COMMIT;
