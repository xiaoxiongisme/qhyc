-- 086_v5x_signal_stop_kind.sql
-- CL-20261124-05：v5x_signal_log 新增 stop_kind（纯诊断字段）。
--
-- 背景：V5X 引擎离场 reason 只有 stop/tp 两档，三种止损来源——
--   1.75ATR 初始止损 / 2.0ATR 吊灯移动止损 / 保本抬到 entry——全部归为 stop。
-- 结果盈利锁盈单也被推送成「离场原因：止损」（Y888 11-24 15:00 net=+4229.96）。
-- 不能用 net_yuan>0 反推吊灯：吊灯触发但扣费后小亏、保本锁盈都会误标。
--
-- 修复：引擎在 _exit 那一刻按三条候选线的胜负判定 stop_kind
--   (initial/trail/be)，作为纯诊断字段随信号落库；reason=stop 语义不变。
-- 推送渲染读回该列，显示「吊灯移动止损（锁盈）」等精确机制标签。
--
-- 兼容性：列可空，历史行保持 NULL → 文案回退原「止损」，不改历史留痕。
-- 不参与去重键 / 推送判定 / 成交层 / 任何交易决策。
--
-- 幂等：ADD COLUMN IF NOT EXISTS，重复执行无副作用。

BEGIN;

ALTER TABLE public.v5x_signal_log
    ADD COLUMN IF NOT EXISTS stop_kind text;

COMMIT;
