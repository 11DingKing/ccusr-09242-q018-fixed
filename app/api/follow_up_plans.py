from datetime import date
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core import get_db
from app.models import FollowUpStage, PlanTaskStatus
from app.schemas import (
    GraduateImportRequest,
    GraduateImportResult,
    PlanActionOut,
    PlanCompleteRequest,
    PlanGenerateRequest,
    PlanRescheduleRequest,
    PlanRunOut,
    PlanSkipRequest,
    PlanTaskDetail,
    PlanTaskOut,
    PlanTransferRequest,
    PlanUnreachableRequest,
)
from app.services import follow_up_plan_service as service
from app.services.follow_up_plan_rules import PlanRuleError

router = APIRouter(prefix="/follow-up-plans", tags=["毕业生回访计划"])


def _parse_stage(value: Optional[str]) -> Optional[FollowUpStage]:
    if value is None:
        return None
    for stage in FollowUpStage:
        if stage.value == value or stage.name == value:
            return stage
    raise HTTPException(status_code=400, detail=f"未知回访阶段: {value}")


def _parse_status(value: Optional[str]) -> Optional[PlanTaskStatus]:
    if value is None:
        return None
    for status in PlanTaskStatus:
        if status.value == value or status.name == value:
            return status
    raise HTTPException(status_code=400, detail=f"未知任务状态: {value}")


@router.post("/generate", response_model=PlanRunOut)
def generate(request: PlanGenerateRequest, db: Session = Depends(get_db)):
    try:
        run = service.generate_plans(
            db,
            graduate_ids=request.graduate_ids,
            as_of=request.as_of,
            created_by=request.created_by,
            assignee=request.assignee,
            idempotency_key=request.idempotency_key,
        )
    except service.PlanServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return run


@router.post("/graduates/import", response_model=GraduateImportResult)
def import_graduates(request: GraduateImportRequest, db: Session = Depends(get_db)):
    try:
        return service.import_graduates_batch(
            db,
            batch_key=request.batch_key,
            items=request.graduates,
            as_of=request.as_of,
            assignee=request.assignee,
            imported_by="batch-import",
        )
    except service.PlanServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/runs", response_model=List[PlanRunOut])
def list_runs(db: Session = Depends(get_db)):
    return service.list_runs(db)


@router.get("", response_model=List[PlanTaskOut])
def list_plans(
    graduate_id: Optional[int] = Query(None, description="毕业生ID"),
    status: Optional[str] = Query(None, description="任务状态"),
    stage: Optional[str] = Query(None, description="回访阶段"),
    assignee: Optional[str] = Query(None, description="负责老师"),
    due_before: Optional[date] = Query(None, description="到期日不晚于"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    return service.list_plans(
        db,
        graduate_id=graduate_id,
        status=_parse_status(status),
        stage=_parse_stage(stage),
        assignee=assignee,
        due_before=due_before,
        skip=skip,
        limit=limit,
    )


@router.get("/{plan_id}", response_model=PlanTaskDetail)
def get_plan(plan_id: int, db: Session = Depends(get_db)):
    try:
        return service.get_plan(db, plan_id)
    except service.PlanServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/{plan_id}/actions", response_model=List[PlanActionOut])
def list_plan_actions(plan_id: int, db: Session = Depends(get_db)):
    try:
        plan = service.get_plan(db, plan_id)
    except service.PlanServiceError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return plan.actions


def _handle_rule_error(exc: Exception) -> HTTPException:
    if isinstance(exc, PlanRuleError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, service.PlanServiceError):
        return HTTPException(status_code=404, detail=str(exc))
    raise exc


@router.post("/{plan_id}/skip", response_model=PlanTaskOut)
def skip_plan(plan_id: int, request: PlanSkipRequest, db: Session = Depends(get_db)):
    try:
        return service.skip_task(
            db, plan_id,
            reason=request.reason,
            acted_by=request.acted_by,
            acted_at=request.acted_at,
        )
    except (PlanRuleError, service.PlanServiceError) as exc:
        raise _handle_rule_error(exc)


@router.post("/{plan_id}/reschedule", response_model=PlanTaskOut)
def reschedule_plan(plan_id: int, request: PlanRescheduleRequest, db: Session = Depends(get_db)):
    try:
        return service.reschedule_task(
            db, plan_id,
            new_scheduled_date=request.new_scheduled_date,
            reason=request.reason,
            acted_by=request.acted_by,
            acted_at=request.acted_at,
        )
    except (PlanRuleError, service.PlanServiceError) as exc:
        raise _handle_rule_error(exc)


@router.post("/{plan_id}/transfer", response_model=PlanTaskOut)
def transfer_plan(plan_id: int, request: PlanTransferRequest, db: Session = Depends(get_db)):
    try:
        return service.transfer_task(
            db, plan_id,
            new_assignee=request.new_assignee,
            reason=request.reason,
            acted_by=request.acted_by,
            acted_at=request.acted_at,
        )
    except (PlanRuleError, service.PlanServiceError) as exc:
        raise _handle_rule_error(exc)


@router.post("/{plan_id}/unreachable", response_model=PlanTaskOut)
def mark_unreachable(plan_id: int, request: PlanUnreachableRequest, db: Session = Depends(get_db)):
    try:
        return service.mark_unreachable(
            db, plan_id,
            reason=request.reason,
            acted_by=request.acted_by,
            acted_at=request.acted_at,
        )
    except (PlanRuleError, service.PlanServiceError) as exc:
        raise _handle_rule_error(exc)


@router.post("/{plan_id}/complete", response_model=PlanTaskOut)
def complete_plan(plan_id: int, request: PlanCompleteRequest, db: Session = Depends(get_db)):
    try:
        return service.complete_task(
            db, plan_id,
            reason=request.reason,
            acted_by=request.acted_by,
            follow_up_date=request.follow_up_date,
            follow_up_id=request.follow_up_id,
            visited_by=request.visited_by,
            employer_name=request.employer_name,
            job_title=request.job_title,
            is_aligned=request.is_aligned,
            is_still_employed=request.is_still_employed,
            satisfaction_score=request.satisfaction_score,
            remark=request.remark,
            acted_at=request.acted_at,
        )
    except (PlanRuleError, service.PlanServiceError) as exc:
        raise _handle_rule_error(exc)
