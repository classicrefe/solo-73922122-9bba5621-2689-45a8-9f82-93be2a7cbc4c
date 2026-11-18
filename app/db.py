"""PostgreSQL 访问层：连接池 + 索引/后端/轮次状态查询。"""
from __future__ import annotations

import pathlib
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .config import settings

_pool: ConnectionPool | None = None

SCHEMA_FILE = pathlib.Path(__file__).resolve().parent.parent / "sql" / "01_schema.sql"


def init_pool() -> None:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(
            settings.database_url,
            min_size=2,
            max_size=10,
            # application_name 写入变更轨迹，便于区分 API 写入与独立 SQL。
            kwargs={"row_factory": dict_row, "application_name": "reports-api"},
            open=True,
        )


def pool() -> ConnectionPool:
    if _pool is None:
        init_pool()
    assert _pool is not None
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def ensure_schema() -> None:
    """幂等建表（容器首次启动由 initdb 执行，这里保证本地/非容器环境同样可用）。"""
    ddl = SCHEMA_FILE.read_text(encoding="utf-8")
    with pool().connection() as conn:
        conn.execute(ddl)
        conn.commit()


# ----------------------------- 变更轨迹（审计） -----------------------------

def audit_objects_exist(conn: psycopg.Connection) -> bool:
    """审计对象（report_changes 表 + reports_audit 触发器）是否都已就绪。"""
    has_table = conn.execute(
        "SELECT to_regclass('public.report_changes') IS NOT NULL AS ok"
    ).fetchone()["ok"]
    if not has_table:
        return False
    has_reports = conn.execute(
        "SELECT to_regclass('public.reports') IS NOT NULL AS ok"
    ).fetchone()["ok"]
    if not has_reports:
        return False
    return bool(conn.execute(
        "SELECT EXISTS (SELECT 1 FROM pg_trigger "
        " WHERE tgname = 'reports_audit' "
        "   AND tgrelid = 'public.reports'::regclass) AS ok"
    ).fetchone()["ok"])


def ensure_audit_schema() -> None:
    """审计对象缺失时执行幂等 DDL（01_schema.sql 全量幂等，含建表与触发器）。

    已就绪则完全不执行 DDL：避免启动期因 DDL 锁与未提交写事务相互阻塞
    （与 service.reconcile_on_startup 的考虑一致）。旧库升级时在此补齐
    report_changes 与触发器；启用前已存在的报告行，其首次改动的前值
    由触发器的 OLD 整行捕获，无需回填。
    """
    with pool().connection() as conn:
        ok = audit_objects_exist(conn)
    if not ok:
        ensure_schema()


# ----------------------------- 索引状态 ---------------------------------

def get_index_status(conn: psycopg.Connection, index_name: str) -> dict[str, Any] | None:
    """返回索引元数据；不存在返回 None。

    valid=True 且 ready=True 才是真正可用的唯一索引。
    """
    row = conn.execute(
        """
        SELECT i.indexrelid::regclass::text              AS index_name,
               i.indisunique                               AS is_unique,
               i.indisvalid                                AS is_valid,
               i.indisready                                AS is_ready,
               EXISTS (SELECT 1 FROM pg_constraint c
                        WHERE c.conindid = i.indexrelid
                          AND c.contype = 'u')             AS has_constraint
        FROM pg_index i
        JOIN pg_class c ON c.oid = i.indexrelid
        WHERE c.relname = %s
        """,
        (index_name,),
    ).fetchone()
    return dict(row) if row else None


def index_is_valid_unique(conn: psycopg.Connection, index_name: str) -> bool:
    meta = get_index_status(conn, index_name)
    return bool(meta and meta["is_unique"] and meta["is_valid"] and meta["is_ready"])


# ----------------------------- 后端进程 ---------------------------------

def get_backend_state(conn: psycopg.Connection, pid: int | None) -> dict[str, Any] | None:
    if not pid:
        return None
    row = conn.execute(
        """
        SELECT pid, state, wait_event_type, wait_event,
               left(query, 200) AS query, query_start
        FROM pg_stat_activity
        WHERE pid = %s
        """,
        (pid,),
    ).fetchone()
    return dict(row) if row else None


def backend_running_create_index(conn: psycopg.Connection, pid: int | None) -> bool:
    """判断该后端是否仍在执行（或等待执行）CREATE INDEX。

    要求 state='active'：语句结束后的 idle 后端即使保留相同 query 文本也不算在跑。
    """
    st = get_backend_state(conn, pid)
    if st is None or st.get("state") != "active":
        return False
    q = (st.get("query") or "").upper()
    return "CREATE" in q and "INDEX" in q


# ----------------------------- 轮次表 -----------------------------------

def get_active_round(conn: psycopg.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT * FROM index_build_rounds
        WHERE status IN ('running', 'cancel_requested')
        ORDER BY id
        LIMIT 1
        """
    ).fetchone()
    return dict(row) if row else None


def get_round_by_id(conn: psycopg.Connection, round_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM index_build_rounds WHERE round_id = %s",
        (round_id,),
    ).fetchone()
    return dict(row) if row else None


def latest_round(conn: psycopg.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM index_build_rounds ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


# ----------------------------- 变更轨迹查询 -----------------------------

def list_report_changes(
    conn: psycopg.Connection, report_id: int, limit: int, offset: int
) -> tuple[int, list[dict[str, Any]]]:
    """按 reports.id 列出已提交变更轨迹（change_no 升序）。返回 (总数, 明细)。"""
    total = conn.execute(
        "SELECT count(*) AS n FROM report_changes WHERE report_id = %s",
        (report_id,),
    ).fetchone()["n"]
    rows = conn.execute(
        """
        SELECT change_no, op, changed_at, txid, changed_by, application_name,
               old_row, new_row
        FROM report_changes
        WHERE report_id = %s
        ORDER BY change_no
        LIMIT %s OFFSET %s
        """,
        (report_id, limit, offset),
    ).fetchall()
    return total, [dict(r) for r in rows]


def get_report_change(
    conn: psycopg.Connection, report_id: int, change_no: int
) -> dict[str, Any] | None:
    """取该报告指定变更编号的一条轨迹；不存在返回 None。"""
    row = conn.execute(
        """
        SELECT change_no, op, changed_at, txid, changed_by, application_name,
               old_row, new_row
        FROM report_changes
        WHERE report_id = %s AND change_no = %s
        """,
        (report_id, change_no),
    ).fetchone()
    return dict(row) if row else None
