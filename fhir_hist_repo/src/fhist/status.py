"""Repository and source status summaries used by the ``status`` entry."""

from __future__ import annotations

import sqlite3
from typing import Any

from .db import snapshot_tx


def repo_status(conn: sqlite3.Connection) -> dict[str, Any]:
    with snapshot_tx(conn) as ro:
        sources = ro.execute(
            "SELECT * FROM sources ORDER BY source_id"
        ).fetchall()
        return {
            "sources": [source_status(ro, dict(s)) for s in sources],
            "isolated_conflict_count": ro.execute(
                "SELECT COUNT(*) AS n FROM isolated_conflicts"
            ).fetchone()["n"],
        }


def source_status(ro: sqlite3.Connection, source: dict[str, Any]) -> dict[str, Any]:
    source_id = source["source_id"]
    staged = ro.execute(
        "SELECT seq, state, operation_count, conflict_reason "
        "FROM transactions WHERE source_id=? ORDER BY seq",
        (source_id,),
    ).fetchall()
    shard_rows = ro.execute(
        "SELECT seq, ordinal, state FROM shards WHERE source_id=? ORDER BY seq, ordinal",
        (source_id,),
    ).fetchall()
    shards: dict[int, list[dict[str, Any]]] = {}
    for r in shard_rows:
        shards.setdefault(r["seq"], []).append(
            {"ordinal": r["ordinal"], "state": r["state"]})

    baseline_shard = shards.get(source["baseline_seq"], [])
    cuts = [r["cut_seq"] for r in ro.execute(
        "SELECT cut_seq FROM cuts WHERE source_id=? ORDER BY cut_seq",
        (source_id,),
    )]
    conflicts = ro.execute(
        "SELECT seq, kind, resource_type, logical_id, version_id, reason "
        "FROM isolated_conflicts WHERE source_id=? ORDER BY conflict_id",
        (source_id,),
    ).fetchall()
    events = ro.execute(
        "SELECT seq, kind, detail, at FROM events "
        "WHERE source_id=? ORDER BY event_id DESC LIMIT 12",
        (source_id,),
    ).fetchall()

    queryable_from = source["baseline_seq"] if cuts else None
    return {
        "source_id": source_id,
        "source_server": source["source_server"],
        "tenant": source["tenant"],
        "baseline_seq": source["baseline_seq"],
        "published_seq": source["published_seq"],
        "frontier_queryable": max(cuts) if cuts else None,
        "queryable_cuts_from": queryable_from,
        "queryable_cuts": cuts,
        "blocked_seq": source["blocked_seq"],
        "blocked_reason": source["blocked_reason"],
        "baseline_shards": baseline_shard,
        "transactions": [
            {
                "seq": t["seq"],
                "state": t["state"],
                "operation_count": t["operation_count"],
                "conflict_reason": t["conflict_reason"],
                "shards": shards.get(t["seq"], []),
                "complete": bool(shards.get(t["seq"]))
                and all(s["state"] == "verified"
                        for s in shards.get(t["seq"], [])),
            }
            for t in staged
        ],
        "isolated_conflicts": [dict(c) for c in conflicts],
        "recent_events": [dict(e) for e in events],
    }
