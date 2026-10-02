-- =============================================================================
-- 014 · 滑点口径 + dim_variety 污染清理
-- 日期：2026-10-03　用户拍板：滑点 = 1 个最小变动价位
-- 适用：本地库与云端库同构执行（幂等，可重复运行）
--
-- 1) 滑点：用户 2026-10-03 拍板「滑点 1 个点」→ slip_ticks = 1
--    口径定义：**每边 1 跳**。一次完整开平 = 开仓 1 跳 + 平仓 1 跳 = 2 跳，
--    金额成本 = 2 × slip_ticks × tick_size × multiplier × 手数。
--    （若按「往返 1 跳」计则成本减半，故此处明确为每边，调用方勿再乘 2）
--
-- 2) dim_variety 污染清理：007 从 dim_symbol.product 回填时，把
--    **合约命名空间**与**天勤命名空间**的符号也灌进了品种字典 ——
--    实测 1219 行形如 'DCE.A2011'（合约）、'KQ.m@DCE.A'（天勤）、'INE.SC2211'，
--    它们不是品种码，会污染任何按 variety_code 的查询/连接。
--    处理：置 is_active=false（保留可审计，不物理删除）。
--
--    ⚠ 刻意**不**清理的部分（需用户确认，不可盲删）：
--      * 16 个无中文名的行（BB/BZ/JR/LR/ME/OP/PD/PL/PM/PT/RS/TC/WH/ZC/RI/FB 等）
--      * 重复别名码 PTA(郑商所英文码) vs TA、ME vs MA、OP vs PG、TC vs SC
--        —— 均为交易所并存的合法代码，删错会丢品种。
-- =============================================================================

BEGIN;

-- 1) 滑点 = 1 跳/边
UPDATE dim_trading_cost
   SET slip_ticks = 1,
       note = COALESCE(note, '') || ' | 滑点=1跳/边(用户 2026-10-03 拍板)',
       updated_at = now()
 WHERE slip_ticks IS DISTINCT FROM 1;

COMMENT ON COLUMN dim_trading_cost.slip_ticks IS
    '滑点，单位=最小变动价位(tick)跳数，语义为**每边**。'
    '一次完整开平的成本 = 2 × slip_ticks × tick_size × multiplier × lots。'
    '用户 2026-10-03 拍板取 1 跳/边。';

-- 2) dim_variety 污染清理：命名空间限定码（非品种码）
UPDATE dim_variety
   SET is_active = false,
       source = COALESCE(source, '') || '+deprecated_ns_symbol',
       updated_at = now()
 WHERE is_active
   AND (variety_code LIKE '%.%' OR variety_code LIKE '%@%');

COMMIT;
