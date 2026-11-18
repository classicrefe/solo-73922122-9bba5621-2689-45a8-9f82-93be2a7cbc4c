"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ReportIn(BaseModel):
    lab_no: str = Field(..., min_length=1, description="实验室号（非空）")
    report_no: str = Field(..., min_length=1, description="实验室内报告号（非空）")
    measured: int = Field(..., description="整数测值")


class ReportOut(ReportIn):
    id: int


class BatchReportIn(BaseModel):
    items: list[ReportIn] = Field(..., min_length=1)


# ------------------------------ 变更轨迹 ------------------------------

# 轨迹中的整行以 JSONB 原样返回（含 id/lab_no/report_no/measured/created_at），
# 列随 reports 表结构演进，故用通用对象表达。
ReportRow = dict[str, Any]


class ChangeEntry(BaseModel):
    change_no: int = Field(..., description="报告内变更编号，从 1 起按实际生效顺序递增")
    op: str = Field(..., description="INSERT 补录 / UPDATE 更正 / DELETE 删除")
    changed_at: datetime = Field(..., description="变更事务提交时间")
    txid: int = Field(..., description="提交事务 ID")
    changed_by: str = Field(..., description="执行改动的数据库用户")
    application_name: str = Field(..., description="来源应用（reports-api / psql 等）")
    old_row: ReportRow | None = Field(..., description="变更前完整行；INSERT 时为 null")
    new_row: ReportRow | None = Field(..., description="变更后完整行；DELETE 时为 null")


class ChangeListOut(BaseModel):
    report_id: int
    total: int = Field(..., description="该报告已提交的变更总数")
    items: list[ChangeEntry]


class ReportStateAtChangeOut(BaseModel):
    report_id: int
    change_no: int
    op: str
    changed_at: datetime
    row: ReportRow = Field(..., description="该变更生效后当时生效的整行")
