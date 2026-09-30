-- 03_sp_l1_fixture.sql · L1 聚合夹具：验证 sp_build_l1_from_minute 的 INSERT 路径
-- 专门捕获 009 原「oi 列名 bug」（bar_* 真实列是 open_interest，非 oi）。
-- 用测试品种 ZZ888 插入 3 根 minute_bar，CALL sp 后检查 bar_5m 是否聚合出来，再清理。

DO $$
DECLARE n int;
BEGIN
  -- 夹具：往 public.minute_bar 插 3 根 ZZ888 棒（5 分钟网格），并给 open_interest
  INSERT INTO minute_bar (symbol, ts, open, high, low, close, volume, amount, open_interest)
  VALUES
    ('ZZ888', '2026-03-01 09:00:00+08', 100, 101, 99, 100, 10, 1000, 5),
    ('ZZ888', '2026-03-01 09:05:00+08', 100, 102, 99, 101, 12, 1200, 5),
    ('ZZ888', '2026-03-01 09:10:00+08', 101, 103, 100, 102, 11, 1100, 5);

  CALL sp_build_l1_from_minute('min5');

  SELECT count(*) INTO n FROM bar_5m WHERE symbol = 'ZZ888';
  IF n < 1 THEN
    RAISE EXCEPTION '[L1夹具] sp_build_l1 未聚合出 ZZ888 行（n=%），可能 oi 列名 bug 未修', n;
  END IF;
  RAISE NOTICE '[OK] sp_build_l1_from_minute 聚合成功，bar_5m.ZZ888 = % 行', n;
EXCEPTION WHEN OTHERS THEN
  -- 清理后重新抛出，便于 ON_ERROR_STOP 捕获
  DELETE FROM bar_5m WHERE symbol = 'ZZ888';
  DELETE FROM minute_bar WHERE symbol = 'ZZ888';
  RAISE;
END $$;

-- 清理夹具数据
DELETE FROM bar_5m WHERE symbol = 'ZZ888';
DELETE FROM minute_bar WHERE symbol = 'ZZ888';
