-- =============================================================================
-- ab_compare_l0.sql  ·  L0 本地 CSV 取代「新源 vs 旧 akshare」A/B 比对
--
-- 部署：在 qhyc 宿主对 timescaledb 执行（加载 CSV 后、切换前）
--   psql "postgres://futures:${POSTGRES_PASSWORD}@timescaledb:5432/futures" \
--        -v ON_ERROR_STOP=1 -f scripts/ab_compare_l0.sql
--
-- 前提（重要）：loader 现在用 ON CONFLICT (symbol,ts) DO UPDATE 幂等覆盖，
--   **同 symbol 同 ts 的旧 akshare 行会被新 CSV 行原地覆盖**。因此若不带
--   `--backup-old` 直接跑 loader，旧源数据不留痕迹、A/B 无从比。
--   正确姿势：
--       python scripts/load_local_csv_1min.py --only IC RB CU --backup-old
--   --backup-old 会把「将被覆盖的非 local_csv 行」先快照到
--       l0_raw.minute_bar_ab_baseline (symbol, ts) 唯一
--   本脚本检测到该基线表就走真 A/B；没有则只出统计摘要并提示。
-- =============================================================================

-- ---------------------------------------------------------------- ⓪ 环境探测
SELECT to_regclass('l0_raw.minute_bar_ab_baseline') IS NOT NULL AS has_baseline \gset

\echo '=== A/B 环境 ==='
SELECT CASE WHEN :'has_baseline'::boolean
            THEN '基线表存在：将做逐行 A/B（旧=minute_bar_ab_baseline，新=minute_bar src=local_csv*）'
            ELSE '基线表缺失：请重跑 loader 时加 --backup-old，否则旧源已被覆盖无法 A/B'
       END AS status;

-- ------------------------------------------------- ① 结构与口径摘要（总是执行）
\echo ''
\echo '=== ① 按来源 × 层 的行数与时间跨度 ==='
SELECT layer, src, count(*) AS rows, min(ts) AS ts_min, max(ts) AS ts_max,
       count(DISTINCT variety_code) AS n_variety
FROM l0_raw.minute_bar
GROUP BY layer, src
ORDER BY layer, src;

\echo ''
\echo '=== ② 口径分布（应：主连 raw / 合约 true 或 no_multiplier / 指数继承）==='
SELECT layer, caliber_vol, caliber_amt, count(*) AS rows
FROM l0_raw.minute_bar
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3;

\echo ''
\echo '=== ③ 元数据完整度（034 加列后旧行 variety_code/exchange 可能为 NULL）==='
SELECT layer,
       count(*) FILTER (WHERE variety_code IS NULL) AS null_variety,
       count(*) FILTER (WHERE exchange IS NULL)     AS null_exchange,
       count(*)                                     AS rows
FROM l0_raw.minute_bar
GROUP BY layer
ORDER BY layer;

-- ------------------------------------------------------------ ④ 逐行 A/B 比对
\if :has_baseline

\echo ''
\echo '=== ④-1 关键品种逐行差异（全重叠区间；close/volume 完全相等占比是关键指标）==='
WITH j AS (
    SELECT b.symbol,
           count(*)                                                       AS n_overlap,
           sum(CASE WHEN a.close = b.close THEN 1 ELSE 0 END)              AS close_exact,
           sum(CASE WHEN a.open  = b.open  THEN 1 ELSE 0 END)              AS open_exact,
           sum(CASE WHEN a.high  = b.high  THEN 1 ELSE 0 END)              AS high_exact,
           sum(CASE WHEN a.low   = b.low   THEN 1 ELSE 0 END)              AS low_exact,
           sum(CASE WHEN a.volume = b.volume THEN 1 ELSE 0 END)            AS vol_exact,
           round(avg(abs(a.close - b.close)::numeric), 4)                  AS close_absdiff_avg,
           round(max(abs(a.close - b.close)::numeric), 4)                  AS close_absdiff_max,
           round(avg((a.volume - b.volume)::numeric), 2)                   AS vol_diff_avg
    FROM l0_raw.minute_bar_ab_baseline b
    JOIN l0_raw.minute_bar a
      ON a.symbol = b.symbol AND a.ts = b.ts
    WHERE a.src LIKE 'local_csv%'
      AND a.symbol IN ('IC888','RB888','CU888','AG888','M888','SR888')
    GROUP BY b.symbol
)
SELECT symbol, n_overlap,
       round(100.0 * close_exact / n_overlap, 3) AS close_exact_pct,
       round(100.0 * open_exact  / n_overlap, 3) AS open_exact_pct,
       round(100.0 * high_exact  / n_overlap, 3) AS high_exact_pct,
       round(100.0 * low_exact   / n_overlap, 3) AS low_exact_pct,
       round(100.0 * vol_exact   / n_overlap, 3) AS vol_exact_pct,
       close_absdiff_avg, close_absdiff_max, vol_diff_avg
FROM j
ORDER BY symbol;

\echo ''
\echo '=== ④-2 差异最大的日期（定位口径/时区/换月问题）==='
SELECT b.symbol,
       (b.ts AT TIME ZONE 'Asia/Shanghai')::date AS sh_day,
       count(*)                                  AS n,
       round(avg(abs(a.close - b.close)::numeric), 4) AS close_absdiff_avg,
       round(max(abs(a.close - b.close)::numeric), 4) AS close_absdiff_max,
       sum(CASE WHEN a.close <> b.close THEN 1 ELSE 0 END) AS n_diff
FROM l0_raw.minute_bar_ab_baseline b
JOIN l0_raw.minute_bar a ON a.symbol = b.symbol AND a.ts = b.ts
WHERE a.src LIKE 'local_csv%'
  AND b.symbol IN ('IC888','RB888','CU888')
GROUP BY 1, 2
ORDER BY close_absdiff_avg DESC NULLS LAST
LIMIT 20;

\else

\echo ''
\echo '=== ④ 跳过逐行 A/B：基线表 l0_raw.minute_bar_ab_baseline 不存在 ==='
\echo '    若旧源尚未被覆盖，可立即补快照（不重跑 loader）：'
\echo '    psql ... -c "CREATE TABLE l0_raw.minute_bar_ab_baseline AS'
\echo '                SELECT * FROM l0_raw.minute_bar WHERE src NOT LIKE ''local_csv%%'';'
\echo '                CREATE UNIQUE INDEX ON l0_raw.minute_bar_ab_baseline (symbol, ts);"'

\endif

-- --------------------------------------------------------------- ⑤ L2 健康度
\echo ''
\echo '=== ⑤ L2 换月段健康度（每品种段数应 ≈ 换月频次；0 段=8888 合成失败/未跑 L2）==='
SELECT substring(symbol, 1, length(symbol) - 3) AS variety,
       freq,
       count(*)                                 AS segments,
       min(seg_start)                           AS first_roll,
       max(seg_start)                           AS last_roll,
       count(*) FILTER (WHERE roll_delta <> 0)  AS n_real_roll
FROM l2_adj.roll_segment
GROUP BY 1, 2
ORDER BY 1, 2;
