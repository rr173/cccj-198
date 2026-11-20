"""Cross-source patient identity dimension for the history warehouse.

The ingest fact tables keep their *source-local* identity
``(source_server, tenant, resourceType, id)`` and the existing reference
rules are untouched. This module adds an additive, independent dimension
that answers: *given a published dimension release, a business date and a
source key, which patient is this?*

Concepts
--------

* A **release** is a caller-numbered, immutable publication. It pins one
  complete cut vector ``{(source_server, tenant): cut}``, a fixed half-open
  business-date range ``[start, end)``, a parent release (``None`` for the
  first) and an optional correction batch of adjudications.
* An **adjudication** (``set``) has an id, a strictly increasing positive
  revision, two complete keys of participating sources, ``same`` or
  ``different``, and a non-empty date interval inside the release range.
  ``retract`` removes the parent release's effective adjudication. A new
  ``set`` revision replaces the whole previous revision (endpoints, kind and
  interval may all change); old intervals of the old revision stop applying.
* **Availability** of a Patient comes only from the repository fact
  ``resource_version`` at the pinned cut vector: the last row is a PUT
  (``available``), a DELETE (``deleted``) or there is no row (``absent``).
  An available PUT takes part in matching for every business date of the
  release. Tombstones and never-seen ids stay distinguishable. ``first_seen``
  (earliest ``meta.lastUpdated`` date at or before the cut) is reported on
  evidence for explanation only and never gates grouping.
* For one business day, available keys connected transitively by active
  ``same`` adjudications form an undirected group (an isolated key is a
  singleton). If an active ``different`` adjudication connects two keys of
  one group, the *whole* group is a conflict and none of its members gets a
  unified key. Conflict-free groups get the lexicographically smallest
  member key (UTF-8 bytes compared field by field) encoded as a three
  element JSON array ``["<source_server>","<tenant>","<id>"]`` — injective,
  so two groups never share an encoding. All rows are computed per day, but
  only for the groups touched by the change; untouched groups are copied
  from the parent release.

Only synthetic, manually adjudicated data is used: nothing here performs
patient-entity matching, and the dimension never changes the source
reference rules or the ingest fact tables.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Iterable

from .db import ensure_identity_schema
from .errors import (
    IdentityDateOutOfRange,
    IdentityReleaseRejected,
    KeyNotParticipating,
    UnknownDecision,
    UnknownIdentityRelease,
    UnknownSource,
)

Key = tuple[int, str]  # (source_id, logical_id)
TextKey = tuple[str, str, str]  # (source_server, tenant, logical_id)


# ---------------------------------------------------------------------------
# Key encoding and ordering
# ---------------------------------------------------------------------------

def encode_key(source_server: str, tenant: str, logical_id: str) -> str:
    """Unambiguous unified-key encoding (a three-element JSON array)."""

    return json.dumps([source_server, tenant, logical_id], ensure_ascii=False,
                      separators=(",", ":"))


def field_sort_key(source_server: str, tenant: str, logical_id: str
                   ) -> tuple[bytes, bytes, bytes]:
    """UTF-8 bytes compared field by field (the required minimum key)."""

    return (source_server.encode("utf-8"), tenant.encode("utf-8"),
            logical_id.encode("utf-8"))


def _parse_date(text: Any, field: str) -> date:
    if not isinstance(text, str) or len(text) != 10:
        raise IdentityReleaseRejected(
            f"{field} must be a YYYY-MM-DD date, got {text!r}")
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise IdentityReleaseRejected(
            f"{field} must be a YYYY-MM-DD date, got {text!r}") from exc
    if parsed.isoformat() != text:
        raise IdentityReleaseRejected(
            f"{field} must be a canonical YYYY-MM-DD date, got {text!r}")
    return parsed


def _date_range(start: Any, end: Any) -> list[str]:
    d0, d1 = _parse_date(start, "date_start"), _parse_date(end, "date_end")
    if not d0 < d1:
        raise IdentityReleaseRejected(
            f"date range must be a non-empty [start, end), got {start}..{end}")
    return list(_iter_days(d0, d1))


def _iter_days(d0: date, d1: date):
    """Stream business dates as ISO strings without materialising the range."""

    d = d0
    while d < d1:
        yield d.isoformat()
        d += timedelta(days=1)


def _day_after(day: str) -> str:
    return (date.fromisoformat(day) + timedelta(days=1)).isoformat()


# ---------------------------------------------------------------------------
# Request normalisation
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SourceCut:
    source_server: str
    tenant: str
    cut: int


@dataclass(frozen=True)
class Correction:
    decision_id: str
    revision: int
    op: str
    kind: str | None = None
    left: TextKey | None = None
    right: TextKey | None = None
    date_start: str | None = None
    date_end: str | None = None

    def sort_payload(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id, "revision": self.revision,
            "op": self.op, "kind": self.kind,
            "left": list(self.left) if self.left else None,
            "right": list(self.right) if self.right else None,
            "date_start": self.date_start, "date_end": self.date_end,
        }


def _norm_text_key(raw: Any, field: str) -> TextKey:
    if isinstance(raw, dict):
        server = raw.get("source_server")
        tenant = raw.get("tenant")
        pid = raw.get("id")
    elif isinstance(raw, (list, tuple)) and len(raw) == 3:
        server, tenant, pid = raw
    else:
        raise IdentityReleaseRejected(
            f"{field} must be an object with source_server/tenant/id or a "
            f"3-element array, got {raw!r}")
    if not all(isinstance(v, str) and v != "" for v in (server, tenant, pid)):
        raise IdentityReleaseRejected(f"{field} fields must be non-empty strings")
    return (server, tenant, pid)


def normalise_corrections(raw_rows: Iterable[dict[str, Any]]) -> list[Correction]:
    """Validate the correction batch (one JSON object per line) as a unit."""

    rows = list(raw_rows)
    seen: set[str] = set()
    out: list[Correction] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise IdentityReleaseRejected(
                f"correction line {index} is not a JSON object")
        did = row.get("decision_id")
        if not isinstance(did, str) or did == "":
            raise IdentityReleaseRejected(
                f"correction line {index}: decision_id must be a non-empty string")
        if did in seen:
            raise IdentityReleaseRejected(
                f"correction batch has more than one row for decision {did!r}")
        seen.add(did)
        revision = row.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) \
                or revision <= 0:
            raise IdentityReleaseRejected(
                f"correction {did!r}: revision must be a positive integer")
        op = row.get("op")
        if op == "retract":
            extra = set(row) - {"decision_id", "revision", "op"}
            if extra:
                raise IdentityReleaseRejected(
                    f"retract {did!r} only takes decision_id/revision/op; "
                    f"unexpected {sorted(extra)}")
            out.append(Correction(did, revision, "retract"))
            continue
        if op != "set":
            raise IdentityReleaseRejected(
                f"correction {did!r}: op must be 'set' or 'retract', got {op!r}")
        kind = row.get("kind")
        if kind not in ("same", "different"):
            raise IdentityReleaseRejected(
                f"correction {did!r}: kind must be 'same' or 'different'")
        if "left" not in row or "right" not in row:
            raise IdentityReleaseRejected(
                f"correction {did!r}: set needs left and right keys")
        left = _norm_text_key(row["left"], f"correction {did}.left")
        right = _norm_text_key(row["right"], f"correction {did}.right")
        if left == right:
            raise IdentityReleaseRejected(
                f"correction {did!r}: the two keys must be different")
        interval = row.get("interval")
        if interval is None:
            interval = {"start": row.get("start"), "end": row.get("end")}
        if not isinstance(interval, dict):
            raise IdentityReleaseRejected(
                f"correction {did!r}: interval must be an object")
        start, end = interval.get("start"), interval.get("end")
        if not isinstance(start, str) or not isinstance(end, str):
            raise IdentityReleaseRejected(
                f"correction {did!r}: interval.start/end must be YYYY-MM-DD")
        out.append(Correction(did, revision, "set", kind, left, right,
                              start, end))
    return out


def _norm_sources(raw_sources: list[dict[str, Any]]) -> list[SourceCut]:
    if not isinstance(raw_sources, list):
        raise IdentityReleaseRejected("sources must be a list")
    out: list[SourceCut] = []
    seen: set[tuple[str, str]] = set()
    for entry in raw_sources:
        if not isinstance(entry, dict):
            raise IdentityReleaseRejected("each source entry must be an object")
        server = entry.get("source_server")
        tenant = entry.get("tenant")
        cut = entry.get("cut")
        if not isinstance(server, str) or not isinstance(tenant, str) \
                or not isinstance(cut, int) or isinstance(cut, bool):
            raise IdentityReleaseRejected(
                "source entry needs source_server, tenant and integer cut")
        if (server, tenant) in seen:
            raise IdentityReleaseRejected(
                f"duplicate participating source {server} / {tenant}")
        seen.add((server, tenant))
        out.append(SourceCut(server, tenant, cut))
    if not out:
        raise IdentityReleaseRejected("at least one participating source required")
    return out


def _request_digest(release_no: int, parent: int | None,
                    sources: list[SourceCut], date_start: str, date_end: str,
                    corrections: list[Correction]) -> str:
    payload = {
        "format": "fhist-identity-release/1",
        "release_no": release_no,
        "parent": parent,
        "sources": sorted([[s.source_server, s.tenant, s.cut]
                           for s in sources]),
        "date_start": date_start,
        "date_end": date_end,
        "corrections": sorted((c.sort_payload() for c in corrections),
                              key=lambda c: c["decision_id"]),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Availability snapshots
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Snapshot:
    method: str | None
    version_id: str | None
    seq: int | None
    tx_ordinal: int | None
    availability: str
    present_from: str | None  # informational only (earliest PUT lastUpdated)

    def present_on(self, day: str) -> bool:
        # A key whose last Patient fact at the release cut is a PUT takes part
        # in matching for every business date of the release range.
        return self.availability == "available"

    def to_dict(self) -> dict[str, Any]:
        return {"availability": self.availability, "method": self.method,
                "version_id": self.version_id, "seq": self.seq,
                "tx_ordinal": self.tx_ordinal,
                "first_seen": self.present_from}


_MISSING_SNAPSHOT = Snapshot(None, None, None, None, "absent", None)


def _snapshot_for(conn: sqlite3.Connection, source_id: int,
                  cut_seq: int) -> dict[str, Snapshot]:
    rows = conn.execute(
        """
        SELECT logical_id, method, version_id, seq, tx_ordinal, availability,
               present_from FROM (
          SELECT rv.logical_id, rv.method, rv.version_id, rv.seq,
                 rv.tx_ordinal,
                 CASE rv.method WHEN 'PUT' THEN 'available' ELSE 'deleted' END
                     AS availability,
                 (SELECT MIN(CASE WHEN p.method='PUT'
                                  AND p.last_updated IS NOT NULL
                             THEN substr(p.last_updated, 1, 10) END)
                  FROM resource_version p
                  WHERE p.source_id=rv.source_id
                    AND p.resource_type='Patient'
                    AND p.logical_id=rv.logical_id
                    AND p.seq<=?) AS present_from,
                 ROW_NUMBER() OVER (
                     PARTITION BY rv.logical_id
                     ORDER BY rv.seq DESC, rv.tx_ordinal DESC) rn
          FROM resource_version rv
          WHERE rv.source_id=? AND rv.resource_type='Patient' AND rv.seq<=?
        ) WHERE rn=1
        """,
        (cut_seq, source_id, cut_seq),
    ).fetchall()
    return {r["logical_id"]: Snapshot(
        method=r["method"], version_id=r["version_id"], seq=r["seq"],
        tx_ordinal=r["tx_ordinal"], availability=r["availability"],
        present_from=r["present_from"]) for r in rows}


def _seen_patient_versions(conn: sqlite3.Connection, source_id: int,
                           cut_seq: int, after_seq: int | None) -> int:
    if after_seq is None:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM resource_version WHERE source_id=? "
            "AND resource_type='Patient' AND seq<=?",
            (source_id, cut_seq)).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM resource_version WHERE source_id=? "
            "AND resource_type='Patient' AND seq>? AND seq<=?",
            (source_id, after_seq, cut_seq)).fetchone()
    return int(row["n"])


# ---------------------------------------------------------------------------
# Release metadata
# ---------------------------------------------------------------------------

@dataclass
class Release:
    release_no: int
    parent_no: int | None
    date_start: str
    date_end: str
    cuts: dict[int, int]                       # source_id -> cut
    sources: dict[int, tuple[str, str]]        # source_id -> coordinate

    def text_of(self, source_id: int) -> tuple[str, str]:
        return self.sources[source_id]

    def in_range(self, day: str) -> bool:
        return self.date_start <= day < self.date_end


def load_release(conn: sqlite3.Connection, release_no: int) -> Release:
    row = conn.execute("SELECT * FROM identity_release WHERE release_no=?",
                       (release_no,)).fetchone()
    if row is None:
        raise UnknownIdentityRelease(f"identity release {release_no} not found")
    sources: dict[int, tuple[str, str]] = {}
    cuts: dict[int, int] = {}
    for r in conn.execute(
            "SELECT s.source_id, s.source_server, s.tenant, i.cut_seq "
            "FROM idr_source i JOIN sources s ON s.source_id=i.source_id "
            "WHERE i.release_no=? ORDER BY s.source_id", (release_no,)):
        sources[int(r["source_id"])] = (r["source_server"], r["tenant"])
        cuts[int(r["source_id"])] = int(r["cut_seq"])
    return Release(release_no=int(row["release_no"]), parent_no=row["parent_no"],
                   date_start=row["date_start"], date_end=row["date_end"],
                   cuts=cuts, sources=sources)


def _load_patients(conn: sqlite3.Connection, release_no: int | None,
                   source_id: int) -> dict[str, Snapshot]:
    if release_no is None:
        return {}
    return {r["logical_id"]: Snapshot(
        method=r["method"], version_id=r["version_id"], seq=r["seq"],
        tx_ordinal=r["tx_ordinal"], availability=r["availability"],
        present_from=r["present_from"])
        for r in conn.execute(
            "SELECT * FROM idr_patient WHERE release_no=? AND source_id=?",
            (release_no, source_id))}


# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------

def publish(conn: sqlite3.Connection, *, release_no: int,
            parent: int | None, sources: list[dict[str, Any]],
            date_start: str, date_end: str,
            corrections: list[dict[str, Any]],
            interrupt: str | None = None) -> dict[str, Any]:
    """Validate and publish one dimension release atomically.

    A repeated publication number with the same semantic input folds onto
    the original release (even when the head moved on); different content
    under an existing number is refused. Nothing becomes visible before the
    header, cuts, patient view, decisions, groups, reverse index and stats
    are committed together in one ``BEGIN IMMEDIATE`` transaction.
    """

    if not isinstance(release_no, int) or isinstance(release_no, bool) \
            or release_no <= 0:
        raise IdentityReleaseRejected(
            "release number must be a positive integer")
    if parent is not None and (not isinstance(parent, int)
                               or isinstance(parent, bool) or parent <= 0):
        raise IdentityReleaseRejected("parent must be a positive integer or null")
    cuts_spec = _norm_sources(sources)
    corrections = normalise_corrections(corrections)
    digest = _request_digest(release_no, parent, cuts_spec, date_start,
                             date_end, corrections)

    ensure_identity_schema(conn)

    # Fast path: identical re-publication folds without taking the write lock.
    existing = conn.execute(
        "SELECT * FROM identity_release WHERE release_no=?",
        (release_no,)).fetchone()
    if existing is not None:
        if existing["request_digest"] == digest:
            info = release_info(conn, release_no)
            info["status"] = "folded"
            return info
        raise IdentityReleaseRejected(
            f"release {release_no} already exists with different content; "
            "published releases are never rewritten")

    conn.execute("BEGIN IMMEDIATE")
    try:
        result = _publish_locked(conn, release_no, parent, cuts_spec,
                                 date_start, date_end, corrections, digest)
        if interrupt == "exit":
            # Test hook: die after every row is written but before COMMIT.
            # WAL discards the uncommitted transaction; nothing is visible.
            import os
            os._exit(17)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return result


def _publish_locked(conn: sqlite3.Connection, release_no: int,
                    parent: int | None, cuts_spec: list[SourceCut],
                    date_start: str, date_end: str,
                    corrections: list[Correction], digest: str
                    ) -> dict[str, Any]:
    _date_range(date_start, date_end)  # re-validate the half-open range

    # -- parent chain (re-read inside the write lock) ----------------------
    # A release number is unique across the repository. A null parent starts
    # a new independent lineage; a non-null parent must be the head of its
    # lineage (no successor yet), so two competing publications from the same
    # parent can never both become successors.
    if conn.execute("SELECT 1 FROM identity_release WHERE release_no=?",
                    (release_no,)).fetchone() is not None:
        raise IdentityReleaseRejected(
            f"release number {release_no} is already used")
    previous: Release | None = None
    if parent is not None:
        row = conn.execute(
            "SELECT 1 FROM identity_release WHERE release_no=?", (parent,)
        ).fetchone()
        if row is None:
            raise IdentityReleaseRejected(
                f"parent release {parent} does not exist")
        child = conn.execute(
            "SELECT release_no FROM identity_release WHERE parent_no=? "
            "ORDER BY release_no LIMIT 1", (parent,)).fetchone()
        if child is not None:
            raise IdentityReleaseRejected(
                f"parent {parent} already has successor {child['release_no']}; "
                "a stale parent cannot be published")
        previous = load_release(conn, parent)
        if previous.date_start != date_start or previous.date_end != date_end:
            raise IdentityReleaseRejected(
                "a lineage keeps one fixed business-date range "
                f"[{previous.date_start}, {previous.date_end})")

    # -- participating sources and complete cut vector ---------------------
    source_ids: list[int] = []
    seen_coords: set[tuple[str, str]] = set()
    for spec in cuts_spec:
        row = conn.execute(
            "SELECT * FROM sources WHERE source_server=? AND tenant=?",
            (spec.source_server, spec.tenant)).fetchone()
        if row is None:
            raise UnknownSource(
                f"{spec.source_server} / {spec.tenant} is not registered")
        sid = int(row["source_id"])
        if conn.execute("SELECT 1 FROM cuts WHERE source_id=? AND cut_seq=?",
                        (sid, spec.cut)).fetchone() is None:
            raise IdentityReleaseRejected(
                f"cut {spec.cut} of {spec.source_server} / {spec.tenant} is "
                "not a published cut")
        source_ids.append(sid)
        seen_coords.add((spec.source_server, spec.tenant))
    if previous is not None:
        if set(previous.sources.values()) != seen_coords:
            raise IdentityReleaseRejected(
                "a lineage keeps one fixed participating (source_server, "
                "tenant) set")
        for sid, spec in zip(source_ids, cuts_spec):
            if spec.cut < previous.cuts[sid]:
                raise IdentityReleaseRejected(
                    f"cut of {spec.source_server} / {spec.tenant} moved "
                    f"backwards from {previous.cuts[sid]} to {spec.cut}")
    new_cuts = {sid: spec.cut for sid, spec in zip(source_ids, cuts_spec)}
    coord_of: dict[int, tuple[str, str]] = {
        sid: (spec.source_server, spec.tenant)
        for sid, spec in zip(source_ids, cuts_spec)}

    # -- participant snapshots and change set ------------------------------
    snapshots: dict[int, dict[str, Snapshot]] = {}
    changed_keys: set[Key] = set()
    worklog: list[dict[str, Any]] = []
    for sid, spec in zip(source_ids, cuts_spec):
        if previous is not None and previous.cuts[sid] == spec.cut:
            snapshots[sid] = _load_patients(conn, previous.release_no, sid)
            changed: set[Key] = set()
        else:
            snapshots[sid] = _snapshot_for(conn, sid, spec.cut)
            if previous is None:
                changed = {(sid, lid) for lid in snapshots[sid]}
            else:
                old = _load_patients(conn, previous.release_no, sid)
                changed = {(sid, lid) for lid in set(old) | set(snapshots[sid])
                           if old.get(lid) != snapshots[sid].get(lid)}
        changed_keys |= changed
        worklog.append({
            "source_id": sid,
            "source_server": spec.source_server, "tenant": spec.tenant,
            "cut": spec.cut,
            "seen_versions": _seen_patient_versions(
                conn, sid, spec.cut,
                previous.cuts[sid] if previous is not None else None),
            "active_versions": sum(1 for s in snapshots[sid].values()
                                   if s.availability == "available"),
            "changed_keys": len(changed),
        })

    # -- adjudication batch against the parent -----------------------------
    parent_decisions = _effective_decisions(conn, parent)
    effective, changed_decisions = _apply_corrections(
        parent_decisions, corrections,
        {(s.source_server, s.tenant): sid
         for sid, s in zip(source_ids, cuts_spec)},
        date_start, date_end)
    affected_keys = _affected_keys(parent_decisions, effective,
                                   changed_decisions)

    # -- write the release header / source cuts / patient view -------------
    conn.execute(
        "INSERT INTO identity_release(release_no, release_id, parent_no, "
        "parent_id, cuts_json, date_start, date_end, correction_json, "
        "request_digest) VALUES (?,?,?,?,?,?,?,?,?)",
        (release_no, digest, parent,
         f"rel-{parent}" if parent is not None else None,
         json.dumps({str(k): v for k, v in sorted(new_cuts.items())}),
         date_start, date_end,
         json.dumps([c.sort_payload() for c in corrections],
                    ensure_ascii=False, sort_keys=True), digest))
    for sid, cut in sorted(new_cuts.items()):
        conn.execute(
            "INSERT INTO idr_source(release_no, source_id, cut_seq) "
            "VALUES (?,?,?)", (release_no, sid, cut))
    for sid in sorted(snapshots):
        if previous is not None and previous.cuts.get(sid) == new_cuts[sid]:
            conn.execute(
                "INSERT INTO idr_patient(release_no, source_id, logical_id, "
                "method, version_id, seq, tx_ordinal, availability, "
                "present_from) SELECT ?, source_id, logical_id, method, "
                "version_id, seq, tx_ordinal, availability, present_from "
                "FROM idr_patient WHERE release_no=? AND source_id=?",
                (release_no, previous.release_no, sid))
        else:
            for lid, snap in snapshots[sid].items():
                conn.execute(
                    "INSERT INTO idr_patient(release_no, source_id, logical_id, "
                    "method, version_id, seq, tx_ordinal, availability, "
                    "present_from) VALUES (?,?,?,?,?,?,?,?,?)",
                    (release_no, sid, lid, snap.method, snap.version_id,
                     snap.seq, snap.tx_ordinal, snap.availability,
                     snap.present_from))
    for dec in effective.values():
        conn.execute(
            "INSERT INTO idr_decision(release_no, decision_id, revision, op, "
            "kind, left_source, left_patient, right_source, right_patient, "
            "date_start, date_end) VALUES (?,?,?,'set',?,?,?,?,?,?,?)",
            (release_no, dec["decision_id"], dec["revision"], dec["kind"],
             dec["left_source"], dec["left_patient"], dec["right_source"],
             dec["right_patient"], dec["date_start"], dec["date_end"]))
    for did in sorted(changed_decisions):
        rev = effective[did]["revision"] if did in effective else 0
        conn.execute(
            "INSERT INTO idr_decision_change(release_no, decision_id, revision) "
            "VALUES (?,?,?)", (release_no, did, rev))

    # -- per-day assignment ------------------------------------------------
    stats = _build_days(conn, release_no, previous, date_start, date_end,
                        snapshots, changed_keys, affected_keys,
                        changed_decisions, coord_of)
    for entry in worklog:
        conn.execute(
            "INSERT INTO idr_worklog(release_no, source_id, cut_seq, "
            "seen_versions, active_versions, changed) VALUES (?,?,?,?,?,?)",
            (release_no, entry["source_id"], entry["cut"], 
             entry["seen_versions"], entry["active_versions"],
             entry["changed_keys"]))
    stats["sources"] = worklog
    stats["decisions_total"] = len(effective)
    stats["decisions_changed"] = len(changed_decisions)
    conn.execute("UPDATE identity_release SET stats_json=? WHERE release_no=?",
                 (json.dumps(stats, sort_keys=True), release_no))
    info = release_info(conn, release_no)
    info["status"] = "published"
    return info


def _effective_decisions(conn: sqlite3.Connection, release_no: int | None
                         ) -> dict[str, dict[str, Any]]:
    if release_no is None:
        return {}
    return {r["decision_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM idr_decision WHERE release_no=?", (release_no,))}


def _apply_corrections(parent: dict[str, dict[str, Any]],
                       corrections: list[Correction],
                       source_ids: dict[tuple[str, str], int],
                       date_start: str, date_end: str
                       ) -> tuple[dict[str, dict[str, Any]], set[str]]:
    effective = {k: dict(v) for k, v in parent.items()}
    changed: set[str] = set()
    for corr in corrections:
        existing = effective.get(corr.decision_id)
        if existing is not None and corr.revision <= existing["revision"]:
            raise IdentityReleaseRejected(
                f"correction {corr.decision_id!r}: revision {corr.revision} "
                f"is not newer than the effective revision "
                f"{existing['revision']}")
        if corr.op == "retract":
            if existing is None:
                raise IdentityReleaseRejected(
                    f"retract {corr.decision_id!r}: no effective decision in "
                    "the parent release")
            del effective[corr.decision_id]
            changed.add(corr.decision_id)
            continue
        for side, key in (("left", corr.left), ("right", corr.right)):
            assert key is not None
            if (key[0], key[1]) not in source_ids:
                raise IdentityReleaseRejected(
                    f"correction {corr.decision_id!r}.{side} references a "
                    f"source that does not participate: {key[0]} / {key[1]}")
        start, end = corr.date_start, corr.date_end
        assert start is not None and end is not None
        _parse_date(start, f"correction {corr.decision_id}.interval.start")
        _parse_date(end, f"correction {corr.decision_id}.interval.end")
        if not start < end:
            raise IdentityReleaseRejected(
                f"correction {corr.decision_id!r}: interval must be a "
                f"non-empty [start, end), got {start}..{end}")
        if not (date_start <= start and end <= date_end):
            raise IdentityReleaseRejected(
                f"correction {corr.decision_id!r}: interval {start}..{end} "
                f"is not inside the dimension range [{date_start}, {date_end})")
        assert corr.left is not None and corr.right is not None
        effective[corr.decision_id] = {
            "decision_id": corr.decision_id, "revision": corr.revision,
            "op": "set", "kind": corr.kind,
            "left_source": source_ids[(corr.left[0], corr.left[1])],
            "left_patient": corr.left[2],
            "right_source": source_ids[(corr.right[0], corr.right[1])],
            "right_patient": corr.right[2],
            "date_start": start, "date_end": end,
        }
        changed.add(corr.decision_id)
    return effective, changed


def _affected_keys(parent: dict[str, dict[str, Any]],
                   effective: dict[str, dict[str, Any]],
                   changed: set[str]) -> set[Key]:
    out: set[Key] = set()
    for did in changed:
        for dec in (parent.get(did), effective.get(did)):
            if dec is None:
                continue
            out.add((dec["left_source"], dec["left_patient"]))
            out.add((dec["right_source"], dec["right_patient"]))
    return out


# ---------------------------------------------------------------------------
# Per-day assignment
# ---------------------------------------------------------------------------

def _build_days(conn: sqlite3.Connection, release_no: int,
                previous: Release | None, date_start: str, date_end: str,
                snapshots: dict[int, dict[str, Snapshot]],
                changed_keys: set[Key], affected_keys: set[Key],
                changed_decisions: set[str],
                coord_of: dict[int, tuple[str, str]]) -> dict[str, Any]:
    trigger_keys = changed_keys | affected_keys
    day_count = 0
    # Business dates are streamed and each day's active adjudications are
    # re-read from SQLite: the working set is one day, never the whole range
    # or the whole adjudication set.
    for day in _iter_days(date.fromisoformat(date_start),
                          date.fromisoformat(date_end)):
        day_count += 1
        active = [dict(r) for r in conn.execute(
            "SELECT * FROM idr_decision WHERE release_no=? AND date_start<=? "
            "AND date_end>? ORDER BY decision_id", (release_no, day, day))]
        active_changed = [d for d in active
                          if d["decision_id"] in changed_decisions]
        parent_pairs: dict[Key, tuple[str, str | None]] = {}
        parent_groups: dict[str, int] = {}
        parent_available = False
        if previous is not None:
            for r in conn.execute(
                    "SELECT source_id, logical_id, group_key, unified_key "
                    "FROM idr_member WHERE release_no=? AND date_key=?",
                    (previous.release_no, day)):
                parent_pairs[(int(r["source_id"]), r["logical_id"])] = (
                    r["group_key"], r["unified_key"])
                parent_available = True
            for r in conn.execute(
                    "SELECT group_key, conflict FROM idr_group WHERE "
                    "release_no=? AND date_key=?", (previous.release_no, day)):
                parent_groups[r["group_key"]] = int(r["conflict"])

        day_changed = previous is None or not parent_available \
            or bool(active_changed)
        if not day_changed:
            for key in trigger_keys:
                snap = snapshots.get(key[0], {}).get(key[1], _MISSING_SNAPSHOT)
                if snap.present_on(day) or key in parent_pairs:
                    day_changed = True
                    break
        if not day_changed:
            conn.execute(
                "INSERT INTO idr_member(release_no, date_key, source_id, "
                "logical_id, group_key, unified_key, parent_group_key, "
                "parent_unified_key, recomputed) "
                "SELECT ?, date_key, source_id, logical_id, group_key, "
                "unified_key, group_key, unified_key, 0 FROM idr_member "
                "WHERE release_no=? AND date_key=?",
                (release_no, previous.release_no, day))
            conn.execute(
                "INSERT INTO idr_group(release_no, date_key, group_key, "
                "unified_key, conflict) SELECT ?, date_key, group_key, "
                "unified_key, conflict FROM idr_group WHERE release_no=? "
                "AND date_key=?", (release_no, previous.release_no, day))
            row = conn.execute(
                "SELECT COUNT(*) AS members, COUNT(DISTINCT group_key) AS "
                "groups, COALESCE(SUM(conflict),0) AS conflicts FROM "
                "idr_group WHERE release_no=? AND date_key=?",
                (release_no, day)).fetchone()
            _record_day(conn, release_no, day, changed=0,
                        group_count=row["groups"],
                        member_count=row["members"],
                        conflict_count=row["conflicts"],
                        recomputed=0, reused=row["members"])
            continue
        stat = _recompute_day(
            conn, release_no, previous, day, parent_pairs, parent_groups,
            snapshots, changed_keys, trigger_keys, active, coord_of)
        _record_day(conn, release_no, day, changed=1,
                    group_count=stat["group_count"],
                    member_count=stat["member_count"],
                    conflict_count=stat["conflict_count"],
                    recomputed=stat["recomputed_keys"],
                    reused=stat["reused_keys"])

    total_recomputed = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT DISTINCT source_id, logical_id "
        "FROM idr_member WHERE release_no=? AND recomputed=1)",
        (release_no,)).fetchone()["n"]
    total_reused = conn.execute(
        "SELECT COUNT(*) AS n FROM (SELECT DISTINCT source_id, logical_id "
        "FROM idr_member WHERE release_no=? AND recomputed=0 EXCEPT SELECT "
        "source_id, logical_id FROM idr_member WHERE release_no=? AND "
        "recomputed=1)", (release_no, release_no)).fetchone()["n"]
    changed_days = conn.execute(
        "SELECT COUNT(*) AS n FROM idr_day WHERE release_no=? AND changed=1",
        (release_no,)).fetchone()["n"]
    return {
        "days_total": day_count,
        "days_changed": int(changed_days),
        "recomputed_keys": int(total_recomputed),
        "reused_keys": int(total_reused),
        "membership_rows": conn.execute(
            "SELECT COUNT(*) AS n FROM idr_member WHERE release_no=?",
            (release_no,)).fetchone()["n"],
        "days": [dict(r) for r in conn.execute(
            "SELECT date_key AS date, changed, group_count, member_count, "
            "conflict_count, recomputed_keys, reused_keys FROM idr_day "
            "WHERE release_no=? ORDER BY date_key", (release_no,))],
    }


def _record_day(conn: sqlite3.Connection, release_no: int, day: str, *,
                changed: int, group_count: int, member_count: int,
                conflict_count: int, recomputed: int, reused: int) -> None:
    conn.execute(
        "INSERT INTO idr_day(release_no, date_key, changed, group_count, "
        "member_count, conflict_count, seen_versions, recomputed_keys, "
        "reused_keys) VALUES (?,?,?,?,?,?,0,?,?)",
        (release_no, day, changed, group_count, member_count, conflict_count,
         recomputed, reused))


def _recompute_day(conn: sqlite3.Connection, release_no: int,
                   previous: Release | None, day: str,
                   parent_pairs: dict[Key, tuple[str, str | None]],
                   parent_groups: dict[str, int],
                   snapshots: dict[int, dict[str, Snapshot]],
                   changed_keys: set[Key], trigger_keys: set[Key],
                   active: list[dict[str, Any]],
                   coord_of: dict[int, tuple[str, str]]) -> dict[str, Any]:
    def present(key: Key) -> bool:
        snap = snapshots.get(key[0], {}).get(key[1])
        return snap is not None and snap.present_on(day)

    def text(key: Key) -> TextKey:
        server, tenant = coord_of[key[0]]
        return (server, tenant, key[1])

    def sort_key(key: Key) -> tuple[bytes, bytes, bytes]:
        return field_sort_key(*text(key))

    group_members: dict[str, set[Key]] = {}
    key_group: dict[Key, str] = {}
    for key, (gkey, _unified) in parent_pairs.items():
        group_members.setdefault(gkey, set()).add(key)
        key_group[key] = gkey

    adjacency: dict[Key, list[Key]] = {}
    for dec in active:
        left = (dec["left_source"], dec["left_patient"])
        right = (dec["right_source"], dec["right_patient"])
        adjacency.setdefault(left, []).append(right)
        adjacency.setdefault(right, []).append(left)

    # Grow the touched component from changed/affected keys, pulling whole
    # parent groups and everything reachable through active adjudications.
    touched: set[Key] = set()
    pulled_groups: set[str] = set()
    queue: list[Key] = []

    def add_key(key: Key) -> None:
        if key in touched:
            return
        touched.add(key)
        queue.append(key)
        gkey = key_group.get(key)
        if gkey is not None and gkey not in pulled_groups:
            pulled_groups.add(gkey)
            for member in group_members.get(gkey, set()):
                if member not in touched:
                    touched.add(member)
                    queue.append(member)

    for key in sorted(trigger_keys):
        if key in parent_pairs or present(key):
            add_key(key)
    index = 0
    while index < len(queue):
        key = queue[index]
        index += 1
        for other in adjacency.get(key, []):
            if other not in touched and present(other):
                add_key(other)

    # Nodes: every *present* member of a touched (pulled) parent group and
    # every present changed/affected key is its own node. Any group that a
    # change touches is conservatively re-derived from active adjudications,
    # so losing a member (or a tombstone) can never leave a stale group.
    node_members: dict[str, set[Key]] = {}
    node_of_key: dict[Key, str] = {}
    for key in touched:
        if not present(key):
            continue
        node = f"k:{key[0]}:{key[1]}"
        node_members[node] = {key}
        node_of_key[key] = node

    parent_of = {node: node for node in node_members}

    def find(node: str) -> str:
        while parent_of[node] != node:
            parent_of[node] = parent_of[parent_of[node]]
            node = parent_of[node]
        return node

    different: list[tuple[str, str]] = []
    for dec in active:
        left = (dec["left_source"], dec["left_patient"])
        right = (dec["right_source"], dec["right_patient"])
        nl, nr = node_of_key.get(left), node_of_key.get(right)
        if nl is None or nr is None:
            continue
        if dec["kind"] == "same":
            ra, rb = find(nl), find(nr)
            if ra != rb:
                parent_of[rb] = ra
        else:
            different.append((nl, nr))
    conflicted_roots: set[str] = set()
    for a, b in different:
        if find(a) == find(b):
            conflicted_roots.add(find(a))

    roots: dict[str, set[Key]] = {}
    for node, members in node_members.items():
        roots.setdefault(find(node), set()).update(members)

    # Untouched parent groups and their member rows are copied verbatim.
    untouched = {k: v for k, v in parent_pairs.items() if k not in touched}
    for key, (gkey, unified) in untouched.items():
        conn.execute(
            "INSERT INTO idr_member(release_no, date_key, source_id, "
            "logical_id, group_key, unified_key, parent_group_key, "
            "parent_unified_key, recomputed) VALUES (?,?,?,?,?,?,?,?,0)",
            (release_no, day, key[0], key[1], gkey, unified, gkey, unified))
    copied_conflicts = 0
    for gkey, conflict in parent_groups.items():
        if gkey in pulled_groups:
            continue
        row = conn.execute(
            "SELECT unified_key FROM idr_group WHERE release_no=? AND "
            "date_key=? AND group_key=?",
            (previous.release_no, day, gkey)).fetchone()
        if row is not None:
            conn.execute(
                "INSERT INTO idr_group(release_no, date_key, group_key, "
                "unified_key, conflict) VALUES (?,?,?,?,?)",
                (release_no, day, gkey, row["unified_key"], conflict))
            copied_conflicts += conflict

    conflict_count = 0
    group_count = 0
    for root, members in roots.items():
        group_count += 1
        conflicted = root in conflicted_roots
        min_key = min(members, key=sort_key)
        group_key = encode_key(*text(min_key))
        unified = None if conflicted else group_key
        if conflicted:
            conflict_count += 1
        conn.execute(
            "INSERT INTO idr_group(release_no, date_key, group_key, "
            "unified_key, conflict) VALUES (?,?,?,?,?)",
            (release_no, day, group_key, unified, 1 if conflicted else 0))
        for key in sorted(members):
            old = parent_pairs.get(key)
            conn.execute(
                "INSERT INTO idr_member(release_no, date_key, source_id, "
                "logical_id, group_key, unified_key, parent_group_key, "
                "parent_unified_key, recomputed) VALUES (?,?,?,?,?,?,?,?,1)",
                (release_no, day, key[0], key[1], group_key, unified,
                 old[0] if old else None, old[1] if old else None))

    copied_groups = len(parent_groups) - len(pulled_groups)
    recomputed = sum(len(m) for m in roots.values())
    return {
        "date": day, "changed": 1,
        "group_count": group_count + copied_groups,
        "member_count": recomputed + len(untouched),
        "conflict_count": conflict_count + copied_conflicts,
        "recomputed_keys": recomputed, "reused_keys": len(untouched),
    }


# ---------------------------------------------------------------------------
# Read-side metadata
# ---------------------------------------------------------------------------

def release_info(conn: sqlite3.Connection, release_no: int) -> dict[str, Any]:
    rel = load_release(conn, release_no)
    row = conn.execute("SELECT * FROM identity_release WHERE release_no=?",
                       (release_no,)).fetchone()
    return {
        "release": release_no,
        "release_id": row["release_id"],
        "parent": rel.parent_no,
        "date_start": rel.date_start,
        "date_end": rel.date_end,
        "cuts": [
            {"source_server": rel.sources[sid][0],
             "tenant": rel.sources[sid][1], "cut": rel.cuts[sid]}
            for sid in sorted(rel.cuts, key=lambda s: rel.sources[s])],
        "corrections": json.loads(row["correction_json"] or "[]"),
        "stats": json.loads(row["stats_json"] or "{}"),
    }


def list_releases(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT release_no, parent_no, date_start, date_end, created_at "
        "FROM identity_release ORDER BY release_no").fetchall()
    return {"status": "ok", "releases": [dict(r) for r in rows]}


def stats(conn: sqlite3.Connection, release_no: int) -> dict[str, Any]:
    info = release_info(conn, release_no)
    return {"status": "ok", "release": release_no, "stats": info["stats"]}


# ---------------------------------------------------------------------------
# Read-side lookups
# ---------------------------------------------------------------------------

def _source_of(conn: sqlite3.Connection, server: str, tenant: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM sources WHERE source_server=? AND tenant=?",
        (server, tenant)).fetchone()
    if row is None:
        raise UnknownSource(f"no source registered for {server} / {tenant}")
    return row


def _participant(rel: Release, source_id: int) -> None:
    if source_id not in rel.cuts:
        raise KeyNotParticipating(
            "the source does not participate in this identity release")


def _snapshot_row(conn: sqlite3.Connection, release_no: int, source_id: int,
                  logical_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM idr_patient WHERE release_no=? AND source_id=? AND "
        "logical_id=?", (release_no, source_id, logical_id)).fetchone()


def _snapshot_of(row: sqlite3.Row) -> Snapshot:
    return Snapshot(row["method"], row["version_id"], row["seq"],
                    row["tx_ordinal"], row["availability"],
                    row["present_from"])


def _member_row(conn: sqlite3.Connection, release_no: int, day: str,
                source_id: int, logical_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT m.*, s.source_server, s.tenant FROM idr_member m "
        "JOIN sources s ON s.source_id=m.source_id WHERE m.release_no=? AND "
        "m.date_key=? AND m.source_id=? AND m.logical_id=?",
        (release_no, day, source_id, logical_id)).fetchone()


def _check_day(rel: Release, day: Any) -> str:
    _parse_date(day, "date")
    if not rel.in_range(day):
        raise IdentityDateOutOfRange(
            f"date {day} is outside release range "
            f"[{rel.date_start}, {rel.date_end})")
    return day


def assign(conn: sqlite3.Connection, release_no: int, source_server: str,
           tenant: str, logical_id: str, day: str) -> dict[str, Any]:
    """Resolve one source key's attribution on one business day."""

    rel = load_release(conn, release_no)
    _check_day(rel, day)
    source = _source_of(conn, source_server, tenant)
    sid = int(source["source_id"])
    _participant(rel, sid)
    out: dict[str, Any] = {
        "release": release_no, "date": day,
        "key": {"source_server": source_server, "tenant": tenant,
                "id": logical_id},
    }
    raw = _snapshot_row(conn, release_no, sid, logical_id)
    if raw is None:
        out.update(status="absent", availability="absent")
        return out
    snap = _snapshot_of(raw)
    out["availability"] = snap.availability
    out["version"] = snap.to_dict()
    if snap.availability == "deleted":
        out["status"] = "deleted"
        return out
    if snap.availability != "available":
        out["status"] = "absent"
        return out
    member = _member_row(conn, release_no, day, sid, logical_id)
    if member is None:  # defensive: available keys always have a row
        out["status"] = "absent"
        return out
    out["group_key"] = member["group_key"]
    out["unified_key"] = member["unified_key"]
    if member["unified_key"] is None:
        out["status"] = "conflict"
        out["conflict"] = True
    else:
        out["status"] = "assigned"
        out["conflict"] = False
    return out


