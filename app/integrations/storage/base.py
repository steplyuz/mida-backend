"""Evidence storage contract. Keys are server-generated, never derived from user input."""

from __future__ import annotations

from typing import Protocol


class EvidenceStorage(Protocol):
    def put(self, data: bytes) -> str: ...

    def get(self, key: str) -> bytes: ...

    def delete(self, key: str) -> None: ...
