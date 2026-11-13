-- 080_forbid_fusion_push_log_revival.sql
-- CL-20261113-05：数据库层禁止已退役的 fusion_push_log 被重建。
--
-- 背景（11-13 实证，闭环 CL-20261113-04 的 S6 未决项）
-- ----------------------------------------------------
-- 069 于 11-09 DROP 了 public.fusion_push_log。但研究影子引擎
-- /home/ubuntu/wb_run/_shadow_run.sh 会把分支 cb/entry-gate-candidate 的
-- app/strategies/fusion_signal.py（11-01 旧版）覆盖进容器
-- /tmp/shadow_engine/app/strategies/fusion_signal.py；该旧版仍定义
-- FusionPushLog，且 ensure_fusion_table() 的 tables 元组含
-- FusionPushLog.__table__ —— 只要 public 下查不到该表就 create_all 重建。
-- 影子脚本 fusion_scan_shadow.py:97 会调用 ensure_fusion_table，且
-- sys.path.insert(0, _ENGINE) 让它命中旧版。11-13 15:10 重组镜像后立刻
-- 复活成 0 行空壳表（id_seq last_value=1 / is_called=f），触发
-- PUSH-LOG-RETIRED block FAIL。
--
-- 为什么还要 DB 层兜底
-- --------------------
-- 代码侧防线（组装脚本剔除 + worktree 修 + 断言扫描）只能覆盖已知副本；
-- /tmp 下还有多份退役前旧版副本，任何一份被 sys.path 命中都能复活它。
-- event trigger 是最后一道「无论谁、用哪份旧代码都建不出来」的行为级门禁。
--
-- 行为：ddl_command_end 时检查本次 DDL 涉及的对象名，含 fusion_push_log
-- 即 RAISE EXCEPTION；异常会回滚整个事务，CREATE TABLE 不生效。
-- 正常业务 DDL 不受影响（只匹配该名字）。
--
-- 回滚：DROP EVENT TRIGGER trg_forbid_fusion_push_log_revival;
--       DROP FUNCTION ops.forbid_fusion_push_log_revival();

BEGIN;

CREATE OR REPLACE FUNCTION ops.forbid_fusion_push_log_revival()
RETURNS event_trigger
LANGUAGE plpgsql
AS $$
DECLARE
    cmd record;
BEGIN
    FOR cmd IN SELECT * FROM pg_event_trigger_ddl_commands() LOOP
        IF coalesce(cmd.object_identity, '') LIKE '%fusion_push_log%' THEN
            RAISE EXCEPTION
                'CL-20261109-18: fusion_push_log 已退役，禁止重建（DDL: % / % / %）。'
                ' 旧版 ensure_fusion_table 会 create_all 复活它；见 CL-20261113-05',
                cmd.command_tag, cmd.object_type, cmd.object_identity;
        END IF;
    END LOOP;
END;
$$;

DROP EVENT TRIGGER IF EXISTS trg_forbid_fusion_push_log_revival;
CREATE EVENT TRIGGER trg_forbid_fusion_push_log_revival
    ON ddl_command_end
    WHEN TAG IN ('CREATE TABLE', 'CREATE TABLE AS', 'SELECT INTO',
                 'ALTER TABLE', 'CREATE SEQUENCE', 'CREATE VIEW')
    EXECUTE FUNCTION ops.forbid_fusion_push_log_revival();

COMMIT;