def members_by_unified(conn: sqlite3.Connection, release_no: int,
                       unified_key: str, day: str) -> dict[str, Any]:
    rel = load_release(conn, release_no)
    _check_day(rel, day)
    rows = conn.execute(
        "SELECT m.source_id, m.logical_id, m.group_key, s.source_server, "
        "s.tenant FROM idr_member m JOIN sources s ON "
        "s.source_id=m.source_id WHERE m.release_no=? AND m.date_key=? AND "
        "m.unified_key=? ORDER BY s.source_server, s.tenant, m.logical_id",
        (release_no, day, unified_key)).fetchall()
    out = {"release": release_no, "date": day, "unified_key": unified_key}
    if rows:
        out.update(status="ok", group_key=rows[0]["group_key"], members=[
            {"source_server": r["source_server"], "tenant": r["tenant"],
             "id": r["logical_id"]} for r in rows])
        return out
    if conn.execute(
            "SELECT 1 FROM idr_group WHERE release_no=? AND date_key=? AND "
            "group_key=?", (release_no, day, unified_key)).fetchone() is not None:
        out.update(status="conflict", conflict=True, members=[])
    else:
        out.update(status="missing", members=[])
    return out


def status_range(conn: sqlite3.Connection, release_no: int,
                 source_server: str, tenant: str, logical_id: str,
                 date_from: str, date_to: str) -> dict[str, Any]:
    """Status/attribution runs over ``[from, to)``, merged by result."""

    rel = load_release(conn, release_no)
    days = _date_range(date_from, date_to)
    for day in (days[0], days[-1]):
        _check_day(rel, day)
    if days[-1] >= rel.date_end:
        raise IdentityDateOutOfRange(
            f"date {days[-1]} is outside release range "
            f"[{rel.date_start}, {rel.date_end})")
    source = _source_of(conn, source_server, tenant)
    sid = int(source["source_id"])
    _participant(rel, sid)
    raw = _snapshot_row(conn, release_no, sid, logical_id)
    snap = _snapshot_of(raw) if raw is not None else _MISSING_SNAPSHOT
    runs: list[dict[str, Any]] = []
    for day in days:
        if snap.availability == "absent":
            status, unified = "absent", None
        elif snap.availability == "deleted":
            status, unified = "deleted", None
        else:
            member = _member_row(conn, release_no, day, sid, logical_id)
            if member is None:
                status, unified = "absent", None
            elif member["unified_key"] is None:
                status, unified = "conflict", None
            else:
                status, unified = "assigned", member["unified_key"]
        if runs and runs[-1]["status"] == status \
                and runs[-1]["unified_key"] == unified:
            runs[-1]["to"] = _day_after(day)
        else:
            runs.append({"status": status, "unified_key": unified,
                         "from": day, "to": _day_after(day)})
    return {"status": "ok", "release": release_no,
            "key": {"source_server": source_server, "tenant": tenant,
                    "id": logical_id},
            "from": date_from, "to": date_to, "runs": runs}


