from datetime import date, datetime
from typing import List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core import get_db
from app.models import (
    EmployerFollowUp,
    FollowUpDecisionAction,
    FollowUpPlanDecision,
    FollowUpPlanTask,
    FollowUpStage,
    FollowUpTaskStatus,
    Graduate,
)
from app.schemas import (
    CompleteTaskRequest,
    EmployerChangeRequest,
    EmployerChangeResponse,
    FollowUpPlanDecision as DecisionSchema,
    FollowUpPlanTask as TaskSchema,
    FollowUpPlanTaskDetail as TaskDetailSchema,
    GeneratePlansRequest,
    GeneratePlansResponse,
    PlanTaskItem,
    RescheduleRequest,
    SkippedGraduateItem,
    TaskActionRequest,
    TransferRequest,
)
from app.services import ExistingTaskView, compute_graduate_plan

router = APIRouter(prefix="/follow-up-plans", tags=["回访计划"])

SYSTEM_OPERATOR = "system"


def _task_item(task: FollowUpPlanTask, student_id: str) -> PlanTaskItem:
    return PlanTaskItem(
        task_id=task.id,
        graduate_id=task.graduate_id,
        student_id=student_id,
        stage=task.stage,
        anchor_date=task.anchor_date,
        due_date=task.due_date,
    )


def _record_decision(
    db: Session,
    task: FollowUpPlanTask,
    action: FollowUpDecisionAction,
    reason: str,
    operator: str,
    new_due_date: Optional[date] = None,
    new_assignee: Optional[str] = None,
) -> None:
    db.add(FollowUpPlanDecision(
        task_id=task.id,
        action=action,
        reason=reason,
        operator=operator,
        new_due_date=new_due_date,
        new_assignee=new_assignee,
    ))


def _reconcile_graduate(
    db: Session,
    graduate: Graduate,
    as_of: date,
    *,
    cancel_reason: str,
    create_reason: str,
    operator: str,
    dry_run: bool = False,
) -> Tuple[List[PlanTaskItem], List[PlanTaskItem], Optional[str]]:
    """按当前入职信息对齐单个毕业生的计划任务，返回(新建, 取消, 跳过原因)。"""
    follow_up_dates = [
        row[0]
        for row in db.query(EmployerFollowUp.follow_up_date)
        .filter(EmployerFollowUp.graduate_id == graduate.id)
        .all()
    ]
    tasks = db.query(FollowUpPlanTask).filter(
        FollowUpPlanTask.graduate_id == graduate.id
    ).all()
    views = [
        ExistingTaskView(
            task_id=t.id, stage=t.stage, anchor_date=t.anchor_date,
            due_date=t.due_date, status=t.status,
        )
        for t in tasks
    ]
    plan = compute_graduate_plan(
        graduation_year=graduate.graduation_year,
        destination_type=graduate.destination_type,
        employment_start_date=graduate.employment_start_date,
        follow_up_dates=follow_up_dates,
        existing_tasks=views,
        as_of=as_of,
    )
    if not plan.eligible:
        return [], [], plan.skip_reason

    by_id = {t.id: t for t in tasks}
    created: List[PlanTaskItem] = []
    cancelled: List[PlanTaskItem] = []

    for cancel in plan.cancels:
        task = by_id[cancel.task_id]
        cancelled.append(_task_item(task, graduate.student_id))
        if not dry_run:
            task.status = FollowUpTaskStatus.CANCELLED
            _record_decision(db, task, FollowUpDecisionAction.CANCEL, cancel_reason, operator)

    for create in plan.creates:
        item = PlanTaskItem(
            task_id=None,
            graduate_id=graduate.id,
            student_id=graduate.student_id,
            stage=create.stage,
            anchor_date=create.anchor_date,
            due_date=create.due_date,
        )
        if not dry_run:
            task = FollowUpPlanTask(
                graduate_id=graduate.id,
                stage=create.stage,
                anchor_date=create.anchor_date,
                due_date=create.due_date,
                status=FollowUpTaskStatus.PENDING,
            )
            db.add(task)
            db.flush()
            _record_decision(
                db, task, FollowUpDecisionAction.GENERATE,
                f"{create_reason}：{create.stage.value}", operator,
            )
            item.task_id = task.id
        created.append(item)

    return created, cancelled, None


