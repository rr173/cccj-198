"""Per-operation and per-transaction validation rules.

These checks run on streamed shard rows before anything is published, so a
bad package can never replace an existing complete cut with a partial state.
"""

from __future__ import annotations

import re
from typing import Any

from .errors import PayloadError, ShardError, TransactionConflict

FHIR_ID_RE = re.compile(r"[A-Za-z0-9\-.]{1,64}\Z")
RESOURCE_TYPE_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{1,63}\Z")
REQUIRED_OP_FIELDS = ("seq", "ordinal", "method", "resourceType", "id", "versionId")


def validate_operation_row(row: dict[str, Any], expected_seq: int) -> dict[str, Any]:
    """Validate the envelope of one shard row; return it unchanged."""

    for field_name in REQUIRED_OP_FIELDS:
        if field_name not in row:
            raise ShardError(f"operation missing field {field_name!r}: {row}")
    if row["seq"] != expected_seq:
        raise PayloadError(
            f"operation seq {row['seq']} does not match transaction seq {expected_seq}"
        )
    if not isinstance(row["ordinal"], int) or row["ordinal"] < 0:
        raise PayloadError(f"ordinal must be a non-negative int: {row['ordinal']!r}")
    if row["method"] not in ("PUT", "DELETE"):
        raise PayloadError(f"method must be PUT or DELETE, got {row['method']!r}")
    if not isinstance(row["resourceType"], str) \
            or not RESOURCE_TYPE_RE.match(row["resourceType"]):
        raise PayloadError(f"invalid resourceType {row.get('resourceType')!r}")
    if not isinstance(row["id"], str) or not FHIR_ID_RE.match(row["id"]):
        raise PayloadError(f"invalid logical id {row.get('id')!r}")
    if not isinstance(row["versionId"], str) or not FHIR_ID_RE.match(row["versionId"]):
        raise PayloadError(f"invalid versionId {row.get('versionId')!r}")
    return row


def validate_put_body(row: dict[str, Any]) -> dict[str, Any]:
    """A PUT carries the full resource; metadata must equal the body."""

    body = row.get("resource")
    if not isinstance(body, dict):
        raise PayloadError(
            f"PUT {row['resourceType']}/{row['id']} must carry a full resource body"
        )
    if body.get("resourceType") != row["resourceType"]:
        raise PayloadError(
            f"PUT {row['resourceType']}/{row['id']}: body resourceType "
            f"{body.get('resourceType')!r} != operation metadata"
        )
    if str(body.get("id")) != row["id"]:
        raise PayloadError(
            f"PUT {row['resourceType']}/{row['id']}: body id "
            f"{body.get('id')!r} != operation metadata"
        )
    meta = body.get("meta")
    body_vid = meta.get("versionId") if isinstance(meta, dict) else None
    if body_vid != row["versionId"]:
        raise PayloadError(
            f"PUT {row['resourceType']}/{row['id']}: meta.versionId "
            f"{body_vid!r} != operation versionId {row['versionId']!r}"
        )
    if isinstance(meta, dict) and isinstance(meta.get("lastUpdated"), str):
        row["_lastUpdated"] = meta["lastUpdated"]
    return body


def validate_delete_row(row: dict[str, Any]) -> None:
    """A DELETE is a versioned tombstone with no resource body."""

    if "resource" in row and row["resource"] is not None:
        raise PayloadError(
            f"DELETE {row['resourceType']}/{row['id']} must not carry a resource body"
        )


class TxnCollisionTracker:
    """Detect same-seq/ordinal and same logical resource collisions in one txn."""

    def __init__(self, seq: int) -> None:
        self.seq = seq
        self._ordinals: dict[int, str] = {}
        self._resources: dict[tuple[str, str], str] = {}

    def add(self, row: dict[str, Any]) -> None:
        loc = f"ordinal={row['ordinal']} {row['resourceType']}/{row['id']}"
        prior = self._ordinals.get(row["ordinal"])
        if prior is not None:
            raise TransactionConflict(
                f"transaction {self.seq}: duplicate ordinal {row['ordinal']} "
                f"at {loc}; first seen at {prior}"
            )
        self._ordinals[row["ordinal"]] = loc
        key = (row["resourceType"], row["id"])
        prior_res = self._resources.get(key)
        if prior_res is not None:
            raise TransactionConflict(
                f"transaction {self.seq}: resource {key[0]}/{key[1]} appears "
                f"more than once ({prior_res} and {loc})"
            )
        self._resources[key] = loc
