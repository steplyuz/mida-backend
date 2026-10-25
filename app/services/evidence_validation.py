"""Accept only what we can name: the declared type must match the real signature, size is capped.

Signature sniffing stops a file that claims to be a PDF from being an executable. It does not scan
content for malware or embedded active content; PDFs are therefore only ever served as attachments.
"""

from __future__ import annotations

import os
import re

from app.core.domain_errors import PayloadTooLargeError, ValidationDomainError

ALLOWED_TYPES: dict[str, bytes] = {
    "image/png": b"\x89PNG\r\n\x1a\n",
    "image/jpeg": b"\xff\xd8\xff",
    "application/pdf": b"%PDF-",
}
MAX_FILENAME = 255
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def validate_file(declared_type: str, data: bytes, max_bytes: int) -> str:
    if not data:
        raise ValidationDomainError("The file is empty.")
    if len(data) > max_bytes:
        raise PayloadTooLargeError(f"The file is larger than {max_bytes // 1_000_000} MB.")
    mime = declared_type.split(";")[0].strip().lower()
    signature = ALLOWED_TYPES.get(mime)
    if signature is None:
        raise ValidationDomainError("This file type is not allowed. Use PNG, JPEG or PDF.")
    if not data.startswith(signature):
        raise ValidationDomainError("The file content does not match its declared type.")
    return mime


def clean_filename(raw: str | None) -> str:
    name = os.path.basename((raw or "").replace("\\", "/"))
    name = " ".join(_CONTROL.sub(" ", name).split())
    return (name or "evidence")[:MAX_FILENAME]