@router.post("/generate", response_model=GeneratePlansResponse)
def generate_plans(request: GeneratePlansRequest, db: Session = Depends(get_db)):
    as_of = request.as_of or date.today()
    query = db.query(Graduate).order_by(Graduate.id)
    if request.graduate_ids:
        query = query.filter(Graduate.id.in_(request.graduate_ids))

    created: List[PlanTaskItem] = []
    cancelled: List[PlanTaskItem] = []
    skipped: List[SkippedGraduateItem] = []

    for graduate in query.all():
        c, x, reason = _reconcile_graduate(
            db, graduate, as_of,
            cancel_reason="入职日期或单位变更，原阶段任务取消",
            create_reason="按计划生成回访任务",
            operator=SYSTEM_OPERATOR,
            dry_run=request.dry_run,
        )
        created.extend(c)
        cancelled.extend(x)
        if reason:
            skipped.append(SkippedGraduateItem(
                graduate_id=graduate.id,
                student_id=graduate.student_id,
                reason=reason,
            ))

    if request.dry_run:
        db.rollback()
    else:
        db.commit()

    return GeneratePlansResponse(
        as_of=as_of,
        dry_run=request.dry_run,
        created=created,
        cancelled=cancelled,
        skipped=skipped,
        created_count=len(created),
        cancelled_count=len(cancelled),
        skipped_count=len(skipped),
    )


