-- 019_pct_unit_to_wanfen.sql
-- 把 dim_trading_cost 中 PCT 费率的**单位**从「千分之一(‰)」改为「万分之一(‱)」。
--
-- 背景（用户 2026-10-03 拍板，取方案 B）：
--   交易所手续费通知里比例值的原文写法是「N%」（CSV 中落库为 '1%%'、'0.2%%' …），
--   其含义是**万分之 N**。此前按千分之(‰)解释，全库比例值成本被高估 10 倍。
--   铁证：纯苯 BZ 名义成交额 6231 元/吨 × 30 吨 = 186,930 元/手，
--         文档明确「万分之1 → 18.69 元/边」；按 ‱ 解释 186,930×1/10000=18.69 完全吻合，
--         按 ‰ 解释则为 186.93，与交易所口径不符。
--   佐证：改用 ‱ 后，交易所通知与第三方文档在 BZ 上首次一致（均为 1.0）。
--   而 akshare 的费率表按 ‰ 记（RB 6.07、AD 0.51 等），故其**比例值不可用**，
--   但其**固定值**部分仍有效（FG 6 / PK 4 / AP平今 20 均与官方一致）。
--
-- 影响面：所有按成交额计费的品种（RB/I/J/MA/SA/PX/U/RB/JD/JM/IU/…约 25 个），
--         以及全部历史回测的成本假设 → 修正后回测净利会**上升**（成本 previously 偏高）。
--
-- 语义约定（改后）：
--   fee_type='PCT' 的 fee_value 以 **‱(1/10000)** 为单位，数值与交易所通知原文 1:1 对应
--   （通知「1%」→ 1.0，通知「0.2%」→ 0.2）。
--   ⚠ 同步修改 app/data/cost.py 的 PCT_DIVISOR（1000 → 10000），两者必须同时生效。

-- ── 1) 需要重标定的行：doc_20260627 的 BZ（此前按 ‰ 写成 0.1）→ 改为 1.0 ──
--    其余来源（交易所通知 / 郑交所官方）入库时即为通知原文数值（1.0 / 0.2 / 4.0…），
--    在 ‱ 语义下**天然正确**，无需改动。
UPDATE dim_trading_cost
SET fee_value = fee_value * 10.0,
    exchange_fee_value = exchange_fee_value * 10.0,
    note = COALESCE(note, '') || ' | 019: 按‱(万分之一)重标定',
    updated_at = now()
WHERE fee_type = 'PCT'
  AND source IN ('doc_20260627')
  AND fee_value < 1.0;

-- ── 2) 校验：改后不应再有 ‰ 语义遗留的 PCT 值（通知原文最小值为 0.1‱ 的 RB）──
DO $$
DECLARE bad int;
BEGIN
    SELECT count(*) INTO bad
    FROM dim_trading_cost
    WHERE fee_type = 'PCT' AND effective_to IS NULL AND fee_value > 100;
    IF bad > 0 THEN
        RAISE EXCEPTION 'PCT 值异常(>100): % 行，请检查单位是否重复换算', bad;
    END IF;
END $$;

COMMENT ON TABLE dim_trading_cost IS
    '费率字典。fee_type=FIXED 单位元/手；fee_type=PCT 单位‱(万分之一)，'
    '数值与交易所通知原文「N%」1:1 对应（019 起）。'
    '任一 (variety_code,instrument_kind,action,scope_kind) 只允许一条 effective_to IS NULL 的行（018）。';
