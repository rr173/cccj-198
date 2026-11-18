"""Read-side queries: resource reads, reference neighbours and trace paths.

Every public function takes an open connection and calls :func:`check_cut`
inside one snapshot transaction, so a whole query observes a single cut and
a single immutable fact set even while an ingest transaction commits next
door.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any

from . import blobs
from .errors import CutNotQueryable, UnknownSource
from .paths import RepoPaths
from .resolver import (
    edges_of_version,
    latest_version_at,
    resolve_edge,
)


def load_source(conn: sqlite3.Connection, source_server: str,
                tenant: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM sources WHERE source_server=? AND tenant=?",
        (source_server, tenant),
    ).fetchone()
    if row is None:
        raise UnknownSource(f"no source registered for {source_server} / {tenant}")
    return row


def check_cut(conn: sqlite3.Connection, source: sqlite3.Row,
              cut_seq: int) -> None:
    baseline_seq = int(source["baseline_seq"])
    if cut_seq < baseline_seq:
        raise CutNotQueryable(
            f"cut {cut_seq} predates baseline_seq {baseline_seq}; no evidence "
            "exists for any earlier history"
        )
    cut = conn.execute(
        "SELECT 1 FROM cuts WHERE source_id=? AND cut_seq=?",
        (source["source_id"], cut_seq),
    ).fetchone()
    if cut is None:
        raise CutNotQueryable(
            f"cut {cut_seq} is not a published complete cut "
            f"(frontier seq {source['published_seq']})"
        )


# ---------------------------------------------------------------------------
# Resource reads
# ---------------------------------------------------------------------------

def read_current(paths: RepoPaths, conn: sqlite3.Connection,
                 source: sqlite3.Row, cut_seq: int, rtype: str,
                 logical_id: str) -> dict[str, Any]:
    from .db import snapshot_tx
    with snapshot_tx(conn) as ro:
        check_cut(ro, source, cut_seq)
        row = latest_version_at(ro, source["source_id"], rtype,
                                logical_id, cut_seq)
        if row is None:
            return _missing_envelope(source, cut_seq, rtype, logical_id)
        return _version_envelope(paths, ro, source, cut_seq, row)


def read_version(paths: RepoPaths, conn: sqlite3.Connection,
                 source: sqlite3.Row, cut_seq: int, rtype: str,
                 logical_id: str, version_id: str) -> dict[str, Any]:
    from .db import snapshot_tx
    with snapshot_tx(conn) as ro:
        check_cut(ro, source, cut_seq)
        row = ro.execute(
            "SELECT * FROM resource_version WHERE source_id=? AND resource_type=? "
            "AND logical_id=? AND version_id=? AND seq<=? LIMIT 1",
            (source["source_id"], rtype, logical_id, version_id, cut_seq),
        ).fetchone()
        if row is None:
            later = ro.execute(
                "SELECT 1 FROM resource_version WHERE source_id=? AND resource_type=? "
                "AND logical_id=? AND seq>? LIMIT 1",
                (source["source_id"], rtype, logical_id, cut_seq),
            ).fetchone()
            status = "version_not_known_at_cut" if later else "missing"
            return {"status": status, "resourceType": rtype, "id": logical_id,
                    "versionId": version_id, "cut_seq": cut_seq}
        return _version_envelope(paths, ro, source, cut_seq, row)


def read_history(paths: RepoPaths, conn: sqlite3.Connection,
                 source: sqlite3.Row, cut_seq: int, rtype: str,
                 logical_id: str) -> dict[str, Any]:
    from .db import snapshot_tx
    with snapshot_tx(conn) as ro:
        check_cut(ro, source, cut_seq)
        rows = ro.execute(
            "SELECT * FROM resource_version WHERE source_id=? AND resource_type=? "
            "AND logical_id=? AND seq<=? ORDER BY seq, tx_ordinal",
            (source["source_id"], rtype, logical_id, cut_seq),
        ).fetchall()
        return {
            "status": "found" if rows else "missing",
            "identity": _identity(source, rtype, logical_id),
            "cut_seq": cut_seq,
            "versions": [
                {
                    "versionId": r["version_id"],
                    "method": r["method"],
                    "deleted": bool(r["is_deleted"]),
                    "seq": r["seq"],
                    "tx_ordinal": r["tx_ordinal"],
                    "body_digest": r["body_digest"],
                }
                for r in rows
            ],
        }


def _version_envelope(paths: RepoPaths, ro: sqlite3.Connection,
                      source: sqlite3.Row, cut_seq: int,
                      row: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = {
        "status": "deleted" if row["is_deleted"] else "resolved",
        "identity": _identity(source, row["resource_type"], row["logical_id"]),
        "versionId": row["version_id"],
        "method": row["method"],
        "seq": row["seq"],
        "cut_seq": cut_seq,
        "body_digest": row["body_digest"],
    }
    if not row["is_deleted"] and row["body_relpath"]:
        out["resource"] = blobs.read_blob(paths, row["body_relpath"])
    return out


def _missing_envelope(source: sqlite3.Row, cut_seq: int, rtype: str,
                      logical_id: str) -> dict[str, Any]:
    return {"status": "missing",
            "identity": _identity(source, rtype, logical_id),
            "cut_seq": cut_seq}


def _identity(source: sqlite3.Row, rtype: str, logical_id: str) -> dict[str, str]:
    return {
        "source_server": source["source_server"],
        "tenant": source["tenant"],
        "resourceType": rtype,
        "id": logical_id,
    }


# ---------------------------------------------------------------------------
# Reference neighbours
# ---------------------------------------------------------------------------

def references_for(paths: RepoPaths, conn: sqlite3.Connection,
                   source: sqlite3.Row, cut_seq: int, rtype: str | None,
                   logical_id: str | None, direction: str) -> dict[str, Any]:
    from .db import snapshot_tx
    with snapshot_tx(conn) as ro:
        check_cut(ro, source, cut_seq)
        source_id = source["source_id"]
        if rtype is not None and logical_id is not None:
            current = latest_version_at(ro, source_id, rtype,
                                        logical_id, cut_seq)
            if current is None:
                return {"status": "missing", "cut_seq": cut_seq,
                        "identity": _identity(source, rtype, logical_id),
                        "outbound": [], "inbound": []}
            owner_type, owner_id = rtype, logical_id
        else:
            current = None
            owner_type = owner_id = None

        outbound: list[dict[str, Any]] = []
        inbound: list[dict[str, Any]] = []

        if direction in ("out", "both"):
            edge_rows = _outbound_edges(ro, source_id, cut_seq,
                                        owner_type, owner_id, current)
            for edge in edge_rows:
                outbound.append(resolve_edge(ro, source_id, cut_seq,
                                             edge).to_dict())

        if direction in ("in", "both"):
            inbound = _inbound(ro, source_id, cut_seq, rtype, logical_id)

        result: dict[str, Any] = {"status": "ok", "cut_seq": cut_seq,
                                  "outbound": outbound}
        if direction in ("in", "both"):
            result["inbound"] = inbound
        if current is not None:
            result["identity"] = _identity(source, rtype, logical_id)
            result["current_at_cut"] = {
                "versionId": current["version_id"],
                "deleted": bool(current["is_deleted"]),
                "seq": current["seq"],
            }
        return result


def _outbound_edges(ro: sqlite3.Connection, source_id: int, cut_seq: int,
                    owner_type: str | None, owner_id: str | None,
                    current: sqlite3.Row | None) -> list[sqlite3.Row]:
    if current is not None:
        # Edges of exactly the version that is current at the cut. The edge
        # rows themselves are immutable facts of the version that emitted
        # them; only target resolution is cut-relative.
        return edges_of_version(ro, source_id, owner_type, owner_id,
                                current["version_id"])
    return list(ro.execute(
        "SELECT * FROM ref_edges WHERE source_id=? AND seq<=? ORDER BY edge_id",
        (source_id, cut_seq),
    ))


def _inbound(ro: sqlite3.Connection, source_id: int, cut_seq: int,
             rtype: str | None, logical_id: str | None) -> list[dict[str, Any]]:
    """Edges from cut-current resource versions that point at the target.

    The inbound set is computed from the versions that are themselves
    current at the cut, so a superseded version does not contribute stale
    inbound edges.
    """

    # Latest version row per identity at the cut (SQLite 3.40 window support).
    latest = ro.execute(
        """
        SELECT * FROM (
          SELECT rv.*, ROW_NUMBER() OVER (
              PARTITION BY resource_type, logical_id
              ORDER BY seq DESC, tx_ordinal DESC
          ) AS rn
          FROM resource_version rv
          WHERE rv.source_id=? AND rv.seq<=?
        ) WHERE rn=1
        """,
        (source_id, cut_seq),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for owner in latest:
        edges = edges_of_version(ro, source_id, owner["resource_type"],
                                 owner["logical_id"], owner["version_id"])
        for edge in edges:
            if edge["kind"] not in ("relative", "absolute", "contained"):
                continue
            if rtype is not None and logical_id is not None:
                if edge["kind"] == "contained":
                    continue
                if edge["target_type"] != rtype or edge["target_id"] != logical_id:
                    continue
            resolution = resolve_edge(ro, source_id, cut_seq, edge).to_dict()
            resolution["from_owner"] = (
                f"{owner['resource_type']}/{owner['logical_id']}"
            )
            resolution["from_version"] = owner["version_id"]
            out.append(resolution)
    return out


# ---------------------------------------------------------------------------
# Trace paths with cycle guard
# ---------------------------------------------------------------------------

@dataclass
class PathHop:
    edge_loc: str
    ref_text: str
    status: str
    target: str | None
    target_version: str | None
    target_seq: int | None
    stopped: bool
    stop_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def trace_path(paths: RepoPaths, conn: sqlite3.Connection,
               source: sqlite3.Row, cut_seq: int, start_type: str,
               start_id: str, max_hops: int = 50) -> dict[str, Any]:
    """Walk unversioned/current outbound edges, stopping on cycles.

    The visited set keys on *resolved versions*. Reaching an already visited
    version ends that branch with ``cycle`` instead of looping forever; the
    evidence of the cycle (the repeated edge) is retained in the output.
    External/invalid/missing/deleted/conflict edges terminate a branch and
    are reported as such.
    """

    from .db import snapshot_tx
    with snapshot_tx(conn) as ro:
        check_cut(ro, source, cut_seq)
        source_id = source["source_id"]
        start = latest_version_at(ro, source_id, start_type, start_id, cut_seq)
        if start is None:
            return {"status": "missing", "cut_seq": cut_seq,
                    "start": f"{start_type}/{start_id}", "paths": []}

        visited: set[tuple[str, str, str]] = set()
        paths_found: list[list[dict[str, Any]]] = []

        def walk(rtype: str, rid: str, version: str,
                 trail: list[dict[str, Any]], depth: int) -> None:
            key = (rtype, rid, version)
            if key in visited:
                if trail:
                    trail[-1]["stopped"] = True
                    trail[-1]["stop_reason"] = "cycle: already-visited version"
                paths_found.append(list(trail))
                return
            visited.add(key)
            if depth >= max_hops:
                paths_found.append(list(trail))
                return
            edges = edges_of_version(ro, source_id, rtype, rid, version)
            navigable = [e for e in edges
                         if e["kind"] in ("relative", "absolute", "contained")]
            if not navigable:
                paths_found.append(list(trail))
                return
            for edge in navigable:
                res = resolve_edge(ro, source_id, cut_seq, edge)
                hop = res.to_dict()
                hop["stopped"] = False
                hop["stop_reason"] = None
                branch = trail + [hop]
                if res.status != "resolved" or res.target_type in (None, "contained"):
                    hop["stopped"] = True
                    hop["stop_reason"] = (
                        "terminal reference status: " + res.status
                    )
                    paths_found.append(branch)
                    continue
                walk(res.target_type, res.target_id,
                     res.target_version or "", branch, depth + 1)

        walk(start_type, start_id, start["version_id"], [], 0)
        return {
            "status": "ok",
            "cut_seq": cut_seq,
            "start": f"{start_type}/{start_id}",
            "start_version": start["version_id"],
            "paths": paths_found,
        }
