"""Shared fixtures: a fresh in-memory database per test, a fake clock, an automation engine,
a demo company and an authenticated HTTP client."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal

os.environ.setdefault("NEXUM_ENVIRONMENT", "test")
os.environ.setdefault("NEXUM_DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("NEXUM_SECRET_KEY", "test-secret-key-not-for-production")
os.environ.setdefault("NEXUM_AUTOMATION_ENABLED", "false")

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from nexum.automation import AutomationEngine, set_engine
from nexum.automation.recipes import install_recipes
from nexum.config import reset_settings_cache
from nexum.db import configure_engine, init_db, session_factory
from nexum.models import Department, Employee, EmploymentType, PayType, Role, User
from nexum.services import people, scheduling
from nexum.services.events import clear_dispatchers, commit_and_dispatch

MONDAY = date(2026, 9, 7)  # a Monday
NOW = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)  # Tuesday 10:00 UTC
PASSWORD = "password123"


class FakeClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


# Point NEXUM_TEST_DATABASE_URL at a PostgreSQL database to run the suite against it.
TEST_DATABASE_URL = os.environ.get("NEXUM_TEST_DATABASE_URL", "sqlite:///:memory:")


@pytest.fixture(autouse=True)
def fresh_db() -> Iterator[None]:
    reset_settings_cache()
    configure_engine(TEST_DATABASE_URL)
    init_db(drop=not TEST_DATABASE_URL.startswith("sqlite"))
    clear_dispatchers()
    set_engine(None)
    yield
    clear_dispatchers()
    set_engine(None)


@pytest.fixture
def session() -> Iterator[Session]:
    db = session_factory()()
    try:
        yield db
    finally:
        db.close()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def engine(clock: FakeClock) -> AutomationEngine:
    eng = AutomationEngine(lambda: session_factory()(), clock=clock)
    eng.subscribe()
    set_engine(eng)
    return eng


@dataclass
class Company:
    admin: User
    manager: User
    department: Department
    eva: Employee  # hourly 210/h, 40 h/week
    max: Employee  # monthly 38 000, 40 h/week
    ida: Employee  # hourly 200/h, 20 h/week part-time


@pytest.fixture
def company(session: Session) -> Company:
    admin = people.create_user(
        session, email="admin@nexum.test", full_name="Admin", password=PASSWORD, role=Role.ADMIN
    )
    manager = people.create_user(
        session, email="mgr@nexum.test", full_name="Manager", password=PASSWORD, role=Role.MANAGER
    )
    dept = people.create_department(session, "Operations", actor=admin)
    eva = people.create_employee(
        session,
        first_name="Eva",
        last_name="Lund",
        email="eva@nexum.test",
        start_date=date(2026, 1, 1),
        department_id=dept.id,
        employment_type=EmploymentType.HOURLY,
        pay_type=PayType.HOURLY,
        hourly_rate=Decimal("210"),
        login_password=PASSWORD,
        actor=admin,
    )
    max_ = people.create_employee(
        session,
        first_name="Max",
        last_name="Berg",
        email="max@nexum.test",
        start_date=date(2026, 1, 1),
        department_id=dept.id,
        pay_type=PayType.MONTHLY,
        monthly_salary=Decimal("38000"),
        login_password=PASSWORD,
        actor=admin,
    )
    ida = people.create_employee(
        session,
        first_name="Ida",
        last_name="Aro",
        email="ida@nexum.test",
        start_date=date(2026, 1, 1),
        department_id=dept.id,
        employment_type=EmploymentType.PART_TIME,
        pay_type=PayType.HOURLY,
        hourly_rate=Decimal("200"),
        weekly_hours=Decimal("20"),
        actor=admin,
    )
    for weekday in range(5):
        scheduling.create_template(
            session,
            department=dept,
            name="Day",
            weekday=weekday,
            start_time=time(8),
            end_time=time(17),
            headcount=2,
        )
    commit_and_dispatch(session)
    return Company(admin=admin, manager=manager, department=dept, eva=eva, max=max_, ida=ida)


@pytest.fixture
def recipes(session: Session) -> None:
    install_recipes(session)
    session.commit()


@pytest.fixture
def client() -> Iterator[TestClient]:
    from nexum.main import create_app

    with TestClient(create_app(), follow_redirects=False) as c:
        yield c


def login(client: TestClient, email: str, password: str = PASSWORD) -> None:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
