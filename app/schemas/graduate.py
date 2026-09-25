from typing import List, Optional
from datetime import date, datetime
from .common import BaseSchema, TimestampSchema
from .college import College
from .micro_major import MicroMajor
from app.models import DestinationStatus, DestinationType, SalaryRange


class GraduateBase(BaseSchema):
    student_id: str
    name: str
    gender: Optional[str] = None
    major: str
    graduation_year: int
    college_id: int
    has_micro_major: bool = False
    micro_major_id: Optional[int] = None
    destination_status: DestinationStatus = DestinationStatus.PENDING
    destination_type: DestinationType = DestinationType.UNDECIDED
    unit_industry: Optional[str] = None
    salary_range: Optional[SalaryRange] = None
    is_aligned: bool = False
    employer_name: Optional[str] = None
    employment_start_date: Optional[date] = None


class GraduateCreate(GraduateBase):
    pass


class GraduateUpdate(BaseSchema):
    name: Optional[str] = None
    gender: Optional[str] = None
    major: Optional[str] = None
    graduation_year: Optional[int] = None
    college_id: Optional[int] = None
    has_micro_major: Optional[bool] = None
    micro_major_id: Optional[int] = None
    destination_type: Optional[DestinationType] = None
    unit_industry: Optional[str] = None
    salary_range: Optional[SalaryRange] = None
    is_aligned: Optional[bool] = None
    employer_name: Optional[str] = None
    employment_start_date: Optional[date] = None


class GraduateImportItem(GraduateBase):
    pass


class GraduateBatchImportRequest(BaseSchema):
    students: List[GraduateImportItem]


class GraduateImportResultItem(BaseSchema):
    student_id: str
    graduate_id: int
    action: str


class GraduateBatchImportResponse(BaseSchema):
    created_count: int
    updated_count: int
    unchanged_count: int
    results: List[GraduateImportResultItem]


class StatusUpdateRequest(BaseSchema):
    new_status: DestinationStatus
    changed_by: str
    remark: Optional[str] = None


class Graduate(GraduateBase, TimestampSchema):
    id: int
    college: Optional[College] = None
    micro_major: Optional[MicroMajor] = None


class GraduateList(Graduate):
    pass
