"""Staging and advancing the source frontier.

Invariants implemented here:

* seq defines commit order. Earlier arrivals for later transactions are
  *staged* but never published before their predecessors.
* A transaction is only publishable when every manifest-listed shard is
  present with matching digest and operation count and every row passes
  validation. Bad input is quarantined and the frontier stays put; an
  existing complete cut is never replaced by partial state.
* Identical retransmissions fold away silently. Divergent payloads for
  already-published content are isolated in ``isolated_conflicts`` and never
  overwrite a fact or rewrite an old cut.
* Advancing is a single serialised SQLite transaction per seq with cut rows
  created inside it, so each published cut is atomic and queryable exactly
  when its seq is durable.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import blobs
from .config import RepoConfig
from .db import snapshot_tx, write_tx
from .errors import (
    CutNotQueryable,
    ManifestError,
    PayloadError,
    ShardError,
    TransactionConflict,
    UnknownSource,
)
from .jsonl import (
    iter_jsonl,
    stream_file_digest,
    stream_jsonl_digest_and_count,
)
from .manifest import Manifest, TxnSpec, load_manifest, resolve_shard
from .paths import RepoPaths
from .refs import extract_edges
from .validate import (
    TxnCollisionTracker,
    validate_delete_row,
    validate_operation_row,
    validate_put_body,
)


# ---------------------------------------------------------------------------
# Source registration
# ---------------------------------------------------------------------------

def get_source(conn: sqlite3.Connection, source_server: str,
               tenant: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sources WHERE source_server=? AND tenant=?",
        (source_server, tenant),
    ).fetchone()


def register_source(conn: sqlite3.Connection, manifest: Manifest) -> int:
    existing = get_source(conn, manifest.source_server, manifest.tenant)
    if existing is not None:
        if existing["baseline_seq"] != manifest.baseline.seq:
            raise ManifestError(
                f"source baseline_seq {manifest.baseline.seq} differs from "
                f"registered {existing['baseline_seq']}"
            )
        return int(existing["source_id"])
    cur = conn.execute(
        "INSERT INTO sources(source_server, tenant, baseline_seq, published_seq) "
        "VALUES (?,?,?,?)",
        (manifest.source_server, manifest.tenant,
         manifest.baseline.seq, -1),
    )
    conn.execute(
        "INSERT INTO events(source_id, seq, kind, detail) VALUES (?,?,?,?)",
        (cur.lastrowid, None, "source_registered",
         json.dumps({"baseline_seq": manifest.baseline.seq})),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Stage
# ---------------------------------------------------------------------------

def stage_manifest(paths: RepoPaths, conn: sqlite3.Connection,
                   config: RepoConfig, manifest_path: str | Path,
                   package_root: str | Path | None = None) -> dict[str, Any]:
    """Validate and stage everything described by one manifest delivery.

    Returns a summary dict for the CLI. Never raises for *missing* shards:
    out-of-order partial deliveries are a normal staging state.
    """

    package_root = Path(package_root) if package_root else Path(manifest_path).parent
    manifest = load_manifest(
        manifest_path,
        allowed_servers=set(config.sources.keys()),
    )
    canonical = config.match_source(manifest.source_server)
    if canonical is None or canonical != manifest.source_server:
        raise ManifestError(
            f"source_server {manifest.source_server!r} is not the registered "
            f"canonical base for this repository"
        )

    with write_tx(conn):
        source_id = register_source(conn, manifest)
        archive = paths.source_manifest_archive(
            manifest.source_server, manifest.tenant, manifest.baseline.seq
        )
        if not archive.exists():
            archive.write_bytes(Path(manifest_path).read_bytes())
        result: dict[str, Any] = {
            "source_id": source_id,
            "baseline_seq": manifest.baseline.seq,
            "staged": [],
            "missing": [],
            "bad": [],
            "folded": [],
            "conflicts": [],
        }

        _stage_baseline(paths, conn, config, manifest, package_root,
                        source_id, result)

        for txn in manifest.transactions:
            _stage_transaction(paths, conn, config, manifest, txn,
                               package_root, source_id, result)
        conn.execute(
            "INSERT INTO events(source_id, seq, kind, detail) VALUES (?,?,?,?)",
            (source_id, None, "manifest_staged",
             json.dumps({"manifest_digest": manifest.digest,
                         "txns": [t.seq for t in manifest.transactions]}))
        )
    return result


def _package_rel(package_root: Path, shard_path: Path) -> str:
    """Path relative to the source's packages/ root (POSIX form)."""

    packages_root = package_root.parent  # .../<source>/packages
    return shard_path.resolve().relative_to(packages_root.resolve()).as_posix()


