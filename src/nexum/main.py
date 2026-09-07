"""ASGI application factory."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from nexum import __version__
from nexum.automation import get_engine
from nexum.config import Settings, get_settings
from nexum.db import init_db, session_scope
from nexum.errors import AuthenticationError, NexumError, ValidationError
from nexum.services import company
from nexum.services.calendar import timezone_name

log = logging.getLogger("nexum")
access_log = logging.getLogger("nexum.access")
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


CSP_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self'; connect-src 'self'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; object-src 'none'"
)
CSP_EXEMPT_PREFIXES = ("/api/docs", "/api/redoc", "/api/openapi.json")
SECURITY_HEADERS: tuple[tuple[bytes, bytes], ...] = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"strict-origin-when-cross-origin"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
)


class SecurityHeadersMiddleware:
    """Adds the standard hardening headers and a Content-Security-Policy (the API docs are
    exempt because Swagger UI loads its assets from a CDN)."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        exempt = path.startswith(CSP_EXEMPT_PREFIXES)

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(SECURITY_HEADERS)
                if not exempt:
                    headers.append((b"content-security-policy", CSP_POLICY.encode()))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


class RequestContextMiddleware:
    """Tags every request with an id (``X-Request-ID``) and writes one access-log line."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-request-id", b"").decode("latin-1").strip()
        request_id = incoming[:64] if incoming else uuid.uuid4().hex[:16]
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_holder = {"status": 0}

        async def send_wrapper(message: Any) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = message["status"]
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode()),
                ]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            access_log.info(
                "%s %s -> %s in %.1fms [%s]",
                scope.get("method"),
                scope.get("path"),
                status_holder["status"],
                elapsed_ms,
                request_id,
            )
            request_id_var.reset(token)


STATIC_DIR = Path(__file__).parent / "web" / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if not logging.getLogger().handlers:
            logging.basicConfig(
                level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
            )
        logging.getLogger("httpx").setLevel(logging.WARNING)
        init_db()
        with session_scope() as session:
            company.prime_timezone(session)
        engine = get_engine()
        engine.subscribe()
        if settings.automation_enabled and not settings.is_test:
            engine.start_background()
        try:
            yield
        finally:
            engine.stop_background()
            engine.unsubscribe()

    app = FastAPI(
        title=settings.app_name,
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        max_age=settings.session_max_age_seconds,
        same_site="lax",
        https_only=settings.environment == "production",
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestContextMiddleware)
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    from nexum.api.router import api_router
    from nexum.web.routes import web_router

    app.include_router(api_router, prefix="/api/v1")
    app.include_router(web_router)

    @app.exception_handler(NexumError)
    async def _domain_error(request: Request, exc: NexumError) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})
        if isinstance(exc, AuthenticationError):
            return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)
        from nexum.web.routes import render_error

        return render_error(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
        if request.url.path.startswith("/api/"):
            return JSONResponse(status_code=422, content={"detail": jsonable_encoder(exc.errors())})
        from nexum.web.routes import render_error

        fields = ", ".join(str(err["loc"][-1]) for err in exc.errors()[:5])
        return render_error(request, ValidationError(f"Please check these fields: {fields}"))

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        from sqlalchemy import text

        from nexum.db import get_engine as get_db_engine

        database = "ok"
        try:
            with get_db_engine().connect() as conn:
                conn.execute(text("SELECT 1"))
        except Exception as exc:  # pragma: no cover - only when the DB is down
            database = f"error: {type(exc).__name__}"
        engine = get_engine()
        ticker = (
            "disabled"
            if not settings.automation_enabled or settings.is_test
            else ("running" if engine.is_running else "stopped")
        )
        body = {
            "status": "ok" if database == "ok" else "degraded",
            "version": __version__,
            "database": database,
            "automation_ticker": ticker,
            "timezone": timezone_name(),
        }
        return JSONResponse(status_code=200 if database == "ok" else 503, content=body)

    return app


app = create_app()
