from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import UserRole


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: int
    mida_id: int | None = None
    # The user's own Telegram numeric id (read from users.telegram_user_id). Shown on their profile.
    telegram_id: int = Field(validation_alias="telegram_user_id")
    first_name: str
    last_name: str | None
    telegram_username: str | None
    photo_url: str | None = None
    phone: str | None
    role: UserRole


class UserUpdate(BaseModel):
    """Only self-editable profile fields. Role, activity and Telegram identity are not here."""

    model_config = ConfigDict(extra="forbid")

    last_name: str | None = Field(default=None, max_length=128)
    phone: str | None = Field(default=None, pattern=r"^\+?[0-9]{7,15}$")
