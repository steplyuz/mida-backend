from pathlib import Path

import pytest

from app.core.domain_errors import NotFoundError, PayloadTooLargeError, ValidationDomainError
from app.integrations.storage.local import LocalEvidenceStorage
from app.services.evidence_validation import clean_filename, validate_file

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
JPEG = b"\xff\xd8\xff" + b"0" * 32
PDF = b"%PDF-1.7\n" + b"0" * 32


@pytest.mark.parametrize(
    ("declared", "data"), [("image/png", PNG), ("image/jpeg", JPEG), ("application/pdf", PDF)]
)
def test_real_signatures_of_allowed_types_are_accepted(declared: str, data: bytes) -> None:
    assert validate_file(declared, data, 10_000) == declared


def test_declared_type_parameters_are_ignored() -> None:
    assert validate_file("IMAGE/PNG; charset=binary", PNG, 10_000) == "image/png"


@pytest.mark.parametrize(
    ("declared", "data"),
    [
        ("image/png", b"<html><script>alert(1)</script></html>"),  # claims PNG, is HTML
        ("application/pdf", b"MZ\x90\x00 executable"),  # claims PDF, is a Windows binary
        ("text/html", b"<html></html>"),  # never an allowed type
        ("application/x-msdownload", b"MZ\x90\x00"),
        ("image/svg+xml", b"<svg></svg>"),  # active content: refused
    ],
)
def test_mismatched_or_disallowed_content_is_rejected(declared: str, data: bytes) -> None:
    with pytest.raises(ValidationDomainError):
        validate_file(declared, data, 10_000)


def test_empty_file_is_rejected() -> None:
    with pytest.raises(ValidationDomainError):
        validate_file("image/png", b"", 10_000)


def test_oversized_file_is_rejected_with_413() -> None:
    with pytest.raises(PayloadTooLargeError) as info:
        validate_file("image/png", PNG + b"x" * 100, 50)
    assert info.value.status_code == 413


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Windows\\evil.exe", "evil.exe"),
        ("report\x00.pdf", "report .pdf"),
        ("", "evidence"),
        (None, "evidence"),
        ("x" * 400, "x" * 255),
    ],
)
def test_client_filenames_are_reduced_to_safe_display_names(raw: str | None, expected: str) -> None:
    assert clean_filename(raw) == expected


def test_storage_round_trip_uses_server_keys(tmp_path: Path) -> None:
    storage = LocalEvidenceStorage(tmp_path / "evidence")
    key = storage.put(PNG)
    assert len(key) == 32 and all(c in "0123456789abcdef" for c in key)
    assert storage.get(key) == PNG
    storage.delete(key)
    with pytest.raises(NotFoundError):
        storage.get(key)


@pytest.mark.parametrize("key", ["../secret", "../../etc/passwd", "a" * 31, "G" * 32, "/etc/passwd"])
def test_storage_refuses_keys_that_are_not_server_generated(tmp_path: Path, key: str) -> None:
    storage = LocalEvidenceStorage(tmp_path / "evidence")
    with pytest.raises(NotFoundError):
        storage.get(key)
    with pytest.raises(NotFoundError):
        storage.delete(key)
