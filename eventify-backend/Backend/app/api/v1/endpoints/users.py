from typing import Annotated

from fastapi import APIRouter, Depends

from app.core.permissions import Permission
from app.database.connection import DbSession
from app.dependencies.authorization import require_permission
from app.models.user import User
from app.schemas.common import Envelope, ok
from app.schemas.user import UserOut, UserUpdate

router = APIRouter(prefix="/users", tags=["Users"])

ProfileUser = Annotated[User, Depends(require_permission(Permission.PROFILE_MANAGE_OWN))]


@router.get("/me", response_model=Envelope[UserOut], summary="Own profile")
async def get_my_profile(user: ProfileUser):
    return ok(UserOut.model_validate(user))


@router.patch("/me", response_model=Envelope[UserOut], summary="Update own profile (name, phone, bio, organization)")
async def update_my_profile(body: UserUpdate, user: ProfileUser, db: DbSession):
    # Only whitelisted fields exist on UserUpdate; role/status/e-mail can never be changed here.
    changes = body.model_dump(exclude_unset=True)
    if "full_name" in changes and changes["full_name"] is None:
        changes.pop("full_name")
    for name, value in changes.items():
        setattr(user, name, value)
    await db.commit()
    await db.refresh(user)
    return ok(UserOut.model_validate(user), "Profile updated successfully")
