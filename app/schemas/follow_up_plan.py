from datetime import date
from typing import Optional

from pydantic import field_validator

from .common import BaseSchema
from app.models import FollowUpStage, PlanTaskStatus, PlanActionType


class PlanGenerateRequest(BaseSchema):
    graduate_ids: Optional[list[int]] = None
    as_of: Optional[date] = None
    created_by: str = "system"
    assignee: Optional[str] = None
    idempotency_key: Optional[str] = None


class PlanRunOut(BaseSchema):
    id: int
    run_id: str
    idempotency_key: str
    as_of: date
    policy_version: str
    requested_count: int
    created_count: int
    skipped_existing_count: int
    superseded_count: int
    missing_onboard_count: int
    created_by: str


class PlanTaskOut(BaseSchema):
    id: int
    graduate_id: int
    employment_seq: int
    stage: FollowUpStage
    status: PlanTaskStatus
    employer_name: Optional[str] = None
    onboard_date: date
    due_date: date
    scheduled_date: Optional[date] = None
    assignee: Optional[str] = None
    completed_at: Optional[date] = None
    follow_up_id: Optional[int] = None
    generation_run_id: str
    generated_as_of: date


class PlanActionOut(BaseSchema):
    id: int
    plan_id: int
    action_type: PlanActionType
    from_status: Optional[PlanTaskStatus] = None
    to_status: Optional[PlanTaskStatus] = None
    reason: str
    acted_by: str
    acted_at: date


class PlanTaskDetail(PlanTaskOut):
    actions: list[PlanActionOut] = []


class _DispositionBase(BaseSchema):
    reason: str
    acted_by: str
    acted_at: Optional[date] = None

    @field_validator("reason")
    @classmethod
    def _reason_not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("处置理由不能为空")
        return value


class PlanSkipRequest(_DispositionBase):
    pass


class PlanRescheduleRequest(_DispositionBase):
    new_scheduled_date: date


class PlanTransferRequest(_DispositionBase):
    new_assignee: str

    @field_validator("new_assignee")
    @classmethod
    def _assignee_not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("转交必须提供新的负责老师")
        return value


class PlanUnreachableRequest(_DispositionBase):
    pass


class PlanCompleteRequest(_DispositionBase):
    follow_up_date: date
    follow_up_id: Optional[int] = None
    visited_by: Optional[str] = None
    employer_name: Optional[str] = None
    job_title: Optional[str] = None
    is_aligned: bool = False
    is_still_employed: bool = True
    satisfaction_score: Optional[float] = None
    remark: Optional[str] = None

    @field_validator("satisfaction_score")
    @classmethod
    def _score_range(cls, value):
        if value is not None and not (1 <= value <= 5):
            raise ValueError("满意度评分必须在1-5之间")
        return value


class GraduateImportItem(BaseSchema):
    student_id: str
    name: str
    gender: Optional[str] = None
    major: str
    graduation_year: int
    graduation_date: Optional[date] = None
    college_id: int
    has_micro_major: bool = False
    micro_major_id: Optional[int] = None
    onboard_date: Optional[date] = None
    current_employer_name: Optional[str] = None
    destination_status: Optional[str] = None
    destination_type: Optional[str] = None
    unit_industry: Optional[str] = None
    is_aligned: bool = False


class GraduateImportRequest(BaseSchema):
    batch_key: str
    as_of: Optional[date] = None
    assignee: Optional[str] = None
    graduates: list[GraduateImportItem]

    @field_validator("batch_key")
    @classmethod
    def _batch_key_not_blank(cls, value: str) -> str:
        if not value or not value.strip():
            raise ValueError("导入批次标识不能为空")
        return value

    @field_validator("graduates")
    @classmethod
    def _not_empty(cls, value):
        if not value:
            raise ValueError("导入名单不能为空")
        return value


class GraduateImportResult(BaseSchema):
    batch_key: str
    total: int
    inserted: int
    updated: int
    missing_onboard: int
    auto_generated_plans: int
    plan_run: Optional[PlanRunOut] = None
