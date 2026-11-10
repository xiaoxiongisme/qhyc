-- CL-20261110-17：f_vwap_dev 口径变更（成交额 → 成交量加权典型价）
--
-- 根因（2026-11-10 主表实测）：
--   public.bar_15m 的 X888 主力连续 2026-09~10 共 35,158 根，amount 全 NULL
--   （has_amt=0），而 volume / high / low / close 全部齐备。
--   factor_ic_scan.build_factors 旧式
--       f_vwap_dev = (cl - amt/vol) / atr * 100
--   中 amt=sum(amount)=0（pandas skipna），因子静默退化成 cl/atr*100：
--   11-04~06 均值 12,578 / 最大 340,750，11-09 均值 5,867 —— 有值但语义失真，
--   逐日覆盖类巡检永远发现不了。历史 2015-01-15~2026-11-10 共 125,347 行。
--
-- 处理：VWAP 改用标准近似 sum(((H+L+C)/3)*volume)/sum(volume)，只用可信字段，
--   不依赖 amount、不编造成交额。该因子 enabled=f / default_weight=0，
--   从未进入信号，口径变更不影响实盘；全历史按 trade_date 幂等重算。
--   11-02 起 l1_mkt.bar_15m.amount 全 0 的源头缺失如实留痕，不在本条伪造。
--
-- 备份：l3_ref.bak_cl20261110_17_vwap（125,347 行）、
--       l3_ref.bak_cl20261110_17_registry（29 行）
-- 回滚：
--   UPDATE l2_adj.factor_value f SET raw_value=b.raw_value, z=b.z
--   FROM l3_ref.bak_cl20261110_17_vwap b
--   WHERE f.factor_id=b.factor_id AND f.trade_date=b.trade_date AND f.symbol=b.symbol;
--   UPDATE factor_registry f SET description=b.description
--   FROM l3_ref.bak_cl20261110_17_registry b WHERE f.factor_id=b.factor_id;
--   （另需回退 factor_ic_scan.py 的 tpv/vwap_vol 与守卫 OPS-VWAP-CALIBER-WIRED）

UPDATE factor_registry
SET description = description
    || ' ｜[2026-11-10 口径变更 CL-20261110-17] VWAP 由 成交额/成交量 改为 成交量加权典型价 sum(((H+L+C)/3)*volume)/sum(volume)；X888 主力连续 amount 结构性全 NULL 使旧式退化为 cl/atr*100（有值但语义失真）。全历史按 trade_date 重算；不依赖 amount，不编造成交额。'
WHERE factor_id = 'f_vwap_dev'
  AND description NOT LIKE '%CL-20261110-17%';