def _stage_baseline(paths: RepoPaths, conn: sqlite3.Connection,
                    config: RepoConfig, manifest: Manifest,
                    package_root: Path, source_id: int,
                    result: dict[str, Any]) -> None:
    spec = manifest.baseline
    existing_shard = conn.execute(
        "SELECT state FROM shards WHERE source_id=? AND seq=? AND ordinal=0",
        (source_id, spec.seq),
    ).fetchone()
    try:
        shard_path = resolve_shard(package_root, spec.relpath)
    except ManifestError as exc:
        if existing_shard is None:
            conn.execute(
                "INSERT INTO shards(source_id, seq, ordinal, shard_relpath, "
                "expected_digest, expected_count, state) "
                "VALUES (?,?,?,?,?,?,'missing')",
                (source_id, spec.seq, 0, spec.relpath, spec.digest,
                 spec.resource_count),
            )
        result["missing"].append({"seq": spec.seq, "path": spec.relpath,
                                  "reason": str(exc)})
        return

    actual_digest, actual_count = stream_jsonl_digest_and_count(shard_path)
    state = "verified"
    reason = None
    if actual_digest != spec.digest:
        state, reason = "bad_digest", (
            f"digest mismatch: expected {spec.digest} got {actual_digest}")
    elif actual_count != spec.resource_count:
        state, reason = "bad_count", (
            f"count mismatch: expected {spec.resource_count} got {actual_count}")
    if state == "verified":
        try:
            for _ in iter_jsonl(shard_path):
                pass
        except ShardError as exc:
            state, reason = "bad_format", str(exc)
    stored_rel = _package_rel(package_root, shard_path)

    # Monotonic guard: never let a later out-of-order bad delivery downgrade
    # an already verified baseline (the first cut must stay reproducible).
    if existing_shard is not None and existing_shard["state"] == "verified" \
            and state != "verified":
        result["bad"].append({
            "seq": spec.seq, "state": state,
            "reason": f"kept previously verified baseline; later delivery "
                      f"reported {state}: {reason}",
        })
        return

    if existing_shard is None:
        conn.execute(
            "INSERT INTO shards(source_id, seq, ordinal, shard_relpath, "
            "expected_digest, actual_digest, expected_count, actual_count, state) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (source_id, spec.seq, 0, stored_rel, spec.digest,
             actual_digest, spec.resource_count, actual_count, state),
        )
    else:
        # A re-delivery carries its own claimed digest/count: the expected
        # values must be replaced, not kept from the previous (possibly bad)
        # delivery, otherwise a good file can never match a stale claim.
        conn.execute(
            "UPDATE shards SET expected_digest=?, actual_digest=?, "
            "expected_count=?, actual_count=?, state=?, shard_relpath=? "
            "WHERE source_id=? AND seq=? AND ordinal=0",
            (spec.digest, actual_digest, spec.resource_count, actual_count,
             state, stored_rel, source_id, spec.seq),
        )
    if reason:
        result["bad"].append({"seq": spec.seq, "state": state, "reason": reason})


