"""Downstream loader for bed daily-report releases, independent of the repo.

Reads only a release directory (``manifest.json`` + delivery files), never
the source repository. Every load is one atomic SQLite transaction:

* the manifest is machine-verified (structure, content-bound release id,
  every listed file's sha256 and row count) before the database is touched;
* an initial release only loads into an empty partition;
* a correction only loads when the *recomputed* local partition identity
  equals the manifest's base facts identity — never by comparing cut
  numbers or release ordering alone;
* after applying, the recomputed partition identity must equal the
  manifest's target facts identity or the whole transaction rolls back;
* replaying an already loaded release is a no-op and never regresses.
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path
from typing import Any

from .bedreport import (
    DATE_RE,
    facts_line,
    load_release_manifest,
    verify_release_files,
)
from .db import write_tx
from .errors import (
    BaseMismatch,
    DeliveryError,
    LocalStateMismatch,
    ManifestError,
    PartitionNotEmpty,
    ShardError,
    TargetMismatch,
    UsageError,
)
from .jsonl import iter_jsonl


def _iter_delivery(path: Path):
    """Stream a delivery NDJSON file, mapping parse failures to bad_delivery."""

    try:
        yield from iter_jsonl(path)
    except ShardError as exc:
        raise DeliveryError(str(exc)) from exc

SCHEMA = """
CREATE TABLE IF NOT EXISTS bedload_series (
    series_id         TEXT PRIMARY KEY,
    facts_sha256      TEXT NOT NULL,
    fact_rows         INTEGER NOT NULL,
    head_release_id   TEXT NOT NULL,
    head_release_seq  INTEGER NOT NULL,
    updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS bedload_releases (
    series_id            TEXT NOT NULL,
    release_seq          INTEGER NOT NULL,
    release_id           TEXT NOT NULL,
    target_facts_sha256  TEXT NOT NULL,
    loaded_at            TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (series_id, release_id)
);
CREATE TABLE IF NOT EXISTS bed_facts (
    series_id         TEXT NOT NULL,
    local_date        TEXT NOT NULL,
    ward_code         TEXT NOT NULL,
    open_seconds      INTEGER NOT NULL,
    empty_seconds     INTEGER NOT NULL,
    single_seconds    INTEGER NOT NULL,
    conflict_seconds  INTEGER NOT NULL,
    PRIMARY KEY (series_id, local_date, ward_code)
);
"""

_MEASURES = ("open_seconds", "empty_seconds", "single_seconds",
             "conflict_seconds")


def _connect(db_path: str | Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    if not db_path.parent.is_dir():
        raise UsageError(f"directory does not exist for db: {db_path}")
    try:
        conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None,
                               check_same_thread=False)
    except sqlite3.OperationalError as exc:
        raise UsageError(f"cannot open db {db_path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _local_facts(conn: sqlite3.Connection, series_id: str) -> tuple[str, int]:
    """Recompute the partition identity from the actual local rows."""

    h = hashlib.sha256()
    n = 0
    for r in conn.execute(
            "SELECT local_date, ward_code, open_seconds, empty_seconds, "
            "single_seconds, conflict_seconds FROM bed_facts "
            "WHERE series_id=? ORDER BY local_date, ward_code",
            (series_id,)):
        h.update(facts_line(r["local_date"], r["ward_code"],
                            r["open_seconds"], r["empty_seconds"],
                            r["single_seconds"], r["conflict_seconds"]
                            ).encode("utf-8"))
        n += 1
    return h.hexdigest(), n


def _check_fact_row(row: Any, where: str) -> None:
    if not isinstance(row, dict) \
            or set(row) != {"local_date", "ward_code", *_MEASURES}:
        raise DeliveryError(f"{where}: fact row must carry exactly the "
                            f"business key and four measures, got {row!r:.160}")
    if not isinstance(row["local_date"], str) \
            or not DATE_RE.match(row["local_date"]):
        raise DeliveryError(f"{where}: bad local_date {row.get('local_date')!r}")
    if not isinstance(row["ward_code"], str) or not row["ward_code"]:
        raise DeliveryError(f"{where}: bad ward_code {row.get('ward_code')!r}")
    vals = [row[k] for k in _MEASURES]
    if any(isinstance(v, bool) or not isinstance(v, int) or v < 0
           for v in vals):
        raise DeliveryError(f"{where}: measures must be non-negative ints")
    if vals[0] != vals[1] + vals[2] + vals[3]:
        raise DeliveryError(
            f"{where}: open_seconds {vals[0]} != empty+single+conflict "
            f"({vals[1]}+{vals[2]}+{vals[3]})")


def _check_key_row(row: Any, where: str) -> None:
    if not isinstance(row, dict) or set(row) != {"local_date", "ward_code"}:
        raise DeliveryError(f"{where}: retract row must be a bare business "
                            f"key, got {row!r:.160}")
    if not isinstance(row["local_date"], str) \
            or not DATE_RE.match(row["local_date"]):
        raise DeliveryError(f"{where}: bad local_date")
    if not isinstance(row["ward_code"], str) or not row["ward_code"]:
        raise DeliveryError(f"{where}: bad ward_code")


def _delivery_path(release_dir: Path, manifest: dict, role: str) -> Path:
    for f in manifest["delivery"]["files"]:
        if f["role"] == role:
            return release_dir / f["path"]
    raise DeliveryError(
        f"manifest lists no delivery file with role {role!r}")


def load_release(db_path: str | Path, release_dir: str | Path) -> dict[str, Any]:
    release_dir = Path(release_dir)
    try:
        manifest = load_release_manifest(release_dir)
    except Exception as exc:
        if isinstance(exc, ManifestError):
            raise
        raise ManifestError(str(exc)) from exc
    problems = verify_release_files(release_dir, manifest)
    if problems:
        raise DeliveryError(
            "delivery verification failed: " + "; ".join(problems))

    series = manifest["series"]
    rel = manifest["release"]
    sid = series["series_id"]
    kind = rel["kind"]

    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        with write_tx(conn):
            state = conn.execute(
                "SELECT * FROM bedload_series WHERE series_id=?",
                (sid,)).fetchone()
            local_digest, local_count = _local_facts(conn, sid)
            if state is None:
                if local_count != 0:
                    raise LocalStateMismatch(
                        f"series {sid[:16]}…: {local_count} local fact rows "
                        "exist without any recorded load; refusing to guess "
                        "their origin")
            elif local_digest != state["facts_sha256"] \
                    or local_count != state["fact_rows"]:
                raise LocalStateMismatch(
                    f"series {sid[:16]}…: recorded head identity "
                    f"{state['facts_sha256'][:12]}…/{state['fact_rows']} rows "
                    f"but the actual partition is {local_digest[:12]}…/"
                    f"{local_count} rows; the local partition was modified "
                    "outside the loader")

            already = conn.execute(
                "SELECT 1 FROM bedload_releases WHERE series_id=? "
                "AND release_id=?", (sid, rel["release_id"])).fetchone()
            if already is not None:
                return _result("already_loaded", sid, rel, local_digest,
                               local_count, 0, 0)

            upserted = retracted = 0
            if kind == "initial":
                if state is not None:
                    raise PartitionNotEmpty(
                        f"series {sid[:16]}… already has a loaded partition; "
                        "an initial release cannot replace it")
                path = _delivery_path(release_dir, manifest, "full")
                for row in _iter_delivery(path):
                    _check_fact_row(row, f"{path.name} line {upserted + 1}")
                    try:
                        conn.execute(
                            "INSERT INTO bed_facts VALUES (?,?,?,?,?,?,?)",
                            (sid, row["local_date"], row["ward_code"],
                             row["open_seconds"], row["empty_seconds"],
                             row["single_seconds"], row["conflict_seconds"]))
                    except sqlite3.IntegrityError as exc:
                        raise DeliveryError(
                            f"duplicate fact key ({row['local_date']}, "
                            f"{row['ward_code']}) in {path.name}") from exc
                    upserted += 1
            else:
                base = rel["base"]
                if state is None:
                    raise BaseMismatch(
                        f"correction {rel['release_id'][:12]}… expects base "
                        f"{base['facts_sha256'][:12]}… but this series was "
                        "never loaded (earlier batches missing)")
                if local_digest != base["facts_sha256"]:
                    raise BaseMismatch(
                        f"correction {rel['release_id'][:12]}… is based on "
                        f"facts {base['facts_sha256'][:12]}… but the local "
                        f"partition is {local_digest[:12]}…; refusing to "
                        "apply a correction onto the wrong base")
                up_path = _delivery_path(release_dir, manifest, "upsert")
                for row in _iter_delivery(up_path):
                    _check_fact_row(row, f"{up_path.name} line {upserted + 1}")
                    conn.execute(
                        "INSERT OR REPLACE INTO bed_facts "
                        "VALUES (?,?,?,?,?,?,?)",
                        (sid, row["local_date"], row["ward_code"],
                         row["open_seconds"], row["empty_seconds"],
                         row["single_seconds"], row["conflict_seconds"]))
                    upserted += 1
                re_path = _delivery_path(release_dir, manifest, "retract")
                for row in _iter_delivery(re_path):
                    _check_key_row(row, f"{re_path.name} line {retracted + 1}")
                    conn.execute(
                        "DELETE FROM bed_facts WHERE series_id=? AND "
                        "local_date=? AND ward_code=?",
                        (sid, row["local_date"], row["ward_code"]))
                    retracted += 1

            new_digest, new_count = _local_facts(conn, sid)
            target = rel["target"]
            if new_digest != target["facts_sha256"] \
                    or new_count != target["fact_rows"]:
                raise TargetMismatch(
                    f"after applying {rel['release_id'][:12]}… the partition "
                    f"identity is {new_digest[:12]}…/{new_count} rows but the "
                    f"manifest targets {target['facts_sha256'][:12]}…/"
                    f"{target['fact_rows']} rows; rolled back")
            conn.execute(
                "INSERT INTO bedload_releases VALUES (?,?,?,?,datetime('now'))",
                (sid, rel["seq"], rel["release_id"],
                 target["facts_sha256"]))
            conn.execute(
                "INSERT INTO bedload_series(series_id, facts_sha256, "
                "fact_rows, head_release_id, head_release_seq, updated_at) "
                "VALUES (?,?,?,?,?,datetime('now')) "
                "ON CONFLICT(series_id) DO UPDATE SET "
                "facts_sha256=excluded.facts_sha256, "
                "fact_rows=excluded.fact_rows, "
                "head_release_id=excluded.head_release_id, "
                "head_release_seq=excluded.head_release_seq, "
                "updated_at=excluded.updated_at",
                (sid, target["facts_sha256"], target["fact_rows"],
                 rel["release_id"], rel["seq"]))
        return _result("loaded", sid, rel, target["facts_sha256"],
                       target["fact_rows"], upserted, retracted)
    finally:
        conn.close()


def _result(status: str, sid: str, rel: dict[str, Any], facts_sha256: str,
            fact_rows: int, upserted: int, retracted: int) -> dict[str, Any]:
    return {
        "status": status,
        "series_id": sid,
        "release_seq": rel["seq"],
        "release_id": rel["release_id"],
        "kind": rel["kind"],
        "facts_sha256": facts_sha256,
        "fact_rows": fact_rows,
        "upserted": upserted,
        "retracted": retracted,
    }


def load_status(db_path: str | Path) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        _ensure_schema(conn)
        series = []
        for s in conn.execute(
                "SELECT * FROM bedload_series ORDER BY series_id"):
            releases = [dict(r) for r in conn.execute(
                "SELECT release_seq, release_id, target_facts_sha256, "
                "loaded_at FROM bedload_releases WHERE series_id=? "
                "ORDER BY release_seq", (s["series_id"],))]
            series.append({
                "series_id": s["series_id"],
                "facts_sha256": s["facts_sha256"],
                "fact_rows": s["fact_rows"],
                "head_release_id": s["head_release_id"],
                "head_release_seq": s["head_release_seq"],
                "releases": releases,
            })
        return {"status": "ok", "db": str(db_path), "series": series}
    finally:
        conn.close()
