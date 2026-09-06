"""Premium windows, holidays, rounding/auto-break, vacation balance and payroll exports."""

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from nexum.models import ShiftStatus, TimeOffKind
from nexum.services import payroll, people, scheduling, time_tracking
from nexum.services.company import Holiday, PayrollRules, PremiumWindow
from tests.conftest import Company, login, post

SEPT = (date(2026, 9, 1), date(2026, 9, 30))


def shift(session: Session, company: Company, employee, start: datetime, hours: float, **kw):
    return scheduling.create_shift(
        session,
        department=company.department,
        starts_at=start,
        ends_at=start + timedelta(hours=hours),
        employee=employee,
        status=ShiftStatus.PUBLISHED,
        **kw,
    )


def evening_and_weekend() -> PayrollRules:
    return PayrollRules(
        premium_windows=[
            PremiumWindow(
                "Evening", frozenset({0, 1, 2, 3, 4}), time(18), time(22), Decimal("1.2")
            ),
            PremiumWindow("Weekend", frozenset({5, 6}), time(0), time(0), Decimal("1.5")),
        ]
    )


def test_evening_and_weekend_premiums(session: Session, company: Company) -> None:
    shift(
        session, company, company.eva, datetime(2026, 9, 7, 14, tzinfo=UTC), 8
    )  # Mon 14-22: 4h evening
    shift(
        session, company, company.eva, datetime(2026, 9, 12, 8, tzinfo=UTC), 4
    )  # Sat 08-12: weekend
    period = payroll.get_or_create_period(session, *SEPT)
    calc = payroll.calculate_payslip(session, company.eva, period, evening_and_weekend())
    assert calc.regular_hours == Decimal("12.00") and calc.overtime_hours == 0
    assert calc.premium_hours == Decimal("8.00")
    assert calc.details["premiums"] == {"Evening": "4.00", "Weekend": "4.00"}
    assert calc.base_amount == Decimal("2520.00")  # 12 * 210
    assert calc.premium_amount == Decimal("588.00")  # 4*210*0.2 + 4*210*0.5
    assert calc.gross_amount == Decimal("3108.00")


def test_overnight_window_and_holiday_precedence(session: Session, company: Company) -> None:
    rules = PayrollRules(
        premium_windows=[
            PremiumWindow("Night", frozenset(range(7)), time(23), time(6), Decimal("1.5"))
        ],
        holidays={date(2026, 9, 8): Holiday(date(2026, 9, 8), "Founders day", Decimal("2"))},
    )
    shift(
        session, company, company.eva, datetime(2026, 9, 7, 21, tzinfo=UTC), 6
    )  # Mon 21 -> Tue 03
    period = payroll.get_or_create_period(session, *SEPT)
    calc = payroll.calculate_payslip(session, company.eva, period, rules)
    assert calc.premium_hours == Decimal("4.00")
    assert calc.details["premiums"] == {"Night": "1.00", "Founders day": "3.00"}
    assert calc.premium_amount == Decimal("735.00")  # 1h*0.5 + 3h*1.0, times 210
    segments = payroll.premium_segments(
        datetime(2026, 9, 7, 21, tzinfo=UTC), datetime(2026, 9, 8, 3, tzinfo=UTC), rules
    )
    assert [(s[2], s[3]) for s in segments] == [
        (Decimal("1.5"), "Night"),
        (Decimal("2"), "Founders day"),
    ]


def test_monthly_employee_premiums_use_hourly_equivalent(
    session: Session, company: Company
) -> None:
    shift(session, company, company.max, datetime(2026, 9, 12, 8, tzinfo=UTC), 4)  # Saturday
    period = payroll.get_or_create_period(session, *SEPT)
    calc = payroll.calculate_payslip(session, company.max, period, evening_and_weekend())
    hourly_equivalent = Decimal("38000") / (Decimal("40") * Decimal("52") / Decimal("12"))
    assert calc.premium_amount == payroll.money(Decimal("4") * Decimal("0.5") * hourly_equivalent)
    assert calc.gross_amount == payroll.money(Decimal("38000") + calc.premium_amount)


def test_rounding_and_auto_break(session: Session, company: Company) -> None:
    rules = PayrollRules(
        rounding_minutes=15, auto_break_minutes=30, auto_break_after_hours=Decimal("6")
    )
    start = datetime(2026, 9, 7, 8, tzinfo=UTC)
    no_break = time_tracking.record_entry(
        session, company.eva, start, start + timedelta(hours=8, minutes=52)
    )
    explicit = time_tracking.record_entry(
        session,
        company.eva,
        start + timedelta(days=1),
        start + timedelta(days=1, hours=8, minutes=52),
        break_minutes=45,
    )
    short = time_tracking.record_entry(
        session, company.eva, start + timedelta(days=2), start + timedelta(days=2, hours=5)
    )
    assert time_tracking.entry_paid_hours(no_break, rules) == Decimal(
        "8.25"
    )  # 532-30=502 -> 495 min
    assert time_tracking.entry_paid_hours(explicit, rules) == Decimal(
        "8.00"
    )  # 532-45=487 -> 480 min
    assert time_tracking.entry_paid_hours(short, rules) == Decimal(
        "5.00"
    )  # under 6h: no auto break
    scheduled = shift(session, company, company.eva, start + timedelta(days=3), 9)
    assert time_tracking.shift_paid_hours(scheduled, rules) == Decimal(
        "8.50"
    )  # auto break, no rounding
    assert time_tracking.shift_paid_hours(scheduled, PayrollRules()) == Decimal("9.00")
    for entry in (no_break, explicit, short):
        time_tracking.decide_entry(session, entry, approve=True)
    period = payroll.get_or_create_period(session, *SEPT)
    calc = payroll.calculate_payslip(session, company.eva, period, rules)
    assert calc.regular_hours == Decimal("29.75")  # 8.25 + 8 + 5 + 8.5


