from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response

from app.core.config import get_settings
from app.core.cookies import REFRESH_COOKIE, clear_auth_cookies, set_auth_cookies
from app.core.exceptions import UnauthorizedError, error_response
from app.core.rate_limit import RateLimit, client_ip
from app.database.connection import DbSession
from app.dependencies.authentication import CurrentUser
from app.schemas.auth import (
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginOut,
    LoginRequest,
    RefreshOut,
    RegisterRequest,
    ResetPasswordRequest,
)
from app.schemas.common import Envelope, ok
from app.schemas.user import UserOut
from app.services import auth_service

router = APIRouter(prefix="/auth", tags=["Authentication"])

_login_limit = RateLimit("login", lambda: get_settings().rate_limit_login_per_minute)
_sensitive_limit = RateLimit("auth-sensitive", lambda: get_settings().rate_limit_sensitive_per_minute)


def _meta(request: Request) -> tuple[str | None, str | None]:
    return client_ip(request), request.headers.get("user-agent")


@router.post(
    "/register",
    status_code=201,
    response_model=Envelope[UserOut],
    summary="Create an attendee or organizer account",
    dependencies=[Depends(_sensitive_limit)],
)
async def register(body: RegisterRequest, db: DbSession):
    user = await auth_service.register_user(
        db,
        email=body.email,
        password=body.password,
        full_name=body.full_name,
        role=body.role,
        organization_name=body.organization_name,
    )
    return ok(UserOut.model_validate(user), "Account created successfully")


@router.post(
    "/login",
    response_model=Envelope[LoginOut],
    summary="Log in (sets HttpOnly cookies and returns an access token)",
    dependencies=[Depends(_login_limit)],
)
async def login(body: LoginRequest, request: Request, response: Response, db: DbSession):
    user = await auth_service.authenticate_user(db, body.email, body.password)
    ip, agent = _meta(request)
    tokens, _ = await auth_service.issue_session(db, user, ip=ip, user_agent=agent)
    await db.commit()
    csrf = set_auth_cookies(response, tokens)
    data = LoginOut(
        access_token=tokens.access_token,
        expires_in=tokens.expires_in,
        csrf_token=csrf,
        user=UserOut.model_validate(user),
    )
    return ok(data, "Logged in successfully")


@router.post(
    "/refresh",
    response_model=Envelope[RefreshOut],
    summary="Rotate the refresh token (read from the HttpOnly cookie) and get a new access token",
    dependencies=[Depends(_login_limit)],
)
async def refresh(request: Request, response: Response, db: DbSession):
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise UnauthorizedError("Refresh token missing")
    ip, agent = _meta(request)
    try:
        _, tokens = await auth_service.rotate_refresh_token(db, raw, ip=ip, user_agent=agent)
    except UnauthorizedError as exc:
        # Build the error response ourselves so the stale cookies can be cleared on it.
        failure = error_response(exc.status_code, exc.message, exc.detail, exc.headers)
        clear_auth_cookies(failure)
        return failure
    csrf = set_auth_cookies(response, tokens)
    data = RefreshOut(access_token=tokens.access_token, expires_in=tokens.expires_in, csrf_token=csrf)
    return ok(data, "Token refreshed")


@router.post("/logout", response_model=Envelope[None], summary="Revoke the refresh token and clear cookies")
async def logout(request: Request, response: Response, db: DbSession):
    await auth_service.revoke_refresh_token(db, request.cookies.get(REFRESH_COOKIE))
    clear_auth_cookies(response)
    return ok(None, "Logged out successfully")


@router.get("/me", response_model=Envelope[UserOut], summary="Current authenticated user")
async def me(user: CurrentUser):
    return ok(UserOut.model_validate(user))


@router.post(
    "/forgot-password",
    response_model=Envelope[None],
    summary="Request a password-reset e-mail (always answers the same way)",
    dependencies=[Depends(_sensitive_limit)],
)
async def forgot_password(body: ForgotPasswordRequest, db: DbSession, background: BackgroundTasks):
    await auth_service.request_password_reset(db, body.email, background)
    return ok(None, "If an account with that email exists, a password reset link has been sent")


@router.post(
    "/reset-password",
    response_model=Envelope[None],
    summary="Set a new password using the e-mailed token",
    dependencies=[Depends(_sensitive_limit)],
)
async def reset_password(body: ResetPasswordRequest, db: DbSession):
    await auth_service.reset_password(db, body.token, body.new_password)
    return ok(None, "Password has been reset. Please log in with your new password")


@router.post(
    "/change-password",
    response_model=Envelope[RefreshOut],
    summary="Change password (revokes every other session and returns a fresh one)",
    dependencies=[Depends(_sensitive_limit)],
)
async def change_password(
    body: ChangePasswordRequest, request: Request, response: Response, user: CurrentUser, db: DbSession
):
    ip, agent = _meta(request)
    tokens = await auth_service.change_password(
        db, user, body.current_password, body.new_password, ip=ip, user_agent=agent
    )
    csrf = set_auth_cookies(response, tokens)
    data = RefreshOut(access_token=tokens.access_token, expires_in=tokens.expires_in, csrf_token=csrf)
    return ok(data, "Password changed successfully")
