"""回访计划生成的纯业务规则。

依据毕业时间、入职时间和最近一次有效回访，推导入职后 3/6/12 个月的阶段任务。
整个模块不接触数据库：相同的输入与计划日期（as_of）必然产生相同的输出，
因此生成过程可以在任意指定日期重放。
"""

from calendar import monthrange
from dataclasses import dataclass
from datetime import date
from typing import Mapping, Sequence

from app.models import DestinationType, FollowUpStage, FollowUpTaskStatus


STAGE_MONTHS: Mapping[FollowUpStage, int] = {
    FollowUpStage.MONTH_3: 3,
    FollowUpStage.MONTH_6: 6,
    FollowUpStage.MONTH_12: 12,
}

STAGE_ORDER: tuple[FollowUpStage, ...] = (
    FollowUpStage.MONTH_3,
    FollowUpStage.MONTH_6,
    FollowUpStage.MONTH_12,
)

# 已取消的任务属于历史记录，不阻止同一锚点重新生成；其余状态都视为老师或流程的决定，必须保留。
_BLOCKING_STATUSES = frozenset({
    FollowUpTaskStatus.PENDING,
    FollowUpTaskStatus.COMPLETED,
    FollowUpTaskStatus.SKIPPED,
    FollowUpTaskStatus.UNREACHABLE,
})


def add_months(value: date, months: int) -> date:
    """按自然月偏移日期，目标月没有对应日时收敛到月末。"""
    total = (value.year * 12 + (value.month - 1)) + months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    day = min(value.day, monthrange(year, month)[1])
    return date(year, month, day)


def stage_due_date(anchor: date, stage: FollowUpStage) -> date:
    return add_months(anchor, STAGE_MONTHS[stage])


@dataclass(frozen=True)
class ExistingTaskView:
    """生成规则所需的既有任务投影。"""

    task_id: int
    stage: FollowUpStage
    anchor_date: date
    due_date: date
    status: FollowUpTaskStatus


@dataclass(frozen=True)
class CreateStageTask:
    stage: FollowUpStage
    anchor_date: date
    due_date: date


@dataclass(frozen=True)
class CancelStageTask:
    task_id: int
    stage: FollowUpStage
    anchor_date: date
    due_date: date


@dataclass(frozen=True)
class GraduatePlan:
    """单个毕业生的计划推导结果。"""

    eligible: bool
    skip_reason: str | None
    creates: tuple[CreateStageTask, ...] = ()
    cancels: tuple[CancelStageTask, ...] = ()
    covered_stages: tuple[FollowUpStage, ...] = ()


def _ineligible(reason: str) -> GraduatePlan:
    return GraduatePlan(eligible=False, skip_reason=reason)


def compute_graduate_plan(
    *,
    graduation_year: int,
    destination_type: DestinationType,
    employment_start_date: date | None,
    follow_up_dates: Sequence[date],
    existing_tasks: Sequence[ExistingTaskView],
    as_of: date,
) -> GraduatePlan:
    """推导单个毕业生在指定计划日期应有的任务变更。

    规则：
    1. 仅就业去向的毕业生需要回访计划；
    2. 毕业届次晚于计划日期的暂不生成；
    3. 缺失入职日期无法锚定阶段，跳过并说明原因；
    4. 入职日期之前的回访不算有效回访；最近一次有效回访日期不早于某阶段
       计划日期时，该阶段视为已被零散回访记录覆盖，不再生成任务；
    5. 只生成计划日期不晚于 as_of 的阶段任务，未到期的阶段留待以后生成；
    6. 同一（阶段, 入职锚点）已存在未取消的任务时不重复生成；
    7. 入职日期变化（如更换单位）后，仍挂在新锚点之外的待回访任务需要取消。
    """
    if destination_type != DestinationType.EMPLOYMENT:
        return _ineligible("非就业去向，无需生成回访计划")
    if graduation_year > as_of.year:
        return _ineligible("毕业届次晚于计划日期，尚未毕业")
    if employment_start_date is None:
        return _ineligible("缺失入职日期，无法锚定回访阶段")

    anchor = employment_start_date

    cancels = tuple(
        CancelStageTask(
            task_id=task.task_id,
            stage=task.stage,
            anchor_date=task.anchor_date,
            due_date=task.due_date,
        )
        for task in existing_tasks
        if task.status == FollowUpTaskStatus.PENDING and task.anchor_date != anchor
    )

    valid_dates = sorted(d for d in follow_up_dates if d >= anchor)
    latest_valid = valid_dates[-1] if valid_dates else None

    blocking_keys = {
        (task.stage, task.anchor_date)
        for task in existing_tasks
        if task.status in _BLOCKING_STATUSES
    }

    creates: list[CreateStageTask] = []
    covered: list[FollowUpStage] = []
    for stage in STAGE_ORDER:
        due = stage_due_date(anchor, stage)
        if due > as_of:
            continue
        if (stage, anchor) in blocking_keys:
            continue
        if latest_valid is not None and due <= latest_valid:
            covered.append(stage)
            continue
        creates.append(CreateStageTask(stage=stage, anchor_date=anchor, due_date=due))

    return GraduatePlan(
        eligible=True,
        skip_reason=None,
        creates=tuple(creates),
        cancels=cancels,
        covered_stages=tuple(covered),
    )
