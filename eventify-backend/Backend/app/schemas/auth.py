import re
from typing import Annotated, Literal

from pydantic import EmailStr, Field, StringConstraints, field_validator

from app.schemas.common import SecretModel, StrictModel
from app.schemas.user import UserOut

_COMMON_PASSWORDS = {
    "password1234", "password12345", "123456789012", "qwertyuiop12", "letmein12345", "welcome12345",
}


def validate_password_strength(value: str) -> str:
    """>= 10 chars, upper + lower + digit + symbol, not a trivially common password."""
    if len(value) < 10:
        raise ValueError("Password must be at least 10 characters long")
    if len(value) > 128:
        raise ValueError("Password must be at most 128 characters long")
    if not re.search(r"[a-z]", value):
        raise ValueError("Password must contain a lowercase letter")
    if not re.search(r"[A-Z]", value):
        raise ValueError("Password must contain an uppercase letter")
    if not re.search(r"\d", value):
        raise ValueError("Password must contain a digit")
    if not re.search(r"[^A-Za-z0-9]", value):
        raise ValueError("Password must contain a symbol")
    if value.lower() in _COMMON_PASSWORDS:
        raise ValueError("Password is too common")
    return value


class RegisterRequest(SecretModel):
    email: EmailStr
    password: str
    full_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=2, max_length=120)]
    # 'admin' is not accepted here — admin accounts are created only through the CLI script.
    role: Literal["attendee", "organizer"] = "attendee"
    organization_name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=150)] | None = None

    _check_password = field_validator("password")(validate_password_strength)


class LoginRequest(SecretModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class LoginOut(StrictModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    # Also delivered as the readable `csrf_token` cookie. Returned in the body for deployments where
    # the frontend and API are on different sites and cannot read each other's cookies.
    csrf_token: str
    user: UserOut


class RefreshOut(StrictModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    csrf_token: str


class ForgotPasswordRequest(StrictModel):
    email: EmailStr


class ResetPasswordRequest(SecretModel):
    token: str = Field(min_length=20, max_length=200)
    new_password: str

    _check_password = field_validator("new_password")(validate_password_strength)


class ChangePasswordRequest(SecretModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str

    _check_password = field_validator("new_password")(validate_password_strength)