def decision_info(conn: sqlite3.Connection, release_no: int,
                  decision_id: str, day: str | None = None) -> dict[str, Any]:
    rel = load_release(conn, release_no)
    row = conn.execute(
        "SELECT * FROM idr_decision WHERE release_no=? AND decision_id=?",
        (release_no, decision_id)).fetchone()
    if row is None:
        raise UnknownDecision(
            f"decision {decision_id!r} is not effective in release {release_no}")
    if day is not None:
        _check_day(rel, day)
    endpoints = []
    for side, sid, lid in (("left", row["left_source"], row["left_patient"]),
                           ("right", row["right_source"], row["right_patient"])):
        server, tenant = rel.text_of(int(sid))
        endpoint: dict[str, Any] = {
            "side": side,
            "key": {"source_server": server, "tenant": tenant, "id": lid}}
        raw = _snapshot_row(conn, release_no, sid, lid)
        if raw is None:
            endpoint["availability"] = "absent"
            endpoint["participates"] = False
        else:
            snap = _snapshot_of(raw)
            endpoint["availability"] = snap.availability
            endpoint["version"] = {"version_id": snap.version_id,
                                   "seq": snap.seq,
                                   "tx_ordinal": snap.tx_ordinal,
                                   "method": snap.method,
                                   "present_from": snap.present_from}
            endpoint["participates"] = snap.availability == "available"
            if day is not None:
                endpoint["present_on_date"] = snap.present_on(day)
                endpoint["participates"] = bool(
                    snap.present_on(day) and snap.availability == "available")
        if day is not None:
            member = _member_row(conn, release_no, day, sid, lid)
            if member is not None:
                endpoint["group_key"] = member["group_key"]
                endpoint["unified_key"] = member["unified_key"]
        endpoints.append(endpoint)
    both = all(e["participates"] for e in endpoints)
    out: dict[str, Any] = {
        "status": "ok", "release": release_no,
        "decision_id": decision_id, "revision": row["revision"],
        "kind": row["kind"], "date_start": row["date_start"],
        "date_end": row["date_end"], "endpoints": endpoints,
        "pending": any(e["availability"] != "available" for e in endpoints),
        "changed_in_release": conn.execute(
            "SELECT 1 FROM idr_decision_change WHERE release_no=? AND "
            "decision_id=?", (release_no, decision_id)).fetchone() is not None,
    }
    if day is not None:
        active = row["date_start"] <= day < row["date_end"]
        out["date"] = day
        out["active_on_date"] = active
        out["participates"] = bool(active and both)
    return out


