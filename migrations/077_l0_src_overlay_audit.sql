-- 077_l0_src_overlay_audit.sql
-- CL-20261112-04 B-4：让 L0 跨源覆盖可审计（**不改主键**）
--
-- 问题（2026-11-12 全系统"看不见的问题"审计）
-- ------------------------------------------
-- l0_raw.minute_bar 主键是 (symbol, ts)，不含 src。同一根 K 线被不同来源
-- 覆盖时，旧行直接消失，谁覆盖了谁、覆盖了什么价格，事后完全无法追溯：
--   · scripts/load_local_csv_1min.py  用 ON CONFLICT DO UPDATE，upd 集合含 src
--     → B888 / CU888 等跨源重叠窗口会被整行覆盖；
--   · app/ingest/minute_collector.py / contract_minute_collector.py 用 DO NOTHING
--     → 「谁先写谁赢」，想写没写进去同样不可见（但至少不篡改已有行）；
--   · scripts/ops/extend_index_8888.py 用 COPY 纯插入。
-- 改主键要重写 6000 万+ 行的 hypertable、还会改变写入冲突语义，风险远大于收益。
--
-- 做法：AFTER UPDATE 行级触发器 + ops 侧轻量审计表
-- ------------------------------------------------
-- 只在 **src 真的变化** 时插一条审计行（WHEN 子句 + 函数内 IF 双保险），
-- 记录 old/new src 与 old/new close。零侵入所有写入脚本；高频采集走 INSERT
-- 或 DO NOTHING，不触发 UPDATE，开销可忽略。
-- TimescaleDB 会把 hypertable 上的行级触发器传播到现有及后续 chunk。
-- ⚠ 已压缩 chunk 本来就不可 UPDATE，触发器不改变这一约束。
--
-- 回滚
-- ----
-- DROP TRIGGER IF EXISTS trg_minute_bar_src_overlay ON l0_raw.minute_bar;
-- DROP FUNCTION IF EXISTS ops.fn_log_minute_bar_src_overlay();
-- DROP TABLE IF EXISTS ops.minute_bar_src_overlay;
--
BEGIN;

CREATE TABLE IF NOT EXISTS ops.minute_bar_src_overlay (
    id          bigserial PRIMARY KEY,
    symbol      text        NOT NULL,
    ts          timestamptz NOT NULL,
    layer       text,
    old_src     text,
    new_src     text,
    old_close   numeric,
    new_close   numeric,
    changed_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_mbso_changed_at
    ON ops.minute_bar_src_overlay (changed_at DESC);

CREATE OR REPLACE FUNCTION ops.fn_log_minute_bar_src_overlay()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.src IS DISTINCT FROM NEW.src THEN
        INSERT INTO ops.minute_bar_src_overlay
            (symbol, ts, layer, old_src, new_src, old_close, new_close)
        VALUES (NEW.symbol, NEW.ts, NEW.layer, OLD.src, NEW.src,
                OLD.close, NEW.close);
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS trg_minute_bar_src_overlay ON l0_raw.minute_bar;
CREATE TRIGGER trg_minute_bar_src_overlay
    AFTER UPDATE ON l0_raw.minute_bar
    FOR EACH ROW
    WHEN (OLD.src IS DISTINCT FROM NEW.src)
    EXECUTE FUNCTION ops.fn_log_minute_bar_src_overlay();

COMMIT;

-- 验收（迁移执行后单独跑）：
--   SELECT count(*) FROM pg_trigger
--   WHERE tgname='trg_minute_bar_src_overlay' AND NOT tgisinternal;
--   SELECT * FROM ops.minute_bar_src_overlay ORDER BY changed_at DESC LIMIT 10;