def _stage_transaction(paths: RepoPaths, conn: sqlite3.Connection,
                       config: RepoConfig, manifest: Manifest, txn: TxnSpec,
                       package_root: Path, source_id: int,
                       result: dict[str, Any]) -> None:
    existing_txn = conn.execute(
        "SELECT state, manifest_digest FROM transactions "
        "WHERE source_id=? AND seq=?",
        (source_id, txn.seq),
    ).fetchone()
    if existing_txn is not None and existing_txn["state"] == "published":
        if existing_txn["manifest_digest"] == manifest.digest:
            result["folded"].append(txn.seq)
            return
        # A re-delivery describing an already-published seq: verify shards
        # against the *new* claims and isolate any divergent payload.
        _check_retransmission(paths, conn, config, manifest, txn,
                              package_root, source_id, result)
        return

    conn.execute(
        "INSERT INTO transactions(source_id, seq, state, operation_count, "
        "manifest_digest, manifest_relpath) VALUES (?,?, 'staged', ?, ?, ?) "
        "ON CONFLICT(source_id, seq) DO UPDATE SET "
        "operation_count=excluded.operation_count, "
        "manifest_digest=excluded.manifest_digest, "
        "manifest_relpath=excluded.manifest_relpath",
        (source_id, txn.seq, txn.operation_count, manifest.digest,
         paths.relative(paths.source_manifest_archive(
             manifest.source_server, manifest.tenant, manifest.baseline.seq))),
    )

    all_verified = True
    seen_shards: set[int] = set()
    for shard in txn.shards:
        seen_shards.add(shard.ordinal)
        state, actual_digest, actual_count, reason, shard_path = \
            _verify_shard_file(package_root, shard)
        relpath = (_package_rel(package_root, shard_path)
                   if shard_path is not None else shard.relpath)

        # Monotonic state guard: a delivery processed out of order (e.g. a
        # partial package replayed after a complete one) must never downgrade
        # an already verified shard back to missing/bad.
        prior = conn.execute(
            "SELECT state FROM shards WHERE source_id=? AND seq=? AND ordinal=?",
            (source_id, txn.seq, shard.ordinal),
        ).fetchone()
        if prior is not None and prior["state"] == "verified" \
                and state != "verified":
            result["missing" if state == "missing" else "bad"].append({
                "seq": txn.seq, "ordinal": shard.ordinal, "state": state,
                "reason": f"kept previously verified shard; later delivery "
                          f"reported {state}: {reason}",
            })
            continue

        conn.execute(
            "INSERT INTO shards(source_id, seq, ordinal, shard_relpath, "
            "expected_digest, actual_digest, expected_count, actual_count, state) "
            "VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(source_id, seq, ordinal) DO UPDATE SET "
            "shard_relpath=excluded.shard_relpath, "
            "expected_digest=excluded.expected_digest, "
            "actual_digest=excluded.actual_digest, "
            "expected_count=excluded.expected_count, "
            "actual_count=excluded.actual_count, state=excluded.state",
            (source_id, txn.seq, shard.ordinal, relpath, shard.digest,
             actual_digest, shard.operation_count, actual_count, state),
        )
        if state != "verified":
            all_verified = False
            if state == "missing":
                result["missing"].append({"seq": txn.seq, "ordinal": shard.ordinal,
                                          "path": shard.relpath, "reason": reason})
            else:
                result["bad"].append({"seq": txn.seq, "ordinal": shard.ordinal,
                                      "state": state, "reason": reason})

    # Shards listed in an earlier manifest but absent from this one keep
    # their prior row; check the union when judging publishability.
    rows = conn.execute(
        "SELECT state FROM shards WHERE source_id=? AND seq=?",
        (source_id, txn.seq),
    ).fetchall()
    all_verified = bool(rows) and all(r["state"] == "verified" for r in rows)

    if not all_verified:
        return

    # Full shard set present and byte-verified: stream-validate all rows and
    # detect intra-transaction conflicts. Nothing is published yet.
    try:
        _validate_txn_rows(manifest, txn, package_root)
    except (ShardError, TransactionConflict, PayloadError) as exc:
        conn.execute(
            "UPDATE transactions SET state='conflict', conflict_reason=? "
            "WHERE source_id=? AND seq=?",
            (str(exc), source_id, txn.seq),
        )
        conn.execute(
            "UPDATE sources SET blocked_seq=?, blocked_reason=? WHERE source_id=?",
            (txn.seq, str(exc), source_id),
        )
        conn.execute(
            "INSERT INTO isolated_conflicts(source_id, seq, kind, reason, "
            "payload_relpath) VALUES (?,?, 'txn_invalid', ?, ?)",
            (source_id, txn.seq, str(exc), None),
        )
        result["conflicts"].append({"seq": txn.seq, "reason": str(exc)})
        return

    result["staged"].append(txn.seq)


