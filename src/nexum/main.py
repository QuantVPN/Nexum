"""ASGI application factory."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from nexum import __version__
from nexum.automation import get_engine
from nexum.config import Settings, get_settings
from nexum.db import init_db
from nexum.errors import AuthenticationError, NexumError, ValidationError

log = logging.getLogger("nexum")

STATIC_DIR = Path(__file__).parent / "web" / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        init_db()
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
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    return app


app = create_app()
