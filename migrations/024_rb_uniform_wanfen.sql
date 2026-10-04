-- 024_rb_uniform_wanfen.sql
-- 用户 2026-10-04 裁定：「RB 统一按万分之一计算」。
--
-- 背景：此前 RB 沿用交易所通知的两档结构
--     ALL       = 0.2‱  （一般合约，如 rb2609/rb2611）
--     CONTRACTS = 1.0‱  （1、5、10 合约 & 2602-2604）
-- 用户明确要求**不分档、全部按万分之一**，与上期所〔2024〕300 号文
-- 「投机交易统一按成交金额万分之一」一致（该文沿用至 2026）。
--
-- 实测影响：主力 rb2610 本就命中 CONTRACTS=1‱（3.08 元/边，与文档算例 3.07 吻合），
--           改后**主力不变**；变化的是一般合约 rb2609/rb2611：0.62 → 3.08 元/边。
--
-- 执行：闭合 RB 全部 CONTRACTS 档，把 ALL 提升为 1‱。
-- 幂等：重复执行时 ALL 已为 1.0，UPDATE 影响 0 行。

-- ⚠ 所有语句必须限定 instrument_kind='FUTURE'：通知里还有「螺纹钢期权 1.5」等
--   **期权行**（同为 RB、同样是 ALL 档），不限品种类型会误改期权费率。

UPDATE dim_trading_cost
SET effective_to = CURRENT_DATE,
    note = COALESCE(note, '') || ' | 024: RB 统一万分之一(用户裁定), 取消分档',
    updated_at = now()
WHERE variety_code = 'RB' AND instrument_kind = 'FUTURE'
  AND effective_to IS NULL
  AND scope_kind = 'CONTRACTS';

UPDATE dim_trading_cost
SET fee_value = 1.0,
    exchange_fee_value = 1.0,
    source = 'shfe_2024_300_uniform',
    note = COALESCE(note, '') || ' | 024: RB 统一万分之一(用户 2026-10-04 裁定, 依上期所〔2024〕300号文)',
    updated_at = now()
WHERE variety_code = 'RB' AND instrument_kind = 'FUTURE'
  AND effective_to IS NULL
  AND scope_kind = 'ALL'
  AND action IN ('OPEN', 'CLOSE_YEST', 'CLOSE_TODAY')
  AND fee_type = 'PCT'
  AND fee_value <> 1.0;

DO $$
DECLARE bad int; dup int;
BEGIN
    SELECT count(*) INTO bad FROM dim_trading_cost
    WHERE variety_code = 'RB' AND instrument_kind = 'FUTURE'
      AND effective_to IS NULL AND fee_type = 'PCT' AND fee_value <> 1.0;
    IF bad > 0 THEN
        RAISE EXCEPTION 'RB 仍有非万分之一的有效行 % 条', bad;
    END IF;
    SELECT count(*) INTO dup FROM (
        SELECT action, scope_kind FROM dim_trading_cost
        WHERE variety_code = 'RB' AND instrument_kind = 'FUTURE'
          AND effective_to IS NULL
        GROUP BY 1,2 HAVING count(*) > 1) t;
    IF dup > 0 THEN
        RAISE EXCEPTION 'RB 期货出现重复有效键 % 个', dup;
    END IF;
END $$;
