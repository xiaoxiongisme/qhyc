-- 046_factor_value_lookup_index.sql
-- 为 factor_value 的 asof lookup 热点查询补精确复合索引
--
-- 【问题】
--   app/factor/asof.py FactorContext.lookup 的热点查询：
--     SELECT available_at, z_value FROM factor_value
--     WHERE factor_id=:f AND symbol=:s ORDER BY available_at DESC
--   既有索引没有任何一个能同时命中 factor_id + symbol 过滤与 available_at 排序：
--     factor_value_pkey            (factor_id, trade_date, symbol)  -- 排序列不符
--     idx_fv_factor_date           (factor_id, available_at)        -- symbol 只能作 Filter
--     ix_factor_value_fid_date     (factor_id, trade_date DESC)
--     ix_factor_value_date_symbol  (trade_date, symbol)
--   实测执行计划：Index Scan Backward using idx_fv_factor_date + Filter(symbol)
--   → 必须读尽该 factor 的**全部**行（约 18 万行/因子，含逐行堆取）再过滤。
--   fusion scan 每轮 对每个 (因子, 品种) 组合 cold-cache 查一次（57 品种 × 20 因子
--   ≈ 1140 次）→ 单轮 scan 约 54s 的主要构成。
--
-- 【修法】
--   复合索引 (factor_id, symbol, available_at DESC)：Filter 变 Index Cond，
--   单次查询 0.05~0.1ms / 3 buffers（实测）。
--   生产已用 CREATE INDEX CONCURRENTLY 预建，本迁移仅幂等补齐其他环境。
--
-- 【不分区说明】
--   评估过按时间分区：lookup 全是点查（点查拿不到分区裁剪额外收益），
--   而 260 万行体量下索引已足够；分区带来的路由复杂度与旧数据归档需求
--   不成比例，故只加索引。

CREATE INDEX IF NOT EXISTS ix_factor_value_fid_sym_avail
    ON l2_adj.factor_value (factor_id, symbol, available_at DESC);
