from typing import List, Optional
from datetime import date, datetime

from pydantic import Field

from .common import BaseSchema, TimestampSchema
from app.models import (
    FollowUpDecisionAction,
    FollowUpStage,
    FollowUpTaskStatus,
    SalaryChange,
)


class FollowUpPlanTask(BaseSchema):
    id: int
    graduate_id: int
    stage: FollowUpStage
    anchor_date: date
    due_date: date
    status: FollowUpTaskStatus
    assignee: Optional[str] = None
    follow_up_id: Optional[int] = None
    completed_at: Optional[datetime] = None


class FollowUpPlanTaskDetail(FollowUpPlanTask, TimestampSchema):
    pass


class FollowUpPlanDecision(BaseSchema):
    id: int
    task_id: int
    action: FollowUpDecisionAction
    reason: str
    operator: str
    new_due_date: Optional[date] = None
    new_assignee: Optional[str] = None
    created_at: datetime


class GeneratePlansRequest(BaseSchema):
    as_of: Optional[date] = Field(None, description="计划日期，缺省为当天；指定后可重放历史生成过程")
    graduate_ids: Optional[List[int]] = Field(None, description="限定毕业生ID，缺省为全体")
    dry_run: bool = Field(False, description="仅预演不写入，用于在指定日期重放生成过程")


class PlanTaskItem(BaseSchema):
    task_id: Optional[int] = None
    graduate_id: int
    student_id: str
    stage: FollowUpStage
    anchor_date: date
    due_date: date


class SkippedGraduateItem(BaseSchema):
    graduate_id: int
    student_id: str
    reason: str


class GeneratePlansResponse(BaseSchema):
    as_of: date
    dry_run: bool
    created: List[PlanTaskItem]
    cancelled: List[PlanTaskItem]
    skipped: List[SkippedGraduateItem]
    created_count: int
    cancelled_count: int
    skipped_count: int


class TaskActionRequest(BaseSchema):
    reason: str = Field(..., min_length=1, description="决定理由，必填留痕")
    operator: str = Field(..., min_length=1, description="操作人")


class RescheduleRequest(TaskActionRequest):
    new_due_date: date = Field(..., description="改期后的计划回访日期")


class TransferRequest(TaskActionRequest):
    new_assignee: str = Field(..., min_length=1, description="转交后的负责老师")


class CompleteTaskRequest(BaseSchema):
    follow_up_date: date = Field(..., description="实际回访日期")
    operator: str = Field(..., min_length=1, description="操作人")
    reason: Optional[str] = Field(None, description="完成说明，缺省时按阶段自动生成")
    visited_by: Optional[str] = None
    is_aligned: bool = False
    satisfaction_score: Optional[float] = None
    is_still_employed: bool = True
    salary_change: Optional[SalaryChange] = None
    employer_name: Optional[str] = None
    job_title: Optional[str] = None
    remark: Optional[str] = None


class EmployerChangeRequest(BaseSchema):
    employer_name: str = Field(..., min_length=1, description="新用人单位名称")
    employment_start_date: date = Field(..., description="新单位入职日期")
    reason: str = Field(..., min_length=1, description="变更理由，留痕到被取消的任务")
    operator: str = Field(..., min_length=1, description="操作人")
    as_of: Optional[date] = Field(None, description="按计划日期重建任务，缺省为当天")


class EmployerChangeResponse(BaseSchema):
    graduate_id: int
    employer_name: str
    employment_start_date: date
    cancelled: List[PlanTaskItem]
    created: List[PlanTaskItem]
    skipped_reason: Optional[str] = None
