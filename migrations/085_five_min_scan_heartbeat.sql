-- 085_five_min_scan_heartbeat.sql
-- CL-20261124-03：five_min_scan 补声明 expected_gap_days，消除心跳误判。
--
-- 背景：five_min_scan(apscheduler, 5,35 * * * *) 在 084 登记时 expected_gap_days
-- 留空（无心跳）。它每 30 分钟跑一次、24h success 流水 30+ 次；守卫
-- OPS-TASK-STATUS-SOURCE 的「高频流水作业被置 NULL」判据在流水攒够 20 次后命中，
-- 报 FAIL。上一轮 2d5f36f 收口时该作业流水尚未攒够故 FAIL 0，属时序触发。
--
-- 修复：与同 executor(apscheduler)、同 schedule(5,35 * * * *)、同样有真实
-- success 流水的 fusion_scan 对齐，设 expected_gap_days=3（3 天无成功=疑似停摆）。
-- 不改调度节奏 / 开关 / 执行逻辑 / meta，仅补心跳阈值这一列。
--
-- 幂等：只 UPDATE 目标行，expected_gap_days 已是 3 则不产生写。

BEGIN;

UPDATE ops.scheduled_task
   SET expected_gap_days = 3
 WHERE task_id = 'five_min_scan'
   AND expected_gap_days IS DISTINCT FROM 3;

COMMIT;