def test_premium_scaled_by_paid_share(session: Session, company: Company) -> None:
    start = datetime(2026, 9, 7, 18, tzinfo=UTC)
    entry = time_tracking.record_entry(
        session, company.eva, start, start + timedelta(hours=4), break_minutes=60
    )
    time_tracking.decide_entry(session, entry, approve=True)
    period = payroll.get_or_create_period(session, *SEPT)
    calc = payroll.calculate_payslip(session, company.eva, period, evening_and_weekend())
    assert calc.regular_hours == Decimal("3.00")
    assert calc.premium_hours == Decimal("3.00")  # 4h window * 3/4 paid share
    assert calc.premium_amount == Decimal("126.00")  # 3 * 210 * 0.2


def test_vacation_balance(session: Session, company: Company) -> None:
    req = people.request_time_off(
        session,
        company.eva,
        kind=TimeOffKind.VACATION,
        start_date=date(2026, 10, 5),
        end_date=date(2026, 10, 11),
    )  # 5 weekdays
    people.decide_time_off(session, req, approve=True)
    balance = people.vacation_balance(session, company.eva, Decimal("25"), date(2026, 9, 30))
    assert balance == {
        "year": 2026,
        "entitlement": Decimal("25.0"),
        "accrued": Decimal("18.7"),
        "taken": Decimal("5"),
        "remaining": Decimal("20.0"),
    }
    company.ida.start_date = date(2026, 7, 1)
    balance = people.vacation_balance(session, company.ida, Decimal("25"), date(2026, 9, 30))
    assert balance["entitlement"] == Decimal("12.6") and balance["accrued"] == Decimal("6.3")
    company.max.end_date = date(2025, 12, 31)
    assert (
        people.vacation_balance(session, company.max, Decimal("25"), date(2026, 9, 30))[
            "entitlement"
        ]
        == 0
    )


def test_export_csv_and_pages(client, session: Session, company: Company) -> None:
    shift(session, company, company.eva, datetime(2026, 9, 12, 8, tzinfo=UTC), 4)
    period = payroll.get_or_create_period(session, *SEPT)
    payroll.compute_payslips(session, period)
    session.commit()
    csv_text = payroll.export_csv(period)
    lines = csv_text.strip().splitlines()
    assert lines[0].startswith("period_start;period_end;employee_id;employee;") and len(lines) == 4
    assert any(line.split(";")[3] == "Eva Lund" and line.endswith(";SEK") for line in lines[1:])
    login(client, "mgr@nexum.test")
    r = client.get(f"/api/v1/payroll/periods/{period.id}/export.csv")
    assert (
        r.status_code == 200
        and r.headers["content-type"].startswith("text/csv")
        and "attachment" in r.headers["content-disposition"]
    )
    detail = client.get(f"/api/v1/payroll/periods/{period.id}").json()
    assert detail["totals"]["premium"] == "0.00" and "premium_hours" in detail["payslips"][0]
    login(client, "eva@nexum.test")
    vacation = client.get("/api/v1/payroll/me/vacation").json()
    assert vacation["entitlement"] == "25.0"
    # web pages
    from tests.test_web import web_login

    web_login(client, "mgr@nexum.test")
    html = client.get(f"/payroll/{period.id}").text
    assert "Export CSV" in html and "Premium h" in html
    import re

    slip_id = re.search(rf"/payroll/{period.id}/payslips/(\d+)", html).group(1)
    page = client.get(f"/payroll/{period.id}/payslips/{slip_id}").text
    assert "Payslip" in page and "Gross pay" in page and "Operations" in page
    assert client.get(f"/payroll/{period.id}/export.csv").status_code == 200
    assert client.get(f"/payroll/999/payslips/{slip_id}").status_code == 404
    web_login(client, "eva@nexum.test")
    eva_slip = next(p for p in period.payslips if p.employee_id == company.eva.id)
    other_slip = next(p for p in period.payslips if p.employee_id != company.eva.id)
    assert client.get(f"/me/pay/{eva_slip.id}").status_code == 200
    assert client.get(f"/me/pay/{other_slip.id}").status_code == 404
    html = client.get("/me/pay").text
    assert "Vacation 2026" in html and "Premiums" in html
    assert "Vacation 2026" in client.get("/me").text
    assert (
        post(
            client,
            "/me/requests",
            {"kind": "vacation", "start_date": "2026-11-02", "end_date": "2026-11-03"},
        ).status_code
        == 303
    )
