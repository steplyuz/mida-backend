"""Domain enums. Values are stored as lowercase strings in the database."""

from enum import StrEnum


class UserRole(StrEnum):
    BUYER = "buyer"
    SELLER = "seller"
    ADMIN = "admin"


class DealStatus(StrEnum):
    CREATED = "created"
    AWAITING_PAYMENT = "awaiting_payment"
    PAYMENT_PENDING_VERIFICATION = "payment_pending_verification"
    PAYMENT_CONFIRMED = "payment_confirmed"
    DELIVERY_IN_PROGRESS = "delivery_in_progress"
    AWAITING_BUYER_CONFIRMATION = "awaiting_buyer_confirmation"
    COMPLETED = "completed"
    DISPUTED = "disputed"
    CANCELLED = "cancelled"
    PAYMENT_FAILED = "payment_failed"
    EXPIRED = "expired"


class DealPaymentStatus(StrEnum):
    PENDING = "pending"
    REQUIRES_VERIFICATION = "requires_verification"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUND_PENDING = "refund_pending"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


class ParticipantRole(StrEnum):
    BUYER = "buyer"
    SELLER = "seller"


class TelegramGroupStatus(StrEnum):
    NOT_CREATED = "not_created"
    PENDING = "pending"
    CREATED = "created"
    FAILED = "failed"


class OutboxStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class MembershipKind(StrEnum):
    MEMBER = "member"
    INVITE = "invite"


class MessageSource(StrEnum):
    BOT_WEBHOOK = "bot_webhook"
    MTPROTO = "mtproto"


class RevisionKind(StrEnum):
    EDITED = "edited"
    DELETED = "deleted"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    REQUIRES_VERIFICATION = "requires_verification"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REFUND_PENDING = "refund_pending"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"


class DisputeReason(StrEnum):
    NOT_DELIVERED = "not_delivered"
    NOT_AS_DESCRIBED = "not_as_described"
    PAYMENT_ISSUE = "payment_issue"
    OTHER = "other"


class DisputeStatus(StrEnum):
    OPEN = "open"
    UNDER_REVIEW = "under_review"
    RESOLVED = "resolved"


class DisputeResolution(StrEnum):
    BUYER_WINS = "buyer_wins"
    SELLER_WINS = "seller_wins"


class EvidenceKind(StrEnum):
    FILE = "file"
    TELEGRAM_MESSAGE = "telegram_message"
    NOTE = "note"


class PayoutStatus(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    PAID = "paid"
    REJECTED = "rejected"


class InquiryStatus(StrEnum):
    NEW = "new"
    IN_PROGRESS = "in_progress"
    CLOSED = "closed"
