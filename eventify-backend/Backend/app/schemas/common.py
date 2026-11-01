"""Response envelope and pagination shapes shared by all endpoints."""

import math
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict

T = TypeVar("T")


class StrictModel(BaseModel):
    """Request bodies reject unknown fields, which blocks mass-assignment
    (e.g. a client smuggling in `role`, `organizer_id` or `status`)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SecretModel(StrictModel):
    """For bodies containing passwords: whitespace must never be silently stripped from them."""

    model_config = ConfigDict(str_strip_whitespace=False)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Envelope(BaseModel, Generic[T]):
    success: bool = True
    message: str = "Success"
    data: T | None = None


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int
    pages: int


def ok(data: Any = None, message: str = "Success") -> dict[str, Any]:
    return {"success": True, "message": message, "data": data}


def paginated(items: list[Any], total: int, page: int, page_size: int) -> dict[str, Any]:
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "pages": math.ceil(total / page_size) if total else 0,
    }
