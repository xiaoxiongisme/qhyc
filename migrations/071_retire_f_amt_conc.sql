-- CL-20261110-16：f_amt_conc 正式退役（结构性不可算）
--
-- 根因（2026-11-10 主表实测，非推断）：
--   public.bar_15m 中 X8888 商品指数行 amount 全为正值（2026-09~10 日盘 22,015 根
--   全部 has_amt>0），而 X888 主力连续行 amount 全 NULL（同期 24,932 根 has_amt=0）。
--   factor_ic_scan.pick_symbols 优先选 X888 并剔除 X8888，故 build_factors 里
--   amt = sum(amount) = 0 → out["f_amt_conc"] = amt2 / amt.replace(0, nan) * 100
--   整列 NaN → compute_a 的 dropna 后 0 行。A 组 dry-run 在 2026-09-01~11-01
--   与 2026-10-01~11-11 两个窗口均只产出 10 个因子，f_amt_conc 恒 0 行。
--   因子停在 2026-02-28（122,753 行 / 57 品种），断供 255 天。
--
-- 处理：该因子 enabled=f / default_weight=0，从未进入信号，且原口径不可补回
--   （除非先给 X888 补成交额并声明口径变更，见 CL-20261110-17）。正式退役比
--   伪造替代值更诚实：巡检（scripts/factor_freshness_check.py 的 RETIRED_FACTORS）
--   跳过告警与补算，历史 122,753 行保留不删。
--
-- 备份：l3_ref.bak_cl20261110_16_registry（29 行，2026-11-10 20:33 建）
-- 回滚：
--   UPDATE factor_registry f SET description = b.description
--   FROM l3_ref.bak_cl20261110_16_registry b
--   WHERE f.factor_id = b.factor_id AND f.factor_id = 'f_amt_conc';
--   （另需同步回退 factor_freshness_check.py 的 RETIRED_FACTORS 与守卫断言）

UPDATE factor_registry
SET description = description
    || ' ｜[2026-11-10 退役 CL-20261110-16] bar_15m 的 X888 主力连续 amount 结构性缺失，amt2/amt 恒除零，原口径不可算；enabled=f/weight=0 从未进信号；历史 122753 行保留，巡检不再告警。复活条件见 CL-20261110-17。'
WHERE factor_id = 'f_amt_conc'
  AND description NOT LIKE '%CL-20261110-16%';
