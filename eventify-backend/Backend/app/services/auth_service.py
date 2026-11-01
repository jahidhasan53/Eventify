"""Authentication business logic: registration, login with lockout, refresh-token rotation with
reuse detection, password reset and password change."""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import BackgroundTasks
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.exceptions import (
    BadRequestError,
    ConflictError,
    ForbiddenError,
    TooManyRequestsError,
    UnauthorizedError,
)
from app.core.security import (
    DUMMY_PASSWORD_HASH,
    create_access_token,
    generate_opaque_token,
    hash_password,
    hash_token,
    password_needs_rehash,
    verify_password,
)
from app.core.utils import utcnow
from app.models.enums import Role, UserStatus
from app.models.user import PasswordResetToken, RefreshToken, User
from app.services.email_service import send_password_reset_email

MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15
# A refresh token that was rotated moments ago may legitimately be presented again by a second
# browser tab racing the first; inside this window we just reject it instead of killing the session.
REFRESH_REUSE_GRACE_SECONDS = 10


@dataclass
class SessionTokens:
    access_token: str
    expires_in: int
    refresh_token: str
    refresh_expires_at: datetime


async def get_user_by_email(db: AsyncSession, email: str) -> User | None:
    result = await db.execute(select(User).where(func.lower(User.email) == email.strip().lower()))
    return result.scalar_one_or_none()


