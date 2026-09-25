from sqlalchemy import (
    Column,
    Integer,
    String,
    ForeignKey,
    Date,
    DateTime,
    Text,
    Enum,
    UniqueConstraint,
    Index,
    func,
)
from sqlalchemy.orm import relationship
from .base import Base, TimestampMixin
from .enums import FollowUpStage, PlanTaskStatus, PlanActionType


class FollowUpPlan(Base, TimestampMixin):
    """单个毕业生在某次入职周期中的一个阶段回访任务。"""

    __tablename__ = "follow_up_plans"
    __table_args__ = (
        UniqueConstraint(
            "graduate_id",
            "employment_seq",
            "stage",
            name="uq_follow_up_plan_employment_stage",
        ),
        Index("ix_follow_up_plan_status_due", "status", "due_date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    graduate_id = Column(Integer, ForeignKey("graduates.id"), nullable=False, comment="毕业生ID")
    employment_seq = Column(Integer, nullable=False, default=1, comment="入职序号，单位变更后递增")
    stage = Column(Enum(FollowUpStage), nullable=False, comment="回访阶段")
    status = Column(
        Enum(PlanTaskStatus),
        nullable=False,
        default=PlanTaskStatus.PENDING,
        comment="任务状态",
    )
    employer_name = Column(String(200), comment="任务对应的用人单位")
    onboard_date = Column(Date, nullable=False, comment="生成任务时依据的入职时间")
    due_date = Column(Date, nullable=False, comment="计划回访日期")
    scheduled_date = Column(Date, comment="当前安排的回访日期(改期后更新)")
    assignee = Column(String(50), comment="负责老师")
    completed_at = Column(Date, comment="实际完成回访日期")
    follow_up_id = Column(
        Integer,
        ForeignKey("employer_follow_ups.id"),
        nullable=True,
        comment="完成时关联的回访记录",
    )
    generation_run_id = Column(String(40), nullable=False, comment="生成批次ID")
    generated_as_of = Column(Date, nullable=False, comment="生成所依据的重放日期")

    graduate = relationship("Graduate", back_populates="follow_up_plans")
    follow_up = relationship("EmployerFollowUp")
    actions = relationship(
        "FollowUpPlanAction",
        back_populates="plan",
        order_by="FollowUpPlanAction.id",
    )


class FollowUpPlanAction(Base):
    """任务上每一次老师决定与系统动作的留痕，理由必存。"""

    __tablename__ = "follow_up_plan_actions"

    id = Column(Integer, primary_key=True, index=True)
    plan_id = Column(Integer, ForeignKey("follow_up_plans.id"), nullable=False, comment="计划任务ID")
    action_type = Column(Enum(PlanActionType), nullable=False, comment="动作类型")
    from_status = Column(Enum(PlanTaskStatus), comment="动作前状态")
    to_status = Column(Enum(PlanTaskStatus), comment="动作后状态")
    reason = Column(Text, nullable=False, comment="决定理由")
    acted_by = Column(String(50), nullable=False, comment="操作人")
    acted_at = Column(Date, nullable=False, comment="操作日期")
    extra = Column(Text, comment="动作附加信息(JSON)")

    plan = relationship("FollowUpPlan", back_populates="actions")


class FollowUpPlanRun(Base):
    """一次计划生成批次，支持按指定日期重放与幂等控制。"""

    __tablename__ = "follow_up_plan_runs"

    id = Column(Integer, primary_key=True, index=True)
    run_id = Column(String(40), unique=True, nullable=False, comment="批次ID")
    idempotency_key = Column(String(64), unique=True, nullable=False, comment="幂等键(范围+重放日期)")
    as_of = Column(Date, nullable=False, comment="本次生成的重放基准日期")
    policy_version = Column(String(20), nullable=False, comment="计划规则版本")
    requested_count = Column(Integer, nullable=False, default=0, comment="请求覆盖的毕业生数")
    created_count = Column(Integer, nullable=False, default=0, comment="新建任务数")
    skipped_existing_count = Column(Integer, nullable=False, default=0, comment="因已存在跳过的任务数")
    superseded_count = Column(Integer, nullable=False, default=0, comment="单位变更作废任务数")
    missing_onboard_count = Column(Integer, nullable=False, default=0, comment="缺少入职日期的毕业生数")
    created_by = Column(String(50), nullable=False, comment="发起人")
    created_at = Column(DateTime, nullable=False, server_default=func.now())


class GraduateImportBatch(Base, TimestampMixin):
    """毕业生批量导入批次，保证同一批重复导入不重复建人也不重复造任务。"""

    __tablename__ = "graduate_import_batches"

    id = Column(Integer, primary_key=True, index=True)
    batch_key = Column(String(64), unique=True, nullable=False, comment="导入批次标识")
    total = Column(Integer, nullable=False, default=0, comment="名单人数")
    inserted = Column(Integer, nullable=False, default=0, comment="新建毕业生数")
    updated = Column(Integer, nullable=False, default=0, comment="更新毕业生数")
    missing_onboard = Column(Integer, nullable=False, default=0, comment="缺入职日期人数")
    plan_run_id = Column(String(40), comment="联动生成的计划批次ID")
    imported_by = Column(String(50), nullable=False, comment="导入人")
