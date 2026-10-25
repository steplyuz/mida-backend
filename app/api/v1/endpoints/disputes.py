"""Disputes and evidence. Services decide authorisation; these handlers translate HTTP."""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Header, Request, Response

from app.api.deps import AdminUserDep, CurrentUserDep, DealServiceDep, SessionDep
from app.core.config import Settings
from app.core.domain_errors import PayloadTooLargeError
from app.integrations.storage.local import LocalEvidenceStorage
from app.models.enums import DisputeReason, DisputeResolution
from app.schemas.dispute import (
    DisputeAssignRequest,
    DisputeOpenRequest,
    DisputePage,
    DisputePublic,
    DisputeResolveRequest,
    EvidenceList,
    EvidencePublic,
    MessageReferenceRequest,
    NoteRequest,
)
from app.services.dispute_service import DisputeService
from app.services.evidence_service import EvidenceService

router = APIRouter(tags=["disputes"])


def _storage(request: Request) -> LocalEvidenceStorage:
    settings: Settings = request.app.state.settings
    return LocalEvidenceStorage(settings.evidence_root)


def _evidence_service(request: Request, session: SessionDep) -> EvidenceService:
    settings: Settings = request.app.state.settings
    return EvidenceService(session, _storage(request), max_bytes=settings.evidence_max_bytes)


async def _read_limited(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise PayloadTooLargeError(f"The file is larger than {limit // 1_000_000} MB.")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise PayloadTooLargeError(f"The file is larger than {limit // 1_000_000} MB.")
        chunks.append(chunk)
    return b"".join(chunks)


# ---------- disputes ----------


@router.post("/deals/{public_id}/disputes", status_code=201, response_model=DisputePublic)
async def open_dispute(
    public_id: str,
    body: DisputeOpenRequest,
    current: CurrentUserDep,
    service: DealServiceDep,
    session: SessionDep,
) -> DisputePublic:
    deal = await service.get_deal_by_public_id(current.actor, public_id)
    dispute = await DisputeService(session).open(
        current.actor,
        deal.id,
        reason=DisputeReason(body.reason_code),
        description=body.description,
        expected_version=body.expected_version,
    )
    return DisputePublic.build(dispute, deal.public_id)


@router.get("/deals/{public_id}/disputes", response_model=DisputePage)
async def list_disputes(
    public_id: str, current: CurrentUserDep, service: DealServiceDep, session: SessionDep
) -> DisputePage:
    deal = await service.get_deal_by_public_id(current.actor, public_id)
    rows = await DisputeService(session).list_for_deal(current.actor, deal)
    return DisputePage(data=[DisputePublic.build(row, deal.public_id) for row in rows])


@router.get("/disputes/{dispute_id}", response_model=DisputePublic)
async def get_dispute(dispute_id: str, current: CurrentUserDep, session: SessionDep) -> DisputePublic:
    dispute, deal = await DisputeService(session).get(current.actor, dispute_id)
    return DisputePublic.build(dispute, deal.public_id)


@router.post("/disputes/{dispute_id}/assign", response_model=DisputePublic)
async def assign_dispute(
    dispute_id: str, body: DisputeAssignRequest, admin: AdminUserDep, session: SessionDep
) -> DisputePublic:
    service = DisputeService(session)
    dispute, deal = await service.get(admin.actor, dispute_id)
    updated = await service.assign(admin.actor, dispute, expected_version=body.expected_version)
    return DisputePublic.build(updated, deal.public_id)


@router.post("/disputes/{dispute_id}/resolve", response_model=DisputePublic)
async def resolve_dispute(
    dispute_id: str, body: DisputeResolveRequest, admin: AdminUserDep, session: SessionDep
) -> DisputePublic:
    service = DisputeService(session)
    dispute, deal = await service.get(admin.actor, dispute_id)
    updated = await service.resolve(
        admin.actor,
        dispute,
        resolution=DisputeResolution(body.resolution),
        reason=body.reason,
        expected_version=body.expected_version,
    )
    return DisputePublic.build(updated, deal.public_id)


# ---------- evidence ----------


@router.post("/deals/{public_id}/evidence/files", status_code=201, response_model=EvidencePublic)
async def upload_file(
    public_id: str,
    request: Request,
    current: CurrentUserDep,
    service: DealServiceDep,
    session: SessionDep,
    label: Annotated[str, Header(alias="X-Evidence-Label")] = "other",
    filename: Annotated[str | None, Header(alias="X-Filename")] = None,
) -> EvidencePublic:
    """Raw body upload. Send the bytes with Content-Type set to the real type (PNG, JPEG or PDF)."""
    settings: Settings = request.app.state.settings
    deal = await service.get_deal_by_public_id(current.actor, public_id)
    data = await _read_limited(request, settings.evidence_max_bytes)
    evidence = await _evidence_service(request, session).upload_file(
        current.actor,
        deal.id,
        label=label,
        filename=filename,
        declared_type=request.headers.get("content-type", ""),
        data=data,
    )
    return EvidencePublic.build(evidence, deal.public_id)


@router.post("/deals/{public_id}/evidence/notes", status_code=201, response_model=EvidencePublic)
async def add_note(
    public_id: str,
    body: NoteRequest,
    request: Request,
    current: CurrentUserDep,
    service: DealServiceDep,
    session: SessionDep,
) -> EvidencePublic:
    deal = await service.get_deal_by_public_id(current.actor, public_id)
    evidence = await _evidence_service(request, session).add_note(current.actor, deal.id, body.text)
    return EvidencePublic.build(evidence, deal.public_id)


@router.post("/deals/{public_id}/evidence/message-references", status_code=201, response_model=EvidencePublic)
async def add_message_reference(
    public_id: str,
    body: MessageReferenceRequest,
    request: Request,
    current: CurrentUserDep,
    service: DealServiceDep,
    session: SessionDep,
) -> EvidencePublic:
    deal = await service.get_deal_by_public_id(current.actor, public_id)
    evidence = await _evidence_service(request, session).add_message_reference(
        current.actor, deal.id, body.message_id
    )
    return EvidencePublic.build(evidence, deal.public_id)


@router.get("/deals/{public_id}/evidence", response_model=EvidenceList)
async def list_evidence(
    public_id: str,
    request: Request,
    current: CurrentUserDep,
    service: DealServiceDep,
    session: SessionDep,
) -> EvidenceList:
    deal = await service.get_deal_by_public_id(current.actor, public_id)
    rows = await _evidence_service(request, session).list_for_deal(current.actor, deal.id)
    return EvidenceList(data=[EvidencePublic.build(row, deal.public_id) for row in rows])


@router.get("/evidence/{evidence_id}/content", summary="Download an evidence file")
async def download_evidence(
    evidence_id: str, request: Request, current: CurrentUserDep, session: SessionDep
) -> Response:
    evidence, data = await _evidence_service(request, session).read_file(current.actor, evidence_id)
    # Always an attachment, never inline: nothing uploaded is rendered by the browser.
    name = quote(evidence.original_filename or "evidence")
    return Response(
        content=data,
        media_type=evidence.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{name}",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        },
    )
