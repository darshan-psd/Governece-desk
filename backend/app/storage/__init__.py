"""Local document storage (same layout as the Node backend's R2-style adapter).

Objects are stored under ``<data dir>/files/<encodeURIComponent(key)>`` so files uploaded by the
Node backend remain readable. Keys are always ``<workspace>/<uuid>``; user-supplied filenames
are never used as paths.
"""
from __future__ import annotations

from pathlib import Path
from urllib.parse import quote


class LocalFileStorage:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Matches JavaScript's encodeURIComponent, so "/" and ".." can never escape the folder.
        return self.root / quote(key, safe="-_.!~*'()")

    def put(self, key: str, data: bytes) -> None:
        self._path(key).write_bytes(data)

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)
