"""Repository on-disk layout and content-addressed blob storage.

The repository is fully relocatable: every path stored in the database or in
``repo.json`` is relative to the repository root, and source coordinates are
mapped to directory names by a stable hash. Copying or moving the whole
directory keeps every cut queryable without the original absolute path.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

REPO_CONFIG = "repo.json"
DB_REL = "meta/history.sqlite3"
BLOB_REL = "blobs"
STAGE_REL = "staging"
CONFLICT_REL = "conflicts"
SOURCES_REL = "sources"


def source_dirname(source_server: str, tenant: str) -> str:
    """Return the stable, filesystem-safe directory name for a source.

    The mapping is content based (SHA-256 of the coordinate pair) so two
    tenants with identical resource ids never share a namespace on disk and
    no absolute input path ever leaks into the repository.
    """

    digest = hashlib.sha256(f"{source_server}\x1f{tenant}".encode("utf-8")).hexdigest()
    return digest[:16]


class RepoPaths:
    """Absolute-ish view of a repository; nothing here is stored verbatim."""

    def __init__(self, root: str | os.PathLike[str]):
        self.root = Path(root).resolve()
        self.config = self.root / REPO_CONFIG
        self.db = self.root / DB_REL
        self.blobs = self.root / BLOB_REL
        self.staging = self.root / STAGE_REL
        self.conflicts = self.root / CONFLICT_REL
        self.sources = self.root / SOURCES_REL

    def exists(self) -> bool:
        return self.config.is_file() and self.db.is_file()

    def ensure_layout(self) -> None:
        for p in (
            self.root / "meta",
            self.blobs,
            self.staging,
            self.conflicts,
            self.sources,
        ):
            p.mkdir(parents=True, exist_ok=True)

    def source_root(self, source_server: str, tenant: str) -> Path:
        d = self.sources / source_dirname(source_server, tenant)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def source_stage(self, source_server: str, tenant: str, seq: int) -> Path:
        d = self.source_root(source_server, tenant) / "stage" / str(seq)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def source_manifest_archive(
        self, source_server: str, tenant: str, seq: int
    ) -> Path:
        d = self.source_root(source_server, tenant) / "manifests"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"manifest-{seq}.json"

    def blob_path(self, digest_hex: str) -> Path:
        # Two-level fanout keeps a single directory small even for big stores.
        d = self.blobs / digest_hex[:2] / digest_hex[2:4]
        d.mkdir(parents=True, exist_ok=True)
        return d / digest_hex

    def conflict_path(self, source_id: int, seq: int, kind: str, key: str) -> Path:
        d = self.conflicts / str(source_id) / str(seq)
        d.mkdir(parents=True, exist_ok=True)
        safe = hashlib.sha256(f"{kind}\x1f{key}".encode("utf-8")).hexdigest()[:24]
        return d / f"{kind}-{safe}.json"

    def relative(self, p: Path) -> str:
        """Path relative to repo root, using POSIX separators."""

        return Path(p).resolve().relative_to(self.root).as_posix()
