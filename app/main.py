"""FastAPI 入口：补录、变更轨迹查询、唯一索引构建的发起/查询/取消。"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

import psycopg
from fastapi import FastAPI, HTTPException, Query, status

from . import db
from .responses import AppJSONResponse
from .schemas import (
    BatchReportIn,
    ChangeListOut,
    ReportIn,
    ReportOut,
    ReportStateAtChangeOut,
)
from .service import service

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("api")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_pool()
    # 审计对象（report_changes + 触发器）缺失时补齐；已就绪则不执行任何 DDL。
    db.ensure_audit_schema()
    service.reconcile_on_startup()
    try:
        yield
    finally:
        db.close_pool()


app = FastAPI(
    title="检验报告唯一性保障 API",
    description=(
        "对持续补录的 reports 表在线构建 (lab_no, report_no) 唯一索引；"
        "支持发起、查询、取消 CREATE UNIQUE INDEX CONCURRENTLY，"
        "构建期间补录不阻塞，API 重启后接续跟踪。"
        "reports 的补录/更正/删除由数据库触发器记录变更前后完整行，"
        "可按 reports.id 查询已提交变更轨迹、按变更编号查看当时生效的整行。"
    ),
    version="1.1.0",
    default_response_class=AppJSONResponse,
    lifespan=lifespan,
)


@app.get("/health")
def health() -> dict:
    with db.pool().connection() as conn:
        conn.execute("SELECT 1")
    return {"status": "ok"}


# ------------------------------ 补录 ------------------------------------

@app.post("/reports", response_model=ReportOut, status_code=201)
def insert_report(payload: ReportIn):
    """单条补录。构建期间（CONCURRENTLY）可正常提交；唯一索引生效后重复键返回 409。"""
    try:
        with db.pool().connection() as conn:
            row = conn.execute(
                "INSERT INTO reports (lab_no, report_no, measured) "
                "VALUES (%s, %s, %s) RETURNING id, lab_no, report_no, measured",
                (payload.lab_no, payload.report_no, payload.measured),
            ).fetchone()
            conn.commit()
    except psycopg.errors.UniqueViolation as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "duplicate_report",
                "message": "同一实验室下报告号已存在，本条补录被拒绝，原有数据保持不变",
                "lab_no": payload.lab_no,
                "report_no": payload.report_no,
                "pg_error": str(exc).strip(),
            },
        )
    return dict(row)


@app.post("/reports/batch")
def insert_reports_batch(payload: BatchReportIn):
    """批量补录：逐条保存，重复键跳过且不影响其它行，返回成功/跳过明细。"""
    inserted, skipped = [], []
    with db.pool().connection() as conn:
        for item in payload.items:
            try:
                row = conn.execute(
                    "INSERT INTO reports (lab_no, report_no, measured) "
                    "VALUES (%s, %s, %s) RETURNING id, lab_no, report_no, measured",
                    (item.lab_no, item.report_no, item.measured),
                ).fetchone()
                inserted.append(dict(row))
            except psycopg.errors.UniqueViolation:
                conn.rollback()
                skipped.append(
                    {"lab_no": item.lab_no, "report_no": item.report_no,
                     "reason": "duplicate_report"}
                )
        conn.commit()
    return {"inserted_count": len(inserted), "skipped_count": len(skipped),
            "inserted": inserted, "skipped": skipped}


@app.get("/reports")
def list_reports(limit: int = 50, offset: int = 0):
    with db.pool().connection() as conn:
        rows = conn.execute(
            "SELECT id, lab_no, report_no, measured FROM reports "
            "ORDER BY id DESC LIMIT %s OFFSET %s",
            (limit, offset),
        ).fetchall()
        total = conn.execute("SELECT count(*) AS n FROM reports").fetchone()["n"]
    return {"total": total, "items": [dict(r) for r in rows]}


# --------------------------- 变更轨迹（审计） ----------------------------

@app.get("/reports/{report_id}/changes", response_model=ChangeListOut)
def list_report_changes(
    report_id: int,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    """按 reports.id 查询已提交的变更轨迹（补录/更正/删除，含变更前后完整行）。

    轨迹由数据库触发器与原行同事务写入：独立 SQL 与 API 写入均覆盖，
    回滚不留记录；change_no 从 1 起按实际生效顺序编号。
    唯一索引未生效时，同报告号的多行按各自 id 独立成轨。
    """
    with db.pool().connection() as conn:
        total, items = db.list_report_changes(conn, report_id, limit, offset)
    return {"report_id": report_id, "total": total, "items": items}


@app.get("/reports/{report_id}/changes/{change_no}",
         response_model=ReportStateAtChangeOut)
def get_report_state_at_change(report_id: int, change_no: int):
    """按报告 ID 和变更编号查看当时生效的整行（即该变更生效后的行状态）。

    该编号对应删除（DELETE）时，行自彼时起不存在，返回 404。
    """
    with db.pool().connection() as conn:
        change = db.get_report_change(conn, report_id, change_no)
    if change is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "change_not_found",
                "message": "该报告不存在此变更编号（或报告从未有已提交变更）",
                "report_id": report_id,
                "change_no": change_no,
            },
        )
    if change["op"] == "DELETE":
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "report_deleted",
                "message": "该变更编号对应删除操作，报告行自彼时起不存在；"
                           "删除前的完整行见 GET /reports/{id}/changes 中该条的 old_row",
                "report_id": report_id,
                "change_no": change_no,
            },
        )
    return {
        "report_id": report_id,
        "change_no": change["change_no"],
        "op": change["op"],
        "changed_at": change["changed_at"],
        "row": change["new_row"],
    }


# --------------------------- 唯一索引构建 --------------------------------

@app.post("/index/build", status_code=202)
def start_build():
    """发起（或复用）CREATE UNIQUE INDEX CONCURRENTLY。

    * 已有活跃轮次：复用同一轮，不新开；
    * 索引已有效：直接返回 complete；
    * 失败后的无效残留索引会先安全清理，绝不删除有效索引。
    """
    view, code = service.start_build()
    return AppJSONResponse(view, status_code=code)


@app.get("/index/status")
def index_status(round_id: str | None = None):
    """查询构建轮次状态，并附带 pg_index 中的真实索引有效性。"""
    return service.get_status(round_id)


@app.post("/index/cancel", status_code=202)
def cancel_build(round_id: str | None = None):
    """取消构建：先受理（cancel_requested），后端 SQL 确认停止后才转 cancelled。

    只向记录在案的构建后端 PID 发 pg_cancel_backend，不影响补录连接；
    若索引已有效则报 complete。
    """
    view, code = service.request_cancel(round_id)
    return AppJSONResponse(view, status_code=code)