def _verify_shard_file(package_root: Path, shard) -> tuple[str, str | None,
                                                            int | None,
                                                            str | None,
                                                            Path | None]:
    try:
        shard_path = resolve_shard(package_root, shard.relpath)
    except ManifestError as exc:
        return "missing", None, None, str(exc), None
    actual_digest, actual_count = stream_jsonl_digest_and_count(shard_path)
    if actual_digest != shard.digest:
        return ("bad_digest", actual_digest, actual_count,
                f"digest mismatch: expected {shard.digest} got {actual_digest}",
                shard_path)
    if actual_count != shard.operation_count:
        return ("bad_count", actual_digest, actual_count,
                f"count mismatch: expected {shard.operation_count} got {actual_count}",
                shard_path)
    try:
        # Touch parse every line; bad JSON must not be publishable.
        for _ in iter_jsonl(shard_path):
            pass
    except ShardError as exc:
        return "bad_format", actual_digest, actual_count, str(exc), shard_path
    return "verified", actual_digest, actual_count, None, shard_path


def _validate_txn_rows(manifest: Manifest, txn: TxnSpec,
                       package_root: Path) -> None:
    tracker = TxnCollisionTracker(txn.seq)
    total = 0
    for shard in txn.shards:
        shard_path = resolve_shard(package_root, shard.relpath)
        for row in iter_jsonl(shard_path):
            validate_operation_row(row, txn.seq)
            if row["method"] == "PUT":
                validate_put_body(row)
            else:
                validate_delete_row(row)
            tracker.add(row)
            total += 1
    if total != txn.operation_count:
        raise ShardError(
            f"transaction {txn.seq}: streamed {total} rows != "
            f"operation_count {txn.operation_count}"
        )


def _check_retransmission(paths: RepoPaths, conn: sqlite3.Connection,
                          config: RepoConfig, manifest: Manifest, txn: TxnSpec,
                          package_root: Path, source_id: int,
                          result: dict[str, Any]) -> None:
    """Fold identical retransmissions; quarantine divergent ones."""

    alias_bases = config.sources[manifest.source_server]["aliases"]
    for shard in txn.shards:
        try:
            shard_path = resolve_shard(package_root, shard.relpath)
        except ManifestError as exc:
            result["missing"].append({"seq": txn.seq, "ordinal": shard.ordinal,
                                      "reason": str(exc)})
            continue
        state, _, _, reason, _ = _verify_shard_file(package_root, shard)
        if state != "verified":
            result["bad"].append({"seq": txn.seq, "ordinal": shard.ordinal,
                                  "state": state, "reason": reason})
            continue
        try:
            rows = list(iter_jsonl(shard_path))
            for row in rows:
                validate_operation_row(row, txn.seq)
        except (ShardError, PayloadError) as exc:
            _isolate_txn_failure(conn, paths, source_id, txn.seq, str(exc),
                                 result)
            return
        for row in rows:
            published = conn.execute(
                "SELECT body_digest, method, version_id FROM resource_version "
                "WHERE source_id=? AND resource_type=? AND logical_id=? AND seq=?",
                (source_id, row["resourceType"], row["id"], txn.seq),
            ).fetchone()
            if published is None:
                # Published txn did not touch this identity: divergent scope.
                _isolate(paths, conn, source_id, txn.seq, row,
                         "retransmitted txn contains an operation absent from "
                         "the published transaction", result)
                continue
            if row["method"] == "PUT":
                try:
                    body = validate_put_body(row)
                except PayloadError as exc:
                    _isolate_txn_failure(conn, paths, source_id, txn.seq,
                                         str(exc), result)
                    return
                digest, relpath = blobs.add_blob(paths, body)
                if digest != published["body_digest"] \
                        or row["versionId"] != published["version_id"]:
                    _isolate(paths, conn, source_id, txn.seq, row,
                             "divergent payload for already-published operation",
                             result, relpath)
            else:
                if row["versionId"] != published["version_id"]:
                    _isolate(paths, conn, source_id, txn.seq, row,
                             "divergent tombstone versionId for published operation",
                             result)
    if not any(c["seq"] == txn.seq for c in result["conflicts"]):
        result["folded"].append(txn.seq)


def _isolate_txn_failure(conn: sqlite3.Connection, paths: RepoPaths,
                         source_id: int, seq: int, reason: str,
                         result: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE transactions SET state='conflict', conflict_reason=? "
        "WHERE source_id=? AND seq=?",
        (reason, source_id, seq),
    )
    conn.execute(
        "UPDATE sources SET blocked_seq=?, blocked_reason=? WHERE source_id=?",
        (seq, reason, source_id),
    )
    conn.execute(
        "INSERT INTO isolated_conflicts(source_id, seq, kind, reason, "
        "payload_relpath) VALUES (?,?,'txn_invalid',?,NULL)",
        (source_id, seq, reason),
    )
    conn.execute(
        "INSERT INTO events(source_id, seq, kind, detail) VALUES (?,?,?,?)",
        (source_id, seq, "conflict_isolated", json.dumps({"reason": reason}))
    )
    result["conflicts"].append({"seq": seq, "reason": reason})


