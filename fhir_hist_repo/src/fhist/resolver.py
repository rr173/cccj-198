"""Cut-time reference resolution.

A query pins one cut seq and one SQLite read snapshot. Resolution semantics:

* **Unversioned references** resolve against the resource *collection as it
  exists at the queried cut* — never against the state at the moment the
  reference was emitted. Advancing the frontier cannot make an old cut's
  unversioned edge silently point at a new object, because the cut predicate
  is applied at query time on immutable version rows.
* **Versioned references** resolve only among exact historical versions
  already known at the cut. A version appearing in a later transaction is
  not visible early; a target that is later deleted still resolves to its
  old version.
* **Contained** ``#id`` references are scoped to the owning resource version
  alone; two owners each defining ``#x`` never merge.
* Absolute URLs on a foreign base are reported ``external`` and never joined
  by a trailing id.
* Divergent payloads isolated as conflicts do not become facts; a target
  version known *only* through an isolated conflict reports ``conflict``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

RESOLVED = "resolved"
DELETED = "deleted"
MISSING = "missing"
VERSION_NOT_KNOWN = "version_not_known_at_cut"
EXTERNAL = "external"
INVALID = "invalid"
CONFLICT = "conflict"


@dataclass
class Resolution:
    status: str
    edge_id: int
    owner: str
    owner_version: str
    container: str
    loc: str
    ref_text: str
    target_type: str | None = None
    target_id: str | None = None
    target_version: str | None = None
    target_seq: int | None = None
    target_method: str | None = None
    conflict_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def latest_version_at(conn: sqlite3.Connection, source_id: int,
                      rtype: str, logical_id: str,
                      cut_seq: int) -> sqlite3.Row | None:
    """The last PUT/DELETE fact for one identity at or before the cut."""

    return conn.execute(
        "SELECT * FROM resource_version WHERE source_id=? AND resource_type=? "
        "AND logical_id=? AND seq<=? ORDER BY seq DESC, tx_ordinal DESC LIMIT 1",
        (source_id, rtype, logical_id, cut_seq),
    ).fetchone()


def exact_version_at(conn: sqlite3.Connection, source_id: int, rtype: str,
                     logical_id: str, version_id: str,
                     cut_seq: int) -> sqlite3.Row | None:
    """The exact version row, but only if its seq is already known at cut."""

    return conn.execute(
        "SELECT * FROM resource_version WHERE source_id=? AND resource_type=? "
        "AND logical_id=? AND version_id=? AND seq<=? LIMIT 1",
        (source_id, rtype, logical_id, version_id, cut_seq),
    ).fetchone()


def _isolated_conflict(conn: sqlite3.Connection, source_id: int, rtype: str,
                       logical_id: str, version_id: str | None,
                       cut_seq: int) -> sqlite3.Row | None:
    q = ("SELECT * FROM isolated_conflicts WHERE source_id=? AND resource_type=? "
         "AND logical_id=? AND seq<=?")
    params: list[Any] = [source_id, rtype, logical_id, cut_seq]
    if version_id is not None:
        q += " AND version_id=?"
        params.append(version_id)
    q += " ORDER BY conflict_id DESC LIMIT 1"
    return conn.execute(q, params).fetchone()


def resolve_edge(conn: sqlite3.Connection, source_id: int, cut_seq: int,
                 edge: sqlite3.Row) -> Resolution:
    """Resolve one stored ref-edge row under the pinned cut snapshot."""

    base = Resolution(
        status=INVALID,
        edge_id=edge["edge_id"],
        owner=f"{edge['owner_type']}/{edge['owner_id']}",
        owner_version=edge["owner_version"],
        container=edge["container"],
        loc=edge["loc"],
        ref_text=edge["ref_text"],
        target_type=edge["target_type"],
        target_id=edge["target_id"],
        target_version=edge["target_version"],
    )
    kind = edge["kind"]

    if kind == "external":
        base.status = EXTERNAL
        return base
    if kind == "invalid":
        base.status = INVALID
        return base

    if kind == "contained":
        return _resolve_contained(conn, source_id, edge, base)

    # relative / absolute same-source references share identity semantics
    rtype, logical_id = edge["target_type"], edge["target_id"]
    if edge["versioned"]:
        row = exact_version_at(conn, source_id, rtype, logical_id,
                               edge["target_version"], cut_seq)
        if row is not None:
            # Even an exact match stays flagged when an isolated divergent
            # copy of the same version was also delivered.
            ic = _isolated_conflict(conn, source_id, rtype, logical_id,
                                    edge["target_version"], cut_seq)
            base.status = CONFLICT if ic is not None else RESOLVED
            if ic is not None:
                base.conflict_reason = ic["reason"]
            base.target_seq = row["seq"]
            base.target_method = row["method"]
            return base
        # Known later version? Then this one simply does not exist yet.
        ic = _isolated_conflict(conn, source_id, rtype, logical_id,
                                edge["target_version"], cut_seq)
        if ic is not None:
            base.status = CONFLICT
            base.conflict_reason = ic["reason"]
            return base
        later = conn.execute(
            "SELECT 1 FROM resource_version WHERE source_id=? AND resource_type=? "
            "AND logical_id=? AND seq>? LIMIT 1",
            (source_id, rtype, logical_id, cut_seq),
        ).fetchone()
        base.status = VERSION_NOT_KNOWN if later is not None else MISSING
        return base

    row = latest_version_at(conn, source_id, rtype, logical_id, cut_seq)
    if row is None:
        ic = _isolated_conflict(conn, source_id, rtype, logical_id, None, cut_seq)
        if ic is not None:
            base.status = CONFLICT
            base.conflict_reason = ic["reason"]
        else:
            base.status = MISSING
        return base
    base.target_version = row["version_id"]
    base.target_seq = row["seq"]
    base.target_method = row["method"]
    ic = _isolated_conflict(conn, source_id, rtype, logical_id,
                            row["version_id"], cut_seq)
    if ic is not None:
        base.status = CONFLICT
        base.conflict_reason = ic["reason"]
        return base
    base.status = DELETED if row["is_deleted"] else RESOLVED
    return base


def _resolve_contained(conn: sqlite3.Connection, source_id: int,
                       edge: sqlite3.Row, base: Resolution) -> Resolution:
    contained_id = edge["target_id"]
    # Fragment scope is exactly the owning resource version.
    hit = conn.execute(
        "SELECT 1 FROM contained_resources WHERE source_id=? AND owner_type=? "
        "AND owner_id=? AND owner_version=? AND contained_id=? LIMIT 1",
        (source_id, edge["owner_type"], edge["owner_id"],
         edge["owner_version"], contained_id),
    ).fetchone()
    base.target_type = "contained"
    base.target_id = contained_id
    base.target_version = edge["owner_version"]
    base.status = RESOLVED if hit is not None else MISSING
    return base


def edges_of_version(conn: sqlite3.Connection, source_id: int,
                     owner_type: str, owner_id: str,
                     owner_version: str) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM ref_edges WHERE source_id=? AND owner_type=? "
        "AND owner_id=? AND owner_version=? ORDER BY edge_id",
        (source_id, owner_type, owner_id, owner_version),
    ))
