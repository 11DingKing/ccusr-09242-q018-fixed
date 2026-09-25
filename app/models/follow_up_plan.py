from sqlalchemy import Column, Integer, String, ForeignKey, Date, DateTime, Text, Enum, Index, func
from sqlalchemy.orm import relationship
from .base import Base, TimestampMixin
from .enums import FollowUpDecisionAction, FollowUpStage, FollowUpTaskStatus


class FollowUpPlanTask(Base, TimestampMixin):
    __tablename__ = "follow_up_plan_tasks"
    __table_args__ = (
        Index("ix_follow_up_plan_tasks_graduate_stage_anchor", "graduate_id", "stage", "anchor_date"),
    )

    id = Column(Integer, primary_key=True, index=True)
    graduate_id = Column(Integer, ForeignKey("graduates.id"), nullable=False, comment="毕业生ID")
    stage = Column(Enum(FollowUpStage), nullable=False, comment="回访阶段")
    anchor_date = Column(Date, nullable=False, comment="生成时采用的入职日期锚点")
    due_date = Column(Date, nullable=False, comment="计划回访日期")
    status = Column(Enum(FollowUpTaskStatus), default=FollowUpTaskStatus.PENDING, nullable=False, comment="任务状态")
    assignee = Column(String(50), comment="负责老师")
    follow_up_id = Column(Integer, ForeignKey("employer_follow_ups.id"), comment="完成时关联的回访记录ID")
    completed_at = Column(DateTime, comment="完成时间")

    graduate = relationship("Graduate", back_populates="plan_tasks")
    follow_up = relationship("EmployerFollowUp")
    decisions = relationship(
        "FollowUpPlanDecision",
        back_populates="task",
        order_by="FollowUpPlanDecision.created_at",
        cascade="all, delete-orphan",
    )


class FollowUpPlanDecision(Base):
    __tablename__ = "follow_up_plan_decisions"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("follow_up_plan_tasks.id"), nullable=False, comment="计划任务ID")
    action = Column(Enum(FollowUpDecisionAction), nullable=False, comment="决定类型")
    reason = Column(Text, nullable=False, comment="决定理由")
    operator = Column(String(50), nullable=False, default="system", comment="操作人")
    new_due_date = Column(Date, comment="改期后的计划回访日期")
    new_assignee = Column(String(50), comment="转交后的负责老师")
    created_at = Column(DateTime, default=func.now(), nullable=False, comment="决定时间")

    task = relationship("FollowUpPlanTask", back_populates="decisions")