def _isolate(paths: RepoPaths, conn: sqlite3.Connection, source_id: int,
             seq: int, row: dict[str, Any], reason: str,
             result: dict[str, Any], payload_relpath: str | None = None) -> None:
    if payload_relpath is None and row["method"] == "PUT":
        _, payload_relpath = blobs.add_blob(paths, row["resource"])
    conn.execute(
        "INSERT INTO isolated_conflicts(source_id, seq, tx_ordinal, kind, "
        "resource_type, logical_id, version_id, reason, payload_relpath) "
        "VALUES (?,?,?, 'retransmit_divergent', ?,?,?,?,?)",
        (source_id, seq, row["ordinal"], row["resourceType"], row["id"],
         row["versionId"], reason, payload_relpath),
    )
    conn.execute(
        "INSERT INTO events(source_id, seq, kind, detail) VALUES (?,?,?,?)",
        (source_id, seq, "conflict_isolated",
         json.dumps({"resource": f"{row['resourceType']}/{row['id']}",
                     "versionId": row["versionId"], "reason": reason}))
    )
    result["conflicts"].append(
        {"seq": seq, "resource": f"{row['resourceType']}/{row['id']}",
         "versionId": row["versionId"], "reason": reason}
    )


# ---------------------------------------------------------------------------
# Advance
# ---------------------------------------------------------------------------

def advance(paths: RepoPaths, conn: sqlite3.Connection, config: RepoConfig,
            source_server: str, tenant: str,
            to_seq: int | None = None) -> dict[str, Any]:
    """Publish every publishable consecutive transaction up to ``to_seq``."""

    with snapshot_tx(conn) as ro:
        source = get_source(ro, source_server, tenant)
        if source is None:
            raise UnknownSource(f"{source_server} / {tenant}")
        source_id = int(source["source_id"])
        baseline_seq = int(source["baseline_seq"])
        published_seq = int(source["published_seq"])
        frontier = max(published_seq, baseline_seq - 1)
        blocked = source["blocked_seq"]

    summary: dict[str, Any] = {
        "source_id": source_id,
        "started_at": published_seq,
        "imported_baseline": False,
        "advanced": [],
        "held": [],
    }

    if published_seq == -1:
        _import_baseline(paths, conn, config, source_id, source_server,
                         tenant, baseline_seq)
        published_seq = baseline_seq
        frontier = baseline_seq
        summary["imported_baseline"] = True

    while True:
        next_seq = published_seq + 1
        if to_seq is not None and next_seq > to_seq:
            break
        with snapshot_tx(conn) as ro:
            txn = ro.execute(
                "SELECT * FROM transactions WHERE source_id=? AND seq=?",
                (source_id, next_seq),
            ).fetchone()
            if txn is None:
                break
            if txn["state"] == "conflict":
                summary["held"].append(
                    {"seq": next_seq, "reason": txn["conflict_reason"]})
                break
            shard_rows = ro.execute(
                "SELECT state, shard_relpath, expected_digest, expected_count "
                "FROM shards WHERE source_id=? AND seq=? ORDER BY ordinal",
                (source_id, next_seq),
            ).fetchall()
        if not shard_rows or any(r["state"] != "verified" for r in shard_rows):
            summary["held"].append(
                {"seq": next_seq, "reason": "shards incomplete or unverified"})
            break
        if blocked is not None and blocked <= next_seq:
            with snapshot_tx(conn) as ro:
                reason = ro.execute(
                    "SELECT blocked_reason FROM sources WHERE source_id=?",
                    (source_id,),
                ).fetchone()["blocked_reason"]
            summary["held"].append({"seq": next_seq, "reason": reason})
            break

        _publish_transaction(paths, conn, config, source_id, source_server,
                             tenant, next_seq, shard_rows)
        published_seq = next_seq
        summary["advanced"].append(next_seq)

    summary["frontier"] = published_seq
    return summary


