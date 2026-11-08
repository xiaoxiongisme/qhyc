-- =====================================================================
-- 061: 清理 TEST-B8 假品种测试单（把 pending 从 30 归零）
--      CL-20261109-09
-- ---------------------------------------------------------------------
-- 一、为什么
--
--   2026-11-09 实测 `public.execution_order` 只有 30 行，全部是：
--     · symbol_continuous = 'TEST-B8-*'（假品种，真实交易所里不存在）
--     · action = CLOSE_YEST / direction = SELL
--     · status = 'NEW'、filled_lots = 0
--     · 最早 2026-10-05，最晚 2026-11-06
--   `execution_fill` 里 0 行、`execution_position` 里 0 行，说明它们
--   从来没有被桥认领过，也没有产生任何成交或持仓——纯粹是 B8 演练的残留。
--
--   危害不是"占空间"，而是**让健康页说谎**：
--   `GET /execution/bridge/health` 的 `pending` 判据是
--   `SELECT count(*) FROM execution_order WHERE status = 'NEW'`。
--   于是 pending 恒为 30，将来任何人（包括接桥的人）打开健康页，
--   都会以为有 30 张单在排队等认领。
--
-- 二、怎么处理
--
--   **不物理删除**。改 `status='CANCELED'` 并在 `close_reason` 写明来由：
--     · 删了就再也无法回答"那 30 行是什么"；
--     · CANCELED 是状态机里既有的终态（CHECK 允许），桥不会再认领；
--     · pending 判据只看 NEW，改完自然归零。
--
--   幂等：条件带 `status='NEW'`，重复执行影响 0 行。
-- =====================================================================

BEGIN;

UPDATE public.execution_order
   SET status       = 'CANCELED',
       close_reason = 'TEST_FIXTURE_CLEANUP CL-20261109-09',
       updated_at   = now()
 WHERE status = 'NEW'
   AND symbol_continuous LIKE 'TEST-B8%';

COMMIT;
