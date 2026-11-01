"""Eventify API entry point.

    uvicorn app.main:app --reload

The schema is managed exclusively by Alembic (`alembic upgrade head`); the app never calls create_all().
"""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import get_settings
from app.core.exceptions import register_exception_handlers
from app.core.middleware import BodySizeLimitMiddleware, CSRFMiddleware, SecurityHeadersMiddleware
from app.core.rate_limit import RateLimit
from app.database.connection import engine
from app.schemas.common import ok

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("eventify")


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    task: asyncio.Task | None = None
    if settings.enable_background_jobs and settings.environment != "testing":
        from app.services.maintenance_service import maintenance_loop

        task = asyncio.create_task(maintenance_loop(), name="eventify-maintenance")
    logger.info("%s started (environment=%s)", settings.app_name, settings.environment)
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title=f"{settings.app_name} API",
        version="1.0.0",
        description="Secure event & workshop management backend. All endpoints live under /api/v1.",
        lifespan=lifespan,
        # Interactive docs are only exposed outside production.
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url="/redoc" if settings.docs_enabled else None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )

    # Middleware: the LAST one added is the OUTERMOST. Request order is
    # SecurityHeaders -> CORS -> BodySizeLimit -> CSRF -> routes, so that even 403/413 responses
    # carry CORS and security headers.
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_body_bytes)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,  # explicit allow-list from FRONTEND_ORIGIN, never "*"
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-CSRF-Token"],
        expose_headers=["Retry-After"],
        max_age=600,
    )
    app.add_middleware(SecurityHeadersMiddleware)

    register_exception_handlers(app)
    # Per-IP ceiling for the whole API; login/sensitive endpoints add their own, stricter limits.
    global_limit = RateLimit("global", lambda: get_settings().rate_limit_global_per_minute)
    app.include_router(api_router, prefix=settings.api_v1_prefix, dependencies=[Depends(global_limit)])

    @app.get("/health", tags=["Health"], summary="Liveness probe")
    async def health():
        return ok({"status": "ok"})

    return app


app = create_app()