async def register_user(
    db: AsyncSession, *, email: str, password: str, full_name: str, role: str, organization_name: str | None
) -> User:
    if role not in (Role.ATTENDEE.value, Role.ORGANIZER.value):  # defence in depth; schema already restricts
        raise ForbiddenError("This role cannot be self-assigned")
    if await get_user_by_email(db, email):
        raise ConflictError("An account with this email already exists")

    password_hash = await run_in_threadpool(hash_password, password)
    user = User(
        email=email.strip().lower(),
        full_name=full_name.strip(),
        password_hash=password_hash,
        role=role,
        status=UserStatus.ACTIVE.value,
        organization_name=organization_name if role == Role.ORGANIZER.value else None,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:  # lost a race against a concurrent registration with the same e-mail
        await db.rollback()
        raise ConflictError("An account with this email already exists") from None
    return user


async def authenticate_user(db: AsyncSession, email: str, password: str) -> User:
    user = await get_user_by_email(db, email)
    if user is None:
        await run_in_threadpool(verify_password, DUMMY_PASSWORD_HASH, password)  # equalise timing
        raise UnauthorizedError("Invalid email or password")

    now = utcnow()
    if user.locked_until and user.locked_until > now:
        minutes = int((user.locked_until - now).total_seconds() // 60) + 1
        raise TooManyRequestsError(
            f"Too many failed login attempts. Try again in about {minutes} minute(s).",
            headers={"Retry-After": str(int((user.locked_until - now).total_seconds()) + 1)},
        )

    if not await run_in_threadpool(verify_password, user.password_hash, password):
        user.failed_login_attempts += 1
        if user.failed_login_attempts >= MAX_FAILED_LOGINS:
            user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_login_attempts = 0
        await db.commit()
        raise UnauthorizedError("Invalid email or password")

    if user.status != UserStatus.ACTIVE.value:
        raise ForbiddenError("This account has been suspended")

    if password_needs_rehash(user.password_hash):
        user.password_hash = await run_in_threadpool(hash_password, password)
    user.failed_login_attempts = 0
    user.locked_until = None
    user.last_login_at = now
    await db.commit()
    return user


async def issue_session(
    db: AsyncSession,
    user: User,
    *,
    family_id: uuid.UUID | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> tuple[SessionTokens, RefreshToken]:
    """Create a new access token + refresh token. The caller commits."""
    settings = get_settings()
    access_token, expires_in = create_access_token(user.id)
    raw_refresh = generate_opaque_token()
    expires_at = utcnow() + timedelta(days=settings.refresh_token_expire_days)
    row = RefreshToken(
        user_id=user.id,
        token_hash=hash_token(raw_refresh),
        family_id=family_id or uuid.uuid4(),
        expires_at=expires_at,
        ip_address=ip,
        user_agent=(user_agent or "")[:255] or None,
    )
    db.add(row)
    await db.flush()
    return SessionTokens(access_token, expires_in, raw_refresh, expires_at), row


async def rotate_refresh_token(
    db: AsyncSession, raw_token: str, *, ip: str | None, user_agent: str | None
) -> tuple[User, SessionTokens]:
    """Exchange a valid refresh token for a new pair; the presented token is revoked (rotation)."""
    now = utcnow()
    result = await db.execute(
        select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_token)).with_for_update()
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise UnauthorizedError("Invalid refresh token")

    if row.revoked_at is not None:
        if (now - row.revoked_at).total_seconds() > REFRESH_REUSE_GRACE_SECONDS:
            # Replay of an old token => assume theft: revoke every token in this family.
            await db.execute(
                update(RefreshToken)
                .where(RefreshToken.family_id == row.family_id, RefreshToken.revoked_at.is_(None))
                .values(revoked_at=now)
            )
            await db.commit()
        raise UnauthorizedError("Session expired. Please log in again.")

    if row.expires_at <= now:
        raise UnauthorizedError("Session expired. Please log in again.")

    user = await db.get(User, row.user_id)
    if user is None:
        raise UnauthorizedError("Invalid refresh token")
    if user.status != UserStatus.ACTIVE.value:
        raise ForbiddenError("This account has been suspended")

    tokens, new_row = await issue_session(db, user, family_id=row.family_id, ip=ip, user_agent=user_agent)
    row.revoked_at = now
    row.replaced_by_id = new_row.id
    await db.commit()
    return user, tokens


async def revoke_refresh_token(db: AsyncSession, raw_token: str | None) -> None:
    if not raw_token:
        return
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.token_hash == hash_token(raw_token), RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    await db.commit()


async def revoke_all_refresh_tokens(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Revoke every active refresh token of a user. The caller commits."""
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


async def request_password_reset(db: AsyncSession, email: str, background: BackgroundTasks) -> None:
    """Always behaves identically to the caller, whether or not the account exists."""
    user = await get_user_by_email(db, email)
    if user is None or user.status != UserStatus.ACTIVE.value:
        return
    settings = get_settings()
    now = utcnow()
    # Invalidate any earlier outstanding reset tokens.
    await db.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None))
        .values(used_at=now)
    )
    raw = generate_opaque_token()
    db.add(
        PasswordResetToken(
            user_id=user.id,
            token_hash=hash_token(raw),
            expires_at=now + timedelta(minutes=settings.password_reset_expire_minutes),
        )
    )
    await db.commit()
    link = f"{settings.password_reset_url}?token={raw}"
    background.add_task(
        send_password_reset_email, user.email, user.full_name, link, settings.password_reset_expire_minutes
    )


async def reset_password(db: AsyncSession, raw_token: str, new_password: str) -> None:
    now = utcnow()
    result = await db.execute(select(PasswordResetToken).where(PasswordResetToken.token_hash == hash_token(raw_token)))
    token = result.scalar_one_or_none()
    if token is None or token.used_at is not None or token.expires_at <= now:
        raise BadRequestError("This password reset link is invalid or has expired")

    # Atomically claim the token so it can be used exactly once, even under concurrent requests.
    claimed = await db.execute(
        update(PasswordResetToken)
        .where(PasswordResetToken.id == token.id, PasswordResetToken.used_at.is_(None))
        .values(used_at=now)
        .returning(PasswordResetToken.id)
    )
    if claimed.scalar_one_or_none() is None:
        await db.rollback()
        raise BadRequestError("This password reset link is invalid or has expired")

    user = await db.get(User, token.user_id)
    if user is None or user.status != UserStatus.ACTIVE.value:
        await db.rollback()
        raise BadRequestError("This password reset link is invalid or has expired")

    user.password_hash = await run_in_threadpool(hash_password, new_password)
    user.failed_login_attempts = 0
    user.locked_until = None
    await revoke_all_refresh_tokens(db, user.id)  # force re-login everywhere
    await db.commit()


async def change_password(
    db: AsyncSession, user: User, current_password: str, new_password: str, *, ip: str | None, user_agent: str | None
) -> SessionTokens:
    if not await run_in_threadpool(verify_password, user.password_hash, current_password):
        raise BadRequestError("Current password is incorrect")
    if current_password == new_password:
        raise BadRequestError("New password must be different from the current password")

    user.password_hash = await run_in_threadpool(hash_password, new_password)
    await revoke_all_refresh_tokens(db, user.id)
    tokens, _ = await issue_session(db, user, ip=ip, user_agent=user_agent)
    await db.commit()
    return tokens
