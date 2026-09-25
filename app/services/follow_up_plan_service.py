"""回访阶段计划的生成、处置与重放服务。

生成过程是确定性的：相同的数据库快照 + 相同 ``as_of`` 日期必然得到
相同的任务集合。批次以幂等键去重，重新导入同一批学生不会重复造任务；
毕业生更换单位时，原周期未完成任务作废，新周期从 3 个月阶段重新排。
"""

import hashlib
import json
from datetime import date
from typing import Sequence

from sqlalchemy.orm import Session

from app.models import (
    DestinationStatus,
    DestinationType,
    EmployerFollowUp,
    FollowUpPlan,
    FollowUpPlanAction,
    FollowUpPlanRun,
    FollowUpStage,
    Graduate,
    GraduateImportBatch,
    PlanActionType,
    PlanTaskStatus,
)
from app.schemas import GraduateImportResult, PlanRunOut
from app.services.follow_up_plan_rules import (
    POLICY_VERSION,
    OPEN_STATUSES,
    PlanMember,
    PlanRuleError,
    PlannedTask,
    PriorCycle,
    TaskActionType,
    VisitEvidence,
    is_valid_visit,
    plan_for_graduate,
    require_reason,
    resolve_task_action,
)

# 老师处置动作到持久化动作枚举的映射。
_ACTION_TO_RECORD = {
    TaskActionType.SKIP: PlanActionType.SKIP,
    TaskActionType.RESCHEDULE: PlanActionType.RESCHEDULE,
    TaskActionType.TRANSFER: PlanActionType.TRANSFER,
    TaskActionType.MARK_UNREACHABLE: PlanActionType.MARK_UNREACHABLE,
    TaskActionType.COMPLETE: PlanActionType.COMPLETE,
}


class PlanServiceError(ValueError):
    """业务层无法完成请求(由接口层映射为 4xx)。"""


def _today() -> date:
    return date.today()


def build_idempotency_key(scope: str, as_of: date) -> str:
    digest = hashlib.sha256(f"{scope}|{as_of.isoformat()}|{POLICY_VERSION}".encode("utf-8"))
    return digest.hexdigest()[:32]


def _scope_token(graduate_ids: Sequence[int] | None) -> str:
    if graduate_ids is None:
        return "ALL"
    return ",".join(str(value) for value in sorted(set(graduate_ids)))


def _load_prior_cycles(db: Session, graduate_id: int) -> list[PriorCycle]:
    plans = (
        db.query(FollowUpPlan)
        .filter(FollowUpPlan.graduate_id == graduate_id)
        .order_by(FollowUpPlan.employment_seq, FollowUpPlan.id)
        .all()
    )
    cycles: dict[int, list[FollowUpPlan]] = {}
    for plan in plans:
        cycles.setdefault(plan.employment_seq, []).append(plan)
    result: list[PriorCycle] = []
    for seq, items in sorted(cycles.items()):
        open_stages = tuple(
            item.stage for item in items if item.status in OPEN_STATUSES
        )
        existing_stages = tuple(item.stage for item in items)
        # 一个周期内入职时间/单位一致，取首条即可。
        anchor = items[0]
        result.append(
            PriorCycle(
                employment_seq=seq,
                onboard_date=anchor.onboard_date,
                employer_name=anchor.employer_name,
                open_stages=open_stages,
                existing_stages=existing_stages,
            )
        )
    return result


def _to_member(graduate: Graduate, as_of: date) -> PlanMember:
    if graduate.onboard_date is None:
        # 缺少入职日期时无法判定阶段，回访投影留空即可。
        visits: tuple[VisitEvidence, ...] = ()
    else:
        visits = tuple(
            VisitEvidence(
                follow_up_id=follow_up.id,
                follow_up_date=follow_up.follow_up_date,
                employer_name=follow_up.employer_name,
            )
            for follow_up in graduate.follow_ups
            if is_valid_visit(
                follow_up.follow_up_date,
                onboard_date=graduate.onboard_date,
                as_of=as_of,
            )
        )
    return PlanMember(
        graduate_id=graduate.id,
        onboard_date=graduate.onboard_date,
        current_employer_name=graduate.current_employer_name,
        graduation_date=graduate.graduation_date,
        visits=visits,
    )


