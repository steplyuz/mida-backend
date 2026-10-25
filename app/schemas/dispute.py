from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, StrictInt

from app.models.dispute import Dispute
from app.models.enums import DisputeReason
from app.models.evidence import Evidence


class DisputeOpenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason_code: DisputeReason
    description: str = Field(min_length=1, max_length=2000)
    expected_version: StrictInt = Field(ge=1)


class DisputeAssignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: StrictInt = Field(ge=1)


class DisputeResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution: str = Field(pattern="^(buyer_wins|seller_wins)$")
    reason: str = Field(min_length=20, max_length=2000)
    expected_version: StrictInt = Field(ge=1)


class DisputePublic(BaseModel):
    id: str
    deal_id: str
    reason_code: str
    description: str
    status: str
    opened_by_user_id: int
    assigned_admin_user_id: int | None
    resolution: str | None
    resolution_reason: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None

    @classmethod
    def build(cls, dispute: Dispute, deal_public_id: str) -> DisputePublic:
        return cls(
            id=dispute.public_id,
            deal_id=deal_public_id,
            reason_code=dispute.reason_code.value,
            description=dispute.description,
            status=dispute.status.value,
            opened_by_user_id=dispute.opened_by,
            assigned_admin_user_id=dispute.assigned_admin_id,
            resolution=dispute.resolution_code.value if dispute.resolution_code else None,
            resolution_reason=dispute.resolution_reason,
            version=dispute.version,
            created_at=dispute.created_at,
            updated_at=dispute.updated_at,
            resolved_at=dispute.resolved_at,
        )


class DisputePage(BaseModel):
    data: list[DisputePublic]


class NoteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=2000)


class MessageReferenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: StrictInt = Field(gt=0)


class EvidencePublic(BaseModel):
    id: str
    deal_id: str
    kind: str
    label: str
    original_filename: str | None
    mime_type: str | None
    file_size: int | None
    sha256: str | None
    note_text: str | None
    message_id: int | None
    uploaded_by_user_id: int
    created_at: datetime

    @classmethod
    def build(cls, evidence: Evidence, deal_public_id: str) -> EvidencePublic:
        return cls(
            id=evidence.public_id,
            deal_id=deal_public_id,
            kind=evidence.kind.value,
            label=evidence.label,
            original_filename=evidence.original_filename,
            mime_type=evidence.mime_type,
            file_size=evidence.file_size,
            sha256=evidence.sha256,
            note_text=evidence.note_text,
            message_id=evidence.message_ref_id,
            uploaded_by_user_id=evidence.uploaded_by,
            created_at=evidence.created_at,
        )


class EvidenceList(BaseModel):
    data: list[EvidencePublic]
