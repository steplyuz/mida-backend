"""Local-disk evidence storage. Writes are atomic (temp file, then rename)."""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from app.core.domain_errors import NotFoundError

_KEY = re.compile(r"^[0-9a-f]{32}$")


class LocalEvidenceStorage:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Keys are server-generated hex. Anything else is rejected before touching the filesystem.
        if _KEY.fullmatch(key) is None:
            raise NotFoundError("Evidence not found.")
        path = (self._root / key[:2] / key).resolve()
        if self._root not in path.parents:
            raise NotFoundError("Evidence not found.")
        return path

    def put(self, data: bytes) -> str:
        key = uuid.uuid4().hex
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(".tmp")
        with open(temporary, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        return key

    def get(self, key: str) -> bytes:
        path = self._path(key)
        if not path.is_file():
            raise NotFoundError("Evidence not found.")
        return path.read_bytes()

    def delete(self, key: str) -> None:
        path = self._path(key)
        path.unlink(missing_ok=True)
