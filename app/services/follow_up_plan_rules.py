"""毕业生入职后回访阶段计划的纯业务规则。

规则不依赖数据库与当前时钟，所有判断都显式传入 ``as_of`` 日期，
因此同一份输入数据在同一个重放日期必然得到同一份计划。

阶段固定为入职后 3 / 6 / 12 个月，每次计划生成只创建在 ``as_of``
当天(含)已经到期的阶段任务，从而支持在任意指定日期重放。
"""

import calendar
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Mapping, Sequence

from app.models import FollowUpStage, PlanTaskStatus, STAGE_ORDER


POLICY_VERSION = "follow-up-plan-v1"

STAGE_MONTHS: Mapping[FollowUpStage, int] = {stage: stage.stage_month for stage in STAGE_ORDER}

# 各阶段可被回访记录命中的窗口相对上一阶段到期日的归属关系在
# ``stage_index_for_visit`` 中实现，避免散落的魔法数字。


class PlanRuleError(ValueError):
    """计划规则本身不允许的输入。"""


@dataclass(frozen=True)
class VisitEvidence:
    """一条可用于判定阶段完成情况的有效回访记录。"""

    follow_up_id: int
    follow_up_date: date
    employer_name: str | None = None


@dataclass(frozen=True)
class PlanMember:
    """生成单个毕业生计划所需的稳定投影。"""

    graduate_id: int
    onboard_date: date | None
    current_employer_name: str | None = None
    graduation_date: date | None = None
    visits: tuple[VisitEvidence, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class PriorCycle:
    """此前已经生成过的一次入职周期的任务状态摘要。"""

    employment_seq: int
    onboard_date: date
    employer_name: str | None
    open_stages: tuple[FollowUpStage, ...]
    existing_stages: tuple[FollowUpStage, ...]


@dataclass(frozen=True)
class PlannedTask:
    employment_seq: int
    stage: FollowUpStage
    due_date: date
    status: PlanTaskStatus
    employer_name: str | None
    completed_at: date | None
    matched_follow_up_id: int | None
    matched_visit_date: date | None


@dataclass(frozen=True)
class GraduatePlanDecision:
    graduate_id: int
    missing_onboard: bool = False
    employer_changed: bool = False
    active_employment_seq: int | None = None
    superseded: tuple[tuple[int, FollowUpStage], ...] = ()
    tasks: tuple[PlannedTask, ...] = ()


def add_months(day: date, months: int) -> date:
    """按月推进，源日为月末或超出目标月天数时取目标月月末。"""

    source_last_day = calendar.monthrange(day.year, day.month)[1]
    month_index = day.year * 12 + (day.month - 1) + months
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    target_last_day = calendar.monthrange(year, month)[1]
    if day.day == source_last_day:
        target_day = target_last_day
    else:
        target_day = min(day.day, target_last_day)
    return date(year, month, target_day)


def stage_due_date(onboard_date: date, stage: FollowUpStage) -> date:
    return add_months(onboard_date, STAGE_MONTHS[stage])


def stage_index_for_visit(onboard_date: date, visit_date: date) -> int | None:
    """回访日期当时(或之前)已经覆盖到的最后一个阶段序号。

    例如入职后第 8 个月的回访证明 3 个月与 6 个月阶段均已覆盖，
    返回序号 1(6个月阶段)；入职前的回访不计入任何阶段。
    """

    if visit_date < onboard_date:
        return None
    covered: int | None = None
    for index, stage in enumerate(STAGE_ORDER):
        if visit_date >= stage_due_date(onboard_date, stage):
            covered = index
        else:
            break
    return covered


def is_valid_visit(visit_date: date | None, *, onboard_date: date, as_of: date) -> bool:
    """有效回访：日期明确、不早于入职、且发生在重放日期之前(含)。"""

    return visit_date is not None and onboard_date <= visit_date <= as_of


def detect_employer_change(
    *,
    onboard_date: date,
    employer_name: str | None,
    latest_cycle: PriorCycle | None,
) -> bool:
    """判断当前入职信息是否构成一次新的入职周期(单位变更)。

    入职时间变化必然视为更换单位；双方都登记了单位名称且不一致也视为变更。
    只有一方缺少名称时不做臆测，避免把补录误判成跳槽。
    """

    if latest_cycle is None:
        return False
    if onboard_date != latest_cycle.onboard_date:
        return True
    if (
        employer_name
        and latest_cycle.employer_name
        and employer_name.strip() != latest_cycle.employer_name.strip()
    ):
        return True
    return False


def plan_for_graduate(
    member: PlanMember,
    prior_cycles: Sequence[PriorCycle],
    as_of: date,
) -> GraduatePlanDecision:
    """依据毕业/入职时间、历史回访与既有任务，计算该毕业生的计划决定。"""

    if member.onboard_date is None:
        return GraduatePlanDecision(graduate_id=member.graduate_id, missing_onboard=True)

    onboard_date = member.onboard_date
    if onboard_date > as_of:
        # 重放时点尚未入职，不产生任何任务。
        return GraduatePlanDecision(graduate_id=member.graduate_id)

    ordered_prior = sorted(prior_cycles, key=lambda cycle: cycle.employment_seq)
    latest_cycle = ordered_prior[-1] if ordered_prior else None
    employer_changed = detect_employer_change(
        onboard_date=onboard_date,
        employer_name=member.current_employer_name,
        latest_cycle=latest_cycle,
    )

    if latest_cycle is None:
        active_seq = 1
        superseded: tuple[tuple[int, FollowUpStage], ...] = ()
        existing_stages: frozenset[FollowUpStage] = frozenset()
    elif employer_changed:
        active_seq = latest_cycle.employment_seq + 1
        superseded = tuple(
            (latest_cycle.employment_seq, stage) for stage in latest_cycle.open_stages
        )
        existing_stages = frozenset()
    else:
        active_seq = latest_cycle.employment_seq
        superseded = ()
        existing_stages = frozenset(latest_cycle.existing_stages)

    # 只统计当前入职周期内、且发生在重放日期前的有效回访。
    cycle_visits = sorted(
        (
            visit
            for visit in member.visits
            if is_valid_visit(
                visit.follow_up_date, onboard_date=onboard_date, as_of=as_of
            )
        ),
        key=lambda item: (item.follow_up_date, item.follow_up_id),
    )

    # 某阶段已有回访覆盖，当且仅当存在不早于该阶段到期日的有效回访；
    # 取到期日之后最早的一次作为该阶段的回访凭证。
    due_dates = [stage_due_date(onboard_date, stage) for stage in STAGE_ORDER]

    tasks: list[PlannedTask] = []
    for index, stage in enumerate(STAGE_ORDER):
        due = due_dates[index]
        if due > as_of:
            continue  # 重放时点尚未到期的阶段不预生成
        if stage in existing_stages:
            continue  # 幂等：任务已存在，不重复造
        matched = next(
            (
                visit
                for visit in cycle_visits
                if visit.follow_up_date >= due
            ),
            None,
        )
        if matched is not None:
            tasks.append(
                PlannedTask(
                    employment_seq=active_seq,
                    stage=stage,
                    due_date=due,
                    status=PlanTaskStatus.COMPLETED,
                    employer_name=member.current_employer_name,
                    completed_at=matched.follow_up_date,
                    matched_follow_up_id=matched.follow_up_id,
                    matched_visit_date=matched.follow_up_date,
                )
            )
        else:
            tasks.append(
                PlannedTask(
                    employment_seq=active_seq,
                    stage=stage,
                    due_date=due,
                    status=PlanTaskStatus.PENDING,
                    employer_name=member.current_employer_name,
                    completed_at=None,
                    matched_follow_up_id=None,
                    matched_visit_date=None,
                )
            )

    return GraduatePlanDecision(
        graduate_id=member.graduate_id,
        missing_onboard=False,
        employer_changed=employer_changed,
        active_employment_seq=active_seq,
        superseded=superseded,
        tasks=tuple(tasks),
    )


def _next_seq(prior_cycles: Sequence[PriorCycle]) -> int:
    if not prior_cycles:
        return 1
    return max(cycle.employment_seq for cycle in prior_cycles) + 1


# 老师可对开放中的任务执行的处置动作。
OPEN_STATUSES = frozenset(
    {
        PlanTaskStatus.PENDING,
        PlanTaskStatus.RESCHEDULED,
        PlanTaskStatus.TRANSFERRED,
    }
)

TERMINAL_STATUSES = frozenset(
    {
        PlanTaskStatus.COMPLETED,
        PlanTaskStatus.SKIPPED,
        PlanTaskStatus.UNREACHABLE,
        PlanTaskStatus.SUPERSEDED,
    }
)


class TaskActionType(str, Enum):
    SKIP = "skip"
    RESCHEDULE = "reschedule"
    TRANSFER = "transfer"
    MARK_UNREACHABLE = "mark_unreachable"
    COMPLETE = "complete"


def require_reason(reason: str | None) -> str:
    text = (reason or "").strip()
    if not text:
        raise PlanRuleError("处置理由不能为空")
    return text


def resolve_task_action(
    *,
    current_status: PlanTaskStatus,
    action: TaskActionType,
    reason: str | None,
    new_scheduled_date: date | None = None,
    new_assignee: str | None = None,
) -> PlanTaskStatus:
    """校验一次老师处置动作并给出目标状态，不触碰存储层。"""

    require_reason(reason)
    if current_status not in OPEN_STATUSES:
        raise PlanRuleError(f"任务处于{current_status.value}状态，不能再执行处置")

    if action is TaskActionType.RESCHEDULE:
        if new_scheduled_date is None:
            raise PlanRuleError("改期必须提供新的回访日期")
        return PlanTaskStatus.RESCHEDULED
    if action is TaskActionType.TRANSFER:
        if not (new_assignee or "").strip():
            raise PlanRuleError("转交必须提供新的负责老师")
        return PlanTaskStatus.TRANSFERRED
    if action is TaskActionType.SKIP:
        return PlanTaskStatus.SKIPPED
    if action is TaskActionType.MARK_UNREACHABLE:
        return PlanTaskStatus.UNREACHABLE
    if action is TaskActionType.COMPLETE:
        return PlanTaskStatus.COMPLETED
    raise PlanRuleError(f"不支持的任务动作: {action}")
