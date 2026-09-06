"""ORM models. Importing this package registers every table on ``Base.metadata``."""

from nexum.models.audit import AuditLog
from nexum.models.automation import AutomationRule, AutomationRun, RunStatus, TriggerType
from nexum.models.company import CompanySettings, PayPeriodType
from nexum.models.identity import ROLE_RANK, Role, User
from nexum.models.notifications import Notification, NotificationLevel
from nexum.models.org import Department
from nexum.models.payroll import PayPeriod, PayPeriodStatus, Payslip
from nexum.models.people import (
    Employee,
    EmploymentType,
    PayType,
    TimeOffKind,
    TimeOffRequest,
    TimeOffStatus,
)
from nexum.models.scheduling import ScheduleTemplate, Shift, ShiftStatus
from nexum.models.time_tracking import TimeEntry, TimeEntryStatus

__all__ = [
    "ROLE_RANK",
    "AuditLog",
    "AutomationRule",
    "AutomationRun",
    "CompanySettings",
    "Department",
    "Employee",
    "EmploymentType",
    "Notification",
    "NotificationLevel",
    "PayPeriod",
    "PayPeriodStatus",
    "PayPeriodType",
    "PayType",
    "Payslip",
    "Role",
    "RunStatus",
    "ScheduleTemplate",
    "Shift",
    "ShiftStatus",
    "TimeEntry",
    "TimeEntryStatus",
    "TimeOffKind",
    "TimeOffRequest",
    "TimeOffStatus",
    "TriggerType",
    "User",
]
