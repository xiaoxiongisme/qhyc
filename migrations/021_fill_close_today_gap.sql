-- 021_fill_close_today_gap.sql
-- 补齐 CLOSE_TODAY 缺口 + 修复 IM 的错误合约码。
--
-- 背景（2026-10-03 全库档位体检查出，两端一致）：
--   * **36 个品种缺 CLOSE_TODAY 有效行**（含 RB/HC/I/J/JM/CU/AG/M/P/Y 等主力品种）。
--     回测算「日内」时 resolver 会**静默回落到 CLOSE_YEST** —— 对多数品种（平今=平昨）
--     恰好正确，但对**平今有惩罚的品种**会严重低估成本（股指 IC/IH 平今 = 10 倍）。
--     这是本项目最典型的「静默失效」模式（见 memory），必须消灭静默、改为显式落地。
--   * **IM（中证1000股指）scope_contracts = ['IM1000']** —— 源于把「中证1000指数」的
--     "1000" 误解析成合约码。真实合约是 IM2609/IM2612 这类，故 IM **所有月份全部落空**。
--     同族的 IC/IH 在通知里是 scope_kind='ALL'，故 IM 也应改为 ALL。
--
-- 处理（用户 2026-10-03 裁定「方案 A」）：
--   1) IM：闭合 ['IM1000'] 的 CONTRACTS 行，改按 ALL 落地（开/平昨 0.23‱、平今 2.3‱）。
--   2) 其余缺口：按「平今 = 平昨」**显式补种**，source='notice_gapfill'，
--      note 标明是推断值与依据 —— 与「静默回落」的区别在于**可审计、可定点修正**。

-- ── 1) IM：CONTRACTS['IM1000'] → ALL ──────────────────────────────────────
UPDATE dim_trading_cost
SET effective_to = CURRENT_DATE, updated_at = now(),
    note = COALESCE(note, '') || ' | 021: IM1000 为误解析(中证1000指数), 改按 ALL 落地'
WHERE variety_code = 'IM' AND effective_to IS NULL AND scope_kind = 'CONTRACTS';

INSERT INTO dim_trading_cost
    (exchange, variety_code, instrument_kind, action, scope_kind, scope_months,
     scope_contracts, fee_type, fee_value, exchange_fee_value, broker_markup_type,
     broker_markup_value, slip_ticks, effective_from, source, note)
SELECT exchange, 'IM', instrument_kind, action, 'ALL', NULL, NULL,
       fee_type, fee_value, exchange_fee_value, broker_markup_type,
       broker_markup_value, slip_ticks, CURRENT_DATE,
       'exchange_notice_20260311',
       '021: 由 CONTRACTS[IM1000] 迁移为 ALL（IM1000 系「中证1000指数」误解析）'
FROM dim_trading_cost
WHERE variety_code = 'IM' AND effective_to = CURRENT_DATE AND scope_kind = 'CONTRACTS'
  AND NOT EXISTS (
        SELECT 1 FROM dim_trading_cost x
        WHERE x.variety_code = 'IM' AND x.instrument_kind = dim_trading_cost.instrument_kind
          AND x.action = dim_trading_cost.action AND x.scope_kind = 'ALL'
          AND x.effective_to IS NULL)
-- ⚠ uq_dim_trading_cost 是**非 partial** 唯一约束（键含 effective_from），
--   同日已存在（即使已闭合）的行会让 INSERT 失败。故改为「冲突即复活并刷新」。
ON CONFLICT (variety_code, instrument_kind, action, scope_kind, effective_from)
DO UPDATE SET effective_to = NULL, fee_value = EXCLUDED.fee_value,
              fee_type = EXCLUDED.fee_type, source = EXCLUDED.source,
              note = EXCLUDED.note, updated_at = now();

-- ── 2) 通用缺口补种：CLOSE_TODAY = CLOSE_YEST（显式、可审计）──────────────
INSERT INTO dim_trading_cost
    (exchange, variety_code, instrument_kind, action, scope_kind, scope_months,
     scope_contracts, fee_type, fee_value, exchange_fee_value, broker_markup_type,
     broker_markup_value, slip_ticks, effective_from, source, note)
SELECT y.exchange, y.variety_code, y.instrument_kind, 'CLOSE_TODAY', y.scope_kind,
       y.scope_months, y.scope_contracts, y.fee_type, y.fee_value,
       y.exchange_fee_value, y.broker_markup_type, y.broker_markup_value,
       y.slip_ticks, CURRENT_DATE,
       'notice_gapfill',
       '021: 交易所通知未列平今费率，按「平今=平昨」显式补种（推断值，非官方）。'
       '若该品种存在平今加倍/减免，请以交易所公告覆盖此行。'
FROM dim_trading_cost y
WHERE y.effective_to IS NULL
  AND y.action = 'CLOSE_YEST'
  AND NOT EXISTS (
        SELECT 1 FROM dim_trading_cost t
        WHERE t.variety_code = y.variety_code
          AND t.instrument_kind = y.instrument_kind
          AND t.scope_kind = y.scope_kind
          AND t.action = 'CLOSE_TODAY'
          AND t.effective_to IS NULL)
-- 同上：同日已有（可能已闭合）的行 → 复活并刷新，而非插入失败
ON CONFLICT (variety_code, instrument_kind, action, scope_kind, effective_from)
DO UPDATE SET effective_to = NULL, fee_value = EXCLUDED.fee_value,
              fee_type = EXCLUDED.fee_type, source = EXCLUDED.source,
              note = EXCLUDED.note, updated_at = now();

-- ── 3) 断言：补种后不应再有负值，且不应产生重复有效键 ────────────────────
DO $$
DECLARE bad_neg int; bad_dup int;
BEGIN
    SELECT count(*) INTO bad_neg FROM dim_trading_cost
    WHERE fee_value < 0 OR broker_markup_value < 0 OR exchange_fee_value < 0;
    IF bad_neg > 0 THEN
        RAISE EXCEPTION '补种后出现负费率 % 行', bad_neg;
    END IF;
    SELECT count(*) INTO bad_dup FROM (
        SELECT variety_code, instrument_kind, action, scope_kind
        FROM dim_trading_cost WHERE effective_to IS NULL
        GROUP BY 1,2,3,4 HAVING count(*) > 1) t;
    IF bad_dup > 0 THEN
        RAISE EXCEPTION '补种后出现重复有效键 % 个', bad_dup;
    END IF;
END $$;
