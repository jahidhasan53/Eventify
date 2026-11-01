"""Application exceptions and the global handlers that turn them into the standard envelope:

    {"success": false, "message": "...", "detail": null | ...}

Internal exception text and stack traces are logged server-side and never sent to clients.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

logger = logging.getLogger("eventify")


class AppError(Exception):
    status_code = 400
    default_message = "Bad request"

    def __init__(
        self,
        message: str | None = None,
        *,
        detail: Any = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.default_message
        self.detail = detail
        self.headers = headers
        super().__init__(self.message)


class BadRequestError(AppError):
    status_code = 400
    default_message = "Bad request"


class UnauthorizedError(AppError):
    status_code = 401
    default_message = "Authentication required"

    def __init__(self, message: str | None = None, *, detail: Any = None, headers: dict[str, str] | None = None):
        super().__init__(message, detail=detail, headers=headers or {"WWW-Authenticate": "Bearer"})


class ForbiddenError(AppError):
    status_code = 403
    default_message = "You do not have permission to perform this action"


class NotFoundError(AppError):
    status_code = 404
    default_message = "Resource not found"


class ConflictError(AppError):
    status_code = 409
    default_message = "Conflict with the current state of the resource"


class TooManyRequestsError(AppError):
    status_code = 429
    default_message = "Too many requests"


_STATUS_MESSAGES = {
    400: "Bad request",
    401: "Authentication required",
    403: "You do not have permission to perform this action",
    404: "Resource not found",
    405: "Method not allowed",
    409: "Conflict",
    413: "Request body too large",
    429: "Too many requests",
}


def error_response(
    status_code: int, message: str, detail: Any = None, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"success": False, "message": message, "detail": detail},
        headers=headers,
    )


async def _app_error_handler(_: Request, exc: AppError) -> JSONResponse:
    return error_response(exc.status_code, exc.message, exc.detail, exc.headers)


async def _http_exception_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    message = _STATUS_MESSAGES.get(exc.status_code, "Request failed")
    return error_response(exc.status_code, message, None, getattr(exc, "headers", None))


async def _validation_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    # Consistent, safe format: never echo the submitted input (it may contain passwords).
    errors = []
    for err in exc.errors():
        location = ".".join(str(part) for part in err.get("loc", ()) if part != "body")
        errors.append({"field": location or None, "message": err.get("msg", "Invalid value"), "type": err.get("type")})
    return error_response(422, "Validation failed", errors)


async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path, exc_info=exc)
    return error_response(500, "Internal server error")


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(RequestValidationError, _validation_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, _unhandled_handler)
