from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.user import UserPublic


class TelegramLoginRequest(BaseModel):
    # extra="forbid": a client cannot smuggle telegram_user_id, role or any other field.
    model_config = ConfigDict(extra="forbid")

    init_data: str = Field(min_length=1, max_length=4096)


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105  (OAuth token type label)
    expires_in: int
    user: UserPublic
