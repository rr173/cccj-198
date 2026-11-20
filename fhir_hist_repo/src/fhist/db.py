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

# Profile-package registry and audit state. These tables are additive: they
# never reference or mutate the ingest fact tables, and old repositories gain
# them lazily via :func:`ensure_audit_schema`.
AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS profile_packages (
    package_digest   TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    version          TEXT NOT NULL,
    fhir_versions    TEXT NOT NULL DEFAULT '[]',
    dependencies     TEXT NOT NULL DEFAULT '{}',
    file_count       INTEGER NOT NULL,
    canonical_count  INTEGER NOT NULL,
    registered_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_profile_pkg_name_version
    ON profile_packages(name, version);

CREATE TABLE IF NOT EXISTS profile_package_files (
    package_digest   TEXT NOT NULL REFERENCES profile_packages(package_digest),
    relpath          TEXT NOT NULL,
    sha256           TEXT NOT NULL,
    blob_relpath     TEXT NOT NULL,
    PRIMARY KEY (package_digest, relpath)
);

CREATE TABLE IF NOT EXISTS profile_canonicals (
    canonical        TEXT NOT NULL,
    version          TEXT NOT NULL,
    kind             TEXT NOT NULL
        CHECK (kind IN ('StructureDefinition','ValueSet','CodeSystem')),
    package_digest   TEXT NOT NULL REFERENCES profile_packages(package_digest),
    file_relpath     TEXT NOT NULL,
    content_digest   TEXT NOT NULL,
    PRIMARY KEY (canonical, version)
);
CREATE INDEX IF NOT EXISTS idx_profile_canonicals_pkg
    ON profile_canonicals(package_digest);

CREATE TABLE IF NOT EXISTS audit_runs (
    audit_id         TEXT PRIMARY KEY,
    request_json     TEXT NOT NULL,
    source_id        INTEGER NOT NULL REFERENCES sources(source_id),
    package_digest   TEXT NOT NULL REFERENCES profile_packages(package_digest),
    state            TEXT NOT NULL CHECK (state IN ('running','complete')),
    frontier_at_start INTEGER NOT NULL,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at     TEXT,
    summary_json     TEXT
);

CREATE TABLE IF NOT EXISTS eval_cache (
    cache_key        TEXT PRIMARY KEY,
    package_digest   TEXT NOT NULL,
    roots_key        TEXT NOT NULL,
    source_id        INTEGER NOT NULL,
    resource_type    TEXT NOT NULL,
    logical_id       TEXT NOT NULL,
    version_id       TEXT NOT NULL,
    body_digest      TEXT NOT NULL,
    result           TEXT NOT NULL,
    selected_json    TEXT NOT NULL,
    findings_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_cache_version
    ON eval_cache(source_id, resource_type, logical_id, version_id);

CREATE TABLE IF NOT EXISTS audit_members (
    audit_id         TEXT NOT NULL REFERENCES audit_runs(audit_id),
    cut_seq          INTEGER NOT NULL,
    cache_key        TEXT NOT NULL REFERENCES eval_cache(cache_key),
    PRIMARY KEY (audit_id, cut_seq, cache_key)
);

CREATE TABLE IF NOT EXISTS audit_deleted (
    audit_id         TEXT NOT NULL REFERENCES audit_runs(audit_id),
    cut_seq          INTEGER NOT NULL,
    resource_type    TEXT NOT NULL,
    logical_id       TEXT NOT NULL,
    version_id       TEXT NOT NULL,
    PRIMARY KEY (audit_id, cut_seq, resource_type, logical_id)
);

CREATE TABLE IF NOT EXISTS audit_cut_progress (
    audit_id         TEXT NOT NULL REFERENCES audit_runs(audit_id),
    cut_seq          INTEGER NOT NULL,
    member_count     INTEGER NOT NULL,
    deleted_count    INTEGER NOT NULL,
    done_at          TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (audit_id, cut_seq)
);
"""

# Cross-source patient identity dimension. Additive and independent of the
# ingest fact tables; old repositories gain it lazily via
# :func:`ensure_identity_schema`. Every published release is immutable.
IDENTITY_SCHEMA = """
CREATE TABLE IF NOT EXISTS identity_release (
    release_no       INTEGER PRIMARY KEY,
    release_id       TEXT NOT NULL UNIQUE,
    parent_no        INTEGER REFERENCES identity_release(release_no),
    parent_id        TEXT,
    cuts_json        TEXT NOT NULL,
    date_start       TEXT NOT NULL,
    date_end         TEXT NOT NULL,
    correction_json  TEXT NOT NULL DEFAULT '[]',
    request_digest   TEXT NOT NULL,
    stats_json       TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS idr_source (
    release_no       INTEGER NOT NULL REFERENCES identity_release(release_no),
    source_id        INTEGER NOT NULL REFERENCES sources(source_id),
    cut_seq          INTEGER NOT NULL,
    PRIMARY KEY (release_no, source_id)
);
CREATE INDEX IF NOT EXISTS idx_idr_source_cut ON idr_source(cut_seq);

CREATE TABLE IF NOT EXISTS idr_patient (
    release_no       INTEGER NOT NULL,
    source_id        INTEGER NOT NULL,
    logical_id       TEXT NOT NULL,
    method           TEXT CHECK (method IN ('PUT','DELETE')),
    version_id       TEXT,
    seq              INTEGER,
    tx_ordinal       INTEGER,
    availability     TEXT NOT NULL
        CHECK (availability IN ('available','deleted','absent')),
    present_from     TEXT,
    PRIMARY KEY (release_no, source_id, logical_id)
);
CREATE INDEX IF NOT EXISTS idx_idr_patient_key
    ON idr_patient(release_no, source_id, logical_id);

CREATE TABLE IF NOT EXISTS idr_decision (
    release_no       INTEGER NOT NULL REFERENCES identity_release(release_no),
    decision_id      TEXT NOT NULL,
    revision         INTEGER NOT NULL,
    op               TEXT NOT NULL CHECK (op IN ('set','retract')),
    kind             TEXT,
    left_source      INTEGER,
    left_patient     TEXT,
    right_source     INTEGER,
    right_patient    TEXT,
    date_start       TEXT,
    date_end         TEXT,
    PRIMARY KEY (release_no, decision_id)
);
CREATE INDEX IF NOT EXISTS idx_idr_decision_left
    ON idr_decision(release_no, left_source, left_patient);
CREATE INDEX IF NOT EXISTS idx_idr_decision_right
    ON idr_decision(release_no, right_source, right_patient);

CREATE TABLE IF NOT EXISTS idr_decision_change (
    release_no       INTEGER NOT NULL REFERENCES identity_release(release_no),
    decision_id      TEXT NOT NULL,
    revision         INTEGER NOT NULL,   -- 0 means the id was retracted
    PRIMARY KEY (release_no, decision_id)
);

CREATE TABLE IF NOT EXISTS idr_group (
    release_no       INTEGER NOT NULL REFERENCES identity_release(release_no),
    date_key         TEXT NOT NULL,
    group_key        TEXT NOT NULL,
    unified_key      TEXT,
    conflict         INTEGER NOT NULL CHECK (conflict IN (0,1)),
    PRIMARY KEY (release_no, date_key, group_key)
);
CREATE INDEX IF NOT EXISTS idx_idr_group_unified
    ON idr_group(release_no, date_key, unified_key);

CREATE TABLE IF NOT EXISTS idr_member (
    release_no       INTEGER NOT NULL,
    date_key         TEXT NOT NULL,
    source_id        INTEGER NOT NULL,
    logical_id       TEXT NOT NULL,
    group_key        TEXT NOT NULL,
    unified_key      TEXT,
    parent_group_key TEXT,
    parent_unified_key TEXT,
    recomputed       INTEGER NOT NULL CHECK (recomputed IN (0,1)),
    PRIMARY KEY (release_no, date_key, source_id, logical_id)
);
CREATE INDEX IF NOT EXISTS idx_idr_member_group
    ON idr_member(release_no, date_key, group_key);
CREATE INDEX IF NOT EXISTS idx_idr_member_unified
    ON idr_member(release_no, date_key, unified_key);

CREATE TABLE IF NOT EXISTS idr_day (
    release_no       INTEGER NOT NULL REFERENCES identity_release(release_no),
    date_key         TEXT NOT NULL,
    changed          INTEGER NOT NULL,
    group_count      INTEGER NOT NULL,
    member_count     INTEGER NOT NULL,
    conflict_count   INTEGER NOT NULL,
    seen_versions    INTEGER NOT NULL,
    recomputed_keys  INTEGER NOT NULL,
    reused_keys      INTEGER NOT NULL,
    PRIMARY KEY (release_no, date_key)
);

CREATE TABLE IF NOT EXISTS idr_worklog (
    release_no       INTEGER NOT NULL REFERENCES identity_release(release_no),
    source_id        INTEGER NOT NULL,
    cut_seq          INTEGER NOT NULL,
    seen_versions    INTEGER NOT NULL,
    active_versions  INTEGER NOT NULL,
    changed          INTEGER NOT NULL,
    PRIMARY KEY (release_no, source_id)
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
        conn.executescript(AUDIT_SCHEMA)
        conn.executescript(IDENTITY_SCHEMA)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            raise RepoLocked(str(exc)) from exc
        raise


def ensure_identity_schema(conn: sqlite3.Connection) -> None:
    """Lazily add identity-dimension tables to older repositories."""

    try:
        conn.executescript(IDENTITY_SCHEMA)
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            raise RepoLocked(str(exc)) from exc
        raise


def ensure_audit_schema(conn: sqlite3.Connection) -> None:
    """Lazily add profile/audit tables to repositories created before them."""

    try:
        conn.executescript(AUDIT_SCHEMA)
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
