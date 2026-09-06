from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import PlainTextResponse

from nexum.api.deps import AdminUser, CurrentEmployee, DbSession, ManagerUser
from nexum.api.schemas import PayPeriodDetailOut, PayPeriodIn, PayPeriodOut, PayslipOut
from nexum.models import PayPeriod
from nexum.models.types import utcnow
from nexum.services import payroll, people
from nexum.services.calendar import today
from nexum.services.events import commit_and_dispatch

router = APIRouter(prefix="/payroll", tags=["payroll"])


def _detail(period: PayPeriod) -> PayPeriodDetailOut:
    out = PayPeriodDetailOut.model_validate(
        {
            **{k: getattr(period, k) for k in PayPeriodOut.model_fields},
            "payslips": [PayslipOut.model_validate(p) for p in period.payslips],
            "totals": payroll.period_totals(period),
        }
    )
    return out


@router.get("/periods", response_model=list[PayPeriodOut])
def list_periods(db: DbSession, _: ManagerUser) -> list[PayPeriodOut]:
    return [PayPeriodOut.model_validate(p) for p in payroll.list_periods(db)]


@router.post("/periods", response_model=PayPeriodOut, status_code=201)
def create_period(payload: PayPeriodIn, db: DbSession, _: AdminUser) -> PayPeriodOut:
    period = payroll.get_or_create_period(db, payload.start_date, payload.end_date)
    commit_and_dispatch(db)
    return PayPeriodOut.model_validate(period)


@router.get("/periods/current", response_model=PayPeriodDetailOut)
def current(db: DbSession, _: ManagerUser) -> PayPeriodDetailOut:
    period = payroll.current_period(db)
    db.commit()
    return _detail(period)


@router.get("/periods/{period_id}", response_model=PayPeriodDetailOut)
def get_period(period_id: int, db: DbSession, _: ManagerUser) -> PayPeriodDetailOut:
    return _detail(payroll.get_period(db, period_id))


@router.get("/periods/{period_id}/export.csv", response_class=PlainTextResponse)
def export_period(period_id: int, db: DbSession, _: ManagerUser) -> PlainTextResponse:
    period = payroll.get_period(db, period_id)
    filename = f"payroll-{period.start_date}-{period.end_date}.csv"
    return PlainTextResponse(
        payroll.export_csv(period),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/periods/{period_id}/compute", response_model=PayPeriodDetailOut)
def compute(period_id: int, db: DbSession, user: AdminUser) -> PayPeriodDetailOut:
    period = payroll.get_period(db, period_id)
    payroll.compute_payslips(db, period, actor=user)
    commit_and_dispatch(db)
    return _detail(period)


@router.post("/periods/{period_id}/close", response_model=PayPeriodDetailOut)
def close(period_id: int, db: DbSession, user: AdminUser) -> PayPeriodDetailOut:
    period = payroll.get_period(db, period_id)
    payroll.close_period(db, period, actor=user)
    commit_and_dispatch(db)
    return _detail(period)


@router.post("/periods/{period_id}/pay", response_model=PayPeriodOut)
def pay(period_id: int, db: DbSession, user: AdminUser) -> PayPeriodOut:
    period = payroll.get_period(db, period_id)
    payroll.mark_paid(db, period, actor=user)
    commit_and_dispatch(db)
    return PayPeriodOut.model_validate(period)


@router.get("/me", response_model=list[PayslipOut])
def my_payslips(db: DbSession, employee: CurrentEmployee) -> list[PayslipOut]:
    return [PayslipOut.model_validate(p) for p in payroll.payslips_for_employee(db, employee.id)]


@router.get("/me/estimate", response_model=PayslipOut)
def my_estimate(db: DbSession, employee: CurrentEmployee) -> PayslipOut:
    period = payroll.current_period(db)
    calc = payroll.calculate_payslip(db, employee, period)
    db.commit()
    return PayslipOut(
        id=0,
        pay_period_id=period.id,
        employee_id=employee.id,
        regular_hours=calc.regular_hours,
        overtime_hours=calc.overtime_hours,
        premium_hours=calc.premium_hours,
        base_amount=calc.base_amount,
        overtime_amount=calc.overtime_amount,
        premium_amount=calc.premium_amount,
        adjustments_amount=calc.adjustments_amount,
        gross_amount=calc.gross_amount,
        currency=employee.currency,
        details=calc.details,
        generated_at=utcnow(),
    )


@router.get("/me/vacation")
def my_vacation(db: DbSession, employee: CurrentEmployee) -> dict[str, Any]:
    rules = payroll.payroll_rules(db)
    return people.vacation_balance(db, employee, rules.vacation_days_per_year, today())
