"""SQLite persistence for immutable facts and queryable cuts.

Design rules enforced by the schema:

* A resource *identity* is ``(source_id, resource_type, logical_id)``. A
  versionId is only ever compared for equality inside one identity.
* Rows in ``resource_version`` are facts of *published* transactions and are
  never updated or deleted by later retransmissions. Divergent retransmitted
  payloads land in ``isolated_conflicts`` instead.
* A cut is a row in ``cuts`` created once when the frontier advances. Query
  code pins a single read transaction (the same SQLite snapshot) for the
  whole query, so an ingest committing the next transaction cannot change the
  fact set observed by an in-flight query.
* Every on-disk location is stored as a repository-relative POSIX path.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from collections.abc import Iterator
from pathlib import Path

from .errors import RepoLocked

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS sources (
    source_id        INTEGER PRIMARY KEY,
    source_server    TEXT NOT NULL,
    tenant           TEXT NOT NULL,
    baseline_seq     INTEGER NOT NULL,
    published_seq    INTEGER NOT NULL,
    blocked_seq      INTEGER,
    blocked_reason   TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_source_coord
    ON sources(source_server, tenant);

CREATE TABLE IF NOT EXISTS transactions (
    source_id        INTEGER NOT NULL REFERENCES sources(source_id),
    seq              INTEGER NOT NULL,
    state            TEXT NOT NULL CHECK (state IN ('staged','published','conflict')),
    operation_count  INTEGER NOT NULL,
    manifest_digest  TEXT NOT NULL,
    manifest_relpath TEXT NOT NULL,
    received_at      TEXT NOT NULL DEFAULT (datetime('now')),
    published_at     TEXT,
    conflict_reason  TEXT,
    PRIMARY KEY (source_id, seq)
);

CREATE TABLE IF NOT EXISTS shards (
    source_id        INTEGER NOT NULL REFERENCES sources(source_id),
    seq              INTEGER NOT NULL,
    ordinal          INTEGER NOT NULL,
    shard_relpath    TEXT NOT NULL,
    expected_digest  TEXT NOT NULL,
    actual_digest    TEXT,
    expected_count   INTEGER NOT NULL,
    actual_count     INTEGER,
    state            TEXT NOT NULL
        CHECK (state IN ('missing','present','verified','bad_digest','bad_count','bad_format')),
    PRIMARY KEY (source_id, seq, ordinal)
);

CREATE TABLE IF NOT EXISTS resource_version (
    source_id        INTEGER NOT NULL REFERENCES sources(source_id),
    seq              INTEGER NOT NULL,
    tx_ordinal       INTEGER NOT NULL,
    resource_type    TEXT NOT NULL,
    logical_id       TEXT NOT NULL,
    version_id       TEXT NOT NULL,
    method           TEXT NOT NULL CHECK (method IN ('PUT','DELETE')),
    is_deleted       INTEGER NOT NULL,
    body_digest      TEXT,
    body_relpath     TEXT,
    last_updated     TEXT,
    PRIMARY KEY (source_id, resource_type, logical_id, seq, tx_ordinal)
);
CREATE INDEX IF NOT EXISTS idx_rv_identity_seq
    ON resource_version(source_id, resource_type, logical_id, seq);
CREATE INDEX IF NOT EXISTS idx_rv_seq
    ON resource_version(source_id, seq);
CREATE UNIQUE INDEX IF NOT EXISTS idx_rv_identity_version
    ON resource_version(source_id, resource_type, logical_id, version_id);

CREATE TABLE IF NOT EXISTS ref_edges (
    edge_id          INTEGER PRIMARY KEY,
    source_id        INTEGER NOT NULL,
    seq              INTEGER NOT NULL,
    tx_ordinal       INTEGER NOT NULL,
    owner_type       TEXT NOT NULL,
    owner_id         TEXT NOT NULL,
    owner_version    TEXT NOT NULL,
    container        TEXT NOT NULL,
    loc              TEXT NOT NULL,
    ref_text         TEXT NOT NULL,
    kind             TEXT NOT NULL,
    versioned        INTEGER NOT NULL,
    target_type      TEXT,
    target_id        TEXT,
    target_version   TEXT
);
CREATE INDEX IF NOT EXISTS idx_edges_out
    ON ref_edges(source_id, owner_type, owner_id, owner_version);

CREATE TABLE IF NOT EXISTS contained_resources (
    source_id        INTEGER NOT NULL,
    owner_type       TEXT NOT NULL,
    owner_id         TEXT NOT NULL,
    owner_version    TEXT NOT NULL,
    contained_id     TEXT NOT NULL,
    PRIMARY KEY (source_id, owner_type, owner_id, owner_version, contained_id)
);

CREATE TABLE IF NOT EXISTS cuts (
    source_id        INTEGER NOT NULL REFERENCES sources(source_id),
    cut_seq          INTEGER NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source_id, cut_seq)
);

CREATE TABLE IF NOT EXISTS isolated_conflicts (
    conflict_id      INTEGER PRIMARY KEY,
    source_id        INTEGER NOT NULL,
    seq              INTEGER NOT NULL,
    tx_ordinal       INTEGER,
    kind             TEXT NOT NULL,
    resource_type    TEXT,
    logical_id       TEXT,
    version_id       TEXT,
    reason           TEXT NOT NULL,
    payload_relpath  TEXT,
    detected_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_ic_target
    ON isolated_conflicts(source_id, resource_type, logical_id, version_id);

CREATE TABLE IF NOT EXISTS events (
    event_id         INTEGER PRIMARY KEY,
    source_id        INTEGER NOT NULL,
    seq              INTEGER,
    kind             TEXT NOT NULL,
    detail           TEXT NOT NULL,
    at               TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(
        str(db_path),
        timeout=30,
        isolation_level=None,  # explicit BEGIN/COMMIT
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")
    return conn


def initialise(conn: sqlite3.Connection) -> None:
    try:
        conn.executescript(SCHEMA)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            raise RepoLocked(str(exc)) from exc
        raise


@contextmanager
def write_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialised writer transaction; retries are handled by busy_timeout."""

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


@contextmanager
def snapshot_tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Pin one read snapshot for the whole query.

    A plain ``BEGIN`` starts a deferred transaction; the first read takes a
    read mark in WAL mode that stays fixed until commit, so a concurrent
    ``advance`` committing the next seq cannot alter the fact set seen here.
    """

    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.execute("COMMIT")
