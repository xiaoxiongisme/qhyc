-- P0-1 执行反解层：futures_symbol 新增 price_tick（最小变动价位）
-- 仅新增列；数值由 scripts/seed_price_tick.py 从行情数据派生填充（数据派生，非编造）。
ALTER TABLE futures_symbol ADD COLUMN IF NOT EXISTS price_tick NUMERIC(12, 6);
