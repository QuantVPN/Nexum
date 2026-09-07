"""Password change/reset, session invalidation, login rate limiting, security headers,
GDPR export/anonymisation and the first-run setup wizard."""

import re
from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from nexum.errors import AuthenticationError, ValidationError
from nexum.models import Notification, PasswordResetToken, User
from nexum.models.types import utcnow
from nexum.services import auth, mailer, notifications, people
from nexum.services import company as company_service
from nexum.services.ratelimit import login_limiter
from tests.conftest import PASSWORD, Company, login, post


def web_login(client: TestClient, email: str, password: str = PASSWORD) -> int:
    return post(client, "/login", {"email": email, "password": password}).status_code


def test_change_password_signs_out_other_sessions(client: TestClient, company: Company) -> None:
    from nexum.main import create_app

    with TestClient(create_app(), follow_redirects=False) as other:
        login(other, "eva@nexum.test")
        assert other.get("/api/v1/auth/me").status_code == 200
        login(client, "eva@nexum.test")
        r = client.post(
            "/api/v1/auth/password",
            json={"current_password": "nope", "new_password": "longer-secret-1"},
        )
        assert r.status_code == 401
        r = client.post(
            "/api/v1/auth/password", json={"current_password": PASSWORD, "new_password": "short"}
        )
        assert r.status_code == 422
        r = client.post(
            "/api/v1/auth/password", json={"current_password": PASSWORD, "new_password": PASSWORD}
        )
        assert r.status_code == 422
        r = client.post(
            "/api/v1/auth/password",
            json={"current_password": PASSWORD, "new_password": "longer-secret-1"},
        )
        assert r.status_code == 200
        assert client.get("/api/v1/auth/me").status_code == 200  # this session survives
        assert other.get("/api/v1/auth/me").status_code == 401  # the other one is out
        assert (
            client.post(
                "/api/v1/auth/login", json={"email": "eva@nexum.test", "password": PASSWORD}
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/v1/auth/login",
                json={"email": "eva@nexum.test", "password": "longer-secret-1"},
            ).status_code
            == 200
        )


def test_forgot_and_reset_flow(
    client: TestClient, session: Session, company: Company, outbox: list[mailer.Message]
) -> None:
    r = client.post("/api/v1/auth/forgot", json={"email": "nobody@nexum.test"})
    assert r.status_code == 200 and not outbox  # no enumeration, no mail
    assert client.post("/api/v1/auth/forgot", json={"email": "eva@nexum.test"}).status_code == 200
    assert len(outbox) == 1 and "Reset your password" in outbox[0].subject
    token = re.search(r"/reset/(\S+)", outbox[0].body).group(1)
    assert client.get(f"/reset/{token}").status_code == 200
    assert client.get("/reset/bogus").status_code == 410
    assert (
        client.post(
            "/api/v1/auth/reset", json={"token": "bogus-token", "new_password": "another-secret-2"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/reset", json={"token": token, "new_password": "short"}
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/auth/reset", json={"token": token, "new_password": "another-secret-2"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/api/v1/auth/reset", json={"token": token, "new_password": "another-secret-3"}
        ).status_code
        == 401
    )  # single use
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "eva@nexum.test", "password": "another-secret-2"}
        ).status_code
        == 200
    )
    # expired tokens are rejected
    raw = auth.create_reset_token(session, company.eva.user)
    stored = session.scalar(select(PasswordResetToken).order_by(PasswordResetToken.id.desc()))
    stored.expires_at = utcnow() - timedelta(minutes=1)
    session.commit()
    assert auth.find_valid_token(session, raw) is None
    # web flow
    r = post(client, "/forgot", {"email": "eva@nexum.test"})
    assert r.status_code == 303 and len(outbox) == 2
    token = re.search(r"/reset/(\S+)", outbox[-1].body).group(1)
    assert (
        post(
            client, f"/reset/{token}", {"password": "web-secret-4", "password_confirm": "different"}
        ).status_code
        == 303
    )
    assert (
        post(
            client,
            f"/reset/{token}",
            {"password": "web-secret-4", "password_confirm": "web-secret-4"},
        ).status_code
        == 303
    )
    assert "Password updated" in client.get("/login").text
    assert web_login(client, "eva@nexum.test", "web-secret-4") == 303


