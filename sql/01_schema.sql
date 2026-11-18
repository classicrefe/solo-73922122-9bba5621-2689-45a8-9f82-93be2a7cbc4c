-- 检验数仓报告表与索引构建轮次表
-- 幂等脚本，容器首次启动时由 PostgreSQL 自动执行（/docker-entrypoint-initdb.d）。

BEGIN;

-- 1) 报告主表
CREATE TABLE IF NOT EXISTS reports (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,  -- 主键流水号
    lab_no       TEXT NOT NULL,                                     -- 实验室号（非空）
    report_no    TEXT NOT NULL,                                     -- 实验室内报告号（非空）
    measured     INTEGER NOT NULL,                                  -- 整数测值
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE  reports            IS '持续补录的检验报告表';
COMMENT ON COLUMN reports.id         IS '主键流水号';
COMMENT ON COLUMN reports.lab_no     IS '实验室号';
COMMENT ON COLUMN reports.report_no  IS '实验室内报告号';
COMMENT ON COLUMN reports.measured   IS '整数测值';

-- 补录期间的普通辅助索引（非唯一），仅用于按实验室号/报告号查询与去重；
-- 唯一索引构建成功前，唯一性由补录 SQL 自行保证。
CREATE INDEX IF NOT EXISTS idx_reports_lab_report
    ON reports (lab_no, report_no);

-- 2) 唯一索引构建轮次表（状态持久化，API 重启后据此接续跟踪）
CREATE TABLE IF NOT EXISTS index_build_rounds (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    round_id    UUID NOT NULL UNIQUE,
    status      TEXT NOT NULL CHECK (status IN (
                    'running',        -- 构建中（含等待长事务阶段）
                    'complete',       -- 索引已有效
                    'failed',         -- 构建失败（如重复键），原数据保留，无效索引已清理
                    'cancel_requested', -- 已受理取消，等待后端 SQL 真正停止
                    'cancelled'       -- 后端 SQL 已确认停止且索引无效
                )),
    backend_pid INTEGER,                       -- 执行 CREATE INDEX 的 PostgreSQL 后端 PID
    index_name  TEXT NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    error       TEXT,                         -- 失败原因（如重复键明细）
    failure_type TEXT,                        -- duplicate_key / lock_timeout / other / cancelled
    restarted   BOOLEAN NOT NULL DEFAULT FALSE -- 是否为 API 重启后接续跟踪的轮次
);

COMMENT ON TABLE index_build_rounds IS 'CREATE UNIQUE INDEX CONCURRENTLY 构建轮次';

-- 同一时刻只允许一轮活跃构建（running 或 cancel_requested 各最多一条）。
-- 该约束在数据库层面兜底，即使多个 API 实例并发发起也不会出现两轮并发构建。
CREATE UNIQUE INDEX IF NOT EXISTS uq_index_rounds_one_active
    ON index_build_rounds ((1))
    WHERE status IN ('running', 'cancel_requested');

-- 3) 报告变更轨迹表（审计）。
-- 从功能启用起，reports 上每一次已提交的补录(INSERT)/更正(UPDATE)/删除(DELETE)
-- 都由行级触发器在此记录变更前、变更后的完整行；轨迹与原行在同一事务提交，
-- 回滚则两者一起消失，不留记录。report_id 即 reports.id：唯一索引生效前
-- 同报告号的多行各按 id 独立成轨；报告删除后轨迹仍保留，故不设外键。
CREATE TABLE IF NOT EXISTS report_changes (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    report_id        BIGINT      NOT NULL,  -- reports.id（业务主键流水号）
    change_no        INTEGER     NOT NULL,  -- 报告内变更编号：从 1 起按实际生效顺序递增
    op               TEXT        NOT NULL CHECK (op IN ('INSERT', 'UPDATE', 'DELETE')),
    changed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    txid             BIGINT      NOT NULL,  -- 提交事务 ID，同事务的多行变更可据此关联
    changed_by       TEXT        NOT NULL,  -- 执行改动的数据库用户
    application_name TEXT        NOT NULL DEFAULT '',  -- 来源应用（reports-api / psql / 脚本等）
    old_row          JSONB,                 -- 变更前完整行（INSERT 为 NULL）
    new_row          JSONB,                 -- 变更后完整行（DELETE 为 NULL）
    UNIQUE (report_id, change_no)           -- 兜底：同一报告内编号绝不重复
);

COMMENT ON TABLE  report_changes              IS 'reports 已提交变更轨迹：补录/更正/删除的前后完整行（触发器写入，与原行同事务）';
COMMENT ON COLUMN report_changes.report_id    IS 'reports.id；唯一索引未生效时同报告号多行按 id 各自成轨';
COMMENT ON COLUMN report_changes.change_no    IS '报告内变更编号，从 1 起按实际生效顺序递增；回滚不留号';
COMMENT ON COLUMN report_changes.op           IS 'INSERT 补录 / UPDATE 更正 / DELETE 删除';
COMMENT ON COLUMN report_changes.txid         IS '提交事务 ID（txid_current()）';
COMMENT ON COLUMN report_changes.old_row      IS '变更前完整行（JSONB）；INSERT 时为 NULL；旧库已有行首次改动时即改动前的值';
COMMENT ON COLUMN report_changes.new_row      IS '变更后完整行（JSONB）；DELETE 时为 NULL';

-- 4) 行级审计触发器：AFTER 触发，与原行同事务写入轨迹。
-- 编号取“该报告已提交的最大编号 + 1”：同一报告的并发改动被 reports 行锁串行化，
-- 后生效的事务解除阻塞后才执行触发器，能读到先生效事务已提交的编号，
-- 因此 change_no 的顺序就是改动实际生效的顺序；回滚事务的编号随事务消失，
-- 后续改动会复用该号，不留空洞、不留未提交的值。
CREATE OR REPLACE FUNCTION log_report_change() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_report_id BIGINT;
    v_next_no   INTEGER;
BEGIN
    v_report_id := COALESCE(NEW.id, OLD.id);

    SELECT COALESCE(MAX(c.change_no), 0) + 1
      INTO v_next_no
      FROM report_changes c
     WHERE c.report_id = v_report_id;

    INSERT INTO report_changes
        (report_id, change_no, op, txid, changed_by, application_name, old_row, new_row)
    VALUES (
        v_report_id,
        v_next_no,
        TG_OP,
        txid_current(),
        current_user,
        current_setting('application_name'),
        CASE WHEN TG_OP <> 'INSERT' THEN to_jsonb(OLD) END,
        CASE WHEN TG_OP <> 'DELETE' THEN to_jsonb(NEW) END
    );

    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

-- 5) 禁止 TRUNCATE：它绕过行级触发器，会让删除不留轨迹。
-- 需要清空时请用 DELETE（逐行记录轨迹）。
CREATE OR REPLACE FUNCTION forbid_reports_truncate() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'TRUNCATE reports 被禁止：删除必须留变更轨迹，请改用 DELETE';
END;
$$;

-- PostgreSQL 的 CREATE TRIGGER 没有 IF NOT EXISTS，用 DROP + CREATE 保证幂等。
DROP TRIGGER IF EXISTS reports_audit ON reports;
CREATE TRIGGER reports_audit
AFTER INSERT OR UPDATE OR DELETE ON reports
FOR EACH ROW EXECUTE FUNCTION log_report_change();

DROP TRIGGER IF EXISTS reports_no_truncate ON reports;
CREATE TRIGGER reports_no_truncate
BEFORE TRUNCATE ON reports
FOR EACH STATEMENT EXECUTE FUNCTION forbid_reports_truncate();

COMMIT;
