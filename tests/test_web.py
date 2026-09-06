import re

from fastapi.testclient import TestClient

from tests.conftest import PASSWORD, Company


def web_login(client: TestClient, email: str) -> None:
    r = client.post("/login", data={"email": email, "password": PASSWORD})
    assert r.status_code == 303, r.text


def page(client: TestClient, url: str, expect: int = 200) -> str:
    r = client.get(url)
    assert r.status_code == expect, f"{url} -> {r.status_code}: {r.text[:200]}"
    return r.text


def test_anonymous_redirects_to_login(client: TestClient) -> None:
    assert client.get("/").headers["location"] == "/login"
    r = client.get("/dashboard")
    assert r.status_code == 303 and r.headers["location"].startswith("/login?next=/dashboard")
    assert "Sign in" in page(client, "/login")


def test_login_logout_and_flash(client: TestClient, company: Company) -> None:
    r = client.post("/login", data={"email": "admin@nexum.test", "password": "nope"})
    assert r.headers["location"] == "/login"
    assert "Invalid email or password" in page(client, "/login")
    web_login(client, "admin@nexum.test")
    assert client.get("/").headers["location"] == "/dashboard"
    assert client.get("/login").headers["location"] == "/dashboard"
    r = client.post(
        "/login", data={"email": "admin@nexum.test", "password": PASSWORD, "next": "//evil.example"}
    )
    assert r.headers["location"] == "/dashboard"  # open redirect blocked
    assert client.post("/logout").headers["location"] == "/login"
    assert client.get("/dashboard").status_code == 303


def test_manager_pages_render(client: TestClient, company: Company, recipes: None) -> None:
    web_login(client, "mgr@nexum.test")
    for url in (
        "/dashboard",
        "/employees",
        "/employees?all=1",
        f"/employees/{company.eva.id}",
        "/schedule",
        "/schedule?week=2026-09-07&department_id=1",
        "/templates",
        "/approvals",
        "/payroll",
        "/automations",
        "/notifications",
    ):
        html = page(client, url)
        assert "<html" in html and company.manager.full_name in html
    assert "Company overview" in page(client, "/dashboard")
    assert page(client, "/employees/999", 404)
    assert page(client, "/automations/new", 403)  # admin only
    assert "No employee record" in page(client, "/me")


def test_admin_schedule_and_payroll_flow(
    client: TestClient, company: Company, recipes: None
) -> None:
    web_login(client, "admin@nexum.test")
    assert (
        client.post(
            "/schedule/generate", data={"week": "2026-09-07", "department_id": ""}
        ).status_code
        == 303
    )
    assert (
        client.post(
            "/schedule/auto-assign", data={"week": "2026-09-07", "department_id": ""}
        ).status_code
        == 303
    )
    html = page(client, "/schedule?week=2026-09-07")
    assert html.count('class="chip chip-draft"') == 10
    assert "Auto-assigned 10 shift(s)" in html
    assert (
        client.post(
            "/shifts",
            data={
                "department_id": str(company.department.id),
                "starts_at": "2026-09-12T08:00",
                "ends_at": "2026-09-12T12:00",
                "employee_id": "",
                "role_label": "Lead",
            },
        ).status_code
        == 303
    )
    html = page(client, "/schedule?week=2026-09-07")
    assert 'class="chip chip-open"' in html and "Lead" in html
    assert (
        client.post(
            "/shifts",
            data={
                "department_id": str(company.department.id),
                "starts_at": "2026-09-12T12:00",
                "ends_at": "2026-09-12T08:00",
            },
        ).status_code
        == 303
    )
    assert "must end after it starts" in page(client, "/schedule?week=2026-09-07")
    assert (
        client.post(
            "/schedule/publish", data={"week": "2026-09-07", "department_id": ""}
        ).status_code
        == 303
    )
    assert "Published 11 shift(s)" in page(client, "/schedule?week=2026-09-07")
    assert (
        client.post(
            "/payroll/periods", data={"start_date": "2026-09-01", "end_date": "2026-09-30"}
        ).status_code
        == 303
    )
    html = page(client, "/payroll")
    pid = re.search(r'href="/payroll/(\d+)"', html).group(1)
    assert client.post(f"/payroll/{pid}/compute").status_code == 303
    html = page(client, f"/payroll/{pid}")
    assert "Computed 3 payslip(s)" in html and "Eva Lund" in html
    assert client.post(f"/payroll/{pid}/close").status_code == 303
    assert client.post(f"/payroll/{pid}/pay").status_code == 303
    assert client.post(f"/payroll/{pid}/bogus").status_code == 303
    assert "Unknown payroll action" in page(client, f"/payroll/{pid}")


