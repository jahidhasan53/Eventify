"""Authentication dependencies. Identity ALWAYS comes from a verified token + a fresh database
lookup — never from request bodies, query strings or client-supplied headers."""

import uuid
from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.exceptions import AppError, ForbiddenError, UnauthorizedError
from app.core.security import decode_access_token
from app.database.connection import DbSession
from app.models.enums import UserStatus
from app.models.user import User

_bearer = HTTPBearer(auto_error=False, description="JWT access token (alternatively sent as the `access_token` cookie)")


def _extract_token(request: Request, credentials: HTTPAuthorizationCredentials | None) -> str | None:
    if credentials is not None and credentials.credentials:
        return credentials.credentials
    return request.cookies.get("access_token")


async def _load_active_user(db, token: str) -> User:
    payload = decode_access_token(token)
    try:
        user_id = uuid.UUID(payload["sub"])
    except (KeyError, ValueError):
        raise UnauthorizedError("Invalid authentication credentials") from None
    user = await db.get(User, user_id)
    if user is None:
        raise UnauthorizedError("Invalid authentication credentials")
    if user.status != UserStatus.ACTIVE.value:
        raise ForbiddenError("This account has been suspended")
    return user


async def get_current_user(
    request: Request,
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User:
    token = _extract_token(request, credentials)
    if not token:
        raise UnauthorizedError("Authentication required")
    return await _load_active_user(db, token)


async def get_optional_user(
    request: Request,
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> User | None:
    """For public endpoints that show more to the owner/admin. A missing or bad token = anonymous."""
    token = _extract_token(request, credentials)
    if not token:
        return None
    try:
        return await _load_active_user(db, token)
    except AppError:
        return None


CurrentUser = Annotated[User, Depends(get_current_user)]
OptionalUser = Annotated[User | None, Depends(get_optional_user)]
