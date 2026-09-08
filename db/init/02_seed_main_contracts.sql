-- =====================================================
-- 期货预测平台 - 内置品种/主连元数据种子（M1）
-- 与 config/local.yaml:main_contracts 保持一致
-- =====================================================

INSERT INTO futures_symbol (symbol, name, exchange, unit, multiplier, product, is_main, main_symbol, active)
VALUES
    ('FG888', '玻璃主连',     'CZCE',  '吨',  20,    'FG', TRUE,  'FG888', TRUE),
    ('SA888', '纯碱主连',     'CZCE',  '吨',  20,    'SA', TRUE,  'SA888', TRUE),
    ('RB888', '螺纹钢主连',   'SHFE',  '吨',  10,    'RB', TRUE,  'RB888', TRUE),
    ('CU888', '沪铜主连',     'SHFE',  '吨',  5,     'CU', TRUE,  'CU888', TRUE),
    ('AU888', '沪金主连',     'SHFE',  '克',  1000,  'AU', TRUE,  'AU888', TRUE),
    ('AG888', '沪银主连',     'SHFE',  '千克',15,    'AG', TRUE,  'AG888', TRUE),
    ('M888',  '豆粕主连',     'DCE',   '吨',  10,    'M',  TRUE,  'M888',  TRUE),
    ('Y888',  '豆油主连',     'DCE',   '吨',  10,    'Y',  TRUE,  'Y888',  TRUE),
    ('I888',  '铁矿石主连',   'DCE',   '吨',  100,   'I',  TRUE,  'I888',  TRUE),
    ('JM888', '焦煤主连',     'DCE',   '吨',  60,    'JM', TRUE,  'JM888', TRUE),
    ('J888',  '焦炭主连',     'DCE',   '吨',  100,   'J',  TRUE,  'J888',  TRUE),
    ('IF888', '沪深300主连',  'CFFEX', '点',  300,   'IF', TRUE,  'IF888', TRUE),
    ('IC888', '中证500主连',  'CFFEX', '点',  200,   'IC', TRUE,  'IC888', TRUE),
    ('IH888', '上证50主连',   'CFFEX', '点',  300,   'IH', TRUE,  'IH888', TRUE),
    ('T888',  '30年国债主连', 'CFFEX', '点',  10000, 'T',  TRUE,  'T888',  TRUE),
    ('TF888', '5年国债主连',  'CFFEX', '点',  10000, 'TF', TRUE,  'TF888', TRUE),
    ('SC888', '原油主连',     'INE',   '桶',  1000,  'SC', TRUE,  'SC888', TRUE)
ON CONFLICT (symbol) DO UPDATE SET
    name       = EXCLUDED.name,
    exchange   = EXCLUDED.exchange,
    unit       = EXCLUDED.unit,
    multiplier = EXCLUDED.multiplier,
    product    = EXCLUDED.product,
    is_main    = EXCLUDED.is_main,
    main_symbol= EXCLUDED.main_symbol,
    active     = EXCLUDED.active,
    updated_at = NOW();