def test_admin_people_and_templates_forms(client: TestClient, company: Company) -> None:
    web_login(client, "admin@nexum.test")
    assert client.post("/departments", data={"name": "Support"}).status_code == 303
    html = page(client, "/employees")
    assert "Support" in html and "created" in html
    data = {
        "first_name": "New",
        "last_name": "Hire",
        "email": "new@nexum.test",
        "start_date": "2026-09-01",
        "department_id": "1",
        "pay_type": "hourly",
        "hourly_rate": "150",
        "login_password": "password123",
        "role": "manager",
        "employment_type": "hourly",
        "weekly_hours": "32",
    }
    assert client.post("/employees", data=data).status_code == 303
    assert "New Hire added" in page(client, "/employees")
    assert (
        client.post("/employees", data=dict(data, email="bad", start_date="not-a-date")).status_code
        == 303
    )
    assert "Start date" in page(client, "/employees")
    assert (
        client.post("/employees", data={"first_name": "only"}).status_code == 422
    )  # missing fields -> HTML error page
    assert (
        client.post(
            "/templates",
            data={
                "department_id": "1",
                "name": "Night",
                "start_time": "22:00",
                "end_time": "06:00",
                "headcount": "1",
                "weekdays": ["4", "5"],
            },
        ).status_code
        == 303
    )
    html = page(client, "/templates")
    assert "Night" in html and "Added template for 2 weekday(s)" in html
    tid = re.findall(r"/templates/(\d+)/delete", html)[-1]
    assert client.post(f"/templates/{tid}/delete").status_code == 303
    assert client.post("/templates/999/delete").status_code == 303
    assert "Template not found" in page(client, "/templates")
    emp_id = re.findall(r'href="/employees/(\d+)"', page(client, "/employees"))[-1]
    assert client.post(f"/employees/{emp_id}/deactivate").status_code == 303


def test_automation_pages_and_forms(client: TestClient, company: Company, recipes: None) -> None:
    web_login(client, "admin@nexum.test")
    html = page(client, "/automations")
    assert "Fill open shifts every morning" in html and "built-in" in html
    page(client, "/automations/new")
    r = client.post(
        "/automations/new",
        data={
            "name": "Broken",
            "trigger_type": "schedule",
            "trigger_config": "{oops",
            "actions": "[]",
        },
    )
    assert r.status_code == 422 and "invalid JSON" in r.text and 'value="Broken"' in r.text
    r = client.post(
        "/automations/new",
        data={
            "name": "Friday note",
            "trigger_type": "schedule",
            "trigger_config": '{"kind": "weekly", "weekday": 4, "at": "15:00"}',
            "conditions": "[]",
            "actions": '[{"type": "notify_role", "params": {"role": "employee", "title": "Nice weekend"}}]',
            "estimated_minutes_saved": "3",
        },
    )
    assert r.status_code == 303
    url = r.headers["location"]
    html = page(client, url)
    assert "Friday note" in html and "weekly on Fri at 15:00 UTC" in html
    assert client.post(f"{url}/run").status_code == 303
    html = page(client, url)
    assert "Run finished: success" in html and '<details class="run"' in html
    assert client.post(f"{url}/toggle").status_code == 303
    assert "disabled" in page(client, "/automations")
    assert client.post(f"{url}/delete").status_code == 303
    assert page(client, url, 404)
    assert page(client, "/automations/999", 404)
    web_login(client, "mgr@nexum.test")
    assert client.post(f"{url}/toggle").status_code == 403


def test_approvals_page_actions(client: TestClient, company: Company) -> None:
    web_login(client, "eva@nexum.test")
    assert (
        client.post(
            "/me/requests",
            data={
                "kind": "vacation",
                "start_date": "2026-10-05",
                "end_date": "2026-10-09",
                "reason": "Trip",
            },
        ).status_code
        == 303
    )
    assert (
        client.post(
            "/me/time/entries",
            data={
                "clock_in": "2026-09-07T08:00",
                "clock_out": "2026-09-07T17:00",
                "break_minutes": "30",
                "shift_id": "",
            },
        ).status_code
        == 303
    )
    assert "Submitted 8.50 h" in page(client, "/me/time")
    web_login(client, "mgr@nexum.test")
    html = page(client, "/approvals")
    assert "Trip" in html and "8.50" in html
    req_id = re.search(r"/time-off/(\d+)/decide", html).group(1)
    entry_id = re.search(r"/time/entries/(\d+)/decide", html).group(1)
    assert (
        client.post(f"/time-off/{req_id}/decide", data={"decision": "approve"}).status_code == 303
    )
    assert (
        client.post(f"/time/entries/{entry_id}/decide", data={"decision": "reject"}).status_code
        == 303
    )
    html = page(client, "/approvals")
    assert "Request approved" in html and "Time entry rejected" in html
    assert client.post("/time-off/999/decide", data={"decision": "approve"}).status_code == 303
    assert "Request not found" in page(client, "/approvals")


def test_employee_self_service(client: TestClient, recipes: None, company: Company) -> None:
    web_login(client, "eva@nexum.test")
    assert client.get("/").headers["location"] == "/me"
    assert page(client, "/dashboard", 403)
    assert page(client, "/employees", 403)
    html = page(client, "/me")
    assert "Hi Eva" in html and "Clock in" in html
    assert client.post("/me/clock-in").status_code == 303
    html = page(client, "/me")
    assert "Clocked in" in html and "Clock out" in html
    assert client.post("/me/clock-in").status_code == 303
    assert "already clocked in" in page(client, "/me")
    assert client.post("/me/clock-out", data={"break_minutes": "0"}).status_code == 303
    assert "submitted for approval" in page(client, "/me")
    assert client.post("/me/clock-out", data={"break_minutes": "0"}).status_code == 303
    assert "not clocked in" in page(client, "/me")
    page(client, "/me/pay")
    page(client, "/me/time")
    page(client, "/me/requests")
    assert (
        client.post(
            "/me/requests",
            data={"kind": "sick", "start_date": "2026-10-02", "end_date": "2026-10-01"},
        ).status_code
        == 303
    )
    assert "on or after the start date" in page(client, "/me/requests")
    html = page(client, "/notifications")
    assert "Welcome to the team, Eva!" in html
    assert client.post("/notifications/read-all").status_code == 303
    assert 'class="unread' not in page(client, "/notifications")
    assert page(client, "/static/style.css").startswith(":root")
