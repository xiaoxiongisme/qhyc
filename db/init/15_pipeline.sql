-- =====================================================
-- M8 · 决策链路容器化：运行留痕表（PRD docs/M8_决策链路容器化_PRD_20260922.md §7）
-- 幂等：全部 IF NOT EXISTS；非 hypertable（运行元数据，行数极小）
-- 注：db/init 仅在空数据卷首次初始化时执行；运行期由
--     app/pipeline/recorder.ensure_tables() 兜底建表。
-- =====================================================

CREATE TABLE IF NOT EXISTS pipeline_run (
  run_id       BIGSERIAL PRIMARY KEY,
  kind         TEXT        NOT NULL,            -- daily | signal | intraday | check
  run_date     DATE        NOT NULL,
  started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at  TIMESTAMPTZ,
  status       TEXT        NOT NULL,            -- running | ok | partial | fail
  steps        JSONB,                           -- [{i,name,ok,dur_s,exit}]
  artifacts    JSONB,                           -- 产出绝对路径清单
  src_manifest TEXT,                            -- 源码 md5 清单摘要（单一真源审计）
  data_ready   BOOLEAN     DEFAULT NULL,        -- readiness 闸门结果
  error        TEXT,
  created_by   TEXT        NOT NULL DEFAULT 'scheduler'
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_pipeline_daily
  ON pipeline_run(kind, run_date) WHERE kind = 'daily';      -- 日链幂等：一天一条
CREATE INDEX IF NOT EXISTS ix_pipeline_run_recent
  ON pipeline_run(kind, started_at DESC);

CREATE TABLE IF NOT EXISTS pipeline_push_log (
  id       BIGSERIAL PRIMARY KEY,
  run_id   BIGINT REFERENCES pipeline_run(run_id) ON DELETE CASCADE,
  sent_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
  channel  TEXT,                                -- pushplus | webhook | none
  ok       BOOLEAN     NOT NULL,
  title    TEXT,
  resp     TEXT
);
CREATE INDEX IF NOT EXISTS ix_pipeline_push_run ON pipeline_push_log(run_id, sent_at DESC);
