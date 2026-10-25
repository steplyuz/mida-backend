"""Evidence: files (validated, stored under server keys), Telegram message references, and notes.

Evidence can be added while a deal is active or while its dispute is open. Once a deal is closed
with no dispute open, its evidence is frozen.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actor import Actor, ActorKind
from app.core.domain_errors import (
    InvalidTransitionError,
    InvariantViolationError,
    NotFoundError,
    ValidationDomainError,
)
from app.core.time import utcnow
from app.integrations.storage.base import EvidenceStorage
from app.models.deal import Deal
from app.models.dispute import Dispute
from app.models.enums import DealStatus, DisputeStatus, EvidenceKind
from app.models.evidence import Evidence
from app.models.message import DealMessage
from app.repositories.deal_repository import DealRepository
from app.services.audit_service import AuditService
from app.services.evidence_validation import clean_filename, validate_file

logger = logging.getLogger("app.evidence")
FILE_LABELS = frozenset({"photo", "document", "delivery_info", "other"})
TERMINAL = frozenset({DealStatus.COMPLETED, DealStatus.CANCELLED, DealStatus.EXPIRED})
MAX_NOTE = 2000
Label = Literal["photo", "document", "delivery_info", "other"]


class EvidenceService:
    def __init__(self, session: AsyncSession, storage: EvidenceStorage, *, max_bytes: int) -> None:
        self._session = session
        self._storage = storage
        self._max_bytes = max_bytes
        self._deals = DealRepository(session)
        self._audit = AuditService(session)

    # ---------- access ----------

    async def _require_access(self, actor: Actor, deal: Deal) -> None:
        if actor.kind is ActorKind.ADMIN:
            return
        if actor.kind is not ActorKind.USER or actor.user_id is None:
            raise NotFoundError("Deal not found.")
        if await self._deals.get_participant_role(deal.id, actor.user_id) is None:
            raise NotFoundError("Deal not found.")

    async def _open_dispute(self, deal_id: int) -> Dispute | None:
        return (
            await self._session.execute(
                select(Dispute).where(Dispute.deal_id == deal_id, Dispute.status != DisputeStatus.RESOLVED)
            )
        ).scalar_one_or_none()

    async def _require_writable(self, deal: Deal) -> Dispute | None:
        dispute = await self._open_dispute(deal.id)
        if deal.status in TERMINAL and dispute is None:
            raise InvalidTransitionError("Evidence can be added only to an active deal or an open dispute.")
        return dispute

    def _writer(self, actor: Actor) -> int:
        if actor.kind not in (ActorKind.USER, ActorKind.ADMIN) or actor.user_id is None:
            raise NotFoundError("Deal not found.")
        return actor.user_id

    # ---------- writes ----------

    async def upload_file(
        self,
        actor: Actor,
        deal_id: int,
        *,
        label: str,
        filename: str | None,
        declared_type: str,
        data: bytes,
    ) -> Evidence:
        if label not in FILE_LABELS:
            raise ValidationDomainError("Evidence label must be photo, document, delivery_info or other.")
        writer = self._writer(actor)
        mime = validate_file(declared_type, data, self._max_bytes)
        digest = hashlib.sha256(data).hexdigest()
        name = clean_filename(filename)

        deal = await self._require_deal(deal_id)
        await self._require_access(actor, deal)
        dispute = await self._require_writable(deal)

        key = await asyncio.to_thread(self._storage.put, data)
        try:
            evidence = Evidence(
                public_id=str(uuid.uuid4()),
                deal_id=deal.id,
                dispute_id=dispute.id if dispute else None,
                uploaded_by=writer,
                kind=EvidenceKind.FILE,
                label=label,
                storage_key=key,
                original_filename=name,
                mime_type=mime,
                file_size=len(data),
                sha256=digest,
                created_at=utcnow(),
            )
            self._session.add(evidence)
            await self._session.flush()
            self._audit.record(
                actor=actor,
                action="evidence.uploaded",
                entity_type="evidence",
                entity_id=evidence.id,
                deal_id=deal.id,
                new={"label": label, "mime": mime, "size": len(data), "sha256": digest},
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            # Bytes were written before the row; remove them so no orphan file survives a failure.
            await asyncio.to_thread(self._safe_delete, key)
            raise
        return evidence

    async def add_note(self, actor: Actor, deal_id: int, text: str) -> Evidence:
        writer = self._writer(actor)
        cleaned = text.strip()
        if not 1 <= len(cleaned) <= MAX_NOTE:
            raise ValidationDomainError(f"Note must be 1 to {MAX_NOTE} characters.")
        deal = await self._require_deal(deal_id)
        await self._require_access(actor, deal)
        dispute = await self._require_writable(deal)
        try:
            evidence = Evidence(
                public_id=str(uuid.uuid4()),
                deal_id=deal.id,
                dispute_id=dispute.id if dispute else None,
                uploaded_by=writer,
                kind=EvidenceKind.NOTE,
                label="note",
                note_text=cleaned,
                created_at=utcnow(),
            )
            self._session.add(evidence)
            await self._session.flush()
            self._audit.record(
                actor=actor,
                action="evidence.noted",
                entity_type="evidence",
                entity_id=evidence.id,
                deal_id=deal.id,
                new={"length": len(cleaned)},
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        return evidence

    async def add_message_reference(self, actor: Actor, deal_id: int, message_id: int) -> Evidence:
        writer = self._writer(actor)
        deal = await self._require_deal(deal_id)
        await self._require_access(actor, deal)
        dispute = await self._require_writable(deal)
        message = await self._session.get(DealMessage, message_id)
        if message is None or message.deal_id != deal.id:
            raise NotFoundError("Message not found in this deal.")
        try:
            evidence = Evidence(
                public_id=str(uuid.uuid4()),
                deal_id=deal.id,
                dispute_id=dispute.id if dispute else None,
                uploaded_by=writer,
                kind=EvidenceKind.TELEGRAM_MESSAGE,
                label="telegram_message",
                message_ref_id=message.id,
                created_at=utcnow(),
            )
            self._session.add(evidence)
            await self._session.flush()
            self._audit.record(
                actor=actor,
                action="evidence.message_referenced",
                entity_type="evidence",
                entity_id=evidence.id,
                deal_id=deal.id,
                new={"message_id": message.id},
            )
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        return evidence

    # ---------- reads ----------

    async def list_for_deal(self, actor: Actor, deal_id: int) -> list[Evidence]:
        deal = await self._require_deal(deal_id)
        await self._require_access(actor, deal)
        rows = await self._session.execute(
            select(Evidence).where(Evidence.deal_id == deal.id).order_by(Evidence.id)
        )
        return list(rows.scalars().all())

    async def read_file(self, actor: Actor, public_id: str) -> tuple[Evidence, bytes]:
        evidence = (
            await self._session.execute(select(Evidence).where(Evidence.public_id == public_id))
        ).scalar_one_or_none()
        if evidence is None or evidence.kind is not EvidenceKind.FILE or evidence.storage_key is None:
            raise NotFoundError("Evidence not found.")
        deal = await self._require_deal(evidence.deal_id)
        await self._require_access(actor, deal)
        data = await asyncio.to_thread(self._storage.get, evidence.storage_key)
        if hashlib.sha256(data).hexdigest() != evidence.sha256:
            logger.error(
                "evidence integrity check failed",
                extra={"operation": "evidence_read", "result": "integrity_failed"},
            )
            raise InvariantViolationError("The stored file failed its integrity check.")
        return evidence, data

    # ---------- internals ----------

    async def _require_deal(self, deal_id: int) -> Deal:
        deal = await self._deals.get_by_id(deal_id)
        if deal is None:
            raise NotFoundError("Deal not found.")
        return deal

    def _safe_delete(self, key: str) -> None:
        try:
            self._storage.delete(key)
        except Exception:
            logger.exception(
                "could not remove orphaned evidence file", extra={"operation": "evidence_cleanup"}
            )