def _import_baseline(paths: RepoPaths, conn: sqlite3.Connection,
                     config: RepoConfig, source_id: int, source_server: str,
                     tenant: str, baseline_seq: int) -> None:
    with snapshot_tx(conn) as ro:
        row = ro.execute(
            "SELECT * FROM shards WHERE source_id=? AND seq=? AND ordinal=0",
            (source_id, baseline_seq),
        ).fetchone()
    if row is None or row["state"] != "verified":
        raise CutNotQueryable(
            f"baseline shard for seq {baseline_seq} is missing or unverified; "
            "cannot initialise the first cut"
        )
    shard_path = _material_shard_path(paths, row["shard_relpath"], source_server,
                                      tenant, baseline_seq,
                                      expected_digest=row["expected_digest"])
    # Defence in depth: re-verify bytes at publish time, so a later bad
    # re-delivery can never sneak a partial baseline into the first cut.
    actual_digest, actual_count = stream_jsonl_digest_and_count(shard_path)
    if actual_digest != row["expected_digest"] \
            or actual_count != row["expected_count"]:
        raise CutNotQueryable(
            f"baseline shard failed re-verification at import: "
            f"digest_ok={actual_digest == row['expected_digest']} "
            f"count_ok={actual_count == row['expected_count']}"
        )
    alias_bases = config.sources[source_server]["aliases"]
    with write_tx(conn):
        count = 0
        for row in iter_jsonl(shard_path):
            # Baseline exports may be either operation envelopes
            # (method/resource) or bare FHIR resources; accept both.
            if isinstance(row.get("resource"), dict) and "method" in row:
                validate_operation_row(row, baseline_seq)
                body = validate_put_body(row)
                ordinal = row["ordinal"]
            else:
                if not isinstance(row.get("resourceType"), str) \
                        or not isinstance(row.get("id"), str):
                    raise PayloadError(
                        "baseline row is neither an operation envelope nor a "
                        f"FHIR resource: {row!r:.120}")
                body = row
                ordinal = count
            _insert_put_fact(paths, conn, source_id, baseline_seq, ordinal,
                             body, alias_bases, origin="baseline")
            count += 1
        conn.execute(
            "UPDATE sources SET published_seq=? WHERE source_id=?",
            (baseline_seq, source_id),
        )
        conn.execute(
            "INSERT INTO cuts(source_id, cut_seq) VALUES (?,?)",
            (source_id, baseline_seq),
        )
        conn.execute(
            "INSERT INTO events(source_id, seq, kind, detail) VALUES (?,?,?,?)",
            (source_id, baseline_seq, "baseline_imported",
             json.dumps({"resources": count}))
        )


def _publish_transaction(paths: RepoPaths, conn: sqlite3.Connection,
                         config: RepoConfig, source_id: int,
                         source_server: str, tenant: str, seq: int,
                         shard_rows: list[sqlite3.Row]) -> None:
    alias_bases = config.sources[source_server]["aliases"]
    material_paths: list[Path] = []
    for shard in shard_rows:
        material = _material_shard_path(
            paths, shard["shard_relpath"], source_server, tenant, seq,
            expected_digest=shard["expected_digest"])
        # Defence in depth at the publish boundary.
        digest, count = stream_jsonl_digest_and_count(material)
        if digest != shard["expected_digest"]:
            raise ShardError(
                f"txn {seq} shard {shard['shard_relpath']!r}: digest changed "
                "between staging and publication"
            )
        if count != shard["expected_count"]:
            raise ShardError(
                f"txn {seq} shard {shard['shard_relpath']!r}: count changed "
                "between staging and publication"
            )
        material_paths.append(material)
    with write_tx(conn):
        seen_ordinals: set[int] = set()
        for shard_path in material_paths:
            for row in iter_jsonl(shard_path):
                validate_operation_row(row, seq)
                if row["ordinal"] in seen_ordinals:
                    raise TransactionConflict(
                        f"transaction {seq}: duplicate ordinal {row['ordinal']} "
                        "across shards"
                    )
                seen_ordinals.add(row["ordinal"])
                if row["method"] == "PUT":
                    body = validate_put_body(row)
                    _insert_put_fact(paths, conn, source_id, seq,
                                     row["ordinal"], body, alias_bases,
                                     origin="txn", envelope=row)
                else:
                    validate_delete_row(row)
                    _insert_delete_fact(conn, source_id, seq, row)
        conn.execute(
            "UPDATE transactions SET state='published', "
            "published_at=datetime('now') WHERE source_id=? AND seq=?",
            (source_id, seq),
        )
        conn.execute(
            "UPDATE sources SET published_seq=? WHERE source_id=?",
            (seq, source_id),
        )
        conn.execute(
            "INSERT INTO cuts(source_id, cut_seq) VALUES (?,?)",
            (source_id, seq),
        )
        conn.execute(
            "INSERT INTO events(source_id, seq, kind, detail) VALUES (?,?,?,?)",
            (source_id, seq, "txn_published",
             json.dumps({"operations": len(seen_ordinals)}))
        )


