import pytest

from app.core.domain_errors import InvalidTransitionError, PermissionDeniedError
from app.models.enums import DealStatus as S
from app.services.state_machine import (
    TERMINAL_STATUSES,
    TRANSITIONS,
    allowed_targets,
    check_transition,
)
from app.services.state_machine import (
    DealRole as R,
)


@pytest.mark.parametrize(("edge", "roles"), list(TRANSITIONS.items()))
def test_every_declared_edge_is_allowed_for_its_roles(edge: tuple[S, S], roles: frozenset[R]) -> None:
    source, target = edge
    for role in roles:
        check_transition(source, target, role)  # must not raise


@pytest.mark.parametrize(("edge", "roles"), list(TRANSITIONS.items()))
def test_every_declared_edge_is_forbidden_for_other_roles(edge: tuple[S, S], roles: frozenset[R]) -> None:
    source, target = edge
    for role in set(R) - roles:
        with pytest.raises(PermissionDeniedError):
            check_transition(source, target, role)


def test_undeclared_edges_are_rejected_for_everyone() -> None:
    assert (S.CREATED, S.COMPLETED) not in TRANSITIONS
    for role in R:
        with pytest.raises(InvalidTransitionError):
            check_transition(S.CREATED, S.COMPLETED, role)


def test_buyer_cannot_mark_payment_confirmed() -> None:
    with pytest.raises(PermissionDeniedError):
        check_transition(S.PAYMENT_PENDING_VERIFICATION, S.PAYMENT_CONFIRMED, R.BUYER)


def test_seller_cannot_complete_the_deal() -> None:
    with pytest.raises(PermissionDeniedError):
        check_transition(S.AWAITING_BUYER_CONFIRMATION, S.COMPLETED, R.SELLER)


def test_payment_confirmed_cannot_be_cancelled_directly() -> None:
    # Cancelling after payment needs a refund flow; not a plain status change.
    with pytest.raises(InvalidTransitionError):
        check_transition(S.PAYMENT_CONFIRMED, S.CANCELLED, R.ADMIN)


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
def test_terminal_statuses_have_no_exits(status: S) -> None:
    for role in R:
        assert allowed_targets(status, role) == frozenset()


def test_dispute_exit_is_reserved_for_admin() -> None:
    for source, target in ((S.DISPUTED, S.COMPLETED), (S.DISPUTED, S.CANCELLED)):
        assert TRANSITIONS[(source, target)] == frozenset({R.ADMIN})


def test_no_self_transitions() -> None:
    assert all(source is not target for source, target in TRANSITIONS)


def test_allowed_targets_for_buyer_on_new_deal() -> None:
    assert allowed_targets(S.CREATED, R.BUYER) == frozenset({S.AWAITING_PAYMENT, S.CANCELLED})
