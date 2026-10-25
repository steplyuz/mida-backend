"""Payment endpoints. Deal-scoped routes live under /deals; provider callbacks under /payments."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response

from app.api.deps import (
    CurrentUserDep,
    DealServiceDep,
    IdempotencyKeyHeader,
    SessionDep,
    enforce_rate_limit,
)
from app.core.config import Settings
from app.core.domain_errors import NotFoundError
from app.integrations.payments.base import PaymentProvider
from app.models.enums import DealPaymentStatus
from app.schemas.payment import PaymentList, PaymentPublic, PaymentStatusPublic
from app.services.idempotency_service import validate_key
from app.services.payment_service import PaymentService

router = APIRouter(prefix="/deals", tags=["payments"])
webhook_router = APIRouter(prefix="/payments", tags=["payments"])
logger = logging.getLogger("app.payments")
REPLAY_HEADER = "Idempotent-Replayed"


def _provider(request: Request) -> PaymentProvider | None:
    provider: PaymentProvider | None = request.app.state.payment_provider
    return provider


@router.post(
    "/{public_id}/payments",
    status_code=201,
    response_model=PaymentPublic,
    summary="Buyer starts a payment for the deal",
)
async def start_payment(
    public_id: str,
    request: Request,
    response: Response,
    current: CurrentUserDep,
    service: DealServiceDep,
    session: SessionDep,
    idempotency_key: IdempotencyKeyHeader = None,
) -> PaymentPublic:
    key = validate_key(idempotency_key)
    settings: Settings = request.app.state.settings
    enforce_rate_limit(
        request,
        f"payment-start:{current.user.id}",
        limit=settings.rate_limit_payment_start_per_hour,
        window_seconds=3600,
    )
    deal = await service.get_deal_by_public_id(current.actor, public_id)
    payment, replayed = await PaymentService(session, _provider(request)).start_payment(
        current.actor, deal.id, key
    )
    if replayed:
        response.headers[REPLAY_HEADER] = "true"
    return PaymentPublic.build(payment, is_buyer=True)


@router.get("/{public_id}/payments", response_model=PaymentList, summary="Payment attempts for the deal")
async def list_payments(
    public_id: str,
    request: Request,
    current: CurrentUserDep,
    service: DealServiceDep,
    session: SessionDep,
) -> PaymentList:
    deal = await service.get_deal_by_public_id(current.actor, public_id)
    role = await service.viewer_role(current.actor, deal)
    rows = await PaymentService(session, _provider(request)).list_for_deal(deal.id)
    return PaymentList(data=[PaymentPublic.build(row, is_buyer=role == "buyer") for row in rows])


@router.get(
    "/{public_id}/payment-status",
    response_model=PaymentStatusPublic,
    summary="Current payment state of the deal",
)
async def payment_status(
    public_id: str,
    request: Request,
    current: CurrentUserDep,
    service: DealServiceDep,
    session: SessionDep,
) -> PaymentStatusPublic:
    deal = await service.get_deal_by_public_id(current.actor, public_id)
    rows = await PaymentService(session, _provider(request)).list_for_deal(deal.id)
    latest = rows[-1].status if rows else None
    deal_status: DealPaymentStatus = deal.payment_status
    return PaymentStatusPublic(deal_payment_status=deal_status, latest_payment_status=latest)


@webhook_router.post("/webhook/{provider}", summary="Payment provider callback")
async def receive_event(provider: str, request: Request, session: SessionDep) -> dict[str, str]:
    adapter = _provider(request)
    if adapter is None or adapter.name != provider:
        raise NotFoundError("Unknown payment provider.")
    body = await request.body()
    headers = {name.lower(): value for name, value in request.headers.items()}
    # Signature first: no field of the body is trusted until verify_webhook accepts it.
    event = adapter.verify_webhook(headers=headers, body=body)
    outcome = await PaymentService(session, adapter).apply_event(event)
    logger.info("payment event processed", extra={"operation": "payment_webhook", "result": outcome})
    return {"status": outcome}