# ---------------------------------------------------------------------------
# Same/different verification with shortest evidence paths
# ---------------------------------------------------------------------------

def verify_pair(conn: sqlite3.Connection, release_no: int, day: str,
                left: TextKey, right: TextKey) -> dict[str, Any]:
    rel = load_release(conn, release_no)
    _check_day(rel, day)
    left_endpoint = _endpoint(conn, rel, day, left)
    right_endpoint = _endpoint(conn, rel, day, right)
    out: dict[str, Any] = {"release": release_no, "date": day,
                           "left": left_endpoint, "right": right_endpoint}
    if not left_endpoint["available_on_date"] \
            or not right_endpoint["available_on_date"]:
        out["status"] = "pending"
        out["items"] = [_pending_item(left_endpoint),
                        _pending_item(right_endpoint)]
        return out
    lkey = (left_endpoint["source_id"], left_endpoint["logical_id"])
    rkey = (right_endpoint["source_id"], right_endpoint["logical_id"])
    if lkey == rkey:
        out["status"] = "same"
        out["unified_key"] = left_endpoint["unified_key"]
        out["path"] = []
        return out
    if left_endpoint["group_key"] == right_endpoint["group_key"]:
        out["group_key"] = left_endpoint["group_key"]
        out["unified_key"] = left_endpoint["unified_key"]
        if left_endpoint["unified_key"] is None:
            out["status"] = "conflict"
            out["evidence"] = _conflict_evidence(
                conn, rel, day, left_endpoint["group_key"])
        else:
            out["status"] = "same"
            out["path"] = _shortest_same_path(conn, rel, day, lkey, rkey)
        return out
    out["status"] = "different"
    out["left_group_key"] = left_endpoint["group_key"]
    out["right_group_key"] = right_endpoint["group_key"]
    return out