def test_forgot_without_email_configured_is_quiet(client: TestClient, company: Company) -> None:
    assert client.post("/api/v1/auth/forgot", json={"email": "eva@nexum.test"}).status_code == 200
    assert "administrator" in client.get("/forgot").text


def test_admin_reset_link_and_web_password_change(
    client: TestClient, session: Session, company: Company
) -> None:
    login(client, "mgr@nexum.test")
    assert client.post(f"/api/v1/employees/{company.eva.id}/reset-link").status_code == 403
    login(client, "admin@nexum.test")
    assert (
        client.post(f"/api/v1/employees/{company.ida.id}/reset-link").status_code == 403
    )  # no login
    r = client.post(f"/api/v1/employees/{company.eva.id}/reset-link")
    assert r.status_code == 200 and r.json()["url"].startswith("/reset/")
    token = r.json()["url"].rsplit("/", 1)[1]
    assert auth.find_valid_token(session, token) is not None
    web_login(client, "admin@nexum.test")
    assert post(client, f"/employees/{company.eva.id}/reset-link").status_code == 303
    assert "Reset link (valid 2 hours" in client.get(f"/employees/{company.eva.id}").text
    assert client.get("/me/password").status_code == 200
    assert (
        post(
            client,
            "/me/password",
            {
                "current_password": PASSWORD,
                "new_password": "brand-new-9",
                "new_password_confirm": "nope",
            },
        ).status_code
        == 303
    )
    assert (
        post(
            client,
            "/me/password",
            {
                "current_password": "wrong",
                "new_password": "brand-new-9",
                "new_password_confirm": "brand-new-9",
            },
        ).status_code
        == 303
    )
    assert "incorrect" in client.get("/me/password").text
    assert (
        post(
            client,
            "/me/password",
            {
                "current_password": PASSWORD,
                "new_password": "brand-new-9",
                "new_password_confirm": "brand-new-9",
            },
        ).status_code
        == 303
    )
    assert client.get("/dashboard").status_code == 200  # still signed in here


def test_login_rate_limiting(client: TestClient, company: Company) -> None:
    login_limiter.clear()
    for _ in range(login_limiter.max_attempts):
        assert (
            client.post(
                "/api/v1/auth/login", json={"email": "eva@nexum.test", "password": "bad"}
            ).status_code
            == 401
        )
    r = client.post("/api/v1/auth/login", json={"email": "eva@nexum.test", "password": PASSWORD})
    assert r.status_code == 429 and "Try again" in r.json()["detail"]
    assert (
        post(client, "/login", {"email": "eva@nexum.test", "password": PASSWORD}).status_code == 303
    )
    assert "Too many" in client.get("/login").text
    # other accounts are unaffected; a successful login resets the counter
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "max@nexum.test", "password": PASSWORD}
        ).status_code
        == 200
    )
    login_limiter.clear()
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "eva@nexum.test", "password": PASSWORD}
        ).status_code
        == 200
    )
    assert not login_limiter.is_blocked(auth.limiter_key("testclient", "eva@nexum.test"))


