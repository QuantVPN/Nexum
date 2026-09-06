"""Jinja2 setup, flash messages and a render helper for the server-rendered UI."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from sqlalchemy.orm import Session

from nexum import __version__
from nexum.config import get_settings
from nexum.models import Role, User
from nexum.services import notifications

TEMPLATES_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    undefined=StrictUndefined,  # typos in templates fail loudly instead of rendering blank
    autoescape=select_autoescape(["html", "xml"]),
)
templates = Jinja2Templates(env=_env)

WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def fmt_money(value: Decimal | float | int | None, currency: str | None = None) -> str:
    if value is None:
        return "-"
    text = f"{Decimal(str(value)):,.2f}".replace(",", "\u00a0")
    return f"{text} {currency}" if currency else text


def fmt_dt(value: datetime | None, fmt: str = "%a %d %b %H:%M") -> str:
    return value.strftime(fmt) if value else "-"


def fmt_date(value: date | datetime | None, fmt: str = "%Y-%m-%d") -> str:
    return value.strftime(fmt) if value else "-"


def fmt_time(value: datetime | None) -> str:
    return value.strftime("%H:%M") if value else "-"


def fmt_hours(value: Decimal | float | int | None) -> str:
    if value is None:
        return "-"
    return f"{float(value):.1f} h"


def weekday_name(index: int) -> str:
    return WEEKDAYS[index % 7]


def pretty_json(value: Any) -> str:
    return json.dumps(value, indent=2, default=str)


templates.env.filters.update(
    {
        "money": fmt_money,
        "dt": fmt_dt,
        "d": fmt_date,
        "t": fmt_time,
        "hours": fmt_hours,
        "weekday": weekday_name,
        "pretty_json": pretty_json,
    }
)
templates.env.globals.update({"app_version": __version__, "Role": Role})


def flash(request: Request, message: str, level: str = "success") -> None:
    queue: list[list[str]] = request.session.setdefault("flash", [])
    queue.append([level, message])
    request.session["flash"] = queue


def pop_flashes(request: Request) -> list[list[str]]:
    queue: list[list[str]] = request.session.pop("flash", [])
    return queue


def render(
    request: Request,
    template: str,
    *,
    db: Session | None = None,
    user: User | None = None,
    status_code: int = 200,
    **context: Any,
) -> HTMLResponse:
    unread = notifications.unread_count(db, user) if (db is not None and user is not None) else 0
    payload: dict[str, Any] = {
        "current_user": user,
        "unread_count": unread,
        "flashes": pop_flashes(request),
        "settings": get_settings(),
        "today": date.today(),
        **context,
    }
    response = templates.TemplateResponse(request, template, payload, status_code=status_code)
    return cast(HTMLResponse, response)
