-- =====================================================================
-- 060: 持仓同步收口开关 + 生产策略单源开关 + V5X 回放标记
--      CL-20261108-26 / CL-20261108-27 / CL-20261108-28
-- ---------------------------------------------------------------------
-- 一、为什么要有 position_sync_enabled（CL-20261108-26）
--
--   2026-11-08 现状核查结论：**「持仓同步」在代码里根本不存在**。
--     · `execution_position` 只能由 `persistence.upsert_position` 写；
--     · 它唯一的业务调用方 `position_manager.apply_fill` **零调用方**；
--     · `POST /execution/bridge/report` 只写 `execution_fill`，从不回写持仓，
--       但它的 docstring 却写着「持仓由 upsert_position 维护」——一句谎话。
--   于是「实盘成交 → 系统持仓」是一条断链，且断在没人报警的地方：
--   桥上报了成交，台账却仍是旧值，所有依赖 `execution_position` 的
--   开平推导（G1）/ 止损成本价（G3）读到的是**过期事实**。
--
--   用户 2026-11-08 拍板：回写实际持仓**暂时不做**，但**不许留着半成品**
--   （半成品比没有更危险：它看起来像能用）。落法：
--     · 收口成唯一入口 `app/execution/position_sync.py`（成交回报 + 手工快照）；
--     · 总开关 `position_sync_enabled` 默认 **false**，关闭时**一个字都不写**；
--     · 关闭期间 `bridge_report` 仍正常记成交与状态，只是不碰持仓。
--
-- 二、为什么要有 fusion_v34_live_enabled（CL-20261108-27）
--
--   切换前生产同时注册了两个扫描作业：
--     · `fusion_scan`（V3.4，DB 字典实测 `1,31 * * * *`）
--     · `v5x_scan`（V5X.3，代码 CronTrigger `minute=5`）
--   两套策略的持仓假设互不相容（V3.4 维护 fusion_position 意图台账，
--   V5X 无状态回放、不认任何台账），同时推送必然出现「一边说开多、
--   一边说平空」的矛盾信号。
--
--   用户拍板：**V5 取代 V3.4**。用 DB 开关而不是删作业，理由：
--     · 可回切（改一行即恢复，不必回滚代码）；
--     · 可断言（守卫能查两个开关是否同时开着 —— 那是最危险的状态）；
--     · 留日志（作业被字典移除是静默的，开关关闭会写明原因）。
--   注意：停 V3.4 **不影响 V5X 供数**。V5X 读 `l2_adj.bar_60m_ba`，
--   由 `minute_and_bars`(:07/:37) 合成 `l1_mkt.bar_60m` 后经触发器联动、
--   再由 `back_adjusted_rebuild`(02:55/11:55/15:55) 校准；akshare 原始
--   小时线另有独立作业 `hourly_collect`(`0 * * * *`) 在采。这三条都不经过
--   `_fusion_scan_job`。
--
-- 三、为什么要给 v5x_signal_log 加 source 列（CL-20261108-28）
--
--   用户要求 V5 的信号统计从 **2026-11-05 夜盘**起算。V5X 是无状态回放
--   扫描器，可对历史小时点逐点复现当时的开平仓事件，补进同一张表。
--   但补录与实盘在语义上必须可区分，否则「哪些是当时真推过的」永远说不清，
--   净值统计也会把「事后补录」和「当时推送」混成一个数。故加 `source`：
--     · `live`   = 实盘扫描写入（会真的推送）
--     · `replay` = 历史回放补录（只落库 + 出对账报告，**不推送**）
--   去重键 dedupe_key 不含 source —— 同一根 bar 的同一动作全局只允许一条，
--   这样「先回放后实盘跑到同一根 bar」不会重复记账。
--
-- 幂等：全部 IF NOT EXISTS / ON CONFLICT DO NOTHING，可重复应用。
-- 三个开关初值都是 false：本迁移**不改变**任何线上行为，生效靠显式开启。
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------- 1) 开关
INSERT INTO cfg_feature_switch
    (switch_key, switch_name, switch_group, enabled, value, description)
VALUES
    (
        'position_sync_enabled',
        '实盘持仓同步（成交回报/手工快照回写 execution_position）',
        'risk',
        false,
        '',
        'CL-20261108-26。唯一写入口 app/execution/position_sync.py。'
        || 'false = 成交只记 execution_fill、不碰任何持仓表（当前口径）；'
        || 'true = 桥回报成交后滚动 execution_position。'
        || '开启前必须先有「实盘持仓 vs 台账」连续对账通过，否则等于把两套真相混成一套错的。'
    ),
    (
        'fusion_v34_live_enabled',
        'V3.4 实盘扫描与推送',
        'strategy',
        false,
        '',
        'CL-20261108-27。false = _fusion_scan_job 整轮跳过（不评估、不写 fusion_position、不推送）；'
        || 'true = V3.4 恢复为生产推送源。'
        || '与 v5x_live_enabled 互斥：两者同时为 true 会产出互相矛盾的开平信号。'
    )
ON CONFLICT (switch_key) DO NOTHING;

COMMENT ON COLUMN cfg_feature_switch.enabled IS
    '功能开关。注意与 ops.scheduled_task.enabled 的区别：后者移除作业（更强势），'
    '两者同时存在时以后者为准。CFG-20261108';

-- ------------------------------------------------- 2) 回放/实盘来源标记
ALTER TABLE public.v5x_signal_log
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'live';

COMMENT ON COLUMN public.v5x_signal_log.source IS
    'live=实盘扫描（会推送）；replay=历史回放补录（只落库+对账，不推送）。'
    'dedupe_key 全局唯一且不含 source，同一根 bar 同一动作只允许一条。CL-20261108-28';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'v5x_signal_log_source_chk'
           AND conrelid = 'public.v5x_signal_log'::regclass
    ) THEN
        ALTER TABLE public.v5x_signal_log
            ADD CONSTRAINT v5x_signal_log_source_chk
            CHECK (source IN ('live', 'replay'));
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_v5x_signal_log_source_ts
    ON public.v5x_signal_log (source, bar_ts DESC);

COMMIT;
