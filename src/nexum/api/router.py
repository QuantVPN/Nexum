from __future__ import annotations

from fastapi import APIRouter

from nexum.api.routes import (
    auth,
    automations,
    company,
    dashboard,
    employees,
    notifications,
    org,
    payroll,
    schedule,
    time,
    timeoff,
)

api_router = APIRouter()
for module in (
    auth,
    company,
    org,
    employees,
    schedule,
    timeoff,
    time,
    payroll,
    dashboard,
    automations,
    notifications,
):
    api_router.include_router(module.router)