def generate_plans(
    db: Session,
    *,
    graduate_ids: Sequence[int] | None = None,
    as_of: date | None = None,
    created_by: str = "system",
    assignee: str | None = None,
    idempotency_key: str | None = None,
) -> FollowUpPlanRun:
    """为一批毕业生生成(或重放生成)阶段回访计划。

    ``graduate_ids=None`` 覆盖全部毕业生；``as_of`` 决定重放基准日，
    只有在该日期当天(含)已到期的阶段才会建任务。
    """

    as_of = as_of or _today()
    scope = _scope_token(graduate_ids)
    key = idempotency_key or build_idempotency_key(scope, as_of)

    existing_run = db.query(FollowUpPlanRun).filter(
        FollowUpPlanRun.idempotency_key == key
    ).first()
    if existing_run is not None:
        # 同一批学生、同一重放日期的重复生成直接幂等返回。
        return existing_run

    query = db.query(Graduate)
    if graduate_ids is not None:
        ids = sorted(set(graduate_ids))
        graduates = query.filter(Graduate.id.in_(ids)).order_by(Graduate.id).all()
        found = {graduate.id for graduate in graduates}
        missing = sorted(set(ids) - found)
        if missing:
            raise PlanServiceError(f"毕业生不存在: {missing}")
    else:
        graduates = query.order_by(Graduate.id).all()

    run = FollowUpPlanRun(
        run_id=f"run-{as_of.strftime('%Y%m%d')}-{key[:8]}",
        idempotency_key=key,
        as_of=as_of,
        policy_version=POLICY_VERSION,
        requested_count=len(graduates),
        created_by=created_by,
    )
    db.add(run)
    db.flush()  # 取得 run.id

    created_count = 0
    skipped_existing = 0
    superseded_count = 0
    missing_onboard_count = 0

    for graduate in graduates:
        prior_cycles = _load_prior_cycles(db, graduate.id)
        decision = plan_for_graduate(_to_member(graduate, as_of), prior_cycles, as_of)

        if decision.missing_onboard:
            missing_onboard_count += 1
            continue

        if decision.superseded:
            latest_seq = max(seq for seq, _stage in decision.superseded)
            open_plans = (
                db.query(FollowUpPlan)
                .filter(
                    FollowUpPlan.graduate_id == graduate.id,
                    FollowUpPlan.employment_seq == latest_seq,
                    FollowUpPlan.status.in_(list(OPEN_STATUSES)),
                )
                .all()
            )
            for open_plan in open_plans:
                old_status = open_plan.status
                open_plan.status = PlanTaskStatus.SUPERSEDED
                reason = (
                    f"检测到毕业生更换单位/入职时间变化"
                    f"(原入职 {open_plan.onboard_date.isoformat()} "
                    f"{open_plan.employer_name or '未登记单位'} → "
                    f"新入职 {graduate.onboard_date.isoformat()} "
                    f"{graduate.current_employer_name or '未登记单位'})，"
                    f"旧周期任务作废，按批次 {run.run_id} 重新排期"
                )
                db.add(
                    FollowUpPlanAction(
                        plan_id=open_plan.id,
                        action_type=PlanActionType.SUPERSEDE,
                        from_status=old_status,
                        to_status=PlanTaskStatus.SUPERSEDED,
                        reason=reason,
                        acted_by=created_by,
                        acted_at=as_of,
                        extra=json.dumps(
                            {"run_id": run.run_id, "new_seq": decision.active_employment_seq},
                            ensure_ascii=False,
                        ),
                    )
                )
                superseded_count += 1

        for planned in decision.tasks:
            created = _persist_planned_task(
                db,
                graduate=graduate,
                planned=planned,
                run=run,
                as_of=as_of,
                created_by=created_by,
                assignee=assignee,
            )
            if created:
                created_count += 1
            else:
                skipped_existing += 1

    run.created_count = created_count
    run.skipped_existing_count = skipped_existing
    run.superseded_count = superseded_count
    run.missing_onboard_count = missing_onboard_count
    db.commit()
    db.refresh(run)
    return run


