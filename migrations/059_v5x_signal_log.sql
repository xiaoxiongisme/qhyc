-- =====================================================================
-- 059: V5X.3 小时级信号日志 + 推送开关 — C3-min / CL-20261108-08
-- ---------------------------------------------------------------------
-- 解决什么问题
--   C3-min 要把 V5X.3 接进「每小时扫描 → 推送信号」链路，但要求
--   **只推信号、不建单、不碰持仓**。缺一张落库表，会带来两个后果：
--     1) 同一根 bar 的信号在重跑/重启后**重复推送**（漏看是问题，轰炸也是）；
--     2) 推过什么没有留痕，事后无法回答「这条信号当时到底推没推、推的什么价」。
--
-- 为什么是独立表而不是复用 fusion_position / execution_order
--   · fusion_position 是**策略意图持仓台账**，写它等于声称「我有这个仓」，
--     而 C3-min 明确不碰持仓；写错会污染 V3.4 状态机。
--   · execution_order 是**可执行订单**，桥上有人认领就会真发单；
--     V5X 在通过影子对账前不得进入该表。
--   故本表只做「信号流水 + 去重」，与下单/持仓完全解耦。
--
-- 价格口径（本表最重要的设计）
--   price_ba / stop_ba   = **BA 后复权空间**（策略内部价，可为负，如 JD888 -462）
--   adj_offset           = 该 bar 的 ba - raw 偏移量（加性常量）
--   price_real / stop_real = **真实可成交价**（= ba - adj_offset）
--   三者同存：反解可复算、对账可溯源，且不允许下游只拿到 BA 价就下单。
--
-- 幂等：表 IF NOT EXISTS，开关 ON CONFLICT DO NOTHING，可重复应用。
-- =====================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS public.v5x_signal_log (
    id           BIGSERIAL PRIMARY KEY,
    dedupe_key   TEXT        NOT NULL UNIQUE,
    symbol       TEXT        NOT NULL,
    bar_ts       TIMESTAMPTZ NOT NULL,
    action       TEXT        NOT NULL,          -- OPEN / ADD / CLOSE
    side         TEXT,                          -- LONG / SHORT
    price_ba     NUMERIC(18, 4),
    adj_offset   NUMERIC(18, 4),
    price_real   NUMERIC(18, 4),
    stop_ba      NUMERIC(18, 4),
    stop_real    NUMERIC(18, 4),
    lots         NUMERIC(10, 4),
    etype        TEXT,                          -- pb=回踩 / bk=突破
    reason       TEXT,                          -- stop / tp
    net_yuan     NUMERIC(18, 2),
    notify_ok    BOOLEAN,
    notify_via   TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT v5x_signal_log_action_chk
        CHECK (action IN ('OPEN', 'ADD', 'CLOSE')),
    CONSTRAINT v5x_signal_log_price_chk
        -- 真实价必须已反解：不能等于 BA 价（除非偏移为 0），且不能缺失
        CHECK (price_real IS NOT NULL AND adj_offset IS NOT NULL)
);

COMMENT ON TABLE public.v5x_signal_log IS
    'V5X.3 小时级信号流水（只读推送，不建单、不回写持仓）。CL-20261108-08 C3-min。';

CREATE INDEX IF NOT EXISTS idx_v5x_signal_log_bar_ts
    ON public.v5x_signal_log (bar_ts DESC);
CREATE INDEX IF NOT EXISTS idx_v5x_signal_log_symbol_ts
    ON public.v5x_signal_log (symbol, bar_ts DESC);

-- 推送总开关：**默认关闭**。开启前必须先跑通 dry-run（run_v5x_scan(do_push=False)），
-- 确认信号条数与口径无误。关闭时调度器整轮不跑（不采集、不评估、不推送）。
INSERT INTO cfg_feature_switch (switch_key, switch_name, switch_group, enabled, value, description)
VALUES (
    'v5x_live_enabled',
    'V5X.3 小时级信号推送',
    'strategy',
    false,
    '',
    'C3-min：每小时扫描 V5X.3 并推送信号（只读，不建单、不回写持仓）。'
    || '开启前须 dry-run 核对条数与价格反解。CL-20261108-08'
)
ON CONFLICT (switch_key) DO NOTHING;

COMMIT;
