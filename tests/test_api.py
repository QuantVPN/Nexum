from fastapi.testclient import TestClient

from tests.conftest import PASSWORD, Company, login

WEEK = {"start": "2026-09-07T00:00:00Z", "end": "2026-09-14T00:00:00Z"}


def test_health_and_docs(client: TestClient) -> None:
    assert client.get("/healthz").json()["status"] == "ok"
    assert client.get("/api/docs").status_code == 200
    assert client.get("/api/openapi.json").status_code == 200


def test_auth_flow(client: TestClient, company: Company) -> None:
    assert client.get("/api/v1/auth/me").status_code == 401
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "admin@nexum.test", "password": "wrong"}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": "not an email", "password": "x"}
        ).status_code
        == 422
    )
    r = client.post("/api/v1/auth/login", json={"email": "ADMIN@nexum.test", "password": PASSWORD})
    assert r.status_code == 200 and r.json()["role"] == "admin" and r.json()["employee_id"] is None
    assert client.get("/api/v1/auth/me").json()["email"] == "admin@nexum.test"
    assert client.post("/api/v1/auth/logout").status_code == 200
    assert client.get("/api/v1/auth/me").status_code == 401


def test_rbac(client: TestClient, company: Company) -> None:
    login(client, "eva@nexum.test")
    assert client.get("/api/v1/employees").status_code == 403
    assert client.get("/api/v1/dashboard/admin").status_code == 403
    assert client.get(f"/api/v1/employees/{company.max.id}").status_code == 403
    assert client.get(f"/api/v1/employees/{company.eva.id}").status_code == 200
    assert client.post("/api/v1/automations/rules", json={}).status_code == 403
    assert client.get("/api/v1/dashboard/me").status_code == 200
    login(client, "mgr@nexum.test")
    assert client.get("/api/v1/employees").status_code == 200
    # managers cannot mint admin logins or close payroll
    r = client.post(
        "/api/v1/employees",
        json={
            "first_name": "A",
            "last_name": "B",
            "email": "ab@nexum.test",
            "start_date": "2026-01-01",
            "monthly_salary": "1000",
            "login_password": "password123",
            "role": "admin",
        },
    )
    assert r.status_code == 403
    assert (
        client.post(
            "/api/v1/payroll/periods", json={"start_date": "2026-09-01", "end_date": "2026-09-30"}
        ).status_code
        == 403
    )
    assert client.get("/api/v1/dashboard/me").status_code == 403  # manager has no employee record


def test_employee_and_department_crud(client: TestClient, company: Company) -> None:
    login(client, "admin@nexum.test")
    r = client.post("/api/v1/departments", json={"name": "Support", "cost_center": "SUP"})
    assert r.status_code == 201
    assert client.post("/api/v1/departments", json={"name": "Support"}).status_code == 409
    payload = {
        "first_name": "New",
        "last_name": "Person",
        "email": "new@nexum.test",
        "start_date": "2026-09-01",
        "department_id": r.json()["id"],
        "pay_type": "hourly",
        "hourly_rate": "150",
        "login_password": "password123",
    }
    r = client.post("/api/v1/employees", json=payload)
    assert r.status_code == 201 and r.json()["full_name"] == "New Person" and r.json()["user_id"]
    assert client.post("/api/v1/employees", json=payload).status_code == 409
    bad = dict(payload, email="x@nexum.test", pay_type="monthly", monthly_salary="0")
    assert client.post("/api/v1/employees", json=bad).status_code == 422
    assert len(client.get("/api/v1/employees").json()) == 4
    r = client.post(f"/api/v1/employees/{r.json()['id']}/deactivate")
    assert r.status_code == 200 and r.json()["is_active"] is False
    assert len(client.get("/api/v1/employees").json()) == 3
    assert len(client.get("/api/v1/employees", params={"active_only": "false"}).json()) == 4
    assert client.get("/api/v1/employees/999").status_code == 404