@router.get("/tasks", response_model=List[TaskDetailSchema])
def list_tasks(
    graduate_id: Optional[int] = Query(None, description="毕业生ID"),
    status: Optional[FollowUpTaskStatus] = Query(None, description="任务状态"),
    stage: Optional[FollowUpStage] = Query(None, description="回访阶段"),
    assignee: Optional[str] = Query(None, description="负责老师"),
    due_before: Optional[date] = Query(None, description="计划回访日期上限(含)"),
    due_after: Optional[date] = Query(None, description="计划回访日期下限(含)"),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    query = db.query(FollowUpPlanTask)
    if graduate_id is not None:
        query = query.filter(FollowUpPlanTask.graduate_id == graduate_id)
    if status is not None:
        query = query.filter(FollowUpPlanTask.status == status)
    if stage is not None:
        query = query.filter(FollowUpPlanTask.stage == stage)
    if assignee is not None:
        query = query.filter(FollowUpPlanTask.assignee == assignee)
    if due_before is not None:
        query = query.filter(FollowUpPlanTask.due_date <= due_before)
    if due_after is not None:
        query = query.filter(FollowUpPlanTask.due_date >= due_after)
    return query.order_by(FollowUpPlanTask.due_date, FollowUpPlanTask.id).offset(skip).limit(limit).all()


def _get_task_or_404(task_id: int, db: Session) -> FollowUpPlanTask:
    task = db.query(FollowUpPlanTask).filter(FollowUpPlanTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="回访计划任务不存在")
    return task


def _require_pending(task: FollowUpPlanTask) -> None:
    if task.status != FollowUpTaskStatus.PENDING:
        raise HTTPException(status_code=409, detail="仅待回访任务可以执行该操作")


@router.get("/tasks/{task_id}", response_model=TaskDetailSchema)
def get_task(task_id: int, db: Session = Depends(get_db)):
    return _get_task_or_404(task_id, db)


@router.get("/tasks/{task_id}/decisions", response_model=List[DecisionSchema])
def list_task_decisions(task_id: int, db: Session = Depends(get_db)):
    _get_task_or_404(task_id, db)
    return db.query(FollowUpPlanDecision).filter(
        FollowUpPlanDecision.task_id == task_id
    ).order_by(FollowUpPlanDecision.created_at, FollowUpPlanDecision.id).all()


@router.post("/tasks/{task_id}/complete", response_model=TaskDetailSchema)
def complete_task(task_id: int, request: CompleteTaskRequest, db: Session = Depends(get_db)):
    task = _get_task_or_404(task_id, db)
    _require_pending(task)

    if request.satisfaction_score is not None:
        if request.satisfaction_score < 1 or request.satisfaction_score > 5:
            raise HTTPException(status_code=400, detail="满意度评分必须在1-5之间")

    follow_up = EmployerFollowUp(
        graduate_id=task.graduate_id,
        follow_up_date=request.follow_up_date,
        is_aligned=request.is_aligned,
        satisfaction_score=request.satisfaction_score,
        is_still_employed=request.is_still_employed,
        salary_change=request.salary_change,
        employer_name=request.employer_name,
        job_title=request.job_title,
        remark=request.remark,
        visited_by=request.visited_by or request.operator,
    )
    db.add(follow_up)
    db.flush()

    task.status = FollowUpTaskStatus.COMPLETED
    task.follow_up_id = follow_up.id
    task.completed_at = datetime.now()
    _record_decision(
        db, task, FollowUpDecisionAction.COMPLETE,
        request.reason or f"完成{task.stage.value}回访", request.operator,
    )
    db.commit()
    db.refresh(task)
    return task


@router.post("/tasks/{task_id}/skip", response_model=TaskDetailSchema)
def skip_task(task_id: int, request: TaskActionRequest, db: Session = Depends(get_db)):
    task = _get_task_or_404(task_id, db)
    _require_pending(task)
    task.status = FollowUpTaskStatus.SKIPPED
    _record_decision(db, task, FollowUpDecisionAction.SKIP, request.reason, request.operator)
    db.commit()
    db.refresh(task)
    return task


@router.post("/tasks/{task_id}/reschedule", response_model=TaskDetailSchema)
def reschedule_task(task_id: int, request: RescheduleRequest, db: Session = Depends(get_db)):
    task = _get_task_or_404(task_id, db)
    _require_pending(task)
    task.due_date = request.new_due_date
    _record_decision(
        db, task, FollowUpDecisionAction.RESCHEDULE,
        request.reason, request.operator, new_due_date=request.new_due_date,
    )
    db.commit()
    db.refresh(task)
    return task


@router.post("/tasks/{task_id}/transfer", response_model=TaskDetailSchema)
def transfer_task(task_id: int, request: TransferRequest, db: Session = Depends(get_db)):
    task = _get_task_or_404(task_id, db)
    _require_pending(task)
    task.assignee = request.new_assignee
    _record_decision(
        db, task, FollowUpDecisionAction.TRANSFER,
        request.reason, request.operator, new_assignee=request.new_assignee,
    )
    db.commit()
    db.refresh(task)
    return task


@router.post("/tasks/{task_id}/unreachable", response_model=TaskDetailSchema)
def mark_task_unreachable(task_id: int, request: TaskActionRequest, db: Session = Depends(get_db)):
    task = _get_task_or_404(task_id, db)
    _require_pending(task)
    task.status = FollowUpTaskStatus.UNREACHABLE
    _record_decision(
        db, task, FollowUpDecisionAction.MARK_UNREACHABLE, request.reason, request.operator,
    )
    db.commit()
    db.refresh(task)
    return task


@router.post("/graduates/{graduate_id}/employer-change", response_model=EmployerChangeResponse)
def change_employer(graduate_id: int, request: EmployerChangeRequest, db: Session = Depends(get_db)):
    graduate = db.query(Graduate).filter(Graduate.id == graduate_id).first()
    if not graduate:
        raise HTTPException(status_code=404, detail="毕业生不存在")

    graduate.employer_name = request.employer_name
    graduate.employment_start_date = request.employment_start_date
    as_of = request.as_of or date.today()

    created, cancelled, skip_reason = _reconcile_graduate(
        db, graduate, as_of,
        cancel_reason=f"单位变更：{request.reason}",
        create_reason="单位变更后按新入职日期重建回访任务",
        operator=request.operator,
    )
    db.commit()

    return EmployerChangeResponse(
        graduate_id=graduate.id,
        employer_name=graduate.employer_name,
        employment_start_date=graduate.employment_start_date,
        cancelled=cancelled,
        created=created,
        skipped_reason=skip_reason,
    )
