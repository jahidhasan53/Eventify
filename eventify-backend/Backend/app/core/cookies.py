"""Auth cookie handling.

* access_token  - HttpOnly, short-lived JWT (path "/")
* refresh_token - HttpOnly, opaque, only sent to /api/v1/auth/* (path-scoped)
* csrf_token    - readable by JavaScript; the client echoes it in the X-CSRF-Token header
                  (double-submit-cookie CSRF protection, enforced in app.core.middleware)

`Secure` is enabled automatically in production (or via COOKIE_SECURE) and SameSite is configurable.
"""

from fastapi import Response

from app.core.config import get_settings
from app.core.security import generate_csrf_token, seconds_until
from app.services.auth_service import SessionTokens

ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"
CSRF_COOKIE = "csrf_token"


def _common() -> dict:
    settings = get_settings()
    return {
        "secure": settings.use_secure_cookies,
        "samesite": settings.cookie_samesite,
        "domain": settings.cookie_domain,
    }


def set_auth_cookies(response: Response, tokens: SessionTokens) -> str:
    """Attach the three auth cookies to `response`. Returns the CSRF token (also sent in the body)."""
    settings = get_settings()
    csrf = generate_csrf_token()
    refresh_ttl = seconds_until(tokens.refresh_expires_at)
    common = _common()
    response.set_cookie(ACCESS_COOKIE, tokens.access_token, max_age=tokens.expires_in, httponly=True, path="/", **common)
    response.set_cookie(
        REFRESH_COOKIE,
        tokens.refresh_token,
        max_age=refresh_ttl,
        httponly=True,
        path=f"{settings.api_v1_prefix}/auth",
        **common,
    )
    response.set_cookie(CSRF_COOKIE, csrf, max_age=refresh_ttl, httponly=False, path="/", **common)
    return csrf


def clear_auth_cookies(response: Response) -> None:
    settings = get_settings()
    common = _common()
    response.delete_cookie(ACCESS_COOKIE, path="/", httponly=True, **common)
    response.delete_cookie(REFRESH_COOKIE, path=f"{settings.api_v1_prefix}/auth", httponly=True, **common)
    response.delete_cookie(CSRF_COOKIE, path="/", **common)