def _pending_item(endpoint: dict[str, Any]) -> dict[str, Any]:
    """Two-endpoint availability report for the pending-verification state."""

    return {
        "key": endpoint["key"],
        "available": endpoint["available_on_date"],
        "availability": endpoint["availability"],
        "version": endpoint.get("version"),
        "group_key": endpoint.get("group_key"),
        "unified_key": endpoint.get("unified_key"),
    }


def _endpoint(conn: sqlite3.Connection, rel: Release, day: str,
              key: TextKey) -> dict[str, Any]:
    server, tenant, lid = key
    source = _source_of(conn, server, tenant)
    sid = int(source["source_id"])
    _participant(rel, sid)
    out: dict[str, Any] = {
        "source_id": sid, "logical_id": lid,
        "key": {"source_server": server, "tenant": tenant, "id": lid},
        "cut": rel.cuts[sid],
        "available_on_date": False,
    }
    raw = _snapshot_row(conn, rel.release_no, sid, lid)
    if raw is None:
        out["availability"] = "absent"
    else:
        snap = _snapshot_of(raw)
        out["availability"] = snap.availability
        out["version"] = {"version_id": snap.version_id, "seq": snap.seq,
                          "tx_ordinal": snap.tx_ordinal,
                          "method": snap.method,
                          "present_from": snap.present_from}
        out["available_on_date"] = snap.present_on(day)
    member = _member_row(conn, rel.release_no, day, sid, lid)
    out["group_key"] = member["group_key"] if member is not None else None
    out["unified_key"] = member["unified_key"] if member is not None else None
    out["source_server"] = server
    out["tenant"] = tenant
    return out


