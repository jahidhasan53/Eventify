import uuid
from datetime import datetime
from typing import Literal

from pydantic import EmailStr, Field

from app.schemas.common import ORMModel, StrictModel


class UserOut(ORMModel):
    """Public representation of an account. Never includes password hashes or token data."""

    id: uuid.UUID
    email: EmailStr
    full_name: str
    role: str
    status: str
    phone: str | None = None
    bio: str | None = None
    organization_name: str | None = None
    created_at: datetime
    last_login_at: datetime | None = None


class UserBrief(ORMModel):
    id: uuid.UUID
    full_name: str


class OrganizerBrief(ORMModel):
    id: uuid.UUID
    full_name: str
    organization_name: str | None = None


class UserUpdate(StrictModel):
    """Self-service profile changes. Role, status and e-mail cannot be changed here."""

    full_name: str | None = Field(default=None, min_length=2, max_length=120)
    phone: str | None = Field(default=None, max_length=30, pattern=r"^[0-9+()\-\s]*$")
    bio: str | None = Field(default=None, max_length=1000)
    organization_name: str | None = Field(default=None, max_length=150)


class AdminUserStatusUpdate(StrictModel):
    status: Literal["active", "suspended"]
    reason: str | None = Field(default=None, max_length=500)
