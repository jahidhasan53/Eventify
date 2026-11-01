"""Pure-ASGI middleware: request size limit, CSRF protection for cookie auth, security headers."""

import json

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings
from app.core.security import constant_time_equals

_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


async def _send_json(send: Send, status: int, message: str) -> None:
    body = json.dumps({"success": False, "message": message, "detail": None}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})


class _BodyTooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    """Rejects bodies larger than MAX_REQUEST_BODY_BYTES (checks Content-Length and the streamed size)."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        raw_length = headers.get(b"content-length")
        if raw_length is not None:
            try:
                if int(raw_length) > self.max_bytes:
                    await _send_json(send, 413, "Request body too large")
                    return
            except ValueError:
                await _send_json(send, 400, "Invalid Content-Length header")
                return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _BodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _BodyTooLarge:
            if not response_started:
                await _send_json(send, 413, "Request body too large")


class CSRFMiddleware:
    """Double-submit-cookie CSRF protection.

    Applies only to state-changing requests authenticated by *cookies* (no Authorization header).
    The client must echo the (JS-readable) `csrf_token` cookie in the `X-CSRF-Token` header.
    Credential-less entry points (login/register/password reset) are exempt. The Origin header,
    when present, must also be on the CORS allow-list.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        settings = get_settings()
        prefix = settings.api_v1_prefix
        self.exempt = {f"{prefix}/auth/{name}" for name in ("login", "register", "forgot-password", "reset-password")}
        self.allowed_origins = set(settings.cors_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] not in _UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        path = request.url.path.rstrip("/")
        cookies = request.cookies
        uses_cookie_auth = "access_token" in cookies or "refresh_token" in cookies

        if path in self.exempt or request.headers.get("authorization") or not uses_cookie_auth:
            await self.app(scope, receive, send)
            return

        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") not in self.allowed_origins:
            await _send_json(send, 403, "Request origin is not allowed")
            return

        cookie_token = cookies.get("csrf_token")
        header_token = request.headers.get("x-csrf-token")
        if not cookie_token or not header_token or not constant_time_equals(cookie_token, header_token):
            await _send_json(send, 403, "CSRF validation failed")
            return

        await self.app(scope, receive, send)


class SecurityHeadersMiddleware:
    """Adds conservative security headers to every HTTP response."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        settings = get_settings()
        self.hsts = settings.is_production
        self.docs_paths = ("/docs", "/redoc", "/openapi.json") if settings.docs_enabled else ()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        is_docs = path.startswith(self.docs_paths) if self.docs_paths else False

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {name.lower() for name, _ in headers}
                extra = {
                    b"x-content-type-options": b"nosniff",
                    b"x-frame-options": b"DENY",
                    b"referrer-policy": b"no-referrer",
                }
                if not is_docs:
                    extra[b"content-security-policy"] = b"default-src 'none'; frame-ancestors 'none'"
                    extra[b"cache-control"] = b"no-store"
                if self.hsts:
                    extra[b"strict-transport-security"] = b"max-age=63072000; includeSubDomains"
                for name, value in extra.items():
                    if name not in existing:
                        headers.append((name, value))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)
