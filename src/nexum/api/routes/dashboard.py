from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from nexum.api.deps import CurrentEmployee, DbSession, ManagerUser
from nexum.api.schemas import PayPeriodOut, ShiftOut, TimeOffOut
from nexum.services import dashboard

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


@router.get("/admin")
def admin(db: DbSession, _: ManagerUser) -> dict[str, Any]:
    data = dashboard.admin_overview(db)
    db.commit()
    return {
        "generated_at": data["generated_at"],
        "headcount": data["headcount"],
        "headcount_by_department": data["headcount_by_department"],
        "departments": data["departments"],
        "week_start": data["week_start"],
        "week_scheduled_hours": data["week_scheduled_hours"],
        "week_shift_count": data["week_shift_count"],
        "week_draft_shifts": data["week_draft_shifts"],
        "open_shifts_next_7_days": data["open_shifts_next_7_days"],
        "open_shifts": [ShiftOut.model_validate(s) for s in data["open_shifts"]],
        "pay_period": PayPeriodOut.model_validate(data["pay_period"]),
        "pay_period_totals": data["pay_period_totals"],
        "projected_gross": data["projected_gross"],
        "pending_time_off": data["pending_time_off"],
        "pending_time_off_items": [
            TimeOffOut.model_validate(r) for r in data["pending_time_off_items"]
        ],
        "pending_time_entries": data["pending_time_entries"],
        "shifts_missing_time_entries": data["shifts_missing_time_entries"],
        "automation": data["automation"],
    }


@router.get("/me")
def me(db: DbSession, employee: CurrentEmployee) -> dict[str, Any]:
    data = dashboard.employee_overview(db, employee)
    db.commit()
    return {
        "employee_id": employee.id,
        "upcoming_shifts": [ShiftOut.model_validate(s) for s in data["upcoming_shifts"]],
        "week_hours": data["week_hours"],
        "weekly_hours_target": data["weekly_hours_target"],
        "current_period": PayPeriodOut.model_validate(data["current_period"]),
        "current_estimate_gross": data["current_estimate"].gross_amount,
        "unread_notifications": data["unread_notifications"],
        "clocked_in": data["open_time_entry"] is not None,
        "calendar_url": f"/calendar/{employee.calendar_token}.ics",
        "claimable_shifts": data["claimable_count"],
    }