def _present_on(conn: sqlite3.Connection, release_no: int, day: str,
                key: Key) -> bool:
    row = conn.execute(
        "SELECT availability FROM idr_patient WHERE "
        "release_no=? AND source_id=? AND logical_id=?",
        (release_no, key[0], key[1])).fetchone()
    if row is None or row["availability"] != "available":
        return False
    return row["availability"] == "available"


def _active_same_edges(conn: sqlite3.Connection, rel: Release, day: str
                       ) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for r in conn.execute(
            "SELECT * FROM idr_decision WHERE release_no=? AND kind='same' "
            "AND date_start<=? AND date_end>? ORDER BY decision_id",
            (rel.release_no, day, day)):
        left = (r["left_source"], r["left_patient"])
        right = (r["right_source"], r["right_patient"])
        if _present_on(conn, rel.release_no, day, left) \
                and _present_on(conn, rel.release_no, day, right):
            out[r["decision_id"]] = dict(r)
    return out


def _seq_less(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    if len(a) != len(b):
        return len(a) < len(b)
    return [x.encode("utf-8") for x in a] < [x.encode("utf-8") for x in b]


def _shortest_same_path(conn: sqlite3.Connection, rel: Release, day: str,
                        start: Key, goal: Key) -> list[dict[str, Any]]:
    """Shortest path by edge count; ties by decision-id byte sequence.

    ``frontier`` maps a node to the best decision-id sequence reaching it at
    that depth; nodes seen at shallower depths are never revisited, so the
    first arrival at ``goal`` is the shortest and lexicographically first.
    """

    edges = _active_same_edges(conn, rel, day)
    adjacency: dict[Key, list[tuple[Key, str]]] = {}
    for did, e in edges.items():
        left = (e["left_source"], e["left_patient"])
        right = (e["right_source"], e["right_patient"])
        adjacency.setdefault(left, []).append((right, did))
        adjacency.setdefault(right, []).append((left, did))
    if start == goal:
        return []
    seen: set[Key] = {start}
    frontier: dict[Key, tuple[str, ...]] = {start: ()}
    goal_sequence: tuple[str, ...] | None = None
    while frontier and goal_sequence is None:
        candidates: dict[Key, tuple[str, ...]] = {}
        for node, sequence in frontier.items():
            for other, did in adjacency.get(node, []):
                if other in seen:
                    continue
                candidate = sequence + (did,)
                current = candidates.get(other)
                if current is None or _seq_less(candidate, current):
                    candidates[other] = candidate
        if not candidates:
            break
        seen.update(candidates)
        if goal in candidates:
            goal_sequence = candidates[goal]
            break
        frontier = candidates
    if goal_sequence is None:
        return []
    path: list[dict[str, Any]] = []
    node = start
    for did in goal_sequence:
        dec = edges[did]
        left = (dec["left_source"], dec["left_patient"])
        right = (dec["right_source"], dec["right_patient"])
        nxt = right if left == node else left
        path.append({
            "decision_id": did, "revision": dec["revision"],
            "from": _endpoint(conn, rel, day, _text_of(rel, node)),
            "to": _endpoint(conn, rel, day, _text_of(rel, nxt)),
        })
        node = nxt
    return path


def _text_of(rel: Release, key: Key) -> TextKey:
    server, tenant = rel.sources[key[0]]
    return (server, tenant, key[1])


def _conflict_evidence(conn: sqlite3.Connection, rel: Release, day: str,
                       group_key: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT * FROM idr_decision WHERE release_no=? AND kind='different' "
        "AND date_start<=? AND date_end>? ORDER BY decision_id",
        (rel.release_no, day, day)).fetchall()
    members = conn.execute(
        "SELECT source_id, logical_id FROM idr_member WHERE release_no=? AND "
        "date_key=? AND group_key=?", (rel.release_no, day, group_key)
    ).fetchall()
    member_keys = {(int(r["source_id"]), r["logical_id"]) for r in members}
    chosen: dict[str, Any] | None = None
    for r in rows:
        dec = dict(r)
        left = (dec["left_source"], dec["left_patient"])
        right = (dec["right_source"], dec["right_patient"])
        if left in member_keys and right in member_keys \
                and _present_on(conn, rel.release_no, day, left) \
                and _present_on(conn, rel.release_no, day, right):
            chosen = dec
            break
    base = {"group_key": group_key,
            "members": [_endpoint(conn, rel, day, _text_of(rel, k))
                        for k in sorted(member_keys)]}
    if chosen is None:
        return base
    left = (chosen["left_source"], chosen["left_patient"])
    right = (chosen["right_source"], chosen["right_patient"])
    base["decision"] = {
        "decision_id": chosen["decision_id"],
        "revision": chosen["revision"], "kind": "different",
        "date_start": chosen["date_start"], "date_end": chosen["date_end"],
        "left": _endpoint(conn, rel, day, _text_of(rel, left)),
        "right": _endpoint(conn, rel, day, _text_of(rel, right)),
    }
    base["contradicting_same_path"] = _shortest_same_path(
        conn, rel, day, left, right)
    return base