def _persist_planned_task(
    db: Session,
    *,
    graduate: Graduate,
    planned: PlannedTask,
    run: FollowUpPlanRun,
    as_of: date,
    created_by: str,
    assignee: str | None,
) -> bool:
    """按纯规则结果落库；已存在同周期同阶段任务时跳过(返回 False)。"""

    duplicate = (
        db.query(FollowUpPlan)
        .filter(
            FollowUpPlan.graduate_id == graduate.id,
            FollowUpPlan.employment_seq == planned.employment_seq,
            FollowUpPlan.stage == planned.stage,
        )
        .first()
    )
    if duplicate is not None:
        return False

    plan = FollowUpPlan(
        graduate_id=graduate.id,
        employment_seq=planned.employment_seq,
        stage=planned.stage,
        status=planned.status,
        employer_name=planned.employer_name,
        onboard_date=graduate.onboard_date,
        due_date=planned.due_date,
        scheduled_date=None,
        assignee=assignee,
        completed_at=planned.completed_at,
        follow_up_id=planned.matched_follow_up_id,
        generation_run_id=run.run_id,
        generated_as_of=as_of,
    )
    db.add(plan)
    db.flush()

    if planned.status is PlanTaskStatus.COMPLETED:
        reason = (
            f"批次 {run.run_id} 生成时依据最近一次有效回访"
            f"({planned.matched_visit_date.isoformat()}) 补建，"
            f"该阶段回访已完成"
        )
        to_status = PlanTaskStatus.COMPLETED
    else:
        reason = (
            f"批次 {run.run_id} 按入职日 {graduate.onboard_date.isoformat()} "
            f"生成{planned.stage.value}阶段任务，应于 {planned.due_date.isoformat()} 前完成"
        )
        to_status = PlanTaskStatus.PENDING
    db.add(
        FollowUpPlanAction(
            plan_id=plan.id,
            action_type=PlanActionType.GENERATE,
            from_status=None,
            to_status=to_status,
            reason=reason,
            acted_by=created_by,
            acted_at=as_of,
            extra=json.dumps(
                {
                    "run_id": run.run_id,
                    "matched_follow_up_id": planned.matched_follow_up_id,
                },
                ensure_ascii=False,
            ),
        )
    )
    return True


def _get_open_plan(db: Session, plan_id: int) -> FollowUpPlan:
    plan = db.query(FollowUpPlan).filter(FollowUpPlan.id == plan_id).first()
    if plan is None:
        raise PlanServiceError("回访计划任务不存在")
    if plan.status not in OPEN_STATUSES:
        raise PlanRuleError(f"任务处于{plan.status.value}状态，不能再执行处置")
    return plan


def _apply_disposition(
    db: Session,
    plan_id: int,
    action: TaskActionType,
    *,
    reason: str,
    acted_by: str,
    acted_at: date | None = None,
    new_scheduled_date: date | None = None,
    new_assignee: str | None = None,
) -> FollowUpPlan:
    acted_at = acted_at or _today()
    require_reason(reason)
    plan = _get_open_plan(db, plan_id)
    target_status = resolve_task_action(
        current_status=plan.status,
        action=action,
        reason=reason,
        new_scheduled_date=new_scheduled_date,
        new_assignee=new_assignee,
    )
    old_status = plan.status
    extra: dict = {}
    if action is TaskActionType.RESCHEDULE:
        plan.scheduled_date = new_scheduled_date
        extra["new_scheduled_date"] = new_scheduled_date.isoformat()
    if action is TaskActionType.TRANSFER:
        plan.assignee = new_assignee
        extra["new_assignee"] = new_assignee

    plan.status = target_status
    db.add(
        FollowUpPlanAction(
            plan_id=plan.id,
            action_type=_ACTION_TO_RECORD[action],
            from_status=old_status,
            to_status=target_status,
            reason=reason.strip(),
            acted_by=acted_by,
            acted_at=acted_at,
            extra=json.dumps(extra, ensure_ascii=False) if extra else None,
        )
    )
    db.commit()
    db.refresh(plan)
    return plan


def skip_task(db, plan_id, *, reason, acted_by, acted_at=None):
    return _apply_disposition(
        db, plan_id, TaskActionType.SKIP,
        reason=reason, acted_by=acted_by, acted_at=acted_at,
    )


def reschedule_task(db, plan_id, *, new_scheduled_date, reason, acted_by, acted_at=None):
    return _apply_disposition(
        db, plan_id, TaskActionType.RESCHEDULE,
        reason=reason, acted_by=acted_by, acted_at=acted_at,
        new_scheduled_date=new_scheduled_date,
    )


