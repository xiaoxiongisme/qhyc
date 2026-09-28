-- =============================================================================
-- 003 · symbol 解析扩展 + 孤儿明细清理 + 补齐剩余外键
-- 日期：2026-09-29　Phase 1（数据层）
--
-- 背景（001 迁移执行后实测）
--   1. dim_symbol 回填 5540 个符号，其中 3209 个落在 namespace='other'
--      —— 实测样例为 `SHFE.au2601` / `DCE.jm2203` / `CZCE.CF009`，
--      即「交易所.品种年月」格式（品种小写），001 的正则未覆盖；
--   2. backtest_detail → backtest_result 外键因存在孤儿明细行建不上
--      （明细引用了不存在的回测主记录）—— 属历史脏数据，应清理。
-- =============================================================================

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. 扩展解析函数：新增「交易所.品种年月」格式
--      ^([A-Z]{4,5})\.([A-Za-z]+)(\d{3,4})$  → contract，exchange=$1，product=upper($2)
--      例：SHFE.au2601 / DCE.jm2203 / CZCE.CF009
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION dim_symbol_parse(s text)
RETURNS TABLE(product text, namespace text, exchange text) AS $$
DECLARE
    m text[];
BEGIN
    -- 天勤原生连续码：KQ.m@DCE.A
    IF s ~ '^KQ\.m@[A-Z]+\.[A-Z0-9]+$' THEN
        m := regexp_match(s, '^KQ\.m@([A-Z]+)\.([A-Z0-9]+)$');
        RETURN QUERY SELECT upper(m[2]), 'tqsdk'::text, m[1];
    -- 交易所.品种年月：SHFE.au2601 / CZCE.CF009
    ELSIF s ~ '^[A-Z]{4,5}\.[A-Za-z]+[0-9]{3,4}$' THEN
        m := regexp_match(s, '^([A-Z]{4,5})\.([A-Za-z]+)([0-9]{3,4})$');
        RETURN QUERY SELECT upper(m[2]), 'contract'::text, m[1];
    -- 指数连：A8888
    ELSIF s ~ '^[A-Za-z0-9]+8888$' THEN
        m := regexp_match(s, '^([A-Za-z0-9]+)8888$');
        RETURN QUERY SELECT upper(m[1]), 'index'::text, NULL::text;
    -- 主连：A888
    ELSIF s ~ '^[A-Za-z]+888$' THEN
        m := regexp_match(s, '^([A-Za-z]+)888$');
        RETURN QUERY SELECT upper(m[1]), 'main'::text, NULL::text;
    -- 标准合约：AP2701
    ELSIF s ~ '^[A-Za-z]+[0-9]{4}$' THEN
        m := regexp_match(s, '^([A-Za-z]+)([0-9]{4})$');
        RETURN QUERY SELECT upper(m[1]), 'contract'::text, NULL::text;
    ELSE
        RETURN QUERY SELECT upper(s), 'other'::text, NULL::text;
    END IF;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- 对已回填的 other 行重新解析（不动主键，只修 product/namespace/exchange）
UPDATE dim_symbol d
   SET product = p.product,
       namespace = p.namespace,
       exchange = coalesce(d.exchange, p.exchange),
       updated_at = now()
  FROM (SELECT symbol, (dim_symbol_parse(symbol)).* FROM dim_symbol WHERE namespace='other') p
 WHERE d.symbol = p.symbol
   AND (d.namespace, coalesce(d.product,'')) IS DISTINCT FROM (p.namespace, p.product);

-- 重算 is_preferred（规则同 001）
UPDATE dim_symbol SET is_preferred = false;
UPDATE dim_symbol d SET is_preferred = true
WHERE d.namespace IN ('main','index')
  AND NOT EXISTS (SELECT 1 FROM dim_symbol o
                  WHERE o.product=d.product AND o.namespace=d.namespace AND o.symbol < d.symbol);
UPDATE dim_symbol d SET is_preferred = true
WHERE d.namespace = 'contract'
  AND NOT EXISTS (SELECT 1 FROM dim_symbol o
                  WHERE o.product=d.product AND o.namespace='contract' AND o.symbol < d.symbol);
UPDATE dim_symbol d SET is_preferred = true
WHERE d.namespace = 'tqsdk'
  AND NOT EXISTS (SELECT 1 FROM dim_symbol o
                  WHERE o.product=d.product AND o.namespace='main');

COMMIT;

-- ---------------------------------------------------------------------------
-- 2. 清理 backtest_detail 孤儿明细（引用了不存在的 backtest_result）
--    这是历史脏数据：主记录被删/未落库但明细残留。
-- ---------------------------------------------------------------------------
BEGIN;
DO $$
DECLARE
    n bigint;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='backtest_detail')
       AND EXISTS (SELECT 1 FROM pg_tables WHERE schemaname='public' AND tablename='backtest_result') THEN
        -- 先记录数量，再删除
        SELECT count(*) INTO n
          FROM backtest_detail d
         WHERE NOT EXISTS (SELECT 1 FROM backtest_result r
                            WHERE r.run_id=d.run_id AND r.symbol=d.symbol AND r.model=d.model);
        IF n > 0 THEN
            DELETE FROM backtest_detail d
             WHERE NOT EXISTS (SELECT 1 FROM backtest_result r
                                WHERE r.run_id=d.run_id AND r.symbol=d.symbol AND r.model=d.model);
            RAISE NOTICE '已清理 backtest_detail 孤儿明细 % 行', n;
        ELSE
            RAISE NOTICE 'backtest_detail 无孤儿明细';
        END IF;

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
    RAISE WARNING 'backtest_detail 处理跳过：%', SQLERRM;
END $$;
COMMIT;
