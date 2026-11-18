"""Manifest loading and structural validation.

A manifest names the source coordinates, the baseline export, and every
later transaction with its positive integer seq, operation_count, shard
paths and digests. seq defines commit order for a source; the manifest is
the only authority on which shards belong to which transaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .errors import ManifestError
from .jsonl import stream_file_digest


@dataclass(frozen=True)
class ShardSpec:
    ordinal: int
    relpath: str
    digest: str
    operation_count: int


@dataclass(frozen=True)
class TxnSpec:
    seq: int
    operation_count: int
    shards: tuple[ShardSpec, ...]


@dataclass(frozen=True)
class BaselineSpec:
    seq: int
    relpath: str
    digest: str
    resource_count: int


@dataclass
class Manifest:
    source_server: str
    canonical_server: str
    tenant: str
    aliases: tuple[str, ...]
    baseline: BaselineSpec
    transactions: tuple[TxnSpec, ...] = field(default_factory=tuple)
    digest: str = ""

    def txn(self, seq: int) -> TxnSpec | None:
        for t in self.transactions:
            if t.seq == seq:
                return t
        return None


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise ManifestError(msg)


def load_manifest(manifest_path: str | Path,
                  allowed_servers: set[str] | None = None) -> Manifest:
    path = Path(manifest_path)
    if not path.is_file():
        raise ManifestError(f"manifest not found: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest {path} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError("manifest root must be a JSON object")

    source_server = raw.get("source_server")
    _require(isinstance(source_server, str) and "://" in source_server,
             "manifest.source_server must be an absolute http(s) URL")
    if allowed_servers is not None and source_server not in allowed_servers:
        raise ManifestError(
            f"source_server {source_server!r} is not registered in repo.json; "
            "register its canonical base and aliases at init time"
        )

    tenant = raw.get("tenant")
    _require(isinstance(tenant, str) and tenant != "", "manifest.tenant is required")

    aliases = raw.get("aliases", [])
    _require(isinstance(aliases, list)
             and all(isinstance(a, str) and "://" in a for a in aliases),
             "manifest.aliases must be a list of absolute URLs")

    base = raw.get("baseline")
    _require(isinstance(base, dict), "manifest.baseline is required")
    baseline_seq = base.get("seq")
    _require(isinstance(baseline_seq, int) and baseline_seq >= 0,
             "manifest.baseline.seq must be a non-negative integer")
    _require(isinstance(base.get("path"), str), "manifest.baseline.path required")
    _require(isinstance(base.get("sha256"), str)
             and len(base["sha256"]) == 64,
             "manifest.baseline.sha256 must be 64 hex chars")
    _require(isinstance(base.get("resource_count"), int)
             and base["resource_count"] >= 0,
             "manifest.baseline.resource_count must be a non-negative integer")
    baseline = BaselineSpec(
        seq=baseline_seq,
        relpath=base["path"],
        digest=base["sha256"],
        resource_count=base["resource_count"],
    )

    txns: list[TxnSpec] = []
    seen_seq: set[int] = set()
    for entry in raw.get("transactions", []):
        _require(isinstance(entry, dict), "each transaction must be an object")
        seq = entry.get("seq")
        _require(isinstance(seq, int) and seq > baseline_seq,
                 f"transaction seq must be an integer > baseline_seq ({baseline_seq})")
        _require(seq not in seen_seq, f"duplicate transaction seq {seq} in manifest")
        seen_seq.add(seq)
        op_count = entry.get("operation_count")
        _require(isinstance(op_count, int) and op_count > 0,
                 f"transaction {seq}: operation_count must be a positive integer")
        shard_raw = entry.get("shards")
        _require(isinstance(shard_raw, list) and len(shard_raw) > 0,
                 f"transaction {seq}: at least one shard is required")
        shards: list[ShardSpec] = []
        seen_ord: set[int] = set()
        total = 0
        for sh in shard_raw:
            _require(isinstance(sh, dict), f"transaction {seq}: bad shard entry")
            ordinal = sh.get("ordinal")
            _require(isinstance(ordinal, int) and ordinal >= 0,
                     f"transaction {seq}: shard ordinal must be a non-negative int")
            _require(ordinal not in seen_ord,
                     f"transaction {seq}: duplicate shard ordinal {ordinal}")
            seen_ord.add(ordinal)
            _require(isinstance(sh.get("path"), str),
                     f"transaction {seq}: shard path required")
            _require(isinstance(sh.get("sha256"), str) and len(sh["sha256"]) == 64,
                     f"transaction {seq}: shard sha256 must be 64 hex chars")
            count = sh.get("operation_count")
            _require(isinstance(count, int) and count >= 0,
                     f"transaction {seq}: shard operation_count must be an int")
            total += count
            shards.append(ShardSpec(ordinal, sh["path"], sh["sha256"], count))
        _require(total == op_count,
                 f"transaction {seq}: shard operation_count sum {total} "
                 f"!= manifest operation_count {op_count}")
        shards.sort(key=lambda s: s.ordinal)
        txns.append(TxnSpec(seq=seq, operation_count=op_count,
                            shards=tuple(shards)))

    txns.sort(key=lambda t: t.seq)
    digest = stream_file_digest(path)
    return Manifest(
        source_server=source_server,
        canonical_server=source_server,
        tenant=tenant,
        aliases=tuple(aliases),
        baseline=baseline,
        transactions=tuple(txns),
        digest=digest,
    )


def resolve_shard(package_root: Path, relpath: str) -> Path:
    """Resolve a manifest-relative shard path, refusing escapes."""

    candidate = (package_root / relpath).resolve()
    root = package_root.resolve()
    if root not in candidate.parents and candidate != root:
        raise ManifestError(f"shard path escapes package root: {relpath}")
    if not candidate.is_file():
        raise ManifestError(f"shard file missing: {relpath}")
    return candidate