def transfer_task(db, plan_id, *, new_assignee, reason, acted_by, acted_at=None):
    return _apply_disposition(
        db, plan_id, TaskActionType.TRANSFER,
        reason=reason, acted_by=acted_by, acted_at=acted_at,
        new_assignee=new_assignee,
    )


def mark_unreachable(db, plan_id, *, reason, acted_by, acted_at=None):
    return _apply_disposition(
        db, plan_id, TaskActionType.MARK_UNREACHABLE,
        reason=reason, acted_by=acted_by, acted_at=acted_at,
    )


def complete_task(
    db: Session,
    plan_id: int,
    *,
    reason: str,
    acted_by: str,
    follow_up_date: date,
    visited_by: str | None = None,
    employer_name: str | None = None,
    job_title: str | None = None,
    is_aligned: bool = False,
    is_still_employed: bool = True,
    satisfaction_score: float | None = None,
    remark: str | None = None,
    follow_up_id: int | None = None,
    acted_at: date | None = None,
) -> FollowUpPlan:
    """登记一次实际回访并只推进该任务对应的阶段。"""

    acted_at = acted_at or _today()
    require_reason(reason)
    plan = _get_open_plan(db, plan_id)
    graduate = db.query(Graduate).filter(Graduate.id == plan.graduate_id).first()
    if graduate is None:
        raise PlanServiceError("毕业生不存在")
    if follow_up_date < plan.onboard_date:
        raise PlanRuleError("回访日期不能早于入职时间")
    if satisfaction_score is not None and not (1 <= satisfaction_score <= 5):
        raise PlanRuleError("满意度评分必须在1-5之间")

    if follow_up_id is not None:
        linked = db.query(EmployerFollowUp).filter(
            EmployerFollowUp.id == follow_up_id,
            EmployerFollowUp.graduate_id == graduate.id,
        ).first()
        if linked is None:
            raise PlanServiceError("关联的回访记录不存在或不属于该毕业生")
    else:
        linked = EmployerFollowUp(
            graduate_id=graduate.id,
            follow_up_date=follow_up_date,
            is_aligned=is_aligned,
            is_still_employed=is_still_employed,
            satisfaction_score=satisfaction_score,
            employer_name=employer_name,
            job_title=job_title,
            remark=remark,
            visited_by=visited_by or acted_by,
        )
        db.add(linked)
        db.flush()

    old_status = plan.status
    plan.status = PlanTaskStatus.COMPLETED
    plan.completed_at = follow_up_date
    plan.follow_up_id = linked.id
    db.add(
        FollowUpPlanAction(
            plan_id=plan.id,
            action_type=PlanActionType.COMPLETE,
            from_status=old_status,
            to_status=PlanTaskStatus.COMPLETED,
            reason=reason.strip(),
            acted_by=acted_by,
            acted_at=acted_at,
            extra=json.dumps(
                {"follow_up_id": linked.id, "follow_up_date": follow_up_date.isoformat()},
                ensure_ascii=False,
            ),
        )
    )
    db.commit()
    db.refresh(plan)
    return plan


def list_plans(
    db: Session,
    *,
    graduate_id: int | None = None,
    status: PlanTaskStatus | None = None,
    stage: FollowUpStage | None = None,
    assignee: str | None = None,
    employment_seq: int | None = None,
    due_before: date | None = None,
    skip: int = 0,
    limit: int = 100,
) -> list[FollowUpPlan]:
    query = db.query(FollowUpPlan)
    if graduate_id is not None:
        query = query.filter(FollowUpPlan.graduate_id == graduate_id)
    if status is not None:
        query = query.filter(FollowUpPlan.status == status)
    if stage is not None:
        query = query.filter(FollowUpPlan.stage == stage)
    if assignee is not None:
        query = query.filter(FollowUpPlan.assignee == assignee)
    if employment_seq is not None:
        query = query.filter(FollowUpPlan.employment_seq == employment_seq)
    if due_before is not None:
        query = query.filter(FollowUpPlan.due_date <= due_before)
    return (
        query.order_by(FollowUpPlan.due_date, FollowUpPlan.id)
        .offset(skip)
        .limit(limit)
        .all()
    )


