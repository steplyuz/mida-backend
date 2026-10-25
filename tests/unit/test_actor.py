import pytest

from app.core.actor import Actor, ActorKind


def test_system_actor_has_no_user_id() -> None:
    assert Actor.system().user_id is None
    assert Actor.system().kind is ActorKind.SYSTEM


def test_system_actor_must_not_carry_user_id() -> None:
    with pytest.raises(ValueError):
        Actor(ActorKind.SYSTEM, 5)


def test_user_and_admin_actors_require_user_id() -> None:
    with pytest.raises(ValueError):
        Actor(ActorKind.USER, None)
    with pytest.raises(ValueError):
        Actor(ActorKind.ADMIN, None)