def test_schedule_flow(client: TestClient, company: Company) -> None:
    login(client, "mgr@nexum.test")
    assert client.get("/api/v1/templates").json().__len__() == 5
    r = client.post("/api/v1/schedule/generate", json={"week_start": "2026-09-07"})
    assert r.status_code == 200 and len(r.json()) == 10
    r = client.post("/api/v1/schedule/auto-assign", json=WEEK)
    assert len(r.json()) == 10
    assert client.get("/api/v1/shifts", params={**WEEK, "only_open": "true"}).json() == []
    r = client.post(
        "/api/v1/shifts",
        json={
            "department_id": company.department.id,
            "starts_at": "2026-09-12T08:00:00Z",
            "ends_at": "2026-09-12T12:00:00Z",
            "role_label": "Support",
        },
    )
    assert r.status_code == 201 and r.json()["employee_id"] is None
    shift_id = r.json()["id"]
    # Eva/Max sit at 36 h (+4 h fits under 40); Ida at 18 h would exceed 20. Ties sort by last name.
    assert client.get(f"/api/v1/shifts/{shift_id}/candidates").json() == [
        company.max.id,
        company.eva.id,
    ]
    assert (
        client.post(
            "/api/v1/shifts",
            json={
                "department_id": company.department.id,
                "starts_at": "2026-09-12T12:00:00Z",
                "ends_at": "2026-09-12T08:00:00Z",
            },
        ).status_code
        == 422
    )
    r = client.post(f"/api/v1/shifts/{shift_id}/assign", json={"employee_id": company.eva.id})
    assert r.status_code == 200 and r.json()["employee_id"] == company.eva.id
    assert client.post(f"/api/v1/shifts/{shift_id}/unassign").json()["employee_id"] is None
    r = client.post("/api/v1/shifts/publish", json=WEEK)
    assert r.json()["count"] == 11
    assert (
        client.post("/api/v1/shifts/publish", json={"shift_ids": [shift_id]}).json()["count"] == 0
    )
    assert client.post(f"/api/v1/shifts/{shift_id}/cancel").json()["status"] == "cancelled"
    assert (
        client.post(
            "/api/v1/shifts/publish", json={"start": WEEK["end"], "end": WEEK["start"]}
        ).status_code
        == 422
    )
    # employees only see their own shifts
    login(client, "eva@nexum.test")
    mine = client.get("/api/v1/shifts", params=WEEK).json()
    assert mine and all(s["employee_id"] == company.eva.id for s in mine)


def test_candidates_reflect_capacity(client: TestClient, company: Company) -> None:
    login(client, "mgr@nexum.test")
    r = client.post(
        "/api/v1/shifts",
        json={
            "department_id": company.department.id,
            "starts_at": "2026-09-12T08:00:00Z",
            "ends_at": "2026-09-12T12:00:00Z",
        },
    )
    ids = client.get(f"/api/v1/shifts/{r.json()['id']}/candidates").json()
    assert set(ids) == {company.eva.id, company.max.id, company.ida.id}