def get_plan(db: Session, plan_id: int) -> FollowUpPlan:
    plan = db.query(FollowUpPlan).filter(FollowUpPlan.id == plan_id).first()
    if plan is None:
        raise PlanServiceError("回访计划任务不存在")
    return plan


def list_runs(db: Session) -> list[FollowUpPlanRun]:
    return db.query(FollowUpPlanRun).order_by(FollowUpPlanRun.id.desc()).all()


_GRADUATE_IMPORT_FIELDS = (
    "name",
    "gender",
    "major",
    "graduation_year",
    "graduation_date",
    "college_id",
    "has_micro_major",
    "micro_major_id",
    "onboard_date",
    "current_employer_name",
    "unit_industry",
    "is_aligned",
)

_ENUM_IMPORT_FIELDS = {
    "destination_status": DestinationStatus,
    "destination_type": DestinationType,
}


def _coerce_enum(enum_cls, value):
    if value is None:
        return None
    if isinstance(value, enum_cls):
        return value
    for member in enum_cls:
        if member.value == value or member.name == value:
            return member
    raise PlanServiceError(f"枚举取值无法识别: {value}")


def import_graduates_batch(
    db: Session,
    *,
    batch_key: str,
    items: Sequence,
    as_of: date | None = None,
    imported_by: str = "system",
    assignee: str | None = None,
):
    """按学号幂等导入一批毕业生并联动生成回访计划。

    同一 ``batch_key`` 重复导入时直接返回首次结果，既不重复建档，
    也不会重复造任务。
    """

    as_of = as_of or _today()
    prior_batch = db.query(GraduateImportBatch).filter(
        GraduateImportBatch.batch_key == batch_key
    ).first()
    if prior_batch is not None:
        run = None
        if prior_batch.plan_run_id:
            run = db.query(FollowUpPlanRun).filter(
                FollowUpPlanRun.run_id == prior_batch.plan_run_id
            ).first()
        return GraduateImportResult(
            batch_key=batch_key,
            total=prior_batch.total,
            inserted=prior_batch.inserted,
            updated=prior_batch.updated,
            missing_onboard=prior_batch.missing_onboard,
            auto_generated_plans=0,
            plan_run=PlanRunOut.model_validate(run) if run else None,
        )

    student_ids = [item.student_id for item in items]
    existing = {
        graduate.student_id: graduate
        for graduate in db.query(Graduate).filter(Graduate.student_id.in_(student_ids)).all()
    }

    inserted = 0
    updated = 0
    missing_onboard = 0
    seen: set[str] = set()

    for item in items:
        if item.student_id in seen:
            raise PlanServiceError(f"导入名单中学号重复: {item.student_id}")
        seen.add(item.student_id)

        data = item.model_dump()
        graduate = existing.get(item.student_id)
        if graduate is None:
            graduate = Graduate(student_id=item.student_id)
            db.add(graduate)
            inserted += 1
        else:
            updated += 1
        for field in _GRADUATE_IMPORT_FIELDS:
            if field in data and data[field] is not None:
                setattr(graduate, field, data[field])
        for field, enum_cls in _ENUM_IMPORT_FIELDS.items():
            if data.get(field) is not None:
                setattr(graduate, field, _coerce_enum(enum_cls, data[field]))
        if graduate.onboard_date is None:
            missing_onboard += 1

    db.flush()

    graduate_ids = sorted(
        graduate.id for graduate in db.query(Graduate)
        .filter(Graduate.student_id.in_(student_ids)).all()
    )

    plan_idempotency_key = build_idempotency_key(f"import:{batch_key}", as_of)
    run = generate_plans(
        db,
        graduate_ids=graduate_ids,
        as_of=as_of,
        created_by=imported_by,
        assignee=assignee,
        idempotency_key=plan_idempotency_key,
    )

    batch = GraduateImportBatch(
        batch_key=batch_key,
        total=len(items),
        inserted=inserted,
        updated=updated,
        missing_onboard=missing_onboard,
        plan_run_id=run.run_id,
        imported_by=imported_by,
    )
    db.add(batch)
    db.commit()

    return GraduateImportResult(
        batch_key=batch_key,
        total=len(items),
        inserted=inserted,
        updated=updated,
        missing_onboard=missing_onboard,
        auto_generated_plans=run.created_count,
        plan_run=PlanRunOut.model_validate(run),
    )
