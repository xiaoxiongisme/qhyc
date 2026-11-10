-- CL-20261110-20 / CL-20261110-21：推送去重留痕表
--
-- 背景（2026-11-10 实测，非事件表推断）：
--   ① check_all 的 digest 走 send_alert(key="digest") 冷却 1800s，而
--      ALERT_INTERVAL_MIN=30 恰好每 30 分钟一轮 → 状态未变化也每轮推一条，
--      11-10 全天 20 条重复「告警汇总」；11-06 曾刷爆 pushplus 额度导致
--      共用同一 token 的交易信号静默 2.5 天（V5X-PUSH-SINGLE-CHANNEL 留痕）。
--   ② V3.4 停用后没有每小时状态简报，V5X 只在新信号时推；无信号时段
--      用户完全看不到推送，无法区分「链路正常」与「推送挂了」。
--
-- 设计：一行 = 一次「状态变化后的实际推送」（或收盘简报的一根 K 线）。
--   · kind='alert_digest'：dedup_key=推送时刻，fingerprint=状态指纹；
--     只在指纹变化 / CRIT 超时重推 / 异常恢复时写入，正常状态不写。
--   · kind='v5x_brief'   ：dedup_key=收盘标签 'YYYY-MM-DD HH:MM'，
--     同一根 K 线只推一条，跨调度器重启仍有效（原 _PUSH_GATE 是进程内）。
--   · 送达失败不写行 → 下轮重试；绝不「先占位再发送」把失败固化成已送达。
--
-- 备份：本表为新建，无历史数据可备份。
-- 回滚：DROP TABLE ops.notify_dedup;（代码侧对表不存在有 try/except 兜底，
--       会退化为进程内去重，不会崩巡检）

CREATE TABLE IF NOT EXISTS ops.notify_dedup (
    kind        text        NOT NULL,
    dedup_key   text        NOT NULL,
    fingerprint text,
    sent_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, dedup_key)
);

COMMENT ON TABLE ops.notify_dedup IS
    '推送去重留痕：alert_digest=告警状态变化才推；v5x_brief=每根收盘K线一条简报。CL-20261110-20/21';
COMMENT ON COLUMN ops.notify_dedup.fingerprint IS
    '告警状态指纹（排序后的 key:level 集合）或简报标记；不含会逐轮漂移的 lag 数值';
