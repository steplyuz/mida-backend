"""Administrator operations: statistics, manual payments, seller payouts, users, inquiries, disputes.

Every route depends on AdminUserDep, which checks the role on each request. Each mutation writes an
audit event with the administrator as the actor.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt
from sqlalchemy import select

from app.api.deps import AdminUserDep, SessionDep
from app.models.deal import Deal
from app.models.dispute import Dispute
from app.models.enums import DisputeStatus, InquiryStatus, PaymentStatus, PayoutStatus
from app.models.inquiry import Inquiry, InquiryMessage
from app.models.payment import Payment
from app.models.payout import Payout
from app.services import stats_service
from app.services.admin_payment_service import AdminPaymentService
from app.services.inquiry_service import InquiryService
from app.services.payout_service import PayoutService
from app.services.user_admin_service import UserAdminService

router = APIRouter(prefix="/admin", tags=["admin"])


class RecordPaymentBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bank_reference: str = Field(min_length=3, max_length=128)
    amount_tiyin: StrictInt = Field(gt=0)


class ReasonBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reason: str = Field(min_length=3, max_length=500)


class BankReferenceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bank_reference: str = Field(min_length=3, max_length=128)


class ReplyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    body: str = Field(min_length=1, max_length=4000)


class StatusBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: InquiryStatus


async def _deal_public_id(session: SessionDep, deal_id: int) -> str | None:
    deal = await session.get(Deal, deal_id)
    return deal.public_id if deal else None


async def _payment_out(session: SessionDep, p: Payment) -> dict[str, Any]:
    return {
        "id": p.id,
        "deal_public_id": await _deal_public_id(session, p.deal_id),
        "provider": p.provider,
        "status": p.status.value,
        "amount_tiyin": p.amount_tiyin,
        "currency": p.currency,
        "bank_reference": p.verify_reference,
        "recorded_by": p.verify_recorded_by,
        "approved_by": p.verify_approved_by,
        "created_at": p.created_at.isoformat(),
        "verified_at": p.verified_at.isoformat() if p.verified_at else None,
    }


def _payout_out(p: Payout, deal_public_id: str | None) -> dict[str, Any]:
    return {
        "id": p.public_id,
        "deal_public_id": deal_public_id,
        "seller_user_id": p.seller_user_id,
        "amount_tiyin": p.amount_tiyin,
        "currency": p.currency,
        "status": p.status.value,
        "requested_by": p.requested_by,
        "approved_by": p.approved_by,
        "paid_by": p.paid_by,
        "bank_reference": p.bank_reference,
        "reject_reason": p.reject_reason,
        "created_at": p.created_at.isoformat(),
        "paid_at": p.paid_at.isoformat() if p.paid_at else None,
    }


def _inquiry_out(i: Inquiry) -> dict[str, Any]:
    return {
        "id": i.public_id,
        "name": i.name,
        "contact": i.contact,
        "subject": i.subject,
        "source": i.source,
        "status": i.status.value,
        "assigned_admin_id": i.assigned_admin_id,
        "created_at": i.created_at.isoformat(),
        "updated_at": i.updated_at.isoformat(),
    }


def _message_out(m: InquiryMessage) -> dict[str, Any]:
    return {
        "author_kind": m.author_kind,
        "author_user_id": m.author_user_id,
        "body": m.body,
        "created_at": m.created_at.isoformat(),
    }


# ---------- statistics ----------


@router.get("/stats", summary="Dashboard counters")
async def stats(admin: AdminUserDep, session: SessionDep) -> dict[str, Any]:
    return await stats_service.dashboard(session)


# ---------- manual payments ----------


@router.get("/payments", summary="Payments, optionally filtered by status")
async def list_payments(
    admin: AdminUserDep,
    session: SessionDep,
    status: PaymentStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    rows = await AdminPaymentService(session).list(status, limit)
    return {"data": [await _payment_out(session, p) for p in rows]}


@router.post("/payments/{payment_id}/record", summary="Maker: record a bank transfer")
async def record_payment(
    payment_id: int, body: RecordPaymentBody, admin: AdminUserDep, session: SessionDep
) -> dict[str, Any]:
    p = await AdminPaymentService(session).record(
        admin.actor, payment_id, bank_reference=body.bank_reference, amount_tiyin=body.amount_tiyin
    )
    return await _payment_out(session, p)


@router.post("/payments/{payment_id}/approve", summary="Checker: approve a recorded transfer")
async def approve_payment(payment_id: int, admin: AdminUserDep, session: SessionDep) -> dict[str, Any]:
    p = await AdminPaymentService(session).approve(admin.actor, payment_id)
    return await _payment_out(session, p)


@router.post("/payments/{payment_id}/reject", summary="Reject a payment waiting for verification")
async def reject_payment(
    payment_id: int, body: ReasonBody, admin: AdminUserDep, session: SessionDep
) -> dict[str, Any]:
    p = await AdminPaymentService(session).reject(admin.actor, payment_id, reason=body.reason)
    return await _payment_out(session, p)


# ---------- seller payouts ----------


@router.get("/payouts", summary="Seller payout ledger")
async def list_payouts(
    admin: AdminUserDep,
    session: SessionDep,
    status: PayoutStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    rows = await PayoutService(session).list(status, limit)
    return {"data": [_payout_out(p, await _deal_public_id(session, p.deal_id)) for p in rows]}


@router.post("/deals/{public_id}/payout", status_code=201, summary="Request a payout for a completed deal")
async def request_payout(public_id: str, admin: AdminUserDep, session: SessionDep) -> dict[str, Any]:
    p = await PayoutService(session).request(admin.actor, public_id)
    return _payout_out(p, public_id)


@router.post("/payouts/{public_id}/approve", summary="Second administrator approves a payout")
async def approve_payout(public_id: str, admin: AdminUserDep, session: SessionDep) -> dict[str, Any]:
    p = await PayoutService(session).approve(admin.actor, public_id)
    return _payout_out(p, await _deal_public_id(session, p.deal_id))


@router.post("/payouts/{public_id}/reject", summary="Reject a requested payout")
async def reject_payout(
    public_id: str, body: ReasonBody, admin: AdminUserDep, session: SessionDep
) -> dict[str, Any]:
    p = await PayoutService(session).reject(admin.actor, public_id, reason=body.reason)
    return _payout_out(p, await _deal_public_id(session, p.deal_id))


@router.post("/payouts/{public_id}/paid", summary="Record that the bank transfer was made")
async def mark_payout_paid(
    public_id: str, body: BankReferenceBody, admin: AdminUserDep, session: SessionDep
) -> dict[str, Any]:
    p = await PayoutService(session).mark_paid(admin.actor, public_id, bank_reference=body.bank_reference)
    return _payout_out(p, await _deal_public_id(session, p.deal_id))


# ---------- users ----------


@router.get("/users", summary="Search users")
async def list_users(
    admin: AdminUserDep,
    session: SessionDep,
    q: str | None = Query(None, max_length=64),
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> dict[str, Any]:
    rows = await UserAdminService(session).search(q, limit, offset)
    return {
        "data": [
            {
                "id": u.id,
                "first_name": u.first_name,
                "telegram_username": u.telegram_username,
                "telegram_user_id": u.telegram_user_id,
                "role": u.role.value,
                "is_active": u.is_active,
                "last_seen_at": u.last_seen_at.isoformat() if u.last_seen_at else None,
            }
            for u in rows
        ]
    }


@router.post("/users/{user_id}/block", summary="Block a user (their tokens stop working at once)")
async def block_user(user_id: int, admin: AdminUserDep, session: SessionDep) -> dict[str, Any]:
    u = await UserAdminService(session).set_active(admin.actor, user_id, active=False)
    return {"id": u.id, "is_active": u.is_active}


@router.post("/users/{user_id}/unblock", summary="Unblock a user")
async def unblock_user(user_id: int, admin: AdminUserDep, session: SessionDep) -> dict[str, Any]:
    u = await UserAdminService(session).set_active(admin.actor, user_id, active=True)
    return {"id": u.id, "is_active": u.is_active}


# ---------- inquiries ----------


@router.get("/inquiries", summary="Inquiry inbox")
async def list_inquiries(
    admin: AdminUserDep,
    session: SessionDep,
    status: InquiryStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    rows = await InquiryService(session).admin_list(status, limit)
    return {"data": [_inquiry_out(i) for i in rows]}


@router.get("/inquiries/{public_id}", summary="One inquiry with its thread")
async def get_inquiry(public_id: str, admin: AdminUserDep, session: SessionDep) -> dict[str, Any]:
    service = InquiryService(session)
    inquiry = await service.admin_get(public_id)
    return {**_inquiry_out(inquiry), "messages": [_message_out(m) for m in await service.messages(inquiry)]}


@router.post("/inquiries/{public_id}/messages", status_code=201, summary="Reply to an inquiry")
async def reply_inquiry(
    public_id: str, body: ReplyBody, admin: AdminUserDep, session: SessionDep
) -> dict[str, Any]:
    message = await InquiryService(session).admin_reply(admin.actor, public_id, body.body)
    return _message_out(message)


@router.post("/inquiries/{public_id}/status", summary="Move an inquiry to in_progress or closed")
async def set_inquiry_status(
    public_id: str, body: StatusBody, admin: AdminUserDep, session: SessionDep
) -> dict[str, Any]:
    inquiry = await InquiryService(session).admin_set_status(admin.actor, public_id, body.status)
    return _inquiry_out(inquiry)


# ---------- disputes ----------


@router.get("/disputes", summary="Disputes across all deals")
async def list_disputes(
    admin: AdminUserDep,
    session: SessionDep,
    status: DisputeStatus | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    stmt = select(Dispute).order_by(Dispute.id.desc()).limit(limit)
    if status is not None:
        stmt = stmt.where(Dispute.status == status)
    rows = (await session.execute(stmt)).scalars().all()
    out = []
    for d in rows:
        deal = await session.get(Deal, d.deal_id)
        out.append(
            {
                "id": d.public_id,
                "deal_public_id": deal.public_id if deal else None,
                "reason_code": d.reason_code.value,
                "status": d.status.value,
                "assigned_admin_id": d.assigned_admin_id,
                "resolution": d.resolution_code.value if d.resolution_code else None,
                "created_at": d.created_at.isoformat(),
            }
        )
    return {"data": out}
