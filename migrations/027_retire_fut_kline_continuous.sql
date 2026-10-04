-- 027_retire_fut_kline_continuous.sql
-- 任务⑤（PRD §14.4 G9 / P1）：退役 fut_kline.continuous（派生品不应留在 L0）。
--
-- 问题（评审 §1.1）：L0 定义"只入库备份、不参与任何计算/合成/复权"，但 fut_kline 同时
--       持有 contract（原始）与 continuous（拼接连续 = 派生品）两种 kind，击穿 L0 边界；
--       且 B9 把默认口径切到 back_adj 后，continuous 变成既冗余又易误用的"第三口径"。
--
-- 处置（加法式，不 DROP、不删存量行）：
--   1) 本迁移只做**语义标记**：在 fut_kline 表注释中声明 continuous 已废弃；
--   2) 停止生成：rebuild_fut_kline.py 默认 --kinds 改为仅 contract（continuous 须显式
--      --kinds continuous,... 才重建），见脚本改动；
--   3) 消费方清零：caliber.ROUTES 中 daily/hourly 的 continuous 改指 daily_bar/hourly_bar
--      （L0 原始主连），不再读 fut_kline.kind='continuous'（见 app/data/caliber.py 改动）；
--   4) 存量 9.9M 行保留不再刷新（orphan，可后续显式 purge，非本次范围）。
COMMENT ON TABLE fut_kline IS
    '行情超表。kind ∈ {contract(原始逐合约), continuous(已废弃派生连续,2026-10-04 G9 退役,'
    '不再刷新), cont_adj(已退役前复权)}。L0 不应含派生 continuous —— 连续序列请用 bar_* + roll_segment '
    '(back_adj) 或 daily_bar/hourly_bar。';

DO $$
DECLARE n bigint;
BEGIN
    SELECT count(*) INTO n FROM fut_kline WHERE kind='continuous';
    RAISE NOTICE '027: fut_kline.continuous 存量 % 行（已废弃，不再刷新；可后续显式 purge）', n;
END $$;
