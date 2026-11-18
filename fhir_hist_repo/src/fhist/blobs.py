"""Content-addressed blob storage.

Resource bodies are serialised once, hashed, and stored under
``blobs/xx/yy/<sha256>``. Identical bodies across versions or tenants share
storage automatically; blobs are append-only and never mutated, which is
what makes divergent retransmissions safe to isolate without touching
published facts.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .paths import RepoPaths


def add_blob(paths: RepoPaths, body: dict) -> tuple[str, str]:
    """Store one JSON body; return (sha256, repo-relative path)."""

    payload = json.dumps(body, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    target = paths.blob_path(digest)
    if not target.exists():
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(payload)
        tmp.replace(target)
    return digest, paths.relative(target)


def add_raw_blob(paths: RepoPaths, payload: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(payload).hexdigest()
    target = paths.blob_path(digest)
    if not target.exists():
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(payload)
        tmp.replace(target)
    return digest, paths.relative(target)


def read_blob(paths: RepoPaths, relpath: str) -> dict:
    return json.loads((paths.root / relpath).read_text(encoding="utf-8"))
