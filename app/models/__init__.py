"""ORM models. Every model module must be imported here so Alembic sees Base.metadata."""

from app.models.audit import AuditLog
from app.models.counter import Counter
from app.models.deal import Deal, DealParticipant
from app.models.dispute import Dispute
from app.models.evidence import Evidence
from app.models.idempotency import IdempotencyRecord
from app.models.inquiry import Inquiry, InquiryMessage
from app.models.message import DealMessage, MessageRevision, TelegramUpdateReceipt
from app.models.notification import Notification
from app.models.outbox import OutboxEvent
from app.models.payment import Payment, PaymentWebhookEvent
from app.models.payout import Payout
from app.models.telegram import TelegramMembership
from app.models.user import User

__all__ = [
    "AuditLog",
    "Counter",
    "Dispute",
    "Evidence",
    "Deal",
    "DealParticipant",
    "DealMessage",
    "IdempotencyRecord",
    "Inquiry",
    "InquiryMessage",
    "Payout",
    "Notification",
    "MessageRevision",
    "TelegramUpdateReceipt",
    "OutboxEvent",
    "Payment",
    "PaymentWebhookEvent",
    "TelegramMembership",
    "User",
]
