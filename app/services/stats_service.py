"""Operational statistics for the admin dashboard. Counts and sums only; no personal data."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.deal import Deal
from app.models.dispute import Dispute
from app.models.enums import (
    DealStatus,
    DisputeStatus,
    InquiryStatus,
    OutboxStatus,
    PaymentStatus,
    PayoutStatus,
)
from app.models.inquiry import Inquiry
from app.models.outbox import OutboxEvent
from app.models.payment import Payment
from app.models.payout import Payout
from app.models.user import User


async def dashboard(session: AsyncSession) -> dict[str, Any]:
    async def count(stmt: Any) -> int:
        return int(await session.scalar(stmt) or 0)

    by_status = dict((await session.execute(select(Deal.status, func.count()).group_by(Deal.status))).all())
    completed = await session.execute(
        select(
            func.coalesce(func.sum(Deal.total_amount_tiyin), 0),
            func.coalesce(func.sum(Deal.service_fee_tiyin), 0),
        ).where(Deal.status == DealStatus.COMPLETED)
    )
    volume, fees = completed.one()
    return {
        "users": {
            "total": await count(select(func.count()).select_from(User)),
            "blocked": await count(select(func.count()).select_from(User).where(User.is_active.is_(False))),
        },
        "deals": {
            "by_status": {getattr(k, "value", str(k)): int(v) for k, v in by_status.items()},
            "completed_volume_tiyin": int(volume),
            "fees_earned_tiyin": int(fees),
        },
        "payments": {
            "awaiting_verification": await count(
                select(func.count())
                .select_from(Payment)
                .where(Payment.status == PaymentStatus.REQUIRES_VERIFICATION)
            ),
            "confirmed": await count(
                select(func.count()).select_from(Payment).where(Payment.status == PaymentStatus.CONFIRMED)
            ),
        },
        "payouts": {
            "awaiting_action": await count(
                select(func.count())
                .select_from(Payout)
                .where(Payout.status.in_([PayoutStatus.REQUESTED, PayoutStatus.APPROVED]))
            ),
            "paid": await count(
                select(func.count()).select_from(Payout).where(Payout.status == PayoutStatus.PAID)
            ),
        },
        "disputes": {
            "open": await count(
                select(func.count()).select_from(Dispute).where(Dispute.status != DisputeStatus.RESOLVED)
            ),
        },
        "inquiries": {
            "new": await count(
                select(func.count()).select_from(Inquiry).where(Inquiry.status == InquiryStatus.NEW)
            ),
        },
        "integrations": {
            "outbox_failed": await count(
                select(func.count()).select_from(OutboxEvent).where(OutboxEvent.status == OutboxStatus.FAILED)
            ),
        },
    }
