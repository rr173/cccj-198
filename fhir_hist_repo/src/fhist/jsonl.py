"""Streaming NDJSON helpers.

Baseline exports and transaction shards are newline-delimited JSON so that a
single large resource never forces the whole package into memory. Each line
is parsed and released before the next one is read.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .errors import ShardError


def iter_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield JSON objects from an NDJSON file, one object per non-empty line."""

    with open(path, "r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ShardError(
                    f"{path}: invalid JSON on line {lineno}: {exc.msg}"
                ) from exc
            if not isinstance(obj, dict):
                raise ShardError(f"{path}: line {lineno} is not a JSON object")
            yield obj


def stream_jsonl_digest_and_count(path: str | Path) -> tuple[str, int]:
    """Return (sha256 over file bytes, number of non-empty lines).

    The digest is computed over the exact bytes delivered in the handoff
    directory, so moving the directory does not invalidate verification.
    """

    h = hashlib.sha256()
    count = 0
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
            count += chunk.count(b"\n")
    # Tolerate a final object without a trailing newline.
    if path.stat().st_size and not _ends_with_newline(path):
        count += 1
    return h.hexdigest(), count


def _ends_with_newline(path: str | Path) -> bool:
    with open(path, "rb") as fh:
        fh.seek(-1, 2)
        return fh.read(1) == b"\n"


def write_jsonl(path: str | Path, rows: Iterator[dict[str, Any]]) -> int:
    """Write rows to ``path`` atomically-ish; return the number of rows."""

    n = 0
    tmp = Path(str(path) + ".tmp")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
            n += 1
    tmp.replace(path)
    return n


def stream_file_digest(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