def test_time_off_and_time_entries(client: TestClient, company: Company) -> None:
    login(client, "eva@nexum.test")
    r = client.post(
        "/api/v1/time-off",
        json={"kind": "vacation", "start_date": "2026-10-05", "end_date": "2026-10-09"},
    )
    assert r.status_code == 201 and r.json()["days"] == 5 and r.json()["status"] == "pending"
    req_id = r.json()["id"]
    assert (
        client.post(
            "/api/v1/time-off",
            json={"kind": "sick", "start_date": "2026-10-07", "end_date": "2026-10-07"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/v1/time-off",
            json={"kind": "sick", "start_date": "2026-10-02", "end_date": "2026-10-01"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/api/v1/time-off",
            json={
                "kind": "sick",
                "start_date": "2026-11-01",
                "end_date": "2026-11-01",
                "employee_id": company.max.id,
            },
        ).status_code
        == 403
    )
    assert (
        client.post(f"/api/v1/time-off/{req_id}/decide", json={"approve": True}).status_code == 403
    )
    r = client.post("/api/v1/time/clock-in", json={})
    assert r.status_code == 201
    assert client.post("/api/v1/time/clock-in", json={}).status_code == 409
    assert (
        client.post("/api/v1/time/clock-out", json={"break_minutes": 15}).json()["status"]
        == "submitted"
    )
    assert client.post("/api/v1/time/clock-out", json={}).status_code == 409
    r = client.post(
        "/api/v1/time/entries",
        json={
            "clock_in": "2026-09-07T08:00:00Z",
            "clock_out": "2026-09-07T17:00:00Z",
            "break_minutes": 60,
        },
    )
    assert (
        r.status_code == 201
        and r.json()["worked_hours"] == 8.0
        and r.json()["status"] == "submitted"
    )
    assert (
        client.post(
            "/api/v1/time/entries",
            json={
                "clock_in": "2026-09-07T08:00:00Z",
                "clock_out": "2026-09-07T17:00:00Z",
                "employee_id": company.max.id,
            },
        ).status_code
        == 403
    )
    entry_id = r.json()["id"]
    login(client, "mgr@nexum.test")
    assert len(client.get("/api/v1/time-off", params={"status": "pending"}).json()) == 1
    assert (
        client.post(f"/api/v1/time-off/{req_id}/decide", json={"approve": False}).json()["status"]
        == "rejected"
    )
    assert (
        client.post(f"/api/v1/time-off/{req_id}/decide", json={"approve": True}).status_code == 409
    )
    assert client.post("/api/v1/time-off/999/decide", json={"approve": True}).status_code == 404
    assert (
        client.post(f"/api/v1/time/entries/{entry_id}/decide", json={"approve": True}).json()[
            "status"
        ]
        == "approved"
    )
    r = client.post(
        "/api/v1/time/entries",
        json={
            "clock_in": "2026-09-08T08:00:00Z",
            "clock_out": "2026-09-08T12:00:00Z",
            "employee_id": company.max.id,
        },
    )
    assert (
        r.status_code == 201 and r.json()["status"] == "approved"
    )  # manager-recorded lines are pre-approved
    assert (
        len(client.get("/api/v1/time/entries", params={"employee_id": company.eva.id}).json()) == 2
    )


def test_payroll_endpoints(client: TestClient, company: Company) -> None:
    login(client, "admin@nexum.test")
    client.post("/api/v1/schedule/generate", json={"week_start": "2026-09-07"})
    client.post("/api/v1/schedule/auto-assign", json=WEEK)
    client.post("/api/v1/shifts/publish", json=WEEK)
    r = client.post(
        "/api/v1/payroll/periods", json={"start_date": "2026-09-01", "end_date": "2026-09-30"}
    )
    assert r.status_code == 201
    pid = r.json()["id"]
    assert client.get("/api/v1/payroll/periods/current").json()["id"] == pid
    r = client.post(f"/api/v1/payroll/periods/{pid}/compute")
    assert r.status_code == 200 and len(r.json()["payslips"]) == 3
    eva = next(p for p in r.json()["payslips"] if p["employee_id"] == company.eva.id)
    assert eva["regular_hours"] == "36.00" and eva["gross_amount"] == "7560.00"
    assert r.json()["totals"]["gross"] == str(7560 + 38000 + 18 * 200) + ".00"
    assert client.post(f"/api/v1/payroll/periods/{pid}/close").json()["status"] == "closed"
    assert client.post(f"/api/v1/payroll/periods/{pid}/pay").json()["status"] == "paid"
    assert client.post(f"/api/v1/payroll/periods/{pid}/compute").status_code == 409
    assert client.get("/api/v1/payroll/periods/999").status_code == 404
    assert [p["id"] for p in client.get("/api/v1/payroll/periods").json()] == [pid]
    login(client, "eva@nexum.test")
    mine = client.get("/api/v1/payroll/me").json()
    assert len(mine) == 1 and mine[0]["gross_amount"] == "7560.00"
    assert client.get("/api/v1/payroll/me/estimate").json()["gross_amount"] == "7560.00"
    assert client.get("/api/v1/payroll/periods").status_code == 403


def test_dashboards(client: TestClient, company: Company) -> None:
    login(client, "mgr@nexum.test")
    client.post("/api/v1/schedule/generate", json={"week_start": "2026-09-07"})
    d = client.get("/api/v1/dashboard/admin").json()
    assert d["headcount"] == 3 and d["headcount_by_department"] == {"Operations": 3}
    assert (
        d["open_shifts_next_7_days"] >= 0
        and "automation" in d
        and d["pay_period"]["status"] == "open"
    )
    login(client, "eva@nexum.test")
    me = client.get("/api/v1/dashboard/me").json()
    assert me["employee_id"] == company.eva.id and me["clocked_in"] is False


def test_automation_endpoints(client: TestClient, company: Company, recipes: None) -> None:
    login(client, "admin@nexum.test")
    rules = client.get("/api/v1/automations/rules").json()
    assert len(rules) == 15 and all(r["enabled"] for r in rules)
    assert len(client.get("/api/v1/automations/actions").json()) >= 15
    assert "shift.created" in client.get("/api/v1/automations/events").json()
    assert client.get("/api/v1/automations/stats").json()["total_rules"] == 15
    # create + validate
    bad = {
        "name": "Bad",
        "trigger_type": "schedule",
        "trigger_config": {"kind": "never"},
        "actions": [{"type": "log"}],
    }
    assert client.post("/api/v1/automations/rules", json=bad).status_code == 422
    good = {
        "name": "Log shifts",
        "trigger_type": "event",
        "trigger_config": {"event": "shift.created"},
        "actions": [{"type": "log", "params": {"message": "shift {event.shift_id}"}}],
        "estimated_minutes_saved": 2,
    }
    r = client.post("/api/v1/automations/rules", json=good)
    assert r.status_code == 201
    rid = r.json()["id"]
    assert client.get(f"/api/v1/automations/rules/{rid}").json()["key"] is None
    # event fires through the API
    client.post(
        "/api/v1/shifts",
        json={
            "department_id": company.department.id,
            "starts_at": "2026-09-12T08:00:00Z",
            "ends_at": "2026-09-12T12:00:00Z",
        },
    )
    runs = client.get("/api/v1/automations/runs", params={"rule_id": rid}).json()
    assert (
        len(runs) == 1 and runs[0]["status"] == "success" and runs[0]["log"][0].startswith("shift ")
    )
    # manual run, patch, toggle, delete
    r = client.post(f"/api/v1/automations/rules/{rid}/run")
    assert r.status_code == 200 and r.json()["trigger_summary"] == "manual"
    assert (
        client.patch(
            f"/api/v1/automations/rules/{rid}", json={"actions": [{"type": "nope"}]}
        ).status_code
        == 422
    )
    r = client.patch(f"/api/v1/automations/rules/{rid}", json={"name": "Renamed", "enabled": False})
    assert r.json()["name"] == "Renamed" and r.json()["enabled"] is False
    assert client.post(f"/api/v1/automations/rules/{rid}/toggle").json()["enabled"] is True
    assert client.post("/api/v1/automations/tick").status_code == 200
    assert client.delete(f"/api/v1/automations/rules/{rid}").status_code == 204
    assert client.get(f"/api/v1/automations/rules/{rid}").status_code == 404
    # managers can run but not edit
    login(client, "mgr@nexum.test")
    daily = next(r for r in rules if r["key"] == "daily-cover")
    assert client.post(f"/api/v1/automations/rules/{daily['id']}/run").json()["status"] == "success"
    assert client.post(f"/api/v1/automations/rules/{daily['id']}/toggle").status_code == 403


def test_notifications(client: TestClient, company: Company, recipes: None) -> None:
    login(client, "eva@nexum.test")
    assert client.get("/api/v1/notifications/unread-count").json()["count"] == 0
    client.post(
        "/api/v1/time-off",
        json={"kind": "vacation", "start_date": "2026-10-05", "end_date": "2026-10-09"},
    )
    login(client, "mgr@nexum.test")
    notes = client.get("/api/v1/notifications").json()
    assert notes and notes[0]["title"].startswith("Time-off request from Eva Lund")
    assert client.get("/api/v1/notifications/unread-count").json()["count"] == 1
    assert client.post("/api/v1/notifications/read-all").json()["count"] == 1
    assert client.get("/api/v1/notifications/unread-count").json()["count"] == 0
