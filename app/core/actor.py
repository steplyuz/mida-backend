"""Who performs an operation. Phase 3 authentication produces these; services consume them."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ActorKind(StrEnum):
    USER = "user"
    ADMIN = "admin"
    SYSTEM = "system"


@dataclass(frozen=True, slots=True)
class Actor:
    kind: ActorKind
    user_id: int | None = None

    def __post_init__(self) -> None:
        if self.kind is ActorKind.SYSTEM and self.user_id is not None:
            raise ValueError("system actor must not carry a user_id")
        if self.kind is not ActorKind.SYSTEM and self.user_id is None:
            raise ValueError(f"{self.kind.value} actor requires a user_id")

    @classmethod
    def user(cls, user_id: int) -> Actor:
        return cls(ActorKind.USER, user_id)

    @classmethod
    def admin(cls, user_id: int) -> Actor:
        return cls(ActorKind.ADMIN, user_id)

    @classmethod
    def system(cls) -> Actor:
        return cls(ActorKind.SYSTEM, None)
