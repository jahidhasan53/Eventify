"""Create the first administrator account.

Admins can NOT be created through the public API (registration only allows attendee/organizer, and no
endpoint changes a user's role), so this script run by someone with server/database access is the only
way to bootstrap one.

Usage (from the Backend/ directory, after `alembic upgrade head`):

    python scripts/create_admin.py --email admin@example.com --name "Site Admin"

The password is read with a hidden prompt, so it never appears in shell history or the process list.
For automation you may instead export EVENTIFY_ADMIN_PASSWORD (and unset it afterwards).
"""

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import EmailStr, TypeAdapter, ValidationError  # noqa: E402


def _read_password() -> str:
    from app.schemas.auth import validate_password_strength

    password = os.environ.get("EVENTIFY_ADMIN_PASSWORD")
    if not password:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat password: "):
            raise SystemExit("Passwords do not match.")
    try:
        validate_password_strength(password)
    except ValueError as exc:
        raise SystemExit(f"Weak password: {exc}") from None
    return password


async def _create(email: str, full_name: str, password: str) -> None:
    from app.core.security import hash_password
    from app.database.connection import SessionLocal, engine
    from app.models.enums import Role, UserStatus
    from app.models.user import User
    from app.services.audit_service import record_audit
    from app.services.auth_service import get_user_by_email

    async with SessionLocal() as db:
        if await get_user_by_email(db, email):
            raise SystemExit(f"An account with the e-mail {email} already exists. Nothing was changed.")
        user = User(
            email=email.lower(),
            full_name=full_name,
            password_hash=hash_password(password),
            role=Role.ADMIN.value,
            status=UserStatus.ACTIVE.value,
        )
        db.add(user)
        await db.flush()
        record_audit(
            db, actor=None, action="admin.create_cli", target_type="user", target_id=user.id,
            details={"email": user.email},
        )
        await db.commit()
        print(f"Administrator created: {user.email} (id {user.id})")
    await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an Eventify administrator account")
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True, help="Full name")
    args = parser.parse_args()

    try:
        email = str(TypeAdapter(EmailStr).validate_python(args.email))
    except ValidationError:
        raise SystemExit("Invalid e-mail address.") from None
    if not 2 <= len(args.name.strip()) <= 120:
        raise SystemExit("Name must be between 2 and 120 characters.")

    asyncio.run(_create(email, args.name.strip(), _read_password()))


if __name__ == "__main__":
    main()
