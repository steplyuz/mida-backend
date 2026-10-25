"""Deal state machine. Pure logic: graph plus who may trigger each edge. No I/O."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from app.core.domain_errors import InvalidTransitionError, PermissionDeniedError
from app.models.enums import DealStatus


class DealRole(StrEnum):
    BUYER = "buyer"
    SELLER = "seller"
    ADMIN = "admin"
    SYSTEM = "system"


S = DealStatus
R = DealRole

# (from, to) -> roles allowed to trigger this edge. Anything absent is forbidden.
TRANSITIONS: Mapping[tuple[DealStatus, DealStatus], frozenset[DealRole]] = {
    (S.CREATED, S.AWAITING_PAYMENT): frozenset({R.BUYER, R.SYSTEM}),
    (S.CREATED, S.CANCELLED): frozenset({R.BUYER, R.SELLER, R.ADMIN}),
    (S.AWAITING_PAYMENT, S.PAYMENT_PENDING_VERIFICATION): frozenset({R.SYSTEM}),
    (S.AWAITING_PAYMENT, S.CANCELLED): frozenset({R.BUYER, R.SELLER, R.ADMIN}),
    (S.AWAITING_PAYMENT, S.EXPIRED): frozenset({R.SYSTEM, R.ADMIN}),
    (S.PAYMENT_PENDING_VERIFICATION, S.PAYMENT_CONFIRMED): frozenset({R.SYSTEM}),
    (S.PAYMENT_PENDING_VERIFICATION, S.PAYMENT_FAILED): frozenset({R.SYSTEM}),
    (S.PAYMENT_FAILED, S.AWAITING_PAYMENT): frozenset({R.BUYER, R.SYSTEM}),
    (S.PAYMENT_FAILED, S.CANCELLED): frozenset({R.BUYER, R.SELLER, R.ADMIN}),
    (S.PAYMENT_CONFIRMED, S.DELIVERY_IN_PROGRESS): frozenset({R.SELLER, R.ADMIN}),
    (S.PAYMENT_CONFIRMED, S.DISPUTED): frozenset({R.BUYER, R.SELLER, R.ADMIN}),
    (S.DELIVERY_IN_PROGRESS, S.AWAITING_BUYER_CONFIRMATION): frozenset({R.SELLER, R.ADMIN}),
    (S.DELIVERY_IN_PROGRESS, S.DISPUTED): frozenset({R.BUYER, R.SELLER, R.ADMIN}),
    (S.AWAITING_BUYER_CONFIRMATION, S.COMPLETED): frozenset({R.BUYER}),
    (S.AWAITING_BUYER_CONFIRMATION, S.DISPUTED): frozenset({R.BUYER, R.SELLER, R.ADMIN}),
    # Dispute outcomes are reserved for the admin decision flow (Phase 8).
    (S.DISPUTED, S.COMPLETED): frozenset({R.ADMIN}),
    (S.DISPUTED, S.CANCELLED): frozenset({R.ADMIN}),
}

TERMINAL_STATUSES: frozenset[DealStatus] = frozenset({S.COMPLETED, S.CANCELLED, S.EXPIRED})


def allowed_targets(current: DealStatus, role: DealRole) -> frozenset[DealStatus]:
    return frozenset(
        target for (source, target), roles in TRANSITIONS.items() if source is current and role in roles
    )


def check_transition(current: DealStatus, target: DealStatus, role: DealRole) -> None:
    roles = TRANSITIONS.get((current, target))
    if roles is None:
        raise InvalidTransitionError(f"Transition from '{current.value}' to '{target.value}' is not allowed.")
    if role not in roles:
        raise PermissionDeniedError("You are not allowed to perform this status change.")