def test_security_headers(client: TestClient, company: Company) -> None:
    r = client.get("/login")
    assert (
        r.headers["x-frame-options"] == "DENY" and r.headers["x-content-type-options"] == "nosniff"
    )
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert (
        "content-security-policy" not in client.get("/api/docs").headers
    )  # Swagger UI needs its CDN
    assert client.get("/healthz").headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_export_and_anonymize(
    client: TestClient, session: Session, company: Company, recipes: None
) -> None:
    notifications.notify_user(session, company.eva.user, "Private note", "body")
    people.set_skills(session, company.eva, ["forklift"])
    people.add_availability(
        session,
        company.eva,
        weekday=0,
        start_time=utcnow().time(),
        end_time=utcnow().time(),
        note="school run",
    )
    session.commit()
    login(client, "mgr@nexum.test")
    assert client.get(f"/api/v1/employees/{company.eva.id}/export").status_code == 403
    login(client, "admin@nexum.test")
    export = client.get(f"/api/v1/employees/{company.eva.id}/export").json()
    assert export["employee"]["email"] == "eva@nexum.test" and export["employee"]["skills"] == [
        "forklift"
    ]
    assert (
        export["login"]["role"] == "employee" and export["availability"][0]["note"] == "school run"
    )
    assert any(n["title"] == "Private note" for n in export["notifications"])
    web = client.get(f"/employees/{company.eva.id}/export.json")
    assert web.status_code == 200 and "attachment" in web.headers["content-disposition"]
    r = client.post(f"/api/v1/employees/{company.eva.id}/anonymize")
    assert (
        r.status_code == 200
        and r.json()["full_name"].startswith("Former Employee")
        and r.json()["is_active"] is False
    )
    session.expire_all()
    eva = session.get(type(company.eva), company.eva.id)
    assert eva.email.endswith("@example.invalid") and eva.skills == [] and eva.availability == []
    assert (
        eva.user is not None
        and eva.user.is_active is False
        and eva.user.email.endswith("@example.invalid")
    )
    assert (
        session.scalar(
            select(func.count(Notification.id)).where(Notification.user_id == eva.user.id)
        )
        == 0
    )
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "eva@nexum.test", "password": PASSWORD}
        ).status_code
        == 401
    )
    # payroll history survives in anonymised form
    assert eva.full_name == f"Former Employee {eva.id}"


def test_setup_wizard(client: TestClient, session: Session) -> None:
    assert client.get("/").headers["location"] == "/setup"
    assert client.get("/login").headers["location"] == "/setup"
    html = client.get("/setup").text
    assert "Create company" in html
    r = post(
        client,
        "/setup",
        {
            "name": "Acme AB",
            "timezone": "Europe/Stockholm",
            "currency": "sek",
            "admin_name": "Alex",
            "admin_email": "alex@acme.test",
            "password": "secret-pass-1",
            "password_confirm": "other",
        },
    )
    assert r.status_code == 422 and "do not match" in r.text
    r = post(
        client,
        "/setup",
        {
            "name": "Acme AB",
            "timezone": "Europe/Stockholm",
            "currency": "sek",
            "admin_name": "Alex",
            "admin_email": "alex@acme.test",
            "password": "secret-pass-1",
            "password_confirm": "secret-pass-1",
        },
    )
    assert r.status_code == 303 and r.headers["location"] == "/employees"
    assert "Welcome to Acme AB" in client.get("/employees").text
    row = company_service.get_company(session)
    assert row.name == "Acme AB" and row.timezone == "Europe/Stockholm" and row.currency == "SEK"
    admin = session.scalar(select(User))
    assert admin.email == "alex@acme.test" and admin.role.value == "admin"
    from nexum.automation.recipes import RECIPES
    from nexum.models import AutomationRule

    assert session.scalar(select(func.count(AutomationRule.id))) == len(RECIPES)
    assert client.get("/setup").headers["location"] == "/login"
    assert (
        post(
            client,
            "/setup",
            {
                "name": "x",
                "timezone": "UTC",
                "currency": "SEK",
                "admin_name": "y",
                "admin_email": "z@z.z",
                "password": "secret-pass-1",
                "password_confirm": "secret-pass-1",
            },
        ).status_code
        == 403
    )


def test_password_policy_and_service_errors(session: Session, company: Company) -> None:
    import pytest

    with pytest.raises(ValidationError):
        auth.change_password(session, company.eva.user, PASSWORD, "password")
    with pytest.raises(AuthenticationError):
        auth.reset_password(session, "nope", "another-secret-2")
    with pytest.raises(ValidationError):
        company_service.setup_company(
            session,
            name="x",
            timezone="UTC",
            currency="SEK",
            admin_email="a@b.c",
            admin_name="a",
            admin_password="secret-pass-1",
        )
