import json

from fastapi.testclient import TestClient

from nexum.services import company as company_service
from nexum.services import mailer
from tests.conftest import Company, login, post


def web_login(client: TestClient, email: str) -> None:
    assert post(client, "/login", {"email": email, "password": "password123"}).status_code == 303


def test_csrf_is_enforced(client: TestClient, company: Company) -> None:
    r = client.post("/login", data={"email": "admin@nexum.test", "password": "password123"})
    assert r.status_code == 403 and "expired" in r.text
    web_login(client, "admin@nexum.test")
    r = client.post("/departments", data={"name": "Nope", "csrf_token": "wrong"})
    assert r.status_code == 403
    assert client.get("/employees").text.count("Nope") == 0
    assert post(client, "/departments", {"name": "Yes"}).status_code == 303


def test_settings_page_roundtrip(client: TestClient, company: Company, session) -> None:
    web_login(client, "mgr@nexum.test")
    assert client.get("/settings").status_code == 403
    web_login(client, "admin@nexum.test")
    html = client.get("/settings").text
    assert "Company settings" in html and 'value="UTC"' in html
    r = post(
        client,
        "/settings",
        {
            "name": "Acme AB",
            "timezone": "Europe/Stockholm",
            "currency": "sek",
            "default_weekly_hours": "38",
            "weekly_overtime_threshold_hours": "38",
            "overtime_multiplier": "2",
            "pay_period_type": "monthly",
            "biweekly_anchor": "",
            "rounding_minutes": "15",
            "auto_break_minutes": "30",
            "auto_break_after_hours": "6",
            "vacation_days_per_year": "30",
            "premium_rules": json.dumps(
                [{"label": "Evening", "start": "18:00", "end": "22:00", "multiplier": 1.2}]
            ),
            "holidays": "[]",
            "smtp_host": "smtp.example.com",
            "smtp_port": "2525",
            "smtp_username": "user",
            "smtp_password": "secret",
            "smtp_from": "nexum@example.com",
            "smtp_use_tls": "1",
            "email_notifications_enabled": "1",
        },
    )
    assert r.status_code == 303
    html = client.get("/settings").text
    assert "Settings saved" in html and "Acme AB" in html and "Europe/Stockholm" in html
    assert "times shown in Europe/Stockholm" in html
    row = company_service.get_company(session)
    assert row.currency == "SEK" and row.smtp_password == "secret" and row.rounding_minutes == 15
    assert row.premium_rules[0]["weekdays"] == [0, 1, 2, 3, 4, 5, 6]
    # blank password keeps the old one; bad JSON is reported
    assert (
        post(
            client,
            "/settings",
            {
                "name": "Acme AB",
                "timezone": "UTC",
                "currency": "SEK",
                "premium_rules": "{bad",
                "holidays": "[]",
            },
        ).status_code
        == 303
    )
    assert "invalid JSON" in client.get("/settings").text
    session.expire_all()
    assert company_service.get_company(session).smtp_password == "secret"


def test_test_email_button(
    client: TestClient, company: Company, outbox: list[mailer.Message]
) -> None:
    web_login(client, "admin@nexum.test")
    assert post(client, "/settings/test-email", {"to": "boss@example.com"}).status_code == 303
    assert "Test e-mail sent" in client.get("/settings").text
    assert outbox and outbox[0].to == "boss@example.com" and "test" in outbox[0].subject.lower()


def test_company_api(client: TestClient, company: Company) -> None:
    login(client, "mgr@nexum.test")
    assert client.get("/api/v1/company").json()["timezone"] == "UTC"
    assert client.patch("/api/v1/company", json={"name": "X"}).status_code == 403
    assert "Europe/Stockholm" in client.get("/api/v1/company/timezones").json()
    login(client, "admin@nexum.test")
    r = client.patch(
        "/api/v1/company",
        json={
            "timezone": "Europe/Oslo",
            "holidays": [{"date": "2026-05-17", "label": "Constitution Day", "multiplier": 2}],
        },
    )
    assert (
        r.status_code == 200
        and r.json()["timezone"] == "Europe/Oslo"
        and r.json()["holidays"][0]["label"] == "Constitution Day"
    )
    assert client.patch("/api/v1/company", json={"timezone": "Nowhere/Land"}).status_code == 422
    assert client.patch("/api/v1/company", json={"currency": "toolong"}).status_code == 422


def test_healthz_reports_components(client: TestClient) -> None:
    body = client.get("/healthz").json()
    assert body["status"] == "ok" and body["database"] == "ok"
    assert body["automation_ticker"] == "disabled" and body["timezone"] == "UTC"
    assert client.get("/healthz").headers["x-request-id"]