def _material_shard_path(paths: RepoPaths, stored_relpath: str,
                         source_server: str, tenant: str, seq: int,
                         expected_digest: str | None = None) -> Path:
    """Shard paths in the DB are relative to the source packages/ root.

    Multiple deliveries can contain the same relative path with different
    bytes, so when an expected digest is known the material is chosen by
    content, never by directory traversal order.
    """

    direct = [
        paths.root / stored_relpath,
        paths.source_root(source_server, tenant) / "packages" / stored_relpath,
    ]
    for candidate in direct:
        if candidate.is_file() and (
                expected_digest is None
                or stream_file_digest(candidate) == expected_digest):
            return candidate
    packages = paths.source_root(source_server, tenant) / "packages"
    if packages.is_dir():
        needle = Path(stored_relpath).name
        matching_digest: Path | None = None
        fallback: Path | None = None
        for found in packages.rglob(needle):
            if not found.is_file():
                continue
            if expected_digest is not None:
                if stream_file_digest(found) == expected_digest:
                    matching_digest = found
                    break
            elif fallback is None:
                fallback = found
        chosen = matching_digest or fallback
        if chosen is not None:
            return chosen
    raise ShardError(
        f"shard material not found for {stored_relpath!r}; re-deliver the package"
    )


def _insert_put_fact(paths: RepoPaths, conn: sqlite3.Connection, source_id: int,
                     seq: int, ordinal: int, body: dict[str, Any],
                     alias_bases: set[str], origin: str,
                     envelope: dict[str, Any] | None = None) -> None:
    rtype = body["resourceType"]
    logical_id = str(body["id"])
    meta = body.get("meta") if isinstance(body.get("meta"), dict) else {}
    version_id = meta["versionId"]
    last_updated = meta.get("lastUpdated")
    digest, relpath = blobs.add_blob(paths, body)
    conn.execute(
        "INSERT INTO resource_version(source_id, seq, tx_ordinal, resource_type, "
        "logical_id, version_id, method, is_deleted, body_digest, body_relpath, "
        "last_updated) VALUES (?,?,?,?,?,?,?,0,?,?,?)",
        (source_id, seq, ordinal, rtype, logical_id, version_id, "PUT",
         digest, relpath, last_updated),
    )
    for edge in extract_edges(body, alias_bases):
        conn.execute(
            "INSERT INTO ref_edges(source_id, seq, tx_ordinal, owner_type, "
            "owner_id, owner_version, container, loc, ref_text, kind, versioned, "
            "target_type, target_id, target_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (source_id, seq, ordinal, rtype, logical_id, version_id,
             edge.container, edge.loc, edge.ref_text, edge.shape.kind,
             1 if edge.shape.versioned else 0, edge.shape.target_type,
             edge.shape.target_id, edge.shape.target_version),
        )
    contained = body.get("contained")
    if isinstance(contained, list):
        for item in contained:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                conn.execute(
                    "INSERT OR IGNORE INTO contained_resources(source_id, "
                    "owner_type, owner_id, owner_version, contained_id) "
                    "VALUES (?,?,?,?,?)",
                    (source_id, rtype, logical_id, version_id, item["id"]),
                )


def _insert_delete_fact(conn: sqlite3.Connection, source_id: int, seq: int,
                        row: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO resource_version(source_id, seq, tx_ordinal, resource_type, "
        "logical_id, version_id, method, is_deleted, body_digest, body_relpath, "
        "last_updated) VALUES (?,?,?,?,?,?,?,1,NULL,NULL,NULL)",
        (source_id, seq, row["ordinal"], row["resourceType"], row["id"],
         row["versionId"], "DELETE"),
    )
