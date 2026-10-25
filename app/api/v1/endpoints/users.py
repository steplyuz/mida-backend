from __future__ import annotations

from fastapi import APIRouter

from app.api.deps import CurrentUserDep, SessionDep
from app.core.time import utcnow
from app.models.user import User
from app.schemas.user import UserPublic, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/me", response_model=UserPublic)
async def get_me(current: CurrentUserDep) -> UserPublic:
    return UserPublic.model_validate(current.user)


@router.patch("/me", response_model=UserPublic)
async def update_me(body: UserUpdate, current: CurrentUserDep, session: SessionDep) -> UserPublic:
    user = await session.get(User, current.user.id)
    if user is None:
        return UserPublic.model_validate(current.user)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    user.updated_at = utcnow()
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return UserPublic.model_validate(user)
