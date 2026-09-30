-- 01_validate_dict_cfg.sql · 字典/配置表校验（007/008/009 应用后运行）
-- 任何断言失败 → RAISE EXCEPTION → psql ON_ERROR_STOP 终止，预检不通过。

-- 1) dim_exchange = 6
DO $$ DECLARE n int; BEGIN
  SELECT count(*) INTO n FROM dim_exchange;
  IF n <> 6 THEN RAISE EXCEPTION 'dim_exchange 预期 6 行, 实际 %', n; END IF;
  RAISE NOTICE '[OK] dim_exchange = 6';
END $$;

-- 2) dim_variety 至少 60（futures_symbol 73 + dim_symbol 补齐）
DO $$ DECLARE n int; BEGIN
  SELECT count(*) INTO n FROM dim_variety;
  IF n < 60 THEN RAISE EXCEPTION 'dim_variety 过少: %', n; END IF;
  RAISE NOTICE '[OK] dim_variety = %', n;
END $$;

-- 3) dim_contract 非空
DO $$ DECLARE n int; BEGIN
  SELECT count(*) INTO n FROM dim_contract;
  IF n < 1 THEN RAISE EXCEPTION 'dim_contract 为空'; END IF;
  RAISE NOTICE '[OK] dim_contract = %', n;
END $$;

-- 4) cfg_feature_switch 关键开关与现状 env 对齐（避免迁移后行为突变）
DO $$ DECLARE v boolean; BEGIN
  SELECT enabled INTO v FROM cfg_feature_switch WHERE switch_key='factor_v1v6_enabled';
  IF v IS DISTINCT FROM true THEN RAISE EXCEPTION 'factor_v1v6_enabled 应为 true, 实际 %', v; END IF;
  SELECT enabled INTO v FROM cfg_feature_switch WHERE switch_key='factor_ic_monitor_enabled';
  IF v IS DISTINCT FROM true THEN RAISE EXCEPTION 'factor_ic_monitor_enabled 应为 true, 实际 %', v; END IF;
  SELECT enabled INTO v FROM cfg_feature_switch WHERE switch_key='data_selfcheck_enabled';
  IF v IS DISTINCT FROM false THEN RAISE EXCEPTION 'data_selfcheck_enabled 应为 false, 实际 %', v; END IF;
  SELECT enabled INTO v FROM cfg_feature_switch WHERE switch_key='portfolio_brake_enabled';
  IF v IS DISTINCT FROM false THEN RAISE EXCEPTION 'portfolio_brake_enabled 应为 false, 实际 %', v; END IF;
  SELECT enabled INTO v FROM cfg_feature_switch WHERE switch_key='l2_roll_enabled';
  IF v IS DISTINCT FROM true THEN RAISE EXCEPTION 'l2_roll_enabled 应为 true, 实际 %', v; END IF;
  RAISE NOTICE '[OK] cfg_feature_switch 关键开关与现状 env 对齐';
END $$;

-- 5) cfg_basic_indicator = 6
DO $$ DECLARE n int; BEGIN
  SELECT count(*) INTO n FROM cfg_basic_indicator;
  IF n <> 6 THEN RAISE EXCEPTION 'cfg_basic_indicator 预期 6 行, 实际 %', n; END IF;
  RAISE NOTICE '[OK] cfg_basic_indicator = %', n;
END $$;

-- 6) data_layer_catalog 分层计数
DO $$ DECLARE n0 int; n1 int; n2 int; n3 int; BEGIN
  SELECT count(*) INTO n0 FROM data_layer_catalog WHERE layer='L0';
  SELECT count(*) INTO n1 FROM data_layer_catalog WHERE layer='L1';
  SELECT count(*) INTO n2 FROM data_layer_catalog WHERE layer='L2';
  SELECT count(*) INTO n3 FROM data_layer_catalog WHERE layer='L3';
  IF n0 < 10 OR n1 < 4 OR n2 <> 1 OR n3 < 1 THEN
    RAISE EXCEPTION 'data_layer_catalog 分层异常 L0=% L1=% L2=% L3=%', n0,n1,n2,n3;
  END IF;
  RAISE NOTICE '[OK] data_layer_catalog L0=% L1=% L2=% L3=%', n0,n1,n2,n3;
END $$;

-- 7) FK 完整性：dim_contract.variety_code 必须命中父表
DO $$ DECLARE n int; BEGIN
  SELECT count(*) INTO n FROM dim_contract dc
    LEFT JOIN dim_variety v ON v.variety_code=dc.variety_code
    WHERE dc.variety_code IS NOT NULL AND v.variety_code IS NULL;
  IF n > 0 THEN RAISE EXCEPTION 'dim_contract 有 % 行 variety_code 无父', n; END IF;
  RAISE NOTICE '[OK] dim_contract.variety_code FK 完整';
END $$;

-- 8) 视图可查询
DO $$ DECLARE n int; BEGIN
  SELECT count(*) INTO n FROM v_variety_contracts;
  RAISE NOTICE '[OK] v_variety_contracts 可查询, % 行', n;
END $$;
