"""Authorization dependencies: role and permission gates evaluated on the server for every request."""

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends

from app.core.exceptions import ForbiddenError
from app.core.permissions import Permission, has_permission
from app.dependencies.authentication import get_current_user
from app.models.enums import Role
from app.models.user import User


def require_roles(*roles: Role) -> Callable[..., Awaitable[User]]:
    allowed = {role.value for role in roles}

    async def checker(user: Annotated[User, Depends(get_current_user)]) -> User:
        if user.role not in allowed:
            raise ForbiddenError("You do not have permission to perform this action")
        return user

    return checker


def require_permission(permission: Permission) -> Callable[..., Awaitable[User]]:
    async def checker(user: Annotated[User, Depends(get_current_user)]) -> User:
        if not has_permission(user.role, permission):
            raise ForbiddenError("You do not have permission to perform this action")
        return user

    return checker


# Ready-made gates used by the routers
AdminUser = Annotated[User, Depends(require_roles(Role.ADMIN))]
OrganizerUser = Annotated[User, Depends(require_roles(Role.ORGANIZER))]
